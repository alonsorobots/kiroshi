"""Build the enriched /status snapshot (fleet + per-job dashboard).

Pure functions over store + in-memory coordinator state so agents, CLI, MCP,
and the HTTP endpoint share one shape.
"""
from __future__ import annotations

import time
from typing import Any, Optional

STALL_LEASED_S = 600.0  # leased + zero completions for this long => STALLED


def _runner_hosts(runners: dict[str, Any]) -> set[str]:
    live: set[str] = set()
    now = time.time()
    for r in runners.values():
        if now - float(r.get("last_seen", 0)) > 120.0:
            continue
        h = str(r.get("host") or "").strip()
        if h and h != "?":
            live.add(h)
    return live


def _aggregate_resources(runner_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum/average resource samples from runners attached to one job."""
    if not runner_rows:
        return {}
    workers = sum(int(r.get("workers") or 0) for r in runner_rows)
    cpu = [float((r.get("resources") or {}).get("cpu_pct") or 0) for r in runner_rows]
    mem_tree = sum(float((r.get("resources") or {}).get("process_tree_rss_gb") or 0)
                   for r in runner_rows)
    mem_used = max(float((r.get("resources") or {}).get("mem_used_gb") or 0)
                   for r in runner_rows)
    mem_total = max(float((r.get("resources") or {}).get("mem_total_gb") or 0)
                    for r in runner_rows)
    gpu = [float((r.get("resources") or {}).get("gpu_util_pct") or 0) for r in runner_rows]
    vram_used = max(float((r.get("resources") or {}).get("vram_used_gb") or 0)
                    for r in runner_rows)
    vram_total = max(float((r.get("resources") or {}).get("vram_total_gb") or 0)
                     for r in runner_rows)
    return {
        "workers": workers,
        "cpu_pct": round(max(cpu) if cpu else 0.0, 1),
        "mem_used_gb": round(mem_used, 2),
        "mem_total_gb": round(mem_total, 2),
        "process_tree_rss_gb": round(mem_tree, 2),
        "gpu_util_pct": round(max(gpu) if gpu else 0.0, 1),
        "vram_used_gb": round(vram_used, 2),
        "vram_total_gb": round(vram_total, 2),
    }


def _job_health(
    *,
    job_slug: str,
    leased: int,
    pending: int,
    rate_per_s: float,
    last_completed: Optional[float],
    last_leased: Optional[float],
    now: float,
    stall_s: float,
    leased_sample: list[str],
) -> tuple[str, Optional[str]]:
    """Return (health_code, health_message)."""
    if leased <= 0 and pending <= 0:
        return "idle", None
    if leased > 0 and rate_per_s <= 0:
        ref = last_completed if last_completed else last_leased
        if ref and (now - float(ref)) >= stall_s:
            sample = ", ".join(leased_sample[:3])
            extra = f" (+{len(leased_sample) - 3} more)" if len(leased_sample) > 3 else ""
            return (
                "stalled",
                f"STALLED: {leased} sub-jobs leased, 0 completions in "
                f"{int(now - float(ref))}s"
                + (f" (e.g. {sample}{extra})" if sample else ""),
            )
    if leased > 0 and rate_per_s > 0:
        return "running", f"RUNNING: {rate_per_s:.2f} sub-jobs/s"
    if pending > 0 and leased == 0:
        return "queued", f"QUEUED: {pending} sub-jobs pending, no runner leased"
    return "active", None


def _action_hint(
    *,
    health_code: str,
    missing_hosts: list[str],
    job_slug: str,
    task: str,
    pending: int,
    leased: int,
) -> Optional[str]:
    hints: list[str] = []
    if missing_hosts:
        hints.append(
            "Attach missing mesh nodes: "
            + ", ".join(f"kiroshi remote join {h} --task {task!r}" for h in missing_hosts[:3])
        )
    if health_code == "stalled":
        hints.append(
            "Stuck workers: kiroshi force-kill --role runner --all; "
            "then kiroshi requeue --state leased"
        )
    elif health_code == "queued" and pending > 0:
        hints.append(f"Start a runner: kiroshi runner --task {task!r} --job {job_slug!r}")
    elif pending == 0 and leased == 0 and health_code == "idle":
        hints.append("Job complete — aggregate outputs or archive the coordinator DB row")
    return "; ".join(hints) if hints else None


def enrich_status(
    base: dict[str, Any],
    *,
    store,
    runners: dict[str, Any],
    groups: list[dict[str, Any]],
    configured_hosts: Optional[list[str]] = None,
    window_s: float = 60.0,
    stall_s: float = STALL_LEASED_S,
    jobs_limit: int = 50,
) -> dict[str, Any]:
    """Merge fleet stats + per-job rows into one agent-friendly snapshot."""
    now = float(base.get("ts") or time.time())
    configured = list(configured_hosts or [])
    live_hosts = sorted(_runner_hosts(runners))

    runner_list: list[dict[str, Any]] = []
    for r in runners.values():
        d = dict(r)
        d["age_s"] = round(now - float(r.get("started_at", now)), 1)
        d["stale_s"] = round(now - float(r.get("last_seen", now)), 1)
        d["alive"] = d["stale_s"] <= 120.0
        runner_list.append(d)
    runner_list.sort(key=lambda x: x.get("runner_id", ""))

    missing_hosts = sorted(
        {h for h in configured if h.lower() not in {x.lower() for x in live_hosts}}
    )

    jobs_out: list[dict[str, Any]] = []
    for g in groups[:jobs_limit]:
        slug = str(g.get("job") or "")
        done = int(g.get("done") or 0)
        pending = int(g.get("pending") or 0)
        leased = int(g.get("leased") or 0)
        failed = int(g.get("failed") or 0)
        total = int(g.get("total") or (done + pending + leased + failed))
        remaining = pending + leased
        recent = store.job_done_in_window(slug, window_s) if slug else 0
        rate = recent / window_s if window_s > 0 else 0.0
        eta_s = remaining / rate if rate > 0 else None
        pct = round(100.0 * done / total, 1) if total else 0.0

        rids = list(g.get("runner_ids") or [])
        job_runners = [runners[rid] for rid in rids if rid in runners]
        resources = _aggregate_resources(job_runners)
        resources["subjobs_in_flight"] = leased
        resources["disk_inflight"] = store.job_disk_inflight(slug) if slug else {}

        last_completed = g.get("last_completed")
        last_leased = g.get("last_leased")
        leased_sample = store.leased_subjob_ids(slug, 5) if slug and leased else []
        health_code, health_msg = _job_health(
            job_slug=slug,
            leased=leased,
            pending=pending,
            rate_per_s=rate,
            last_completed=last_completed,
            last_leased=last_leased,
            now=now,
            stall_s=stall_s,
            leased_sample=leased_sample,
        )
        task = (job_runners[0].get("task") if job_runners else "") or ""
        runner_details = [{
            "runner_id": r.get("runner_id"),
            "host": r.get("host"),
            "workers": r.get("workers"),
            "task": r.get("task"),
            "job": r.get("job"),
            "resources": r.get("resources") or {},
            "code_fingerprint": r.get("code_fingerprint"),
            "launch_command": r.get("launch_command"),
            "in_flight": r.get("in_flight") or [],
        } for r in job_runners]
        jobs_out.append({
            "job": slug,
            "label": g.get("label") or "",
            "subjobs_total": total,
            "subjobs_done": done,
            "subjobs_pending": pending,
            "subjobs_leased": leased,
            "subjobs_failed": failed,
            "subjobs_remaining": remaining,
            "pct_done": pct,
            "rate_per_s": round(rate, 3),
            "eta_s": round(eta_s, 1) if eta_s is not None else None,
            "window_s": window_s,
            "launch_commands": list(g.get("launch_commands") or []),
            "runners": rids,
            "runner_details": runner_details,
            "last_completed_at": last_completed,
            "last_leased_at": last_leased,
            "resources": resources,
            "health": health_code,
            "health_detail": health_msg,
            "error_digest": store.error_digest(slug) if slug else [],
            "action_hint": _action_hint(
                health_code=health_code,
                missing_hosts=missing_hosts,
                job_slug=slug,
                task=task,
                pending=pending,
                leased=leased,
            ),
            # wire-compat aliases for /groups consumers
            "total": total,
            "done": done,
            "pending": pending,
            "leased": leased,
            "failed": failed,
        })

    n_jobs = len(jobs_out)
    summary_parts = [
        f"{n_jobs} job(s)",
        f"{base.get('total', 0):,} sub-jobs",
        f"({base.get('done', 0):,} done",
        f"{base.get('failed', 0):,} failed",
        f"{base.get('pending', 0):,} pending",
        f"{base.get('leased', 0):,} leased)",
    ]
    if missing_hosts:
        summary_parts.append(f"missing hosts: {', '.join(missing_hosts)}")
    stalled = [j["job"] for j in jobs_out if j.get("health") == "stalled"]
    if stalled:
        summary_parts.append(f"STALLED: {', '.join(stalled)}")

    out = dict(base)
    out["counts_are"] = "sub-jobs"
    out["n_jobs"] = n_jobs
    out["summary"] = " · ".join(summary_parts)
    out["fleet"] = {
        "subjobs_total": base.get("total"),
        "subjobs_done": base.get("done"),
        "subjobs_pending": base.get("pending"),
        "subjobs_leased": base.get("leased"),
        "subjobs_failed": base.get("failed"),
        "subjobs_remaining": base.get("remaining"),
        "rate_per_s": base.get("rate_per_s"),
        "eta_s": base.get("eta_s"),
        "window_s": base.get("window_s"),
        "n_jobs": n_jobs,
        "runners": runner_list,
        "mesh": {
            "configured_hosts": configured,
            "live_hosts": live_hosts,
            "missing_hosts": missing_hosts,
        },
        "per_host": base.get("per_host"),
        "disk_inflight": base.get("disk_inflight"),
        "disk_budget": base.get("disk_budget"),
        "disk_info": base.get("disk_info"),
        "resource": base.get("resource"),
        "scheduling": base.get("scheduling"),
        "recent_errors": base.get("recent_errors"),
    }
    out["jobs"] = jobs_out
    return out
