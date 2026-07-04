"""Hardening tests for the within-node execution engine (LocalPool).

Exercises the failure modes that wedge naive process pools:
  - normal completion
  - per-item failure (local retry then reported error)
  - worker crash (os._exit) -> BrokenProcessPool recovery
  - hung gig -> per-gig timeout + force-terminate

Runnable two ways::
    pytest tests/test_hardening.py
    python tests/test_hardening.py     # prints PASS/FAIL, sets exit code

No third-party deps required (pure-python tasks), so it runs anywhere.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
for _p in (SRC, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi.pool import LocalPool  # noqa: E402

TASK = "tests._tasks:dispatch"
PATHS = [SRC, ROOT]


def _gigs(specs):
    return [{"subjob_id": f"j{i}", "spec": s} for i, s in enumerate(specs)]


def _counts(results):
    ok = sum(1 for r in results if r["status"] in ("ok", "skipped"))
    err = sum(1 for r in results if r["status"] == "error")
    return ok, err


def test_normal():
    pool = LocalPool(TASK, workers=2, extra_syspath=PATHS, item_retries=0)
    try:
        res = pool.run_batch(_gigs([{"action": "ok", "x": i} for i in range(6)]), max_pending=2)
    finally:
        pool.close()
    ok, err = _counts(res)
    assert len(res) == 6 and ok == 6 and err == 0, res


def test_failure_reported():
    pool = LocalPool(TASK, workers=1, extra_syspath=PATHS, item_retries=1)
    try:
        res = pool.run_batch(_gigs([{"action": "fail"}]), max_pending=1)
    finally:
        pool.close()
    assert len(res) == 1 and res[0]["status"] == "error", res
    assert "intentional failure" in (res[0]["error"] or ""), res


def test_crash_recovery():
    pool = LocalPool(TASK, workers=1, extra_syspath=PATHS, item_retries=0)
    try:
        res = pool.run_batch(
            _gigs([{"action": "ok"}, {"action": "crash"}, {"action": "ok"}]),
            max_pending=1,
        )
    finally:
        pool.close()
    ok, err = _counts(res)
    assert len(res) == 3 and ok == 2 and err == 1, res
    assert any("BrokenProcessPool" in (r.get("error") or "") for r in res), res


def test_timeout():
    pool = LocalPool(TASK, workers=1, extra_syspath=PATHS, item_retries=0)
    try:
        res = pool.run_batch(
            _gigs([{"action": "ok"}, {"action": "slow", "seconds": 30}, {"action": "ok"}]),
            max_pending=1,
            gig_timeout=2.0,
        )
    finally:
        pool.close()
    ok, err = _counts(res)
    assert len(res) == 3 and ok == 2 and err == 1, res
    assert any("timeout" in (r.get("error") or "") for r in res), res


def _main() -> int:
    import time

    cases = [
        ("normal", test_normal),
        ("failure_reported", test_failure_reported),
        ("crash_recovery", test_crash_recovery),
        ("timeout", test_timeout),
    ]
    failures = 0
    for name, fn in cases:
        t0 = time.time()
        try:
            fn()
            print(f"PASS  {name:18s} ({time.time() - t0:.1f}s)", flush=True)
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name:18s} {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name:18s} {e!r}", flush=True)
    print(f"\n{len(cases) - failures}/{len(cases)} passed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    # spawn-safe entry point
    import multiprocessing as mp

    mp.freeze_support()
    raise SystemExit(_main())
