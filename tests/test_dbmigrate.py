"""Tests for the DB migration: old Cyberpunk schema → operator vocab.

The tricky part: the word "jobs" MOVES from the sub-job table to the job table.
The migration must create `subjobs` from old `jobs` BEFORE reusing the `jobs`
name for the old `jobs` data.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.dbmigrate import needs_migration, migrate  # noqa: E402


def _make_old_db(path: str) -> None:
    """Create a synthetic OLD-schema DB with a few rows."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE jobs (
            subjob_id        TEXT PRIMARY KEY,
            spec          TEXT NOT NULL,
            job           TEXT,
            disk          TEXT,
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
        CREATE TABLE jobs (
            job        TEXT PRIMARY KEY,
            label      TEXT,
            created_at REAL NOT NULL
        );
        CREATE INDEX idx_jobs_state ON jobs(state);
        CREATE INDEX idx_jobs_grp ON jobs(job);
    """)
    # Insert some test data
    conn.executemany(
        "INSERT INTO jobs (subjob_id, spec, job, disk, state, created_at) "
        "VALUES (?, '{}', ?, ?, ?, 1000.0)",
        [("clip_001", "reduce30", "disk1", "done"),
         ("clip_002", "reduce30", "disk1", "pending"),
         ("clip_003", "slerp", "cache_nvme", "failed")],
    )
    conn.executemany(
        "INSERT INTO jobs (job, label, created_at) VALUES (?, ?, 1000.0)",
        [("reduce30", "Canonical 30fps -> 88-DoF"),
         ("slerp", "88-DoF@30 -> @4fps")],
    )
    conn.commit()
    conn.close()


def test_needs_migration_on_old_schema(tmp_path):
    db = tmp_path / "test.db"
    _make_old_db(str(db))
    conn = sqlite3.connect(str(db))
    assert needs_migration(conn) is True
    conn.close()


def test_needs_migration_on_new_schema(tmp_path):
    db = tmp_path / "test.db"
    _make_old_db(str(db))
    conn = sqlite3.connect(str(db))
    migrate(conn)  # migrate first
    assert needs_migration(conn) is False  # already migrated
    conn.close()


def test_needs_migration_on_fresh_db(tmp_path):
    db = tmp_path / "fresh.db"
    conn = sqlite3.connect(str(db))
    # No tables at all — not an old schema
    assert needs_migration(conn) is False
    conn.close()


def test_migrate_preserves_row_counts(tmp_path):
    db = tmp_path / "test.db"
    _make_old_db(str(db))
    conn = sqlite3.connect(str(db))
    old_jobs_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    old_campaigns_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    migrate(conn)
    new_subjobs_count = conn.execute("SELECT COUNT(*) FROM subjobs").fetchone()[0]
    new_jobs_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert new_subjobs_count == old_jobs_count == 3
    assert new_jobs_count == old_campaigns_count == 2


def test_migrate_renames_columns_correctly(tmp_path):
    db = tmp_path / "test.db"
    _make_old_db(str(db))
    conn = sqlite3.connect(str(db))
    migrate(conn)
    # Check subjobs columns
    sj_cols = {r[1] for r in conn.execute("PRAGMA table_info(subjobs)")}
    assert "subjob_id" in sj_cols
    assert "job" in sj_cols
    assert "disk" in sj_cols
    assert "subjob_id" not in sj_cols  # old name gone
    assert "job" not in sj_cols     # old name gone
    # Check jobs columns
    j_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "job" in j_cols
    assert "label" in j_cols
    assert "job" not in j_cols  # old name gone


def test_migrate_data_round_trips(tmp_path):
    db = tmp_path / "test.db"
    _make_old_db(str(db))
    conn = sqlite3.connect(str(db))
    migrate(conn)
    # Check a specific sub-job
    row = conn.execute(
        "SELECT subjob_id, job, disk, state FROM subjobs WHERE subjob_id='clip_001'"
    ).fetchone()
    assert row[0] == "clip_001"
    assert row[1] == "reduce30"  # was job
    assert row[2] == "disk1"
    assert row[3] == "done"
    # Check a specific job
    row = conn.execute(
        "SELECT job, label FROM jobs WHERE job='slerp'"
    ).fetchone()
    assert row[0] == "slerp"
    assert row[1] == "88-DoF@30 -> @4fps"


def test_migrate_does_not_invert_counts(tmp_path):
    """The critical trap: jobs↔jobs swap could invert job/sub-job counts.
    Verify 3 sub-jobs stay as 3 subjobs (not 2) and 2 jobs stay as 2 jobs."""
    db = tmp_path / "test.db"
    _make_old_db(str(db))
    conn = sqlite3.connect(str(db))
    migrate(conn)
    subjobs = conn.execute("SELECT COUNT(*) FROM subjobs").fetchone()[0]
    jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert subjobs == 3, f"subjobs should be 3 (was old jobs), got {subjobs}"
    assert jobs == 2, f"jobs should be 2 (was old jobs), got {jobs}"


def test_migrate_is_idempotent(tmp_path):
    db = tmp_path / "test.db"
    _make_old_db(str(db))
    conn = sqlite3.connect(str(db))
    assert migrate(conn) is True  # first migration
    assert migrate(conn) is False  # second is no-op
    # Data still intact
    assert conn.execute("SELECT COUNT(*) FROM subjobs").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_migrate_creates_indexes(tmp_path):
    db = tmp_path / "test.db"
    _make_old_db(str(db))
    conn = sqlite3.connect(str(db))
    migrate(conn)
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}
    assert "idx_subjobs_state" in indexes
    assert "idx_subjobs_job" in indexes
    assert "idx_subjobs_disk" in indexes


if __name__ == "__main__":
    tests = [n for n in dir(sys.modules[__name__]) if n.startswith("test_")]
    fail = 0
    for name in tests:
        try:
            globals()[name]()
            print(f"PASS  {name}")
        except Exception as exc:
            print(f"FAIL  {name}: {exc}")
            fail += 1
    print(f"\n{len(tests)-fail}/{len(tests)} passed")
    sys.exit(fail)
