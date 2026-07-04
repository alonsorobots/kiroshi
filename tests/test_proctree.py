"""Tests for proctree — process-tree reap on parent death.

The critical regression: when the runner is force-killed (Stop-Process /
taskkill /F, hard crash), signal handlers never fire, ProcessPoolExecutor
spawn workers orphan, and they hold the wrapper's pipe handle — breaking
auto-restart. The Job Object / setsid binding ensures the OS reaps them.

On Windows we test by: spawning a child process that creates a Job Object,
spawns a sub-child, then force-terminating the child — and asserting the
sub-child is also gone.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _run_in_subprocess(code: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run Python code in a subprocess with kiroshi's src on the path."""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )


def test_bind_job_object_returns_bool():
    """bind_job_object should return a bool. Run in a subprocess so the test
    runner isn't assigned to a real KILL_ON_JOB_CLOSE job (which would pollute
    the process and cache global _bound state for other tests)."""
    result = _run_in_subprocess(
        "from kiroshi.proctree import bind_job_object; print(bind_job_object())"
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    val = result.stdout.strip()
    assert val in ("True", "False"), f"expected bool, got {val!r}"


def test_bind_job_object_idempotent():
    """Calling bind_job_object twice in the same process should be safe (cached)."""
    result = _run_in_subprocess(
        "from kiroshi.proctree import bind_job_object; "
        "r1 = bind_job_object(); r2 = bind_job_object(); "
        "print(r1 == r2)"
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    assert result.stdout.strip() == "True"


@pytest.mark.slow
@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object test")
def test_windows_job_object_reaps_children_on_force_kill():
    """Integration test: force-kill a process that bound a Job Object + spawned
    a child. The child should be reaped by the kernel (not orphaned).

    This is the exact scenario from the session: Stop-Process on the runner
    parent left 48 orphaned spawn_main workers. With the Job Object, they die.
    """
    child_script = '''
import sys, os, time
sys.path.insert(0, r"{src}")
from kiroshi.proctree import bind_job_object
bind_job_object()
# Spawn a long-lived child (simulates a pool worker)
import subprocess
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
# Write child PID so the test can verify it died
with open(r"{pidfile}", "w") as f:
    f.write(str(child.pid))
# Wait to be killed
child.wait()
'''.format(src=str(ROOT / "src"),
           pidfile=str(Path(__file__).parent / "_jobobj_child_pid.txt"))

    pidfile = Path(__file__).parent / "_jobobj_child_pid.txt"
    pidfile.unlink(missing_ok=True)

    proc = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Wait for the child PID to be written
    deadline = time.time() + 10
    while time.time() < deadline:
        if pidfile.exists():
            break
        time.sleep(0.2)
    assert pidfile.exists(), "child script didn't write PID file in time"
    child_pid = int(pidfile.read_text().strip())

    # Force-kill the parent (simulates Stop-Process / taskkill /F)
    proc.kill()
    proc.wait(timeout=5)

    # Give the kernel a moment to reap the Job Object
    time.sleep(2)

    # The child should be dead — not orphaned
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32")
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, child_pid)
    if handle:
        kernel32.CloseHandle(handle)
        pidfile.unlink(missing_ok=True)
        pytest.fail(f"child PID {child_pid} survived parent force-kill — "
                    f"Job Object did NOT reap it (orphaned!)")
    pidfile.unlink(missing_ok=True)


if __name__ == "__main__":
    tests = [n for n in dir(sys.modules[__name__]) if n.startswith("test_")]
    fail = 0
    for name in tests:
        try:
            globals()[name]()
            print(f"PASS  {name}")
        except Exception as exc:
            print(f"FAIL  {name}: {exc}")
            fail += 1
    print(f"\n{len(tests)-fail}/{len(tests)} passed")
    sys.exit(fail)


# ------------------------------------------------------------------ POSIX
# These tests exercise the POSIX path logic by mocking os.setsid. On Windows
# os.setsid doesn't exist, so we mock at module level instead of patching os.

def test_posix_setsid_failure_does_not_register_reap():
    """Regression (Opus finding B): if os.setsid() fails, the atexit killpg
    hook must NOT be registered — otherwise a normal runner exit would
    SIGKILL the launching shell's entire process job."""
    import atexit
    from unittest.mock import patch
    import kiroshi.proctree as pt

    pt._bound = False
    registered = []

    # Patch os.setsid to raise (simulate EPERM / already-leader).
    # On Windows os.setsid doesn't exist, so patch the module function directly.
    with patch.object(pt.os, "setsid", side_effect=OSError("EPERM"), create=True):
        with patch("atexit.register", side_effect=lambda f: registered.append(f)):
            result = pt._bind_posix_setsid()

    assert result is False, "setsid failure should return False"
    assert len(registered) == 0, (
        "atexit reap hook was registered even though setsid failed — "
        "this would SIGKILL the launching shell on runner exit!")


def test_posix_setsid_success_registers_reap():
    """When setsid succeeds, the atexit reap hook SHOULD be registered."""
    from unittest.mock import patch
    import kiroshi.proctree as pt

    pt._bound = False
    registered = []

    with patch.object(pt.os, "setsid", create=True):
        with patch("atexit.register", side_effect=lambda f: registered.append(f)):
            result = pt._bind_posix_setsid()

    assert result is True
    assert len(registered) == 1
    assert registered[0].__name__ == "_reap_on_exit"
