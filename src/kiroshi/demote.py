"""kiroshi.demote — write-back mover: NVMe cache → deterministic sharded HDD.

A demote job copies files that were written to a fast NVMe cache tier onto the
HDD parity array in a **deterministic per-spindle shard layout**, so later reads
spread across every head instead of hammering one. It is meant to be seeded
*idle-gated* (see idlegate.py + docs/DEMOTE.md) so it sits quiet and only flushes
when the array is free.

Placement is **stable by construction**: each file's destination spindle is a
hash of its relative path (``disk = 1 + sha1(rel) % n_disks``). No plan file to
keep in sync, and re-running is idempotent — a file always lands on the same disk
regardless of what else is in the batch. (For byte-balanced placement of a fixed
corpus, ``kiroshi nas shard`` + a frozen plan is the alternative; hashing is the
right default for an incremental write-back stream of many small files.)

Deterministic sharding *requires* writing direct ``/mnt/diskN`` paths (mergerfs
won't honor a custom shard layout), which the iohint write-danger gate refuses by
default. The demote mover therefore writes ``direct_disk_write=True`` into each
spec on purpose: it owns the shard routing, so bypassing FUSE is intended here.

Task ABI: module-level ``enumerate_gigs(args)`` (fan out one copy per file) and
``run(spec)`` (copy one file, idempotent). Bind a runner to ``kiroshi.demote:run``.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from typing import Any, Iterator, Optional

from . import kfs
from . import paths as kpaths
from .staging import _copy_file

# Unraid convention on Alexandria: LubuN lives on diskN, and the mergerfs union
# view is /mnt/user. So the *direct* per-spindle root for the k-th shard of a
# dataset dir seen at /mnt/user/Lubu*/<rest> is /mnt/diskK/LubuK/<rest>.
_LUBU_GLOB_RE = re.compile(r"^(?P<prefix>.*[/\\])?(?P<word>[^/\\*]+)\*(?P<rest>[/\\].*)?$")


def expand_lubu_glob(dest_glob: str, *, union_mount: str = "/mnt/user") -> str:
    """Turn a globbed mergerfs destination into a physical per-disk template.

    ``/mnt/user/Lubu*/MonologDataset`` -> ``/mnt/disk{n}/Lubu{n}/MonologDataset``

    The ``*`` in the glob'd path component (e.g. ``Lubu*``) means "one dir per
    spindle"; the union mount ``/mnt/user`` is swapped for the direct ``/mnt/disk{n}``
    mount. Returns a template string with a ``{n}`` placeholder (1-based disk #).

    If the input already contains ``{n}`` it is returned unchanged (explicit
    template). If it doesn't match the Lubu-glob shape, a best-effort ``*``->``{n}``
    substitution is applied and the prefix is left as-is (caller should verify).
    """
    if "{n}" in dest_glob:
        return dest_glob
    norm = dest_glob.replace("\\", "/").rstrip("/")
    m = _LUBU_GLOB_RE.match(norm)
    if not m:
        # No recognizable glob component — nothing to fan out.
        raise ValueError(
            f"demote destination {dest_glob!r} has no '*' shard component and no "
            f"'{{n}}' placeholder; pass e.g. '/mnt/user/Lubu*/Dataset' or an "
            f"explicit template '/mnt/disk{{n}}/Lubu{{n}}/Dataset'.")
    word = m.group("word")
    rest = m.group("rest") or ""
    prefix = (m.group("prefix") or "").rstrip("/")
    um = union_mount.replace("\\", "/").rstrip("/")
    # Swap the union mount prefix for the direct per-disk mount when present.
    if prefix == um:
        return f"/mnt/disk{{n}}/{word}{{n}}{rest}"
    # Unknown prefix: keep it, just expand the glob component.
    return f"{prefix}/{word}{{n}}{rest}" if prefix else f"{word}{{n}}{rest}"


def disk_dest_root(dest_tmpl: str, disk_no: int) -> str:
    """Physical destination root for the k-th spindle (1-based)."""
    return dest_tmpl.format(n=disk_no)


def assign_disk(rel: str, n_disks: int) -> int:
    """Stable 1-based spindle assignment for a relative path (hash mod N).

    Deterministic across runs/processes (unlike ``hash()``), so demotion is
    idempotent and a file always lands on the same disk.
    """
    h = hashlib.sha1(rel.replace("\\", "/").encode("utf-8")).hexdigest()
    return 1 + (int(h[:8], 16) % max(1, n_disks))


def _rel_of(full: str, root: str) -> str:
    f = str(full).replace("\\", "/").rstrip("/")
    b = str(root).replace("\\", "/").rstrip("/")
    return f[len(b):].lstrip("/") if f.startswith(b) else f


def enumerate_gigs(args: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield one copy sub-job per file under ``args['from']``.

    Args:
        from:     NVMe source root (walked here, on the launcher).
        to:       destination glob (``/mnt/user/Lubu*/X``) or template
                  (``/mnt/disk{n}/Lubu{n}/X``).
        n_disks:  number of spindles (default 7).
        pattern:  filename glob filter (default ``*``).
        union_mount: mergerfs union mount to strip (default ``/mnt/user``).

    Each sub-job carries ``disk="diskK"`` (so the coordinator budgets per-spindle
    and the idle gate's disk set lines up) and ``direct_disk_write=True`` (the
    deliberate FUSE-bypass ack for deterministic sharding).
    """
    src_root = str(args["from"]).rstrip("/\\")
    n_disks = int(args.get("n_disks") or 7)
    pattern = args.get("pattern") or "*"
    dest_tmpl = expand_lubu_glob(str(args["to"]),
                                 union_mount=args.get("union_mount") or "/mnt/user")

    for dirpath, _dirs, files in kfs.walk(src_root):
        for fn in files:
            if not fnmatch.fnmatch(fn, pattern):
                continue
            sep = "\\" if kfs.is_unc(src_root) else os.sep
            full = str(dirpath).rstrip("/\\") + sep + fn
            rel = _rel_of(full, src_root)
            k = assign_disk(rel, n_disks)
            yield {
                "subjob_id": rel,
                "disk": f"disk{k}",
                "spec": {
                    "src_path": rel,
                    "dst_path": rel,
                    "read_root": src_root,
                    "write_root": disk_dest_root(dest_tmpl, k),
                    "direct_disk_write": True,
                },
            }


def run(spec: dict[str, Any]) -> dict[str, Any]:
    """Copy one file NVMe→HDD-shard, idempotently (skip if dst exists, same size).

    Writes a direct ``/mnt/diskN`` path on purpose (deterministic sharding).
    """
    read_root = kpaths.gig_read_root(spec) or os.environ.get("KIROSHI_READ_ROOT")
    write_root = kpaths.gig_write_root(spec) or os.environ.get("KIROSHI_WRITE_ROOT")
    if not read_root or not write_root:
        raise RuntimeError(
            "demote.run: spec has no read_root/write_root and "
            "KIROSHI_READ_ROOT/KIROSHI_WRITE_ROOT are unset")
    src = kpaths.confined_join(read_root, spec["src_path"])
    dst = kpaths.confined_join(write_root, spec["dst_path"])

    if kfs.exists(dst):
        try:
            src_sz = os.path.getsize(src) if kfs.exists(src) else -1
            if src_sz >= 0 and os.path.getsize(dst) == src_sz:
                return {"status": "skipped",
                        "metrics": {"reason": "exists", "bytes": src_sz}}
        except OSError:
            pass  # size check failed — re-copy to be safe

    try:
        n = _copy_file(src, dst)
    except FileNotFoundError:
        parent = dst.rsplit("/", 1)[0].rsplit("\\", 1)[0]
        kfs.makedirs(parent, exist_ok=True)
        n = _copy_file(src, dst)
    return {"status": "ok", "metrics": {"bytes": n}}
