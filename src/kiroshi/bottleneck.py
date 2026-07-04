"""kiroshi.bottleneck — per-moment dominant-pressure classifier.

The smart part of the attribution feature (P2 of ROADMAP_ATTRIBUTION.local.md).
At each sample tick, fuses host-level resource gauges (CPU, MEM, per-disk
I/O from ``iowatcher``, optional GPU) with Kiroshi's routing knowledge
(``disk_inflight`` distribution) and bench-calibrated ceilings to produce a
**verdict**: which resource is the dominant pressure, or whether the workload
is latency-bound (slow but nothing is saturated — the most common real case
for NAS work).

This is a **heuristic**, not ground truth. True critical-path attribution is
a research problem (a job can be lock-bound or latency-bound with every gauge
at 50%). We claim "dominant resource pressure, with a headroom/latency
verdict" — never "THE bottleneck." The verdict is actionable: each bucket
carries a ``hint`` string an operator (or advisory) can act on.

The classifier is a **pure function** (no I/O) so it's unit-testable with
synthetic samples — the tests in ``tests/test_bottleneck.py`` are the
acceptance gate, especially the ``latency_bound`` case.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---- data structures ----------------------------------------------------

@dataclass
class DiskPressure:
    disk_id: str
    util_pct: float           # 0-100 from iowatcher
    mbps: float               # achieved throughput
    ceiling_mbps: float       # benchmarked peak (from bench.py); 0 = unknown
    inflight: int = 0         # gigs currently reading/writing this disk


@dataclass
class ResourceSample:
    """One point-in-time snapshot of all host resources."""
    cpu_pct: float = 0.0          # aggregate CPU utilization (0-100)
    cpu_cores: int = 1            # logical core count (for normalization)
    mem_used_gb: float = 0.0
    mem_total_gb: float = 1.0
    disks: list[DiskPressure] = field(default_factory=list)
    net_mbps: float = 0.0
    net_cap_mbps: float = 1000.0  # link speed; default 1 GbE
    gpu_util_pct: float = 0.0     # 0-100; 0 if no GPU
    vram_used_gb: float = 0.0
    vram_total_gb: float = 0.0    # 0 = no GPU
    # observed throughput (gigs/s) — used for latency_bound detection
    observed_gigs_per_s: float = 0.0
    expected_gigs_per_s: float = 0.0  # 0 = unknown; if set, used for latency check


@dataclass
class Ceilings:
    """Benchmarked/calibrated limits for pressure comparison."""
    cpu_pct: float = 90.0         # above this = CPU-bound
    mem_pct: float = 90.0         # above this = MEM pressure
    disk_util_pct: float = 95.0   # above this = disk at ceiling
    disk_throughput_frac: float = 0.9  # fraction of benchmarked peak = at ceiling
    net_frac: float = 0.9         # fraction of link cap = net-bound
    gpu_util_pct: float = 95.0
    vram_pct: float = 90.0
    # for latency_bound: if observed throughput < this fraction of expected
    # AND no resource is above its ceiling → latency_bound
    latency_throughput_frac: float = 0.5


@dataclass
class Verdict:
    """Classification result for one sample."""
    verdict: str                   # one of the bucket names below
    dominant_resource: str         # "cpu", "mem", "disk:disk3", "net", "gpu", "vram", "latency", "none"
    pressures: dict[str, float]    # resource -> 0..1 normalized pressure
    hint: str                      # actionable suggestion
    detail: str                    # human-readable explanation


# verdict bucket names
CPU_BOUND = "cpu_bound"
MEM_PRESSURE = "mem_pressure"
DISK_AT_CEILING = "disk_at_ceiling"
NAS_SINGLE_SPINDLE = "nas_single_spindle"
NET_BOUND = "net_bound"
GPU_BOUND = "gpu_bound"
VRAM_PRESSURE = "vram_pressure"
LATENCY_BOUND = "latency_bound"
HEALTHY = "healthy"


# ---- the pure classifier ------------------------------------------------

def classify(sample: ResourceSample, ceilings: Optional[Ceilings] = None) -> Verdict:
    """Classify the dominant resource pressure from a single sample.

    Returns a :class:`Verdict` with the bucket name, a normalized pressure
    map (0..1 per resource), and an actionable hint.

    Priority (first match wins — a resource at its ceiling is more actionable
    than a lower-pressure one):
      1. ``vram_pressure`` — VRAM ≥ 90% (GPU tasks OOM catastrophically)
      2. ``gpu_bound`` — GPU util ≥ 95%
      3. ``mem_pressure`` — MEM ≥ 90%
      4. ``disk_at_ceiling`` — a disk's MBps ≥ 90% of benchmarked peak,
         OR util ≥ 95%
      5. ``nas_single_spindle`` — I/O concentrated on 1 disk while ≥2 others
         are idle AND that disk is pressured (routing knowledge)
      6. ``net_bound`` — network ≥ 90% of link cap
      7. ``cpu_bound`` — CPU ≥ 90% (normalized to core count)
      8. ``latency_bound`` — throughput low BUT no resource ≥ its ceiling
      9. ``healthy`` — throughput near expected, nothing pressured

    The ``latency_bound`` bucket is the **critical acceptance gate** — it's
    the case the naive ``argmax(util)`` classifier gets wrong (reports
    "healthy" when the operator most needs an answer).
    """
    c = ceilings or Ceilings()

    # ---- compute per-resource pressures (0..1) ----
    pressures: dict[str, float] = {}
    pressures["cpu"] = min(sample.cpu_pct / 100.0, 1.0)
    pressures["mem"] = (sample.mem_used_gb / sample.mem_total_gb
                        if sample.mem_total_gb > 0 else 0.0)
    pressures["net"] = (sample.net_mbps / sample.net_cap_mbps
                        if sample.net_cap_mbps > 0 else 0.0)
    if sample.vram_total_gb > 0:
        pressures["vram"] = sample.vram_used_gb / sample.vram_total_gb
        pressures["gpu"] = min(sample.gpu_util_pct / 100.0, 1.0)
    else:
        pressures["vram"] = 0.0
        pressures["gpu"] = 0.0

    # disk pressures
    hot_disks: list[DiskPressure] = []       # at or near ceiling
    pressured_disks: list[DiskPressure] = [] # active but not at ceiling
    idle_disks: list[DiskPressure] = []
    _PRESSURED_UTIL = 50.0     # above this = "actively used" but not necessarily at ceiling
    for d in sample.disks:
        tp_pressure = (d.mbps / d.ceiling_mbps if d.ceiling_mbps > 0
                       else d.util_pct / 100.0)
        util_pressure = d.util_pct / 100.0
        p = max(tp_pressure, util_pressure)
        pressures[f"disk:{d.disk_id}"] = min(p, 1.0)
        if p >= c.disk_throughput_frac or d.util_pct >= c.disk_util_pct:
            hot_disks.append(d)
        elif d.util_pct >= _PRESSURED_UTIL or d.inflight > 0:
            pressured_disks.append(d)
        elif d.inflight == 0 and d.util_pct < 20:
            idle_disks.append(d)

    # ---- classify (priority order) ----
    # 1. VRAM pressure (GPU OOM is catastrophic)
    if pressures["vram"] >= c.vram_pct / 100.0:
        return Verdict(
            VRAM_PRESSURE, "vram", pressures,
            hint="reduce batch size or offload to CPU; VRAM is near full",
            detail=f"VRAM {sample.vram_used_gb:.1f}/{sample.vram_total_gb:.1f} GB "
                   f"({pressures['vram']*100:.0f}%)")

    # 2. GPU bound
    if pressures["gpu"] >= c.gpu_util_pct / 100.0:
        return Verdict(
            GPU_BOUND, "gpu", pressures,
            hint="GPU is the bottleneck — add more GPU nodes or reduce GPU work per sub-job",
            detail=f"GPU util {sample.gpu_util_pct:.0f}%")

    # 3. MEM pressure
    if pressures["mem"] >= c.mem_pct / 100.0:
        return Verdict(
            MEM_PRESSURE, "mem", pressures,
            hint="reduce per-worker memory or add RAM; approaching OOM",
            detail=f"MEM {sample.mem_used_gb:.1f}/{sample.mem_total_gb:.1f} GB "
                   f"({pressures['mem']*100:.0f}%)")

    # 4. Disk at ceiling (bench-calibrated)
    for d in hot_disks:
        ceiling_str = (f"{d.mbps:.0f}/{d.ceiling_mbps:.0f} MB/s"
                        if d.ceiling_mbps > 0
                        else f"util {d.util_pct:.0f}%")
        return Verdict(
            DISK_AT_CEILING, f"disk:{d.disk_id}", pressures,
            hint=f"disk {d.disk_id} is at its benchmarked ceiling — "
                 f"this is the disk, not the code. Stage hot data to NVMe "
                 f"or spread reads across spindles.",
            detail=f"disk {d.disk_id}: {ceiling_str}")

    # 5. NAS single-spindle (routing knowledge — Kiroshi-only)
    # One disk is actively used (but not at ceiling) while ≥2 others sit idle.
    # This is a routing/topology problem, not a throughput problem.
    if len(pressured_disks) == 1 and len(idle_disks) >= 2:
        hot = pressured_disks[0]
        return Verdict(
            NAS_SINGLE_SPINDLE, f"disk:{hot.disk_id}", pressures,
            hint=f"I/O concentrated on {hot.disk_id} while {len(idle_disks)} "
                 f"disks sit idle — spread reads across spindles (check "
                 f"topology match patterns or shard plan)",
            detail=f"hot: {hot.disk_id} (inflight={hot.inflight}), "
                   f"idle: {[d.disk_id for d in idle_disks]}")

    # 6. Net bound
    if pressures["net"] >= c.net_frac:
        return Verdict(
            NET_BOUND, "net", pressures,
            hint="network link is saturated — stage data locally or use a faster link",
            detail=f"net {sample.net_mbps:.0f}/{sample.net_cap_mbps:.0f} MB/s")

    # 7. CPU bound
    if pressures["cpu"] >= c.cpu_pct / 100.0:
        return Verdict(
            CPU_BOUND, "cpu", pressures,
            hint="CPU is the bottleneck — add workers/nodes or optimize the task",
            detail=f"CPU {sample.cpu_pct:.0f}% across {sample.cpu_cores} cores")

    # 8. latency_bound — THE critical bucket
    # Throughput is low BUT no resource is at its ceiling. This is the most
    # common real case for NAS work: SMB round-trip latency, not throughput.
    if (sample.expected_gigs_per_s > 0
            and sample.observed_gigs_per_s
               < sample.expected_gigs_per_s * c.latency_throughput_frac):
        # verify nothing is actually saturated (all pressures < their ceilings)
        max_pressure = max(pressures.values()) if pressures else 0.0
        if max_pressure < c.disk_throughput_frac:
            return Verdict(
                LATENCY_BOUND, "latency", pressures,
                hint="work is slow but no resource is near its ceiling — "
                     "likely round-trip latency (SMB metadata ops) or lock "
                     "contention, not throughput. Batch ops, stage to a "
                     "lower-latency tier (NVMe), or increase I/O depth.",
                detail=f"observed {sample.observed_gigs_per_s:.1f}/"
                       f"{sample.expected_gigs_per_s:.1f} gigs/s, "
                       f"max pressure {max_pressure*100:.0f}%")

    # 9. healthy
    return Verdict(
        HEALTHY, "none", pressures,
        hint="",
        detail="all resources within headroom")
