"""Tests for the cross-network robustness layer.

Covers the dependency-light, deterministic pieces:
  - discovery beacon encode/parse (incl. round-trip over a real UDP socket pair)
  - JobStore.requeue (failed/leased -> pending, attempt reset)
  - drive-letter / UNC path normalization
  - doctor's root checks (read listable, write writable)

No FastAPI / network server required. Runnable two ways::
    pytest tests/test_robustness.py
    python tests/test_robustness.py
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
for _p in (SRC, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi import discovery  # noqa: E402
from kiroshi import paths as kpaths  # noqa: E402
from kiroshi.jobstore import JobStore  # noqa: E402


# --------------------------------------------------------------- discovery
def test_beacon_encode_parse_roundtrip():
    msg = discovery.parse_beacon(discovery.encode_beacon(8787, "ab12cd"))
    assert msg is not None
    assert msg["svc"] == "kiroshi-fixer"
    assert msg["port"] == 8787
    # Hardened beacon carries a non-secret fingerprint, NOT the hostname.
    assert msg["fp"] == "ab12cd"
    assert "name" not in msg


def test_beacon_does_not_leak_hostname():
    import socket as _s
    raw = discovery.encode_beacon(8787).decode("utf-8")
    assert _s.gethostname().lower() not in raw.lower()


def test_beacon_parse_rejects_junk():
    assert discovery.parse_beacon(b"not json") is None
    assert discovery.parse_beacon(b'{"svc":"something-else","port":1}') is None
    assert discovery.parse_beacon(b'{"svc":"kiroshi-fixer"}') is None  # no port


def test_discover_over_loopback():
    """A real broadcaster -> discover_fixer round-trip on an ephemeral port."""
    port = _free_udp_port()
    bc = discovery.BeaconBroadcaster(fixer_port=9999, interval=0.2,
                                     disc_port=port, passive=True).start()
    try:
        url = discovery.discover_fixer(timeout=4.0, disc_port=port)
    finally:
        bc.stop()
    assert url is not None and url.endswith(":9999"), url


def test_discover_times_out_when_silent():
    port = _free_udp_port()
    t0 = time.time()
    url = discovery.discover_fixer(timeout=1.0, disc_port=port)
    assert url is None
    assert time.time() - t0 < 5.0


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ----------------------------------------------------------------- requeue
def test_requeue_failed_to_pending():
    store = _store()
    store.seed([{"subjob_id": "a", "spec": {}}, {"subjob_id": "b", "spec": {}}])
    lease = store.lease("r1", "h", capacity=10, ttl=60)
    # exhaust retries so both end up failed
    for _ in range(store.max_retries + 2):
        results = [{"subjob_id": g["subjob_id"], "status": "error", "error": "boom"}
                   for g in lease.gigs]
        store.complete(results)
        lease = store.lease("r1", "h", capacity=10, ttl=60)
    assert store.stats()["failed"] == 2

    n = store.requeue(("failed",), reset_attempts=True)
    assert n == 2
    s = store.stats()
    assert s["failed"] == 0 and s["pending"] == 2
    # attempts reset means a fresh lease is allowed again
    relased = store.lease("r1", "h", capacity=10, ttl=60)
    assert len(relased.gigs) == 2


def test_requeue_leased_reclaims():
    store = _store()
    store.seed([{"subjob_id": "a", "spec": {}}])
    store.lease("r1", "h", capacity=10, ttl=600)  # long TTL, not reaped
    assert store.stats()["leased"] == 1
    n = store.requeue(("leased",), reset_attempts=False)
    assert n == 1
    assert store.stats()["pending"] == 1


def test_requeue_ignores_unknown_state():
    store = _store()
    store.seed([{"subjob_id": "a", "spec": {}}])
    assert store.requeue(("bogus",)) == 0


def _store() -> JobStore:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return JobStore(path, max_retries=2)


# ------------------------------------------------------------------- paths
def test_normalize_passthrough_posix():
    assert kpaths.normalize_root("/mnt/data") == "/mnt/data"


def test_normalize_passthrough_unc():
    unc = r"\\192.0.2.10\disk7\x"
    assert kpaths.normalize_root(unc) == unc


def test_normalize_unmapped_drive_unchanged():
    # A drive letter with no network mapping (or off-Windows) is returned as-is,
    # never silently dropped.
    assert kpaths.normalize_root(r"Z:\nope") == r"Z:\nope"


def test_normalize_strips_whitespace_and_quotes():
    # `set VAR=value ` (trailing space) and quoted values are common Windows
    # foot-guns; normalize must clean them.
    assert kpaths.normalize_root(r"\\srv\share ") == r"\\srv\share"
    assert kpaths.normalize_root('  "/mnt/data"  ') == "/mnt/data"
    assert kpaths.normalize_root("   ") is None


def test_looks_like_drive_letter():
    assert kpaths.looks_like_drive_letter(r"X:\foo")
    assert not kpaths.looks_like_drive_letter(r"\\srv\share")
    assert not kpaths.looks_like_drive_letter("/mnt/x")


# ------------------------------------------------------------------ doctor
def test_doctor_write_root_ok(tmp_path_factory=None):
    from kiroshi.doctor import _Report, _check_write_root, _check_read_root

    d = tempfile.mkdtemp()
    rep = _Report()
    _check_write_root(rep, d)
    _check_read_root(rep, d)
    assert not rep.failed


def test_doctor_read_root_missing_fails():
    from kiroshi.doctor import _Report, _check_read_root

    rep = _Report()
    _check_read_root(rep, os.path.join(tempfile.gettempdir(), "kiroshi_nope_xyz_123"))
    assert rep.failed


# --------------------------------------------------------------------- main
def _main() -> int:
    cases = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in cases:
        name = fn.__name__
        t0 = time.time()
        try:
            fn()
            print(f"PASS  {name:38s} ({time.time() - t0:.2f}s)", flush=True)
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name:38s} {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name:38s} {e!r}", flush=True)
    print(f"\n{len(cases) - failures}/{len(cases)} passed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    import multiprocessing as mp

    mp.freeze_support()
    raise SystemExit(_main())
