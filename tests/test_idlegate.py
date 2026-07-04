"""Unit tests for kiroshi.idlegate — pure hysteresis, no coordinator/clock sleeps."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.idlegate import (
    IdleGateTracker, normalize_gate,
    REASON_OPEN, REASON_WAIT, REASON_NO_TELEMETRY,
)


def _snap(*utils, samples=10):
    return {"disks": [{"disk": f"disk{i+1}", "util_pct": u, "samples": samples}
                      for i, u in enumerate(utils)]}


def test_normalize_minutes_to_seconds():
    g = normalize_gate({"util_pct": 20, "sustain_min": 30})
    assert g["sustain_s"] == 1800.0
    assert g["util_pct"] == 20.0
    assert g["disks"] is None


def test_normalize_none_is_not_gated():
    assert normalize_gate(None) is None
    assert normalize_gate({}) is None


def test_busy_array_never_admits():
    g = normalize_gate({"util_pct": 15, "sustain_s": 100})
    t = IdleGateTracker()
    res = t.evaluate("j", g, _snap(80, 5, 5), now=1000.0)
    assert res.admit is False
    assert res.reason == REASON_WAIT
    assert res.cur_util == 80


def test_quiet_must_be_sustained():
    g = normalize_gate({"util_pct": 15, "sustain_s": 100})
    t = IdleGateTracker()
    # t=0: quiet begins
    r0 = t.evaluate("j", g, _snap(5, 5, 5), now=0.0)
    assert r0.admit is False and r0.reason == REASON_WAIT
    # t=50: still within sustain window
    r1 = t.evaluate("j", g, _snap(10, 5, 5), now=50.0)
    assert r1.admit is False
    # t=100: sustained long enough -> admit
    r2 = t.evaluate("j", g, _snap(5, 5, 5), now=100.0)
    assert r2.admit is True and r2.reason == REASON_OPEN


def test_breach_resets_the_clock():
    g = normalize_gate({"util_pct": 15, "sustain_s": 100})
    t = IdleGateTracker()
    t.evaluate("j", g, _snap(5), now=0.0)
    t.evaluate("j", g, _snap(5), now=90.0)      # almost there
    t.evaluate("j", g, _snap(99), now=95.0)     # BREACH -> reset (quiet_since=None)
    # Clock only restarts on the next QUIET observation (t=190), not at breach.
    r = t.evaluate("j", g, _snap(5), now=190.0)  # quiet_since := 190
    assert r.admit is False
    r_mid = t.evaluate("j", g, _snap(5), now=289.0)  # only 99s sustained
    assert r_mid.admit is False
    r2 = t.evaluate("j", g, _snap(5), now=290.0)  # 100s sustained -> admit
    assert r2.admit is True


def test_only_watched_disks_count():
    g = normalize_gate({"disks": ["disk1"], "util_pct": 15, "sustain_s": 0})
    t = IdleGateTracker()
    # disk2 is slammed but we only watch disk1 (quiet) -> admit (sustain=0)
    res = t.evaluate("j", g, _snap(5, 99), now=0.0)
    assert res.admit is True
    assert res.cur_util == 5


def test_no_telemetry_fails_open():
    g = normalize_gate({"util_pct": 15, "sustain_s": 9999})
    t = IdleGateTracker()
    res = t.evaluate("j", g, {"disks": []}, now=0.0)
    assert res.admit is True
    assert res.reason == REASON_NO_TELEMETRY
    # a disk with <2 samples is not real telemetry
    res2 = t.evaluate("j", g, _snap(5, samples=1), now=0.0)
    assert res2.reason == REASON_NO_TELEMETRY


if __name__ == "__main__":
    fail = 0
    for name in sorted(n for n in dir() if n.startswith("test_")):
        try:
            globals()[name]()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            fail += 1
    sys.exit(fail)
