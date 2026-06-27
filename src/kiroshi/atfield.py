"""at-field awareness — let a Runner back off while the rig is being protected.

at-field (the sibling GPU/thermal watchdog) signals "stop hammering this box" by
writing a ``pause.sentinel`` file in its state dir whose first line is an ISO-8601
expiry timestamp (an empty file means an indefinite pause). When that sentinel is
active, a polite Runner should stop leasing new work and let the rig cool/recover
rather than getting its whole process tree killed.

This is read-only and entirely optional: if at-field isn't installed, the sentinel
never exists and ``pause_state()`` always reports "not paused".
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple


def _state_dir() -> Optional[Path]:
    base = os.environ.get("ATFIELD_STATE_DIR")
    if base:
        return Path(base)
    if sys.platform == "win32":
        pd = os.environ.get("PROGRAMDATA")
        if pd:
            return Path(pd) / "ATField"
    return None


def pause_sentinel_path() -> Optional[Path]:
    d = _state_dir()
    return (d / "pause.sentinel") if d else None


def pause_state() -> Tuple[bool, Optional[str]]:
    """Return ``(paused, until_iso)``. ``paused`` is False if at-field isn't present."""
    p = pause_sentinel_path()
    if p is None or not p.exists():
        return (False, None)
    try:
        first = p.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return (False, None)
    if not first or not first[0].strip():
        return (True, None)  # empty sentinel => indefinite pause
    iso = first[0].strip()
    try:
        expiry = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) < expiry, iso)
    except ValueError:
        # Unparseable but present: treat as paused (fail safe / be polite).
        return (True, iso)


def is_paused() -> bool:
    return pause_state()[0]
