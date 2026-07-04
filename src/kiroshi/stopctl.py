"""Stop / force-kill registered Kiroshi processes (local host only).

Shared by ``kiroshi stop``, ``kiroshi force-kill``, the MCP ``stop`` /
``force_kill`` tools, and status action hints.
"""
from __future__ import annotations

import socket
import time
from typing import Any, Optional

from .processreg import _pid_alive, list_registered, request_stop
from .proctree import terminate_tree


def _local_process(p: dict[str, Any]) -> bool:
    hn = str(p.get("hostname") or "")
    return (not hn) or hn == socket.gethostname()


def _select_targets(
    *,
    role: Optional[str] = None,
    pid: Optional[int] = None,
    procs: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    procs = procs if procs is not None else list_registered()
    targets = []
    for p in procs:
        if role and p.get("role") != role:
            continue
        if pid is not None and p.get("pid") != pid:
            continue
        targets.append(p)
    return targets


def stop_registered(
    *,
    role: Optional[str] = None,
    pid: Optional[int] = None,
    all: bool = False,
    force: bool = False,
    grace: float = 30.0,
    no_escalate: bool = False,
    procs: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Stop or force-kill matching registered processes on THIS host.

    Returns a structured result for CLI printing and MCP tools. Never raises.
    """
    messages: list[str] = []
    targets = _select_targets(role=role, pid=pid, procs=procs)
    if not targets:
        return {
            "ok": False,
            "exit_code": 1,
            "messages": ["no matching registered processes."],
            "killed": 0,
            "stopped": 0,
            "ambiguous": False,
            "matches": [],
        }
    if len(targets) > 1 and not all and pid is None:
        return {
            "ok": False,
            "exit_code": 1,
            "messages": [
                f"{len(targets)} processes match; pass --all (or --pid) to confirm.",
            ],
            "killed": 0,
            "stopped": 0,
            "ambiguous": True,
            "matches": [
                {"role": p.get("role"), "pid": p.get("pid"),
                 "launch_command": p.get("launch_command", "")}
                for p in targets
            ],
        }

    if force:
        killed = 0
        for p in targets:
            pid_n = int(p.get("pid", 0))
            if _local_process(p) and terminate_tree(pid_n):
                messages.append(f"force-killed: {p.get('role')} pid={pid_n} (process tree)")
                killed += 1
            else:
                messages.append(
                    f"could not force-kill {p.get('role')} pid={pid_n} "
                    f"({'remote' if not _local_process(p) else 'kill failed'})"
                )
        return {
            "ok": killed > 0,
            "exit_code": 0 if killed else 1,
            "messages": messages,
            "killed": killed,
            "stopped": 0,
            "force": True,
            "ambiguous": False,
            "matches": [],
        }

    requested = []
    for p in targets:
        if request_stop(p.get("role", ""), int(p.get("pid", 0))):
            messages.append(f"stop requested (draining): {p.get('role')} pid={p.get('pid')}")
            requested.append(p)
    if not requested:
        return {
            "ok": False,
            "exit_code": 1,
            "messages": messages or ["stop request failed."],
            "killed": 0,
            "stopped": 0,
            "ambiguous": False,
            "matches": [],
        }

    if no_escalate or grace <= 0:
        if grace <= 0 and not no_escalate:
            messages.append("(--grace 0: requested drain, not waiting/escalating)")
        return {
            "ok": True,
            "exit_code": 0,
            "messages": messages,
            "killed": 0,
            "stopped": len(requested),
            "ambiguous": False,
            "matches": [],
        }

    messages.append(f"waiting up to {grace:.0f}s for graceful drain before force-kill...")
    deadline = time.time() + grace
    pending = list(requested)
    while pending and time.time() < deadline:
        time.sleep(1.0)
        still = []
        for p in pending:
            if _local_process(p) and _pid_alive(int(p.get("pid", 0))):
                still.append(p)
            else:
                messages.append(f"drained cleanly: {p.get('role')} pid={p.get('pid')}")
        pending = still

    survivors = [
        p for p in pending
        if _local_process(p) and _pid_alive(int(p.get("pid", 0)))
    ]
    if not survivors:
        messages.append("all targets drained gracefully.")
        return {
            "ok": True,
            "exit_code": 0,
            "messages": messages,
            "killed": 0,
            "stopped": len(requested),
            "ambiguous": False,
            "matches": [],
        }

    killed = 0
    for p in survivors:
        pid_n = int(p.get("pid", 0))
        messages.append(f"grace expired; force-killing {p.get('role')} pid={pid_n} (process tree)")
        if terminate_tree(pid_n):
            killed += 1
    return {
        "ok": True,
        "exit_code": 0,
        "messages": messages,
        "killed": killed,
        "stopped": len(requested),
        "ambiguous": False,
        "matches": [],
    }
