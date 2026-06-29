# kiroshi

**A zero-broker, work-stealing mesh runner for Windows (and friends).**

You have a few machines and a NAS. You have an embarrassingly-parallel Python
workload. You don't want to stand up Ray (no Windows multi-node), a Celery broker,
or a Dask scheduler. Kiroshi is the missing middle:

> A **Fixer** (coordinator) hands **Gigs** (jobs) to **Runners** (worker nodes)
> that pull work over HTTP and execute it on a local process pool. **Kiroshi**
> optics — the dashboard — let you watch the whole fleet live.

- **Zero broker.** One local SQLite job store on the coordinator; everything else
  is plain HTTP. Nothing to install on the NAS.
- **Work-stealing.** Fast machines simply pull more. No manual sharding.
- **Resumable & self-healing.** Output-existence is the source of truth; dead
  Runners' leases are reclaimed automatically.
- **Fast on Windows.** Process-pool execution, bounded submission window (avoids
  the Windows pipe deadlock), per-item retry, atomic writes.

> Sibling to **at-field** (a Windows GPU/thermal watchdog): at-field keeps each rig
> *alive*; Kiroshi keeps each rig *busy*.

---

## Install

```bash
pip install -e .            # from source
pip install -e ".[fast]"    # + orjson for faster JSON
pip install -e ".[tray]"    # + system-tray UI (pystray + pillow)
```

## Quickstart — one command

`kiroshi run` is the front door: it enumerates your inputs, stands up a coordinator
+ a local worker in-process, runs the job, and shows a live progress bar — then
prints the dashboard URL and where outputs landed.

```bash
# Run a task across all matching inputs on THIS box (a local process pool):
kiroshi run mypkg.mytask:run --items "data/**/*.npz"

# Let the task fan out its own gigs (e.g. one read -> a 4fps AND an 8fps output):
kiroshi run examples.motion_resample:run --enumerate \
    --read-root //nas/clips --write-root //nas/out \
    --label "Seamless 30fps -> 4,8 fps" -- --fps 4 --fps 8

# Same command, now invite other machines to help (binds the LAN, prints a token):
kiroshi run mypkg.mytask:run --items "data/**/*.npz" --lan
#   on each other machine:   kiroshi join        (discovers it, asks for the token)
```

### Adding a machine — `kiroshi join`

On any other box (the one prerequisite is `pip install kiroshi`):

```bash
kiroshi join                      # discovers the Fixer, prompts for the token, pulls work
kiroshi join --service            # ...and install it as an auto-start Runner service
```

If the task is already importable on that machine, `join` uses it. If not, a Fixer
started with `kiroshi run --serve-task` (single-file tasks) can hand over the task
source — `join` shows its **SHA-256 and asks you to approve** before running it,
and pins the hash so it can't silently change later. This is opt-in by design; see
[SECURITY.md](SECURITY.md) §6.5. Multi-module tasks: pre-install them, or (planned)
`--task-repo`.

The same command scales from **one box to many** — `--lan` just opens the door.
For a permanent, always-on mesh that survives reboots, see
[Run as a service](#run-as-a-service-survive-reboot) or simply `kiroshi install`.

### The explicit way (full control)

`run` is glue over four primitives you can also drive directly:

```bash
# 1. Start the Fixer (coordinator + dashboard). It prints a mesh token on first run.
kiroshi fixer --db demo.db

# 2. Seed some demo gigs (token read from env/token-file automatically on this box)
kiroshi seed --fixer http://localhost:8787 --demo 500

# 3. Join with a Runner (point --task at any importable module:function)
kiroshi runner --fixer auto --task examples.sleep_task:run --workers 8
```

Open the URL the Fixer prints — `http://<host>:8787/?token=<token>` — to watch it
live. `--fixer auto` finds the Fixer over the LAN, so you rarely hardcode an IP.

### Joining from another machine

The Fixer binds **loopback by default** (secure). To let other machines join,
start it bound to the LAN:

```bash
kiroshi fixer --db demo.db --host 0.0.0.0    # exposes the (token-gated) API to the LAN
```

It prints a **mesh token** on startup. Copy it to each Runner once:

```bash
set KIROSHI_TOKEN=<the-token>           # Windows (or pass --token)
kiroshi runner --fixer auto --task mypkg.mytask:run
```

That one-time copy is the only manual step. The Runner cryptographically
**verifies the Fixer** (HMAC challenge) before sending the token or running work,
so `--fixer auto` can't be hijacked by a rogue Fixer. Do **not** port-forward the
Fixer to the internet — use a private overlay (WireGuard/Tailscale). See
[SECURITY.md](SECURITY.md).

## Write a task

A task is a module-level function (picklable for Windows `spawn`):

```python
# mypkg/mytask.py
def run(spec: dict) -> dict:
    # CPU-bound work described by `spec`
    return {"status": "ok", "metrics": {...}}
    # return {"status": "skipped"} if the output already exists (free resume)
```

Then on each machine:

```bash
kiroshi runner --fixer http://<coordinator>:8787 --task mypkg.mytask:run
```

The Fixer never imports your task — only Runners do.

## Configuration

Machine-specific values (NAS paths, per-host worker counts) go in a **gitignored**
`kiroshi.local.toml` or environment variables — never in committed files:

```toml
[fixer]
host = "host-a"
port = 8787

[paths]
read_root  = "\\\\nas\\share_direct"
write_root = "\\\\nas\\share"

[hosts.host-b]
workers = 12
capacity = 200
```

Env overrides: `KIROSHI_FIXER_HOST`, `KIROSHI_FIXER_PORT`, `KIROSHI_READ_ROOT`,
`KIROSHI_WRITE_ROOT`, `KIROSHI_CONFIG`. For a NAS over SMB, also set
`KIROSHI_NAS_USER` / `KIROSHI_NAS_PASS` (see [NAS / shared storage](#nas--shared-storage-over-smb)).

> Tip: write UNC roots with **forward slashes** (`//nas/share/path`). A shell or
> env var can silently eat a leading backslash from `\\nas\...`, turning it into a
> *local* path; `kiroshi doctor` detects this and fails loudly.

## NAS / shared storage over SMB

Runners read inputs and write outputs over a shared filesystem. On a real NAS this
is the part that bites: a Windows **network logon** (SSH, a Scheduled Task, or a
service running as `LocalSystem`/SYSTEM) **cannot use mapped drives or the
per-user credentials in Credential Manager** — the classic "double-hop" — so
authenticated UNC paths fail with *path not found* even though the same path works
in Explorer.

Kiroshi solves this with a built-in SMB data plane (the `smb` extra, install
`pip install kiroshi[smb]`). When a read/write root is a UNC path **and** NAS
credentials are present, Kiroshi talks SMB **directly over TCP 445** with explicit
credentials (via `smbprotocol`), bypassing the Windows redirector entirely. The
result: the **same Runner config works from an interactive shell, SSH, a Scheduled
Task, or a service — under any account, including SYSTEM**. No drive mapping, no
`cmdkey`, no Credential Manager.

```powershell
$env:KIROSHI_NAS_USER = "svc_account"        # a NAS/Samba account, not a Windows login
$env:KIROSHI_NAS_PASS = "<password>"         # keep out of committed files
$env:KIROSHI_READ_ROOT  = "//nas/dataset"
$env:KIROSHI_WRITE_ROOT = "//nas/outputs"
kiroshi runner --fixer auto --task mypkg.mytask:run
```

- Per-server creds: `KIROSHI_NAS_USER_<SERVER>` / `KIROSHI_NAS_PASS_<SERVER>`
  (e.g. `KIROSHI_NAS_USER_192_168_1_10`) override the defaults for one host.
- Without `smbprotocol` or creds, Kiroshi falls back to the OS redirector — fine
  for an interactive session, but it will fail from SSH/service logons (doctor
  warns about exactly this).
- `kiroshi doctor` authenticates to the share and does a real **write+delete
  probe** as the configured account, so a credential/ACL problem surfaces before
  you seed a single gig.
- Storage tip: on a parity array, point the **read** root at a read-optimized
  share and the **write** root at a cache/SSD-backed share — small-file writes to
  a parity disk are the usual throughput wall.

## CLI

| Command | What |
|---|---|
| `kiroshi run m:f --items GLOB` | **Front door** — enumerate, run a local mesh, live progress bar. `--lan` invites other machines; `--enumerate` calls the task's `enumerate_gigs(args)`; `--serve-task` offers the code to joiners. |
| `kiroshi join` | Add this machine to a running mesh as a Runner (`--service` to auto-start; consent-gated code fetch). |
| `kiroshi install` | One-command always-on: Fixer as a boot-start service + tray autostart on login. (`uninstall` to remove.) |
| `kiroshi autostart on\|off\|status` | Manage the tray's login-autostart (`HKCU\Run`). |
| `kiroshi fixer` | Run the coordinator + dashboard (auto-generates a mesh token). |
| `kiroshi runner --task m:f` | Run a worker node (`--fixer auto` to discover). |
| `kiroshi seed --demo N` / `--jobs file.jsonl` | Enqueue gigs (`--group`/`--label` to name a campaign). |
| `kiroshi status` | Print a `/status` snapshot. |
| `kiroshi requeue --state failed` | Return failed/stuck gigs to pending. |
| `kiroshi doctor --task m:f` | Preflight checks (env, task import, NAS, fixer). |
| `kiroshi ps` | List Kiroshi processes registered on this machine. |
| `kiroshi stop --role runner --all` | Ask local Runners/Fixer to drain + exit. |
| `kiroshi tray` | System-tray status icon + menu (needs the `tray` extra). |

Common flags: `--token` (or `KIROSHI_TOKEN`) on every networked command;
`--no-auth` on `fixer` for a trusted-LAN dev mesh (discouraged on public binds).

## Watching the fleet

The dashboard has three views, all themed like Kiroshi optics:

- **Overview** (`/`) — totals, aggregate progress bar, and an at-field-style
  **throughput rate-curve over time** (hand-rolled SVG, no chart libs).
- **Jobs** (`/ui/jobs`) — one row per **campaign** (gigs grouped by `job_id`
  prefix): a pill progress bar, live counts, the **full launch command** that
  produced it, a **graph** button (per-job rate-over-time curve), and an
  optional **page** button to a task-supplied custom HTML view.
- **History** (`/ui/history`) — every campaign ever seeded with progress,
  start/end times, and launch command.

### Custom per-job pages
Drop a `job.html` into the Fixer's `--pages-dir`; it's served at
`/p/job.html?job=<campaign>` and a **page** button appears per job. See
[`examples/job.html`](examples/job.html) for the (small) contract — it gets the
campaign id + token and can fetch `/jobs` and `/job/<id>` to render outputs.

## Observability, logging & emergency stop

- **Launch-command capture:** every Fixer/Runner records its full command line
  (all flags); Runners report theirs to the Fixer so it shows up per job.
- **Terminal logging:** each process tees stdout+stderr to a rotating log under
  the state dir (`%PROGRAMDATA%\Kiroshi\logs` / `~/.kiroshi/logs`).
- **Process registry:** each Fixer/Runner writes a manifest (pid, role, launch
  command, log path, graceful-stop hook) so a watchdog can enumerate and stop
  Kiroshi processes. If **at-field** is installed, manifests are also advertised
  under `%PROGRAMDATA%\ATField\clients\kiroshi`. `kiroshi stop` (or the tray)
  requests a clean **drain** before any hard kill.
- **at-field awareness:** Runners watch at-field's `pause.sentinel` and back off
  (stop leasing) while the rig is being thermal/OOM-protected.

## Run as a service (survive reboot)

For an unattended mesh, wrap the Fixer/Runners as auto-starting Windows services
via [NSSM](https://nssm.cc) (the at-field pattern). Kiroshi finds `nssm.exe` on
`PATH`, via `KIROSHI_NSSM`, or in its state dir; the logic lives in
`kiroshi service` and the `scripts\*.ps1` are just elevation shims.

```powershell
# Fixer — LocalSystem is fine (local SQLite + a port, no NAS). Run elevated:
kiroshi service install --role fixer --db C:\kiroshi\jobs.db --port 8787
nssm start kiroshi-fixer

# Runner — with the SMB data plane, NAS access works under ANY account:
kiroshi service install --role runner --task mypkg.mytask:run --fixer auto `
  --workers 8 --token <mesh-token> `
  --read-root '//nas/dataset' --write-root '//nas/outputs' `
  --env KIROSHI_NAS_USER=svc_account --env KIROSHI_NAS_PASS=<pw>
nssm start kiroshi-runner

kiroshi service status          # sc query (no admin needed)
kiroshi service uninstall --role runner
```

> **NAS access from services:** with the `smb` extra and `KIROSHI_NAS_USER/PASS`
> set (see [NAS / shared storage](#nas--shared-storage-over-smb)), a NAS-bound
> Runner works even as `LocalSystem`/SYSTEM — Kiroshi authenticates SMB directly
> rather than relying on Credential Manager. Services don't inherit your shell's
> environment, so pass everything the task needs via `--token`,
> `--read-root/--write-root`, and `--env KEY=VALUE`. (The legacy fallback — a
> Runner under a real user account whose Credential Manager holds the NAS login —
> still works if you don't install the `smb` extra; Kiroshi warns when a UNC root
> is configured without SMB credentials.)

## Security

Security is a first-class constraint. **Every coordination endpoint requires a
shared mesh token**; the Runner **mutually authenticates the Fixer** (HMAC
challenge) before sending the token or running work; the Fixer **binds loopback
by default**; the dashboard escapes untrusted input; the token is **redacted from
logs**; and the discovery beacon leaks no hostname or secret. The one thing
Kiroshi does *not* provide is transport encryption — on an untrusted network run
it over a private overlay (WireGuard/Tailscale) or TLS, and never port-forward it
to the internet. Read [SECURITY.md](SECURITY.md) for the full threat model
(additive vs inherent risk) and the frontier-deployment posture.

## Status

Early but functional (v0.0.1). See [PLAN.md](PLAN.md) for architecture and the
build milestones. MIT licensed.
