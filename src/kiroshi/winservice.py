"""Windows service install/uninstall via NSSM — the persistence layer (M4).

Mirrors the at-field pattern: wrap ``kiroshi fixer`` / ``kiroshi runner`` as
auto-starting Windows services with rotating stdout/stderr logs and crash
auto-restart, so the mesh survives reboots and runs unattended.

The one Windows gotcha that *must* be respected (learned the hard way wiring the
first cross-host consumer): **a Runner that reads/writes a NAS over SMB cannot
run as ``LocalSystem``.** LocalSystem has no access to the per-user credentials
stored in Credential Manager, so mapped drives and authenticated UNC paths fail
under it. Therefore:

- **Fixer** → defaults to ``LocalSystem`` (only needs a local SQLite file + a TCP
  port; no NAS).
- **Runner** → *requires* a real user account (``DOMAIN\\user`` or ``.\\user``)
  whose Credential Manager holds the NAS credentials, unless it only touches
  local disk. We refuse to silently install a NAS-bound Runner as LocalSystem.

The CLI (``kiroshi service ...``) is the single source of truth; the PowerShell
scripts under ``scripts/`` are thin elevation shims that call back into it.

The command *builders* here are pure functions (no side effects) so they can be
unit-tested without touching the Windows Service Control Manager.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

DEFAULT_FIXER_SERVICE = "kiroshi-fixer"
DEFAULT_RUNNER_SERVICE = "kiroshi-runner"
_LOG_ROTATE_BYTES = 5 * 1024 * 1024  # 5 MB per stdout/stderr log, like at-field
_RESTART_DELAY_MS = 5000


# --------------------------------------------------------------- discovery
def find_nssm() -> Optional[str]:
    """Locate an ``nssm.exe``. Order: env override, PATH, Kiroshi state dir,
    at-field state dir. Returns the path or ``None``."""
    env = os.environ.get("KIROSHI_NSSM")
    if env and Path(env).is_file():
        return env
    on_path = shutil.which("nssm")
    if on_path:
        return on_path
    candidates = []
    pd = os.environ.get("PROGRAMDATA")
    if pd:
        candidates.append(Path(pd) / "Kiroshi" / "nssm.exe")
        candidates.append(Path(pd) / "ATField" / "nssm.exe")
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def is_admin() -> bool:
    if sys.platform != "win32":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------- command builders
def build_install_commands(
    *,
    nssm: str,
    service_name: str,
    python_exe: str,
    app_parameters: str,
    app_directory: str,
    log_dir: str,
    display_name: str,
    description: str,
    account: Optional[str] = None,
    password: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> list[list[str]]:
    """Return the ordered list of NSSM command invocations to install a service.

    Pure function — builds argv lists; does not execute anything.
    """
    n = nssm
    s = service_name
    stdout = str(Path(log_dir) / f"{service_name}.stdout.log")
    stderr = str(Path(log_dir) / f"{service_name}.stderr.log")
    cmds: list[list[str]] = [
        [n, "install", s, python_exe],
        [n, "set", s, "AppParameters", app_parameters],
        [n, "set", s, "AppDirectory", app_directory],
        [n, "set", s, "DisplayName", display_name],
        [n, "set", s, "Description", description],
        [n, "set", s, "Start", "SERVICE_AUTO_START"],
        # logging (rotating), exactly like at-field's service
        [n, "set", s, "AppStdout", stdout],
        [n, "set", s, "AppStderr", stderr],
        [n, "set", s, "AppRotateFiles", "1"],
        [n, "set", s, "AppRotateOnline", "1"],
        [n, "set", s, "AppRotateBytes", str(_LOG_ROTATE_BYTES)],
        # crash auto-restart
        [n, "set", s, "AppExit", "Default", "Restart"],
        [n, "set", s, "AppRestartDelay", str(_RESTART_DELAY_MS)],
    ]
    if account:
        # NSSM wants a password arg with ObjectName for non-builtin accounts.
        if password is not None and account not in ("LocalSystem",
                                                    "NT AUTHORITY\\LocalService",
                                                    "NT AUTHORITY\\NetworkService"):
            cmds.append([n, "set", s, "ObjectName", account, password])
        else:
            cmds.append([n, "set", s, "ObjectName", account])
    if env:
        # AppEnvironmentExtra takes NAME=VALUE entries; a service does NOT inherit
        # the interactive shell's environment, so anything the task needs (token,
        # PYTHONPATH, NAS roots) must be injected here.
        extra = [f"{k}={v}" for k, v in env.items()]
        cmds.append([n, "set", s, "AppEnvironmentExtra", *extra])
    return cmds


def build_uninstall_commands(nssm: str, service_name: str) -> list[list[str]]:
    return [
        [nssm, "stop", service_name],
        [nssm, "remove", service_name, "confirm"],
    ]


def runner_needs_user_account(read_root: Optional[str], write_root: Optional[str],
                              account: Optional[str]) -> bool:
    """True if we'd be installing a NAS-bound Runner as a non-user account — the
    configuration that silently fails on Windows. Used to refuse + warn."""
    def _is_network(p: Optional[str]) -> bool:
        if not p:
            return False
        p = p.strip().strip('"')
        return p.startswith("\\\\") or p.startswith("//")

    builtin = {None, "LocalSystem", "NT AUTHORITY\\LocalService",
               "NT AUTHORITY\\NetworkService"}
    touches_nas = _is_network(read_root) or _is_network(write_root)
    return touches_nas and account in builtin


# --------------------------------------------------------------- execution
def _run(cmds: list[list[str]], *, check_first: bool = True) -> tuple[bool, str]:
    out_lines = []
    for i, c in enumerate(cmds):
        try:
            r = subprocess.run(c, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"failed to run {c[:3]}...: {e}"
        tag = " ".join(c[1:4])
        out_lines.append(f"$ nssm {tag} -> rc={r.returncode}")
        if r.stdout.strip():
            out_lines.append(r.stdout.strip())
        if r.stderr.strip():
            out_lines.append(r.stderr.strip())
        # The very first command (install) must succeed; subsequent `set`s are
        # best-effort-tolerant but we still surface failures.
        if check_first and i == 0 and r.returncode != 0:
            return False, "\n".join(out_lines)
    return True, "\n".join(out_lines)


def install(commands: list[list[str]]) -> tuple[bool, str]:
    return _run(commands, check_first=True)


def uninstall(commands: list[list[str]]) -> tuple[bool, str]:
    return _run(commands, check_first=False)


def status(service_name: str) -> str:
    """Human-readable service status via ``sc query`` (no admin needed)."""
    try:
        r = subprocess.run(["sc", "query", service_name], capture_output=True,
                           text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return f"{service_name}: query failed ({e})"
    if "1060" in r.stdout or "1060" in r.stderr or r.returncode == 1060:
        return f"{service_name}: not installed"
    state = "?"
    for line in r.stdout.splitlines():
        if "STATE" in line:
            state = line.split(":", 1)[1].strip()
    return f"{service_name}: {state}"
