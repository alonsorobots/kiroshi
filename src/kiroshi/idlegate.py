"""kiroshi.idlegate — admit background gigs only when the HDD array is quiet.

A job can be marked *idle-gated*: the Coordinator withholds its leases until the
target disks have stayed below a utilization threshold for a sustained window.
This is what lets a write-back "demote NVMe → sharded HDD" job sit mostly idle
and only flush when the array is free (see docs/DEMOTE.md).

The logic here is deliberately **pure + stateful-in-one-object** so it can be
unit-tested without a Coordinator, an IOWatcher, or wall-clock sleeps. The
Coordinator owns one :class:`IdleGateTracker` on ``app.state`` and calls
``evaluate`` on every ``/lease`` for a gated job.

Hysteresis (the "30 minutes" part):
    * Read the IOWatcher rolling snapshot (per-disk 5-min avg ``util_pct``).
    * ``cur = max(util_pct)`` over the gate's disks (all HDD disks if unset).
    * ``cur <= util_pct``  -> quiet; set ``quiet_since`` if not already set.
    * ``cur >  util_pct``  -> breach; **reset** ``quiet_since`` (clock restarts).
    * Admit iff ``quiet_since`` set AND ``now - quiet_since >= sustain_s``.

Fail-open, loudly: if the snapshot has no usable disk telemetry (Windows
coordinator, or a topology with no HDD disks), the gate can't judge idle, so it
admits with reason ``IDLE_GATE_NO_TELEMETRY`` rather than stalling forever.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

# Lease decision reasons this module can produce (surfaced in /status + decisions).
REASON_OPEN = "IDLE_GATE_OPEN"          # array quiet long enough -> normal leasing
REASON_WAIT = "IDLE_GATE_WAIT"          # array busy or not-yet-sustained -> hold
REASON_NO_TELEMETRY = "IDLE_GATE_NO_TELEMETRY"  # can't judge -> fail open

# Defaults if a gate config omits fields.
DEFAULT_UTIL_PCT = 15.0
DEFAULT_SUSTAIN_S = 1800.0   # 30 minutes


def normalize_gate(cfg: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Validate/normalize a raw idle_gate config. Returns None if not gated.

    Accepts ``sustain_s`` (seconds) or ``sustain_min`` (minutes, convenience).
    ``disks`` may be a list of disk ids or None/empty meaning "all HDD disks".
    """
    if not cfg:
        return None
    disks = cfg.get("disks") or None
    if disks is not None:
        disks = [str(d) for d in disks]
        if not disks:
            disks = None
    sustain_s = cfg.get("sustain_s")
    if sustain_s is None and cfg.get("sustain_min") is not None:
        sustain_s = float(cfg["sustain_min"]) * 60.0
    if sustain_s is None:
        sustain_s = DEFAULT_SUSTAIN_S
    util = cfg.get("util_pct")
    return {
        "disks": disks,
        "util_pct": float(util if util is not None else DEFAULT_UTIL_PCT),
        "sustain_s": float(sustain_s),
    }


def _relevant_utils(snapshot: Optional[dict[str, Any]],
                    disks: Optional[list[str]]) -> list[float]:
    """Extract per-disk util_pct for the gate's disks from an IOWatcher snapshot.

    Returns [] when there is no usable telemetry (inert watcher / no matching
    disks with samples).
    """
    if not snapshot:
        return []
    rows = snapshot.get("disks") or []
    out: list[float] = []
    for r in rows:
        if disks is not None and r.get("disk") not in disks:
            continue
        # A disk with <2 samples reports util 0 but isn't real telemetry; the
        # watcher marks that via the "samples" field.
        if int(r.get("samples", 0)) < 2:
            continue
        out.append(float(r.get("util_pct", 0.0)))
    return out


@dataclass
class GateResult:
    admit: bool
    reason: str
    cur_util: Optional[float]        # max util across gate disks (None if no data)
    quiet_for_s: float               # how long the array has been quiet (0 if busy)
    sustain_s: float
    util_pct: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "admit": self.admit,
            "reason": self.reason,
            "cur_util": self.cur_util,
            "quiet_for_s": round(self.quiet_for_s, 1),
            "sustain_s": self.sustain_s,
            "util_pct": self.util_pct,
        }


@dataclass
class IdleGateTracker:
    """Holds ``quiet_since`` per job. One instance lives on the Coordinator."""
    _quiet_since: dict[str, Optional[float]] = field(default_factory=dict)
    _last: dict[str, GateResult] = field(default_factory=dict)

    def evaluate(self, job: str, gate: dict[str, Any],
                 snapshot: Optional[dict[str, Any]],
                 now: Optional[float] = None) -> GateResult:
        """Update hysteresis for ``job`` and decide whether to admit leases.

        ``gate`` must be a normalized config (see :func:`normalize_gate`).
        """
        now = time.time() if now is None else now
        utils = _relevant_utils(snapshot, gate.get("disks"))
        if not utils:
            # No telemetry to judge idle — fail open (don't stall a job forever).
            res = GateResult(True, REASON_NO_TELEMETRY, None, 0.0,
                             gate["sustain_s"], gate["util_pct"])
            self._last[job] = res
            return res

        cur = max(utils)
        if cur <= gate["util_pct"]:
            qs = self._quiet_since.get(job)
            if qs is None:
                qs = now
                self._quiet_since[job] = qs
            quiet_for = max(0.0, now - qs)
            admit = quiet_for >= gate["sustain_s"]
        else:
            # Breach — restart the clock.
            self._quiet_since[job] = None
            quiet_for = 0.0
            admit = False

        res = GateResult(admit, REASON_OPEN if admit else REASON_WAIT,
                         cur, quiet_for, gate["sustain_s"], gate["util_pct"])
        self._last[job] = res
        return res

    def last(self, job: str) -> Optional[GateResult]:
        return self._last.get(job)

    def forget(self, job: str) -> None:
        self._quiet_since.pop(job, None)
        self._last.pop(job, None)
