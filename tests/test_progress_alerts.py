"""Tests for the per-node progress + NAS-contention popup feature.

Covers the Python-testable surface added for:

1. Stacked per-node progress chart + plateau alerts:
   - ``JobStore.group_runner_done_counts`` — the per-runner/per-job done
     breakdown the sampler snapshots into the metrics ring so the job page
     can draw a stacked area "who contributed what" chart.
   - the coordinator sampler now attaches ``groups_by_runner`` to every
     ``/metrics/history`` sample.
   - ``job.html`` carries the new chart/legend/alert scaffolding the JS
     renders into.

2. Native Windows popup for NAS-contention advisories:
   - ``GET /ui/advisory_notifier.js`` serves the shared notifier script.
   - every dashboard page includes that script tag so the popup fires no
     matter which page the operator has open.

The JS itself (chart rendering, plateau math, Notification API toasts) has
no JS test harness in this repo; these tests assert the data + delivery
plumbing the JS depends on, plus that the HTML exposes the hook elements
the JS writes into.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.jobstore import JobStore  # noqa: E402


# ====================================================== per-runner done counts


def _store_with_done() -> JobStore:
    """A store where two runners have completed gigs across two groups."""
    d = tempfile.mkdtemp()
    s = JobStore(Path(d) / "t.db", max_retries=3)
    # job "worker1-camp": 3 gigs, all done by runner "worker1"
    s.seed([{"subjob_id": "c/1", "spec": {}}, {"subjob_id": "c/2", "spec": {}},
            {"subjob_id": "c/3", "spec": {}}], job="worker1-camp")
    lease = s.lease(runner_id="worker1", host="h", capacity=10, ttl=60.0)
    s.complete([{"subjob_id": j, "status": "ok", "metrics": {}}
                for j in (g["subjob_id"] for g in lease.gigs)])
    # job "mixed-camp": 4 gigs split between runner "athena" and runner "zeus"
    s.seed([{"subjob_id": "m/1", "spec": {}}, {"subjob_id": "m/2", "spec": {}},
            {"subjob_id": "m/3", "spec": {}}, {"subjob_id": "m/4", "spec": {}}],
           job="mixed-camp")
    a = s.lease(runner_id="athena", host="h", capacity=2, ttl=60.0)
    s.complete([{"subjob_id": j, "status": "ok", "metrics": {}}
                for j in (g["subjob_id"] for g in a.gigs)])
    z = s.lease(runner_id="zeus", host="h", capacity=2, ttl=60.0)
    s.complete([{"subjob_id": j, "status": "ok", "metrics": {}}
                for j in (g["subjob_id"] for g in z.gigs)])
    return s


def test_group_runner_done_counts_attributes_per_runner_per_group():
    s = _store_with_done()
    counts = s.group_runner_done_counts()
    assert counts["worker1-camp"] == {"worker1": 3}
    assert counts["mixed-camp"] == {"athena": 2, "zeus": 2}


def test_group_runner_done_counts_only_counts_done_state():
    s = _store_with_done()
    # lease but don't complete a new pending gig -> should not appear in counts
    s.seed([{"subjob_id": "c/4", "spec": {}}], job="worker1-camp")
    s.lease(runner_id="worker1", host="h", capacity=1, ttl=60.0)
    counts = s.group_runner_done_counts()
    assert counts["worker1-camp"] == {"worker1": 3}  # the leased-but-undone gig excluded


def test_group_runner_done_counts_empty_when_nothing_done():
    d = tempfile.mkdtemp()
    s = JobStore(Path(d) / "t.db", max_retries=3)
    s.seed([{"subjob_id": "g/1", "spec": {}}], job="g")
    assert s.group_runner_done_counts() == {"g": {}}


def test_group_runner_done_counts_unassigned_gigs_grouped_under_empty_key():
    """Gigs completed without a recorded runner_id collapse to the "" key so
    the chart still attributes them (as "(unassigned)") instead of dropping
    them — early-seeded rows pre-date runner tracking."""
    d = tempfile.mkdtemp()
    s = JobStore(Path(d) / "t.db", max_retries=3)
    s.seed([{"subjob_id": "u/1", "spec": {}}], job="u-camp")
    # Force a done row with a NULL runner_id directly.
    s._conn.execute(  # noqa: SLF001
        "UPDATE jobs SET state='done', runner_id=NULL WHERE subjob_id='u/1'")
    s._conn.commit()  # noqa: SLF001
    counts = s.group_runner_done_counts()
    assert counts["u-camp"] == {"": 1}


def test_group_runner_done_counts_respects_limit_and_recency():
    """Only the top N most-recently-active groups are returned."""
    d = tempfile.mkdtemp()
    s = JobStore(Path(d) / "t.db", max_retries=3)
    for i in range(5):
        s.seed([{"subjob_id": f"job{i}/1", "spec": {}}], job=f"job{i}")
        lease = s.lease(runner_id="r", host="h", capacity=1, ttl=60.0)
        s.complete([{"subjob_id": g["subjob_id"], "status": "ok", "metrics": {}}
                    for g in lease.gigs])
        time.sleep(0.005)  # stagger completion timestamps
    counts = s.group_runner_done_counts(limit_groups=2)
    assert len(counts) == 2
    # most-recently-active groups win
    assert set(counts) == {"grp4", "grp3"}


# ============================================================ coordinator sampler


def _coord_client(**kw):
    from fastapi.testclient import TestClient

    from kiroshi.coordinator import create_app

    app = create_app(JobStore(":memory:", max_retries=3), token=None, **kw)
    return TestClient(app), app


def test_metrics_history_sample_includes_groups_by_runner():
    """The 2s sampler must snapshot per-runner per-job done counts into each
    metrics sample so the job page can render the stacked contribution chart."""
    c, app = _coord_client()
    with c:
        # seed + complete some work attributed to two runners in one job
        c.post("/seed", json={
            "gigs": [{"subjob_id": "camp/1", "spec": {}},
                     {"subjob_id": "camp/2", "spec": {}}], "job": "camp"})
        lease = c.post("/lease", json={
            "runner_id": "worker1", "host": "h", "capacity": 2}).json()
        c.post("/complete", json={
            "lease_id": lease["lease_id"],
            "results": [{"subjob_id": j, "status": "ok", "metrics": {}}
                        for j in (g["subjob_id"] for g in lease["gigs"])]})

        # Wait (bounded) for the background sampler to emit a sample that
        # reflects the completed work (not an earlier pre-completion sample).
        deadline = time.time() + 8.0
        found = None
        while time.time() < deadline:
            body = c.get("/metrics/history").json()
            samples = body.get("samples") or []
            for sm in samples:
                gbr = sm.get("groups_by_runner")
                if gbr and gbr.get("camp", {}).get("worker1", 0) >= 2:
                    found = sm
                    break
            if found:
                break
            time.sleep(0.2)
        assert found is not None, "sampler never attributed completed work to worker1"
        gbr = found["groups_by_runner"]
        assert gbr["camp"].get("worker1") == 2


# ============================================================ notifier route


def test_advisory_notifier_js_route_serves_script():
    c, _ = _coord_client()
    with c:
        r = c.get("/ui/advisory_notifier.js")
        assert r.status_code == 200
        assert "javascript" in r.headers.get("content-type", "")
        body = r.text
        # sanity: the notifier polls the advisory endpoint and raises toasts
        assert "/advisories" in body
        assert "Notification" in body
        assert "showToast" in body


# ============================================================ dashboard wiring


_DASH = ROOT / "src" / "kiroshi" / "dashboard"


def test_every_dashboard_page_loads_the_notifier():
    for name in ("index.html", "jobs.html", "job.html", "history.html"):
        html = (_DASH / name).read_text(encoding="utf-8")
        assert "/ui/advisory_notifier.js" in html, f"{name} missing notifier include"


def test_job_page_has_stacked_chart_and_plateau_alert_scaffolding():
    """The JS renders into specific element ids; assert the HTML provides them
    so a refactor that drops one is caught."""
    html = (_DASH / "job.html").read_text(encoding="utf-8")
    # stacked-area chart containers
    assert 'id="stacks"' in html
    assert 'id="legend"' in html
    assert 'id="nowline"' in html
    # plateau alert modal
    assert 'id="alertmask"' in html
    assert 'id="alertmsg"' in html
    assert 'id="alertwho"' in html
    # the per-runner contribution plumbing (server-side field the JS reads)
    assert "groups_by_runner" in html
