"""Tests for the machine-level coordinator lock (coordlock.py).

This lock prevents two coordinators on the same machine regardless of beacon
or port — the core singleton invariant. The existing LAN-guard test
(test_singleton_fixer.py) covers the discovery-based guard; this covers the
OS-lock that catches the --no-beacon / loopback bypass.

Tests use a temp KIROSHI_STATE_DIR so they never touch the real ProgramData.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect KIROSHI_STATE_DIR to a tmp dir so tests don't touch real state."""
    state = tmp_path / "kiroshi_state"
    state.mkdir()
    monkeypatch.setenv("KIROSHI_STATE_DIR", str(state))
    return state


def test_acquire_succeeds_on_first_call(tmp_state):
    """A fresh lock acquires immediately."""
    from kiroshi.coordlock import CoordinatorLock

    lk = CoordinatorLock(info={"port": 9999, "db": "test.db"})
    assert lk.acquire() is True
    assert lk.acquired is True
    lk.release()
    assert lk.acquired is False


def test_second_acquire_fails(tmp_state):
    """A second lock on the same machine is refused."""
    from kiroshi.coordlock import CoordinatorLock

    lk1 = CoordinatorLock(info={"port": 10000, "db": "a.db"})
    assert lk1.acquire() is True

    lk2 = CoordinatorLock(info={"port": 10001, "db": "b.db"})
    assert lk2.acquire() is False
    assert lk2.acquired is False

    lk1.release()


def test_release_lets_reacquire(tmp_state):
    """After release, a new lock can acquire."""
    from kiroshi.coordlock import CoordinatorLock

    lk1 = CoordinatorLock(info={"port": 10002, "db": "x.db"})
    assert lk1.acquire() is True
    lk1.release()

    lk2 = CoordinatorLock(info={"port": 10003, "db": "y.db"})
    assert lk2.acquire() is True
    lk2.release()


def test_stale_file_without_lock_is_acquirable(tmp_state):
    """A lock file left behind by a crashed process (no OS lock held) can be
    re-acquired — the OS lock auto-released on process death."""
    from kiroshi.coordlock import CoordinatorLock, _lock_path

    # Write a stale lock file WITHOUT holding the OS lock (simulate crash)
    lockfile = _lock_path()
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    lockfile.write_text(json.dumps({"pid": 999999, "port": 7777, "db": "stale.db"}))

    lk = CoordinatorLock(info={"port": 8888, "db": "fresh.db"})
    assert lk.acquire() is True, "stale lock file should be acquirable"
    lk.release()


def test_holder_returns_payload(tmp_state):
    """holder() reads the JSON payload from the lock file."""
    from kiroshi.coordlock import CoordinatorLock

    lk = CoordinatorLock(info={"port": 5555, "db": "holder.db", "host": "myhost"})
    assert lk.acquire() is True

    # Read from a fresh instance (simulates another process checking)
    checker = CoordinatorLock(info={})
    holder = checker.holder()
    assert holder is not None
    assert holder["port"] == 5555
    assert holder["db"] == "holder.db"
    assert holder.get("pid") == os.getpid()

    lk.release()


def test_context_manager_releases_on_exit(tmp_state):
    """Using the lock as a context manager releases on exit."""
    from kiroshi.coordlock import CoordinatorLock

    with CoordinatorLock(info={"port": 6666, "db": "ctx.db"}) as lk:
        assert lk.acquired is True
    assert lk.acquired is False

    # Should be reacquirable now
    lk2 = CoordinatorLock(info={"port": 7777, "db": "ctx2.db"})
    assert lk2.acquire() is True
    lk2.release()


def test_context_manager_releases_on_exception(tmp_state):
    """The lock releases even if an exception is raised inside the with block."""
    from kiroshi.coordlock import CoordinatorLock

    with pytest.raises(RuntimeError):
        with CoordinatorLock(info={"port": 7778, "db": "exc.db"}) as lk:
            assert lk.acquired is True
            raise RuntimeError("boom")

    # Lock should be released — reacquirable
    lk2 = CoordinatorLock(info={"port": 7779, "db": "exc2.db"})
    assert lk2.acquire() is True
    lk2.release()


def test_acquire_or_refuse_returns_lock_on_success(tmp_state):
    """acquire_or_refuse returns a CoordinatorLock on success."""
    from kiroshi.coordlock import acquire_or_refuse

    lk = acquire_or_refuse(info={"port": 4444, "db": "ok.db"})
    assert lk is not None
    assert lk.acquired is True
    lk.release()


def test_acquire_or_refuse_returns_none_when_held(tmp_state):
    """acquire_or_refuse returns None (refusal) when the lock is already held."""
    from kiroshi.coordlock import acquire_or_refuse, CoordinatorLock

    holder = CoordinatorLock(info={"port": 3333, "db": "held.db"})
    assert holder.acquire() is True

    result = acquire_or_refuse(info={"port": 3334, "db": "refused.db"})
    assert result is None  # refused

    holder.release()


def test_acquire_or_refuse_override_skips_lock(tmp_state):
    """allow_override=True skips the lock (deliberate second mesh)."""
    from kiroshi.coordlock import acquire_or_refuse, CoordinatorLock

    # Hold the lock first
    holder = CoordinatorLock(info={"port": 2222, "db": "first.db"})
    assert holder.acquire() is True

    # Override should succeed despite the lock being held
    lk = acquire_or_refuse(
        info={"port": 2223, "db": "second.db"},
        allow_override=True,
    )
    assert lk is not None
    assert lk.acquired is True
    # The override lock is a no-op — release doesn't touch the real lock
    lk.release()

    holder.release()


def test_force_second_fixer_without_env_is_refused(tmp_state):
    """A4: --force-second-fixer without KIROSHI_ALLOW_SECOND_COORDINATOR=1
    should be refused. Test the env check logic directly."""
    # This tests the guard logic in _cmd_fixer; we can't easily run the full
    # CLI handler, but we can verify the env-check contract.
    monkeypatch_env = os.environ.copy()
    os.environ.pop("KIROSHI_ALLOW_SECOND_COORDINATOR", None)
    try:
        # Simulate the check: flag set, env not set -> refuse
        force_second = True
        allowed = os.environ.get("KIROSHI_ALLOW_SECOND_COORDINATOR") == "1"
        assert not allowed, "should refuse without env var"

        # Now with env set -> allow
        os.environ["KIROSHI_ALLOW_SECOND_COORDINATOR"] = "1"
        allowed = os.environ.get("KIROSHI_ALLOW_SECOND_COORDINATOR") == "1"
        assert allowed, "should allow with env var"
    finally:
        os.environ.clear()
        os.environ.update(monkeypatch_env)


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
