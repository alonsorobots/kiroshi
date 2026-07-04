"""Tests for kiroshi.iohint — path-aware, static storage-class guidance.

These are pure/unit: no network, no filesystem. They pin the guidance an agent
sees at job-creation time against a synthetic topology (HDD parity array with a
direct spindle share + an NVMe scratch disk).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi import iohint  # noqa: E402
from kiroshi.storage import DiskConfig  # noqa: E402


def _topo() -> list[DiskConfig]:
    return [
        DiskConfig(
            id="disk1", kind="hdd", match="shard_01..08",
            read="//nas/disk1_direct/data", write="//nas/user/data",
            direct_path="/mnt/disk1", parity_protected=True, cache_tier="nvme",
        ),
        DiskConfig(
            id="scratch", kind="nvme", match="reduced_*",
            read="//nas/nvme/reduced", write="//nas/nvme/reduced",
        ),
    ]


def _codes(adv) -> set[str]:
    return {f.code for f in adv.findings}


def _by_code(adv, code):
    return next(f for f in adv.findings if f.code == code)


# --------------------------------------------------------------------- inputs
def test_hdd_input_warns_and_suggests_sharding():
    adv = iohint.advise_job(sample_src="shard_03/clip.npz", disks=_topo())
    assert "input.hdd" in _codes(adv)
    assert adv.severity == "warn"
    msg = _by_code(adv, "input.hdd").message.lower()
    assert "hdd" in msg and "shard" in msg


def test_nvme_input_is_confirmed_ok_not_warned():
    adv = iohint.advise_job(read_root="//nas/nvme/reduced",
                            sample_src="reduced_88dof/clip.npz", disks=_topo())
    assert "input.nvme" in _codes(adv)
    assert _by_code(adv, "input.nvme").level == "ok"
    assert "input.hdd" not in _codes(adv)


def test_reading_cached_share_when_direct_exists_warns():
    # read_root points at the FUSE/user share; disk1 has a faster direct share.
    adv = iohint.advise_job(read_root="/mnt/user/data",
                            sample_src="shard_02/clip.npz", disks=_topo())
    assert "read.not_direct" in _codes(adv)
    assert "disk1_direct" in _by_code(adv, "read.not_direct").message


def test_direct_share_read_root_does_not_warn_not_direct():
    adv = iohint.advise_job(read_root="//nas/disk1_direct/data",
                            sample_src="shard_02/clip.npz", disks=_topo())
    assert "read.not_direct" not in _codes(adv)


# --------------------------------------------------------------------- outputs
def test_parity_output_warns():
    adv = iohint.advise_job(write_root="//nas/user/data",
                            sample_dst="shard_04/out.npz", disks=_topo())
    f = _by_code(adv, "output.parity")
    assert f.level == "warn"
    assert "parity" in f.message.lower()
    assert "nvme" in f.message.lower()  # cache-tier hint surfaced


def test_nvme_output_is_ok():
    adv = iohint.advise_job(write_root="//nas/nvme/reduced",
                            sample_dst="reduced_88dof/out.npz", disks=_topo())
    assert _by_code(adv, "output.nvme").level == "ok"


def test_direct_disk_write_is_dangerous(monkeypatch):
    monkeypatch.setenv("KIROSHI_NAS_USER", "u")
    monkeypatch.setenv("KIROSHI_NAS_PASS", "p")
    # Writing to the raw /mnt/disk1 device path bypasses the pooled share.
    adv = iohint.advise_job(write_root="/mnt/disk1/out",
                            sample_dst="shard_02/out.npz", disks=_topo())
    f = _by_code(adv, "output.direct_disk_write")
    assert "LOSE DATA" in f.message
    res = iohint.gate(adv, acks=None)
    assert res.blocked and "direct_disk_write" in res.tokens()


def test_direct_read_share_as_write_target_is_dangerous(monkeypatch):
    monkeypatch.setenv("KIROSHI_NAS_USER", "u")
    monkeypatch.setenv("KIROSHI_NAS_PASS", "p")
    # Writing to the direct per-spindle READ share (cached write share differs).
    adv = iohint.advise_job(write_root="//nas/disk1_direct/data",
                            sample_dst="shard_02/out.npz", disks=_topo())
    assert "output.direct_disk_write" in _codes(adv)


def test_cached_write_share_is_safe(monkeypatch):
    monkeypatch.setenv("KIROSHI_NAS_USER", "u")
    monkeypatch.setenv("KIROSHI_NAS_PASS", "p")
    # Writing to the cached user share is the SAFE recommended path (only the
    # parity warning applies, not the direct-disk danger).
    adv = iohint.advise_job(write_root="//nas/user/data",
                            sample_dst="shard_02/out.npz", disks=_topo())
    assert "output.direct_disk_write" not in _codes(adv)


def test_nvme_scratch_write_not_flagged_direct(monkeypatch):
    monkeypatch.setenv("KIROSHI_NAS_USER", "u")
    monkeypatch.setenv("KIROSHI_NAS_PASS", "p")
    # NVMe scratch has read==write and no direct_path -> not a direct-disk write.
    adv = iohint.advise_job(write_root="//nas/nvme/reduced",
                            sample_dst="reduced_a/out.npz", disks=_topo())
    assert "output.direct_disk_write" not in _codes(adv)


# --------------------------------------------------------------------- transport
def test_unc_without_creds_warns(monkeypatch):
    monkeypatch.delenv("KIROSHI_NAS_USER", raising=False)
    monkeypatch.delenv("KIROSHI_NAS_PASS", raising=False)
    adv = iohint.advise_job(read_root="//nas/disk1_direct/data", disks=_topo())
    assert "smb.no_creds" in _codes(adv)


def test_creds_present_no_smb_warning(monkeypatch):
    monkeypatch.setenv("KIROSHI_NAS_USER", "u")
    monkeypatch.setenv("KIROSHI_NAS_PASS", "p")
    adv = iohint.advise_job(read_root="//nas/disk1_direct/data", disks=_topo())
    assert "smb.no_creds" not in _codes(adv)


def test_brokered_server_is_ok_not_blocked(monkeypatch):
    # No local creds, but the coordinator can broker for this server: the
    # Runners get the direct smbprotocol plane at startup, so it's the fast path,
    # not a slow-path block. This is what lets a seed from an uncredentialed /
    # headless shell proceed without a manual --io-ack.
    monkeypatch.delenv("KIROSHI_NAS_USER", raising=False)
    monkeypatch.delenv("KIROSHI_NAS_PASS", raising=False)
    adv = iohint.advise_job(read_root="//nas/nvme/reduced",
                            write_root="//nas/nvme/reduced",
                            sample_src="reduced_a/clip.npz", disks=_topo(),
                            broker_servers={"nas"})
    assert "smb.brokered" in _codes(adv)
    assert "smb.no_creds" not in _codes(adv)
    assert iohint.gate(adv, acks=None).blocked is False


def test_broker_only_for_named_server(monkeypatch):
    # Brokering another server does NOT clear the block for this one.
    monkeypatch.delenv("KIROSHI_NAS_USER", raising=False)
    monkeypatch.delenv("KIROSHI_NAS_PASS", raising=False)
    adv = iohint.advise_job(read_root="//nas/nvme/reduced",
                            sample_src="reduced_a/clip.npz", disks=_topo(),
                            broker_servers={"someotherhost"})
    assert "smb.no_creds" in _codes(adv)


# --------------------------------------------------------------------- edges
def test_no_topology_says_so():
    adv = iohint.advise_job(read_root="//nas/x/y", disks=[])
    assert "topology.none" in _codes(adv)


def test_nvme_with_creds_is_clean(monkeypatch):
    # NVMe in/out + SMB creds set: the fully-fast path, zero warnings.
    monkeypatch.setenv("KIROSHI_NAS_USER", "u")
    monkeypatch.setenv("KIROSHI_NAS_PASS", "p")
    adv = iohint.advise_job(read_root="//nas/nvme/reduced",
                            write_root="//nas/nvme/reduced",
                            sample_src="reduced_a/clip.npz",
                            sample_dst="reduced_a/out.npz",
                            disks=_topo())
    assert adv.severity == "ok"


def test_classify_root_reports_facts():
    facts = iohint.classify_root("/mnt/user/data", disks=_topo(),
                                 sample="shard_02/clip.npz")
    assert facts["kind"] == "hdd"
    assert facts["parity"] is True
    assert facts["direct_available"] is True


# ------------------------------------------------- doctor preflight integration
# --------------------------------------------------------------- fail-closed gate
def test_gate_blocks_parity_write():
    adv = iohint.advise_job(write_root="//nas/user/data",
                            sample_dst="shard_04/out.npz", disks=_topo())
    res = iohint.gate(adv, acks=None)
    assert res.blocked is True
    assert "parity_write" in res.tokens()


def test_gate_cleared_by_ack(monkeypatch):
    monkeypatch.setenv("KIROSHI_NAS_USER", "u")
    monkeypatch.setenv("KIROSHI_NAS_PASS", "p")
    adv = iohint.advise_job(write_root="//nas/user/data",
                            sample_dst="shard_04/out.npz", disks=_topo())
    res = iohint.gate(adv, acks={"parity_write"})
    assert res.blocked is False
    assert "parity_write" in res.acknowledged


def test_gate_blocks_non_direct_read():
    adv = iohint.advise_job(read_root="/mnt/user/data",
                            sample_src="shard_02/clip.npz", disks=_topo())
    res = iohint.gate(adv, acks=None)
    assert res.blocked is True
    assert "no_direct_share" in res.tokens()


def test_gate_blocks_unclassified_nas(monkeypatch):
    monkeypatch.setenv("KIROSHI_NAS_USER", "u")
    monkeypatch.setenv("KIROSHI_NAS_PASS", "p")
    adv = iohint.advise_job(read_root="//nas/random_share/data",
                            sample_src="clip.npz", disks=_topo())
    res = iohint.gate(adv, acks=None)
    assert res.blocked is True
    assert "unclassified_nas" in res.tokens()


def test_gate_blocks_missing_smb_creds(monkeypatch):
    monkeypatch.delenv("KIROSHI_NAS_USER", raising=False)
    monkeypatch.delenv("KIROSHI_NAS_PASS", raising=False)
    adv = iohint.advise_job(read_root="//nas/nvme/reduced",
                            sample_src="reduced_a/clip.npz", disks=_topo())
    res = iohint.gate(adv, acks=None)
    assert res.blocked is True
    assert "no_smb_creds" in res.tokens()


def test_gate_passes_fast_path(monkeypatch):
    monkeypatch.setenv("KIROSHI_NAS_USER", "u")
    monkeypatch.setenv("KIROSHI_NAS_PASS", "p")
    adv = iohint.advise_job(read_root="//nas/nvme/reduced",
                            write_root="//nas/nvme/reduced",
                            sample_src="reduced_a/clip.npz",
                            sample_dst="reduced_a/out.npz", disks=_topo())
    assert iohint.gate(adv, acks=None).blocked is False


def test_gate_passes_local_and_no_topology():
    # No topology at all -> can't judge -> never block.
    adv = iohint.advise_job(read_root="C:/data", write_root="C:/out", disks=[])
    assert iohint.gate(adv, acks=None).blocked is False
    # Local path with a topology configured -> still no NAS trade-off -> pass.
    adv2 = iohint.advise_job(read_root="C:/data", sample_src="foo/clip.npz",
                             disks=_topo())
    assert iohint.gate(adv2, acks=None).blocked is False


def test_gate_kill_switch(monkeypatch):
    monkeypatch.setenv("KIROSHI_IO_GATE", "0")
    assert iohint.gate_enabled() is False
    monkeypatch.setenv("KIROSHI_IO_GATE", "1")
    assert iohint.gate_enabled() is True
    monkeypatch.delenv("KIROSHI_IO_GATE", raising=False)
    assert iohint.gate_enabled() is True  # on by default


def test_block_message_names_reason_and_token():
    adv = iohint.advise_job(write_root="//nas/user/data",
                            sample_dst="shard_04/out.npz", disks=_topo())
    msg = iohint.block_message(iohint.gate(adv, acks=None))
    assert "parity_write" in msg
    assert "--io-ack" in msg
    assert "KIROSHI_IO_GATE=0" in msg


def test_doctor_storage_class_check_emits_advice(monkeypatch, capsys):
    from kiroshi import doctor, storage

    monkeypatch.setattr(storage, "load_topology", _topo)
    rep = doctor._Report()
    doctor._check_storage_class(rep, read_root="/mnt/user/data",
                                write_root="//nas/user/data")
    out = capsys.readouterr().out
    assert "storage class" in out
    assert "parity" in out.lower()
    assert rep.warned is True  # HDD/parity/not-direct are actionable warnings
