"""Storage path resolution — read root vs write root.

On a NAS, reading is fastest from a per-disk "direct" share (bypasses the union
filesystem) while writing is safest/fastest through the cached user share. Kiroshi
keeps these as two independent roots so a task can read from one and write to the
other. Always use UNC / mount paths in config — never drive letters, which are
per-machine and break across the mesh.

Drive letters are a double foot-gun on Windows: a letter like ``X:`` is mapped
per **logon session**, so a process launched from an elevated shell, a Windows
service, or a Scheduled Task often can't see a drive the interactive desktop
mapped (the ``EnableLinkedConnections`` split). UNC paths (``\\\\server\\share``)
authenticate via Credential Manager and work from every logon context. So Kiroshi
resolves any drive-letter root to its underlying UNC target before use.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from .config import HostConfig, MeshConfig


def resolve_read_root(cfg: MeshConfig, host: Optional[HostConfig] = None) -> Optional[Path]:
    host = host or cfg.host()
    val = os.environ.get("KIROSHI_READ_ROOT") or host.read_root or cfg.read_root
    return Path(normalize_root(val)) if val else None


def resolve_write_root(cfg: MeshConfig, host: Optional[HostConfig] = None) -> Optional[Path]:
    host = host or cfg.host()
    val = os.environ.get("KIROSHI_WRITE_ROOT") or host.write_root or cfg.write_root
    return Path(normalize_root(val)) if val else None


def looks_like_drive_letter(p: str | os.PathLike[str]) -> bool:
    """True for paths like ``Q:\\foo`` — a portability foot-gun across the mesh."""
    s = str(p)
    return len(s) >= 2 and s[1] == ":" and s[0].isalpha()


def unc_for_drive(letter: str) -> Optional[str]:
    """Return the UNC target a Windows drive letter is mapped to, or ``None``.

    Uses the Win32 ``WNetGetConnectionW`` API (no subprocess, no parsing). The
    ``letter`` may be ``"X"`` or ``"X:"``.
    """
    if sys.platform != "win32":
        return None
    drive = letter[0].upper() + ":"
    try:
        import ctypes
        from ctypes import wintypes

        mpr = ctypes.WinDLL("mpr", use_last_error=True)
        ERROR_MORE_DATA = 234
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        ret = mpr.WNetGetConnectionW(
            ctypes.c_wchar_p(drive), buf, ctypes.byref(size)
        )
        if ret == ERROR_MORE_DATA:
            buf = ctypes.create_unicode_buffer(size.value)
            ret = mpr.WNetGetConnectionW(
                ctypes.c_wchar_p(drive), buf, ctypes.byref(size)
            )
        if ret == 0 and buf.value:
            return buf.value
    except Exception:
        return None
    return None


def is_mapped_network_drive(p: str | os.PathLike[str]) -> bool:
    """True only for a drive-letter path whose letter maps to a network share.

    This is the case that breaks across logon sessions (elevated shell / service /
    Scheduled Task). A plain local drive (``C:``) is visible everywhere, so it is
    deliberately *not* flagged.
    """
    s = str(p)
    return looks_like_drive_letter(s) and unc_for_drive(s[0]) is not None


def normalize_root(p: Optional[str | os.PathLike[str]]) -> Optional[str]:
    """Make a configured root portable across logon contexts.

    If ``p`` is a Windows drive-letter path whose letter maps to a network share,
    rewrite it to the equivalent UNC path so it resolves from elevated shells,
    services, and Scheduled Tasks. Local drive letters and UNC/POSIX paths are
    returned unchanged.
    """
    if p is None:
        return None
    # Trailing whitespace is a common Windows foot-gun: ``set VAR=value `` keeps
    # the space, and ``\\server\share `` then fails to resolve. Strip it (and
    # surrounding quotes) before anything else.
    s = str(p).strip().strip('"')
    if not s:
        return None
    if not looks_like_drive_letter(s):
        return s
    unc = unc_for_drive(s[0])
    if not unc:
        return s
    rest = s[2:].lstrip("\\/")
    base = unc.rstrip("\\/")
    return f"{base}\\{rest}" if rest else base
