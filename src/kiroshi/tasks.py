"""Task resolution & contract.

A Kiroshi task is a **module-level** function (so it is picklable for the
``spawn`` start method on Windows) referenced as ``"package.module:function"``::

    # mypkg/mytask.py
    def run(spec: dict) -> dict:
        # do CPU-bound work described by `spec`
        return {"status": "ok", "metrics": {...}}

Return a dict. Conventional ``status`` values:
    - ``"ok"``       — completed successfully (default if status omitted)
    - ``"skipped"``  — nothing to do (e.g. output already exists)
Raising an exception marks the sub-job failed; the Runner records the error and the
Coordinator re-queues it up to the retry budget.

The Coordinator never imports the task — only Runners do (``kiroshi runner --task ...``).
"""
from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, Iterator, Optional

TaskFn = Callable[[Dict[str, Any]], Dict[str, Any]]

# The enumeration contract (see PLAN §7.5). A task module MAY define a
# module-level ``enumerate_gigs(args: dict) -> Iterator[dict]`` that turns the
# pass-through ``--`` args from ``kiroshi run`` into gigs. Each yielded sub-job is a
# ``{"subjob_id": str, "spec": dict, "job"?: str}`` dict — exactly the shape
# ``/seed`` and :meth:`JobStore.seed` accept. This lets a task own its own
# fan-out (e.g. one source read -> a 4-fps and an 8-fps sub-job) which a generic
# ``--items`` globber can't infer.
ENUMERATE_FN = "enumerate_gigs"
EnumerateFn = Callable[[Dict[str, Any]], Iterator[Dict[str, Any]]]

# The self-test contract. A task module MAY define a module-level
# ``selftest() -> None`` that exercises its FULL runtime path on a tiny,
# self-contained fixture (build a synthetic input, run the core transform,
# assert the output shape) and raises on any problem. Preflight
# (``kiroshi remote``/``doctor``) imports the module and calls it, which catches
# the failure modes ``find_spec`` cannot: a LAZY import inside ``run()`` (a
# dep present on the coordinator but missing on a stale node), a missing
# repo-relative asset, or a broken native extension — before a single sub-job is
# leased, on the exact interpreter that will run the work.
SELFTEST_FN = "selftest"
SelfTestFn = Callable[[], None]


def resolve_task(ref: str) -> TaskFn:
    """Resolve a ``"module:function"`` reference to a callable."""
    module_name, sep, fn_name = ref.partition(":")
    if not sep or not fn_name:
        raise ValueError(
            f"Task reference must be 'module:function', got {ref!r}"
        )
    module = importlib.import_module(module_name)
    fn = getattr(module, fn_name, None)
    if fn is None or not callable(fn):
        raise ValueError(f"{ref!r} did not resolve to a callable")
    return fn  # type: ignore[return-value]


def module_of(ref: str) -> str:
    """The module part of a ``"module:function"`` reference."""
    return ref.partition(":")[0]


def resolve_enumerator(ref: str) -> Optional[EnumerateFn]:
    """Return the task module's ``enumerate_gigs`` hook, or ``None`` if absent.

    ``ref`` may be a full ``"module:function"`` task ref or a bare module name;
    the enumerator is looked up by the :data:`ENUMERATE_FN` convention in that
    same module.
    """
    module = importlib.import_module(module_of(ref))
    fn = getattr(module, ENUMERATE_FN, None)
    if fn is None or not callable(fn):
        return None
    return fn  # type: ignore[return-value]


def resolve_selftest(ref: str) -> Optional[SelfTestFn]:
    """Return the task module's ``selftest`` hook, or ``None`` if absent.

    Importing the module here (not just ``find_spec``) is deliberate: it forces
    the task's top-level imports to resolve on this interpreter. The returned
    callable, when invoked, must exercise the rest (lazy imports + core compute).
    """
    module = importlib.import_module(module_of(ref))
    fn = getattr(module, SELFTEST_FN, None)
    if fn is None or not callable(fn):
        return None
    return fn  # type: ignore[return-value]
