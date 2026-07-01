"""Mesh resource coordination service — the I/O governor.

Kiroshi's disk-concurrency control was locked inside ``jobstore.lease()`` —
only formal gigs could benefit from the per-disk budget. This module extracts
it into a **standalone client** that *any* process (downloads, bulk transfers,
GPU processors, ad-hoc scripts) can use to coordinate I/O across the whole mesh.

Two budgets, both enforced by the Fixer (the only mesh-global arbiter):

1. **Per-disk read budget** (existing, now exported): caps in-flight reads per
   spindle across the fleet. Avoids seek thrash on HDD arrays.

2. **Global write/parity budget** (new): on parity-protected arrays (Unraid
   single/dual parity, RAID5/6), every array write RMWs through the parity
   spindle — a fleet-wide bottleneck. This caps concurrent *writes* globally
   so non-gig workloads self-limit instead of pinning the parity disk to 100%.

Usage (any process, not just Kiroshi gigs)::

    from kiroshi.resource import ResourceClient

    rc = ResourceClient(fixer="http://nas:8800", token=...)
    # Acquire a read slot on disk3 (blocks if the per-disk budget is full)
    with rc.acquire(disk="disk3", mode="read"):
        ... read the file ...
    # Acquire a write slot (blocks if the global parity-write budget is full)
    with rc.acquire(disk="disk3", mode="write"):
        ... write the file ...

If no Fixer is reachable, ``acquire()`` is a no-op (fail-open) — the process
runs without coordination but logs a warning. This ensures scripts work
standalone; they just don't get contention protection.

Design principles:
- **HW-config-gated**: read budgets and the parity-write semaphore are only
  active when the topology declares HDD/parity disks. NVMe-only nodes get
  no-op budgets (no seek penalty, no parity RMW).
- **No shortcuts**: the Fixer is the single source of truth for in-flight
  counts. No client-side guessing.
- **Idempotent + crash-safe**: if a client crashes, its acquired slots expire
  (TTL) and are reclaimed — same pattern as gig leases.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

# Default TTL for a resource slot (seconds). If the holder crashes without
# releasing, the Fixer reclaims after this. Matches the gig-lease default.
_DEFAULT_SLOT_TTL = 120.0
# How long to block on acquire before timing out (seconds).
_DEFAULT_ACQUIRE_TIMEOUT = 300.0
# Poll interval when waiting for a slot (seconds).
_POLL_INTERVAL = 0.5


class ResourceClient:
    """Client for the Fixer's mesh-global resource budget service.

    Any process can construct one and call ``acquire()`` to coordinate I/O.
    If the Fixer is unreachable, acquire is a no-op (fail-open).
    """

    def __init__(self, fixer: str, token: Optional[str] = None,
                 timeout: float = _DEFAULT_ACQUIRE_TIMEOUT,
                 slot_ttl: float = _DEFAULT_SLOT_TTL):
        self._fixer = fixer.rstrip("/")
        self._token = token or os.environ.get("KIROSHI_TOKEN", "")
        self._timeout = timeout
        self._slot_ttl = slot_ttl
        self._local_slots: set[str] = set()
        self._lock = threading.Lock()
        self._available: Optional[bool] = None  # cached fixer-reachability

    def _headers(self):
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _check_available(self) -> bool:
        """One-time check: is the Fixer reachable? Cached."""
        if self._available is not None:
            return self._available
        try:
            import requests
            r = requests.get(f"{self._fixer}/healthz",
                             headers=self._headers(), timeout=5)
            self._available = r.status_code == 200
        except Exception:
            self._available = False
        if not self._available:
            logger.warning("resource: Fixer at %s unreachable — acquire will "
                           "be a no-op (no contention protection)", self._fixer)
        return self._available

    def acquire(self, disk: Optional[str] = None, mode: str = "read",
                timeout: Optional[float] = None) -> "ResourceSlot":
        """Acquire a resource slot (read per-disk or write global-parity).

        Blocks until a slot is available or ``timeout`` seconds elapse. If the
        Fixer is unreachable, returns immediately (fail-open).

        Args:
            disk: The disk ID (for read mode) or None (for global write mode).
            mode: "read" (per-disk budget) or "write" (global parity budget).
            timeout: Override the default acquire timeout.

        Returns:
            A ``ResourceSlot`` context manager. Use with ``with``.
        """
        return ResourceSlot(self, disk, mode, timeout or self._timeout)

    def _do_acquire(self, disk: Optional[str], mode: str,
                    timeout: float) -> Optional[str]:
        """Actually acquire from the Fixer. Returns a slot_id or None (fail-open)."""
        if not self._check_available():
            return None

        import requests
        slot_id = uuid.uuid4().hex
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                r = requests.post(
                    f"{self._fixer}/resource/acquire",
                    json={"slot_id": slot_id, "disk": disk, "mode": mode,
                          "ttl": self._slot_ttl},
                    headers=self._headers(), timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("granted"):
                        with self._lock:
                            self._local_slots.add(slot_id)
                        return slot_id
                    # Not granted — wait and retry
                    retry_after = data.get("retry_after", _POLL_INTERVAL)
                    time.sleep(min(retry_after, _POLL_INTERVAL))
                    continue
                elif r.status_code == 503:
                    # Budget full — retry
                    time.sleep(_POLL_INTERVAL)
                    continue
                else:
                    logger.warning("resource: acquire returned %d: %s",
                                   r.status_code, r.text[:200])
                    return None  # fail-open on unexpected response
            except Exception as e:
                logger.warning("resource: acquire error (fail-open): %r", e)
                return None

        logger.warning("resource: acquire timed out after %.1fs (fail-open)", timeout)
        return None

    def _do_renew(self, slot_id: Optional[str], disk: Optional[str],
                  mode: str) -> None:
        """Extend a held slot's TTL so a long operation isn't reaped mid-hold."""
        if slot_id is None or not self._check_available():
            return
        try:
            import requests
            requests.post(f"{self._fixer}/resource/renew",
                          json={"slot_id": slot_id, "disk": disk, "mode": mode,
                                "ttl": self._slot_ttl},
                          headers=self._headers(), timeout=5)
        except Exception:
            pass  # a missed renewal is fine as long as some succeed

    def _do_release(self, slot_id: Optional[str]) -> None:
        """Release a slot back to the Fixer."""
        if slot_id is None:
            return
        with self._lock:
            self._local_slots.discard(slot_id)
        if not self._check_available():
            return
        try:
            import requests
            requests.post(f"{self._fixer}/resource/release",
                          json={"slot_id": slot_id},
                          headers=self._headers(), timeout=5)
        except Exception:
            pass  # TTL will reclaim if release fails


class ResourceSlot:
    """Context manager for a held resource slot. Released on __exit__.

    While held, a background thread RENEWS the slot at ~1/3 the TTL interval —
    so a long-running hold (a multi-minute download, a big file stage) keeps its
    slot alive instead of being reaped by the Fixer's TTL and over-subscribed by
    another client. Same pattern as gig-lease heartbeats.
    """

    def __init__(self, client: ResourceClient, disk: Optional[str],
                 mode: str, timeout: float):
        self._client = client
        self._disk = disk
        self._mode = mode
        self._timeout = timeout
        self._slot_id: Optional[str] = None
        self._renew_stop = threading.Event()
        self._renew_thread: Optional[threading.Thread] = None

    def __enter__(self) -> "ResourceSlot":
        self._slot_id = self._client._do_acquire(self._disk, self._mode, self._timeout)
        if self._slot_id is not None:
            # Renew at 1/3 TTL so a slow op never lets its slot expire mid-hold.
            interval = max(5.0, self._client._slot_ttl / 3.0)
            self._renew_thread = threading.Thread(
                target=self._renew_loop, args=(interval,), daemon=True)
            self._renew_thread.start()
        return self

    def _renew_loop(self, interval: float) -> None:
        while not self._renew_stop.wait(interval):
            self._client._do_renew(self._slot_id, self._disk, self._mode)

    def __exit__(self, *exc) -> None:
        self._renew_stop.set()
        self._client._do_release(self._slot_id)

    @property
    def granted(self) -> bool:
        """True if the Fixer granted a slot. False if fail-open (no Fixer)."""
        return self._slot_id is not None
