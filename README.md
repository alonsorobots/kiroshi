# kiroshi

**A zero-broker, work-stealing mesh runner for Windows (and friends).**

You have a few machines and a NAS. You have an embarrassingly-parallel Python
workload. You don't want to stand up Ray (no Windows multi-node), a Celery broker,
or a Dask scheduler. Kiroshi is the missing middle:

> A **Coordinator** (coordinator) hands **Sub-jobs** (jobs) to **Runners** (worker nodes)
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
pip install -e ".[mcp]"     # + expose Kiroshi to LLM agents via MCP
```

## What Kiroshi gives you

- **Zero-broker work-stealing mesh** — a Coordinator (coordinator + SQLite queue +
  dashboard) hands sub-jobs to Runners that pull over HTTP. Fast machines pull more.
- **Resumable & self-healing** — output-existence is the source of truth; dead
  runners' leases are reclaimed; kill/restart any part and it picks up where it
  left off.
- **Storage-topology aware** — per-disk read/write concurrency budgets so a
  sharded NAS isn't over-saturated (`kiroshi nas probe`/`assess`/`benchmark`).
- **Declarative multi-stage pipelines** — chain dependent stages with typed
  edges (`each` / `quorum` / `all` / `artifact`) instead of hand-rolled glue
  (`kiroshi pipeline`; see [`docs/PIPELINE.md`](docs/PIPELINE.md)).
- **Resource governor** — mesh-wide slot budgeting for shared resources
  (per-disk reads, parity writes, GPU/download budgets) from inside a task.
- **Live advisories** — the Coordinator emits structured warnings (NAS thrash,
  throughput collapse, failure spikes) over `/advisories`.
- **Operator UX** — a self-restarting system-tray lens (`kiroshi tray` +
  `kiroshi autostart`), firewall/service installers, and `kiroshi doctor`
  preflight.
- **Agent-friendly** — a task-indexed [`AGENTS.md`](AGENTS.md),
  `kiroshi capabilities --json`, and an optional MCP server
  (`kiroshi mcp`) that exposes all of the above as typed tools to LLM clients.

### Learn it fast

- [`AGENTS.md`](AGENTS.md) — the task-indexed capability guide (also machine-
  readable via `kiroshi capabilities --json`).
- [`examples/hello_mesh.md`](examples/hello_mesh.md) — 5-minute walkthrough:
  one-shot, ad-hoc LAN, and durable production.
- [`examples/task_minimal.py`](examples/task_minimal.py) — idiomatic task
  template (the `run`/`enumerate_sub-jobs` ABI, crash-safe writes, idempotent skip).
- [`examples/pipeline.example.toml`](examples/pipeline.example.toml) — a
  4-stage pipeline with typed dependency edges.

## Quickstart — one command

`kiroshi run` is the front door: it enumerates your inputs, stands up a coordinator
+ a local worker in-process, runs the job, and shows a live progress bar — then
prints the dashboard URL and where outputs landed.

```bash
# Run a task across all matching inputs on THIS box (a local process pool):
kiroshi run mypkg.mytask:run --items "data/**/*.npz"

# Let the task fan out its own sub-jobs (e.g. one read -> a 4fps AND an 8fps output):
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
kiroshi join                      # discovers the Coordinator, prompts for the token, pulls work
kiroshi join --service            # ...and install it as an auto-start Runner service
```

If the task is already importable on that machine, `join` uses it. If not, a Coordinator
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
# 1. Start the Coordinator (coordinator + dashboard). It prints a mesh token on first run.
kiroshi coordinator --db demo.db

# 2. Seed some demo sub-jobs (token read from env/token-file automatically on this box)
kiroshi seed --coordinator http://localhost:8787 --demo 500

# 3. Join with a Runner (point --task at any importable module:function)
kiroshi runner --coordinator auto --task examples.sleep_task:run --workers 8
```

Open the URL the Coordinator prints — `http://<host>:8787/?token=<token>` — to watch it
live. `--coordinator auto` finds the Coordinator over the LAN, so you rarely hardcode an IP.

### Joining from another machine

The Coordinator binds **loopback by default** (secure). To let other machines join,
start it bound to the LAN:

```bash
kiroshi coordinator --db demo.db --host 0.0.0.0    # exposes the (token-gated) API to the LAN
```

It prints a **mesh token** on startup. Copy it to each Runner once:

```bash
set KIROSHI_TOKEN=<the-token>           # Windows (or pass --token)
kiroshi runner --coordinator auto --task mypkg.mytask:run
```

That one-time copy is the only manual step. The Runner cryptographically
**verifies the Coordinator** (HMAC challenge) before sending the token or running work,
so `--coordinator auto` can't be hijacked by a rogue Coordinator. Do **not** port-forward the
Coordinator to the internet — use a private overlay (WireGuard/Tailscale). See
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
kiroshi runner --coordinator http://<coordinator>:8787 --task mypkg.mytask:run
```

The Coordinator never imports your task — only Runners do.

## Configuration

Machine-specific values (NAS paths, per-host worker counts) go in a **gitignored**
`kiroshi.local.toml` or environment variables — never in committed files:

```toml
[coordinator]
host = "host-a"
port = 8787

[paths]
read_root  = "\\\\nas\\share_direct"
write_root = "\\\\nas\\share"

[hosts.host-b]
workers = 12
capacity = 200
```

Env overrides: `KIROSHI_COORDINATOR_HOST`, `KIROSHI_COORDINATOR_PORT` (legacy
`KIROSHI_FIXER_HOST`/`KIROSHI_FIXER_PORT` still honored), `KIROSHI_READ_ROOT`,
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
kiroshi runner --coordinator auto --task mypkg.mytask:run
```

- Per-server creds: `KIROSHI_NAS_USER_<SERVER>` / `KIROSHI_NAS_PASS_<SERVER>`
  (e.g. `KIROSHI_NAS_USER_192_168_1_10`) override the defaults for one host.
- Without `smbprotocol` or creds, Kiroshi falls back to the OS redirector — fine
  for an interactive session, but it will fail from SSH/service logons (doctor
  warns about exactly this).
- `kiroshi doctor` authenticates to the share and does a real **write+delete
  probe** as the configured account, so a credential/ACL problem surfaces before
  you seed a single sub-job.
- Storage tip: on a parity array, point the **read** root at a read-optimized
  share and the **write** root at a cache/SSD-backed share — small-file writes to
  a parity disk are the usual throughput wall.

## NAS sharding (opt-in: maximize a multi-HDD NAS)

A NAS with N HDDs delivers ~N× the throughput **only if** every spindle is busy
and none is over-subscribed (too many concurrent readers on one HDD = head
thrashing → throughput *collapses*). On a single machine you'd enforce that with a
semaphore per disk; across a mesh you can't — three Runners each capping themselves
would still pile 3× onto one spindle. **Only the Coordinator sees the whole fleet**, so
it enforces a *mesh-global per-spindle budget* — the one thing that structurally
belongs in Kiroshi, not the task.

It's **100% opt-in**. With no `[[storage.disk]]` config Kiroshi behaves exactly as
today (one read/write root, plain work-stealing). Declare your spindles once in
`kiroshi.local.toml` (gitignored):

```toml
[[storage.disk]]
kind  = "hdd"                          # hdd -> low concurrency; nvme/ssd -> high
read  = "//nas/disk1_direct/dataset"   # direct per-spindle share (fast sequential read)
write = "//nas/disk1/dataset"          # cached share (absorbs small-file write storms)
match = "shard_01..08"                 # which sub-jobs live on this spindle
# concurrency = 6   # optional; `kiroshi nas benchmark` finds the thrash knee
```

Then the Coordinator tags each sub-job to its disk, **round-robin interleaves leases across
spindles**, enforces the per-disk budget fleet-wide, and routes each sub-job to its
disk's direct-read / cached-write path. The dashboard grows a **Storage panel**
(per-spindle in-flight/budget + throughput sparkline) so you can watch every disk.

Tools to set it up and keep it healthy:

```bash
kiroshi nas assess //nas/dataset --pattern "*.npz" --topology  # readiness health check
kiroshi nas shard  //nas/dataset --disks 7                     # bin-pack into shard_NN/ dirs
kiroshi nas benchmark                                          # sweep concurrency, find the knee
kiroshi nas probe  nas --pattern "disk{1..7}"                  # scaffold a starter topology
```

`nas assess` gives a **READY / NEEDS-ATTENTION** verdict with actionable fixes
(format mismatch, data concentrated on one disk, shards matching no disk, skew).

## CLI

| Command | What |
|---|---|
| `kiroshi run m:f --items GLOB` | **Front door** — enumerate, run a local mesh, live progress bar. `--lan` invites other machines; `--enumerate` calls the task's `enumerate_sub-jobs(args)`; `--serve-task` offers the code to joiners. |
| `kiroshi join` | Add this machine to a running mesh as a Runner (`--service` to auto-start; consent-gated code fetch). |
| `kiroshi nas assess <root>` | **Throughput-readiness health check** (read-only): walks a dataset, checks file format (`--pattern`), per-disk distribution + concentration, shard-to-disk coverage, and balance — then gives a READY / NEEDS-ATTENTION verdict with actionable fixes. `nas benchmark` sweeps per-disk concurrency and recommends `concurrency`. `nas shard` bin-packs a flat dataset into `shard_NN/` dirs across N disks (+ emits config). `nas probe` discovers a NAS's shares and scaffolds a topology. |
| `kiroshi install` | One-command always-on: Coordinator as a boot-start service + tray autostart on login. (`uninstall` to remove.) |
| `kiroshi autostart on\|off\|status` | Manage the tray's login-autostart (`HKCU\Run`). |
| `kiroshi coordinator` | Run the coordinator + dashboard (auto-generates a mesh token). |
| `kiroshi runner --task m:f` | Run a worker node (`--coordinator auto` to discover). |
| `kiroshi seed --demo N` / `--jobs file.jsonl` | Enqueue sub-jobs (`--group`/`--label` to name a job). |
| `kiroshi status` | Print a `/status` snapshot. |
| `kiroshi requeue --state failed` | Return failed/stuck sub-jobs to pending. |
| `kiroshi doctor --task m:f` | Preflight checks (env, task import, NAS, coordinator). |
| `kiroshi ps` | List Kiroshi processes registered on this machine. |
| `kiroshi stop --role runner --all` | Ask local Runners/Coordinator to drain + exit. |
| `kiroshi tray` | System-tray status icon + menu (needs the `tray` extra). |

Common flags: `--token` (or `KIROSHI_TOKEN`) on every networked command;
`--no-auth` on `coordinator` for a trusted-LAN dev mesh (discouraged on public binds).

## Watching the fleet

The dashboard has three views, all themed like Kiroshi optics:

- **Overview** (`/`) — totals, aggregate progress bar, an at-field-style
  **throughput rate-curve over time** (hand-rolled SVG, no chart libs), and — when
  a NAS topology is configured — a **Storage panel** with per-spindle
  in-flight/budget and a throughput sparkline per disk.
- **Jobs** (`/ui/jobs`) — one row per **job** (sub-jobs grouped by `job_id`
  prefix): a pill progress bar, live counts, the **full launch command** that
  produced it, a **graph** button (per-job rate-over-time curve), and an
  optional **page** button to a task-supplied custom HTML view.
- **History** (`/ui/history`) — every job ever seeded with progress,
  start/end times, and launch command.

### Custom per-job pages
Drop a `job.html` into the Coordinator's `--pages-dir`; it's served at
`/p/job.html?job=<job>` and a **page** button appears per job. See
[`examples/job.html`](examples/job.html) for the (small) contract — it gets the
job id + token and can fetch `/jobs` and `/job/<id>` to render outputs.

## Observability, logging & emergency stop

- **Launch-command capture:** every Coordinator/Runner records its full command line
  (all flags); Runners report theirs to the Coordinator so it shows up per job.
- **Terminal logging:** each process tees stdout+stderr to a rotating log under
  the state dir (`%PROGRAMDATA%\Kiroshi\logs` / `~/.kiroshi/logs`).
- **Process registry:** each Coordinator/Runner writes a manifest (pid, role, launch
  command, log path, graceful-stop hook) so a watchdog can enumerate and stop
  Kiroshi processes. If **at-field** is installed, manifests are also advertised
  under `%PROGRAMDATA%\ATField\clients\kiroshi`. `kiroshi stop` (or the tray)
  requests a clean **drain** before any hard kill.
- **at-field awareness:** Runners watch at-field's `pause.sentinel` and back off
  (stop leasing) while the rig is being thermal/OOM-protected.

## Run as a service (survive reboot)

For an unattended mesh, wrap the Coordinator/Runners as auto-starting Windows services
via [NSSM](https://nssm.cc) (the at-field pattern). Kiroshi finds `nssm.exe` on
`PATH`, via `KIROSHI_NSSM`, or in its state dir; the logic lives in
`kiroshi service` and the `scripts\*.ps1` are just elevation shims.

```powershell
# Coordinator — LocalSystem is fine (local SQLite + a port, no NAS). Run elevated:
kiroshi service install --role coordinator --db C:\kiroshi\jobs.db --port 8787
nssm start kiroshi-coordinator

# Runner — with the SMB data plane, NAS access works under ANY account:
kiroshi service install --role runner --task mypkg.mytask:run --coordinator auto `
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
shared mesh token**; the Runner **mutually authenticates the Coordinator** (HMAC
challenge) before sending the token or running work; the Coordinator **binds loopback
by default**; the dashboard escapes untrusted input; the token is **redacted from
logs**; and the discovery beacon leaks no hostname or secret. The one thing
Kiroshi does *not* provide is transport encryption — on an untrusted network run
it over a private overlay (WireGuard/Tailscale) or TLS, and never port-forward it
to the internet. Read [SECURITY.md](SECURITY.md) for the full threat model
(additive vs inherent risk) and the frontier-deployment posture.

## Status

Early but functional (v0.0.1). See [PLAN.md](PLAN.md) for architecture and the
build milestones. MIT licensed.
