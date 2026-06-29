"""Path-portability + preflight guards.

Covers the Windows footgun where a UNC root (``\\\\server\\share``) loses a
leading separator in transit (shells/env vars eat backslashes) and silently
becomes a *local* drive-relative path. ``kiroshi doctor`` must refuse that
rather than create a bogus local directory and report a false PASS.
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

from kiroshi import paths as kpaths  # noqa: E402
from kiroshi import doctor  # noqa: E402

_WIN = sys.platform == "win32"
winonly = pytest.mark.skipif(not _WIN, reason="UNC-mangling is Windows-only")


def test_looks_like_unc():
    assert kpaths.looks_like_unc(r"\\server\share\x")
    assert kpaths.looks_like_unc("//server/share/x")
    assert not kpaths.looks_like_unc(r"C:\data")
    assert not kpaths.looks_like_unc("/mnt/nas")


@winonly
def test_mangled_unc_detected():
    # the exact shapes we saw after a backslash got eaten
    assert kpaths.looks_like_mangled_unc(r"\192.0.2.10\disk7\data")
    assert kpaths.looks_like_mangled_unc(r"\server\share\out")


@winonly
def test_proper_unc_and_drive_paths_not_flagged():
    assert not kpaths.looks_like_mangled_unc(r"\\192.0.2.10\disk7\data")
    assert not kpaths.looks_like_mangled_unc("//192.0.2.10/disk7/data")
    assert not kpaths.looks_like_mangled_unc(r"C:\data\out")
    assert not kpaths.looks_like_mangled_unc("/c/Users/me")  # git-bash drive, 1-char host


def test_posix_paths_never_flagged():
    # On POSIX, /mnt/nas is a real root and must never be treated as mangled.
    if not _WIN:
        assert not kpaths.looks_like_mangled_unc("/mnt/nas/data")


@winonly
def test_doctor_write_root_refuses_mangled_unc_without_creating_local_dir(tmp_path, capsys):
    # Point the "mangled" root at a real temp drive so that, if the guard were
    # missing, mkdir would actually create it — then assert it did NOT.
    mangled = f"\\{tmp_path.drive[0]}__kiroshi_should_not_exist__\\share\\out"
    rep = doctor._Report()
    doctor._check_write_root(rep, mangled)
    out = capsys.readouterr().out
    assert rep.failed
    assert "FAIL" in out and "//server/share" in out
    assert not os.path.exists(mangled)  # no bogus local tree created


def test_doctor_write_root_ok_on_real_dir(tmp_path, capsys):
    wroot = tmp_path / "out"
    rep = doctor._Report()
    doctor._check_write_root(rep, str(wroot))
    out = capsys.readouterr().out
    assert not rep.failed
    assert "PASS" in out
    assert wroot.exists()
