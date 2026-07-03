"""Mesh configuration — per-host x per-task, with hostname auto-detect.

Real deployments keep machine-specific values (NAS paths, host python paths) in a
**gitignored** ``kiroshi.local.toml`` or in environment variables, NEVER in
committed files. This module loads, in priority order:

    1. explicit ``path=`` argument
    2. ``$KIROSHI_CONFIG``
    3. ``./kiroshi.local.toml`` (gitignored)
    4. ``./kiroshi.toml``

Environment overrides always win for connection + path values:
    KIROSHI_FIXER_HOST, KIROSHI_FIXER_PORT, KIROSHI_READ_ROOT, KIROSHI_WRITE_ROOT
"""
from __future__ import annotations

import os
import platform
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore

DEFAULT_PORT = 8787
DEFAULT_CAPACITY = 200


def current_host() -> str:
    """Best-effort short hostname for this machine."""
    return platform.node() or socket.gethostname() or "localhost"


@dataclass
class HostConfig:
    name: str = "_DEFAULT"
    python: Optional[str] = None
    workers: int = field(default_factory=lambda: os.cpu_count() or 4)
    capacity: int = DEFAULT_CAPACITY
    read_root: Optional[str] = None
    write_root: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class MeshConfig:
    fixer_host: str = "localhost"
    fixer_port: int = DEFAULT_PORT
    # All Fixer TCP ports the mesh uses (persistent service + campaign Fixers).
    # Drives `kiroshi firewall install` so opening one port never closes another.
    # Defaults to [fixer_port] when [fixer].ports is absent.
    fixer_ports: list = field(default_factory=list)
    read_root: Optional[str] = None
    write_root: Optional[str] = None
    hosts: dict[str, HostConfig] = field(default_factory=dict)
    # Storage topology for shard-aware I/O scheduling (PLAN §7.6). Empty list =
    # inert (no [[storage.disk]] config -> plain work-stealing, no per-disk budget).
    disks: list = field(default_factory=list)

    @property
    def fixer_url(self) -> str:
        return f"http://{self.fixer_host}:{self.fixer_port}"

    def host(self, name: Optional[str] = None) -> HostConfig:
        """Resolve config for ``name`` (default: this machine), case-insensitive,
        falling back to a ``_DEFAULT`` entry then to baked defaults."""
        name = name or current_host()
        if name in self.hosts:
            return self.hosts[name]
        for k, v in self.hosts.items():
            if k.lower() == name.lower():
                return v
        fallback = self.hosts.get("_DEFAULT", HostConfig(name=name))
        # inherit mesh-level roots if host didn't specify its own
        if fallback.read_root is None:
            fallback.read_root = self.read_root
        if fallback.write_root is None:
            fallback.write_root = self.write_root
        return fallback


def _find_config(path: Optional[str]) -> Optional[Path]:
    candidates = [
        path,
        os.environ.get("KIROSHI_CONFIG"),
        "kiroshi.local.toml",
        "kiroshi.toml",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


def load_config(path: Optional[str] = None) -> MeshConfig:
    cfg = MeshConfig()

    found = _find_config(path)
    if found and tomllib is not None:
        with open(found, "rb") as f:
            data = tomllib.load(f)
        fixer = data.get("fixer", {})
        cfg.fixer_host = fixer.get("host", cfg.fixer_host)
        cfg.fixer_port = int(fixer.get("port", cfg.fixer_port))
        if fixer.get("ports"):
            cfg.fixer_ports = [int(p) for p in fixer["ports"]]
        paths = data.get("paths", {})
        cfg.read_root = paths.get("read_root", cfg.read_root)
        cfg.write_root = paths.get("write_root", cfg.write_root)
        for name, hc in (data.get("hosts", {}) or {}).items():
            cfg.hosts[name] = HostConfig(
                name=name,
                python=hc.get("python"),
                workers=int(hc.get("workers", os.cpu_count() or 4)),
                capacity=int(hc.get("capacity", DEFAULT_CAPACITY)),
                read_root=hc.get("read_root"),
                write_root=hc.get("write_root"),
                extra={k: v for k, v in hc.items()
                       if k not in {"python", "workers", "capacity", "read_root", "write_root"}},
            )
        # Storage topology: [[storage.disk]] sections (opt-in NAS sharding).
        from .storage import DiskConfig

        for d in (data.get("storage", {}).get("disk", []) or []):
            cfg.disks.append(DiskConfig(
                id=d.get("id") or d.get("match") or "disk",
                kind=d.get("kind", "hdd"),
                read=d.get("read"),
                write=d.get("write"),
                match=d.get("match", ""),
                concurrency=d.get("concurrency"),
                parity_protected=d.get("parity_protected", False),
                write_concurrency=d.get("write_concurrency"),
                direct_path=d.get("direct_path"),
                cache_tier=d.get("cache_tier"),
                seq_read_mbps=d.get("seq_read_mbps"),
                write_mbps=d.get("write_mbps"),
            ))

    # Environment overrides (highest priority for connection + roots)
    cfg.fixer_host = os.environ.get("KIROSHI_FIXER_HOST", cfg.fixer_host)
    cfg.fixer_port = int(os.environ.get("KIROSHI_FIXER_PORT", cfg.fixer_port))
    cfg.read_root = os.environ.get("KIROSHI_READ_ROOT", cfg.read_root)
    cfg.write_root = os.environ.get("KIROSHI_WRITE_ROOT", cfg.write_root)

    # Normalize the fixer-port set: default to [fixer_port], and always include
    # fixer_port (so an env override of the port is covered by firewall rules).
    if not cfg.fixer_ports:
        cfg.fixer_ports = [cfg.fixer_port]
    elif cfg.fixer_port not in cfg.fixer_ports:
        cfg.fixer_ports = [cfg.fixer_port, *cfg.fixer_ports]
    return cfg
