"""Unit tests for FailureBreaker.step()-equivalent (record/allow_lease) as a
pure control law -- no threads/sleep/real I/O, a controlled clock throughout.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi.failure_breaker import FailureBreaker  # noqa: E402


def _fill_ok(b: FailureBreaker, n: int, t: float) -> None:
    for _ in range(n):
        b.record("ok", None, t)


# --------------------------------------------------------- consecutive-permanent trip
def test_three_consecutive_permanent_trips():
    b = FailureBreaker()
    t = 1000.0
    b.record("error", "LogonFailure", t)
    assert b.state == FailureBreaker.CLOSED
    b.record("error", "LogonFailure", t)
    assert b.state == FailureBreaker.CLOSED
    b.record("error", "LogonFailure", t)
    assert b.state == FailureBreaker.OPEN
    assert b.dominant_error == "logonfailure"


def test_non_permanent_result_resets_consecutive_counter():
    # Isolate the CONSECUTIVE-permanent fast-path counter specifically (a
    # separate, slower window-based trip legitimately covers the case where 4
    # of the last 5 completions share one error -- that's tested elsewhere).
    b = FailureBreaker()
    t = 1000.0
    b.record("error", "LogonFailure", t)
    b.record("error", "LogonFailure", t)
    assert b._consecutive_permanent == 2
    b.record("ok", None, t)  # resets the streak
    assert b._consecutive_permanent == 0
    b.record("error", "LogonFailure", t)
    assert b._consecutive_permanent == 1  # NOT 3 -- the reset held


def test_transient_errors_never_trigger_consecutive_permanent_counter():
    # Verifies the FAST path specifically: transient errors never advance
    # _consecutive_permanent, no matter how many occur. (A sustained run of
    # identical transient errors CAN still trip the separate, slower
    # window-based path -- see test_homogeneous_transient_window_trips --
    # that's correct, intended behavior, not what this test isolates.)
    b = FailureBreaker()
    t = 1000.0
    for _ in range(3):  # stay under MIN_SAMPLE so only the fast path is in play
        b.record("error", "TimeoutError", t)
    assert b._consecutive_permanent == 0
    assert b.state == FailureBreaker.CLOSED


# ------------------------------------------------------------------ window-based trip
def test_homogeneous_transient_window_trips():
    b = FailureBreaker()
    t = 1000.0
    # 20-item window: 12 failures (60% >= 50%), all the SAME transient sig.
    for _ in range(12):
        b.record("error", "ConnectionResetError", t)
    for _ in range(8):
        b.record("ok", None, t)
    assert b.state == FailureBreaker.OPEN
    assert "connectionreseterror" in b.dominant_error.lower()


def test_heterogeneous_scatter_does_not_trip():
    b = FailureBreaker()
    t = 1000.0
    errors = ["ErrorA", "ErrorB", "ErrorC", "ErrorD", "ErrorE",
              "ErrorF", "ErrorG", "ErrorH", "ErrorI", "ErrorJ",
              "ErrorK", "ErrorL"]
    for e in errors:
        b.record("error", e, t)
    for _ in range(8):
        b.record("ok", None, t)
    assert b.state == FailureBreaker.CLOSED  # scattered distinct errors, not systemic


def test_low_volume_never_trips_even_if_all_fail():
    b = FailureBreaker()
    t = 1000.0
    for _ in range(3):  # below MIN_SAMPLE=5
        b.record("error", "ConnectionResetError", t)
    assert b.state == FailureBreaker.CLOSED


def test_below_fifty_percent_failure_fraction_does_not_trip():
    b = FailureBreaker()
    t = 1000.0
    # Fill with "ok" FIRST so the window's fail-ratio is correct at every
    # intermediate check (recording all failures before any "ok" would make
    # the window 100%-failed the moment MIN_SAMPLE is reached, which is a
    # different, correctly-tripping scenario -- not what this test checks).
    for _ in range(15):
        b.record("ok", None, t)
    for _ in range(5):
        b.record("error", "ConnectionResetError", t)
    assert b.state == FailureBreaker.CLOSED  # 5/20 = 25% failed, under the 50% threshold


def test_requeue_status_counts_as_ok_not_a_failure():
    b = FailureBreaker()
    t = 1000.0
    for _ in range(15):
        b.record("requeue", "evicted: pressure pause", t)
    for _ in range(5):
        b.record("error", "ConnectionResetError", t)
    assert b.state == FailureBreaker.CLOSED  # requeues are not failures


# ---------------------------------------------------------------- leasing gate
def test_allow_lease_true_when_closed():
    b = FailureBreaker()
    may, cap = b.allow_lease(1000.0)
    assert may is True and cap is None


def test_allow_lease_false_before_cooldown_elapses():
    b = FailureBreaker()
    t = 1000.0
    b.record("error", "LogonFailure", t)
    b.record("error", "LogonFailure", t)
    b.record("error", "LogonFailure", t)
    assert b.state == FailureBreaker.OPEN
    may, cap = b.allow_lease(t + 10.0)  # well under BASE_COOLDOWN_S=120
    assert may is False and cap is None


def test_allow_lease_transitions_to_half_open_after_cooldown():
    b = FailureBreaker()
    t = 1000.0
    b.record("error", "LogonFailure", t)
    b.record("error", "LogonFailure", t)
    b.record("error", "LogonFailure", t)
    may, cap = b.allow_lease(t + FailureBreaker.BASE_COOLDOWN_S + 1.0)
    assert may is True and cap == 1
    assert b.state == FailureBreaker.HALF_OPEN


def test_half_open_probe_inflight_blocks_further_leases():
    b = FailureBreaker()
    t = 1000.0
    b.record("error", "LogonFailure", t)
    b.record("error", "LogonFailure", t)
    b.record("error", "LogonFailure", t)
    b.allow_lease(t + FailureBreaker.BASE_COOLDOWN_S + 1.0)  # -> HALF_OPEN
    b.note_leased(1)
    may, cap = b.allow_lease(t + FailureBreaker.BASE_COOLDOWN_S + 2.0)
    assert may is False  # probe already out, don't lease a second one


def test_half_open_probe_success_closes_and_resets_cooldown():
    b = FailureBreaker()
    t = 1000.0
    b.record("error", "LogonFailure", t)
    b.record("error", "LogonFailure", t)
    b.record("error", "LogonFailure", t)
    b.allow_lease(t + FailureBreaker.BASE_COOLDOWN_S + 1.0)  # -> HALF_OPEN
    b.note_leased(1)
    b.record("ok", None, t + FailureBreaker.BASE_COOLDOWN_S + 5.0)
    assert b.state == FailureBreaker.CLOSED
    assert b.cooldown == FailureBreaker.BASE_COOLDOWN_S
    may, cap = b.allow_lease(t + FailureBreaker.BASE_COOLDOWN_S + 6.0)
    assert may is True and cap is None


def test_half_open_probe_failure_reopens_with_doubled_cooldown():
    b = FailureBreaker()
    t = 1000.0
    b.record("error", "LogonFailure", t)
    b.record("error", "LogonFailure", t)
    b.record("error", "LogonFailure", t)
    first_cooldown = b.cooldown
    probe_time = t + FailureBreaker.BASE_COOLDOWN_S + 1.0
    b.allow_lease(probe_time)  # -> HALF_OPEN
    b.note_leased(1)
    b.record("error", "LogonFailure", probe_time + 1.0)  # probe fails
    assert b.state == FailureBreaker.OPEN
    assert b.cooldown == first_cooldown * 2


def test_cooldown_doubling_is_capped_at_max():
    b = FailureBreaker()
    t = 1000.0
    b.record("error", "LogonFailure", t)
    b.record("error", "LogonFailure", t)
    b.record("error", "LogonFailure", t)
    now = t
    # Repeatedly fail the probe until cooldown should be capped.
    for _ in range(20):
        now += b.cooldown + 1.0
        may, cap = b.allow_lease(now)
        if may:
            b.note_leased(1)
            b.record("error", "LogonFailure", now + 1.0)
    assert b.cooldown == FailureBreaker.MAX_COOLDOWN_S


# -------------------------------------------------------------------- snapshot
def test_snapshot_shape():
    b = FailureBreaker()
    snap = b.snapshot()
    assert set(snap.keys()) == {"state", "dominant_error", "consecutive_permanent", "cooldown_s"}
    assert snap["state"] == "closed"


def test_is_open_property():
    b = FailureBreaker()
    assert b.is_open is False
    b.record("error", "LogonFailure", 1000.0)
    b.record("error", "LogonFailure", 1000.0)
    b.record("error", "LogonFailure", 1000.0)
    assert b.is_open is True
