"""Windows Firewall management for Kiroshi's inbound ports.

Kiroshi's Coordinator binds *exactly two* ports for the mesh to work:

- TCP ``<fixer_port>`` (default 8787) — HTTP API + dashboard.
- UDP ``<discovery_port>`` (default 8788) — solicited-reply beacon so runners
  can find the Coordinator via ``--fixer auto``.

On Windows both are silently dropped by default unless a firewall rule opens
them. Runners **do not need any inbound rule** — their outbound TCP is always
allowed, and the stateful firewall lets the response back in. So this module
only cares about the Coordinator host.

The design goal is "do it once, permanently, and stop chasing new ports":

1. Enumerate ALL Kiroshi-managed rules (identified by the ``RULE_PREFIX`` name
   prefix), so re-running is idempotent and cleans up drift from earlier
   experiments (e.g. old ``Kiroshi Coordinator 8800`` rules) in the same shot.
2. Recompute the desired rule set from the current Kiroshi config, so the
   answer to "which ports are open?" always tracks ``kiroshi.local.toml``
   instead of a hand-maintained pile of ``netsh`` invocations.
3. Rules default to ``private,domain`` profiles and (when possible) are
   scoped to the local RFC1918 ``/24`` for defense in depth — a laptop
   that later joins a public Wi-Fi won't accidentally expose the Coordinator.

All destructive operations (add/delete) require admin and are gated by
:func:`is_admin`; the callers surface a copy-paste elevated command instead
of silently failing. Nothing here auto-elevates — matches the "no magic"
principle: the user always sees exactly what will change.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

RULE_PREFIX = "Kiroshi "
FIXER_RULE_NAME = "Kiroshi Coordinator HTTP"
DISCOVERY_RULE_NAME = "Kiroshi Discovery UDP"

NetshRunner = Callable[[list[str]], subprocess.CompletedProcess]


# --------------------------------------------------------------------- rules
@dataclass(frozen=True)
class FirewallRule:
    """One inbound-allow rule Kiroshi manages. Immutable; compared by value."""

    name: str
    protocol: str  # "TCP" | "UDP"
    port: int
    profiles: str = "private,domain"
    remote_ip: str = "any"  # e.g. "192.168.1.0/24" or "any"

    def netsh_add_args(self) -> list[str]:
        return [
            "advfirewall", "firewall", "add", "rule",
            f"name={self.name}",
            "dir=in", "action=allow",
            f"protocol={self.protocol}",
            f"localport={self.port}",
            f"profile={self.profiles}",
            f"remoteip={self.remote_ip}",
        ]


# ---------------------------------------------------------- subnet discovery
def local_lan_ips() -> list[str]:
    """Best-effort list of this host's non-loopback IPv4 addresses."""
    out: set[str] = set()
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                out.add(ip)
    except OSError:
        pass
    return sorted(out)


def primary_route_ip() -> Optional[str]:
    """Return the IP the OS would use to reach a common public address.

    This is the classic "which NIC is my default route on?" trick: open a UDP
    socket, ``connect()`` to a routable address (no packet is actually sent),
    then read ``getsockname()`` — the OS fills in the source IP of the
    interface it would use. On a multi-homed box (LAN + WSL + VPN + Hyper-V
    switch), this reliably returns the real LAN NIC's address instead of
    whichever virtual adapter enumerates first. Returns ``None`` if it can't
    even open the probe socket (offline, sandboxed, etc.).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.5)
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def pick_lan_subnet(ips: Optional[list[str]] = None) -> Optional[str]:
    """Return an RFC1918 ``/24`` covering the primary LAN NIC, or ``None``.

    Prefers the IP on this host's default route (via :func:`primary_route_ip`)
    so a machine with both a real ethernet (``192.168.1.166``) and a WSL/
    Hyper-V virtual switch (``172.25.96.0/24``) picks the ethernet subnet
    instead of the useless virtual one. Falls back to scanning
    ``getaddrinfo(hostname)`` when the socket probe is unavailable
    (tests, offline machines).
    """
    # 1. Prefer the primary-route IP — this is the single most accurate signal
    #    for "which subnet is my LAN".
    if ips is None:
        primary = primary_route_ip()
        if primary and not primary.startswith("127."):
            try:
                if ipaddress.IPv4Address(primary).is_private:
                    return str(ipaddress.IPv4Network(f"{primary}/24", strict=False))
            except ValueError:
                pass
        ips = local_lan_ips()

    # 2. Fallback: scan every private IP we know about, but skip common
    #    virtual-switch ranges so we don't accidentally scope rules to WSL.
    _VIRTUAL_HINTS = ("172.17.", "172.18.", "172.19.", "172.20.",
                      "172.21.", "172.22.", "172.23.", "172.24.",
                      "172.25.", "172.26.", "172.27.", "172.28.",
                      "172.29.", "172.30.", "172.31.")
    private_candidates: list[str] = []
    virtual_candidates: list[str] = []
    for ip in ips:
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if not addr.is_private:
            continue
        if ip.startswith(_VIRTUAL_HINTS):
            virtual_candidates.append(ip)
        else:
            private_candidates.append(ip)
    # Real LAN first, virtual only as last resort.
    for ip in private_candidates + virtual_candidates:
        return str(ipaddress.IPv4Network(f"{ip}/24", strict=False))
    return None


# ---------------------------------------------------------------- planning
def plan_rules(
    fixer_ports: "int | Iterable[int]",
    discovery_port: int = 8788,
    remote_ip: str = "any",
) -> list[FirewallRule]:
    """The desired set of Kiroshi-managed rules. Pure function; drives tests.

    ``fixer_ports`` may be a single port (int) or many (iterable). Passing the
    full set of ports the mesh uses — e.g. the persistent service plus every
    job Coordinator (8787, 8800, 8801, 8802) — opens them all in one idempotent
    shot, so you never silently close one job's port by opening another's.

    Naming: a single port keeps the historical unsuffixed ``FIXER_RULE_NAME``
    (backward compatible); multiple ports get per-port names
    (``Kiroshi Coordinator HTTP 8800``) so each is tracked + drift-cleaned
    independently.
    """
    if isinstance(fixer_ports, int):
        ports = [int(fixer_ports)]
    else:
        # dedup, preserve order
        ports = list(dict.fromkeys(int(p) for p in fixer_ports))
    single = len(ports) == 1
    rules: list[FirewallRule] = []
    for p in ports:
        name = FIXER_RULE_NAME if single else f"{FIXER_RULE_NAME} {p}"
        rules.append(FirewallRule(name, "TCP", p, remote_ip=remote_ip))
    rules.append(FirewallRule(DISCOVERY_RULE_NAME, "UDP", int(discovery_port),
                              remote_ip=remote_ip))
    return rules


# ------------------------------------------------------------------- admin
def is_admin() -> bool:
    """True if the current process is elevated on Windows (root on POSIX)."""
    if sys.platform != "win32":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def elevated_install_hint(argv_tail: str = "firewall install") -> str:
    """A copy-pasteable PowerShell one-liner that re-runs Kiroshi elevated.

    Deliberately does NOT auto-elevate — the user must see and approve the UAC
    prompt for a firewall change. Prints exactly what will run.
    """
    exe = sys.executable.replace("'", "''")
    return (
        f'powershell -NoProfile -Command "Start-Process -Verb RunAs -Wait '
        f'-FilePath \'{exe}\' -ArgumentList \'-m\',\'kiroshi\','
        + ",".join(f"'{tok}'" for tok in argv_tail.split())
        + '"'
    )


# ------------------------------------------------------------------- netsh
def _run_netsh(args: list[str], *, runner: Optional[NetshRunner] = None
               ) -> subprocess.CompletedProcess:
    if runner is not None:
        return runner(["netsh"] + args)
    return subprocess.run(["netsh"] + args, capture_output=True, text=True)


def list_kiroshi_rules(*, runner: Optional[NetshRunner] = None) -> list[str]:
    """Enumerate the names of currently-installed Kiroshi-managed rules.

    Uses ``netsh advfirewall firewall show rule name=all`` and filters on
    :data:`RULE_PREFIX`. Read-only; no admin required.
    """
    r = _run_netsh(["advfirewall", "firewall", "show", "rule", "name=all"],
                   runner=runner)
    if r.returncode != 0:
        return []
    names: list[str] = []
    for line in (r.stdout or "").splitlines():
        if not line.startswith("Rule Name:"):
            continue
        name = line.split(":", 1)[1].strip()
        if name.startswith(RULE_PREFIX):
            names.append(name)
    seen: set[str] = set()
    dedup: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            dedup.append(n)
    return dedup


def delete_rule(name: str, *, runner: Optional[NetshRunner] = None) -> bool:
    r = _run_netsh(
        ["advfirewall", "firewall", "delete", "rule", f"name={name}"],
        runner=runner,
    )
    return r.returncode == 0


def add_rule(rule: FirewallRule, *, runner: Optional[NetshRunner] = None
             ) -> tuple[bool, str]:
    r = _run_netsh(rule.netsh_add_args(), runner=runner)
    return (r.returncode == 0, (r.stdout or "") + (r.stderr or ""))


# ------------------------------------------------------------------- apply
@dataclass
class ApplyResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def apply_rules(
    rules: list[FirewallRule],
    *,
    runner: Optional[NetshRunner] = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Make the machine's Kiroshi-managed rules match ``rules`` exactly.

    Strategy: enumerate all ``Kiroshi *`` rules; delete anything not in the
    desired set (drift cleanup); delete-then-add each desired rule (so the
    body of a re-run rule always reflects the current port/subnet without
    fighting ``netsh set rule new`` quirks). Idempotent.

    Read-only when ``dry_run=True``; safe to run without admin as an inspection
    tool. Otherwise writes require admin — the caller enforces that.
    """
    desired_by_name = {r.name: r for r in rules}
    existing = list_kiroshi_rules(runner=runner)
    result = ApplyResult()

    for name in existing:
        if dry_run:
            if name not in desired_by_name:
                result.removed.append(name)
            continue
        if not delete_rule(name, runner=runner):
            result.errors.append(f"failed to delete existing rule: {name!r}")
            continue
        if name not in desired_by_name:
            result.removed.append(name)

    for rule in rules:
        if dry_run:
            result.added.append(rule.name)
            continue
        ok, out = add_rule(rule, runner=runner)
        if ok:
            if rule.name in existing:
                result.kept.append(rule.name)
            else:
                result.added.append(rule.name)
        else:
            result.errors.append(
                f"failed to add rule {rule.name!r}: {out.strip()[:200]}"
            )

    return result


# ----------------------------------------------------------------- status
def format_status(rules: list[FirewallRule], existing: list[str]) -> str:
    """Human-readable diff of desired vs installed rules."""
    lines = ["Kiroshi firewall status:"]
    desired = {r.name for r in rules}
    for r in rules:
        mark = "OK " if r.name in existing else "-- "
        lines.append(
            f"  {mark}{r.name}: {r.protocol} {r.port} "
            f"remote={r.remote_ip} profiles={r.profiles}"
        )
    stale = [n for n in existing if n not in desired]
    for n in stale:
        lines.append(f"  ?? {n}  (stale / drift — will be removed on next install)")
    if not existing:
        lines.append("  (no Kiroshi-* rules found)")
    return "\n".join(lines)
