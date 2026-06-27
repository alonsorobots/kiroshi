"""Per-machine state directory for Kiroshi (token, process registry, logs).

A single well-known, per-user location so the token, the process manifest, and
log files all live together and are discoverable by the tray, the CLI, and an
external watchdog (at-field):

    Windows : %PROGRAMDATA%\\Kiroshi   (falls back to %LOCALAPPDATA%\\Kiroshi)
    POSIX   : $XDG_STATE_HOME/kiroshi  (falls back to ~/.kiroshi)

Override with ``KIROSHI_STATE_DIR``. Subdirectories: ``logs/``, ``registry/``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def state_dir() -> Path:
    override = os.environ.get("KIROSHI_STATE_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        root = os.environ.get("PROGRAMDATA") or os.environ.get("LOCALAPPDATA") \
            or os.path.expanduser("~")
        base = Path(root) / "Kiroshi"
    else:
        root = os.environ.get("XDG_STATE_HOME")
        base = Path(root) / "kiroshi" if root else Path(os.path.expanduser("~/.kiroshi"))
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


def logs_dir() -> Path:
    d = state_dir() / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def registry_dir() -> Path:
    d = state_dir() / "registry"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d
