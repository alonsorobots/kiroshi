"""Tests for kiroshi.remote_sync — the mesh code-sync planner + executor.

The planner is I/O-free; the executor is thin ssh glue with an injectable
callable so we test the whole flow without touching a real network."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi import remote_sync as rs  # noqa: E402


@dataclass
class _HC:
    python: Optional[str] = None


HOSTS = {
    "_DEFAULT": _HC(),
    "host-local": _HC(python="C:/kiroshi/.venv/Scripts/python.exe"),
    "host-a": _HC(python="D:/py/python.exe"),
    "host-b":  _HC(),                        # no python override
}


def test_plan_skips_default_and_local_host():
    plans = rs.plan_sync(HOSTS, local_hostnames=("host-local",))
    by = {p.host: p for p in plans}
    assert "_DEFAULT" not in by, "_DEFAULT is a config fallback, not a host"
    assert by["host-local"].skipped, "local host must be skipped, not sshed into"
    assert "local host" in by["host-local"].skip_reason
    assert not by["host-a"].skipped and not by["host-b"].skipped


def test_plan_uses_ff_only_pulls():
    plans = rs.plan_sync(HOSTS, repos=("/opt/kiroshi",), local_hostnames=("host-local",))
    hosta = next(p for p in plans if p.host == "host-a")
    pull = next(s for s in hosta.steps if s.kind == "pull")
    assert "/opt/kiroshi" in pull.remote_cmd
    assert "pull --ff-only" in pull.remote_cmd, \
        "ff-only refuses to merge silently on a diverged remote — required"
    assert "git -C" in pull.remote_cmd, "must use -C so remote cwd doesn't matter"


def test_plan_quotes_paths_with_spaces():
    # A path with a space MUST be shell-safely quoted. We use double quotes
    # (not POSIX single quotes) because they are honoured by both POSIX sh and
    # the Windows cmd.exe that OpenSSH launches on a Windows remote.
    plans = rs.plan_sync(HOSTS, repos=("/opt/my repos/kiroshi",),
                         local_hostnames=("host-local",))
    hosta = next(p for p in plans if p.host == "host-a")
    pull = next(s for s in hosta.steps if s.kind == "pull")
    assert '"/opt/my repos/kiroshi"' in pull.remote_cmd, \
        "path with spaces must be shell-quoted"


def test_plan_windows_repo_path_is_cmd_safe():
    # Regression: a Windows backslash path must NOT be POSIX single-quoted
    # (shlex.quote did this) — cmd.exe passes single quotes through literally,
    # so `git -C 'C:\...'` fails with "cannot change to ''C:\\...''". The path
    # must be forward-slashed + double-quoted so both cmd.exe and sh accept it.
    win = r"C:\Users\admin\Desktop\RESEARCH\kiroshi"
    plans = rs.plan_sync(HOSTS, repos=(win,), local_hostnames=("host-local",))
    hosta = next(p for p in plans if p.host == "host-a")
    pull = next(s for s in hosta.steps if s.kind == "pull")
    assert "'" not in pull.remote_cmd, "must not emit POSIX single-quotes for cmd.exe"
    assert '"C:/Users/admin/Desktop/RESEARCH/kiroshi"' in pull.remote_cmd
    assert "\\" not in pull.remote_cmd, "backslashes must be normalized to /"


def test_reinstall_uses_host_python_when_provided():
    plans = rs.plan_sync(HOSTS, repos=("/opt/kiroshi",), reinstall=True,
                         local_hostnames=("host-local",))
    hosta = next(p for p in plans if p.host == "host-a")
    ri = next(s for s in hosta.steps if s.kind == "reinstall")
    assert "D:/py/python.exe" in ri.remote_cmd
    assert "-m pip install" in ri.remote_cmd
    hostb = next(p for p in plans if p.host == "host-b")
    ri = next(s for s in hostb.steps if s.kind == "reinstall")
    assert ri.remote_cmd.startswith("python -m pip"), \
        "fall back to bare 'python' when host has no --python configured"


def test_no_restart_by_default():
    plans = rs.plan_sync(HOSTS, local_hostnames=("host-local",))
    for p in plans:
        if p.skipped:
            continue
        assert not any(s.kind == "restart" for s in p.steps), \
            "restart must be opt-in — never signal runners without the operator asking"


def test_restart_opt_in_adds_stop_signal():
    plans = rs.plan_sync(HOSTS, restart=True, local_hostnames=("host-local",))
    hosta = next(p for p in plans if p.host == "host-a")
    steps = [s for s in hosta.steps if s.kind == "restart"]
    assert steps and "kiroshi stop" in steps[0].remote_cmd


def test_render_plan_is_readable_and_shows_ssh_command():
    plans = rs.plan_sync(HOSTS, repos=("/opt/kiroshi",), local_hostnames=("host-local",))
    rendered = rs.render_plan(plans)
    assert "[host-local]" in rendered and "SKIP" in rendered
    assert "[host-a]" in rendered
    assert "ssh host-a" in rendered and "pull --ff-only" in rendered
    assert "/opt/kiroshi" in rendered


def test_execute_dry_run_never_calls_ssh():
    calls: list[str] = []
    def fake_ssh(host, cmd, timeout=120):
        calls.append(f"{host}:{cmd}")
        return 0, "", ""
    plans = rs.plan_sync(HOSTS, repos=("/opt/kiroshi",), local_hostnames=("host-local",))
    rs.execute_plan(plans, dry_run=True, ssh=fake_ssh, out=lambda _m: None)
    assert calls == [], "dry-run must never invoke ssh"


def test_execute_real_run_calls_ssh_per_step():
    calls: list[tuple[str, str]] = []
    def fake_ssh(host, cmd, timeout=120):
        calls.append((host, cmd))
        return 0, "Already up to date.", ""
    plans = rs.plan_sync(HOSTS, repos=("/opt/kiroshi", "/opt/pose"),
                         local_hostnames=("host-local",))
    fails = rs.execute_plan(plans, dry_run=False, ssh=fake_ssh, out=lambda _m: None)
    # 2 non-local hosts x 2 pull steps
    assert fails == 0
    assert len(calls) == 4
    assert all("pull --ff-only" in c for _, c in calls)


def test_execute_reports_failure_count():
    def fake_ssh(host, cmd, timeout=120):
        return (1, "", "fatal: not a git repo") if "host-a" in host else (0, "", "")
    plans = rs.plan_sync(HOSTS, repos=("/opt/kiroshi",), local_hostnames=("host-local",))
    fails = rs.execute_plan(plans, dry_run=False, ssh=fake_ssh, out=lambda _m: None)
    assert fails == 1


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
