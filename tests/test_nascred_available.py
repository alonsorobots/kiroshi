"""`GET /mesh/nas-cred/available`: a non-secret brokerability check the I/O gate
uses to decide whether an uncredentialed seed host is actually on the fast path
(the Runners can fetch creds from the broker). It must never leak the password,
must require the mesh token, and must be False when brokering can't work.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TOKEN = "test-mesh-token"


def _client(token=TOKEN):
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app
    from kiroshi.jobstore import JobStore

    app = create_app(JobStore(":memory:", max_retries=3), token=token)
    return TestClient(app)


def _hdr():
    return {"Authorization": f"Bearer {TOKEN}"}


def _patch_status(monkeypatch, present, servers):
    from kiroshi import nascred

    monkeypatch.setattr(nascred, "status",
                        lambda server="default": {"present": present,
                                                  "servers": servers,
                                                  "path": "x", "user": None})


def test_available_true_when_secret_stored(monkeypatch):
    _patch_status(monkeypatch, present=True, servers=["192.0.2.1"])
    with _client() as c:
        r = c.get("/mesh/nas-cred/available",
                  params={"server": "192.0.2.1"}, headers=_hdr()).json()
        assert r["available"] is True


def test_available_true_via_default_fallback(monkeypatch):
    # No per-server entry, but a "default" cred exists — load_secret falls back to
    # it, so brokering works and availability must reflect that.
    _patch_status(monkeypatch, present=False, servers=["default"])
    with _client() as c:
        r = c.get("/mesh/nas-cred/available",
                  params={"server": "192.0.2.1"}, headers=_hdr()).json()
        assert r["available"] is True


def test_unavailable_when_no_secret(monkeypatch):
    _patch_status(monkeypatch, present=False, servers=[])
    with _client() as c:
        r = c.get("/mesh/nas-cred/available",
                  params={"server": "nas"}, headers=_hdr()).json()
        assert r["available"] is False


def test_unavailable_without_mesh_token(monkeypatch):
    # No mesh token => the transit seal can't key off it => broker disabled, so
    # availability is False regardless of stored creds. (token=None also disables
    # the auth middleware, so no header is needed here.)
    _patch_status(monkeypatch, present=True, servers=["default"])
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app
    from kiroshi.jobstore import JobStore

    app = create_app(JobStore(":memory:", max_retries=3), token=None)
    with TestClient(app) as c:
        r = c.get("/mesh/nas-cred/available", params={"server": "nas"}).json()
        assert r["available"] is False


def test_requires_bearer_token(monkeypatch):
    _patch_status(monkeypatch, present=True, servers=["default"])
    with _client() as c:
        r = c.get("/mesh/nas-cred/available", params={"server": "nas"})
        assert r.status_code == 401


def test_never_returns_password(monkeypatch):
    _patch_status(monkeypatch, present=True, servers=["default"])
    with _client() as c:
        body = c.get("/mesh/nas-cred/available",
                     params={"server": "nas"}, headers=_hdr()).text.lower()
        assert "pw" not in body and "sealed" not in body
