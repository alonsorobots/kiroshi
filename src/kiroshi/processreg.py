"""Process registry — a manifest of running Kiroshi processes for watchdogs.

at-field (the sibling GPU/thermal watchdog) performs emergency shutdown by
killing ``python.exe`` process *trees* it discovers via psutil — it has no
opt-in registration API. So Kiroshi instead **advertises** every Fixer/Runner it
starts as a small JSON manifest in a well-known place. Any watchdog (at-field,
the tray, an ops script) can then enumerate exactly which PIDs belong to Kiroshi,
the full launch command behind each, and how to stop it *gracefully* before
resorting to a hard kill.

Manifests are written to:
    <state_dir>/registry/<role>-<pid>.json          (Kiroshi's own registry)
    %PROGRAMDATA%/ATField/clients/kiroshi/<role>-<pid>.json   (if at-field present)

Graceful shutdown contract: a watchdog (or the tray) drops a sibling
``<role>-<pid>.stop`` file; the process notices within ~1s and drains cleanly
(finishing/​reporting in-flight work) instead of being killed mid-batch. If it
doesn't drain in time, the watchdog still has the PID for a hard kill.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import jsonio
from .appstate import registry_dir

SCHEMA = "kiroshi.process/1"

# Stale entries survive process crashes and pile up in the registry over time
# (crash, `kill -9`, host reboot, power loss — anywhere `close()` never runs).
# Any manifest whose host matches this box AND whose PID is not alive on this
# box is treated as garbage. We only GC the file after it's also been stale for
# at least this long, to avoid racing a brand-new manifest that hasn't had its
# first refresh tick yet.
_STALE_GC_AGE_S = 60.0


def _pid_alive(pid: int) -> bool:
    """Best-effort PID liveness check. Windows uses OpenProcess; POSIX uses
    ``os.kill(pid, 0)``. Returns True if the PID is running, False if it's
    demonstrably gone, and True on ambiguity (permission denied etc.) — we bias
    toward "alive" so we never GC a manifest we shouldn't."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                # ERROR_INVALID_PARAMETER (0x57) means "no such process".
                # Any other error (e.g. access denied) → assume alive.
                return ctypes.get_last_error() != 87 and k32.GetLastError() != 87
            try:
                code = wintypes.DWORD(0)
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return True
                return code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _atfield_clients_dir() -> Optional[Path]:
    """at-field's state dir, if it exists, so we can advertise into its namespace."""
    base = os.environ.get("ATFIELD_STATE_DIR")
    if not base:
        if sys.platform != "win32":
            return None
        pd = os.environ.get("PROGRAMDATA")
        if not pd:
            return None
        base = str(Path(pd) / "ATField")
    p = Path(base)
    if not p.is_dir():
        return None
    d = p / "clients" / "kiroshi"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return d


def list_registered(*, include_stale: bool = False,
                    gc: bool = True) -> list[dict[str, Any]]:
    """All currently-advertised Kiroshi process manifests (for tray/CLI).

    A process is only healthy while its owning PID is alive. When a Fixer or
    Runner crashes (kill -9, power loss, OOM), its manifest sticks around
    forever — turning ``kiroshi ps`` into a graveyard. This function filters
    to *live* PIDs (on this host) by default and, when ``gc=True``, deletes
    manifest files whose host matches this box, whose PID is definitively
    dead, and whose ``updated_at`` is older than ``_STALE_GC_AGE_S`` (so a
    fresh manifest that hasn't refreshed yet is never touched).

    ``include_stale=True`` returns everything including dead entries (useful
    for ``kiroshi ps --all`` when debugging why something crashed).
    """
    out: list[dict[str, Any]] = []
    my_host = socket.gethostname()
    now = time.time()
    for f in registry_dir().glob("*.json"):
        try:
            info = jsonio.loads(f.read_bytes())
        except Exception:  # noqa: BLE001
            continue
        # Prefer the immutable `hostname` field (added Jul 2026); fall back to
        # legacy `host` for manifests written before the split — those old
        # entries with host="0.0.0.0"/"127.0.0.1" are ALWAYS from this machine
        # (bind addresses only make sense locally) so treat them as local too.
        pid = int(info.get("pid", 0) or 0)
        hostname = str(info.get("hostname") or "")
        legacy_host = str(info.get("host", ""))
        if hostname:
            is_local = hostname == my_host
        else:
            is_local = (legacy_host == my_host
                        or legacy_host in ("0.0.0.0", "127.0.0.1", "::1", ""))
        alive = _pid_alive(pid) if is_local else True  # can't check remote PIDs
        info["_alive"] = alive
        if not alive and is_local:
            age = now - float(info.get("updated_at", info.get("started_at", now)))
            if gc and age > _STALE_GC_AGE_S:
                try:
                    f.unlink()
                except OSError:
                    pass
                continue
            if not include_stale:
                continue
        out.append(info)
    out.sort(key=lambda d: (d.get("role", ""), d.get("pid", 0)))
    return out


class ProcessRegistration:
    """Writes + maintains this process's manifest; watches for a stop request."""

    def __init__(self, role: str, info: dict[str, Any],
                 on_stop: Optional[Callable[[], None]] = None,
                 refresh: float = 5.0):
        self.role = role
        self.pid = os.getpid()
        self.on_stop = on_stop
        self.refresh = refresh
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fired = False
        my_host = socket.gethostname()
        base = {
            "schema": SCHEMA,
            "pid": self.pid,
            "role": role,
            "name": "kiroshi",
            "host": my_host,
            "exe": sys.executable,
            "started_at": time.time(),
            "control": {"graceful_stop": "drop a '<role>-<pid>.stop' file beside this manifest"},
        }
        base.update(info or {})
        # `host` is a display field callers routinely override with the bind
        # address (e.g. 0.0.0.0). Keep a stable, never-overridden `hostname`
        # so locality checks (see list_registered) don't get confused between
        # "this machine" and "bind address on this machine".
        base["hostname"] = my_host
        self.info = base

    @property
    def _stem(self) -> str:
        return f"{self.role}-{self.pid}"

    def _targets(self) -> list[Path]:
        paths = [registry_dir() / f"{self._stem}.json"]
        af = _atfield_clients_dir()
        if af is not None:
            paths.append(af / f"{self._stem}.json")
        return paths

    def _stop_files(self) -> list[Path]:
        return [p.with_name(f"{self._stem}.stop") for p in self._targets()]

    def _write(self) -> None:
        self.info["updated_at"] = time.time()
        data = jsonio.dumps_bytes(self.info)
        for p in self._targets():
            try:
                tmp = p.with_suffix(".json.tmp")
                tmp.write_bytes(data)
                os.replace(tmp, p)
            except OSError:
                pass

    def update(self, **fields: Any) -> None:
        self.info.update(fields)

    def start(self) -> "ProcessRegistration":
        self._write()
        self._thread = threading.Thread(target=self._loop, name="kiroshi-procreg",
                                         daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(min(1.0, self.refresh)):
            # check for a stop request
            for sf in self._stop_files():
                if sf.exists():
                    if not self._fired and self.on_stop is not None:
                        self._fired = True
                        print(f"[{self.role}] stop requested via {sf.name}; draining...",
                              flush=True)
                        try:
                            self.on_stop()
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        sf.unlink()
                    except OSError:
                        pass
            self._write()

    def close(self) -> None:
        self._stop.set()
        for p in self._targets():
            try:
                p.unlink()
            except OSError:
                pass


def request_stop(role: str, pid: int) -> bool:
    """Ask a registered process to drain. Returns True if the manifest existed."""
    stem = f"{role}-{pid}"
    manifest = registry_dir() / f"{stem}.json"
    if not manifest.exists():
        return False
    try:
        (registry_dir() / f"{stem}.stop").write_text("stop\n", encoding="utf-8")
        return True
    except OSError:
        return False
