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

    ``git status --porcelain`` (used originally by fingerprint_repos' dirty
    flag) is the WRONG signal for a launch gate: it flags untracked
    scratch/output/.pyc (every research repo has these) and CRLF<->LF
    line-ending artifacts (a persistent condition on our Windows runners).
    Gating on that with no override would refuse to start every runner.

    ``git diff --name-only --ignore-cr-at-eol HEAD`` is the correct primitive:
    it lists only TRACKED files whose *content* differs from HEAD (untracked
    files never appear in a diff), and ``--ignore-cr-at-eol`` makes pure EOL
    noise drop out of the list. Non-empty stdout => a real, tracked change.

    Why NOT ``git diff --quiet ... HEAD`` (the obvious choice, and what this
    used to do): ``--quiet`` implies ``--exit-code`` and suppresses actual
    diff generation, so it does NOT reliably honor ``--ignore-cr-at-eol`` when
    computing its exit status. Empirically (Demeter, held-frames campaign) it
    returned exit 0 = "clean" for a file with a genuine 8-add/85-del change,
    and its result even varied between an interactive shell and a Python
    subprocess for the same tree -- a nondeterministic gate that silently
    passed uncommitted code through, which is exactly the failure this gate
    exists to prevent. ``--name-only`` forces the content comparison, so the
    ignore flag is honored and the result is stable across calls.

    stdout carries only changed filenames (git's CRLF-renormalization
    "warning: ..." lines go to stderr, which we do not read). A git error /
    non-repo (nonzero return) is treated as CLEAN -- we never invent dirtiness
    we cannot verify, matching how the remote preflight treats an unverifiable
    SHA as advisory.
    """
    git = r"C:\Program Files\Git\bin\git.exe"
    if not os.path.isfile(git):
        git = "git"
    try:
        r = subprocess.run(
            [git, "-C", root, "diff", "--name-only", "--ignore-cr-at-eol", "HEAD"],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False  # can't verify -> don't block
    if r.returncode != 0:
        return False  # git error / not a repo -> can't verify -> don't block
    return bool((r.stdout or "").strip())


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
