"""Tests for kiroshi.bottleneck — the per-moment pressure classifier.

The pure ``classify()`` function is the acceptance gate for P2. Each test
constructs a synthetic ``ResourceSample`` for a specific scenario and
asserts the verdict bucket + hint. The ``latency_bound`` test is the
critical one — it's the case the naive argmax(util) classifier gets wrong.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.bottleneck import (  # noqa: E402
    classify, ResourceSample, DiskPressure, Ceilings, Verdict,
    CPU_BOUND, MEM_PRESSURE, DISK_AT_CEILING, NAS_SINGLE_SPINDLE,
    NET_BOUND, GPU_BOUND, VRAM_PRESSURE, LATENCY_BOUND, HEALTHY,
)


def _sample(**kw) -> ResourceSample:
    """Convenience: build a sample with sane defaults, override per-test."""
    defaults = dict(
        cpu_pct=30.0, cpu_cores=8,
        mem_used_gb=8.0, mem_total_gb=32.0,
        disks=[], net_mbps=10.0, net_cap_mbps=1000.0,
        gpu_util_pct=0.0, vram_used_gb=0.0, vram_total_gb=0.0,
        observed_gigs_per_s=0.0, expected_gigs_per_s=0.0,
    )
    defaults.update(kw)
    return ResourceSample(**defaults)


# ---- the critical acceptance gate: latency_bound ------------------------

def test_latency_bound_nothing_saturated_but_slow():
    """THE test: everything at ~50%, throughput at 30% of expected →
    latency_bound, NOT healthy, NOT a false CPU/disk verdict."""
    s = _sample(
        cpu_pct=50.0, mem_used_gb=16.0, mem_total_gb=32.0,
        net_mbps=50.0, net_cap_mbps=1000.0,
        observed_gigs_per_s=0.3, expected_gigs_per_s=1.0,
    )
    v = classify(s)
    assert v.verdict == LATENCY_BOUND, f"expected latency_bound, got {v.verdict}"
    assert "latency" in v.hint.lower()
    assert "round-trip" in v.hint.lower() or "latency" in v.detail.lower()


def test_latency_bound_not_triggered_when_throughput_is_fine():
    """If observed throughput is ≥ 50% of expected, NOT latency_bound."""
    s = _sample(
        cpu_pct=50.0,
        observed_gigs_per_s=0.8, expected_gigs_per_s=1.0,
    )
    v = classify(s)
    assert v.verdict == HEALTHY


def test_latency_bound_not_triggered_when_a_resource_IS_saturated():
    """If CPU is at 95%, it's cpu_bound — not latency_bound, even if
    throughput is also low."""
    s = _sample(
        cpu_pct=95.0,
        observed_gigs_per_s=0.2, expected_gigs_per_s=1.0,
    )
    v = classify(s)
    assert v.verdict == CPU_BOUND


# ---- CPU bound ----------------------------------------------------------

def test_cpu_bound():
    s = _sample(cpu_pct=95.0)
    v = classify(s)
    assert v.verdict == CPU_BOUND
    assert "CPU" in v.detail


# ---- MEM pressure -------------------------------------------------------

def test_mem_pressure():
    s = _sample(mem_used_gb=30.0, mem_total_gb=32.0)
    v = classify(s)
    assert v.verdict == MEM_PRESSURE
    assert "MEM" in v.detail


# ---- disk at ceiling ----------------------------------------------------

def test_disk_at_ceiling_throughput():
    """Disk MBps ≥ 90% of benchmarked peak → disk_at_ceiling."""
    s = _sample(disks=[
        DiskPressure(disk_id="disk3", util_pct=80, mbps=145, ceiling_mbps=150),
    ])
    v = classify(s)
    assert v.verdict == DISK_AT_CEILING
    assert "disk3" in v.dominant_resource
    assert "ceiling" in v.hint.lower()


def test_disk_at_ceiling_util():
    """Disk util ≥ 95% (no bench ceiling known) → disk_at_ceiling."""
    s = _sample(disks=[
        DiskPressure(disk_id="disk1", util_pct=98, mbps=0, ceiling_mbps=0),
    ])
    v = classify(s)
    assert v.verdict == DISK_AT_CEILING


def test_disk_not_at_ceiling_when_well_below_bench():
    """Disk at 20/150 MB/s → NOT disk_at_ceiling (it's something else)."""
    s = _sample(disks=[
        DiskPressure(disk_id="disk1", util_pct=15, mbps=20, ceiling_mbps=150),
    ])
    v = classify(s)
    assert v.verdict != DISK_AT_CEILING


# ---- NAS single-spindle (routing knowledge — Kiroshi-only) --------------

def test_nas_single_spindle():
    """1 hot-but-not-at-ceiling disk + 2 idle → nas_single_spindle.
    (If the disk WERE at ceiling, disk_at_ceiling fires first — single-spindle
    is about routing imbalance, not throughput saturation.)"""
    s = _sample(disks=[
        DiskPressure(disk_id="disk3", util_pct=70, mbps=100, ceiling_mbps=150,
                     inflight=8),
        DiskPressure(disk_id="disk1", util_pct=5, mbps=2, ceiling_mbps=150,
                     inflight=0),
        DiskPressure(disk_id="disk2", util_pct=3, mbps=1, ceiling_mbps=150,
                     inflight=0),
        DiskPressure(disk_id="disk4", util_pct=5, mbps=2, ceiling_mbps=150,
                     inflight=0),
    ])
    v = classify(s)
    assert v.verdict == NAS_SINGLE_SPINDLE
    assert "disk3" in v.dominant_resource
    assert "spread" in v.hint.lower()


def test_nas_single_spindle_not_triggered_when_disks_are_balanced():
    """3 hot disks → NOT single-spindle (I/O is spread)."""
    s = _sample(disks=[
        DiskPressure(disk_id="disk1", util_pct=85, mbps=130, ceiling_mbps=150,
                     inflight=5),
        DiskPressure(disk_id="disk2", util_pct=82, mbps=125, ceiling_mbps=150,
                     inflight=5),
        DiskPressure(disk_id="disk3", util_pct=88, mbps=135, ceiling_mbps=150,
                     inflight=5),
    ])
    v = classify(s)
    assert v.verdict != NAS_SINGLE_SPINDLE


# ---- net bound ----------------------------------------------------------

def test_net_bound():
    s = _sample(net_mbps=950, net_cap_mbps=1000)
    v = classify(s)
    assert v.verdict == NET_BOUND


# ---- GPU / VRAM (P3 — tests pass even without pynvml installed) --------

def test_vram_pressure():
    s = _sample(gpu_util_pct=60, vram_used_gb=23, vram_total_gb=24)
    v = classify(s)
    assert v.verdict == VRAM_PRESSURE
    assert "VRAM" in v.detail


def test_gpu_bound():
    s = _sample(gpu_util_pct=98, vram_used_gb=10, vram_total_gb=24)
    v = classify(s)
    assert v.verdict == GPU_BOUND


# ---- healthy ------------------------------------------------------------

def test_healthy():
    s = _sample(cpu_pct=20, mem_used_gb=8, mem_total_gb=32)
    v = classify(s)
    assert v.verdict == HEALTHY
    assert v.hint == ""


# ---- priority ordering --------------------------------------------------

def test_vram_takes_priority_over_cpu():
    """VRAM pressure is detected before CPU (GPU OOM is catastrophic)."""
    s = _sample(cpu_pct=95, gpu_util_pct=50, vram_used_gb=23, vram_total_gb=24)
    v = classify(s)
    assert v.verdict == VRAM_PRESSURE


def test_disk_ceiling_takes_priority_over_cpu():
    """Disk at ceiling is more actionable than high CPU."""
    s = _sample(cpu_pct=85, disks=[
        DiskPressure(disk_id="disk3", util_pct=98, mbps=145, ceiling_mbps=150),
    ])
    v = classify(s)
    assert v.verdict == DISK_AT_CEILING


# ---- pressures map ------------------------------------------------------

def test_pressures_map_is_normalized_0_to_1():
    s = _sample(cpu_pct=50, mem_used_gb=16, mem_total_gb=32)
    v = classify(s)
    assert 0.0 <= v.pressures["cpu"] <= 1.0
    assert 0.0 <= v.pressures["mem"] <= 1.0
    assert abs(v.pressures["cpu"] - 0.5) < 1e-9
    assert abs(v.pressures["mem"] - 0.5) < 1e-9


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
