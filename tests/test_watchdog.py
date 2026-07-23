"""Runner watchdog: hard-exit backstop for when run_batch's own in-process
--subjob-timeout enforcement is itself defeated (observed 2026-07-22: a
worker crash corrupted the ProcessPoolExecutor's bookkeeping, `wait()` stopped
honoring its timeout, and the runner burned GPU for 14+ minutes producing
nothing). The decision logic is tested as a pure function -- no real
threads/sleep/os._exit involved.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi import worker  # noqa: E402


def _runner(gig_timeout=None, last_progress_at=0.0):
    r = worker.Runner.__new__(worker.Runner)
    r._draining = False
    r.gig_timeout = gig_timeout
    r._last_progress_at = last_progress_at
    return r


# --------------------------------------------------------------- pure logic
def test_disabled_when_no_gig_timeout_configured():
    r = _runner(gig_timeout=None, last_progress_at=0.0)
    assert r._watchdog_should_exit(now=10_000.0) is False  # huge gap, still off


def test_disabled_when_gig_timeout_is_zero():
    r = _runner(gig_timeout=0, last_progress_at=0.0)
    assert r._watchdog_should_exit(now=10_000.0) is False


def test_no_exit_within_ceiling():
    r = _runner(gig_timeout=300, last_progress_at=1000.0)
    # ceiling = 600s; gap of 500s is under it
    assert r._watchdog_should_exit(now=1500.0) is False


def test_exit_at_exactly_the_ceiling_boundary_is_false():
    r = _runner(gig_timeout=300, last_progress_at=1000.0)
    assert r._watchdog_should_exit(now=1000.0 + 600.0) is False  # strictly greater-than


def test_exit_past_ceiling():
    r = _runner(gig_timeout=300, last_progress_at=1000.0)
    assert r._watchdog_should_exit(now=1000.0 + 601.0) is True


def test_progress_bump_resets_the_gap():
    r = _runner(gig_timeout=300, last_progress_at=1000.0)
    assert r._watchdog_should_exit(now=1601.0) is True
    r._last_progress_at = 1601.0  # a liveness pulse arrives
    assert r._watchdog_should_exit(now=1601.0) is False


def test_ceiling_is_2x_gig_timeout():
    r = _runner(gig_timeout=150)
    assert r._watchdog_ceiling() == 300.0


def test_check_interval_bounds():
    assert worker.Runner._watchdog_check_interval(40.0) == 10.0    # 40/4
    assert worker.Runner._watchdog_check_interval(4.0) == 5.0      # floor
    assert worker.Runner._watchdog_check_interval(1000.0) == 30.0  # ceiling


# ---------------------------------------------------------------- lifecycle
def test_start_watchdog_noop_without_gig_timeout(monkeypatch):
    r = _runner(gig_timeout=None)
    started = []
    monkeypatch.setattr(worker.threading, "Thread",
                        lambda *a, **k: started.append((a, k)))
    r._start_watchdog()
    assert started == []


def test_start_watchdog_spawns_daemon_thread_when_armed(monkeypatch):
    r = _runner(gig_timeout=300, last_progress_at=worker.time.time())
    captured = {}

    class FakeThread:
        def __init__(self, target, name, daemon):
            captured["target"] = target
            captured["name"] = name
            captured["daemon"] = daemon

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(worker.threading, "Thread", FakeThread)
    r._start_watchdog()
    assert captured["daemon"] is True
    assert captured["name"] == "kiroshi-watchdog"
    assert captured["started"] is True


def test_watchdog_loop_exits_process_when_wedged(monkeypatch):
    """Drive the real _watch() closure (captured via a fake Thread) with a
    controlled clock and no real sleep, and confirm it calls os._exit(1)
    exactly when the pure decision says to."""
    r = _runner(gig_timeout=10, last_progress_at=0.0)
    clock = {"t": 0.0}
    monkeypatch.setattr(worker.time, "time", lambda: clock["t"])
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        clock["t"] += s
        # check_interval=5 (ceiling=20 -> 20/4), so the clock crosses the
        # ceiling (20s) on the 5th sleep (t=25); stop shortly after so the
        # loop runs exactly one post-ceiling iteration, not an unbounded loop.
        if len(sleeps) >= 5:
            r._draining = True

    monkeypatch.setattr(worker.time, "sleep", fake_sleep)
    exits = []
    monkeypatch.setattr(worker.os, "_exit", lambda code: exits.append(code))

    class FakeThread:
        def __init__(self, target, name, daemon):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(worker.threading, "Thread", FakeThread)
    r._start_watchdog()
    # ceiling = 20s; check_interval = 5s (20/4). After enough fake-sleep steps
    # the clock exceeds the ceiling and os._exit(1) must fire exactly once.
    assert exits == [1]


# --------------------------------------------------- Fix A: progress = completions
def test_note_progress_bumps_the_clock_off_a_stale_gap():
    """`_note_progress` (run_batch's progress_cb) is the ONLY thing that resets
    the watchdog now -- a real completion, never a bare heartbeat. Bumping it
    clears an otherwise-stale gap."""
    import time as _t
    r = _runner(gig_timeout=300, last_progress_at=1000.0)
    assert r._watchdog_should_exit(now=1601.0) is True   # stale: >600s ceiling
    before = _t.time()
    r._note_progress()
    assert r._last_progress_at >= before
    # from the fresh progress mark, we're well inside the ceiling again
    assert r._watchdog_should_exit(now=r._last_progress_at + 1.0) is False
    assert r._watchdog_should_exit(now=r._last_progress_at + 601.0) is True
