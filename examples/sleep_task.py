"""Trivial example task for smoke-testing the mesh end-to-end.

Run a Fixer, seed some sleep gigs, then start a Runner pointed here::

    kiroshi fixer --db demo.db
    kiroshi seed --fixer http://localhost:8787 --demo 500
    kiroshi runner --fixer http://localhost:8787 --task examples.sleep_task:run --workers 8

(Ensure this file is importable, e.g. run from the repo root or `pip install -e .`.)
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict


def run(spec: Dict[str, Any]) -> Dict[str, Any]:
    seconds = float(spec.get("seconds", 0.05))
    time.sleep(seconds)
    return {"status": "ok", "metrics": {"pid": os.getpid(), "slept": seconds}}
