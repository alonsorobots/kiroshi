"""Tests for `kiroshi join` machinery: task-code serving + consent gate.

Covers taskdist (read/hash/pin/write, single-file-only rule), the Coordinator's
/task/meta + /task/source endpoints (token-gated, opt-in), and the consent
decision (pin auto-accept, --accept-task-hash, interactive y/N). The full
discover→verify→run orchestration is exercised by the end-to-end smoke join.
"""
from __future__ import annotations

import builtins
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi import taskdist  # noqa: E402
from kiroshi.jobstore import JobStore  # noqa: E402


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROSHI_STATE_DIR", str(tmp_path / "state"))
    yield tmp_path


# ------------------------------------------------------------- taskdist
def test_read_task_source_single_file(tmp_path, monkeypatch):
    (tmp_path / "echo_task.py").write_text(
        "def run(spec):\n    return {'status': 'ok'}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    import importlib
    importlib.invalidate_caches()

    src = taskdist.read_task_source("echo_task:run")
    assert src["module"] == "echo_task"
    assert src["filename"] == "echo_task.py"
    assert "def run" in src["source"]
    assert src["sha256"] == taskdist.source_sha256(src["source"])


def test_read_task_source_refuses_dotted_module():
    # served code is single-file/top-level only; dotted modules are refused
    # (find_spec is never reached — the dotted check raises first, so no numpy import)
    with pytest.raises(ValueError):
        taskdist.read_task_source("examples.motion_resample:run")


def test_pin_roundtrip_and_write_source(isolated_state):
    assert taskdist.read_pin("m") is None
    taskdist.write_pin("m", "abc123")
    assert taskdist.read_pin("m") == "abc123"
    p = taskdist.write_task_source("foo", "X = 1\n")
    assert p.exists() and p.read_text(encoding="utf-8") == "X = 1\n"
    assert p.name == "foo.py"


# ------------------------------------------------------ coordinator endpoints
def _client(task_source=None, token="T0KEN"):
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app

    app = create_app(JobStore(":memory:"), token=token, task_source=task_source)
    return TestClient(app)


_TS = {
    "task_ref": "echo:run", "module": "echo", "filename": "echo.py",
    "source": "def run(spec):\n    return {'status': 'ok'}\n", "sha256": "deadbeef",
}
_H = {"Authorization": "Bearer T0KEN"}


def test_task_meta_not_served_by_default():
    with _client() as c:
        d = c.get("/task/meta", headers=_H).json()
        assert d["served"] is False and d["task_ref"] is None
        assert c.get("/task/source", headers=_H).status_code == 404


def test_task_source_served_and_gated():
    with _client(task_source=_TS) as c:
        meta = c.get("/task/meta", headers=_H).json()
        assert meta["served"] is True and meta["task_ref"] == "echo:run"
        assert meta["sha256"] == "deadbeef"
        src = c.get("/task/source", headers=_H).json()
        assert src["source"].startswith("def run") and src["module"] == "echo"
        # token-gated: no creds -> 401
        assert c.get("/task/source").status_code == 401
        assert c.get("/task/meta").status_code == 401


# ----------------------------------------------------------- consent gate
def test_consent_auto_accepts_when_pin_matches(isolated_state):
    from kiroshi.join import _consent

    src = {"sha256": "abc123", "module": "m", "task_ref": "m:run",
           "filename": "m.py", "source": "x"}
    taskdist.write_pin("m", "abc123")
    assert _consent(src, "http://coordinator", None) is True


def test_consent_accept_hash_must_match(isolated_state):
    from kiroshi.join import _consent

    src = {"sha256": "abc", "module": "m2", "task_ref": "m2:run",
           "filename": "m2.py", "source": "x"}
    assert _consent(src, "http://coordinator", "abc") is True
    assert _consent(src, "http://coordinator", "wrong") is False


def test_consent_interactive_yes_no(isolated_state, monkeypatch):
    from kiroshi.join import _consent

    src = {"sha256": "z9", "module": "m3", "task_ref": "m3:run",
           "filename": "m3.py", "source": "x"}
    monkeypatch.setattr(builtins, "input", lambda *_a: "y")
    assert _consent(src, "http://coordinator", None) is True
    monkeypatch.setattr(builtins, "input", lambda *_a: "n")
    assert _consent(src, "http://coordinator", None) is False
    # fails closed on no input (non-interactive, no --accept-task-hash)
    def _raise(*_a):
        raise EOFError
    monkeypatch.setattr(builtins, "input", _raise)
    assert _consent(src, "http://coordinator", None) is False
