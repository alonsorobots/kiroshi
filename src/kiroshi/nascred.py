"""Coordinator-brokered NAS credential — encrypted at rest, sealed in transit.

The problem
-----------
A headless mesh needs the NAS SMB credential on every node so ``kfs`` can talk
SMB directly (``smbprotocol``) in *any* logon context — service, SSH/network,
scheduled task, interactive. Storing the password as plaintext machine env
(``setx /M``) works everywhere but is cleartext in the registry. User-scoped
DPAPI (Credential Manager / keyring) is encrypted but *fails headless* — the
user key isn't loaded under a network/service logon. gMSA (the textbook answer)
needs Active Directory, which a workgroup + Unraid/Samba mesh doesn't have.

The design (broker)
-------------------
The secret lives **encrypted at rest on exactly one node — the Coordinator** —
and is handed to authenticated Runners *in memory* at startup, which inject it
into their own process env for ``smbprotocol`` and **never persist it**. This is
the workgroup-friendly form of "don't store long-lived secrets on every client":

* **At rest** (Coordinator only): machine-scoped DPAPI (``CryptProtectData`` +
  ``CRYPTPROTECT_LOCAL_MACHINE``) with app-specific secondary *entropy*, written
  to ``%PROGRAMDATA%\\Kiroshi\\nas.cred`` and ACL'd to SYSTEM+Administrators.
  Machine scope (not user scope) is what lets the Coordinator service decrypt it
  under any logon — the same mechanism Windows itself uses for service-account
  secrets (LSA secrets). Only a local admin on *that one box* can decrypt it,
  which is the inherent floor for any unattended auto-starting service.
* **In transit**: sealed with a key derived from the shared mesh token via a
  fresh per-request nonce (challenge-response), so the password is confidential
  on the wire **even over plain HTTP** and the token itself is never sent for
  the credential fetch. Uses stdlib HMAC-SHA256 only (no new dependency):
  HKDF(token, nonce) -> (enc_key, mac_key); encrypt-then-MAC stream cipher.

Only ``username`` is ever stored/returned in the clear (it is not a secret); the
password is DPAPI-sealed at rest and token-sealed on the wire.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
from typing import Optional

from .appstate import state_dir

CRED_FILENAME = "nas.cred"
_ENTROPY = b"kiroshi-nascred-v1"  # secondary DPAPI entropy: binds the blob to this app
_HKDF_INFO = b"kiroshi-nascred-transit-v1"


def cred_path() -> str:
    return str(state_dir() / CRED_FILENAME)


# --------------------------------------------------------------------------- #
#  at-rest: machine-scoped DPAPI (Windows) via ctypes — no pywin32 dependency
# --------------------------------------------------------------------------- #
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_CRYPTPROTECT_LOCAL_MACHINE = 0x4


def _dpapi(data: bytes, entropy: bytes, *, protect: bool) -> bytes:
    """Call CryptProtectData/CryptUnprotectData with machine scope + entropy.

    Buffers are kept alive for the duration of the call (their addresses are
    handed to Win32 via DATA_BLOB, so they must not be GC'd mid-call)."""
    if sys.platform != "win32":  # pragma: no cover - mesh nodes are Windows
        raise RuntimeError("machine-DPAPI credential store is Windows-only")
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _mk(buf: bytes):
        cbuf = ctypes.create_string_buffer(buf, len(buf))
        return _BLOB(len(buf), ctypes.cast(cbuf, ctypes.POINTER(ctypes.c_char))), cbuf

    in_blob, _in_ref = _mk(data)
    ent_blob, _ent_ref = _mk(entropy)
    out_blob = _BLOB()
    flags = _CRYPTPROTECT_LOCAL_MACHINE | _CRYPTPROTECT_UI_FORBIDDEN
    fn = (ctypes.windll.crypt32.CryptProtectData if protect
          else ctypes.windll.crypt32.CryptUnprotectData)
    ok = fn(ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
            None, None, flags, ctypes.byref(out_blob))
    if not ok:
        raise ctypes.WinError()  # type: ignore[attr-defined]
    try:
        return ctypes.string_at(out_blob.pbData, int(out_blob.cbData))
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def dpapi_protect(data: bytes) -> bytes:
    return _dpapi(data, _ENTROPY, protect=True)


def dpapi_unprotect(blob: bytes) -> bytes:
    return _dpapi(blob, _ENTROPY, protect=False)


def _restrict_acl(path: str) -> None:
    """Best-effort: lock the cred file to SYSTEM + Administrators + the running
    user, and drop inheritance so non-admin *other* users can't read the
    (already-encrypted) blob.

    The running user is granted explicitly on purpose: under UAC, a non-elevated
    admin process carries the ``Administrators`` group as *deny-only*, so an ACL
    that grants only ``Administrators`` would lock the (typically non-elevated)
    coordinator out of its own secret. Granting the owner account keeps it
    readable by the coordinator whether it runs as that user or as SYSTEM.
    Failure is non-fatal — machine-DPAPI is the real protection; this is
    defense in depth against other local non-admin users."""
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    grants = ["SYSTEM:F", "Administrators:F"]
    dom = os.environ.get("USERDOMAIN")
    usr = os.environ.get("USERNAME")
    if usr:
        grants.append(f"{dom + chr(92) if dom else ''}{usr}:F")
    try:
        subprocess.run(["icacls", path, "/inheritance:r"],
                       capture_output=True, check=False)
        subprocess.run(["icacls", path, "/grant:r", *grants],
                       capture_output=True, check=False)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
#  store API (Coordinator side)
# --------------------------------------------------------------------------- #
def set_secret(user: str, password: str, *, server: str = "default") -> str:
    """Encrypt+store the NAS credential at rest. Returns the file path.

    ``server`` allows per-server creds ("default" applies mesh-wide). The
    username is stored in the clear (not a secret); the password is DPAPI-sealed.
    """
    path = cred_path()
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("version", 1)
    creds = doc.setdefault("creds", {})
    creds[server] = {
        "user": user,
        "pw_dpapi": base64.b64encode(dpapi_protect(password.encode("utf-8"))).decode(),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, path)
    _restrict_acl(path)
    return path


def load_secret(server: str = "default") -> Optional[tuple[str, str]]:
    """Load + decrypt (user, password) from the at-rest store, or None."""
    try:
        with open(cred_path(), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    entry = (doc.get("creds") or {}).get(server) or (doc.get("creds") or {}).get("default")
    if not entry:
        return None
    user = entry.get("user")
    blob = entry.get("pw_dpapi")
    if not user or not blob:
        return None
    try:
        pw = dpapi_unprotect(base64.b64decode(blob)).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None
    return user, pw


def status(server: str = "default") -> dict:
    """Non-secret summary for `kiroshi nas-cred show` / doctor (never the pw)."""
    try:
        with open(cred_path(), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {"present": False, "path": cred_path()}
    entry = (doc.get("creds") or {}).get(server)
    return {
        "present": bool(entry),
        "path": cred_path(),
        "user": (entry or {}).get("user"),
        "servers": sorted((doc.get("creds") or {}).keys()),
    }


# --------------------------------------------------------------------------- #
#  in-transit seal: HKDF(token, nonce) + encrypt-then-MAC (stdlib HMAC only)
# --------------------------------------------------------------------------- #
def _hkdf(token: str, salt: bytes, length: int) -> bytes:
    """RFC-5869 HKDF-SHA256. token=IKM, salt=fresh per-request nonce."""
    prk = hmac.new(salt, token.encode("utf-8"), hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + _HKDF_INFO + bytes([counter]),
                         hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def _keystream(enc_key: bytes, n: int) -> bytes:
    out = b""
    ctr = 0
    while len(out) < n:
        out += hmac.new(enc_key, ctr.to_bytes(8, "big"), hashlib.sha256).digest()
        ctr += 1
    return out[:n]


def seal(token: str, nonce_hex: str, plaintext: bytes) -> str:
    """Encrypt-then-MAC ``plaintext`` under a key derived from token+nonce.

    Returns base64(tag32 || ciphertext). Only a holder of ``token`` can derive
    the key, so the payload is confidential even on plain HTTP; ``token`` itself
    is never transmitted for the fetch (the Runner proves it via HMAC)."""
    salt = bytes.fromhex(nonce_hex)
    key = _hkdf(token, salt, 64)
    enc_key, mac_key = key[:32], key[32:]
    ks = _keystream(enc_key, len(plaintext))
    ct = bytes(a ^ b for a, b in zip(plaintext, ks))
    tag = hmac.new(mac_key, salt + ct, hashlib.sha256).digest()
    return base64.b64encode(tag + ct).decode()


def unseal(token: str, nonce_hex: str, sealed_b64: str) -> Optional[bytes]:
    """Verify + decrypt a :func:`seal` payload. None on tamper / wrong token."""
    try:
        raw = base64.b64decode(sealed_b64)
    except Exception:  # noqa: BLE001
        return None
    if len(raw) < 32:
        return None
    tag, ct = raw[:32], raw[32:]
    salt = bytes.fromhex(nonce_hex)
    key = _hkdf(token, salt, 64)
    enc_key, mac_key = key[:32], key[32:]
    expect = hmac.new(mac_key, salt + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expect):
        return None
    ks = _keystream(enc_key, len(ct))
    return bytes(a ^ b for a, b in zip(ct, ks))


# Proof that a caller holds the mesh token, bound to this request's nonce +
# server, WITHOUT transmitting the token (mirrors security.prove for /auth).
def cred_proof(token: str, nonce_hex: str, server: str) -> str:
    return hmac.new(token.encode("utf-8"),
                    f"nascred:{nonce_hex}:{server}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify_cred_proof(token: Optional[str], nonce_hex: str, server: str,
                      proof: Optional[str]) -> bool:
    if not token or not nonce_hex or not proof or len(nonce_hex) < 16:
        return False
    return hmac.compare_digest(cred_proof(token, nonce_hex, server), proof)


def selftest() -> bool:
    """Round-trip the transit seal (always) and the DPAPI store (Windows)."""
    tok = "test-token-" + base64.b16encode(os.urandom(6)).decode()
    nonce = os.urandom(16).hex()
    msg = b"s3cr3t-p@ss w/ spaces & \xf0\x9f\x94\x92"
    sealed = seal(tok, nonce, msg)
    assert unseal(tok, nonce, sealed) == msg, "seal round-trip failed"
    assert unseal(tok + "x", nonce, sealed) is None, "wrong-token must fail"
    assert verify_cred_proof(tok, nonce, "default", cred_proof(tok, nonce, "default"))
    assert not verify_cred_proof(tok, nonce, "default", "deadbeef")
    if sys.platform == "win32":
        blob = dpapi_protect(msg)
        assert blob != msg and dpapi_unprotect(blob) == msg, "DPAPI round-trip failed"
    return True


if __name__ == "__main__":  # pragma: no cover
    print("nascred selftest:", "OK" if selftest() else "FAIL")
