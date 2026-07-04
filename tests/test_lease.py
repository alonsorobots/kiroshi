"""Adaptive lease TTL: the Fixer sizes a lease as a safe multiple of the Runner's
heartbeat cadence, so a slow-but-alive Runner isn't reaped and handed the same gig
twice (at-least-once duplication — the cause of the rare "file in use" write race).
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

    app = create_app(JobStore(":memory:", max_retries=3), token=None, **kw)
    return TestClient(app)


def _lease_ttl(c, hb=None):
    payload = {"runner_id": "r1", "host": "h", "capacity": 10}
    if hb is not None:
        payload["heartbeat_interval"] = hb
    return c.post("/lease", json=payload).json()["ttl"]


def test_default_ttl_when_no_heartbeat_reported():
    with _client(lease_ttl=120.0) as c:
        assert _lease_ttl(c, hb=None) == 120.0


def test_ttl_scales_to_heartbeat_cadence():
    # hb=40s * miss_tolerance(4) = 160 > base 120 -> adaptive floor applies
    with _client(lease_ttl=120.0, lease_miss_tolerance=4.0) as c:
        assert _lease_ttl(c, hb=40.0) == 160.0


def test_fast_heartbeat_keeps_base_floor():
    # hb=10s * 4 = 40 < base 120 -> never shrink below the configured floor
    with _client(lease_ttl=120.0, lease_miss_tolerance=4.0) as c:
        assert _lease_ttl(c, hb=10.0) == 120.0


def test_ttl_is_capped():
    with _client(lease_ttl=120.0, lease_miss_tolerance=4.0, lease_ttl_cap=300.0) as c:
        assert _lease_ttl(c, hb=1000.0) == 300.0


def test_heartbeat_extends_with_same_adaptive_ttl():
    with _client(lease_ttl=120.0, lease_miss_tolerance=4.0) as c:
        # seed one gig so the lease holds something
        c.post("/seed", json={"gigs": [{"subjob_id": "g1", "spec": {}}]})
        lease = c.post("/lease", json={"runner_id": "r1", "host": "h", "capacity": 5,
                                       "heartbeat_interval": 40.0}).json()
        hb = c.post("/heartbeat", json={"lease_id": lease["lease_id"], "runner_id": "r1",
                                        "heartbeat_interval": 40.0}).json()
        assert hb["ttl"] == 160.0 and hb["extended"] >= 1
