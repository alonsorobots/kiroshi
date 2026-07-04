"""`kiroshi cancel --job X`: drop a job's queued gigs (and optionally purge its
history + metadata) via a token-gated coordinator endpoint. This is the
first-class alternative to hand-editing the SQLite queue when a job is stale.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _client(token=None):
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app
    from kiroshi.jobstore import JobStore

    app = create_app(JobStore(":memory:", max_retries=3), token=token)
    return TestClient(app)


def _seed(c, job, ids):
    c.post("/seed", json={"gigs": [{"subjob_id": i, "spec": {}} for i in ids],
                          "job": job})


def test_cancel_drops_only_target_job_queued_gigs():
    with _client() as c:
        _seed(c, "stale", ["a", "b", "c"])
        _seed(c, "keep", ["x", "y"])
        r = c.post("/cancel", json={"job": "stale"}).json()
        assert r["deleted"] == 3 and r["purged"] == 0
        # the other job is untouched and still leasable
        got = c.post("/lease", json={"runner_id": "r", "host": "h",
                                     "capacity": 10, "job": "keep"}).json()
        assert {g["subjob_id"] for g in got["gigs"]} == {"x", "y"}
        # the cancelled job has nothing left to lease
        none = c.post("/lease", json={"runner_id": "r2", "host": "h",
                                      "capacity": 10, "job": "stale"}).json()
        assert none["gigs"] == []


def test_cancel_reclaims_leased_gigs():
    with _client() as c:
        _seed(c, "j", ["g1", "g2"])
        c.post("/lease", json={"runner_id": "r", "host": "h", "capacity": 10,
                               "job": "j"})  # -> both leased
        r = c.post("/cancel", json={"job": "j"}).json()
        assert r["deleted"] == 2  # leased gigs are dropped too


def test_purge_removes_completed_history_and_metadata():
    with _client() as c:
        _seed(c, "j", ["g1", "g2"])
        lease = c.post("/lease", json={"runner_id": "r", "host": "h",
                                       "capacity": 10, "job": "j"}).json()
        c.post("/complete", json={
            "lease_id": lease["lease_id"],
            "results": [{"subjob_id": g["subjob_id"], "status": "ok"}
                        for g in lease["gigs"]]})
        # plain cancel leaves completed history in place (nothing pending/leased)
        assert c.post("/cancel", json={"job": "j"}).json()["deleted"] == 0
        # purge nukes everything, job disappears from /jobs
        p = c.post("/cancel", json={"job": "j", "purge": True}).json()
        assert p["deleted"] == 2 and p["purged"] == 1
        # the job is gone entirely — no rows under its slug
        assert c.get("/subjobs", params={"job": "j"}).json()["jobs"] == []


def test_cancel_requires_job():
    with _client() as c:
        # pydantic rejects a missing required field
        assert c.post("/cancel", json={}).status_code == 422
        # empty slug is refused by the store guard (surfaces as a server error)
        assert c.post("/cancel", json={"job": ""}).status_code >= 400


def test_cancel_is_token_gated():
    with _client(token="secret") as c:
        assert c.post("/cancel", json={"job": "j"}).status_code == 401
        ok = c.post("/cancel", json={"job": "j"},
                    headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200
