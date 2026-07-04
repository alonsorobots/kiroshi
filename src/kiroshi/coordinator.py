"""The Coordinator — coordinator HTTP service.

FastAPI app over a local :class:`~kiroshi.jobstore.JobStore`. Hands batches of gigs
to Runners, applies their results, extends leases on heartbeat, and runs a
background reaper that returns dead Runners' leases to the pool (self-heal).

Serves the live Kiroshi dashboard at ``/`` and a JSON snapshot at ``/status``.
"""
from __future__ import annotations
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import security
from .jobstore import JobStore

_DASHBOARD = Path(__file__).parent / "dashboard" / "index.html"

# Paths reachable without a token: the dashboard shell (carries no data of its
# own; everything sensitive comes from guarded endpoints it fetches), a liveness
# probe, and the auth challenge (which is itself the authentication mechanism and
# never reveals the token). Everything else requires the mesh token when set.
_OPEN_PATHS = frozenset({"/", "/favicon.ico", "/healthz", "/auth/challenge"})


class SeedGig(BaseModel):
    subjob_id: str
    spec: dict[str, Any] = Field(default_factory=dict)
    job: Optional[str] = None


class SeedReq(BaseModel):
    gigs: list[SeedGig]
    # Batch-wide job slug + human-readable label, applied to every sub-job that
    # doesn't carry its own `job`. Lets `kiroshi seed --job X --label "..."` head
    # a whole job in the dashboard instead of fragmenting it per subjob_id prefix.
    job: Optional[str] = None
    label: Optional[str] = None
    # Opaque attribution blob — carried as-is, never interpreted by Kiroshi except
    # for the optional ``callback`` URL used by the advisory webhook. Lets a
    # launcher say "I'm cursor-agent X, POST warnings to Y" so a script that
    # oversubscribes the NAS gets a message back to whoever authored it.
    # See ``advisories.py`` and the M9 plan for the full delivery contract.
    origin: Optional[dict[str, Any]] = None


class LeaseReq(BaseModel):
    runner_id: str
    host: str = "?"
    capacity: int = 100
    # The Runner's heartbeat cadence (s). The Coordinator sizes the lease as a safe
    # multiple of it, so a slow-but-alive Runner isn't reaped into a duplicate.
    heartbeat_interval: Optional[float] = None


class ResultItem(BaseModel):
    subjob_id: str
    status: str = "ok"
    error: Optional[str] = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class CompleteReq(BaseModel):
    lease_id: str
    results: list[ResultItem]


class HeartbeatReq(BaseModel):
    lease_id: str
    runner_id: str = "?"
    stats: dict[str, Any] = Field(default_factory=dict)
    heartbeat_interval: Optional[float] = None


class RequeueReq(BaseModel):
    states: list[str] = Field(default_factory=lambda: ["failed"])
    reset_attempts: bool = True


class RegisterReq(BaseModel):
    runner_id: str
    host: str = "?"
    launch_command: str = ""
    task: str = ""
    workers: int = 0
    pid: Optional[int] = None
    log_path: Optional[str] = None


def create_app(
    store: JobStore,
    lease_ttl: float = 120.0,
    reap_interval: float = 15.0,
    pages_dir: Optional[str] = None,
    token: Optional[str] = None,
    launch_command: Optional[str] = None,
    lease_miss_tolerance: float = 4.0,
    lease_ttl_cap: float = 3600.0,
    task_source: Optional[dict[str, Any]] = None,
    disks: Optional[list] = None,
    # M9 advisory channel — background detectors + optional outbound webhook.
    # Additive: with defaults the Coordinator behaves exactly as before, only gaining a
    # /advisories endpoint that returns [] until something fires. Set
    # ``enable_advisories=False`` to skip starting the threads entirely (used by
    # some unit tests to keep the app synchronous).
    enable_advisories: bool = True,
    advisory_sample_interval_s: float = 10.0,
    advisory_sustain_s: float = 60.0,
    advisory_webhook_interval_s: float = 2.0,
    fair_share: bool = False,
    decision_ring: int = 5000,
    jobevent_ring: int = 50000,
) -> FastAPI:
    app = FastAPI(title="Kiroshi Coordinator", version="0.0.1")
    app.state.store = store
    app.state.lease_ttl = lease_ttl
    # A lease must outlive several missed heartbeats, or a momentarily-slow (but
    # alive) Runner gets reaped and its gigs handed to a second Runner — i.e. the
    # same output written twice (at-least-once delivery). We size each lease to
    # max(lease_ttl, miss_tolerance * runner_heartbeat), capped, so the floor is
    # robust regardless of how the Runner is configured.
    app.state.lease_miss_tolerance = lease_miss_tolerance
    app.state.lease_ttl_cap = lease_ttl_cap
    app.state.token = token
    app.state._stop = threading.Event()
    # In-memory runner registry (launch command, pid, liveness) + throughput
    # time-series for the dashboard rate curve (ring buffer, ~last 30 min @ 2s).
    app.state.runners = {}
    app.state.metrics = deque(maxlen=900)
    app.state.started_at = time.time()
    app.state.launch_command = launch_command or ""
    # Opt-in task-code serving for `kiroshi join` (SECURITY.md §6.5). None unless
    # the Coordinator was started with --serve-task; gated + consent-checked client-side.
    app.state.task_source = task_source
    # Storage topology for shard-aware leasing (PLAN §7.6). None/[] => inert (no
    # per-disk budget, plain work-stealing). The mesh-global per-spindle budget is
    # derived here once; only the Coordinator can enforce it across the whole fleet.
    app.state.disks = disks or []
    from .storage import disk_concurrency_map, validate_disks

    app.state.disk_concurrency = disk_concurrency_map(app.state.disks)

    # Fair-share leasing (opt-in). When on, each host's in-flight gigs are capped
    # at its slice of the fleet-wide budget, weighted by the live worker count of
    # its active runners — so a fast poller can't hoard the whole disk budget and
    # starve slower hosts. Inert (behaves exactly as before) unless enabled AND a
    # per-disk budget exists to divide.
    app.state.fair_share = fair_share

    # --- Coordination decision ledger (observability) ---
    # Bounded ring buffers recording *why* every /lease call returned the count
    # it did (LeaseDecision) and per-job coordination transitions (JobEvent).
    # In-memory only — no DB schema change. See COORDINATION_DECISION_LOG plan.
    app.state.lease_decisions: deque = deque(maxlen=decision_ring)
    app.state.job_events: deque = deque(maxlen=jobevent_ring)
    app.state.job_event_index: dict[str, deque] = {}
    app.state.jobevent_ring = jobevent_ring  # also bounds the index dict below
    app.state._last_lease_log: dict[str, float] = {}  # host -> ts for throttle

    # Warn about likely-misconfigured disk topologies at boot so a bad match
    # rule surfaces immediately instead of as 129k runtime "READ_ROOT not set".
    for _w in validate_disks(app.state.disks):
        print(f"[fixer][WARN] {_w}", flush=True)

    # --- Mesh resource governor (standalone resource-acquire service) ---
    # Extends the per-disk read budget to non-sub-job workloads + adds a global
    # write/parity budget. Any process can acquire via /resource/acquire.
    from .storage import has_parity, global_write_concurrency
    app.state.resource_slots = {}  # slot_id -> {disk, mode, holder, deadline}
    app.state.resource_lock = threading.Lock()
    app.state.has_parity = has_parity(app.state.disks)
    app.state.global_write_budget = global_write_concurrency(app.state.disks)
    app.state.global_write_inflight = 0  # current write slots held

    # --- External-process I/O watcher (rolling-window observability) ---
    # Only active when the topology has HDD disks (seek/saturation matters).
    # NVMe-only nodes skip it (no contention concern). On Linux (NAS) reads
    # /proc/diskstats; inert on Windows or when no HDD disks configured.
    from .iowatcher import IOWatcher
    _hdd_disk_ids = [d.id for d in app.state.disks if d.kind.lower() == "hdd"]
    _parity_map = {d.id: d.parity_protected for d in app.state.disks}
    _direct_paths = {d.id: d.direct_path for d in app.state.disks if d.direct_path}
    app.state.io_watcher = IOWatcher(_hdd_disk_ids, _parity_map, _direct_paths)
    app.state.io_watcher.start()

    # --- Advisory channel (M9) ---
    # Structured warnings for whoever launched the work: humans reading the
    # dashboard, monitors, MCP clients, or LLM agents via an outbound webhook.
    # The Coordinator only ships primitives (detect + query + optional POST); IDE-
    # specific consumers live outside this repo. Origins are opaque attribution
    # blobs the launcher supplied via `--origin` on `run`/`seed`.
    from . import advisories as _adv_mod

    app.state.advisories = _adv_mod.AdvisoryStore()
    # job -> list[origin_dict], deduped by identity of (kind, agent_id, callback).
    # Merged from every /seed request that carries an origin.
    app.state.origins_by_group: dict[str, list[dict[str, Any]]] = {}
    app.state.enable_advisories = enable_advisories

    def _origins_for(disk: Optional[str]) -> list[dict[str, Any]]:
        """Union of origins whose in-flight (leased/pending) gigs land on ``disk``.

        For fleet-wide advisories (``disk is None``) it's the union across every
        job with any pending/leased work. Cheap enough at Kiroshi scale (the
        detector runs every 10s and leased-set size is capped by budget)."""
        origins_map = app.state.origins_by_group
        if not origins_map:
            return []
        try:
            rows = store.list_jobs(states=("leased", "pending"), limit=5000)
        except Exception:
            return []
        grps: set[str] = set()
        for row in rows:
            if disk is not None and row.get("disk") != disk:
                continue
            g = row.get("job")
            if g:
                grps.add(g)
        seen: set[tuple] = set()
        out: list[dict[str, Any]] = []
        for g in grps:
            for o in origins_map.get(g, []):
                key = (o.get("kind"), o.get("agent_id"), o.get("callback"))
                if key in seen:
                    continue
                seen.add(key)
                out.append(o)
        return out

    def _parity_disks() -> set[str]:
        return {d.id for d in app.state.disks if getattr(d, "parity_protected", False)}

    def _dashboard_url_for(_disk: Optional[str]) -> Optional[str]:
        # Kiroshi doesn't know its own external base URL (Coordinator may bind
        # 0.0.0.0 behind NAT / an overlay), so we return a relative path a
        # consumer can join to whatever hostname they used. Callers that need
        # an absolute URL should combine with `origin.dashboard_base`.
        return "/ui/jobs"

    def _resource_state() -> dict[str, Any]:
        return {
            "has_parity": app.state.has_parity,
            "global_write_budget": app.state.global_write_budget,
            "global_write_inflight": app.state.global_write_inflight,
        }

    app.state.advisory_detector = _adv_mod.AdvisoryDetector(
        adv_store=app.state.advisories,
        stats_fn=store.stats,
        iowatcher_fn=(app.state.io_watcher.snapshot
                      if _hdd_disk_ids else None),
        metrics_ring=app.state.metrics,
        resource_fn=_resource_state,
        origins_for=_origins_for,
        disk_budget_fn=lambda: dict(app.state.disk_concurrency or {}),
        disk_inflight_fn=store.disk_inflight_count,
        parity_disks_fn=_parity_disks,
        dashboard_url_fn=_dashboard_url_for,
        sample_interval_s=advisory_sample_interval_s,
        sustain_s=advisory_sustain_s,
    )
    app.state.advisory_dispatcher = _adv_mod.WebhookDispatcher(
        adv_store=app.state.advisories,
        interval_s=advisory_webhook_interval_s,
    )

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        cfg = app.state.token
        path = request.url.path
        # HTML *shells* (the console pages) carry no data of their own — every
        # sensitive byte comes from a guarded JSON endpoint they fetch with the
        # token. So the shells are open; the data is not. Custom per-job pages
        # under /p/ ARE gated: the dashboard links to them already carry ?token=,
        # and they expose task data, so they must not be world-readable.
        is_open = path in _OPEN_PATHS or path.startswith("/ui/")
        if cfg and not is_open:
            presented = security.extract_presented_token(
                dict(request.headers), request.query_params.get("token")
            )
            if not security.token_matches(cfg, presented):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "auth": bool(app.state.token)}

    @app.get("/auth/challenge")
    def auth_challenge(nonce: str = "") -> JSONResponse:
        """Mutual auth: prove to a Runner that *this* Coordinator holds the shared
        token, so the Runner can verify the Coordinator BEFORE sending its bearer token
        or executing leased work. Defends `--fixer auto` against a rogue Coordinator
        (LAN attacker who wins UDP discovery) harvesting the token + injecting
        specs. Reveals only HMAC(token, nonce), never the token. Open by design."""
        tok = app.state.token
        if not tok:
            return JSONResponse({"auth": False, "proof": None})
        if not nonce or len(nonce) < 8:
            return JSONResponse({"error": "nonce required (>=8 chars)"}, status_code=400)
        return JSONResponse({"auth": True, "proof": security.prove(tok, nonce)})

    # Optional per-task custom views: any *.html dropped in pages_dir is served at
    # /p/<name> and linked from the dashboard. A task ships its own visualization
    # (e.g. a SLERP-quality viewer) without Kiroshi knowing anything about it.
    pages_path = Path(pages_dir) if pages_dir else None
    if pages_path and pages_path.is_dir():
        app.mount("/p", StaticFiles(directory=str(pages_path), html=True), name="pages")
    app.state.pages_path = pages_path

    @app.get("/pages")
    def pages() -> list[dict[str, str]]:
        if not pages_path or not pages_path.is_dir():
            return []
        return [
            {"name": f.stem, "url": f"/p/{f.name}"}
            for f in sorted(pages_path.glob("*.html"))
        ]

    def _reaper() -> None:
        while not app.state._stop.wait(reap_interval):
            try:
                n = store.reap()
                if n:
                    print(f"[fixer] reaped {n} expired lease(s) -> pending", flush=True)
                    # The reaped gigs are now 'pending' again; emit a summary
                    # event (per-sub-job identification isn't available post-reap).
                    _job_event("(reaper)", "REAPED", count=n)
            except Exception as e:  # pragma: no cover
                print(f"[fixer] reaper error: {e}", flush=True)

    def _sampler() -> None:
        # Sample throughput/counts into the ring buffer so the dashboard can draw
        # an at-field-style rate curve over time (TRUE rate from /status window).
        # Also snapshot per-job done counts (top groups) so each "job" gets its
        # own progress-over-time curve, and per-disk done counts (N6) so each
        # spindle gets its own throughput sparkline.
        while not app.state._stop.wait(2.0):
            try:
                s = store.stats()
                groups = {g["job"]: g["done"]
                          for g in store.group_stats(limit=40)}
                disk_done = store.disk_done_counts() if app.state.disks else {}
                # Per-runner contribution per job so the job page can render
                # a stacked-area "who is contributing what" chart and detect
                # per-node plateaus (a computer that stopped making progress).
                groups_by_runner = store.group_runner_done_counts(
                    limit_groups=40
                )
                app.state.metrics.append({
                    "ts": s["ts"],
                    "rate": s.get("rate_per_s", 0.0),
                    "done": s.get("done", 0),
                    "pending": s.get("pending", 0),
                    "leased": s.get("leased", 0),
                    "failed": s.get("failed", 0),
                    "groups": groups,
                    "groups_by_runner": groups_by_runner,
                    "disk_done": disk_done,
                })
            except Exception:  # pragma: no cover
                pass

    @app.on_event("startup")
    def _start() -> None:
        t = threading.Thread(target=_reaper, name="kiroshi-reaper", daemon=True)
        t.start()
        app.state._reaper = t
        ts = threading.Thread(target=_sampler, name="kiroshi-sampler", daemon=True)
        ts.start()
        app.state._sampler = ts
        if app.state.enable_advisories:
            app.state.advisory_detector.start()
            app.state.advisory_dispatcher.start()

    @app.on_event("shutdown")
    def _stop() -> None:
        app.state._stop.set()
        if app.state.enable_advisories:
            app.state.advisory_detector.stop()
            app.state.advisory_dispatcher.stop()

    @app.post("/seed")
    def seed(req: SeedReq) -> dict[str, int]:
        # Derive each sub-job's physical disk from the topology (if declared) so /lease
        # can budget per-spindle. A sub-job that already carries a disk (set by the
        # task's enumerate_gigs) wins; only absent ones are derived. No topology =>
        # disk stays None (uncapped / inert).
        disks = app.state.disks
        gigs = [g.model_dump() for g in req.gigs]
        if disks:
            from .storage import derive_disk

            for g in gigs:
                if not g.get("disk"):
                    d = derive_disk(g["subjob_id"], g.get("spec", {}), disks)
                    if d:
                        g["disk"] = d
        inserted = store.seed(gigs, job=req.job, label=req.label)
        # Record SEEDED job events (capped for bulk seeds).
        new_ids = [g["subjob_id"] for g in gigs]
        if len(new_ids) <= 5000:
            for jid in new_ids:
                _job_event(jid, "SEEDED")
        else:
            _job_event("(bulk)", "SEEDED_BULK", count=len(new_ids))
        # M9: remember which origin(s) seeded which job, so an advisory that
        # trips on a spindle can name (and optionally webhook back to) whoever
        # launched the work. Deduped by (kind, agent_id, callback) so re-seeding
        # doesn't stack copies of the same launcher.
        if req.origin:
            # Any job referenced by this seed request should carry the origin.
            grps: set[str] = set()
            if req.job:
                grps.add(req.job)
            for g in gigs:
                gg = g.get("job") or req.job
                if gg:
                    grps.add(gg)
            if not grps:
                # Ungrouped gigs live under the sentinel job name from jobstore.
                from .jobstore import UNGROUPED
                grps.add(UNGROUPED)
            for job in grps:
                lst = app.state.origins_by_group.setdefault(job, [])
                key = (req.origin.get("kind"),
                       req.origin.get("agent_id"),
                       req.origin.get("callback"))
                if not any(
                    (o.get("kind"), o.get("agent_id"), o.get("callback")) == key
                    for o in lst
                ):
                    lst.append(dict(req.origin))
        return {"inserted": inserted, "received": len(req.gigs)}

    def _touch_runner(runner_id: str, host: str) -> None:
        r = app.state.runners.get(runner_id)
        now = time.time()
        if r is None:
            app.state.runners[runner_id] = {
                "runner_id": runner_id, "host": host, "launch_command": "",
                "task": "", "workers": 0, "pid": None, "log_path": None,
                "started_at": now, "last_seen": now,
            }
        else:
            r["last_seen"] = now
            if host and host != "?":
                r["host"] = host

    def _job_event(subjob_id: str, event: str, **fields) -> None:
        """Record a per-job coordination transition in the bounded ring buffer +
        a per-job index for O(1) trace lookup. No-op effect on the store.

        Both the events ring AND the index dict are bounded — a long-lived Coordinator
        processing millions of gigs across jobs must not leak memory, so the
        index evicts its oldest-tracked subjob_ids once it exceeds the ring size."""
        ts = time.time()
        rec = {"ts": ts, "subjob_id": subjob_id, "event": event, **fields}
        app.state.job_events.append(rec)
        index = app.state.job_event_index
        idx = index.get(subjob_id)
        if idx is None:
            idx = deque(maxlen=20)
            index[subjob_id] = idx
            # dict preserves insertion order -> pop oldest-tracked jobs first.
            while len(index) > app.state.jobevent_ring:
                index.pop(next(iter(index)), None)
        idx.append(rec)

    @app.post("/register")
    def register(req: RegisterReq) -> dict[str, Any]:
        now = time.time()
        app.state.runners[req.runner_id] = {
            "runner_id": req.runner_id, "host": req.host,
            "launch_command": req.launch_command, "task": req.task,
            "workers": req.workers, "pid": req.pid, "log_path": req.log_path,
            "started_at": now, "last_seen": now,
        }
        print(f"[fixer] runner registered: {req.runner_id} on {req.host} "
              f"({req.workers}w) task={req.task}", flush=True)
        return {"ok": True}

    @app.get("/runners")
    def runners() -> dict[str, Any]:
        now = time.time()
        out = []
        for r in app.state.runners.values():
            d = dict(r)
            d["age_s"] = round(now - r["started_at"], 1)
            d["stale_s"] = round(now - r["last_seen"], 1)
            out.append(d)
        out.sort(key=lambda d: d["runner_id"])
        return {"runners": out, "ts": now}

    @app.get("/metrics/history")
    def metrics_history() -> dict[str, Any]:
        return {"samples": list(app.state.metrics), "ts": time.time()}

    # /groups + its {"groups": [...]} payload are frozen wire-compat names (see
    # the "gigs" note on /lease). The dashboard fetches /groups; renaming it
    # would require lockstep client updates for no functional gain.
    @app.get("/groups")
    def groups(limit: int = 200) -> dict[str, Any]:
        rows = store.group_stats(limit=min(max(limit, 1), 2000))
        idx = app.state.runners
        for g in rows:
            cmds = []
            for rid in g.get("runner_ids", []):
                r = idx.get(rid)
                if r and r.get("launch_command") and r["launch_command"] not in cmds:
                    cmds.append(r["launch_command"])
            g["launch_commands"] = cmds
            g["has_custom_page"] = _has_custom_job_page()
        return {"groups": rows, "ts": time.time()}

    @app.get("/subjobs")
    def jobs(state: Optional[str] = None, limit: int = 200,
             job: Optional[str] = None,
             subjob_id_re: Optional[str] = None,
             error_re: Optional[str] = None) -> dict[str, Any]:
        states = tuple(s.strip() for s in state.split(",")) if state else None
        try:
            rows = store.list_jobs(
                states=states, limit=min(max(limit, 1), 2000), job=job,
                subjob_id_re=subjob_id_re, error_re=error_re)
        except re.error as exc:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=400,
                content={"error": f"bad regex: {exc}", "jobs": []})
        return {"jobs": _attach_launch(rows), "runners": _runner_index(), "ts": time.time()}

    @app.get("/metrics/export")
    def metrics_export(job: Optional[str] = None,
                       state: Optional[str] = "done",
                       limit: int = 100000) -> dict[str, Any]:
        """Bulk per-sub-job metrics for result aggregation across a job.

        Returns ``{rows: [{subjob_id, metrics, state, job, disk}], count, ts}``.
        Unlike ``/jobs`` (dashboard-shaped, capped at 2000), this streams up to
        ``limit`` (default 100k) lightweight rows so a consumer can fold metrics
        across a whole job -- e.g. ranking every clip by worst-section
        error. ``job`` filters to one job; ``state`` defaults to ``done``.
        Ordered by subjob_id for deterministic paging.
        """
        states = tuple(s.strip() for s in state.split(",")) if state else None
        rows = store.export_metrics(job=job, states=states,
                                    limit=min(max(limit, 1), 200000))
        return {"rows": rows, "count": len(rows), "ts": time.time()}

    @app.get("/history")
    def history(limit: int = 500) -> dict[str, Any]:
        rows = store.list_jobs(states=None, limit=min(max(limit, 1), 5000))
        return {"jobs": _attach_launch(rows), "runners": _runner_index(), "ts": time.time()}

    def _runner_index() -> dict[str, Any]:
        return {rid: {"launch_command": r.get("launch_command", ""),
                      "task": r.get("task", ""), "host": r.get("host", "")}
                for rid, r in app.state.runners.items()}

    def _attach_launch(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        idx = app.state.runners
        for d in rows:
            rid = d.get("runner_id")
            r = idx.get(rid) if rid else None
            d["launch_command"] = r.get("launch_command", "") if r else ""
            d["task"] = r.get("task", "") if r else ""
        return rows

    def _effective_ttl(hb: Optional[float]) -> float:
        """Lease lifetime adapted to the Runner's heartbeat cadence (bounded)."""
        floor = app.state.lease_ttl
        if hb and hb > 0:
            floor = max(floor, hb * app.state.lease_miss_tolerance)
        return min(floor, app.state.lease_ttl_cap)

    def _effective_disk_budget() -> Optional[dict[str, int]]:
        """The per-disk read budget available to GIGS = topology budget minus
        read resource-slots currently held by external (non-sub-job) clients. This
        unifies the mesh-global budget: gigs and external workloads draw from
        ONE shared counter, so their combined in-flight never exceeds the cap.
        """
        base = app.state.disk_concurrency
        if not base:
            return None
        # Count active read slots per disk held by external clients
        now = time.time()
        slot_reads: dict[str, int] = {}
        with app.state.resource_lock:
            for s in app.state.resource_slots.values():
                if s["mode"] == "read" and s["disk"] and s["deadline"] >= now:
                    slot_reads[s["disk"]] = slot_reads.get(s["disk"], 0) + 1
        if not slot_reads:
            return base
        return {d: max(0, cap - slot_reads.get(d, 0)) for d, cap in base.items()}

    def _host_share(host: str, budget: Optional[dict[str, int]]) -> Optional[int]:
        """Fair-share in-flight ceiling for ``host`` (or None => uncapped).

        Weight = sum of ``workers`` across this host's *live* runners (seen within
        the lease TTL). The ceiling is this host's proportional slice of the total
        per-disk budget, rounded up, with a floor of 1 so every host always makes
        progress. Auto-adapts as runners join/leave; no static config needed.
        """
        if not app.state.fair_share or not budget:
            return None
        total_budget = sum(budget.values())
        if total_budget <= 0:
            return None
        now = time.time()
        fresh = app.state.lease_ttl
        weights: dict[str, float] = {}
        for r in app.state.runners.values():
            if now - r.get("last_seen", 0) > fresh:
                continue  # stale runner — don't reserve budget for it
            h = (r.get("host") or "?")
            weights[h] = weights.get(h, 0.0) + max(0, int(r.get("workers") or 0))
        total_w = sum(weights.values())
        my_w = weights.get(host, 0.0)
        if total_w <= 0 or my_w <= 0:
            return None  # unknown weights — don't cap (fail open)
        import math
        return max(1, math.ceil(my_w / total_w * total_budget))

    def _record_lease_decision(req: "LeaseReq", res, ttl: float) -> None:
        """Build a LeaseDecision record from the lease result's diag and append
        it to the ring buffer. Emit LEASED JobEvents for each granted sub-job.
        Throttled console log when the host got fewer gigs than requested."""
        diag = res.diag or {}
        ts = time.time()
        decision = {
            "ts": ts,
            "runner_id": req.runner_id,
            "host": req.host,
            "requested_capacity": diag.get("requested_capacity", req.capacity),
            "effective_capacity": diag.get("effective_capacity", req.capacity),
            "granted": diag.get("granted", len(res.gigs)),
            "lease_id": res.lease_id,
            "binding_reason": diag.get("binding_reason", "GRANTED_FULL"),
            "pending_total": diag.get("pending_total", 0),
            "fair_share_ceiling": diag.get("fair_share_ceiling"),
            "host_inflight_before": diag.get("host_inflight_before", 0),
            "disk": diag.get("disk", {}),
            "granted_subjob_ids": diag.get("granted_subjob_ids", []),
            "ttl": ttl,
        }
        app.state.lease_decisions.append(decision)

        # Emit a LEASED event for *every* leased sub-job — not the 32-capped
        # granted_subjob_ids used for the compact decision record — so job_trace is
        # complete. Bounded by the per-lease grant size (workers+buffer, further
        # capped by the disk budget) and by the index eviction in _job_event.
        for g in res.gigs:
            _job_event(g["subjob_id"], "LEASED", host=req.host,
                       runner_id=req.runner_id, lease_id=res.lease_id)

        # Throttled log: at most 1 line per host per ~10s when under-granted.
        granted = decision["granted"]
        requested = decision["requested_capacity"]
        if granted < requested and requested > 0:
            host = req.host
            last = app.state._last_lease_log.get(host, 0.0)
            if ts - last >= 10.0:
                app.state._last_lease_log[host] = ts
                free_map = {d: s["free"] for d, s in decision["disk"].items()}
                print(f"[fixer][lease] host={host} req={requested} "
                      f"granted={granted} reason={decision['binding_reason']} "
                      f"free={free_map} pending={decision['pending_total']}",
                      flush=True)

    @app.post("/lease")
    def lease(req: LeaseReq) -> dict[str, Any]:
        _touch_runner(req.runner_id, req.host)
        ttl = _effective_ttl(req.heartbeat_interval)
        budget = _effective_disk_budget()
        res = store.lease(req.runner_id, req.host, req.capacity, ttl,
                          disk_concurrency=budget,
                          host_share=_host_share(req.host, budget))
        # Dual-path routing (N3): stamp each sub-job's spec with its disk's read/write
        # roots so the task reads the direct spindle share / writes the cached share.
        # Inert without a topology (sub-job has no disk -> spec roots unset -> env fallback).
        if app.state.disks and res.gigs:
            from .storage import inject_roots

            inject_roots(res.gigs, app.state.disks)
        # M9: attach advisories the Runner should see — unscoped ones plus any
        # tied to the disks in this batch. Empty list unless something is
        # actively firing, so the /lease payload shape is unchanged in the
        # steady state. Runner prints them as ``KIROSHI-ADVISORY:`` lines.
        adv_out: list[dict[str, Any]] = []
        if app.state.enable_advisories:
            from .advisories import filter_advisories_for_lease

            leased_disks = {g.get("disk") for g in res.gigs if g.get("disk")}
            active = app.state.advisories.active()
            adv_out = filter_advisories_for_lease(active, leased_disks)
        # NOTE: the wire key "gigs" is an intentional compat name kept from the
        # pre-rename protocol (workers/pipeline read lease["gigs"]). Renaming it
        # would break every in-flight worker, so it stays as a frozen wire term
        # even though the vocabulary is now "sub-jobs". Same for /groups below.
        payload: dict[str, Any] = {
            "lease_id": res.lease_id, "gigs": res.gigs, "ttl": ttl,
        }
        if adv_out:
            payload["advisories"] = adv_out

        # --- Record the lease decision for observability ---
        _record_lease_decision(req, res, ttl)

        return payload

    @app.post("/complete")
    def complete(req: CompleteReq) -> dict[str, int]:
        results = [r.model_dump() for r in req.results]
        for r in results:
            status = r.get("status", "ok")
            event = "COMPLETED" if status in ("ok", "skipped") else "FAILED"
            _job_event(r["subjob_id"], event, status=status,
                       error=r.get("error"))
        return store.complete(results)

    @app.post("/heartbeat")
    def heartbeat(req: HeartbeatReq) -> dict[str, Any]:
        if req.runner_id and req.runner_id != "?":
            _touch_runner(req.runner_id, "?")
        ttl = _effective_ttl(req.heartbeat_interval)
        extended = store.heartbeat(req.lease_id, ttl)
        return {"extended": extended, "ttl": ttl}

    @app.post("/requeue")
    def requeue(req: RequeueReq) -> dict[str, int]:
        n = store.requeue(tuple(req.states), reset_attempts=req.reset_attempts)
        return {"requeued": n}

    @app.get("/task/meta")
    def task_meta() -> dict[str, Any]:
        """Whether this Coordinator serves task code, and its identity/hash (token-gated).

        ``served=False`` means the joining Runner must already have the task
        importable (pre-installed) — the safe default.
        """
        ts = app.state.task_source
        if not ts:
            return {"served": False, "task_ref": None, "sha256": None, "filename": None}
        return {"served": True, "task_ref": ts["task_ref"], "sha256": ts["sha256"],
                "filename": ts["filename"], "module": ts["module"],
                "bytes": len(ts["source"])}

    @app.get("/task/source")
    def task_source_ep() -> JSONResponse:
        """Serve the task source for a consenting joiner (token-gated, opt-in).

        Only present when the Coordinator was started with ``--serve-task``. The client
        (`kiroshi join`) shows the SHA-256 and requires operator approval before
        writing/importing — see SECURITY.md §6.5.
        """
        ts = app.state.task_source
        if not ts:
            return JSONResponse({"error": "this Coordinator does not serve task code"},
                                status_code=404)
        return JSONResponse({
            "task_ref": ts["task_ref"], "module": ts["module"],
            "filename": ts["filename"], "source": ts["source"], "sha256": ts["sha256"],
        })

    @app.get("/status")
    def status() -> JSONResponse:
        st = store.stats()
        # Per-disk observability (N6): attach the budget + disk metadata so the
        # dashboard can render in-flight vs budget per spindle. Inert without a
        # topology (disk_budget empty -> the panel doesn't render).
        if app.state.disk_concurrency:
            st["disk_budget"] = app.state.disk_concurrency
            st["disk_info"] = [
                {"id": d.id, "kind": d.kind, "match": d.match}
                for d in app.state.disks
            ]
            # Resource governor state
            st["resource"] = {
                "has_parity": app.state.has_parity,
                "global_write_budget": app.state.global_write_budget,
                "global_write_inflight": app.state.global_write_inflight,
                "active_resource_slots": len(app.state.resource_slots),
            }
        # Scheduling observability block: per-host grant ratio in the last ~120s.
        st["scheduling"] = _scheduling_summary(window_s=120.0)
        return JSONResponse(st)

    # ================================================================
    # Coordination decision ledger (observability endpoints)
    # ================================================================

    def _scheduling_summary(window_s: float = 300.0) -> dict[str, Any]:
        """Aggregate recent lease decisions into per-host stats. The 'is anyone
        starving?' view: a host with requested>0 and grant_ratio≈0 is starved."""
        now = time.time()
        cutoff = now - window_s
        per_host: dict[str, dict[str, Any]] = {}
        for d in app.state.lease_decisions:
            if d["ts"] < cutoff:
                continue
            h = d["host"]
            agg = per_host.setdefault(h, {
                "requested": 0, "granted": 0, "decisions": 0,
                "reasons": {},
            })
            agg["requested"] += d["requested_capacity"]
            agg["granted"] += d["granted"]
            agg["decisions"] += 1
            reason = d["binding_reason"]
            agg["reasons"][reason] = agg["reasons"].get(reason, 0) + 1
        starved: list[str] = []
        for h, agg in per_host.items():
            req = agg["requested"]
            granted = agg["granted"]
            agg["grant_ratio"] = round(granted / req, 3) if req > 0 else 1.0
            top = max(agg["reasons"], key=agg["reasons"].get) if agg["reasons"] else ""
            agg["main_reason"] = top
            if req > 0 and agg["grant_ratio"] < 0.05 and agg["decisions"] >= 2:
                starved.append(h)
        return {
            "window_s": window_s,
            "per_host": per_host,
            "starved_hosts": starved,
            "ts": now,
        }

    @app.get("/lease/decisions")
    def lease_decisions(
        host: Optional[str] = None,
        reason: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Recent lease decisions (newest-first). Filter by host / reason / since."""
        limit = min(max(limit, 1), 2000)
        out = []
        for d in reversed(app.state.lease_decisions):
            if host and d["host"] != host:
                continue
            if reason and d["binding_reason"] != reason:
                continue
            if since and d["ts"] < since:
                continue
            out.append(d)
            if len(out) >= limit:
                break
        return {"decisions": out, "count": len(out), "ts": time.time()}

    @app.get("/subjob/trace")
    def job_trace(subjob_id: str) -> dict[str, Any]:
        """Coordination timeline for one job/sub-job: seeded/leased/completed/
        failed/requeued/expired events, plus the job's current DB row."""
        events = list(app.state.job_event_index.get(subjob_id, []))
        row = store.job(subjob_id)
        return {"subjob_id": subjob_id, "events": events, "current": row,
                "ts": time.time()}

    @app.get("/decisions/summary")
    def decisions_summary(window_s: float = 300.0) -> dict[str, Any]:
        """Aggregated scheduling health: per-host grant ratio + starved hosts."""
        return _scheduling_summary(window_s=window_s)

    @app.get("/advisories")
    def advisories_list(
        since: Optional[float] = None,
        severity: Optional[str] = None,
        disk: Optional[str] = None,
        active_only: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Structured Coordinator-side warnings for humans / monitors / agents (M9).

        Query params:
          - ``since``: unix timestamp; only newer entries.
          - ``severity``: one of ``info|warn|critical``.
          - ``disk``: filter to one spindle.
          - ``active_only``: hide resolved fingerprints.
          - ``limit``: cap (default 200; max 1000).

        Returns ``{advisories: [...], active: N, ts: <now>}`` where each entry
        is the JSON of :class:`~kiroshi.advisories.Advisory`.
        """
        items = app.state.advisories.list(
            since=since, severity=severity, disk=disk,
            active_only=active_only, limit=min(max(limit, 1), 1000),
        )
        return {
            "advisories": [a.to_dict() for a in items],
            "active": len(app.state.advisories.active()),
            "ts": time.time(),
        }

    @app.get("/storage")
    def storage() -> dict[str, Any]:
        """Storage topology + live per-disk in-flight/budget for the dashboard.
        Empty when no topology is configured (inert)."""
        if not app.state.disks:
            return {"disks": [], "budget": {}}
        return {
            "disks": [{"id": d.id, "kind": d.kind, "match": d.match,
                        "read": d.read, "write": d.write}
                      for d in app.state.disks],
            "budget": app.state.disk_concurrency,
        }

    @app.get("/subjob/{subjob_id:path}")
    def job_detail(subjob_id: str) -> JSONResponse:
        d = store.job(subjob_id)
        if d is None:
            return JSONResponse({"error": "not found", "subjob_id": subjob_id}, status_code=404)
        r = app.state.runners.get(d.get("runner_id")) if d.get("runner_id") else None
        d["launch_command"] = r.get("launch_command", "") if r else ""
        d["runner_log_path"] = r.get("log_path") if r else None
        d["has_custom_page"] = _has_custom_job_page()
        return JSONResponse(d)

    def _has_custom_job_page() -> bool:
        pp = app.state.pages_path
        return bool(pp and (pp / "job.html").is_file())

    def _serve(name: str) -> str:
        f = _DASHBOARD.parent / name
        if f.is_file():
            return f.read_text(encoding="utf-8")
        return f"<h1>Kiroshi</h1><p>UI asset missing: {name}</p>"

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return _serve("index.html")

    @app.get("/ui/jobs", response_class=HTMLResponse)
    def ui_jobs() -> str:
        return _serve("jobs.html")

    @app.get("/ui/history", response_class=HTMLResponse)
    def ui_history() -> str:
        return _serve("history.html")

    @app.get("/ui/job", response_class=HTMLResponse)
    def ui_job() -> str:
        return _serve("job.html")

    @app.get("/ui/advisory_notifier.js")
    def ui_advisory_notifier() -> Response:
        """Shared JS included by every dashboard page: polls /advisories and
        raises a native Windows notification (plus in-page toast) whenever a
        new NAS-contention/thrash/saturation/failure advisory fires. Keeping
        it in one file means /, /ui/jobs, /ui/history, /ui/job all get the
        popup behavior without duplication."""
        f = _DASHBOARD.parent / "advisory_notifier.js"
        body = f.read_text(encoding="utf-8") if f.is_file() else "/* missing */"
        return Response(content=body, media_type="application/javascript")

    # ================================================================
    # Mesh resource governor: standalone resource-acquire service
    # ================================================================

    @app.post("/resource/acquire")
    def resource_acquire(body: dict = Body(...)) -> JSONResponse:
        """Acquire a read (per-disk) or write (global-parity) resource slot.

        Any process — not just Kiroshi gigs — can call this to coordinate I/O
        across the mesh. The Coordinator is the single mesh-global arbiter.

        Request: {"slot_id": "...", "disk": "disk3"|"None", "mode": "read"|"write", "ttl": 120}
        Response: {"granted": true} or {"granted": false, "retry_after": 0.5}
        If no topology / no parity: always granted (inert, HW-config-gated).
        """
        slot_id = body.get("slot_id", "")
        disk = body.get("disk")
        mode = (body.get("mode") or "read").lower()
        ttl = float(body.get("ttl", 120))

        # Reap expired slots first
        now = time.time()
        with app.state.resource_lock:
            expired = [sid for sid, s in app.state.resource_slots.items()
                       if s["deadline"] < now]
            for sid in expired:
                s = app.state.resource_slots.pop(sid)
                if s["mode"] == "write":
                    app.state.global_write_inflight -= 1

        with app.state.resource_lock:
            if mode == "write":
                # Global parity-write budget
                if not app.state.has_parity or app.state.global_write_budget == 0:
                    # No parity protection — always grant (inert for NVMe/SSD)
                    app.state.resource_slots[slot_id] = {
                        "disk": None, "mode": "write", "deadline": now + ttl}
                    return JSONResponse({"granted": True})
                if app.state.global_write_inflight >= app.state.global_write_budget:
                    return JSONResponse({"granted": False, "retry_after": 0.5},
                                        status_code=503)
                app.state.global_write_inflight += 1
                app.state.resource_slots[slot_id] = {
                    "disk": None, "mode": "write", "deadline": now + ttl}
                return JSONResponse({"granted": True})

            else:  # read — per-disk budget
                budget_map = app.state.disk_concurrency
                if not budget_map or disk not in budget_map:
                    # No budget for this disk — always grant
                    app.state.resource_slots[slot_id] = {
                        "disk": disk, "mode": "read", "deadline": now + ttl}
                    return JSONResponse({"granted": True})
                # Count in-flight reads on this disk (from both gigs + resource slots)
                inflight_gigs = store.disk_inflight_count(disk) if hasattr(store, "disk_inflight_count") else 0
                inflight_slots = sum(1 for s in app.state.resource_slots.values()
                                     if s["mode"] == "read" and s["disk"] == disk)
                total_inflight = inflight_gigs + inflight_slots
                if total_inflight >= budget_map[disk]:
                    return JSONResponse({"granted": False, "retry_after": 0.5},
                                        status_code=503)
                app.state.resource_slots[slot_id] = {
                    "disk": disk, "mode": "read", "deadline": now + ttl}
                return JSONResponse({"granted": True})

    @app.post("/resource/renew")
    def resource_renew(body: dict = Body(...)) -> JSONResponse:
        """Extend a held slot's TTL (heartbeat) so a long-running hold — a
        multi-minute download, a large file stage — isn't reaped and
        over-subscribed. Re-grants the slot if it was already reaped (best
        effort, subject to the same budget)."""
        slot_id = body.get("slot_id", "")
        ttl = float(body.get("ttl", 120))
        now = time.time()
        with app.state.resource_lock:
            slot = app.state.resource_slots.get(slot_id)
            if slot is not None:
                slot["deadline"] = now + ttl
                return JSONResponse({"renewed": True})
        return JSONResponse({"renewed": False}, status_code=404)

    @app.post("/resource/release")
    def resource_release(body: dict = Body(...)) -> JSONResponse:
        """Release a previously acquired resource slot."""
        slot_id = body.get("slot_id", "")
        with app.state.resource_lock:
            slot = app.state.resource_slots.pop(slot_id, None)
            if slot and slot["mode"] == "write":
                app.state.global_write_inflight = max(0, app.state.global_write_inflight - 1)
        return JSONResponse({"released": slot is not None})

    @app.get("/resource/status")
    def resource_status() -> dict[str, Any]:
        """Live view of resource slots + per-disk I/O saturation for observability."""
        now = time.time()
        with app.state.resource_lock:
            active = [s for s in app.state.resource_slots.values() if s["deadline"] >= now]
        result = {
            "active_slots": len(active),
            "global_write_inflight": app.state.global_write_inflight,
            "global_write_budget": app.state.global_write_budget,
            "has_parity": app.state.has_parity,
            "read_budget": app.state.disk_concurrency,
        }
        # I/O watcher rolling-window stats (which spindle is the wall)
        if hasattr(app.state, "io_watcher") and app.state.io_watcher:
            result["io"] = app.state.io_watcher.snapshot()
        return result

    return app
