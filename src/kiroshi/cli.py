"""Kiroshi CLI — ``kiroshi fixer | runner | seed | status``.

Examples::

    # 1. Start the Fixer (coordinator + dashboard) on this box
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
                      help="Glob of input files; one gig per match (spec={'path':...}).")
    prun.add_argument("--jobs", default=None,
                      help="JSONL gig file (each line {job_id, spec}).")
    prun.add_argument("--enumerate", action="store_true",
                      help="Call the task module's enumerate_gigs(args) with the "
                           "args given after a literal '--'.")
    prun.add_argument("--group", default=None, help="Campaign slug (groups gigs in the UI).")
    prun.add_argument("--label", default=None,
                      help="Human-readable campaign name for the dashboard header.")
    prun.add_argument("--workers", type=int, default=0,
                      help="Local worker processes (default: CPU count).")
    prun.add_argument("--capacity", type=int, default=cfg.host().capacity)
    prun.add_argument("--port", type=int, default=cfg.fixer_port)
    prun.add_argument("--lan", action="store_true",
                      help="Bind 0.0.0.0 so other machines can join (generates a mesh token).")
    prun.add_argument("--db", default=None,
                      help="Run job-store path (default: state-dir/run-<slug>.db).")
    prun.add_argument("--token", default=None, help="Mesh token (for --lan).")
    prun.add_argument("--read-root", default=None, help="Set KIROSHI_READ_ROOT for the task.")
    prun.add_argument("--write-root", default=None, help="Set KIROSHI_WRITE_ROOT for the task.")
    prun.add_argument("--gig-timeout", type=float, default=None,
                      help="Seconds before a hung gig is abandoned + its worker killed.")
    prun.add_argument("--syspath", action="append", default=None,
                      help="Extra sys.path entries for task import (repeatable).")
    prun.add_argument("--max-retries", type=int, default=3)
    prun.add_argument("--max-tasks-per-child", type=int, default=None,
                      help="Recycle worker processes every N gigs (band-aid for C-level "
                           "leaks; off by default — prefer fixing the real accumulator).")
    prun.add_argument("--gc-between-tasks", action="store_true",
                      help="Run gc.collect() after every gig (defensive; off by default).")
    prun.add_argument("--serve-task", action="store_true",
                      help="Serve this (single-file, top-level) task's source to "
                           "joiners so `kiroshi join` needs no checkout. Opt-in + "
                           "consent-gated on the joiner — see SECURITY.md §6.5.")

    # ---- join (add this machine to a running mesh) ----
    pjoin = sub.add_parser(
        "join", help="Join this machine to a running mesh as a Runner.")
    pjoin.add_argument("--fixer", default="auto", help="Fixer URL or 'auto' (default).")
    pjoin.add_argument("--task", default=None,
                       help="Task 'module:function' (default: the Fixer's served task).")
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
    pjoin.add_argument("--gig-timeout", type=float, default=None,
                       help="Seconds before a hung gig is abandoned + its worker killed.")

    # ---- remote (launch/manage a Runner on another machine, quoting-proof) ----
    prem = sub.add_parser(
        "remote",
        help="Launch a Runner on another machine over SSH (interpreter-aware, "
             "durable, no shell-quoting pitfalls).")
    prem.add_argument("remote_cmd", choices=["probe", "join"],
                      help="probe: preflight only (report what's missing on the "
                           "remote). join: preflight + durable launch.")
    prem.add_argument("host", help="SSH host (alias in ~/.ssh/config or user@host). "
                                   "Matched to [hosts.<Host>] in kiroshi.local.toml.")
    prem.add_argument("--task", default=None, help="Task 'module:function' to run.")
    prem.add_argument("--fixer", default=None,
                      help="Fixer URL the remote should pull from "
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
    prem.add_argument("--group", default=None,
                      help="Campaign slug (names the launcher/log/task).")
    prem.add_argument("--task-name", default=None,
                      help="Scheduled Task name on the remote (default derived from --group).")
    prem.add_argument("--force", action="store_true",
                      help="Launch even if preflight reports problems.")
    prem.add_argument("--no-verify", action="store_true",
                      help="Skip waiting for the runner to appear in the fixer.")

    # ---- fixer ----
    pf = sub.add_parser("fixer", help="Run the coordinator (Fixer) + dashboard.")
    pf.add_argument("--db", default="kiroshi.db", help="SQLite job-store path (gitignored).")
    pf.add_argument("--host", default="127.0.0.1",
                    help="Bind address. Defaults to loopback (secure). Pass "
                         "0.0.0.0 to expose the mesh to the LAN (requires a token).")
    pf.add_argument("--port", type=int, default=cfg.fixer_port)
    pf.add_argument("--max-retries", type=int, default=3)
    pf.add_argument("--lease-ttl", type=float, default=120.0)
    pf.add_argument("--reap-interval", type=float, default=15.0)
    pf.add_argument("--no-beacon", action="store_true",
                    help="Disable the UDP discovery beacon (runners must use an explicit --fixer).")
    pf.add_argument("--token", default=None,
                    help="Mesh auth token (default: env KIROSHI_TOKEN, token file, or auto-generated).")
    pf.add_argument("--no-auth", action="store_true",
                    help="Run WITHOUT auth (trusted LAN / dev only). Strongly discouraged on 0.0.0.0.")
    pf.add_argument("--pages-dir", default=None,
                    help="Directory of custom *.html task views; served at /p, linked from dashboard.")

    # ---- runner ----
    pr = sub.add_parser("runner", help="Run a worker node (Runner).")
    pr.add_argument("--fixer", default=cfg.fixer_url,
                    help="Fixer base URL, or 'auto' to discover it on the LAN.")
    pr.add_argument("--task", required=True, help="Task as 'module:function'.")
    pr.add_argument("--workers", type=int, default=cfg.host().workers)
    pr.add_argument("--capacity", type=int, default=cfg.host().capacity)
    pr.add_argument("--id", default=None, help="Runner id (default: host-rand).")
    pr.add_argument("--host", default=current_host())
    pr.add_argument("--poll", type=float, default=2.0)
    pr.add_argument("--heartbeat", type=float, default=30.0)
    pr.add_argument("--retries", type=int, default=2, help="Per-item local retries.")
    pr.add_argument("--gig-timeout", type=float, default=None,
                    help="Seconds before a hung gig is abandoned + its worker killed.")
    pr.add_argument("--max-tasks-per-child", type=int, default=None,
                    help="Recycle worker processes every N gigs (band-aid for leaks; off by default).")
    pr.add_argument("--gc-between-tasks", action="store_true",
                    help="Run gc.collect() after every gig (defensive; off by default).")
    pr.add_argument("--syspath", action="append", default=None,
                    help="Extra sys.path entries for task import (repeatable).")
    pr.add_argument("--token", default=None,
                    help="Mesh auth token (default: env KIROSHI_TOKEN or token file).")

    # ---- seed ----
    ps = sub.add_parser("seed", help="Enqueue gigs into the Fixer.")
    ps.add_argument("--fixer", default=cfg.fixer_url, help="Fixer base URL, or 'auto'.")
    ps.add_argument("--jobs", default=None,
                    help="JSONL file; each line {\"job_id\":..., \"spec\":{...}}.")
    ps.add_argument("--demo", type=int, default=0, help="Seed N demo sleep gigs.")
    ps.add_argument("--batch", type=int, default=1000, help="POST batch size.")
    ps.add_argument("--group", default=None,
                    help="Campaign slug; all gigs are grouped under it in the dashboard "
                         "(overrides the job_id-prefix grouping).")
    ps.add_argument("--label", default=None,
                    help="Human-readable campaign name shown in the dashboard header "
                         "(e.g. 'Converting Seamless Interactions 30fps -> 4,8 fps'). "
                         "Pairs with --group (or a single shared group in --jobs).")
    ps.add_argument("--token", default=None, help="Mesh auth token.")

    # ---- status ----
    pt = sub.add_parser("status", help="Print a /status snapshot.")
    pt.add_argument("--fixer", default=cfg.fixer_url, help="Fixer base URL, or 'auto'.")
    pt.add_argument("--token", default=None, help="Mesh auth token.")

    # ---- requeue ----
    pq = sub.add_parser("requeue", help="Return failed/stuck gigs to pending.")
    pq.add_argument("--fixer", default=cfg.fixer_url, help="Fixer base URL, or 'auto'.")
    pq.add_argument("--state", action="append", choices=["failed", "leased", "done"],
                    help="Gig state(s) to requeue (repeatable; default: failed).")
    pq.add_argument("--keep-attempts", action="store_true",
                    help="Don't reset the attempt counter (default: reset to 0).")
    pq.add_argument("--token", default=None, help="Mesh auth token.")

    # ---- doctor ----
    pd = sub.add_parser("doctor", help="Preflight checks for this machine + env.")
    pd.add_argument("--fixer", default=cfg.fixer_url, help="Fixer base URL, or 'auto'.")
    pd.add_argument("--task", default=None, help="Task 'module:function' to import-test.")
    pd.add_argument("--syspath", action="append", default=None,
                    help="Extra sys.path entries for the task import (repeatable).")
    pd.add_argument("--read-root", default=None, help="Override KIROSHI_READ_ROOT.")
    pd.add_argument("--write-root", default=None, help="Override KIROSHI_WRITE_ROOT.")
    pd.add_argument("--token", default=None, help="Mesh auth token.")

    # ---- ps (list registered kiroshi processes) ----
    pp = sub.add_parser("ps", help="List locally-registered Kiroshi processes.")
    pp.add_argument("--json", action="store_true", help="Emit raw JSON.")

    # ---- stop (request graceful drain of a registered process) ----
    pstop = sub.add_parser("stop", help="Ask a registered Fixer/Runner to drain + exit.")
    pstop.add_argument("--role", choices=["fixer", "runner"], help="Limit to a role.")
    pstop.add_argument("--pid", type=int, default=None, help="Limit to one PID.")
    pstop.add_argument("--all", action="store_true", help="Stop all registered processes.")

    # ---- tray ----
    ptray = sub.add_parser("tray", help="Run the system-tray UI (needs the 'tray' extra).")
    ptray.add_argument("--fixer", default=cfg.fixer_url, help="Fixer base URL, or 'auto'.")
    ptray.add_argument("--token", default=None, help="Mesh auth token.")

    # ---- install (one-command setup: fixer service + tray autostart) ----
    pins = sub.add_parser("install",
                          help="One-command setup: install the Fixer as a Windows service "
                               "+ register the tray to auto-start on login.")
    pins.add_argument("--db", default="kiroshi.db", help="(fixer) SQLite job-store path.")
    pins.add_argument("--host", default="0.0.0.0", help="(fixer) bind host (LAN-default).")
    pins.add_argument("--port", type=int, default=cfg.fixer_port, help="(fixer) bind port.")
    pins.add_argument("--pages-dir", default=None, help="(fixer) custom views dir.")
    pins.add_argument("--no-tray", action="store_true",
                      help="Skip tray autostart registration (service only).")

    # ---- uninstall (remove the service + tray autostart) ----
    sub.add_parser("uninstall",
                   help="Remove the Kiroshi Fixer service + tray autostart entry.")

    # ---- autostart (manage just the tray login-autostart) ----
    pau = sub.add_parser("autostart",
                         help="Manage tray auto-start on login (HKCU\\Run).")
    pau.add_argument("action", choices=["on", "off", "status"],
                     help="on=register, off=unregister, status=show current.")

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
                          help="Install/uninstall/inspect Fixer or Runner as a Windows service (NSSM).")
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
    psvc.add_argument("--fixer", default="auto", help="(runner) Fixer URL or 'auto'.")
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
    if args.cmd == "install":
        return _cmd_install(args)
    if args.cmd == "uninstall":
        return _cmd_uninstall(args)
    if args.cmd == "autostart":
        return _cmd_autostart(args)
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
        raise SystemExit("No fixer beacon heard. Is a Fixer running? "
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

    store = JobStore(args.db, max_retries=args.max_retries)
    from .storage import load_topology

    app = create_app(
        store,
        lease_ttl=args.lease_ttl,
        reap_interval=args.reap_interval,
        pages_dir=args.pages_dir,
        token=token,
        disks=load_topology(),
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
        group=args.group,
        label=args.label,
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


def _cmd_seed(args) -> int:
    import requests

    args.fixer = _resolve_fixer_arg(args.fixer)
    headers = _auth_headers(args)

    def post(gigs: list[dict]) -> None:
        body: dict = {"gigs": gigs}
        if args.group:
            body["group"] = args.group
        if args.label:
            body["label"] = args.label
        r = requests.post(f"{args.fixer.rstrip('/')}/seed", json=body,
                          timeout=60, headers=headers)
        r.raise_for_status()
        out = r.json()
        print(f"  seeded {out['inserted']}/{out['received']}", flush=True)

    buf: list[dict] = []
    total = 0
    if args.demo > 0:
        for i in range(args.demo):
            buf.append({"job_id": f"demo-{i:06d}", "spec": {"seconds": 0.05}})
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
                g.setdefault("job_id", uuid.uuid4().hex)
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
    print(f"[seed] submitted {total} gig(s).", flush=True)
    return 0


def _cmd_status(args) -> int:
    import requests

    args.fixer = _resolve_fixer_arg(args.fixer)
    r = requests.get(f"{args.fixer.rstrip('/')}/status", timeout=30,
                     headers=_auth_headers(args))
    r.raise_for_status()
    print(json.dumps(r.json(), indent=2))
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
    print(f"[requeue] {n} gig(s) ({', '.join(states)}) -> pending", flush=True)
    return 0


def _cmd_ps(args) -> int:
    from .processreg import list_registered

    procs = list_registered()
    if args.json:
        print(json.dumps(procs, indent=2))
        return 0
    if not procs:
        print("no registered Kiroshi processes on this machine.")
        return 0
    print(f"{'ROLE':<7} {'PID':>7}  {'HOST':<16} LAUNCH COMMAND")
    for p in procs:
        print(f"{p.get('role',''):<7} {p.get('pid',''):>7}  {p.get('host',''):<16} "
              f"{p.get('launch_command','')}")
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
        display = "Kiroshi Fixer"
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
        parts = ["-m", "kiroshi", "runner", "--fixer", args.fixer, "--task", args.task]
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
    from . import autostart

    if args.action == "on":
        outcome = autostart.ensure_registered()
        if outcome == "failed":
            print("[autostart] FAILED to write HKCU\\Run (registry error).", file=sys.stderr)
            return 1
        print(f"[autostart] {outcome} — tray will launch on login.")
        return 0
    if args.action == "off":
        outcome = autostart.unregister()
        print(f"[autostart] {outcome}.")
        return 0
    # status
    reg = autostart.current_registration()
    if reg:
        print(f"[autostart] registered: {reg}")
    else:
        print("[autostart] not registered (tray won't auto-start on login).")
    return 0


def _cmd_install(args) -> int:
    """One-command setup: Fixer as a Windows service + tray autostart.

    Mirrors at-field's ``atf install``: the heavy engine (Fixer) becomes a
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

    # 2. Fixer service (needs elevation)
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
        print(f"[install] registering Fixer service '{name}'...")

    parts = ["-m", "kiroshi", "fixer", "--db", args.db,
             "--host", args.host, "--port", str(args.port)]
    if args.pages_dir:
        parts += ["--pages-dir", args.pages_dir]
    app_parameters = " ".join(_win_quote(p) for p in parts)
    cmds = ws.build_install_commands(
        nssm=nssm, service_name=name, python_exe=sys.executable,
        app_parameters=app_parameters, app_directory=str(state_dir()),
        log_dir=str(logs_dir()), display_name="Kiroshi Fixer",
        description="Kiroshi coordinator (hands gigs to runners; serves the dashboard).",
        account="LocalSystem",
    )
    ok, out = ws.install(cmds)
    print(out)
    if ok:
        print(f"[install] Fixer service installed. Start it with:  nssm start {name}")
        print("[install] Done. Next reboot: Fixer auto-starts as a service, tray appears on login.")
    else:
        print(f"[install] service install FAILED for '{name}'.", file=sys.stderr)
    # Start the service immediately if we can
    if ok:
        try:
            subprocess.run([nssm, "start", name], timeout=15, capture_output=True)
            print(f"[install] started service '{name}'.")
        except Exception:  # noqa: BLE001
            print(f"[install] could not auto-start '{name}' — run: nssm start {name}")
    return 0 if ok else 1


def _cmd_uninstall(args) -> int:
    """Remove the Fixer service + tray autostart entry."""
    from . import autostart, winservice as ws

    rc = 0

    # 1. Tray autostart (user-mode)
    outcome = autostart.unregister()
    print(f"[uninstall] tray autostart: {outcome}.")

    # 2. Fixer service (needs elevation)
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
