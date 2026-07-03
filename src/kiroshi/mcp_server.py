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

  * **Thin over existing HTTP.** Every tool is a wrapper around a Fixer
    endpoint already exercised by the CLI and dashboard — no new server
    surface, no auth surface. If ``kiroshi status`` works, so does the
    ``status`` MCP tool. This keeps the security posture identical.
  * **Everything the AGENTS.md doc describes, plus the machine-readable
    capability map, is exposed as a resource.** So an agent connecting cold
    reads ``kiroshi://agents.md`` + ``kiroshi://capabilities.json`` and
    knows what to do — no source-diving.
  * **No hidden state.** Fixer URLs + tokens are tool arguments (or read
    from the local ``kiroshi.local.toml`` when the agent doesn't pass
    them). Nothing is silently pinned.

The FastMCP decorator style keeps the server compact; the underlying SDK
is ``mcp>=1.0``.
"""
from __future__ import annotations

import json
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


def _get(fixer: str, path: str, token: Optional[str], **params) -> Any:
    rq = _requests()
    p = {**params}
    if token:
        p["token"] = token
    r = rq.get(f"{fixer.rstrip('/')}{path}", params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(fixer: str, path: str, token: Optional[str], payload: dict) -> Any:
    rq = _requests()
    p = {"token": token} if token else {}
    r = rq.post(f"{fixer.rstrip('/')}{path}", params=p, json=payload, timeout=60)
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

def build_server(default_fixer: Optional[str] = None,
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
    def _fx(fixer: Optional[str]) -> str:
        f = fixer or default_fixer or os.environ.get("KIROSHI_FIXER")
        if not f:
            raise ValueError("no fixer URL — pass 'fixer' or set KIROSHI_FIXER")
        return f

    def _tk(token: Optional[str]) -> Optional[str]:
        return token or default_token or os.environ.get("KIROSHI_TOKEN")

    @app.tool(description="Get a fleet /status snapshot from a Fixer "
                          "(counts, rate, ETA, per-disk in-flight).")
    def status(fixer: Optional[str] = None,
               token: Optional[str] = None) -> dict:
        return _get(_fx(fixer), "/status", _tk(token))

    @app.tool(description="List currently-active Fixer advisories (NAS "
                          "throughput collapse, gig failure spike, etc.).")
    def list_advisories(fixer: Optional[str] = None,
                        token: Optional[str] = None) -> dict:
        return _get(_fx(fixer), "/advisories", _tk(token))

    @app.tool(description="List registered runners and their heartbeats "
                          "(authoritative for 'is my runner alive').")
    def list_runners(fixer: Optional[str] = None,
                     token: Optional[str] = None) -> dict:
        return _get(_fx(fixer), "/runners", _tk(token))

    @app.tool(description="Get the loaded storage topology (disks, read/write "
                          "roots, concurrency budgets).")
    def get_storage(fixer: Optional[str] = None,
                    token: Optional[str] = None) -> dict:
        return _get(_fx(fixer), "/storage", _tk(token))

    @app.tool(description="Enqueue gigs into a Fixer. `gigs` is a list of "
                          "{job_id, spec}; duplicates by job_id are ignored.")
    def seed_gigs(gigs: list[dict], group: str, label: str = "",
                  fixer: Optional[str] = None,
                  token: Optional[str] = None) -> dict:
        return _post(_fx(fixer), "/seed", _tk(token),
                     {"gigs": gigs, "group": group, "label": label})

    @app.tool(description="Search jobs by regex on job_id (default) or error, "
                          "filtered by state/group. Returns matching job rows "
                          "(job_id, state, attempts, error, metrics, etc.).")
    def search_jobs(regex: str = "", field: str = "job_id",
                    state: str = "", group: str = "", limit: int = 200,
                    fixer: Optional[str] = None,
                    token: Optional[str] = None) -> dict:
        params = {"limit": min(max(limit, 1), 2000)}
        if state:
            params["state"] = state
        if group:
            params["grp"] = group
        if regex:
            if field == "error":
                params["error_re"] = regex
            else:
                params["job_id_re"] = regex
        return _get(_fx(fixer), "/jobs", _tk(token), **params)

    @app.tool(description="Return a lightweight rows list for one campaign — "
                          "the fastest way to know which items a stage has "
                          "finished. state defaults to 'done'.")
    def export_metrics(group: str, state: str = "done", limit: int = 100000,
                       fixer: Optional[str] = None,
                       token: Optional[str] = None) -> dict:
        return _get(_fx(fixer), "/metrics/export", _tk(token),
                    grp=group, state=state, limit=limit)

    @app.tool(description="Validate a kiroshi pipeline .toml spec and return "
                          "the parsed DAG (stages, edges) with no I/O.")
    def validate_pipeline(spec_path: str) -> dict:
        from .pipeline import Pipeline
        p = Pipeline.from_toml(spec_path)
        return {
            "stages": {n: {"fixer": s.fixer, "group": s.group,
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
                          "mesh I/O budgeting. Returns the enumerated gig count; "
                          "use 'seed_gigs' or 'kiroshi runner' to execute them.")
    def stage_data(src_root: str, dst_root: str, pattern: str = "*",
                   fixer: Optional[str] = None,
                   token: Optional[str] = None) -> dict:
        from .staging import enumerate_gigs
        gigs = list(enumerate_gigs(
            {"from": src_root, "to": dst_root, "pattern": pattern}))
        if fixer and gigs:
            _post(_fx(fixer), "/seed", _tk(token),
                  {"gigs": gigs, "group": f"stage-{int(time.time())}",
                   "label": f"stage: {src_root} -> {dst_root}"})
        return {"gig_count": len(gigs), "fixer": fixer,
                "task": "kiroshi.staging:run"}

    @app.tool(description="Measure TRUE throughput of a campaign. Either pass "
                          "output_dir (from file mtimes; needs FS access) OR "
                          "fixer+group (from /jobs completed_at over HTTP).")
    def bench_rate(output_dir: Optional[str] = None, pattern: str = "*",
                   recursive: bool = True,
                   fixer: Optional[str] = None, group: Optional[str] = None,
                   token: Optional[str] = None) -> dict:
        from . import bench as _bench
        if fixer and group:
            rows = _get(_fx(fixer), "/jobs", _tk(token),
                        state="done", limit=2000, grp=group).get("jobs", [])
            times = [r["completed_at"] for r in rows if r.get("completed_at")]
            if not times:
                return {"count": 0, "span_s": 0.0, "items_per_s": 0.0}
            span = max(0.0, max(times) - min(times))
            n = len(times)
            return {"count": n, "span_s": span,
                    "items_per_s": (n / span) if span > 0 else 0.0,
                    "sampled": n >= 2000}
        if not output_dir:
            raise ValueError("bench_rate needs output_dir OR fixer+group")
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

    # ---- Process management tools (local-host only) ------------------
    # These read the local process registry (``processreg``), NOT the Fixer
    # HTTP API. They are only meaningful when the MCP server is co-located
    # with the Kiroshi processes you want to inspect/stop. If you're driving
    # a remote Fixer from your laptop, these describe YOUR laptop's processes.

    @app.tool(description="Return failed/stuck gigs to pending (HTTP /requeue). "
                          "States default to ['failed']; set reset_attempts=True "
                          "to clear the retry counter.")
    def requeue(states: list[str] = ["failed"], reset_attempts: bool = True,
                fixer: Optional[str] = None,
                token: Optional[str] = None) -> dict:
        return _post(_fx(fixer), "/requeue", _tk(token),
                     {"states": states, "reset_attempts": reset_attempts})

    @app.tool(description="List Kiroshi processes registered ON THIS HOST "
                          "(local only — reads the process manifest, not the "
                          "Fixer API). Set include_stale=True to see crashed "
                          "processes whose manifest file is still on disk.")
    def ps(include_stale: bool = False) -> list[dict]:
        from .processreg import list_registered
        return list_registered(include_stale=include_stale)

    @app.tool(description="Ask a LOCAL registered Fixer/Runner to drain+exit "
                          "(local host only). Pass role ('fixer'/'runner') or "
                          "pid. If multiple match and neither pid nor all=True "
                          "is given, returns the list without stopping anything "
                          "(safety guard against accidental mass-stop).")
    def stop(role: Optional[str] = None, pid: Optional[int] = None,
             all: bool = False) -> dict:
        return _stop_impl(role, pid, all)

    # keep the tool function body thin so the logic is unit-testable without
    # needing to go through FastMCP's async tool-dispatch layer.


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

    return app


def run_stdio(default_fixer: Optional[str] = None,
              default_token: Optional[str] = None) -> int:
    """Blocking: run the MCP server over stdio. Called by ``kiroshi mcp``."""
    if FastMCP is None:
        print("kiroshi mcp: the MCP SDK is not installed. "
              "Install with: pip install 'kiroshi[mcp]'", file=sys.stderr)
        return 2
    app = build_server(default_fixer=default_fixer, default_token=default_token)
    app.run("stdio")
    return 0
