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
Raising an exception marks the gig failed; the Runner records the error and the
Fixer re-queues it up to the retry budget.

The Fixer never imports the task — only Runners do (``kiroshi runner --task ...``).
"""
from __future__ import annotations

import importlib
from typing import Any, Callable, Dict

TaskFn = Callable[[Dict[str, Any]], Dict[str, Any]]


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
