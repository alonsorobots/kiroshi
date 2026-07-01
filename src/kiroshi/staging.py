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
