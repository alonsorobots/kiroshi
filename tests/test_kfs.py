"""kfs: the logon-session-proof filesystem layer.

These cover the parts that don't need a live SMB server: UNC detection, server
parsing, credential resolution + routing, and the *local* branch of the os-like
API (including atomic-write crash safety). The SMB branch is proven live against
the NAS; here we assert the routing decision and that local I/O is unaffected.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
for _p in (SRC, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi import kfs  # noqa: E402


# ------------------------------------------------------------------- detection
def test_is_unc_and_server_of():
    assert kfs.is_unc(r"\\nas\share\x")
    assert kfs.is_unc("//nas/share/x")
    assert not kfs.is_unc(r"C:\data")
    assert not kfs.is_unc("/mnt/nas")
    assert kfs.server_of("//192.0.2.10/disk7_direct/a") == "192.0.2.10"
    assert kfs.server_of(r"\\nas\share\a") == "nas"
    assert kfs.server_of(r"C:\data") is None


# ----------------------------------------------------------------- credentials
def test_creds_from_env_global_and_per_server(monkeypatch):
    monkeypatch.delenv("KIROSHI_NAS_USER", raising=False)
    monkeypatch.delenv("KIROSHI_NAS_PASS", raising=False)
    assert kfs.creds_for("nas") == (None, None)

    monkeypatch.setenv("KIROSHI_NAS_USER", "kiroshi")
    monkeypatch.setenv("KIROSHI_NAS_PASS", "pw")
    assert kfs.creds_for("nas") == ("kiroshi", "pw")

    # per-server override wins and is sanitized (dots -> underscores, upper)
    monkeypatch.setenv("KIROSHI_NAS_USER_192_0_2_10", "svc")
    monkeypatch.setenv("KIROSHI_NAS_PASS_192_0_2_10", "pw2")
    assert kfs.creds_for("192.0.2.10") == ("svc", "pw2")


def test_use_smb_requires_unc_and_creds(monkeypatch):
    monkeypatch.delenv("KIROSHI_NAS_USER", raising=False)
    monkeypatch.delenv("KIROSHI_NAS_PASS", raising=False)
    # UNC but no creds -> fall back to OS redirector
    assert kfs.use_smb("//nas/share/x") is False
    assert kfs.backend("//nas/share/x") == "os"

    monkeypatch.setenv("KIROSHI_NAS_USER", "u")
    monkeypatch.setenv("KIROSHI_NAS_PASS", "p")
    assert kfs.use_smb("//nas/share/x") is True
    assert kfs.backend("//nas/share/x") == "smb"
    # local paths never use SMB, regardless of creds
    assert kfs.use_smb(r"C:\data\x") is False
    assert kfs.use_smb("/tmp/x") is False


# ------------------------------------------------------------- local os-like API
def test_local_roundtrip(tmp_path):
    d = tmp_path / "sub" / "deep"
    kfs.makedirs(str(d), exist_ok=True)
    assert os.path.isdir(d)
    f = d / "a.bin"
    with kfs.open(str(f), "wb") as fh:
        fh.write(b"hello")
    assert kfs.exists(str(f))
    with kfs.open(str(f), "rb") as fh:
        assert fh.read() == b"hello"
    # walk finds it
    found = [
        os.path.join(dp, fn)
        for dp, _dn, files in kfs.walk(str(tmp_path))
        for fn in files
    ]
    assert str(f) in found
    kfs.remove(str(f))
    assert not kfs.exists(str(f))


def test_local_atomic_write_promotes(tmp_path):
    dst = tmp_path / "out" / "clip.npz"
    with kfs.atomic_write(str(dst)) as fh:
        fh.write(b"payload")
    assert dst.read_bytes() == b"payload"
    # no leftover temp files in the dir
    assert [p.name for p in dst.parent.iterdir()] == ["clip.npz"]


def test_local_atomic_write_crash_leaves_no_partial(tmp_path):
    dst = tmp_path / "clip.npz"
    with pytest.raises(RuntimeError):
        with kfs.atomic_write(str(dst)) as fh:
            fh.write(b"partial")
            raise RuntimeError("boom mid-write")
    # target was never created, and the temp was cleaned up
    assert not dst.exists()
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------- commit retry / idempotent promote
def test_commit_with_retry_succeeds_after_transient_lock(monkeypatch):
    monkeypatch.setattr(kfs.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(1, "The process cannot access the file ... used by another process")

    out = kfs._commit_with_retry(flaky, lambda: False, lambda: None)
    assert out == "written" and calls["n"] == 3


def test_commit_with_retry_idempotent_when_other_writer_won(monkeypatch):
    monkeypatch.setattr(kfs.time, "sleep", lambda *_: None)
    cleaned = {"v": False}

    def always_locked():
        raise OSError(1, "file in use")

    # destination already exists (a concurrent worker produced it) -> success
    out = kfs._commit_with_retry(
        always_locked, lambda: True, lambda: cleaned.__setitem__("v", True)
    )
    assert out == "exists" and cleaned["v"] is True


def test_commit_with_retry_raises_after_exhaustion(monkeypatch):
    monkeypatch.setattr(kfs.time, "sleep", lambda *_: None)
    monkeypatch.setattr(kfs, "_COMMIT_ATTEMPTS", 3)

    def always_locked():
        raise OSError(13, "permission denied")

    with pytest.raises(OSError):
        kfs._commit_with_retry(always_locked, lambda: False, lambda: None)


def test_atomic_write_local_rides_out_transient_replace_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(kfs.time, "sleep", lambda *_: None)
    dst = tmp_path / "clip.npz"
    real_replace = os.replace
    state = {"n": 0}

    def flaky_replace(a, b):
        state["n"] += 1
        if state["n"] == 1:
            raise PermissionError("transient AV/indexer lock on the temp file")
        return real_replace(a, b)

    monkeypatch.setattr(kfs.os, "replace", flaky_replace)
    with kfs.atomic_write(str(dst)) as fh:
        fh.write(b"payload")
    assert dst.read_bytes() == b"payload"
    assert [p.name for p in dst.parent.iterdir()] == ["clip.npz"]  # temp cleaned
