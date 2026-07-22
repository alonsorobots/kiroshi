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
- **Per-sub-job timeout** — a hung sub-job is abandoned and its worker process is
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
  cancelled and returned as ``status="requeue"`` (the Coordinator returns them to
  pending without burning the retry budget); running gigs are left to finish.
- **PYTHONPATH propagation** — Windows ``spawn`` starts fresh interpreters without
  the parent's ``sys.path``; we push the needed roots into the environment so
  children can import both ``kiroshi`` and the task module.

Failures are never silent: every sub-job produces a result dict, and unrecoverable
ones come back as ``{"status": "error", "error": ...}`` for the Coordinator to re-queue.
"""
from __future__ import annotations

import gc
import os
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from typing import Any, Callable, Iterable, Optional, Union

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
    # Detach inherited stdout/stderr: spawn workers inherit the parent's handles,
    # which may be a pipe (scheduled task / WMI) with no reader. Once the OS pipe
    # buffer fills, the worker's print()/traceback writes block at the C level
    # and the worker hangs silently. Redirect to devnull so worker output never
    # blocks; the parent's _Tee already captures everything to a log file.
    #
    # CRITICAL: under a windowless parent (pythonw / scheduled task / service),
    # spawned multiprocessing workers get sys.stdout = None (Python bpo-706263).
    # Calling .isatty() on None raises AttributeError and kills every worker —
    # so guard with `is not None` before any method call.
    if sys.stdout is not None and not sys.stdout.isatty():
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
    if sys.stderr is not None and not sys.stderr.isatty():
        try:
            sys.stderr = open(os.devnull, "w")
        except OSError:
            pass
    for p in extra_syspath:
        if p and p not in sys.path:
            sys.path.insert(0, p)
    from kiroshi.tasks import resolve_task

    _TASK_FN = resolve_task(task_ref)


def _attach_tail_log(subjob_id: str, out: dict[str, Any], *, cap_active: bool) -> None:
    """Best-effort: fold the sub-job's captured terminal tail into its result,
    then discard the on-disk capture -- called on EVERY return path (success,
    handled failure, unhandled exception), not conditionally on status. See
    subjob_capture.py module docstring for why this is unconditional."""
    from . import subjob_capture
    if cap_active:
        tail = subjob_capture.read_tail(subjob_id)
        if tail and isinstance(out.get("metrics"), dict):
            out["metrics"]["tail_log"] = tail
    subjob_capture.discard(subjob_id)


def _run_one(payload: tuple[str, dict, int, float]) -> dict[str, Any]:
    subjob_id, spec, retries, backoff = payload
    from .profiler import GigProfiler
    from .subjob_capture import SubjobCapture
    last_err: Optional[BaseException] = None
    for attempt in range(retries + 1):
        profiler = GigProfiler()
        profiler.start()
        cap = SubjobCapture(subjob_id)
        try:
            with cap:
                result = _TASK_FN(spec) or {}  # type: ignore[misc]
            status = result.get("status", "ok")
            metrics = result.get("metrics", {})
            # A task that reports failure WITHOUT raising (the pattern
            # gpu_4fps.run()'s hardened wrapper uses: catch everything, return
            # status="error" with the traceback in metrics) still needs a real
            # top-level error string -- the coordinator persists only that
            # string on the failed path (jobstore stores str(error), so a bare
            # None became the literal "None", and the traceback in metrics was
            # dropped entirely). Prefer the task's own error; otherwise
            # synthesize one from wherever tasks conventionally stash it, so a
            # real failure is never recorded as "None".
            error = None
            if status in ("error", "failed"):
                error = (
                    result.get("error")
                    or (metrics.get("traceback") if isinstance(metrics, dict) else None)
                    or (metrics.get("error") if isinstance(metrics, dict) else None)
                    or (metrics.get("reason") if isinstance(metrics, dict) else None)
                    or "task reported error with no detail"
                )
            elif status == "requeue":
                # Not a failure (jobstore.complete() clears this in the DB --
                # "error is cleared, not a fault"), but still worth carrying
                # through this intermediate result for in-flight visibility
                # (e.g. a runner's own log/print) -- unlike the error/failed
                # case, don't synthesize a placeholder if the task didn't
                # give one; a requeue needs no explanation to be valid.
                error = result.get("error")
            out = {
                "subjob_id": subjob_id,
                "status": status,
                "metrics": metrics,
                "error": error,
            }
            # attach the per-sub-job resource profile (empty dict if psutil absent)
            proc_profile = profiler.stop()
            if proc_profile and isinstance(out["metrics"], dict):
                out["metrics"]["proc"] = proc_profile
            _attach_tail_log(subjob_id, out, cap_active=cap.active)
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
    out = {"subjob_id": subjob_id, "status": "error", "error": repr(last_err), "metrics": {}}
    _attach_tail_log(subjob_id, out, cap_active=cap.active)
    return out


def _err(subjob_id: str, msg: str) -> dict[str, Any]:
    return {"subjob_id": subjob_id, "status": "error", "error": msg, "metrics": {}}


def _err_with_tail(subjob_id: str, msg: str) -> dict[str, Any]:
    """Like _err, but reads back whatever the crashed/timed-out sub-job had
    already written to its capture file (best-effort, may be a few ms stale
    relative to the worker's very last write). This is the most valuable
    capture point -- exactly the hang/crash case a worker's own return value
    can never report, since it never got to return anything.

    Deliberately does NOT discard the file: on Windows, a file another
    process still has open can't be deleted (the call silently no-ops), and
    at the moment this is called the still-wedged worker may well still hold
    it open. Callers must discard AFTER the corresponding tree-kill actually
    lands -- see the callers in run_batch."""
    out = _err(subjob_id, msg)
    from . import subjob_capture
    tail = subjob_capture.read_tail(subjob_id)
    if tail:
        out["metrics"]["tail_log"] = tail
    return out


def _requeue(subjob_id: str, reason: str) -> dict[str, Any]:
    """An evicted sub-job: the Coordinator returns it to ``pending`` without burning retries."""
    return {"subjob_id": subjob_id, "status": "requeue", "error": reason, "metrics": {}}


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

    def resize(self, workers: int) -> None:
        """Change the process count and rebuild the pool at the new size.

        Only safe to call with NO in-flight work (tree-kills the current
        pool). The runner's lease->run_batch->complete loop already
        guarantees this between batches -- see WorkerTuner in worker.py,
        which is the only caller and only calls this between `run_batch`
        calls, never during one.
        """
        n = max(1, workers)
        if n == self.workers:
            return
        self.workers = n
        self._rebuild()

    # --------------------------------------------------------------- run batch
    def run_batch(
        self,
        gigs: Iterable[dict[str, Any]],
        max_pending: Optional[Union[int, Callable[[], int]]] = None,
        gig_timeout: Optional[float] = None,
        heartbeat_cb: Optional[Callable[[], None]] = None,
        hb_interval: float = 30.0,
        pause_cb: Optional[Callable[[], bool]] = None,
    ) -> list[dict[str, Any]]:
        queue = list(gigs)
        default_max_pending = max(1, self.workers * 2)

        # ``max_pending`` may be a live callable (e.g. a WorkerTuner's current
        # in-flight cap) instead of a fixed int -- the fast mid-batch pressure
        # brake. ``refill()`` re-resolves it on every call (every loop
        # iteration, via the completion-wait below), so lowering it mid-batch
        # simply stops new submissions and lets in-flight gigs drain -- no
        # cancellation, no lost work, no pool rebuild.
        def resolve_max_pending() -> int:
            if max_pending is None:
                return default_max_pending
            if callable(max_pending):
                try:
                    v = int(max_pending())
                except Exception:
                    return default_max_pending
                return max(1, v)
            return max(1, max_pending)

        idx = 0
        results: list[dict[str, Any]] = []
        inflight: dict[Any, list] = {}  # future -> [subjob_id, submit_time]
        evicting = False  # True once pause_cb fired: stop refilling, drain running

        def submit_next() -> bool:
            nonlocal idx
            if idx >= len(queue):
                return False
            g = queue[idx]
            idx += 1
            fut = self._pool.submit(  # type: ignore[union-attr]
                _run_one,
                (g["subjob_id"], g.get("spec", {}), self.item_retries, self.item_backoff),
            )
            inflight[fut] = [g["subjob_id"], time.time()]
            return True

        def refill(stagger: float = 0.0) -> None:
            n = 0
            cap = resolve_max_pending()
            while len(inflight) < cap and submit_next():
                n += 1
                if stagger and n < cap:
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

        def recover(stagger: float, extra_jids: tuple[str, ...] = ()) -> None:
            """Handle a BrokenProcessPool cascade: mark inflight errors, rebuild
            once (deduped), refill staggered. Dedup stops the cascade from
            spawning N pools; staggered refill avoids re-triggering the init race.

            ``extra_jids`` lets a caller that already popped+errored a jid out of
            `inflight` (the per-future catch below) still have its capture file
            discarded on the SAME schedule -- after this rebuild, not before --
            since discarding while a still-wedged worker holds the file open
            silently no-ops on Windows.
            """
            from . import subjob_capture
            jids = [jid for jid, _st in inflight.values()] + list(extra_jids)
            for jid, _st in inflight.values():
                results.append(_err_with_tail(jid, "BrokenProcessPool"))
            inflight.clear()
            if time.time() - self._last_rebuild >= _RECOVERY_DEDUP_S:
                self._rebuild()
            for jid in jids:
                subjob_capture.discard(jid)
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
            broken_jids: list[str] = []
            for fut in done:
                jid, _st = inflight.pop(fut)
                try:
                    results.append(fut.result())
                except BrokenProcessPool:
                    results.append(_err_with_tail(jid, "BrokenProcessPool"))
                    broke = True
                    broken_jids.append(jid)
                except Exception as e:  # noqa: BLE001
                    results.append(_err_with_tail(jid, repr(e)))
                    # Not a pool crash -- the worker already returned/raised
                    # normally, so _run_one's own capture cleanup already ran
                    # in the child; discarding here is safe and just a no-op
                    # if the file is already gone.
                    from . import subjob_capture
                    subjob_capture.discard(jid)
            if broke:
                recover(_RECOVERY_STAGGER_S, extra_jids=tuple(broken_jids))
            elif not evicting:
                refill()  # steady-state refill is instant (no stagger)

            # per-sub-job timeout: abandon + kill, then carry on with a fresh pool
            if gig_timeout and inflight:
                now = time.time()
                if any(now - st > gig_timeout for _jid, st in inflight.values()):
                    timed_out_jids = [jid for jid, _st in inflight.values()]
                    for fut, (jid, st) in list(inflight.items()):
                        results.append(
                            _err_with_tail(
                                jid, "timeout" if now - st > gig_timeout else "pool_reset"))
                    inflight.clear()
                    self._rebuild()
                    from . import subjob_capture
                    for jid in timed_out_jids:
                        subjob_capture.discard(jid)
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
