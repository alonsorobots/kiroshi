"""nascred.rotate (atomic NAS password rotation) + kfs.smb_auth_probe.

No real SSH/SMB: subprocess and the smbclient session setup are mocked. The
point is to lock in the two pitfalls this feature was born from -- (1) the
password must reach smbpasswd as bytes with clean '\\n' (Windows text-mode
turns it into '\\r\\n' and sets a CR-tailed password that then fails), and
(2) rotate must NOT write the store unless the NAS provably accepts the new
password -- plus the auth-error classification the runner preflight gates on.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi import nascred  # noqa: E402
from kiroshi import kfs  # noqa: E402


class _R:
    def __init__(self, rc=0, out=b"", err=b""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _wire(monkeypatch, *, smbpasswd_rc=0, local_ok=True, store_ok=True):
    """Mock the ssh subprocess calls + the DPAPI store. Returns a dict capturing
    what was fed to smbpasswd so tests can assert the bytes/CRLF contract."""
    cap: dict = {}

    def fake_run(cmd, **kw):
        remote = cmd[-1]
        if "smbpasswd" in remote:
            cap["smbpasswd_input"] = kw.get("input")
            return _R(rc=smbpasswd_rc)
        # smbclient -L local verify
        out = b"\n\tSharename       Type\n" if local_ok else b"session setup failed: NT_STATUS_LOGON_FAILURE\n"
        return _R(rc=0, out=out)

    monkeypatch.setattr(nascred.subprocess, "run", fake_run)
    stored: dict = {}
    monkeypatch.setattr(nascred, "set_secret",
                        lambda u, pw, server="default": stored.update(u=u, pw=pw) or "C:/store")
    monkeypatch.setattr(nascred, "load_secret",
                        lambda server="default": (stored.get("u"), stored.get("pw")) if store_ok else ("x", "y"))
    return cap


def test_rotate_happy_and_bytes_no_crlf(monkeypatch):
    cap = _wire(monkeypatch)
    info = nascred.rotate("kiroshi", ssh_target="nas")
    assert info["user"] == "kiroshi" and info["pw_len"] == 32
    stdin = cap["smbpasswd_input"]
    assert isinstance(stdin, (bytes, bytearray)), "password must be piped as BYTES"
    assert b"\r" not in stdin, "no CR -- Windows text mode would break the password"
    assert stdin.count(b"\n") == 2 and stdin.endswith(b"\n")  # new + retype


def test_rotate_aborts_if_smbpasswd_fails(monkeypatch):
    _wire(monkeypatch, smbpasswd_rc=1)
    with pytest.raises(RuntimeError, match="smbpasswd failed"):
        nascred.rotate("kiroshi", ssh_target="nas")


def test_rotate_aborts_if_nas_rejects_new_pw(monkeypatch):
    # smbpasswd returns 0 but the local auth check fails -> must NOT store.
    _wire(monkeypatch, local_ok=False)
    with pytest.raises(RuntimeError, match="rejected the freshly-set password"):
        nascred.rotate("kiroshi", ssh_target="nas")


def test_rotate_flags_store_desync(monkeypatch):
    _wire(monkeypatch, store_ok=False)
    with pytest.raises(RuntimeError, match="OUT OF SYNC"):
        nascred.rotate("kiroshi", ssh_target="nas")


# ---------------------------------------------------------------- auth probe
def _probe_with(monkeypatch, raiser):
    monkeypatch.setattr(kfs, "have_creds", lambda s: True)
    monkeypatch.setattr(kfs, "_smbclient", lambda: type("SC", (), {"delete_session": staticmethod(lambda s: None)})())
    monkeypatch.setattr(kfs, "_ensure_session", raiser)
    return kfs.smb_auth_probe("192.0.2.1")


def test_probe_none_when_no_creds(monkeypatch):
    monkeypatch.setattr(kfs, "have_creds", lambda s: False)
    assert kfs.smb_auth_probe("192.0.2.1") is None


def test_probe_ok(monkeypatch):
    assert _probe_with(monkeypatch, lambda s: None) is None


def test_probe_auth_rejected(monkeypatch):
    def boom(s):
        raise Exception("STATUS_LOGON_FAILURE ... 0xc000006d")
    assert _probe_with(monkeypatch, boom) == "auth_rejected"


def test_probe_access_denied(monkeypatch):
    def boom(s):
        raise Exception("STATUS_ACCESS_DENIED")
    assert _probe_with(monkeypatch, boom) == "access_denied"


def test_probe_unreachable(monkeypatch):
    def boom(s):
        raise OSError("connection refused")
    assert _probe_with(monkeypatch, boom) == "unreachable"
