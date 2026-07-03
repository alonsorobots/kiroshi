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
        "purpose": "One-shot 'front door': starts a local Fixer + Runner in one process, seeds, runs, exits. Optional --lan binds the Fixer to the LAN so other machines can join with 'kiroshi remote'.",
        "command": "kiroshi run <module:fn> --items ... | --jobs gigs.jsonl | --enumerate",
        "when_to_use": "Quick campaigns and dev iteration on a single machine (or a small ad-hoc mesh with --lan). --enumerate uses the task's own 'enumerate_gigs' hook to fan out gigs (no external gig file).",
        "when_not": "Long-running production campaigns — prefer separate fixer + runner + seed so the state DB and workers can be restarted independently.",
    },
    {
        "name": "fixer",
        "purpose": "Run the coordinator (HTTP API + SQLite job store + dashboard).",
        "command": "kiroshi fixer --db X.db --port 8800",
        "when_to_use": "One per mesh; the queue + state owner. Restart-safe (DB persists).",
        "when_not": "Don't run two on the same port; the split-brain guard will refuse a discoverable duplicate (use --force-second-fixer only deliberately).",
    },
    {
        "name": "runner",
        "purpose": "Worker that leases gigs from a Fixer and runs ONE task.",
        "command": "kiroshi runner --fixer <url> --task <module:fn> --workers N",
        "when_to_use": "Scale a single stage across cores/hosts. --capacity caps leases to avoid hoarding the disk budget.",
        "when_not": "One task per runner. For multi-stage work use multiple runners + `pipeline`.",
    },
    {
        "name": "seed",
        "purpose": "Enqueue gigs into a Fixer (dedups by job_id).",
        "command": "kiroshi seed --fixer <url> --jobs gigs.jsonl --group <slug>",
        "when_to_use": "Stage a campaign. Re-running is safe (idempotent dedup). Use --group for dashboard/metrics filtering.",
        "when_not": "Don't hand-roll per-gig POSTs; the CLI batches + dedups.",
    },
    {
        "name": "stage",
        "purpose": "Copy a dataset between storage tiers with mesh I/O budgeting + resume. Replaces hand-rolled parallel rsync.",
        "command": "kiroshi stage --from <src-root> --to <dst-root> [--pattern glob] [--fixer <url>]",
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
        "purpose": "Print a fleet /status snapshot (counts + rate + ETA).",
        "command": "kiroshi status --fixer <url>",
        "when_to_use": "Quick health check. For per-gig detail use HTTP /jobs or /metrics/export.",
        "when_not": "Counts only — not a substitute for /advisories when diagnosing a stall.",
    },
    {
        "name": "requeue",
        "purpose": "Return failed/stuck gigs to pending (respects max-retries).",
        "command": "kiroshi requeue --fixer <url> --state failed",
        "when_to_use": "After fixing a systemic error so failed gigs re-run.",
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
        "command": "kiroshi nas benchmark --root <dir>",
        "when_to_use": "Tune per-disk concurrency caps in the topology.",
        "when_not": "—",
    },
    {
        "name": "nas.shard",
        "purpose": "Partition a dataset across spindles (writes a shard plan JSON).",
        "command": "kiroshi nas shard --root <dir> --n-shards 7",
        "when_to_use": "Produce the gig source-path prefixes that route to per-disk read roots.",
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
        "purpose": "Preflight checks for this machine + env (python, deps, disk, firewall, config).",
        "command": "kiroshi doctor",
        "when_to_use": "On a new node before joining the mesh.",
        "when_not": "—",
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
        "purpose": "The ABI a task must satisfy: module-level 'def run(spec)->dict' (status/metrics) + optional 'enumerate_gigs(args)->Iterator[gig]' that lets the task fan out its own gigs.",
        "command": "# see src/kiroshi/tasks.py; conventions: status='ok'|'skipped'; raise -> failed + retry",
        "when_to_use": "Any new pipeline stage. Idempotent skip-if-output-exists in run() plus a good enumerate_gigs makes a stage resumable and re-runnable for free.",
        "when_not": "Don't do I/O in enumerate_gigs (it runs on the launcher, before the mesh). Don't do long blocking waits in run() without honoring gig-timeout.",
    },
    {
        "name": "kfs",
        "purpose": "FS abstraction for tasks (local, UNC/SMB, mapped drives). API: exists, open, walk, atomic_write, makedirs, remove, backend + SMB creds helpers.",
        "command": "# in a task:  from kiroshi import kfs;  kfs.atomic_write(dst) as fh: fh.write(...)",
        "when_to_use": "Every I/O call in a task. atomic_write gives crash-safe writes; walk streams a huge NAS tree without materializing; SMB creds come from env (KIROSHI_SMB_USER/PASS/AUTH/ENCRYPT) so tasks work in scheduled/service sessions with no mapped drives.",
        "when_not": "Don't shell out to xcopy/robocopy from a task — you lose retry semantics and creds. Don't os.walk a UNC path — kfs.walk handles SMB re-connects on transient errors.",
    },
    {
        "name": "resource-governor",
        "purpose": "Fixer-mediated slot budgeting for shared resources (per-disk reads, global parity writes, named budgets). Tasks call ResourceClient.acquire(); fail-open if Fixer unreachable.",
        "command": "# in a task:  from kiroshi.resource import ResourceClient;  with ResourceClient(fixer).acquire(disk='disk3', mode='write'): ...",
        "when_to_use": "Tasks that hammer a shared resource — HDD parity writes, HuggingFace downloads with a rate cap, a small GPU pool. Cross-host coordination without a broker.",
        "when_not": "Anything covered by the built-in per-disk topology (which is auto-applied by the leaser) — don't re-implement that. Use for resources the topology doesn't know about.",
    },
    {
        "name": "mcp",
        "purpose": "Run the Model Context Protocol server — exposes Kiroshi's tools + docs to LLM agents (Claude Desktop, Cursor, custom clients) over stdio.",
        "command": "kiroshi mcp",
        "when_to_use": "So an MCP-compatible agent can drive Kiroshi (submit_pipeline, seed_gigs, status, list_advisories, export_metrics) with typed tool calls instead of shelling out to the CLI. Also serves the docs (agents.md, pipeline.md) + capability map as resources so the agent starts with full context.",
        "when_not": "Headless nodes that never run an LLM client. Requires 'pip install kiroshi[mcp]'.",
    },
    {
        "name": "autostart",
        "purpose": "Register the tray to launch at login. Two mechanisms: HKCU\\Run (logon-only, legacy) and Task Scheduler with restart-on-failure (self-heals within ~1 min after a crash — recommended default).",
        "command": "kiroshi autostart on --mode {auto|scheduled|run}",
        "when_to_use": "So the tray survives reboots AND crashes on an operator machine. Use --mode scheduled (or 'auto', which prefers it) for supervision; --mode run only on locked-down machines that disallow scheduled tasks.",
        "when_not": "Non-operator machines that never carry a UI (headless nodes just running Fixer/Runner services don't need the tray).",
    },
    {
        "name": "firewall",
        "purpose": "Idempotent Windows Firewall rules for the Fixer's inbound ports.",
        "command": "kiroshi firewall install",
        "when_to_use": "First setup on a Fixer host so runners on other machines can reach it.",
        "when_not": "—",
    },
    {
        "name": "service",
        "purpose": "Install/uninstall/inspect a Fixer or Runner as a Windows service (NSSM).",
        "command": "kiroshi service install",
        "when_to_use": "Persistent daemon without a logged-in user (needs admin).",
        "when_not": "User-level interactive use — prefer the tray/autostart or a scheduled task.",
    },
    {
        "name": "advisories",
        "purpose": "Structured NAS-contention + gig-failure warnings emitted by the Fixer.",
        "command": "GET /advisories?token=<T>",
        "when_to_use": "Poll to detect stalls (nas.throughput_collapse, nas.disk_saturation, gig.failure_spike).",
        "when_not": "—",
    },
    {
        "name": "metrics.export",
        "purpose": "Bulk per-gig metrics for a whole campaign (up to 100k rows).",
        "command": "GET /metrics/export?grp=<g>&state=done&limit=100000",
        "when_to_use": "Find which items a stage has finished (the pipeline coordinator uses this). Aggregate results across a campaign.",
        "when_not": "Not for live leasing — use /status for counts.",
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
