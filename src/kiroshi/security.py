"""Mesh authentication — a shared bearer token gating the Fixer's HTTP API.

Why this is mandatory (unlike at-field, which binds localhost and skips auth):
Kiroshi is a *mesh*. The Fixer must bind a routable address so Runners on other
machines can reach it, which means the coordination API is exposed on the LAN.
Every endpoint either hands out work to execute, accepts results, mutates the
queue, or discloses topology (hostnames, file paths in error strings). On an
**open-source** project the wire protocol is public knowledge, so an unauthecated
``0.0.0.0`` API is a LAN-local DoS / work-tampering / info-disclosure surface.

So all data + control endpoints require a shared **mesh token**:

    Authorization: Bearer <token>      (preferred)
    X-Kiroshi-Token: <token>           (header alt)
    ?token=<token>                     (query, for browser/EventSource convenience)

Token resolution (first hit wins):
    1. explicit argument / ``--token``
    2. ``KIROSHI_TOKEN`` env var
    3. token file in the state dir (``mesh.token``)
    4. (Fixer only) auto-generate a strong token, persist it, and print it once

The token is compared in constant time. It is **never** placed in the discovery
beacon (that is LAN-broadcast); cross-machine Runners receive it out-of-band
(shown in the dashboard/tray, or set via env) — a one-time "mesh join code".
"""
from __future__ import annotations

import hmac
import os
import secrets
import stat
import sys
from typing import Optional

from .appstate import state_dir

TOKEN_FILENAME = "mesh.token"
_ENV = "KIROSHI_TOKEN"


def token_path() -> str:
    return str(state_dir() / TOKEN_FILENAME)


def _read_token_file() -> Optional[str]:
    p = token_path()
    try:
        with open(p, encoding="utf-8") as f:
            val = f.read().strip()
        return val or None
    except OSError:
        return None


def _write_token_file(token: str) -> None:
    p = token_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        # Best-effort tighten perms (owner-only) on POSIX; on Windows the
        # ProgramData ACL already restricts non-admins from writing.
        if sys.platform != "win32":
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def generate_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def resolve_token(explicit: Optional[str] = None) -> Optional[str]:
    """Return the configured mesh token without creating one. ``None`` = no auth."""
    if explicit:
        return explicit.strip() or None
    env = os.environ.get(_ENV)
    if env and env.strip():
        return env.strip()
    return _read_token_file()


def ensure_fixer_token(explicit: Optional[str] = None,
                       allow_insecure: bool = False) -> Optional[str]:
    """Resolve the token for the Fixer, auto-generating + persisting if absent.

    Returns ``None`` only when the operator explicitly opted out (``allow_insecure``
    with no explicit token) — used to run a wide-open dev mesh on a trusted LAN.
    An explicit ``--no-auth`` wins over an *ambient* token (env / persisted
    ``mesh.token``): the operator asked for no auth, so a stale persisted token from
    a previous service install must not silently re-enable it.
    """
    if explicit:
        return explicit.strip() or None
    if allow_insecure:
        return None
    tok = resolve_token(None)
    if tok:
        return tok
    tok = generate_token()
    _write_token_file(tok)
    return tok


def token_matches(configured: Optional[str], presented: Optional[str]) -> bool:
    """Constant-time compare. If no token is configured, auth is disabled (True)."""
    if not configured:
        return True
    if not presented:
        return False
    return hmac.compare_digest(configured, presented)


def prove(token: str, nonce: str) -> str:
    """Return HMAC-SHA256(token, nonce) as hex — the Fixer's proof that it holds
    the shared mesh token, without ever revealing the token itself."""
    return hmac.new(token.encode("utf-8"), nonce.encode("utf-8"),
                    "sha256").hexdigest()


def verify_proof(token: Optional[str], nonce: str, proof: Optional[str]) -> bool:
    """Constant-time check that ``proof`` is a valid HMAC of ``nonce`` under
    ``token``. Used by a Runner to authenticate the *Fixer* (mutual auth) before
    sending its bearer token or executing any leased gig. Fails closed."""
    if not token or not nonce or not proof:
        return False
    return hmac.compare_digest(prove(token, nonce), proof)


def new_nonce(nbytes: int = 16) -> str:
    return secrets.token_hex(nbytes)


def extract_presented_token(headers: dict, query_token: Optional[str]) -> Optional[str]:
    """Pull a presented token from request headers/query (case-insensitive)."""
    # Normalize header keys
    low = {k.lower(): v for k, v in headers.items()}
    auth = low.get("authorization")
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return auth.strip()
    xtok = low.get("x-kiroshi-token")
    if xtok:
        return xtok.strip()
    if query_token:
        return query_token.strip()
    return None
