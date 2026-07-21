"""Unit tests for WorkerTuner.step() -- the pure AIMD control law.

No HTTP, no real AT-Field instance, no pool: feed synthetic headroom traces
and assert the shape of the response (slow growth, fast shrink, no
oscillation, never guesses blind). This is deliberately the cheap,
fast-running half of the verification plan; the live convergence check
against the real held-frames-4fps campaign is separate.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi.worker_tuner import WorkerTuner  # noqa: E402


def _tuner(floor=1, ceiling=8, **kw) -> WorkerTuner:
    return WorkerTuner(floor=floor, ceiling=ceiling, cache_path=None, **kw)


# --------------------------------------------------------------- basic shape
def test_unreachable_atfield_disables_tuner_never_guesses_blind():
    t = _tuner()
    start = t.target
    result = t.step(None, now=0.0)
    assert t.enabled is False
    assert result == start  # frozen, not changed


def test_reachable_after_unreachable_reenables():
    t = _tuner()
    t.step(None, now=0.0)
    assert t.enabled is False
    t.step(0.5, now=1.0)
    assert t.enabled is True


# ------------------------------------------------------------- slow growth
def test_flat_safe_grows_by_one_only_after_confirm_streak():
    t = _tuner(floor=1, ceiling=8)
    assert t.target == 1
    # First safe reading: not enough on its own (INCREASE_CONFIRM=2).
    t.step(0.9, now=0.0)
    assert t.target == 1
    # Second consecutive safe reading confirms -> +1, not a jump to ceiling.
    t.target_before = t.target
    new = t.step(0.9, now=1.0)
    assert new == 2
    assert new - 1 == 1  # exactly additive, never more than +1 per confirm


def test_flat_safe_never_exceeds_ceiling():
    t = _tuner(floor=1, ceiling=3)
    now = 0.0
    for _ in range(50):
        t.step(0.9, now=now)
        now += 1.0
    assert t.target <= 3
    assert t.target == 3  # does converge to the ceiling eventually, just slowly


def test_hold_band_does_not_grow_or_shrink():
    t = _tuner(floor=1, ceiling=8)
    t.step(0.9, now=0.0)
    t.step(0.9, now=1.0)
    grown = t.target
    assert grown == 2
    # Mid-band readings (between DANGER=0.15 and SAFE=0.35) must hold.
    for i in range(10):
        t.step(0.25, now=2.0 + i)
    assert t.target == grown


# -------------------------------------------------------------- fast shrink
def test_danger_shrinks_immediately_not_gradually():
    t = _tuner(floor=1, ceiling=8)
    # Ramp up to 4 first.
    now = 0.0
    while t.target < 4:
        now += 1.0
        t.step(0.9, now=now)
    before = t.target
    assert before == 4
    now += 1.0
    after = t.step(0.05, now=now)  # single danger reading
    assert after < before  # immediate cut, no confirmation streak needed


def test_danger_never_undercuts_the_floor():
    t = _tuner(floor=2, ceiling=8)
    now = 0.0
    while t.target < 8:
        now += 1.0
        t.step(0.9, now=now)
    # Hammer it with danger readings.
    for i in range(20):
        now += 1.0
        t.step(0.01, now=now)
    assert t.target >= 2


def test_decrease_starts_cooldown_blocking_immediate_regrowth():
    t = _tuner(floor=1, ceiling=8)
    now = 0.0
    while t.target < 4:
        now += 1.0
        t.step(0.9, now=now)
    now += 1.0
    t.step(0.05, now=now)  # trigger a decrease at time `now`
    shrunk = t.target
    # Immediately after, even consecutive safe readings must NOT grow --
    # cooldown must hold for DECREASE_COOLDOWN_S.
    for i in range(5):
        now += 1.0
        t.step(0.9, now=now)
    assert t.target == shrunk


def test_regrowth_resumes_after_cooldown_elapses():
    t = _tuner(floor=1, ceiling=8)
    now = 0.0
    while t.target < 4:
        now += 1.0
        t.step(0.9, now=now)
    now += 1.0
    t.step(0.05, now=now)
    shrunk = t.target
    decrease_at = now
    # Jump past the cooldown window, then confirm-streak safe readings.
    now = decrease_at + WorkerTuner.DECREASE_COOLDOWN_S + 1.0
    t.step(0.9, now=now)
    now += 1.0
    grown = t.step(0.9, now=now)
    assert grown == shrunk + 1


# ------------------------------------------------------------- no oscillation
def test_sawtooth_headroom_does_not_thrash_every_tick():
    """Alternating safe/danger every single tick must not produce a change
    on every tick -- growth requires a CONFIRM streak that a sawtooth never
    completes, so the net effect should be dominated by the danger cuts
    (asymmetric by design) without endless up/down churn."""
    t = _tuner(floor=1, ceiling=8)
    now = 0.0
    changes = 0
    prev = t.target
    for i in range(40):
        headroom = 0.9 if i % 2 == 0 else 0.05
        t.step(headroom, now=now)
        now += 1.0
        if t.target != prev:
            changes += 1
            prev = t.target
    # A sawtooth alternates every step; if we were naively symmetric AIMD
    # (grow on any single safe reading) this would change ~40 times. The
    # confirm-streak requirement means growth basically never completes
    # under alternation, so almost all changes (if any) are decreases.
    assert changes < 10


def test_sudden_cliff_reacts_within_one_tick():
    t = _tuner(floor=1, ceiling=8)
    now = 0.0
    while t.target < 6:
        now += 1.0
        t.step(0.9, now=now)
    high = t.target
    now += 1.0
    after_cliff = t.step(0.0, now=now)  # cliff: headroom drops to zero
    assert after_cliff < high  # reacted on the very next tick, not delayed


# ----------------------------------------------------------------- clamping
def test_headroom_exactly_at_boundaries():
    t = _tuner(floor=1, ceiling=8)
    # Exactly at DANGER boundary: spec says `< DANGER` triggers decrease, so
    # exactly-DANGER should NOT decrease (it's the hold band's edge).
    t.step(0.9, now=0.0)
    t.step(0.9, now=1.0)
    grown = t.target
    t.step(WorkerTuner.DANGER, now=2.0)
    assert t.target == grown  # boundary itself is hold, not danger


# ------------------------------------------------------------- persistence
def test_converges_and_persists_after_sustained_hold(tmp_path):
    cache = tmp_path / "task.json"
    t = WorkerTuner(floor=1, ceiling=8, cache_path=cache)
    now = 0.0
    while t.target < 3:
        now += 1.0
        t.step(0.9, now=now)
    # Not yet saved -- needs CONVERGE_HOLDS consecutive in-band ticks.
    assert not cache.exists()
    for _ in range(WorkerTuner.CONVERGE_HOLDS):
        now += 1.0
        t.step(0.25, now=now)  # mid-band = hold
    assert cache.exists()
    import json
    saved = json.loads(cache.read_text())
    assert saved["workers"] == t.target


def test_next_run_starts_below_cached_value_not_at_it(tmp_path):
    cache = tmp_path / "task.json"
    import json
    cache.write_text(json.dumps({"workers": 10, "updated_at": 0.0, "headroom_at_converge": 0.3}))
    t = WorkerTuner(floor=1, ceiling=20, cache_path=cache)
    assert t.target < 10  # re-probes from below, doesn't blindly trust the cache
    assert t.target == 8  # floor(10 * 0.8)


def test_missing_or_corrupt_cache_starts_at_floor(tmp_path):
    cache = tmp_path / "does_not_exist.json"
    t = WorkerTuner(floor=2, ceiling=8, cache_path=cache)
    assert t.target == 2

    bad_cache = tmp_path / "corrupt.json"
    bad_cache.write_text("{not valid json")
    t2 = WorkerTuner(floor=2, ceiling=8, cache_path=bad_cache)
    assert t2.target == 2


# -------------------------------------------------------------- mid-batch brake
def test_max_pending_cap_generous_when_not_braking():
    t = _tuner(floor=1, ceiling=6)
    t._braking = False
    assert t.max_pending_cap(default_cap=4) >= 4


def test_max_pending_cap_trickle_when_braking():
    t = _tuner(floor=1, ceiling=6)
    t._braking = True
    assert t.max_pending_cap(default_cap=12) == 1


# --------------------------------------------------------------- maybe_resize
class _FakePool:
    def __init__(self, workers: int):
        self.workers = workers
        self.resize_calls: list[int] = []

    def resize(self, n: int) -> None:
        self.resize_calls.append(n)
        self.workers = n


def test_maybe_resize_growth_is_rate_limited():
    t = _tuner(floor=1, ceiling=8)
    t.enabled = True
    t.target = 4
    pool = _FakePool(workers=1)
    t._last_resize_at = 100.0
    import kiroshi.worker_tuner as wt
    orig_time = wt.time.time
    try:
        wt.time.time = lambda: 100.0 + 1.0  # well within MIN_RESIZE_INTERVAL_S
        assert t.maybe_resize(pool) is False
        assert pool.workers == 1  # unchanged -- growth deferred
    finally:
        wt.time.time = orig_time


def test_maybe_resize_shrink_is_never_rate_limited():
    """The exact bug found during live verification: a shrink arriving right
    after a growth resize must apply immediately, not wait out
    MIN_RESIZE_INTERVAL_S -- "fast to shrink" must hold at the actuation
    layer, not just in the state machine's own target value."""
    t = _tuner(floor=1, ceiling=8)
    t.enabled = True
    t.target = 2
    pool = _FakePool(workers=6)
    t._last_resize_at = 100.0  # a resize "just happened"
    import kiroshi.worker_tuner as wt
    orig_time = wt.time.time
    try:
        wt.time.time = lambda: 100.0 + 1.0  # 1s later -- well inside the interval
        assert t.maybe_resize(pool) is True
        assert pool.workers == 2  # applied immediately despite the interval
    finally:
        wt.time.time = orig_time


def test_maybe_resize_no_op_when_disabled_or_already_at_target():
    t = _tuner(floor=1, ceiling=8)
    t.target = 4
    pool = _FakePool(workers=1)

    t.enabled = False
    assert t.maybe_resize(pool) is False
    assert pool.workers == 1

    t.enabled = True
    pool2 = _FakePool(workers=4)
    assert t.maybe_resize(pool2) is False  # already at target
    assert pool2.resize_calls == []
