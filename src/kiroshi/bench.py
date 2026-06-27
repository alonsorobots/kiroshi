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
