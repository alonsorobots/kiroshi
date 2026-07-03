"""Tests for kiroshi.staging — the mesh-task ABI + budgeted copy.

Covers the pure enumeration logic and the run() task with a fake
ResourceClient (no network, no real Fixer).
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
        names = sorted(g["job_id"] for g in gigs)
        assert names == ["a.txt", "b.log", "sub/c.txt", "sub/d.log"], names


def test_enumerate_pattern_filter():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in"
        _make_tree(src)
        gigs = list(staging.enumerate_gigs(
            {"from": str(src), "to": "/out", "pattern": "*.txt"}))
        names = sorted(g["job_id"] for g in gigs)
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


def test_run_fail_open_without_fixer():
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
