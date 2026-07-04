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
            "'status' is the ONE dashboard call (fleet + jobs + resources + mesh "
            "coverage). 'seed_gigs' to enqueue, 'validate_pipeline'/'tick_pipeline' "
            "for multi-stage work. 'search_subjobs' greps individual sub-jobs — NOT "
            "jobs. Before seeding a NAS job, call 'advise_io'. Read "
            "'kiroshi://capabilities.json' and 'kiroshi://agents.md' first."
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

    @app.tool(description="ONE-CALL mesh dashboard from a coordinator. Returns "
                          "fleet + jobs[] with per-job progress, ETA, launch_commands, "
                          "runners, resources (CPU/RAM/GPU/VRAM/disk), health/stall "
                          "detection, error_digest, action_hint, and mesh coverage "
                          "(configured vs live hosts). Top-level total/done/pending "
                          "fields are SUB-JOB counts (legacy dashboard compat).")
    def status(coordinator: Optional[str] = None,
               token: Optional[str] = None) -> dict:
        return _get(_fx(coordinator), "/status", _tk(token))

    @app.tool(description="List currently-active coordinator advisories (NAS "
                          "throughput collapse, sub-job failure spike, etc.).")
    def list_advisories(coordinator: Optional[str] = None,
                        token: Optional[str] = None) -> dict:
        return _get(_fx(coordinator), "/advisories", _tk(token))

    @app.tool(description="List registered runners and their heartbeats "
                          "(authoritative for 'is my runner alive'). Prefer "
                          "'status' for the full picture — runners also appear "
                          "in status.fleet.runners with resources + code_fingerprint.")
    def list_runners(coordinator: Optional[str] = None,
                     token: Optional[str] = None) -> dict:
        return _get(_fx(coordinator), "/runners", _tk(token))

    @app.tool(description="Get the loaded storage topology (disks, read/write "
                          "roots, concurrency budgets).")
    def get_storage(coordinator: Optional[str] = None,
                    token: Optional[str] = None) -> dict:
        return _get(_fx(coordinator), "/storage", _tk(token))

    @app.tool(description="STORAGE-CLASS GUIDANCE for a job's I/O paths — call this "
                          "BEFORE seeding a NAS job. Given read_root/write_root (and "
                          "optionally a sample input/output path), returns static "
                          "fast-path advice: is the input on NVMe (fast) or an HDD "
                          "array (shard + read the direct spindle share)? Do writes "
                          "hit a parity array (RMW bottleneck)? Are SMB creds set (or "
                          "will it fall back to the slow Windows redirector)? No "
                          "benchmarking — pure classification against the topology.")
    def advise_io(read_root: Optional[str] = None, write_root: Optional[str] = None,
                  sample_src: Optional[str] = None, sample_dst: Optional[str] = None,
                  coordinator: Optional[str] = None,
                  token: Optional[str] = None) -> dict:
        from . import iohint
        disks = _disks_for_advice(coordinator, token)
        adv = iohint.advise_job(read_root=read_root, write_root=write_root,
                                sample_src=sample_src, sample_dst=sample_dst,
                                disks=disks)
        out = adv.as_dict()
        out["lines"] = adv.lines()
        out["topology_disks"] = len(disks)
        return out

    def _disks_for_advice(coordinator: Optional[str],
                          token: Optional[str]) -> list:
        """Full-fidelity DiskConfig list for advice. Prefer the local topology
        (has parity/direct/cache fields); fall back to the coordinator's /storage
        so an agent on a laptop still gets useful advice."""
        from .storage import DiskConfig, load_topology
        try:
            local = load_topology()
        except Exception:  # noqa: BLE001
            local = []
        if local:
            return local
        try:
            data = _get(_fx(coordinator), "/storage", _tk(token))
        except Exception:  # noqa: BLE001
            return []
        disks = []
        for d in (data.get("disks") or []):
            disks.append(DiskConfig(
                id=d.get("id", ""), kind=d.get("kind", "hdd"),
                read=d.get("read"), write=d.get("write"), match=d.get("match", ""),
                parity_protected=bool(d.get("parity_protected")),
                direct_path=d.get("direct_path"), cache_tier=d.get("cache_tier")))
        return disks

    @app.tool(description="Enqueue gigs into a coordinator. `gigs` is a list of "
                          "{subjob_id, spec}; duplicates by subjob_id are ignored. "
                          "FAIL-CLOSED I/O gate: if the gigs' paths are on a slow "
                          "trade-off path (parity write, non-direct read, no SMB "
                          "creds, unclassified NAS) this REFUSES with the fast "
                          "alternative + the io_ack token to proceed anyway. Fix the "
                          "paths (preferred) or pass io_ack=['<token>'].")
    def seed_gigs(gigs: list[dict], job: str, label: str = "",
                  io_ack: Optional[list[str]] = None,
                  coordinator: Optional[str] = None,
                  token: Optional[str] = None) -> dict:
        from . import iohint
        # Sample the first gig's declared I/O and gate the whole batch on it.
        spec0 = (gigs[0].get("spec") or {}) if gigs else {}
        adv = iohint.advise_job(
            read_root=spec0.get("read_root"), write_root=spec0.get("write_root"),
            sample_src=spec0.get("src_path") or spec0.get("input"),
            sample_dst=spec0.get("dst_path"),
            disks=_disks_for_advice(coordinator, token))
        res = iohint.gate(adv, io_ack)
        if res.blocked and iohint.gate_enabled():
            raise ValueError(iohint.block_message(res, ack_syntax="io_ack="))
        return _post(_fx(coordinator), "/seed", _tk(token),
                     {"gigs": gigs, "job": job, "label": label})

    def _search_subjobs_impl(regex: str = "", field: str = "subjob_id",
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

    @app.tool(description="Search SUB-JOBS (not jobs) by regex on subjob_id "
                          "(default) or error, filtered by state/job slug. "
                          "For a job-level overview use 'status' instead.")
    def search_subjobs(regex: str = "", field: str = "subjob_id",
                       state: str = "", job: str = "", limit: int = 200,
                       coordinator: Optional[str] = None,
                       token: Optional[str] = None) -> dict:
        return _search_subjobs_impl(regex, field, state, job, limit,
                                    coordinator, token)

    @app.tool(description="Deprecated alias for search_subjobs — searches individual "
                          "sub-jobs, NOT job slugs. Prefer search_subjobs or status.")
    def search_jobs(regex: str = "", field: str = "subjob_id",
                    state: str = "", job: str = "", limit: int = 200,
                    coordinator: Optional[str] = None,
                    token: Optional[str] = None) -> dict:
        return _search_subjobs_impl(regex, field, state, job, limit,
                                    coordinator, token)

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

    @app.tool(description="Write-back mover: flush an NVMe cache dir onto the "
                          "sharded HDD array, ONLY when the array is idle. Seeds an "
                          "idle-gated copy job (leases withheld until HDD util stays "
                          "<= util_pct for sustain_min). dst_glob uses '*' for one "
                          "shard per spindle (e.g. '/mnt/user/Lubu*/Dataset'); "
                          "placement is a stable per-path hash (idempotent). Writes "
                          "direct /mnt/diskN paths by design. Set now=True to skip "
                          "the gate. Returns the enumerated file count + spindle "
                          "distribution; run a 'kiroshi.demote:run' runner to execute.")
    def demote(src_root: str, dst_glob: str, coordinator: str,
               n_disks: int = 7, pattern: str = "*",
               util_pct: float = 15.0, sustain_min: float = 30.0,
               watch_disks: Optional[list[str]] = None, now: bool = False,
               union_mount: str = "/mnt/user",
               token: Optional[str] = None) -> dict:
        from . import demote as _demote
        dest_tmpl = _demote.expand_lubu_glob(dst_glob, union_mount=union_mount)
        gigs = list(_demote.enumerate_gigs(
            {"from": src_root, "to": dst_glob, "n_disks": n_disks,
             "pattern": pattern, "union_mount": union_mount}))
        if not gigs:
            return {"gig_count": 0, "coordinator": coordinator,
                    "error": "no files found to demote"}
        job = f"demote-{int(time.time())}"
        payload: dict = {"gigs": gigs, "job": job,
                         "label": f"demote: {src_root} -> {dest_tmpl}"}
        if not now:
            payload["idle_gate"] = {"disks": watch_disks, "util_pct": util_pct,
                                    "sustain_min": sustain_min}
        _post(_fx(coordinator), "/seed", _tk(token), payload)
        per_disk: dict[str, int] = {}
        for g in gigs:
            per_disk[g["disk"]] = per_disk.get(g["disk"], 0) + 1
        return {"gig_count": len(gigs), "job": job, "coordinator": coordinator,
                "dest_template": dest_tmpl, "spindle_distribution": per_disk,
                "idle_gated": (not now), "task": "kiroshi.demote:run"}

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

    @app.tool(description="Paragraph-form health diagnosis of ONE job slug: "
                          "progress %, resources, active advisories, recent errors — "
                          "shaped to paste straight into context. Prefer 'status' "
                          "for the full fleet; use this for a single-job paragraph.")
    def campaign_health(job: str, limit_errors: int = 5,
                        coordinator: Optional[str] = None,
                        token: Optional[str] = None) -> dict:
        fx, tk = _fx(coordinator), _tk(token)
        st = _get(fx, "/status", tk)
        advisories = _get(fx, "/advisories", tk, active_only="true", limit=100)
        job_row = None
        for j in st.get("jobs") or []:
            if j.get("job") == job or j.get("label") == job:
                job_row = j
                break
        return _campaign_health(job, limit_errors, job_row, advisories, st)

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
                          "(safety guard against accidental mass-stop). "
                          "Set force=True to skip drain and hard-kill immediately; "
                          "grace>0 waits that many seconds before escalating.")
    def stop(role: Optional[str] = None, pid: Optional[int] = None,
             all: bool = False, force: bool = False, grace: float = 0.0) -> dict:
        return _stop_impl(role, pid, all, force=force, grace=grace)

    @app.tool(description="Immediately hard-kill LOCAL registered coordinator/Runner "
                          "process trees (no graceful drain, no grace period). "
                          "Same as 'kiroshi force-kill' / 'stop --force'. "
                          "Use when workers are wedged and won't observe a drain flag.")
    def force_kill(role: Optional[str] = None, pid: Optional[int] = None,
                   all: bool = False) -> dict:
        return _stop_impl(role, pid, all, force=True)

    # keep the tool function body thin so the logic is unit-testable without
    # needing to go through FastMCP's async tool-dispatch layer.

    return app


def _campaign_health(job_slug: str, limit_errors: int,
                     job: Optional[dict], advisories: Any, status: Any) -> dict:
    """Compose a paste-ready job diagnosis from enriched /status + advisories."""
    relevant: list[dict] = []
    if isinstance(advisories, dict):
        for a in advisories.get("advisories", []) or []:
            relevant.append(a)
    errors = []
    if job:
        errors = list(job.get("error_digest") or [])[: max(0, int(limit_errors))]
    if not errors and isinstance(status, dict):
        errors = (status.get("recent_errors") or [])[: max(0, int(limit_errors))]
    return {
        "summary": _format_summary(job_slug, job, relevant, errors, status),
        "job": job,
        "advisories": relevant,
        "errors": errors,
    }


def _format_summary(job_slug: str, job: Optional[dict],
                    advisories: list[dict], errors: list,
                    status: Any) -> str:
    """One deterministic paragraph an agent can paste directly (no LLM)."""
    parts: list[str] = []
    if job:
        parts.append(
            f"Job {job_slug!r} ({job.get('label') or job_slug}): "
            f"{job.get('subjobs_done', job.get('done', 0))}/"
            f"{job.get('subjobs_total', job.get('total', 0))} sub-jobs done "
            f"({job.get('pct_done', 0):.0f}%), "
            f"{job.get('subjobs_failed', job.get('failed', 0))} failed, "
            f"{job.get('subjobs_pending', job.get('pending', 0))} pending, "
            f"{job.get('subjobs_leased', job.get('leased', 0))} in-flight."
        )
        if job.get("health_detail"):
            parts.append(str(job["health_detail"]) + ".")
        if job.get("rate_per_s") is not None:
            parts.append(f"Job rate ~{job.get('rate_per_s')}/s.")
        res = job.get("resources") or {}
        if res:
            parts.append(
                f"Resources: {res.get('workers', 0)} workers, "
                f"CPU {res.get('cpu_pct', 0):.0f}%, "
                f"RAM {res.get('process_tree_rss_gb', res.get('mem_used_gb', 0)):.1f}GB, "
                f"GPU {res.get('gpu_util_pct', 0):.0f}%, "
                f"VRAM {res.get('vram_used_gb', 0):.1f}/"
                f"{res.get('vram_total_gb', 0):.1f}GB."
            )
        cmds = job.get("launch_commands") or []
        if cmds:
            parts.append(f"Command: {cmds[0][:160]}.")
        if job.get("action_hint"):
            parts.append(f"Hint: {job['action_hint']}.")
    else:
        parts.append(f"No job matched {job_slug!r} in status.jobs.")
    if isinstance(status, dict):
        mesh = ((status.get("fleet") or {}).get("mesh") or {})
        missing = mesh.get("missing_hosts") or []
        if missing:
            parts.append(f"Missing mesh hosts: {', '.join(missing)}.")
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
        if errors and isinstance(errors[0], dict) and "count" in errors[0]:
            parts.append("Top errors: " + "; ".join(
                f"{e.get('error', '?')[:80]} ×{e.get('count', 1)}" for e in errors))
        else:
            parts.append(f"Recent errors (up to {len(errors)}): " + "; ".join(
                f"{e.get('subjob_id', '?')}: {(e.get('error') or '')[:120]}"
                for e in errors))
    return " ".join(parts)


def _stop_impl(role: Optional[str] = None, pid: Optional[int] = None,
               all: bool = False, *, force: bool = False,
               grace: float = 0.0) -> dict:
    """Stop / force-kill logic, extracted for direct unit testing.

    Mirrors the CLI safety guard: if multiple processes match and neither
    ``pid`` nor ``all=True`` is given, returns an ambiguous result WITHOUT
    stopping anything. Default MCP ``stop`` requests a drain and returns
    immediately (grace=0); use ``force_kill`` or ``force=True`` to hard-kill.
    """
    from .stopctl import stop_registered

    result = stop_registered(
        role=role,
        pid=pid,
        all=all,
        force=force,
        grace=float(grace or 0.0),
        no_escalate=(not force and float(grace or 0.0) <= 0),
    )
    messages = list(result.get("messages") or [])
    out: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "stopped": int(result.get("stopped") or 0),
        "killed": int(result.get("killed") or 0),
        "force": bool(result.get("force") or force),
        "ambiguous": bool(result.get("ambiguous")),
        "matches": list(result.get("matches") or []),
        "messages": messages,
    }
    if messages:
        out["message"] = messages[-1] if len(messages) == 1 else "; ".join(messages)
    elif not out["ambiguous"]:
        out["message"] = "no matching registered processes"
    return out


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
