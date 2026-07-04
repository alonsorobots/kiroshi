"""Tests for storage topology validation — catches misconfigured disk match rules
at coordinator boot instead of at runtime.

Key regression: a single-pool topology with ``match=""`` routes nothing (every
gig gets ``disk=None``, fails with ``KIROSHI_READ_ROOT is not set``). The coordinator
should warn at startup.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.storage import DiskConfig, validate_disks  # noqa: E402


def test_single_disk_empty_match_warns():
    """The exact regression: single NVMe pool with match='' routes nothing."""
    disks = [DiskConfig(id="cache_nvme", kind="nvme", match="")]
    warns = validate_disks(disks)
    assert len(warns) == 1
    assert "match='*'" in warns[0]
    assert "routes NOTHING" in warns[0]


def test_single_disk_star_match_clean():
    """match='*' is the correct wildcard — no warning."""
    disks = [DiskConfig(id="cache_nvme", kind="nvme", match="*")]
    assert validate_disks(disks) == []


def test_multi_disk_one_empty_is_inert_warning():
    """In a multi-disk topology, an empty match is intentional inert — warn gently."""
    disks = [
        DiskConfig(id="disk1", match="shard_01"),
        DiskConfig(id="placeholder", match=""),
    ]
    warns = validate_disks(disks)
    assert len(warns) == 1
    assert "inert" in warns[0]


def test_all_good_topology_clean():
    disks = [
        DiskConfig(id="disk1", match="shard_01..03"),
        DiskConfig(id="disk2", match="shard_04..07"),
    ]
    assert validate_disks(disks) == []


def test_whitespace_only_match_treated_as_empty():
    disks = [DiskConfig(id="d1", match="   ")]
    warns = validate_disks(disks)
    assert len(warns) == 1


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
