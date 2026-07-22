"""End-to-end verification that the circuit breaker's state actually reaches
an operator: heartbeat -> coordinator._touch_runner -> /status's
runner_details -- the whole point of shipping this now instead of deferring
to a future dashboard pass. Uses a real coordinator (FastAPI TestClient,
in-memory JobStore) -- no live mesh/NAS involved, so this is safe to run
against production without any risk of disrupting a real campaign.
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

    app = create_app(
        JobStore(":memory:", max_retries=3),
        token=None,
        enable_advisories=False,
        configured_hosts=["Chronos"],
        **kw,
    )
    return TestClient(app)


def test_new_runner_defaults_to_closed_circuit():
    with _client() as c:
        c.post("/seed", json={"gigs": [{"subjob_id": "s0", "spec": {}}], "job": "j"})
        c.post("/register", json={
            "runner_id": "r1", "host": "Chronos", "task": "t:run", "workers": 4,
        })
        lease = c.post("/lease", json={
            "runner_id": "r1", "host": "Chronos", "capacity": 1, "job": "j",
        }).json()
        lease_id = lease["lease_id"]

        st = c.get("/status").json()
        job = next(j for j in st["jobs"] if j["job"] == "j")
        rd = next(r for r in job["runner_details"] if r["runner_id"] == "r1")
        assert rd["circuit"] == {"state": "closed"}

        # heartbeat with no circuit stats yet -> still defaults sanely (unchanged)
        r = c.post("/heartbeat", json={"lease_id": lease_id, "runner_id": "r1", "stats": {}})
        assert r.status_code == 200


def test_open_circuit_reported_via_heartbeat_flows_to_status():
    with _client() as c:
        c.post("/seed", json={"gigs": [{"subjob_id": "s0", "spec": {}}], "job": "j"})
        c.post("/register", json={
            "runner_id": "r1", "host": "Chronos", "task": "t:run", "workers": 4,
        })
        lease = c.post("/lease", json={
            "runner_id": "r1", "host": "Chronos", "capacity": 1, "job": "j",
        }).json()
        lease_id = lease["lease_id"]

        open_snapshot = {
            "state": "open",
            "dominant_error": "logonfailure",
            "consecutive_permanent": 3,
            "cooldown_s": 120.0,
        }
        r = c.post("/heartbeat", json={
            "lease_id": lease_id,
            "runner_id": "r1",
            "stats": {"job": "j", "circuit": open_snapshot},
        })
        assert r.status_code == 200

        st = c.get("/status").json()
        job = next(j for j in st["jobs"] if j["job"] == "j")
        rd = next(r for r in job["runner_details"] if r["runner_id"] == "r1")
        assert rd["circuit"] == open_snapshot

        # /runners also carries it through (full-dict passthrough)
        runners = c.get("/runners").json()["runners"]
        r1 = next(x for x in runners if x["runner_id"] == "r1")
        assert r1["circuit"] == open_snapshot


def test_circuit_transitions_back_to_closed_after_recovery():
    with _client() as c:
        c.post("/seed", json={"gigs": [{"subjob_id": "s0", "spec": {}}], "job": "j"})
        c.post("/register", json={
            "runner_id": "r1", "host": "Chronos", "task": "t:run", "workers": 4,
        })
        lease = c.post("/lease", json={
            "runner_id": "r1", "host": "Chronos", "capacity": 1, "job": "j",
        }).json()
        lease_id = lease["lease_id"]

        c.post("/heartbeat", json={
            "lease_id": lease_id, "runner_id": "r1",
            "stats": {"circuit": {"state": "open", "dominant_error": "logonfailure",
                                   "consecutive_permanent": 3, "cooldown_s": 120.0}},
        })
        # dependency fixed -> half-open probe succeeds -> runner reports closed again
        c.post("/heartbeat", json={
            "lease_id": lease_id, "runner_id": "r1",
            "stats": {"circuit": {"state": "closed", "dominant_error": "",
                                   "consecutive_permanent": 0, "cooldown_s": 120.0}},
        })

        st = c.get("/status").json()
        job = next(j for j in st["jobs"] if j["job"] == "j")
        rd = next(r for r in job["runner_details"] if r["runner_id"] == "r1")
        assert rd["circuit"]["state"] == "closed"


# ------------------------------------------------------- full lease-gating cycle
def test_full_cycle_trip_stop_leasing_then_self_heal_without_restart():
    """The actual end-to-end behavior the spec's verification section asks
    for, using the REAL FailureBreaker driving REAL lease decisions -- not a
    live NAS (too risky to point production at a broken credential mid-
    campaign), but the exact same control law a live runner uses.

    Simulates: a runner leases and fails a permanent error 3x (breaker trips
    -> stops leasing), confirms leasing is refused during the cooldown, then
    (dependency now fixed) confirms the half-open probe succeeds and full
    leasing resumes -- all without any process restart."""
    from kiroshi.failure_breaker import FailureBreaker

    breaker = FailureBreaker()
    t = 1000.0

    # Normal operation: leasing allowed, uncapped.
    may, cap = breaker.allow_lease(t)
    assert may is True and cap is None

    # Three permanent failures in a row -> trips.
    breaker.record("error", "NT_STATUS_LOGON_FAILURE", t)
    breaker.record("error", "NT_STATUS_LOGON_FAILURE", t)
    breaker.record("error", "NT_STATUS_LOGON_FAILURE", t)
    assert breaker.is_open

    # Still within cooldown -> refuses to lease at all (the flood-stopping behavior).
    may, cap = breaker.allow_lease(t + 5.0)
    assert may is False

    # Cooldown elapses -> half-open, exactly one probe sub-job allowed.
    probe_time = t + FailureBreaker.BASE_COOLDOWN_S + 1.0
    may, cap = breaker.allow_lease(probe_time)
    assert may is True and cap == 1
    breaker.note_leased(1)

    # A second poll while the probe is still outstanding must NOT lease more.
    may, cap = breaker.allow_lease(probe_time + 1.0)
    assert may is False

    # Dependency is fixed now -> probe succeeds -> breaker closes, full capacity resumes.
    breaker.record("ok", None, probe_time + 2.0)
    assert breaker.state == FailureBreaker.CLOSED
    may, cap = breaker.allow_lease(probe_time + 3.0)
    assert may is True and cap is None  # back to full, uncapped leasing -- no restart needed
