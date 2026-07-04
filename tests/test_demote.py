"""Unit tests for kiroshi.demote — glob expansion, stable assignment, gigs, run."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi import demote


def test_expand_lubu_glob_maps_union_to_direct_disk():
    assert (demote.expand_lubu_glob("/mnt/user/Lubu*/MonologDataset")
            == "/mnt/disk{n}/Lubu{n}/MonologDataset")


def test_expand_lubu_glob_passthrough_template():
    tmpl = "/mnt/disk{n}/Lubu{n}/X"
    assert demote.expand_lubu_glob(tmpl) == tmpl


def test_expand_lubu_glob_rejects_no_shard_component():
    try:
        demote.expand_lubu_glob("/mnt/user/Dataset")
    except ValueError:
        return
    raise AssertionError("expected ValueError for glob with no '*' or '{n}'")


def test_disk_dest_root_formats_number():
    assert (demote.disk_dest_root("/mnt/disk{n}/Lubu{n}/X", 3)
            == "/mnt/disk3/Lubu3/X")


def test_assign_disk_is_stable_and_in_range():
    for rel in ("a/b.npz", "clip_00042.npz", "x/y/z.bin"):
        k1 = demote.assign_disk(rel, 7)
        k2 = demote.assign_disk(rel, 7)
        assert k1 == k2
        assert 1 <= k1 <= 7
    # backslash vs forward slash normalize to the same disk
    assert demote.assign_disk("a\\b.npz", 7) == demote.assign_disk("a/b.npz", 7)


def test_assign_disk_spreads_across_all_spindles():
    seen = {demote.assign_disk(f"clip_{i:05d}.npz", 7) for i in range(2000)}
    assert seen == set(range(1, 8)), f"not all spindles used: {sorted(seen)}"


def test_enumerate_gigs_shapes_specs(tmp_path, monkeypatch):
    # Build a small tree and enumerate over it via kfs.walk.
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.npz").write_bytes(b"x")
    (src / "sub" / "b.npz").write_bytes(b"yy")
    (src / "skip.txt").write_bytes(b"z")

    gigs = list(demote.enumerate_gigs({
        "from": str(src), "to": "/mnt/user/Lubu*/Data",
        "n_disks": 7, "pattern": "*.npz",
    }))
    ids = {g["subjob_id"] for g in gigs}
    assert ids == {"a.npz", "sub/b.npz"}, ids
    for g in gigs:
        assert g["disk"].startswith("disk")
        spec = g["spec"]
        assert spec["direct_disk_write"] is True
        assert spec["read_root"] == str(src)
        k = int(g["disk"][4:])
        assert spec["write_root"] == f"/mnt/disk{k}/Lubu{k}/Data"


def test_run_copies_then_skips_idempotently(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "f.npz").write_bytes(b"hello")
    spec = {"src_path": "f.npz", "dst_path": "f.npz",
            "read_root": str(src), "write_root": str(dst),
            "direct_disk_write": True}
    r1 = demote.run(spec)
    assert r1["status"] == "ok"
    assert (dst / "f.npz").read_bytes() == b"hello"
    r2 = demote.run(spec)
    assert r2["status"] == "skipped"


if __name__ == "__main__":
    import tempfile
    fail = 0
    for name in sorted(n for n in dir() if n.startswith("test_")):
        fn = globals()[name]
        try:
            if "tmp_path" in fn.__code__.co_varnames:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d), None) if fn.__code__.co_argcount == 2 else fn(Path(d))
            else:
                fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {name}: {e}")
            fail += 1
    sys.exit(fail)
