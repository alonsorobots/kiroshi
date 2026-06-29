"""``kiroshi join`` — add this machine to a running mesh (PLAN §7.5).

One command on a new box: discover the Fixer, **mutually authenticate** it, make
the task available (pre-installed, or consent-fetch its served source — SECURITY.md
§6.5), then run a Runner in the foreground or install it as an auto-start service.

Scope (deliberately small): the one prerequisite is Python + ``pip install
kiroshi``. ``join`` does **not** bootstrap a Python environment — that stays a
separate concern.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import requests

from . import security, taskdist
from .discovery import discover_fixer
from .tasks import resolve_task
from .worker import _AUTO, verify_fixer


def _resolve_fixer(fixer: str, timeout: float = 6.0) -> Optional[str]:
    if (fixer or "").strip().lower() not in _AUTO:
        return fixer.rstrip("/")
    print("[join] discovering fixer on the LAN...", flush=True)
    url = discover_fixer(timeout=timeout)
    if url:
        print(f"[join] found fixer at {url}", flush=True)
    return url


def _headers(token: Optional[str]) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _task_importable(task_ref: str) -> bool:
    try:
        resolve_task(task_ref)
        return True
    except Exception:  # noqa: BLE001
        return False


def _consent(src: dict, url: str, accept_hash: Optional[str]) -> bool:
    """Decide whether to trust + write served task code. Fails closed."""
    sha, module = src["sha256"], src["module"]
    pinned = taskdist.read_pin(module)
    if pinned == sha:
        print(f"[join] task code already approved earlier (sha {sha[:12]}…).", flush=True)
        return True
    if accept_hash:
        if accept_hash.strip().lower() == sha:
            return True
        print(f"[join] REFUSED: --accept-task-hash does not match served code "
              f"(served {sha[:12]}…).", file=sys.stderr)
        return False
    print("\n[join] This Fixer wants to send CODE to run on this machine:", flush=True)
    print(f"         task:   {src['task_ref']}", flush=True)
    print(f"         file:   {src['filename']}  ({len(src['source'])} bytes)", flush=True)
    print(f"         sha256: {sha}", flush=True)
    print(f"         from:   {url}", flush=True)
    if pinned and pinned != sha:
        print(f"  WARNING: this DIFFERS from previously-approved {pinned[:12]}… — "
              f"the task code changed.", flush=True)
    print("  Only approve code from a Fixer you control (see SECURITY.md §6.5).",
          flush=True)
    try:
        ans = input("[join] Run this code on your machine? [y/N] ").strip().lower()
    except EOFError:
        ans = ""
    return ans in ("y", "yes")


def _acquire_task(url: str, token: Optional[str], task_ref: Optional[str],
                  accept_hash: Optional[str]) -> Optional[str]:
    """Ensure the task is importable; return the resolved task_ref or None on failure.

    Order: pre-installed wins. Otherwise, if the Fixer serves code and the operator
    consents, fetch + write + pin it and add the tasks dir to ``sys.path``.
    """
    # Make any previously-fetched task importable for the check.
    td = str(taskdist.tasks_dir())
    if td not in sys.path:
        sys.path.insert(0, td)

    try:
        meta = requests.get(f"{url}/task/meta", headers=_headers(token), timeout=15).json()
    except (requests.RequestException, ValueError):
        meta = {"served": False, "task_ref": None}

    ref = task_ref or meta.get("task_ref")
    if not ref:
        print("[join] no --task given and the Fixer doesn't advertise one. Pass "
              "--task module:function.", file=sys.stderr)
        return None

    if _task_importable(ref):
        print(f"[join] task {ref} is already importable here.", flush=True)
        return ref

    if not meta.get("served"):
        print(f"[join] task {ref!r} is not importable on this machine and the Fixer "
              f"isn't serving code.\n"
              f"  Fix: pre-install the task (pip install / checkout) on this box, or "
              f"start the Fixer with `run --serve-task` (single-file tasks only).",
              file=sys.stderr)
        return None

    # Fetch the served source, verify integrity, get consent, write + pin.
    try:
        r = requests.get(f"{url}/task/source", headers=_headers(token), timeout=30)
        r.raise_for_status()
        src = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[join] could not fetch task source: {e}", file=sys.stderr)
        return None
    if taskdist.source_sha256(src["source"]) != src["sha256"]:
        print("[join] REFUSED: served source failed its own integrity hash.",
              file=sys.stderr)
        return None

    if not _consent(src, url, accept_hash):
        print("[join] declined — not running unapproved code.", file=sys.stderr)
        return None

    path = taskdist.write_task_source(src["module"], src["source"])
    taskdist.write_pin(src["module"], src["sha256"])
    import importlib
    importlib.invalidate_caches()
    print(f"[join] wrote + approved task code -> {path}", flush=True)

    ref = task_ref or src["task_ref"]
    if not _task_importable(ref):
        print(f"[join] wrote the code but {ref!r} still won't import — check the "
              f"task reference.", file=sys.stderr)
        return None
    return ref


def join(
    fixer: str = "auto",
    *,
    task: Optional[str] = None,
    token: Optional[str] = None,
    workers: int = 0,
    service: bool = False,
    accept_task_hash: Optional[str] = None,
    syspath: Optional[list[str]] = None,
    read_root: Optional[str] = None,
    write_root: Optional[str] = None,
    gig_timeout: Optional[float] = None,
    launch_command: str = "",
) -> int:
    url = _resolve_fixer(fixer)
    if not url:
        print("[join] no fixer found. Is one running with --lan? Pass --fixer "
              "http://HOST:PORT to skip discovery.", file=sys.stderr)
        return 2

    token = security.resolve_token(token)

    # Mutual auth BEFORE sending the token or fetching code (SECURITY.md §3).
    if not verify_fixer(url, token):
        print(f"[join] could not verify the Fixer at {url} (wrong/missing token, or "
              f"a rogue Fixer). Refusing to join.", file=sys.stderr)
        return 2

    ref = _acquire_task(url, token, task, accept_task_hash)
    if not ref:
        return 2

    extra_syspath = [str(taskdist.tasks_dir())] + list(syspath or [])

    if service:
        return _install_runner_service(
            url, ref, token, workers, extra_syspath, read_root, write_root)

    # Foreground: hand off to the Runner (it re-verifies + runs the pull loop).
    if read_root:
        os.environ["KIROSHI_READ_ROOT"] = read_root
    if write_root:
        os.environ["KIROSHI_WRITE_ROOT"] = write_root
    from .worker import Runner

    print(f"[join] joining {url} as a Runner for task {ref} "
          f"(Ctrl-C to leave)…", flush=True)
    Runner(
        fixer_url=url,
        task_ref=ref,
        workers=workers,
        token=token,
        extra_syspath=extra_syspath,
        gig_timeout=gig_timeout,
        launch_command=launch_command,
    ).run()
    return 0


def _install_runner_service(url, task_ref, token, workers, syspath,
                            read_root, write_root) -> int:
    from . import winservice as ws
    from .appstate import logs_dir, state_dir

    if sys.platform != "win32":
        print("[join] --service is Windows-only.", file=sys.stderr)
        return 2
    nssm = ws.find_nssm()
    if not nssm:
        print("[join] could not find nssm.exe (install NSSM, set KIROSHI_NSSM, or "
              f"drop it in {state_dir()}\\nssm.exe).", file=sys.stderr)
        return 2
    # The NAS lesson: a NAS-bound Runner can't run as LocalSystem.
    if ws.runner_needs_user_account(read_root, write_root, None):
        print("[join] this Runner targets a NAS (UNC root) — it must run as a real "
              "user account, not LocalSystem. Use `kiroshi service install --role "
              "runner --account .\\\\<user> --password <pw> ...` for that case.",
              file=sys.stderr)
        return 2

    parts = ["-m", "kiroshi", "runner", "--fixer", url, "--task", task_ref]
    if workers:
        parts += ["--workers", str(workers)]
    for sp in syspath:
        parts += ["--syspath", sp]

    def _q(s: str) -> str:  # Windows-safe quoting (not POSIX shlex single-quotes)
        return '"' + s.replace('"', '\\"') + '"' if (" " in s or "\t" in s) else s

    app_parameters = " ".join(_q(p) for p in parts)
    env = {}
    if token:
        env["KIROSHI_TOKEN"] = token
    if read_root:
        env["KIROSHI_READ_ROOT"] = read_root
    if write_root:
        env["KIROSHI_WRITE_ROOT"] = write_root
    name = ws.DEFAULT_RUNNER_SERVICE
    if not ws.status(name).endswith(": not installed"):
        print(f"[join] service '{name}' exists — removing for clean reinstall...")
        ws.uninstall(ws.build_uninstall_commands(nssm, name))
    cmds = ws.build_install_commands(
        nssm=nssm, service_name=name, python_exe=sys.executable,
        app_parameters=app_parameters, app_directory=str(state_dir()),
        log_dir=str(logs_dir()), display_name="Kiroshi Runner",
        description="Kiroshi worker node (pulls gigs, runs them on a local pool).",
        account=None, env=env or None,
    )
    ok, out = ws.install(cmds)
    print(out)
    if ok:
        import subprocess
        try:
            subprocess.run([nssm, "start", name], timeout=15, capture_output=True)
        except Exception:  # noqa: BLE001
            pass
        print(f"[join] installed + started Runner service '{name}'. It will "
              f"auto-start on boot and pull from {url}.")
    else:
        print(f"[join] service install FAILED for '{name}'.", file=sys.stderr)
    return 0 if ok else 1
