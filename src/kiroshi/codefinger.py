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
        dirty = _git(root, "status", "--porcelain")
        fp["repos"][os.path.basename(root)] = {
            "sha": (sha or "")[:12],
            "dirty": bool(dirty),
        }
    return fp
