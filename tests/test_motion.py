"""Round-trip correctness for the SLERP motion-resample task.

Skipped automatically where numpy isn't installed. Validates that:
  - downsample -> upsample recovers a smooth rotation within a tight geodesic bound
  - the validity (NN-invalid-block) rule behaves
  - the task writes a real .npz atomically and is resume-safe (skips on re-run)
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
for _p in (SRC, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

np = pytest.importorskip("numpy")

from examples.motion_resample import (  # noqa: E402
    enumerate_gigs,
    resample_quaternions,
    run,
    slerp_batch,
)


def _axis_angle_quat(axis, angle):
    axis = np.asarray(axis, dtype=np.float64)
    axis = axis / np.linalg.norm(axis)
    h = angle / 2.0
    q = np.array([np.cos(h), *(np.sin(h) * axis)])
    if q[0] < 0:
        q = -q
    return q


def _geodesic_deg(qa, qb):
    d = np.clip(abs(float(np.dot(qa, qb))), -1.0, 1.0)
    return np.degrees(2.0 * np.arccos(d))


def test_slerp_midpoint_constant_velocity():
    # rotating about Z from 0 -> 90 deg; midpoint must be exactly 45 deg
    q0 = _axis_angle_quat([0, 0, 1], 0.0)[None, None, :]
    q1 = _axis_angle_quat([0, 0, 1], np.pi / 2)[None, None, :]
    mid = slerp_batch(q0, q1, np.array([0.5]))[0, 0]
    expect = _axis_angle_quat([0, 0, 1], np.pi / 4)
    assert _geodesic_deg(mid, expect) < 1e-3


def test_resample_smooth_rotation_roundtrip():
    # 5 s of smooth rotation about a tilted axis at 30 fps
    T = 150
    src_fps = 30.0
    t = np.arange(T) / src_fps
    axis = np.array([1.0, 0.5, -0.3])
    quat = np.stack([_axis_angle_quat(axis, 2.0 * np.pi * 0.2 * ti) for ti in t])[:, None, :]
    out, valid, tgt = resample_quaternions(quat, t, target_fps=8.0)
    # reconstruct ground-truth at target times and compare
    gt = np.stack([_axis_angle_quat(axis, 2.0 * np.pi * 0.2 * ti) for ti in tgt])
    errs = [_geodesic_deg(out[i, 0], gt[i]) for i in range(out.shape[0])]
    assert max(errs) < 1.0, f"max geodesic err {max(errs):.3f} deg"
    assert valid.all()


def test_validity_block_rule():
    T = 10
    t = np.arange(T) / 10.0
    quat = np.tile(_axis_angle_quat([0, 0, 1], 0.1), (T, 1))[:, None, :]
    is_valid = np.ones(T, dtype=bool)
    is_valid[4:6] = False  # an invalid block in the middle
    _out, valid, _tgt = resample_quaternions(quat, t, target_fps=10.0, is_valid=is_valid)
    assert not valid.all() and valid.any()


def test_run_writes_and_resumes(tmp_path):
    rroot = tmp_path / "in"
    wroot = tmp_path / "out"
    rroot.mkdir()
    T = 60
    t = np.arange(T) / 30.0
    quat = np.stack([_axis_angle_quat([0, 1, 0], 0.05 * i) for i in range(T)])[:, None, :]
    np.savez(rroot / "clip.npz", quat=quat.astype(np.float32), times=t)

    os.environ["KIROSHI_READ_ROOT"] = str(rroot)
    os.environ["KIROSHI_WRITE_ROOT"] = str(wroot)
    spec = {"src_path": "clip.npz", "dst_path": "r8/clip.npz", "target_fps": 8.0}

    r1 = run(spec)
    assert r1["status"] == "ok", r1
    assert (wroot / "r8" / "clip.npz").exists()

    r2 = run(spec)  # resume: output exists -> skipped
    assert r2["status"] == "skipped", r2


def test_enumerate_gigs_walks_nested_tree(tmp_path):
    # Seeding must find clips at any depth and be deterministic/idempotent. This
    # uses os.walk (not Path.glob('**')) because the latter returns nothing on a
    # Windows UNC root — which would silently emit zero gigs from a NAS.
    root = tmp_path / "canonical"
    rels = [
        "improvised/dev/0000/a.npz",
        "improvised/train/0001/b.npz",
        "naturalistic/c.npz",
        "top.npz",
    ]
    for rel in rels:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    (root / "note.txt").write_bytes(b"ignore me")  # non-.npz must be skipped

    enum_args = {"read_root": str(root), "fps": [4.0, 8.0]}
    gigs = list(enumerate_gigs(enum_args))
    srcs = sorted({g["spec"]["src_path"] for g in gigs})
    assert srcs == sorted(rels)                       # all npz, no txt
    assert len(gigs) == len(rels)                     # ONE gig per clip (targets fan-out)
    for g in gigs:                                     # each carries both fps targets
        fps_set = {t["target_fps"] for t in g["spec"]["targets"]}
        assert fps_set == {4.0, 8.0}
    # deterministic ordering + ids => re-seeding is idempotent
    assert [g["subjob_id"] for g in gigs] == [g["subjob_id"] for g in enumerate_gigs(enum_args)]
    assert gigs[0]["spec"]["targets"][0]["dst_path"] == \
        "resampled_4fps/" + gigs[0]["spec"]["src_path"]


def test_run_multi_target_one_read_two_outputs(tmp_path):
    # A gig may carry several fps `targets`: the source is read ONCE and every
    # output is produced from it (halves source-disk reads on the real corpus).
    # Per-target resume must also work (skip only the outputs that already exist).
    rroot = tmp_path / "in"
    wroot = tmp_path / "out"
    rroot.mkdir()
    T = 60
    t = np.arange(T) / 30.0
    quat = np.stack([_axis_angle_quat([0, 1, 0], 0.05 * i) for i in range(T)])[:, None, :]
    np.savez(rroot / "clip.npz", quat=quat.astype(np.float32), times=t)

    os.environ["KIROSHI_READ_ROOT"] = str(rroot)
    os.environ["KIROSHI_WRITE_ROOT"] = str(wroot)
    spec = {
        "src_path": "clip.npz",
        "targets": [
            {"target_fps": 4.0, "dst_path": "r4/clip.npz"},
            {"target_fps": 8.0, "dst_path": "r8/clip.npz"},
        ],
    }

    r1 = run(spec)
    assert r1["status"] == "ok", r1
    assert set(r1["metrics"]["frames_out"]) == {"4fps", "8fps"}
    assert (wroot / "r4" / "clip.npz").exists()
    assert (wroot / "r8" / "clip.npz").exists()

    # delete only the 8fps output -> rerun must redo just that one target
    (wroot / "r8" / "clip.npz").unlink()
    r2 = run(spec)
    assert r2["status"] == "ok", r2
    assert list(r2["metrics"]["frames_out"]) == ["8fps"], r2

    r3 = run(spec)  # both exist now -> fully skipped
    assert r3["status"] == "skipped", r3


def test_run_skips_empty_clip(tmp_path):
    # Real corpora contain zero-frame clips; these must skip cleanly (not fail +
    # burn retries). Found running the real seamless_interaction corpus.
    rroot = tmp_path / "in"
    wroot = tmp_path / "out"
    rroot.mkdir()
    empty = np.zeros((0, 52, 4), dtype=np.float32)
    np.savez(rroot / "empty.npz", quat=empty)
    os.environ["KIROSHI_READ_ROOT"] = str(rroot)
    os.environ["KIROSHI_WRITE_ROOT"] = str(wroot)
    r = run({"src_path": "empty.npz", "dst_path": "r8/empty.npz",
             "target_fps": 8.0, "src_fps": 30.0})
    assert r["status"] == "skipped" and r["metrics"]["reason"] == "empty_input", r
