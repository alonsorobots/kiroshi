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


# ---------------------------------------------------- resize() (Phase 3: WorkerTuner)
def test_pool_resize_changes_worker_count_and_no_op_when_unchanged():
    from kiroshi.pool import LocalPool

    pool = LocalPool(task_ref="examples.sleep_task:run", workers=2)
    try:
        assert pool.workers == 2
        old_pool_obj = pool._pool

        pool.resize(2)  # same size -> no-op, must NOT rebuild
        assert pool._pool is old_pool_obj

        pool.resize(4)
        assert pool.workers == 4
        assert pool._pool is not old_pool_obj  # rebuilt

        # Confirm the resized pool is actually usable (not left half-torn-down).
        results = pool.run_batch(
            [{"subjob_id": f"g{i}", "spec": {"seconds": 0.05}} for i in range(4)],
            max_pending=4, hb_interval=30.0,
        )
        assert len(results) == 4
        assert all(r["status"] == "ok" for r in results)

        pool.resize(1)
        assert pool.workers == 1
    finally:
        pool.close()


# ------------------------------------------ dynamic max_pending (Phase 3: fast brake)
def test_run_batch_max_pending_accepts_live_callable():
    """A callable max_pending must be re-resolved each refill -- lowering it
    mid-batch stops new submissions without cancelling in-flight work (the
    fast pressure brake), as opposed to pause_cb's hard eviction."""
    from kiroshi.pool import LocalPool

    pool = LocalPool(task_ref="examples.sleep_task:run", workers=4)
    try:
        cap = {"v": 4}

        def dynamic_cap() -> int:
            return cap["v"]

        # 8 gigs, cap starts at 4. Drop the cap to 1 shortly after start so we
        # can observe that refill() picks up the new (lower) value on its next
        # call instead of continuing to submit up to the original cap.
        def dropper():
            time.sleep(0.1)
            cap["v"] = 1

        import threading
        threading.Thread(target=dropper, daemon=True).start()

        gigs = [{"subjob_id": f"g{i}", "spec": {"seconds": 0.3}} for i in range(8)]
        results = pool.run_batch(gigs, max_pending=dynamic_cap, hb_interval=30.0)

        # No gig lost or duplicated regardless of exact timing of the cap drop.
        assert len(results) == 8
        assert all(r["status"] == "ok" for r in results)
    finally:
        pool.close()


def test_run_batch_max_pending_callable_exception_falls_back_to_default():
    """A misbehaving tuner (raises) must never take the pool down with it."""
    from kiroshi.pool import LocalPool

    pool = LocalPool(task_ref="examples.sleep_task:run", workers=2)
    try:
        def broken_cap() -> int:
            raise RuntimeError("tuner exploded")

        results = pool.run_batch(
            [{"subjob_id": f"g{i}", "spec": {"seconds": 0.05}} for i in range(3)],
            max_pending=broken_cap, hb_interval=30.0,
        )
        assert len(results) == 3
        assert all(r["status"] == "ok" for r in results)
    finally:
        pool.close()


# ------------------------------------ _run_one error surfacing (not swallowing)
def test_run_one_surfaces_task_reported_error_without_raising():
    """A task that RETURNS status=error (rather than raising) -- the pattern
    gpu_4fps.run()'s hardened wrapper uses -- must produce a real top-level
    error string, not None. Otherwise the coordinator persists str(None) =
    "None" and the traceback (which tasks stash in metrics) is lost."""
    import kiroshi.pool as pool

    orig = pool._TASK_FN
    try:
        # (a) task sets a top-level error explicitly
        pool._TASK_FN = lambda spec: {"status": "error", "error": "boom explicit", "metrics": {}}
        out = pool._run_one(("g1", {}, 0, 0.0))
        assert out["status"] == "error"
        assert out["error"] == "boom explicit"

        # (b) gpu_4fps convention: no top-level error, traceback in metrics
        pool._TASK_FN = lambda spec: {"status": "error", "metrics": {"traceback": "Traceback: kaboom"}}
        out = pool._run_one(("g2", {}, 0, 0.0))
        assert out["status"] == "error"
        assert "kaboom" in out["error"]  # synthesized from metrics, never None

        # (c) error status with truly no detail -> a placeholder, never "None"
        pool._TASK_FN = lambda spec: {"status": "error", "metrics": {}}
        out = pool._run_one(("g3", {}, 0, 0.0))
        assert out["status"] == "error"
        assert out["error"] and out["error"] != "None"
        assert str(out["error"]).lower() != "none"

        # (d) success path is unaffected: no error, status preserved
        pool._TASK_FN = lambda spec: {"status": "ok", "metrics": {"n": 1}}
        out = pool._run_one(("g4", {}, 0, 0.0))
        assert out["status"] == "ok"
        assert out["error"] is None

        # (e) skipped is not an error -> no synthesized error string
        pool._TASK_FN = lambda spec: {"status": "skipped", "metrics": {"reason": "exists"}}
        out = pool._run_one(("g5", {}, 0, 0.0))
        assert out["status"] == "skipped"
        assert out["error"] is None
    finally:
        pool._TASK_FN = orig


def test_run_one_does_not_retry_a_permanent_error():
    """A task that RAISES a permanent error (bad credential, etc.) must not
    burn its retry budget -- retrying is guaranteed useless. Contrast with a
    transient exception, which still gets the full retry budget."""
    import kiroshi.pool as pool

    orig = pool._TASK_FN
    try:
        calls = {"n": 0}

        def raises_permanent(spec):
            calls["n"] += 1
            raise RuntimeError("spnego.exceptions.LogonFailure: NT_STATUS_LOGON_FAILURE")

        pool._TASK_FN = raises_permanent
        out = pool._run_one(("perm-1", {}, 3, 0.0))  # retries=3 -> up to 4 attempts allowed
        assert out["status"] == "error"
        assert calls["n"] == 1, "a permanent error must stop after the first attempt"

        calls["n"] = 0

        def raises_transient(spec):
            calls["n"] += 1
            raise RuntimeError("connection reset")

        pool._TASK_FN = raises_transient
        out = pool._run_one(("trans-1", {}, 3, 0.0))
        assert out["status"] == "error"
        assert calls["n"] == 4, "a transient error must use the full retry budget"
    finally:
        pool._TASK_FN = orig
