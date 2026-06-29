"""Tests for M8 N1+N2 — storage topology + disk-aware leasing (PLAN §7.6).

Covers: gig.disk derivation (range/glob/substring + spec-path), the disk column +
migration, and the disk-aware lease — the mesh-global per-spindle budget +
round-robin interleave. The key invariant: only the Fixer can cap per-disk
in-flight across the whole fleet, so over-subscribing a spindle is impossible.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi.jobstore import JobStore  # noqa: E402
from kiroshi.storage import (  # noqa: E402
    DiskConfig,
    derive_disk,
    disk_concurrency_map,
    kind_default_concurrency,
    match_disk,
)


# --------------------------------------------------------------- match / derive
def test_match_range_substring_and_glob():
    assert match_disk("shard_03/clip.npz", "shard_01..08")
    assert not match_disk("shard_09/clip.npz", "shard_01..08")
    assert match_disk("shard_01/clip.npz", "shard_01")           # substring
    assert match_disk("shard_03/clip.npz", "shard_0[1-4]*")      # glob
    assert not match_disk("notes.txt", "shard_01..08")


def test_kind_defaults_hdd_low_nvme_high():
    assert kind_default_concurrency("hdd") < kind_default_concurrency("ssd") \
        < kind_default_concurrency("nvme")
    assert kind_default_concurrency(None) == 4


def test_derive_disk_from_job_id_and_spec_path():
    disks = [DiskConfig(id="d1", match="shard_01..08"),
             DiskConfig(id="d2", match="shard_09..16")]
    assert derive_disk("shard_03/a.npz", {}, disks) == "d1"
    assert derive_disk("x", {"src_path": "//nas/disk2/shard_12/c.npz"}, disks) == "d2"
    assert derive_disk("no-shard-here", {}, disks) is None      # unmatched -> uncapped
    assert derive_disk("anything", {}, []) is None               # no topology -> inert


def test_concurrency_map_only_caps_declared_disks():
    disks = [DiskConfig(id="d1", kind="hdd", concurrency=6),
             DiskConfig(id="d2", kind="nvme")]
    m = disk_concurrency_map(disks)
    assert m == {"d1": 6, "d2": kind_default_concurrency("nvme")}


# --------------------------------------------------------- disk column + seed
def test_seed_persists_disk(tmp_path):
    store = JobStore(str(tmp_path / "s.db"), max_retries=3)
    store.seed([
        {"job_id": "shard_01/a", "spec": {}, "disk": "d1"},
        {"job_id": "shard_09/b", "spec": {}},  # no disk -> NULL (uncapped)
    ])
    assert store.job("shard_01/a")["disk"] == "d1"
    assert store.job("shard_09/b")["disk"] is None


def test_disk_column_migrates_on_old_db(tmp_path):
    db = tmp_path / "old.db"
    import sqlite3

    # A DB from before the `disk` column: every column the schema had EXCEPT disk.
    c = sqlite3.connect(str(db))
    c.execute(
        "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, spec TEXT, state TEXT, "
        "lease_id TEXT, runner_id TEXT, host TEXT, attempts INTEGER, "
        "leased_at REAL, lease_deadline REAL, completed_at REAL, error TEXT, "
        "metrics TEXT, created_at REAL, grp TEXT)")
    c.execute("INSERT INTO jobs (job_id,spec,state,created_at,grp) "
              "VALUES ('x','{}','pending',1,'g')")
    c.commit()
    c.close()
    # opening with JobStore must add the disk column transparently
    store = JobStore(str(db), max_retries=3)
    assert store.job("x")["disk"] is None


# ------------------------------------------------------- disk-aware leasing
def _seed_many(store, n_per_disk, disks=("d1", "d2")):
    gigs = []
    for d in disks:
        for i in range(n_per_disk):
            gigs.append({"job_id": f"{d}/g{i}", "spec": {}, "disk": d})
    store.seed(gigs)


def test_lease_inert_without_budget(tmp_path):
    store = JobStore(str(tmp_path / "l.db"), max_retries=3)
    _seed_many(store, 5)
    # no disk_concurrency -> plain "first N pending", no cap
    res = store.lease("r1", "h", 10, 60)
    assert len(res.gigs) == 10
    assert all(g["disk"] in ("d1", "d2") for g in res.gigs)


def test_lease_caps_per_disk_budget(tmp_path):
    store = JobStore(str(tmp_path / "l2.db"), max_retries=3)
    _seed_many(store, 5)  # 5 on d1, 5 on d2
    budget = {"d1": 2, "d2": 2}
    res = store.lease("r1", "h", 10, 60, disk_concurrency=budget)
    # capacity 10 but budget 2+2=4 -> only 4 leased, split 2/2
    assert len(res.gigs) == 4
    by = {}
    for g in res.gigs:
        by[g["disk"]] = by.get(g["disk"], 0) + 1
    assert by == {"d1": 2, "d2": 2}


def test_lease_round_robin_keeps_all_disks_busy(tmp_path):
    store = JobStore(str(tmp_path / "l3.db"), max_retries=3)
    _seed_many(store, 4)  # 4 each
    budget = {"d1": 4, "d2": 4}
    res = store.lease("r1", "h", 8, 60, disk_concurrency=budget)
    assert len(res.gigs) == 8
    # round-robin: the FIRST two leased should span both disks (not drain d1 first)
    first_disks = [g["disk"] for g in res.gigs[:2]]
    assert len(set(first_disks)) == 2


def test_second_lease_blocked_while_first_holds_budget(tmp_path):
    # the distributed semaphore: Runner A fills disk1's budget; Runner B gets none
    # from disk1 until A completes (only the Fixer sees this fleet-wide).
    store = JobStore(str(tmp_path / "l4.db"), max_retries=3)
    _seed_many(store, 5)
    budget = {"d1": 2, "d2": 2}
    a = store.lease("A", "h", 10, 60, disk_concurrency=budget)
    assert len(a.gigs) == 4  # fills both disks' budgets
    b = store.lease("B", "h", 10, 60, disk_concurrency=budget)
    assert b.gigs == []       # both disks saturated fleet-wide -> nothing left to lease
    # A completes -> budget frees -> B can now lease
    store.complete([{"job_id": g["job_id"], "status": "ok", "metrics": {}}
                    for g in a.gigs])
    b2 = store.lease("B", "h", 10, 60, disk_concurrency=budget)
    assert len(b2.gigs) == 4


def test_unknown_disk_is_uncapped(tmp_path):
    # a gig whose disk is NOT in the budget map is uncapped (safe default: only
    # declared disks are capped) — never starved by a missing topology entry.
    store = JobStore(str(tmp_path / "l5.db"), max_retries=3)
    store.seed([{"job_id": f"dx/g{i}", "spec": {}, "disk": "dx"} for i in range(6)])
    res = store.lease("r", "h", 6, 60, disk_concurrency={"d1": 2})  # 'dx' not in map
    assert len(res.gigs) == 6


def test_coordinator_seed_derives_disk(tmp_path):
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app
    from kiroshi.storage import DiskConfig

    disks = [DiskConfig(id="d1", match="shard_01..08"),
             DiskConfig(id="d2", match="shard_09..16")]
    app = create_app(JobStore(str(tmp_path / "c.db")), token="T", disks=disks)
    c = TestClient(app)
    H = {"Authorization": "Bearer T"}
    c.post("/seed", headers=H, json={"gigs": [
        {"job_id": "shard_03/a", "spec": {"src_path": "shard_03/a.npz"}},
        {"job_id": "shard_12/b", "spec": {}},
    ]})
    g3 = c.get("/job/shard_03/a", headers=H).json()
    g12 = c.get("/job/shard_12/b", headers=H).json()
    assert g3["disk"] == "d1"
    assert g12["disk"] == "d2"


# --------------------------------------------------- dual-path routing (N3)
def test_inject_roots_stamps_disk_paths():
    from kiroshi.storage import DiskConfig, inject_roots

    disks = [DiskConfig(id="d1", read="//nas/disk1_direct/data",
                        write="//nas/disk1/data"),
             DiskConfig(id="d2", read="//nas/disk2_direct/data",
                        write="//nas/disk2/data")]
    gigs = [
        {"job_id": "shard_01/a", "disk": "d1", "spec": {"src_path": "shard_01/a.npz"}},
        {"job_id": "shard_09/b", "disk": "d2", "spec": {}},
        {"job_id": "x/c", "disk": None, "spec": {}},   # no disk -> inert
    ]
    inject_roots(gigs, disks)
    assert gigs[0]["spec"]["read_root"] == "//nas/disk1_direct/data"
    assert gigs[0]["spec"]["write_root"] == "//nas/disk1/data"
    assert gigs[1]["spec"]["read_root"] == "//nas/disk2_direct/data"
    assert "read_root" not in gigs[2]["spec"]           # inert for no-disk gig


def test_inject_roots_inert_without_topology():
    from kiroshi.storage import inject_roots

    gigs = [{"job_id": "a", "disk": None, "spec": {}}]
    inject_roots(gigs, [])  # no disks
    assert gigs[0]["spec"] == {}


def test_lease_injects_dual_path_roots(tmp_path):
    # end-to-end: /lease stamps the spec with the disk's read/write roots so the
    # task reads the direct share / writes the cached share.
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app
    from kiroshi.storage import DiskConfig

    disks = [DiskConfig(id="d1", kind="hdd", concurrency=4,
                        read="//nas/disk1_direct/data", write="//nas/disk1/data",
                        match="shard_01..04")]
    app = create_app(JobStore(str(tmp_path / "dp.db")), token="T", disks=disks)
    c = TestClient(app)
    H = {"Authorization": "Bearer T"}
    c.post("/seed", headers=H, json={"gigs": [
        {"job_id": "shard_01/a", "spec": {"src_path": "shard_01/a.npz"}}]})
    lease = c.post("/lease", headers=H, json={
        "runner_id": "r", "host": "h", "capacity": 4, "heartbeat_interval": 30}).json()
    g = lease["gigs"][0]
    assert g["spec"]["read_root"] == "//nas/disk1_direct/data"
    assert g["spec"]["write_root"] == "//nas/disk1/data"


def test_confined_join_refuses_escape_and_handles_unc(tmp_path):
    import pytest

    from kiroshi.paths import confined_join

    local = str(tmp_path).replace("\\", "/")
    joined = confined_join(str(tmp_path), "clips/a.npz").replace("\\", "/")
    assert joined == f"{local}/clips/a.npz"
    # UNC root -> backslash separators (Windows wants \\server\share\x)
    assert confined_join("//nas/disk1/data", "shard_01/a.npz") == \
        "\\\\nas\\disk1\\data\\shard_01\\a.npz"
    # absolute + traversal refused
    with pytest.raises(ValueError):
        confined_join(str(tmp_path), "/etc/passwd")
    with pytest.raises(ValueError):
        confined_join(str(tmp_path), "../../escape")
    with pytest.raises(ValueError):
        confined_join(str(tmp_path), "C:\\Windows\\evil")


# --------------------------------------------------- per-disk observability (N6)
def test_stats_includes_disk_inflight(tmp_path):
    store = JobStore(str(tmp_path / "obs.db"), max_retries=3)
    store.seed([
        {"job_id": "shard_01/a", "spec": {}, "disk": "d1"},
        {"job_id": "shard_01/b", "spec": {}, "disk": "d1"},
        {"job_id": "shard_09/c", "spec": {}, "disk": "d2"},
    ])
    store.lease("r", "h", 10, 60, disk_concurrency={"d1": 4, "d2": 4})
    st = store.stats()
    assert st["disk_inflight"]["d1"] == 2
    assert st["disk_inflight"]["d2"] == 1


def test_disk_done_counts(tmp_path):
    store = JobStore(str(tmp_path / "obs2.db"), max_retries=3)
    store.seed([
        {"job_id": "shard_01/a", "spec": {}, "disk": "d1"},
        {"job_id": "shard_09/b", "spec": {}, "disk": "d2"},
    ])
    store.lease("r", "h", 10, 60)
    store.complete([{"job_id": "shard_01/a", "status": "ok", "metrics": {}}])
    counts = store.disk_done_counts()
    assert counts == {"d1": 1}  # d2 still pending


def test_status_has_disk_budget_with_topology(tmp_path):
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app
    from kiroshi.storage import DiskConfig

    disks = [DiskConfig(id="d1", kind="hdd", concurrency=4, match="shard_01"),
             DiskConfig(id="d2", kind="nvme", concurrency=16, match="shard_09")]
    app = create_app(JobStore(str(tmp_path / "obs3.db")), token="T", disks=disks)
    c = TestClient(app)
    H = {"Authorization": "Bearer T"}
    st = c.get("/status", headers=H).json()
    assert st["disk_budget"] == {"d1": 4, "d2": 16}
    ids = {d["id"] for d in st["disk_info"]}
    assert ids == {"d1", "d2"}


def test_status_no_disk_budget_without_topology(tmp_path):
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app

    app = create_app(JobStore(str(tmp_path / "obs4.db")), token="T")
    c = TestClient(app)
    H = {"Authorization": "Bearer T"}
    st = c.get("/status", headers=H).json()
    assert "disk_budget" not in st  # inert


def test_storage_endpoint(tmp_path):
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app
    from kiroshi.storage import DiskConfig

    disks = [DiskConfig(id="d1", kind="hdd", read="//nas/d1_direct",
                        write="//nas/d1", match="shard_01")]
    app = create_app(JobStore(str(tmp_path / "obs5.db")), token="T", disks=disks)
    c = TestClient(app)
    H = {"Authorization": "Bearer T"}
    s = c.get("/storage", headers=H).json()
    assert len(s["disks"]) == 1
    assert s["disks"][0]["id"] == "d1"
    assert s["budget"] == {"d1": 4}
    # without topology -> empty
    app2 = create_app(JobStore(str(tmp_path / "obs6.db")), token="T")
    s2 = TestClient(app2).get("/storage", headers=H).json()
    assert s2 == {"disks": [], "budget": {}}

