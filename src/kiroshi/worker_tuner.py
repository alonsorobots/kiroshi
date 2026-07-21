"""Adaptive worker-count ramping — replaces static ``--workers N`` guessing.

Directly targets the failure mode that dominated the held-frames-4fps
campaign: worker counts were hand-tuned by trial and error, crash after
crash (16->6->8->6 on one machine, 8->3 on another), because there was no
principled way to know what a machine could actually handle.

Framing that keeps this safe and simple:

* **This controller is an optimization, not a safety mechanism.** AT-Field
  remains the hard floor -- if the controller overshoots, AT-Field kills as
  it does today. So a bug here is non-catastrophic (a bad decision just
  means AT-Field does what it already does); the controller does not need
  to be provably correct, only "usually avoids the kill."
* **Overshoot is catastrophic (a kill loses all in-flight work), undershoot
  is cheap (just slower).** So the control law is deliberately risk-averse:
  slow to grow, fast to shrink -- textbook AIMD (additive-increase /
  multiplicative-decrease), the same shape TCP congestion control uses for
  exactly this "probe up slowly, back off hard" problem.

Two knobs, both already present in ``pool.py``:

1. Process count (``LocalPool.workers`` / ``LocalPool.resize()``) -- the
   slow, structural knob. Only ever adjusted BETWEEN batches (the runner's
   lease -> run_batch -> complete loop guarantees zero in-flight work
   there, so resizing is completely lossless).
2. ``run_batch``'s ``max_pending`` -- the fast, lossless brake. Passed as a
   live callable; lowering it mid-batch stops new submissions and lets
   in-flight gigs drain, no rebuild, no lost work.

The control law (``WorkerTuner.step``) is a pure function of
``(headroom, now)`` plus internal state, deliberately separated from the
HTTP polling (``poll_headroom``) so it can be unit-tested with synthetic
headroom traces without a real AT-Field instance.
"""
from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger("kiroshi.worker_tuner")

DEFAULT_HEADROOM_URL = "http://127.0.0.1:8765/headroom"


class WorkerTuner:
    # Hysteresis band: hold steady between DANGER and SAFE. Widely separated
    # on purpose so noisy headroom readings don't cause rebuild-thrash.
    SAFE = 0.35
    DANGER = 0.15

    # Additive increase: only grow after this many CONSECUTIVE safe polls,
    # and only once we're past the post-decrease cooldown.
    INCREASE_CONFIRM = 2
    # Multiplicative decrease factor (25% cut) applied immediately on danger.
    DECREASE_FACTOR = 0.75
    # No growth for this long after any decrease -- give the system time to
    # actually settle before probing back up.
    DECREASE_COOLDOWN_S = 180.0
    # Minimum time between actual pool rebuilds, independent of the above --
    # a second guard against thrash even if the state machine misbehaves.
    MIN_RESIZE_INTERVAL_S = 60.0
    # Consecutive between-batch holds (no change, in-band) before persisting
    # the current target as "converged."
    CONVERGE_HOLDS = 3

    POLL_TIMEOUT_S = 2.0

    def __init__(
        self,
        floor: int = 1,
        ceiling: int = 8,
        *,
        headroom_url: str = DEFAULT_HEADROOM_URL,
        cache_path: Optional[Path] = None,
    ):
        self.floor = max(1, floor)
        self.ceiling = max(self.floor, ceiling)
        self.headroom_url = headroom_url
        self.cache_path = cache_path

        # None = not yet determined whether AT-Field is reachable at all.
        # False after the first successful probe fails -- disables the
        # tuner for good this run (never auto-tune blind; see module doc).
        self.enabled: Optional[bool] = None

        self._consecutive_safe = 0
        self._consecutive_hold = 0
        self._last_decrease_at: Optional[float] = None  # None = never decreased yet
        self._last_resize_at = 0.0
        self._braking = False

        self.target = self._load_initial_target()

    # ------------------------------------------------------------ startup
    def _load_initial_target(self) -> int:
        cached = self._load_cache()
        if cached and cached.get("workers"):
            # Start just below last-known-good rather than from the floor --
            # skips re-discovering the safe level from scratch every launch,
            # while still re-probing (not blindly trusting a possibly-stale
            # number if the workload or machine changed).
            start = max(self.floor, math.floor(int(cached["workers"]) * 0.8))
        else:
            start = self.floor
        return min(start, self.ceiling)

    def _load_cache(self) -> Optional[dict]:
        if self.cache_path is None or not self.cache_path.is_file():
            return None
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save_cache(self, headroom: float) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "workers": self.target,
                "updated_at": time.time(),
                "headroom_at_converge": headroom,
            }
            tmp = self.cache_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            _log.debug("failed to persist worker-tuning cache", exc_info=True)

    # ------------------------------------------------------------- polling
    def poll_headroom(self) -> Optional[float]:
        """GET AT-Field's local /headroom. None on any failure (unreachable,
        not installed, API disabled, malformed response) -- never raises."""
        try:
            import requests

            r = requests.get(self.headroom_url, timeout=self.POLL_TIMEOUT_S)
            r.raise_for_status()
            data = r.json()
            v = data.get("min_headroom")
            return float(v) if v is not None else None
        except Exception:
            return None

    # --------------------------------------------------- pure control law
    def step(self, headroom: Optional[float], now: float) -> int:
        """Advance the AIMD state machine one tick. Returns the new target
        worker count (may be unchanged). Pure given ``(headroom, now)`` plus
        the tuner's own accumulated state -- no I/O, unit-testable directly.

        ``headroom is None`` means AT-Field is unreachable: disable the
        tuner (freeze at whatever target it last held) rather than guess.
        This is a deliberate one-way latch for the run's lifetime -- see
        module docstring "never auto-tune blind." A transient blip doesn't
        re-enable it, since flapping the controller's authority on and off
        is its own source of instability; a fresh run re-probes from
        scratch.
        """
        if headroom is None:
            self.enabled = False
            return self.target
        self.enabled = True

        if headroom < self.DANGER:
            new_target = max(self.floor, math.ceil(self.target * self.DECREASE_FACTOR))
            if new_target < self.target:
                self.target = new_target
            self._last_decrease_at = now
            self._consecutive_safe = 0
            self._consecutive_hold = 0
        elif headroom > self.SAFE:
            in_cooldown = (
                self._last_decrease_at is not None
                and now - self._last_decrease_at < self.DECREASE_COOLDOWN_S
            )
            if in_cooldown:
                # Still cooling down from a recent cut -- let things settle
                # before probing back up, even though this reading is safe.
                self._consecutive_hold = 0
            else:
                self._consecutive_safe += 1
                self._consecutive_hold = 0
                if self._consecutive_safe >= self.INCREASE_CONFIRM:
                    if self.target < self.ceiling:
                        self.target += 1
                    self._consecutive_safe = 0
        else:
            # Hysteresis band: hold. Reset the safe-streak (a dip back into
            # the band shouldn't count toward the next increase) but track
            # holds for convergence detection.
            self._consecutive_safe = 0
            self._consecutive_hold += 1
            if self._consecutive_hold >= self.CONVERGE_HOLDS:
                self._save_cache(headroom)
                self._consecutive_hold = 0

        return self.target

    # ------------------------------------------------- between-batch entry
    def update_between_batches(self) -> int:
        """Call once between run_batch() calls (guaranteed zero in-flight
        work there). Polls AT-Field and advances the control law."""
        headroom = self.poll_headroom()
        return self.step(headroom, time.time())

    def maybe_resize(self, pool) -> bool:
        """Apply ``self.target`` to ``pool`` if it differs. Returns True if a
        resize happened.

        The ``MIN_RESIZE_INTERVAL_S`` thrash guard applies ONLY to growth.
        Shrinking must never be rate-limited: the whole point of "fast to
        shrink" is that a pool sitting at a too-high process count while
        real danger is measured must come down immediately, not wait out an
        interval meant to stop rapid *growth* churn. (The mid-batch brake in
        ``max_pending_cap`` already provides a faster, sub-interval reaction
        by freezing new submissions -- this is the structural follow-through,
        which must not lag behind it.)
        """
        if not self.enabled:
            return False
        if pool.workers == self.target:
            return False
        now = time.time()
        shrinking = self.target < pool.workers
        if not shrinking and now - self._last_resize_at < self.MIN_RESIZE_INTERVAL_S:
            return False
        pool.resize(self.target)
        self._last_resize_at = now
        return True

    # ----------------------------------------------------- mid-batch brake
    def mid_batch_check(self) -> None:
        """Cheap headroom re-check to arm/disarm the fast brake. Meant to be
        called from the runner's existing heartbeat callback (~30s cadence)
        so a burst of unusually heavy gigs within one batch (e.g. several
        4K-portrait clips landing together) gets a faster reaction than
        waiting for the next between-batch AIMD step."""
        headroom = self.poll_headroom()
        if headroom is None:
            return  # don't change brake state on a transient poll failure
        self._braking = headroom < self.DANGER

    def max_pending_cap(self, default_cap: int) -> int:
        """The live value handed to ``run_batch(max_pending=...)``. Trickle
        (effectively frozen) while braking; otherwise generous enough to
        never be the actual constraint (real concurrency is bounded by the
        pool's process count, not this)."""
        if self._braking:
            return 1
        return max(default_cap, self.ceiling * 2)
