"""Tests for the split-brain guard: `kiroshi fixer` / `kiroshi run --lan` refuse
to start when another Fixer is already discoverable on the LAN.

We test the guard by monkeypatching :func:`check_singleton_fixer` so no real
UDP sockets are used. Two integration paths are covered:

- ``kiroshi fixer --host 0.0.0.0`` (via :func:`kiroshi.cli._cmd_fixer`)
- ``kiroshi run --lan`` (via :func:`kiroshi.runjob.run_job`) — the more common
  accidental-second-Fixer footgun (workstation `kiroshi run --lan` while the
  coordinator host's service is already up).

Note: imports inside `_cmd_fixer` and `run_job` are function-local
(`from . import security` etc.), so patches MUST target the source module
(`kiroshi.security`), not `kiroshi.cli.security`.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from typing import Optional
from unittest.mock import patch

import pytest


# ------------------------------------------------- discovery.check_singleton_fixer
def test_check_singleton_fixer_is_a_thin_wrapper_over_discover_fixer():
    """The guard must use the exact same solicited-reply mechanism runners use
    — otherwise it could fail-open on a Fixer that runners CAN see."""
    from kiroshi import discovery

    with patch.object(discovery, "discover_fixer",
                      return_value="http://192.168.50.166:8787") as m:
        got = discovery.check_singleton_fixer(timeout=1.5)
    assert got == "http://192.168.50.166:8787"
    m.assert_called_once()
    assert m.call_args.kwargs.get("timeout") == 1.5


def test_check_singleton_fixer_returns_none_when_none_found():
    from kiroshi import discovery

    with patch.object(discovery, "discover_fixer", return_value=None):
        assert discovery.check_singleton_fixer() is None


# ------------------------------------------------------------- cli._cmd_fixer
def _make_fixer_args(**over) -> argparse.Namespace:
    ns = argparse.Namespace(
        db="tmp.db", host="0.0.0.0", port=8787, max_retries=3, lease_ttl=120.0,
        reap_interval=15.0, no_beacon=False, force_second_fixer=False,
        token=None, pages_dir=None, no_auth=False,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _stub_fixer_deps(stack: ExitStack) -> None:
    """Stub every side-effecting dep of `_cmd_fixer` so tests exercise only the
    guard, not real socket binds / SQLite files / uvicorn threads.

    All patches target the source modules because `_cmd_fixer` uses local
    imports (`from . import security` etc.) that don't create attrs on
    `kiroshi.cli`.
    """
    stack.enter_context(patch("kiroshi.security.ensure_fixer_token",
                              return_value="tok"))
    stack.enter_context(patch("kiroshi.logsetup.tee_process_output"))
    stack.enter_context(patch("kiroshi.logsetup.redact"))
    stack.enter_context(patch("kiroshi.logsetup.current_log_path",
                              return_value="log.txt"))
    stack.enter_context(patch("kiroshi.jobstore.JobStore"))
    stack.enter_context(patch("kiroshi.coordinator.create_app"))
    stack.enter_context(patch("kiroshi.storage.load_topology",
                              return_value=None))
    stack.enter_context(patch("kiroshi.discovery.BeaconBroadcaster"))
    reg = stack.enter_context(patch("kiroshi.processreg.ProcessRegistration"))
    reg.return_value.start.return_value = reg.return_value
    reg.return_value.update = lambda **kw: None
    srv = stack.enter_context(patch("uvicorn.Server"))
    srv.return_value.run.side_effect = KeyboardInterrupt


def test_cmd_fixer_refuses_when_another_fixer_is_discoverable(capsys):
    from kiroshi import cli

    args = _make_fixer_args()
    with ExitStack() as stack:
        _stub_fixer_deps(stack)
        stack.enter_context(patch(
            "kiroshi.discovery.check_singleton_fixer",
            return_value="http://192.168.50.166:8787"))
        rc = cli._cmd_fixer(args)
    assert rc == 3
    err = capsys.readouterr().err
    assert "REFUSING" in err
    assert "http://192.168.50.166:8787" in err
    assert "--force-second-fixer" in err


def test_cmd_fixer_skips_guard_when_no_beacon_is_set():
    from kiroshi import cli

    args = _make_fixer_args(no_beacon=True)
    with ExitStack() as stack:
        _stub_fixer_deps(stack)
        check = stack.enter_context(patch(
            "kiroshi.discovery.check_singleton_fixer"))
        try:
            cli._cmd_fixer(args)
        except (KeyboardInterrupt, Exception):  # noqa: BLE001
            pass
    check.assert_not_called()


def test_cmd_fixer_skips_guard_when_bind_is_loopback():
    from kiroshi import cli

    args = _make_fixer_args(host="127.0.0.1")
    with ExitStack() as stack:
        _stub_fixer_deps(stack)
        check = stack.enter_context(patch(
            "kiroshi.discovery.check_singleton_fixer"))
        try:
            cli._cmd_fixer(args)
        except (KeyboardInterrupt, Exception):  # noqa: BLE001
            pass
    check.assert_not_called()


def test_cmd_fixer_force_second_fixer_bypasses_guard():
    from kiroshi import cli

    args = _make_fixer_args(force_second_fixer=True)
    with ExitStack() as stack:
        _stub_fixer_deps(stack)
        check = stack.enter_context(patch(
            "kiroshi.discovery.check_singleton_fixer"))
        try:
            cli._cmd_fixer(args)
        except (KeyboardInterrupt, Exception):  # noqa: BLE001
            pass
    check.assert_not_called()


# ----------------------------------------------------------- runjob.run_job
def test_run_job_lan_refuses_when_another_fixer_discoverable(capsys, tmp_path):
    """`kiroshi run --lan` — the classic accidental-second-Fixer path."""
    from kiroshi import runjob

    with patch("kiroshi.discovery.check_singleton_fixer",
               return_value="http://192.168.50.166:8787"):
        rc = runjob.run_job(
            task_ref="examples.sleep_task:run",
            items=None, jobs=None,
            lan=True,
            db=str(tmp_path / "run.db"),
            port=8890,
        )
    assert rc == 3
    err = capsys.readouterr().err
    assert "REFUSING --lan" in err
    assert "192.168.50.166:8787" in err
    assert "seed --fixer" in err  # actionable hint


def test_run_job_without_lan_does_not_check_singleton(tmp_path,
                                                       monkeypatch: pytest.MonkeyPatch):
    """Non-LAN `kiroshi run` binds loopback + skips beacon → no split-brain
    risk → guard must not fire even if another Fixer is out there."""
    from kiroshi import runjob

    called: dict[str, bool] = {"check": False}

    def _spy(*a, **kw) -> Optional[str]:
        called["check"] = True
        return "http://any-fixer:8787"

    monkeypatch.setattr("kiroshi.discovery.check_singleton_fixer", _spy)
    with patch("kiroshi.tasks.resolve_task", side_effect=ImportError("stub")):
        rc = runjob.run_job(
            task_ref="no.such:task",
            items=None, jobs=None,
            lan=False,
            db=str(tmp_path / "run.db"),
            port=8891,
        )
    assert rc == 2  # failed on task import, not on split-brain guard
    assert called["check"] is False


def test_run_job_lan_force_second_fixer_bypasses_guard(tmp_path):
    """--force-second-fixer via the run_job parameter must skip the check."""
    from kiroshi import runjob

    called: dict[str, bool] = {"check": False}

    def _spy(*a, **kw) -> Optional[str]:
        called["check"] = True
        return "http://any:8787"

    with patch("kiroshi.discovery.check_singleton_fixer", side_effect=_spy):
        with patch("kiroshi.tasks.resolve_task",
                   side_effect=ImportError("stub")):
            rc = runjob.run_job(
                task_ref="no.such:task",
                items=None, jobs=None,
                lan=True,
                db=str(tmp_path / "run.db"),
                port=8892,
                force_second_fixer=True,
            )
    assert rc == 2  # failed on task import, not on split-brain guard
    assert called["check"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
