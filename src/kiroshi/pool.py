"""LocalPool — the within-node execution engine for a Runner.

A managed ``ProcessPoolExecutor`` that turns a batch of gigs into results with the
hard-won single-node robustness patterns (ported clean from production Windows
pipelines — spawn, IPC pipe, GPU/subprocess-teardown hangs):

- **ProcessPool, not threads** — CPU-bound tasks need real cores (the GIL makes
  threads useless for them).
- **Bounded submission window** (``max_pending = workers * 2``) — submitting a
  huge batch of futures at once can deadlock the executor pipe on Windows. We keep
  a sliding window and refill as gigs finish.
- **Staggered cold-start** — the initial submissions are spaced ~100ms so worker
  init (task import / model load) doesn't race; only at startup, not steady-state.
- **Per-gig timeout** — a hung gig is abandoned and its worker process is
  force-terminated, so one bad clip can't wedge a node forever.
- **BrokenProcessPool recovery with dedup + refill** — when one worker crashes,
  *every* pending future cascades to ``BrokenProcessPool``. We mark them as
  errors, rebuild the pool **once** (deduped within a short window so the cascade
  doesn't spawn N pools), and refill the window staggered.
- **Hard shutdown** — ``shutdown(wait=True)`` can hang forever on Windows when
  workers deadlock in cleanup (CUDA/subprocess teardown). We ``wait=False``, give
  workers a grace period, then **tree-kill** (``taskkill /F /T``) survivors so
  resources actually release.
- **Abort-with-eviction under pressure** — an optional ``pause_cb`` lets the
  Runner signal "stop now" (e.g. at-field pause). Not-yet-started gigs are
  cancelled and returned as ``status="requeue"`` (the Fixer returns them to
  pending without burning the retry budget); running gigs are left to finish.
- **PYTHONPATH propagation** — Windows ``spawn`` starts fresh interpreters without
  the parent's ``sys.path``; we push the needed roots into the environment so
  children can import both ``kiroshi`` and the task module.

Failures are never silent: every gig produces a result dict, and unrecoverable
ones come back as ``{"status": "error", "error": ...}`` for the Fixer to re-queue.
"""
from __future__ import annotations

import gc
import os
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from typing import Any, Callable, Iterable, Optional

try:
    from concurrent.futures.process import BrokenProcessPool
except Exception:  # pragma: no cover
    BrokenProcessPool = RuntimeError  # type: ignore

# --- tuning constants --------------------------------------------------------
_COLD_START_STAGGER_S = 0.1   # delay between *initial* submissions (init-race guard)
_RECOVERY_STAGGER_S = 0.05    # shorter stagger when refilling after a crash
_RECOVERY_DEDUP_S = 5.0       # don't rebuild the pool twice within this window
_SHUTDOWN_GRACE_S = 10.0      # wait this long for workers to exit before tree-kill
_PAUSE_POLL_S = 5.0           # check pause_cb at least this often during a batch

# --- per-process worker globals (populated by the pool initializer) ----------
_TASK_FN: Optional[Callable[[dict], dict]] = None
_GC_BETWEEN_TASKS = False


def _init_worker(task_ref: str, extra_syspath: list[str]) -> None:
    global _TASK_FN
    for p in extra_syspath:
        if p and p not in sys.path:
            sys.path.insert(0, p)
    from kiroshi.tasks import resolve_task

    _TASK_FN = resolve_task(task_ref)


def _run_one(payload: tuple[str, dict, int, float]) -> dict[str, Any]:
    job_id, spec, retries, backoff = payload
    from .profiler import GigProfiler
    last_err: Optional[BaseException] = None
    for attempt in range(retries + 1):
        profiler = GigProfiler()
        profiler.start()
        try:
            result = _TASK_FN(spec) or {}  # type: ignore[misc]
            out = {
                "job_id": job_id,
                "status": result.get("status", "ok"),
                "metrics": result.get("metrics", {}),
                "error": None,
            }
            # attach the per-gig resource profile (empty dict if psutil absent)
            proc_profile = profiler.stop()
            if proc_profile:
                out["metrics"]["proc"] = proc_profile
            return out
        except Exception as e:  # noqa: BLE001 - report everything
            profiler.stop()             # always stop, even on failure
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
    # Optional per-task cleanup: prevents cross-task accumulation of fragmented
    # heap / leaked refs in long-lived workers. Off by default (a band-aid for
    # C-level leaks you can't patch; the real fix is evicting the accumulator).
    if _GC_BETWEEN_TASKS:
        gc.collect()
    return {"job_id": job_id, "status": "error", "error": repr(last_err), "metrics": {}}


def _err(job_id: str, msg: str) -> dict[str, Any]:
    return {"job_id": job_id, "status": "error", "error": msg, "metrics": {}}


def _requeue(job_id: str, reason: str) -> dict[str, Any]:
    """An evicted gig: the Fixer returns it to ``pending`` without burning retries."""
    return {"job_id": job_id, "status": "requeue", "error": reason, "metrics": {}}


def _tree_kill(proc) -> None:
    """Best-effort kill of a worker process AND its child tree.

    ``proc.kill()`` only kills the worker, not subprocesses it spawned (ffmpeg,
    a model server, ...). On Windows ``taskkill /F /T /PID`` tree-kills the whole
    process tree so GPU/subprocess resources actually release — the lesson from
    pipelines where ``proc.kill()`` left VRAM pinned. No psutil dependency.
    """
    try:
        pid = proc.pid
    except Exception:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


class LocalPool:
    def __init__(
        self,
        task_ref: str,
        workers: int,
        extra_syspath: Optional[list[str]] = None,
        item_retries: int = 2,
        item_backoff: float = 0.5,
        max_tasks_per_child: Optional[int] = None,
        gc_between_tasks: bool = False,
    ):
        self.task_ref = task_ref
        self.workers = max(1, workers)
        self.extra_syspath = [p for p in (extra_syspath or []) if p]
        self.item_retries = item_retries
        self.item_backoff = item_backoff
        self.max_tasks_per_child = max_tasks_per_child
        self.gc_between_tasks = gc_between_tasks
        self._pool: Optional[ProcessPoolExecutor] = None
        self._last_rebuild = 0.0
        self._propagate_pythonpath()
        self._open()

    # --------------------------------------------------------------- lifecycle
    def _propagate_pythonpath(self) -> None:
        existing = os.environ.get("PYTHONPATH", "")
        parts = existing.split(os.pathsep) if existing else []
        for p in self.extra_syspath:
            ap = os.path.abspath(p)
            if ap not in parts:
                parts.insert(0, ap)
        if parts:
            os.environ["PYTHONPATH"] = os.pathsep.join(parts)

    def _open(self) -> None:
        kwargs: dict[str, Any] = dict(
            max_workers=self.workers,
            initializer=_init_worker,
            initargs=(self.task_ref, [os.path.abspath(p) for p in self.extra_syspath]),
        )
        # Recycle workers every N gigs — a safety net for unfixable C-level leaks,
        # NOT a substitute for finding the real accumulator. Off by default.
        if self.max_tasks_per_child:
            try:
                kwargs["max_tasks_per_child"] = self.max_tasks_per_child
            except TypeError:  # pragma: no cover - <3.11
                pass
        if self.gc_between_tasks:
            # The worker reads this global; set it before workers spawn.
            global _GC_BETWEEN_TASKS
            _GC_BETWEEN_TASKS = True
        self._pool = ProcessPoolExecutor(**kwargs)

    def _hard_terminate(self, pool: Optional[ProcessPoolExecutor]) -> None:
        """shutdown(wait=False) + tree-kill lingering workers (frees hung tasks).

        ``wait=True`` can deadlock forever on Windows (worker cleanup hangs);
        ``wait=False`` + a grace poll + tree-kill guarantees termination.
        """
        if pool is None:
            return
        procs = list((getattr(pool, "_processes", None) or {}).values())
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        deadline = time.time() + _SHUTDOWN_GRACE_S
        while time.time() < deadline:
            if not procs:
                break
            if all(not _alive(p) for p in procs):
                break
            time.sleep(0.3)
        for p in procs:
            if _alive(p):
                _tree_kill(p)

    def _rebuild(self) -> None:
        old = self._pool
        self._pool = None
        self._hard_terminate(old)  # CRITICAL: tree-kill old workers (no 16+16 orphans)
        self._open()
        self._last_rebuild = time.time()

    def close(self) -> None:
        if self._pool is not None:
            self._hard_terminate(self._pool)
            self._pool = None

    # --------------------------------------------------------------- run batch
    def run_batch(
        self,
        gigs: Iterable[dict[str, Any]],
        max_pending: Optional[int] = None,
        gig_timeout: Optional[float] = None,
        heartbeat_cb: Optional[Callable[[], None]] = None,
        hb_interval: float = 30.0,
        pause_cb: Optional[Callable[[], bool]] = None,
    ) -> list[dict[str, Any]]:
        queue = list(gigs)
        max_pending = max_pending or max(1, self.workers * 2)
        idx = 0
        results: list[dict[str, Any]] = []
        inflight: dict[Any, list] = {}  # future -> [job_id, submit_time]
        evicting = False  # True once pause_cb fired: stop refilling, drain running

        def submit_next() -> bool:
            nonlocal idx
            if idx >= len(queue):
                return False
            g = queue[idx]
            idx += 1
            fut = self._pool.submit(  # type: ignore[union-attr]
                _run_one,
                (g["job_id"], g.get("spec", {}), self.item_retries, self.item_backoff),
            )
            inflight[fut] = [g["job_id"], time.time()]
            return True

        def refill(stagger: float = 0.0) -> None:
            n = 0
            while len(inflight) < max_pending and submit_next():
                n += 1
                if stagger and n < max_pending:
                    time.sleep(stagger)

        # Staggered COLD START: space the initial submissions so worker init (task
        # import / model load) doesn't race. Only at startup, not steady-state.
        refill(stagger=_COLD_START_STAGGER_S)

        last_hb = time.time()
        # poll faster when a timeout or pause check must be enforced
        poll = hb_interval
        if gig_timeout:
            poll = min(poll, 1.0)
        if pause_cb:
            poll = min(poll, _PAUSE_POLL_S)

        def recover(stagger: float) -> None:
            """Handle a BrokenProcessPool cascade: mark inflight errors, rebuild
            once (deduped), refill staggered. Dedup stops the cascade from
            spawning N pools; staggered refill avoids re-triggering the init race."""
            for jid, _st in inflight.values():
                results.append(_err(jid, "BrokenProcessPool"))
            inflight.clear()
            if time.time() - self._last_rebuild >= _RECOVERY_DEDUP_S:
                self._rebuild()
            refill(stagger=stagger)

        while inflight:
            # --- abort-with-eviction: a pause signal releases queued work now ---
            if pause_cb and not evicting and pause_cb():
                evicting = True
                for fut in list(inflight):
                    if fut.cancel():  # True = not yet started by a worker
                        jid, _st = inflight.pop(fut)
                        results.append(_requeue(jid, "evicted: pressure pause"))
                if not inflight:
                    break  # nothing was running; whole batch evicted

            try:
                done, _ = wait(list(inflight.keys()), timeout=poll,
                               return_when=FIRST_COMPLETED)
            except BrokenProcessPool:
                recover(_RECOVERY_STAGGER_S)
                continue

            broke = False
            for fut in done:
                jid, _st = inflight.pop(fut)
                try:
                    results.append(fut.result())
                except BrokenProcessPool:
                    results.append(_err(jid, "BrokenProcessPool"))
                    broke = True
                except Exception as e:  # noqa: BLE001
                    results.append(_err(jid, repr(e)))
            if broke:
                recover(_RECOVERY_STAGGER_S)
            elif not evicting:
                refill()  # steady-state refill is instant (no stagger)

            # per-gig timeout: abandon + kill, then carry on with a fresh pool
            if gig_timeout and inflight:
                now = time.time()
                if any(now - st > gig_timeout for _jid, st in inflight.values()):
                    for fut, (jid, st) in list(inflight.items()):
                        results.append(
                            _err(jid, "timeout" if now - st > gig_timeout else "pool_reset"))
                    inflight.clear()
                    self._rebuild()
                    refill(stagger=_RECOVERY_STAGGER_S)

            if heartbeat_cb and time.time() - last_hb >= hb_interval:
                try:
                    heartbeat_cb()
                except Exception:
                    pass
                last_hb = time.time()

        return results


def _alive(proc) -> bool:
    try:
        return proc.is_alive()
    except Exception:  # noqa: BLE001
        return False
