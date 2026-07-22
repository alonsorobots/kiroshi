"""requeue() batching: a single UPDATE over thousands of rows held
self._lock (needed by every lease()/heartbeat()) for the whole operation --
on 2026-07-22 a ~9k-row requeue made the coordinator unresponsive for 10+
minutes. requeue() now processes in bounded batches, committing (and
releasing the lock) between each, so it can't monopolize the coordinator.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.jobstore import JobStore  # noqa: E402


def _store_with_failed(n: int) -> JobStore:
    s = JobStore(":memory:", max_retries=3)
    s.seed([{"subjob_id": f"g{i}", "spec": {}} for i in range(n)])
    with s._lock:
        s._conn.executemany(
            "UPDATE subjobs SET state='failed', attempts=2, error='boom' "
            "WHERE subjob_id=?",
            [(f"g{i}",) for i in range(n)],
        )
        s._conn.commit()
    return s


def test_requeue_correct_across_multiple_batches():
    s = _store_with_failed(23)
    s._REQUEUE_BATCH = 5  # force several batches for 23 rows
    n = s.requeue(("failed",), reset_attempts=True)
    assert n == 23
    rows = s._conn.execute(
        "SELECT state, attempts FROM subjobs"
    ).fetchall()
    assert all(r["state"] == "pending" for r in rows)
    assert all(r["attempts"] == 0 for r in rows)


def test_requeue_zero_matching_rows_no_infinite_loop():
    s = _store_with_failed(0)
    s._REQUEUE_BATCH = 5
    assert s.requeue(("failed",)) == 0


def test_requeue_exact_multiple_of_batch_size_terminates():
    s = _store_with_failed(10)
    s._REQUEUE_BATCH = 5  # exactly 2 batches
    assert s.requeue(("failed",)) == 10


def test_requeue_releases_lock_between_batches():
    # A real threading.Lock has no acquire counter, so swap in one that counts.
    import threading

    class CountingLock:
        def __init__(self):
            self._lock = threading.RLock()
            self.acquires = 0

        def __enter__(self):
            self.acquires += 1
            self._lock.acquire()
            return self

        def __exit__(self, *a):
            self._lock.release()

    s = _store_with_failed(11)
    s._REQUEUE_BATCH = 5
    s._lock = CountingLock()
    n = s.requeue(("failed",))
    assert n == 11
    # 11 rows / batch 5 -> batches of 5,5,1 then a terminating 0-row check = 4 acquires.
    assert s._lock.acquires >= 3, "must re-acquire the lock per batch, not hold it once"


def test_requeue_reset_attempts_false_preserves_attempts():
    s = _store_with_failed(6)
    s._REQUEUE_BATCH = 3
    s.requeue(("failed",), reset_attempts=False)
    rows = s._conn.execute("SELECT attempts FROM subjobs").fetchall()
    assert all(r["attempts"] == 2 for r in rows)
