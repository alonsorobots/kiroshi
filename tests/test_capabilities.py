"""Tests for kiroshi.capabilities — the machine-readable feature map.

The map is consumed by ``kiroshi capabilities [--json]`` and (later) the MCP
server. If an entry drifts (missing key, non-JSON output), agents that rely
on it break — so these are contract tests, not implementation tests.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi import capabilities as cap  # noqa: E402


REQUIRED_KEYS = {"name", "purpose", "command", "when_to_use", "when_not"}
# Names that MUST be present so an agent can discover the pillars of Kiroshi
# without reading the source. Add here (and to CAPABILITIES) as we ship new
# top-level features. Missing = broken discoverability.
MUST_INCLUDE = {"coordinator", "runner", "seed", "pipeline", "status", "requeue",
                "nas.assess", "advisories"}


def test_every_entry_has_all_required_keys():
    for e in cap.CAPABILITIES:
        missing = REQUIRED_KEYS - set(e.keys())
        assert not missing, f"entry {e.get('name')!r} missing keys {missing}"


def test_names_are_unique():
    names = [e["name"] for e in cap.CAPABILITIES]
    assert len(names) == len(set(names)), f"duplicate names in CAPABILITIES: {names}"


def test_pillars_present():
    names = {e["name"] for e in cap.CAPABILITIES}
    missing = MUST_INCLUDE - names
    assert not missing, f"pillar capabilities missing: {missing}"


def test_as_json_round_trips():
    js = cap.as_json()
    parsed = json.loads(js)
    assert isinstance(parsed, list)
    assert len(parsed) == len(cap.CAPABILITIES)
    for entry in parsed:
        assert REQUIRED_KEYS <= set(entry.keys())


def test_as_table_is_non_empty_and_has_header():
    t = cap.as_table()
    assert "NAME" in t and "PURPOSE" in t and "COMMAND" in t
    # every entry name appears in the table (no silent dropping)
    for e in cap.CAPABILITIES:
        assert e["name"] in t, f"table missing entry {e['name']!r}"


def test_purposes_are_short_one_liners():
    # keeps the map dense; long paragraphs belong in AGENTS.md / docs.
    for e in cap.CAPABILITIES:
        assert "\n" not in e["purpose"], f"{e['name']!r} purpose has newline"
        assert len(e["purpose"]) < 200, f"{e['name']!r} purpose too long"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fail = 0
    for t in tests:
        try:
            t(); print(f"PASS  {t.__name__}")
        except Exception as exc:
            print(f"FAIL  {t.__name__}: {exc!r}"); fail += 1
    print(f"\n{len(tests) - fail}/{len(tests)} passed")
    sys.exit(fail)
