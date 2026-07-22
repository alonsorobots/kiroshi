"""Integration tests for per-sub-job output capture against a REAL LocalPool
(real spawned worker processes) -- required because fd-level redirection
cannot be meaningfully proven with mocks; the whole point is that it crosses
a real process boundary and catches output that never goes through Python's
``sys.stdout`` object.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def test_tail_log_captures_print_stderr_and_raw_fd_write(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROSHI_STATE_DIR", str(tmp_path))
    from kiroshi.pool import LocalPool

    pool = LocalPool(task_ref="examples.capture_probe_task:run", workers=1)
    try:
        results = pool.run_batch(
            [{"subjob_id": "probe-1", "spec": {}}],
            hb_interval=30.0,
        )
    finally:
        pool.close()

    assert len(results) == 1
    r = results[0]
    assert r["status"] == "ok"
    tail = r["metrics"].get("tail_log")
    assert tail, "expected a captured tail_log on a successful sub-job"
    # All three channels must be present -- proves the fd-level redirect
    # crosses process boundaries and catches raw os.write (the native-library
    # simulation), not just Python-level print()/sys.stderr.
    assert "print-line" in tail
    assert "stderr-line" in tail
    assert "raw-fd-line" in tail


def test_tail_log_survives_timeout_kill(tmp_path, monkeypatch):
    """The single most important test: a sub-job writes partial output, then
    hangs past gig_timeout. Confirm the timed-out result's tail_log contains
    that partial output (proving capture is durable BEFORE the tree-kill
    lands, not dependent on graceful cleanup), and that the on-disk capture
    files are cleaned up afterward (discard() ran)."""
    monkeypatch.setenv("KIROSHI_STATE_DIR", str(tmp_path))
    from kiroshi.pool import LocalPool
    from kiroshi import subjob_capture

    pool = LocalPool(task_ref="examples.capture_probe_task:run", workers=1)
    try:
        t0 = time.time()
        results = pool.run_batch(
            [{"subjob_id": "hang-1", "spec": {"hang_after_output": 30.0}}],
            gig_timeout=2.0,
            hb_interval=30.0,
        )
        elapsed = time.time() - t0
    finally:
        pool.close()

    assert elapsed < 15.0, "must not wait out the full 30s hang"
    assert len(results) == 1
    r = results[0]
    assert r["status"] == "error"
    assert r["error"] == "timeout"
    tail = r["metrics"].get("tail_log")
    assert tail, "partial output must survive the tree-kill"
    assert "print-line" in tail
    assert "raw-fd-line" in tail
    # discard() must have removed both files after the parent read the tail
    assert subjob_capture.read_tail("hang-1") is None
    assert not subjob_capture._marker_path("hang-1").exists()


def test_capture_disabled_via_env_var_still_runs_normally(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROSHI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("KIROSHI_SUBJOB_CAPTURE", "0")
    from kiroshi.pool import LocalPool

    pool = LocalPool(task_ref="examples.capture_probe_task:run", workers=1)
    try:
        results = pool.run_batch(
            [{"subjob_id": "no-capture-1", "spec": {}}], hb_interval=30.0,
        )
    finally:
        pool.close()

    assert results[0]["status"] == "ok"
    assert "tail_log" not in results[0]["metrics"]
