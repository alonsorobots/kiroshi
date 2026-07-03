"""Cross-platform process-tree reap — ensures worker processes die when the
parent runner dies, even on force-kill / hard crash (where signal handlers
never fire).

**Windows:** creates a Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``
and assigns the current process to it. When the process dies (for any reason),
the kernel automatically terminates every process in the Job Object — including
spawned ``ProcessPoolExecutor`` workers. No signal handler or atexit hook
needed; the OS does it at the kernel level.

**POSIX:** calls ``os.setsid()`` to become a process-group leader so that
``os.killpg(os.getpgid(0), SIGKILL)`` in an atexit/signal handler reaps the
whole tree.

Usage (in ``Runner.run`` before the pool is built)::

    from .proctree import bind_job_object
    bind_job_object()  # Windows: Job Object; POSIX: setsid

The function is idempotent — calling it twice is harmless. On platforms where
the mechanism is unavailable (e.g. ctypes can't find kernel32), it silently
no-ops (the runner still works, just without the orphan-reap guarantee).
"""
from __future__ import annotations

import os
import sys

_bound = False


def bind_job_object() -> bool:
    """Bind the current process to a process-tree-reap mechanism.

    Returns True if the binding succeeded, False if unavailable (silent no-op).
    Idempotent: subsequent calls return the cached result.
    """
    global _bound
    if _bound:
        return True

    if sys.platform == "win32":
        _bound = _bind_windows_job_object()
    else:
        _bound = _bind_posix_setsid()
    return _bound


# ------------------------------------------------------------------ Windows

def _bind_windows_job_object() -> bool:
    """Create a Job Object with KILL_ON_JOB_CLOSE and assign ourselves to it.

    Uses ctypes against kernel32 — no pywin32 dependency. When this process
    exits (graceful, force-killed, or crashed), the kernel kills all processes
    in the Job Object, which includes any ProcessPoolExecutor spawn workers
    that inherited the handle.
    """
    import ctypes
    from ctypes import wintypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # --- Job Object creation ---
        # CreateJobObjectW(lpJobAttributes, lpName) -> HANDLE
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]

        # SetInformationJobObject(hJob, JobObjectInfoClass, lpJobObjectInfo, cbJobObjectInfo)
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        ]

        # AssignProcessToJobObject(hJob, hProcess) -> BOOL
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

        # GetCurrentProcess() -> HANDLE (pseudo-handle, always -1)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []

        # --- EXTENDED_LIMIT_INFORMATION struct ---
        # typedef struct _JOBOBJECT_BASIC_LIMIT_INFORMATION {
        #   LARGE_INTEGER PerProcessUserTimeLimit;
        #   LARGE_INTEGER PerJobUserTimeLimit;
        #   DWORD LimitFlags;
        #   SIZE_T MinimumWorkingSetSize;
        #   SIZE_T MaximumWorkingSetSize;
        #   DWORD ActiveProcessLimit;
        #   ULONG_PTR Affinity;
        #   DWORD PriorityClass;
        #   DWORD SchedulingClass;
        # } JOBOBJECT_BASIC_LIMIT_INFORMATION;
        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        # typedef struct _IO_COUNTERS {
        #   ULONGLONG ReadOperationCount;
        #   ULONGLONG WriteOperationCount;
        #   ULONGLONG OtherOperationCount;
        #   ULONGLONG ReadTransferCount;
        #   ULONGLONG WriteTransferCount;
        #   ULONGLONG OtherTransferCount;
        # } IO_COUNTERS;
        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        # typedef struct _JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
        #   JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        #   IO_COUNTERS IoInfo;
        #   SIZE_T ProcessMemoryLimit;
        #   SIZE_T JobMemoryLimit;
        #   SIZE_T PeakProcessMemoryUsed;
        #   SIZE_T PeakJobMemoryUsed;
        # } JOBOBJECT_EXTENDED_LIMIT_INFORMATION;
        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        h_job = kernel32.CreateJobObjectW(None, None)
        if not h_job:
            return False

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        if not kernel32.SetInformationJobObject(
            h_job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            return False

        if not kernel32.AssignProcessToJobObject(h_job, kernel32.GetCurrentProcess()):
            # On Windows 7+, we can always assign even if the process is already
            # in another job (nested jobs). If it fails, the reap guarantee
            # won't work but the runner still functions.
            return False

        # Hold a reference to the Job Object handle so it doesn't get GC'd
        # (closing the last handle triggers KILL_ON_JOB_CLOSE — which is what
        # we want on process exit, but NOT during normal operation).
        # The handle is intentionally never closed: when the process dies, the
        # kernel closes all handles, which triggers the kill-on-close.
        _JOB_HANDLE[0] = h_job  # prevent GC
        return True

    except Exception:
        return False


# Module-level holder so the Job Object handle survives garbage collection.
# When the process exits, the kernel closes this handle and kills all workers.
_JOB_HANDLE: list = [None]


# ------------------------------------------------------------------ POSIX

def _bind_posix_setsid() -> bool:
    """Become a process-group leader so killpg reaps the whole tree.

    On POSIX, ``os.setsid()`` creates a new session and process group. When
    the runner exits, an atexit hook calls ``os.killpg(0, SIGKILL)`` to reap
    all processes in the group (the pool workers).

    CRITICAL: the atexit reap is registered **only if setsid succeeded**. If
    setsid fails (already a session leader, no permission), the process is
    still in the launching shell's process group — killing that group on exit
    would SIGKILL the shell and all its sibling processes.
    """
    import atexit
    import signal

    try:
        os.setsid()
    except (OSError, PermissionError):
        # Already a session leader or not allowed — do NOT register the reap
        # hook; we don't own the process group and killpg would hit the shell.
        return False

    def _reap_on_exit():
        try:
            os.killpg(os.getpgid(0), signal.SIGKILL)
        except Exception:
            pass

    atexit.register(_reap_on_exit)
    return True
