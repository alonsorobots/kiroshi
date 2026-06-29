"""User-mode autostart for the Kiroshi tray.

Registers ``kiroshi tray`` in ``HKCU\\Software\\Microsoft\\Windows\\
CurrentVersion\\Run`` so it launches automatically when the current user
logs in — mirroring at-field's tray autostart pattern.

Per-user (HKCU) instead of per-machine (HKLM) by design:

  * No UAC elevation required — HKCU is writable by the current user.
  * The tray is a personal lens onto the mesh. The Fixer itself is a
    LocalSystem service that's already auto-starting via NSSM on boot,
    regardless of whether anyone has logged in. The tray is purely a UI
    affordance for whoever's at the keyboard.
  * Other utilities (Slack, Discord, Spotify) use the same key for the
    same reason; users who want to disable it can do so from Task Manager
    → Startup or ``msconfig``.

This module is Windows-only. On other platforms its public functions are
no-ops so the rest of the codebase can call them unconditionally.
"""
from __future__ import annotations

import sys
from typing import Optional

VALUE_NAME = "Kiroshi Tray"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


if sys.platform == "win32":
    import winreg

    def _tray_command() -> str:
        """The command line to register: ``"<python>" -m kiroshi tray``.

        Uses the current interpreter so an editable/venv install keeps
        working. Quotes paths with spaces (common for venvs under
        ``Program Files`` or user profiles).
        """
        exe = sys.executable
        cmd = f'"{exe}" -m kiroshi tray'
        return cmd

    def ensure_registered() -> str:
        """Idempotently register the tray for user-mode autostart.

        Returns ``"registered"`` if a new entry was written,
        ``"already"`` if the existing entry already points at this
        interpreter (no-op), or ``"updated"`` if a stale entry was
        overwritten. Never raises — autostart is best-effort; the Fixer
        service runs regardless.
        """
        cmd = _tray_command()
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                                 0, winreg.KEY_READ)
            try:
                existing, _ = winreg.QueryValueEx(key, VALUE_NAME)
            except FileNotFoundError:
                existing = None
            finally:
                key.Close()
        except OSError:
            existing = None

        if existing == cmd:
            return "already"

        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                                 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
            key.Close()
        except OSError:
            return "failed"
        return "updated" if existing else "registered"

    def unregister() -> str:
        """Remove the autostart entry. Idempotent — missing is OK."""
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                                 0, winreg.KEY_SET_VALUE)
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
            finally:
                key.Close()
        except OSError:
            return "failed"
        return "removed"

    def current_registration() -> Optional[str]:
        """Return the stored command string, or ``None`` if not registered."""
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                                 0, winreg.KEY_READ)
            try:
                val, _ = winreg.QueryValueEx(key, VALUE_NAME)
                return val
            except FileNotFoundError:
                return None
            finally:
                key.Close()
        except OSError:
            return None

else:  # pragma: no cover — non-Windows

    def ensure_registered() -> str:
        return "already"

    def unregister() -> str:
        return "noop"

    def current_registration() -> Optional[str]:
        return None
