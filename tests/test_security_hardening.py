"""Security-hardening tests: mutual auth (Coordinator proves it holds the token before
a Runner trusts it), the /auth/challenge endpoint, secret redaction in logs and
captured launch commands, and path confinement of the example task.

These encode the specific attacks the hardening pass closed:
  - rogue Coordinator (e.g. winning `--fixer auto`) harvesting the token / injecting specs
  - the mesh token leaking to disk via teed logs or to the dashboard via launch cmd
  - a malicious spec making a Runner read/write outside its configured roots
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))  # so `examples.*` is importable

from kiroshi import security  # noqa: E402


# ----------------------------------------------------------- HMAC challenge
def test_prove_verify_roundtrip():
    tok = "mesh-secret-123"
    nonce = security.new_nonce()
    proof = security.prove(tok, nonce)
    assert security.verify_proof(tok, nonce, proof) is True
    # wrong token, wrong nonce, missing proof all fail closed
    assert security.verify_proof("other", nonce, proof) is False
    assert security.verify_proof(tok, "different-nonce", proof) is False
    assert security.verify_proof(tok, nonce, None) is False
    assert security.verify_proof(None, nonce, proof) is False


def test_no_auth_opt_out_ignores_persisted_token(tmp_path, monkeypatch):
    # --no-auth is an explicit operator opt-out: it must return None (no auth)
    # even when a persisted mesh.token file exists (e.g. left by a service install).
    # Previously ensure_coordinator_token consulted the token file BEFORE the opt-out,
    # silently re-enabling auth with a stale token.
    monkeypatch.setenv("KIROSHI_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "mesh.token").write_text("STALE-PERSISTED-TOKEN\n")
    monkeypatch.delenv("KIROSHI_TOKEN", raising=False)
    assert security.ensure_coordinator_token(None, allow_insecure=True) is None
    # without the opt-out, the persisted token IS honored (auth on)
    assert security.ensure_coordinator_token(None, allow_insecure=False) == "STALE-PERSISTED-TOKEN"
    # an explicit --token always wins, even alongside --no-auth
    assert security.ensure_coordinator_token("explicit", allow_insecure=True) == "explicit"


def test_auth_challenge_endpoint():
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app
    from kiroshi.jobstore import JobStore

    store = JobStore(":memory:", max_retries=3)
    app = create_app(store, token="T0KEN")
    with TestClient(app) as c:
        # challenge is reachable WITHOUT a token (it is the auth mechanism)
        r = c.get("/auth/challenge", params={"nonce": "abcdefgh12"})
        assert r.status_code == 200
        d = r.json()
        assert d["auth"] is True
        assert security.verify_proof("T0KEN", "abcdefgh12", d["proof"]) is True
        # a too-short / missing nonce is rejected (no MAC oracle on trivial input)
        assert c.get("/auth/challenge", params={"nonce": "x"}).status_code == 400


def test_auth_challenge_no_auth_app():
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app
    from kiroshi.jobstore import JobStore

    app = create_app(JobStore(":memory:", max_retries=3), token=None)
    with TestClient(app) as c:
        d = c.get("/auth/challenge", params={"nonce": "abcdefgh12"}).json()
        assert d["auth"] is False and d["proof"] is None


def test_custom_pages_are_token_gated():
    """/p/ must NOT be world-readable (it exposes task data)."""
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app
    from kiroshi.jobstore import JobStore

    app = create_app(JobStore(":memory:", max_retries=3), token="T0KEN")
    with TestClient(app) as c:
        # no token -> blocked (404/401 both acceptable; the point is "not 200 open")
        assert c.get("/p/job.html").status_code in (401, 404)


# ------------------------------------------------ Runner authenticates Coordinator
class _FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._p


def _runner(token, monkeypatch, server_token, server_auth=True):
    import kiroshi.worker as worker

    def fake_get(url, params=None, timeout=None):
        nonce = (params or {}).get("nonce", "")
        if not server_auth:
            return _FakeResp({"auth": False, "proof": None})
        return _FakeResp({"auth": True, "proof": security.prove(server_token, nonce)})

    monkeypatch.setattr(worker.requests, "get", fake_get)
    r = worker.Runner(coordinator_url="http://coordinator.local:8787", task_ref="t:run",
                      token=token)
    return r


def test_runner_trusts_matching_coordinator(monkeypatch):
    r = _runner("shared", monkeypatch, server_token="shared")
    assert r._verify_coordinator(r.coordinator_url) is True


def test_runner_refuses_rogue_coordinator_wrong_token(monkeypatch):
    # rogue Coordinator holds a DIFFERENT token -> cannot produce a valid proof
    r = _runner("shared", monkeypatch, server_token="attacker-token")
    assert r._verify_coordinator(r.coordinator_url) is False


def test_runner_refuses_coordinator_claiming_no_auth(monkeypatch):
    # We hold a token; a Coordinator that reports no-auth is rogue/misconfigured -> refuse
    r = _runner("shared", monkeypatch, server_token="shared", server_auth=False)
    assert r._verify_coordinator(r.coordinator_url) is False


def test_runner_unverifiable_coordinator_fails_closed(monkeypatch):
    import kiroshi.worker as worker

    def boom(url, params=None, timeout=None):
        raise worker.requests.RequestException("connection refused")

    monkeypatch.setattr(worker.requests, "get", boom)
    r = worker.Runner(coordinator_url="http://x:8787", task_ref="t:run", token="shared")
    assert r._verify_coordinator(r.coordinator_url) is False


# ------------------------------------------------------- secret redaction
def test_logsetup_redacts_secret():
    from kiroshi import logsetup

    secret = "abcd1234verysecrettoken"
    logsetup.redact(secret)
    scrubbed = logsetup._scrub(f"mesh token: {secret} done")
    assert secret not in scrubbed
    assert "REDACTED" in scrubbed


def test_launch_command_masks_token(monkeypatch):
    from kiroshi import cli

    monkeypatch.setattr(sys, "argv",
                        ["kiroshi", "runner", "--token", "SUPERSECRET",
                         "--task", "t:run"])
    cmd = cli._launch_command()
    assert "SUPERSECRET" not in cmd
    assert "***" in cmd and "t:run" in cmd


# --------------------------------------------------- example task confinement
def test_motion_resample_path_confinement(tmp_path, monkeypatch):
    pytest.importorskip("numpy")  # the example task imports numpy (the `motion` extra)
    root = str(tmp_path)
    monkeypatch.setenv("KIROSHI_READ_ROOT", root)
    mr = importlib.import_module("examples.motion_resample")

    # a normal relative path resolves INSIDE the root
    p = mr._resolve("clips/a.npz", root, "KIROSHI_READ_ROOT")
    assert root in str(p)

    # absolute paths are refused (Windows drive + POSIX anchor forms)
    with pytest.raises(ValueError):
        mr._resolve("C:\\Windows\\System32\\evil.npz", root, "KIROSHI_READ_ROOT")
    with pytest.raises(ValueError):
        mr._resolve("/etc/passwd", root, "KIROSHI_READ_ROOT")

    # parent-escape traversal is refused
    with pytest.raises(ValueError):
        mr._resolve("../../../escape.npz", root, "KIROSHI_READ_ROOT")

    # missing root (no per-disk root AND no env) => refuse rather than fall back to cwd
    monkeypatch.delenv("KIROSHI_READ_ROOT", raising=False)
    with pytest.raises(ValueError):
        mr._resolve("a.npz", None, "KIROSHI_READ_ROOT")

    # per-gig root wins over env (dual-path routing): a topology gig carries its
    # disk's direct root and the task reads from there, not the env root.
    from kiroshi import paths as kpaths

    assert kpaths.gig_read_root({"read_root": "//disk1/data"}) == "//disk1/data"
    monkeypatch.setenv("KIROSHI_READ_ROOT", "//env/fallback")
    assert kpaths.gig_read_root({"read_root": "//disk1/data"}) == "//disk1/data"
    assert kpaths.gig_read_root({}) == "//env/fallback"  # no spec root -> env
