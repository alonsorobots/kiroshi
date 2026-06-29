"""Real Kiroshi task: SLERP fps-resampling of canonical-quaternion motion clips.

This is the genuine, CPU-bound, embarrassingly-parallel workload Kiroshi was built
to run (Stage B of a motion-tokenization pipeline). Each gig resamples one clip's
per-joint unit quaternions from its source timebase to a uniform target FPS using
piecewise spherical-linear interpolation (SLERP), then writes the result back to
the NAS atomically.

Why SLERP (not nearest-neighbor decimation): rotations live on a sphere; linear
interpolation of quaternion components warps angular velocity. SLERP gives constant
angular velocity between key orientations — important for fast joints (e.g. hands).

Input  npz (under KIROSHI_READ_ROOT): keys
    quat      float (T, J, 4)   canonical quaternions, wxyz, qw >= 0
    is_valid  bool  (T,)        optional per-frame validity mask
    times     float (T,)        optional source timestamps (seconds); else use src_fps
    fps       float scalar      optional source fps fallback
Output npz (under KIROSHI_WRITE_ROOT): quat (Tt, J, 4), is_valid (Tt,), fps, n_in, n_out

Resume is free: if the destination already exists, the gig returns "skipped".

Spec fields:
    src_path     str   read-root-relative (or absolute) input npz
    dst_path     str   write-root-relative (or absolute) output npz
    target_fps   float target frame rate (e.g. 4 or 8)
    src_fps      float optional source fps (used only if npz lacks `times`/`fps`)
    quat_key     str   default "quat"
    valid_key    str   default "is_valid"

Dependencies: numpy only (declared as the `motion` extra).

Enumerate gigs for seeding::

    python -m examples.motion_resample --read-root <dir> --fps 4 --fps 8 > gigs.jsonl
    kiroshi seed --fixer http://<host>:8787 --jobs gigs.jsonl
"""
from __future__ import annotations

import fnmatch
import io
import os
from pathlib import PurePosixPath
from typing import Any, Iterator, Optional

import numpy as np

from kiroshi import kfs
from kiroshi import paths as kpaths

_EPS = 1e-8


# --------------------------------------------------------------------------- math
def slerp_batch(q0: np.ndarray, q1: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Vectorized SLERP. q0, q1: (N, J, 4) wxyz unit quats; w: (N,) in [0,1].

    Returns (N, J, 4). Falls back to LERP for nearly-parallel pairs.
    """
    w = w[:, None, None]  # (N,1,1)
    dot = np.sum(q0 * q1, axis=-1, keepdims=True)  # (N,J,1)
    # take the shorter arc
    q1 = np.where(dot < 0.0, -q1, q1)
    dot = np.abs(dot)
    dot = np.clip(dot, -1.0, 1.0)
    theta = np.arccos(dot)
    sin_theta = np.sin(theta)
    small = sin_theta < 1e-6
    # SLERP weights where well-conditioned, LERP weights where nearly parallel
    with np.errstate(invalid="ignore", divide="ignore"):
        s0 = np.where(small, 1.0 - w, np.sin((1.0 - w) * theta) / sin_theta)
        s1 = np.where(small, w, np.sin(w * theta) / sin_theta)
    out = s0 * q0 + s1 * q1
    norm = np.linalg.norm(out, axis=-1, keepdims=True)
    out = out / np.maximum(norm, _EPS)
    # keep canonical hemisphere (qw >= 0)
    out = np.where(out[..., 0:1] < 0.0, -out, out)
    return out


def resample_quaternions(
    quat: np.ndarray,
    src_times: np.ndarray,
    target_fps: float,
    is_valid: Optional[np.ndarray] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Resample (T, J, 4) quats from `src_times` to a uniform `target_fps` grid.

    Validity uses the NN-invalid-block rule: a target frame is valid only if both
    bracketing source frames are valid. Returns (quat_out, valid_out, tgt_times).
    """
    quat = np.asarray(quat, dtype=np.float64)
    src_times = np.asarray(src_times, dtype=np.float64)
    T = quat.shape[0]
    if T == 0:
        raise ValueError("empty clip")
    if T == 1:
        return quat.copy(), np.ones(1, dtype=bool), src_times[:1].copy()

    duration = float(src_times[-1] - src_times[0])
    n_out = max(1, int(np.floor(duration * target_fps)) + 1)
    tgt_times = src_times[0] + np.arange(n_out) / float(target_fps)
    tgt_times = np.clip(tgt_times, src_times[0], src_times[-1])

    idx = np.searchsorted(src_times, tgt_times, side="right")
    i1 = np.clip(idx, 1, T - 1)
    i0 = i1 - 1
    denom = src_times[i1] - src_times[i0]
    w = np.where(denom > _EPS, (tgt_times - src_times[i0]) / np.maximum(denom, _EPS), 0.0)
    w = np.clip(w, 0.0, 1.0)

    quat_out = slerp_batch(quat[i0], quat[i1], w).astype(np.float32)

    if is_valid is None:
        valid_out = np.ones(n_out, dtype=bool)
    else:
        is_valid = np.asarray(is_valid, dtype=bool)
        valid_out = is_valid[i0] & is_valid[i1]
    return quat_out, valid_out, tgt_times


# --------------------------------------------------------------------------- task
def _resolve(path: str, root: Optional[str], env_name: str) -> str:
    """Resolve a gig-supplied path *confined to its root*.

    ``root`` is the per-gig root (the disk's direct/cached share for topology-aware
    gigs, else the env ``KIROSHI_READ_ROOT``/``KIROSHI_WRITE_ROOT``) — see
    :func:`kiroshi.paths.gig_read_root`. The confinement itself (no absolute paths,
    no ``..`` traversal, pure path arithmetic so an SMB UNC is never touched via the
    OS redirector) lives in :func:`kiroshi.paths.confined_join` and is shared by
    every task. SECURITY: ``spec.src_path``/``dst_path`` are untrusted.
    """
    if not root:
        raise ValueError(
            f"{env_name} is not set and the gig carries no per-disk root; "
            f"refusing to resolve an unconfined path {path!r}"
        )
    return kpaths.confined_join(root, path)


def _targets(spec: dict[str, Any]) -> list[tuple[float, str]]:
    """Normalize the gig's output targets to ``[(target_fps, resolved_dst), ...]``.

    A gig may carry several ``targets`` (e.g. 4 and 8 fps) so a single, expensive
    source read serves *all* of them — on a seek-bound source disk the read, not
    the SLERP, dominates, so amortizing it across fps roughly halves total I/O.
    The legacy single-fps form (``target_fps`` + ``dst_path``) is still accepted.
    """
    raw = spec.get("targets") or [{"target_fps": spec["target_fps"], "dst_path": spec["dst_path"]}]
    wroot = kpaths.gig_write_root(spec)
    return [(float(t["target_fps"]), _resolve(t["dst_path"], wroot, "KIROSHI_WRITE_ROOT"))
            for t in raw]


def run(spec: dict[str, Any]) -> dict[str, Any]:
    src = _resolve(spec["src_path"], kpaths.gig_read_root(spec), "KIROSHI_READ_ROOT")
    quat_key = spec.get("quat_key", "quat")
    valid_key = spec.get("valid_key", "is_valid")

    targets = _targets(spec)
    # Resume is per-target: only produce outputs that don't already exist, so a
    # gig that crashed after writing some fps re-does only the rest.
    pending = [(fps, dst) for (fps, dst) in targets if not kfs.exists(dst)]
    if not pending:
        return {"status": "skipped", "metrics": {"reason": "exists", "targets": len(targets)}}

    # Pull the whole npz in ONE sequential streaming read. np.load on a network
    # handle issues many tiny seek+read round trips (it parses the zip central
    # directory at EOF, then re-seeks per member), which pins the source disk
    # under concurrency. Reading the bytes once and parsing from RAM removes all
    # those round trips. NpzFile is lazy, so materialize arrays before discarding.
    with kfs.open(src, "rb") as fh:
        raw = fh.read()
    with np.load(io.BytesIO(raw), allow_pickle=False) as data:
        quat = np.asarray(data[quat_key])
        # Real corpora contain occasional zero-frame clips. That's benign data,
        # not a task failure — skip it (don't burn Fixer retries).
        if quat.shape[0] == 0:
            return {"status": "skipped", "metrics": {"reason": "empty_input"}}
        is_valid = np.asarray(data[valid_key]) if valid_key in data.files else None
        if "times" in data.files:
            src_times = np.asarray(data["times"], dtype=np.float64)
        else:
            src_fps = float(spec.get("src_fps") or (data["fps"] if "fps" in data.files else 0.0))
            if src_fps <= 0:
                raise ValueError(
                    f"no source timebase for {src} (need npz 'times'/'fps' or spec.src_fps)"
                )
            src_times = np.arange(quat.shape[0], dtype=np.float64) / src_fps

    n_in = int(quat.shape[0])
    frames_out: dict[str, int] = {}
    for target_fps, dst in pending:
        quat_out, valid_out, _tgt = resample_quaternions(quat, src_times, target_fps, is_valid)
        # Serialize to RAM, then write the whole payload in one streaming write
        # (symmetric reason: avoid per-chunk SMB write round trips).
        buf = io.BytesIO()
        np.savez(
            buf,
            quat=quat_out,
            is_valid=valid_out,
            fps=np.float32(target_fps),
            n_in=np.int64(n_in),
            n_out=np.int64(quat_out.shape[0]),
        )
        with kfs.atomic_write(dst) as fh:
            fh.write(buf.getvalue())
        frames_out[f"{target_fps:g}fps"] = int(quat_out.shape[0])

    return {"status": "ok", "metrics": {"frames_in": n_in, "frames_out": frames_out}}


# ----------------------------------------------------------------------- seeding
def _rel_posix(full: str, base: str) -> str:
    """Root-relative POSIX path of ``full`` under ``base`` (separator-agnostic)."""
    f = str(full).replace("\\", "/").rstrip("/")
    b = str(base).replace("\\", "/").rstrip("/")
    return f[len(b):].lstrip("/") if f.startswith(b) else f


def _walk_matches(root: str, pattern: str) -> list[str]:
    """List files under ``root`` matching ``pattern``, via :func:`kfs.walk`.

    ``Path.glob("**/...")`` silently returns *nothing* on a Windows UNC root,
    and ``os.walk`` over a UNC path fails entirely from an SSH/service network
    logon. :func:`kfs.walk` uses ``smbprotocol`` for credentialed SMB shares and
    ``os.walk`` otherwise, so seeding works from every context.

    We support the common ``**/<fileglob>`` form (match the filename at any
    depth) and otherwise match the root-relative POSIX path. Results are sorted
    so ``job_id``s are deterministic and re-seeding stays idempotent.
    """
    if pattern.startswith("**/"):
        filepat = pattern[3:]

        def keep(rel: str) -> bool:
            return fnmatch.fnmatch(PurePosixPath(rel).name, filepat)
    else:

        def keep(rel: str) -> bool:
            return fnmatch.fnmatch(rel, pattern)

    base = str(root).rstrip("/\\")
    sep = "\\" if kfs.is_unc(root) else os.sep
    out: list[str] = []
    for dirpath, _dirs, files in kfs.walk(root):
        for fn in files:
            full = str(dirpath).rstrip("/\\") + sep + fn
            rel = _rel_posix(full, base)
            if keep(rel):
                out.append(full)
    out.sort(key=lambda p: _rel_posix(p, base))
    return out


def _fps_list(raw: Any) -> list[float]:
    """Coerce the `fps` arg (scalar / repeated-list / absent) to a float list."""
    if raw in (None, True):
        return [4.0, 8.0]  # the project's two canonical targets
    seq = raw if isinstance(raw, list) else [raw]
    return [float(x) for x in seq]


def enumerate_gigs(args: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Kiroshi enumeration contract (PLAN §7.5): ``args -> gigs``.

    Produces **one gig per clip** carrying every fps as a ``targets`` entry, so
    the (seek-bound) source npz is read once and resampled to all target fps —
    far cheaper on a NAS than one gig per (clip, fps). This is exactly the fan-out
    a generic ``--items`` globber can't express, which is why the task owns it.

    Recognized ``args`` (from the tokens after ``--`` on the ``kiroshi run`` line):
        read_root   root to enumerate under (else ``KIROSHI_READ_ROOT`` env)
        fps         target fps; repeat for several (``--fps 4 --fps 8``); default 4,8
        pattern     glob (default ``**/*.npz``)
        group       optional campaign slug
    Run it::

        kiroshi run examples.motion_resample:run --enumerate \\
            --read-root //nas/clips --write-root //nas/out \\
            --label "Seamless 30fps -> 4,8 fps" -- --fps 4 --fps 8
    """
    read_root = args.get("read_root") or os.environ.get("KIROSHI_READ_ROOT")
    if not read_root:
        raise ValueError(
            "motion enumerate needs a read root: pass --read-root or set "
            "KIROSHI_READ_ROOT"
        )
    fps_list = _fps_list(args.get("fps"))
    pattern = args.get("pattern") or "**/*.npz"
    out_tmpl = args.get("out_subdir_tmpl") or "resampled_{fps:g}fps"
    group = args.get("group")

    base = str(read_root).rstrip("/\\")
    for full in _walk_matches(read_root, pattern):
        rel = _rel_posix(full, base)
        targets = [
            {"target_fps": fps, "dst_path": f"{out_tmpl.format(fps=fps)}/{rel}"}
            for fps in fps_list
        ]
        gig: dict[str, Any] = {"job_id": rel, "spec": {"src_path": rel, "targets": targets}}
        if group:
            gig["group"] = group
        yield gig


def _cli() -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="Emit motion-resample gigs as JSONL (for `kiroshi seed --jobs`).")
    ap.add_argument("--read-root", required=True, help="Root dir to enumerate *.npz under.")
    ap.add_argument("--fps", type=float, action="append", required=True, help="Target fps (repeatable).")
    ap.add_argument("--pattern", default="**/*.npz")
    ap.add_argument("--group", default=None, help="Optional campaign slug.")
    args = ap.parse_args()
    n = 0
    enum_args = {"read_root": args.read_root, "fps": args.fps,
                 "pattern": args.pattern, "group": args.group}
    for gig in enumerate_gigs(enum_args):
        sys.stdout.write(json.dumps(gig) + "\n")
        n += 1
    sys.stderr.write(f"emitted {n} gigs\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
