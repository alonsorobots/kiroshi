"""Unit tests for kiroshi.stopctl (shared stop / force-kill logic)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.stopctl import stop_registered


def test_force_kill_calls_terminate_tree_not_drain():
    fake = [{"role": "runner", "pid": 42, "hostname": "", "launch_command": "k"}]
    mock_stop = MagicMock(return_value=True)
    mock_kill = MagicMock(return_value=True)
    with patch("kiroshi.stopctl.list_registered", return_value=fake):
        with patch("kiroshi.stopctl.request_stop", mock_stop):
            with patch("kiroshi.stopctl.terminate_tree", mock_kill):
                result = stop_registered(role="runner", force=True)
    assert result["killed"] == 1
    assert result["stopped"] == 0
    assert result["force"] is True
    mock_stop.assert_not_called()
    mock_kill.assert_called_once_with(42)


def test_graceful_stop_requests_drain_without_waiting():
    fake = [{"role": "runner", "pid": 7, "hostname": "", "launch_command": "k"}]
    mock_stop = MagicMock(return_value=True)
    with patch("kiroshi.stopctl.list_registered", return_value=fake):
        with patch("kiroshi.stopctl.request_stop", mock_stop):
            result = stop_registered(role="runner", grace=0.0)
    assert result["stopped"] == 1
    assert result["killed"] == 0
    mock_stop.assert_called_once_with("runner", 7)


def test_ambiguous_requires_all_or_pid():
    fake = [
        {"role": "runner", "pid": 1, "launch_command": "a"},
        {"role": "runner", "pid": 2, "launch_command": "b"},
    ]
    with patch("kiroshi.stopctl.list_registered", return_value=fake):
        result = stop_registered(role="runner")
    assert result["ambiguous"] is True
    assert result["exit_code"] == 1


if __name__ == "__main__":
    for name in sorted(n for n in globals() if n.startswith("test_")):
        globals()[name]()
        print(f"PASS  {name}")
