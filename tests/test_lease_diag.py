"""Tests for the LeaseResult.diag field — decision diagnostics that explain
*why* a lease returned the count it did (binding_reason, fair-share ceiling,
per-disk budget snapshot). No algorithm change; pure observability.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _store():
    from kiroshi.jobstore import JobStore
    return JobStore(":memory:", max_retries=3)


def _seed(s, n, disk=None, prefix="g"):
    gigs = []
    for i in range(n):
        g = {"subjob_id": f"{prefix}{i}", "spec": {}}
        if disk:
            g["disk"] = disk
        gigs.append(g)
    s.seed(gigs)


def test_empty_store_returns_no_pending():
    s = _store()
    r = s.lease("r1", "hostA", capacity=10, ttl=60)
    assert r.diag is not None
    assert r.diag["binding_reason"] == "NO_PENDING"
    assert r.diag["granted"] == 0
    assert r.diag["pending_total"] == 0


def test_granted_full_when_capacity_covers_pending():
    s = _store()
    _seed(s, 5)
    r = s.lease("r1", "hostA", capacity=10, ttl=60)
    assert len(r.gigs) == 5
    assert r.diag["binding_reason"] == "GRANTED_FULL"
    assert r.diag["granted"] == 5
    assert r.diag["requested_capacity"] == 10


def test_fair_share_cap_when_ceiling_exhausted():
    s = _store()
    _seed(s, 20)
    # First lease fills hostA's 5-slot ceiling.
    first = s.lease("r1", "hostA", capacity=100, ttl=60, host_share=5)
    assert len(first.gigs) == 5
    # Second lease on same host: ceiling met -> 0 granted.
    blocked = s.lease("r2", "hostA", capacity=5, ttl=60, host_share=5)
    assert blocked.lease_id is None
    assert blocked.diag["binding_reason"] == "FAIR_SHARE_CAP"
    assert blocked.diag["fair_share_ceiling"] == 5
    assert blocked.diag["host_inflight_before"] == 5
    assert blocked.diag["granted"] == 0


def test_fair_share_partial_cap_reason():
    s = _store()
    _seed(s, 20)
    # host_share=5 shrinks a request of 100 down to 5: the host DID get work,
    # but fair-share (not an empty queue) held it below the request -> the
    # binding constraint is FAIR_SHARE_CAP, not a misleading GRANTED_FULL.
    r = s.lease("r1", "hostA", capacity=100, ttl=60, host_share=5)
    assert len(r.gigs) == 5
    assert r.diag["binding_reason"] == "FAIR_SHARE_CAP"
    assert r.diag["fair_share_ceiling"] == 5


def test_disk_budget_full_when_budget_saturated():
    s = _store()
    # Seed gigs on two disks, budget=2 each.
    _seed(s, 10, disk="diskA", prefix="a")
    _seed(s, 10, disk="diskB", prefix="b")
    budget = {"diskA": 2, "diskB": 2}
    # First lease asks for 100, grabs only 4 (2 per disk = full budget) and
    # leaves 16 pending on the table -> the disk budget was the binding wall,
    # so this must read DISK_BUDGET_FULL (NOT GRANTED_FULL, which would make a
    # host stuck at grant_ratio=0.04 look healthy in /decisions/summary).
    first = s.lease("r1", "hostA", capacity=100, ttl=60, disk_concurrency=budget)
    assert len(first.gigs) == 4
    assert first.diag["binding_reason"] == "DISK_BUDGET_FULL"
    # Second lease: all disks at budget -> 0 granted.
    blocked = s.lease("r2", "hostB", capacity=10, ttl=60, disk_concurrency=budget)
    assert blocked.lease_id is None
    assert blocked.diag["binding_reason"] == "DISK_BUDGET_FULL"
    assert blocked.diag["granted"] == 0
    snap = blocked.diag["disk"]
    assert snap["diskA"]["free"] == 0
    assert snap["diskB"]["free"] == 0


def test_diag_has_per_disk_snapshot():
    s = _store()
    _seed(s, 10, disk="diskA", prefix="a")
    _seed(s, 10, disk="diskB", prefix="b")
    budget = {"diskA": 3, "diskB": 3}
    r = s.lease("r1", "hostA", capacity=4, ttl=60, disk_concurrency=budget)
    assert r.diag["disk"]["diskA"]["budget"] == 3
    assert r.diag["disk"]["diskB"]["budget"] == 3
    # granted_here should sum to len(gigs)
    total_granted = sum(d["granted_here"] for d in r.diag["disk"].values())
    assert total_granted == len(r.gigs)
    assert len(r.diag["granted_subjob_ids"]) == len(r.gigs)


def test_no_disk_concurrency_still_has_diag():
    s = _store()
    _seed(s, 3)
    r = s.lease("r1", "hostA", capacity=10, ttl=60)
    assert r.diag is not None
    assert r.diag["binding_reason"] == "GRANTED_FULL"
    assert r.diag["disk"] == {}  # no disk budget -> empty snapshot


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
