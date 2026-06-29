"""Tests for the tray autostart module (HKCU\\Run registration).

On Windows these exercise the real registry under HKCU (safe — we clean up
after ourselves). On other platforms the functions are no-ops, so the tests
just verify they don't blow up.
"""
from __future__ import annotations

import sys

import pytest

from kiroshi import autostart

_WIN = sys.platform == "win32"


@pytest.mark.skipif(not _WIN, reason="autostart registry is Windows-only")
def test_autostart_register_unregister_cycle():
    # Clean slate
    autostart.unregister()
    assert autostart.current_registration() is None

    # Register
    outcome = autostart.ensure_registered()
    assert outcome in ("registered", "updated")
    reg = autostart.current_registration()
    assert reg is not None
    assert "kiroshi" in reg.lower() or "tray" in reg.lower()

    # Idempotent: second call is a no-op
    assert autostart.ensure_registered() == "already"

    # Cleanup
    assert autostart.unregister() == "removed"
    assert autostart.current_registration() is None
    # Unregister again is safe
    assert autostart.unregister() == "removed"


@pytest.mark.skipif(_WIN, reason="non-Windows no-op path")
def test_autostart_noop_on_posix():
    assert autostart.ensure_registered() == "already"
    assert autostart.unregister() == "noop"
    assert autostart.current_registration() is None
