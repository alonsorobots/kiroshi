"""Zero-config Coordinator discovery over UDP broadcast.

Hardcoding the Coordinator's IP is fragile on real networks: home/office DHCP leases
drift, machines move subnets, and most users can't (or shouldn't have to) set up
static IPs or router reservations. So Kiroshi ships a tiny zero-config discovery
layer, the same idea as mDNS/Bonjour but dependency-free and Windows-friendly:

Two complementary mechanisms run on the Coordinator's UDP port
(``KIROSHI_DISCOVERY_PORT``, default 8788):

- **Solicited (primary, firewall-friendly):** a client broadcasts a small query
  datagram; the Coordinator replies *unicast* to the sender. The query is outbound from
  the client (always allowed) and the reply rides back through the client's
  stateful firewall state, so **clients need no inbound rule** — only the Coordinator
  opens one UDP port, exactly like its HTTP port.
- **Passive (fallback):** the Coordinator also periodically broadcasts the same beacon,
  which works on flat networks where inbound broadcast is allowed.

Because the runner re-discovers whenever it loses the Coordinator (see
``worker.Runner``), a DHCP lease change just causes a brief reconnect instead of
a dead mesh. No router config, no static IPs, no central registry.

Privacy / OSS-exposure hardening:
- The beacon carries **no hostname and no secret** — only the magic string, the
  HTTP port, and a short random per-process fingerprint. An observer learns only
  "a Kiroshi Coordinator is at this IP:port", which is already inferable from the open
  TCP port; it does **not** leak the machine's name.
- It is **solicited by default** (reply only when asked) rather than constantly
  broadcasting, so the Coordinator isn't shouting its presence across the LAN. Passive
  broadcast is opt-in (``KIROSHI_DISCOVERY_BROADCAST=1``) for flat networks.
- The mesh **token still gates everything** — discovery only finds the URL;
  joining requires the token, which never travels in the beacon.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
from typing import Optional

DEFAULT_DISCOVERY_PORT = 8788
_MAGIC = "kiroshi-fixer"
_QUERY = b"kiroshi-who?"


def _passive_default() -> bool:
    return os.environ.get("KIROSHI_DISCOVERY_BROADCAST", "0").strip().lower() in (
        "1", "true", "yes", "on")


def discovery_port() -> int:
    try:
        return int(os.environ.get("KIROSHI_DISCOVERY_PORT", DEFAULT_DISCOVERY_PORT))
    except (TypeError, ValueError):
        return DEFAULT_DISCOVERY_PORT


def _broadcast_addrs() -> list[str]:
    """Best-effort set of broadcast addresses to cover multi-NIC hosts.

    Always includes the limited broadcast ``255.255.255.255``; adds per-adapter
    subnet broadcasts derived from this host's IPv4 addresses (assuming /24,
    which covers the overwhelming majority of home/office LANs).
    """
    addrs = {"255.255.255.255"}
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            octets = ip.split(".")
            if len(octets) == 4:
                addrs.add(".".join(octets[:3] + ["255"]))
    except OSError:
        pass
    return sorted(addrs)


def encode_beacon(fixer_port: int, fp: str = "") -> bytes:
    """Encode a discovery beacon. ``fp`` is a short non-secret, non-identifying
    fingerprint (NOT the hostname) so multiple fixers can be told apart."""
    return json.dumps(
        {"svc": _MAGIC, "port": int(fixer_port), "fp": fp, "ts": time.time()}
    ).encode("utf-8")


def parse_beacon(data: bytes) -> Optional[dict]:
    """Parse + validate a beacon datagram. Returns the dict or ``None``."""
    try:
        msg = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(msg, dict) or msg.get("svc") != _MAGIC:
        return None
    if not isinstance(msg.get("port"), int):
        return None
    return msg


class BeaconBroadcaster:
    """Coordinator-side discovery server: answers queries + periodically broadcasts.

    Binds the discovery UDP port and runs one thread that both (a) replies
    unicast to ``kiroshi-who?`` queries and (b) emits a periodic broadcast beacon
    as a fallback for flat networks.
    """

    def __init__(self, fixer_port: int, fp: Optional[str] = None,
                 interval: float = 3.0, disc_port: Optional[int] = None,
                 passive: Optional[bool] = None):
        self.fixer_port = fixer_port
        # Non-identifying fingerprint (random per process), never the hostname.
        self.fp = fp if fp is not None else secrets.token_hex(3)
        self.interval = interval
        self.disc_port = disc_port or discovery_port()
        self.passive = _passive_default() if passive is None else passive
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "BeaconBroadcaster":
        self._thread = threading.Thread(
            target=self._loop, name="kiroshi-beacon", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("", self.disc_port))
        except OSError:
            # Port busy: fall back to broadcast-only from an ephemeral socket.
            sock.close()
            self._broadcast_only()
            return
        sock.settimeout(0.5)
        last_bcast = 0.0
        try:
            while not self._stop.is_set():
                # answer solicited queries (the firewall-friendly primary path)
                try:
                    data, sender = sock.recvfrom(2048)
                    if data.strip() == _QUERY:
                        sock.sendto(encode_beacon(self.fixer_port, self.fp), sender)
                except socket.timeout:
                    pass
                except OSError:
                    pass
                # periodic passive broadcast — opt-in only (don't advertise by default)
                if self.passive:
                    now = time.time()
                    if now - last_bcast >= self.interval:
                        payload = encode_beacon(self.fixer_port, self.fp)
                        for addr in _broadcast_addrs():
                            try:
                                sock.sendto(payload, (addr, self.disc_port))
                            except OSError:
                                pass
                        last_bcast = now
        finally:
            sock.close()

    def _broadcast_only(self) -> None:
        if not self.passive:
            return  # solicited responder couldn't bind and passive is off: nothing to do
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            while not self._stop.is_set():
                payload = encode_beacon(self.fixer_port, self.fp)
                for addr in _broadcast_addrs():
                    try:
                        sock.sendto(payload, (addr, self.disc_port))
                    except OSError:
                        pass
                self._stop.wait(self.interval)
        finally:
            sock.close()


def check_singleton_fixer(
    timeout: float = 3.0,
    disc_port: Optional[int] = None,
) -> Optional[str]:
    """Split-brain guard: is another discoverable Coordinator already on this LAN?

    Called during Coordinator startup (before we bind our own beacon) to detect the
    common footgun of accidentally starting two Fixers on the same LAN — e.g.
    running ``kiroshi run --lan`` on a workstation while the persistent
    ``kiroshi-fixer`` service is up on the coordinator host. Two Fixers means
    two disjoint queues + two disjoint per-spindle disk budgets, so each
    happily saturates the shared NAS assuming the other doesn't exist.

    Returns the reachable existing-Coordinator URL if one is discoverable, else
    ``None`` (safe to proceed). Uses the same solicited-reply mechanism as
    :func:`discover_fixer` so anything the runners would see, we see.
    """
    return discover_fixer(timeout=timeout, disc_port=disc_port)


def discover_fixer(
    timeout: float = 5.0,
    disc_port: Optional[int] = None,
) -> Optional[str]:
    """Find the Coordinator and return its base URL (``http://ip:port``), or ``None``.

    Actively solicits (broadcast query -> unicast reply, firewall-friendly) and
    simultaneously listens for passive broadcasts, retrying the query a few times
    within ``timeout``.
    """
    disc_port = disc_port or discovery_port()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    # Bind ephemeral so the unicast reply (and any broadcast) reaches us; the
    # client deliberately does NOT bind the discovery port, so it needs no
    # inbound firewall rule.
    try:
        sock.bind(("", 0))
    except OSError:
        sock.close()
        return None

    deadline = time.time() + timeout
    last_query = 0.0
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            # (re)send the solicitation every ~1s
            now = time.time()
            if now - last_query >= 1.0:
                for addr in _broadcast_addrs():
                    try:
                        sock.sendto(_QUERY, (addr, disc_port))
                    except OSError:
                        pass
                last_query = now
            sock.settimeout(min(1.0, max(0.05, remaining)))
            try:
                data, sender = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return None
            msg = parse_beacon(data)
            if msg is None:
                continue
            return f"http://{sender[0]}:{msg['port']}"
    finally:
        sock.close()
