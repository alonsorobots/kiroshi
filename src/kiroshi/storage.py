"""Storage topology for shard-aware I/O scheduling (PLAN §7.6, milestone M8/N1-N2).

When a mesh's shared storage is **many physical drives** (a NAS HDD array,
optionally with an NVMe cache tier), peak throughput needs every spindle busy AND
no spindle over-subscribed. That budget is **mesh-global** — only the Coordinator sees
the fleet-wide in-flight count per disk — so it lives here as config the Coordinator
reads, and the coordinator enforces it at lease time (``JobStore.lease``).

This module is **pure config + derivation**: it describes the topology and maps a
sub-job to a disk. It does no I/O. The actual read/write happens in the task; dual-path
routing (read direct / write cached) is N3. With no ``[[storage.disk]]`` config,
``load_topology()`` returns ``[]`` and everything is inert.

Boundary: Kiroshi owns the topology + the per-spindle budget + sub-job→disk mapping +
leasing policy. Reading/writing the bytes stays in the job.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Optional

# kind -> default per-disk concurrency (the budget). HDD low (seeks dominate;
# over-parallelizing one disk goes *slower* — measured d=6 optimal, d=8 thrash).
# NVMe/SSD high (no seeks; wants queue depth). `kiroshi nas benchmark` can override
# per-disk via the explicit ``concurrency`` field.
_KIND_DEFAULTS: dict[str, int] = {"hdd": 4, "ssd": 8, "nvme": 16}


def kind_default_concurrency(kind: Optional[str]) -> int:
    return _KIND_DEFAULTS.get((kind or "").lower(), 4)


@dataclass
class DiskConfig:
    id: str
    kind: str = "hdd"
    read: Optional[str] = None        # direct per-spindle share (fast sequential read)
    write: Optional[str] = None       # cached user share (fast small-file writes)
    match: str = ""                   # which gigs live here: "shard_01..08", glob, or substring
    concurrency: Optional[int] = None  # None -> kind default; benchmark fills this
    # --- write/parity contention model (PLAN: mesh resource governor) ---
    # On a parity-protected array (Unraid single/dual parity, RAID5/6), every
    # array write requires a read-modify-write through the parity spindle(s) —
    # a GLOBAL bottleneck, not per-disk. A single parity_protected=true disk
    # in the topology turns on a fleet-wide write semaphore so non-sub-job workloads
    # (downloads, bulk transfers) self-limit instead of thrashing the parity disk.
    parity_protected: bool = False
    write_concurrency: Optional[int] = None  # None -> 6 (measured optimal for RMW parity)
    # --- storage performance characteristics (for preflight routing + warnings) ---
    direct_path: Optional[str] = None   # raw device path (e.g. /mnt/diskN) — bypass FUSE
    cache_tier: Optional[str] = None    # "nvme" / "ssd" / None — write-fast tier
    seq_read_mbps: Optional[float] = None   # measured sequential read throughput
    write_mbps: Optional[float] = None      # measured write throughput (post-parity)

    @property
    def effective_concurrency(self) -> int:
        return self.concurrency if self.concurrency and self.concurrency > 0 \
            else kind_default_concurrency(self.kind)

    @property
    def effective_write_concurrency(self) -> int:
        if self.write_concurrency and self.write_concurrency > 0:
            return self.write_concurrency
        return 6  # measured: RMW parity writes are fastest with ~6 concurrent


def load_topology() -> list[DiskConfig]:
    """Storage disks declared in config (``[[storage.disk]]``). Empty if none —
    the inert default. Imported lazily to avoid a config <-> storage import cycle."""
    from .config import load_config

    return load_config().disks


def disk_concurrency_map(disks: list[DiskConfig]) -> dict[str, int]:
    """``{disk_id: budget}`` for the disks that have a cap. Only disks present in
    this map are budgeted at lease time; any other disk (or ``None``) is uncapped."""
    return {d.id: d.effective_concurrency for d in disks if d.id}


def validate_disks(disks: list[DiskConfig]) -> list[str]:
    """Return human-readable warnings for likely-misconfigured disk topologies.

    Called at coordinator boot (non-fatal) so a misconfiguration surfaces at startup
    instead of as 129k cryptic runtime ``KIROSHI_READ_ROOT is not set`` errors.

    The key case: a disk with ``match=""`` routes NOTHING (``match_disk`` returns
    ``False`` for empty match — deliberately, so an unruled disk can't shadow
    properly-routed ones by iteration order). A single-pool topology with an
    empty match almost certainly wanted ``match="*"``.
    """
    warns: list[str] = []
    for d in disks:
        m = (d.match or "").strip()
        if not m:
            if len(disks) == 1:
                warns.append(
                    f"disk '{d.id}': empty match rule routes NOTHING. A single-pool "
                    f"topology almost certainly wants match='*'.")
            else:
                warns.append(
                    f"disk '{d.id}': empty match rule is inert (routes nothing). "
                    f"Intentional placeholders are fine; otherwise set match='*'.")
    return warns


def has_parity(disks: list[DiskConfig]) -> bool:
    """True if any disk in the topology is parity-protected (triggers the global
    write semaphore). On a non-parity setup (all NVMe/SSD, or a RAID0 stripe),
    this returns False and write budgeting is inert — matching the user's
    requirement that these features only apply to HW configs that need them."""
    return any(d.parity_protected for d in disks)


def global_write_concurrency(disks: list[DiskConfig]) -> int:
    """The fleet-wide write budget for parity-protected arrays. If any disk
    declares parity_protected, the tightest (smallest) write_concurrency wins —
    that's the bottleneck spindle."""
    caps = [d.effective_write_concurrency for d in disks if d.parity_protected]
    return min(caps) if caps else 0


# --------------------------------------------------------------- sub-job -> disk
_RANGE_RE = re.compile(r"^(.*?)(\d+)$")


def _expand_range(match: str) -> list[str]:
    """``"shard_01..08"`` -> ``["shard_01", ..., "shard_08"]`` (zero-pad preserved)."""
    left, sep, right = match.partition("..")
    if not sep:
        return [match]
    m = _RANGE_RE.match(left)
    if not m or not right.isdigit():
        return [match]
    prefix, start, pad = m.group(1), int(m.group(2)), len(m.group(2))
    end = int(right)
    return [f"{prefix}{str(n).zfill(pad)}" for n in range(start, end + 1)]


def match_disk(haystack: str, match: str) -> bool:
    """Does ``haystack`` (a sub-job's subjob_id / path text) belong to the disk with this
    ``match`` rule? Supports a shard range (``shard_01..08``), a glob
    (``shard_0[1-8]*``), or a plain substring (``shard_01``)."""
    if not match:
        return False
    if ".." in match:
        return any(name in haystack for name in _expand_range(match))
    if any(c in match for c in "*?["):
        return fnmatch.fnmatch(haystack, match)
    return match in haystack


def _norm_share(path: object) -> str:
    """Comparable share form: forward slashes, lower-case, no trailing sep."""
    return str(path).replace("\\", "/").rstrip("/").lower() if path else ""


def _disk_by_root_share(spec: dict[str, Any],
                        disks: list[DiskConfig]) -> Optional[str]:
    """Resolve a disk when the sub-job's ``read_root``/``write_root`` is a declared
    share of that disk (``read`` / ``write`` / ``direct_path``), even though no
    ``match`` rule hit. This lets the per-spindle budget apply from the folders a
    job declares — no shard token required. Purely a *resolution* helper; it never
    rewrites the spec (dual-path fill stays in ``inject_roots``, which only fills
    absent roots)."""
    for key in ("read_root", "write_root"):
        r = _norm_share(spec.get(key))
        if not r:
            continue
        for d in disks:
            for share in (d.read, d.write, d.direct_path):
                s = _norm_share(share)
                if s and (r == s or r.startswith(s + "/")):
                    return d.id
    return None


def derive_disk(subjob_id: str, spec: dict[str, Any],
                disks: list[DiskConfig]) -> Optional[str]:
    """Resolve which physical disk a sub-job's input lives on, or ``None`` if no rule
    matches (uncapped / inert). Matches against the subjob_id and common path fields in
    the spec, so it works whether the shard is in the subjob_id or in a ``src_path``.
    Falls back to the declared ``read_root``/``write_root`` share when no ``match``
    rule hits, so a job that declares its folders is budgeted even without a shard
    token."""
    if not disks:
        return None
    candidates = [subjob_id]
    for k in ("path", "src_path", "dst_path", "input", "video_path"):
        v = spec.get(k)
        if isinstance(v, str):
            candidates.append(v)
    hay = " ".join(candidates)
    for d in disks:
        if match_disk(hay, d.match):
            return d.id
    return _disk_by_root_share(spec, disks)


def inject_roots(gigs: list[dict[str, Any]], disks: list[DiskConfig]) -> None:
    """Augment each leased sub-job's spec with its disk's ``read``/``write`` roots
    (dual-path routing, N3): the task then reads from the direct per-spindle share
    and writes to the cached share, without knowing the topology. In-place on the
    freshly-loaded lease copy (the stored spec is untouched). Inert if the sub-job has
    no disk or the disk declares no roots — the task falls back to the env roots.

    If a sub-job arrives with ``disk=None`` (e.g. seeded under a misconfigured topology
    whose match rule was later fixed), re-derive the disk on the fly so a config
    fix takes effect without wiping + re-seeding the entire DB. Gigs that were
    *intentionally* inert (no matching rule) stay inert — ``derive_disk`` returns
    ``None`` and we skip them, exactly as before.
    """
    by_id = {d.id: d for d in disks}
    for g in gigs:
        did = g.get("disk")
        if did is None and disks:
            # Re-derive: the sub-job was seeded when no disk rule matched (or the
            # topology has since changed). Only fires for untagged gigs —
            # already-tagged gigs pay zero cost.
            did = derive_disk(g.get("subjob_id", ""), g.get("spec") or {}, disks)
            if did:
                g["disk"] = did       # lease-copy only; stored row untouched
        d = by_id.get(did)
        if d is None:
            continue
        spec = g.get("spec")
        if not isinstance(spec, dict):
            continue
        # Only fill roots the spec doesn't already carry: a sub-job that ships its
        # own read_root/write_root (e.g. a slerp step reading reduced_88dof_30fps
        # rather than the disk's raw data_canonical share) knows its paths better
        # than the topology default. Overwriting them silently misroutes the task.
        if d.read and not spec.get("read_root"):
            spec["read_root"] = d.read
        if d.write and not spec.get("write_root"):
            spec["write_root"] = d.write
