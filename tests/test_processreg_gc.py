"""Tests for the stale-registry cleanup in processreg.list_registered().

Focus: dead-PID filtering + GC of stale manifest files. Uses monkeypatched
liveness check + registry dir so no real subprocesses are needed.
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from kiroshi import processreg as pr


@pytest.fixture
def reg_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the module at a fresh temp registry directory for each test."""
    monkeypatch.setattr(pr, "registry_dir", lambda: tmp_path)
    return tmp_path


def _write(reg: Path, *, role: str, pid: int, host: str,
           updated_at: float | None = None) -> Path:
    path = reg / f"{role}-{pid}.json"
    body = {
        "schema": pr.SCHEMA, "role": role, "pid": pid, "host": host,
        "name": "kiroshi", "started_at": time.time() - 1,
        "updated_at": updated_at if updated_at is not None else time.time(),
    }
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_live_local_process_is_included_and_marked_alive(reg_dir: Path,
                                                          monkeypatch: pytest.MonkeyPatch):
    my_pid = os.getpid()
    _write(reg_dir, role="coordinator", pid=my_pid, host=socket.gethostname())
    monkeypatch.setattr(pr, "_pid_alive", lambda pid: pid == my_pid)
    got = pr.list_registered()
    assert len(got) == 1
    assert got[0]["_alive"] is True
    assert got[0]["pid"] == my_pid


def test_dead_local_pid_is_filtered_out_by_default(reg_dir: Path,
                                                    monkeypatch: pytest.MonkeyPatch):
    _write(reg_dir, role="coordinator", pid=99999,
           host=socket.gethostname(),
           updated_at=time.time())  # fresh - no GC yet
    monkeypatch.setattr(pr, "_pid_alive", lambda pid: False)
    assert pr.list_registered() == []


def test_include_stale_returns_dead_entries(reg_dir: Path,
                                             monkeypatch: pytest.MonkeyPatch):
    _write(reg_dir, role="coordinator", pid=99999, host=socket.gethostname())
    monkeypatch.setattr(pr, "_pid_alive", lambda pid: False)
    got = pr.list_registered(include_stale=True, gc=False)
    assert len(got) == 1
    assert got[0]["_alive"] is False


def test_stale_local_manifest_older_than_gc_threshold_is_deleted(
        reg_dir: Path, monkeypatch: pytest.MonkeyPatch):
    old = time.time() - (pr._STALE_GC_AGE_S + 30)
    path = _write(reg_dir, role="runner", pid=99999,
                  host=socket.gethostname(), updated_at=old)
    monkeypatch.setattr(pr, "_pid_alive", lambda pid: False)
    pr.list_registered()
    assert not path.exists(), "old dead manifest should have been GC'd"


def test_fresh_stale_manifest_is_not_deleted_immediately(
        reg_dir: Path, monkeypatch: pytest.MonkeyPatch):
    path = _write(reg_dir, role="runner", pid=99999,
                  host=socket.gethostname(), updated_at=time.time())
    monkeypatch.setattr(pr, "_pid_alive", lambda pid: False)
    pr.list_registered()
    assert path.exists(), "fresh manifest should be kept in case owner is booting"


def test_remote_host_pid_is_never_gc_and_appears(reg_dir: Path,
                                                  monkeypatch: pytest.MonkeyPatch):
    old = time.time() - (pr._STALE_GC_AGE_S + 999)
    path = _write(reg_dir, role="runner", pid=1, host="not-this-host",
                  updated_at=old)
    monkeypatch.setattr(pr, "_pid_alive", lambda pid: False)
    got = pr.list_registered()
    assert path.exists(), "we should never GC manifests owned by other hosts"
    assert len(got) == 1
    assert got[0]["host"] == "not-this-host"


def test_disabling_gc_keeps_stale_files_but_still_filters_output(
        reg_dir: Path, monkeypatch: pytest.MonkeyPatch):
    old = time.time() - (pr._STALE_GC_AGE_S + 999)
    path = _write(reg_dir, role="runner", pid=99999,
                  host=socket.gethostname(), updated_at=old)
    monkeypatch.setattr(pr, "_pid_alive", lambda pid: False)
    got = pr.list_registered(gc=False)
    assert got == [], "dead PID still filtered"
    assert path.exists(), "gc=False should not delete on-disk manifests"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
