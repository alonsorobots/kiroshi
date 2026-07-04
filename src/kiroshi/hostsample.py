"""Lightweight host resource sampling for runner heartbeats.

Best-effort: uses psutil when installed (``kiroshi[profiler]``), optional
pynvml (``kiroshi[gpu]``), or ``nvidia-smi`` as a fallback. Never raises.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Optional


def _try_psutil():
    try:
        import psutil  # type: ignore
        return psutil
    except ImportError:
        return None


def _mem_gb() -> tuple[float, float]:
    ps = _try_psutil()
    if ps is not None:
        vm = ps.virtual_memory()
        return vm.used / (1024 ** 3), vm.total / (1024 ** 3)
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64),
                ]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                total = st.ullTotalPhys / (1024 ** 3)
                used = (st.ullTotalPhys - st.ullAvailPhys) / (1024 ** 3)
                return used, total
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            with open("/proc/meminfo", encoding="utf-8") as fh:
                info = {}
                for line in fh:
                    k, v = line.split(":", 1)
                    info[k.strip()] = int(v.split()[0])
            total = info.get("MemTotal", 0) / (1024 ** 2)
            avail = info.get("MemAvailable", info.get("MemFree", 0)) / (1024 ** 2)
            return max(0.0, total - avail), total
        except OSError:
            pass
    return 0.0, 0.0


def _cpu_pct() -> float:
    ps = _try_psutil()
    if ps is not None:
        try:
            return float(ps.cpu_percent(interval=0.0))
        except Exception:  # noqa: BLE001
            pass
    return 0.0


def _runner_tree_rss_gb(root_pid: int) -> float:
    """Working-set RAM of the runner + its child workers (process tree)."""
    ps = _try_psutil()
    if ps is None or root_pid <= 0:
        return 0.0
    try:
        root = ps.Process(root_pid)
        procs = [root] + root.children(recursive=True)
        return sum(p.memory_info().rss for p in procs) / (1024 ** 3)
    except Exception:  # noqa: BLE001
        return 0.0


def _gpu_stats() -> tuple[float, float, float]:
    """Return (gpu_util_pct, vram_used_gb, vram_total_gb)."""
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            return float(util.gpu), mem.used / (1024 ** 3), mem.total / (1024 ** 3)
        finally:
            pynvml.nvmlShutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = [p.strip() for p in r.stdout.strip().split(",")]
            if len(parts) >= 3:
                util = float(parts[0].replace(" %", "") or 0)
                used = float(parts[1]) / 1024.0  # MiB -> GiB approx
                total = float(parts[2]) / 1024.0
                return util, used, total
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return 0.0, 0.0, 0.0


def sample_host(*, root_pid: Optional[int] = None) -> dict[str, Any]:
    """One resource snapshot for a runner heartbeat."""
    mem_used, mem_total = _mem_gb()
    gpu_util, vram_used, vram_total = _gpu_stats()
    tree_gb = _runner_tree_rss_gb(root_pid) if root_pid else 0.0
    return {
        "cpu_pct": round(_cpu_pct(), 1),
        "mem_used_gb": round(mem_used, 2),
        "mem_total_gb": round(mem_total, 2),
        "process_tree_rss_gb": round(tree_gb, 2),
        "gpu_util_pct": round(gpu_util, 1),
        "vram_used_gb": round(vram_used, 2),
        "vram_total_gb": round(vram_total, 2),
    }
