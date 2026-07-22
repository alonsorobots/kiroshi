"""jobstore.complete()'s error/timeout branch previously dropped `metrics`
entirely -- only the `error` string was persisted. This silently defeated
per-sub-job output capture (subjob_capture.py) for exactly the cases it
exists to explain (a crashed/timed-out sub-job's tail_log). Pin the fix so
it can't silently regress.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.jobstore import JobStore  # noqa: E402


def test_metrics_persisted_on_permanent_failure(tmp_path):
    store = JobStore(str(tmp_path / "m.db"), max_retries=0)  # 0 -> fails immediately
    store.seed([{"subjob_id": "g1", "spec": {}}])
    store.lease("r1", "host", 10, 60)

    store.complete([{
        "subjob_id": "g1", "status": "error", "error": "timeout",
        "metrics": {"tail_log": "print-line\nraw-fd-line\n"},
    }])

    row = store.job("g1")
    assert row["state"] == "failed"
    assert row["error"] == "timeout"
    assert row["metrics"]["tail_log"] == "print-line\nraw-fd-line\n"


def test_metrics_persisted_on_requeue_after_transient_error(tmp_path):
    store = JobStore(str(tmp_path / "m2.db"), max_retries=3)
    store.seed([{"subjob_id": "g2", "spec": {}}])
    store.lease("r1", "host", 10, 60)

    store.complete([{
        "subjob_id": "g2", "status": "error", "error": "BrokenProcessPool",
        "metrics": {"tail_log": "partial output before crash"},
    }])

    row = store.job("g2")
    assert row["state"] == "pending"  # under max_retries -> requeued, not failed
    assert row["metrics"]["tail_log"] == "partial output before crash"


def test_export_metrics_includes_tail_log_for_failed_subjob(tmp_path):
    store = JobStore(str(tmp_path / "m3.db"), max_retries=0)
    store.seed([{"subjob_id": "g3", "spec": {}, "job": "myjob"}])
    store.lease("r1", "host", 10, 60, job="myjob")
    store.complete([{
        "subjob_id": "g3", "status": "error", "error": "timeout",
        "metrics": {"tail_log": "hello"},
    }])
    rows = store.export_metrics(job="myjob", states=("failed",))
    assert len(rows) == 1
    assert rows[0]["metrics"]["tail_log"] == "hello"
