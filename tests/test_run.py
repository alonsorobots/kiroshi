"""Tests for the `kiroshi run` front door plumbing (PLAN §7.5).

Covers the pure pieces (pass-through arg parsing, glob enumeration, slug) and the
enumeration-contract resolver, plus the seed-count fix (campaign label row must
not inflate the inserted count). The full in-process Fixer+Runner orchestration is
exercised by the end-to-end smoke run, not here.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi import runjob  # noqa: E402
from kiroshi import tasks  # noqa: E402
from kiroshi.jobstore import JobStore  # noqa: E402


# --------------------------------------------------------- pass-through args
def test_parse_task_args_scalar_list_and_flag():
    args = runjob.parse_task_args(
        ["--read-root", "//nas/clips", "--fps", "4", "--fps", "8", "--dry-run"]
    )
    assert args["read_root"] == "//nas/clips"
    assert args["fps"] == ["4", "8"]      # repeated -> list
    assert args["dry_run"] is True        # bare flag -> True, de-hyphenated key


def test_parse_task_args_single_value_stays_scalar():
    assert runjob.parse_task_args(["--fps", "8"]) == {"fps": "8"}


# --------------------------------------------------------- items enumeration
def test_gigs_from_items_globs_files(tmp_path):
    for name in ("a.txt", "b.txt", "c.log"):
        (tmp_path / name).write_text("x")
    pattern = str(tmp_path / "*.txt").replace("\\", "/")
    gigs = runjob._gigs_from_items(pattern)
    assert len(gigs) == 2
    assert all(g["spec"]["path"].endswith(".txt") for g in gigs)
    # deterministic + each carries a path spec
    assert gigs == sorted(gigs, key=lambda g: g["job_id"])


# --------------------------------------------------------- three-state bar
def test_render_bar_all_pending():
    assert runjob._render_bar(0, 0, 100) == "-" * 28


def test_render_bar_done_and_in_flight():
    # 25 done, 50 in flight, 25 pending of 100 -> 7 #, 14 ~, 7 -
    bar = runjob._render_bar(25, 50, 100)
    assert bar.count("#") == 7
    assert bar.count("~") == 14
    assert bar.count("-") == 28 - 7 - 14


def test_render_bar_leased_whole_queue_moves_immediately():
    # the case this fixes: nothing done yet but everything leased -> bar is all ~,
    # not all - (so it visibly "started" instead of looking frozen at 0%)
    assert runjob._render_bar(0, 160, 160) == "~" * 28


def test_render_bar_complete():
    assert runjob._render_bar(100, 0, 100) == "#" * 28


def test_render_bar_clamps_overflow():
    # rounding can push done+leased widths past barw; must never exceed it
    bar = runjob._render_bar(99, 99, 100)
    assert len(bar) == 28
    assert bar.count("-") == 0  # done+leased >= total -> no pending segment


def test_slug_is_filesystem_safe():
    assert runjob._slug("Seamless 30fps -> 4,8 fps") == "seamless-30fps-4-8-fps"
    assert runjob._slug("") == "run"
    assert runjob._slug("///") == "run"


# --------------------------------------------------------- enumerate contract
def test_resolve_enumerator_present_and_absent():
    # sleep_task has no enumerate_gigs; the motion task does
    assert tasks.resolve_enumerator("examples.sleep_task:run") is None
    import importlib.util
    if importlib.util.find_spec("numpy") is not None:
        fn = tasks.resolve_enumerator("examples.motion_resample:run")
        assert callable(fn)


def test_module_of():
    assert tasks.module_of("pkg.mod:run") == "pkg.mod"
    assert tasks.module_of("pkg.mod") == "pkg.mod"


# --------------------------------------------------------- seed count fix
def test_seed_count_excludes_campaign_label_row(tmp_path):
    store = JobStore(str(tmp_path / "c.db"), max_retries=3)
    n = store.seed(
        [{"job_id": "x/1", "spec": {}}, {"job_id": "x/2", "spec": {}}],
        group="x", label="My Campaign",
    )
    assert n == 2  # not 3 — the campaigns upsert must not be counted
    # re-seed is idempotent -> 0 new even though the label upserts again
    assert store.seed(
        [{"job_id": "x/1", "spec": {}}], group="x", label="My Campaign",
    ) == 0
