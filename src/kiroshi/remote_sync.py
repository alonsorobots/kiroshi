"""kiroshi remote sync — propagate committed code to every mesh node.

Iterates ``[hosts.*]`` from the loaded config and, for each host:

    1. ``git -C <repo> pull --ff-only``  for every tracked repo path
    2. optionally ``pip install -e .``   (only if entry points / deps changed)
    3. optionally signal any live runner to exit; the durable auto-restart
       wrapper re-launches it against the new code on its next cycle.

Design goals:

  * **Dry-run is first-class.** ``--dry-run`` prints the exact per-host commands
    without touching anything remote. This is the intended default for the
    first use — operator eyeballs the plan, then runs for real.
  * **Never destructive without opt-in.** No ``git reset --hard``, no forced
    re-checkout, no service restart unless ``--restart`` is passed.
  * **``ff-only`` pulls.** If a node has diverged (someone committed on the
    box), the pull refuses and reports it instead of merging blindly.
  * **Pure planner, thin executor.** ``plan_sync()`` returns a list of
    per-host actions with no I/O; ``execute_plan()`` walks them via ssh.
    That makes the planning logic unit-testable.

Skips:
  * ``_DEFAULT`` (a config fallback, not a real host)
  * the local host (already up to date by definition — this is where the sync
    is being launched from)
"""
from __future__ import annotations

import shlex
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterable, Optional


def _remap_repo(local_path: str, root: Optional[str]) -> str:
    """Map a coordinator-local repo path to the same repo on a remote whose
    checkout lives under a different ``root`` (e.g. a different Windows user).

    ``C:\\Users\\admin\\...\\kiroshi`` + root ``C:\\Users\\alons\\...\\RESEARCH``
    -> ``C:\\Users\\alons\\...\\RESEARCH\\kiroshi``. With no ``root`` the path is
    used verbatim (nodes that mirror the coordinator's layout — unchanged).
    """
    if not root:
        return local_path
    name = PureWindowsPath(local_path).name or Path(local_path).name
    sep = "\\" if ("\\" in root or ":" in root) else "/"
    return root.rstrip("/\\") + sep + name


def _default_repos() -> tuple[str, ...]:
    """Best-effort default: assume each remote mirrors THIS machine's kiroshi
    checkout location. Derived from the package's own path (no hardcoded,
    machine-specific paths in source). Sites whose remotes use a different
    layout — or who want to sync additional repos — pass ``--repos`` explicitly.
    """
    try:
        return (str(Path(__file__).resolve().parents[2]),)  # <repo>/src/kiroshi -> <repo>
    except Exception:
        return ()


# Repos git-pulled when --repos is not given (each entry is the REMOTE path).
DEFAULT_REPOS = _default_repos()


# --------------------------------------------------------------------------
# Pure planner (no I/O — unit-testable)
# --------------------------------------------------------------------------

@dataclass
class SyncStep:
    """One command in a host's sync plan."""
    kind: str                   # "pull" | "reinstall" | "restart" | "note"
    description: str            # human-readable one-liner
    remote_cmd: Optional[str] = None   # command to run on the remote (or None)


@dataclass
class HostPlan:
    host: str
    python: Optional[str]
    steps: list[SyncStep] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    ssh_target: str = ""   # ssh argument (user@host); defaults to host

    def __post_init__(self) -> None:
        if not self.ssh_target:
            self.ssh_target = self.host


def _repo_pull_cmd(repo: str) -> str:
    """``git -C`` avoids depending on the remote shell's cwd. ``--ff-only``
    refuses to merge — a diverged remote surfaces cleanly instead of silently
    creating a merge commit on a production box."""
    return f"git -C {shlex.quote(repo)} pull --ff-only"


def _reinstall_cmd(repo: str, python: Optional[str]) -> str:
    py = shlex.quote(python) if python else "python"
    return f"{py} -m pip install --quiet -e {shlex.quote(repo)}"


def _restart_cmd(host: str) -> str:
    """Signal runners on the remote to exit so the auto-restart wrapper
    respawns them against the new code. Idempotent: harmless if no runner
    is registered from this host."""
    return f"kiroshi stop --host {shlex.quote(host)}"


def plan_sync(hosts: dict[str, Any],
              repos: Iterable[str] = DEFAULT_REPOS,
              reinstall: bool = False,
              restart: bool = False,
              local_hostnames: Iterable[str] = ()) -> list[HostPlan]:
    """Build per-host command plans.

    ``hosts`` maps host name -> anything with ``.python`` (typically
    ``HostConfig``); it accepts a dict for testability.
    ``local_hostnames`` is the set of names identifying the box running this
    command (case-insensitive) so we don't try to ssh into ourselves.
    """
    local = {h.lower() for h in local_hostnames}
    plans: list[HostPlan] = []
    for name, hc in hosts.items():
        if name == "_DEFAULT":
            continue
        if name.lower() in local:
            plans.append(HostPlan(host=name, python=getattr(hc, "python", None),
                                  skipped=True, skip_reason="local host (nothing to sync)"))
            continue
        python = getattr(hc, "python", None)
        ssh_target = getattr(hc, "ssh_target", None) or name
        root = getattr(hc, "root", None)
        steps: list[SyncStep] = []
        for repo in repos:
            remote_repo = _remap_repo(repo, root)
            steps.append(SyncStep("pull", f"git pull (ff-only) {remote_repo}",
                                  _repo_pull_cmd(remote_repo)))
            if reinstall:
                steps.append(SyncStep("reinstall", f"pip install -e {remote_repo}",
                                      _reinstall_cmd(remote_repo, python)))
        if restart:
            steps.append(SyncStep("restart",
                                  "signal runners to exit (auto-restart wrapper respawns)",
                                  _restart_cmd(name)))
        if not steps:
            steps.append(SyncStep("note", "no repos to sync (empty --repos)", None))
        plans.append(HostPlan(host=name, python=python, steps=steps,
                              ssh_target=ssh_target))
    return plans


def render_plan(plans: list[HostPlan]) -> str:
    """Human-readable render of a plan. Same format used by ``--dry-run``."""
    lines: list[str] = []
    for p in plans:
        if p.skipped:
            lines.append(f"[{p.host}] SKIP  ({p.skip_reason})")
            continue
        py = f"python={p.python}" if p.python else "python=<remote default>"
        lines.append(f"[{p.host}] {py}")
        for s in p.steps:
            if s.remote_cmd is None:
                lines.append(f"  # {s.description}")
            else:
                lines.append(f"  $ ssh {p.ssh_target} {s.remote_cmd}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Executor (thin ssh shell — injectable for tests)
# --------------------------------------------------------------------------

def _default_ssh(host: str, cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    """Return ``(rc, stdout, stderr)``. Non-interactive: BatchMode=yes so a
    misconfigured host fails fast instead of prompting for a password."""
    argv = ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", host, cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"ssh {host!r}: timeout"
    except OSError as exc:
        return 127, "", f"ssh {host!r}: {exc}"
    return r.returncode, r.stdout or "", r.stderr or ""


def execute_plan(plans: list[HostPlan], dry_run: bool = True,
                 ssh: Callable = _default_ssh,
                 out: Callable[[str], None] = print) -> int:
    """Execute a plan. Returns the number of failed steps across all hosts."""
    failures = 0
    for p in plans:
        if p.skipped:
            out(f"[{p.host}] SKIP ({p.skip_reason})")
            continue
        out(f"[{p.host}] python={p.python or '<remote default>'}")
        for s in p.steps:
            if s.remote_cmd is None:
                out(f"  # {s.description}")
                continue
            if dry_run:
                out(f"  DRY: ssh {p.ssh_target} {s.remote_cmd}")
                continue
            out(f"  RUN: ssh {p.ssh_target} {s.remote_cmd}")
            rc, so, se = ssh(p.ssh_target, s.remote_cmd)
            if rc != 0:
                failures += 1
                out(f"    FAIL rc={rc} {se.strip()[:400]}")
            else:
                if so.strip():
                    out(f"    ok: {so.strip().splitlines()[-1][:200]}")
    return failures


# --------------------------------------------------------------------------
# helpers used by the CLI
# --------------------------------------------------------------------------

def local_hostnames() -> tuple[str, ...]:
    """Names that identify THIS machine, so ``sync`` never ssh-loops into itself."""
    names: set[str] = set()
    try:
        names.add(socket.gethostname())
    except Exception:
        pass
    try:
        names.add(socket.getfqdn())
    except Exception:
        pass
    return tuple(names)
