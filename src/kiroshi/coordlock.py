"""Machine-level exclusive lock for the coordinator (Coordinator).

ONE coordinator per machine is the core invariant of Kiroshi's architecture:
a single brain enforces the mesh-global per-spindle NAS budget. Two coordinators
on the same box (even on different ports) means two disjoint budgets that each
happily saturate the shared NAS — the exact footgun that caused production
incidents.

The existing LAN split-brain guard (``discovery.check_singleton_coordinator``) only
fires when beaconing AND binding public — ``--no-beacon`` or loopback bypasses
it entirely. This module adds a **beacon-independent, machine-level OS lock**
that catches the same-box case regardless of network configuration.

Mechanism: an exclusive OS advisory lock on a well-known file in the Kiroshi
state dir. The lock auto-releases when the holding process dies (crash, kill,
normal exit), so no staleness-check is needed for the core mutual exclusion.
A small JSON payload in the file records who holds it (pid/port/db) so a
rejected starter can print a helpful message.

Escape hatch: ``KIROSHI_ALLOW_SECOND_COORDINATOR=1`` environment variable
(used alongside ``--force-second-fixer`` — see A4 in the work order).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .appstate import state_dir

_LOCK_NAME = "coordinator.lock"
_INFO_NAME = "coordinator.lock.info"  # readable sidecar (not OS-locked)


def _lock_path() -> Path:
    return state_dir() / _LOCK_NAME


class CoordinatorLock:
    """Cross-process exclusive lock keyed to the machine (NOT the port).

    Usage::

        with CoordinatorLock(info={"port": 8787, ...}) as lk:
            if not lk.acquired:
                print(f"refused: held by {lk.holder()}")
                return 3
            # ... run the coordinator ...

    The lock file lives at ``state_dir()/coordinator.lock``. On Windows we use
    ``msvcrt.locking(LK_NBLCK)``; on POSIX ``fcntl.flock(LOCK_EX|LOCK_NB)``.
    Both auto-release on process death.
    """

    def __init__(self, info: dict[str, Any]):
        self.info = info
        self._fd: Optional[int] = None
        self._path: Path = _lock_path()
        self.acquired: bool = False

    # --------------------------------------------------------------- acquire

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True if we got it, False if held."""
        # Ensure parent dir exists
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Open for read+write; create if absent. We keep the fd open for the
        # lifetime of the coordinator so the OS lock persists.
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o644)
        self._fd = fd

        try:
            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    os.close(fd)
                    self._fd = None
                    return False
            else:
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    self._fd = None
                    return False
        except (ImportError, AttributeError):
            # No OS locking primitive available — fall back to file-existence
            # check (best effort, not race-free). Read the existing payload to
            # see if the PID is alive; if not, overwrite.
            existing = self._read_raw()
            if existing and _pid_alive(existing.get("pid")):
                os.close(fd)
                self._fd = None
                return False

        # We hold the OS lock — write our payload.
        self._write_payload()
        self.acquired = True
        return True

    # --------------------------------------------------------------- release

    def release(self) -> None:
        """Release the lock and close the file handle."""
        if self._fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(self._fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
            except (ImportError, AttributeError):
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        # Clean up the readable sidecar
        try:
            info_path = self._path.with_name(_INFO_NAME)
            info_path.unlink(missing_ok=True)
        except OSError:
            pass
        self.acquired = False

    # --------------------------------------------------------------- holder

    def holder(self) -> Optional[dict[str, Any]]:
        """Read the current holder's payload (best-effort). Returns None if no
        lock file exists or it's unreadable."""
        return self._read_raw()

    # --------------------------------------------------------------- helpers

    def _write_payload(self) -> None:
        payload = dict(self.info)
        payload.setdefault("pid", os.getpid())
        payload.setdefault("started_at", time.time())
        data = json.dumps(payload, indent=2).encode("utf-8")
        # Write to the lock fd (truncated + overwritten)
        try:
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, data)
        except OSError:
            pass
        # ALSO write a readable sidecar file (not OS-locked) so another
        # process can read who holds the lock even when msvcrt/flock blocks
        # reads on the lock file itself.
        try:
            info_path = self._path.with_name(_INFO_NAME)
            with open(info_path, "w", encoding="utf-8") as f:
                f.write(data.decode("utf-8"))
        except OSError:
            pass

    def _read_raw(self) -> Optional[dict[str, Any]]:
        """Read the holder's payload from the readable sidecar file."""
        try:
            info_path = self._path.with_name(_INFO_NAME)
            with open(info_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            # Fallback: try reading the lock file directly (POSIX allows this)
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError, ValueError):
                return None

    # --------------------------------------------------------------- context

    def __enter__(self) -> "CoordinatorLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _pid_alive(pid: Any) -> bool:
    """Check if a PID is alive, cross-platform. Returns False if pid is None."""
    if pid is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            # os.kill with signal 0 doesn't work on Windows; use OpenProcess
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False


def acquire_or_refuse(info: dict[str, Any],
                      *,
                      allow_override: bool = False) -> Optional[CoordinatorLock]:
    """Acquire the coordinator lock or return None with a printed refusal.

    Args:
        info: metadata to write into the lock file (pid, port, db, host).
        allow_override: if True (caller verified KIROSHI_ALLOW_SECOND_COORDINATOR),
            skip the lock entirely and return a no-op lock.

    Returns:
        A CoordinatorLock (acquired=True) on success, or None on refusal
        (caller should print the holder and exit 3).
    """
    if allow_override:
        lk = CoordinatorLock(info=info)
        lk.acquired = True  # no-op; the caller deliberately wants a second one
        return lk

    lk = CoordinatorLock(info=info)
    if lk.acquire():
        return lk

    # Refused — print who holds it
    holder = lk.holder()
    if holder:
        print(
            f"[coordinator] REFUSING to start: another coordinator is already "
            f"running on this machine.\n"
            f"  Holder: pid={holder.get('pid')}, port={holder.get('port')}, "
            f"db={holder.get('db', '?')}\n"
            f"  Two coordinators on one machine means two disjoint NAS budgets "
            f"— both would saturate the shared disks.\n"
            f"  Fix: stop the other coordinator, or seed jobs into it via "
            f"'kiroshi seed --fixer <url>'.\n"
            f"  (To override: set KIROSHI_ALLOW_SECOND_COORDINATOR=1 AND pass "
            f"--force-second-fixer.)",
            file=sys.stderr,
        )
    else:
        print(
            "[coordinator] REFUSING to start: another process holds the "
            "coordinator lock on this machine.\n"
            f"  Lock file: {_lock_path()}\n"
            f"  If the previous coordinator crashed, the OS lock auto-releases. "
            f"If not, stop it first.",
            file=sys.stderr,
        )
    return None
