"""User-mode autostart for the Kiroshi tray.

TWO registration paths, both per-user (no admin required):

  * ``run`` — legacy ``HKCU\\...\\Run`` registry entry. Fires at **logon only**.
    If the tray dies mid-session (crash, dev killed the process, an update
    was applied), nothing restarts it until the next logon.
  * ``scheduled`` — Windows Task Scheduler with **restart-on-failure**. Fires
    at logon *and* self-heals within ~1 minute if the tray process exits or
    is killed. Recommended default on Win10+; use ``run`` for locked-down
    machines where scheduled tasks are disallowed.

The in-process fragility that historically killed the tray icon (bare
``print`` under ``pythonw``, unhandled callback exceptions escaping into the
Win32 message loop) is already handled by ``tray._guard`` + ``tray._log``.
The remaining failure mode is *supervision* — that's what ``scheduled`` fixes.

Per-user (HKCU / current-user task) instead of per-machine by design: no UAC,
runs in the interactive session (SMB creds + desktop available to the tray),
and the Coordinator service itself already covers "runs regardless of logon" via
NSSM. This module is Windows-only; non-Windows public functions are no-ops.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from typing import Optional

VALUE_NAME = "Kiroshi Tray"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TASK_NAME = "KiroshiTray"


# --- pure XML builder (unit-testable, no I/O) ----------------------------

def _tray_task_xml(command: str, user: str = "",
                   task_name: str = TASK_NAME) -> str:
    """Build the Task Scheduler XML for the tray task.

    Kept as a pure function so tests can assert the XML shape without
    touching the actual scheduler. ``command`` is the full command line
    (e.g. ``"C:\\...\\pythonw.exe" -m kiroshi tray``) which is split into an
    executable path + arguments per the ``<Exec>`` schema. ``user`` defaults
    to ``%USERDOMAIN%\\%USERNAME%`` when empty (interpolation happens in the
    schtasks import step; the XML just carries the literal string we pass).

    Semantics baked in:
      * ``LogonTrigger`` for the given user — same trigger point as HKCU\\Run.
      * ``RestartOnFailure`` count=999, interval=PT1M — self-heals within a
        minute after any crash/kill for the lifetime of the session.
      * ``MultipleInstancesPolicy=IgnoreNew`` — never double-launch.
      * ``LogonType=InteractiveToken`` — runs in the logged-on session so
        the tray sees the desktop and inherits the user's SMB credentials.
      * ``ExecutionTimeLimit=PT0S`` — no time cap (Windows convention for
        long-running background tasks).
    """
    parts = shlex.split(command, posix=False)
    if not parts:
        raise ValueError("command is empty")
    exe = parts[0].strip('"')
    args = " ".join(parts[1:])
    user = user or f"%USERDOMAIN%\\%USERNAME%"
    # &, <, > must be XML-escaped in embedded fields
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <RegistrationInfo>\n'
        f'    <Description>Kiroshi tray (auto-restart on failure)</Description>\n'
        f'    <URI>\\{esc(task_name)}</URI>\n'
        '  </RegistrationInfo>\n'
        '  <Triggers>\n'
        '    <LogonTrigger>\n'
        '      <Enabled>true</Enabled>\n'
        f'      <UserId>{esc(user)}</UserId>\n'
        '    </LogonTrigger>\n'
        '  </Triggers>\n'
        '  <Principals>\n'
        '    <Principal id="Author">\n'
        f'      <UserId>{esc(user)}</UserId>\n'
        '      <LogonType>InteractiveToken</LogonType>\n'
        '      <RunLevel>LeastPrivilege</RunLevel>\n'
        '    </Principal>\n'
        '  </Principals>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <AllowHardTerminate>true</AllowHardTerminate>\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\n'
        '    <AllowStartOnDemand>true</AllowStartOnDemand>\n'
        '    <Enabled>true</Enabled>\n'
        '    <Hidden>false</Hidden>\n'
        '    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n'
        '    <WakeToRun>false</WakeToRun>\n'
        '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n'
        '    <Priority>7</Priority>\n'
        '    <RestartOnFailure>\n'
        '      <Interval>PT1M</Interval>\n'
        '      <Count>999</Count>\n'
        '    </RestartOnFailure>\n'
        '  </Settings>\n'
        '  <Actions Context="Author">\n'
        '    <Exec>\n'
        f'      <Command>{esc(exe)}</Command>\n'
        f'      <Arguments>{esc(args)}</Arguments>\n'
        '    </Exec>\n'
        '  </Actions>\n'
        '</Task>\n'
    )


if sys.platform == "win32":
    import winreg

    def _tray_command() -> str:
        """The command line to register: ``"<pythonw>" -m kiroshi tray``.

        Uses the current interpreter so an editable/venv install keeps
        working. Quotes paths with spaces (common for venvs under
        ``Program Files`` or user profiles). Prefers the windowless
        ``pythonw.exe`` beside ``python.exe`` (same dir) so the tray launches
        silently on login without popping a console window.
        """
        exe = sys.executable
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            exe = cand
        return f'"{exe}" -m kiroshi tray'

    def ensure_registered() -> str:
        """Idempotently register the tray for user-mode autostart.

        Returns ``"registered"`` if a new entry was written,
        ``"already"`` if the existing entry already points at this
        interpreter (no-op), or ``"updated"`` if a stale entry was
        overwritten. Never raises — autostart is best-effort; the Coordinator
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

    # ---- scheduled-task path (restart-on-failure) ----------------------
    def _current_user() -> str:
        dom = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
        usr = os.environ.get("USERNAME") or ""
        return f"{dom}\\{usr}" if dom and usr else (usr or "")

    def _schtasks_query(task_name: str) -> Optional[str]:
        """Return the raw XML of a scheduled task, or None if it doesn't exist."""
        try:
            r = subprocess.run(
                ["schtasks", "/query", "/tn", task_name, "/xml"],
                capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    def ensure_scheduled(task_name: str = TASK_NAME) -> str:
        """Idempotently register the tray as a Windows scheduled task with
        restart-on-failure. Runs in the interactive user session (SMB creds +
        desktop). Returns ``"registered"`` / ``"already"`` / ``"updated"`` /
        ``"failed"``.

        Not raising is a deliberate policy — autostart is best-effort; the
        rest of Kiroshi runs regardless.
        """
        cmd = _tray_command()
        want_xml = _tray_task_xml(cmd, user=_current_user(), task_name=task_name)
        have_xml = _schtasks_query(task_name)
        # crude but effective comparison — the schtasks-returned XML gets
        # normalized by Windows (whitespace, attribute order) so we compare on
        # the fields we actually control.
        def _key_fields(xml: Optional[str]) -> tuple:
            if not xml:
                return ()
            def _pick(tag: str) -> str:
                s = f"<{tag}>"
                e = f"</{tag}>"
                i = xml.find(s)
                if i < 0:
                    return ""
                j = xml.find(e, i)
                return xml[i + len(s):j] if j > 0 else ""
            return (_pick("Command"), _pick("Arguments"),
                    _pick("Interval"), _pick("Count"))
        already = have_xml is not None and _key_fields(have_xml) == _key_fields(want_xml)
        if already:
            return "already"

        # write UTF-16-LE with BOM (schtasks /xml requires this)
        fd, tmp = tempfile.mkstemp(prefix="kiroshi_tray_task_", suffix=".xml")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"\xff\xfe")                   # UTF-16 LE BOM
                fh.write(want_xml.encode("utf-16-le"))
            args = ["schtasks", "/create", "/tn", task_name, "/xml", tmp, "/f"]
            try:
                r = subprocess.run(args, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                return "failed"
            if r.returncode != 0:
                return "failed"
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return "updated" if have_xml else "registered"

    def unregister_scheduled(task_name: str = TASK_NAME) -> str:
        """Remove the scheduled task. Idempotent — missing is OK."""
        try:
            r = subprocess.run(
                ["schtasks", "/delete", "/tn", task_name, "/f"],
                capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return "failed"
        if r.returncode == 0:
            return "removed"
        # schtasks returns non-zero when the task doesn't exist; treat as ok
        if "cannot find" in (r.stderr or "").lower() or "not found" in (r.stderr or "").lower():
            return "already"
        return "failed"

    def current_scheduled(task_name: str = TASK_NAME) -> Optional[str]:
        """Return the ``<Command>`` field of the registered scheduled task,
        or ``None`` if not registered."""
        xml = _schtasks_query(task_name)
        if not xml:
            return None
        i, e = xml.find("<Command>"), xml.find("</Command>")
        return xml[i + len("<Command>"):e] if 0 <= i < e else None


else:  # pragma: no cover — non-Windows

    def ensure_registered() -> str:
        return "already"

    def unregister() -> str:
        return "noop"

    def current_registration() -> Optional[str]:
        return None

    def ensure_scheduled(task_name: str = TASK_NAME) -> str:
        return "already"

    def unregister_scheduled(task_name: str = TASK_NAME) -> str:
        return "noop"

    def current_scheduled(task_name: str = TASK_NAME) -> Optional[str]:
        return None
