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

from examples.motion_resample import resample_quaternions, run, slerp_batch  # noqa: E402


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
