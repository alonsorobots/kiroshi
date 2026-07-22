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
    "nt_status_access_denied", "accessdenied", "permissionerror",  # perms
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


def classify(status: str, error_str: Optional[str]) -> str:
    """Returns "ok" | "permanent" | "transient".

    status in ("ok", "skipped") -> "ok" (a real success).
    status == "requeue" -> "ok" (an eviction under pressure, not a fault --
    the sub-job was preempted, not tried and failed; matches jobstore's own
    "error is cleared, not a fault" treatment of requeue).
    Otherwise (error/failed): "permanent" if the error matches a known
    permanent signature, else "transient".
    """
    if status in ("ok", "skipped", "requeue"):
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
