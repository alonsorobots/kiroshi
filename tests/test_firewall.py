"""Tests for src/kiroshi/firewall.py — pure planning + netsh interaction.

The netsh subprocess is fully injected via the ``runner`` parameter so no
Windows APIs are hit and tests run on any platform.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

import pytest

from kiroshi import firewall as fw


# ------------------------------------------------------------- fake netsh
@dataclass
class _FakeResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class _FakeNetsh:
    """Records every netsh invocation and returns scripted replies.

    - Any ``show rule name=all`` call returns the current ``show_stdout``.
    - ``delete rule`` and ``add rule`` succeed by default (rc=0) and mutate
      the in-memory rule name set so a follow-up ``show`` reflects them.
    """

    def __init__(self, initial_rules: list[str] | None = None):
        self.calls: list[list[str]] = []
        self.rules: set[str] = set(initial_rules or [])
        self.fail_next_add = False
        self.fail_delete_of: set[str] = set()

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        # argv is ["netsh", "advfirewall", "firewall", <op>, "rule", ...]
        if len(argv) >= 4 and argv[3] == "show":
            body = "\n".join(f"Rule Name:                            {n}"
                             for n in sorted(self.rules))
            return _FakeResult(0, stdout=body)  # type: ignore[return-value]
        if len(argv) >= 4 and argv[3] == "delete":
            name = _extract_name(argv)
            if name in self.fail_delete_of:
                return _FakeResult(1, stderr="Access denied.")  # type: ignore[return-value]
            self.rules.discard(name)
            return _FakeResult(0)  # type: ignore[return-value]
        if len(argv) >= 4 and argv[3] == "add":
            if self.fail_next_add:
                self.fail_next_add = False
                return _FakeResult(1, stderr="rule exists?")  # type: ignore[return-value]
            name = _extract_name(argv)
            self.rules.add(name)
            return _FakeResult(0)  # type: ignore[return-value]
        return _FakeResult(0)  # type: ignore[return-value]


def _extract_name(argv: list[str]) -> str:
    for a in argv:
        if a.startswith("name="):
            return a[len("name="):]
    return ""


# ---------------------------------------------------------- planning tests
def test_plan_rules_yields_exactly_two_rules_with_expected_ports():
    rules = fw.plan_rules(8787, 8788, remote_ip="192.168.1.0/24")
    assert len(rules) == 2
    tcp = next(r for r in rules if r.protocol == "TCP")
    udp = next(r for r in rules if r.protocol == "UDP")
    assert tcp.name == fw.FIXER_RULE_NAME
    assert tcp.port == 8787
    assert tcp.remote_ip == "192.168.1.0/24"
    assert udp.name == fw.DISCOVERY_RULE_NAME
    assert udp.port == 8788


def test_plan_rules_defaults_remote_ip_any():
    rules = fw.plan_rules(9000)
    assert all(r.remote_ip == "any" for r in rules)


def test_plan_rules_multi_port_yields_per_port_suffixed_rules():
    rules = fw.plan_rules([8787, 8800, 8801], 8788, remote_ip="192.168.1.0/24")
    tcp = [r for r in rules if r.protocol == "TCP"]
    udp = [r for r in rules if r.protocol == "UDP"]
    assert len(tcp) == 3 and len(udp) == 1
    names = {r.name: r.port for r in tcp}
    assert names == {
        f"{fw.FIXER_RULE_NAME} 8787": 8787,
        f"{fw.FIXER_RULE_NAME} 8800": 8800,
        f"{fw.FIXER_RULE_NAME} 8801": 8801,
    }
    assert all(r.remote_ip == "192.168.1.0/24" for r in rules)


def test_plan_rules_multi_port_dedups_and_preserves_order():
    rules = fw.plan_rules([8800, 8800, 8787], 8788)
    tcp_ports = [r.port for r in rules if r.protocol == "TCP"]
    assert tcp_ports == [8800, 8787]


def test_plan_rules_single_element_list_keeps_unsuffixed_name():
    # A one-element iterable must behave like the scalar form (back-compat).
    rules = fw.plan_rules([8787], 8788)
    tcp = next(r for r in rules if r.protocol == "TCP")
    assert tcp.name == fw.FIXER_RULE_NAME
    assert tcp.port == 8787


def test_firewall_rule_netsh_add_args_contain_all_required_params():
    r = fw.FirewallRule("Kiroshi X", "TCP", 8787, remote_ip="10.0.0.0/24")
    args = r.netsh_add_args()
    assert args[:3] == ["advfirewall", "firewall", "add"]
    joined = " ".join(args)
    assert "name=Kiroshi X" in joined
    assert "protocol=TCP" in joined
    assert "localport=8787" in joined
    assert "remoteip=10.0.0.0/24" in joined
    assert "dir=in" in joined
    assert "action=allow" in joined


# ------------------------------------------------- subnet detection tests
def test_pick_lan_subnet_returns_slash24_for_rfc1918_ip():
    assert fw.pick_lan_subnet(["192.168.1.166"]) == "192.168.1.0/24"
    assert fw.pick_lan_subnet(["10.0.5.7"]) == "10.0.5.0/24"
    assert fw.pick_lan_subnet(["172.16.100.1"]) == "172.16.100.0/24"


def test_pick_lan_subnet_ignores_public_ips_and_returns_none():
    assert fw.pick_lan_subnet(["8.8.8.8"]) is None


def test_pick_lan_subnet_prefers_first_private_when_mixed():
    assert fw.pick_lan_subnet(["8.8.8.8", "192.168.1.42"]) == "192.168.1.0/24"


def test_pick_lan_subnet_prefers_real_lan_over_wsl_virtual_switch():
    """On a WSL-enabled Windows box, the WSL bridge shows up on 172.25.x.x
    while the real LAN NIC is on 192.168.x.x. Never pick the WSL subnet if a
    real LAN address exists."""
    assert fw.pick_lan_subnet(["172.25.96.1", "192.168.1.166"]) \
        == "192.168.1.0/24"


def test_pick_lan_subnet_falls_back_to_virtual_range_if_no_real_lan():
    """If the ONLY private IP is in a container/WSL range, use it rather than
    returning None — it's still better than allow-any."""
    assert fw.pick_lan_subnet(["172.25.96.1"]) == "172.25.96.0/24"


# ------------------------------------------------ enumeration + apply
def test_list_kiroshi_rules_filters_by_prefix_and_dedups():
    fake = _FakeNetsh(initial_rules=[
        "Kiroshi Fixer HTTP", "Kiroshi Discovery UDP", "Something Else",
        "Kiroshi Fixer HTTP",  # dedupped
    ])
    names = fw.list_kiroshi_rules(runner=fake)
    assert names == ["Kiroshi Discovery UDP", "Kiroshi Fixer HTTP"]


def test_apply_rules_creates_desired_rules_when_none_exist():
    fake = _FakeNetsh()
    rules = fw.plan_rules(8787, 8788, remote_ip="192.168.1.0/24")
    res = fw.apply_rules(rules, runner=fake)
    assert res.ok
    assert set(res.added) == {fw.FIXER_RULE_NAME, fw.DISCOVERY_RULE_NAME}
    assert res.removed == []
    # Both rules should now live in the fake registry.
    assert fw.FIXER_RULE_NAME in fake.rules
    assert fw.DISCOVERY_RULE_NAME in fake.rules


def test_apply_rules_removes_stale_kiroshi_rules_not_in_desired_set():
    fake = _FakeNetsh(initial_rules=["Kiroshi Fixer 8800", "Kiroshi Old Beacon"])
    rules = fw.plan_rules(8787, 8788, remote_ip="any")
    res = fw.apply_rules(rules, runner=fake)
    assert res.ok
    assert set(res.removed) == {"Kiroshi Fixer 8800", "Kiroshi Old Beacon"}
    assert set(res.added) == {fw.FIXER_RULE_NAME, fw.DISCOVERY_RULE_NAME}


def test_apply_rules_re_creates_existing_rules_to_pick_up_config_changes():
    fake = _FakeNetsh(initial_rules=[fw.FIXER_RULE_NAME, fw.DISCOVERY_RULE_NAME])
    rules = fw.plan_rules(9999, 8788, remote_ip="10.0.0.0/24")
    res = fw.apply_rules(rules, runner=fake)
    assert res.ok
    # existing rules were deleted then re-added; final call for the fixer add
    # must carry the new port + subnet.
    add_calls = [c for c in fake.calls if len(c) >= 4 and c[3] == "add"]
    fixer_add = next(
        c for c in add_calls
        if any(a == f"name={fw.FIXER_RULE_NAME}" for a in c)
    )
    assert "localport=9999" in " ".join(fixer_add)
    assert "remoteip=10.0.0.0/24" in " ".join(fixer_add)


def test_apply_rules_dry_run_makes_no_changes():
    fake = _FakeNetsh(initial_rules=["Kiroshi Fixer 8800"])
    rules = fw.plan_rules(8787, 8788, remote_ip="any")
    res = fw.apply_rules(rules, runner=fake, dry_run=True)
    assert res.ok
    assert res.removed == ["Kiroshi Fixer 8800"]
    assert set(res.added) == {fw.FIXER_RULE_NAME, fw.DISCOVERY_RULE_NAME}
    # Only the initial `show` call should have hit netsh in dry-run.
    ops = [c[3] for c in fake.calls if len(c) >= 4]
    assert ops == ["show"]
    assert fake.rules == {"Kiroshi Fixer 8800"}


def test_apply_rules_reports_errors_on_failed_add():
    fake = _FakeNetsh()
    fake.fail_next_add = True
    rules = fw.plan_rules(8787, 8788)
    res = fw.apply_rules(rules, runner=fake)
    assert not res.ok
    assert res.errors and "failed to add" in res.errors[0]


def test_format_status_flags_missing_and_stale_rules():
    rules = fw.plan_rules(8787, 8788, remote_ip="192.168.1.0/24")
    existing = [fw.FIXER_RULE_NAME, "Kiroshi Fixer 8800"]  # missing UDP, has stale
    out = fw.format_status(rules, existing)
    assert "OK " in out and fw.FIXER_RULE_NAME in out
    assert "-- " in out and fw.DISCOVERY_RULE_NAME in out
    assert "Kiroshi Fixer 8800" in out and "stale" in out


def test_elevated_install_hint_includes_action_and_powershell():
    hint = fw.elevated_install_hint("firewall install")
    assert "powershell" in hint.lower()
    assert "runas" in hint.lower()
    assert "firewall" in hint and "install" in hint


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
