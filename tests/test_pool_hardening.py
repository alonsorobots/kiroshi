"""Tests for M7 — Runner pool hardening.

Covers: the ``status="requeue"`` result (eviction returns a gig to pending WITHOUT
burning the retry budget), and the pool's abort-with-eviction via ``pause_cb``
(not-yet-started gigs are cancelled + reported as ``requeue``; a running gig is
left to finish).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi.jobstore import JobStore  # noqa: E402


# --------------------------------------------------- requeue status (jobstore)
def test_requeue_status_returns_to_pending_without_burning_retries(tmp_path):
    store = JobStore(str(tmp_path / "r.db"), max_retries=2)
    store.seed([{"subjob_id": "g1", "spec": {}}, {"subjob_id": "g2", "spec": {}}])
    # lease both so they're "leased"
    res = store.lease("r1", "host", 10, 60)
    jids = {g["subjob_id"] for g in res.gigs}
    assert jids == {"g1", "g2"}

    # evict g1 (requeue) — must go back to pending, attempts untouched
    # fail g2 (error) — must consume a retry (attempts++) and also go pending
    out = store.complete([
        {"subjob_id": "g1", "status": "requeue", "error": "evicted: pause", "metrics": {}},
        {"subjob_id": "g2", "status": "error", "error": "boom", "metrics": {}},
    ])
    assert out["requeued"] == 2 and out["done"] == 0 and out["failed"] == 0

    rows = {jid: store.job(jid) for jid in ("g1", "g2")}
    assert rows["g1"]["state"] == "pending"
    assert rows["g2"]["state"] == "pending"
    # g1 (eviction) did NOT burn a retry; g2 (real error) did
    assert rows["g1"]["attempts"] == 0
    assert rows["g2"]["attempts"] == 1
    # eviction records no error; a real error does
    assert rows["g1"]["error"] is None
    assert rows["g2"]["error"]


def test_requeue_does_not_eventually_fail_from_repeated_evictions(tmp_path):
    # The whole point: an at-field pause that flaps on/off must NOT exhaust the
    # retry budget and mark a healthy gig 'failed'. Evict the same gig 5x.
    store = JobStore(str(tmp_path / "r2.db"), max_retries=2)
    store.seed([{"subjob_id": "g", "spec": {}}])
    for _ in range(5):
        store.lease("r", "h", 10, 60)
        out = store.complete([{"subjob_id": "g", "status": "requeue",
                               "error": "evicted", "metrics": {}}])
        assert out["failed"] == 0
    rows = {r["subjob_id"]: r for r in (store.job("g"),)}
    assert rows["g"]["state"] == "pending" and rows["g"]["attempts"] == 0


# --------------------------------------------- pool abort-with-eviction (pause)
def test_pool_pause_cb_evicts_queued_keeps_running():
    from kiroshi.pool import LocalPool

    pool = LocalPool(task_ref="examples.sleep_task:run", workers=1)
    try:
        # 6 gigs: 1 worker, so at most 1 runs at a time; the other 5 sit queued.
        gigs = [{"subjob_id": f"g{i}", "spec": {"seconds": 3.0}} for i in range(6)]

        fired = {"v": False}

        def pause_cb() -> bool:
            # Fire once, ~immediately, so the 5 queued gigs are evicted before the
            # single running gig (3s sleep) finishes.
            fired["v"] = True
            return True

        t0 = time.time()
        results = pool.run_batch(
            gigs, max_pending=6, gig_timeout=10.0,
            hb_interval=30.0, pause_cb=pause_cb,
        )
        elapsed = time.time() - t0

        by_id = {r["subjob_id"]: r for r in results}
        assert len(results) == 6                      # no gig lost
        oks = [j for j, r in by_id.items() if r["status"] == "ok"]
        evicted = [j for j, r in by_id.items() if r["status"] == "requeue"]
        # The invariant: pause_cb evicts the not-yet-running gigs (requeue) and
        # leaves already-dispatched ones to finish (ok). Exact split depends on
        # ProcessPool's dispatch timing, so assert the meaningful bounds:
        assert len(oks) + len(evicted) == 6
        assert len(evicted) >= 1, "pause_cb should evict at least the queued gigs"
        assert len(oks) >= 1, "the dispatched gig should finish, not be lost"
        assert all(by_id[j]["error"] == "evicted: pressure pause" for j in evicted)
        # Eviction must cut the run short: all-6-sequential would be 6*3s = 18s.
        assert elapsed < 15.0
    finally:
        pool.close()
