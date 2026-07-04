"""Task-code distribution for ``kiroshi join`` (PLAN §7.5, SECURITY.md §6.5).

Lets a ``run --serve-task`` Coordinator hand its task's source to a joining Runner so a
new machine doesn't need a manual checkout. This is a **security-sensitive** path
(a Coordinator shipping code a Runner executes), so it is:

  * **opt-in** — only a Coordinator started with ``--serve-task`` serves anything, and
    only a single-file, top-level task module (the safe, legible 80% case);
  * **consent-gated** — :func:`kiroshi.join` shows the SHA-256 and requires the
    operator to approve before the code is written or imported;
  * **hash-pinned** — the approved hash is recorded; a later mismatch is refused
    until re-approved, so a Coordinator (or MITM) can't swap code after consent.

Multi-module / package tasks are deliberately **not** served — pre-install them or
use ``--task-repo`` (planned). This module only handles the single-file case.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from typing import Optional

from .appstate import state_dir
from .tasks import module_of


def tasks_dir() -> Path:
    d = state_dir() / "tasks"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def source_sha256(src: str) -> str:
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


def read_task_source(task_ref: str) -> dict:
    """Read a single-file, top-level task module's source for serving.

    Returns ``{task_ref, module, filename, source, sha256}``. Raises ``ValueError``
    for dotted/package/non-.py modules — those can't be safely shipped as one file.
    """
    module = module_of(task_ref)
    if not module:
        raise ValueError(f"no module in task ref {task_ref!r}")
    if "." in module:
        raise ValueError(
            f"task module {module!r} is dotted; served code supports only "
            f"top-level single-file modules. Pre-install the task on each machine "
            f"or use --task-repo (planned)."
        )
    spec = importlib.util.find_spec(module)
    if spec is None or not spec.origin or not spec.origin.endswith(".py") \
            or spec.submodule_search_locations:
        raise ValueError(
            f"{module!r} is not a single .py file (package or builtin); can't "
            f"serve its source. Pre-install it or use --task-repo (planned)."
        )
    src = Path(spec.origin).read_text(encoding="utf-8")
    return {
        "task_ref": task_ref,
        "module": module,
        "filename": f"{module}.py",
        "source": src,
        "sha256": source_sha256(src),
    }


def _pin_path(module: str) -> Path:
    return tasks_dir() / f"{module}.sha256"


def read_pin(module: str) -> Optional[str]:
    try:
        return _pin_path(module).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def write_pin(module: str, sha256: str) -> None:
    try:
        _pin_path(module).write_text(sha256 + "\n", encoding="utf-8")
    except OSError:
        pass


def write_task_source(module: str, source: str) -> Path:
    """Write served source to ``<state_dir>/tasks/<module>.py`` and return the path.

    Add :func:`tasks_dir` to the Runner's ``--syspath`` so the (spawned) pool
    workers can import the written module.
    """
    p = tasks_dir() / f"{module}.py"
    p.write_text(source, encoding="utf-8")
    return p
