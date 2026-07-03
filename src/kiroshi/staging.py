"""Staging + bulk-transfer primitives — resource-budget-aware I/O helpers.

These are the building blocks that let non-gig workloads (downloads, GPU
processors, ad-hoc scripts) participate in Kiroshi's mesh-global I/O budget
without becoming formal Kiroshi gigs. They use :class:`ResourceClient` to
acquire read/write slots from the Fixer before touching the disks.

Two patterns:

1. **stage_to_local** (read-budget-aware): copy a file from a shared disk to
   local NVMe, acquiring a per-disk read slot first. After staging, the
   expensive compute (GPU decode, etc.) runs on the local copy with zero
   network contention. The slot is released as soon as the copy finishes —
   the compute phase needs no disk budget (it's reading local NVMe).

    with stage_to_local(remote_path, disk="disk3", client=rc) as local_path:
        ... GPU decode from local_path ...  # no disk budget held

2. **bulk_download** (write-budget-aware): download files to a cache tier
   (NVMe), acquiring a global-parity-write slot before each write. This is
   what the Seamless MP4 download *should* have used — it would have
   self-limited to the parity-write budget instead of thrashing with 64
   concurrent curls.

    for url, dest in download_list:
        bulk_download(url, dest, client=rc)  # acquires write slot, writes, releases

Both are fail-open: if the Fixer is unreachable, they proceed without budget
coordination (the work still runs, just without contention protection).
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from .resource import ResourceClient

logger = logging.getLogger(__name__)


@contextmanager
def stage_to_local(remote_path: str, disk: Optional[str] = None,
                   client: Optional[ResourceClient] = None,
                   scratch_dir: Optional[str] = None) -> Generator[str, None, None]:
    """Stage a remote file to local NVMe scratch, respecting the read budget.

    Acquires a per-disk read slot from the Fixer (if a client is provided),
    copies the file to a local temp path, then releases the slot. The caller
    does its expensive compute on the local copy (no disk budget held during
    compute). The temp file is deleted on exit.

    Args:
        remote_path: The file to stage (UNC path, NFS path, etc.).
        disk: The disk ID for the read budget (e.g. "disk3"). None = uncapped.
        client: A ResourceClient connected to the Fixer. None = no coordination.
        scratch_dir: Where to put the temp file. Defaults to system temp.

    Yields:
        The local path of the staged copy.
    """
    scratch = scratch_dir or os.path.join(
        os.environ.get("TEMP", tempfile.gettempdir()), "kiroshi_stage")
    os.makedirs(scratch, exist_ok=True)
    local_path = os.path.join(scratch, f"stage_{uuid.uuid4().hex[:8]}_{Path(remote_path).name}")

    # Acquire read slot (fail-open if no client / Fixer unreachable)
    slot = client.acquire(disk=disk, mode="read") if client else None
    try:
        with slot if slot else _null_ctx():
            shutil.copyfile(remote_path, local_path)
        logger.debug("staged %s -> %s", remote_path, local_path)
        yield local_path
    finally:
        try:
            os.remove(local_path)
        except Exception:  # noqa: BLE001
            pass


def bulk_download(url: str, dest: str, client: Optional[ResourceClient] = None,
                  timeout: int = 300, retries: int = 3) -> bool:
    """Download a URL to a destination, respecting the write budget.

    Acquires a global-parity-write slot from the Fixer (if a client is provided)
    before writing. This is what prevents 64 concurrent downloads from
    thrashing the parity disk — each download waits for its write slot.

    Args:
        url: The URL to download.
        dest: The destination path (should be on a cache tier for best perf).
        client: A ResourceClient for write-budget coordination. None = no limit.
        timeout: Per-download timeout in seconds.
        retries: Number of retry attempts on failure.

    Returns:
        True if the download succeeded, False otherwise.
    """
    import subprocess

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".partial"

    slot = client.acquire(disk=None, mode="write") if client else None
    try:
        with slot if slot else _null_ctx():
            for attempt in range(retries):
                try:
                    result = subprocess.run(
                        ["curl", "-sSL", "--fail", "--connect-timeout", "15",
                         "-o", tmp, url],
                        capture_output=True, timeout=timeout)
                    if result.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                        os.replace(tmp, dest)
                        return True
                except (subprocess.TimeoutExpired, Exception):  # noqa: BLE001
                    pass
                if attempt < retries - 1:
                    import time; time.sleep(2)
            # All retries failed
            try: os.remove(tmp)
            except Exception: pass
            return False
    finally:
        pass  # slot released by context manager


@contextmanager
def _null_ctx():
    """No-op context manager for when no ResourceClient is available."""
    yield None


# ---- mesh-task ABI (run + enumerate_gigs) --------------------------------
# These let the staging copy be distributed as normal Kiroshi gigs — the mesh
# handles retry, resume (skip-if-exists), per-disk budgeting via ResourceClient,
# and true-throughput reporting via bench.rate_from_dir.

import fnmatch
from typing import Any, Iterator

from . import kfs
from . import paths as kpaths


def enumerate_gigs(args: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Walk ``args["from"]`` and yield one copy gig per file.

    Each gig copies ``<from>/<rel>`` → ``<to>/<rel>``. The ``read_root`` and
    ``write_root`` are embedded in the spec so mesh runners (which may have
    different env vars) resolve paths correctly.

    ``args["pattern"]`` (default ``"*"``) filters filenames. ``args["by"]``
    (``"file"`` default, ``"shard"`` future) controls granularity.
    """
    src_root = str(args["from"]).rstrip("/\\")
    dst_root = str(args["to"]).rstrip("/\\")
    pattern = args.get("pattern") or "*"
    sep = "\\" if kfs.is_unc(src_root) else os.sep

    def _rel(full: str) -> str:
        f = str(full).replace("\\", "/").rstrip("/")
        b = src_root.replace("\\", "/").rstrip("/")
        return f[len(b):].lstrip("/") if f.startswith(b) else f

    for dirpath, _dirs, files in kfs.walk(src_root):
        for fn in files:
            if not fnmatch.fnmatch(fn, pattern):
                continue
            full = str(dirpath).rstrip("/\\") + sep + fn
            rel = _rel(full)
            yield {
                "job_id": rel,
                "spec": {
                    "src_path": rel,
                    "dst_path": rel,          # same rel under dst_root
                    "read_root":  src_root,
                    "write_root": dst_root,
                },
            }


def _copy_file(src: str, dst: str) -> int:
    """Stream-copy ``src`` → ``dst`` crash-safely via kfs. Returns bytes copied."""
    with kfs.open(src, "rb") as fin, kfs.atomic_write(dst) as fout:
        buf = fin.read(1 << 20)     # 1 MiB chunks
        total = 0
        while buf:
            fout.write(buf)
            total += len(buf)
            buf = fin.read(1 << 20)
    return total


def run(spec: dict[str, Any]) -> dict[str, Any]:
    """Mesh task: copy one file ``src→dst`` with I/O-budget coordination.

    Idempotent (skip if dst exists with same byte count). Acquires a read slot
    on the source disk and a write slot for the dest via ResourceClient
    (fail-open if the Fixer is unreachable). Parent dirs are created lazily.
    """
    read_root = kpaths.gig_read_root(spec) or os.environ.get("KIROSHI_READ_ROOT")
    write_root = kpaths.gig_write_root(spec) or os.environ.get("KIROSHI_WRITE_ROOT")
    if not read_root or not write_root:
        raise RuntimeError(
            "staging.run: spec has no read_root/write_root and "
            "KIROSHI_READ_ROOT/KIROSHI_WRITE_ROOT are unset")
    src = kpaths.confined_join(read_root, spec["src_path"])
    dst = kpaths.confined_join(write_root, spec["dst_path"])

    # idempotent skip
    if kfs.exists(dst):
        try:
            src_sz = os.path.getsize(src) if kfs.exists(src) else -1
            dst_sz = os.path.getsize(dst)
            if src_sz == dst_sz and src_sz >= 0:
                return {"status": "skipped", "metrics": {"reason": "exists",
                        "bytes": dst_sz}}
        except OSError:
            pass  # size check failed — re-copy to be safe

    # I/O budget (fail-open)
    fixer = os.environ.get("KIROSHI_FIXER")
    token = os.environ.get("KIROSHI_TOKEN")
    client = None
    if fixer:
        try:
            client = ResourceClient(fixer, token)
        except Exception:  # noqa: BLE001
            client = None

    src_disk = spec.get("disk")           # optional; topology router may set it
    read_slot = client.acquire(disk=src_disk, mode="read") if client else None
    write_slot = client.acquire(mode="write") if client else None
    try:
        try:
            n = _copy_file(src, dst)
        except FileNotFoundError:
            parent = dst.rsplit("/", 1)[0].rsplit("\\", 1)[0]
            kfs.makedirs(parent, exist_ok=True)
            n = _copy_file(src, dst)
    finally:
        # slots are context-managed but we acquired them bare; release explicitly
        for slot in (read_slot, write_slot):
            if slot is not None:
                try:
                    slot.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass

    return {"status": "ok", "metrics": {"bytes": n}}
