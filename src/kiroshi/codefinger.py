"""Git/code freshness fingerprint for mesh runners (shared by remote preflight)."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from typing import Any, Iterable, Optional


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


# --------------------------------------------------------------------------
# Import-closure reproducibility gate
#
# `dirty_repos` (above) is whole-repo: it flags a repo dirty if ANY tracked
# file differs from HEAD. For a large research monorepo that is both too coarse
# and too lax as a launch gate:
#   * too coarse -- an unrelated edit (a paper .tex, a sibling script the task
#     never imports) blocks a runner whose actual code is committed;
#   * too lax -- it ignores UNTRACKED files, so a task that imports a local
#     module living only in the working tree (never committed) sails through,
#     which is the real reproducibility hole.
# The correct scope is "the code that actually runs": the task module plus the
# transitive closure of the local modules it imports -- wherever they live (the
# task repo AND the kiroshi framework files the task pulls in). Each such file
# must be tracked AND unmodified; an untracked file in the closure is a hard
# fail. Resolution is static (AST of top-level + nested imports) so we never
# execute task code (no torch/CUDA import side effects) just to gate it.
# --------------------------------------------------------------------------

def _resolve_module_file(modname: str, search_roots: Iterable[str],
                         level: int = 0,
                         current_pkg_dir: Optional[str] = None) -> Optional[str]:
    """Resolve a module name to a .py file on disk WITHOUT importing anything.
    Mirrors how Python's import machinery would find the source given
    ``search_roots`` (a sys.path-like list). ``level``>0 is a relative import
    resolved against ``current_pkg_dir``. Returns an abspath or None."""
    if level > 0:
        base = current_pkg_dir
        for _ in range(level - 1):
            if not base:
                return None
            base = os.path.dirname(base)
        if not base:
            return None
        parts = modname.split(".") if modname else []
        cand = os.path.join(base, *parts) if parts else base
        for c in (cand + ".py", os.path.join(cand, "__init__.py")):
            if os.path.isfile(c):
                return os.path.abspath(c)
        return None
    if not modname:
        return None
    parts = modname.split(".")
    for root in search_roots:
        if not root:
            continue
        cand = os.path.join(root, *parts)
        for c in (cand + ".py", os.path.join(cand, "__init__.py")):
            if os.path.isfile(c):
                return os.path.abspath(c)
    return None


def _iter_imports(pyfile: str):
    """Yield (modname, level) for every import in ``pyfile`` (top-level AND
    nested inside functions -- tasks commonly import heavy deps lazily). For
    ``from pkg import name`` we also yield ``pkg.name`` so a submodule import is
    resolvable when ``name`` is itself a module."""
    try:
        with open(pyfile, encoding="utf-8") as f:
            tree = ast.parse(f.read(), pyfile)
    except (OSError, SyntaxError, ValueError):
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, 0
        elif isinstance(node, ast.ImportFrom):
            yield (node.module or ""), node.level
            if node.level == 0 and node.module:
                for a in node.names:
                    yield f"{node.module}.{a.name}", 0


# Path segments that mark a file as third-party/vendored rather than
# first-party source -- excluded from the closure even if a first-party root
# happens to contain them (e.g. a venv checked out inside a repo).
_VENDORED = ("site-packages", "dist-packages", ".venv", "node_modules",
             "__pypackages__")


def _is_vendored(path: str) -> bool:
    parts = os.path.normpath(path).replace("\\", "/").split("/")
    return any(seg in _VENDORED for seg in parts)


def import_closure_files(task_module: str,
                         search_roots: Iterable[str]) -> set[str]:
    """The task module's source file plus the transitive closure of local
    module files it imports, resolvable under ``search_roots``. Pass FIRST-PARTY
    roots only (task import roots + the framework source root) -- not the whole
    sys.path: a module that resolves only under stdlib/site-packages drops out
    (third-party deps are pinned by the environment, not the git tree), and
    vendored paths are excluded defensively. Returns abspaths; empty if the task
    itself can't be resolved (we never invent a closure we can't see)."""
    roots = [r for r in search_roots if r]
    start = _resolve_module_file(task_module, roots)
    if not start or _is_vendored(start):
        return set()
    seen: set[str] = set()
    queue = [start]
    while queue:
        f = queue.pop()
        if f in seen:
            continue
        seen.add(f)
        pkg_dir = os.path.dirname(f)
        for modname, level in _iter_imports(f):
            rf = _resolve_module_file(modname, roots, level, pkg_dir)
            if rf and rf not in seen and not _is_vendored(rf):
                queue.append(rf)
    return seen


def _file_git_state(root: str, rel: str) -> str:
    """'untracked' | 'modified' | 'clean' for a single path within ``root``.
    'modified' uses the same content-level, EOL-noise-ignoring comparison as
    has_real_changes."""
    git = r"C:\Program Files\Git\bin\git.exe"
    if not os.path.isfile(git):
        git = "git"
    try:
        tracked = subprocess.run(
            [git, "-C", root, "ls-files", "--error-unmatch", "--", rel],
            capture_output=True, text=True, timeout=8, check=False,
        ).returncode == 0
        if not tracked:
            return "untracked"
        r = subprocess.run(
            [git, "-C", root, "diff", "--name-only", "--ignore-cr-at-eol",
             "HEAD", "--", rel],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "clean"  # can't verify -> don't block
    if r.returncode != 0:
        return "clean"
    return "modified" if (r.stdout or "").strip() else "clean"


def dirty_import_closure(task_module: str,
                         search_roots: Iterable[str]) -> list[str]:
    """Problems in the task's import closure that break reproducibility:
    ``"<repo>:<relpath> (untracked|modified)"`` for every closure file that is
    NOT committed-and-clean. Files outside any git repo (stdlib, a pip-installed
    non-editable kiroshi) are skipped -- we can't verify them and never invent
    dirtiness. Empty list => the code that will actually run is all committed."""
    problems: list[str] = []
    for f in sorted(import_closure_files(task_module, search_roots)):
        root = repo_root(f)
        if not root:
            continue
        rel = os.path.relpath(f, root).replace("\\", "/")
        state = _file_git_state(root, rel)
        if state != "clean":
            problems.append(f"{os.path.basename(root)}:{rel} ({state})")
    return problems


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
