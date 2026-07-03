"""Tests for inject_roots re-derive-on-lease — non-destructive config edits.

Regression: gigs seeded under a bad topology (match='' → disk=None) should
get their disk re-derived at lease time after the config is fixed, without
needing a DB wipe + re-seed. Already-tagged gigs must be untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.storage import DiskConfig, inject_roots, derive_disk  # noqa: E402


def _nvme_disks():
    return [DiskConfig(id="cache_nvme", kind="nvme", match="*",
                       read=r"\\nas\pool\reads", write=r"\\nas\pool\writes")]


def test_untagged_gig_gets_disk_and_roots():
    """Gig with disk=None + topology with matching rule → roots injected."""
    disks = _nvme_disks()
    gig = {"job_id": "shard_01/clip.npz", "disk": None,
           "spec": {"src_path": "shard_01/clip.npz"}}
    inject_roots([gig], disks)
    assert gig["disk"] == "cache_nvme"
    assert gig["spec"]["read_root"] == r"\\nas\pool\reads"
    assert gig["spec"]["write_root"] == r"\\nas\pool\writes"


def test_untagged_gig_with_no_match_stays_inert():
    """Gig with disk=None + no matching rule → left untouched (inert)."""
    disks = [DiskConfig(id="d1", match="shard_99")]   # won't match shard_01
    gig = {"job_id": "shard_01/clip.npz", "disk": None, "spec": {}}
    inject_roots([gig], disks)
    assert gig["disk"] is None
    assert "read_root" not in gig["spec"]
    assert "write_root" not in gig["spec"]


def test_already_tagged_gig_not_re_derived():
    """Already-tagged gig → derive_disk NOT called, roots injected from existing disk."""
    disks = _nvme_disks()
    gig = {"job_id": "shard_01/clip.npz", "disk": "cache_nvme",
           "spec": {"src_path": "shard_01/clip.npz"}}
    with patch("kiroshi.storage.derive_disk") as mock_dd:
        inject_roots([gig], disks)
        mock_dd.assert_not_called()
    assert gig["spec"]["read_root"] == r"\\nas\pool\reads"


def test_no_topology_inert():
    """No disks → nothing happens (same as before)."""
    gig = {"job_id": "x", "disk": None, "spec": {}}
    inject_roots([gig], [])
    assert gig["disk"] is None
    assert gig["spec"] == {}


def test_re_derive_only_fires_for_none_disk():
    """Gigs with a non-None but unknown disk id are NOT re-derived (corrupt DB edge)."""
    disks = _nvme_disks()
    gig = {"job_id": "shard_01/clip.npz", "disk": "ghost_disk", "spec": {}}
    with patch("kiroshi.storage.derive_disk") as mock_dd:
        inject_roots([gig], disks)
        mock_dd.assert_not_called()
    # disk stays as-is (not in by_id → no roots, but not re-derived either)
    assert gig["disk"] == "ghost_disk"


if __name__ == "__main__":
    tests = [n for n in dir(sys.modules[__name__]) if n.startswith("test_")]
    fail = 0
    for name in tests:
        try:
            globals()[name]()
            print(f"PASS  {name}")
        except Exception as exc:
            print(f"FAIL  {name}: {exc}")
            fail += 1
    print(f"\n{len(tests)-fail}/{len(tests)} passed")
    sys.exit(fail)
