"""Tests for the mesh resource governor (resource.py + coordinator endpoints)
and the I/O watcher — the modules GLM added without any test coverage.

Verifies the two bugs found in review are fixed:
  1. IOWatcher actually records samples (device<->disk_id mapping works)
  2. The per-disk budget is UNIFIED: gigs + external resource slots draw from
     one shared counter, so their combined in-flight never exceeds the cap.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from kiroshi.coordinator import create_app
from kiroshi.jobstore import JobStore
from kiroshi.storage import (DiskConfig, has_parity, global_write_concurrency,
                             disk_concurrency_map)
from kiroshi.iowatcher import IOWatcher, DiskRollingStats, DiskSample


# --------------------------------------------------------------------------- #
#  storage helpers: parity gating
# --------------------------------------------------------------------------- #
def test_has_parity_gating():
    """Parity budget is inert for non-parity (NVMe/SSD) topologies."""
    nvme = [DiskConfig(id="cache", kind="nvme")]
    assert has_parity(nvme) is False
    assert global_write_concurrency(nvme) == 0

    parity = [DiskConfig(id="disk1", kind="hdd", parity_protected=True),
              DiskConfig(id="disk2", kind="hdd", parity_protected=True)]
    assert has_parity(parity) is True
    assert global_write_concurrency(parity) == 6  # default


def test_global_write_concurrency_takes_min():
    """The tightest write cap wins (the bottleneck spindle)."""
    disks = [DiskConfig(id="d1", parity_protected=True, write_concurrency=8),
             DiskConfig(id="d2", parity_protected=True, write_concurrency=4)]
    assert global_write_concurrency(disks) == 4


# --------------------------------------------------------------------------- #
#  IOWatcher: device<->disk_id mapping + sampling
# --------------------------------------------------------------------------- #
def test_iowatcher_rolling_stats():
    """Rolling stats compute MBps + util from cumulative samples."""
    s = DiskRollingStats("disk1")
    # two samples 1s apart: 2048 sectors read = 1 MiB, 50% util
    s.add(DiskSample(timestamp=100.0, read_sectors=0, write_sectors=0, io_ms=0))
    s.add(DiskSample(timestamp=101.0, read_sectors=200000, write_sectors=0, io_ms=500))
    st = s.stats()
    assert st["disk"] == "disk1"
    assert st["read_mbps"] > 100.0  # ~102 MB/s (200000*512/1e6)
    assert 49 < st["util_pct"] < 51  # 500ms / 1000ms = 50%


def test_iowatcher_inert_without_disks():
    """No HDD disks => watcher records nothing, snapshot is empty."""
    w = IOWatcher(disk_ids=[], is_parity={})
    snap = w.snapshot()
    assert snap["disks"] == []


def test_iowatcher_device_mapping(monkeypatch, tmp_path):
    """The disk_id<->device mapping resolves via /proc/mounts (the bug fix):
    a watcher for 'disk1' with direct_path /mnt/disk1 must find its device."""
    import kiroshi.iowatcher as iow

    # Fake /proc/mounts: /dev/sde1 mounted at /mnt/disk1
    mounts = "/dev/sde1 /mnt/disk1 xfs rw 0 0\n/dev/sdf1 /mnt/disk2 xfs rw 0 0\n"
    diskstats = ("   8  64 sde 100 0 4096 50 0 0 0 0 0 500 50 0 0 0 0\n"
                 "   8  80 sdf 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n")

    orig_open = open

    def fake_open(path, *a, **k):
        if path == "/proc/mounts":
            import io
            return io.StringIO(mounts)
        if path == "/proc/diskstats":
            import io
            return io.StringIO(diskstats)
        return orig_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(iow.sys, "platform", "linux")

    w = IOWatcher(disk_ids=["disk1", "disk2"],
                  is_parity={"disk1": True, "disk2": True},
                  direct_paths={"disk1": "/mnt/disk1", "disk2": "/mnt/disk2"})
    # device resolution: disk1 -> sde, disk2 -> sdf
    assert w._dev_for_disk == {"disk1": "sde", "disk2": "sdf"}
    # sampling records under disk_id (not device name)
    w._sample_linux()
    assert len(w._stats["disk1"].samples) == 1
    assert w._stats["disk1"].samples[0].read_sectors == 4096


# --------------------------------------------------------------------------- #
#  Resource governor: acquire/release + budget unification
# --------------------------------------------------------------------------- #
@pytest.fixture
def app_with_parity(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    disks = [
        DiskConfig(id="disk1", kind="hdd", match="shard_01", concurrency=2,
                   parity_protected=True, write_concurrency=3),
        DiskConfig(id="disk2", kind="hdd", match="shard_02", concurrency=2,
                   parity_protected=True),
    ]
    app = create_app(store, disks=disks, token=None)
    return app, store


def test_resource_write_budget_global(app_with_parity):
    """Global parity-write budget: at most write_concurrency writes at once."""
    app, _ = app_with_parity
    client = TestClient(app)
    granted = []
    for i in range(5):
        r = client.post("/resource/acquire",
                        json={"slot_id": f"w{i}", "mode": "write", "ttl": 60})
        granted.append(r.json().get("granted", False))
    # write_concurrency = min(3, 6) = 3 -> first 3 granted, rest 503
    assert sum(granted) == 3
    # release one -> next acquire succeeds
    client.post("/resource/release", json={"slot_id": "w0"})
    r = client.post("/resource/acquire",
                    json={"slot_id": "w5", "mode": "write", "ttl": 60})
    assert r.json()["granted"] is True


def test_resource_read_budget_per_disk(app_with_parity):
    """Per-disk read budget: at most `concurrency` reads per disk."""
    app, _ = app_with_parity
    client = TestClient(app)
    # disk1 budget = 2
    g = [client.post("/resource/acquire",
                     json={"slot_id": f"r{i}", "disk": "disk1", "mode": "read", "ttl": 60})
         .json().get("granted", False) for i in range(4)]
    assert sum(g) == 2  # only 2 read slots on disk1


def test_budget_unified_gigs_plus_slots(app_with_parity):
    """THE fix: gigs + external read slots share one per-disk budget.

    disk1 budget = 2. Hold 1 external read slot -> a gig lease can take at most
    1 (not 2), so combined in-flight never exceeds the cap."""
    app, store = app_with_parity
    client = TestClient(app)
    # seed 3 pending gigs on disk1 (disk stamped directly)
    store.seed([{"job_id": f"shard_01/clip{i}", "spec": {}, "disk": "disk1"}
                for i in range(3)])

    # hold 1 external read slot on disk1
    r = client.post("/resource/acquire",
                    json={"slot_id": "ext1", "disk": "disk1", "mode": "read", "ttl": 60})
    assert r.json()["granted"] is True

    # now a runner leases — effective budget = 2 - 1 = 1, so it gets 1 gig
    lease = client.post("/lease", json={"runner_id": "R1", "host": "h1",
                                        "capacity": 10}).json()
    assert len(lease.get("gigs", [])) == 1, \
        "gig lease must respect external read slots (unified budget)"


def test_resource_inert_no_parity(tmp_path):
    """No parity topology => write acquire always granted (HW-config-gated)."""
    store = JobStore(str(tmp_path / "j.db"))
    disks = [DiskConfig(id="cache", kind="nvme")]  # no parity
    app = create_app(store, disks=disks, token=None)
    client = TestClient(app)
    g = [client.post("/resource/acquire",
                     json={"slot_id": f"w{i}", "mode": "write", "ttl": 60})
         .json()["granted"] for i in range(20)]
    assert all(g)  # NVMe: no write budget, all granted


def test_resource_renew_extends_slot(app_with_parity):
    """A held slot can be renewed (heartbeat) so a long op isn't reaped."""
    app, _ = app_with_parity
    client = TestClient(app)
    client.post("/resource/acquire", json={"slot_id": "w1", "mode": "write", "ttl": 60})
    r = client.post("/resource/renew", json={"slot_id": "w1", "mode": "write", "ttl": 120})
    assert r.json()["renewed"] is True
    # renewing an unknown slot returns 404
    r2 = client.post("/resource/renew", json={"slot_id": "nope", "ttl": 60})
    assert r2.status_code == 404


def test_resource_slot_ttl_reap(app_with_parity):
    """Expired slots are reaped so a crashed holder doesn't pin the budget."""
    app, _ = app_with_parity
    client = TestClient(app)
    # acquire all 3 write slots with a tiny TTL
    for i in range(3):
        client.post("/resource/acquire",
                    json={"slot_id": f"w{i}", "mode": "write", "ttl": 0.01})
    time.sleep(0.05)
    # next acquire triggers reap of the expired slots -> granted
    r = client.post("/resource/acquire",
                    json={"slot_id": "w9", "mode": "write", "ttl": 60})
    assert r.json()["granted"] is True


def test_resource_status_endpoint(app_with_parity):
    app, _ = app_with_parity
    client = TestClient(app)
    client.post("/resource/acquire", json={"slot_id": "w1", "mode": "write", "ttl": 60})
    st = client.get("/resource/status").json()
    assert st["has_parity"] is True
    assert st["global_write_budget"] == 3
    assert st["global_write_inflight"] == 1
