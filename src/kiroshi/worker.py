"""The Runner — worker node that pulls gigs and executes them locally.

Pull loop: lease a batch -> run it on a :class:`~kiroshi.pool.LocalPool` -> report
results -> repeat. All the within-node robustness (process pool, bounded window,
per-sub-job timeout, broken-pool recovery, PYTHONPATH propagation) lives in
``LocalPool``; the Runner is just the HTTP coordination + lifecycle around it.

Graceful drain on Ctrl-C / SIGTERM: finish + report the current batch, then exit.
"""
from __future__ import annotations

import os
import re
import signal
import socket
import sys
import threading
import time
import uuid
from typing import Any, Optional

import requests

from . import security
from .discovery import discover_coordinator
from .pool import LocalPool
from .failure_breaker import FailureBreaker
from .worker_tuner import WorkerTuner

# How often the Phase 6 clean-tree gate re-checks a dirty working tree.
_DIRTY_RECHECK_S = 15.0

# Sentinel values (any of these, or an empty url) trigger zero-config discovery.
_AUTO = {"auto", "discover", "", "auto://", "http://auto"}


def verify_coordinator(url: str, token: Optional[str], timeout: float = 30.0) -> bool:
    """Authenticate the *Coordinator* via the HMAC challenge before trusting it.

    Standalone form of :meth:`Runner._verify_coordinator` so ``kiroshi join`` can verify
    a Coordinator *before* sending the token or fetching task code. Sends a random nonce
    with NO Authorization header; only a Coordinator holding the mesh token can return
    ``HMAC(token, nonce)``. With no token, trusts only a Coordinator that declares
    ``auth: false`` (a deliberately open dev mesh). Fails closed.
    """
    nonce = security.new_nonce()
    try:
        r = requests.get(f"{url.rstrip('/')}/auth/challenge",
                         params={"nonce": nonce}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return False
    if not token:
        return data.get("auth") is False
    if not data.get("auth"):
        return False
    return security.verify_proof(token, nonce, data.get("proof"))


class Runner:
    def __init__(
        self,
        coordinator_url: str,
        task_ref: str,
        workers: int = 0,
        capacity: int = 0,
        runner_id: Optional[str] = None,
        host: Optional[str] = None,
        poll_interval: float = 2.0,
        heartbeat_interval: float = 30.0,
        item_retries: int = 2,
        item_backoff: float = 0.5,
        gig_timeout: Optional[float] = None,
        extra_syspath: Optional[list[str]] = None,
        http_timeout: float = 30.0,
        discover_timeout: float = 6.0,
        rediscover_after: int = 3,
        token: Optional[str] = None,
        launch_command: str = "",
        quiet: bool = False,
        max_tasks_per_child: Optional[int] = None,
        gc_between_tasks: bool = False,
        job: Optional[str] = None,
    ):
        # quiet suppresses the routine per-batch / startup prints so an
        # in-process `kiroshi run` can render a clean progress bar. Errors and
        # security warnings are always printed.
        self.quiet = quiet
        self._auto = (coordinator_url or "").strip().lower() in _AUTO
        self.coordinator_url = "" if self._auto else coordinator_url.rstrip("/")
        self.token = token if token is not None else security.resolve_token()
        self.launch_command = launch_command
        self._registered = False
        self._warned_open = False  # print the CIRCUIT OPEN message once per episode
        self._verified_url: Optional[str] = None  # last Coordinator that passed the auth challenge
        self.task_ref = task_ref
        # Job-scoped leasing: this Runner only leases sub-jobs of ``self.job`` so
        # a single coordinator can host many jobs. None => lease any job (legacy).
        self.job = job
        self.workers = workers or (os.cpu_count() or 4)
        # capacity <= 0 is the "auto" sentinel: size it to workers + buffer so a
        # runner keeps every core fed without hoarding the disk budget. This
        # matches HostConfig's auto (workers + CAPACITY_BUFFER); previously the
        # constructor hardcoded 100, so `join`/`remote join` (which don't pass a
        # capacity) over-leased vs a tuned `kiroshi runner`.
        from .config import CAPACITY_BUFFER
        self.capacity = capacity if capacity and capacity > 0 \
            else self.workers + CAPACITY_BUFFER
        self.runner_id = runner_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.host = host or socket.gethostname()
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.item_retries = item_retries
        self.item_backoff = item_backoff
        self.gig_timeout = gig_timeout
        self._last_progress_at = time.time()  # re-armed in run(); see _start_watchdog
        self.max_tasks_per_child = max_tasks_per_child
        self.gc_between_tasks = gc_between_tasks
        self.http_timeout = http_timeout
        self.discover_timeout = discover_timeout
        self.rediscover_after = rediscover_after
        self._fails = 0  # consecutive transport failures
        self._draining = False

        sp = list(extra_syspath or [])
        cwd = os.getcwd()
        if cwd not in sp:
            sp.append(cwd)
        self.extra_syspath = sp

    # --------------------------------------------------------- discovery
    def _resolve_coordinator(self, *, blocking: bool = True) -> Optional[str]:
        """Ensure ``self.coordinator_url`` points at a live Coordinator.

        In auto mode this listens for a discovery beacon; with a fixed URL it's a
        no-op. When ``blocking`` it retries (with backoff) until a Coordinator appears
        or the runner is told to drain — so a runner can be started before the
        Coordinator, or survive the Coordinator moving to a new IP.
        """
        if not self._auto:
            return self.coordinator_url
        backoff = 1.0
        while not self._draining:
            url = discover_coordinator(timeout=self.discover_timeout)
            if url:
                if url != self.coordinator_url:
                    print(f"[runner] discovered coordinator at {url}", flush=True)
                self.coordinator_url = url
                self._fails = 0
                return url
            if not blocking:
                return None
            print(
                f"[runner] no coordinator beacon yet; retrying in {backoff:.0f}s "
                f"(is the Coordinator running?)",
                flush=True,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 15.0)
        return None

    # ------------------------------------------------------- mutual auth
    def _verify_coordinator(self, url: str) -> bool:
        """Authenticate the *Coordinator* before trusting it. The Runner sends a random
        nonce (with NO Authorization header) and requires the Coordinator to return
        HMAC(token, nonce); only a Coordinator holding the same mesh token can. This
        runs BEFORE we ever send our bearer token or execute a leased sub-job, so a
        rogue Coordinator that wins `--fixer auto` discovery can neither harvest the
        token nor inject specs. Fails closed (un-verifiable Coordinator => not trusted).
        """
        nonce = security.new_nonce()
        try:
            r = requests.get(f"{url}/auth/challenge", params={"nonce": nonce},
                             timeout=self.http_timeout)  # deliberately unauthenticated
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return False
        if not self.token:
            # We hold no token; only trust a Coordinator that also declares no auth
            # (a deliberately open dev mesh on a trusted LAN).
            return data.get("auth") is False
        if not data.get("auth"):
            print("[runner] SECURITY: Coordinator reports NO auth but this runner has a "
                  "token — refusing (possible rogue or misconfigured Coordinator).",
                  flush=True)
            return False
        if not security.verify_proof(self.token, nonce, data.get("proof")):
            print("[runner] SECURITY: Coordinator failed the token challenge — refusing "
                  "to send credentials or run work (rogue Coordinator / wrong token).",
                  flush=True)
            return False
        return True

    def _bootstrap_nas_creds(self, server: str = "default") -> None:
        """Fetch the NAS SMB credential from the Coordinator and inject it into
        THIS process's env, so ``smbprotocol`` authenticates directly in every
        logon context (service / SSH / scheduled / interactive) and pool workers
        spawned after this inherit it. Never written to disk.

        Runs before the worker pool is created. Skips silently if creds are
        already in env (operator override), no token is configured (open mesh),
        or the Coordinator has none stored. The Coordinator is verified first
        (mutual auth), and the fetch proves token possession via HMAC without
        transmitting the token; the reply is sealed under a token+nonce key."""
        if os.environ.get("KIROSHI_NAS_USER") and os.environ.get("KIROSHI_NAS_PASS"):
            return
        if not self.token:
            return
        if not self.coordinator_url or not self._verify_coordinator(self.coordinator_url):
            return
        from . import nascred
        nonce = security.new_nonce()  # 32 hex chars
        try:
            r = requests.get(
                f"{self.coordinator_url}/mesh/nas-cred",
                params={"server": server, "nonce": nonce},
                headers={"X-Kiroshi-Cred-Proof":
                         nascred.cred_proof(self.token, nonce, server)},
                timeout=self.http_timeout,
            )
            if r.status_code == 404:
                return  # coordinator has no stored cred — env/keyring will be tried
            r.raise_for_status()
            sealed = (r.json() or {}).get("sealed")
        except (requests.RequestException, ValueError):
            print("[runner] NAS credential broker unreachable; falling back to "
                  "env/keyring for SMB auth.", flush=True)
            return
        payload = nascred.unseal(self.token, nonce, sealed) if sealed else None
        if not payload or b"\n" not in payload:
            print("[runner] SECURITY: NAS credential seal failed to open "
                  "(tamper or token mismatch) — not using it.", flush=True)
            return
        user, pw = payload.decode("utf-8").split("\n", 1)
        os.environ["KIROSHI_NAS_USER"] = user
        os.environ["KIROSHI_NAS_PASS"] = pw
        # SMB3 payload encryption on the data plane (confidentiality + integrity);
        # operator can override by pre-setting KIROSHI_SMB_ENCRYPT.
        os.environ.setdefault("KIROSHI_SMB_ENCRYPT", "1")
        if not self.quiet:
            print(f"[runner] NAS credential provisioned from coordinator "
                  f"(user={user!r}, SMB3 encryption on); nothing persisted.",
                  flush=True)

    def _trusted(self) -> bool:
        """True iff the current Coordinator URL has passed (and still passes) the auth
        challenge. Caches the last verified URL so we challenge once per connect."""
        if not self.coordinator_url:
            return False
        if self._verified_url == self.coordinator_url:
            return True
        if self._verify_coordinator(self.coordinator_url):
            self._verified_url = self.coordinator_url
            return True
        self._verified_url = None
        return False

    # --------------------------------------------------------------- http
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _post(self, path: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not self.coordinator_url:
            self._resolve_coordinator()
        url = f"{self.coordinator_url}{path}"
        for attempt in range(3):
            try:
                r = requests.post(url, json=payload, timeout=self.http_timeout,
                                  headers=self._headers())
                r.raise_for_status()
                self._fails = 0
                return r.json()
            except requests.RequestException as e:
                if attempt == 2:
                    print(f"[runner] POST {path} failed: {e}", flush=True)
                    self._on_transport_failure()
                    return None
                time.sleep(1.0 * (attempt + 1))
        return None

    def _on_transport_failure(self) -> None:
        """After repeated failures, assume the Coordinator moved and re-discover."""
        self._fails += 1
        if self._auto and self._fails >= self.rediscover_after:
            print("[runner] lost contact with coordinator; re-discovering...", flush=True)
            self.coordinator_url = ""
            self._registered = False
            self._verified_url = None
            self._resolve_coordinator(blocking=False)

    def _register(self) -> None:
        """Announce our launch command + identity so the Coordinator can surface it on
        the dashboard/history (and so jobs can be traced to the exact command)."""
        from .codefinger import fingerprint_repos
        from .hostsample import sample_host
        from .logsetup import current_log_path

        ok = self._post("/register", {
            "runner_id": self.runner_id,
            "host": self.host,
            "launch_command": self.launch_command,
            "task": self.task_ref,
            "workers": self.workers,
            "pid": os.getpid(),
            "log_path": current_log_path(),
            "job": self.job,
            "code_fingerprint": fingerprint_repos(self.extra_syspath),
            "resources": sample_host(root_pid=os.getpid()),
        })
        self._registered = bool(ok)

    # ----------------------------------------------------------- tuning
    def _tuner_cache_path(self):
        # Machine-local (appstate.state_dir() is per-machine), so keying by
        # task_ref alone is enough -- no need to also fold in the host.
        from . import appstate
        from pathlib import Path
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.task_ref)
        return Path(appstate.state_dir()) / "worker_tuning" / f"{safe}.json"

    # ------------------------------------------------------ reproducibility gate
    def _closure_problems(self) -> list[str]:
        """Uncommitted files in the task's import closure -- the code that will
        ACTUALLY run (the task module + the local modules it transitively
        imports, in the task repo AND the kiroshi framework). NOT a whole-repo
        check: an unrelated edit elsewhere in a research monorepo does not
        block, but an UNTRACKED local module the task imports does. See
        codefinger.dirty_import_closure."""
        from .codefinger import dirty_import_closure
        import kiroshi as _kir
        task_module = self.task_ref.split(":", 1)[0]
        # FIRST-PARTY roots only: the task's own import roots plus the kiroshi
        # source root. Deliberately NOT the full sys.path -- that includes
        # site-packages/stdlib, and walking those would (a) explode the closure
        # into every third-party dep and (b) flag vendored files (a .venv can
        # even live inside a repo) as "untracked". Third-party deps are pinned
        # by the environment, not the git tree, so a module that only resolves
        # under site-packages simply drops out of the closure.
        kiroshi_src = os.path.dirname(os.path.dirname(os.path.abspath(_kir.__file__)))
        search_roots = list(self.extra_syspath) + [kiroshi_src]
        return dirty_import_closure(task_module, search_roots)

    def _await_clean_tree(self) -> None:
        """Phase 6 reproducibility gate: refuse to run uncommitted code.

        A runner must execute committed code, or the mesh silently runs
        whatever local edits happen to be on this box -- exactly the drift
        that made "which code is actually running?" unanswerable during the
        held-frames campaign. On a dirty closure we deliberately do NOT exit: a
        silent process death under Task Scheduler is its own invisible-failure
        trap (a runner that "just isn't there"). Instead we BLOCK, log loudly,
        and re-check -- so the runner stays visibly alive and self-heals the
        moment the operator commits.

        Scope is the task's IMPORT CLOSURE, not the whole repo: only files the
        running code actually imports gate the launch (untracked-module-in-the-
        path fails; an unrelated committed-or-not sibling does not). No
        override, by design -- commit is the way forward.
        """
        warned = False
        while not self._draining:
            problems = self._closure_problems()
            if not problems:
                if warned and not self.quiet:
                    print("[runner] import closure clean now -- proceeding.", flush=True)
                return
            # Always print (even under --quiet): a blocked runner producing no
            # work must never be silent, or it looks identical to a crash.
            print(
                f"[runner] REFUSING to start: task import closure has "
                f"uncommitted code: {', '.join(problems)}. Runs must be "
                f"reproducible -- commit + push so every runner executes known "
                f"code. Blocking until clean (re-checking every "
                f"{_DIRTY_RECHECK_S:.0f}s; commit to proceed).",
                file=sys.stderr, flush=True,
            )
            warned = True
            time.sleep(_DIRTY_RECHECK_S)

    # ------------------------------------------------------ NAS credential gate
    def _nas_servers_from_topology(self) -> list[str]:
        """Distinct UNC server hosts in the coordinator's storage topology
        (read/write/direct roots). Empty on any error -- never block on our own
        inability to introspect."""
        from . import kfs
        try:
            r = requests.get(f"{self.coordinator_url.rstrip('/')}/storage",
                             headers=self._headers(), timeout=self.http_timeout)
            r.raise_for_status()
            disks = (r.json() or {}).get("disks", [])
        except (requests.RequestException, ValueError):
            return []
        servers: set[str] = set()
        for d in disks:
            for key in ("read", "write", "direct_path"):
                srv = kfs.server_of(d.get(key) or "")
                if srv:
                    servers.add(srv)
        return sorted(servers)

    def _nas_auth_preflight(self) -> None:
        """Credential analogue of the clean-tree gate: refuse to start (loudly,
        self-healing) if the NAS credential we hold is REJECTED by the server.

        A wrong password is permanent -- leasing-and-retrying it is exactly what
        DoS'd the NAS on 2026-07-21 (thousands of failed SMB3 auths). So we probe
        the servers the topology actually uses BEFORE leasing, and an
        ``auth_rejected`` blocks until fixed. A transient ``unreachable`` does NOT
        block (the NAS may just be briefly down -- that's the leasing loop's
        concern, not a credential fault). Self-heals the moment
        ``kiroshi nas-cred rotate`` corrects the stored credential."""
        from . import kfs
        servers = self._nas_servers_from_topology()
        if not servers:
            return
        warned = False
        while not self._draining:
            rejected = [s for s in servers if kfs.smb_auth_probe(s) == "auth_rejected"]
            if not rejected:
                if warned and not self.quiet:
                    print("[runner] NAS credential accepted now -- proceeding.", flush=True)
                return
            print(
                f"[runner] REFUSING to start: NAS credential REJECTED by "
                f"{', '.join(rejected)} (LOGON_FAILURE) -- the stored password is "
                f"wrong/stale. Fix it on the coordinator with "
                f"`kiroshi nas-cred rotate --user <user> --ssh-target <nas>`. Not "
                f"leasing (re-checking every {_DIRTY_RECHECK_S:.0f}s).",
                file=sys.stderr, flush=True,
            )
            warned = True
            time.sleep(_DIRTY_RECHECK_S)

    # ---------------------------------------------------------- watchdog
    def _start_watchdog(self) -> None:
        """Hard-exit the process if the lease/run_batch loop stops making
        forward progress for too long -- the backstop for when run_batch's OWN
        in-process ``--subjob-timeout`` enforcement is itself defeated.

        Observed 2026-07-22: a worker crashed at the native/CUDA level (an
        NVDEC decode-error storm on a corrupted clip) in a way that apparently
        corrupted the ProcessPoolExecutor's internal bookkeeping -- `wait()`
        stopped honoring its timeout, so the per-sub-job timeout check
        (pool.py's own safety net) never got to run. The runner burned
        GPU/CPU producing nothing for 14+ minutes with no way to recover
        itself; only an external restart fixed it.

        Deliberately a HARD exit (``os._exit``), not a graceful shutdown: by
        definition we don't trust a wedged process to unwind cleanly (that's
        the same failure mode that already defeated the in-process safety
        net). A fresh process -- relaunched by whatever supervises this one
        (Task Scheduler, a service manager) -- is simpler and safer than
        trying to salvage unknown internal state.

        Only armed when ``--subjob-timeout`` (``gig_timeout``) is set --
        with no configured per-item ceiling there's nothing principled to
        compare a "no progress" gap against. ``self._last_progress_at`` is
        bumped by the runner's own heartbeat callback (fires from *inside*
        run_batch's loop while it's genuinely cycling) and once per finished
        batch -- so a single long-but-healthy batch doesn't false-trip this,
        only a loop that has actually stopped advancing.
        """
        if not self.gig_timeout:
            return
        ceiling = self._watchdog_ceiling()
        check_interval = self._watchdog_check_interval(ceiling)

        def _watch() -> None:
            while not self._draining:
                time.sleep(check_interval)
                if self._watchdog_should_exit(time.time()):
                    gap = time.time() - self._last_progress_at
                    print(
                        f"[runner] WATCHDOG: no progress in {gap:.0f}s (ceiling "
                        f"{ceiling:.0f}s = 2x --subjob-timeout {self.gig_timeout:.0f}s). "
                        f"The lease/run_batch loop appears wedged past its own "
                        f"per-sub-job timeout -- hard-exiting so the supervisor "
                        f"relaunches a clean process.",
                        file=sys.stderr, flush=True,
                    )
                    os._exit(1)

        threading.Thread(target=_watch, name="kiroshi-watchdog", daemon=True).start()

    def _watchdog_ceiling(self) -> float:
        return 2.0 * self.gig_timeout

    @staticmethod
    def _watchdog_check_interval(ceiling: float) -> float:
        return min(30.0, max(5.0, ceiling / 4))

    def _watchdog_should_exit(self, now: float) -> bool:
        """Pure decision (given ``now`` and the runner's own state) -- no
        thread/sleep/os._exit involved, so this is directly unit-testable."""
        if not self.gig_timeout:
            return False
        return (now - self._last_progress_at) > self._watchdog_ceiling()

    # --------------------------------------------------------------- loop
    def run(self) -> None:
        self._install_signal_handlers()
        # Bind a process-tree-reap mechanism (Windows Job Object / POSIX setsid)
        # so that if the runner is force-killed or crashes, the OS automatically
        # reaps all spawned pool workers — no orphaned processes holding wrapper
        # pipe handles, no stuck auto-restart loops. Must be before pool creation
        # so spawned workers inherit the Job Object membership.
        from .proctree import bind_job_object
        bind_job_object()
        # Phase 6: block here until the working tree has no real uncommitted
        # changes -- before we resolve a coordinator, register, or spawn any
        # worker. Self-healing (proceeds on commit); never exits silently.
        self._await_clean_tree()
        if self._draining:
            return
        self._resolve_coordinator()  # block until a Coordinator is known (auto mode)
        # Provision NAS creds into env BEFORE the pool spawns, so every worker
        # inherits them and smbprotocol authenticates directly in any logon type.
        self._bootstrap_nas_creds()
        # Credential preflight: if the NAS rejects our stored credential, block
        # here (loud, self-healing) instead of leasing gigs that would each fail
        # LOGON_FAILURE and hammer the NAS. Skips cleanly when no NAS/creds apply.
        self._nas_auth_preflight()
        if self._draining:
            return

        # Arm the wedge-detection watchdog only now -- after the intentional,
        # potentially-long blocking gates above (clean-tree, NAS preflight),
        # which are legitimate waits-for-operator-action, not bugs. It must
        # only ever supervise the lease/run_batch work loop below.
        self._last_progress_at = time.time()
        self._start_watchdog()

        # Startup orphan sweep: reclaim per-sub-job capture/marker files a
        # PREVIOUS process left behind with no chance to clean up itself --
        # a hard-killed worker (taskkill /F /T) or this runner's own watchdog
        # os._exit both skip every finally/__exit__, so nothing but a fresh
        # process's startup sweep ever reclaims them. Sized from gig_timeout
        # when set (a stale marker outlives its own subjob-timeout many times
        # over); best-effort, never fatal.
        try:
            from . import subjob_capture
            sweep_age = max(1800.0, 2.0 * self.gig_timeout) if self.gig_timeout else 1800.0
            subjob_capture.sweep_stale(sweep_age)
        except Exception:  # noqa: BLE001
            pass

        # Phase 3 adaptive ramping: the operator's --workers becomes the
        # CEILING the tuner will grow to, not a fixed value. One synchronous
        # headroom probe up front decides the starting size -- if AT-Field
        # isn't reachable, the tuner disables itself for the run and we start
        # at the full requested count (today's behavior, unchanged). If it
        # IS reachable, we deliberately start low (from the tuner's own
        # floor/cache logic) rather than slamming the machine immediately.
        tuner = WorkerTuner(floor=1, ceiling=self.workers, cache_path=self._tuner_cache_path())
        tuner.step(tuner.poll_headroom(), time.time())
        initial_workers = tuner.target if tuner.enabled else self.workers

        # Phase 9: error-class-aware backpressure. Stops the runner from
        # hammering a permanently-broken dependency (e.g. a stale NAS
        # credential) at full retry rate -- see failure_breaker.py.
        breaker = FailureBreaker()

        if not self.quiet:
            tuning_note = "auto-tuning" if tuner.enabled else "static (AT-Field unreachable)"
            print(
                f"[runner] {self.runner_id} on {self.host}: {initial_workers} workers "
                f"({tuning_note}, ceiling {self.workers}), "
                f"capacity {self.capacity}, task {self.task_ref}, coordinator {self.coordinator_url}",
                flush=True,
            )
        pool = LocalPool(
            task_ref=self.task_ref,
            workers=initial_workers,
            extra_syspath=self.extra_syspath,
            item_retries=self.item_retries,
            item_backoff=self.item_backoff,
            max_tasks_per_child=self.max_tasks_per_child,
            gc_between_tasks=self.gc_between_tasks,
        )
        reg = self._start_process_registration()
        try:
            while not self._draining:
                # Mutual auth gate: never register, lease, or run until we've
                # cryptographically verified this Coordinator holds the mesh token.
                if not self._trusted():
                    time.sleep(self.poll_interval)
                    if self._auto:
                        self.coordinator_url = ""
                        self._verified_url = None
                        self._resolve_coordinator(blocking=False)
                    continue
                if not self._registered:
                    self._register()
                if self._check_atfield_pause():
                    continue
                # Phase 9 circuit breaker: refuse to lease (or lease only a
                # single half-open probe) while the dependency this runner
                # depends on is failing systemically. Loud + visible (once
                # per OPEN episode, not every poll) -- a silently-idle runner
                # must never look identical to a crash.
                may_lease, breaker_cap = breaker.allow_lease(time.time())
                if not may_lease:
                    if not self._warned_open and not self.quiet:
                        snap = breaker.snapshot()
                        print(
                            f"[runner] CIRCUIT OPEN: repeated failures "
                            f"({snap['dominant_error']}) -- not leasing until it "
                            f"clears (cooldown {snap['cooldown_s']:.0f}s). Fix the "
                            f"failing dependency (e.g. `kiroshi nas-cred rotate`).",
                            file=sys.stderr, flush=True,
                        )
                        self._warned_open = True
                    time.sleep(self.poll_interval)
                    continue
                self._warned_open = False
                # Lease against the CURRENT (possibly throttled-down) pool
                # size, not the operator's ceiling -- don't over-lease work
                # to a runner that's currently backed off under pressure.
                lease_capacity = min(self.capacity, pool.workers * 2)
                if breaker_cap is not None:
                    lease_capacity = min(lease_capacity, breaker_cap)
                lease = self._post(
                    "/lease",
                    {"runner_id": self.runner_id, "host": self.host,
                     "capacity": lease_capacity,
                     "heartbeat_interval": self.heartbeat_interval,
                     "job": self.job},
                )
                gigs = (lease or {}).get("gigs") or []
                lease_id = (lease or {}).get("lease_id")
                breaker.note_leased(len(gigs))
                # M9: any advisories the Coordinator attached to this lease response
                # are printed loudly on the Runner's stdout so they survive into
                # the rotating log file — the "in-band" delivery channel that
                # works for every consumer (a human tailing logs, an LLM that
                # reads terminal output between turns, a CI grep). Deduped
                # server-side by fingerprint; we just render.
                self._emit_advisories((lease or {}).get("advisories") or [])
                if not gigs:
                    time.sleep(self.poll_interval)
                    continue

                def _hb() -> None:
                    # Watchdog liveness pulse: this callback only fires from
                    # *inside* run_batch's own inner loop while it's genuinely
                    # cycling. Bumping here (not just once per finished batch)
                    # means a single long-but-healthy batch keeps resetting the
                    # clock throughout its run, so only a loop that has
                    # actually stopped advancing goes stale -- see
                    # _start_watchdog. Unconditional: must not depend on the
                    # POST below succeeding.
                    self._last_progress_at = time.time()
                    # Fast mid-batch pressure brake: re-check headroom on the
                    # same cadence as the heartbeat and arm/disarm the
                    # max_pending cap accordingly (see WorkerTuner.max_pending_cap).
                    # This is what catches a burst of unusually heavy gigs
                    # landing together within a single batch, faster than
                    # waiting for the next between-batch AIMD step.
                    if tuner.enabled:
                        tuner.mid_batch_check()
                    if lease_id:
                        from .hostsample import sample_host
                        from . import subjob_capture
                        try:
                            in_flight = subjob_capture.list_inflight()
                        except Exception:  # noqa: BLE001
                            in_flight = []
                        self._post("/heartbeat", {
                            "lease_id": lease_id,
                            "runner_id": self.runner_id,
                            "heartbeat_interval": self.heartbeat_interval,
                            "stats": {
                                "job": self.job,
                                "resources": sample_host(root_pid=os.getpid()),
                                "workers_active": pool.workers,
                                "workers_ceiling": self.workers,
                                "tuning_enabled": bool(tuner.enabled),
                                "in_flight": in_flight,
                                "circuit": breaker.snapshot(),
                            },
                        })

                default_max_pending = pool.workers * 2
                results = pool.run_batch(
                    gigs,
                    max_pending=(lambda: tuner.max_pending_cap(default_max_pending)),
                    gig_timeout=self.gig_timeout,
                    heartbeat_cb=_hb,
                    hb_interval=self.heartbeat_interval,
                    pause_cb=self._pause_active,
                )
                self._last_progress_at = time.time()  # a full batch just finished
                now = time.time()
                for r in results:
                    breaker.record(r.get("status", "ok"), r.get("error"), now)
                try:
                    from . import subjob_capture
                    subjob_capture.sweep_stale(
                        max(1800.0, 2.0 * self.gig_timeout) if self.gig_timeout else 1800.0)
                except Exception:  # noqa: BLE001
                    pass
                if lease_id:
                    self._post("/complete", {"lease_id": lease_id, "results": results})
                if not self.quiet:
                    ok = sum(1 for r in results if r["status"] in ("ok", "skipped"))
                    ev = sum(1 for r in results if r["status"] == "requeue")
                    extra = f", {ev} evicted" if ev else ""
                    print(f"[runner] batch {len(gigs)} done ({ok} ok{extra})", flush=True)

                # Between-batch: run_batch() has returned, so there is
                # ZERO in-flight work right now -- the only point in the loop
                # where resizing the pool (a tree-kill + respawn) is
                # completely lossless. See WorkerTuner / LocalPool.resize.
                if tuner.enabled:
                    tuner.update_between_batches()
                    if tuner.maybe_resize(pool) and not self.quiet:
                        print(f"[runner] auto-tuned to {pool.workers} workers "
                              f"(ceiling {self.workers})", flush=True)
        finally:
            pool.close()
            if reg is not None:
                reg.close()
        if not self.quiet:
            print("[runner] drained, exiting.", flush=True)

    # -------------------------------------------------------------- advisories
    def _emit_advisories(self, advisories: list[dict[str, Any]]) -> None:
        """Print each advisory as a distinctive stdout line (M9).

        Format: ``KIROSHI-ADVISORY: <SEV> <code> [disk=<d>] | <detail> | action: <sa>``.
        Always printed (even with ``quiet=True``) — advisories are the whole
        point of the channel, and they're rate-limited server-side by
        fingerprint dedup so they're never spammy.
        """
        if not advisories:
            return
        try:
            from .advisories import format_stdout_line
        except Exception:  # pragma: no cover - import-time defense
            return
        seen: set[str] = getattr(self, "_advisory_seen", set())
        for adv in advisories:
            fp = adv.get("fingerprint") or adv.get("code") or ""
            # Skip the exact same fingerprint we just printed on the previous
            # tick so an advisory that stays active for minutes doesn't spam
            # our log every poll interval. New fingerprints are always shown.
            if fp and fp in seen:
                continue
            if fp:
                seen.add(fp)
            print(format_stdout_line(adv), flush=True)
        # Keep only what's still active-with-us; forget the rest so a re-fired
        # advisory (after the condition cleared + returned) prints again.
        current = {a.get("fingerprint") or a.get("code") or "" for a in advisories}
        self._advisory_seen = seen & current

    # ------------------------------------------------------ registry / pause
    def _start_process_registration(self):
        from .logsetup import current_log_path
        from .processreg import ProcessRegistration

        def _on_stop() -> None:
            self._draining = True

        try:
            return ProcessRegistration(
                "runner",
                {
                    "runner_id": self.runner_id,
                    "launch_command": self.launch_command,
                    "task": self.task_ref,
                    "workers": self.workers,
                    "coordinator_url": self.coordinator_url,
                    # AT-Field's generic event-webhook subscription convention
                    # (see at-field's reporter.py / README "Presence detection
                    # and event webhooks") -- any manifest under AT-Field's
                    # clients/ dir carrying this field gets POSTed kill/pressure
                    # events. Only set once a coordinator URL is actually known.
                    "atfield_event_webhook": (
                        f"{self.coordinator_url}/atfield/event" if self.coordinator_url else None
                    ),
                    "log_path": current_log_path(),
                },
                on_stop=_on_stop,
            ).start()
        except Exception:  # noqa: BLE001
            return None

    def _pause_active(self) -> bool:
        """Pure check (no side effects) — used as ``pause_cb`` inside run_batch so a
        pressure signal mid-batch evicts queued gigs immediately (abort-with-
        eviction) instead of holding the whole lease while sleeping."""
        from . import atfield

        return atfield.is_paused()

    def _check_atfield_pause(self) -> bool:
        """If at-field has paused the rig, back off (don't lease). Returns True
        when we paused (caller should `continue`)."""
        from . import atfield

        paused, until = atfield.pause_state()
        if not paused:
            self._was_paused = False
            return False
        if not getattr(self, "_was_paused", False):
            print(f"[runner] at-field pause active (until {until or 'further notice'}); "
                  f"backing off", flush=True)
            self._was_paused = True
        time.sleep(max(self.poll_interval, 3.0))
        return True

    # ------------------------------------------------------------- signals
    def _install_signal_handlers(self) -> None:
        def _drain(_signum, _frame):
            if not self._draining:
                print("[runner] drain requested; finishing current batch...", flush=True)
            self._draining = True

        try:
            signal.signal(signal.SIGINT, _drain)
            signal.signal(signal.SIGTERM, _drain)
        except (ValueError, OSError):  # pragma: no cover - non-main-thread
            pass
