"""Integration: an idle-gated job's leases are withheld until the array is quiet.

Drives a live in-process Coordinator via TestClient. Fakes the IOWatcher snapshot
so we control the reported disk utilization deterministically.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    from fastapi.testclient import TestClient  # noqa: F401
    _HAVE = True
except ImportError:
    _HAVE = False


def _skip():
    if not _HAVE:
        print("SKIP  (fastapi not installed)")
        sys.exit(0)


def _client():
    from fastapi.testclient import TestClient
    from kiroshi.coordinator import create_app
    from kiroshi.jobstore import JobStore
    app = create_app(JobStore(":memory:"), token=None)
    return app, TestClient(app)


def _fake_snapshot(util):
    def snap():
        return {"disks": [{"disk": "disk1", "util_pct": util, "samples": 10}]}
    return snap


def test_gate_holds_when_busy_then_opens_when_quiet():
    _skip()
    app, c = _client()
    # Seed an idle-gated job (sustain 0 so quiet -> immediate admit).
    c.post("/seed", json={
        "gigs": [{"subjob_id": "f1", "spec": {}}, {"subjob_id": "f2", "spec": {}}],
        "job": "demote-x",
        "idle_gate": {"disks": ["disk1"], "util_pct": 15, "sustain_s": 0},
    })

    # Busy array -> lease withheld with IDLE_GATE_WAIT.
    app.state.io_watcher.snapshot = _fake_snapshot(90)
    r = c.post("/lease", json={"runner_id": "r1", "host": "h",
                               "capacity": 10, "job": "demote-x"}).json()
    assert r["gigs"] == []
    assert r["idle_gate"]["reason"] == "IDLE_GATE_WAIT"

    # Quiet array -> lease proceeds.
    app.state.io_watcher.snapshot = _fake_snapshot(5)
    r2 = c.post("/lease", json={"runner_id": "r1", "host": "h",
                                "capacity": 10, "job": "demote-x"}).json()
    assert len(r2["gigs"]) == 2


def test_ungated_job_is_unaffected():
    _skip()
    app, c = _client()
    c.post("/seed", json={"gigs": [{"subjob_id": "g1", "spec": {}}], "job": "normal"})
    app.state.io_watcher.snapshot = _fake_snapshot(99)  # busy, but not gated
    r = c.post("/lease", json={"runner_id": "r1", "host": "h",
                               "capacity": 10, "job": "normal"}).json()
    assert len(r["gigs"]) == 1


def test_gate_persists_and_reloads_from_store():
    _skip()
    from kiroshi.jobstore import JobStore
    db = JobStore(":memory:")
    db.set_job_gate("demote-y", {"util_pct": 10, "sustain_min": 5})
    assert db.job_gate("demote-y")["util_pct"] == 10
    assert "demote-y" in db.all_job_gates()


if __name__ == "__main__":
    fail = 0
    for name in sorted(n for n in dir() if n.startswith("test_")):
        try:
            globals()[name]()
            print(f"PASS  {name}")
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {name}: {e}")
            fail += 1
    sys.exit(fail)
