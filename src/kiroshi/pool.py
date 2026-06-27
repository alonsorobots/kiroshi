"""LocalPool — the within-node execution engine for a Runner.

A managed ``ProcessPoolExecutor`` that turns a batch of gigs into results with the
hard-won single-node robustness patterns:

- **ProcessPool, not threads** — CPU-bound tasks need real cores (the GIL makes
  threads useless for them).
- **Bounded submission window** (``max_pending = workers * 2``) — submitting a
  huge batch of futures at once can deadlock the executor pipe on Windows. We keep
  a sliding window and refill as gigs finish.
- **Per-gig timeout** — a hung gig is abandoned and its worker process is
  force-terminated, so one bad clip can't wedge a node forever.
- **BrokenProcessPool recovery** — if a worker segfaults / os._exit's, we record
  the affected gigs as errors, rebuild the pool, and carry on with the rest.
- **PYTHONPATH propagation** — Windows ``spawn`` starts fresh interpreters without
  the parent's ``sys.path``; we push the needed roots into the environment so
  children can import both ``kiroshi`` and the task module.

Failures are never silent: every gig produces a result dict, and unrecoverable
ones come back as ``{"status": "error", "error": ...}`` for the Fixer to re-queue.
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from typing import Any, Callable, Iterable, Optional

try:
    from concurrent.futures.process import BrokenProcessPool
except Exception:  # pragma: no cover
    BrokenProcessPool = RuntimeError  # type: ignore

# --- per-process worker globals (populated by the pool initializer) ----------
_TASK_FN: Optional[Callable[[dict], dict]] = None


def _init_worker(task_ref: str, extra_syspath: list[str]) -> None:
    global _TASK_FN
    for p in extra_syspath:
        if p and p not in sys.path:
            sys.path.insert(0, p)
    from kiroshi.tasks import resolve_task

    _TASK_FN = resolve_task(task_ref)


def _run_one(payload: tuple[str, dict, int, float]) -> dict[str, Any]:
    job_id, spec, retries, backoff = payload
    last_err: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            result = _TASK_FN(spec) or {}  # type: ignore[misc]
            return {
                "job_id": job_id,
                "status": result.get("status", "ok"),
                "metrics": result.get("metrics", {}),
                "error": None,
            }
        except Exception as e:  # noqa: BLE001 - report everything
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
    return {"job_id": job_id, "status": "error", "error": repr(last_err), "metrics": {}}


def _err(job_id: str, msg: str) -> dict[str, Any]:
    return {"job_id": job_id, "status": "error", "error": msg, "metrics": {}}


class LocalPool:
    def __init__(
        self,
        task_ref: str,
        workers: int,
        extra_syspath: Optional[list[str]] = None,
        item_retries: int = 2,
        item_backoff: float = 0.5,
    ):
        self.task_ref = task_ref
        self.workers = max(1, workers)
        self.extra_syspath = [p for p in (extra_syspath or []) if p]
        self.item_retries = item_retries
        self.item_backoff = item_backoff
        self._pool: Optional[ProcessPoolExecutor] = None
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
        self._pool = ProcessPoolExecutor(
            max_workers=self.workers,
            initializer=_init_worker,
            initargs=(self.task_ref, [os.path.abspath(p) for p in self.extra_syspath]),
        )

    def _force_terminate(self, pool: Optional[ProcessPoolExecutor]) -> None:
        """Best-effort kill of a pool's worker processes (frees hung tasks)."""
        if pool is None:
            return
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        procs = getattr(pool, "_processes", None) or {}
        for proc in list(procs.values()):
            try:
                if proc.is_alive():
                    proc.terminate()
            except Exception:
                pass

    def _rebuild(self) -> None:
        old = self._pool
        self._pool = None
        self._force_terminate(old)
        self._open()

    def close(self) -> None:
        if self._pool is not None:
            try:
                self._pool.shutdown(wait=True, cancel_futures=True)
            except Exception:
                pass
            self._pool = None

    # --------------------------------------------------------------- run batch
    def run_batch(
        self,
        gigs: Iterable[dict[str, Any]],
        max_pending: Optional[int] = None,
        gig_timeout: Optional[float] = None,
        heartbeat_cb: Optional[Callable[[], None]] = None,
        hb_interval: float = 30.0,
    ) -> list[dict[str, Any]]:
        queue = list(gigs)
        max_pending = max_pending or max(1, self.workers * 2)
        idx = 0
        results: list[dict[str, Any]] = []
        inflight: dict[Any, list] = {}  # future -> [job_id, submit_time]

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

        def refill() -> None:
            while len(inflight) < max_pending and submit_next():
                pass

        for _ in range(min(max_pending, len(queue))):
            if not submit_next():
                break

        last_hb = time.time()
        # poll faster when a timeout must be enforced
        poll = min(hb_interval, 1.0) if gig_timeout else hb_interval

        while inflight:
            try:
                done, _ = wait(list(inflight.keys()), timeout=poll, return_when=FIRST_COMPLETED)
            except BrokenProcessPool:
                for jid, _st in inflight.values():
                    results.append(_err(jid, "BrokenProcessPool"))
                inflight.clear()
                self._rebuild()
                refill()
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
                for jid, _st in inflight.values():
                    results.append(_err(jid, "BrokenProcessPool"))
                inflight.clear()
                self._rebuild()
                refill()
            else:
                refill()

            # per-gig timeout: abandon + kill, then carry on with a fresh pool
            if gig_timeout and inflight:
                now = time.time()
                if any(now - st > gig_timeout for _jid, st in inflight.values()):
                    for fut, (jid, st) in list(inflight.items()):
                        results.append(_err(jid, "timeout" if now - st > gig_timeout else "pool_reset"))
                    inflight.clear()
                    self._rebuild()
                    refill()

            if heartbeat_cb and time.time() - last_hb >= hb_interval:
                try:
                    heartbeat_cb()
                except Exception:
                    pass
                last_hb = time.time()

        return results
