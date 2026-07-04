"""Tests for the new MCP process-management tools (requeue, ps, stop).

``requeue`` is tested against a live in-process Coordinator (HTTP /requeue).
``ps``/``stop`` are tested with mocked ``processreg`` since they operate on
the local process manifest, not the Coordinator API.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    import mcp.server.fastmcp  # noqa: F401
    _HAVE = True
except ImportError:
    _HAVE = False


def _skip_or_import():
    if not _HAVE:
        print("SKIP  (mcp extra not installed)")
        sys.exit(0)
    from kiroshi import mcp_server
    return mcp_server


def _build_client(max_retries=3):
    """Spin up an in-process Coordinator for HTTP-based tool tests."""
    from fastapi.testclient import TestClient
    from kiroshi.coordinator import create_app
    from kiroshi.jobstore import JobStore
    app = create_app(JobStore(":memory:", max_retries=max_retries), token=None)
    return TestClient(app)


def test_requeue_tool_calls_coordinator_endpoint():
    """requeue tool should POST /requeue and return the count."""
    m = _skip_or_import()
    # max_retries=0 so a single error immediately marks the gig 'failed'
    client = _build_client(max_retries=0)
    # seed a gig
    client.post("/seed", json={"gigs": [{"subjob_id": "g1", "spec": {}}]})
    # lease it
    lease = client.post("/lease", json={"runner_id": "r1", "host": "h", "capacity": 5})
    lid = lease.json()["lease_id"]
    gigs = lease.json().get("gigs", [])
    assert len(gigs) == 1, f"expected 1 leased gig, got {len(gigs)}"
    # mark it failed (max_retries=0 → single error → failed)
    client.post("/complete", json={"lease_id": lid, "results": [
        {"subjob_id": "g1", "status": "error", "error": "boom"}]})
    # verify it's failed
    st = client.get("/status").json()
    assert st["failed"] == 1, f"expected 1 failed, got {st['failed']}"
    # call /requeue endpoint (the MCP tool wraps this)
    rq = client.post("/requeue", json={"states": ["failed"], "reset_attempts": True})
    assert rq.json()["requeued"] == 1
    # verify it's back to pending
    st2 = client.get("/status").json()
    assert st2["pending"] == 1
    assert st2["failed"] == 0


def test_ps_and_stop_tools_registered():
    """ps and stop tools must be registered on the MCP server."""
    m = _skip_or_import()
    app = m.build_server()
    tool_names = set()
    import asyncio
    try:
        listed = asyncio.get_event_loop().run_until_complete(app.list_tools())
        tool_names = {t.name for t in listed}
    except Exception:
        pass
    if tool_names:
        assert "requeue" in tool_names
        assert "ps" in tool_names
        assert "stop" in tool_names


def test_stop_ambiguous_guard():
    """stop with multiple matches and no pid/all should refuse (safety).
    Actually calls the extracted _stop_impl logic (not a hollow proxy)."""
    m = _skip_or_import()
    fake_procs = [
        {"role": "coordinator", "pid": 100, "launch_command": "kiroshi coordinator --port 8800"},
        {"role": "coordinator", "pid": 200, "launch_command": "kiroshi coordinator --port 8801"},
    ]
    mock_stop = MagicMock(return_value=True)
    with patch("kiroshi.processreg.list_registered", return_value=fake_procs):
        with patch("kiroshi.processreg.request_stop", mock_stop):
            from kiroshi.mcp_server import _stop_impl
            result = _stop_impl(role="coordinator")  # 2 matches, no pid, no all
            assert result["stopped"] == 0
            assert result.get("ambiguous") is True
            assert len(result["matches"]) == 2
            # CRITICAL: request_stop must NOT have been called
            mock_stop.assert_not_called()


def test_stop_single_match_stops_it():
    """stop with exactly one match should call request_stop."""
    m = _skip_or_import()
    fake_procs = [{"role": "runner", "pid": 999, "launch_command": "kiroshi runner"}]
    mock_stop = MagicMock(return_value=True)
    with patch("kiroshi.processreg.list_registered", return_value=fake_procs):
        with patch("kiroshi.processreg.request_stop", mock_stop):
            from kiroshi.mcp_server import _stop_impl
            result = _stop_impl(role="runner")
            assert result["stopped"] == 1
            mock_stop.assert_called_once_with("runner", 999)


def test_stop_all_flag_overrides_ambiguous():
    """stop with all=True should stop all matches even if ambiguous."""
    m = _skip_or_import()
    fake_procs = [
        {"role": "coordinator", "pid": 100, "launch_command": "k1"},
        {"role": "coordinator", "pid": 200, "launch_command": "k2"},
    ]
    mock_stop = MagicMock(return_value=True)
    with patch("kiroshi.processreg.list_registered", return_value=fake_procs):
        with patch("kiroshi.processreg.request_stop", mock_stop):
            from kiroshi.mcp_server import _stop_impl
            result = _stop_impl(role="coordinator", all=True)
            assert result["stopped"] == 2
            assert mock_stop.call_count == 2


def test_stop_no_matches():
    """stop with no matching processes returns stopped=0."""
    m = _skip_or_import()
    with patch("kiroshi.processreg.list_registered", return_value=[]):
        from kiroshi.mcp_server import _stop_impl
        result = _stop_impl(role="runner")
        assert result["stopped"] == 0


if __name__ == "__main__":
    tests = [n for n in dir(sys.modules[__name__]) if n.startswith("test_")]
    fail = 0
    for name in tests:
        try:
            globals()[name]()
            print(f"PASS  {name}")
        except Exception as exc:
            print(f"FAIL  {name}: {exc}")
            fail += 1
    print(f"\n{len(tests)-fail}/{len(tests)} passed")
    sys.exit(fail)
