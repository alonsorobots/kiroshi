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
wrong on the remote (kiroshi importable? task importable? coordinator reachable? NAS
roots visible?) instead of a cryptic stack trace.
"""
from __future__ import annotations

import json
import os
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


def _git_local(path: str, *args: str) -> Optional[str]:
    git = r"C:\Program Files\Git\bin\git.exe"
    if not os.path.isfile(git):
        git = "git"
    try:
        p = subprocess.run([git, "-C", path, *args], capture_output=True,
                           text=True, timeout=15)
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _repo_root_local(start: str) -> Optional[str]:
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            return None
        d = nd


def _fingerprint_local(syspath: list) -> dict:
    """Compute the same env fingerprint locally (the coordinator = source of
    truth) so the remote's can be compared against it."""
    import sys as _sys
    fp = {"python": ".".join(map(str, _sys.version_info[:2])), "repos": {}, "pkgs": {},
          "interpreter": _sys.executable}
    roots = set()
    try:
        import kiroshi
        kr = _repo_root_local(os.path.dirname(kiroshi.__file__))
        if kr:
            roots.add(kr)
    except Exception:  # noqa: BLE001
        pass
    for sp in (syspath or []):
        rr = _repo_root_local(sp)
        if rr:
            roots.add(rr)
    for root in roots:
        sha = _git_local(root, "rev-parse", "HEAD")
        dirty = _git_local(root, "status", "--porcelain")
        fp["repos"][os.path.basename(root)] = {"sha": (sha or "")[:12],
                                               "dirty": bool(dirty)}
    for mod in ("kiroshi", "numpy"):
        try:
            m = __import__(mod)
            fp["pkgs"][mod] = getattr(m, "__version__", "?")
        except Exception:  # noqa: BLE001
            fp["pkgs"][mod] = None
    return fp


def _lan_ip(hint: str = "192.168.1.1") -> str:
    """Best-effort primary LAN IPv4 of this machine (the Coordinator host)."""
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
R["task_selftest"] = None
if mod:
    try:
        ok = importlib.util.find_spec(mod) is not None
        R["task"] = bool(ok)
    except Exception as e:
        R["task"] = False
        R["task_error"] = f"{e.__class__.__name__}: {e}"
    # Deep check: import the module (catches top-level import errors that
    # find_spec misses) and run its selftest() if it has one. selftest()
    # exercises LAZY imports + core compute on THIS interpreter — the failure
    # mode find_spec is blind to (a dep imported inside run(), a missing
    # repo-relative asset, a broken .pyd). Time-bounded; compute-only by design.
    if R["task"]:
        import threading as _th
        _st_box = {}
        def _run_selftest():
            try:
                import importlib as _il
                m = _il.import_module(mod)
                fn = getattr(m, "selftest", None)
                if fn is None or not callable(fn):
                    _st_box["result"] = "absent"
                    return
                fn()
                _st_box["result"] = "ok"
            except Exception as e:  # noqa: BLE001
                _st_box["result"] = "fail"
                _st_box["error"] = f"{e.__class__.__name__}: {e}"
        _st_t = _th.Thread(target=_run_selftest, daemon=True)
        _st_t.start()
        _st_t.join(timeout=30)
        if _st_t.is_alive():
            R["task_selftest"] = "timeout"
        else:
            R["task_selftest"] = _st_box.get("result")
            if _st_box.get("error"):
                R["task_selftest_error"] = _st_box["error"]

# coordinator reachable + token accepted?
fx = CFG.get("coordinator")
R["coordinator"] = None
if fx:
    import urllib.request
    req = urllib.request.Request(fx.rstrip("/") + "/status",
                                 headers={"Authorization": "Bearer " + CFG.get("token", "")})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            R["coordinator"] = resp.status
    except Exception as e:
        R["coordinator"] = f"{e.__class__.__name__}: {e}"

# NAS roots visible from this box (SMB as the logged-in user)?
for key in ("read_root", "write_root"):
    v = CFG.get(key)
    if v:
        R[key] = {"path": v, "exists": os.path.isdir(v)}

R["schtasks"] = bool(__import__("shutil").which("schtasks"))

# ---- ENV FINGERPRINT (catches stale code + version drift in ONE check) ----
import subprocess

def _git(path, *args):
    g = r"C:\Program Files\Git\bin\git.exe"
    if not os.path.isfile(g):
        g = "git"
    try:
        p = subprocess.run([g, "-C", path, *args], capture_output=True,
                           text=True, timeout=15)
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:
        return None

def _repo_root(start):
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            return None
        d = nd

fp = {"python": ".".join(map(str, sys.version_info[:2])), "repos": {}, "pkgs": {},
      "interpreter": sys.executable}
# kiroshi's own repo + each requested syspath repo
roots = set()
try:
    import kiroshi
    kr = _repo_root(os.path.dirname(kiroshi.__file__))
    if kr:
        roots.add(kr)
except Exception:
    pass
for sp in CFG.get("syspath", []):
    rr = _repo_root(sp)
    if rr:
        roots.add(rr)
for root in roots:
    sha = _git(root, "rev-parse", "HEAD")
    dirty = _git(root, "status", "--porcelain")
    fp["repos"][os.path.basename(root)] = {
        "sha": (sha or "")[:12], "dirty": bool(dirty)}
for mod in ("kiroshi", "numpy"):
    try:
        m = __import__(mod)
        fp["pkgs"][mod] = getattr(m, "__version__", "?")
    except Exception:
        fp["pkgs"][mod] = None
R["fingerprint"] = fp

# ---- REAL I/O PROBE (exercise kfs read + write in the remote's context) ----
def _io_probe():
    out = {}
    try:
        from kiroshi import kfs
        from kiroshi import paths as kpaths
    except Exception as e:
        return {"error": "kfs import failed: " + repr(e)}
    rr = CFG.get("read_root")
    wr = CFG.get("write_root")
    # Backend signal: direct SMB (context-proof) vs OS redirector (logon-bound)
    out["is_unc"] = bool(rr and kfs.is_unc(rr))
    out["smb_creds_env"] = bool(os.environ.get("KIROSHI_NAS_USER"))
    # READ: find one file under read_root (bounded walk) and read a few bytes
    if rr:
        try:
            found = None
            seen = 0
            for dp, _dirs, files in kfs.walk(rr):
                seen += 1
                if files:
                    found = kpaths.confined_join(rr, files[0]) if dp.rstrip("/\\") == str(rr).rstrip("/\\") \
                        else (dp.rstrip("/\\") + ("\\" if kfs.is_unc(rr) else "/") + files[0])
                    break
                if seen > 60:
                    break
            if found:
                with kfs.open(found, "rb") as fh:
                    fh.read(64)
                out["read"] = {"ok": True}
            else:
                out["read"] = {"ok": False, "err": "no file found under read_root"}
        except Exception as e:
            out["read"] = {"ok": False, "err": repr(e)[:200]}
    # WRITE: atomic_write a tiny temp under write_root, then remove it
    if wr:
        try:
            import uuid as _uuid
            name = ".kiroshi_ioprobe_" + _uuid.uuid4().hex[:8]
            sep = "\\" if kfs.is_unc(wr) else "/"
            tpath = str(wr).rstrip("/\\") + sep + name
            with kfs.atomic_write(tpath) as fh:
                fh.write(b"probe")
            try:
                kfs.remove(tpath)
            except Exception:
                pass
            out["write"] = {"ok": True}
        except Exception as e:
            out["write"] = {"ok": False, "err": repr(e)[:200]}
    return out

# Time-bound the I/O probe: the OS SMB redirector can HANG in a network-logon
# (SSH) context, so never let it block the whole preflight.
import threading
_io_box = {}
_io_t = threading.Thread(target=lambda: _io_box.update(_io_probe()), daemon=True)
_io_t.start()
_io_t.join(timeout=12)
R["io"] = _io_box if not _io_t.is_alive() else {"timeout": True}

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
         "--fixer", CFG["coordinator"], "--task", CFG["task"],
         "--workers", str(CFG["workers"])]
for sp in CFG.get("syspath", []):
    parts += ["--syspath", q(sp)]
if CFG.get("read_root"):
    parts += ["--read-root", q(CFG["read_root"])]
if CFG.get("write_root"):
    parts += ["--write-root", q(CFG["write_root"])]
cmdline = " ".join(parts) + " > " + q(logpath) + " 2>&1"
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
    ssh_target = getattr(hc, "ssh_target", None) or host
    remote_python = args.python or hc.python or "python"
    coordinator = args.coordinator or f"http://{_lan_ip()}:{cfg.coordinator_port}"
    workers = args.workers or hc.workers
    read_root = args.read_root or hc.read_root or cfg.read_root
    write_root = args.write_root or hc.write_root or cfg.write_root
    token = security.resolve_token(args.token)
    syspath = list(args.syspath or [])
    # Name the durable Scheduled Task / launcher / log after the job (the
    # `remote` subparser exposes --job, not --group; referencing a nonexistent
    # attribute here crashed every `remote join`).
    slug_src = getattr(args, "job", None) or getattr(args, "group", None) or "runner"
    slug = "".join(c if c.isalnum() else "-" for c in slug_src).strip("-")
    return {
        "host": host,
        "ssh_target": ssh_target,
        "python": remote_python,
        "coordinator": coordinator.rstrip("/"),
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


def _print_preflight(host: str, r: dict, local_fp: Optional[dict] = None) -> bool:
    """Pretty-print the preflight report; return True if good to launch."""
    ok = True

    def line(label, good, detail=""):
        nonlocal ok
        mark = "OK  " if good else "FAIL"
        if not good:
            ok = False
        print(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))

    def warn(label, good, detail=""):
        # Advisory: prints OK/WARN but never blocks launch (drift signals that
        # rarely affect correctness — python minor version, dep versions, a
        # dirty working tree). Code-SHA mismatch stays a hard block via line().
        mark = "OK  " if good else "WARN"
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
        st = r.get("task_selftest")
        if st == "ok":
            line("task selftest", True, "fixture passed (lazy imports + compute)")
        elif st == "absent":
            print("  [ -- ] task selftest: task defines no selftest() hook")
        elif st == "timeout":
            warn("task selftest", False, "timed out (>30s) — skipped, not blocking")
        elif st == "fail":
            line("task selftest", False,
                 (r.get("task_selftest_error") or "failed")
                 + " — a runtime/lazy dep is missing on this node")
    fx = r.get("coordinator")
    line("coordinator reachable + authed", fx == 200,
         "HTTP 200" if fx == 200 else str(fx))

    # --- env fingerprint: code (git SHA) + key deps vs the coordinator -------
    rfp = r.get("fingerprint") or {}
    lfp = local_fp or {}
    if lfp:
        # python major.minor — advisory (rarely affects numpy correctness)
        warn(f"python version matches ({rfp.get('python','?')})",
             rfp.get("python") == lfp.get("python"),
             f"local {lfp.get('python','?')}")
        # per-repo git SHA — THE check that prevents stale-code drift. A real
        # SHA divergence is a HARD block (line); a dirty tree alone is advisory.
        rrepos, lrepos = rfp.get("repos", {}), lfp.get("repos", {})
        for name in sorted(set(rrepos) | set(lrepos)):
            rv, lv = rrepos.get(name, {}), lrepos.get(name, {})
            rsha, lsha = rv.get("sha"), lv.get("sha")
            dirty = rv.get("dirty") or lv.get("dirty")
            if rsha and lsha and rsha == lsha:
                detail = rsha + (" (dirty working tree)" if dirty else "")
                warn(f"code in sync: {name}", True, detail) if dirty \
                    else line(f"code in sync: {name}", True, detail)
            elif not rsha or not lsha:
                warn(f"code in sync: {name}", False,
                     f"cannot verify (no git) remote {rsha or '-'} / local {lsha or '-'}")
            else:
                line(f"code in sync: {name}", False,
                     f"remote {rsha} vs local {lsha} - STALE CODE; sync before launch")
        # key package versions — advisory
        rpk, lpk = rfp.get("pkgs", {}), lfp.get("pkgs", {})
        for mod in sorted(set(rpk) | set(lpk)):
            rv, lv = rpk.get(mod), lpk.get(mod)
            warn(f"{mod} version matches ({rv or '-'})", rv == lv,
                 f"local {lv or '-'}")
        # interpreter path — advisory (different paths aren't wrong, but flag
        # them so hardcoded paths in launch scripts are caught early)
        r_int = rfp.get("interpreter", "?")
        l_int = lfp.get("interpreter", "?")
        warn(f"interpreter path", r_int == l_int,
             f"remote {r_int} vs local {l_int}" +
             (" (different env managers — don't hardcode paths!)" if r_int != l_int else ""))

    # --- real I/O probe: did kfs actually read + write the NAS roots? --------
    #
    # Authority rule: this preflight runs over SSH (a *network* logon). The
    # Windows SMB redirector CANNOT authenticate a UNC share from a network
    # logon, so an os-level / redirector probe here FALSE-FAILS even though the
    # runner's Scheduled Task (an *interactive* logon with cached creds) will
    # read/write fine. Therefore a NAS probe failure is only a HARD block when
    # KIROSHI_NAS_USER is set — smbprotocol authenticates explicitly and is
    # context-proof, so its result is authoritative in every logon type.
    # Without creds, redirector-bound failures are advisory (warn), not blocks.
    io = r.get("io") or {}
    smb_authoritative = bool(io.get("smb_creds_env"))
    nas_check = line if smb_authoritative else warn
    if io.get("timeout"):
        print("  [WARN] NAS I/O probe timed out (>12s). The OS SMB redirector "
              "hangs in a network-logon (SSH) context — this does NOT necessarily "
              "mean the Scheduled Task (interactive) will fail, but it's a strong "
              "signal to configure direct SMB (KIROSHI_NAS_USER/PASS) for "
              "context-proof access.")
    elif io.get("error"):
        nas_check("kfs available", False, io["error"])
    else:
        backend = ("direct-SMB" if smb_authoritative
                   else ("OS-redirector (logon-bound!)" if io.get("is_unc") else "local-fs"))
        rd, wr = io.get("read"), io.get("write")
        if rd is not None:
            nas_check(f"NAS read probe ({backend})", bool(rd.get("ok")),
                      rd.get("err", ""))
        if wr is not None:
            nas_check(f"NAS write probe ({backend})", bool(wr.get("ok")),
                      wr.get("err", ""))
        if io.get("is_unc") and not smb_authoritative:
            print("  [WARN] using the OS SMB redirector (no KIROSHI_NAS_USER set); "
                  "this probe ran over SSH (network logon) and may differ from the "
                  "Scheduled Task (interactive) context. Set machine-scoped NAS "
                  "creds for context-proof, higher-throughput direct SMB.")

    # The os.path.isdir "visible" check has the SAME network-logon blind spot as
    # above, and is redundant with the kfs probe — keep it advisory only, and
    # skip it entirely when smbprotocol already gave an authoritative answer.
    if not smb_authoritative:
        for key in ("read_root", "write_root"):
            if key in r:
                d = r[key]
                warn(f"{key} visible (redirector)", bool(d.get("exists")),
                     d.get("path", ""))
    line("schtasks available", bool(r.get("schtasks")))

    # --- storage-class advisory (shared with doctor + MCP advise_io) ---
    from . import iohint
    from .storage import has_parity, load_topology
    topo = load_topology()
    if topo:
        rr = (r.get("read_root") or {}).get("path") or None
        wr = (r.get("write_root") or {}).get("path") or None
        for f in iohint.advise_job(read_root=rr, write_root=wr, disks=topo).findings:
            print(f"  [{f.level.upper()}] {f.message}")
        if has_parity(topo):
            print("  [INFO] parity-protected topology detected — the resource "
                  "governor's global write budget is active. Non-sub-job workloads "
                  "using ResourceClient.acquire(mode='write') will self-limit.")

    return ok


def run_remote(args) -> int:
    cfg = load_config()
    eff = _resolve(args, cfg)
    host = eff["host"]

    if not eff["token"]:
        print("[remote] WARNING: no mesh token resolved locally — the runner "
              "will be rejected by an authed coordinator. Set KIROSHI_TOKEN or pass "
              "--token.", file=sys.stderr)

    # --- 1. preflight (always) ------------------------------------------------
    pre_cfg = {k: eff[k] for k in ("task", "syspath", "coordinator", "token",
                                   "read_root", "write_root")}
    rc, out, err = _ssh_python(eff["ssh_target"], eff["python"],
                               _embed(pre_cfg) + _PREFLIGHT, timeout=60)
    if rc != 0 and not _marker_json(out, "PREFLIGHT="):
        print(f"[remote] could not reach {host} or run its interpreter "
              f"({eff['python']}).", file=sys.stderr)
        if err.strip():
            print("  ssh/stderr: " + err.strip()[:400], file=sys.stderr)
        return 2
    report = _marker_json(out, "PREFLIGHT=") or {}
    local_fp = _fingerprint_local(eff["syspath"])
    good = _print_preflight(host, report, local_fp)

    if args.remote_cmd == "probe":
        return 0 if good else 1

    if not good and not args.force:
        print("[remote] preflight failed — fix the above, or re-run with "
              "--force to launch anyway.", file=sys.stderr)
        return 1

    # --- 2. provision: token + durable logon task + start now ----------------
    prov_cfg = {k: eff[k] for k in ("python", "coordinator", "task", "workers",
                                    "syspath", "read_root", "write_root",
                                    "token", "task_name", "slug", "user")}
    rc, out, err = _ssh_python(eff["ssh_target"], eff["python"],
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

    # --- 3. verify the runner shows up in the coordinator's per-host view -----------
    if args.no_verify:
        return 0
    print(f"[remote] verifying {host} joins the mesh…", flush=True)
    if _verify_join(eff["coordinator"], eff["token"], host, timeout=40):
        print(f"[remote] {host} is now pulling gigs from {eff['coordinator']}.")
        return 0
    print(f"[remote] {host} did not appear in the coordinator's runner list within "
          f"the timeout. Check the runner log on {host}:\n   {prov.get('log')}",
          file=sys.stderr)
    return 1


def _verify_join(coordinator: str, token: Optional[str], host: str,
                 timeout: float = 40.0) -> bool:
    import time
    import urllib.request
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    deadline = time.time() + timeout
    want = host.lower()
    while time.time() < deadline:
        try:
            req = urllib.request.Request(coordinator.rstrip("/") + "/status",
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
