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
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Optional

from .config import HostConfig, MeshConfig


def resolve_read_root(cfg: MeshConfig, host: Optional[HostConfig] = None) -> Optional[Path]:
    host = host or cfg.host()
    val = os.environ.get("KIROSHI_READ_ROOT") or host.read_root or cfg.read_root
    return Path(normalize_root(val)) if val else None


def resolve_write_root(cfg: MeshConfig, host: Optional[HostConfig] = None) -> Optional[Path]:
    host = host or cfg.host()
    val = os.environ.get("KIROSHI_WRITE_ROOT") or host.write_root or cfg.write_root
    return Path(normalize_root(val)) if val else None


# --------------------------------------------------------- per-sub-job roots (N3)
def gig_read_root(spec: dict[str, Any]) -> Optional[str]:
    """The read root for a specific sub-job: the disk's direct share (injected by the
    Coordinator at lease time for topology-aware gigs), else the env
    ``KIROSHI_READ_ROOT``. ``None`` if neither is set. Prefer the spec root so a
    sub-job on ``disk1`` reads from ``disk1``'s direct spindle share, not the single
    mesh-wide root — the dual-path routing win (PLAN §7.6)."""
    return spec.get("read_root") or os.environ.get("KIROSHI_READ_ROOT")


def gig_write_root(spec: dict[str, Any]) -> Optional[str]:
    """The write root for a specific sub-job: the disk's cached share, else the env
    ``KIROSHI_WRITE_ROOT``. See :func:`gig_read_root`."""
    return spec.get("write_root") or os.environ.get("KIROSHI_WRITE_ROOT")


def confined_join(root: str, rel: str) -> str:
    """Join a sub-job-supplied relative path under ``root`` using PURE path arithmetic
    (no ``resolve()``/realpath — the root may be an SMB UNC we deliberately never
    touch via the OS redirector).

    SECURITY: ``rel`` is untrusted (whoever seeded the sub-job). We refuse absolute
    paths and any ``..`` traversal that would escape ``root`` — otherwise a
    malicious/buggy spec could make a Runner read/write anywhere its account can
    reach. A task that genuinely needs unconfined paths must opt in explicitly.

    UNC roots get backslash separators (Windows wants ``\\\\server\\share\\x``);
    everything else uses ``os.path.join``. Extracted from the motion task so every
    task gets the same confinement for free.
    """
    if looks_like_mangled_unc(root):
        raise ValueError(
            f"root {root!r} looks like a UNC path that lost a leading separator; "
            f"use the //server/share form")
    r = str(rel).replace("\\", "/")
    pw = PureWindowsPath(r)
    if r.startswith("/") or pw.is_absolute() or pw.drive:
        raise ValueError(f"absolute path not allowed for a sub-job: {rel!r}")
    parts = [seg for seg in PurePosixPath(r).parts if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise ValueError(f"path {rel!r} escapes its root {root!r}")
    base = root.rstrip("/\\")
    if looks_like_unc(base):
        return base.replace("/", "\\") + "\\" + "\\".join(parts)
    return os.path.join(base, *parts)


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


def looks_like_unc(p: str | os.PathLike[str]) -> bool:
    """True for a proper UNC path (``\\\\server\\share`` or ``//server/share``)."""
    s = str(p)
    return s.startswith("\\\\") or s.startswith("//")


# A single leading separator, then a host-ish token (>=2 chars, so we don't trip
# on git-bash drive paths like ``/c/Users``), then another separator + a token.
_MANGLED_UNC_RE = re.compile(r"^[\\/](?![\\/])([^\\/]{2,})[\\/]([^\\/]+)")


def looks_like_mangled_unc(p: Optional[str | os.PathLike[str]]) -> bool:
    """True if ``p`` was probably a UNC path that lost a leading separator.

    Shells and env-var plumbing love to eat a backslash, turning the intended
    ``\\\\server\\share\\...`` into ``\\server\\share\\...``. On Windows that
    single-leading-separator form silently resolves to a *local* drive-relative
    path (``C:\\server\\share\\...``) — so a write probe "succeeds" against a
    bogus local directory and a real job would dump output on the wrong disk.
    We detect that shape so callers can fail loudly with the ``//server/share``
    fix instead. Windows-only: on POSIX ``/mnt/nas`` is a legitimate root.
    """
    if p is None or sys.platform != "win32":
        return False
    s = str(p).strip().strip('"')
    if looks_like_unc(s):
        return False
    return bool(_MANGLED_UNC_RE.match(s))


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
