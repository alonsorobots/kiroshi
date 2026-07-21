"""Phase 6 -- git-clean-tree reproducibility gate.

Three layers under test:
  1. codefinger.has_real_changes / dirty_repos -- the detection primitive
     (real tracked edit -> dirty; EOL/CRLF noise -> clean; untracked -> clean).
  2. Runner._await_clean_tree -- blocks-then-proceeds when the tree goes clean
     (mocked detector; no real subprocess, no real pool).
  3. cli._require_clean_launch -- seed/run refuse (nonzero exit) on a dirty
     launcher repo.

The detector tests build throwaway git repos so we exercise real `git diff`
behavior, not a mock of it -- that's the whole point (the bug this replaces was
a WRONG git command, `status --porcelain`, so mocking git would test nothing).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kiroshi import codefinger  # noqa: E402


def _git_exe() -> str:
    cand = r"C:\Program Files\Git\bin\git.exe"
    return cand if os.path.isfile(cand) else "git"


def _run_git(root: Path, *args: str) -> None:
    subprocess.run([_git_exe(), "-C", str(root), *args],
                   check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A committed git repo with one tracked LF text file."""
    root = tmp_path / "repo"
    root.mkdir()
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "t@t")
    _run_git(root, "config", "user.name", "t")
    # Commit with LF endings; write bytes so the test controls the newline.
    (root / "a.txt").write_bytes(b"line1\nline2\nline3\n")
    _run_git(root, "add", "a.txt")
    _run_git(root, "commit", "-qm", "init")
    return root


# ------------------------------------------------------------ detection primitive
def test_clean_repo_reads_clean(repo: Path):
    assert codefinger.has_real_changes(str(repo)) is False


def test_real_tracked_edit_reads_dirty(repo: Path):
    (repo / "a.txt").write_bytes(b"line1\nCHANGED\nline3\n")
    assert codefinger.has_real_changes(str(repo)) is True


def test_eol_only_change_reads_clean(repo: Path):
    # Same content, LF -> CRLF. This is the exact false-positive that
    # `git status --porcelain` produced on the Windows runners and that
    # bricked the naive gate; the content-level diff must ignore it.
    (repo / "a.txt").write_bytes(b"line1\r\nline2\r\nline3\r\n")
    assert codefinger.has_real_changes(str(repo)) is False


def test_real_change_with_crlf_rewrite_reads_dirty(repo: Path):
    # The exact production failure: a file whose endings ALSO flip LF->CRLF but
    # that has a genuine content edit too. `git diff --quiet --ignore-cr-at-eol`
    # wrongly reported this CLEAN on the real repo (it suppresses diff
    # generation, so the ignore flag isn't honored for the exit code); the
    # `--name-only` primitive must still see the content change. This guards
    # against ever regressing back to the --quiet approach.
    (repo / "a.txt").write_bytes(b"line1\r\nCHANGED\r\nline3\r\n")
    assert codefinger.has_real_changes(str(repo)) is True


def test_detector_is_deterministic_across_repeated_calls(repo: Path):
    # The --quiet approach returned different answers on repeated calls / across
    # process contexts for the same tree. The result must be stable.
    (repo / "a.txt").write_bytes(b"line1\nCHANGED\nline3\n")
    results = {codefinger.has_real_changes(str(repo)) for _ in range(5)}
    assert results == {True}


def test_untracked_file_reads_clean(repo: Path):
    # Every research repo has scratch/output/.pyc lying around; those must
    # never block a launch.
    (repo / "scratch.tmp").write_bytes(b"junk\n")
    assert codefinger.has_real_changes(str(repo)) is False


def test_missing_git_repo_reads_clean(tmp_path: Path):
    # No .git at all -> unverifiable -> never invent dirtiness we can't prove.
    plain = tmp_path / "notarepo"
    plain.mkdir()
    assert codefinger.has_real_changes(str(plain)) is False


def test_dirty_repos_lists_basename_on_real_edit(repo: Path):
    (repo / "a.txt").write_bytes(b"line1\nCHANGED\nline3\n")
    names = codefinger.dirty_repos([str(repo)])
    assert "repo" in names


def test_dirty_repos_empty_when_clean(repo: Path):
    assert codefinger.dirty_repos([str(repo)]) == [] or "repo" not in codefinger.dirty_repos([str(repo)])


# ------------------------------------------------------------ runner startup gate
def test_runner_blocks_then_proceeds(monkeypatch):
    """_await_clean_tree loops while dirty and returns the moment it's clean,
    without exiting -- self-healing on commit."""
    from kiroshi import worker

    # Build a bare Runner-like object without running __init__ (which wants a
    # coordinator, pool, etc.). We only exercise the gate method.
    r = worker.Runner.__new__(worker.Runner)
    r._draining = False
    r.quiet = True

    # Dirty for the first two checks, clean on the third.
    calls = {"n": 0}

    def fake_dirty():
        calls["n"] += 1
        return ["repo"] if calls["n"] < 3 else []

    r._dirty_repos = fake_dirty  # type: ignore[method-assign]

    sleeps: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", lambda s: sleeps.append(s))

    r._await_clean_tree()  # must return (not hang, not exit)

    assert calls["n"] == 3           # re-checked until clean
    assert len(sleeps) == 2          # slept between the two dirty checks


def test_runner_gate_returns_when_draining(monkeypatch):
    """A shutdown signal during the block must let run() bail cleanly."""
    from kiroshi import worker

    r = worker.Runner.__new__(worker.Runner)
    r._draining = False
    r.quiet = True

    def fake_dirty():
        r._draining = True  # simulate SIGINT arriving mid-block
        return ["repo"]

    r._dirty_repos = fake_dirty  # type: ignore[method-assign]
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)

    r._await_clean_tree()  # returns because loop condition is `not self._draining`
    assert r._draining is True


# ------------------------------------------------------------ seed/run launch gate
def test_require_clean_launch_refuses_on_dirty(monkeypatch, capsys):
    from kiroshi import cli

    monkeypatch.setattr(cli, "dirty_repos", lambda sp: ["kiroshi"], raising=False)
    # dirty_repos is imported inside the function, so patch at source too.
    from kiroshi import codefinger as cf
    monkeypatch.setattr(cf, "dirty_repos", lambda sp=None: ["kiroshi"])

    class Args:
        syspath = None

    rc = cli._require_clean_launch(Args())
    assert rc == 2
    err = capsys.readouterr().err
    assert "refusing" in err.lower()
    assert "kiroshi" in err


def test_require_clean_launch_passes_on_clean(monkeypatch):
    from kiroshi import cli
    from kiroshi import codefinger as cf
    monkeypatch.setattr(cf, "dirty_repos", lambda sp=None: [])

    class Args:
        syspath = None

    assert cli._require_clean_launch(Args()) is None
