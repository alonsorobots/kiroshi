"""Tests for the robustness/observability/security feature set:
auth, runner registration, /jobs + /history, the process registry, and
at-field pause awareness. No network or external deps beyond httpx (TestClient).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiroshi import atfield, processreg, security  # noqa: E402
from kiroshi.jobstore import JobStore  # noqa: E402


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Point the state dir at a temp folder so we never touch real ProgramData."""
    monkeypatch.setenv("KIROSHI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("KIROSHI_TOKEN", raising=False)
    monkeypatch.delenv("ATFIELD_STATE_DIR", raising=False)
    yield tmp_path


# --------------------------------------------------------------- security
def test_token_constant_time_and_disabled():
    assert security.token_matches(None, "anything") is True   # auth disabled
    assert security.token_matches("s3cret", "s3cret") is True
    assert security.token_matches("s3cret", "nope") is False
    assert security.token_matches("s3cret", None) is False


def test_extract_presented_token_variants():
    assert security.extract_presented_token({"Authorization": "Bearer abc"}, None) == "abc"
    assert security.extract_presented_token({"authorization": "bearer xyz"}, None) == "xyz"
    assert security.extract_presented_token({"X-Kiroshi-Token": "q"}, None) == "q"
    assert security.extract_presented_token({}, "fromquery") == "fromquery"
    assert security.extract_presented_token({}, None) is None


def test_ensure_fixer_token_persists(isolated_state):
    t1 = security.ensure_fixer_token()
    assert t1 and len(t1) > 16
    # a fresh resolve reads it back from the token file
    assert security.resolve_token() == t1
    # allow_insecure with no token configured => None (wide-open dev mesh)
    os.unlink(security.token_path())
    assert security.ensure_fixer_token(allow_insecure=True) is None


# --------------------------------------------------- coordinator auth + API
@pytest.fixture()
def client(isolated_state):
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app

    db = str(isolated_state / "jobs.db")
    store = JobStore(db, max_retries=3)
    app = create_app(store, token="T0KEN", launch_command="kiroshi fixer")
    with TestClient(app) as c:
        yield c


def test_unauthorized_without_token(client):
    assert client.get("/status").status_code == 401
    assert client.post("/seed", json={"gigs": []}).status_code == 401
    # open paths are reachable without a token
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 200


def test_full_flow_with_token_surfaces_launch_command(client):
    H = {"Authorization": "Bearer T0KEN"}
    client.post("/seed", headers=H, json={"gigs": [
        {"subjob_id": "j1", "spec": {}}, {"subjob_id": "j2", "spec": {}}]})
    client.post("/register", headers=H, json={
        "runner_id": "R1", "host": "host-b", "workers": 8, "task": "t:run",
        "launch_command": "kiroshi runner --workers 8 --task t:run --fixer auto"})
    lz = client.post("/lease", headers=H,
                     json={"runner_id": "R1", "host": "host-b", "capacity": 10}).json()
    assert len(lz["gigs"]) == 2
    client.post("/complete", headers=H, json={"lease_id": lz["lease_id"], "results": [
        {"subjob_id": "j1", "status": "ok"}, {"subjob_id": "j2", "status": "ok"}]})

    runners = client.get("/runners", headers=H).json()["runners"]
    assert runners[0]["launch_command"].startswith("kiroshi runner")
    jobs = client.get("/subjobs", headers=H).json()["jobs"]
    assert all(j["launch_command"].startswith("kiroshi runner") for j in jobs)
    hist = client.get("/history", headers=H).json()["jobs"]
    assert {j["state"] for j in hist} == {"done"}
    # query-param token also works (browser/EventSource convenience)
    assert client.get("/status?token=T0KEN").status_code == 200


# --------------------------------------------------- jobstore listing
def test_list_jobs_and_job_detail(isolated_state):
    store = JobStore(str(isolated_state / "j.db"), max_retries=3)
    store.seed([{"subjob_id": "a", "spec": {"x": 1}}, {"subjob_id": "b", "spec": {}}])
    rows = store.list_jobs(limit=10)
    assert {r["subjob_id"] for r in rows} == {"a", "b"}
    assert all(r["state"] == "pending" for r in rows)
    detail = store.job("a")
    assert detail and detail["spec"] == {"x": 1}
    assert store.job("missing") is None


# --------------------------------------------------- process registry
def test_process_registration_and_stop(isolated_state):
    fired = {"v": False}
    reg = processreg.ProcessRegistration(
        "runner", {"launch_command": "kiroshi runner --workers 4"},
        on_stop=lambda: fired.__setitem__("v", True), refresh=0.2,
    ).start()
    try:
        listed = processreg.list_registered()
        assert any(p["role"] == "runner" and p["launch_command"].startswith("kiroshi runner")
                   for p in listed)
        # external "emergency stop": drop the stop sentinel
        assert processreg.request_stop("runner", os.getpid()) is True
        deadline = time.time() + 3
        while not fired["v"] and time.time() < deadline:
            time.sleep(0.1)
        assert fired["v"] is True
    finally:
        reg.close()
    # manifest removed on close
    assert not any(p["pid"] == os.getpid() for p in processreg.list_registered())


def test_request_stop_unknown_returns_false(isolated_state):
    assert processreg.request_stop("runner", 999999) is False


# --------------------------------------------------- campaign grouping
def test_group_stats_and_migration(isolated_state):
    import sqlite3

    # old-schema DB with no `job` column gets migrated + backfilled on open
    db = str(isolated_state / "old.db")
    con = sqlite3.connect(db)
    # OLD schema: table 'jobs' with 'job_id', 'grp' columns
    con.execute(
        "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, spec TEXT NOT NULL, "
        "state TEXT NOT NULL DEFAULT 'pending', lease_id TEXT, runner_id TEXT, "
        "host TEXT, attempts INTEGER NOT NULL DEFAULT 0, leased_at REAL, "
        "lease_deadline REAL, completed_at REAL, error TEXT, metrics TEXT, "
        "created_at REAL NOT NULL, grp TEXT)")
    con.execute("CREATE TABLE campaigns (grp TEXT PRIMARY KEY, label TEXT, created_at REAL NOT NULL)")
    con.execute("INSERT INTO jobs(job_id,spec,state,created_at,grp) VALUES "
                "('camp/a','{}','done',1.0,'camp')")
    con.execute("INSERT INTO jobs(job_id,spec,state,created_at,grp) VALUES "
                "('camp/b','{}','pending',2.0,'camp')")
    con.commit()
    con.close()

    # JobStore auto-migrates old schema → new (subjobs + jobs tables)
    store = JobStore(db, max_retries=3)
    gs = {g["job"]: g for g in store.group_stats()}
    assert gs["camp"]["total"] == 2 and gs["camp"]["done"] == 1
    store.seed([{"subjob_id": "x2/q", "spec": {}}, {"subjob_id": "loose", "spec": {}}])
    gs = {g["job"]: g for g in store.group_stats()}
    assert set(gs) == {"camp", "x2", "(ungrouped)"}


def test_groups_endpoint_attaches_launch_commands(client):
    H = {"Authorization": "Bearer T0KEN"}
    client.post("/seed", headers=H, json={"gigs": [
        {"subjob_id": "campA/1", "spec": {}}, {"subjob_id": "campA/2", "spec": {}}]})
    client.post("/register", headers=H, json={
        "runner_id": "R1", "host": "h", "task": "t:run", "workers": 4,
        "launch_command": "kiroshi runner --task t:run --workers 4"})
    client.post("/lease", headers=H, json={"runner_id": "R1", "host": "h", "capacity": 5})
    groups = client.get("/groups", headers=H).json()["groups"]
    campA = next(g for g in groups if g["job"] == "campA")
    assert campA["launch_commands"] == ["kiroshi runner --task t:run --workers 4"]


def test_job_detail_endpoint(client):
    H = {"Authorization": "Bearer T0KEN"}
    client.post("/seed", headers=H, json={"gigs": [{"subjob_id": "g/1", "spec": {"k": 9}}]})
    d = client.get("/subjob/g/1", headers=H).json()
    assert d["subjob_id"] == "g/1" and d["spec"] == {"k": 9} and d["state"] == "pending"
    assert client.get("/subjob/missing", headers=H).status_code == 404


# --------------------------------------------------- explicit campaign + label
def test_explicit_group_overrides_job_id_prefix(isolated_state):
    store = JobStore(str(isolated_state / "g.db"), max_retries=3)
    # subjob_ids have no shared prefix, but an explicit job collects them under one
    # campaign — the fix for "thousands of per-clip jobs" in the dashboard.
    store.seed(
        [{"subjob_id": "clipA.npz|4", "spec": {}}, {"subjob_id": "clipB.npz|8", "spec": {}}],
        job="seamless-30to48", label="Converting Seamless Interactions 30fps -> 4,8 fps",
    )
    gs = {g["job"]: g for g in store.group_stats()}
    assert set(gs) == {"seamless-30to48"}
    assert gs["seamless-30to48"]["total"] == 2
    assert gs["seamless-30to48"]["label"] == "Converting Seamless Interactions 30fps -> 4,8 fps"


def test_label_skipped_for_mixed_batch_without_group(isolated_state):
    store = JobStore(str(isolated_state / "g2.db"), max_retries=3)
    # No batch job + gigs resolve to different prefixes => no single label home.
    store.seed(
        [{"subjob_id": "campA/1", "spec": {}}, {"subjob_id": "campB/1", "spec": {}}],
        label="should not stick",
    )
    for g in store.group_stats():
        assert g.get("label") in (None, "")


def test_seed_endpoint_accepts_group_and_label(client):
    H = {"Authorization": "Bearer T0KEN"}
    client.post("/seed", headers=H, json={
        "gigs": [{"subjob_id": "x/1", "spec": {}}, {"subjob_id": "y/2", "spec": {}}],
        "job": "camp", "label": "My Campaign",
    })
    groups = client.get("/groups", headers=H).json()["groups"]
    camp = next(g for g in groups if g["job"] == "camp")
    assert camp["total"] == 2 and camp["label"] == "My Campaign"


# --------------------------------------------------- at-field pause
def test_atfield_pause_absent_and_active(tmp_path, monkeypatch):
    monkeypatch.setenv("ATFIELD_STATE_DIR", str(tmp_path))
    assert atfield.is_paused() is False
    sentinel = tmp_path / "pause.sentinel"
    sentinel.write_text("", encoding="utf-8")          # empty => indefinite pause
    assert atfield.is_paused() is True
    # future expiry => paused
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    sentinel.write_text(future + "\n", encoding="utf-8")
    assert atfield.is_paused() is True
    # past expiry => not paused
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    sentinel.write_text(past + "\n", encoding="utf-8")
    assert atfield.is_paused() is False
