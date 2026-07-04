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
    s.seed([{"subjob_id": f"g{i}", "spec": {}} for i in range(n)])
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


# ------------------------------------------- per-host SSH identity (Aurora fix)
def test_ssh_target_uses_configured_user():
    from kiroshi.config import HostConfig

    hc = HostConfig(name="Aurora", user="alons")
    assert hc.ssh_target == "alons@aurora" or hc.ssh_target == "alons@Aurora"


def test_ssh_target_defaults_to_host_when_no_user():
    from kiroshi.config import HostConfig

    hc = HostConfig(name="Demeter")
    assert hc.ssh_target == "Demeter"


def test_ssh_target_honors_explicit_ssh_host():
    from kiroshi.config import HostConfig

    hc = HostConfig(name="Aurora", user="alons", ssh_host="aurora.lan")
    assert hc.ssh_target == "alons@aurora.lan"


def test_remap_repo_swaps_root_for_node_user():
    from kiroshi.remote_sync import _remap_repo

    out = _remap_repo(r"C:\Users\admin\Desktop\RESEARCH\Pose_MBPE",
                      r"C:\Users\alons\Desktop\RESEARCH")
    assert out == r"C:\Users\alons\Desktop\RESEARCH\Pose_MBPE"


def test_remap_repo_no_root_is_verbatim():
    from kiroshi.remote_sync import _remap_repo

    p = r"C:\Users\admin\Desktop\RESEARCH\kiroshi"
    assert _remap_repo(p, None) == p


def test_plan_sync_uses_ssh_target_and_remapped_path():
    from kiroshi.config import HostConfig
    from kiroshi.remote_sync import plan_sync

    hosts = {"Aurora": HostConfig(name="Aurora", user="alons",
                                  root=r"C:\Users\alons\Desktop\RESEARCH")}
    plans = plan_sync(hosts, repos=[r"C:\Users\admin\Desktop\RESEARCH\kiroshi"])
    assert plans[0].ssh_target == "alons@Aurora"
    # Path is remapped admin->alons AND normalized to forward-slash + double-quote
    # so the command is safe for both cmd.exe and POSIX sh (see _quote_remote_path).
    assert '"C:/Users/alons/Desktop/RESEARCH/kiroshi"' in plans[0].steps[0].remote_cmd
