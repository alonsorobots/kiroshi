"""Per-sub-job stdout/stderr capture -- "as if each sub-job ran in its own
terminal" -- with a crash-survivable, bounded on-disk tail.

Why an OS-level fd redirect, not a Python-level ``sys.stdout`` swap: native
libraries (NVDEC, CUDA) write straight to the C-level stdout/stderr file
descriptor, bypassing ``sys.stdout`` entirely. Only ``os.dup2`` onto the real
fd 1/2 catches that -- and because a worker's ``_init_worker`` (pool.py) may
already have pointed ``sys.stdout`` at ``os.devnull`` (a DIFFERENT fd), this
module also rebinds the ``sys.stdout``/``sys.stderr`` *objects* onto the same
fds, so Python-level ``print()`` is captured through the identical target.

Crash-survivability is a byproduct of using a real file, not a mechanism:
every write goes straight through the OS fd to disk, unbuffered, so a
tree-killed worker (``taskkill /F /T``, zero Python cleanup ever runs) still
leaves whatever was written durable -- nothing here depends on
``__exit__``/``finally`` executing in the dying process.

Bounded like a professional job-log system (Cloud Logging, ``kubectl logs
--tail=N``, CI step logs): every sub-job keeps a tail by default; the cost
control is the bound (last ~500 lines + a byte ceiling), not selective
capture. This is additive to -- not a replacement for -- the existing
structured ``metrics.error``/``metrics.traceback`` fields, which remain the
"things of interest" a task deliberately reports; this module only adds the
raw terminal record underneath.

Best-effort throughout: any failure (unusual platform, fd exhaustion, a
sandboxed environment) degrades to "sub-job runs uncaptured", never fatal.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .appstate import subjob_logs_dir

_TAIL_LINES_DEFAULT = 500
_TAIL_BYTES_DEFAULT = 200_000  # defensive backstop against pathologically long lines
_SWEEP_MAX_AGE_S_DEFAULT = 1800.0


def _safe_name(subjob_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in subjob_id)[:200]


def log_path(subjob_id: str) -> Path:
    return subjob_logs_dir() / f"{_safe_name(subjob_id)}.log"


def _marker_path(subjob_id: str) -> Path:
    return subjob_logs_dir() / f"{_safe_name(subjob_id)}.json"


def read_tail(subjob_id: str, max_lines: int = _TAIL_LINES_DEFAULT,
              max_bytes: int = _TAIL_BYTES_DEFAULT) -> Optional[str]:
    """Best-effort: the last ``max_lines`` lines (bounded also by
    ``max_bytes``) of a sub-job's capture file, or ``None`` if it doesn't
    exist / can't be read."""
    p = log_path(subjob_id)
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


def discard(subjob_id: str) -> None:
    """Best-effort delete of both the capture file and its marker. Safe to
    call even if neither exists (idempotent)."""
    for p in (log_path(subjob_id), _marker_path(subjob_id)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def list_inflight(max_age_s: Optional[float] = None) -> list[dict[str, Any]]:
    """Currently-executing sub-jobs (marker file present), newest-first
    excluded -- just ``[{"subjob_id", "elapsed_s"}]``. Optionally drop markers
    older than ``max_age_s`` (treat as probably-orphaned rather than live)."""
    out: list[dict[str, Any]] = []
    now = time.time()
    try:
        entries = list(os.scandir(subjob_logs_dir()))
    except OSError:
        return out
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        try:
            with open(entry.path, encoding="utf-8") as f:
                marker = json.load(f)
        except (OSError, ValueError):
            continue
        started_at = marker.get("started_at")
        if not isinstance(started_at, (int, float)):
            continue
        elapsed = now - started_at
        if max_age_s is not None and elapsed > max_age_s:
            continue
        subjob_id = marker.get("subjob_id")
        if subjob_id:
            out.append({"subjob_id": subjob_id, "elapsed_s": round(elapsed, 1)})
    return out


def sweep_stale(max_age_s: float = _SWEEP_MAX_AGE_S_DEFAULT) -> int:
    """Delete capture/marker files older than ``max_age_s`` -- catches
    orphans that nothing explicitly claimed (a hard `os._exit` watchdog kill,
    a whole-machine crash, ...). Best-effort; never raises. Returns the
    number of files removed."""
    removed = 0
    now = time.time()
    try:
        entries = list(os.scandir(subjob_logs_dir()))
    except OSError:
        return removed
    for entry in entries:
        try:
            if now - entry.stat().st_mtime > max_age_s:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    return removed


class SubjobCapture:
    """Context manager: redirect this WORKER PROCESS's combined stdout+stderr
    (OS fd 1/2, plus the ``sys.stdout``/``sys.stderr`` objects) to this
    sub-job's file for the duration of one attempt. ``.active`` is False if
    setup failed -- callers do not need to branch, just always use the
    ``with`` block; a failed setup means the sub-job simply runs uncaptured.
    """

    def __init__(self, subjob_id: str):
        self.subjob_id = subjob_id
        self.path = log_path(subjob_id)
        self._marker = _marker_path(subjob_id)
        self.active = False
        self._file = None
        self._saved_out: Optional[int] = None
        self._saved_err: Optional[int] = None
        self._prev_stdout = None
        self._prev_stderr = None

    def __enter__(self) -> "SubjobCapture":
        if os.environ.get("KIROSHI_SUBJOB_CAPTURE", "1") == "0":
            return self
        try:
            self._marker.write_text(json.dumps(
                {"subjob_id": self.subjob_id, "started_at": time.time(),
                 "pid": os.getpid()}))
            self._file = open(self.path, "wb", buffering=0)  # truncates
            for s in (sys.stdout, sys.stderr):
                try:
                    s.flush()
                except Exception:  # noqa: BLE001
                    pass
            self._saved_out = os.dup(1)
            self._saved_err = os.dup(2)
            os.dup2(self._file.fileno(), 1)
            os.dup2(self._file.fileno(), 2)
            self._prev_stdout, self._prev_stderr = sys.stdout, sys.stderr
            # closefd=False: these wrap the already-redirected real fds 1/2;
            # GC/close of the wrapper must not close the fd out from under
            # the process -- __exit__ restores the saved dup'd fds instead.
            sys.stdout = os.fdopen(1, "w", buffering=1, closefd=False)
            sys.stderr = os.fdopen(2, "w", buffering=1, closefd=False)
            self.active = True
        except Exception:  # noqa: BLE001 - capture is best-effort, never fatal
            self._teardown_partial()
            self.active = False
        return self

    def __exit__(self, *exc: object) -> None:
        if not self.active:
            return
        for s in (sys.stdout, sys.stderr):
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass
        if self._prev_stdout is not None:
            sys.stdout = self._prev_stdout
        if self._prev_stderr is not None:
            sys.stderr = self._prev_stderr
        try:
            if self._saved_out is not None:
                os.dup2(self._saved_out, 1)
            if self._saved_err is not None:
                os.dup2(self._saved_err, 2)
        except Exception:  # noqa: BLE001
            pass
        for fd in (self._saved_out, self._saved_err):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if self._file is not None:
            try:
                self._file.close()
            except Exception:  # noqa: BLE001
                pass

    def _teardown_partial(self) -> None:
        for fd in (self._saved_out, self._saved_err):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if self._file is not None:
            try:
                self._file.close()
            except Exception:  # noqa: BLE001
                pass
