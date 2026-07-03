"""Tests for headless stdout safety — the runner and pool workers must never
block on an inherited pipe with no reader.

Regression: launched via scheduled task / WMI (no console), the runner's
``print()`` fills the ~4KB OS pipe buffer and the next write blocks at the C
level. The runner hangs silently (heartbeat alive, zero gigs complete).
``tee_process_output`` should detect the non-TTY and drop the console side;
``_init_worker`` should redirect worker stdout to devnull.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.logsetup import _Tee, tee_process_output  # noqa: E402


def test_tee_drops_console_when_not_tty():
    """When stdout is not a TTY, the _Tee should have None for the console side
    so writes only go to the log file (never a blocking pipe)."""
    # Simulate a headless launch: replace stdout with a non-TTY pipe-like object
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        # io.StringIO is not a TTY
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        path = tee_process_output("test_runner", host="testhost")
        assert path is not None
        # The Tee wrapping stdout should have console=None (headless)
        tee = sys.stdout
        assert isinstance(tee, _Tee)
        assert tee._console is None, "console should be None when not a TTY"
        # Writing should succeed (goes to logfile only, no console blocking)
        print("hello from headless runner")
        tee.flush()
        # Verify it landed in the log file
        content = Path(path).read_text(encoding="utf-8")
        assert "hello from headless runner" in content
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err


def test_tee_keeps_console_when_tty():
    """When stdout IS a TTY, the Tee should keep the console side (normal launch)."""
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        # Create a fake TTY-like stream
        class FakeTTY(io.StringIO):
            def isatty(self): return True
        sys.stdout = FakeTTY()
        sys.stderr = FakeTTY()
        tee_process_output("test_runner_tty", host="testhost")
        tee = sys.stdout
        assert isinstance(tee, _Tee)
        assert tee._console is not None, "console should be kept when TTY"
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err


def test_tee_write_never_raises_on_dead_console():
    """Even with a console side, writes that fail should be swallowed, not raised."""
    class DeadConsole:
        def write(self, s):
            raise OSError("broken pipe")
        def flush(self):
            raise OSError("broken pipe")
        def isatty(self):
            return False
    f = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".log")
    tee = _Tee(DeadConsole(), f)
    # Should not raise
    tee.write("test line\n")
    tee.flush()
    f.close()
    os.unlink(f.name)


def test_tee_safe_when_stdout_is_none():
    """Regression: under pythonw / scheduled tasks, spawned processes get
    sys.stdout=None. tee_process_output must not crash with AttributeError
    when calling .isatty() on None."""
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        sys.stdout = None
        sys.stderr = None
        # Must not raise AttributeError
        path = tee_process_output("test_none_stdout", host="testhost")
        assert path is not None
        # Writing should succeed (goes to logfile only, console is None)
        print("survived None stdout")
        Path(path).read_text(encoding="utf-8")  # file is readable
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err


def test_init_worker_safe_when_stdout_is_none():
    """Regression: pool._init_worker must not crash when sys.stdout=None
    (the pythonw / scheduled-task spawn scenario — Python bpo-706263)."""
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        sys.stdout = None
        sys.stderr = None
        # _init_worker is called by ProcessPoolExecutor in each spawned worker.
        # We call it directly to test the guard without spawning a real pool.
        # We need a dummy task_ref that won't actually import — but the stdout
        # guard runs BEFORE the task import, so if it crashes we never get there.
        # Use a try/except to isolate the stdout guard from the task import.
        try:
            from kiroshi.pool import _init_worker
            _init_worker("nonexistent.task:run", [])
        except AttributeError:
            pytest.fail("_init_worker crashed with AttributeError on sys.stdout=None")
        except Exception:
            pass  # other errors (task not found) are expected and fine
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err


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
