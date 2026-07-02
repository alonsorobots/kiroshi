"""``kiroshi doctor`` — preflight checks that fail fast with actionable fixes.

Most "the mesh isn't working" incidents are environmental, not bugs in Kiroshi:
a worker env missing a dependency, a NAS root that isn't visible from this logon
session, a drive letter that an elevated/service process can't see, or a Fixer
that moved IPs. Left undiagnosed, these burn the per-gig retry budget and surface
only as cryptic ``recent_errors`` after the fact.

``doctor`` reproduces, on the exact machine + interpreter that will run the
Runner, the things that actually break:

  1. the task module imports (this catches missing deps and OS policy blocks such
     as Windows Smart App Control refusing an unsigned ``.pyd``);
  2. the read root is listable and the write root is actually writable;
  3. drive-letter roots are flagged + their UNC target shown;
  4. the Fixer is reachable (or discoverable via beacon).

It prints a PASS/WARN/FAIL line per check and exits non-zero if anything is a
hard FAIL, so it slots into a launch script or Scheduled Task as a gate.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

from . import kfs
from . import paths as kpaths

_OK = "PASS"
_WARN = "WARN"
_FAIL = "FAIL"


class _Report:
    def __init__(self) -> None:
        self.failed = False
        self.warned = False

    def line(self, level: str, check: str, detail: str = "") -> None:
        tail = f"  {detail}" if detail else ""
        print(f"  [{level}] {check}{tail}", flush=True)
        if level == _FAIL:
            self.failed = True
        elif level == _WARN:
            self.warned = True


def _check_python(rep: _Report) -> None:
    rep.line(_OK, "python", f"{sys.version.split()[0]} @ {sys.executable}")


def _check_task(rep: _Report, task_ref: Optional[str], syspath: list[str]) -> None:
    if not task_ref:
        rep.line(_WARN, "task", "no --task given; skipping import check")
        return
    for p in syspath:
        if p and p not in sys.path:
            sys.path.insert(0, p)
    try:
        from .tasks import resolve_task

        resolve_task(task_ref)
        rep.line(_OK, "task import", task_ref)
    except ModuleNotFoundError as e:
        rep.line(_FAIL, "task import", f"{task_ref}: missing module '{e.name}'. "
                 f"Install it into THIS interpreter's env.")
    except ImportError as e:
        # Covers Windows Smart App Control / WDAC blocking an unsigned native
        # extension ("An Application Control policy has blocked this file").
        rep.line(_FAIL, "task import", f"{task_ref}: {e}. If this mentions an "
                 f"'Application Control policy', install signed binaries (e.g. "
                 f"from the Anaconda 'defaults' channel) or relax the policy.")
    except Exception as e:  # noqa: BLE001
        rep.line(_FAIL, "task import", f"{task_ref}: {e!r}")


def _describe_root(raw: str) -> str:
    if kpaths.looks_like_drive_letter(raw):
        unc = kpaths.unc_for_drive(raw[0])
        if unc:
            return f"{raw}  (drive letter -> {unc})"
        return f"{raw}  (local/unknown drive letter)"
    return raw


def _mangled_unc_msg(kind: str, raw: str) -> str:
    return (f"{raw!r} looks like a UNC path that lost a leading separator "
            f"(\\\\server\\share became \\server\\...), which resolves to a "
            f"*local* path — not the {kind}. A shell or env var probably ate a "
            f"backslash; set it with forward slashes (//server/share/...) which "
            f"don't get mangled.")


def _unc_no_creds_warn(rep: _Report, kind: str, raw: str) -> None:
    rep.line(_WARN, kind, f"{raw} is a UNC share but no SMB credentials are set "
             f"(KIROSHI_NAS_USER/KIROSHI_NAS_PASS). Kiroshi will fall back to the "
             f"Windows redirector, which CANNOT authenticate from an SSH or "
             f"service (network) logon. Set creds to use the smbprotocol data "
             f"plane that works in every logon context.")


def _check_smb_read(rep: _Report, raw: str) -> None:
    server = kfs.server_of(raw)
    user = kfs.creds_for(server)[0] if server else None
    try:
        if kfs.exists(raw):
            rep.line(_OK, "read root", f"{raw}  (smbprotocol -> {server}, auth OK as {user})")
        else:
            rep.line(_FAIL, "read root", f"{raw}: authenticated to {server} but path "
                     f"not found on the share")
    except Exception as e:  # noqa: BLE001
        rep.line(_FAIL, "read root", f"{raw}: SMB error ({e}); check "
                 f"KIROSHI_NAS_USER/PASS, share name, and ACL for {user!r}")


def _check_smb_write(rep: _Report, raw: str) -> None:
    server = kfs.server_of(raw)
    user = kfs.creds_for(server)[0] if server else None
    probe = raw.rstrip("/\\") + f"/.kiroshi_doctor_{os.getpid()}_{int(time.time())}.tmp"
    try:
        with kfs.atomic_write(probe) as fh:
            fh.write(b"ok")
        if kfs.exists(probe):
            kfs.remove(probe)
        rep.line(_OK, "write root", f"{raw} (smbprotocol -> {server}, write+delete "
                 f"verified as {user})")
    except Exception as e:  # noqa: BLE001
        rep.line(_FAIL, "write root", f"{raw}: SMB write failed ({e}); check creds "
                 f"and that {user!r} is on the share's write list")


def _check_read_root(rep: _Report, raw: Optional[str]) -> None:
    if not raw:
        rep.line(_WARN, "read root", "KIROSHI_READ_ROOT not set")
        return
    if kpaths.looks_like_mangled_unc(raw):
        rep.line(_FAIL, "read root", _mangled_unc_msg("share", raw))
        return
    if kfs.use_smb(raw):
        _check_smb_read(rep, raw)
        return
    if kpaths.looks_like_unc(raw):
        _unc_no_creds_warn(rep, "read root", raw)
    drive_warn = kpaths.is_mapped_network_drive(raw)
    resolved = kpaths.normalize_root(raw) or raw
    p = Path(resolved)
    if drive_warn:
        rep.line(_WARN, "read root", f"{_describe_root(raw)} — prefer UNC for "
                 f"elevated/service/Scheduled-Task contexts")
    try:
        if not p.exists():
            rep.line(_FAIL, "read root", f"{resolved} does not exist / not visible "
                     f"from this logon session")
            return
        next(iter(os.scandir(p)), None)
        rep.line(_OK, "read root", resolved)
    except OSError as e:
        rep.line(_FAIL, "read root", f"{resolved}: {e}")


def _check_write_root(rep: _Report, raw: Optional[str]) -> None:
    if not raw:
        rep.line(_WARN, "write root", "KIROSHI_WRITE_ROOT not set")
        return
    # Guard BEFORE any mkdir: a mangled UNC resolves to a local path, and
    # mkdir(parents=True) would happily create a bogus local tree and report a
    # false PASS (masking the misconfig until jobs write to the wrong disk).
    if kpaths.looks_like_mangled_unc(raw):
        rep.line(_FAIL, "write root", _mangled_unc_msg("share", raw))
        return
    if kfs.use_smb(raw):
        _check_smb_write(rep, raw)
        return
    if kpaths.looks_like_unc(raw):
        _unc_no_creds_warn(rep, "write root", raw)
    drive_warn = kpaths.is_mapped_network_drive(raw)
    resolved = kpaths.normalize_root(raw) or raw
    p = Path(resolved)
    if drive_warn:
        rep.line(_WARN, "write root", f"{_describe_root(raw)} — prefer UNC")
    probe = p / f".kiroshi_doctor_{os.getpid()}_{int(time.time())}.tmp"
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"ok")
        probe.unlink()
        rep.line(_OK, "write root", f"{resolved} (write+delete verified)")
    except OSError as e:
        rep.line(_FAIL, "write root", f"{resolved}: not writable ({e})")
        try:
            if probe.exists():
                probe.unlink()
        except OSError:
            pass


def _check_fixer(rep: _Report, fixer_url: Optional[str], auto: bool,
                 token: Optional[str] = None) -> None:
    import requests

    from .discovery import discover_fixer

    if auto or not fixer_url:
        url = discover_fixer(timeout=6.0)
        if not url:
            rep.line(_FAIL, "fixer discovery",
                     "no beacon heard in 6s. Either no Fixer is running on this "
                     "LAN (start it with: kiroshi fixer), OR the Fixer host is "
                     "silently dropping UDP :8788 at the firewall. On the Fixer "
                     "host, run (elevated): kiroshi firewall install")
            return
        rep.line(_OK, "fixer discovery", f"beacon -> {url}")
        fixer_url = url
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = requests.get(f"{fixer_url.rstrip('/')}/status", timeout=8, headers=headers)
        if r.status_code == 401:
            rep.line(_FAIL, "fixer auth", f"{fixer_url}: 401 unauthorized — wrong/"
                     f"missing mesh token (set KIROSHI_TOKEN or --token).")
            return
        r.raise_for_status()
        d = r.json()
        rep.line(_OK, "fixer reachable", f"{fixer_url}  "
                 f"(pending={d.get('pending')} leased={d.get('leased')} "
                 f"done={d.get('done')} failed={d.get('failed')})")
    except Exception as e:  # noqa: BLE001
        emsg = str(e)
        hint = ""
        if "timed out" in emsg.lower() or "connectiontimeout" in emsg.lower().replace(" ", ""):
            hint = ("  hint: TCP timeout usually means Windows Firewall is dropping "
                    "inbound on the Fixer host. On that host, run (elevated): "
                    "kiroshi firewall install")
        rep.line(_FAIL, "fixer reachable", f"{fixer_url}: {e}" + (f"\n{hint}" if hint else ""))


def run_doctor(
    task_ref: Optional[str] = None,
    syspath: Optional[list[str]] = None,
    fixer_url: Optional[str] = None,
    auto: bool = False,
    read_root: Optional[str] = None,
    write_root: Optional[str] = None,
    token: Optional[str] = None,
) -> int:
    """Run all checks; return 0 if no hard failures, else 1."""
    rep = _Report()
    read_root = read_root or os.environ.get("KIROSHI_READ_ROOT")
    write_root = write_root or os.environ.get("KIROSHI_WRITE_ROOT")

    print("kiroshi doctor — preflight", flush=True)
    _check_python(rep)
    _check_task(rep, task_ref, list(syspath or []))
    _check_read_root(rep, read_root)
    _check_write_root(rep, write_root)
    _check_fixer(rep, fixer_url, auto, token)

    if rep.failed:
        print("\nRESULT: FAIL — fix the above before joining the mesh.", flush=True)
        return 1
    if rep.warned:
        print("\nRESULT: OK (with warnings).", flush=True)
        return 0
    print("\nRESULT: OK — ready to join.", flush=True)
    return 0
