"""Tests for kiroshi.bench — true throughput + concurrency calibration.

TrueRate math + suggest_concurrency knee detection are pure functions;
rate_from_dir touches the filesystem via tmp_path.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi import bench  # noqa: E402


# ---- TrueRate math ------------------------------------------------------

def test_truerate_items_per_s():
    r = bench.TrueRate(first_mtime=100.0, last_mtime=110.0, count=50)
    assert r.span_s == 10.0
    assert abs(r.items_per_s - 5.0) < 1e-9


def test_truerate_zero_span_is_safe():
    r = bench.TrueRate(first_mtime=100.0, last_mtime=100.0, count=5)
    assert r.span_s == 0.0
    assert r.items_per_s == 0.0          # no div-by-zero


def test_truerate_zero_count_is_safe():
    r = bench.TrueRate(0.0, 0.0, 0)
    assert r.items_per_s == 0.0
    assert "n/a" in str(r) or "0" in str(r)


def test_truerate_str_formats():
    r = bench.TrueRate(100.0, 110.0, 50)
    s = str(r)
    assert "50" in s and "5.00" in s


# ---- rate_from_dir ------------------------------------------------------

def test_rate_from_dir_staggered_mtimes():
    with tempfile.TemporaryDirectory() as d:
        for i in range(5):
            p = Path(d) / f"out_{i}.txt"
            p.write_text("x")
            os.utime(p, (1000.0 + i, 1000.0 + i))
        rate = bench.rate_from_dir(d, pattern="*.txt")
        assert rate.count == 5
        assert rate.span_s == 4.0          # out_0 at t=1000, out_4 at t=1004


def test_rate_from_dir_pattern_filter():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "a.txt").write_text("x")
        (Path(d) / "b.log").write_text("x")
        rate = bench.rate_from_dir(d, pattern="*.txt")
        assert rate.count == 1


def test_rate_from_dir_nonexistent_is_zero():
    rate = bench.rate_from_dir("/nonexistent/path/xyz")
    assert rate.count == 0


# ---- suggest_concurrency (the killer feature) ---------------------------

def test_suggest_balanced_picks_knee():
    # throughput saturates at concurrency 4 (95% of peak 150 = 142.5)
    samples = [(1, 50), (2, 95), (4, 140), (8, 150), (16, 130)]
    assert bench.suggest_concurrency(samples, "balanced") == 4


def test_suggest_conservative_backs_off():
    # conservative = 90% of 150 = 135 -> concurrency 4 (140 >= 135)
    samples = [(1, 50), (2, 95), (4, 140), (8, 150), (16, 130)]
    assert bench.suggest_concurrency(samples, "conservative") == 4


def test_suggest_aggressive_picks_peak():
    # aggressive = 100% of peak 150 -> concurrency 8
    samples = [(1, 50), (2, 95), (4, 140), (8, 150), (16, 130)]
    assert bench.suggest_concurrency(samples, "aggressive") == 8


def test_suggest_handles_collapse():
    # throughput collapses past concurrency 4 — should not pick 8 or 16
    samples = [(1, 80), (2, 140), (4, 150), (8, 60), (16, 30)]
    rec = bench.suggest_concurrency(samples, "balanced")
    assert rec <= 4                       # must back off before the collapse


def test_suggest_monotonic_rising_returns_max():
    # no knee found — throughput keeps rising
    samples = [(1, 10), (2, 20), (4, 40), (8, 80)]
    assert bench.suggest_concurrency(samples, "balanced") == 8


def test_suggest_empty_returns_default():
    assert bench.suggest_concurrency([]) == 4


def test_suggest_single_sample():
    assert bench.suggest_concurrency([(3, 100)]) == 3


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
