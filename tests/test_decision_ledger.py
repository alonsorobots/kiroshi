"""Tests for the coordinator decision ledger — /lease/decisions, /job/trace,
/decisions/summary, and the /status scheduling block.

Uses FastAPI TestClient against an in-memory coordinator to verify the full
observability chain: seed -> lease -> decision recorded -> queryable.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _client(**kw):
    from fastapi.testclient import TestClient
    from kiroshi.coordinator import create_app
    from kiroshi.jobstore import JobStore

    app = create_app(JobStore(":memory:", max_retries=3), token=None,
                     enable_advisories=False, **kw)
    return TestClient(app)


def _seed(c, n, prefix="g"):
    gigs = [{"subjob_id": f"{prefix}{i}", "spec": {}} for i in range(n)]
    return c.post("/seed", json={"gigs": gigs}).json()


def test_lease_decision_recorded_after_lease():
    with _client() as c:
        _seed(c, 5)
        c.post("/lease", json={"runner_id": "r1", "host": "hostA", "capacity": 3})
        r = c.get("/lease/decisions").json()
        assert r["count"] >= 1
        d = r["decisions"][0]
        assert d["host"] == "hostA"
        assert d["granted"] == 3
        assert d["binding_reason"] == "GRANTED_FULL"


def test_lease_decision_no_pending():
    with _client() as c:
        c.post("/lease", json={"runner_id": "r1", "host": "hostA", "capacity": 5})
        r = c.get("/lease/decisions").json()
        d = r["decisions"][0]
        assert d["binding_reason"] == "NO_PENDING"
        assert d["granted"] == 0


def test_lease_decision_filter_by_host():
    with _client() as c:
        _seed(c, 10)
        c.post("/lease", json={"runner_id": "r1", "host": "hostA", "capacity": 2})
        c.post("/lease", json={"runner_id": "r2", "host": "hostB", "capacity": 2})
        r = c.get("/lease/decisions", params={"host": "hostB"}).json()
        assert all(d["host"] == "hostB" for d in r["decisions"])
        assert r["count"] >= 1


def test_job_trace_shows_seeded_then_leased():
    with _client() as c:
        _seed(c, 3, prefix="job")
        c.post("/lease", json={"runner_id": "r1", "host": "hostA", "capacity": 2})
        r = c.get("/subjob/trace", params={"subjob_id": "job0"}).json()
        events = [e["event"] for e in r["events"]]
        assert "SEEDED" in events
        assert "LEASED" in events
        assert r["current"] is not None
        assert r["current"]["subjob_id"] == "job0"


def test_decisions_summary_flags_starved_host():
    with _client(decision_ring=100) as c:
        _seed(c, 20)
        # hostA gets work, hostB gets nothing (empty store after hostA drains).
        c.post("/lease", json={"runner_id": "r1", "host": "hostA", "capacity": 20})
        # Now no pending left; hostB will get NO_PENDING repeatedly.
        for _ in range(3):
            c.post("/lease", json={"runner_id": "r2", "host": "hostB", "capacity": 5})
        r = c.get("/decisions/summary").json()
        per = r["per_host"]
        assert "hostB" in per
        assert per["hostB"]["granted"] == 0
        assert per["hostB"]["main_reason"] == "NO_PENDING"
        assert "hostB" in r["starved_hosts"]


def test_ring_buffer_bounded():
    with _client(decision_ring=5) as c:
        for i in range(20):
            c.post("/lease", json={"runner_id": "r1", "host": "h", "capacity": 1})
        r = c.get("/lease/decisions", params={"limit": 2000}).json()
        assert len(r["decisions"]) <= 5


def test_job_event_index_bounded():
    # The per-job index dict must be bounded (not just each deque), or a
    # long-lived coordinator leaks one entry per distinct sub-job forever.
    with _client(jobevent_ring=10) as c:
        for i in range(50):
            jid = f"ev{i}"
            c.post("/seed", json={"gigs": [{"subjob_id": jid, "spec": {}}]})
            lease = c.post("/lease", json={
                "runner_id": "r", "host": "h", "capacity": 1}).json()
            c.post("/complete", json={
                "lease_id": lease["lease_id"],
                "results": [{"subjob_id": jid, "status": "ok"}]})
        assert len(c.app.state.job_event_index) <= 10


def test_status_has_scheduling_block():
    with _client() as c:
        _seed(c, 5)
        c.post("/lease", json={"runner_id": "r1", "host": "hostA", "capacity": 3})
        st = c.get("/status").json()
        assert "scheduling" in st
        assert "hostA" in st["scheduling"]["per_host"]


def test_complete_records_job_event():
    with _client() as c:
        _seed(c, 1, prefix="cj")
        lease = c.post("/lease", json={"runner_id": "r1", "host": "h", "capacity": 1}).json()
        c.post("/complete", json={
            "lease_id": lease["lease_id"],
            "results": [{"subjob_id": "cj0", "status": "ok"}]})
        r = c.get("/subjob/trace", params={"subjob_id": "cj0"}).json()
        events = [e["event"] for e in r["events"]]
        assert "COMPLETED" in events


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fail = 0
    for t in tests:
        try:
            t(); print(f"PASS  {t.__name__}")
        except Exception as exc:
            print(f"FAIL  {t.__name__}: {exc!r}"); fail += 1
    print(f"\n{len(tests)-fail}/{len(tests)} passed")
    sys.exit(fail)
