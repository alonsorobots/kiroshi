"""Tests for the scheduled-task tray autostart.

Focuses on the PURE XML builder (``_tray_task_xml``) so tests work on any
platform without touching the actual Task Scheduler. The XML has to survive
Windows' schtasks import — that means:

  * well-formed XML,
  * ``<LogonTrigger>`` present (so it fires at logon),
  * ``<RestartOnFailure>`` with ``Interval=PT1M`` and a count (so a killed
    tray comes back within a minute — the whole point of the feature),
  * ``<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>`` (never
    double-launch), and
  * ``<LogonType>InteractiveToken</LogonType>`` (runs in the logged-on
    session so it has a desktop + SMB creds).

If any of those drift, restart-on-failure silently breaks — which is exactly
the class of regression this test file exists to catch.
"""
from __future__ import annotations

import sys
from pathlib import Path
from xml.dom import minidom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.autostart import _tray_task_xml  # noqa: E402  (pure fn, no I/O)


CMD = r'"C:\Program Files\Python\pythonw.exe" -m kiroshi tray'


def _xml(user: str = "TESTDOM\\alice", name: str = "KiroshiTray") -> str:
    return _tray_task_xml(CMD, user=user, task_name=name)


def test_xml_is_wellformed():
    # If schtasks can't parse this we can't schedule anything.
    minidom.parseString(_xml())


def test_xml_carries_restart_on_failure_within_one_minute():
    xml = _xml()
    assert "<RestartOnFailure>" in xml
    assert "<Interval>PT1M</Interval>" in xml, "restart cadence must be PT1M"
    assert "<Count>" in xml, "restart count required (else Windows only tries once)"


def test_xml_uses_logon_trigger_for_the_current_user():
    xml = _xml(user="MYPC\\bob")
    assert "<LogonTrigger>" in xml
    assert "<UserId>MYPC\\bob</UserId>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml, \
        "must run in the interactive session (desktop + SMB creds)"


def test_xml_prevents_double_launch():
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in _xml()


def test_xml_no_time_limit_and_survives_battery():
    xml = _xml()
    # tray is long-running; ExecutionTimeLimit PT0S = no cap.
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml


def test_xml_splits_command_into_exec_and_arguments():
    xml = _xml()
    assert "<Command>C:\\Program Files\\Python\\pythonw.exe</Command>" in xml
    assert "<Arguments>-m kiroshi tray</Arguments>" in xml


def test_xml_escapes_specials_in_user_and_command():
    # An attacker-controlled or unusual user string should never inject XML.
    xml = _tray_task_xml(CMD, user="DOM<x>&y")
    assert "<x>" not in xml.replace("<Command>", "").replace("<Arguments>", "")
    assert "&lt;x&gt;" in xml and "&amp;y" in xml


def test_xml_task_name_appears_in_uri():
    xml = _xml(name="KiroshiTrayAlt")
    assert "<URI>\\KiroshiTrayAlt</URI>" in xml


def test_empty_command_rejected():
    try:
        _tray_task_xml("")
    except ValueError:
        return
    raise AssertionError("empty command should raise ValueError")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fail = 0
    for t in tests:
        try:
            t(); print(f"PASS  {t.__name__}")
        except Exception as exc:
            print(f"FAIL  {t.__name__}: {exc!r}"); fail += 1
    print(f"\n{len(tests)-fail}/{len(tests)} passed")
    sys.exit(fail)
