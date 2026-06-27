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


class SeedReq(BaseModel):
    gigs: list[SeedGig]


class LeaseReq(BaseModel):
    runner_id: str
    host: str = "?"
    capacity: int = 100


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
) -> FastAPI:
    app = FastAPI(title="Kiroshi Fixer", version="0.0.1")
    app.state.store = store
    app.state.lease_ttl = lease_ttl
    app.state.token = token
    app.state._stop = threading.Event()
    # In-memory runner registry (launch command, pid, liveness) + throughput
    # time-series for the dashboard rate curve (ring buffer, ~last 30 min @ 2s).
    app.state.runners = {}
    app.state.metrics = deque(maxlen=900)
    app.state.started_at = time.time()
    app.state.launch_command = launch_command or ""

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
        # own progress-over-time curve.
        while not app.state._stop.wait(2.0):
            try:
                s = store.stats()
                groups = {g["grp"]: g["done"]
                          for g in store.group_stats(limit=40)}
                app.state.metrics.append({
                    "ts": s["ts"],
                    "rate": s.get("rate_per_s", 0.0),
                    "done": s.get("done", 0),
                    "pending": s.get("pending", 0),
                    "leased": s.get("leased", 0),
                    "failed": s.get("failed", 0),
                    "groups": groups,
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
        inserted = store.seed([g.model_dump() for g in req.gigs])
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
    def jobs(state: Optional[str] = None, limit: int = 200) -> dict[str, Any]:
        states = tuple(s.strip() for s in state.split(",")) if state else None
        rows = store.list_jobs(states=states, limit=min(max(limit, 1), 2000))
        return {"jobs": _attach_launch(rows), "runners": _runner_index(), "ts": time.time()}

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

    @app.post("/lease")
    def lease(req: LeaseReq) -> dict[str, Any]:
        _touch_runner(req.runner_id, req.host)
        res = store.lease(req.runner_id, req.host, req.capacity, app.state.lease_ttl)
        return {"lease_id": res.lease_id, "gigs": res.gigs, "ttl": app.state.lease_ttl}

    @app.post("/complete")
    def complete(req: CompleteReq) -> dict[str, int]:
        return store.complete([r.model_dump() for r in req.results])

    @app.post("/heartbeat")
    def heartbeat(req: HeartbeatReq) -> dict[str, Any]:
        if req.runner_id and req.runner_id != "?":
            _touch_runner(req.runner_id, "?")
        extended = store.heartbeat(req.lease_id, app.state.lease_ttl)
        return {"extended": extended, "ttl": app.state.lease_ttl}

    @app.post("/requeue")
    def requeue(req: RequeueReq) -> dict[str, int]:
        n = store.requeue(tuple(req.states), reset_attempts=req.reset_attempts)
        return {"requeued": n}

    @app.get("/status")
    def status() -> JSONResponse:
        return JSONResponse(store.stats())

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
