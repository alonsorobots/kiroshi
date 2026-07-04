"""kiroshi.iohint — path-aware I/O guidance derived from the storage topology.

The problem this solves: whoever creates a Kiroshi job (a human, or an LLM
"vibe-coding" a task) can't be expected to hold every static storage fact in
their head — that NVMe beats an HDD parity array, that a parity write is a
read-modify-write through one spindle, that reading via the FUSE ``/mnt/user``
pool serializes across disks, that a UNC path with no SMB creds silently falls
back to the flaky Windows redirector. Kiroshi already *knows* all of this from
the ``[[storage.disk]]`` topology (:mod:`kiroshi.storage`) and the SMB-creds
state (:mod:`kiroshi.kfs`). This module turns a job's declared input/output
*paths* into plain-language advice, surfaced at the moment the job is created —
the "remind them at the right time" nudge.

Design:
  * **Pure + static.** No I/O, no network, no benchmarking. These facts don't
    change per job, so we classify once from paths + config, never measure.
  * **Zero task changes.** We infer everything from the spec paths Kiroshi
    already sees (``read_root`` / ``write_root`` / ``src_path`` / ``dst_path``).
  * **One source of truth.** doctor, the MCP ``advise_io`` tool, the seed/run
    preflight, and ``remote`` all call in here so the wording never drifts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .storage import DiskConfig, kind_default_concurrency, match_disk


# ---------------------------------------------------------------- data model
@dataclass
class Finding:
    """A single piece of advice about a job's I/O paths."""
    level: str      # "ok" (confirmation / fast path) | "warn" (actionable improvement)
    code: str       # stable machine slug, e.g. "input.hdd", "read.not_direct"
    message: str    # human-readable, paste-ready guidance

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass
class Advice:
    findings: list[Finding] = field(default_factory=list)

    @property
    def severity(self) -> str:
        return "warn" if any(f.level == "warn" for f in self.findings) else "ok"

    def add(self, level: str, code: str, message: str) -> None:
        self.findings.append(Finding(level, code, message))

    def lines(self) -> list[str]:
        return [f"[{f.level.upper()}] {f.code}: {f.message}" for f in self.findings]

    def as_dict(self) -> dict[str, Any]:
        return {"severity": self.severity,
                "findings": [f.as_dict() for f in self.findings]}


# ------------------------------------------------------------------- helpers
def _norm(path: Optional[str]) -> str:
    """Comparable form: forward slashes, lower-case, no trailing separator."""
    if not path:
        return ""
    return str(path).replace("\\", "/").rstrip("/").lower()


def _is_fuse_pool(path: Optional[str]) -> bool:
    """Unraid/mergerfs FUSE pool (``/mnt/user``) — correct, but serializes reads
    across every spindle. A direct per-disk share is far faster for sequential
    reads."""
    return "/mnt/user" in _norm(path)


def _disk_by_match(text: str, disks: list[DiskConfig]) -> Optional[DiskConfig]:
    """First disk whose ``match`` rule hits the text (subjob_id / relative path)."""
    for d in disks:
        if d.match and match_disk(text, d.match):
            return d
    return None


def _disk_by_share(root: Optional[str], disks: list[DiskConfig]) -> Optional[DiskConfig]:
    """First disk whose declared ``read`` / ``write`` / ``direct_path`` share is a
    prefix of ``root`` — matches a bare root (``//nas/disk1_direct/...``) that
    carries no shard token for ``match`` to catch."""
    r = _norm(root)
    if not r:
        return None
    for d in disks:
        for share in (d.read, d.write, d.direct_path):
            s = _norm(share)
            if s and (r == s or r.startswith(s + "/")):
                return d
    return None


def _resolve_disk(root: Optional[str], sample: Optional[str], subjob_id: str,
                  disks: list[DiskConfig]) -> Optional[DiskConfig]:
    haystack = " ".join(x for x in (subjob_id, root, sample) if x)
    return _disk_by_match(haystack, disks) or _disk_by_share(root, disks)


def _uses_direct_share(root: Optional[str], disk: DiskConfig) -> bool:
    """True if ``root`` is already the disk's direct per-spindle read share (or its
    raw ``direct_path``), i.e. the fast read path."""
    r = _norm(root)
    if not r:
        return False
    for fast in (disk.read, disk.direct_path):
        s = _norm(fast)
        if s and (r == s or r.startswith(s + "/")):
            return True
    return False


def _is_direct_disk_write(write_root: Optional[str], disk: DiskConfig) -> bool:
    """True if ``write_root`` targets a RAW/direct disk location (the ``direct_path``,
    or the direct per-spindle read share when a distinct cached write share exists),
    bypassing the pooled user/cached share. Fast for reads, but a data-loss footgun
    for WRITES on a FUSE-pooled array: mixing ``/mnt/diskN`` and ``/mnt/user`` access
    to the same files creates shadowed/duplicate files."""
    w = _norm(write_root)
    if not w:
        return False
    dp = _norm(disk.direct_path)
    if dp and (w == dp or w.startswith(dp + "/")):
        return True
    rd, wr = _norm(disk.read), _norm(disk.write)
    if rd and wr and rd != wr and (w == rd or w.startswith(rd + "/")):
        return True
    return False


def _creds_state(path: Optional[str]) -> tuple[Optional[str], bool]:
    """``(server, have_creds)``; ``server`` is None for non-UNC (local) paths."""
    from . import kfs
    server = kfs.server_of(path) if path else None
    if not server:
        return None, False
    return server, kfs.have_creds(server)


def _is_nas(path: Optional[str]) -> bool:
    """True for a UNC/NAS path (``//server/share/...``). Local paths are False."""
    from . import kfs
    return bool(path) and kfs.server_of(path) is not None


# ----------------------------------------------------------------- the advisor
def advise_job(
    *,
    read_root: Optional[str] = None,
    write_root: Optional[str] = None,
    sample_src: Optional[str] = None,
    sample_dst: Optional[str] = None,
    subjob_id: str = "",
    disks: Optional[list[DiskConfig]] = None,
) -> Advice:
    """Classify a job's declared input/output paths and return static fast-path
    guidance. Everything is best-effort: unknown paths simply yield fewer
    findings, never an error."""
    disks = disks or []
    adv = Advice()

    if not disks:
        adv.add("ok", "topology.none",
                "No [[storage.disk]] topology is configured, so Kiroshi can't tell "
                "HDD from NVMe or route reads to a direct spindle share. If this job "
                "touches a multi-disk NAS, declare the topology so leasing and dual-"
                "path routing become storage-aware.")

    # ---- transport: UNC without SMB creds falls back to the OS redirector ----
    seen_servers: set[str] = set()
    for kind, root in (("read root", read_root), ("write root", write_root)):
        server, ok = _creds_state(root)
        if server and server not in seen_servers:
            seen_servers.add(server)
            if not ok:
                adv.add("warn", "smb.no_creds",
                        f"{kind} {root!r} is a UNC share on {server!r} but no SMB "
                        f"credentials are set (KIROSHI_NAS_USER/PASS). Kiroshi will "
                        f"fall back to the Windows redirector: slower, and unable to "
                        f"authenticate from a service or SSH (network) logon. Set "
                        f"creds to use the direct smbprotocol data plane.")

    # ---- input storage class -------------------------------------------------
    in_disk = _resolve_disk(read_root, sample_src, subjob_id, disks)
    if in_disk is not None:
        conc = in_disk.effective_concurrency
        kind = (in_disk.kind or "").lower()
        if kind == "hdd":
            adv.add("warn", "input.hdd",
                    f"Inputs resolve to an HDD (disk {in_disk.id!r}). HDDs are seek-"
                    f"bound: shard the dataset across spindles for parallel I/O and "
                    f"keep per-disk read concurrency near {conc} (Kiroshi's HDD "
                    f"default) — over-parallelizing one disk thrashes the heads and "
                    f"goes slower.")
        elif kind in ("nvme", "ssd"):
            adv.add("ok", f"input.{kind}",
                    f"Inputs are on {kind.upper()} (disk {in_disk.id!r}) — already the "
                    f"fast path. No seek penalty; push read concurrency high (~{conc}).")
        # direct-share-available-but-unused (only meaningful for a spindle disk)
        if in_disk.read and not _uses_direct_share(read_root, in_disk) \
                and (_is_fuse_pool(read_root) or _disk_by_share(read_root, disks) is in_disk):
            adv.add("warn", "read.not_direct",
                    f"read_root {read_root!r} isn't the direct per-spindle share. "
                    f"Disk {in_disk.id!r} exposes a faster direct read share at "
                    f"{in_disk.read!r}"
                    + (f" (raw {in_disk.direct_path!r})" if in_disk.direct_path else "")
                    + ". Reading via the FUSE/cache pool serializes across spindles — "
                    "set read_root to the direct share.")
    elif read_root or sample_src:
        if _is_nas(read_root) or _is_nas(sample_src):
            # A NAS path we can't classify: we can't route it to a direct spindle
            # share or budget the disk. Gateable — see gate().
            adv.add("warn", "input.unclassified_nas",
                    f"Inputs are on a NAS/UNC path ({read_root or sample_src!r}) that "
                    f"matches no [[storage.disk]] rule, so Kiroshi can't route them to "
                    f"a direct spindle share or budget the spindle. Add a topology "
                    f"match rule for this data so I/O is spindle-aware.")
        else:
            adv.add("ok", "input.unknown",
                    "Inputs don't match any configured storage disk (they look "
                    "local/non-NAS). No storage-class advice available.")

    # ---- output storage class ------------------------------------------------
    out_disk = _resolve_disk(write_root, sample_dst, subjob_id, disks)
    if out_disk is not None:
        kind = (out_disk.kind or "").lower()
        if out_disk.parity_protected:
            wconc = out_disk.effective_write_concurrency
            cache = f" A {out_disk.cache_tier.upper()} cache tier is configured — " \
                    f"write there and let the array absorb it in the background." \
                    if out_disk.cache_tier else ""
            adv.add("warn", "output.parity",
                    f"Outputs land on a parity-protected array (disk {out_disk.id!r}). "
                    f"Every write is a read-modify-write through the parity spindle — "
                    f"a fleet-global bottleneck, not per-disk. Keep write concurrency "
                    f"modest (~{wconc}).{cache}")
        elif kind in ("nvme", "ssd"):
            adv.add("ok", f"output.{kind}",
                    f"Outputs on {kind.upper()} (disk {out_disk.id!r}) — fast small-"
                    f"file writes, no parity penalty.")
        if _is_direct_disk_write(write_root, out_disk):
            adv.add("warn", "output.direct_disk_write",
                    f"write_root {write_root!r} targets a RAW/direct location on disk "
                    f"{out_disk.id!r}, bypassing the pooled user/cached share. On a "
                    f"FUSE-pooled array (Unraid/mergerfs), mixing direct-disk and pool "
                    f"access to the same files creates shadowed/duplicate files and "
                    f"can LOSE DATA. Prefer the cached write share ({out_disk.write!r})"
                    ". Only write direct if you fully control placement and never "
                    "touch these paths through the pool.")
    elif (write_root or sample_dst) and (_is_nas(write_root) or _is_nas(sample_dst)):
        adv.add("warn", "output.unclassified_nas",
                f"Outputs go to a NAS/UNC path ({write_root or sample_dst!r}) that "
                f"matches no [[storage.disk]] rule, so Kiroshi can't budget the "
                f"spindle or protect a parity array. Add a topology match rule.")

    return adv


# ------------------------------------------------------------------ the gate
# Fail-closed policy: a job whose I/O is on a *genuine trade-off* slow path
# (not something Kiroshi can silently fix) does not run unless the creator
# acknowledges that specific trade-off. The gate NEVER mutates a declared path
# — it refuses and re-transmits the fast alternative, so the spec always matches
# reality (see the "never-mutate" discussion). Blocking codes → the ack token
# that clears them; everything else is advisory only.
_BLOCKING: dict[str, str] = {
    "smb.no_creds": "no_smb_creds",
    "read.not_direct": "no_direct_share",
    "output.parity": "parity_write",
    "output.direct_disk_write": "direct_disk_write",
    "input.unclassified_nas": "unclassified_nas",
    "output.unclassified_nas": "unclassified_nas",
}


@dataclass
class GateResult:
    blocked: bool
    reasons: list[Finding]        # unacknowledged blocking findings
    acknowledged: list[str]       # ack tokens that were supplied + actually used

    def tokens(self) -> list[str]:
        """The ack tokens still needed to let this job through."""
        seen: list[str] = []
        for f in self.reasons:
            tok = _BLOCKING.get(f.code)
            if tok and tok not in seen:
                seen.append(tok)
        return seen


def gate(adv: Advice, acks=None) -> GateResult:
    """Decide whether an :class:`Advice` blocks job creation. ``acks`` is the set
    of trade-off tokens the creator explicitly acknowledged."""
    have = set(acks or [])
    reasons: list[Finding] = []
    used: set[str] = set()
    for f in adv.findings:
        tok = _BLOCKING.get(f.code)
        if not tok:
            continue
        if tok in have:
            used.add(tok)
        else:
            reasons.append(f)
    return GateResult(blocked=bool(reasons), reasons=reasons,
                      acknowledged=sorted(used))


def gate_enabled() -> bool:
    """The gate is on by default. ``KIROSHI_IO_GATE=0`` is the emergency override
    (a last resort for a false positive — prefer a specific --io-ack token)."""
    import os
    return (os.environ.get("KIROSHI_IO_GATE") or "1").strip().lower() \
        not in {"0", "false", "no", "off"}


def block_message(res: GateResult, *, ack_syntax: str = "--io-ack") -> str:
    """Actionable refusal text: what's slow, why, and the exact token to proceed.
    ``ack_syntax`` lets the CLI say ``--io-ack X`` and MCP say ``io_ack=['X']``."""
    lines = ["[io-gate] Refusing to create this job — its I/O is not on the "
             "fast path:", ""]
    for f in res.reasons:
        tok = _BLOCKING.get(f.code, "?")
        lines.append(f"  - {f.code}: {f.message}")
        lines.append(f"      acknowledge this trade-off with: {ack_syntax} {tok}")
        lines.append("")
    lines.append("Preferred: fix the path(s) above so the job is actually fast. "
                 "Otherwise add the token(s) to run anyway (a deliberate, recorded "
                 "choice). Emergency override for a false positive: KIROSHI_IO_GATE=0.")
    return "\n".join(lines)


def classify_root(root: Optional[str], disks: Optional[list[DiskConfig]] = None,
                  sample: Optional[str] = None) -> dict[str, Any]:
    """Lightweight single-root classification for callers that just want the
    facts (doctor's per-root line, dashboards). Returns kind/disk/parity/creds."""
    disks = disks or []
    disk = _resolve_disk(root, sample, "", disks)
    server, ok = _creds_state(root)
    return {
        "root": root,
        "disk": disk.id if disk else None,
        "kind": disk.kind if disk else None,
        "parity": bool(disk.parity_protected) if disk else False,
        "direct_available": bool(disk and disk.read and not _uses_direct_share(root, disk)),
        "is_unc": server is not None,
        "have_creds": ok,
    }
