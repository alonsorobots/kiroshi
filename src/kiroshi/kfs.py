"""Logon-session-proof filesystem layer (local + SMB-over-TCP).

Why this exists
---------------
On Windows, an SSH key-auth session (and a service / Scheduled Task) runs as a
*network logon* with no DPAPI credential context. In that context the SMB
**redirector** cannot authenticate outward — the classic "double-hop" problem —
so ``open(r"\\\\nas\\share\\x")``, ``os.walk`` over a UNC path, mapped drives,
``cmdkey`` and persistent ``net use`` all fail. Interactive desktop logons don't
hit this, which is why "it works when I RDP in but not over SSH" is so common.

The fix used here is the state-of-the-art one for an application: don't use the
OS redirector for network shares at all. Talk SMB directly over TCP/445 with
**explicit credentials** via ``smbprotocol``. The same code then works
identically from interactive, SSH, and service contexts — and on Linux/macOS.

Routing
-------
``kfs`` exposes a tiny ``os``-like surface (``open``, ``exists``, ``makedirs``,
``walk``, ``remove``, ``atomic_write``). For a UNC path (``//server/share/...``
or ``\\\\server\\share\\...``) *when SMB credentials are configured*, it uses
``smbprotocol``; otherwise it falls back to plain ``os``/``open`` so local paths
(and credential-backed interactive UNC) keep working unchanged.

Credentials (never in the repo / logs)
---------------------------------------
Resolved per server, in order:
    1. ``KIROSHI_NAS_USER`` / ``KIROSHI_NAS_PASS``  (recommended; env survives
       every logon type — set Machine-scoped with ``setx /M`` for persistence)
    2. per-server override ``KIROSHI_NAS_USER_<SERVER>`` / ``..._PASS_<SERVER>``
    3. optional OS keyring (service ``kiroshi``) — convenience for *interactive*
       use only; note Windows keyring == Credential Manager, which is itself
       unreadable from a network logon, so do not rely on it over SSH.

Knobs
-----
    KIROSHI_SMB_AUTH     ntlm (default) | negotiate | kerberos
    KIROSHI_SMB_ENCRYPT  0 (default) | 1   — SMB3 payload encryption (AES). Off
                         by default for throughput on a trusted LAN; auth is
                         always protected regardless. Turn on for untrusted nets.
"""
from __future__ import annotations

import os
import random
import sys
import time
import uuid
from contextlib import contextmanager
from typing import IO, Iterator, Optional

# Lazily imported so the rest of kiroshi works without the optional `smb` extra.
_SMB_READY = False

# Atomic-promote (rename/replace) is the one step that races an *external* holder
# of the file — a virus scanner, an indexer, the SMB server settling a handle from
# a just-killed client, or (under at-least-once delivery) a second worker writing
# the same output. Those locks clear in seconds, so we retry the commit with
# jittered backoff instead of failing the whole gig on a transient "file in use".
_COMMIT_ATTEMPTS = max(1, int(os.environ.get("KIROSHI_COMMIT_ATTEMPTS", "6")))
_COMMIT_BASE_DELAY = float(os.environ.get("KIROSHI_COMMIT_BASE_DELAY", "0.5"))
_COMMIT_MAX_DELAY = float(os.environ.get("KIROSHI_COMMIT_MAX_DELAY", "8.0"))


def _commit_backoff(attempt: int) -> float:
    """Exponential backoff with full jitter (decorrelated), capped."""
    ceiling = min(_COMMIT_MAX_DELAY, _COMMIT_BASE_DELAY * (2 ** attempt))
    return random.uniform(_COMMIT_BASE_DELAY, max(_COMMIT_BASE_DELAY, ceiling))


def _commit_with_retry(promote, dst_exists, cleanup_tmp) -> str:
    """Run ``promote()`` (the rename/replace), retrying transient OS lock errors.

    Returns "written" if we promoted the file, or "exists" if a concurrent writer
    already produced ``dst`` (idempotent success — duplicate execution under
    at-least-once delivery is harmless). Re-raises the last error only if every
    attempt failed AND the destination still isn't there.
    """
    last: Optional[BaseException] = None
    for attempt in range(_COMMIT_ATTEMPTS):
        try:
            promote()
            return "written"
        except OSError as e:
            last = e
            # Another writer won the race (or our previous attempt's replace
            # actually landed): the output exists and is complete -> success.
            if dst_exists():
                cleanup_tmp()
                return "exists"
            if attempt == _COMMIT_ATTEMPTS - 1:
                break
            time.sleep(_commit_backoff(attempt))
    cleanup_tmp()
    assert last is not None
    raise last


# --------------------------------------------------------------------- detection
def is_unc(path: object) -> bool:
    """True for a UNC path: ``//server/share/...`` or ``\\\\server\\share\\...``."""
    s = str(path)
    return s.startswith("\\\\") or s.startswith("//")


def _to_unc(path: object) -> str:
    """Normalize a UNC path to backslash form that smbprotocol expects."""
    return str(path).replace("/", "\\")


def server_of(path: object) -> Optional[str]:
    """Extract the server component of a UNC path, else ``None``."""
    if not is_unc(path):
        return None
    parts = _to_unc(path).lstrip("\\").split("\\", 1)
    return parts[0] or None


def _sanitize(server: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in server).upper()


# ------------------------------------------------------------------- credentials
def creds_for(server: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve (username, password) for ``server`` from env, then keyring."""
    sfx = _sanitize(server)
    user = os.environ.get(f"KIROSHI_NAS_USER_{sfx}") or os.environ.get("KIROSHI_NAS_USER")
    pw = os.environ.get(f"KIROSHI_NAS_PASS_{sfx}") or os.environ.get("KIROSHI_NAS_PASS")
    if user and pw:
        return user, pw
    # Optional keyring fallback (interactive use only; see module docstring).
    try:
        import keyring  # type: ignore

        if user is None:
            user = keyring.get_password("kiroshi", f"{server}:user")
        if user:
            pw = pw or keyring.get_password("kiroshi", server)
    except Exception:  # noqa: BLE001 - keyring optional / may be unavailable
        pass
    return user, pw


def have_creds(server: str) -> bool:
    user, pw = creds_for(server)
    return bool(user and pw)


def use_smb(path: object) -> bool:
    """Whether to route ``path`` through smbprotocol rather than the OS.

    UNC + credentials configured for that server. Local paths and uncredentialed
    UNC (e.g. an interactive session relying on the redirector) use the OS.
    """
    server = server_of(path)
    return bool(server and have_creds(server))


# ----------------------------------------------------------------- smb internals
def _auth_protocol() -> str:
    return (os.environ.get("KIROSHI_SMB_AUTH") or "ntlm").strip().lower()


def _encrypt() -> bool:
    return (os.environ.get("KIROSHI_SMB_ENCRYPT") or "0").strip().lower() in {"1", "true", "yes", "on"}


_PATCHED = False
_REGISTERED: set[str] = set()


def _force_ntlm_patch() -> None:
    """Make smbprotocol use pyspnego's pure-Python NTLM.

    smbprotocol calls ``spnego.client(options=session_key, ...)`` which, on
    Windows, auto-selects SSPI. SSPI rejects a *non-domain* account (a Samba/NAS
    user) with SEC_E_UNKNOWN_CREDENTIALS even when the password is correct. ORing
    in ``use_ntlm`` forces the cross-platform NTLM path that authenticates with
    the explicit username/password directly (same as Linux ``smbclient``).
    """
    global _PATCHED
    if _PATCHED:
        return
    import spnego  # type: ignore
    import smbprotocol.session as _sess  # type: ignore

    _orig = spnego.client

    def _client(*a, **k):  # noqa: ANN002, ANN003
        opts = k.get("options", spnego.NegotiateOptions(0))
        k["options"] = opts | spnego.NegotiateOptions.use_ntlm
        return _orig(*a, **k)

    _sess.spnego.client = _client
    _PATCHED = True


def _ensure_session(server: str) -> None:
    global _SMB_READY
    if server in _REGISTERED:
        return
    try:
        import smbclient  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "SMB path requested but smbprotocol is not installed. "
            "Install with: pip install 'kiroshi[smb]'"
        ) from e
    user, pw = creds_for(server)
    if not (user and pw):
        raise RuntimeError(
            f"No SMB credentials for server {server!r}. Set KIROSHI_NAS_USER / "
            f"KIROSHI_NAS_PASS (or per-server KIROSHI_NAS_USER_{_sanitize(server)})."
        )
    if _auth_protocol() == "ntlm":
        _force_ntlm_patch()
    smbclient.register_session(
        server,
        username=user,
        password=pw,
        encrypt=_encrypt(),
        auth_protocol=_auth_protocol(),
    )
    _REGISTERED.add(server)
    _SMB_READY = True


def _smbclient():  # noqa: ANN202
    import smbclient  # type: ignore

    return smbclient


# --------------------------------------------------------------------- os-like API
def exists(path: object) -> bool:
    if use_smb(path):
        _ensure_session(server_of(path))  # type: ignore[arg-type]
        return _smbclient().path.exists(_to_unc(path))
    return os.path.exists(str(path))


def makedirs(path: object, exist_ok: bool = True) -> None:
    if use_smb(path):
        _ensure_session(server_of(path))  # type: ignore[arg-type]
        _smbclient().makedirs(_to_unc(path), exist_ok=exist_ok)
        return
    os.makedirs(str(path), exist_ok=exist_ok)


def remove(path: object) -> None:
    if use_smb(path):
        _ensure_session(server_of(path))  # type: ignore[arg-type]
        _smbclient().remove(_to_unc(path))
        return
    os.remove(str(path))


def open(path: object, mode: str = "rb", **kwargs) -> IO:  # noqa: A001 - mirror builtins.open
    if use_smb(path):
        _ensure_session(server_of(path))  # type: ignore[arg-type]
        return _smbclient().open_file(_to_unc(path), mode=mode, **kwargs)
    import builtins

    return builtins.open(str(path), mode, **kwargs)


def walk(top: object):  # noqa: ANN201 - yields like os.walk
    if use_smb(top):
        _ensure_session(server_of(top))  # type: ignore[arg-type]
        yield from _smbclient().walk(_to_unc(top))
    else:
        yield from os.walk(str(top))


def _parent(unc: str) -> str:
    return unc.rsplit("\\", 1)[0]


@contextmanager
def atomic_write(path: object, fsync: bool = True) -> Iterator[IO[bytes]]:
    """Yield a binary handle; atomically promote to ``path`` on clean exit.

    Works for local paths (temp + ``os.replace`` + fsync) and SMB paths (temp on
    the same share + server-side replace). A crash mid-write leaves only a stray
    ``.tmp``, never a half-written output that would fool the resume check.
    """
    if use_smb(path):
        sc = _smbclient()
        _ensure_session(server_of(path))  # type: ignore[arg-type]
        dst = _to_unc(path)
        sc.makedirs(_parent(dst), exist_ok=True)
        tmp = f"{_parent(dst)}\\.{dst.rsplit(chr(92), 1)[-1]}.{uuid.uuid4().hex}.tmp"
        ok = False

        def _dst_exists() -> bool:
            try:
                return bool(sc.path.exists(dst))
            except OSError:
                return False

        def _cleanup_tmp() -> None:
            try:
                sc.remove(tmp)
            except Exception:  # noqa: BLE001
                pass

        def _promote() -> None:
            # rename with replace-if-exists semantics
            try:
                sc.replace(tmp, dst)
            except AttributeError:  # older smbclient without replace()
                if sc.path.exists(dst):
                    sc.remove(dst)
                sc.rename(tmp, dst)

        try:
            with sc.open_file(tmp, mode="wb") as fh:
                yield fh
            ok = True
        finally:
            if ok:
                _commit_with_retry(_promote, _dst_exists, _cleanup_tmp)
            else:
                _cleanup_tmp()
        return

    # Local branch
    import tempfile

    p = str(path)
    parent = os.path.dirname(p) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(p)}.", suffix=".tmp", dir=parent)
    wrote = False
    try:
        with os.fdopen(fd, "wb") as fh:
            yield fh
            fh.flush()
            if fsync:
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        wrote = True
        # Windows can raise a transient sharing violation on replace too (AV /
        # Search indexer holding the freshly-written temp). Same retry policy.
        _commit_with_retry(
            lambda: os.replace(tmp, p),
            lambda: os.path.exists(p),
            lambda: os.path.exists(tmp) and os.unlink(tmp),
        )
    finally:
        if not wrote and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def backend(path: object) -> str:
    """\"smb\" or \"os\" — handy for diagnostics/doctor output."""
    return "smb" if use_smb(path) else "os"


def smb_diagnostics(server: str) -> dict:
    """Non-secret summary for ``kiroshi doctor`` (never returns the password)."""
    user, pw = creds_for(server)
    return {
        "server": server,
        "have_creds": bool(user and pw),
        "user": user or None,
        "auth": _auth_protocol(),
        "encrypt": _encrypt(),
        "smbprotocol_importable": _smbprotocol_importable(),
        "platform_network_logon_note": (
            "smbprotocol bypasses the Windows redirector, so this works from "
            "SSH/service network logons too"
            if sys.platform == "win32"
            else "n/a (non-Windows)"
        ),
    }


def _smbprotocol_importable() -> bool:
    try:
        import smbclient  # type: ignore  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False
