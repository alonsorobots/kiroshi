"""Tests for `kiroshi nas` — assess + benchmark (PLAN §7.6, M8 N4).

``assess`` is tested with a synthetic shard layout on the local filesystem
(covering balance, skew verdict, and topology match-coverage). ``benchmark`` is
tested with a local disk entry (the sweep + knee-finding logic on a real file).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi.nascli import (  # noqa: E402
    _expand_probe_pattern,
    assess_layout,
    benchmark_disks,
    emit_shard_config,
    execute_shard,
    plan_shard,
)
from kiroshi.storage import DiskConfig  # noqa: E402


# --------------------------------------------------------------- assess
def _make_layout(tmp_path, shards):
    """Create a synthetic shard layout: {shard_name: [(file, size_bytes), ...]}."""
    for shard, files in shards.items():
        d = tmp_path / shard
        d.mkdir(parents=True, exist_ok=True)
        for fname, size in files:
            (d / fname).write_bytes(b"\0" * size)


def test_assess_balanced(tmp_path):
    _make_layout(tmp_path, {
        "shard_01": [("a.bin", 1000), ("b.bin", 1000)],
        "shard_02": [("a.bin", 1000), ("b.bin", 1000)],
    })
    r = assess_layout(str(tmp_path), depth=1)
    assert r["total_files"] == 4
    assert r["total_bytes"] == 4000
    assert r["verdict"] == "well-balanced"
    assert set(r["shards"]) == {"shard_01", "shard_02"}
    assert r["readiness"]["ok"] is True


def test_assess_skewed(tmp_path):
    _make_layout(tmp_path, {
        "shard_01": [("big.bin", 10000)],
        "shard_02": [("tiny.bin", 100)],
    })
    r = assess_layout(str(tmp_path), depth=1)
    assert r["verdict"] == "skewed"
    assert r["skew_ratio"] > 10.0
    # skew > 3:1 -> readiness flags a balance issue
    assert r["readiness"]["ok"] is False
    assert any("skew" in i for i in r["readiness"]["issues"])


def test_assess_empty(tmp_path):
    r = assess_layout(str(tmp_path), depth=1)
    assert r["total_files"] == 0
    assert r["verdict"] == "empty"
    assert r["readiness"]["ok"] is False


def test_assess_pattern_filters_and_flags_format(tmp_path):
    _make_layout(tmp_path, {
        "shard_01": [("a.npz", 500), ("junk.txt", 100)],
    })
    # pattern matches only .npz -> 1 file counted, 2 total -> format check flags <90%
    r = assess_layout(str(tmp_path), depth=1, pattern="*.npz")
    assert r["total_files"] == 1   # only the .npz
    assert r["total_all_files"] == 2
    fmt = next(c for c in r["readiness"]["checks"] if c["name"] == "format")
    assert fmt["ok"] is False  # 50% match -> flagged
    # without a pattern, all files counted
    r2 = assess_layout(str(tmp_path), depth=1)
    assert r2["total_files"] == 2


def test_assess_pattern_no_match(tmp_path):
    _make_layout(tmp_path, {"shard_01": [("a.npz", 500)]})
    r = assess_layout(str(tmp_path), depth=1, pattern="*.mp4")
    assert r["total_files"] == 0
    assert r["total_all_files"] == 1
    assert r["readiness"]["ok"] is False
    assert any("match pattern" in i for i in r["readiness"]["issues"])


def test_assess_shard_depth(tmp_path):
    # depth=2 groups by first TWO path components
    _make_layout(tmp_path, {
        "proj/shard_01": [("a.bin", 500)],
        "proj/shard_02": [("a.bin", 500)],
    })
    r = assess_layout(str(tmp_path), depth=2)
    assert set(r["shards"]) == {"proj/shard_01", "proj/shard_02"}


def test_assess_topology_coverage(tmp_path):
    _make_layout(tmp_path, {
        "shard_01": [("a.bin", 500)],
        "shard_02": [("a.bin", 500)],
        "shard_09": [("a.bin", 500)],   # matches d2
        "orphan": [("a.bin", 500)],      # matches no disk
    })
    disks = [DiskConfig(id="d1", match="shard_01..08"),
             DiskConfig(id="d2", match="shard_09..16")]
    r = assess_layout(str(tmp_path), depth=1, disks=disks)
    assert r["disk_coverage"] == {"d1": 2, "d2": 1}
    assert r["unmatched_shards"] == ["orphan"]
    # per-disk byte distribution
    assert r["disk_distribution"]["d1"]["bytes"] == 1000
    assert r["disk_distribution"]["d2"]["bytes"] == 500
    # orphan shard -> coverage issue flagged
    cov = next(c for c in r["readiness"]["checks"] if c["name"] == "coverage")
    assert cov["ok"] is False


def test_assess_concentration_on_one_disk(tmp_path):
    # 90% of data on disk1, 10% on disk2 -> distribution check flags it
    _make_layout(tmp_path, {
        "shard_01": [("huge.bin", 9000)],
        "shard_09": [("small.bin", 1000)],
    })
    disks = [DiskConfig(id="d1", match="shard_01..08"),
             DiskConfig(id="d2", match="shard_09..16")]
    r = assess_layout(str(tmp_path), depth=1, disks=disks)
    dist = next(c for c in r["readiness"]["checks"] if c["name"] == "distribution")
    assert dist["ok"] is False  # d1 holds 90% -> bottleneck
    assert r["readiness"]["ok"] is False


def test_assess_all_on_one_disk(tmp_path):
    _make_layout(tmp_path, {
        "shard_01": [("a.bin", 500)],
        "shard_02": [("a.bin", 500)],
    })
    disks = [DiskConfig(id="d1", match="shard_01..08"),
             DiskConfig(id="d2", match="shard_09..16")]  # d2 has no data
    r = assess_layout(str(tmp_path), depth=1, disks=disks)
    dist = next(c for c in r["readiness"]["checks"] if c["name"] == "distribution")
    assert dist["ok"] is False  # only 1 of 2 disks has data
    assert any("single disk" in i for i in r["readiness"]["issues"])


def test_assess_ready_when_well_distributed(tmp_path):
    _make_layout(tmp_path, {
        "shard_01": [("a.bin", 1000)],
        "shard_02": [("a.bin", 1000)],
        "shard_09": [("a.bin", 1000)],
        "shard_10": [("a.bin", 1000)],
    })
    disks = [DiskConfig(id="d1", match="shard_01..08"),
             DiskConfig(id="d2", match="shard_09..16")]
    r = assess_layout(str(tmp_path), depth=1, disks=disks)
    # balanced, 2 disks each 50%, all shards matched -> READY
    assert r["readiness"]["ok"] is True
    assert len(r["readiness"]["issues"]) == 0


# --------------------------------------------------------------- benchmark
def test_benchmark_local_disk(tmp_path):
    # A single local "disk" pointed at a temp dir: write + read + sweep.
    # Use a small file + short sweep so the test is fast.
    d = tmp_path / "disk1"
    d.mkdir()
    disks = [DiskConfig(id="d1", kind="ssd", read=str(d), write=str(d), match="x")]
    reports = benchmark_disks(disks, size_mb=2,
                              levels=(1, 2, 4), seconds=0.5)
    assert len(reports) == 1
    r = reports[0]
    assert r["disk_id"] == "d1"
    assert r["best_concurrency"] is not None
    assert r["peak_mbs"] > 0
    assert len(r["results"]) == 3
    # temp file cleaned up
    assert not (d / "__kiroshi_bench_d1.bin").exists()


def test_benchmark_skips_disk_without_paths():
    disks = [DiskConfig(id="d1", match="x")]  # no read/write
    reports = benchmark_disks(disks, size_mb=1, levels=(1,), seconds=0.1)
    assert len(reports) == 0  # skipped, not errored


# --------------------------------------------------------------- shard plan
def test_plan_shard_balances_by_size():
    # largest-first greedy: the 9000-byte file and the 1000-byte file should end
    # up on different bins (not both on bin 0).
    files = [("a.bin", 9000), ("b.bin", 1000), ("c.bin", 1000), ("d.bin", 1000)]
    bins = plan_shard(files, n_disks=2)
    sizes = [sum(s for _, s in b) for b in bins]
    assert sizes[0] + sizes[1] == 12000
    # the 9000 goes to bin 0, the three 1000s go to bin 1 -> 9000 vs 3000
    # (greedy: 9000->bin0, 1000->bin1, 1000->bin1(2000), 1000->bin1(3000))
    assert 9000 in sizes and 3000 in sizes


def test_plan_shard_empty():
    assert plan_shard([], n_disks=3) == [[], [], []]


def test_execute_shard_moves_files(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    (src / "a.bin").write_bytes(b"\0" * 8000)
    (src / "b.bin").write_bytes(b"\0" * 2000)
    (src / "c.bin").write_bytes(b"\0" * 2000)
    files = [("a.bin", 8000), ("b.bin", 2000), ("c.bin", 2000)]
    bins = plan_shard(files, n_disks=2)
    result = execute_shard(str(src), bins)
    assert result["moved"] == 3
    assert result["errors"] == 0
    # files should now be under shard_01/ and shard_02/
    assert (src / "shard_01" / "a.bin").exists()
    assert (src / "shard_02" / "b.bin").exists()
    # originals gone
    assert not (src / "a.bin").exists()


def test_execute_shard_dry_run(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    (src / "a.bin").write_bytes(b"\0" * 100)
    bins = plan_shard([("a.bin", 100)], n_disks=1)
    result = execute_shard(str(src), bins, dry_run=True)
    assert result["moved"] == 1  # counted as "would move"
    assert (src / "a.bin").exists()  # but NOT actually moved


def test_emit_shard_config():
    cfg = emit_shard_config(2, kind="hdd")
    assert 'id = "disk1"' in cfg
    assert 'id = "disk2"' in cfg
    assert 'match = "shard_01"' in cfg
    assert 'match = "shard_02"' in cfg
    assert 'kind = "hdd"' in cfg
    # with templates
    cfg2 = emit_shard_config(2, read_tmpl="//nas/disk{n}/data",
                             write_tmpl="//nas/disk{n}/cache")
    assert 'read = "//nas/disk1/data"' in cfg2
    assert 'write = "//nas/disk2/cache"' in cfg2


def test_shard_then_assess_ready(tmp_path):
    # full round-trip: shard a flat dataset, then assess it with the emitted topology
    src = tmp_path / "dataset"
    src.mkdir()
    for i in range(8):
        (src / f"clip_{i}.npz").write_bytes(b"\0" * (1000 + i * 100))

    from kiroshi.nascli import _collect_files

    files = _collect_files(str(src))
    bins = plan_shard(files, n_disks=2)
    execute_shard(str(src), bins)
    # now assess with a matching topology
    disks = [DiskConfig(id="disk1", kind="hdd", match="shard_01"),
             DiskConfig(id="disk2", kind="hdd", match="shard_02")]
    report = assess_layout(str(src), depth=1, pattern="*.npz", disks=disks)
    assert report["total_files"] == 8
    assert report["readiness"]["ok"] is True  # balanced, all matched, format ok


# --------------------------------------------------------------- probe pattern
def test_expand_probe_pattern_range():
    assert _expand_probe_pattern("disk{1..4}", 7) == ["disk1", "disk2", "disk3", "disk4"]
    assert _expand_probe_pattern("vol{1..3}", 7) == ["vol1", "vol2", "vol3"]


def test_expand_probe_pattern_no_braces():
    assert _expand_probe_pattern("disk1", 7) == ["disk1"]
