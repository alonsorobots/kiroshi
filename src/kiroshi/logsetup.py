"""Terminal-output logging — tee every process's stdout/stderr to a log file.

Operators (and the tray) want the full console history of a Fixer/Runner without
having to have launched it from a terminal they kept open. So at startup each
process mirrors its stdout+stderr into a per-process log file under the state
dir's ``logs/`` folder, while still writing to the real console.

Files are named ``<role>-<host>-<pid>.log`` and size-rotated on startup (the
previous run is kept as ``*.log.1``) so they don't grow without bound. The path
is recorded in the process registry so the tray can open it.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
from pathlib import Path
from typing import Optional, TextIO

from .appstate import logs_dir

_MAX_BYTES = 8 * 1024 * 1024  # rotate logs larger than this at startup
_current_log_path: Optional[str] = None

# Secrets to scrub from the *on-disk* log stream. The live console still shows
# them (so the operator can copy the mesh token), but they must never be written
# to a file under the state dir — those logs are readable by other local users.
_SECRETS: set[str] = set()


def redact(secret: Optional[str]) -> None:
    """Register a secret to be scrubbed from anything written to log files."""
    if secret and len(secret) >= 6:
        _SECRETS.add(secret)


def _scrub(s: str) -> str:
    if not _SECRETS:
        return s
    for sec in _SECRETS:
        if sec in s:
            mask = sec[:3] + "\u2026REDACTED" if len(sec) > 3 else "REDACTED"
            s = s.replace(sec, mask)
    return s


class _Tee:
    """A write-through stream that forwards to the console *and* a log file."""

    def __init__(self, console: Optional[TextIO], logfile: TextIO):
        self._console = console
        self._logfile = logfile
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        with self._lock:
            if self._console is not None:
                try:
                    self._console.write(s)
                except (ValueError, OSError):
                    pass
            try:
                self._logfile.write(_scrub(s))
            except (ValueError, OSError):
                pass
        return len(s)

    def flush(self) -> None:
        with self._lock:
            for st in (self._console, self._logfile):
                try:
                    if st is not None:
                        st.flush()
                except (ValueError, OSError):
                    pass

    def isatty(self) -> bool:
        return bool(self._console and getattr(self._console, "isatty", lambda: False)())

    def fileno(self) -> int:  # some libs probe this
        if self._console is not None and hasattr(self._console, "fileno"):
            return self._console.fileno()
        return self._logfile.fileno()


def current_log_path() -> Optional[str]:
    return _current_log_path


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            backup = path.with_suffix(path.suffix + ".1")
            try:
                if backup.exists():
                    backup.unlink()
            except OSError:
                pass
            path.replace(backup)
    except OSError:
        pass


def tee_process_output(role: str, host: Optional[str] = None) -> Optional[str]:
    """Begin mirroring stdout+stderr to a per-process log file. Returns its path."""
    global _current_log_path
    host = host or socket.gethostname()
    safe_host = "".join(c if c.isalnum() or c in "-_" else "_" for c in host)
    path = logs_dir() / f"{role}-{safe_host}-{os.getpid()}.log"
    _rotate(path)
    try:
        logfile = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return None
    sys.stdout = _Tee(sys.stdout, logfile)  # type: ignore[assignment]
    sys.stderr = _Tee(sys.stderr, logfile)  # type: ignore[assignment]
    _current_log_path = str(path)
    return _current_log_path
