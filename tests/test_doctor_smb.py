"""doctor's SMB-awareness: SMB roots are validated via kfs (not the OS redirector),
and a credential-less UNC root warns that the redirector path will fail over SSH.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
for _p in (SRC, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi import doctor  # noqa: E402
from kiroshi import kfs  # noqa: E402


def test_smb_read_root_passes_via_kfs(monkeypatch):
    monkeypatch.setattr(kfs, "use_smb", lambda p: True)
    monkeypatch.setattr(kfs, "exists", lambda p: True)
    monkeypatch.setattr(kfs, "creds_for", lambda s: ("kiroshi", "pw"))
    rep = doctor._Report()
    doctor._check_read_root(rep, "//nas/disk7_direct/data")
    assert not rep.failed
    assert not rep.warned  # SMB path used; no redirector warning


def test_smb_read_root_auth_failure_is_fail(monkeypatch):
    def _boom(_p):
        raise OSError("NT_STATUS_LOGON_FAILURE")

    monkeypatch.setattr(kfs, "use_smb", lambda p: True)
    monkeypatch.setattr(kfs, "exists", _boom)
    monkeypatch.setattr(kfs, "creds_for", lambda s: ("kiroshi", "pw"))
    rep = doctor._Report()
    doctor._check_read_root(rep, "//nas/disk7_direct/data")
    assert rep.failed


def test_unc_without_creds_warns_about_redirector(monkeypatch, capsys):
    # UNC root, but no SMB creds -> warn it will use the doomed OS redirector.
    monkeypatch.setattr(kfs, "use_smb", lambda p: False)
    rep = doctor._Report()
    doctor._check_read_root(rep, "//nas/disk7_direct/data")
    out = capsys.readouterr().out
    assert rep.warned
    assert "no SMB credentials" in out and "redirector" in out
