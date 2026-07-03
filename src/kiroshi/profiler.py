"""kiroshi.profiler — per-gig resource attribution via psutil.

Samples a worker process's CPU, memory, and I/O **during** a gig's execution,
then folds a compact summary into the gig's ``metrics``. This is the
foundation (P1) of the bottleneck-detection feature: it answers *"what did
each job use?"* without any cross-process PID mapping (the profiler runs
inside the worker process that executes the task).

**Soft dependency on psutil:** if psutil is not installed, the profiler is a
no-op (returns an empty dict). Install with ``pip install kiroshi[profiler]``
to enable per-gig attribution.

Design:
  * A daemon thread samples ``psutil.Process(os.getpid())`` + its children
    every ``interval`` seconds (default 3s — coarse, negligible overhead).
  * On stop, it folds the samples into a compact summary:
    ``{cpu_pct_mean, cpu_pct_peak, rss_peak_mb, read_mb, write_mb, wall_s, samples}``
  * IO counters are cumulative (psutil convention); the profiler reports the
    delta between the first and last sample = bytes moved during the gig.
  * ``cpu_percent(interval=None)`` is primed at start so the first real sample
    is meaningful (psutil's first call always returns 0.0 otherwise).
  * Disabled via ``KIROSHI_PROFILER=0`` env var (operator kill switch).

This module is imported by ``pool._run_one`` which runs inside the
ProcessPoolExecutor worker — so it lives entirely in the worker process, no
pickling concerns.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

_INTERVAL_DEFAULT = 3.0


class GigProfiler:
    """Sample a worker process's resources during a gig's execution.

    Usage::

        p = GigProfiler()
        p.start()
        try:
            result = task_fn(spec)
        finally:
            profile = p.stop()
        result["metrics"]["proc"] = profile
    """

    def __init__(self, interval: float = _INTERVAL_DEFAULT,
                 psutil_mod: Any = None):
        """``psutil_mod`` is for test injection; production leaves it None
        and the real psutil is imported lazily in :meth:`start`."""
        self.interval = interval
        self._psutil: Any = psutil_mod  # None → import lazily; False → forced off
        self._thread: Optional[threading.Thread] = None
        self._stop: Optional[threading.Event] = None
        self._samples: list[dict[str, float]] = []
        self._t0: float = 0.0

    def _collect_sample(self) -> None:
        """Take one sample and append it. Safe to call from any thread."""
        ps = self._psutil
        if not ps:
            return
        try:
            proc = ps.Process(os.getpid())
            cpu = proc.cpu_percent(interval=None)
            mem = proc.memory_info().rss
            io = proc.io_counters()
            read_bytes = io.read_bytes
            write_bytes = io.write_bytes
            for child in proc.children(recursive=True):
                try:
                    cpu += child.cpu_percent(interval=None)
                    mem += child.memory_info().rss
                    cio = child.io_counters()
                    read_bytes += cio.read_bytes
                    write_bytes += cio.write_bytes
                except Exception:  # noqa: BLE001
                    pass
            self._samples.append({
                "cpu_pct": cpu,
                "rss_bytes": float(mem),
                "read_bytes": float(read_bytes),
                "write_bytes": float(write_bytes),
                "ts": time.time(),
            })
        except Exception:  # noqa: BLE001
            pass

    def start(self) -> None:
        """Begin sampling. If psutil is unavailable or profiling is disabled,
        this is a no-op (subsequent :meth:`stop` returns ``{}``)."""
        if os.environ.get("KIROSHI_PROFILER", "1") == "0":
            self._psutil = False          # sentinel: definitely off
            return
        if self._psutil is None:
            try:
                import psutil
                self._psutil = psutil
            except ImportError:
                self._psutil = False      # sentinel: import failed
                return                        # soft dep — no-op

        self._stop = threading.Event()
        self._t0 = time.time()
        # Prime cpu_percent so the first real sample is meaningful (psutil
        # returns 0.0 on the very first call).
        try:
            self._psutil.Process(os.getpid()).cpu_percent(interval=None)
        except Exception:  # noqa: BLE001
            pass
        # Take a baseline sample immediately so even a task that finishes
        # in <interval gets at least one data point.
        self._collect_sample()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                         name="kiroshi-profiler")
        self._thread.start()

    def _loop(self) -> None:
        """Sample on each interval until ``stop`` is signaled."""
        while not self._stop.wait(self.interval):
            self._collect_sample()

    def stop(self) -> dict[str, Any]:
        """Stop sampling and return a compact summary dict.

        Returns ``{}`` if psutil was unavailable or profiling was disabled.
        Takes one final sample before folding so even a task that finishes
        between intervals gets a baseline + final data point.
        """
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 2)
        # Take a final sample if we have psutil (covers short tasks that
        # finished before the first interval tick fired).
        if self._psutil and self._psutil is not False:
            self._collect_sample()
        if not self._samples:
            return {}
        cpu_vals = [s["cpu_pct"] for s in self._samples]
        rss_vals = [s["rss_bytes"] for s in self._samples]
        wall = time.time() - self._t0 if self._t0 else 0.0
        # IO is cumulative — delta = bytes during the profiling window
        read = self._samples[-1]["read_bytes"] - self._samples[0]["read_bytes"]
        write = self._samples[-1]["write_bytes"] - self._samples[0]["write_bytes"]
        return {
            "cpu_pct_mean": round(sum(cpu_vals) / len(cpu_vals), 1),
            "cpu_pct_peak": round(max(cpu_vals), 1),
            "rss_peak_mb": round(max(rss_vals) / 1e6, 1),
            "read_mb": round(read / 1e6, 1),
            "write_mb": round(write / 1e6, 1),
            "wall_s": round(wall, 1),
            "samples": len(self._samples),
        }
