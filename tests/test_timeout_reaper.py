"""Fix A + B (2026-07-23): make the per-sub-job timeout survive a wedged loop.

The incident: a native NVDEC hang left ``run_batch``'s ``wait()`` no longer
honoring its timeout, so BOTH in-loop defenses were defeated -- the per-sub-job
timeout (which runs after ``wait()`` returns) never ran, and the heartbeat the
watchdog keyed off went silent WITHOUT the watchdog firing, because it treated
"loop still cycling" as progress.

Fix B: an INDEPENDENT reaper thread reads the on-disk sub-job markers (which
carry the worker pid) and tree-kills a stuck worker directly -- no dependency
on the wedged loop.

Fix A: the watchdog's progress clock is bumped ONLY on a real completion
(``progress_cb``), never on a bare heartbeat -- so a heartbeating-but-stalled
runner ages the watchdog as it should.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi import pool as poolmod  # noqa: E402
from kiroshi import subjob_capture  # noqa: E402
from kiroshi.pool import LocalPool  # noqa: E402


# ------------------------------------------------------- B: reaper unit (mocked)
def test_stuck_workers_reports_only_over_threshold_with_pid(tmp_path, monkeypatch):
    d = tmp_path / "subjob_logs"
    d.mkdir()
    monkeypatch.setattr(subjob_capture, "subjob_logs_dir", lambda: d)
    (d / "old.json").write_text(json.dumps(
        {"subjob_id": "old", "started_at": time.time() - 100, "pid": 4242}))
    (d / "young.json").write_text(json.dumps(
        {"subjob_id": "young", "started_at": time.time() - 1, "pid": 4243}))
    (d / "nopid.json").write_text(json.dumps(
        {"subjob_id": "nopid", "started_at": time.time() - 100}))  # skipped: no pid

    stuck = subjob_capture.stuck_workers(60)
    assert len(stuck) == 1
    assert stuck[0]["subjob_id"] == "old" and stuck[0]["pid"] == 4242


def test_reaper_kills_only_current_pool_workers(tmp_path, monkeypatch):
    """A stale marker whose pid was reused by an unrelated process must NOT be
    killed -- the reaper only touches pids that are current pool workers."""
    d = tmp_path / "subjob_logs"
    d.mkdir()
    monkeypatch.setattr(subjob_capture, "subjob_logs_dir", lambda: d)
    (d / "stuck.json").write_text(json.dumps(
        {"subjob_id": "stuck", "started_at": time.time() - 100, "pid": 111}))
    (d / "reused.json").write_text(json.dumps(
        {"subjob_id": "reused", "started_at": time.time() - 100, "pid": 999}))

    killed: list[int] = []
    monkeypatch.setattr(poolmod, "_tree_kill_pid", lambda pid: killed.append(pid))

    lp = LocalPool.__new__(LocalPool)          # no real pool
    lp._reaped_for_timeout = set()

    class _FakePool:
        _processes = {111: object()}           # 111 is ours; 999 is not
    lp._pool = _FakePool()

    lp._reap_stuck_workers(60)
    assert killed == [111]
    assert "stuck" in lp._reaped_for_timeout
    assert "reused" not in lp._reaped_for_timeout


# --------------------------------------------- A: progress = completions only
def test_progress_cb_fires_once_per_completion_not_per_heartbeat():
    """Fix A's core: progress is pulsed per completed sub-job, decoupled from
    the heartbeat cadence -- so a runner that heartbeats without completing
    anything makes ZERO progress pulses and its watchdog ages."""
    prog = {"n": 0}
    hb = {"n": 0}
    pool = LocalPool(task_ref="examples.sleep_task:run", workers=2)
    try:
        gigs = [{"subjob_id": f"g{i}", "spec": {"seconds": 0.05}} for i in range(4)]
        results = pool.run_batch(
            gigs, max_pending=2, hb_interval=0.001,   # heartbeat fires very often
            heartbeat_cb=lambda: hb.__setitem__("n", hb["n"] + 1),
            progress_cb=lambda: prog.__setitem__("n", prog["n"] + 1),
        )
        assert len(results) == 4
        assert prog["n"] == 4          # exactly one pulse per completion
        assert hb["n"] >= 1            # heartbeats fired independently...
        # ...and are NOT coupled to progress: many heartbeats, only 4 progresses.
    finally:
        pool.close()


# --------------------------------------- B: real subprocess -- hang gets reaped
def test_hanging_task_is_reaped_and_labeled_timeout():
    """A genuinely-hanging task (long sleep) is killed at ~gig_timeout and
    reported "timeout" -- fast, NOT after the full 60s sleep."""
    pool = LocalPool(task_ref="examples.sleep_task:run", workers=2)
    try:
        t0 = time.time()
        results = pool.run_batch(
            [{"subjob_id": "hang", "spec": {"seconds": 60.0}}],
            max_pending=2, gig_timeout=2.0, hb_interval=30.0,
        )
        elapsed = time.time() - t0
        by = {r["subjob_id"]: r for r in results}
        assert by["hang"]["status"] == "error"
        assert by["hang"]["error"] == "timeout"
        assert elapsed < 25.0, f"timeout not enforced promptly (took {elapsed:.1f}s of a 60s sleep)"
    finally:
        pool.close()


def test_reaper_enforces_timeout_even_when_loop_wait_is_wedged(monkeypatch):
    """The incident, isolated: make ``wait()`` stop honoring its timeout (block
    long), so the in-loop per-sub-job timeout is STARVED. Only the independent
    reaper can save it -- and it does, surfacing "timeout" (relabeled from the
    BrokenProcessPool its kill produces) well before the 60s sleep."""
    real_wait = poolmod.wait

    def wedged_wait(fs, timeout=None, return_when=None):
        # Emulate the corrupted-executor hang: ignore the caller's short poll
        # and block long. FIRST_COMPLETED still returns early once the reaper's
        # kill makes a future complete -- which is exactly the property we test.
        return real_wait(fs, timeout=30.0, return_when=return_when)

    monkeypatch.setattr(poolmod, "wait", wedged_wait)

    pool = LocalPool(task_ref="examples.sleep_task:run", workers=2)
    try:
        t0 = time.time()
        results = pool.run_batch(
            [{"subjob_id": "hang", "spec": {"seconds": 60.0}}],
            max_pending=2, gig_timeout=2.0, hb_interval=30.0,
        )
        elapsed = time.time() - t0
        by = {r["subjob_id"]: r for r in results}
        assert by["hang"]["error"] == "timeout", (
            "reaper must relabel its kill as 'timeout', not leave it BrokenProcessPool")
        # Killed by the out-of-band reaper, NOT by waiting out the 60s sleep or
        # even the 30s wedged wait -- proving enforcement independent of the loop.
        assert elapsed < 25.0, f"took {elapsed:.1f}s"
    finally:
        pool.close()
