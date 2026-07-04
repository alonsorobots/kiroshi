"""Database migration: rename old Cyberpunk-schema tables/columns to operator vocab.

Old schema (confusingly named):
  - table ``jobs`` (PK ``job_id``, col ``grp``, col ``disk``) held **sub-jobs**
  - table ``campaigns`` (PK ``grp``, col ``label``) held **jobs**

New schema (operator vocab):
  - table ``subjobs`` (PK ``subjob_id``, col ``job``, col ``disk``) — sub-jobs
  - table ``jobs`` (PK ``job``, col ``label``) — jobs

The tricky part: the word "jobs" MOVES from the sub-job table to the job table.
We must create ``subjobs`` from old ``jobs`` BEFORE creating new ``jobs`` from
old ``campaigns``, or the name collides.

Migration is idempotent: if ``subjobs`` already exists, it's a no-op.
"""
from __future__ import annotations

import sqlite3
import time


def needs_migration(conn: sqlite3.Connection) -> bool:
    """True if the DB has the old schema (table 'jobs' with col 'job_id')."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "subjobs" in tables:
        return False  # already migrated
    if "jobs" in tables:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        return "job_id" in cols  # old schema has job_id, new would have subjob_id
    return False


def migrate(conn: sqlite3.Connection) -> bool:
    """Migrate old-schema DB to new schema. Returns True if migrated.

    Safe to call on an already-migrated DB (no-op, returns False).

    The migration:
      1. Create ``subjobs`` from old ``jobs`` (copy all columns, rename
         job_id→subjob_id, grp→job).
      2. Drop old ``jobs`` (now safe — subjobs has the data).
      3. Create new ``jobs`` from old ``campaigns`` (rename grp→job).
      4. Drop old ``campaigns``.
      5. Rebuild indexes under new names.
      6. Bump PRAGMA user_version.
    """
    if not needs_migration(conn):
        return False

    # Detect which columns exist in the old jobs table (some pre-disk DBs
    # don't have the 'disk' column; some pre-grp DBs don't have 'grp').
    old_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    has_disk = "disk" in old_cols
    has_grp = "grp" in old_cols

    # Step 1: create subjobs from old jobs (BEFORE reusing the 'jobs' name)
    # Build the SELECT dynamically based on available columns.
    disk_expr = "disk" if has_disk else "NULL"
    grp_expr = "grp" if has_grp else "NULL"
    conn.executescript(f"""
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
        INSERT OR IGNORE INTO subjobs
            (subjob_id, spec, job, disk, state, lease_id, runner_id, host,
             attempts, leased_at, lease_deadline, completed_at, error,
             metrics, created_at)
        SELECT
            job_id, spec, {grp_expr}, {disk_expr}, state, lease_id, runner_id, host,
            attempts, leased_at, lease_deadline, completed_at, error,
            metrics, created_at
        FROM jobs;
    """)

    # Step 2: drop old jobs table (subjobs has the data now)
    conn.execute("DROP TABLE IF EXISTS jobs")
    conn.execute("DROP INDEX IF EXISTS idx_jobs_state")
    conn.execute("DROP INDEX IF EXISTS idx_jobs_lease")
    conn.execute("DROP INDEX IF EXISTS idx_jobs_completed")
    conn.execute("DROP INDEX IF EXISTS idx_jobs_grp")
    conn.execute("DROP INDEX IF EXISTS idx_jobs_disk")

    # Step 3: create new jobs table from campaigns
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            job         TEXT PRIMARY KEY,
            label       TEXT,
            created_at  REAL NOT NULL
        );
        INSERT OR IGNORE INTO jobs (job, label, created_at)
        SELECT grp, label, created_at FROM campaigns;
    """)

    # Step 4: drop old campaigns
    conn.execute("DROP TABLE IF EXISTS campaigns")

    # Step 5: rebuild indexes under new names
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_subjobs_state ON subjobs(state);
        CREATE INDEX IF NOT EXISTS idx_subjobs_lease ON subjobs(lease_id);
        CREATE INDEX IF NOT EXISTS idx_subjobs_completed ON subjobs(completed_at);
        CREATE INDEX IF NOT EXISTS idx_subjobs_job ON subjobs(job);
        CREATE INDEX IF NOT EXISTS idx_subjobs_disk ON subjobs(disk);
    """)

    # Step 6: bump user_version so detect is fast next time
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    return True
