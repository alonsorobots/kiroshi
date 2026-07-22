"""Host ban: the Coordinator's lever over a runner it can't otherwise reach
(SSH dead/wedged) -- refuse ALL its future leases, every job, until unbanned.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.jobstore import JobStore  # noqa: E402


def _store(n: int = 10):
    s = JobStore(":memory:", max_retries=3)
    s.seed([{"subjob_id": f"g{i}", "spec": {}} for i in range(n)])
    return s


def test_unbanned_host_leases_normally():
    s = _store()
    r = s.lease("r1", "good-host", capacity=5, ttl=60)
    assert len(r.gigs) == 5


def test_banned_host_gets_nothing():
    s = _store()
    s.ban_host("bad-host", reason="disk full, SSH wedged")
    r = s.lease("r1", "bad-host", capacity=5, ttl=60)
    assert r.gigs == []
    assert r.diag["binding_reason"] == "HOST_BANNED"


def test_ban_is_host_scoped_not_fleet_wide():
    # A ban on one host must not affect another host's leasing.
    s = _store()
    s.ban_host("bad-host")
    banned = s.lease("r1", "bad-host", capacity=5, ttl=60)
    other = s.lease("r2", "good-host", capacity=5, ttl=60)
    assert banned.gigs == []
    assert len(other.gigs) == 5


def test_unban_restores_leasing():
    s = _store()
    s.ban_host("bad-host")
    assert s.lease("r1", "bad-host", capacity=5, ttl=60).gigs == []
    assert s.unban_host("bad-host") is True
    r = s.lease("r1", "bad-host", capacity=5, ttl=60)
    assert len(r.gigs) == 5


def test_unban_nonexistent_returns_false():
    s = _store()
    assert s.unban_host("never-banned") is False


def test_is_host_banned_and_listing():
    s = _store()
    assert s.is_host_banned("h1") is False
    s.ban_host("h1", reason="wedged")
    assert s.is_host_banned("h1") is True
    rows = s.banned_hosts()
    assert len(rows) == 1 and rows[0]["host"] == "h1" and rows[0]["reason"] == "wedged"


def test_ban_survives_reban_updates_reason():
    s = _store()
    s.ban_host("h1", reason="first")
    s.ban_host("h1", reason="second")
    rows = s.banned_hosts()
    assert len(rows) == 1 and rows[0]["reason"] == "second"


def test_job_scoped_lease_also_respects_ban():
    s = _store(0)
    s.seed([{"subjob_id": "held/x1", "spec": {}, "job": "held-frames-4fps"}])
    s.ban_host("bad-host")
    r = s.lease("r1", "bad-host", capacity=5, ttl=60, job="held-frames-4fps")
    assert r.gigs == [] and r.diag["binding_reason"] == "HOST_BANNED"
