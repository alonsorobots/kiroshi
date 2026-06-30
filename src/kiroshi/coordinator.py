"""The Fixer — coordinator HTTP service.

FastAPI app over a local :class:`~kiroshi.jobstore.JobStore`. Hands batches of gigs
to Runners, applies their results, extends leases on heartbeat, and runs a
background reaper that returns dead Runners' leases to the pool (self-heal).

Serves the live Kiroshi dashboard at ``/`` and a JSON snapshot at ``/status``.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
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
    job_id: str
    spec: dict[str, Any] = Field(default_factory=dict)
    group: Optional[str] = None


class SeedReq(BaseModel):
    gigs: list[SeedGig]
    # Batch-wide campaign slug + human-readable label, applied to every gig that
    # doesn't carry its own `group`. Lets `kiroshi seed --group X --label "..."` head
    # a whole campaign in the dashboard instead of fragmenting it per job_id prefix.
    group: Optional[str] = None
    label: Optional[str] = None


class LeaseReq(BaseModel):
    runner_id: str
    host: str = "?"
    capacity: int = 100
    # The Runner's heartbeat cadence (s). The Fixer sizes the lease as a safe
    # multiple of it, so a slow-but-alive Runner isn't reaped into a duplicate.
    heartbeat_interval: Optional[float] = None


class ResultItem(BaseModel):
    job_id: str
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
) -> FastAPI:
    app = FastAPI(title="Kiroshi Fixer", version="0.0.1")
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
    # the Fixer was started with --serve-task; gated + consent-checked client-side.
    app.state.task_source = task_source
    # Storage topology for shard-aware leasing (PLAN §7.6). None/[] => inert (no
    # per-disk budget, plain work-stealing). The mesh-global per-spindle budget is
    # derived here once; only the Fixer can enforce it across the whole fleet.
    app.state.disks = disks or []
    from .storage import disk_concurrency_map

    app.state.disk_concurrency = disk_concurrency_map(app.state.disks)

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
        """Mutual auth: prove to a Runner that *this* Fixer holds the shared
        token, so the Runner can verify the Fixer BEFORE sending its bearer token
        or executing leased work. Defends `--fixer auto` against a rogue Fixer
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
            except Exception as e:  # pragma: no cover
                print(f"[fixer] reaper error: {e}", flush=True)

    def _sampler() -> None:
        # Sample throughput/counts into the ring buffer so the dashboard can draw
        # an at-field-style rate curve over time (TRUE rate from /status window).
        # Also snapshot per-group done counts (top groups) so each "job" gets its
        # own progress-over-time curve, and per-disk done counts (N6) so each
        # spindle gets its own throughput sparkline.
        while not app.state._stop.wait(2.0):
            try:
                s = store.stats()
                groups = {g["grp"]: g["done"]
                          for g in store.group_stats(limit=40)}
                disk_done = store.disk_done_counts() if app.state.disks else {}
                app.state.metrics.append({
                    "ts": s["ts"],
                    "rate": s.get("rate_per_s", 0.0),
                    "done": s.get("done", 0),
                    "pending": s.get("pending", 0),
                    "leased": s.get("leased", 0),
                    "failed": s.get("failed", 0),
                    "groups": groups,
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

    @app.on_event("shutdown")
    def _stop() -> None:
        app.state._stop.set()

    @app.post("/seed")
    def seed(req: SeedReq) -> dict[str, int]:
        # Derive each gig's physical disk from the topology (if declared) so /lease
        # can budget per-spindle. A gig that already carries a disk (set by the
        # task's enumerate_gigs) wins; only absent ones are derived. No topology =>
        # disk stays None (uncapped / inert).
        disks = app.state.disks
        gigs = [g.model_dump() for g in req.gigs]
        if disks:
            from .storage import derive_disk

            for g in gigs:
                if not g.get("disk"):
                    d = derive_disk(g["job_id"], g.get("spec", {}), disks)
                    if d:
                        g["disk"] = d
        inserted = store.seed(gigs, group=req.group, label=req.label)
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

    @app.get("/jobs")
    def jobs(state: Optional[str] = None, limit: int = 200,
             grp: Optional[str] = None) -> dict[str, Any]:
        states = tuple(s.strip() for s in state.split(",")) if state else None
        rows = store.list_jobs(states=states, limit=min(max(limit, 1), 2000), grp=grp)
        return {"jobs": _attach_launch(rows), "runners": _runner_index(), "ts": time.time()}

    @app.get("/metrics/export")
    def metrics_export(grp: Optional[str] = None,
                       state: Optional[str] = "done",
                       limit: int = 100000) -> dict[str, Any]:
        """Bulk per-gig metrics for result aggregation across a campaign.

        Returns ``{rows: [{job_id, metrics, state, grp, disk}], count, ts}``.
        Unlike ``/jobs`` (dashboard-shaped, capped at 2000), this streams up to
        ``limit`` (default 100k) lightweight rows so a consumer can fold metrics
        across a whole campaign -- e.g. ranking every clip by worst-section
        error. ``grp`` filters to one campaign; ``state`` defaults to ``done``.
        Ordered by job_id for deterministic paging.
        """
        states = tuple(s.strip() for s in state.split(",")) if state else None
        rows = store.export_metrics(grp=grp, states=states,
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

    @app.post("/lease")
    def lease(req: LeaseReq) -> dict[str, Any]:
        _touch_runner(req.runner_id, req.host)
        ttl = _effective_ttl(req.heartbeat_interval)
        res = store.lease(req.runner_id, req.host, req.capacity, ttl,
                          disk_concurrency=app.state.disk_concurrency or None)
        # Dual-path routing (N3): stamp each gig's spec with its disk's read/write
        # roots so the task reads the direct spindle share / writes the cached share.
        # Inert without a topology (gig has no disk -> spec roots unset -> env fallback).
        if app.state.disks and res.gigs:
            from .storage import inject_roots

            inject_roots(res.gigs, app.state.disks)
        return {"lease_id": res.lease_id, "gigs": res.gigs, "ttl": ttl}

    @app.post("/complete")
    def complete(req: CompleteReq) -> dict[str, int]:
        return store.complete([r.model_dump() for r in req.results])

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
        """Whether this Fixer serves task code, and its identity/hash (token-gated).

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

        Only present when the Fixer was started with ``--serve-task``. The client
        (`kiroshi join`) shows the SHA-256 and requires operator approval before
        writing/importing — see SECURITY.md §6.5.
        """
        ts = app.state.task_source
        if not ts:
            return JSONResponse({"error": "this Fixer does not serve task code"},
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
        return JSONResponse(st)

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

    @app.get("/job/{job_id:path}")
    def job_detail(job_id: str) -> JSONResponse:
        d = store.job(job_id)
        if d is None:
            return JSONResponse({"error": "not found", "job_id": job_id}, status_code=404)
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

    return app
