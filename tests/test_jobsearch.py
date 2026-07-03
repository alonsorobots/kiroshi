"""Tests for regex job search — REGEXP function + list_jobs filters.

Uses a real (tmp-file) JobStore so the REGEXP function is exercised on a
live sqlite connection, matching production behavior.
"""
from __future__ import annotations

import contextlib
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.jobstore import JobStore  # noqa: E402


@contextlib.contextmanager
def _store_ctx():
    """Create a seeded JobStore in a temp dir; close it cleanly on exit so
    Windows doesn't complain about the sqlite file being locked on cleanup."""
    with tempfile.TemporaryDirectory() as d:
        store = _make_store(d)
        try:
            yield store
        finally:
            store.close()


def _make_store(tmpdir: str) -> JobStore:
    """Create a fresh JobStore with a few seeded jobs for regex testing."""
    store = JobStore(str(Path(tmpdir) / "test_jobs.db"))
    gigs = [
        {"job_id": "shard_01/a/1/CLIP_P089.npz", "spec": {}, "group": "camp-a"},
        {"job_id": "shard_01/a/2/CLIP_P08x.npz", "spec": {}, "group": "camp-a"},
        {"job_id": "shard_03/b/1/CLIP_P089.npz", "spec": {}, "group": "camp-b"},
        {"job_id": "shard_03/b/2/CLIP_P100.npz", "spec": {}, "group": "camp-b"},
        {"job_id": "shard_07/c/1/CLIP_P050.npz", "spec": {}, "group": "camp-b"},
    ]
    store.seed(gigs, group="mixed")
    # complete two of them so we can filter by state
    store.complete([
        {"job_id": "shard_01/a/1/CLIP_P089.npz", "status": "ok", "metrics": {}},
        {"job_id": "shard_03/b/1/CLIP_P089.npz", "status": "ok", "metrics": {}},
    ])
    # fail one with an error message
    store.complete([
        {"job_id": "shard_03/b/2/CLIP_P100.npz", "status": "error",
         "error": "PermissionError: access denied", "metrics": {}},
    ])
    return store


def test_regexp_matches_job_id_prefix():
    with _store_ctx() as store:
        rows = store.list_jobs(job_id_re="^shard_03/")
        ids = sorted(r["job_id"] for r in rows)
        assert len(ids) == 2, ids
        assert all(i.startswith("shard_03/") for i in ids)


def test_regexp_matches_pattern_in_filename():
    with _store_ctx() as store:
        rows = store.list_jobs(job_id_re="P089")
        ids = sorted(r["job_id"] for r in rows)
        assert len(ids) == 2, ids          # shard_01/.../P089 + shard_03/.../P089
        assert all("P089" in i for i in ids)


def test_regexp_anchors_dont_match_too_broadly():
    with _store_ctx() as store:
        rows = store.list_jobs(job_id_re="P089\\.npz$")
        ids = [r["job_id"] for r in rows]
        assert len(ids) == 2              # only the two that END with P089.npz
        assert "shard_01/a/2/CLIP_P08x.npz" not in ids


def test_error_re_filters_on_error_column():
    with _store_ctx() as store:
        rows = store.list_jobs(error_re="PermissionError")
        ids = [r["job_id"] for r in rows]
        assert len(ids) == 1
        assert ids[0] == "shard_03/b/2/CLIP_P100.npz"


def test_combined_with_state_filter():
    with _store_ctx() as store:
        # all done jobs matching shard_01
        rows = store.list_jobs(states=("done",), job_id_re="^shard_01/")
        ids = [r["job_id"] for r in rows]
        assert ids == ["shard_01/a/1/CLIP_P089.npz"]


def test_combined_with_group_filter():
    with _store_ctx() as store:
        rows = store.list_jobs(grp="camp-b", job_id_re="^shard_03/")
        ids = sorted(r["job_id"] for r in rows)
        assert len(ids) == 2              # camp-b has two shard_03 gigs


def test_bad_regex_raises_re_error_not_sqlite_crash():
    with _store_ctx() as store:
        try:
            store.list_jobs(job_id_re="(")         # unbalanced paren
            assert False, "should have raised re.error"
        except re.error:
            pass                                  # clean — NOT a sqlite error


def test_no_regex_returns_all_as_before():
    with _store_ctx() as store:
        rows = store.list_jobs()
        assert len(rows) == 5                      # all 5 gigs, no filter


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
