"""TRUE throughput — measured from output-file mtimes, not wall-clock.

The canonical metric is ``(last_output_mtime - first_output_mtime) / item_count``.
File timestamps capture real end-to-end throughput including worker coordination,
I/O contention, and scheduling overhead. Wall-clock and per-item timers are
misleading once many workers run concurrently (they include warmup/teardown and
hide queueing). Always report TRUE rate, or clearly label a metric as something
else (e.g. "wall rate").
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class TrueRate:
    first_mtime: float
    last_mtime: float
    count: int

    @property
    def span_s(self) -> float:
        return max(0.0, self.last_mtime - self.first_mtime)

    @property
    def items_per_s(self) -> float:
        return self.count / self.span_s if self.span_s > 0 and self.count > 0 else 0.0

    def __str__(self) -> str:
        if self.count == 0 or self.span_s <= 0:
            return f"TrueRate(count={self.count}, span=0.0s, rate=n/a)"
        return (f"TrueRate(count={self.count}, span={self.span_s:.1f}s, "
                f"rate={self.items_per_s:.2f}/s)")


def rate_from_files(paths: Iterable[os.PathLike[str] | str]) -> TrueRate:
    first = float("inf")
    last = 0.0
    count = 0
    for p in paths:
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        first = min(first, m)
        last = max(last, m)
        count += 1
    if count == 0:
        return TrueRate(0.0, 0.0, 0)
    return TrueRate(first, last, count)


def rate_from_dir(root: os.PathLike[str] | str, pattern: str = "*", recursive: bool = True) -> TrueRate:
    root = Path(root)
    if not root.exists():
        return TrueRate(0.0, 0.0, 0)
    globber = root.rglob if recursive else root.glob
    return rate_from_files(p for p in globber(pattern) if p.is_file())


# ---- concurrency calibration -------------------------------------------

# Bias thresholds: fraction of peak throughput at which we consider the knee
# "reached". conservative stays below the knee (fewer slots, less contention);
# aggressive pushes to the peak; balanced is the default middle ground.
_BIAS_THRESHOLDS = {
    "conservative": 0.85,
    "balanced": 0.90,
    "aggressive": 1.0,
}


def suggest_concurrency(
    samples: list[tuple[int, float]],
    bias: str = "balanced",
) -> int:
    """Pick a per-disk ``concurrency`` from throughput-vs-concurrency samples.

    ``samples`` is a list of ``(concurrency, throughput_Mbps)`` pairs — e.g.
    measured by ``kiroshi nas benchmark`` or observed during a campaign. The
    function finds the **knee**: the lowest concurrency where throughput
    reaches a fraction of the peak, determined by ``bias``:

      * ``conservative`` — 90% of peak (stays below the saturation cliff)
      * ``balanced``     — 95% of peak (at the knee; recommended default)
      * ``aggressive``   — 100% of peak (pushes to max throughput, risks thrash)

    If throughput *declines* past a point (oversaturation), the conservative
    and balanced biases naturally back off because the peak is before the
    decline. If all samples are monotonically rising (no knee found), returns
    the highest tested concurrency.
    """
    if not samples:
        return 4                        # safe default
    samples = sorted(samples, key=lambda s: s[0])    # sort by concurrency
    max_mbps = max(s[1] for s in samples)
    threshold = max_mbps * _BIAS_THRESHOLDS.get(bias, 0.95)
    for conc, mbps in samples:
        if mbps >= threshold:
            return conc
    return samples[-1][0]               # monotonically rising — push to max tested
