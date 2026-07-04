"""kiroshi.mcp_server — Model Context Protocol server exposing Kiroshi.

An MCP-compatible LLM agent (Claude Desktop, Cursor, custom clients, etc.)
can enumerate + call Kiroshi's capabilities as **typed tools + resources**
without reading the source. This is the strategic alternative to bespoke
per-agent glue (which is what an older external "cascade seeder" would have
required for each new agent that wanted to drive Kiroshi).

Ships as an OPTIONAL install:
    pip install "kiroshi[mcp]"

Start via:
    kiroshi mcp                          # stdio transport (default)

Design principles:

  * **Thin over existing HTTP.** Every tool is a wrapper around a coordinator
    endpoint already exercised by the CLI and dashboard — no new server
    surface, no auth surface. If ``kiroshi status`` works, so does the
    ``status`` MCP tool. This keeps the security posture identical.
  * **Everything the AGENTS.md doc describes, plus the machine-readable
    capability map, is exposed as a resource.** So an agent connecting cold
    reads ``kiroshi://agents.md`` + ``kiroshi://capabilities.json`` and
    knows what to do — no source-diving.
  * **No hidden state.** coordinator URLs + tokens are tool arguments (or read
    from the local ``kiroshi.local.toml`` when the agent doesn't pass
    them). Nothing is silently pinned.

The FastMCP decorator style keeps the server compact; the underlying SDK
is ``mcp>=1.0``.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

try:
    from mcp.server.fastmcp import FastMCP  # type: ignore
except ImportError as _exc:  # pragma: no cover — captured by _cmd_mcp
    FastMCP = None
    _IMPORT_ERROR: Optional[Exception] = _exc
else:
    _IMPORT_ERROR = None


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_PIPELINE = REPO_ROOT / "docs" / "PIPELINE.md"
DOCS_AGENTS   = REPO_ROOT / "AGENTS.md"


def _requests():
    """Deferred import so importing this module doesn't force requests
    into headless installs."""
    import requests
    return requests


def _get(coordinator: str, path: str, token: Optional[str], **params) -> Any:
    rq = _requests()
    p = {**params}
    if token:
        p["token"] = token
    r = rq.get(f"{coordinator.rstrip('/')}{path}", params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(coordinator: str, path: str, token: Optional[str], payload: dict) -> Any:
    rq = _requests()
    p = {"token": token} if token else {}
    r = rq.post(f"{coordinator.rstrip('/')}{path}", params=p, json=payload, timeout=60)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"ok": True}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"[unavailable: {exc}]"


# --------------------------------------------------------------------------
# server factory (kept as a factory so tests can build one w/o starting it)
# --------------------------------------------------------------------------

def build_server(default_coordinator: Optional[str] = None,
                 default_token: Optional[str] = None) -> "FastMCP":
    """Assemble the FastMCP server. Kept out of module import so a plain
    ``python -c 'import kiroshi.mcp_server'`` never opens stdio."""
    if FastMCP is None:  # pragma: no cover
        raise RuntimeError(
            f"MCP SDK not installed. Install with: pip install 'kiroshi[mcp]' "
            f"(original ImportError: {_IMPORT_ERROR!r})")

    app = FastMCP(
        name="kiroshi",
        instructions=(
            "Kiroshi mesh work-queue. Prefer high-level tools over raw HTTP: "
            "'submit_pipeline' for multi-stage work, 'seed_gigs' for a single "
            "stage, 'status'/'list_advisories' for observability. Read the "
            "'kiroshi://capabilities.json' and 'kiroshi://agents.md' resources "
            "first if you're new to Kiroshi."
        ),
    )

    # ---- Resources ----------------------------------------------------
    @app.resource("kiroshi://capabilities.json",
                  description="Machine-readable feature map (name, purpose, "
                              "command, when_to_use, when_not).")
    def _cap_json() -> str:
        from . import capabilities as cap
        return cap.as_json()

    @app.resource("kiroshi://agents.md",
                  description="Task-indexed guide for agents using Kiroshi.")
    def _agents_md() -> str:
        return _read_text(DOCS_AGENTS)

    @app.resource("kiroshi://pipeline.md",
                  description="How to declare multi-stage dependent pipelines.")
    def _pipeline_md() -> str:
        return _read_text(DOCS_PIPELINE)

    # ---- Tools (thin wrappers over existing HTTP) --------------------
    _auto_cache: dict[str, str] = {}  # discovered coordinator URL, resolved once

    def _fx(coordinator: Optional[str]) -> str:
        """Resolve the coordinator URL for a tool call.

        Order: explicit per-call ``coordinator`` -> server default -> ``KIROSHI_COORDINATOR``
        env -> LAN beacon discovery. An empty value or the literal ``"auto"``
        forces discovery, so one config (``--fixer auto``) is portable across
        every node and survives host/port/job changes without hardcoding an
        address. Discovery is lazy (first use) + cached, so IDEs that launch the
        MCP server at startup don't pay a UDP round-trip until a tool is called."""
        for cand in (coordinator, default_coordinator,
                     os.environ.get("KIROSHI_COORDINATOR"),
                     os.environ.get("KIROSHI_FIXER")):  # fallback for one release
            c = (cand or "").strip()
            if c and c.lower() != "auto":
                return c
        cached = _auto_cache.get("url")
        if cached:
            return cached
        from .discovery import discover_coordinator
        url = discover_coordinator(timeout=6.0)
        if not url:
            raise ValueError(
                "no coordinator URL: LAN discovery heard no beacon. Pass "
                "--fixer http://HOST:PORT, set KIROSHI_COORDINATOR, or start a coordinator.")
        _auto_cache["url"] = url
        return url

    def _tk(token: Optional[str]) -> Optional[str]:
        return token or default_token or os.environ.get("KIROSHI_TOKEN")

    @app.tool(description="Get a fleet /status snapshot from a coordinator "
                          "(counts, rate, ETA, per-disk in-flight).")
    def status(coordinator: Optional[str] = None,
               token: Optional[str] = None) -> dict:
        return _get(_fx(coordinator), "/status", _tk(token))

    @app.tool(description="List currently-active coordinator advisories (NAS "
                          "throughput collapse, sub-job failure spike, etc.).")
    def list_advisories(coordinator: Optional[str] = None,
                        token: Optional[str] = None) -> dict:
        return _get(_fx(coordinator), "/advisories", _tk(token))

    @app.tool(description="List registered runners and their heartbeats "
                          "(authoritative for 'is my runner alive').")
    def list_runners(coordinator: Optional[str] = None,
                     token: Optional[str] = None) -> dict:
        return _get(_fx(coordinator), "/runners", _tk(token))

    @app.tool(description="Get the loaded storage topology (disks, read/write "
                          "roots, concurrency budgets).")
    def get_storage(coordinator: Optional[str] = None,
                    token: Optional[str] = None) -> dict:
        return _get(_fx(coordinator), "/storage", _tk(token))

    @app.tool(description="Enqueue gigs into a coordinator. `gigs` is a list of "
                          "{subjob_id, spec}; duplicates by subjob_id are ignored.")
    def seed_gigs(gigs: list[dict], job: str, label: str = "",
                  coordinator: Optional[str] = None,
                  token: Optional[str] = None) -> dict:
        return _post(_fx(coordinator), "/seed", _tk(token),
                     {"gigs": gigs, "job": job, "label": label})

    @app.tool(description="Search jobs by regex on subjob_id (default) or error, "
                          "filtered by state/job. Returns matching job rows "
                          "(subjob_id, state, attempts, error, metrics, etc.).")
    def search_jobs(regex: str = "", field: str = "subjob_id",
                    state: str = "", job: str = "", limit: int = 200,
                    coordinator: Optional[str] = None,
                    token: Optional[str] = None) -> dict:
        params = {"limit": min(max(limit, 1), 2000)}
        if state:
            params["state"] = state
        if job:
            params["job"] = job
        if regex:
            if field == "error":
                params["error_re"] = regex
            else:
                params["subjob_id_re"] = regex
        return _get(_fx(coordinator), "/subjobs", _tk(token), **params)

    @app.tool(description="Return a lightweight rows list for one job — "
                          "the fastest way to know which items a stage has "
                          "finished. state defaults to 'done'.")
    def export_metrics(job: str, state: str = "done", limit: int = 100000,
                       coordinator: Optional[str] = None,
                       token: Optional[str] = None) -> dict:
        return _get(_fx(coordinator), "/metrics/export", _tk(token),
                    job=job, state=state, limit=limit)

    @app.tool(description="Validate a kiroshi pipeline .toml spec and return "
                          "the parsed DAG (stages, edges) with no I/O.")
    def validate_pipeline(spec_path: str) -> dict:
        from .pipeline import Pipeline
        p = Pipeline.from_toml(spec_path)
        return {
            "stages": {n: {"coordinator": s.coordinator, "job": s.job,
                           "task": s.task, "has_command": bool(s.command),
                           "produces": list(s.produces)}
                       for n, s in p.stages.items()},
            "edges": [{"from": e.upstream, "to": e.downstream, "kind": e.kind,
                       "k": e.k, "artifact": list(e.artifact)}
                      for e in p.edges],
            "poll_s": p.poll_s,
        }

    @app.tool(description="Apply the pipeline's declared edges once (no loop). "
                          "Idempotent — safe to call repeatedly.")
    def tick_pipeline(spec_path: str,
                      token: Optional[str] = None) -> dict:
        from .pipeline import Pipeline, PipelineCoordinator
        pipe = Pipeline.from_toml(spec_path)
        if token or default_token:
            pipe.token = token or default_token or pipe.token
        log_lines: list[str] = []
        coord = PipelineCoordinator(pipe, log=log_lines.append)
        coord.tick()
        return {"log": log_lines}

    @app.tool(description="Stage (copy) a dataset between storage tiers with "
                          "mesh I/O budgeting. Returns the enumerated sub-job count; "
                          "use 'seed_gigs' or 'kiroshi runner' to execute them.")
    def stage_data(src_root: str, dst_root: str, pattern: str = "*",
                   coordinator: Optional[str] = None,
                   token: Optional[str] = None) -> dict:
        from .staging import enumerate_gigs
        gigs = list(enumerate_gigs(
            {"from": src_root, "to": dst_root, "pattern": pattern}))
        if coordinator and gigs:
            _post(_fx(coordinator), "/seed", _tk(token),
                  {"gigs": gigs, "job": f"stage-{int(time.time())}",
                   "label": f"stage: {src_root} -> {dst_root}"})
        return {"gig_count": len(gigs), "coordinator": coordinator,
                "task": "kiroshi.staging:run"}

    @app.tool(description="Measure TRUE throughput of a job. Either pass "
                          "output_dir (from file mtimes; needs FS access) OR "
                          "coordinator+job (from /jobs completed_at over HTTP).")
    def bench_rate(output_dir: Optional[str] = None, pattern: str = "*",
                   recursive: bool = True,
                   coordinator: Optional[str] = None, job: Optional[str] = None,
                   token: Optional[str] = None) -> dict:
        from . import bench as _bench
        if coordinator and job:
            rows = _get(_fx(coordinator), "/subjobs", _tk(token),
                        state="done", limit=2000, job=job).get("jobs", [])
            times = [r["completed_at"] for r in rows if r.get("completed_at")]
            if not times:
                return {"count": 0, "span_s": 0.0, "items_per_s": 0.0}
            span = max(0.0, max(times) - min(times))
            n = len(times)
            return {"count": n, "span_s": span,
                    "items_per_s": (n / span) if span > 0 else 0.0,
                    "sampled": n >= 2000}
        if not output_dir:
            raise ValueError("bench_rate needs output_dir OR coordinator+job")
        rate = _bench.rate_from_dir(output_dir, pattern=pattern,
                                    recursive=recursive)
        return {"count": rate.count, "span_s": rate.span_s,
                "items_per_s": rate.items_per_s}

    @app.tool(description="Suggest per-disk concurrency from throughput-vs-"
                          "concurrency samples. Pass a list of [concurrency, "
                          "mbps] pairs; returns the recommended concurrency.")
    def bench_calibrate(samples: list[list[float]],
                        bias: str = "balanced") -> dict:
        from . import bench as _bench
        pairs = [(int(s[0]), float(s[1])) for s in samples]
        rec = _bench.suggest_concurrency(pairs, bias=bias)
        peak_conc, peak_mbps = max(pairs, key=lambda s: s[1])
        return {"recommended_concurrency": rec, "bias": bias,
                "peak_mbps": peak_mbps, "peak_at_concurrency": peak_conc}

    # ---- Observability / scheduling (thin over coordinator HTTP) -----------
    # Decision-log tools for debugging node starvation / underutilization.

    @app.tool(description="Recent lease DECISIONS (why each host got N gigs): "
                          "requested vs granted, binding_reason, per-disk budget "
                          "snapshot. Filter by host/reason. Use to debug node "
                          "starvation or underutilization.")
    def lease_decisions(host: str = "", reason: str = "", since: float = 0,
                        limit: int = 100,
                        coordinator: Optional[str] = None,
                        token: Optional[str] = None) -> dict:
        p: dict = {"limit": limit}
        if host:
            p["host"] = host
        if reason:
            p["reason"] = reason
        if since:
            p["since"] = since
        return _get(_fx(coordinator), "/lease/decisions", _tk(token), **p)

    @app.tool(description="Coordination timeline for one job/sub-job "
                          "(seeded/leased/completed/failed/expired events + "
                          "current DB row). Use to trace a single sub-job's lifecycle.")
    def job_trace(subjob_id: str,
                  coordinator: Optional[str] = None,
                  token: Optional[str] = None) -> dict:
        return _get(_fx(coordinator), "/subjob/trace", _tk(token), subjob_id=subjob_id)

    @app.tool(description="Aggregated scheduling health over a window: per-host "
                          "grant ratio, main binding reason, and which hosts are "
                          "STARVED. The 'is anyone starving?' call — use first "
                          "when diagnosing underutilization.")
    def scheduling_summary(window_s: int = 300,
                           coordinator: Optional[str] = None,
                           token: Optional[str] = None) -> dict:
        return _get(_fx(coordinator), "/decisions/summary", _tk(token),
                    window_s=window_s)

    @app.tool(description="Paragraph-form health diagnosis of ONE job: "
                          "progress %, active advisories on its spindles, and "
                          "recent errors — shaped to paste straight into context. "
                          "The fastest 'how did my run go?' call.")
    def campaign_health(subjob_id: str, limit_errors: int = 5,
                        coordinator: Optional[str] = None,
                        token: Optional[str] = None) -> dict:
        fx, tk = _fx(coordinator), _tk(token)
        groups = _get(fx, "/groups", tk, limit=500)
        advisories = _get(fx, "/advisories", tk, active_only="true", limit=100)
        status = _get(fx, "/status", tk)
        return _campaign_health(subjob_id, limit_errors, groups, advisories, status)

    @app.tool(description="Return failed/stuck gigs to pending (HTTP /requeue). "
                          "States default to ['failed']; set reset_attempts=True "
                          "to clear the retry counter.")
    def requeue(states: list[str] = ["failed"], reset_attempts: bool = True,
                coordinator: Optional[str] = None,
                token: Optional[str] = None) -> dict:
        return _post(_fx(coordinator), "/requeue", _tk(token),
                     {"states": states, "reset_attempts": reset_attempts})

    # ---- Process management tools (local-host only) ------------------
    # These read the local process registry (``processreg``), NOT the coordinator
    # HTTP API. Only meaningful when the MCP server is co-located with the
    # Kiroshi processes to inspect/stop; driving a remote coordinator from a laptop,
    # these describe YOUR laptop's processes.

    @app.tool(description="List Kiroshi processes registered ON THIS HOST "
                          "(local only — reads the process manifest, not the "
                          "coordinator API). Set include_stale=True to see crashed "
                          "processes whose manifest file is still on disk.")
    def ps(include_stale: bool = False) -> list[dict]:
        from .processreg import list_registered
        return list_registered(include_stale=include_stale)

    @app.tool(description="Ask a LOCAL registered coordinator/Runner to drain+exit "
                          "(local host only). Pass role ('coordinator'/'runner') or "
                          "pid. If multiple match and neither pid nor all=True "
                          "is given, returns the list without stopping anything "
                          "(safety guard against accidental mass-stop).")
    def stop(role: Optional[str] = None, pid: Optional[int] = None,
             all: bool = False) -> dict:
        return _stop_impl(role, pid, all)

    # keep the tool function body thin so the logic is unit-testable without
    # needing to go through FastMCP's async tool-dispatch layer.

    return app


def _campaign_health(subjob_id: str, limit_errors: int,
                     groups: Any, advisories: Any, status: Any) -> dict:
    """Compose a paste-ready job diagnosis from /groups + /advisories +
    /status. Extracted for direct unit testing (no FastMCP dispatch needed)."""
    job = None
    for g in (groups.get("groups") if isinstance(groups, dict) else []) or []:
        if g.get("job") == subjob_id or g.get("label") == subjob_id:
            job = g
            break
    relevant: list[dict] = []
    if isinstance(advisories, dict):
        for a in advisories.get("advisories", []) or []:
            # Surface fleet-wide advisories and (absent per-job disk metadata)
            # any active advisory — a job author wants to see them.
            relevant.append(a)
    errors = []
    if isinstance(status, dict):
        errors = (status.get("recent_errors") or [])[: max(0, int(limit_errors))]
    return {
        "summary": _format_summary(subjob_id, job, relevant, errors, status),
        "job": job,
        "advisories": relevant,
        "errors": errors,
    }


def _format_summary(subjob_id: str, job: Optional[dict],
                    advisories: list[dict], errors: list[dict],
                    status: Any) -> str:
    """One deterministic paragraph an agent can paste directly (no LLM)."""
    parts: list[str] = []
    if job:
        done = int(job.get("done", 0))
        failed = int(job.get("failed", 0))
        pending = int(job.get("pending", 0))
        leased = int(job.get("leased", 0))
        total = done + failed + pending + leased
        pct = (100.0 * done / total) if total else 0.0
        parts.append(f"Job {subjob_id!r}: {done}/{total} done ({pct:.0f}%), "
                     f"{failed} failed, {pending} pending, {leased} in-flight.")
    else:
        parts.append(f"No job matched {subjob_id!r} in the coordinator's groups.")
    if isinstance(status, dict) and status.get("rate_per_s") is not None:
        parts.append(f"Fleet throughput ~{status.get('rate_per_s')}/s.")
    if advisories:
        by_sev: dict[str, int] = {}
        for a in advisories:
            by_sev[a.get("severity", "?")] = by_sev.get(a.get("severity", "?"), 0) + 1
        parts.append("Active advisories: "
                     + ", ".join(f"{v} {k}" for k, v in sorted(by_sev.items()))
                     + f". Top: {advisories[0].get('code', '?')} — "
                     + (advisories[0].get("detail", "") or "").strip())
    else:
        parts.append("No active advisories.")
    if errors:
        parts.append(f"Recent errors (up to {len(errors)}): " + "; ".join(
            f"{e.get('subjob_id', '?')}: {(e.get('error') or '')[:120]}" for e in errors))
    return " ".join(parts)


def _stop_impl(role: Optional[str] = None, pid: Optional[int] = None,
               all: bool = False) -> dict:
    """Stop logic, extracted for direct unit testing.

    Mirrors the CLI ``kiroshi stop`` safety guard: if multiple processes match
    and neither ``pid`` nor ``all=True`` is given, returns an ambiguous result
    WITHOUT stopping anything.
    """
    from .processreg import list_registered, request_stop
    procs = list_registered()
    targets = []
    for p in procs:
        if role and p.get("role") != role:
            continue
        if pid is not None and p.get("pid") != pid:
            continue
        targets.append(p)
    if not targets:
        return {"stopped": 0, "message": "no matching registered processes"}
    if len(targets) > 1 and not all and pid is None:
        return {"stopped": 0, "ambiguous": True,
                "message": f"{len(targets)} processes match; "
                           f"pass all=True or pid=<N> to confirm.",
                "matches": [{"role": p.get("role"), "pid": p.get("pid"),
                             "launch_command": p.get("launch_command", "")}
                            for p in targets]}
    stopped = 0
    for p in targets:
        if request_stop(p.get("role", ""), int(p.get("pid", 0))):
            stopped += 1
    return {"stopped": stopped}


def run_stdio(default_coordinator: Optional[str] = None,
              default_token: Optional[str] = None) -> int:
    """Blocking: run the MCP server over stdio. Called by ``kiroshi mcp``."""
    if FastMCP is None:
        print("kiroshi mcp: the MCP SDK is not installed. "
              "Install with: pip install 'kiroshi[mcp]'", file=sys.stderr)
        return 2
    app = build_server(default_coordinator=default_coordinator, default_token=default_token)
    app.run("stdio")
    return 0
