"""Error-class-aware backpressure — a circuit breaker for a Runner's lease loop.

Directly targets the failure mode that took down the NAS on 2026-07-21/22: a
stored credential went stale mid-campaign, and Kiroshi's only failure
response -- retry -- applied identically to every error. A wrong password
never succeeds on retry, but the runner kept re-leasing and re-running
against the failing dependency thousands of times, flooding it until it
crashed. This is the automated form of "first failed batch, stop and
diagnose" -- encoded so it holds with no human watching.

Framing that keeps this safe and simple (mirrors WorkerTuner):

* **This is best-effort protection, not a correctness mechanism.** A tripped
  breaker just PAUSES leasing; the queue is durable (SQLite-backed), gigs
  simply sit pending and re-lease once the breaker closes. A false trip is
  cheap (a runner idles a while longer than strictly necessary); a missed
  trip only regresses to today's behavior (retry-and-hope). So the control
  law does not need to be provably optimal, only "usually stops a failure
  storm quickly" -- which keeps it small.
* **Permanent errors get a fast trip; systemic transient failures get a
  slower, homogeneity-gated trip.** A wrong password produces the SAME error
  every single time -- 3 in a row is already overwhelming evidence, so we
  don't wait for a large sample. A general "lots of things are failing"
  signal is noisier (could be N independent bad sub-jobs, not one systemic
  cause), so it requires both volume AND that the failures share one
  dominant cause before tripping -- heterogeneous scatter (many distinct
  errors) looks like ordinary bad data, not a systemic fault, and must NOT
  trip.
* **Self-healing via a half-open probe**, not a fixed timeout-then-resume:
  after the cooldown, lease exactly ONE sub-job. If it succeeds, the
  dependency is fixed -- resume at full capacity, no restart needed. If it
  fails, go back to fully open with a LONGER cooldown (capped) -- never
  hammer a still-broken dependency with a full-capacity lease just because
  time passed.

The control law (``FailureBreaker.record`` / ``allow_lease``) is a pure
function of (state, now) plus internal counters -- deliberately separated
from any I/O, so it's unit-testable with synthetic result streams and a
controlled clock, exactly like ``WorkerTuner.step()``.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Optional, Tuple

from .errclass import classify, signature


class FailureBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    # Fast trip: this many permanent-classified failures in a row is already
    # overwhelming evidence (no legitimate transient produces N consecutive
    # identical auth rejections, say) -- trip immediately, don't wait for a
    # large sample.
    CONSECUTIVE_PERMANENT_TRIP = 3

    # Slower trip: a sliding window over the last N completed sub-jobs.
    WINDOW = 20
    MIN_SAMPLE = 5              # never trip below this many samples in-window
    TRANSIENT_TRIP_FRACTION = 0.5    # >= 50% of the window failed...
    HOMOGENEOUS_FRACTION = 0.8        # ...AND one signature is >= 80% of those failures

    BASE_COOLDOWN_S = 120.0
    MAX_COOLDOWN_S = 1800.0     # cooldown doubles on each re-open, capped here

    def __init__(self) -> None:
        self.state = self.CLOSED
        self._window: Deque[Tuple[str, str]] = deque(maxlen=self.WINDOW)  # (cls, sig)
        self._consecutive_permanent = 0
        self.opened_at: Optional[float] = None
        self.cooldown = self.BASE_COOLDOWN_S
        self.dominant_error = ""
        self._probe_inflight = False

    # ---------------------------------------------------------- feed results
    def record(self, status: str, error_str: Optional[str], now: float) -> None:
        """Feed ONE completed sub-job's outcome. May trip (CLOSED -> OPEN) or
        resolve a HALF_OPEN probe (-> CLOSED on success, -> OPEN with a longer
        cooldown on failure)."""
        cls = classify(status, error_str)
        sig = signature(error_str) if cls != "ok" else ""
        self._window.append((cls, sig))

        if cls == "permanent":
            self._consecutive_permanent += 1
        else:
            self._consecutive_permanent = 0

        if self.state == self.HALF_OPEN:
            self._probe_inflight = False
            if cls == "ok":
                self._close(now)
            else:
                self._reopen(now, sig or "unknown")
            return

        if self.state == self.CLOSED:
            if self._consecutive_permanent >= self.CONSECUTIVE_PERMANENT_TRIP:
                self._open(now, sig or "unknown")
                return
            self._maybe_trip_on_window(now)

    def _maybe_trip_on_window(self, now: float) -> None:
        if len(self._window) < self.MIN_SAMPLE:
            return
        failures = [sig for cls, sig in self._window if cls != "ok"]
        if not failures:
            return
        fail_fraction = len(failures) / len(self._window)
        if fail_fraction < self.TRANSIENT_TRIP_FRACTION:
            return
        # Homogeneity check: is one signature dominant among the failures?
        counts: dict[str, int] = {}
        for s in failures:
            counts[s] = counts.get(s, 0) + 1
        top_sig, top_count = max(counts.items(), key=lambda kv: kv[1])
        if (top_count / len(failures)) >= self.HOMOGENEOUS_FRACTION:
            self._open(now, top_sig)

    # -------------------------------------------------------------- state transitions
    def _open(self, now: float, dominant_error: str) -> None:
        self.state = self.OPEN
        self.opened_at = now
        self.dominant_error = dominant_error
        self._probe_inflight = False

    def _reopen(self, now: float, dominant_error: str) -> None:
        self.cooldown = min(self.cooldown * 2, self.MAX_COOLDOWN_S)
        self.state = self.OPEN
        self.opened_at = now
        self.dominant_error = dominant_error
        self._probe_inflight = False

    def _close(self, now: float) -> None:
        self.state = self.CLOSED
        self.opened_at = None
        self.cooldown = self.BASE_COOLDOWN_S
        self.dominant_error = ""
        self._consecutive_permanent = 0
        self._probe_inflight = False
        self._window.clear()

    # -------------------------------------------------------------- leasing gate
    def allow_lease(self, now: float) -> Tuple[bool, Optional[int]]:
        """Returns (may_lease, capacity_cap).

        CLOSED -> (True, None): lease normally, no cap.
        OPEN, cooldown not yet elapsed -> (False, None): don't lease at all.
        OPEN, cooldown elapsed -> transitions to HALF_OPEN and returns
          (True, 1): lease exactly one probe sub-job.
        HALF_OPEN, probe already in flight -> (False, None): wait for it to
          resolve before leasing anything else.
        HALF_OPEN, no probe in flight (shouldn't normally happen -- note_leased
          wasn't called after the transition) -> (True, 1): safe to retry.
        """
        if self.state == self.CLOSED:
            return True, None
        if self.state == self.OPEN:
            assert self.opened_at is not None
            if now - self.opened_at >= self.cooldown:
                self.state = self.HALF_OPEN
                self._probe_inflight = False
                return True, 1
            return False, None
        # HALF_OPEN
        if self._probe_inflight:
            return False, None
        return True, 1

    def note_leased(self, n: int) -> None:
        """Called after a successful lease. In HALF_OPEN, marks the probe as
        in flight so allow_lease won't grant a second one before this
        resolves."""
        if self.state == self.HALF_OPEN and n > 0:
            self._probe_inflight = True

    # -------------------------------------------------------------- observability
    @property
    def is_open(self) -> bool:
        return self.state in (self.OPEN, self.HALF_OPEN)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "dominant_error": self.dominant_error,
            "consecutive_permanent": self._consecutive_permanent,
            "cooldown_s": self.cooldown,
        }
