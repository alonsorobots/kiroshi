"""Advisory channel — structured Fixer-side warnings for humans, monitors, and agents.

Kiroshi already has the *sensor* data: per-disk in-flight vs budget
(``store.disk_inflight_count`` + ``resource_slots``), rolling I/O saturation
(``iowatcher``), throughput ring (``metrics``), and failure counters
(``store.stats``). What was missing is the *advisory* layer that turns those
signals into structured, deduped, actionable messages — and gets them to the
person or process that launched the work.

The design is deliberately generic: this module only produces JSON documents
that describe "something bad is happening." Where those go — the dashboard,
a Slack bot, an MCP client, a webhook that pokes an LLM agent — lives outside
Kiroshi. This file ships only the primitives:

- :class:`Advisory` — the schema.
- :class:`AdvisoryStore` — in-memory ring buffer with fingerprint dedup, so the
  same condition firing every 10s becomes one entry with a count, not spam.
- :class:`AdvisoryDetector` — background thread that samples fleet state and
  fires advisories when sustained-condition detectors trip.
- :class:`WebhookDispatcher` — best-effort background POST to each advisory's
  origin ``callback`` URL (if declared by the launcher via ``--origin``).

Everything here is opt-in and fail-open:

- Detectors that lack data (no topology, no ``iowatcher``, no metrics history)
  simply don't fire.
- The webhook dispatcher never crashes the Fixer; a bad callback URL is
  logged and dropped, no retries beyond one.
- The store is bounded (``capacity`` entries, oldest evicted) and lives only
  in memory — restart-time reset is intentional for v1; advisories describe
  *current* fleet state, not persistent history.
"""
from __future__ import annotations
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"
_SEVERITIES = (SEVERITY_INFO, SEVERITY_WARN, SEVERITY_CRITICAL)


@dataclass
class Advisory:
    """One structured warning about fleet state.

    Consumers key off ``code`` (a stable dotted identifier like ``nas.thrash``)
    and ``fingerprint`` (dedup key — same fingerprint firing twice bumps
    ``count`` + ``ts`` on the existing entry instead of appending).
    """
    id: str
    ts: float
    first_ts: float
    count: int
    severity: str
    code: str
    fingerprint: str
    disk: Optional[str]
    detail: str
    suggested_action: str
    dashboard_url: Optional[str] = None
    origins: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": round(self.ts, 3),
            "first_ts": round(self.first_ts, 3),
            "count": self.count,
            "severity": self.severity,
            "code": self.code,
            "fingerprint": self.fingerprint,
            "disk": self.disk,
            "detail": self.detail,
            "suggested_action": self.suggested_action,
            "dashboard_url": self.dashboard_url,
            "origins": list(self.origins),
        }


class AdvisoryStore:
    """In-memory ring buffer of advisories with fingerprint dedup.

    Firing the *same* fingerprint again bumps ``count`` + ``ts`` on the existing
    entry (and re-marks it "active") rather than appending — so a condition
    that persists for minutes shows up as one row, not dozens.

    ``resolve(fingerprint)`` clears the "active" flag but keeps the entry in
    history so ``list(since=...)`` still returns it.
    """

    def __init__(self, capacity: int = 500):
        self._by_fp: OrderedDict[str, Advisory] = OrderedDict()
        self._active: set[str] = set()
        self._capacity = capacity
        self._lock = threading.Lock()
        self._pending_dispatch: deque[Advisory] = deque()

    def fire(
        self,
        *,
        severity: str,
        code: str,
        disk: Optional[str],
        detail: str,
        suggested_action: str,
        dashboard_url: Optional[str] = None,
        origins: Optional[list[dict[str, Any]]] = None,
        fingerprint: Optional[str] = None,
    ) -> Advisory:
        """Record an advisory, deduping on ``fingerprint``.

        Returns the (new or updated) :class:`Advisory`. Also enqueues it for
        the webhook dispatcher iff this call created it or bumped its count
        (i.e. always — same-tick re-fire IS a dispatchable event because it
        means the condition is still true).
        """
        if severity not in _SEVERITIES:
            raise ValueError(f"invalid severity: {severity!r}")
        fp = fingerprint or f"{code}:{disk or '*'}"
        now = time.time()
        with self._lock:
            existing = self._by_fp.get(fp)
            if existing is not None:
                existing.ts = now
                existing.count += 1
                existing.severity = severity  # allow escalation
                existing.detail = detail
                existing.suggested_action = suggested_action
                existing.dashboard_url = dashboard_url or existing.dashboard_url
                existing.origins = list(origins or existing.origins)
                self._active.add(fp)
                # Move to end for LRU semantics on eviction
                self._by_fp.move_to_end(fp)
                self._pending_dispatch.append(existing)
                return existing
            adv = Advisory(
                id=uuid.uuid4().hex,
                ts=now,
                first_ts=now,
                count=1,
                severity=severity,
                code=code,
                fingerprint=fp,
                disk=disk,
                detail=detail,
                suggested_action=suggested_action,
                dashboard_url=dashboard_url,
                origins=list(origins or []),
            )
            self._by_fp[fp] = adv
            self._active.add(fp)
            self._pending_dispatch.append(adv)
            while len(self._by_fp) > self._capacity:
                evicted_fp, _ = self._by_fp.popitem(last=False)
                self._active.discard(evicted_fp)
            return adv

    def resolve(self, fingerprint: str) -> bool:
        """Mark ``fingerprint`` inactive. Returns True if it was active."""
        with self._lock:
            if fingerprint in self._active:
                self._active.discard(fingerprint)
                return True
            return False

    def list(
        self,
        since: Optional[float] = None,
        severity: Optional[str] = None,
        disk: Optional[str] = None,
        active_only: bool = False,
        limit: int = 200,
    ) -> list[Advisory]:
        with self._lock:
            items = list(self._by_fp.values())
        out: list[Advisory] = []
        for adv in items:
            if since is not None and adv.ts < since:
                continue
            if severity is not None and adv.severity != severity:
                continue
            if disk is not None and adv.disk != disk:
                continue
            if active_only and adv.fingerprint not in self._active:
                continue
            out.append(adv)
        out.sort(key=lambda a: a.ts, reverse=True)
        return out[:limit]

    def active(self) -> list[Advisory]:
        return self.list(active_only=True)

    def is_active(self, fingerprint: str) -> bool:
        with self._lock:
            return fingerprint in self._active

    def drain_pending(self) -> list[Advisory]:
        """Consume all advisories enqueued for webhook dispatch since last drain."""
        with self._lock:
            out = list(self._pending_dispatch)
            self._pending_dispatch.clear()
        return out


# --------------------------------------------------------------------------- detectors


# Public tuning knobs; overridable via AdvisoryDetector kwargs.
DEFAULT_SAMPLE_INTERVAL_S = 10.0
DEFAULT_SUSTAIN_S = 60.0
DEFAULT_THRASH_FACTOR = 1.2         # in-flight > budget * factor
DEFAULT_COLLAPSE_RATIO = 0.4        # current rate < baseline * ratio
DEFAULT_SATURATION_PCT = 95.0       # util_pct at/above this = saturated
DEFAULT_FAILURE_SPIKE_COUNT = 20    # failed deltas in the window
DEFAULT_FAILURE_SPIKE_RATIO = 0.30  # failed / completions in the window


class AdvisoryDetector:
    """Background sampler that turns fleet state into advisories.

    Every ``sample_interval_s``, evaluate each detector against the latest
    ``store.stats()``, ``iowatcher.snapshot()``, ``metrics_ring`` and
    ``get_resource_state()``. A condition that has held for ``sustain_s``
    seconds fires; going back below threshold resolves the fingerprint on
    the next tick.

    ``origins_for(disk)`` is a callback: given a disk id (or ``None`` for
    fleet-wide advisories), return the list of origin dicts that own
    in-flight work in that scope. Kiroshi wires this from
    ``app.state.origins`` + the store's leased-gig list.

    Fail-open: any detector that raises is logged and skipped — a bad
    detector never crashes the Fixer.
    """

    def __init__(
        self,
        adv_store: AdvisoryStore,
        stats_fn: Callable[[], dict[str, Any]],
        iowatcher_fn: Optional[Callable[[], dict[str, Any]]] = None,
        metrics_ring: Optional[Any] = None,   # a deque[dict] with 'ts','rate','failed','done'
        resource_fn: Optional[Callable[[], dict[str, Any]]] = None,
        origins_for: Optional[Callable[[Optional[str]], list[dict[str, Any]]]] = None,
        disk_budget_fn: Optional[Callable[[], dict[str, int]]] = None,
        disk_inflight_fn: Optional[Callable[[str], int]] = None,
        parity_disks_fn: Optional[Callable[[], set[str]]] = None,
        dashboard_url_fn: Optional[Callable[[Optional[str]], Optional[str]]] = None,
        sample_interval_s: float = DEFAULT_SAMPLE_INTERVAL_S,
        sustain_s: float = DEFAULT_SUSTAIN_S,
        thrash_factor: float = DEFAULT_THRASH_FACTOR,
        collapse_ratio: float = DEFAULT_COLLAPSE_RATIO,
        saturation_pct: float = DEFAULT_SATURATION_PCT,
        failure_spike_count: int = DEFAULT_FAILURE_SPIKE_COUNT,
        failure_spike_ratio: float = DEFAULT_FAILURE_SPIKE_RATIO,
    ):
        self._adv = adv_store
        self._stats = stats_fn
        self._io = iowatcher_fn
        self._metrics = metrics_ring
        self._resource = resource_fn
        self._origins_for = origins_for or (lambda _d: [])
        self._budget = disk_budget_fn or (lambda: {})
        self._inflight = disk_inflight_fn or (lambda _d: 0)
        self._parity_disks = parity_disks_fn or (lambda: set())
        self._dashboard_url = dashboard_url_fn or (lambda _d: None)

        self.sample_interval_s = sample_interval_s
        self.sustain_s = sustain_s
        self.thrash_factor = thrash_factor
        self.collapse_ratio = collapse_ratio
        self.saturation_pct = saturation_pct
        self.failure_spike_count = failure_spike_count
        self.failure_spike_ratio = failure_spike_ratio

        # fingerprint -> first-time-condition-held (for sustain tracking)
        self._condition_since: dict[str, float] = {}
        self._prev_failed: Optional[int] = None
        self._prev_done: Optional[int] = None
        self._prev_ts: Optional[float] = None

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="kiroshi-advisory-detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.sample_interval_s):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("advisory detector tick failed (continuing)")

    # ------------------------------------------------------------------ tick
    def tick(self) -> list[Advisory]:
        """Run every detector once. Returns advisories fired *this* tick."""
        fired: list[Advisory] = []
        try:
            stats = self._stats()
        except Exception:
            logger.exception("stats_fn failed; skipping this tick")
            return fired

        fired += self._detect_thrash(stats)
        fired += self._detect_throughput_collapse(stats)
        fired += self._detect_failure_spike(stats)
        io_snap: Optional[dict[str, Any]] = None
        if self._io is not None:
            try:
                io_snap = self._io()
            except Exception:
                io_snap = None
            if io_snap is not None:
                fired += self._detect_saturation(io_snap)
        if self._resource is not None:
            try:
                rstate = self._resource()
            except Exception:
                rstate = None
            if rstate is not None:
                fired += self._detect_parity_pressure(rstate, stats)
        # bottleneck classifier (P2) — fuses host resources + iowatcher + topology
        try:
            fired += self._detect_bottleneck(stats, io_snap if self._io else None)
        except Exception:
            logger.debug("bottleneck detector skipped (no data or error)")
        return fired

    # --------------------------------------------------------- sustain helper
    def _sustained(self, fingerprint: str, condition_true: bool) -> bool:
        """Track how long a fingerprint's condition has been continuously true.

        Returns True only after ``sustain_s`` seconds of continuous truth.
        A False observation clears the timer AND resolves the fingerprint in
        the advisory store (so consumers can see "the thrash cleared").
        """
        now = time.time()
        if condition_true:
            first = self._condition_since.get(fingerprint)
            if first is None:
                self._condition_since[fingerprint] = now
                # With no sustain window, a single observation is enough — this
                # is what tests / one-shot callers rely on when `sustain_s == 0`.
                return self.sustain_s <= 0.0
            return (now - first) >= self.sustain_s
        # Not true anymore — clear timer + resolve any active advisory
        if fingerprint in self._condition_since:
            del self._condition_since[fingerprint]
        self._adv.resolve(fingerprint)
        return False

    # -------------------------------------------------------------- detectors
    def _detect_thrash(self, stats: dict[str, Any]) -> list[Advisory]:
        """Fire when disk in-flight exceeds the budget by ``thrash_factor``
        sustained for ``sustain_s``. Inert without a per-disk budget."""
        out: list[Advisory] = []
        budgets = self._budget() or {}
        if not budgets:
            return out
        disk_inflight = stats.get("disk_inflight") or {}
        for disk, budget in budgets.items():
            if budget <= 0:
                continue
            inflight = int(disk_inflight.get(disk, 0))
            fp = f"nas.thrash:{disk}"
            over = inflight > int(budget * self.thrash_factor)
            if not self._sustained(fp, over):
                continue
            detail = (f"{disk} in-flight {inflight} vs budget {budget} "
                      f"(> {int(self.thrash_factor * 100)}%) sustained for "
                      f"{int(self.sustain_s)}s; HDD head-thrash likely — "
                      f"throughput will collapse if not relieved.")
            out.append(self._adv.fire(
                severity=SEVERITY_WARN, code="nas.thrash", disk=disk, detail=detail,
                suggested_action=("reduce per-Runner workers, cap `--capacity`, "
                                  "or rebalance shards across more spindles "
                                  "(`kiroshi nas shard --rebalance`)"),
                dashboard_url=self._dashboard_url(disk),
                origins=self._origins_for(disk),
                fingerprint=fp,
            ))
        return out

    def _detect_saturation(self, io_snap: dict[str, Any]) -> list[Advisory]:
        """Fire when a disk's rolling util_pct sits at/above ``saturation_pct``.
        Escalates to CRITICAL for parity disks (single-spindle RMW bottleneck)."""
        out: list[Advisory] = []
        parity = self._parity_disks() or set()
        for d in io_snap.get("disks", []):
            disk = d.get("disk")
            util = float(d.get("util_pct") or 0.0)
            if not disk:
                continue
            fp = f"nas.disk_saturation:{disk}"
            saturated = util >= self.saturation_pct
            if not self._sustained(fp, saturated):
                continue
            is_parity = disk in parity
            sev = SEVERITY_CRITICAL if is_parity else SEVERITY_WARN
            role = "parity spindle (RMW wall)" if is_parity else "spindle"
            detail = (f"{disk} {role} util {util:.0f}% sustained for "
                      f"{int(self.sustain_s)}s at "
                      f"{d.get('read_mbps', 0):.0f}/{d.get('write_mbps', 0):.0f} MB/s "
                      f"(read/write) — this disk is the throughput wall.")
            action = ("check for non-Kiroshi processes hitting this disk; "
                      "if it's parity, reduce concurrent writers globally")
            if is_parity:
                action = ("this is the parity disk — every array write "
                          "read-modify-writes here. Reduce concurrent writers "
                          "via the mesh write budget or route writes to a "
                          "cache tier.")
            out.append(self._adv.fire(
                severity=sev, code="nas.disk_saturation", disk=disk, detail=detail,
                suggested_action=action,
                dashboard_url=self._dashboard_url(disk),
                origins=self._origins_for(disk),
                fingerprint=fp,
            ))
        return out

    def _detect_throughput_collapse(self, stats: dict[str, Any]) -> list[Advisory]:
        """Fire when current 60s throughput has dropped to <``collapse_ratio``
        of the ~5-min baseline while there is still work to do."""
        out: list[Advisory] = []
        if self._metrics is None or len(self._metrics) < 6:
            return out
        pending = int(stats.get("pending", 0))
        leased = int(stats.get("leased", 0))
        if pending == 0 and leased == 0:
            self._sustained("nas.throughput_collapse:*", False)
            return out
        samples = list(self._metrics)
        current = samples[-3:]
        baseline = samples[:-3] if len(samples) > 3 else samples
        cur_rate = sum(s.get("rate", 0.0) for s in current) / max(1, len(current))
        base_rate = sum(s.get("rate", 0.0) for s in baseline) / max(1, len(baseline))
        fp = "nas.throughput_collapse:*"
        collapsed = (base_rate > 0.1) and (cur_rate < base_rate * self.collapse_ratio)
        if not self._sustained(fp, collapsed):
            return out
        drop_pct = 100.0 * (1.0 - (cur_rate / base_rate)) if base_rate > 0 else 0.0
        detail = (f"fleet throughput {cur_rate:.2f}/s (last ~30s) vs "
                  f"{base_rate:.2f}/s baseline — dropped {drop_pct:.0f}% "
                  f"while {pending} pending + {leased} in-flight.")
        out.append(self._adv.fire(
            severity=SEVERITY_WARN, code="nas.throughput_collapse", disk=None,
            detail=detail,
            suggested_action=("check the dashboard's per-disk sparklines to "
                              "locate the bottleneck; a single hot spindle or "
                              "an oversubscribed parity disk is the usual cause."),
            dashboard_url=self._dashboard_url(None),
            origins=self._origins_for(None),
            fingerprint=fp,
        ))
        return out

    def _detect_failure_spike(self, stats: dict[str, Any]) -> list[Advisory]:
        """Fire when failed-gig deltas over the sample window exceed either an
        absolute count or a ratio of completions in the same window."""
        out: list[Advisory] = []
        now = time.time()
        failed = int(stats.get("failed", 0))
        done = int(stats.get("done", 0))
        prev_ts = self._prev_ts
        prev_failed = self._prev_failed
        prev_done = self._prev_done
        self._prev_ts, self._prev_failed, self._prev_done = now, failed, done
        if prev_ts is None or prev_failed is None or prev_done is None:
            return out
        dt = now - prev_ts
        if dt < 1.0:
            return out
        d_failed = max(0, failed - prev_failed)
        d_done = max(0, done - prev_done)
        completions = d_failed + d_done
        fp = "gig.failure_spike:*"
        spike = (d_failed >= self.failure_spike_count) or (
            completions > 0 and (d_failed / completions) >= self.failure_spike_ratio
            and d_failed >= 3)
        if not self._sustained(fp, spike):
            return out
        ratio = (100.0 * d_failed / completions) if completions else 100.0
        detail = (f"{d_failed} failed vs {d_done} done in the last "
                  f"~{dt:.0f}s ({ratio:.0f}% failure rate) — task exceptions "
                  f"or transport errors, not just slowness.")
        out.append(self._adv.fire(
            severity=SEVERITY_WARN, code="gig.failure_spike", disk=None, detail=detail,
            suggested_action=("read `/status`'s recent_errors, or `kiroshi status`, "
                              "for the exception messages; if they cluster on one "
                              "host or one shard, the failure is structural."),
            dashboard_url=self._dashboard_url(None),
            origins=self._origins_for(None),
            fingerprint=fp,
        ))
        return out

    def _detect_parity_pressure(
        self, rstate: dict[str, Any], stats: dict[str, Any]
    ) -> list[Advisory]:
        """Fire when the mesh-global write budget is pinned at cap with work
        still pending. Info-severity (not a failure — a hint to shard wider)."""
        out: list[Advisory] = []
        if not rstate.get("has_parity"):
            return out
        budget = int(rstate.get("global_write_budget") or 0)
        inflight = int(rstate.get("global_write_inflight") or 0)
        if budget <= 0:
            return out
        pending = int(stats.get("pending", 0))
        fp = "nas.parity_write_pressure:*"
        pressured = inflight >= budget and pending > 0
        if not self._sustained(fp, pressured):
            return out
        detail = (f"global write budget saturated ({inflight}/{budget} in-flight) "
                  f"with {pending} pending — parity RMW is capping fleet throughput.")
        out.append(self._adv.fire(
            severity=SEVERITY_INFO, code="nas.parity_write_pressure", disk=None,
            detail=detail,
            suggested_action=("expected on parity arrays under heavy write load; "
                              "consider routing writes to a cache/SSD tier or "
                              "widening the shard count."),
            dashboard_url=self._dashboard_url(None),
            origins=self._origins_for(None),
            fingerprint=fp,
        ))
        return out

    # --------------------------------------------------------- bottleneck (P2)

    # Maps bottleneck verdict → advisory code + severity
    _BOTTLENECK_CODES = {
        "cpu_bound": ("host.cpu_bound", SEVERITY_WARN),
        "mem_pressure": ("host.mem_pressure", SEVERITY_WARN),
        "disk_at_ceiling": ("disk.at_ceiling", SEVERITY_WARN),
        "nas_single_spindle": ("nas.single_spindle", SEVERITY_WARN),
        "net_bound": ("net.bound", SEVERITY_INFO),
        "gpu_bound": ("gpu.bound", SEVERITY_WARN),
        "vram_pressure": ("gpu.vram_pressure", SEVERITY_CRITICAL),
        "latency_bound": ("nas.latency_bound", SEVERITY_WARN),
    }

    def _detect_bottleneck(self, stats: dict[str, Any],
                           io_snap: Optional[dict[str, Any]]) -> list[Advisory]:
        """Fuse host resources + iowatcher + topology into a dominant-pressure
        verdict via :func:`kiroshi.bottleneck.classify`, and fire an advisory
        when the verdict is sustained for ``sustain_s``.

        Fail-open: if psutil is unavailable or the sample is incomplete, this
        detector silently skips (returns ``[]``)."""
        try:
            from .bottleneck import classify, ResourceSample, DiskPressure
        except ImportError:
            return []

        # ---- build ResourceSample from available data ----
        cpu_pct = 0.0
        mem_used = 0.0
        mem_total = 1.0
        try:
            import psutil
            cpu_pct = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            mem_used = vm.used / 1e9
            mem_total = vm.total / 1e9
        except Exception:
            pass

        # disk pressures from iowatcher
        disks: list[DiskPressure] = []
        if io_snap and "disks" in io_snap:
            for d in io_snap["disks"]:
                did = d.get("disk_id") or d.get("id") or "unknown"
                util = float(d.get("util_pct", 0))
                mbps = float(d.get("read_mbps", 0)) + float(d.get("write_mbps", 0))
                # TODO(roadmap P2): wire bench.py ceilings here so disk_at_ceiling
                # uses measured peak MB/s instead of raw util_pct. Until then,
                # ceiling=0 makes the classifier fall back to util_pct (fine for
                # detecting saturation, less precise for "at ceiling vs headroom").
                ceiling = 0.0
                inflight = self._inflight(did) if self._inflight else 0
                disks.append(DiskPressure(did, util, mbps, ceiling, inflight))

        # observed vs expected throughput from metrics ring.
        # "expected" = rolling max of recent rate samples — a self-calibrating
        # baseline that captures what this fleet was achieving before the
        # slowdown. If current rate < 50% of that AND nothing is at its
        # ceiling → latency_bound fires (the critical acceptance-gate case).
        observed = 0.0
        expected = 0.0
        if self._metrics and len(self._metrics) > 0:
            rates = []
            for m in self._metrics:
                try:
                    r = float(m.get("rate", 0))
                    if r > 0:
                        rates.append(r)
                except Exception:
                    pass
            if rates:
                observed = rates[-1]
                # rolling max over the recent window = "what we were doing
                # before the slowdown" (skip the last sample to avoid
                # self-referencing a current dip as the ceiling)
                historical = rates[:-1] if len(rates) > 1 else rates
                expected = max(historical) if historical else 0.0

        sample = ResourceSample(
            cpu_pct=cpu_pct, cpu_cores=os.cpu_count() or 4,
            mem_used_gb=mem_used, mem_total_gb=mem_total,
            disks=disks,
            observed_gigs_per_s=observed, expected_gigs_per_s=expected,
        )

        verdict = classify(sample)
        fp = f"bottleneck:{verdict.verdict}"
        if verdict.verdict == "healthy" or verdict.verdict not in self._BOTTLENECK_CODES:
            # resolve the previously-active bottleneck fingerprint (if any)
            self._resolve_bottleneck()
            return []

        code, severity = self._BOTTLENECK_CODES[verdict.verdict]
        if not self._sustained(fp, True):
            return []

        # resolve a *different* previously-active bottleneck before firing the
        # new one, so advisories don't accumulate across verdict transitions.
        self._resolve_bottleneck(except_fp=fp)

        self._adv.fire(
            severity=severity,
            code=code,
            disk=verdict.dominant_resource.split(":", 1)[1]
                 if ":" in verdict.dominant_resource else None,
            detail=verdict.detail,
            suggested_action=verdict.hint,
            dashboard_url=self._dashboard_url(None),
            fingerprint=fp,   # MUST match the fp used in _sustained/_resolve
        )
        self._active_bottleneck_fp = fp
        return [a for a in self._adv.list_active() if a.code == code]

    def _resolve_bottleneck(self, except_fp: Optional[str] = None) -> None:
        """Resolve the currently-active bottleneck advisory, unless it matches
        ``except_fp`` (the new verdict we're about to fire). Uses the
        fingerprint tracked in ``self._active_bottleneck_fp`` rather than
        iterating all codes — the old version guessed fingerprints from code
        names and silently failed for multi-dot codes like
        nas.single_spindle (verdict name) vs nas.single_spindle (advisory code).
        """
        fp = getattr(self, "_active_bottleneck_fp", None)
        if fp and fp != except_fp:
            self._sustained(fp, False)
            self._active_bottleneck_fp = None


# --------------------------------------------------------------------------- webhook


class WebhookDispatcher:
    """Best-effort background POST of new advisories to their origins' callbacks.

    An origin is an opaque JSON dict the launcher supplied via
    ``kiroshi run --origin '{...}'`` or ``KIROSHI_ORIGIN`` — Kiroshi only cares
    about the optional ``callback`` URL. Any advisory that lands with origins
    holding a ``callback`` is POSTed to that URL (5s timeout, no retries beyond
    one). This is fire-and-forget: a broken URL logs a warning and is dropped.

    A stateful ``last_result`` dict is kept per callback URL so tests can
    observe what happened, and so a future dashboard panel can show delivery
    health.
    """

    def __init__(
        self,
        adv_store: AdvisoryStore,
        interval_s: float = 2.0,
        http_timeout: float = 5.0,
        http_post: Optional[Callable[..., Any]] = None,
    ):
        self._adv = adv_store
        self._interval_s = interval_s
        self._http_timeout = http_timeout
        self._http_post = http_post
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_result: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="kiroshi-advisory-webhook", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self.dispatch_once()
            except Exception:  # noqa: BLE001
                logger.exception("advisory dispatcher iteration failed (continuing)")

    def dispatch_once(self) -> int:
        pending = self._adv.drain_pending()
        n = 0
        for adv in pending:
            for origin in adv.origins:
                cb = origin.get("callback")
                if not cb:
                    continue
                n += self._post(cb, adv, origin)
        return n

    def _post(self, url: str, adv: Advisory, origin: dict[str, Any]) -> int:
        body = {"advisory": adv.to_dict(), "origin": origin}
        try:
            poster = self._http_post
            if poster is None:
                import requests
                poster = requests.post
            r = poster(url, json=body, timeout=self._http_timeout)
            status = getattr(r, "status_code", 0)
            ok = 200 <= status < 300
            self._last_result[url] = {"ok": ok, "status": status, "ts": time.time()}
            if not ok:
                logger.warning("advisory webhook %s -> HTTP %s (dropped)", url, status)
            return 1 if ok else 0
        except Exception as e:  # noqa: BLE001
            self._last_result[url] = {"ok": False, "error": repr(e), "ts": time.time()}
            logger.warning("advisory webhook %s failed: %r (dropped)", url, e)
            return 0

    @property
    def last_results(self) -> dict[str, dict[str, Any]]:
        return dict(self._last_result)


# --------------------------------------------------------------------------- helpers


def filter_advisories_for_lease(
    advisories: list[Advisory],
    leased_disks: set[str],
) -> list[dict[str, Any]]:
    """Pick the advisories a Runner should see attached to its lease response.

    Include every unscoped advisory (``disk is None``) plus every advisory
    whose ``disk`` is one the Runner just leased work on. Excludes advisories
    for other spindles — a Runner leasing disk1 doesn't need to hear about
    disk7's thrash.
    """
    out: list[dict[str, Any]] = []
    for adv in advisories:
        if adv.disk is None or adv.disk in leased_disks:
            out.append(adv.to_dict())
    return out


def format_stdout_line(adv: dict[str, Any]) -> str:
    """The one-line human-readable form the Runner writes to stdout.

    Format::

        KIROSHI-ADVISORY: <severity> <code> [disk=<d>] | <detail> | action: <suggested_action>

    Prefixed so it's greppable in `logs/*.log` even when interleaved with
    normal Runner output.
    """
    disk = adv.get("disk")
    disk_frag = f" disk={disk}" if disk else ""
    return (
        f"KIROSHI-ADVISORY: {adv.get('severity','?').upper()} "
        f"{adv.get('code','?')}{disk_frag} | {adv.get('detail','')} | "
        f"action: {adv.get('suggested_action','')}"
    )
