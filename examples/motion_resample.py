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

import os
from pathlib import Path
from typing import Any, Iterator, Optional

import numpy as np

from kiroshi.atomic import atomic_path

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
def _resolve(path: str, env_root: str) -> Path:
    """Resolve a gig-supplied path *confined to its configured root*.

    SECURITY: ``spec.src_path``/``dst_path`` come from whoever seeded the gig, so
    they are untrusted input. We refuse absolute paths and any ``..`` traversal
    that would escape ``KIROSHI_READ_ROOT`` / ``KIROSHI_WRITE_ROOT`` — otherwise a
    malicious (or buggy) spec could make the Runner read/write anywhere its
    account can reach (e.g. overwrite a Startup script). A task that genuinely
    needs unconfined paths must opt in explicitly; the default is locked down.
    """
    root = os.environ.get(env_root)
    if not root:
        raise ValueError(
            f"{env_root} is not set; refusing to resolve an unconfined path {path!r}"
        )
    root_p = Path(root).resolve()
    p = Path(path)
    if p.is_absolute() or p.drive or p.anchor:
        raise ValueError(f"absolute path not allowed for a gig: {path!r}")
    full = (root_p / p).resolve()
    try:
        full.relative_to(root_p)
    except ValueError:
        raise ValueError(
            f"path {path!r} escapes its root {env_root}={root!r}"
        ) from None
    return full


def run(spec: dict[str, Any]) -> dict[str, Any]:
    target_fps = float(spec["target_fps"])
    src = _resolve(spec["src_path"], "KIROSHI_READ_ROOT")
    dst = _resolve(spec["dst_path"], "KIROSHI_WRITE_ROOT")
    quat_key = spec.get("quat_key", "quat")
    valid_key = spec.get("valid_key", "is_valid")

    if dst.exists():
        return {"status": "skipped", "metrics": {"reason": "exists"}}

    with np.load(src, allow_pickle=False) as data:
        quat = data[quat_key]
        is_valid = data[valid_key] if valid_key in data.files else None
        if "times" in data.files:
            src_times = np.asarray(data["times"], dtype=np.float64)
        else:
            src_fps = float(spec.get("src_fps") or (data["fps"] if "fps" in data.files else 0.0))
            if src_fps <= 0:
                raise ValueError(f"no source timebase for {src} (need npz 'times'/'fps' or spec.src_fps)")
            src_times = np.arange(quat.shape[0], dtype=np.float64) / src_fps

    quat_out, valid_out, _tgt = resample_quaternions(quat, src_times, target_fps, is_valid)

    with atomic_path(dst) as tmp:
        # Pass a file handle so np.savez doesn't append ".npz" to the temp name
        # (which would break the atomic rename).
        with open(tmp, "wb") as fh:
            np.savez(
                fh,
                quat=quat_out,
                is_valid=valid_out,
                fps=np.float32(target_fps),
                n_in=np.int64(quat.shape[0]),
                n_out=np.int64(quat_out.shape[0]),
            )

    return {
        "status": "ok",
        "metrics": {"frames_in": int(quat.shape[0]), "frames_out": int(quat_out.shape[0])},
    }


# ----------------------------------------------------------------------- seeding
def enumerate_gigs(
    read_root: str,
    fps_list: list[float],
    pattern: str = "**/*.npz",
    out_subdir_tmpl: str = "resampled_{fps:g}fps",
) -> Iterator[dict[str, Any]]:
    """Yield one gig per (clip, fps). job_id is deterministic so re-seeding is safe."""
    root = Path(read_root)
    for p in sorted(root.glob(pattern)):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        for fps in fps_list:
            dst = f"{out_subdir_tmpl.format(fps=fps)}/{rel}"
            yield {
                "job_id": f"{rel}|{fps:g}",
                "spec": {"src_path": rel, "dst_path": dst, "target_fps": fps},
            }


def _cli() -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="Emit motion-resample gigs as JSONL (for `kiroshi seed --jobs`).")
    ap.add_argument("--read-root", required=True, help="Root dir to enumerate *.npz under.")
    ap.add_argument("--fps", type=float, action="append", required=True, help="Target fps (repeatable).")
    ap.add_argument("--pattern", default="**/*.npz")
    args = ap.parse_args()
    n = 0
    for gig in enumerate_gigs(args.read_root, args.fps, args.pattern):
        sys.stdout.write(json.dumps(gig) + "\n")
        n += 1
    sys.stderr.write(f"emitted {n} gigs\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
