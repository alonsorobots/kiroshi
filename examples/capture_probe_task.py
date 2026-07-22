"""Example task for exercising per-sub-job output capture (subjob_capture.py).

Deliberately writes through THREE different channels so a test can prove the
capture crosses all of them:
  - Python ``print()`` (goes through ``sys.stdout``)
  - ``sys.stderr.write()`` (goes through ``sys.stderr``)
  - ``os.write(1, ...)`` (raw OS-level fd write -- simulates a native library
    like NVDEC/CUDA writing straight to the C-level stdout, bypassing
    ``sys.stdout`` entirely; only an fd-level ``os.dup2`` redirect catches this)

``spec["hang_after_output"]`` (seconds, default 0) lets a test simulate a
sub-job that writes partial output then hangs past a ``gig_timeout`` --
the crash-survivability scenario.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict


def run(spec: Dict[str, Any]) -> Dict[str, Any]:
    print("print-line: hello from print()")
    sys.stderr.write("stderr-line: hello from stderr\n")
    sys.stderr.flush()
    os.write(1, b"raw-fd-line: hello from os.write(1, ...)\n")

    hang_after_output = float(spec.get("hang_after_output", 0) or 0)
    if hang_after_output > 0:
        time.sleep(hang_after_output)  # simulate a wedged native call

    return {"status": "ok", "metrics": {"pid": os.getpid()}}
