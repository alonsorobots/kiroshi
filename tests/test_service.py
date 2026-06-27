"""Tests for the Windows-service (NSSM) install tooling.

These exercise the pure command-builders + discovery + the NAS-credential safety
rule without touching the real Service Control Manager. The only live call is a
read-only `sc query` for a service we know doesn't exist.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiroshi import winservice as ws  # noqa: E402


def test_find_nssm_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "nssm.exe"
    fake.write_bytes(b"\x00")
    monkeypatch.setenv("KIROSHI_NSSM", str(fake))
    assert ws.find_nssm() == str(fake)
    monkeypatch.setenv("KIROSHI_NSSM", str(tmp_path / "does-not-exist.exe"))
    # falls through to PATH/other candidates (likely None in CI)
    assert ws.find_nssm() != str(tmp_path / "does-not-exist.exe")


def test_build_install_commands_shape():
    cmds = ws.build_install_commands(
        nssm="nssm.exe", service_name="kiroshi-fixer", python_exe="py.exe",
        app_parameters="-m kiroshi fixer --port 8787",
        app_directory="C:\\work", log_dir="C:\\logs",
        display_name="Kiroshi Fixer", description="desc",
        account="LocalSystem",
    )
    # first command must be the install
    assert cmds[0] == ["nssm.exe", "install", "kiroshi-fixer", "py.exe"]
    flat = [" ".join(c) for c in cmds]
    assert any("AppParameters -m kiroshi fixer --port 8787" in f for f in flat)
    assert any("Start SERVICE_AUTO_START" in f for f in flat)
    assert any("AppStdout" in f and "kiroshi-fixer.stdout.log" in f for f in flat)
    assert any("AppRotateBytes" in f for f in flat)
    assert any("AppExit Default Restart" in f for f in flat)
    # builtin account => ObjectName WITHOUT a password
    obj = [c for c in cmds if "ObjectName" in c][0]
    assert obj[-1] == "LocalSystem"


def test_build_install_user_account_with_password_and_env():
    cmds = ws.build_install_commands(
        nssm="nssm.exe", service_name="kiroshi-runner", python_exe="py.exe",
        app_parameters="-m kiroshi runner --task t:run",
        app_directory="C:\\work", log_dir="C:\\logs",
        display_name="Kiroshi Runner", description="desc",
        account=".\\me", password="pw",
        env={"KIROSHI_TOKEN": "tok", "KIROSHI_READ_ROOT": "\\\\nas\\share"},
    )
    obj = [c for c in cmds if "ObjectName" in c][0]
    assert obj[-2:] == [".\\me", "pw"]   # password passed alongside the account
    envc = [c for c in cmds if "AppEnvironmentExtra" in c][0]
    assert "KIROSHI_TOKEN=tok" in envc and "KIROSHI_READ_ROOT=\\\\nas\\share" in envc


def test_uninstall_commands():
    cmds = ws.build_uninstall_commands("nssm.exe", "kiroshi-fixer")
    assert cmds == [["nssm.exe", "stop", "kiroshi-fixer"],
                    ["nssm.exe", "remove", "kiroshi-fixer", "confirm"]]


def test_runner_nas_guard():
    # NAS UNC + builtin account => unsafe (must refuse)
    assert ws.runner_needs_user_account("\\\\nas\\share", None, None) is True
    assert ws.runner_needs_user_account(None, "\\\\nas\\share", "LocalSystem") is True
    # NAS + real user account => fine
    assert ws.runner_needs_user_account("\\\\nas\\share", None, ".\\me") is False
    # local paths => fine regardless
    assert ws.runner_needs_user_account("C:\\data", "D:\\out", None) is False


def test_status_not_installed_is_safe():
    out = ws.status("kiroshi-definitely-not-a-real-service-xyz")
    assert "kiroshi-definitely-not-a-real-service-xyz" in out
