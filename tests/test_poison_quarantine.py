"""Fix C (2026-07-23): poison-clip quarantine.

A sub-job that HANGS past its per-sub-job timeout will almost certainly hang
again -- the clip itself is the problem. Retrying it just re-wedges a worker
every ``gig_timeout`` seconds, mesh-wide, until the retry budget is finally
burned. So the SECOND consecutive ``"timeout"`` quarantines it immediately
(fail, no requeue), keyed off the prior ``error`` column (migration-free).

Only the exact ``"timeout"`` label counts -- NOT ``"pool_reset"`` (collateral
in-flight killed alongside a real timeout) and NOT any transient error, so an
innocent clip is never quarantined.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi.jobstore import JobStore  # noqa: E402


def _lease_one(store, jid):
    res = store.lease("r", "h", 10, 60)
    assert any(g["subjob_id"] == jid for g in res.gigs), "expected to lease the pending sub-job"


def _timeout(jid):
    return {"subjob_id": jid, "status": "error", "error": "timeout", "metrics": {}}


# max_retries high so quarantine is clearly the 2nd-timeout rule firing, NOT
# ordinary retry-budget exhaustion.
def test_second_consecutive_timeout_quarantines(tmp_path):
    store = JobStore(str(tmp_path / "q.db"), max_retries=5)
    store.seed([{"subjob_id": "g", "spec": {}}])

    _lease_one(store, "g")
    out = store.complete([_timeout("g")])
    assert out["requeued"] == 1 and out["failed"] == 0
    assert store.job("g")["state"] == "pending"      # first timeout: still retryable

    _lease_one(store, "g")
    out = store.complete([_timeout("g")])
    assert out["failed"] == 1 and out["requeued"] == 0   # second: quarantined
    row = store.job("g")
    assert row["state"] == "failed"
    assert "quarantin" in row["error"].lower()


def test_single_timeout_does_not_quarantine(tmp_path):
    store = JobStore(str(tmp_path / "q.db"), max_retries=5)
    store.seed([{"subjob_id": "g", "spec": {}}])
    _lease_one(store, "g")
    out = store.complete([_timeout("g")])
    assert out["requeued"] == 1 and out["failed"] == 0
    assert store.job("g")["state"] == "pending"


def test_timeout_then_different_error_does_not_quarantine(tmp_path):
    # A timeout followed by a DIFFERENT (transient) error is not the poison
    # signature -- keep retrying, don't quarantine.
    store = JobStore(str(tmp_path / "q.db"), max_retries=5)
    store.seed([{"subjob_id": "g", "spec": {}}])
    _lease_one(store, "g")
    store.complete([_timeout("g")])
    _lease_one(store, "g")
    out = store.complete([{"subjob_id": "g", "status": "error",
                           "error": "connection reset", "metrics": {}}])
    assert out["requeued"] == 1 and out["failed"] == 0
    assert store.job("g")["state"] == "pending"


def test_pool_reset_is_not_treated_as_timeout(tmp_path):
    # "pool_reset" = collateral in-flight killed alongside a real timeout, NOT
    # the hanging clip. Two pool_resets must not quarantine an innocent clip.
    store = JobStore(str(tmp_path / "q.db"), max_retries=5)
    store.seed([{"subjob_id": "g", "spec": {}}])
    for _ in range(2):
        _lease_one(store, "g")
        store.complete([{"subjob_id": "g", "status": "error",
                         "error": "pool_reset", "metrics": {}}])
    assert store.job("g")["state"] == "pending"


def test_two_timeouts_persist_the_tail_log_on_quarantine(tmp_path):
    # The quarantine (failed) path must still persist metrics -- a tail_log from
    # the hang is exactly what an operator needs to see WHY the clip is poison.
    store = JobStore(str(tmp_path / "q.db"), max_retries=5)
    store.seed([{"subjob_id": "g", "spec": {}}])
    _lease_one(store, "g")
    store.complete([_timeout("g")])
    _lease_one(store, "g")
    store.complete([{"subjob_id": "g", "status": "error", "error": "timeout",
                     "metrics": {"tail_log": "Decode Error occurred for picture 41\n...x1000"}}])
    row = store.job("g")
    assert row["state"] == "failed"
    import json
    m = row["metrics"] if isinstance(row["metrics"], dict) else json.loads(row["metrics"] or "{}")
    assert "Decode Error" in json.dumps(m)
