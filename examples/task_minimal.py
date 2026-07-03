"""A minimal, idiomatic Kiroshi task — copy/paste as a starting template.

This shows every non-obvious moving part in the smallest possible working
task: the ABI (``run``/``enumerate_gigs``), idempotent skip-if-exists via
:mod:`kiroshi.kfs`, crash-safe atomic writes, and (optional) coordination
via the resource governor when a task hammers a shared budget.

Run it in three ways:

  1. One-shot local:      kiroshi run examples.task_minimal:run --items file1.txt file2.txt
  2. One-shot enumerate:  kiroshi run examples.task_minimal:run --enumerate -- --read-root ./in --write-root ./out
  3. Durable + multi-node:
       kiroshi fixer  --db mesh.db --port 8800
       kiroshi runner --fixer http://localhost:8800 --task examples.task_minimal:run --workers 4
       kiroshi seed   --fixer http://localhost:8800 --jobs gigs.jsonl --group demo
"""
from __future__ import annotations

import io
import os
from pathlib import PurePosixPath
from typing import Any, Iterator

from kiroshi import kfs
from kiroshi import paths as kpaths


# --- (optional) enumerate_gigs -------------------------------------------
# The task's own fan-out hook. When present, ``kiroshi run <task> --enumerate``
# calls this to generate gigs from CLI ``--`` args, no external jsonl needed.
# Keep this pure/fast — it runs on the LAUNCHER, before the mesh starts work.

def enumerate_gigs(args: dict[str, Any]) -> Iterator[dict[str, Any]]:
    read_root = args["read_root"]
    write_subdir = args.get("out_subdir", "processed")
    # kfs.walk streams over local, UNC, and mapped-drive paths uniformly.
    for dirpath, _dirs, files in kfs.walk(read_root):
        for fn in files:
            if not fn.endswith(".txt"):
                continue
            full = os.path.join(str(dirpath), fn)
            # rel to read_root -> becomes the gig's stable job_id (dedup key)
            rel = full[len(str(read_root)):].lstrip("/\\").replace("\\", "/")
            yield {
                "job_id": rel,
                "spec": {
                    "src_path": rel,
                    "dst_path": f"{write_subdir}/{PurePosixPath(rel).with_suffix('.upper.txt')}",
                },
            }


# --- run — the actual worker ---------------------------------------------
# Rule of thumb: keep this idempotent (skip if dst already exists) so
# kill/restart of the runner is free. That's what makes the mesh resumable.

def run(spec: dict[str, Any]) -> dict[str, Any]:
    # Resolve I/O roots: per-gig override wins, else the topology-derived
    # KIROSHI_READ_ROOT / KIROSHI_WRITE_ROOT the runner injects.
    read_root = kpaths.gig_read_root(spec) or os.environ["KIROSHI_READ_ROOT"]
    write_root = kpaths.gig_write_root(spec) or os.environ["KIROSHI_WRITE_ROOT"]
    src = kpaths.confined_join(read_root, spec["src_path"])
    dst = kpaths.confined_join(write_root, spec["dst_path"])

    # Idempotency: cheap skip if already produced. Combined with the
    # persistent Fixer DB, this makes the whole campaign restart-safe.
    if kfs.exists(dst):
        return {"status": "skipped", "metrics": {"reason": "exists"}}

    with kfs.open(src, "rb") as fh:
        raw = fh.read()
    payload = raw.upper()      # the "work" — swap for real logic

    # Atomic write: writes to a temp sibling then renames, so a crash mid-
    # write never leaves a half-file. Does NOT create parent dirs — the try
    # keeps the fast path fast; the except handles the once-per-dir case.
    parent = dst.rsplit("/", 1)[0].replace("\\", "/")
    try:
        with kfs.atomic_write(dst) as fh:
            fh.write(payload)
    except FileNotFoundError:
        kfs.makedirs(parent, exist_ok=True)
        with kfs.atomic_write(dst) as fh:
            fh.write(payload)

    return {"status": "ok", "metrics": {"bytes_in": len(raw), "bytes_out": len(payload)}}


# --- (optional) resource-governor example --------------------------------
# Uncomment if the task hammers a shared budget the topology doesn't cover
# (e.g. HuggingFace downloads, a small GPU pool, an API with rate limits).
# ResourceClient is imported lazily so tasks that don't need it stay lean.
#
#   from kiroshi.resource import ResourceClient
#   _rc = ResourceClient(os.environ["KIROSHI_FIXER"], os.environ.get("KIROSHI_TOKEN"))
#
# then in run():
#   with _rc.acquire(budget="hf_download"):   # blocks until a slot is free
#       ...
#
# Fail-open: if the Fixer is unreachable, acquire() is a no-op — the task
# keeps working (degrades to unbounded parallelism rather than dying).
