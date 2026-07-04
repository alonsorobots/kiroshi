"""Tests for kiroshi.staging — the mesh-task ABI + budgeted copy.

Covers the pure enumeration logic and the run() task with a fake
ResourceClient (no network, no real Coordinator).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi import staging  # noqa: E402


# ---- enumerate_gigs -----------------------------------------------------

def _make_tree(base: Path):
    base.mkdir(parents=True, exist_ok=True)
    (base / "a.txt").write_text("hello")
    (base / "b.log").write_text("world")
    sub = base / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("deep")
    (sub / "d.log").write_text("deep-log")


def test_enumerate_yields_one_gig_per_file():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in"
        _make_tree(src)
        gigs = list(staging.enumerate_gigs(
            {"from": str(src), "to": "/out", "pattern": "*"}))
        names = sorted(g["subjob_id"] for g in gigs)
        assert names == ["a.txt", "b.log", "sub/c.txt", "sub/d.log"], names


def test_enumerate_pattern_filter():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in"
        _make_tree(src)
        gigs = list(staging.enumerate_gigs(
            {"from": str(src), "to": "/out", "pattern": "*.txt"}))
        names = sorted(g["subjob_id"] for g in gigs)
        assert names == ["a.txt", "sub/c.txt"], names
        assert not any(".log" in n for n in names)


def test_enumerate_embeds_roots_in_spec():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in"
        _make_tree(src)
        gigs = list(staging.enumerate_gigs(
            {"from": str(src), "to": "/cache/out", "pattern": "a.txt"}))
        assert len(gigs) == 1
        spec = gigs[0]["spec"]
        assert spec["read_root"].endswith("in")
        assert spec["write_root"] == "/cache/out"
        assert spec["src_path"] == "a.txt"
        assert spec["dst_path"] == "a.txt"


# ---- run (the mesh task) ------------------------------------------------

def test_run_copies_file_content():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src"
        dst = Path(d) / "dst"
        src.mkdir(); dst.mkdir()
        (src / "f.txt").write_text("payload-123")
        spec = {"src_path": "f.txt", "dst_path": "f.txt",
                "read_root": str(src), "write_root": str(dst)}
        res = staging.run(spec)
        assert res["status"] == "ok"
        assert res["metrics"]["bytes"] == len("payload-123")
        assert (dst / "f.txt").read_text() == "payload-123"


def test_run_is_idempotent_skip():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src"
        dst = Path(d) / "dst"
        src.mkdir(); dst.mkdir()
        (src / "f.txt").write_text("data")
        (dst / "f.txt").write_text("data")      # pre-existing, same size
        spec = {"src_path": "f.txt", "dst_path": "f.txt",
                "read_root": str(src), "write_root": str(dst)}
        res = staging.run(spec)
        assert res["status"] == "skipped"
        assert res["metrics"]["reason"] == "exists"


def test_run_creates_parent_dirs():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src"
        dst = Path(d) / "dst"
        src.mkdir()
        (src / "a").mkdir()
        (src / "a" / "b.txt").write_text("deep")
        spec = {"src_path": "a/b.txt", "dst_path": "a/b.txt",
                "read_root": str(src), "write_root": str(dst)}
        res = staging.run(spec)
        assert res["status"] == "ok"
        assert (dst / "a" / "b.txt").read_text() == "deep"


def test_run_fail_open_without_coordinator():
    """When KIROSHI_FIXER is unset, run() must still copy (fail-open)."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "s"; dst = Path(d) / "d"
        src.mkdir(); dst.mkdir()
        (src / "x.txt").write_text("ok")
        old = os.environ.pop("KIROSHI_FIXER", None)
        try:
            spec = {"src_path": "x.txt", "dst_path": "x.txt",
                    "read_root": str(src), "write_root": str(dst)}
            res = staging.run(spec)
            assert res["status"] == "ok"
        finally:
            if old is not None:
                os.environ["KIROSHI_FIXER"] = old


class _FakeSlot:
    """Records __enter__/__exit__ so tests can prove the slot was actually held."""
    def __init__(self, label, disk, mode):
        self.label = label; self.disk = disk; self.mode = mode
        self.entered = False; self.exited = False
    def __enter__(self):
        self.entered = True
        return self
    def __exit__(self, *exc):
        self.exited = True


class _FakeResourceClient:
    """Drop-in for ResourceClient that records acquire() calls."""
    def __init__(self):
        self.calls = []     # list of (disk, mode, slot)
    def acquire(self, disk=None, mode="read", timeout=None):
        slot = _FakeSlot(f"{mode}:{disk}", disk, mode)
        self.calls.append((disk, mode, slot))
        return slot


def test_run_actually_acquires_and_releases_slots():
    """The bug this catches: acquire() returns a context manager whose __enter__
    does the POST /resource/acquire. If run() never enters the CM, the Coordinator
    never sees the slot and mesh budgeting is silently disabled."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "s"; dst = Path(d) / "d"
        src.mkdir(); dst.mkdir()
        (src / "f.txt").write_text("data")
        spec = {"src_path": "f.txt", "dst_path": "f.txt",
                "read_root": str(src), "write_root": str(dst),
                "disk": "disk3"}
        fake = _FakeResourceClient()
        # inject the fake client by monkeypatching ResourceClient in staging
        orig = staging.ResourceClient
        staging.ResourceClient = lambda *_a, **_kw: fake
        os.environ["KIROSHI_FIXER"] = "http://fake:9999"
        try:
            res = staging.run(spec)
        finally:
            staging.ResourceClient = orig
            os.environ.pop("KIROSHI_FIXER", None)
        assert res["status"] == "ok"
        # must have called acquire twice: read with disk="disk3", write with no disk
        assert len(fake.calls) == 2
        read_disk, read_mode, read_slot = fake.calls[0]
        write_disk, write_mode, write_slot = fake.calls[1]
        assert read_disk == "disk3" and read_mode == "read"
        assert write_mode == "write"
        # the critical assertion: __enter__ was called (the POST happened)
        assert read_slot.entered, "read slot __enter__ was never called — budget disabled!"
        assert write_slot.entered, "write slot __enter__ was never called — budget disabled!"
        # and __exit__ was called (the release happened)
        assert read_slot.exited and write_slot.exited


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fail = 0
    for t in tests:
        try:
            t(); print(f"PASS  {t.__name__}")
        except Exception as exc:
            print(f"FAIL  {t.__name__}: {exc!r}"); fail += 1
    print(f"\n{len(tests)-fail}/{len(tests)} passed")
    sys.exit(fail)
