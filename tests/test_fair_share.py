"""Fair-share leasing + auto-sized capacity + the task selftest hook.

These guard the three hardening changes that reshape how work is distributed:
a Runner no longer defaults to a queue-draining capacity, no single host can
hoard the per-disk budget, and preflight can run a task's own fixture.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _store(n: int):
    from kiroshi.jobstore import JobStore

    s = JobStore(":memory:", max_retries=3)
    s.seed([{"job_id": f"g{i}", "spec": {}} for i in range(n)])
    return s


# ----------------------------------------------------------- host_share cap
def test_host_share_caps_in_flight_for_one_host():
    s = _store(20)
    first = s.lease("r1", "hostA", capacity=100, ttl=60, host_share=5)
    assert len(first.gigs) == 5  # capacity clamped to the ceiling


def test_host_share_accounts_for_already_leased():
    s = _store(20)
    s.lease("r1", "hostA", capacity=3, ttl=60, host_share=5)
    again = s.lease("r2", "hostA", capacity=100, ttl=60, host_share=5)
    assert len(again.gigs) == 2  # 5 ceiling - 3 already in flight


def test_host_share_zero_room_returns_empty_lease():
    s = _store(20)
    s.lease("r1", "hostA", capacity=5, ttl=60, host_share=5)
    blocked = s.lease("r2", "hostA", capacity=5, ttl=60, host_share=5)
    assert blocked.lease_id is None and blocked.gigs == []


def test_host_share_is_per_host_not_global():
    s = _store(20)
    s.lease("r1", "hostA", capacity=5, ttl=60, host_share=5)  # A saturated
    b = s.lease("r2", "hostB", capacity=100, ttl=60, host_share=5)
    assert len(b.gigs) == 5  # B gets its own independent slice


def test_no_host_share_is_inert():
    s = _store(20)
    r = s.lease("r1", "hostA", capacity=100, ttl=60, host_share=None)
    assert len(r.gigs) == 20  # unchanged first-N-pending behavior


# --------------------------------------------------- auto-sized capacity
def test_capacity_auto_sizes_from_workers():
    from kiroshi.config import CAPACITY_BUFFER, HostConfig

    hc = HostConfig(name="h", workers=8)
    assert hc.capacity == 8 + CAPACITY_BUFFER


def test_capacity_explicit_value_is_honored():
    from kiroshi.config import HostConfig

    hc = HostConfig(name="h", workers=8, capacity=64)
    assert hc.capacity == 64


def test_capacity_is_not_the_old_flat_200():
    from kiroshi.config import HostConfig

    hc = HostConfig(name="h", workers=4)
    assert hc.capacity < 200  # the whole point of the change


# ------------------------------------------------------- selftest resolver
def test_resolve_selftest_finds_hook(tmp_path, monkeypatch):
    mod = tmp_path / "mytask_ok.py"
    mod.write_text("def run(spec):\n    return {}\n"
                   "def selftest():\n    pass\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    from kiroshi.tasks import resolve_selftest

    assert resolve_selftest("mytask_ok:run") is not None


def test_resolve_selftest_absent_returns_none(tmp_path, monkeypatch):
    mod = tmp_path / "mytask_none.py"
    mod.write_text("def run(spec):\n    return {}\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    from kiroshi.tasks import resolve_selftest

    assert resolve_selftest("mytask_none:run") is None
