"""JobStore — the Fixer's gig ledger (local SQLite, WAL mode).

Lives **locally on the coordinator**. Workers never touch it; they coordinate over
HTTP. This avoids unreliable SQLite locking over SMB/network filesystems.

Gig states: ``pending`` -> ``leased`` -> ``done`` | ``failed``.
- ``seed`` is idempotent (INSERT OR IGNORE on job_id) — re-seeding is safe.
- ``lease`` atomically hands a batch of pending gigs to one Runner with a TTL.
- ``complete`` marks done/failed; failures re-queue until the retry budget is spent.
- ``reap`` returns expired leases to the pool (self-heal when a Runner dies).
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from . import jsonio


UNGROUPED = "(ungrouped)"


def _group_of(job_id: str) -> str:
    """Campaign/group for a gig: everything before the last '/'. Gigs with no
    '/' are bucketed under ``(ungrouped)`` so every gig has a home."""
    i = job_id.rfind("/")
    return job_id[:i] if i > 0 else UNGROUPED


@dataclass
class LeaseResult:
    lease_id: Optional[str]
    gigs: list[dict[str, Any]]  # [{job_id, spec}]


class JobStore:
    def __init__(self, db_path: str, max_retries: int = 3):
        self.db_path = db_path
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id        TEXT PRIMARY KEY,
                    spec          TEXT NOT NULL,
                    state         TEXT NOT NULL DEFAULT 'pending',
                    lease_id      TEXT,
                    runner_id     TEXT,
                    host          TEXT,
                    attempts      INTEGER NOT NULL DEFAULT 0,
                    leased_at     REAL,
                    lease_deadline REAL,
                    completed_at  REAL,
                    error         TEXT,
                    metrics       TEXT,
                    created_at    REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
                CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(lease_id);
                CREATE INDEX IF NOT EXISTS idx_jobs_completed ON jobs(completed_at);
                -- Campaign metadata: one human-readable label per group slug, so
                -- the dashboard can head a campaign "Converting X 30fps -> 4,8 fps"
                -- instead of showing the raw slug / per-clip rows. Optional: a group
                -- with no label falls back to showing its slug.
                CREATE TABLE IF NOT EXISTS campaigns (
                    grp        TEXT PRIMARY KEY,
                    label      TEXT,
                    created_at REAL NOT NULL
                );
                """
            )
            # Migration: a `grp` column groups gigs into "jobs"/campaigns by the
            # job_id prefix (everything before the last '/'), so the dashboard can
            # show per-campaign progress + a rate curve. Added + backfilled here so
            # databases from before this feature upgrade transparently.
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
            if "grp" not in cols:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN grp TEXT")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_grp ON jobs(grp)")
            # Migration: a `disk` column tags each gig with the physical spindle its
            # input lives on (PLAN §7.6), so /lease can enforce a global per-disk
            # in-flight budget + round-robin across spindles. Nullable: gigs with no
            # disk (no topology, or unmatched) are uncapped — the inert default.
            if "disk" not in cols:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN disk TEXT")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_disk ON jobs(disk)")
            self._conn.commit()
            self._backfill_groups()

    def _backfill_groups(self) -> None:
        rows = self._conn.execute(
            "SELECT job_id FROM jobs WHERE grp IS NULL"
        ).fetchall()
        if not rows:
            return
        self._conn.executemany(
            "UPDATE jobs SET grp=? WHERE job_id=?",
            [(_group_of(r["job_id"]), r["job_id"]) for r in rows],
        )
        self._conn.commit()

    # ---------------------------------------------------------------- seed
    def seed(self, gigs: list[dict[str, Any]],
             group: Optional[str] = None,
             label: Optional[str] = None) -> int:
        """Insert gigs idempotently. Each gig: {job_id, spec, group?}.

        ``group`` (per-gig or batch-wide) overrides the job_id-prefix grouping, so
        a whole campaign can be seeded under one readable slug regardless of how
        the ``job_id``s are shaped. ``label`` is a human-readable name for that
        campaign, stored once in the ``campaigns`` table and shown in the UI.

        Returns # inserted.
        """
        now = time.time()
        rows = []
        for g in gigs:
            jid = g["job_id"]
            grp = g.get("group") or group or _group_of(jid)
            disk = g.get("disk")
            rows.append((jid, jsonio.dumps(g.get("spec", {})), grp, disk, now))
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO jobs (job_id, spec, grp, disk, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            # Count gig inserts only — *before* the campaign upsert, so the label
            # row doesn't inflate the reported "inserted" count.
            inserted = self._conn.total_changes - before
            if label:
                lbl_grp = self._label_group(gigs, group)
                if lbl_grp:
                    self._conn.execute(
                        "INSERT INTO campaigns (grp, label, created_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(grp) DO UPDATE SET label=excluded.label",
                        (lbl_grp, label, now),
                    )
            self._conn.commit()
            return inserted

    @staticmethod
    def _label_group(gigs: list[dict[str, Any]],
                     group: Optional[str]) -> Optional[str]:
        """The group slug a batch-wide ``label`` should attach to.

        An explicit batch ``group`` wins. Otherwise, if every gig resolves to the
        same effective group, that one is used; a mixed batch has no single home
        for the label, so we return None (and skip labelling rather than mislabel).
        """
        if group:
            return group
        if not gigs:
            return None
        eff = {g.get("group") or _group_of(g["job_id"]) for g in gigs}
        return next(iter(eff)) if len(eff) == 1 else None

    def mark_done_existing(self, job_ids: list[str]) -> int:
        """Mark gigs done without execution (e.g. output already exists = resume)."""
        now = time.time()
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "UPDATE jobs SET state='done', completed_at=?, error=NULL "
                "WHERE job_id=? AND state!='done'",
                [(now, jid) for jid in job_ids],
            )
            self._conn.commit()
            return self._conn.total_changes - before

    # --------------------------------------------------------------- lease
    def lease(self, runner_id: str, host: str, capacity: int, ttl: float,
              disk_concurrency: Optional[dict[str, int]] = None) -> LeaseResult:
        """Lease up to ``capacity`` pending gigs atomically.

        With ``disk_concurrency`` (the mesh-global per-spindle budget, only the
        Fixer can supply), candidate gigs are **round-robin interleaved across
        disks** (every spindle fed from the first lease) and a **per-disk in-flight
        cap** is enforced: never lease beyond ``budget[disk]`` gigs currently leased
        across the *whole fleet*. This is the distributed DiskSemaphore — possible
        only because the ledger is central. Disks not in the map (and ``None``) are
        uncapped. Without ``disk_concurrency`` the selection is plain "first N
        pending" (the inert default).
        """
        now = time.time()
        lease_id = uuid.uuid4().hex
        with self._lock:
            if not disk_concurrency:
                cur = self._conn.execute(
                    "SELECT job_id, spec, disk FROM jobs WHERE state='pending' "
                    "ORDER BY created_at LIMIT ?",
                    (capacity,),
                )
                rows = cur.fetchall()
                if not rows:
                    return LeaseResult(lease_id=None, gigs=[])
                return self._finalize_lease(rows, lease_id, runner_id, host, now, ttl)

            # --- disk-aware path ---
            # 1. current in-flight per disk (leased gigs across the whole fleet)
            inflight: dict[Optional[str], int] = {}
            for r in self._conn.execute(
                "SELECT disk, COUNT(*) AS c FROM jobs WHERE state='leased' GROUP BY disk"
            ):
                inflight[r["disk"]] = r["c"]
            # 2. pending gigs with their disk, in creation order
            rows = self._conn.execute(
                "SELECT job_id, spec, disk FROM jobs WHERE state='pending' "
                "ORDER BY created_at"
            ).fetchall()
            if not rows:
                return LeaseResult(lease_id=None, gigs=[])

            # 3. split into capped disks (in the budget map) vs uncapped (None / unknown)
            capped: dict[str, list] = {}
            uncapped: list = []
            for r in rows:
                d = r["disk"]
                if d is not None and d in disk_concurrency:
                    capped.setdefault(d, []).append(r)
                else:
                    uncapped.append(r)

            # 4. round-robin interleave: one per capped disk (if budget remains) +
            #    one from the uncapped stream, per round — keeps every spindle busy
            #    and never starves uncapped gigs.
            iters = {d: iter(capped[d]) for d in capped}
            unc_iter = iter(uncapped)
            unc_active = bool(uncapped)
            selected: list = []
            while len(selected) < capacity:
                progressed = False
                for d in list(iters):
                    remaining = disk_concurrency[d] - inflight.get(d, 0)
                    if remaining <= 0:
                        continue
                    try:
                        selected.append(next(iters[d]))
                    except StopIteration:
                        del iters[d]
                        continue
                    inflight[d] = inflight.get(d, 0) + 1
                    progressed = True
                    if len(selected) >= capacity:
                        break
                if unc_active and len(selected) < capacity:
                    try:
                        selected.append(next(unc_iter))
                        progressed = True
                    except StopIteration:
                        unc_active = False
                if not progressed:
                    break  # all capped disks saturated/exhausted and uncapped drained

            if not selected:
                return LeaseResult(lease_id=None, gigs=[])
            return self._finalize_lease(selected, lease_id, runner_id, host, now, ttl)

    def _finalize_lease(self, rows, lease_id, runner_id, host, now, ttl) -> LeaseResult:
        ids = [r["job_id"] for r in rows]
        self._conn.executemany(
            "UPDATE jobs SET state='leased', lease_id=?, runner_id=?, host=?, "
            "leased_at=?, lease_deadline=?, attempts=attempts+1 WHERE job_id=?",
            [(lease_id, runner_id, host, now, now + ttl, jid) for jid in ids],
        )
        self._conn.commit()
        gigs = [{"job_id": r["job_id"], "spec": jsonio.loads(r["spec"]),
                 "disk": r["disk"]} for r in rows]
        return LeaseResult(lease_id=lease_id, gigs=gigs)

    # ------------------------------------------------------------ complete
    def complete(self, results: list[dict[str, Any]]) -> dict[str, int]:
        """Apply a batch of results. Each: {job_id, status, error?, metrics?}.

        status 'ok'/'skipped' -> done. 'requeue' -> back to pending WITHOUT
        consuming the retry budget (an eviction under pressure, not a task
        failure — e.g. an at-field pause mid-batch). Otherwise (error) re-queue
        (pending) until attempts exceed max_retries, then mark failed.
        """
        now = time.time()
        done = requeued = failed = 0
        with self._lock:
            for r in results:
                jid = r["job_id"]
                status = r.get("status", "ok")
                if status in ("ok", "skipped"):
                    self._conn.execute(
                        "UPDATE jobs SET state='done', completed_at=?, error=NULL, "
                        "metrics=?, lease_id=NULL, lease_deadline=NULL WHERE job_id=?",
                        (now, jsonio.dumps(r.get("metrics", {})), jid),
                    )
                    done += 1
                elif status == "requeue":
                    # Eviction: return to pending immediately. Undo the lease's
                    # attempts+1 so a flapping pressure pause can't exhaust the
                    # retry budget and mark a healthy gig 'failed' — the task was
                    # preempted, not actually tried. error is cleared (not a fault).
                    self._conn.execute(
                        "UPDATE jobs SET state='pending', error=NULL, "
                        "attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END, "
                        "lease_id=NULL, lease_deadline=NULL WHERE job_id=?",
                        (jid,),
                    )
                    requeued += 1
                else:
                    row = self._conn.execute(
                        "SELECT attempts FROM jobs WHERE job_id=?", (jid,)
                    ).fetchone()
                    attempts = row["attempts"] if row else 0
                    if attempts > self.max_retries:
                        self._conn.execute(
                            "UPDATE jobs SET state='failed', completed_at=?, error=?, "
                            "lease_id=NULL, lease_deadline=NULL WHERE job_id=?",
                            (now, str(r.get("error", "unknown"))[:2000], jid),
                        )
                        failed += 1
                    else:
                        self._conn.execute(
                            "UPDATE jobs SET state='pending', error=?, "
                            "lease_id=NULL, lease_deadline=NULL WHERE job_id=?",
                            (str(r.get("error", "unknown"))[:2000], jid),
                        )
                        requeued += 1
            self._conn.commit()
        return {"done": done, "requeued": requeued, "failed": failed}

    def requeue(self, states: tuple[str, ...] = ("failed",),
                reset_attempts: bool = True) -> int:
        """Return gigs in the given states to ``pending``. Returns # requeued.

        Lets an operator recover from a *systematic* failure (a missing
        dependency, an unreachable NAS, a misconfigured root) after fixing the
        root cause — without re-seeding under fresh ``job_id``s. ``leased`` is a
        valid target too, to forcibly reclaim gigs from a wedged runner without
        waiting out the lease TTL.
        """
        states = tuple(states) or ("failed",)
        valid = {"failed", "leased", "pending", "done"}
        states = tuple(s for s in states if s in valid)
        if not states:
            return 0
        placeholders = ",".join("?" for _ in states)
        attempts_clause = ", attempts=0" if reset_attempts else ""
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE jobs SET state='pending', lease_id=NULL, runner_id=NULL, "
                f"leased_at=NULL, lease_deadline=NULL, error=NULL{attempts_clause} "
                f"WHERE state IN ({placeholders})",
                states,
            )
            self._conn.commit()
            return cur.rowcount

    def heartbeat(self, lease_id: str, ttl: float) -> int:
        """Extend the deadline for all gigs still held under this lease."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET lease_deadline=? WHERE lease_id=? AND state='leased'",
                (now + ttl, lease_id),
            )
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------- self-heal
    def reap(self) -> int:
        """Return expired leases to the pending pool. Returns # reaped."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET state='pending', lease_id=NULL, runner_id=NULL, "
                "lease_deadline=NULL WHERE state='leased' AND lease_deadline < ?",
                (now,),
            )
            self._conn.commit()
            return cur.rowcount

    # ----------------------------------------------------------------- stats
    def stats(self, window_s: float = 60.0) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            counts = {
                row["state"]: row["n"]
                for row in self._conn.execute(
                    "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"
                )
            }
            recent = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE state='done' AND completed_at >= ?",
                (now - window_s,),
            ).fetchone()["n"]
            per_host = [
                {"host": row["host"] or "?", "in_flight": row["n"]}
                for row in self._conn.execute(
                    "SELECT host, COUNT(*) AS n FROM jobs WHERE state='leased' GROUP BY host"
                )
            ]
            per_host_recent = {
                (row["host"] or "?"): row["n"]
                for row in self._conn.execute(
                    "SELECT host, COUNT(*) AS n FROM jobs "
                    "WHERE state='done' AND completed_at >= ? GROUP BY host",
                    (now - window_s,),
                )
            }
            # Per-disk in-flight (leased gigs per spindle, across the whole fleet).
            # The dashboard shows this vs the budget so you can SEE every spindle
            # saturated and spot a cold or thrashing disk (PLAN §7.6 N6).
            disk_inflight = {
                (row["disk"] or "(uncapped)"): row["n"]
                for row in self._conn.execute(
                    "SELECT disk, COUNT(*) AS n FROM jobs "
                    "WHERE state='leased' AND disk IS NOT NULL GROUP BY disk"
                )
            }
            recent_errors = [
                {"job_id": row["job_id"], "host": row["host"], "error": row["error"]}
                for row in self._conn.execute(
                    "SELECT job_id, host, error FROM jobs WHERE state='failed' "
                    "ORDER BY completed_at DESC LIMIT 20"
                )
            ]

        pending = counts.get("pending", 0)
        leased = counts.get("leased", 0)
        done = counts.get("done", 0)
        failed = counts.get("failed", 0)
        total = pending + leased + done + failed
        rate = recent / window_s if window_s > 0 else 0.0
        remaining = pending + leased
        eta_s = remaining / rate if rate > 0 else None

        for h in per_host:
            h["recent_rate"] = per_host_recent.get(h["host"], 0) / window_s

        return {
            "total": total,
            "pending": pending,
            "leased": leased,
            "done": done,
            "failed": failed,
            "rate_per_s": round(rate, 3),
            "window_s": window_s,
            "remaining": remaining,
            "eta_s": round(eta_s, 1) if eta_s is not None else None,
            "per_host": per_host,
            "disk_inflight": disk_inflight,
            "recent_errors": recent_errors,
            "ts": now,
        }

    # ------------------------------------------------------------- listing
    def disk_done_counts(self) -> dict[str, int]:
        """``{disk_id: done_count}`` — for the throughput sampler to derive per-disk
        rate over time (PLAN §7.6 N6). Only disks with at least one done gig."""
        with self._lock:
            return {
                row["disk"]: row["n"]
                for row in self._conn.execute(
                    "SELECT disk, COUNT(*) AS n FROM jobs "
                    "WHERE state='done' AND disk IS NOT NULL GROUP BY disk"
                )
            }

    def disk_inflight_count(self, disk: str) -> int:
        """Count of leased (in-flight) gigs on a specific disk — for the resource
        governor to share the per-disk read budget between gigs and external
        resource-acquire clients."""
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE state='leased' AND disk=?",
                (disk,)).fetchone()
            return r["c"] if r else 0

    _LIST_COLS = ("job_id", "state", "host", "runner_id", "attempts",
                  "created_at", "leased_at", "completed_at", "error", "metrics",
                  "grp", "disk")

    def list_jobs(self, states: Optional[tuple[str, ...]] = None,
                  limit: int = 200, newest_first: bool = True,
                  grp: Optional[str] = None) -> list[dict[str, Any]]:
        """Return job rows (for the /jobs + /history views). Newest by activity.

        ``grp`` optionally filters to one campaign (the ``grp`` column set at
        seed time). Without it, rows from every campaign are mixed (the existing
        behavior for the dashboard).
        """
        cols = ", ".join(self._LIST_COLS)
        order = "DESC" if newest_first else "ASC"
        # sort by most-recent activity timestamp
        order_expr = ("COALESCE(completed_at, leased_at, created_at) " + order)
        params: list[Any] = []
        clauses: list[str] = []
        if states:
            clauses.append("state IN (%s)" % ",".join("?" for _ in states))
            params.extend(states)
        if grp:
            clauses.append("grp = ?")
            params.append(grp)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {cols} FROM jobs {where} ORDER BY {order_expr} LIMIT ?",
                params,
            ).fetchall()
        out = []
        for r in rows:
            d = {k: r[k] for k in self._LIST_COLS}
            try:
                d["metrics"] = jsonio.loads(d["metrics"]) if d["metrics"] else {}
            except Exception:  # noqa: BLE001
                d["metrics"] = {}
            out.append(d)
        return out

    def export_metrics(self, grp: Optional[str] = None,
                       states: tuple[str, ...] = ("done",),
                       limit: int = 100000) -> list[dict[str, Any]]:
        """Bulk export of (job_id, metrics, state) for result aggregation.

        Unlike :meth:`list_jobs` (dashboard-shaped, capped at 2000), this is
        for consumers that need to fold metrics across a whole campaign -- e.g.
        ranking 32k clips by worst-section MPJPE. Returns lightweight rows
        (job_id, metrics, state, grp, disk) ordered by job_id (deterministic),
        so a caller can page by job_id if the set is huge. ``limit`` defaults
        high (100k) since a single campaign rarely exceeds that.
        """
        cols = "job_id, metrics, state, grp, disk"
        params: list[Any] = []
        clauses: list[str] = []
        if states:
            clauses.append("state IN (%s)" % ",".join("?" for _ in states))
            params.extend(states)
        if grp:
            clauses.append("grp = ?")
            params.append(grp)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {cols} FROM jobs {where} ORDER BY job_id LIMIT ?",
                params,
            ).fetchall()
        out = []
        for r in rows:
            try:
                m = jsonio.loads(r["metrics"]) if r["metrics"] else {}
            except Exception:  # noqa: BLE001
                m = {}
            out.append({"job_id": r["job_id"], "metrics": m,
                        "state": r["state"], "grp": r["grp"], "disk": r["disk"]})
        return out

    def group_stats(self, limit: int = 200) -> list[dict[str, Any]]:
        """Per-campaign rollup ("jobs" for the UI): counts by state + timing.

        A "job" here is a group of gigs sharing a ``job_id`` prefix. Returns the
        most-recently-active groups first.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT j.grp AS grp,
                       c.label AS label,
                       COUNT(*)                                   AS total,
                       SUM(j.state='done')                        AS done,
                       SUM(j.state='pending')                     AS pending,
                       SUM(j.state='leased')                      AS leased,
                       SUM(j.state='failed')                      AS failed,
                       MIN(j.created_at)                          AS first_created,
                       MAX(j.leased_at)                           AS last_leased,
                       MAX(j.completed_at)                        AS last_completed,
                       GROUP_CONCAT(DISTINCT j.runner_id)         AS runner_ids
                FROM jobs j
                LEFT JOIN campaigns c ON c.grp = j.grp
                GROUP BY j.grp
                ORDER BY COALESCE(MAX(j.completed_at), MAX(j.leased_at), MIN(j.created_at)) DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        out = []
        for r in rows:
            d = {k: r[k] for k in r.keys()}
            for k in ("total", "done", "pending", "leased", "failed"):
                d[k] = int(d[k] or 0)
            rid = d.pop("runner_ids", None)
            d["runner_ids"] = [x for x in (rid.split(",") if rid else []) if x]
            out.append(d)
        return out

    def job(self, job_id: str) -> Optional[dict[str, Any]]:
        cols = ", ".join(self._LIST_COLS + ("spec",))
        with self._lock:
            r = self._conn.execute(
                f"SELECT {cols} FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if not r:
            return None
        d = {k: r[k] for k in self._LIST_COLS}
        try:
            d["metrics"] = jsonio.loads(d["metrics"]) if d["metrics"] else {}
        except Exception:  # noqa: BLE001
            d["metrics"] = {}
        try:
            d["spec"] = jsonio.loads(r["spec"]) if r["spec"] else {}
        except Exception:  # noqa: BLE001
            d["spec"] = {}
        return d

    def close(self) -> None:
        with self._lock:
            self._conn.close()
