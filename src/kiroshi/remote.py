"""``kiroshi remote`` — launch + manage Runners on other machines, robustly.

The recurring failure mode when bringing a new box into the mesh was never auth
or the engine: it was **shell quoting across machine boundaries**. A command
typed for one shell (POSIX) is re-parsed by another (PowerShell on Windows, bash
on Linux) after SSH hands it over, and multi-layer quoting (``python -c "..."``
with nested quotes, ``;``/``&`` chaining, ``2>/dev/null``) breaks ~a third of the
time. Worse, foreground runners launched over SSH die the instant the session
closes, and the *interpreter* actually used on the far side is easy to get wrong
(base conda vs. the project env), surfacing as a bare ``ModuleNotFoundError``.

``kiroshi remote`` removes that whole class of problem with three rules:

1. **Never let a shell parse the payload.** We run ``ssh <host> <python> -`` via
   :func:`subprocess.run` with an *argv list* (no local shell) and feed the
   program on **stdin** (no remote shell parsing). The only thing either shell
   sees is ``<python> -`` — no quotes, no operators, nothing to mangle.
2. **Pass data as structured JSON embedded in the piped program**, never as
   command-line arguments. Secrets (the mesh token) are written to the remote's
   token store, not put on any command line.
3. **Launch durably + interpreter-aware.** The Runner is registered as a logon
   Scheduled Task that runs in the user's interactive session (so NAS/SMB works
   without a stored password) and survives SSH disconnect *and* reboot. The exact
   interpreter comes from ``[hosts.<Host>].python`` in ``kiroshi.local.toml``.

Every step runs a **preflight** first and reports, per check, exactly what is
wrong on the remote (kiroshi importable? task importable? fixer reachable? NAS
roots visible?) instead of a cryptic stack trace.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from typing import Any, Optional

from . import security
from .config import MeshConfig, load_config


# --------------------------------------------------------------------------- #
#  transport — the quoting-proof core
# --------------------------------------------------------------------------- #
def _ssh_python(host: str, remote_python: str, script: str,
                timeout: float = 60.0) -> tuple[int, str, str]:
    """Run ``script`` on ``host`` under ``remote_python``, feeding it on stdin.

    No shell parses the script on either side: locally we use an argv list (no
    ``shell=True``); remotely the only command is ``<remote_python> -`` which
    reads the program from stdin. Returns ``(returncode, stdout, stderr)``.
    """
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           host, remote_python, "-"]
    try:
        p = subprocess.run(cmd, input=script, text=True, capture_output=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"ssh to {host} timed out after {timeout:.0f}s"
    except FileNotFoundError:
        return 127, "", "ssh executable not found on PATH"


def _marker_json(stdout: str, marker: str) -> Optional[dict]:
    """Pull a ``MARKER={...json...}`` line out of remote stdout."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith(marker):
            try:
                return json.loads(line[len(marker):])
            except ValueError:
                return None
    return None


def _embed(cfg: dict) -> str:
    """Serialize a config dict as a Python literal that's safe to embed in a
    stdin-piped program (JSON inside a raw triple-quoted string; backslashes in
    UNC paths survive because the string is raw and json.loads un-escapes them)."""
    payload = json.dumps(cfg)
    assert "'''" not in payload  # json never emits a triple single-quote
    return f"CFG = __import__('json').loads(r'''{payload}''')\n"


def _lan_ip(hint: str = "192.168.50.69") -> str:
    """Best-effort primary LAN IPv4 of this machine (the Fixer host)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((hint, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# --------------------------------------------------------------------------- #
#  remote programs (run under the remote interpreter, fed on stdin)
# --------------------------------------------------------------------------- #
_PREFLIGHT = r'''
import json, os, sys, importlib.util
R = {"python": sys.executable, "version": sys.version.split()[0],
     "user": os.environ.get("USERNAME") or os.environ.get("USER")}

# kiroshi importable in THIS interpreter?
spec = importlib.util.find_spec("kiroshi")
R["kiroshi"] = bool(spec)
if spec:
    try:
        import kiroshi
        R["kiroshi_version"] = getattr(kiroshi, "__version__", "?")
    except Exception as e:
        R["kiroshi"] = False
        R["kiroshi_error"] = repr(e)

# task importable once the requested syspath entries are added?
for sp in CFG.get("syspath", []):
    if sp and sp not in sys.path:
        sys.path.insert(0, sp)
task = CFG.get("task") or ""
mod = task.split(":")[0].replace("/", ".") if task else ""
R["task"] = None
if mod:
    try:
        ok = importlib.util.find_spec(mod) is not None
        R["task"] = bool(ok)
    except Exception as e:
        R["task"] = False
        R["task_error"] = f"{e.__class__.__name__}: {e}"

# fixer reachable + token accepted?
fx = CFG.get("fixer")
R["fixer"] = None
if fx:
    import urllib.request
    req = urllib.request.Request(fx.rstrip("/") + "/status",
                                 headers={"Authorization": "Bearer " + CFG.get("token", "")})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            R["fixer"] = resp.status
    except Exception as e:
        R["fixer"] = f"{e.__class__.__name__}: {e}"

# NAS roots visible from this box (SMB as the logged-in user)?
for key in ("read_root", "write_root"):
    v = CFG.get(key)
    if v:
        R[key] = {"path": v, "exists": os.path.isdir(v)}

R["schtasks"] = bool(__import__("shutil").which("schtasks"))
print("PREFLIGHT=" + json.dumps(R))
'''


_PROVISION = r'''
import json, os, subprocess, sys

# 1) Persist the mesh token to the remote's token store (keeps it OFF any
#    command line / task definition).
tok_status = "skipped"
if CFG.get("token"):
    try:
        from kiroshi import security
        security._write_token_file(CFG["token"])
        tok_status = security.token_path()
    except Exception as e:
        tok_status = "ERROR: " + repr(e)

# 2) Write a launcher .cmd whose body is generated here (no shell parsing) and
#    redirects output to a per-runner log (per-host debugging visibility).
appdata = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
base = os.path.join(appdata, "Kiroshi")
logdir = os.path.join(base, "logs")
os.makedirs(logdir, exist_ok=True)
launcher = os.path.join(base, "remote-runner-" + CFG["slug"] + ".cmd")
logpath = os.path.join(logdir, "remote-runner-" + CFG["slug"] + ".log")

def q(s):
    s = str(s)
    return '"' + s + '"' if (" " in s or "\t" in s) else s

parts = [q(CFG["python"]), "-m", "kiroshi", "join",
         "--fixer", CFG["fixer"], "--task", CFG["task"],
         "--workers", str(CFG["workers"])]
for sp in CFG.get("syspath", []):
    parts += ["--syspath", q(sp)]
if CFG.get("read_root"):
    parts += ["--read-root", q(CFG["read_root"])]
if CFG.get("write_root"):
    parts += ["--write-root", q(CFG["write_root"])]
cmdline = " ".join(parts) + " >> " + q(logpath) + " 2>&1"
with open(launcher, "w", encoding="utf-8") as f:
    f.write("@echo off\r\n" + cmdline + "\r\n")

# 3) Register a logon Scheduled Task that runs interactively as this user
#    (NAS-capable, no stored password) and survives SSH disconnect + reboot.
tn = CFG["task_name"]
user = os.environ.get("USERNAME") or CFG.get("user") or ""
subprocess.run(["schtasks", "/delete", "/f", "/tn", tn], capture_output=True)
tr = 'cmd /c "' + launcher + '"'
create = subprocess.run(
    ["schtasks", "/create", "/f", "/tn", tn, "/sc", "onlogon",
     "/ru", user, "/it", "/tr", tr],
    capture_output=True, text=True)
run = subprocess.run(["schtasks", "/run", "/tn", tn],
                     capture_output=True, text=True)

print("PROVISION=" + json.dumps({
    "token": tok_status, "launcher": launcher, "log": logpath,
    "task_name": tn,
    "create_rc": create.returncode,
    "create_out": (create.stdout + create.stderr).strip()[:500],
    "run_rc": run.returncode,
    "run_out": (run.stdout + run.stderr).strip()[:500],
}))
'''


# --------------------------------------------------------------------------- #
#  driver
# --------------------------------------------------------------------------- #
def _resolve(args, cfg: MeshConfig) -> dict:
    """Build the effective remote-launch config from CLI args + kiroshi.local.toml."""
    host = args.host
    hc = cfg.host(host)  # case-insensitive [hosts.<Host>] lookup
    remote_python = args.python or hc.python or "python"
    fixer = args.fixer or f"http://{_lan_ip()}:{cfg.fixer_port}"
    workers = args.workers or hc.workers
    read_root = args.read_root or hc.read_root or cfg.read_root
    write_root = args.write_root or hc.write_root or cfg.write_root
    token = security.resolve_token(args.token)
    syspath = list(args.syspath or [])
    slug = "".join(c if c.isalnum() else "-" for c in (args.group or "runner")).strip("-")
    return {
        "host": host,
        "python": remote_python,
        "fixer": fixer.rstrip("/"),
        "task": args.task or "",
        "workers": int(workers),
        "syspath": syspath,
        "read_root": read_root,
        "write_root": write_root,
        "token": token or "",
        "task_name": args.task_name or f"Kiroshi Runner ({slug})",
        "slug": slug or "runner",
        "user": None,
    }


def _print_preflight(host: str, r: dict) -> bool:
    """Pretty-print the preflight report; return True if good to launch."""
    ok = True

    def line(label, good, detail=""):
        nonlocal ok
        mark = "OK  " if good else "FAIL"
        if not good:
            ok = False
        print(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))

    print(f"[remote] preflight on {host} (interpreter: {r.get('python')}):")
    line("python present", bool(r.get("python")),
         f"{r.get('version','?')} as {r.get('user','?')}")
    line("kiroshi importable", bool(r.get("kiroshi")),
         r.get("kiroshi_error") or f"v{r.get('kiroshi_version','?')}")
    if r.get("task") is None:
        print("  [ -- ] task import: no --task given")
    else:
        line("task importable", bool(r.get("task")),
             r.get("task_error") or "found")
    fx = r.get("fixer")
    line("fixer reachable + authed", fx == 200,
         "HTTP 200" if fx == 200 else str(fx))
    for key in ("read_root", "write_root"):
        if key in r:
            d = r[key]
            line(f"{key} visible", bool(d.get("exists")), d.get("path", ""))
    line("schtasks available", bool(r.get("schtasks")))
    return ok


def run_remote(args) -> int:
    cfg = load_config()
    eff = _resolve(args, cfg)
    host = eff["host"]

    if not eff["token"]:
        print("[remote] WARNING: no mesh token resolved locally — the runner "
              "will be rejected by an authed fixer. Set KIROSHI_TOKEN or pass "
              "--token.", file=sys.stderr)

    # --- 1. preflight (always) ------------------------------------------------
    pre_cfg = {k: eff[k] for k in ("task", "syspath", "fixer", "token",
                                   "read_root", "write_root")}
    rc, out, err = _ssh_python(host, eff["python"],
                               _embed(pre_cfg) + _PREFLIGHT, timeout=45)
    if rc != 0 and not _marker_json(out, "PREFLIGHT="):
        print(f"[remote] could not reach {host} or run its interpreter "
              f"({eff['python']}).", file=sys.stderr)
        if err.strip():
            print("  ssh/stderr: " + err.strip()[:400], file=sys.stderr)
        return 2
    report = _marker_json(out, "PREFLIGHT=") or {}
    good = _print_preflight(host, report)

    if args.remote_cmd == "probe":
        return 0 if good else 1

    if not good and not args.force:
        print("[remote] preflight failed — fix the above, or re-run with "
              "--force to launch anyway.", file=sys.stderr)
        return 1

    # --- 2. provision: token + durable logon task + start now ----------------
    prov_cfg = {k: eff[k] for k in ("python", "fixer", "task", "workers",
                                    "syspath", "read_root", "write_root",
                                    "token", "task_name", "slug", "user")}
    rc, out, err = _ssh_python(host, eff["python"],
                               _embed(prov_cfg) + _PROVISION, timeout=90)
    prov = _marker_json(out, "PROVISION=")
    if not prov:
        print(f"[remote] provisioning failed on {host}.", file=sys.stderr)
        if err.strip():
            print("  stderr: " + err.strip()[:400], file=sys.stderr)
        if out.strip():
            print("  stdout: " + out.strip()[:400], file=sys.stderr)
        return 2

    print(f"[remote] token -> {prov.get('token')}")
    print(f"[remote] launcher -> {prov.get('launcher')}")
    print(f"[remote] runner log -> {prov.get('log')}")
    if prov.get("create_rc") != 0:
        print(f"[remote] schtasks /create failed: {prov.get('create_out')}",
              file=sys.stderr)
        return 2
    if prov.get("run_rc") != 0:
        print(f"[remote] schtasks /run failed: {prov.get('run_out')}",
              file=sys.stderr)
        return 2
    print(f"[remote] installed + started Scheduled Task '{prov.get('task_name')}' "
          f"on {host} (runs at logon, survives disconnect).")

    # --- 3. verify the runner shows up in the fixer's per-host view -----------
    if args.no_verify:
        return 0
    print(f"[remote] verifying {host} joins the mesh…", flush=True)
    if _verify_join(eff["fixer"], eff["token"], host, timeout=40):
        print(f"[remote] {host} is now pulling gigs from {eff['fixer']}.")
        return 0
    print(f"[remote] {host} did not appear in the fixer's runner list within "
          f"the timeout. Check the runner log on {host}:\n   {prov.get('log')}",
          file=sys.stderr)
    return 1


def _verify_join(fixer: str, token: Optional[str], host: str,
                 timeout: float = 40.0) -> bool:
    import time
    import urllib.request
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    deadline = time.time() + timeout
    want = host.lower()
    while time.time() < deadline:
        try:
            req = urllib.request.Request(fixer.rstrip("/") + "/status",
                                         headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read())
            for h in data.get("per_host", []) or []:
                if want in str(h.get("host", "")).lower():
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    return False
