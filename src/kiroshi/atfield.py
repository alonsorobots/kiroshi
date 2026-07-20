"""at-field awareness — let a Runner back off while the rig is being protected
or while a human is actively using it.

at-field (the sibling GPU/thermal watchdog) exposes two SEPARATE sentinel
files -- deliberately not one, see at-field's own ``service.py`` comment
for why they must never be conflated:

* ``pause.sentinel`` -- at-field's own self-pause (``atf pause``/``atf
  unpause``), which suppresses AT-FIELD'S kill rules. First line is an
  ISO-8601 expiry timestamp; an empty file means an indefinite pause.
* ``presence.sentinel`` -- a read-only-by-consumers signal that a human is
  currently at the keyboard/mouse (input-idle time below at-field's
  configured threshold). Existence alone is the signal; no expiry, since
  presence is continuously re-evaluated every tick, not scheduled.

Both are read-only and entirely optional here: if at-field isn't installed,
neither sentinel ever exists and both state functions report "no".
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


def presence_sentinel_path() -> Optional[Path]:
    d = _state_dir()
    return (d / "presence.sentinel") if d else None


def is_present() -> bool:
    """True if at-field reports a human is currently at this machine.

    Unlike :func:`is_paused`, absence of at-field (or of the sentinel) means
    "not present" (False) -- there is no fail-safe direction to prefer here
    the way there is for pause (where "unparseable => paused" errs toward
    politeness); the honest default when we don't know is "assume away."
    """
    p = presence_sentinel_path()
    return p is not None and p.exists()
