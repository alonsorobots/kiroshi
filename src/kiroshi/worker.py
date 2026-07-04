"""The Runner — worker node that pulls gigs and executes them locally.

Pull loop: lease a batch -> run it on a :class:`~kiroshi.pool.LocalPool` -> report
results -> repeat. All the within-node robustness (process pool, bounded window,
per-sub-job timeout, broken-pool recovery, PYTHONPATH propagation) lives in
``LocalPool``; the Runner is just the HTTP coordination + lifecycle around it.

Graceful drain on Ctrl-C / SIGTERM: finish + report the current batch, then exit.
"""
from __future__ import annotations

import os
import signal
import socket
import time
import uuid
from typing import Any, Optional

import requests

from . import security
from .discovery import discover_coordinator
from .pool import LocalPool

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
        capacity: int = 100,
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
        self._verified_url: Optional[str] = None  # last Coordinator that passed the auth challenge
        self.task_ref = task_ref
        self.workers = workers or (os.cpu_count() or 4)
        self.capacity = capacity
        self.runner_id = runner_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.host = host or socket.gethostname()
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.item_retries = item_retries
        self.item_backoff = item_backoff
        self.gig_timeout = gig_timeout
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
        from .logsetup import current_log_path

        ok = self._post("/register", {
            "runner_id": self.runner_id,
            "host": self.host,
            "launch_command": self.launch_command,
            "task": self.task_ref,
            "workers": self.workers,
            "pid": os.getpid(),
            "log_path": current_log_path(),
        })
        self._registered = bool(ok)

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
        self._resolve_coordinator()  # block until a Coordinator is known (auto mode)
        if not self.quiet:
            print(
                f"[runner] {self.runner_id} on {self.host}: {self.workers} workers, "
                f"capacity {self.capacity}, task {self.task_ref}, coordinator {self.coordinator_url}",
                flush=True,
            )
        pool = LocalPool(
            task_ref=self.task_ref,
            workers=self.workers,
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
                lease = self._post(
                    "/lease",
                    {"runner_id": self.runner_id, "host": self.host,
                     "capacity": min(self.capacity, self.workers * 2),
                     "heartbeat_interval": self.heartbeat_interval},
                )
                gigs = (lease or {}).get("gigs") or []
                lease_id = (lease or {}).get("lease_id")
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
                    if lease_id:
                        self._post("/heartbeat", {"lease_id": lease_id,
                                                  "runner_id": self.runner_id,
                                                  "heartbeat_interval": self.heartbeat_interval})

                results = pool.run_batch(
                    gigs,
                    max_pending=self.workers * 2,
                    gig_timeout=self.gig_timeout,
                    heartbeat_cb=_hb,
                    hb_interval=self.heartbeat_interval,
                    pause_cb=self._pause_active,
                )
                if lease_id:
                    self._post("/complete", {"lease_id": lease_id, "results": results})
                if not self.quiet:
                    ok = sum(1 for r in results if r["status"] in ("ok", "skipped"))
                    ev = sum(1 for r in results if r["status"] == "requeue")
                    extra = f", {ev} evicted" if ev else ""
                    print(f"[runner] batch {len(gigs)} done ({ok} ok{extra})", flush=True)
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
