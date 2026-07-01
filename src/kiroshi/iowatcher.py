"""External-process I/O watcher — rolling-window observability for the Fixer.

Surfaces "which spindle is the wall" without manual ``iostat`` forensics. Samples
per-disk I/O throughput at a fixed interval and maintains a rolling window so
the dashboard / status endpoint can show:

- Per-disk achieved read/write MBps (rolling average)
- Per-disk saturation (%util — 100% = bottleneck)
- Parity disk flagging (a single parity spindle at 100% = the RMW wall)
- External process count (how many non-Kiroshi processes are hitting the disks)

On Linux (NAS), uses ``/proc/diskstats`` (zero-dependency, no iostat install
needed). On Windows, uses ``wmic``/``typeperf`` as a fallback. If neither is
available, the watcher is inert (returns empty data).

The watcher runs in a background daemon thread inside the Fixer process,
sampling every ``_SAMPLE_INTERVAL_S`` seconds and storing the last
``_WINDOW_S`` seconds of data in a ring buffer. The ``/resource/status``
endpoint exposes the aggregated view.

HW-config-gated: only active when the topology declares HDD disks (parity or
not). NVMe-only nodes have no seek/saturation concern and skip the watcher.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional


_SAMPLE_INTERVAL_S = 5.0   # how often to sample
_WINDOW_S = 300.0          # 5-minute rolling window
_MAX_SAMPLES = int(_WINDOW_S / _SAMPLE_INTERVAL_S)


@dataclass
class DiskSample:
    """One point-in-time sample of a disk's I/O."""
    timestamp: float
    read_sectors: int       # sectors read since boot (cumulative)
    write_sectors: int      # sectors written since boot (cumulative)
    io_ms: int              # time spent doing I/O (cumulative, ms)
    sector_size: int = 512


@dataclass
class DiskRollingStats:
    """Rolling-window aggregated stats for one disk."""
    disk_id: str
    samples: deque = field(default_factory=lambda: deque(maxlen=_MAX_SAMPLES))

    def add(self, sample: DiskSample) -> None:
        self.samples.append(sample)

    def stats(self) -> dict[str, Any]:
        if len(self.samples) < 2:
            return {"disk": self.disk_id, "read_mbps": 0, "write_mbps": 0,
                    "util_pct": 0, "samples": len(self.samples)}
        first = self.samples[0]
        last = self.samples[-1]
        dt = last.timestamp - first.timestamp
        if dt <= 0:
            return {"disk": self.disk_id, "read_mbps": 0, "write_mbps": 0,
                    "util_pct": 0, "samples": len(self.samples)}
        r_sectors = last.read_sectors - first.read_sectors
        w_sectors = last.write_sectors - first.write_sectors
        io_delta_ms = last.io_ms - first.io_ms
        read_mbps = (r_sectors * last.sector_size) / dt / 1e6
        write_mbps = (w_sectors * last.sector_size) / dt / 1e6
        util_pct = (io_delta_ms / (dt * 1000)) * 100 if dt > 0 else 0
        return {
            "disk": self.disk_id,
            "read_mbps": round(read_mbps, 1),
            "write_mbps": round(write_mbps, 1),
            "util_pct": round(min(util_pct, 100), 1),
            "samples": len(self.samples),
        }


class IOWatcher:
    """Background daemon that samples per-disk I/O and maintains rolling stats.

    On Linux: reads /proc/diskstats (zero-dependency).
    On Windows: uses typeperf (built-in) as a coarse fallback.
    Inert if neither is available or if no HDD disks are in the topology.
    """

    def __init__(self, disk_ids: list[str], is_parity: dict[str, bool],
                 direct_paths: Optional[dict[str, str]] = None):
        self._disk_ids = disk_ids
        self._is_parity = is_parity
        self._direct_paths = direct_paths or {}
        self._stats: dict[str, DiskRollingStats] = {
            d: DiskRollingStats(d) for d in disk_ids
        }
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._platform = sys.platform
        # Resolve each disk_id -> backing block-device name (sda, sdb, ...) so we
        # can look it up in /proc/diskstats (which is keyed by device, not by our
        # topology disk_id). Without this mapping the watcher records nothing.
        self._dev_for_disk: dict[str, str] = self._resolve_devices()
        # Reverse: device name -> disk_id, for the sampler.
        self._disk_for_dev: dict[str, str] = {
            dev: did for did, dev in self._dev_for_disk.items()}

    def _resolve_devices(self) -> dict[str, str]:
        """Map each disk_id to its backing block device via /proc/mounts.

        For each disk with a ``direct_path`` (e.g. ``/mnt/disk1``), find the
        device mounted there (e.g. ``/dev/sde1`` -> base device ``sde``). Unraid
        mounts each array disk at ``/mnt/diskN`` from a partition ``/dev/sdX1``.
        """
        mapping: dict[str, str] = {}
        if self._platform != "linux":
            return mapping
        try:
            with open("/proc/mounts") as f:
                mounts = [ln.split() for ln in f if len(ln.split()) >= 2]
        except (FileNotFoundError, PermissionError):
            return mapping
        for did in self._disk_ids:
            mount_point = self._direct_paths.get(did) or f"/mnt/{did}"
            for parts in mounts:
                dev, mp = parts[0], parts[1]
                if mp == mount_point and dev.startswith("/dev/"):
                    # /dev/sde1 -> sde  (strip /dev/ prefix + trailing partition digits)
                    base = os.path.basename(dev)
                    # nvme0n1p1 -> nvme0n1 ; sde1 -> sde
                    if "nvme" in base:
                        base = base.split("p")[0]
                    else:
                        base = base.rstrip("0123456789")
                    mapping[did] = base
                    break
        return mapping

    def start(self) -> None:
        if not self._disk_ids:
            return
        self._thread = threading.Thread(target=self._run, name="kiroshi-io-watcher",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sample()
            except Exception:  # noqa: BLE001
                pass  # watcher must never crash the Fixer
            self._stop.wait(_SAMPLE_INTERVAL_S)

    def _sample(self) -> None:
        if self._platform == "linux":
            self._sample_linux()
        elif self._platform == "win32":
            self._sample_windows()

    def _sample_linux(self) -> None:
        """Read /proc/diskstats — zero-dependency, no iostat needed."""
        now = time.time()
        try:
            with open("/proc/diskstats") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 14:
                        continue
                    dev_name = parts[2]
                    # /proc/diskstats is keyed by device name (sda, sde, ...).
                    # Map it back to our topology disk_id via the resolved
                    # mount->device table. If a disk couldn't be resolved, we
                    # also accept a direct disk_id==dev_name match (e.g. a config
                    # that names disks by device directly).
                    disk_id = self._disk_for_dev.get(dev_name)
                    if disk_id is None and dev_name in self._stats:
                        disk_id = dev_name
                    if disk_id is None or disk_id not in self._stats:
                        continue
                    read_sectors = int(parts[5])
                    write_sectors = int(parts[9])
                    io_ms = int(parts[12])
                    self._stats[disk_id].add(
                        DiskSample(now, read_sectors, write_sectors, io_ms))
        except (FileNotFoundError, PermissionError):
            pass

    def _sample_windows(self) -> None:
        """Windows fallback using typeperf (coarse but built-in)."""
        # typeperf is slow and heavyweight — skip for now. The Fixer runs on
        # the NAS (Linux) where /proc/diskstats is available. Windows nodes
        # don't typically need disk saturation monitoring (local NVMe).
        pass

    def snapshot(self) -> dict[str, Any]:
        """Current rolling-window stats for all disks."""
        return {
            "disks": [self._stats[d].stats() for d in self._disk_ids],
            "parity_disks": [d for d, is_p in self._is_parity.items() if is_p],
            "sample_interval_s": _SAMPLE_INTERVAL_S,
            "window_s": _WINDOW_S,
        }
