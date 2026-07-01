"""M9 — advisory channel tests.

Covers the pieces that make the channel useful:

- :class:`Advisory` / :class:`AdvisoryStore` — schema round-trip, fingerprint
  dedup, filtered listing, resolve semantics.
- :class:`AdvisoryDetector` — sustain windowing (must hold N seconds before
  firing), each of the built-in detectors (thrash, saturation, throughput
  collapse, failure spike, parity pressure).
- Coordinator HTTP surface — ``GET /advisories``, origin capture on ``/seed``,
  advisories attached to ``/lease`` responses.
- :class:`WebhookDispatcher` — best-effort POST to ``origin.callback``.

No PII: every origin URL used here is a ``127.0.0.1`` loopback placeholder;
no real hostnames, agent ids, or NAS shares.
"""
from __future__ import annotations

import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiroshi import advisories as adv_mod  # noqa: E402
from kiroshi.advisories import (  # noqa: E402
    Advisory,
    AdvisoryDetector,
    AdvisoryStore,
    WebhookDispatcher,
    filter_advisories_for_lease,
    format_stdout_line,
)


# ============================================================ store / schema


def test_advisory_to_dict_round_trip_has_stable_fields():
    a = Advisory(
        id="abc", ts=1.0, first_ts=0.0, count=2, severity="warn",
        code="nas.thrash", fingerprint="nas.thrash:disk3", disk="disk3",
        detail="d", suggested_action="s", dashboard_url="/ui/jobs",
        origins=[{"kind": "test", "callback": "http://127.0.0.1:0/x"}],
    )
    out = a.to_dict()
    for k in ("id", "ts", "first_ts", "count", "severity", "code",
              "fingerprint", "disk", "detail", "suggested_action",
              "dashboard_url", "origins"):
        assert k in out
    assert out["origins"][0]["callback"] == "http://127.0.0.1:0/x"


def test_store_dedups_by_fingerprint_and_bumps_count():
    s = AdvisoryStore()
    a1 = s.fire(severity="warn", code="nas.thrash", disk="disk3",
                detail="d", suggested_action="s")
    a2 = s.fire(severity="warn", code="nas.thrash", disk="disk3",
                detail="d2", suggested_action="s2")
    assert a1.id == a2.id  # same underlying entry
    assert a2.count == 2
    assert a2.detail == "d2"
    # Only one entry in the store's history
    listed = s.list()
    assert len(listed) == 1
    assert listed[0].count == 2


def test_store_severity_escalation_and_disk_scoping():
    s = AdvisoryStore()
    s.fire(severity="warn", code="nas.disk_saturation", disk="disk1",
           detail="d", suggested_action="a")
    s.fire(severity="critical", code="nas.disk_saturation", disk="disk1",
           detail="d", suggested_action="a")
    active = s.active()
    assert len(active) == 1
    assert active[0].severity == "critical"

    # Different disk = different fingerprint = different entry
    s.fire(severity="warn", code="nas.disk_saturation", disk="disk7",
           detail="d", suggested_action="a")
    assert len(s.active()) == 2
    assert {a.disk for a in s.active()} == {"disk1", "disk7"}


def test_store_resolve_hides_from_active_but_keeps_history():
    s = AdvisoryStore()
    a = s.fire(severity="warn", code="x", disk=None, detail="d", suggested_action="a")
    assert s.is_active(a.fingerprint)
    assert s.resolve(a.fingerprint) is True
    assert not s.is_active(a.fingerprint)
    assert len(s.active()) == 0
    # But list() with no filter still shows it (history)
    assert len(s.list()) == 1


def test_store_list_filters():
    s = AdvisoryStore()
    s.fire(severity="info", code="a", disk="d1", detail="", suggested_action="")
    time.sleep(0.01)
    s.fire(severity="warn", code="b", disk="d2", detail="", suggested_action="")
    later = time.time()
    time.sleep(0.01)
    s.fire(severity="critical", code="c", disk="d1", detail="", suggested_action="")
    assert len(s.list(severity="warn")) == 1
    assert {a.code for a in s.list(disk="d1")} == {"a", "c"}
    assert all(a.ts >= later for a in s.list(since=later))
    assert len(s.list(active_only=True)) == 3


def test_store_capacity_evicts_oldest():
    s = AdvisoryStore(capacity=3)
    for i in range(5):
        s.fire(severity="info", code=f"c{i}", disk=None,
               detail="", suggested_action="")
    codes = {a.code for a in s.list()}
    assert codes == {"c2", "c3", "c4"}


def test_drain_pending_returns_new_events_and_clears():
    s = AdvisoryStore()
    s.fire(severity="warn", code="x", disk=None, detail="", suggested_action="")
    s.fire(severity="warn", code="x", disk=None, detail="", suggested_action="")
    drained = s.drain_pending()
    assert len(drained) == 2  # both the fire and the re-fire are dispatchable
    assert s.drain_pending() == []


# ================================================================ detectors


def _detector(**overrides):
    """Build a detector wired to synthetic sources so we can drive it directly.

    Every knob is overridable; sustain defaults to 0 so a single tick trips a
    condition — individual sustain tests override this."""
    store = AdvisoryStore()
    state = {
        "stats": {"pending": 0, "leased": 0, "done": 0, "failed": 0,
                  "disk_inflight": {}, "rate_per_s": 0.0},
        "io": {"disks": []},
        "resource": {"has_parity": False, "global_write_budget": 0,
                     "global_write_inflight": 0},
        "budget": {},
        "inflight_map": {},
        "parity": set(),
        "origins": {},
    }
    kwargs = dict(
        adv_store=store,
        stats_fn=lambda: state["stats"],
        iowatcher_fn=lambda: state["io"],
        metrics_ring=deque(),
        resource_fn=lambda: state["resource"],
        origins_for=lambda d: state["origins"].get(d, state["origins"].get(None, [])),
        disk_budget_fn=lambda: state["budget"],
        disk_inflight_fn=lambda d: state["inflight_map"].get(d, 0),
        parity_disks_fn=lambda: state["parity"],
        sample_interval_s=0.1,
        sustain_s=0.0,  # fire on first tick unless a test overrides
    )
    kwargs.update(overrides)
    det = AdvisoryDetector(**kwargs)
    return det, store, state


def test_thrash_detector_fires_when_inflight_exceeds_budget():
    det, store, state = _detector()
    state["budget"] = {"disk3": 6}
    state["stats"]["disk_inflight"] = {"disk3": 10}  # > 6 * 1.2 = 7.2
    fired = det.tick()
    assert any(a.code == "nas.thrash" and a.disk == "disk3" for a in fired)
    assert store.is_active("nas.thrash:disk3")


def test_thrash_detector_inert_without_budget():
    det, store, state = _detector()
    state["stats"]["disk_inflight"] = {"disk3": 100}
    assert det.tick() == []
    assert store.list() == []


def test_thrash_resolves_when_condition_clears():
    det, store, state = _detector()
    state["budget"] = {"disk3": 6}
    state["stats"]["disk_inflight"] = {"disk3": 10}
    det.tick()
    assert store.is_active("nas.thrash:disk3")
    state["stats"]["disk_inflight"] = {"disk3": 3}  # back below threshold
    det.tick()
    assert not store.is_active("nas.thrash:disk3")


def test_sustain_window_requires_persistence():
    det, store, state = _detector(sustain_s=10.0)
    state["budget"] = {"disk3": 6}
    state["stats"]["disk_inflight"] = {"disk3": 10}
    assert det.tick() == []  # not yet — sustain window not elapsed
    assert not store.is_active("nas.thrash:disk3")
    # Rewind the "condition first observed" timestamp to force the sustain
    # window to have elapsed, without sleeping.
    det._condition_since["nas.thrash:disk3"] = time.time() - 30.0
    fired = det.tick()
    assert any(a.code == "nas.thrash" for a in fired)


def test_saturation_detector_escalates_on_parity_disk():
    det, store, state = _detector()
    state["parity"] = {"disk1"}
    state["io"]["disks"] = [
        {"disk": "disk1", "util_pct": 98, "read_mbps": 10, "write_mbps": 5},
        {"disk": "disk2", "util_pct": 99, "read_mbps": 20, "write_mbps": 0},
    ]
    fired = det.tick()
    by_disk = {a.disk: a for a in fired if a.code == "nas.disk_saturation"}
    assert by_disk["disk1"].severity == "critical"
    assert by_disk["disk2"].severity == "warn"


def test_throughput_collapse_detector():
    ring = deque([
        {"ts": 0, "rate": 100, "done": 0, "failed": 0},
        {"ts": 5, "rate": 100, "done": 0, "failed": 0},
        {"ts": 10, "rate": 100, "done": 0, "failed": 0},
        {"ts": 15, "rate": 5, "done": 0, "failed": 0},
        {"ts": 20, "rate": 5, "done": 0, "failed": 0},
        {"ts": 25, "rate": 5, "done": 0, "failed": 0},
    ])
    det, store, state = _detector(metrics_ring=ring)
    state["stats"] = {"pending": 100, "leased": 1, "done": 0,
                      "failed": 0, "disk_inflight": {}, "rate_per_s": 5}
    fired = det.tick()
    assert any(a.code == "nas.throughput_collapse" for a in fired)


def test_throughput_collapse_inert_when_no_work_pending():
    ring = deque([{"ts": i * 5, "rate": 100 if i < 3 else 5,
                   "done": 0, "failed": 0} for i in range(6)])
    det, store, state = _detector(metrics_ring=ring)
    state["stats"] = {"pending": 0, "leased": 0, "done": 100,
                      "failed": 0, "disk_inflight": {}, "rate_per_s": 5}
    assert not any(a.code == "nas.throughput_collapse" for a in det.tick())


def test_failure_spike_absolute_count():
    det, store, state = _detector(failure_spike_count=5)
    state["stats"] = {"pending": 0, "leased": 0, "done": 100, "failed": 0,
                      "disk_inflight": {}, "rate_per_s": 0}
    det.tick()  # establishes baseline
    # Rewind so the dt gate (>= 1s) passes without sleeping.
    det._prev_ts = time.time() - 5
    state["stats"]["failed"] = 30
    state["stats"]["done"] = 100
    fired = det.tick()
    assert any(a.code == "gig.failure_spike" for a in fired)


def test_failure_spike_ratio():
    det, store, state = _detector(failure_spike_count=1_000_000,
                                  failure_spike_ratio=0.20)
    state["stats"] = {"pending": 0, "leased": 0, "done": 0, "failed": 0,
                      "disk_inflight": {}, "rate_per_s": 0}
    det.tick()
    det._prev_ts = time.time() - 5
    state["stats"]["done"] = 10
    state["stats"]["failed"] = 5    # 5/(5+10)=33%
    fired = det.tick()
    assert any(a.code == "gig.failure_spike" for a in fired)


def test_parity_write_pressure_only_on_parity_arrays():
    det, store, state = _detector()
    state["resource"] = {"has_parity": True, "global_write_budget": 4,
                         "global_write_inflight": 4}
    state["stats"]["pending"] = 50
    assert any(a.code == "nas.parity_write_pressure" for a in det.tick())

    det2, store2, state2 = _detector()
    state2["resource"] = {"has_parity": False, "global_write_budget": 0,
                          "global_write_inflight": 0}
    state2["stats"]["pending"] = 50
    assert not any(a.code == "nas.parity_write_pressure" for a in det2.tick())


def test_detector_attaches_origins():
    det, store, state = _detector()
    state["budget"] = {"disk3": 4}
    state["stats"]["disk_inflight"] = {"disk3": 100}
    state["origins"]["disk3"] = [
        {"kind": "test-agent", "agent_id": "abc",
         "callback": "http://127.0.0.1:0/notify"}
    ]
    fired = det.tick()
    thrash = next(a for a in fired if a.code == "nas.thrash")
    assert thrash.origins and thrash.origins[0]["agent_id"] == "abc"


# ============================================================== helpers


def test_filter_advisories_for_lease_includes_unscoped_and_matching_disks():
    store = AdvisoryStore()
    store.fire(severity="warn", code="a", disk="disk1", detail="", suggested_action="")
    store.fire(severity="warn", code="b", disk="disk9", detail="", suggested_action="")
    store.fire(severity="info", code="c", disk=None, detail="", suggested_action="")
    out = filter_advisories_for_lease(store.active(), leased_disks={"disk1"})
    disks = {a["disk"] for a in out}
    assert disks == {"disk1", None}


def test_format_stdout_line_is_greppable():
    line = format_stdout_line({
        "severity": "warn", "code": "nas.thrash", "disk": "disk3",
        "detail": "over budget", "suggested_action": "reduce",
    })
    assert line.startswith("KIROSHI-ADVISORY: WARN nas.thrash disk=disk3 |")
    assert "action: reduce" in line


# ============================================================== HTTP surface


def _http_client(**app_kw):
    """TestClient with advisories enabled but detector cadence long enough that
    we drive it manually via ``app.state.advisory_detector.tick()``."""
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app
    from kiroshi.jobstore import JobStore

    app = create_app(
        JobStore(":memory:", max_retries=3),
        token=None,
        enable_advisories=True,
        advisory_sample_interval_s=3600,   # never auto-tick inside a test
        advisory_webhook_interval_s=3600,
        **app_kw,
    )
    return TestClient(app), app


def test_seed_captures_origin_by_group():
    c, app = _http_client()
    with c:
        r = c.post("/seed", json={
            "gigs": [{"job_id": "grpA/1", "spec": {}}],
            "group": "grpA",
            "origin": {"kind": "test", "agent_id": "AGT",
                       "callback": "http://127.0.0.1:0/x"},
        })
        assert r.status_code == 200
        assert app.state.origins_by_group["grpA"][0]["agent_id"] == "AGT"

        # Re-seed with the SAME origin — should not duplicate.
        c.post("/seed", json={
            "gigs": [{"job_id": "grpA/2", "spec": {}}], "group": "grpA",
            "origin": {"kind": "test", "agent_id": "AGT",
                       "callback": "http://127.0.0.1:0/x"},
        })
        assert len(app.state.origins_by_group["grpA"]) == 1


def test_advisories_endpoint_lists_fired_entries():
    c, app = _http_client()
    with c:
        app.state.advisories.fire(
            severity="warn", code="nas.thrash", disk="disk3",
            detail="d", suggested_action="s")
        r = c.get("/advisories")
        assert r.status_code == 200
        body = r.json()
        assert body["active"] == 1
        assert body["advisories"][0]["code"] == "nas.thrash"

        # Severity filter
        assert len(c.get("/advisories?severity=critical").json()["advisories"]) == 0
        assert len(c.get("/advisories?severity=warn").json()["advisories"]) == 1


def test_lease_response_carries_disk_scoped_advisories():
    c, app = _http_client()
    with c:
        c.post("/seed", json={
            "gigs": [{"job_id": "g/1", "spec": {}}],
        })
        app.state.advisories.fire(
            severity="warn", code="nas.thrash", disk="disk3",
            detail="d", suggested_action="s")
        app.state.advisories.fire(
            severity="info", code="fleet.pressure", disk=None,
            detail="d", suggested_action="s")
        # No topology configured => leased gigs carry no disk => only the
        # unscoped advisory should be attached.
        r = c.post("/lease", json={"runner_id": "r1", "host": "h",
                                   "capacity": 5})
        adv = r.json().get("advisories") or []
        assert any(a["code"] == "fleet.pressure" for a in adv)
        assert not any(a["code"] == "nas.thrash" for a in adv)


def test_origin_flows_from_seed_to_advisory_via_detector():
    """End-to-end (in-process): seed with an origin, fire a thrash on the same
    disk, verify the advisory gets attributed back to that origin."""
    c, app = _http_client()
    with c:
        c.post("/seed", json={
            "gigs": [{"job_id": "camp/1", "spec": {}, "group": "camp"}],
            "group": "camp",
            "origin": {"kind": "test", "callback": "http://127.0.0.1:0/x"},
        })
        # Fake the gig into "leased on disk3" so origins_for("disk3") finds it.
        # We do this by reaching into the store directly — the coordinator's
        # origins_for lookup only cares about (grp, disk) pairs.
        app.state.store._conn.execute(  # noqa: SLF001
            "UPDATE jobs SET state='leased', disk='disk3' WHERE job_id=?",
            ("camp/1",))
        app.state.store._conn.commit()  # noqa: SLF001

        # Configure a budget that the fake in-flight exceeds, then tick.
        app.state.disk_concurrency = {"disk3": 1}
        # jobstore's disk_inflight_count reads the leased count -> 1;
        # give the detector a synthetic stat map showing 10 in-flight (>1*1.2).
        # Easiest: pass the state via app.state.advisory_detector directly.
        det = app.state.advisory_detector
        det.sustain_s = 0.0  # instant fire
        # Also patch its stats_fn so we don't depend on the store's actual counts.
        det._stats = lambda: {"pending": 0, "leased": 10, "done": 0, "failed": 0,
                              "disk_inflight": {"disk3": 10}, "rate_per_s": 0}
        det.tick()
        active = app.state.advisories.active()
        thrash = next(a for a in active if a.code == "nas.thrash")
        assert any(o.get("callback") == "http://127.0.0.1:0/x"
                   for o in thrash.origins)


# =============================================================== webhook


def test_webhook_dispatcher_posts_advisories_with_callbacks():
    """Uses an injected fake `http_post` so the test never hits the network."""
    class _Resp:
        def __init__(self, code): self.status_code = code

    calls: list[dict] = []

    def fake_post(url, json=None, timeout=None, **_kw):
        calls.append({"url": url, "json": json})
        return _Resp(200)

    store = AdvisoryStore()
    disp = WebhookDispatcher(store, http_post=fake_post)
    store.fire(severity="warn", code="x", disk=None, detail="d",
               suggested_action="a",
               origins=[{"kind": "test", "callback": "http://127.0.0.1:0/hook"}])
    n = disp.dispatch_once()
    assert n == 1
    assert calls and calls[0]["url"] == "http://127.0.0.1:0/hook"
    assert calls[0]["json"]["advisory"]["code"] == "x"


def test_webhook_skips_origins_without_callback_and_survives_errors():
    def bad_post(url, json=None, timeout=None, **_kw):
        raise RuntimeError("connection refused")

    store = AdvisoryStore()
    disp = WebhookDispatcher(store, http_post=bad_post)
    store.fire(severity="warn", code="x", disk=None, detail="", suggested_action="",
               origins=[{"kind": "cursor"}])  # no callback -> skipped
    store.fire(severity="warn", code="y", disk=None, detail="", suggested_action="",
               origins=[{"kind": "cursor",
                         "callback": "http://127.0.0.1:0/broken"}])
    # Must not raise even though the "network" call blew up.
    assert disp.dispatch_once() == 0
    assert "http://127.0.0.1:0/broken" in disp.last_results
    assert disp.last_results["http://127.0.0.1:0/broken"]["ok"] is False


def test_webhook_ignores_non_2xx():
    class _Resp:
        def __init__(self, code): self.status_code = code

    def flaky_post(url, json=None, timeout=None, **_kw):
        return _Resp(500)

    store = AdvisoryStore()
    disp = WebhookDispatcher(store, http_post=flaky_post)
    store.fire(severity="warn", code="x", disk=None, detail="", suggested_action="",
               origins=[{"kind": "cursor",
                         "callback": "http://127.0.0.1:0/x"}])
    assert disp.dispatch_once() == 0
    assert disp.last_results["http://127.0.0.1:0/x"]["ok"] is False


# ============================================================ module surface


def test_module_re_exports():
    for name in ("Advisory", "AdvisoryStore", "AdvisoryDetector",
                 "WebhookDispatcher", "filter_advisories_for_lease",
                 "format_stdout_line", "SEVERITY_WARN"):
        assert hasattr(adv_mod, name), name
