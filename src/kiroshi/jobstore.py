"""JobStore — the Coordinator's sub-job ledger (local SQLite, WAL mode).

Lives **locally on the coordinator**. Workers never touch it; they coordinate over
HTTP. This avoids unreliable SQLite locking over SMB/network filesystems.

Sub-job states: ``pending`` -> ``leased`` -> ``done`` | ``failed``.
- ``seed`` is idempotent (INSERT OR IGNORE on subjob_id) — re-seeding is safe.
- ``lease`` atomically hands a batch of pending gigs to one Runner with a TTL.
- ``complete`` marks done/failed; failures re-queue until the retry budget is spent.
- ``reap`` returns expired leases to the pool (self-heal when a Runner dies).
"""
from __future__ import annotations
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from . import jsonio


UNGROUPED = "(ungrouped)"


def _job_of(subjob_id: str) -> str:
    """Job/job for a sub-job: everything before the last '/'. Gigs with no
    '/' are bucketed under ``(ungrouped)`` so every sub-job has a home."""
    i = subjob_id.rfind("/")
    return subjob_id[:i] if i > 0 else UNGROUPED


@dataclass
class LeaseResult:
    lease_id: Optional[str]
    gigs: list[dict[str, Any]]  # [{subjob_id, spec}]
    # Diagnostics explaining *why* this lease returned the count it did.
    # ``None`` on the inert default (no fair-share, no disk budget) for callers
    # that don't care; populated whenever a decision constraint was evaluated.
    diag: Optional[dict[str, Any]] = None


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
        # Register a REGEXP function so WHERE subjob_id REGEXP ? works in-DB
        # (lets /jobs filter server-side without shipping 100k rows to grep).
        self._conn.create_function(
            "regexp", 2,
            lambda pat, val: 1 if val is not None and re.search(pat, val) else 0)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            # Auto-migrate old-schema DBs (jobs→subjobs, campaigns→jobs)
            from .dbmigrate import needs_migration, migrate
            if needs_migration(self._conn):
                migrate(self._conn)

            # Create tables (IF NOT EXISTS = no-op if already present)
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS subjobs (
                    subjob_id      TEXT PRIMARY KEY,
                    spec           TEXT NOT NULL,
                    job            TEXT,
                    disk           TEXT,
                    state          TEXT NOT NULL DEFAULT 'pending',
                    lease_id       TEXT,
                    runner_id      TEXT,
                    host           TEXT,
                    attempts       INTEGER NOT NULL DEFAULT 0,
                    leased_at      REAL,
                    lease_deadline REAL,
                    completed_at   REAL,
                    error          TEXT,
                    metrics        TEXT,
                    created_at     REAL NOT NULL
                );
                -- Job metadata: one human-readable label per job, so the dashboard
                -- can show "Converting X 30fps -> 4,8 fps" instead of the raw slug.
                CREATE TABLE IF NOT EXISTS jobs (
                    job         TEXT PRIMARY KEY,
                    label       TEXT,
                    created_at  REAL NOT NULL
                );
                -- AT-Field kill/pressure events, pushed in by each machine's
                -- AT-Field instance (POST /atfield/event). Lets the coordinator
                -- explain WHY a runner disappeared (killed for pagefile
                -- pressure, say) instead of a heartbeat timeout looking
                -- identical to a crash or a wedge.
                CREATE TABLE IF NOT EXISTS atfield_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    host        TEXT,
                    rule        TEXT,
                    signal      TEXT,
                    threshold   REAL,
                    action      TEXT,
                    succeeded   INTEGER,
                    kill_pid    INTEGER,
                    kill_name   TEXT,
                    skipped_reason TEXT,
                    ts          REAL NOT NULL,
                    received_at REAL NOT NULL
                );
                """
            )
            # Column-level migration: if subjobs table existed without disk/job
            # columns (from a pre-rename DB that was migrated by dbmigrate but
            # didn't have these columns), add them transparently BEFORE creating
            # indexes that reference them.
            sj_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(subjobs)")}
            if "disk" not in sj_cols:
                self._conn.execute("ALTER TABLE subjobs ADD COLUMN disk TEXT")
            if "job" not in sj_cols:
                self._conn.execute("ALTER TABLE subjobs ADD COLUMN job TEXT")
            # idle_gate: per-job JSON config that parks a background job's leases
            # until the HDD array is quiet (see idlegate.py + docs/DEMOTE.md).
            job_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
            if "idle_gate" not in job_cols:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN idle_gate TEXT")
            # Now create indexes (columns are guaranteed to exist)
            self._conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_subjobs_state ON subjobs(state);
                CREATE INDEX IF NOT EXISTS idx_subjobs_lease ON subjobs(lease_id);
                CREATE INDEX IF NOT EXISTS idx_subjobs_completed ON subjobs(completed_at);
                CREATE INDEX IF NOT EXISTS idx_subjobs_job ON subjobs(job);
                CREATE INDEX IF NOT EXISTS idx_subjobs_disk ON subjobs(disk);
                CREATE INDEX IF NOT EXISTS idx_atfield_events_host ON atfield_events(host);
                CREATE INDEX IF NOT EXISTS idx_atfield_events_ts ON atfield_events(ts);
                """
            )
            self._conn.commit()
            self._backfill_jobs()

    def _backfill_jobs(self) -> None:
        rows = self._conn.execute(
            "SELECT subjob_id FROM subjobs WHERE job IS NULL"
        ).fetchall()
        if not rows:
            return
        self._conn.executemany(
            "UPDATE subjobs SET job=? WHERE subjob_id=?",
            [(_job_of(r["subjob_id"]), r["subjob_id"]) for r in rows],
        )
        self._conn.commit()

    # ---------------------------------------------------------------- seed
    def set_job_gate(self, job: str, idle_gate: Optional[dict[str, Any]]) -> None:
        """Attach (or clear) an idle-gate config for a job. Upserts the jobs row."""
        now = time.time()
        payload = jsonio.dumps(idle_gate) if idle_gate else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (job, created_at, idle_gate) VALUES (?, ?, ?) "
                "ON CONFLICT(job) DO UPDATE SET idle_gate=excluded.idle_gate",
                (job, now, payload),
            )
            self._conn.commit()

    def job_gate(self, job: str) -> Optional[dict[str, Any]]:
        """The idle-gate config for a job, or None."""
        row = self._conn.execute(
            "SELECT idle_gate FROM jobs WHERE job=?", (job,)
        ).fetchone()
        if not row or not row["idle_gate"]:
            return None
        try:
            return jsonio.loads(row["idle_gate"])
        except Exception:  # noqa: BLE001
            return None

    def all_job_gates(self) -> dict[str, dict[str, Any]]:
        """All jobs with a non-null idle_gate, as {job: config}. For startup cache."""
        out: dict[str, dict[str, Any]] = {}
        for row in self._conn.execute(
            "SELECT job, idle_gate FROM jobs WHERE idle_gate IS NOT NULL"
        ):
            try:
                cfg = jsonio.loads(row["idle_gate"])
            except Exception:  # noqa: BLE001
                continue
            if cfg:
                out[row["job"]] = cfg
        return out

    # ------------------------------------------------------------ at-field
    def record_atfield_event(self, host: str, event: dict[str, Any]) -> None:
        """Persist one AT-Field kill/pressure event (POST /atfield/event)."""
        kill_root = event.get("kill_root") or {}
        with self._lock:
            self._conn.execute(
                "INSERT INTO atfield_events "
                "(host, rule, signal, threshold, action, succeeded, kill_pid, "
                " kill_name, skipped_reason, ts, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    host,
                    event.get("rule"),
                    event.get("signal"),
                    event.get("threshold"),
                    event.get("action"),
                    1 if event.get("succeeded") else 0,
                    kill_root.get("pid"),
                    kill_root.get("name"),
                    event.get("skipped_reason"),
                    float(event.get("ts") or time.time()),
                    time.time(),
                ),
            )
            self._conn.commit()

    def recent_atfield_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent AT-Field events across all hosts, newest first."""
        rows = self._conn.execute(
            "SELECT host, rule, signal, threshold, action, succeeded, kill_pid, "
            "kill_name, skipped_reason, ts FROM atfield_events "
            "ORDER BY ts DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def seed(self, gigs: list[dict[str, Any]],
             job: Optional[str] = None,
             label: Optional[str] = None) -> int:
        """Insert gigs idempotently. Each sub-job: {subjob_id, spec, job?}.

        ``job`` (per-sub-job or batch-wide) overrides the subjob_id-prefix grouping, so
        a whole job can be seeded under one readable slug regardless of how
        the ``subjob_id``s are shaped. ``label`` is a human-readable name for that
        job, stored once in the ``jobs`` table and shown in the UI.

        Returns # inserted.
        """
        now = time.time()
        rows = []
        for g in gigs:
            jid = g["subjob_id"]
            gj = g.get("job") or job or _job_of(jid)
            disk = g.get("disk")
            rows.append((jid, jsonio.dumps(g.get("spec", {})), gj, disk, now))
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO subjobs (subjob_id, spec, job, disk, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            # Count sub-job inserts only — *before* the job upsert, so the label
            # row doesn't inflate the reported "inserted" count.
            inserted = self._conn.total_changes - before
            if label:
                lbl_grp = self._label_job(gigs, job)
                if lbl_grp:
                    self._conn.execute(
                        "INSERT INTO jobs (job, label, created_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(job) DO UPDATE SET label=excluded.label",
                        (lbl_grp, label, now),
                    )
            self._conn.commit()
            return inserted

    @staticmethod
    def _label_job(gigs: list[dict[str, Any]],
                     job: Optional[str]) -> Optional[str]:
        """The job slug a batch-wide ``label`` should attach to.

        An explicit batch ``job`` wins. Otherwise, if every sub-job resolves to the
        same effective job, that one is used; a mixed batch has no single home
        for the label, so we return None (and skip labelling rather than mislabel).
        """
        if job:
            return job
        if not gigs:
            return None
        eff = {g.get("job") or _job_of(g["subjob_id"]) for g in gigs}
        return next(iter(eff)) if len(eff) == 1 else None

    def mark_done_existing(self, subjob_ids: list[str]) -> int:
        """Mark gigs done without execution (e.g. output already exists = resume)."""
        now = time.time()
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "UPDATE subjobs SET state='done', completed_at=?, error=NULL "
                "WHERE subjob_id=? AND state!='done'",
                [(now, jid) for jid in subjob_ids],
            )
            self._conn.commit()
            return self._conn.total_changes - before

    # --------------------------------------------------------------- lease
    def lease(self, runner_id: str, host: str, capacity: int, ttl: float,
              disk_concurrency: Optional[dict[str, int]] = None,
              host_share: Optional[int] = None,
              job: Optional[str] = None) -> LeaseResult:
        """Lease up to ``capacity`` pending gigs atomically.

        With ``disk_concurrency`` (the mesh-global per-spindle budget, only the
        Coordinator can supply), candidate gigs are **round-robin interleaved across
        disks** (every spindle fed from the first lease) and a **per-disk in-flight
        cap** is enforced: never lease beyond ``budget[disk]`` gigs currently leased
        across the *whole fleet*. This is the distributed DiskSemaphore — possible
        only because the ledger is central. Disks not in the map (and ``None``) are
        uncapped. Without ``disk_concurrency`` the selection is plain "first N
        pending" (the inert default).

        ``host_share`` (optional) is a **fair-share ceiling**: the maximum number
        of gigs this ``host`` may hold in-flight across the whole fleet at once.
        It solves disk-budget *hoarding* — without it, the first host to poll can
        drain the entire per-disk budget before slower hosts get a look-in,
        serializing a mesh that should run in parallel. The Coordinator sets it
        proportional to each host's live worker weight. ``None`` => inert.
        """
        now = time.time()
        lease_id = uuid.uuid4().hex
        requested = capacity
        # Job-scoped leasing: when a runner declares its ``job`` (the workload it
        # can execute), only sub-jobs of that job are candidates. This is what
        # makes the single "one brain" coordinator safe to run *many* jobs at
        # once -- a reduce30 runner never leases a slerp sub-job (different task,
        # different spec) and vice-versa. ``job=None`` => fleet-wide (legacy).
        job_sql = " AND job=?" if job is not None else ""
        job_args: tuple = (job,) if job is not None else ()
        with self._lock:
            # Pending count snapshot for diagnostics (cheap COUNT(*)).
            pending_total = self._conn.execute(
                "SELECT COUNT(*) AS c FROM subjobs WHERE state='pending'" + job_sql,
                job_args,
            ).fetchone()["c"]

            # Fair-share ceiling: never let this host exceed its slice of the
            # fleet-wide in-flight budget. Applied uniformly to both the plain
            # and disk-aware paths by shrinking the effective capacity up-front.
            host_inflight_before = 0
            if host_share is not None:
                host_inflight_before = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM subjobs WHERE state='leased' AND host=?",
                    (host,),
                ).fetchone()["c"]
                capacity = min(capacity, max(0, host_share - host_inflight_before))
                if capacity <= 0:
                    return LeaseResult(
                        lease_id=None, gigs=[],
                        diag=self._lease_diag(
                            requested=requested, effective=capacity,
                            granted=0, binding_reason="FAIR_SHARE_CAP",
                            pending_total=pending_total,
                            host_inflight_before=host_inflight_before,
                            fair_share_ceiling=host_share,
                            disk_concurrency=disk_concurrency,
                            inflight=None, granted_here=None,
                            ids=[]))

            if not disk_concurrency:
                cur = self._conn.execute(
                    "SELECT subjob_id, spec, disk FROM subjobs WHERE state='pending'"
                    + job_sql + " ORDER BY created_at LIMIT ?",
                    job_args + (capacity,),
                )
                rows = cur.fetchall()
                if not rows:
                    return LeaseResult(
                        lease_id=None, gigs=[],
                        diag=self._lease_diag(
                            requested=requested, effective=capacity,
                            granted=0, binding_reason="NO_PENDING",
                            pending_total=pending_total,
                            host_inflight_before=host_inflight_before,
                            fair_share_ceiling=host_share,
                            disk_concurrency=disk_concurrency,
                            inflight=None, granted_here=None,
                            ids=[]))
                ids = [r["subjob_id"] for r in rows]
                # Binding constraint, judged against the runner's *request*: the
                # plain path has no disk budget, so a shortfall is either the
                # fair-share ceiling (host got its slice, not the whole request) or
                # an empty queue. Distinguish them so a host parked at its
                # fair-share slice doesn't read as a healthy GRANTED_FULL.
                if (host_share is not None and capacity < requested
                        and len(rows) >= capacity):
                    reason = "FAIR_SHARE_CAP"
                else:
                    reason = "GRANTED_FULL"
                res = self._finalize_lease(rows, lease_id, runner_id, host, now, ttl)
                res.diag = self._lease_diag(
                    requested=requested, effective=capacity,
                    granted=len(rows), binding_reason=reason,
                    pending_total=pending_total,
                    host_inflight_before=host_inflight_before,
                    fair_share_ceiling=host_share,
                    disk_concurrency=disk_concurrency,
                    inflight=None, granted_here=None,
                    ids=ids)
                return res

            # --- disk-aware path ---
            # 1. current in-flight per disk (leased gigs across the whole fleet)
            inflight: dict[Optional[str], int] = {}
            for r in self._conn.execute(
                "SELECT disk, COUNT(*) AS c FROM subjobs WHERE state='leased' GROUP BY disk"
            ):
                inflight[r["disk"]] = r["c"]
            inflight_before = dict(inflight)
            # 2. pending gigs with their disk, in creation order
            rows = self._conn.execute(
                "SELECT subjob_id, spec, disk FROM subjobs WHERE state='pending'"
                + job_sql + " ORDER BY created_at",
                job_args,
            ).fetchall()
            if not rows:
                return LeaseResult(
                    lease_id=None, gigs=[],
                    diag=self._lease_diag(
                        requested=requested, effective=capacity,
                        granted=0, binding_reason="NO_PENDING",
                        pending_total=pending_total,
                        host_inflight_before=host_inflight_before,
                        fair_share_ceiling=host_share,
                        disk_concurrency=disk_concurrency,
                        inflight=inflight_before, granted_here=None,
                        ids=[]))

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
            granted_here: dict[str, int] = {d: 0 for d in disk_concurrency}
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
                    granted_here[d] = granted_here.get(d, 0) + 1
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

            ids = [r["subjob_id"] for r in selected]
            # Binding constraint, judged against the runner's *request* (not the
            # fair-share-shrunk `capacity`): a host that asks for 100 and gets 4
            # because the spindles are capped must read as DISK_BUDGET_FULL. The
            # old "were any disks full at the *start*" test mislabeled the first
            # poller (which itself drains the budget) as GRANTED_FULL, making a
            # plainly starved host look healthy in /decisions/summary.
            if len(selected) >= requested:
                reason = "GRANTED_FULL"
            elif (host_share is not None and capacity < requested
                    and len(selected) >= capacity):
                # Fair-share ceiling (not lack of work) held the grant down.
                reason = "FAIR_SHARE_CAP"
            elif len(selected) < len(rows):
                # Pending gigs left unleased -> the per-disk budget was the wall.
                reason = "DISK_BUDGET_FULL"
            else:
                # Drained every pending sub-job we could see; just no more work.
                reason = "GRANTED_FULL"

            if not selected:
                return LeaseResult(
                    lease_id=None, gigs=[],
                    diag=self._lease_diag(
                        requested=requested, effective=capacity,
                        granted=0, binding_reason=reason,
                        pending_total=pending_total,
                        host_inflight_before=host_inflight_before,
                        fair_share_ceiling=host_share,
                        disk_concurrency=disk_concurrency,
                        inflight=inflight_before, granted_here=granted_here,
                        ids=[]))
            res = self._finalize_lease(selected, lease_id, runner_id, host, now, ttl)
            res.diag = self._lease_diag(
                requested=requested, effective=capacity,
                granted=len(selected), binding_reason=reason,
                pending_total=pending_total,
                host_inflight_before=host_inflight_before,
                fair_share_ceiling=host_share,
                disk_concurrency=disk_concurrency,
                inflight=inflight_before, granted_here=granted_here,
                ids=ids)
            return res

    @staticmethod
    def _lease_diag(*, requested: int, effective: int, granted: int,
                    binding_reason: str, pending_total: int,
                    host_inflight_before: int,
                    fair_share_ceiling: Optional[int],
                    disk_concurrency: Optional[dict[str, int]],
                    inflight: Optional[dict],
                    granted_here: Optional[dict[str, int]],
                    ids: list[str]) -> dict[str, Any]:
        """Build the diagnostics dict attached to every LeaseResult.

        Captures the state *before* the grant so a reviewer can reconstruct
        *why* the coordinator returned the count it did — the binding
        constraint, the fair-share ceiling, and the per-disk budget snapshot.
        """
        disk: dict[str, Any] = {}
        if disk_concurrency:
            inf = inflight or {}
            gh = granted_here or {}
            for d, budget_d in disk_concurrency.items():
                ib = inf.get(d, 0)
                disk[d] = {
                    "budget": budget_d,
                    "inflight_before": ib,
                    "free": max(0, budget_d - ib),
                    "granted_here": gh.get(d, 0),
                }
        return {
            "requested_capacity": requested,
            "effective_capacity": effective,
            "granted": granted,
            "binding_reason": binding_reason,
            "pending_total": pending_total,
            "host_inflight_before": host_inflight_before,
            "fair_share_ceiling": fair_share_ceiling,
            "disk": disk,
            "granted_subjob_ids": ids[:32],
        }

    def _finalize_lease(self, rows, lease_id, runner_id, host, now, ttl) -> LeaseResult:
        ids = [r["subjob_id"] for r in rows]
        self._conn.executemany(
            "UPDATE subjobs SET state='leased', lease_id=?, runner_id=?, host=?, "
            "leased_at=?, lease_deadline=?, attempts=attempts+1 WHERE subjob_id=?",
            [(lease_id, runner_id, host, now, now + ttl, jid) for jid in ids],
        )
        self._conn.commit()
        gigs = [{"subjob_id": r["subjob_id"], "spec": jsonio.loads(r["spec"]),
                 "disk": r["disk"]} for r in rows]
        return LeaseResult(lease_id=lease_id, gigs=gigs)

    # ------------------------------------------------------------ complete
    def complete(self, results: list[dict[str, Any]]) -> dict[str, int]:
        """Apply a batch of results. Each: {subjob_id, status, error?, metrics?}.

        status 'ok'/'skipped' -> done. 'requeue' -> back to pending WITHOUT
        consuming the retry budget (an eviction under pressure, not a task
        failure — e.g. an at-field pause mid-batch). Otherwise (error) re-queue
        (pending) until attempts exceed max_retries, then mark failed.
        """
        now = time.time()
        done = requeued = failed = 0
        with self._lock:
            for r in results:
                jid = r["subjob_id"]
                status = r.get("status", "ok")
                if status in ("ok", "skipped"):
                    self._conn.execute(
                        "UPDATE subjobs SET state='done', completed_at=?, error=NULL, "
                        "metrics=?, lease_id=NULL, lease_deadline=NULL WHERE subjob_id=?",
                        (now, jsonio.dumps(r.get("metrics", {})), jid),
                    )
                    done += 1
                elif status == "requeue":
                    # Eviction: return to pending immediately. Undo the lease's
                    # attempts+1 so a flapping pressure pause can't exhaust the
                    # retry budget and mark a healthy sub-job 'failed' — the task was
                    # preempted, not actually tried. error is cleared (not a fault).
                    self._conn.execute(
                        "UPDATE subjobs SET state='pending', error=NULL, "
                        "attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END, "
                        "lease_id=NULL, lease_deadline=NULL WHERE subjob_id=?",
                        (jid,),
                    )
                    requeued += 1
                else:
                    row = self._conn.execute(
                        "SELECT attempts FROM subjobs WHERE subjob_id=?", (jid,)
                    ).fetchone()
                    attempts = row["attempts"] if row else 0
                    if attempts > self.max_retries:
                        self._conn.execute(
                            "UPDATE subjobs SET state='failed', completed_at=?, error=?, "
                            "lease_id=NULL, lease_deadline=NULL WHERE subjob_id=?",
                            (now, str(r.get("error", "unknown"))[:2000], jid),
                        )
                        failed += 1
                    else:
                        self._conn.execute(
                            "UPDATE subjobs SET state='pending', error=?, "
                            "lease_id=NULL, lease_deadline=NULL WHERE subjob_id=?",
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
        root cause — without re-seeding under fresh ``subjob_id``s. ``leased`` is a
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
                f"UPDATE subjobs SET state='pending', lease_id=NULL, runner_id=NULL, "
                f"leased_at=NULL, lease_deadline=NULL, error=NULL{attempts_clause} "
                f"WHERE state IN ({placeholders})",
                states,
            )
            self._conn.commit()
            return cur.rowcount

    def cancel(self, job: str, *, purge: bool = False) -> dict[str, int]:
        """Cancel a job's queued work. Deletes its ``pending`` + ``leased`` gigs
        so no runner leases them again (an already-in-flight gig finishes on its
        runner and is simply discarded when it reports — its row is gone).

        ``purge=True`` additionally deletes the job's completed rows and its
        ``jobs`` metadata row, removing the job from ``/jobs`` entirely.

        ``job`` is REQUIRED — there is intentionally no "cancel everything" so a
        typo can't wipe the whole queue. Returns ``{deleted, purged}``.
        """
        if not job:
            raise ValueError("cancel requires a non-empty job slug")
        with self._lock:
            if purge:
                n = self._conn.execute(
                    "DELETE FROM subjobs WHERE job=?", (job,)).rowcount
                self._conn.execute("DELETE FROM jobs WHERE job=?", (job,))
                self._conn.commit()
                return {"deleted": int(n), "purged": 1}
            cur = self._conn.execute(
                "DELETE FROM subjobs WHERE job=? AND state IN ('pending','leased')",
                (job,))
            self._conn.commit()
            return {"deleted": int(cur.rowcount), "purged": 0}

    def heartbeat(self, lease_id: str, ttl: float) -> int:
        """Extend the deadline for all gigs still held under this lease."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE subjobs SET lease_deadline=? WHERE lease_id=? AND state='leased'",
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
                "UPDATE subjobs SET state='pending', lease_id=NULL, runner_id=NULL, "
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
                    "SELECT state, COUNT(*) AS n FROM subjobs GROUP BY state"
                )
            }
            recent = self._conn.execute(
                "SELECT COUNT(*) AS n FROM subjobs WHERE state='done' AND completed_at >= ?",
                (now - window_s,),
            ).fetchone()["n"]
            per_host = [
                {"host": row["host"] or "?", "in_flight": row["n"]}
                for row in self._conn.execute(
                    "SELECT host, COUNT(*) AS n FROM subjobs WHERE state='leased' GROUP BY host"
                )
            ]
            per_host_recent = {
                (row["host"] or "?"): row["n"]
                for row in self._conn.execute(
                    "SELECT host, COUNT(*) AS n FROM subjobs "
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
                    "SELECT disk, COUNT(*) AS n FROM subjobs "
                    "WHERE state='leased' AND disk IS NOT NULL GROUP BY disk"
                )
            }
            recent_errors = [
                {"subjob_id": row["subjob_id"], "host": row["host"], "error": row["error"]}
                for row in self._conn.execute(
                    "SELECT subjob_id, host, error FROM subjobs WHERE state='failed' "
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

    def job_done_in_window(self, job: str, window_s: float = 60.0) -> int:
        """Sub-jobs completed for one job slug in the last ``window_s`` seconds."""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM subjobs "
                "WHERE job=? AND state='done' AND completed_at >= ?",
                (job, now - window_s),
            ).fetchone()
        return int(row["n"] or 0)

    def job_disk_inflight(self, job: str) -> dict[str, int]:
        with self._lock:
            return {
                (row["disk"] or "(uncapped)"): int(row["n"])
                for row in self._conn.execute(
                    "SELECT disk, COUNT(*) AS n FROM subjobs "
                    "WHERE job=? AND state='leased' AND disk IS NOT NULL GROUP BY disk",
                    (job,),
                )
            }

    def error_digest(self, job: str, limit: int = 5) -> list[dict[str, Any]]:
        """Top failure messages for a job slug, grouped by error text."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT error, COUNT(*) AS n FROM subjobs "
                "WHERE job=? AND state='failed' AND error IS NOT NULL AND error != '' "
                "GROUP BY error ORDER BY n DESC LIMIT ?",
                (job, int(limit)),
            ).fetchall()
        return [{"error": r["error"], "count": int(r["n"])} for r in rows]

    def leased_subjob_ids(self, job: str, limit: int = 10) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT subjob_id FROM subjobs WHERE job=? AND state='leased' "
                "ORDER BY leased_at ASC LIMIT ?",
                (job, int(limit)),
            ).fetchall()
        return [r["subjob_id"] for r in rows]

    # ------------------------------------------------------------- listing
    def disk_done_counts(self) -> dict[str, int]:
        """``{disk_id: done_count}`` — for the throughput sampler to derive per-disk
        rate over time (PLAN §7.6 N6). Only disks with at least one done sub-job."""
        with self._lock:
            return {
                row["disk"]: row["n"]
                for row in self._conn.execute(
                    "SELECT disk, COUNT(*) AS n FROM subjobs "
                    "WHERE state='done' AND disk IS NOT NULL GROUP BY disk"
                )
            }

    def group_runner_done_counts(
        self, limit_groups: int = 40
    ) -> dict[str, dict[str, int]]:
        """``{job: {runner_id: done_count}}`` for the top ``limit_groups`` recently
        active jobs.

        Used by the sampler so the job page can render a stacked area chart of
        per-node contribution over time (which computer contributed how much,
        at each sampled instant). ``runner_id`` may be ``""`` for gigs whose
        completing runner was not recorded — those are grouped together.
        """
        with self._lock:
            top = self._conn.execute(
                """
                SELECT j.job AS job
                FROM subjobs j
                GROUP BY j.job
                ORDER BY COALESCE(MAX(j.completed_at),
                                  MAX(j.leased_at),
                                  MIN(j.created_at)) DESC
                LIMIT ?
                """,
                (int(limit_groups),),
            ).fetchall()
            grps = [r["job"] for r in top]
            if not grps:
                return {}
            placeholders = ",".join("?" for _ in grps)
            rows = self._conn.execute(
                f"SELECT job, COALESCE(runner_id,'') AS runner_id, "
                f"COUNT(*) AS n FROM subjobs WHERE state='done' "
                f"AND job IN ({placeholders}) GROUP BY job, runner_id",
                tuple(grps),
            ).fetchall()
        out: dict[str, dict[str, int]] = {g: {} for g in grps}
        for r in rows:
            out[r["job"]][r["runner_id"] or ""] = int(r["n"])
        return out

    def disk_inflight_count(self, disk: str) -> int:
        """Count of leased (in-flight) gigs on a specific disk — for the resource
        governor to share the per-disk read budget between gigs and external
        resource-acquire clients."""
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) AS c FROM subjobs WHERE state='leased' AND disk=?",
                (disk,)).fetchone()
            return r["c"] if r else 0

    _LIST_COLS = ("subjob_id", "state", "host", "runner_id", "attempts",
                  "created_at", "leased_at", "completed_at", "error", "metrics",
                  "job", "disk")

    def list_jobs(self, states: Optional[tuple[str, ...]] = None,
                  limit: int = 200, newest_first: bool = True,
                  job: Optional[str] = None,
                  subjob_id_re: Optional[str] = None,
                  error_re: Optional[str] = None) -> list[dict[str, Any]]:
        """Return job rows (for the /jobs + /history views). Newest by activity.

        ``job`` optionally filters to one job (the ``job`` column set at
        seed time). Without it, rows from every job are mixed (the existing
        behavior for the dashboard).

        ``subjob_id_re`` / ``error_re`` optionally apply a regex filter **server-side**
        via the registered REGEXP function (so a 100k-sub-job job doesn't ship
        all rows to the client to grep). Patterns are validated with
        ``re.compile`` before the query runs — a bad pattern raises ``re.error``
        which the caller should catch and report cleanly.
        """
        cols = ", ".join(self._LIST_COLS)
        order = "DESC" if newest_first else "ASC"
        order_expr = ("COALESCE(completed_at, leased_at, created_at) " + order)
        params: list[Any] = []
        clauses: list[str] = []
        if states:
            clauses.append("state IN (%s)" % ",".join("?" for _ in states))
            params.extend(states)
        if job:
            clauses.append("job = ?")
            params.append(job)
        if subjob_id_re is not None:
            re.compile(subjob_id_re)           # validate before query (raises re.error)
            clauses.append("subjob_id REGEXP ?")
            params.append(subjob_id_re)
        if error_re is not None:
            re.compile(error_re)
            clauses.append("error REGEXP ?")
            params.append(error_re)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {cols} FROM subjobs {where} ORDER BY {order_expr} LIMIT ?",
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

    def export_metrics(self, job: Optional[str] = None,
                       states: tuple[str, ...] = ("done",),
                       limit: int = 100000) -> list[dict[str, Any]]:
        """Bulk export of (subjob_id, metrics, state) for result aggregation.

        Unlike :meth:`list_jobs` (dashboard-shaped, capped at 2000), this is
        for consumers that need to fold metrics across a whole job -- e.g.
        ranking 32k clips by worst-section MPJPE. Returns lightweight rows
        (subjob_id, metrics, state, job, disk) ordered by subjob_id (deterministic),
        so a caller can page by subjob_id if the set is huge. ``limit`` defaults
        high (100k) since a single job rarely exceeds that.
        """
        cols = "subjob_id, metrics, state, job, disk"
        params: list[Any] = []
        clauses: list[str] = []
        if states:
            clauses.append("state IN (%s)" % ",".join("?" for _ in states))
            params.extend(states)
        if job:
            clauses.append("job = ?")
            params.append(job)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {cols} FROM subjobs {where} ORDER BY subjob_id LIMIT ?",
                params,
            ).fetchall()
        out = []
        for r in rows:
            try:
                m = jsonio.loads(r["metrics"]) if r["metrics"] else {}
            except Exception:  # noqa: BLE001
                m = {}
            out.append({"subjob_id": r["subjob_id"], "metrics": m,
                        "state": r["state"], "job": r["job"], "disk": r["disk"]})
        return out

    def group_stats(self, limit: int = 200) -> list[dict[str, Any]]:
        """Per-job rollup ("jobs" for the UI): counts by state + timing.

        A "job" here is a job of gigs sharing a ``subjob_id`` prefix. Returns the
        most-recently-active groups first.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT j.job AS job,
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
                FROM subjobs j
                LEFT JOIN jobs c ON c.job = j.job
                GROUP BY j.job
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

    def job(self, subjob_id: str) -> Optional[dict[str, Any]]:
        cols = ", ".join(self._LIST_COLS + ("spec",))
        with self._lock:
            r = self._conn.execute(
                f"SELECT {cols} FROM subjobs WHERE subjob_id=?", (subjob_id,)
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
