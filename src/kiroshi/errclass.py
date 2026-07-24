"""Shared, pure error classifier -- permanent vs transient.

Zero-dependency by design: imported by both pool.py (inside every spawned
WORKER process) and failure_breaker.py, so it must never pull in anything
heavy (no requests/torch/etc).

Bias hard toward "transient": a false "transient" classification just means
the existing retry-then-requeue behavior applies (today's status quo,
harmless). A false "permanent" classification would wrongly fail a sub-job
that might have actually succeeded on retry. Only add a signature to
_PERMANENT_SIGNATURES if retrying it is GENUINELY always useless.
"""
from __future__ import annotations

from typing import Optional

# Conservative allowlist of DEFINITELY-permanent signatures (substring match,
# case-insensitive against the error string). Sourced from the 2026-07-21/22
# incident (a stale NAS credential) plus the io-gate's own fail-closed tokens
# (see AGENTS.md's io-gate table) -- both are "retrying this is guaranteed
# useless" cases, not guesses.
_PERMANENT_SIGNATURES = (
    "nt_status_logon_failure", "logonfailure", "0xc000006d",   # bad credential
    # Permission denied. The SMB layer surfaces this as "STATUS_ACCESS_DENIED"
    # / NtStatus "0xc0000022" (NO "nt_" prefix), so match the underscore form
    # and the hex too -- the 2026-07-23 live run hit exactly this (the kiroshi
    # user could auth but lacked write/mkdir permission on the output path) and
    # it was being misclassified transient, so the breaker only tripped slowly
    # via the window instead of fast-tripping and surfacing "fix permissions".
    "nt_status_access_denied", "access_denied", "accessdenied",
    "0xc0000022", "permissionerror",
    "no_smb_creds", "unclassified_nas",   # io-gate fail-closed tokens
)


def is_permanent(error_str: Optional[str]) -> bool:
    """True iff error_str matches a known-permanent signature. None/empty is
    never permanent (we can't classify what we can't see, and defaulting to
    transient is the safe direction)."""
    if not error_str:
        return False
    low = error_str.lower()
    return any(sig in low for sig in _PERMANENT_SIGNATURES)


# Local per-sub-job COMPUTE faults -- the pool's exact labels for a sub-job it
# abandoned/killed for exceeding its own timeout (and the collateral in-flight
# killed alongside it). These are NOT signals about a shared dependency (the
# NAS, a coordinator), so they must NOT feed the circuit breaker: a cluster of
# bad clips in one shard is per-clip bad data handled by the reaper + poison
# quarantine (Fix B/C, 2026-07-23), and stopping ALL leasing over it just idles
# the whole runner instead of grinding past the bad region. Matched EXACTLY
# (not substring) so a genuine dependency error like "connection timeout" is
# still classified transient and can still trip the breaker.
_LOCAL_COMPUTE_FAULTS = ("timeout", "pool_reset")


def is_local_compute_fault(error_str: Optional[str]) -> bool:
    return bool(error_str) and error_str.strip().lower() in _LOCAL_COMPUTE_FAULTS


def classify(status: str, error_str: Optional[str]) -> str:
    """Returns "ok" | "permanent" | "transient".

    status in ("ok", "skipped") -> "ok" (a real success).
    status == "requeue" -> "ok" (an eviction under pressure, not a fault --
    the sub-job was preempted, not tried and failed; matches jobstore's own
    "error is cleared, not a fault" treatment of requeue).
    A local per-sub-job compute fault (exact "timeout"/"pool_reset") -> "ok"
    for BREAKER purposes: it's handled by the reaper + poison quarantine, and
    is not evidence a shared dependency is failing. (This does not change
    is_permanent / pool.py retry behavior, which never saw these as permanent
    anyway.)
    Otherwise (error/failed): "permanent" if the error matches a known
    permanent signature, else "transient".
    """
    if status in ("ok", "skipped", "requeue"):
        return "ok"
    if is_local_compute_fault(error_str):
        return "ok"
    return "permanent" if is_permanent(error_str) else "transient"


def signature(error_str: Optional[str]) -> str:
    """A short, stable grouping key for "are these failures the SAME error?"
    -- used by the breaker to distinguish a systemic failure (one dominant
    signature) from scattered per-sub-job bad data (many distinct signatures).

    For a recognized permanent signature, returns that exact token (so e.g.
    both "NT_STATUS_LOGON_FAILURE" and "LogonFailure" strings collapse to the
    SAME group if they both happen to match the same underlying cause --
    though in practice each error string will only match one). Otherwise
    falls back to a normalized prefix of the message (strips digits/hex-ish
    noise that would otherwise make near-identical errors look distinct).
    """
    if not error_str:
        return "<none>"
    low = error_str.lower()
    for sig in _PERMANENT_SIGNATURES:
        if sig in low:
            return sig
    # Fallback: first ~40 chars, collapsed whitespace, as a coarse grouping
    # key for transient errors (e.g. "ConnectionResetError(...)" instances
    # with different socket details should still group together).
    normalized = " ".join(error_str.split())
    return normalized[:40]
