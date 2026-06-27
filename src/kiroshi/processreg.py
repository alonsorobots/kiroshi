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


def list_registered() -> list[dict[str, Any]]:
    """All currently-advertised Kiroshi process manifests (for tray/CLI)."""
    out = []
    for f in registry_dir().glob("*.json"):
        try:
            out.append(jsonio.loads(f.read_bytes()))
        except Exception:  # noqa: BLE001
            continue
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
        base = {
            "schema": SCHEMA,
            "pid": self.pid,
            "role": role,
            "name": "kiroshi",
            "host": socket.gethostname(),
            "exe": sys.executable,
            "started_at": time.time(),
            "control": {"graceful_stop": "drop a '<role>-<pid>.stop' file beside this manifest"},
        }
        base.update(info or {})
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
