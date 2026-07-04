"""``kiroshi run`` — the front door (PLAN §7.5).

One command: enumerate inputs → start an in-process Fixer (loopback by default,
or ``0.0.0.0`` with ``--lan``) + a local Runner → seed → render a live terminal
progress bar (aggregate across every joined machine) → print where outputs landed
+ the dashboard URL. Ctrl-C drains and exits.

The coordinator here is *ephemeral* — it lives only for this run. For a permanent,
boot-start Fixer use ``kiroshi install``.
"""
from __future__ import annotations

import glob as _glob
import json
import os
import sys
import threading
import time
from typing import Any, Iterator, Optional

from .appstate import state_dir


def parse_task_args(tokens: list[str]) -> dict[str, Any]:
    """Parse the pass-through ``--`` tokens into a dict for ``enumerate_gigs``.

    ``--read-root //nas``     -> {"read_root": "//nas"}
    ``--fps 4 --fps 8``       -> {"fps": ["4", "8"]}   (repeated flag -> list)
    ``--dry-run``             -> {"dry_run": True}     (bare flag -> True)
    Values stay strings; the task coerces. Keys are de-hyphenated to snake_case.
    """
    args: dict[str, Any] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:].replace("-", "_")
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                val: Any = tokens[i + 1]
                i += 2
            else:
                val = True
                i += 1
            if key in args:
                if not isinstance(args[key], list):
                    args[key] = [args[key]]
                args[key].append(val)
            else:
                args[key] = val
        else:
            args.setdefault("_args", []).append(tok)
            i += 1
    return args


def _slug(s: str) -> str:
    import re

    out = "".join(c if c.isalnum() or c in "-_" else "-" for c in s)
    out = re.sub(r"-+", "-", out).strip("-_").lower()  # collapse runs of separators
    return out or "run"


def _read_jsonl(path: str) -> Iterator[dict[str, Any]]:
    import uuid

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            g = json.loads(line)
            g.setdefault("subjob_id", uuid.uuid4().hex)
            g.setdefault("spec", {})
            yield g


def _gigs_from_items(pattern: str) -> list[dict[str, Any]]:
    """One gig per file matching a local glob. spec = {'path': <match>}.

    Local filesystem only — for UNC/NAS enumeration use the task's
    ``enumerate_gigs`` hook (which can walk SMB via kfs). Deterministic subjob_ids
    (the path) keep re-runs idempotent.
    """
    matches = sorted(_glob.glob(pattern, recursive=True))
    return [
        {"subjob_id": m.replace("\\", "/"), "spec": {"path": m}}
        for m in matches
        if os.path.isfile(m)
    ]


def _fmt_eta(s: Optional[float]) -> str:
    if s is None:
        return "--"
    s = int(s)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{sec}s"
    return f"{sec}s"


def _render_bar(done: int, leased: int, total: int, barw: int = 28) -> str:
    """Three-state progress bar: ``#`` done, ``~`` in flight (leased), ``-`` pending.

    Moving as soon as work *starts* (leased) — not only when batches complete —
    avoids the "frozen at 0%" look when a Runner leases the whole queue at once
    (capacity >= total). The ``#`` region width matches the done percentage; the
    ``~`` region extends it with in-flight work.
    """
    dw = int(barw * done / total) if total else barw
    lw = int(barw * leased / total) if total else 0
    if dw + lw > barw:  # clamp against rounding drift / races
        lw = max(0, barw - dw)
    return "#" * dw + "~" * lw + "-" * (barw - dw - lw)


def _progress_loop(store, total: int, stop: threading.Event, is_tty: bool) -> None:
    """Render aggregate progress from the in-process store until all gigs settle.

    ASCII-only on purpose: the ``run`` output goes to a Windows console where the
    middle-dot/em-dash used elsewhere in Kiroshi garble under cp1252.
    """
    barw = 28
    last_line = 0.0
    while not stop.is_set():
        st = store.stats()
        done, failed = st["done"], st["failed"]
        pending, leased = st["pending"], st["leased"]
        rate, eta = st.get("rate_per_s", 0.0), st.get("eta_s")
        pct = (100.0 * done / total) if total else 100.0
        bar = _render_bar(done, leased, total, barw)
        hosts = len(st.get("per_host", []))
        # `#`=done `~`=in-flight `-`=queued ; pct is honest completion (done/total)
        msg = (f"[{bar}] {pct:5.1f}%  {done}/{total} done | {leased} in flight | "
               f"{rate:6.1f}/s | ETA {_fmt_eta(eta)} | {failed} failed")
        if hosts:
            msg += f" | {hosts} host{'s' if hosts != 1 else ''}"
        if is_tty:
            sys.stdout.write("\r" + msg + "   ")
            sys.stdout.flush()
        else:
            now = time.time()
            if now - last_line >= 5.0:
                print(msg, flush=True)
                last_line = now
        if total and pending == 0 and leased == 0:
            break
        stop.wait(0.5)
    if is_tty:
        sys.stdout.write("\n")
        sys.stdout.flush()


def run_job(
    task_ref: str,
    *,
    items: Optional[str] = None,
    jobs: Optional[str] = None,
    enumerate_: bool = False,
    task_args: Optional[list[str]] = None,
    job: Optional[str] = None,
    label: Optional[str] = None,
    workers: int = 0,
    capacity: int = 200,
    port: int = 8787,
    lan: bool = False,
    db: Optional[str] = None,
    token: Optional[str] = None,
    read_root: Optional[str] = None,
    write_root: Optional[str] = None,
    gig_timeout: Optional[float] = None,
    syspath: Optional[list[str]] = None,
    max_retries: int = 3,
    poll: float = 1.0,
    serve_task: bool = False,
    max_tasks_per_child: Optional[int] = None,
    gc_between_tasks: bool = False,
    launch_command: str = "",
    origin: Optional[dict[str, Any]] = None,
    force_second_fixer: bool = False,
) -> int:
    import uvicorn

    from . import security
    from .config import current_host
    from .coordinator import create_app
    from .discovery import BeaconBroadcaster, check_singleton_fixer
    from .jobstore import JobStore
    from .tasks import module_of, resolve_enumerator, resolve_task
    from .worker import Runner

    # Split-brain guard for `kiroshi run --lan`: refuse to broadcast a second
    # discoverable Fixer on a LAN that already has one. See cli._cmd_fixer for
    # the same guard on `kiroshi fixer`. When --lan is off we bind loopback and
    # don't beacon, so this check is skipped (no cross-host contention possible).
    if lan and not force_second_fixer:
        other = check_singleton_fixer(timeout=3.0)
        if other:
            print(f"[run] REFUSING --lan: another Fixer is already "
                  f"discoverable at {other}.\n"
                  f"  Seed to it instead:\n"
                  f"    kiroshi seed --fixer {other} ...\n"
                  f"  Or drop --lan to run a private loopback-only Fixer here.\n"
                  f"  (Pass --force-second-fixer only if you deliberately want\n"
                  f"  two isolated meshes on the same LAN.)",
                  file=sys.stderr)
            return 3

    # --- make the task's data roots visible to the (spawned) pool workers ---
    if read_root:
        os.environ["KIROSHI_READ_ROOT"] = read_root
    if write_root:
        os.environ["KIROSHI_WRITE_ROOT"] = write_root

    # --- apply --syspath to THIS process too, so the fail-fast task import
    # check below sees the same paths the spawned runner workers will. Without
    # this, `kiroshi run <task>` from a cwd that isn't the task's repo fails the
    # import validation even though the workers (which DO get extra_syspath)
    # would have imported it fine. ---
    if syspath:
        for p in syspath:
            if p and p not in sys.path:
                sys.path.insert(0, p)

    # --- fail fast: the task must import before we stand anything up ---
    try:
        resolve_task(task_ref)
    except Exception as e:  # noqa: BLE001
        print(f"[run] cannot import task {task_ref!r}: {e}", file=sys.stderr)
        return 2

    # --- opt-in task-code serving for `kiroshi join` (SECURITY.md §6.5) ---
    task_source = None
    if serve_task:
        from . import taskdist
        try:
            task_source = taskdist.read_task_source(task_ref)
        except ValueError as e:
            print(f"[run] --serve-task: {e}", file=sys.stderr)
            return 2
        if not lan:
            print("[run] note: --serve-task only matters with --lan (joiners need "
                  "the LAN bind to reach this Fixer).", flush=True)

    # --- build the gig list ---
    try:
        if jobs:
            gigs = list(_read_jsonl(jobs))
        elif enumerate_:
            fn = resolve_enumerator(task_ref)
            if fn is None:
                print(f"[run] --enumerate: module {module_of(task_ref)!r} has no "
                      f"enumerate_gigs(args) function.", file=sys.stderr)
                return 2
            gigs = list(fn(parse_task_args(task_args or [])))
        elif items:
            gigs = _gigs_from_items(items)
        else:
            print("[run] nothing to run: pass --items GLOB, --jobs FILE, or "
                  "--enumerate (with the task's enumerate_gigs).", file=sys.stderr)
            return 2
    except Exception as e:  # noqa: BLE001
        print(f"[run] enumeration failed: {e}", file=sys.stderr)
        return 2

    total = len(gigs)
    if total == 0:
        print("[run] no gigs produced — nothing to do.", file=sys.stderr)
        return 1

    # --- tag each gig with its physical disk (topology-aware leasing, PLAN §7.6) ---
    # A gig may already carry a disk (set by enumerate_gigs); only absent ones are
    # derived. No topology => disk stays None and leasing is inert (plain work-steal).
    from .storage import derive_disk, load_topology

    disks = load_topology()
    if disks:
        for g in gigs:
            if not g.get("disk"):
                d = derive_disk(g["subjob_id"], g.get("spec", {}), disks)
                if d:
                    g["disk"] = d

    # --- auth follows the bind, resolved CONSISTENTLY for both sides ---
    # --lan: ensure a token exists (generate+persist) so joiners have a join code.
    # loopback: use an explicit/env/file token if one exists, else no-auth. We must
    # resolve it the SAME way the Runner would, then hand the SAME value to both the
    # Fixer and the Runner — otherwise the Runner auto-resolves a persisted
    # mesh.token while the Fixer runs no-auth, and mutual auth refuses (the Fixer
    # "reports NO auth but this runner has a token").
    host = "0.0.0.0" if lan else "127.0.0.1"
    if lan:
        token = security.ensure_fixer_token(token)
    else:
        token = security.resolve_token(token)

    # --- persistent run DB (self-heal/resume survive a launcher restart) ---
    if not db:
        db = str(state_dir() / f"run-{_slug(job or task_ref)}.db")

    store = JobStore(db, max_retries=max_retries)
    inserted = store.seed(gigs, job=job, label=label)

    disp_host = current_host() if lan else "127.0.0.1"
    url = f"http://{disp_host}:{port}/"
    if token:
        url += f"?token={token}"

    print(f"[run] task={task_ref}  gigs={total} ({inserted} new)  db={db}", flush=True)
    if label:
        print(f"[run] campaign: {label}", flush=True)
    print(f"[run] dashboard: {url}", flush=True)
    if lan:
        print(f"[run] LAN mode — other machines can join with:  kiroshi join "
              f"(token: {token})", flush=True)
        if task_source:
            print(f"[run] serving task code to joiners: {task_source['filename']} "
                  f"(sha256 {task_source['sha256'][:12]}…) — joiners must approve it.",
                  flush=True)

    # A4: --force-second-fixer now requires KIROSHI_ALLOW_SECOND_COORDINATOR=1
    if force_second_fixer:
        if os.environ.get("KIROSHI_ALLOW_SECOND_COORDINATOR") != "1":
            print(
                "[run] REFUSING: --force-second-fixer now requires the "
                "environment variable KIROSHI_ALLOW_SECOND_COORDINATOR=1.\n"
                "  This two-key safety prevents accidentally running a second "
                "coordinator on the same machine.",
                file=sys.stderr,
            )
            return 3

    # Machine-level exclusive lock (catches same-box second coordinator even
    # when --no-beacon / loopback). The override path skips the lock.
    from .coordlock import acquire_or_refuse

    coord_lock = acquire_or_refuse(
        info={"port": port, "db": db, "host": host},
        allow_override=force_second_fixer,
    )
    if coord_lock is None:
        return 3

    app = create_app(store, token=token, launch_command=launch_command,
                     task_source=task_source, disks=disks)
    # M9: stash the origin so advisories fired against this campaign's spindle
    # can be attributed (and, if `callback` is present, webhook'd back to
    # whoever launched this run). Same in-memory shape as the /seed endpoint's
    # ``origins_by_group`` map — the seed already happened in-process above,
    # so we just record the origin here for the same job.
    if origin:
        from .jobstore import UNGROUPED

        grp = job or UNGROUPED
        app.state.origins_by_group.setdefault(grp, []).append(dict(origin))
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, name="kiroshi-run-fixer",
                                     daemon=True)
    server_thread.start()
    for _ in range(200):  # wait up to ~10s for uvicorn to bind
        if getattr(server, "started", False):
            break
        time.sleep(0.05)

    beacon = BeaconBroadcaster(fixer_port=port).start() if lan else None

    runner = Runner(
        fixer_url=f"http://127.0.0.1:{port}",
        task_ref=task_ref,
        workers=workers,
        capacity=capacity,
        token=token,
        poll_interval=poll,
        gig_timeout=gig_timeout,
        extra_syspath=syspath,
        quiet=True,
        launch_command=launch_command,
        max_tasks_per_child=max_tasks_per_child,
        gc_between_tasks=gc_between_tasks,
    )
    runner_thread = threading.Thread(target=runner.run, name="kiroshi-run-runner",
                                     daemon=True)
    runner_thread.start()

    stop = threading.Event()
    interrupted = False
    try:
        _progress_loop(store, total, stop, sys.stdout.isatty())
    except KeyboardInterrupt:
        interrupted = True
        print("\n[run] interrupt — draining current batch...", flush=True)
    finally:
        runner._draining = True
        stop.set()
        server.should_exit = True
        runner_thread.join(timeout=30)
        server_thread.join(timeout=10)
        if beacon is not None:
            beacon.stop()
        coord_lock.release()

    st = store.stats()
    store.close()
    print(f"[run] {'stopped' if interrupted else 'complete'}: "
          f"{st['done']}/{st['total']} done, {st['failed']} failed.", flush=True)
    if write_root:
        print(f"[run] outputs under: {write_root}", flush=True)
    if st["failed"]:
        print(f"[run] {st['failed']} gig(s) failed — re-run to retry, or "
              f"`kiroshi requeue --state failed` against the same db.", flush=True)
    return 1 if (interrupted or st["failed"]) else 0
