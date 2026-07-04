"""Tests for statusview.enrich_status — unified /status dashboard shape."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _client(**kw):
    from fastapi.testclient import TestClient
    from kiroshi.coordinator import create_app
    from kiroshi.jobstore import JobStore

    app = create_app(
        JobStore(":memory:", max_retries=3),
        token=None,
        enable_advisories=False,
        configured_hosts=["Chronos", "Demeter", "Aurora"],
        **kw,
    )
    return TestClient(app)


def test_status_includes_jobs_and_fleet():
    with _client() as c:
        c.post("/seed", json={
            "gigs": [{"subjob_id": "g0", "spec": {}}],
            "job": "demo-job",
            "label": "demo label",
        })
        st = c.get("/status").json()
        assert st["counts_are"] == "sub-jobs"
        assert "jobs" in st
        assert "fleet" in st
        assert len(st["jobs"]) == 1
        j = st["jobs"][0]
        assert j["job"] == "demo-job"
        assert j["label"] == "demo label"
        assert "subjobs_total" in j
        assert "resources" in j
        assert "health" in j
        assert st["fleet"]["mesh"]["configured_hosts"] == ["Chronos", "Demeter", "Aurora"]
        assert st["fleet"]["mesh"]["missing_hosts"] == ["Aurora", "Chronos", "Demeter"]


def test_status_marks_stalled_job():
    with _client() as c:
        c.post("/seed", json={
            "gigs": [{"subjob_id": "s0", "spec": {}}],
            "job": "stuck",
        })
        c.post("/register", json={
            "runner_id": "r1", "host": "Chronos", "task": "t:run", "workers": 4,
        })
        lease = c.post("/lease", json={
            "runner_id": "r1", "host": "Chronos", "capacity": 1, "job": "stuck",
        }).json()
        assert lease.get("gigs")
        # Backdate last_leased via direct store manipulation isn't easy; use
        # enrich_status unit test for stall timing instead.
        from kiroshi.statusview import _job_health
        code, msg = _job_health(
            job_slug="stuck",
            leased=1,
            pending=0,
            rate_per_s=0.0,
            last_completed=None,
            last_leased=time.time() - 700,
            now=time.time(),
            stall_s=600,
            leased_sample=["s0"],
        )
        assert code == "stalled"
        assert "STALLED" in (msg or "")


def test_status_runner_resources_on_register():
    with _client() as c:
        c.post("/register", json={
            "runner_id": "r1",
            "host": "Chronos",
            "task": "mod:run",
            "workers": 8,
            "resources": {"cpu_pct": 90.0, "mem_used_gb": 4.0, "gpu_util_pct": 12.0},
            "code_fingerprint": {"repos": {"kiroshi": {"sha": "abc123", "dirty": False}}},
        })
        st = c.get("/status").json()
        runners = st["fleet"]["runners"]
        assert len(runners) == 1
        assert runners[0]["resources"]["cpu_pct"] == 90.0
        assert runners[0]["code_fingerprint"]["repos"]["kiroshi"]["sha"] == "abc123"
        assert "Chronos" in st["fleet"]["mesh"]["live_hosts"]


def test_error_digest_per_job():
    with _client() as c:
        from kiroshi.jobstore import JobStore
        from kiroshi.coordinator import create_app
        from fastapi.testclient import TestClient

        app = create_app(
            JobStore(":memory:", max_retries=0),
            token=None,
            enable_advisories=False,
        )
        c = TestClient(app)
        c.post("/seed", json={
            "gigs": [
                {"subjob_id": "a", "spec": {}},
                {"subjob_id": "b", "spec": {}},
            ],
            "job": "fail-demo",
        })
        c.post("/register", json={"runner_id": "r1", "host": "h", "workers": 1})
        lease = c.post("/lease", json={"runner_id": "r1", "host": "h", "capacity": 2}).json()
        lease_id = lease.get("lease_id")
        assert lease_id
        c.post("/complete", json={
            "lease_id": lease_id,
            "results": [
                {"subjob_id": "a", "status": "error", "error": "FileNotFoundError(2)"},
                {"subjob_id": "b", "status": "error", "error": "FileNotFoundError(2)"},
            ],
        })
        st = c.get("/status").json()
        digest = st["jobs"][0]["error_digest"]
        assert digest[0]["count"] == 2
