"""Git/code freshness fingerprint for mesh runners (shared by remote preflight)."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Optional


def _git(root: str, *args: str) -> Optional[str]:
    git = r"C:\Program Files\Git\bin\git.exe"
    if not os.path.isfile(git):
        git = "git"
    try:
        r = subprocess.run([git, "-C", root, *args], capture_output=True,
                           text=True, timeout=8, check=False)
        if r.returncode != 0:
            return None
        return (r.stdout or "").strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def repo_root(start: str) -> Optional[str]:
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            return None
        d = nd


def has_real_changes(root: str) -> bool:
    """True iff ``root`` has uncommitted changes to TRACKED files that are
    real content differences -- NOT untracked files, NOT EOL/CRLF noise.

    ``git status --porcelain`` (used by fingerprint_repos' dirty flag) is the
    WRONG signal for a launch gate: it flags untracked scratch/output/.pyc
    (every research repo has these) and CRLF<->LF line-ending artifacts (a
    persistent condition on our Windows runners). Gating on that with no
    override would refuse to start every runner, forever.

    ``git diff --quiet HEAD`` compares tracked working-tree content to HEAD:
    it ignores untracked files entirely, and git normalizes line endings at
    the content level so pure EOL noise reads as clean. ``--ignore-cr-at-eol``
    is belt-and-suspenders for the CRLF case. Exit code: 0 = clean, 1 = real
    changes. A missing/unusable git (returns None from _git-style call) is
    treated as CLEAN -- we never invent dirtiness we can't verify, matching
    how the remote preflight treats an unverifiable SHA as advisory.
    """
    git = r"C:\Program Files\Git\bin\git.exe"
    if not os.path.isfile(git):
        git = "git"
    try:
        r = subprocess.run(
            [git, "-C", root, "diff", "--quiet", "--ignore-cr-at-eol", "HEAD"],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False  # can't verify -> don't block
    # git diff --quiet: 0 = no diff, 1 = diff present, >1 = error (treat as clean)
    return r.returncode == 1


def dirty_repos(extra_syspath: Optional[list[str]] = None) -> list[str]:
    """Names of the kiroshi + task repos that have REAL uncommitted changes
    (see has_real_changes). Empty list => safe to launch. Reuses the same
    root-discovery as fingerprint_repos so it checks exactly the repos whose
    code will execute."""
    roots: set[str] = set()
    try:
        import kiroshi
        kr = repo_root(os.path.dirname(kiroshi.__file__))
        if kr:
            roots.add(kr)
    except Exception:  # noqa: BLE001
        pass
    for sp in extra_syspath or []:
        rr = repo_root(sp)
        if rr:
            roots.add(rr)
    return sorted(os.path.basename(root) for root in roots if has_real_changes(root))


def fingerprint_repos(extra_syspath: Optional[list[str]] = None) -> dict[str, Any]:
    """SHA + dirty flag for kiroshi and each task-repo root on sys.path."""
    fp: dict[str, Any] = {
        "python": ".".join(map(str, sys.version_info[:2])),
        "interpreter": sys.executable,
        "repos": {},
    }
    roots: set[str] = set()
    try:
        import kiroshi
        kr = repo_root(os.path.dirname(kiroshi.__file__))
        if kr:
            roots.add(kr)
    except Exception:  # noqa: BLE001
        pass
    for sp in extra_syspath or []:
        rr = repo_root(sp)
        if rr:
            roots.add(rr)
    for root in sorted(roots):
        sha = _git(root, "rev-parse", "HEAD")
        # `dirty` = REAL uncommitted changes to tracked files only (see
        # has_real_changes). Previously this used `git status --porcelain`,
        # which also flags untracked scratch files and CRLF/LF line-ending
        # noise -- so the coordinator's stored dirty flag was almost always
        # a false positive on our Windows runners. Using the content-level
        # check makes the flag meaningful (and safe for a launch gate to act on).
        fp["repos"][os.path.basename(root)] = {
            "sha": (sha or "")[:12],
            "dirty": has_real_changes(root),
        }
    return fp
