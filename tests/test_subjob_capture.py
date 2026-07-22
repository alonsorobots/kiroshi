"""Unit tests for subjob_capture.py's pure/file-based helpers (no subprocess).

The fd-redirection itself (the part that has to cross a real process boundary
to prove it catches native-library writes) is exercised separately in
test_subjob_capture_integration.py against a real LocalPool -- fd redirection
can't be meaningfully unit-tested with mocks.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi import subjob_capture as sc  # noqa: E402


def _use_tmp_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("KIROSHI_STATE_DIR", str(tmp_path))


# --------------------------------------------------------------- naming/paths
def test_safe_name_strips_path_separators():
    name = sc._safe_name("shard_01/V03_S1041_I00000130_P1502")
    assert "/" not in name and "\\" not in name
    assert "shard_01" in name


def test_log_path_and_marker_path_are_deterministic(monkeypatch, tmp_path):
    _use_tmp_state_dir(monkeypatch, tmp_path)
    p1 = sc.log_path("abc/def")
    p2 = sc.log_path("abc/def")
    assert p1 == p2
    assert p1.suffix == ".log"
    assert sc._marker_path("abc/def").suffix == ".json"


# --------------------------------------------------------------------- tail
def test_read_tail_missing_file_returns_none(monkeypatch, tmp_path):
    _use_tmp_state_dir(monkeypatch, tmp_path)
    assert sc.read_tail("does-not-exist") is None


def test_read_tail_returns_full_content_under_the_line_cap(monkeypatch, tmp_path):
    _use_tmp_state_dir(monkeypatch, tmp_path)
    sc.log_path("j1").write_bytes(b"line1\nline2\nline3\n")
    tail = sc.read_tail("j1", max_lines=500)
    assert tail == "line1\nline2\nline3"


def test_read_tail_truncates_to_last_n_lines(monkeypatch, tmp_path):
    _use_tmp_state_dir(monkeypatch, tmp_path)
    lines = [f"line{i}" for i in range(1000)]
    sc.log_path("j2").write_bytes(("\n".join(lines) + "\n").encode())
    tail = sc.read_tail("j2", max_lines=500)
    tail_lines = tail.split("\n")
    assert len(tail_lines) == 500
    assert tail_lines[-1] == "line999"
    assert tail_lines[0] == "line500"


def test_read_tail_respects_byte_cap_on_huge_single_line(monkeypatch, tmp_path):
    _use_tmp_state_dir(monkeypatch, tmp_path)
    huge = b"x" * 500_000
    sc.log_path("j3").write_bytes(huge)
    tail = sc.read_tail("j3", max_lines=500, max_bytes=1000)
    assert len(tail) <= 1000


# ------------------------------------------------------------------- discard
def test_discard_removes_both_files_and_is_idempotent(monkeypatch, tmp_path):
    _use_tmp_state_dir(monkeypatch, tmp_path)
    sc.log_path("j4").write_bytes(b"x")
    sc._marker_path("j4").write_text("{}")
    sc.discard("j4")
    assert not sc.log_path("j4").exists()
    assert not sc._marker_path("j4").exists()
    sc.discard("j4")  # must not raise on missing files


# ---------------------------------------------------------------- in-flight
def test_list_inflight_reads_markers(monkeypatch, tmp_path):
    _use_tmp_state_dir(monkeypatch, tmp_path)
    import json
    now = time.time()
    sc._marker_path("running-1").write_text(
        json.dumps({"subjob_id": "running-1", "started_at": now - 5, "pid": 123}))
    out = sc.list_inflight()
    assert len(out) == 1
    assert out[0]["subjob_id"] == "running-1"
    assert out[0]["elapsed_s"] >= 5


def test_list_inflight_ignores_malformed_marker(monkeypatch, tmp_path):
    _use_tmp_state_dir(monkeypatch, tmp_path)
    sc._marker_path("bad").write_text("not json")
    assert sc.list_inflight() == []


def test_list_inflight_max_age_excludes_old_entries(monkeypatch, tmp_path):
    _use_tmp_state_dir(monkeypatch, tmp_path)
    import json
    sc._marker_path("old").write_text(
        json.dumps({"subjob_id": "old", "started_at": time.time() - 10_000, "pid": 1}))
    assert sc.list_inflight(max_age_s=60.0) == []
    assert len(sc.list_inflight(max_age_s=None)) == 1


# ------------------------------------------------------------------ sweeping
def test_sweep_stale_removes_only_old_files(monkeypatch, tmp_path):
    _use_tmp_state_dir(monkeypatch, tmp_path)
    fresh = sc.log_path("fresh")
    stale = sc.log_path("stale")
    fresh.write_bytes(b"x")
    stale.write_bytes(b"x")
    old_time = time.time() - 10_000
    os.utime(stale, (old_time, old_time))
    removed = sc.sweep_stale(max_age_s=60.0)
    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


def test_sweep_stale_empty_dir_returns_zero(monkeypatch, tmp_path):
    _use_tmp_state_dir(monkeypatch, tmp_path)
    assert sc.sweep_stale(max_age_s=60.0) == 0


# --------------------------------------------------------- SubjobCapture
def test_capture_kill_switch_env_var_disables(monkeypatch, tmp_path):
    _use_tmp_state_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("KIROSHI_SUBJOB_CAPTURE", "0")
    cap = sc.SubjobCapture("kill-switch-job")
    with cap:
        pass
    assert cap.active is False
    assert not sc.log_path("kill-switch-job").exists()


def test_capture_enter_failure_is_best_effort(monkeypatch, tmp_path):
    _use_tmp_state_dir(monkeypatch, tmp_path)
    monkeypatch.delenv("KIROSHI_SUBJOB_CAPTURE", raising=False)
    # Force the marker-write step to fail, simulating an unusual environment.
    monkeypatch.setattr(
        sc.Path, "write_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("no perms")))
    saved_out, saved_err = os.dup(1), os.dup(2)
    try:
        cap = sc.SubjobCapture("bad-job")
        with cap:
            pass  # must not raise
        assert cap.active is False
    finally:
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)


def test_capture_roundtrip_in_process(monkeypatch, tmp_path):
    """Same-process sanity check (the cross-process/native-fd case is covered
    by the integration test): sys.stdout writes land in the capture file and
    fd 1/2 are correctly restored afterward."""
    _use_tmp_state_dir(monkeypatch, tmp_path)
    monkeypatch.delenv("KIROSHI_SUBJOB_CAPTURE", raising=False)
    cap = sc.SubjobCapture("roundtrip")
    with cap:
        print("hello-inside-capture")
    assert cap.active is True
    tail = sc.read_tail("roundtrip")
    assert "hello-inside-capture" in tail
    # fd 1/2 must be usable again after __exit__
    print("hello-after-restore")  # should not raise / should go to real stdout
