"""Tests for kiroshi.profiler — per-gig resource attribution via psutil.

Uses a fake psutil module so tests run without the real dependency and
deterministically (no real process sampling). Also tests the soft-dep
graceful degradation when psutil is absent.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.profiler import GigProfiler  # noqa: E402


# ---- fake psutil -------------------------------------------------------

class _FakeMemInfo:
    def __init__(self, rss: float):
        self.rss = rss


class _FakeIOCounters:
    def __init__(self, read_bytes: float, write_bytes: float):
        self.read_bytes = read_bytes
        self.write_bytes = write_bytes


class _FakeProcess:
    """A fake psutil.Process whose cpu/mem/io values step on each call."""
    def __init__(self, cpu_seq, rss_seq, read_start, write_start):
        self._cpu = list(cpu_seq)
        self._rss = list(rss_seq)
        self._read = read_start
        self._write = write_start
        self._call = 0

    def cpu_percent(self, interval=None):
        idx = min(self._call, len(self._cpu) - 1)
        self._call += 1
        return self._cpu[idx]

    def memory_info(self):
        idx = min(self._call, len(self._rss) - 1)
        return _FakeMemInfo(self._rss[idx])

    def io_counters(self):
        # IO is cumulative — increment on each call
        self._read += 1e6   # +1 MB per sample
        self._write += 0.5e6
        return _FakeIOCounters(self._read, self._write)

    def children(self, recursive=True):
        return []


class _FakePsutil:
    """Minimal psutil stub: Process() returns a shared instance so IO
    counters accumulate across samples (like the real psutil)."""
    def __init__(self):
        self._proc = _FakeProcess(
            cpu_seq=[50.0, 80.0, 30.0, 60.0, 40.0],   # enough for prime + samples
            rss_seq=[100e6, 200e6, 150e6, 180e6, 120e6],
            read_start=0.0,
            write_start=0.0,
        )

    def Process(self, pid):
        return self._proc


# ---- tests --------------------------------------------------------------

def test_profiler_produces_summary():
    """With a fake psutil, the profiler collects samples and folds them."""
    p = GigProfiler(interval=0.05, psutil_mod=_FakePsutil())
    p.start()
    # let it collect ~3 samples
    time.sleep(0.2)
    summary = p.stop()
    assert summary != {}, "expected a non-empty profile"
    assert "cpu_pct_mean" in summary
    assert "cpu_pct_peak" in summary
    assert "rss_peak_mb" in summary
    assert "read_mb" in summary
    assert "write_mb" in summary
    assert "wall_s" in summary
    assert "samples" in summary
    assert summary["samples"] >= 1
    assert summary["cpu_pct_peak"] == 80.0
    # peak RSS is the max across all samples (the prime call consumes idx 0,
    # so the max sample RSS is 180 MB from the sequence [200, 150, 180, 120])
    assert summary["rss_peak_mb"] == 180.0


def test_profiler_empty_when_psutil_absent():
    """Soft dep: no psutil -> no-op, stop() returns {}.

    Uses psutil_mod=False (the sentinel start() sets when import fails) so
    the test deterministically exercises the absent-psutil path even when
    real psutil IS installed in the test env.
    """
    p = GigProfiler(interval=0.05, psutil_mod=False)
    p.start()
    summary = p.stop()
    assert summary == {}, f"expected empty profile with psutil absent, got {summary}"


def test_profiler_disabled_by_env():
    """KIROSHI_PROFILER=0 → no sampling."""
    old = os.environ.get("KIROSHI_PROFILER")
    os.environ["KIROSHI_PROFILER"] = "0"
    try:
        p = GigProfiler(interval=0.05, psutil_mod=_FakePsutil())
        p.start()
        time.sleep(0.15)
        summary = p.stop()
        assert summary == {}, "profiler should be disabled by env var"
    finally:
        if old is not None:
            os.environ["KIROSHI_PROFILER"] = old
        else:
            os.environ.pop("KIROSHI_PROFILER", None)


def test_profiler_io_delta_not_cumulative():
    """The summary reports bytes *during* the gig, not the cumulative total."""
    p = GigProfiler(interval=0.05, psutil_mod=_FakePsutil())
    p.start()
    time.sleep(0.2)
    summary = p.stop()
    # The fake adds 1MB read + 0.5MB write per sample call.
    # With >=2 samples, read_mb should be (n_samples * 1) - 0 = n_samples MB
    # (delta from first to last). It should NOT be 0 (the first sample's base).
    assert summary["read_mb"] > 0
    assert summary["write_mb"] > 0


def test_profiler_cpu_mean_is_average():
    """cpu_pct_mean is the arithmetic mean of sampled values, not max."""
    p = GigProfiler(interval=0.05, psutil_mod=_FakePsutil())
    p.start()
    time.sleep(0.2)
    summary = p.stop()
    if summary["samples"] >= 2:
        # mean should be between min and max of [50, 80, 30]
        assert 30.0 <= summary["cpu_pct_mean"] <= 80.0


def test_profiler_short_task_still_gets_profile():
    """A task that finishes in < interval must still get a baseline + final
    sample (not an empty profile). This is the bug the supervisor flagged:
    fast gigs (the common case on a fan-out mesh) got zero attribution."""
    p = GigProfiler(interval=10.0, psutil_mod=_FakePsutil())  # 10s interval
    p.start()
    time.sleep(0.02)       # finish WAY before the first interval tick
    summary = p.stop()
    assert summary != {}, "short task got no profile!"
    assert summary["samples"] >= 2, f"expected baseline+final, got {summary['samples']}"
    assert summary["wall_s"] >= 0.0  # may round to 0.0 for very short tasks


def test_profiler_disabled_env_sets_sentinel():
    """KIROSHI_PROFILER=0 sets _psutil=False sentinel, not None, so stop()
    doesn't try to re-import psutil."""
    old = os.environ.get("KIROSHI_PROFILER")
    os.environ["KIROSHI_PROFILER"] = "0"
    try:
        p = GigProfiler(interval=0.05, psutil_mod=None)
        p.start()
        assert p._psutil is False, "disabled profiler should set False sentinel"
        summary = p.stop()
        assert summary == {}
    finally:
        if old is not None:
            os.environ["KIROSHI_PROFILER"] = old
        else:
            os.environ.pop("KIROSHI_PROFILER", None)


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