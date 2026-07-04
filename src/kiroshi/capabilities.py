"""kiroshi.capabilities — machine-readable feature map.

A single source of truth for what Kiroshi can do, task-indexed. Consumed by:
  * ``kiroshi capabilities``        (human-readable table)
  * ``kiroshi capabilities --json`` (structured, for LLM agents / MCP server)
  * the planned MCP server (exposed as a resource)

Keep entries dense: ``name`` (slug), ``purpose`` (one line), ``command``
(the invocation), ``when_to_use`` / ``when_not`` (the guidance `--help` can't
give). When you add a subcommand or a non-obvious feature, add an entry here
AND a line in ``AGENTS.md`` so agents stay current.
"""
from __future__ import annotations

from typing import Any

# The canonical list. Order = rough task-frequency (most-asked-first).
CAPABILITIES: list[dict[str, Any]] = [
    {
        "name": "run",
        "purpose": "One-shot 'front door': starts a local coordinator + Runner in one process, seeds, runs, exits. Optional --lan binds the coordinator to the LAN so other machines can join with 'kiroshi remote'.",
        "command": "kiroshi run <module:fn> --items ... | --jobs sub-jobs.jsonl | --enumerate",
        "when_to_use": "Quick jobs and dev iteration on a single machine (or a small ad-hoc mesh with --lan). --enumerate uses the task's own 'enumerate_gigs' hook to fan out sub-jobs (no external sub-job file).",
        "when_not": "Long-running production jobs — prefer separate coordinator + runner + seed so the state DB and workers can be restarted independently.",
    },
    {
        "name": "coordinator",
        "purpose": "Run the coordinator (HTTP API + SQLite job store + dashboard).",
        "command": "kiroshi coordinator --db X.db --port 8800",
        "when_to_use": "One per mesh; the queue + state owner. Restart-safe (DB persists).",
        "when_not": "Don't run two on the same port; the split-brain guard will refuse a discoverable duplicate (use --force-second-coordinator only deliberately).",
    },
    {
        "name": "runner",
        "purpose": "Worker that leases sub-jobs from a coordinator and runs ONE task.",
        "command": "kiroshi runner --coordinator <url> --task <module:fn> --workers N",
        "when_to_use": "Scale a single stage across cores/hosts. --capacity caps leases to avoid hoarding the disk budget.",
        "when_not": "One task per runner. For multi-stage work use multiple runners + `pipeline`.",
    },
    {
        "name": "seed",
        "purpose": "Enqueue sub-jobs into a coordinator (dedups by subjob_id).",
        "command": "kiroshi seed --coordinator <url> --jobs sub-jobs.jsonl --job <slug>",
        "when_to_use": "Stage a job. Re-running is safe (idempotent dedup). Use --job for dashboard/metrics filtering.",
        "when_not": "Don't hand-roll per-sub-job POSTs; the CLI batches + dedups.",
    },
    {
        "name": "stage",
        "purpose": "Copy a dataset between storage tiers with mesh I/O budgeting + resume. Replaces hand-rolled parallel rsync.",
        "command": "kiroshi stage --from <src-root> --to <dst-root> [--pattern glob] [--coordinator <url>]",
        "when_to_use": "Tier promotion (HDD->NVMe), remote fetch, or cross-node prefetch before a compute stage. Shares the mesh I/O budget via ResourceClient; skips already-copied files (resumable).",
        "when_not": "Single-file copies inside a task — use kfs directly. Don't use for compute (it's a data-movement verb, not a transform).",
    },
    {
        "name": "pipeline",
        "purpose": "Declarative multi-stage pipeline with typed dependency edges.",
        "command": "kiroshi pipeline run spec.toml   # or: validate spec.toml",
        "when_to_use": "Any dependent chain (A->B->C) or a map->reduce->map barrier (e.g. build a codebook from a corpus sample, then encode). Edges: each / quorum:k / all / artifact.",
        "when_not": "Do NOT hand-roll a cascade-seeder script that polls a DB and seeds the next stage — this is the tested replacement.",
    },
    {
        "name": "status",
        "purpose": "Unified mesh dashboard: fleet + jobs[] with per-job progress, ETA, launch_commands, runners, resources (CPU/RAM/GPU/VRAM/disk), stall detection, error_digest, action_hint, mesh coverage.",
        "command": "kiroshi status --coordinator <url>  (add --brief for a table)",
        "when_to_use": "The ONE call for 'what is running?' — replaces stitching /status + /groups + /runners.",
        "when_not": "For sub-job grep use 'kiroshi jobs' / MCP search_subjobs; for advisory detail use list_advisories.",
    },
    {
        "name": "jobs",
        "purpose": "Search/list SUB-JOBS by regex on subjob_id or error, filtered by state/job slug. Server-side filtered.",
        "command": "kiroshi jobs --coordinator <url> --grep <regex> [--field subjob_id|error] [--state failed] [--job <slug>]",
        "when_to_use": "Find specific sub-jobs — e.g. failed shards or errors matching a pattern.",
        "when_not": "For job-level overview use 'kiroshi status' (not this command).",
    },
    {
        "name": "requeue",
        "purpose": "Return failed/stuck sub-jobs to pending (respects max-retries).",
        "command": "kiroshi requeue --coordinator <url> --state failed",
        "when_to_use": "After fixing a systemic error so failed sub-jobs re-run.",
        "when_not": "Don't loop it blindly against 'leased' while runners are alive — they'll re-lease.",
    },
    {
        "name": "nas.probe",
        "purpose": "Discover a NAS's per-disk shares (e.g. 'disk1'..'disk7') and scaffold a matching storage topology block for kiroshi.local.toml.",
        "command": "kiroshi nas probe <server> [--shares vol1,vol2 | --pattern 'disk{1..7}']",
        "when_to_use": "First contact with a new NAS — before you can 'nas assess' or seed anything, you need the topology config; this generates it.",
        "when_not": "If the NAS uses one unified share (single write root), you don't need per-disk routing — plain [paths] suffices.",
    },
    {
        "name": "nas.assess",
        "purpose": "Walk a dataset root; report shard balance + throughput-readiness.",
        "command": "kiroshi nas assess --root <dir>",
        "when_to_use": "Before seeding, to verify the topology matches where data physically lives.",
        "when_not": "Don't skip on HDD arrays — wrong routing = slow reads.",
    },
    {
        "name": "nas.benchmark",
        "purpose": "Measure per-disk read throughput at increasing concurrency.",
        "command": "kiroshi nas benchmark --root <dir> --concurrency 1,2,4,8,16",
        "when_to_use": "Find a disk's I/O knee before setting its topology concurrency. Feed the results to 'kiroshi bench calibrate'.",
        "when_not": "Don't guess concurrency — measure it. But benchmark on the SAME workload class you'll run in production.",
    },
    {
        "name": "bench",
        "purpose": "True throughput reporting (from output mtimes or per-sub-job completed_at over HTTP) + concurrency calibration from samples.",
        "command": "kiroshi bench rate --dir <outputs> | --coordinator <url> --job <slug>  |  kiroshi bench calibrate --samples '1=50,2=95,4=140,8=150'",
        "when_to_use": "rate --dir: honest throughput from output-file mtimes (FS access). rate --coordinator: same, from /jobs completed_at (no FS access). calibrate: turn nas-benchmark samples into a per-disk concurrency recommendation.",
        "when_not": "calibrate needs representative samples — don't calibrate on a cold cache. rate --coordinator needs the coordinator reachable + a valid job.",
    },
    {
        "name": "nas.shard",
        "purpose": "Partition a dataset across spindles (writes a shard plan JSON).",
        "command": "kiroshi nas shard --root <dir> --n-shards 7",
        "when_to_use": "Produce the sub-job source-path prefixes that route to per-disk read roots.",
        "when_not": "—",
    },
    {
        "name": "remote",
        "purpose": "Launch a Runner on another machine over SSH (probe/join), or 'sync' — git-pull tracked repos on every [hosts.*] node.",
        "command": "kiroshi remote {probe|join} <host> ...  |  kiroshi remote sync [--dry-run] [--reinstall] [--restart]",
        "when_to_use": "probe/join: add worker machines without manual ssh plumbing. sync: propagate a fresh commit to every node's runner. Always start with --dry-run.",
        "when_not": "Needs SSH key auth to each target host. Never uses git reset/force — a diverged remote fails cleanly instead of getting clobbered.",
    },
    {
        "name": "doctor",
        "purpose": "Preflight checks for this machine + env (python, deps, disk, firewall, config) + storage-class advice for the resolved read/write roots.",
        "command": "kiroshi doctor",
        "when_to_use": "On a new node before joining the mesh.",
        "when_not": "—",
    },
    {
        "name": "advise-io",
        "purpose": "Path-aware storage-class classifier (no benchmarking): NVMe vs HDD, direct-share-unused, parity-write, missing SMB creds. Also powers the fail-closed I/O gate on seed/run/seed_gigs.",
        "command": "# MCP: advise_io(read_root, write_root, sample_src, sample_dst)  |  gate is automatic on seed/run/seed_gigs; ack a trade-off with --io-ack TOKEN (CLI) or io_ack=['TOKEN'] (MCP); KIROSHI_IO_GATE=0 disables",
        "when_to_use": "advise_io: the 'am I on the fast path?' check before a NAS job. The GATE runs automatically at creation — a slow trade-off (parity_write / no_direct_share / no_smb_creds / unclassified_nas) is REFUSED until you fix the path or pass the matching io_ack token. Kiroshi never mutates your declared paths; it fills blanks and enforces.",
        "when_not": "Local-only work, demo jobs, an already-fast path, or no [[storage.disk]] topology — the gate can't judge those, so it passes silently.",
    },
    {
        "name": "tray",
        "purpose": "System-tray status icon (windowless via pythonw; needs the 'tray' extra).",
        "command": "kiroshi tray",
        "when_to_use": "Visual health for an operator at the console.",
        "when_not": "Headless / service contexts — use the dashboard URL instead.",
    },
    {
        "name": "task-contract",
        "purpose": "The ABI a task must satisfy: module-level 'def run(spec)->dict' (status/metrics) + optional 'enumerate_gigs(args)->Iterator[sub-job]' that lets the task fan out its own sub-jobs.",
        "command": "# see src/kiroshi/tasks.py; conventions: status='ok'|'skipped'; raise -> failed + retry",
        "when_to_use": "Any new pipeline stage. Idempotent skip-if-output-exists in run() plus a good enumerate_gigs makes a stage resumable and re-runnable for free.",
        "when_not": "Don't do I/O in enumerate_gigs (it runs on the launcher, before the mesh). Don't do long blocking waits in run() without honoring gig-timeout.",
    },
    {
        "name": "kfs",
        "purpose": "FS abstraction for tasks (local, UNC/SMB, mapped drives). API: exists, open, walk, atomic_write, makedirs, remove, backend + SMB creds helpers.",
        "command": "# in a task:  from kiroshi import kfs;  kfs.atomic_write(dst) as fh: fh.write(...)",
        "when_to_use": "Every I/O call in a task. atomic_write gives crash-safe writes; walk streams a huge NAS tree without materializing; SMB creds come from env (KIROSHI_NAS_USER/PASS, optional per-server KIROSHI_NAS_USER_<SERVER>; tune with KIROSHI_SMB_AUTH/ENCRYPT) so tasks work in scheduled/service sessions with no mapped drives.",
        "when_not": "Don't shell out to xcopy/robocopy from a task — you lose retry semantics and creds. Don't os.walk a UNC path — kfs.walk handles SMB re-connects on transient errors.",
    },
    {
        "name": "resource-governor",
        "purpose": "coordinator-mediated slot budgeting for shared resources (per-disk reads, global parity writes, named budgets). Tasks call ResourceClient.acquire(); fail-open if coordinator unreachable.",
        "command": "# in a task:  from kiroshi.resource import ResourceClient;  with ResourceClient(coordinator).acquire(disk='disk3', mode='write'): ...",
        "when_to_use": "Tasks that hammer a shared resource — HDD parity writes, HuggingFace downloads with a rate cap, a small GPU pool. Cross-host coordination without a broker.",
        "when_not": "Anything covered by the built-in per-disk topology (which is auto-applied by the leaser) — don't re-implement that. Use for resources the topology doesn't know about.",
    },
    {
        "name": "profiler",
        "purpose": "Per-sub-job CPU/MEM/IO attribution via psutil. Each completed sub-job carries a compact proc summary in its metrics.",
        "command": "# automatic — wired into the runner's pool._run_one; needs 'pip install kiroshi[profiler]'",
        "when_to_use": "Understand what each job consumed (peak CPU, RSS, bytes read/written, wall time). Appears in /jobs and /job/{id} metrics.proc. Disable with KIROSHI_PROFILER=0.",
        "when_not": "Headless nodes where you don't care about per-sub-job attribution. Soft dep — works without psutil installed (just no proc summary).",
    },
    {
        "name": "bottleneck",
        "purpose": "Per-moment dominant-pressure classifier: fuses CPU/MEM/disk I/O + routing knowledge + bench ceilings to label the bottleneck (or latency_bound). Fires advisories on sustained breach.",
        "command": "# automatic — wired into AdvisoryDetector.tick; fires host.cpu_bound / nas.single_spindle / nas.latency_bound / disk.at_ceiling / etc.",
        "when_to_use": "When throughput is lower than expected and you need to know WHY — is it the disk, the routing, CPU, or round-trip latency? The latency_bound verdict catches the case where nothing is saturated but work is still slow (SMB metadata latency).",
        "when_not": "NVMe-only nodes with no I/O contention concern. The classifier is a heuristic, not ground truth — it says 'dominant pressure', not 'THE critical path'.",
    },
    {
        "name": "mcp",
        "purpose": "Run the Model Context Protocol server — exposes Kiroshi's tools + docs to LLM agents (Claude Desktop, Cursor, custom clients) over stdio.",
        "command": "kiroshi mcp",
        "when_to_use": "So an MCP-compatible agent can drive Kiroshi (advise_io, seed_gigs, validate_pipeline/tick_pipeline, status, list_advisories, export_metrics) with typed tool calls instead of shelling out to the CLI. Also serves the docs (agents.md, pipeline.md) + capability map as resources so the agent starts with full context.",
        "when_not": "Headless nodes that never run an LLM client. Requires 'pip install kiroshi[mcp]'.",
    },
    {
        "name": "autostart",
        "purpose": "Register the tray to launch at login. Two mechanisms: HKCU\\Run (logon-only, legacy) and Task Scheduler with restart-on-failure (self-heals within ~1 min after a crash — recommended default).",
        "command": "kiroshi autostart on --mode {auto|scheduled|run}",
        "when_to_use": "So the tray survives reboots AND crashes on an operator machine. Use --mode scheduled (or 'auto', which prefers it) for supervision; --mode run only on locked-down machines that disallow scheduled tasks.",
        "when_not": "Non-operator machines that never carry a UI (headless nodes just running coordinator/Runner services don't need the tray).",
    },
    {
        "name": "firewall",
        "purpose": "Idempotent Windows Firewall rules for the coordinator's inbound ports.",
        "command": "kiroshi firewall install",
        "when_to_use": "First setup on a coordinator host so runners on other machines can reach it.",
        "when_not": "—",
    },
    {
        "name": "service",
        "purpose": "Install/uninstall/inspect a coordinator or Runner as a Windows service (NSSM).",
        "command": "kiroshi service install",
        "when_to_use": "Persistent daemon without a logged-in user (needs admin).",
        "when_not": "User-level interactive use — prefer the tray/autostart or a scheduled task.",
    },
    {
        "name": "advisories",
        "purpose": "Structured NAS-contention + sub-job-failure warnings emitted by the coordinator.",
        "command": "GET /advisories?token=<T>",
        "when_to_use": "Poll to detect stalls (nas.throughput_collapse, nas.disk_saturation, gig.failure_spike).",
        "when_not": "—",
    },
    {
        "name": "metrics.export",
        "purpose": "Bulk per-sub-job metrics for a whole job (up to 100k rows).",
        "command": "GET /metrics/export?job=<g>&state=done&limit=100000",
        "when_to_use": "Find which items a stage has finished (the pipeline coordinator uses this). Aggregate results across a job.",
        "when_not": "Not for live leasing — use /status for counts.",
    },
    {
        "name": "job.health",
        "purpose": "Paragraph-form diagnosis of ONE job slug: progress, resources, advisories, error_digest — paste-ready for agents.",
        "command": "MCP tool: campaign_health(job=<slug>)  (reads enriched /status + /advisories)",
        "when_to_use": "Single-job paragraph when you already know the job slug.",
        "when_not": "For the full fleet use MCP 'status' first.",
    },
    {
        "name": "lease.decisions",
        "purpose": "Per-lease-call decision log: why each host got N sub-jobs (requested vs granted, binding_reason, per-disk budget snapshot).",
        "command": "GET /lease/decisions?host=<h>&reason=<r>&limit=100  (MCP tool: lease_decisions)",
        "when_to_use": "Debug node starvation or underutilization — see exactly which constraint (FAIR_SHARE_CAP / DISK_BUDGET_FULL / NO_PENDING) blocked a host from getting work.",
        "when_not": "For aggregate fleet health use 'decisions.summary' first; for a single sub-job's history use 'job.trace'.",
    },
    {
        "name": "job.trace",
        "purpose": "Coordination timeline for one job/sub-job: seeded -> leased -> completed/failed/expired events + current DB row.",
        "command": "GET /job/trace?subjob_id=<id>  (MCP tool: job_trace)",
        "when_to_use": "Trace a single sub-job's full lifecycle through the coordinator — which host leased it, when it completed or failed, how many attempts.",
        "when_not": "For fleet-wide or per-host questions use 'lease.decisions' or 'decisions.summary'.",
    },
    {
        "name": "decisions.summary",
        "purpose": "Aggregated scheduling health over a time window: per-host grant ratio, main binding reason, and which hosts are STARVED.",
        "command": "GET /decisions/summary?window_s=300  (MCP tool: scheduling_summary)",
        "when_to_use": "The first call when diagnosing underutilization — instantly shows if any host is being starved and why. Also appears as the 'scheduling' block on /status.",
        "when_not": "For per-sub-job detail use 'job.trace'; for raw decision records use 'lease.decisions'.",
    },
]


def as_json() -> str:
    import json
    return json.dumps(CAPABILITIES, indent=2)


def as_table() -> str:
    cols = ("name", "purpose", "command")
    widths = {c: max(len(c), max(len(str(e[c])) for e in CAPABILITIES)) for c in cols}
    sep = "  "
    lines = [sep.join(c.upper().ljust(widths[c]) for c in cols)]
    lines.append(sep.join("-" * widths[c] for c in cols))
    for e in CAPABILITIES:
        lines.append(sep.join(str(e[c]).ljust(widths[c]) for c in cols))
    return "\n".join(lines)
