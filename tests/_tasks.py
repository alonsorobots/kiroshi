"""Module-level task functions for hardening tests.

These must live in an importable module (not a test-local closure) because the
ProcessPool re-imports them in spawned child interpreters.
"""
from __future__ import annotations

import os
import time
from typing import Any


def dispatch(spec: dict[str, Any]) -> dict[str, Any]:
    action = spec.get("action", "ok")
    if action == "ok":
        return {"status": "ok", "metrics": {"x": spec.get("x", 0)}}
    if action == "fail":
        raise RuntimeError("intentional failure")
    if action == "slow":
        time.sleep(spec.get("seconds", 30))
        return {"status": "ok"}
    if action == "crash":
        os._exit(1)  # hard-kill the worker process -> BrokenProcessPool
    return {"status": "ok"}
