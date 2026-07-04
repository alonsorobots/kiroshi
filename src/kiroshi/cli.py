"""Kiroshi CLI — ``kiroshi fixer | runner | seed | status``.

Examples::

    # 1. Start the Coordinator (coordinator + dashboard) on this box
    kiroshi fixer --db demo.db

    # 2. Seed some demo gigs
    kiroshi seed --fixer http://localhost:8787 --demo 500

    # 3. Join with a Runner on every box
    kiroshi runner --fixer http://localhost:8787 --task examples.sleep_task:run --workers 8

    # watch it live at  http://localhost:8787/
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Optional

from . import __version__
from .config import current_host, load_config


def main(argv: Optional[list[str]] = None) -> int:
    # Split off task pass-through args after a literal `--` (for `kiroshi run
    # TASK --enumerate -- --read-root //nas --fps 4`). Everything after `--`
    # goes to the task's enumerate_gigs, never to kiroshi's own parser.
    raw = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in raw:
        i = raw.index("--")
        passthrough = raw[i + 1:]
        raw = raw[:i]

    cfg = load_config()
    parser = argparse.ArgumentParser(prog="kiroshi", description="Work-stealing mesh runner.")
    parser.add_argument("--version", action="version", version=f"kiroshi {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ---- run (the front door: one command to run a task across the mesh) ----
    prun = sub.add_parser(
        "run",
        help="Run a task across the mesh from one command (the front door).")
    prun.add_argument("task", help="Task as 'module:function'.")
    prun.add_argument("--items", default=None,
                      help="Glob of input files; one sub-job per match (spec={'path':...}).")
    prun.add_argument("--jobs", default=None,
                      help="JSONL sub-job file (each line {subjob_id, spec}).")
    prun.add_argument("--enumerate", action="store_true",
                      help="Call the task module's enumerate_gigs(args) with the "
                           "args given after a literal '--'.")
    prun.add_argument("--job", default=None, help="Job slug (groups gigs in the UI).")
    prun.add_argument("--label", default=None,
                      help="Human-readable job name for the dashboard header.")
    prun.add_argument("--origin", default=None,
                      help="Opaque JSON attribution blob for advisory delivery "
                           "(M9). Any dict; if it has a `callback` URL, structured "
                           "warnings about this job's spindle/health are "
                           "POSTed there. Also picked up from KIROSHI_ORIGIN. "
                           "Example: --origin '{\"kind\":\"cursor-agent\","
                           "\"callback\":\"http://localhost:9123/notify\"}'.")
    prun.add_argument("--workers", type=int, default=0,
                      help="Local worker processes (default: CPU count).")
    prun.add_argument("--capacity", type=int, default=cfg.host().capacity)
    prun.add_argument("--port", type=int, default=cfg.fixer_port)
    prun.add_argument("--lan", action="store_true",
                      help="Bind 0.0.0.0 so other machines can join (generates a mesh token).")
    prun.add_argument("--force-second-fixer", action="store_true",
                      help="Skip the singleton coordinator guard. Requires BOTH this "
                           "flag AND KIROSHI_ALLOW_SECOND_COORDINATOR=1 env var. Use "
                           "ONLY when you deliberately want two isolated meshes.")
    prun.add_argument("--db", default=None,
                      help="Run job-store path (default: state-dir/run-<slug>.db).")
    prun.add_argument("--token", default=None, help="Mesh token (for --lan).")
    prun.add_argument("--read-root", default=None, help="Set KIROSHI_READ_ROOT for the task.")
    prun.add_argument("--write-root", default=None, help="Set KIROSHI_WRITE_ROOT for the task.")
    prun.add_argument("--sub-job-timeout", type=float, default=None,
                      help="Seconds before a hung sub-job is abandoned + its worker killed.")
    prun.add_argument("--syspath", action="append", default=None,
                      help="Extra sys.path entries for task import (repeatable).")
    prun.add_argument("--max-retries", type=int, default=3)
    prun.add_argument("--max-tasks-per-child", type=int, default=None,
                      help="Recycle worker processes every N gigs (band-aid for C-level "
                           "leaks; off by default — prefer fixing the real accumulator).")
    prun.add_argument("--gc-between-tasks", action="store_true",
                      help="Run gc.collect() after every sub-job (defensive; off by default).")
    prun.add_argument("--serve-task", action="store_true",
                      help="Serve this (single-file, top-level) task's source to "
                           "joiners so `kiroshi join` needs no checkout. Opt-in + "
                           "consent-gated on the joiner — see SECURITY.md §6.5.")

    # ---- join (add this machine to a running mesh) ----
    pjoin = sub.add_parser(
        "join", help="Join this machine to a running mesh as a Runner.")
    pjoin.add_argument("--coordinator", "--fixer", dest="fixer", default="auto", help="Coordinator URL or 'auto' (default).")
    pjoin.add_argument("--task", default=None,
                       help="Task 'module:function' (default: the Coordinator's served task).")
    pjoin.add_argument("--token", default=None, help="Mesh token (the join code).")
    pjoin.add_argument("--workers", type=int, default=0,
                       help="Worker processes (default: CPU count).")
    pjoin.add_argument("--service", action="store_true",
                       help="Install as an auto-start Runner service (else run foreground).")
    pjoin.add_argument("--accept-task-hash", default=None,
                       help="Pre-approve served task code by sha256 (non-interactive).")
    pjoin.add_argument("--syspath", action="append", default=None,
                       help="Extra sys.path entries for task import (repeatable).")
    pjoin.add_argument("--read-root", default=None, help="Set KIROSHI_READ_ROOT for the task.")
    pjoin.add_argument("--write-root", default=None, help="Set KIROSHI_WRITE_ROOT for the task.")
    pjoin.add_argument("--sub-job-timeout", type=float, default=None,
                       help="Seconds before a hung sub-job is abandoned + its worker killed.")

    # ---- remote (launch/manage a Runner on another machine, quoting-proof) ----
    prem = sub.add_parser(
        "remote",
        help="Launch a Runner on another machine over SSH (interpreter-aware, "
             "durable, no shell-quoting pitfalls).")
    prem.add_argument("remote_cmd", choices=["probe", "join", "sync"],
                      help="probe: preflight only. join: preflight + durable launch. "
                           "sync: git-pull the tracked repos on every [hosts.*] node "
                           "and restart their runners (use --dry-run first).")
    prem.add_argument("host", nargs="?", default=None,
                      help="SSH host (alias or user@host) matched to [hosts.<Host>]. "
                           "Required for probe/join; for 'sync' omit to iterate all hosts.")
    prem.add_argument("--dry-run", action="store_true",
                      help="[sync] print the per-host command plan without executing.")
    prem.add_argument("--repos", default=None,
                      help="[sync] comma-separated remote paths to git-pull "
                           "(default: the kiroshi checkout on each host). "
                           "Ex: '/opt/kiroshi,/opt/myrepo'.")
    prem.add_argument("--reinstall", action="store_true",
                      help="[sync] run 'pip install -e .' after pull "
                           "(needed if pyproject/entry-points changed). Default: no.")
    prem.add_argument("--restart", action="store_true",
                      help="[sync] signal runners to exit so the auto-restart "
                           "wrapper picks up the new code. Default: no (report only).")
    prem.add_argument("--task", default=None, help="Task 'module:function' to run.")
    prem.add_argument("--coordinator", "--fixer", dest="fixer", default=None,
                      help="Coordinator URL the remote should pull from "
                           "(default: http://<this-LAN-ip>:<fixer_port>).")
    prem.add_argument("--workers", type=int, default=0,
                      help="Worker processes on the remote (default: its [hosts.<Host>].workers).")
    prem.add_argument("--python", default=None,
                      help="Remote interpreter (default: [hosts.<Host>].python).")
    prem.add_argument("--syspath", action="append", default=None,
                      help="Extra sys.path entry on the remote for task import (repeatable).")
    prem.add_argument("--read-root", default=None, help="Remote KIROSHI_READ_ROOT.")
    prem.add_argument("--write-root", default=None, help="Remote KIROSHI_WRITE_ROOT.")
    prem.add_argument("--token", default=None,
                      help="Mesh token (default: resolved from local env/token file).")
    prem.add_argument("--job", default=None,
                      help="Job slug (names the launcher/log/task).")
    prem.add_argument("--task-name", default=None,
                      help="Scheduled Task name on the remote (default derived from --job).")
    prem.add_argument("--force", action="store_true",
                      help="Launch even if preflight reports problems.")
    prem.add_argument("--no-verify", action="store_true",
                      help="Skip waiting for the runner to appear in the fixer.")

    # ---- fixer ----
    pf = sub.add_parser("fixer", help="Run the coordinator + dashboard.",
                        aliases=["coordinator"], description="Run the coordinator + dashboard.")
    pf.add_argument("--db", default="kiroshi.db", help="SQLite job-store path (gitignored).")
    pf.add_argument("--host", default="127.0.0.1",
                    help="Bind address. Defaults to loopback (secure). Pass "
                         "0.0.0.0 to expose the mesh to the LAN (requires a token).")
    pf.add_argument("--port", type=int, default=cfg.fixer_port)
    pf.add_argument("--max-retries", type=int, default=3)
    pf.add_argument("--lease-ttl", type=float, default=120.0)
    pf.add_argument("--reap-interval", type=float, default=15.0)
    pf.add_argument("--fair-share", dest="fair_share", action="store_true",
                    default=cfg.fair_share,
                    help="Cap each host's in-flight gigs at its live-worker slice "
                         "of the per-disk budget, so a fast poller can't hoard the "
                         "budget and starve slower hosts. Default from "
                         "[fixer].fair_share (off).")
    pf.add_argument("--no-beacon", action="store_true",
                    help="Disable the UDP discovery beacon (runners must use an explicit --fixer).")
    pf.add_argument("--force-second-fixer", action="store_true",
                    help="Skip the singleton coordinator guard. Requires BOTH this "
                         "flag AND KIROSHI_ALLOW_SECOND_COORDINATOR=1 env var. Use "
                         "ONLY when you deliberately want two isolated meshes on "
                         "different NAS pools (rare — usually you want one coordinator).")
    pf.add_argument("--token", default=None,
                    help="Mesh auth token (default: env KIROSHI_TOKEN, token file, or auto-generated).")
    pf.add_argument("--no-auth", action="store_true",
                    help="Run WITHOUT auth (trusted LAN / dev only). Strongly discouraged on 0.0.0.0.")
    pf.add_argument("--pages-dir", default=None,
                    help="Directory of custom *.html task views; served at /p, linked from dashboard.")

    # ---- runner ----
    pr = sub.add_parser("runner", help="Run a worker node (Runner).")
    pr.add_argument("--coordinator", "--fixer", dest="fixer", default=cfg.fixer_url,
                    help="Coordinator base URL, or 'auto' to discover it on the LAN.")
    pr.add_argument("--task", required=True, help="Task as 'module:function'.")
    pr.add_argument("--workers", type=int, default=cfg.host().workers)
    pr.add_argument("--capacity", type=int, default=cfg.host().capacity)
    pr.add_argument("--id", default=None, help="Runner id (default: host-rand).")
    pr.add_argument("--host", default=current_host())
    pr.add_argument("--poll", type=float, default=2.0)
    pr.add_argument("--heartbeat", type=float, default=30.0)
    pr.add_argument("--retries", type=int, default=2, help="Per-item local retries.")
    pr.add_argument("--sub-job-timeout", type=float, default=None,
                    help="Seconds before a hung sub-job is abandoned + its worker killed.")
    pr.add_argument("--max-tasks-per-child", type=int, default=None,
                    help="Recycle worker processes every N gigs (band-aid for leaks; off by default).")
    pr.add_argument("--gc-between-tasks", action="store_true",
                    help="Run gc.collect() after every sub-job (defensive; off by default).")
    pr.add_argument("--syspath", action="append", default=None,
                    help="Extra sys.path entries for task import (repeatable).")
    pr.add_argument("--token", default=None,
                    help="Mesh auth token (default: env KIROSHI_TOKEN or token file).")

    # ---- seed ----
    ps = sub.add_parser("seed", help="Enqueue gigs into the Coordinator.")
    ps.add_argument("--coordinator", "--fixer", dest="fixer", default=cfg.fixer_url, help="Coordinator base URL, or 'auto'.")
    ps.add_argument("--jobs", default=None,
                    help="JSONL file; each line {\"subjob_id\":..., \"spec\":{...}}.")
    ps.add_argument("--demo", type=int, default=0, help="Seed N demo sleep gigs.")
    ps.add_argument("--batch", type=int, default=1000, help="POST batch size.")
    ps.add_argument("--job", default=None,
                    help="Job slug; all gigs are grouped under it in the dashboard "
                         "(overrides the subjob_id-prefix grouping).")
    ps.add_argument("--label", default=None,
                    help="Human-readable job name shown in the dashboard header "
                         "(e.g. 'Converting Seamless Interactions 30fps -> 4,8 fps'). "
                         "Pairs with --job (or a single shared job in --jobs).")
    ps.add_argument("--origin", default=None,
                    help="Opaque JSON attribution blob (M9); if it has a `callback` "
                         "URL, structured advisories about this job's spindle "
                         "health are POSTed there. Also picked up from KIROSHI_ORIGIN.")
    ps.add_argument("--token", default=None, help="Mesh auth token.")

    # ---- status ----
    pt = sub.add_parser("status", help="Print a /status snapshot.")
    pt.add_argument("--coordinator", "--fixer", dest="fixer", default=cfg.fixer_url, help="Coordinator base URL, or 'auto'.")
    pt.add_argument("--token", default=None, help="Mesh auth token.")

    # ---- pipeline (declarative multi-stage DAG with typed edges) ----
    ppipe = sub.add_parser(
        "pipeline",
        help="Run a declarative multi-stage pipeline (typed dependency edges).")
    ppipe_sub = ppipe.add_subparsers(dest="pipe_cmd", required=True)
    ppr = ppipe_sub.add_parser("run", help="Run the pipeline coordinator loop.")
    ppr.add_argument("spec", help="Path to the pipeline .toml spec.")
    ppr.add_argument("--once", action="store_true",
                     help="Apply edges a single time and exit (no loop).")
    ppr.add_argument("--token", default=None, help="Mesh auth token override.")
    ppv = ppipe_sub.add_parser("validate", help="Load + validate a spec, print the DAG.")
    ppv.add_argument("spec", help="Path to the pipeline .toml spec.")

    # ---- capabilities (machine-readable feature map for agents) ----
    pcap = sub.add_parser(
        "capabilities",
        help="List Kiroshi capabilities (what to use for a given task).")
    pcap.add_argument("--json", dest="as_json", action="store_true",
                      help="Emit the capability list as JSON (for LLM agents / MCP).")

    # ---- stage (budgeted, resumable data movement) ----
    pst = sub.add_parser(
        "stage",
        help="Stage (copy) a dataset between storage tiers with mesh I/O budgeting.")
    pst.add_argument("--from", dest="src_root", required=True,
                     help="Source root (local, UNC, or mapped drive).")
    pst.add_argument("--to", dest="dst_root", required=True,
                     help="Destination root (same path types).")
    pst.add_argument("--pattern", default="*",
                     help="Filename glob filter (default: all files).")
    pst.add_argument("--by", choices=["file", "shard"], default="file",
                     help="Granularity: 'file' = one sub-job per file (default); "
                          "'shard' = one sub-job per top-level dir (not implemented yet).")
    pst.add_argument("--coordinator", "--fixer", dest="fixer", default=None,
                     help="If set, seed gigs to this Coordinator for mesh execution. "
                          "If omitted, runs in-process like 'kiroshi run'.")
    pst.add_argument("--workers", type=int, default=0,
                     help="Local worker processes (default: cpu_count). "
                          "Mesh mode: start a runner separately.")
    pst.add_argument("--job", default=None,
                     help="Job slug (mesh mode only). Default: 'stage-<timestamp>'.")
    pst.add_argument("--token", default=None, help="Mesh auth token.")
    pst.add_argument("--sub-job-timeout", type=int, default=300,
                      help="Per-sub-job timeout in seconds (local mode only).")

    # ---- jobs (search/list jobs by regex) ----
    pj = sub.add_parser(
        "jobs",
        help="Search/list jobs by regex on subjob_id or error, filtered by state/job.")
    pj.add_argument("--coordinator", "--fixer", dest="fixer", default=cfg.fixer_url, help="Coordinator base URL, or 'auto'.")
    pj.add_argument("--grep", default=None,
                    help="Regex to match against subjob_id (default) or error (--field error).")
    pj.add_argument("--field", choices=["subjob_id", "error"], default="subjob_id",
                    help="Which column --grep matches (default: subjob_id).")
    pj.add_argument("--state", default=None,
                    help="Comma-separated states to filter (e.g. 'failed,leased').")
    pj.add_argument("--job", default=None, help="Job slug filter.")
    pj.add_argument("--limit", type=int, default=200,
                    help="Max rows to return (server caps at 2000).")
    pj.add_argument("--token", default=None, help="Mesh auth token.")
    pj.add_argument("--json", dest="as_json", action="store_true",
                    help="Emit rows as JSON (for LLM agents / piping).")

    # ---- bench (true throughput + concurrency calibration) ----
    pb = sub.add_parser(
        "bench",
        help="Measure true throughput (from output mtimes) + calibrate concurrency.")
    pb_sub = pb.add_subparsers(dest="bench_cmd", required=True)
    pbr = pb_sub.add_parser("rate", help="Report TRUE throughput of a completed/running job.")
    pbr.add_argument("--dir", default=None,
                     help="Output directory to scan (uses file mtimes).")
    pbr.add_argument("--coordinator", "--fixer", dest="fixer", default=None,
                     help="Coordinator URL — use with --job to derive throughput from "
                          "per-sub-job completed_at timestamps over HTTP (no FS access needed).")
    pbr.add_argument("--job", default=None,
                     help="Job slug (with --fixer). Filters /jobs to this job.")
    pbr.add_argument("--pattern", default="*",
                     help="Filename glob (with --dir only; default: all).")
    pbr.add_argument("--no-recursive", action="store_true",
                     help="Only scan the top level (with --dir only).")
    pbr.add_argument("--token", default=None, help="Mesh auth token (with --fixer).")
    pbc = pb_sub.add_parser("calibrate", help="Suggest per-disk concurrency from throughput samples.")
    pbc.add_argument("--samples", default=None,
                     help="Comma-separated 'conc=MBps' pairs, e.g. '1=50,2=95,4=140,8=150,16=130'.")
    pbc.add_argument("--bias", choices=["conservative", "balanced", "aggressive"],
                     default="balanced",
                     help="conservative=stay below knee; balanced=at knee; aggressive=push past knee (default).")

    # ---- mcp (Model Context Protocol server; optional extra) ----
    pmcp = sub.add_parser(
        "mcp",
        help="Run the MCP server (exposes Kiroshi to LLM agents; needs [mcp] extra).")
    pmcp.add_argument("--coordinator", "--fixer", dest="fixer", default="auto",
                      help="Default Coordinator URL for tool calls that don't pass one. "
                           "'auto' (default) discovers it on the LAN — portable "
                           "across nodes/ports; env KIROSHI_FIXER overrides.")
    pmcp.add_argument("--token", default=None,
                      help="Default mesh token for tool calls that don't pass one.")

    # ---- cursor-bridge (advisory -> Cursor agent prompt; optional [cursor] extra) ----
    pcb = sub.add_parser(
        "cursor-bridge",
        help="Run the advisory->Cursor webhook bridge (needs [cursor] extra).")
    pcb.add_argument("--host", default=None, help="Bind address (default 127.0.0.1).")
    pcb.add_argument("--port", type=int, default=None, help="Bind port (default 9123).")

    # ---- requeue ----
    pq = sub.add_parser("requeue", help="Return failed/stuck gigs to pending.")
    pq.add_argument("--coordinator", "--fixer", dest="fixer", default=cfg.fixer_url, help="Coordinator base URL, or 'auto'.")
    pq.add_argument("--state", action="append", choices=["failed", "leased", "done"],
                    help="Sub-job state(s) to requeue (repeatable; default: failed).")
    pq.add_argument("--keep-attempts", action="store_true",
                    help="Don't reset the attempt counter (default: reset to 0).")
    pq.add_argument("--token", default=None, help="Mesh auth token.")

    # ---- doctor ----
    pd = sub.add_parser("doctor", help="Preflight checks for this machine + env.")
    pd.add_argument("--coordinator", "--fixer", dest="fixer", default=cfg.fixer_url, help="Coordinator base URL, or 'auto'.")
    pd.add_argument("--task", default=None, help="Task 'module:function' to import-test.")
    pd.add_argument("--syspath", action="append", default=None,
                    help="Extra sys.path entries for the task import (repeatable).")
    pd.add_argument("--read-root", default=None, help="Override KIROSHI_READ_ROOT.")
    pd.add_argument("--write-root", default=None, help="Override KIROSHI_WRITE_ROOT.")
    pd.add_argument("--token", default=None, help="Mesh auth token.")

    # ---- ps (list registered kiroshi processes) ----
    pp = sub.add_parser("ps", help="List locally-registered Kiroshi processes.")
    pp.add_argument("--json", action="store_true", help="Emit raw JSON.")
    pp.add_argument("--all", action="store_true",
                    help="Include stale entries (crashed processes whose "
                         "manifest is still on disk). Default filters to live PIDs.")

    # ---- stop (request graceful drain of a registered process) ----
    pstop = sub.add_parser("stop", help="Ask a registered Coordinator/Runner to drain + exit.")
    pstop.add_argument("--role", choices=["fixer", "runner"], help="Limit to a role.")
    pstop.add_argument("--pid", type=int, default=None, help="Limit to one PID.")
    pstop.add_argument("--all", action="store_true", help="Stop all registered processes.")

    # ---- tray ----
    ptray = sub.add_parser("tray", help="Run the system-tray UI (needs the 'tray' extra).")
    # A tray is a GLOBAL lens on the whole mesh, so it defaults to LAN discovery
    # (the persistent beaconing Coordinator) rather than cfg.fixer_url — which may point
    # at a specific job port and would pin the icon to a single job.
    ptray.add_argument("--coordinator", "--fixer", dest="fixer", default="auto", help="Coordinator base URL, or 'auto' (default: discover).")
    ptray.add_argument("--token", default=None, help="Mesh auth token.")

    # ---- firewall (Windows: manage inbound rules for TCP fixer + UDP discovery) ----
    pfw = sub.add_parser(
        "firewall",
        help="Manage Windows Firewall rules for Kiroshi's two inbound ports "
             "(idempotent; opens TCP fixer + UDP discovery from `kiroshi.local.toml`).",
    )
    pfw.add_argument("action", choices=["install", "status", "remove"],
                     help="install/remove need admin; status is read-only.")
    pfw.add_argument("--fixer-port", type=int, default=None, action="append",
                     help="Override the TCP Coordinator port(s) to open (repeatable). "
                          f"Default: [fixer].ports or [fixer].port = {cfg.fixer_port}.")
    pfw.add_argument("--discovery-port", type=int, default=None,
                     help="Override the UDP discovery port (default: 8788 or "
                          "KIROSHI_DISCOVERY_PORT env).")
    pfw.add_argument("--remote-ip", default=None,
                     help="Scope rules to this remote IP/CIDR. Default: auto-detected "
                          "LAN /24 for defense in depth; pass 'any' to allow all.")

    # ---- install (one-command setup: fixer service + tray autostart) ----
    pins = sub.add_parser("install",
                          help="One-command setup: install the Coordinator as a Windows service "
                               "+ register the tray to auto-start on login.")
    pins.add_argument("--db", default="kiroshi.db", help="(fixer) SQLite job-store path.")
    pins.add_argument("--host", default="0.0.0.0", help="(fixer) bind host (LAN-default).")
    pins.add_argument("--port", type=int, default=cfg.fixer_port, help="(fixer) bind port.")
    pins.add_argument("--pages-dir", default=None, help="(fixer) custom views dir.")
    pins.add_argument("--no-tray", action="store_true",
                      help="Skip tray autostart registration (service only).")

    # ---- uninstall (remove the service + tray autostart) ----
    sub.add_parser("uninstall",
                   help="Remove the Kiroshi Coordinator service + tray autostart entry.")

    # ---- autostart (manage just the tray login-autostart) ----
    pau = sub.add_parser(
        "autostart",
        help="Manage tray auto-start on login (HKCU\\Run or Scheduled Task).")
    pau.add_argument("action", choices=["on", "off", "status"],
                     help="on=register, off=unregister, status=show current.")
    pau.add_argument(
        "--mode", choices=["auto", "run", "scheduled"], default="auto",
        help="'run' = HKCU\\...\\Run (logon-only, legacy). "
             "'scheduled' = Task Scheduler with restart-on-failure "
             "(recommended; self-heals within ~1 min of a crash). "
             "'auto' (default) = scheduled on Win10+, falls back to run.")

    # ---- nas (storage topology tools) ----
    pnas = sub.add_parser("nas",
                          help="Assess + benchmark NAS storage topology (PLAN §7.6).")
    nas_sub = pnas.add_subparsers(dest="nas_cmd", required=True)

    pna = nas_sub.add_parser("assess", help="Walk a dataset root and report shard balance + throughput-readiness (read-only).")
    pna.add_argument("root", help="Dataset root to assess (local path or UNC //server/share).")
    pna.add_argument("--shard-depth", type=int, default=1,
                     help="Path components that form a shard key (default: 1 = top-level dir).")
    pna.add_argument("--pattern", default=None,
                     help="File glob to count (e.g. '*.npz'); non-matching files are flagged in the format check.")
    pna.add_argument("--topology", action="store_true",
                     help="Load [[storage.disk]] from config and check shard->disk coverage + per-disk distribution.")

    pnb = nas_sub.add_parser("benchmark",
                             help="Measure per-disk read throughput at increasing concurrency; "
                                  "recommend `concurrency` per disk (finds the thrash knee).")
    pnb.add_argument("--size", type=int, default=64, help="Temp file size in MB per disk.")
    pnb.add_argument("--levels", default="1,2,4,6,8,12,16",
                     help="Comma-separated concurrency levels to sweep.")
    pnb.add_argument("--seconds", type=float, default=3.0,
                     help="Seconds to measure at each level.")

    pns = nas_sub.add_parser("shard",
                             help="Distribute a dataset into shard_NN/ dirs bin-packed by "
                                  "byte size across N disks (sets up the layout the "
                                  "scheduler expects). Emits matching [[storage.disk]] config.")
    pns.add_argument("root", help="Dataset root to shard (local or UNC).")
    pns.add_argument("--disks", type=int, default=2, help="Number of target disks/shards.")
    pns.add_argument("--dest", default=None, help="Destination root (default: same as root).")
    pns.add_argument("--dry-run", action="store_true", help="Show the plan without moving files.")
    pns.add_argument("--rebalance", action="store_true",
                     help="Re-pack an existing sharded layout for better balance.")
    pns.add_argument("--kind", default="hdd", choices=["hdd", "ssd", "nvme"],
                     help="Device kind for the emitted config (drives concurrency defaults).")
    pns.add_argument("--read-tmpl", default=None,
                     help="Read-path template for config, e.g. '//nas/disk{n}/data' ({n}=disk#).")
    pns.add_argument("--write-tmpl", default=None,
                     help="Write-path template for config, e.g. '//nas/disk{n}/data'.")

    pnp = nas_sub.add_parser("probe",
                             help="Discover a NAS's per-disk shares and scaffold a topology.")
    pnp.add_argument("server", help="NAS server (hostname or //server).")
    pnp.add_argument("--shares", default=None,
                     help="Comma-separated explicit share names to check (e.g. 'vol1,vol2').")
    pnp.add_argument("--pattern", default=None,
                     help="Share-name pattern with a range, e.g. 'disk{1..7}' or 'vol{1..3}'.")
    pnp.add_argument("--n", type=int, default=7,
                     help="Number of shares to try if no --shares/--pattern (default: disk1..7).")

    # ---- service (NSSM persistence) ----
    psvc = sub.add_parser("service",
                          help="Install/uninstall/inspect Coordinator or Runner as a Windows service (NSSM).")
    psvc.add_argument("action", choices=["install", "uninstall", "status"],
                      help="What to do.")
    psvc.add_argument("--role", choices=["fixer", "runner"],
                      help="Which service (required for install).")
    psvc.add_argument("--name", default=None, help="Service name (default: kiroshi-<role>).")
    psvc.add_argument("--python", default=None,
                      help="Python interpreter to run the service (default: this one).")
    psvc.add_argument("--account", default=None,
                      help="Run-as account, e.g. '.\\\\me' or 'DOMAIN\\\\me'. "
                           "Runners that touch a NAS MUST use a real user account.")
    psvc.add_argument("--password", default=None, help="Password for --account.")
    psvc.add_argument("--force", action="store_true",
                      help="Override the NAS-as-LocalSystem safety refusal.")
    # fixer params
    psvc.add_argument("--db", default="kiroshi.db", help="(fixer) job store path.")
    psvc.add_argument("--host", default="0.0.0.0", help="(fixer) bind host.")
    psvc.add_argument("--port", type=int, default=cfg.fixer_port, help="(fixer) bind port.")
    psvc.add_argument("--pages-dir", default=None, help="(fixer) custom views dir.")
    # runner params
    psvc.add_argument("--coordinator", "--fixer", dest="fixer", default="auto", help="(runner) Coordinator URL or 'auto'.")
    psvc.add_argument("--task", default=None, help="(runner) task 'module:function'.")
    psvc.add_argument("--workers", type=int, default=0, help="(runner) worker count.")
    psvc.add_argument("--syspath", action="append", default=None,
                      help="(runner) extra sys.path entries (repeatable).")
    psvc.add_argument("--read-root", default=None, help="(runner) KIROSHI_READ_ROOT.")
    psvc.add_argument("--write-root", default=None, help="(runner) KIROSHI_WRITE_ROOT.")
    # shared
    psvc.add_argument("--token", default=None, help="Mesh token (injected as env).")
    psvc.add_argument("--env", action="append", default=None,
                      help="Extra env as KEY=VALUE (repeatable).")

    args = parser.parse_args(raw)
    args._passthrough = passthrough

    # Best-effort: ensure the tray autostarts on login for interactive
    # commands. This closes the chicken-and-egg where autostart was only
    # registered *inside* Tray.run() (so if no one ever launched `kiroshi
    # tray`, it never registered and never autostarted). Registered once here
    # on first CLI use. Headless-only paths (seed/requeue/stop) and any
    # non-Windows host are skipped; never raises -- autostart is best-effort
    # and must not block a job. The tray itself stays decoupled from jobs.
    if args.cmd in ("run", "fixer", "runner", "status", "ps", "tray"):
        try:
            from . import autostart as _autostart
            if hasattr(_autostart, "ensure_registered"):
                _autostart.ensure_registered()
        except Exception:  # noqa: BLE001
            pass

    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "join":
        return _cmd_join(args)
    if args.cmd == "remote":
        if getattr(args, "remote_cmd", None) == "sync":
            return _cmd_remote_sync(args)
        # sync doesn't require a host; probe/join do.
        if not getattr(args, "host", None):
            print("[remote] 'probe' and 'join' require a host argument.",
                  file=sys.stderr)
            return 2
        from .remote import run_remote
        return run_remote(args)
    if args.cmd == "fixer":
        return _cmd_fixer(args)
    if args.cmd == "runner":
        return _cmd_runner(args)
    if args.cmd == "seed":
        return _cmd_seed(args)
    if args.cmd == "status":
        return _cmd_status(args)
    if args.cmd == "requeue":
        return _cmd_requeue(args)
    if args.cmd == "doctor":
        return _cmd_doctor(args)
    if args.cmd == "ps":
        return _cmd_ps(args)
    if args.cmd == "stop":
        return _cmd_stop(args)
    if args.cmd == "tray":
        return _cmd_tray(args)
    if args.cmd == "firewall":
        return _cmd_firewall(args)
    if args.cmd == "install":
        return _cmd_install(args)
    if args.cmd == "uninstall":
        return _cmd_uninstall(args)
    if args.cmd == "autostart":
        return _cmd_autostart(args)
    if args.cmd == "pipeline":
        return _cmd_pipeline(args)
    if args.cmd == "capabilities":
        return _cmd_capabilities(args)
    if args.cmd == "mcp":
        return _cmd_mcp(args)
    if args.cmd == "cursor-bridge":
        return _cmd_cursor_bridge(args)
    if args.cmd == "stage":
        return _cmd_stage(args)
    if args.cmd == "jobs":
        return _cmd_jobs(args)
    if args.cmd == "bench":
        return _cmd_bench(args)
    if args.cmd == "nas":
        return _cmd_nas(args)
    if args.cmd == "service":
        return _cmd_service(args)
    return 1


def _resolve_fixer_arg(value: str) -> str:
    """Turn a ``--fixer`` value into a base URL, discovering it if 'auto'."""
    from .discovery import discover_fixer
    from .worker import _AUTO

    if (value or "").strip().lower() not in _AUTO:
        return value
    print("[kiroshi] discovering fixer on the LAN...", flush=True)
    url = discover_fixer(timeout=6.0)
    if not url:
        raise SystemExit("No fixer beacon heard. Is a Coordinator running? "
                         "Pass --fixer http://HOST:PORT to skip discovery.")
    print(f"[kiroshi] found fixer at {url}", flush=True)
    return url


def _cmd_fixer(args) -> int:
    import uvicorn

    from . import security
    from .config import current_host
    from .coordinator import create_app
    from .discovery import BeaconBroadcaster
    from .jobstore import JobStore
    from .logsetup import tee_process_output

    tee_process_output("fixer")

    token = security.ensure_fixer_token(args.token, allow_insecure=args.no_auth)
    if token:
        # Keep the token off disk: the lines below print it to the console, and
        # stdout is teed into a state-dir log readable by other local users.
        from .logsetup import redact
        redact(token)
    bind_is_public = args.host not in ("127.0.0.1", "localhost", "::1")
    if token is None:
        print("[fixer] WARNING: running with NO AUTH (--no-auth).", flush=True)
        if bind_is_public:
            print("[fixer] DANGER: --no-auth on a public bind address exposes an "
                  "unauthenticated job API to the whole LAN. Anyone can enqueue/steal "
                  "work. Use a token, or bind 127.0.0.1.", flush=True)
    elif bind_is_public:
        print(f"[fixer] NOTE: binding {args.host} exposes the mesh API to the LAN "
              "(token-gated). Do NOT port-forward this to the internet — put the "
              "mesh on a private overlay (WireGuard/Tailscale) or TLS instead.",
              flush=True)
    else:
        print("[fixer] bound to loopback (secure default). To let other machines "
              "join, restart with --host 0.0.0.0.", flush=True)

    # Split-brain guard: refuse to spin up a discoverable Coordinator if another
    # one is already reachable on this LAN. Two Fixers → two disjoint queues
    # + two disjoint per-spindle budgets → both saturate the shared NAS
    # assuming the other doesn't exist. Skipped when we're not participating
    # in LAN discovery anyway (--no-beacon or loopback bind).
    beacon_would_broadcast = not args.no_beacon and bind_is_public
    if beacon_would_broadcast and not args.force_second_fixer:
        from .discovery import check_singleton_fixer

        other = check_singleton_fixer(timeout=3.0)
        if other:
            print(f"[fixer] REFUSING to start: another Coordinator is already "
                  f"discoverable at {other}.\n"
                  f"  Two Fixers on one LAN means two disjoint queues and two\n"
                  f"  disjoint per-spindle budgets — both would happily saturate\n"
                  f"  the shared NAS assuming the other doesn't exist.\n"
                  f"  Fix: stop the other Coordinator, or pass --force-second-fixer if\n"
                  f"  you deliberately want two isolated meshes on the same LAN.",
                  file=sys.stderr)
            return 3

    # A4: --force-second-fixer now requires KIROSHI_ALLOW_SECOND_COORDINATOR=1
    # as a two-key safety. A single flag is too easy to pass by muscle memory.
    force_second = bool(args.force_second_fixer)
    if force_second:
        if os.environ.get("KIROSHI_ALLOW_SECOND_COORDINATOR") != "1":
            print(
                "[fixer] REFUSING: --force-second-fixer now requires the "
                "environment variable KIROSHI_ALLOW_SECOND_COORDINATOR=1.\n"
                "  This two-key safety prevents accidentally running a second "
                "coordinator on the same machine.\n"
                "  If you deliberately need two isolated meshes on different "
                "NAS pools, set the env var AND pass the flag.",
                file=sys.stderr,
            )
            return 3

    # Machine-level exclusive lock (beacon-independent): catches the same-box
    # footgun that --no-beacon bypasses. The lock auto-releases on process
    # death. Override path (deliberate second mesh) skips the lock entirely.
    from .coordlock import acquire_or_refuse

    coord_lock = acquire_or_refuse(
        info={"port": args.port, "db": args.db, "host": args.host},
        allow_override=force_second,
    )
    if coord_lock is None:
        return 3

    store = JobStore(args.db, max_retries=args.max_retries)
    from .storage import load_topology

    app = create_app(
        store,
        lease_ttl=args.lease_ttl,
        reap_interval=args.reap_interval,
        pages_dir=args.pages_dir,
        token=token,
        disks=load_topology(),
        fair_share=args.fair_share,
    )
    beacon = None
    if not args.no_beacon:
        beacon = BeaconBroadcaster(fixer_port=args.port).start()
        print(f"[fixer] broadcasting discovery beacon (port {args.port}); "
              f"runners can use --fixer auto", flush=True)
    print(f"[fixer] db={args.db}  dashboard=http://{args.host}:{args.port}/", flush=True)
    if token:
        print(f"[fixer] mesh token: {token}", flush=True)
        print("[fixer]   on other machines: set KIROSHI_TOKEN, or pass --token", flush=True)
        print(f"[fixer]   open dashboard: http://{current_host()}:{args.port}/?token={token}",
              flush=True)
    from .logsetup import current_log_path
    from .processreg import ProcessRegistration

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    reg = ProcessRegistration(
        "fixer",
        {
            "launch_command": _launch_command(),
            "host": args.host, "port": args.port,
            "dashboard": f"http://{current_host()}:{args.port}/",
            "log_path": current_log_path(),
            "auth": bool(token),
        },
        on_stop=lambda: setattr(server, "should_exit", True),
    ).start()
    try:
        server.run()
    finally:
        reg.close()
        if beacon is not None:
            beacon.stop()
        coord_lock.release()
    return 0


_SECRET_FLAGS = {"--token", "--password"}


def _win_quote(s: str) -> str:
    """Windows command-line quoting for NSSM AppParameters.

    ``shlex.quote`` is POSIX-only and wraps any path containing backslashes
    in **single quotes** — Windows treats those as literal characters, so
    SQLite would try to open ``'C:\\path\\jobs.db'`` (quotes included) and
    fail. This uses double quotes only when needed (spaces), leaving
    backslash paths alone.
    """
    if " " in s or "\t" in s:
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _launch_command() -> str:
    """Best-effort reconstruction of the command line that launched us, with flags.

    Secret-bearing flags (``--token``, ``--password``) are masked: this string is
    surfaced on the dashboard/history and teed to logs, so it must not carry the
    mesh token even if an operator passed it on the command line.
    """
    import shlex

    argv = list(sys.argv)
    prog = "kiroshi"
    raw = [prog] + argv[1:]
    parts: list[str] = []
    mask_next = False
    for tok in raw:
        if mask_next:
            parts.append("***")
            mask_next = False
            continue
        if tok in _SECRET_FLAGS:
            parts.append(tok)
            mask_next = True
        elif "=" in tok and tok.split("=", 1)[0] in _SECRET_FLAGS:
            parts.append(tok.split("=", 1)[0] + "=***")
        else:
            parts.append(tok)
    try:
        return " ".join(shlex.quote(p) for p in parts)
    except Exception:  # noqa: BLE001
        return " ".join(parts)


def _cmd_run(args) -> int:
    from .runjob import run_job

    return run_job(
        task_ref=args.task,
        items=args.items,
        jobs=args.jobs,
        enumerate_=args.enumerate,
        task_args=getattr(args, "_passthrough", []),
        job=args.job,
        label=args.label,
        origin=_resolve_origin(getattr(args, "origin", None)),
        workers=args.workers,
        capacity=args.capacity,
        port=args.port,
        lan=args.lan,
        db=args.db,
        token=args.token,
        read_root=args.read_root,
        write_root=args.write_root,
        gig_timeout=args.gig_timeout,
        syspath=args.syspath,
        max_retries=args.max_retries,
        serve_task=args.serve_task,
        max_tasks_per_child=args.max_tasks_per_child,
        gc_between_tasks=args.gc_between_tasks,
        launch_command=_launch_command(),
        force_second_fixer=getattr(args, "force_second_fixer", False),
    )


def _cmd_join(args) -> int:
    from .join import join

    return join(
        fixer=args.fixer,
        task=args.task,
        token=args.token,
        workers=args.workers,
        service=args.service,
        accept_task_hash=args.accept_task_hash,
        syspath=args.syspath,
        read_root=args.read_root,
        write_root=args.write_root,
        gig_timeout=args.gig_timeout,
        launch_command=_launch_command(),
    )


def _cmd_runner(args) -> int:
    from . import security
    from .logsetup import redact, tee_process_output

    tee_process_output("runner", host=args.host)
    redact(security.resolve_token(getattr(args, "token", None)))

    from .worker import Runner

    Runner(
        fixer_url=args.fixer,
        task_ref=args.task,
        workers=args.workers,
        capacity=args.capacity,
        runner_id=args.id,
        host=args.host,
        poll_interval=args.poll,
        heartbeat_interval=args.heartbeat,
        item_retries=args.retries,
        gig_timeout=args.gig_timeout,
        extra_syspath=args.syspath,
        token=args.token,
        launch_command=_launch_command(),
        max_tasks_per_child=getattr(args, "max_tasks_per_child", None),
        gc_between_tasks=getattr(args, "gc_between_tasks", False),
    ).run()
    return 0


def _auth_headers(args) -> dict:
    from . import security

    tok = security.resolve_token(getattr(args, "token", None))
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _resolve_origin(cli_arg: Optional[str]) -> Optional[dict]:
    """Resolve the M9 attribution blob from ``--origin`` or ``KIROSHI_ORIGIN``.

    Both must be JSON that parses to an object (dict). Anything else prints a
    warning and returns None so the caller proceeds without attribution — a
    malformed origin should never block the run.
    """
    raw = cli_arg if cli_arg is not None else os.environ.get("KIROSHI_ORIGIN")
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        print(f"[origin] ignoring --origin/KIROSHI_ORIGIN: not valid JSON ({raw[:80]!r})",
              file=sys.stderr)
        return None
    if not isinstance(obj, dict):
        print(f"[origin] ignoring --origin/KIROSHI_ORIGIN: must be a JSON object, "
              f"got {type(obj).__name__}", file=sys.stderr)
        return None
    return obj


def _cmd_seed(args) -> int:
    import requests

    args.fixer = _resolve_fixer_arg(args.fixer)
    headers = _auth_headers(args)
    origin = _resolve_origin(getattr(args, "origin", None))

    def post(gigs: list[dict]) -> None:
        body: dict = {"gigs": gigs}
        if args.job:
            body["job"] = args.job
        if args.label:
            body["label"] = args.label
        if origin:
            body["origin"] = origin
        r = requests.post(f"{args.fixer.rstrip('/')}/seed", json=body,
                          timeout=60, headers=headers)
        r.raise_for_status()
        out = r.json()
        print(f"  seeded {out['inserted']}/{out['received']}", flush=True)

    buf: list[dict] = []
    total = 0
    if args.demo > 0:
        for i in range(args.demo):
            buf.append({"subjob_id": f"demo-{i:06d}", "spec": {"seconds": 0.05}})
            if len(buf) >= args.batch:
                post(buf)
                total += len(buf)
                buf = []
    elif args.jobs:
        with open(args.jobs, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                g = json.loads(line)
                g.setdefault("subjob_id", uuid.uuid4().hex)
                g.setdefault("spec", {})
                buf.append(g)
                if len(buf) >= args.batch:
                    post(buf)
                    total += len(buf)
                    buf = []
    else:
        print("Nothing to seed: pass --demo N or --jobs FILE.", file=sys.stderr)
        return 2

    if buf:
        post(buf)
        total += len(buf)
    print(f"[seed] submitted {total} sub-job(s).", flush=True)
    return 0


def _cmd_status(args) -> int:
    import requests

    args.fixer = _resolve_fixer_arg(args.fixer)
    r = requests.get(f"{args.fixer.rstrip('/')}/status", timeout=30,
                     headers=_auth_headers(args))
    r.raise_for_status()
    print(json.dumps(r.json(), indent=2))
    return 0


def _cmd_pipeline(args) -> int:
    """Declarative multi-stage pipeline. Replaces ad-hoc cascade-seeder glue
    with typed dependency edges (each / quorum / all / artifact)."""
    from .pipeline import Pipeline, PipelineCoordinator

    pipe = Pipeline.from_toml(args.spec)
    if getattr(args, "token", None):
        pipe.token = args.token

    if args.pipe_cmd == "validate":
        print(f"pipeline: {len(pipe.stages)} stages, {len(pipe.edges)} edges, "
              f"poll={pipe.poll_s}s")
        for name, s in pipe.stages.items():
            extra = f" command={'yes' if s.command else 'no'}" if s.command else ""
            print(f"  stage {name:12s} fixer={s.fixer} job={s.job}{extra}")
        for e in pipe.edges:
            tag = e.kind + (f":{e.k}" if e.kind == "quorum" else "")
            gate = f" gate={e.artifact}" if e.artifact else ""
            print(f"  edge  {e.upstream:12s} --{tag}--> {e.downstream}{gate}")
        return 0

    def log(m: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)

    coord = PipelineCoordinator(pipe, log=log)
    if args.once:
        coord.tick()
        return 0
    coord.run()
    return 0


def _cmd_remote_sync(args) -> int:
    """kiroshi remote sync — git-pull tracked repos on every [hosts.*] node.
    Dry-run by default is enforced at the operator level (must pass --dry-run
    or explicitly opt into execution). The planner is unit-testable; this
    handler is just a thin executor."""
    from . import remote_sync

    cfg = load_config()
    hosts = cfg.hosts or {}
    if not hosts:
        print("[sync] no [hosts.*] in kiroshi config — nothing to sync.",
              file=sys.stderr)
        return 2

    repos = tuple((args.repos or "").split(",")) if args.repos else remote_sync.DEFAULT_REPOS
    repos = tuple(r.strip() for r in repos if r.strip())

    plans = remote_sync.plan_sync(
        hosts=hosts, repos=repos,
        reinstall=args.reinstall, restart=args.restart,
        local_hostnames=remote_sync.local_hostnames(),
    )

    # Dry-run: print the plan and stop. This is the intended first invocation
    # so an operator can eyeball the exact commands before we ssh anywhere.
    if args.dry_run:
        print(remote_sync.render_plan(plans))
        print()
        print("[sync] DRY RUN — nothing was executed. Re-run without --dry-run to apply.")
        return 0

    failures = remote_sync.execute_plan(plans, dry_run=False)
    if failures:
        print(f"[sync] completed with {failures} failed step(s).", file=sys.stderr)
        return 1
    print("[sync] all hosts synced OK.")
    return 0


def _cmd_bench(args) -> int:
    """kiroshi bench — measure true throughput + calibrate concurrency.

    Subcommands:
      rate       — report TRUE throughput from output-file mtimes (not wall-clock)
      calibrate  — suggest per-disk concurrency from throughput-vs-concurrency samples
    """
    from . import bench

    if args.bench_cmd == "rate":
        if args.fixer:
            # HTTP mode: derive true throughput from per-sub-job completed_at over
            # /jobs. NOTE: /jobs is hard-capped at 2000 rows (most-recent-first),
            # so for jobs > 2000 done gigs this is a SAMPLE of the tail, not
            # the whole run — we say so in the output. For a total count use
            # /status; for the full mtime-based rate use --dir on the outputs.
            LIMIT = 2000
            import requests
            params = {"state": "done", "limit": LIMIT, "job": args.job or ""}
            if args.token:
                params["token"] = args.token
            try:
                r = requests.get(f"{args.fixer.rstrip('/')}/jobs",
                                 params=params, timeout=30)
                r.raise_for_status()
            except requests.RequestException as exc:
                print(f"[bench rate] FAILED to reach {args.fixer}: {exc}",
                      file=sys.stderr)
                return 1
            rows = r.json().get("jobs", [])
            times = [row["completed_at"] for row in rows
                     if row.get("completed_at")]
            if not times:
                print(f"[bench rate] no completed gigs found for job "
                      f"{args.job!r} on {args.fixer}.")
                return 0
            span = max(0.0, max(times) - min(times))
            n = len(times)
            sampled = " (SAMPLE: most-recent 2000 gigs — use --dir for whole run)" \
                if n >= LIMIT else ""
            print(f"[bench rate] job={args.job}  fixer={args.fixer}{sampled}")
            if span > 0:
                print(f"  {n} completed gigs, span={span:.1f}s, "
                      f"true rate={n / span:.2f} gigs/s")
            else:
                print(f"  {n} completed gigs, span=0s, rate=n/a")
            return 0
        if not args.dir:
            print("[bench rate] --dir or --fixer+--job is required.",
                  file=sys.stderr)
            return 2
        rate = bench.rate_from_dir(
            args.dir, pattern=args.pattern,
            recursive=not args.no_recursive)
        print(f"[bench rate] {rate}")
        print(f"  dir={args.dir}  pattern={args.pattern}"
              f"  recursive={not args.no_recursive}")
        if rate.count > 0:
            print(f"  {rate.count} files, span={rate.span_s:.1f}s, "
                  f"true rate={rate.items_per_s:.2f} files/s")
        else:
            print("  no files found matching the pattern.")
        return 0

    if args.bench_cmd == "calibrate":
        if not args.samples:
            print("[bench calibrate] --samples is required, e.g. "
                  "--samples '1=50,2=95,4=140,8=150,16=130'", file=sys.stderr)
            return 2
        pairs = []
        for token in args.samples.split(","):
            token = token.strip()
            if "=" not in token:
                continue
            conc_s, mbps_s = token.split("=", 1)
            pairs.append((int(conc_s.strip()), float(mbps_s.strip())))
        if not pairs:
            print("[bench calibrate] could not parse any conc=MBps pairs.",
                  file=sys.stderr)
            return 2
        rec = bench.suggest_concurrency(pairs, bias=args.bias)
        print(f"[bench calibrate] recommended concurrency = {rec} "
              f"(bias={args.bias})")
        print(f"  samples: {pairs}")
        peak_conc, peak_mbps = max(pairs, key=lambda s: s[1])
        print(f"  peak: {peak_mbps:.1f} MB/s at concurrency {peak_conc}")
        print(f"  suggested: concurrency {rec} "
              f"(paste into [[storage.disk]] concurrency = {rec})")
        # By design this PRINTS rather than patching kiroshi.local.toml: a
        # surgical TOML edit that preserves comments + other keys is easy to get
        # wrong, and topology changes deserve a human eyeball. A future --write
        # could patch it safely (round-trip via tomlkit) if that's ever wanted.
        return 0

    print(f"[bench] unknown subcommand {args.bench_cmd!r}", file=sys.stderr)
    return 2


def _cmd_jobs(args) -> int:
    """Search/list jobs by regex on subjob_id or error, filtered by state/job."""
    import requests
    fixer = _resolve_fixer_arg(args.fixer)
    params: dict[str, Any] = {"limit": min(max(args.limit, 1), 2000)}
    if args.state:
        params["state"] = args.state
    if args.job:
        params["job"] = args.job
    if args.grep:
        if args.field == "error":
            params["error_re"] = args.grep
        else:
            params["subjob_id_re"] = args.grep
    if args.token:
        params["token"] = args.token
    try:
        r = requests.get(f"{fixer.rstrip('/')}/jobs", params=params, timeout=30)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"[jobs] FAILED to reach {fixer}: {exc}", file=sys.stderr)
        return 1
    body = r.json()
    if "error" in body:
        print(f"[jobs] {body['error']}", file=sys.stderr)
        return 1
    rows = body.get("jobs", [])
    if not rows:
        print("[jobs] no matching jobs found.")
        return 0
    if args.as_json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    # compact table
    print(f"{'JOB_ID':40s} {'STATE':10s} {'ATT':>3s} {'DISK':8s} ERROR")
    print("-" * 80)
    for row in rows:
        jid = (row.get("subjob_id") or "")[:40]
        state = row.get("state", "")[:10]
        att = str(row.get("attempts", ""))
        disk = str(row.get("disk") or "")[:8]
        err = (row.get("error") or "")[:40]
        print(f"{jid:40s} {state:10s} {att:>3s} {disk:8s} {err}")
    print(f"\n{len(rows)} job(s) shown.")
    return 0


def _cmd_stage(args) -> int:
    """Stage (copy) a dataset between storage tiers with mesh I/O budgeting.

    Two execution paths:
      * local (no --fixer): reuses the in-process run_job pipeline with
        kiroshi.staging:run + enumerate_gigs (same as 'kiroshi run --enumerate').
      * mesh (--fixer): enumerates gigs locally, seeds them to the Coordinator; the
        operator starts a runner bound to kiroshi.staging:run separately.
    """
    from .staging import enumerate_gigs

    if getattr(args, "by", "file") == "shard":
        # TODO(roadmap A1): per-shard gigs (one sub-job copies a whole top-level dir)
        # for fewer, larger transfers on sharded NAS layouts. Until then, --by
        # file is the only granularity.
        print("[stage] --by shard is not implemented yet; use --by file "
              "(one sub-job per file). See ROADMAP.", file=sys.stderr)
        return 2

    task_args = ["--from", args.src_root, "--to", args.dst_root]
    if args.pattern and args.pattern != "*":
        task_args += ["--pattern", args.pattern]

    if args.fixer:
        # mesh mode: enumerate + seed
        gigs = list(enumerate_gigs({
            "from": args.src_root, "to": args.dst_root,
            "pattern": args.pattern,
        }))
        if not gigs:
            print("[stage] no files found to stage.", file=sys.stderr)
            return 1
        job = args.job or f"stage-{int(time.time())}"
        import requests
        payload = {"gigs": gigs, "job": job,
                   "label": f"stage: {args.src_root} -> {args.dst_root}"}
        params = {"token": args.token} if args.token else {}
        try:
            r = requests.post(f"{args.fixer.rstrip('/')}/seed",
                              params=params, json=payload, timeout=60)
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"[stage] FAILED to seed to {args.fixer}: {exc}",
                  file=sys.stderr)
            return 1
        print(f"[stage] seeded {len(gigs)} gigs to {args.fixer} (job={job}).")
        print(f"  Start a runner:  kiroshi runner --fixer {args.fixer} "
              f"--task kiroshi.staging:run --workers N "
              f"--syspath <kiroshi-src> --syspath <kiroshi-src>/src")
        return 0

    # local mode: reuse run_job
    from .runjob import run_job
    return run_job(
        "kiroshi.staging:run",
        enumerate_=True,
        task_args=task_args,
        read_root=args.src_root,
        write_root=args.dst_root,
        workers=args.workers,
        gig_timeout=float(args.gig_timeout),
        token=args.token,
        syspath=None,
    )


def _cmd_mcp(args) -> int:
    """Run the MCP server on stdio so an LLM client (Claude Desktop, Cursor,
    etc.) can enumerate + call Kiroshi tools without shelling out to the CLI.
    Optional extra: 'pip install kiroshi[mcp]'."""
    from . import mcp_server
    return mcp_server.run_stdio(default_fixer=args.fixer, default_token=args.token)


def _cmd_cursor_bridge(args) -> int:
    """Run the Kiroshi-advisory -> Cursor-agent webhook bridge.
    Optional extra: 'pip install kiroshi[cursor]'."""
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print("kiroshi cursor-bridge: missing deps. Install with: "
              "pip install 'kiroshi[cursor]'", file=sys.stderr)
        return 2
    from .integrations.cursor_bridge import DEFAULT_HOST, DEFAULT_PORT, create_app
    import uvicorn
    host = args.host or os.environ.get("KIROSHI_CURSOR_HOST", DEFAULT_HOST)
    port = args.port or int(os.environ.get("KIROSHI_CURSOR_PORT", DEFAULT_PORT))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
    return 0


def _cmd_capabilities(args) -> int:
    """Print the machine-readable capability map. Consumed by LLM agents +
    the planned MCP server; ``--json`` emits the structured form."""
    from . import capabilities as _cap
    print(_cap.as_json() if getattr(args, "as_json", False) else _cap.as_table())
    return 0


def _cmd_requeue(args) -> int:
    import requests

    args.fixer = _resolve_fixer_arg(args.fixer)
    states = args.state or ["failed"]
    r = requests.post(
        f"{args.fixer.rstrip('/')}/requeue",
        json={"states": states, "reset_attempts": not args.keep_attempts},
        timeout=30,
        headers=_auth_headers(args),
    )
    r.raise_for_status()
    n = r.json().get("requeued", 0)
    print(f"[requeue] {n} sub-job(s) ({', '.join(states)}) -> pending", flush=True)
    return 0


def _cmd_ps(args) -> int:
    from .processreg import list_registered

    procs = list_registered(include_stale=bool(getattr(args, "all", False)))
    if args.json:
        print(json.dumps(procs, indent=2))
        return 0
    if not procs:
        print("no registered Kiroshi processes on this machine.")
        return 0
    print(f"{'ST':<3} {'ROLE':<7} {'PID':>7}  {'HOST':<16} LAUNCH COMMAND")
    for p in procs:
        state = "  " if p.get("_alive", True) else "!!"
        print(f"{state:<3} {p.get('role',''):<7} {p.get('pid',''):>7}  "
              f"{p.get('host',''):<16} {p.get('launch_command','')}")
    return 0


def _cmd_stop(args) -> int:
    from .processreg import list_registered, request_stop

    procs = list_registered()
    targets = []
    for p in procs:
        if args.role and p.get("role") != args.role:
            continue
        if args.pid is not None and p.get("pid") != args.pid:
            continue
        targets.append(p)
    if not targets:
        print("no matching registered processes.")
        return 1
    if len(targets) > 1 and not args.all and args.pid is None:
        print(f"{len(targets)} processes match; pass --all (or --pid) to confirm.")
        for p in targets:
            print(f"  {p.get('role')} pid={p.get('pid')} {p.get('launch_command','')}")
        return 1
    n = 0
    for p in targets:
        if request_stop(p.get("role", ""), int(p.get("pid", 0))):
            print(f"stop requested: {p.get('role')} pid={p.get('pid')}")
            n += 1
    return 0 if n else 1


def _cmd_service(args) -> int:
    from . import winservice as ws
    from .appstate import logs_dir, state_dir

    if args.action == "status":
        names = [args.name] if args.name else [ws.DEFAULT_FIXER_SERVICE,
                                               ws.DEFAULT_RUNNER_SERVICE]
        for n in names:
            print(ws.status(n))
        return 0

    nssm = ws.find_nssm()
    if not nssm:
        print("[service] could not find nssm.exe.\n"
              "  Install NSSM (https://nssm.cc) and either put it on PATH, set "
              "KIROSHI_NSSM=<path to nssm.exe>, or drop it in "
              f"{state_dir()}\\nssm.exe", file=sys.stderr)
        return 2
    if not ws.is_admin():
        print("[service] WARNING: not elevated. Service install/remove needs an "
              "Administrator shell; this will likely fail. Re-run from an elevated "
              "terminal (or use scripts\\install_service.ps1).", file=sys.stderr)

    if args.action == "uninstall":
        name = args.name or (ws.DEFAULT_FIXER_SERVICE if args.role != "runner"
                             else ws.DEFAULT_RUNNER_SERVICE)
        ok, out = ws.uninstall(ws.build_uninstall_commands(nssm, name))
        print(out)
        print(f"[service] {'removed' if ok else 'uninstall reported errors for'} {name}")
        return 0 if ok else 1

    # ---- install ----
    if not args.role:
        print("[service] install requires --role fixer|runner", file=sys.stderr)
        return 2
    python_exe = args.python or sys.executable
    name = args.name or (ws.DEFAULT_FIXER_SERVICE if args.role == "fixer"
                        else ws.DEFAULT_RUNNER_SERVICE)
    env: dict[str, str] = {}
    for kv in (args.env or []):
        if "=" in kv:
            k, v = kv.split("=", 1)
            env[k.strip()] = v
    if args.token:
        env["KIROSHI_TOKEN"] = args.token
    if args.read_root:
        env["KIROSHI_READ_ROOT"] = args.read_root
    if args.write_root:
        env["KIROSHI_WRITE_ROOT"] = args.write_root

    if args.role == "fixer":
        parts = ["-m", "kiroshi", "fixer", "--db", args.db,
                 "--host", args.host, "--port", str(args.port)]
        if args.pages_dir:
            parts += ["--pages-dir", args.pages_dir]
        account = args.account or "LocalSystem"
        display = "Kiroshi Coordinator"
        desc = "Kiroshi coordinator (hands gigs to runners; serves the dashboard)."
    else:
        if not args.task:
            print("[service] runner install requires --task module:function",
                  file=sys.stderr)
            return 2
        # The NAS lesson, enforced: refuse a NAS-bound runner under a builtin account.
        if ws.runner_needs_user_account(args.read_root, args.write_root, args.account) \
                and not args.force:
            print("[service] REFUSING: this Runner targets a NAS (UNC read/write "
                  "root) but would run as LocalSystem, which CANNOT use the "
                  "per-user NAS credentials in Credential Manager — gigs would "
                  "fail with 'path not found'.\n"
                  "  Fix: pass --account '.\\\\<user>' --password <pw> for an "
                  "account whose Credential Manager holds the NAS login.\n"
                  "  (Override with --force if you really mean LocalSystem.)",
                  file=sys.stderr)
            return 2
        parts = ["-m", "kiroshi", "runner", "--coordinator", args.fixer, "--task", args.task]
        if args.workers:
            parts += ["--workers", str(args.workers)]
        for sp in (args.syspath or []):
            parts += ["--syspath", sp]
        account = args.account  # may be None -> LocalSystem default
        display = "Kiroshi Runner"
        desc = "Kiroshi worker node (pulls gigs, runs them on a local process pool)."

    app_parameters = " ".join(_win_quote(p) for p in parts)
    cmds = ws.build_install_commands(
        nssm=nssm, service_name=name, python_exe=python_exe,
        app_parameters=app_parameters, app_directory=os.getcwd(),
        log_dir=str(logs_dir()), display_name=display, description=desc,
        account=account, password=args.password, env=env or None,
    )
    ok, out = ws.install(cmds)
    print(out)
    if ok:
        print(f"[service] installed '{name}'. Start it with:  nssm start {name}  "
              f"(or:  sc start {name})")
    else:
        print(f"[service] install FAILED for '{name}' (see output above).")
    return 0 if ok else 1


def _cmd_tray(args) -> int:
    try:
        from .tray import run_tray
    except ImportError as e:  # pragma: no cover
        print(f"[tray] missing dependency: {e}\n"
              f"Install the tray extra:  pip install kiroshi[tray]")
        return 2
    return run_tray(fixer=args.fixer, token=args.token)


def _cmd_firewall(args) -> int:
    """Manage the two inbound firewall rules Kiroshi's Coordinator needs.

    Reads current config to derive the desired ports so re-running always
    matches ``kiroshi.local.toml``; cleans up stale ``Kiroshi *`` rules from
    previous experiments in the same shot. Write actions require admin;
    ``status`` is read-only.
    """
    if sys.platform != "win32":
        print("[firewall] this command is Windows-only (uses netsh).",
              file=sys.stderr)
        return 2

    from . import firewall as fw
    from .discovery import discovery_port

    cfg = load_config()
    fixer_ports = args.fixer_port or cfg.fixer_ports or [cfg.fixer_port]
    disc_port = args.discovery_port or discovery_port()
    remote_ip = args.remote_ip
    if remote_ip is None:
        auto = fw.pick_lan_subnet()
        remote_ip = auto or "any"
        if auto:
            print(f"[firewall] auto-detected LAN subnet: {auto}")
        else:
            print("[firewall] no private /24 detected — falling back to remote_ip=any. "
                  "Pass --remote-ip <cidr> to pin.", file=sys.stderr)

    print(f"[firewall] fixer TCP ports: {', '.join(str(p) for p in fixer_ports)}")
    rules = fw.plan_rules(fixer_ports, disc_port, remote_ip=remote_ip)
    existing = fw.list_kiroshi_rules()

    if args.action == "status":
        print(fw.format_status(rules, existing))
        return 0

    if args.action == "install":
        print(f"[firewall] desired rules ({len(rules)}):")
        for r in rules:
            print(f"  - {r.name}: {r.protocol} {r.port} remote={r.remote_ip} profiles={r.profiles}")
        stale = [n for n in existing
                 if n not in {r.name for r in rules}]
        if stale:
            print(f"[firewall] stale drift to remove ({len(stale)}):")
            for n in stale:
                print(f"  - {n}")
        if not fw.is_admin():
            print("\n[firewall] NOT elevated — cannot modify firewall rules.\n"
                  "  Copy-paste this to re-run under UAC (you'll see one prompt):\n"
                  f"    {fw.elevated_install_hint('firewall install')}\n",
                  file=sys.stderr)
            return 2
        res = fw.apply_rules(rules)
        for n in res.removed:
            print(f"[firewall] removed: {n}")
        for n in res.added:
            print(f"[firewall] added:   {n}")
        for e in res.errors:
            print(f"[firewall] ERROR:   {e}", file=sys.stderr)
        return 0 if res.ok else 1

    if args.action == "remove":
        if not existing:
            print("[firewall] no Kiroshi-* rules installed; nothing to remove.")
            return 0
        if not fw.is_admin():
            print("\n[firewall] NOT elevated — cannot modify firewall rules.\n"
                  "  Copy-paste this to re-run under UAC:\n"
                  f"    {fw.elevated_install_hint('firewall remove')}\n",
                  file=sys.stderr)
            return 2
        errs = 0
        for n in existing:
            if fw.delete_rule(n):
                print(f"[firewall] removed: {n}")
            else:
                print(f"[firewall] ERROR removing {n}", file=sys.stderr)
                errs += 1
        return 0 if errs == 0 else 1

    return 2


def _cmd_nas(args) -> int:
    from . import nascli
    from .storage import load_topology

    if args.nas_cmd == "assess":
        disks = load_topology() if args.topology else None
        report = nascli.assess_layout(args.root, depth=args.shard_depth,
                                      pattern=args.pattern, disks=disks)
        nascli.print_assessment(report, disks)
        return 0 if report["total_files"] > 0 else 1

    if args.nas_cmd == "benchmark":
        disks = load_topology()
        if not disks:
            print("[benchmark] no [[storage.disk]] topology in config — nothing to "
                  "benchmark. Declare disks in kiroshi.local.toml first.", file=sys.stderr)
            return 2
        levels = tuple(int(x) for x in args.levels.split(",") if x.strip())
        reports = nascli.benchmark_disks(disks, size_mb=args.size, levels=levels,
                                         seconds=args.seconds)
        nascli.print_benchmark(reports)
        return 0

    if args.nas_cmd == "shard":
        files = nascli._collect_files(args.root)
        if not files:
            print(f"[shard] no files found under {args.root!r}", file=sys.stderr)
            return 1
        total_bytes = sum(s for _, s in files)
        print(f"[shard] {len(files)} files, {nascli._fmt_bytes(total_bytes)} "
              f"-> {args.disks} disk(s)", flush=True)
        bins = nascli.plan_shard(files, args.disks)
        nascli.print_shard_plan(bins, total_bytes)
        print(flush=True)
        if args.dry_run:
            print("dry-run plan (no files moved):", flush=True)
        else:
            print(f"{'rebalancing' if args.rebalance else 'moving'} files...", flush=True)
        result = nascli.execute_shard(
            args.root, bins, dest=args.dest, dry_run=args.dry_run,
            rebalance=args.rebalance)
        if not args.dry_run:
            print(f"  moved={result['moved']} skipped={result['skipped']} "
                  f"errors={result['errors']}", flush=True)
        print(flush=True)
        print("Matching topology (paste into kiroshi.local.toml):", flush=True)
        print(flush=True)
        print(nascli.emit_shard_config(args.disks, kind=args.kind,
                                       read_tmpl=args.read_tmpl,
                                       write_tmpl=args.write_tmpl), flush=True)
        return 0 if result["errors"] == 0 else 1

    if args.nas_cmd == "probe":
        server = args.server.lstrip("/").split("/")[0]  # normalize //server -> server
        shares = args.shares.split(",") if args.shares else None
        disks = nascli.probe_nas(server, shares=shares, pattern=args.pattern, n=args.n)
        nascli.print_probe_topology(disks)
        return 0 if disks else 1

    print(f"[nas] unknown subcommand {args.nas_cmd!r}", file=sys.stderr)
    return 2


def _cmd_autostart(args) -> int:
    """Manage tray autostart. Two mechanisms:
      * run       — HKCU\\...\\Run. Logon-only; dies-stays-dead until next logon.
      * scheduled — Task Scheduler with restart-on-failure. Self-heals within
                    ~1 min after a crash. Recommended default.
    ``--mode auto`` picks scheduled on Windows and falls back to run if that
    path fails (unusual — Task Scheduler is available on every supported
    Windows). ``status`` prints whichever mechanism is registered (both, if
    both, so an operator can see + clean any stale duplicate)."""
    from . import autostart

    mode = getattr(args, "mode", "auto")

    if args.action == "on":
        if mode in ("scheduled", "auto"):
            outcome = autostart.ensure_scheduled()
            if outcome != "failed":
                print(f"[autostart] scheduled: {outcome} — tray runs at logon "
                      f"AND restarts within ~1 min if it dies.")
                return 0
            if mode == "scheduled":
                print("[autostart] scheduled: FAILED (schtasks error).", file=sys.stderr)
                return 1
            # auto -> fall through to Run-key
            print("[autostart] scheduled failed, falling back to HKCU\\Run.",
                  file=sys.stderr)
        outcome = autostart.ensure_registered()
        if outcome == "failed":
            print("[autostart] run: FAILED to write HKCU\\Run.", file=sys.stderr)
            return 1
        print(f"[autostart] run: {outcome} — tray will launch on login.")
        return 0

    if args.action == "off":
        outs = []
        if mode in ("scheduled", "auto"):
            outs.append(("scheduled", autostart.unregister_scheduled()))
        if mode in ("run", "auto"):
            outs.append(("run", autostart.unregister()))
        for tag, outcome in outs:
            print(f"[autostart] {tag}: {outcome}.")
        return 0

    # status — always show both so a stale HKCU\\Run entry doesn't hide behind
    # a newer scheduled task.
    reg = autostart.current_registration()
    sch = autostart.current_scheduled()
    if reg is None and sch is None:
        print("[autostart] not registered (tray won't auto-start on login).")
        return 0
    if sch:
        print(f"[autostart] scheduled: {sch} (restart-on-failure enabled)")
    if reg:
        print(f"[autostart] run: {reg}")
    return 0


def _cmd_install(args) -> int:
    """One-command setup: Coordinator as a Windows service + tray autostart.

    Mirrors at-field's ``atf install``: the heavy engine (Coordinator) becomes a
    boot-start LocalSystem service via NSSM; the tray (UI lens) is registered
    in HKCU\\Run to launch on login. After this + a reboot (or ``nssm start``),
    the mesh is always-on and the tray icon appears automatically.
    """
    import subprocess

    from . import autostart, winservice as ws
    from .appstate import logs_dir, state_dir

    if sys.platform != "win32":
        print("[install] Windows-only (NSSM service + HKCU autostart).", file=sys.stderr)
        return 2

    # 1. Tray autostart (user-mode, no elevation needed)
    if not args.no_tray:
        outcome = autostart.ensure_registered()
        if outcome == "registered":
            print("[install] tray autostart: registered (launches on login).")
        elif outcome == "updated":
            print("[install] tray autostart: updated (interpreter path changed).")
        elif outcome == "already":
            print("[install] tray autostart: already registered.")
        else:
            print("[install] tray autostart: FAILED — continuing with service install.",
                  file=sys.stderr)
    else:
        print("[install] skipping tray autostart (--no-tray).")

    # 2. Coordinator service (needs elevation)
    nssm = ws.find_nssm()
    if not nssm:
        print("[install] could not find nssm.exe.\n"
              "  Install NSSM (https://nssm.cc) and either put it on PATH, set "
              "KIROSHI_NSSM=<path to nssm.exe>, or drop it in "
              f"{state_dir()}\\nssm.exe", file=sys.stderr)
        return 2
    if not ws.is_admin():
        print("[install] WARNING: not elevated. Service install needs an Administrator "
              "shell; this will likely fail. Re-run from an elevated terminal.", file=sys.stderr)

    name = ws.DEFAULT_FIXER_SERVICE
    # If the service already exists, stop + remove it first so `nssm install`
    # succeeds cleanly (nssm install fails on an existing service). This makes
    # `kiroshi install` idempotent — re-running updates the config, like at-field.
    if not ws.status(name).endswith(": not installed"):
        print(f"[install] service '{name}' already installed — stopping + removing for clean reinstall...")
        ws.uninstall(ws.build_uninstall_commands(nssm, name))
    else:
        print(f"[install] registering Coordinator service '{name}'...")

    parts = ["-m", "kiroshi", "fixer", "--db", args.db,
             "--host", args.host, "--port", str(args.port)]
    if args.pages_dir:
        parts += ["--pages-dir", args.pages_dir]
    app_parameters = " ".join(_win_quote(p) for p in parts)
    cmds = ws.build_install_commands(
        nssm=nssm, service_name=name, python_exe=sys.executable,
        app_parameters=app_parameters, app_directory=str(state_dir()),
        log_dir=str(logs_dir()), display_name="Kiroshi Coordinator",
        description="Kiroshi coordinator (hands gigs to runners; serves the dashboard).",
        account="LocalSystem",
    )
    ok, out = ws.install(cmds)
    print(out)
    if ok:
        print(f"[install] Coordinator service installed. Start it with:  nssm start {name}")
        print("[install] Done. Next reboot: Coordinator auto-starts as a service, tray appears on login.")
    else:
        print(f"[install] service install FAILED for '{name}'.", file=sys.stderr)
    # Start the service immediately if we can
    if ok:
        try:
            subprocess.run([nssm, "start", name], timeout=15, capture_output=True)
            print(f"[install] started service '{name}'.")
        except Exception:  # noqa: BLE001
            print(f"[install] could not auto-start '{name}' — run: nssm start {name}")

    # 3. Firewall — best-effort, we already required admin above. Rules idempotent.
    if ok:
        try:
            from . import firewall as fw
            from .discovery import discovery_port

            subnet = fw.pick_lan_subnet() or "any"
            rules = fw.plan_rules(args.port, discovery_port(), remote_ip=subnet)
            if fw.is_admin():
                res = fw.apply_rules(rules)
                for n in res.removed:
                    print(f"[install] firewall: removed stale rule {n}")
                for n in res.added:
                    print(f"[install] firewall: opened {n}")
                for e in res.errors:
                    print(f"[install] firewall WARNING: {e}", file=sys.stderr)
                if res.ok:
                    print(f"[install] firewall: TCP {args.port} + UDP {discovery_port()} "
                          f"open for {subnet}.")
            else:
                print("[install] firewall: skipping (not elevated). "
                      "Run later:  kiroshi firewall install", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[install] firewall step failed (non-fatal): {e}", file=sys.stderr)
    return 0 if ok else 1


def _cmd_uninstall(args) -> int:
    """Remove the Coordinator service + tray autostart entry."""
    from . import autostart, winservice as ws

    rc = 0

    # 1. Tray autostart (user-mode)
    outcome = autostart.unregister()
    print(f"[uninstall] tray autostart: {outcome}.")

    # 2. Coordinator service (needs elevation)
    if sys.platform == "win32":
        nssm = ws.find_nssm()
        if nssm:
            name = ws.DEFAULT_FIXER_SERVICE
            ok, out = ws.uninstall(ws.build_uninstall_commands(nssm, name))
            print(out)
            print(f"[uninstall] service '{name}': {'removed' if ok else 'errors reported'}.")
            rc = 0 if ok else 1
        else:
            print("[uninstall] nssm.exe not found; skipping service removal.")
    return rc


def _cmd_doctor(args) -> int:
    from .doctor import run_doctor
    from .worker import _AUTO

    from . import security

    auto = (args.fixer or "").strip().lower() in _AUTO
    return run_doctor(
        task_ref=args.task,
        syspath=args.syspath,
        fixer_url=None if auto else args.fixer,
        auto=auto,
        read_root=args.read_root,
        write_root=args.write_root,
        token=security.resolve_token(args.token),
    )


if __name__ == "__main__":
    raise SystemExit(main())
