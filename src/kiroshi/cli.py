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
    cfg = load_config()
    parser = argparse.ArgumentParser(prog="kiroshi", description="Work-stealing mesh runner.")
    parser.add_argument("--version", action="version", version=f"kiroshi {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

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

    args = parser.parse_args(argv)

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
    app = create_app(
        store,
        lease_ttl=args.lease_ttl,
        reap_interval=args.reap_interval,
        pages_dir=args.pages_dir,
        token=token,
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
        r = requests.post(f"{args.fixer.rstrip('/')}/seed", json={"gigs": gigs},
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
    import shlex

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

    app_parameters = " ".join(shlex.quote(p) for p in parts)
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
