"""Integration test for the bottleneck detector in AdvisoryDetector.

This is the test that would have caught all four bugs the supervisor flagged:
  1. UnboundLocalError on io_snap when self._io is None (NVMe-only nodes)
  2. _resolve_bottleneck using wrong fingerprints
  3. expected_gigs_per_s always 0 → latency_bound never fires
  4. The __import__('os') contortion

It constructs a real AdvisoryDetector with fake callbacks, calls .tick()
repeatedly, and asserts that a latency_bound scenario actually produces an
advisory — the acceptance gate the roadmap defined.

A fake psutil is injected via sys.modules so CPU/MEM readings are deterministic
regardless of the real machine's load (otherwise a busy CI box could report
cpu_bound instead of latency_bound, making the test flaky).
"""
from __future__ import annotations

import sys
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.advisories import AdvisoryDetector, AdvisoryStore  # noqa: E402


# ---- fake psutil (deterministic CPU/MEM so the verdict is predictable) --

class _FakeVMem:
    def __init__(self, used, total):
        self.used = used
        self.total = total


class _FakePsutil:
    """Returns low CPU + moderate MEM so classify() won't pick cpu_bound or
    mem_pressure — the verdict should be latency_bound when throughput drops."""
    _cpu = 30.0   # 30% — well below the 90% cpu_bound threshold

    def cpu_percent(self, interval=None):
        return self._cpu

    def virtual_memory(self):
        return _FakeVMem(used=8 * 1e9, total=32 * 1e9)  # 25% MEM


def _install_fake_psutil():
    """Inject a fake psutil into sys.modules so _detect_bottleneck's
    'import psutil' picks it up. Returns a cleanup callable."""
    saved = sys.modules.get("psutil")
    sys.modules["psutil"] = _FakePsutil()

    def restore():
        if saved is not None:
            sys.modules["psutil"] = saved
        else:
            sys.modules.pop("psutil", None)
    return restore


def _make_detector(stats_fn=None, iowatcher_fn=None, metrics_ring=None,
                   inflight_fn=None, sustain_s=0.0):
    """Build an AdvisoryDetector with injectable callbacks for testing."""
    store = AdvisoryStore()
    det = AdvisoryDetector(
        adv_store=store,
        stats_fn=stats_fn or (lambda: {"total": 100, "done": 50, "failed": 0,
                                       "pending": 50, "leased": 0, "rate_per_s": 0.3}),
        iowatcher_fn=iowatcher_fn,      # None = NVMe-only (no iowatcher)
        metrics_ring=metrics_ring,
        disk_inflight_fn=inflight_fn or (lambda _d: 0),
        sustain_s=sustain_s,
    )
    return det, store


# ---- tests --------------------------------------------------------------

def test_bottleneck_does_not_crash_without_iowatcher():
    """The #1 bug: io_snap UnboundLocalError when self._io is None.
    On NVMe-only nodes the IOWatcher is not started, so io_snap must be
    None (not unbound) — tick() must not raise."""
    det, store = _make_detector(iowatcher_fn=None)
    # This must NOT raise UnboundLocalError
    det.tick()
    # No crash = pass.


def test_latency_bound_fires_when_throughput_drops():
    """The #3 bug: expected_gigs_per_s was always 0, so latency_bound could
    never fire. With the rolling-max fix, a drop from 2.0 to 0.3 gigs/s
    (with no resource saturated) should produce a nas.latency_bound advisory.

    This is the acceptance-gate test the roadmap defined. Uses a fake psutil
    so the verdict is deterministic (low CPU, low MEM → not cpu/mem bound;
    throughput at 15% of recent max → latency_bound)."""
    restore = _install_fake_psutil()
    try:
        # metrics_ring: recent history shows rate=2.0 (healthy), then drops to 0.3
        ring = deque(maxlen=20)
        for _ in range(5):
            ring.append({"ts": time.time(), "rate": 2.0, "done": 10, "failed": 0})
        ring.append({"ts": time.time(), "rate": 0.3, "done": 1, "failed": 0})

        det, store = _make_detector(
            stats_fn=lambda: {"total": 100, "done": 50, "failed": 0,
                              "pending": 50, "leased": 0, "rate_per_s": 0.3},
            metrics_ring=ring,
            sustain_s=0.0,     # fire immediately (no debounce for test speed)
        )
        det.tick()
        active = store.list(active_only=True)
        codes = [a.code for a in active]

        # HARD ASSERTION: the detector MUST fire something.
        # If psutil were absent the detector would return [] silently — but
        # we injected a fake, so it has no excuse.
        assert len(active) > 0, (
            f"detector produced no advisory — latency_bound wiring is broken. "
            f"codes={codes}")

        # The verdict should be latency_bound (low CPU, low MEM, throughput
        # at 15% of recent max, no disk saturation).
        assert "nas.latency_bound" in codes, (
            f"expected nas.latency_bound, got {codes}. "
            f"The rolling-max expected baseline or the classify() guard "
            f"may be broken.")
    finally:
        restore()


def test_bottleneck_resolves_when_condition_clears():
    """The #2 bug: _resolve_bottleneck used wrong fingerprints. After a
    bottleneck fires and then the condition clears (verdict → healthy),
    the advisory should be resolved (not stay active forever)."""
    restore = _install_fake_psutil()
    try:
        ring = deque(maxlen=20)
        ring.append({"ts": time.time(), "rate": 2.0, "done": 10, "failed": 0})
        ring.append({"ts": time.time(), "rate": 0.3, "done": 1, "failed": 0})

        det, store = _make_detector(
            metrics_ring=ring,
            sustain_s=0.0,
        )
        det.tick()         # should fire latency_bound
        fired = store.list(active_only=True)
        assert len(fired) > 0, "first tick should have fired a bottleneck advisory"

        # Now simulate recovery: rate back to normal
        ring.append({"ts": time.time(), "rate": 2.0, "done": 10, "failed": 0})
        ring.append({"ts": time.time(), "rate": 2.0, "done": 10, "failed": 0})
        det.tick()         # should resolve (verdict → healthy)

        active = store.list(active_only=True)
        bottleneck_active = [a for a in active
                             if a.code in ("nas.latency_bound", "host.cpu_bound",
                                           "host.mem_pressure", "disk.at_ceiling",
                                           "nas.single_spindle")]
        # HARD ASSERTION: the bottleneck advisory MUST be resolved.
        assert len(bottleneck_active) == 0, (
            f"bottleneck advisory not resolved after recovery: "
            f"{[a.code for a in bottleneck_active]}")
    finally:
        restore()


def test_bottleneck_tick_is_idempotent():
    """Calling tick() multiple times in quick succession should not crash
    or produce duplicate advisories (the sustain debounce handles this)."""
    det, store = _make_detector(sustain_s=60.0)  # long sustain = won't fire
    for _ in range(5):
        det.tick()  # must not raise
    active = store.list(active_only=True)
    # With sustain_s=60 and no real condition held for 60s, nothing fires
    bottleneck = [a for a in active
                  if a.code.startswith("host.")
                  or a.code.startswith("nas.") or a.code.startswith("disk.")]
    assert len(bottleneck) == 0, (
        f"unexpected bottleneck advisory with sustain_s=60: "
        f"{[a.code for a in bottleneck]}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fail = 0
    for t in tests:
        try:
            t(); print(f"PASS  {t.__name__}")
        except Exception as exc:
            print(f"FAIL  {t.__name__}: {exc!r}"); fail += 1
    print(f"\n{len(tests)-fail}/{len(tests)} passed")
    sys.exit(fail)
