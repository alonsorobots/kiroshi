# AGENTS.md — Kiroshi capability map for LLM agents

> Task-indexed guide so an LLM can use Kiroshi fully without reading the source.
> Run `kiroshi capabilities --json` for the machine-readable version.

## Mental model

Kiroshi is a **work-stealing mesh** for embarrassingly parallel file-shaped jobs:

- A **Coordinator** is the coordinator: HTTP API + SQLite job store + web dashboard.
  It owns the sub-job queue, leases sub-jobs to runners, budgets I/O per disk/host via
  a storage topology, and emits **advisories** when something goes wrong.
- A **Runner** is a worker process bound to **one task** (`module:function`).
  It leases sub-jobs from the Coordinator, runs the task, reports results. Many runners
  across many machines share one Coordinator's queue.
- A **sub-job** = `{subjob_id, spec}`. `subjob_id` is the dedup key (re-seeding
  the same id is a no-op). `spec` is opaque to Kiroshi — the task interprets it.
  (Legacy JSONL using the old `job_id` key is still accepted by `kiroshi seed`.)
- **Auth:** every HTTP endpoint is token-gated. Pass `?token=<MESH_TOKEN>` on
  the URL or `Authorization: Bearer <MESH_TOKEN>` header. `/healthz` is the
  only exception. First thing an agent trips on if it forgets.

There is **no central scheduler guessing what to run** — you seed sub-jobs, runners
pull them. For *dependent* multi-stage work, declare a **pipeline** (below) so
the staggering is first-class instead of hand-rolled.

## Where things live (read before adding a feature)

**This is a monorepo. Everything Kiroshi lives here** — don't create or edit a
separate `kiroshi-mcp` / `kiroshi-cursor` repo (both are RETIRED; folding them
back in is why this note exists).

- **Coordinator / jobstore / leasing / decision-log** → `src/kiroshi/` (e.g.
  `coordinator.py`, `jobstore.py`). New Coordinator HTTP endpoints go here.
- **MCP server + tools** (`kiroshi mcp`, extra `[mcp]`) → `src/kiroshi/mcp_server.py`.
  When you add a Coordinator endpoint an agent should reach, add the thin tool here in
  the *same* commit, and document it in `capabilities.py` + this file.
- **Vendor-specific integrations** (Cursor bridge, future Slack/etc.) →
  `src/kiroshi/integrations/`, each behind its own optional extra
  (`[cursor]`, …) so headless nodes stay lean and core stays runtime-neutral.

## Subcommands (task → command → when)

| task | command | when to use / NOT |
|---|---|---|
| Run one task across the mesh | `kiroshi run <task> --enumerate --lan` | Quick one-shot; the "front door." Avoid for long jobs — seed into the persistent Coordinator as a group (below) for durability/resume. |
| Run the coordinator | *(already running — don't start another)* | **There is ONE Coordinator for the whole mesh** — the persistent `kiroshi-coordinator` service (port 8787, beacons, `--coordinator auto` finds it, topology-aware). You almost never start a Coordinator by hand. Every job is a GROUP inside this one Coordinator, not a new port. `--force-second-coordinator` is for deliberately isolated *meshes* (different NAS/topology), NEVER for "a parallel job" — that fragments the queue + disk budget and breaks `auto`/MCP. See "Job model" below. |
| Run a worker | `kiroshi runner --coordinator <url> --task <m:f> --workers N` | Bind ONE task per runner. `--capacity` caps how many sub-jobs it holds leased (prevents hoarding the whole disk budget). `--syspath` adds import roots for the task code. |
| Enqueue sub-jobs | `kiroshi seed --coordinator <url> --jobs sub-jobs.jsonl --group <slug> --label "<desc>"` | Dedups by `subjob_id`. Use `--group` to name a job (dashboard + `/metrics/export` filter on it). |
| Stage data between tiers | `kiroshi stage --from <src> --to <dst> [--pattern glob] [--coordinator <url>]` | Budgeted, resumable parallel copy (HDD→NVMe, remote fetch, prefetch). Shares the mesh I/O budget via ResourceClient; skips already-copied files. Local mode (no `--coordinator`) runs in-process like `kiroshi run`; mesh mode seeds sub-jobs for a `kiroshi.staging:run` runner. |
| Snapshot | `kiroshi status --coordinator <url>` | Counts only. For per-sub-job detail use HTTP `/subjobs` or `/metrics/export`. |
| Search jobs by regex | `kiroshi jobs --coordinator <url> --grep '<regex>' [--field subjob_id\|error] [--state failed] [--group <slug>]` | Server-side regex filter (no 100k-row download). Find specific sub-jobs on large jobs — e.g. `--grep 'PermissionError' --field error --state failed`. `--json` for machine output. |
| Return stuck/failed sub-jobs to pending | `kiroshi requeue --coordinator <url> --state failed` | Bumps attempts; respects max-retries. Use after fixing a systemic error so failed sub-jobs re-run. |
| **Multi-stage dependent work** | `kiroshi pipeline run spec.toml` | **USE THIS** instead of hand-rolling a cascade-seeder. Declares stages + typed edges (`each` / `quorum:k` / `all` / `artifact`). See `docs/PIPELINE.md` + `examples/pipeline.example.toml`. |
| Discover Kiroshi's own features | `kiroshi capabilities [--json]` | Task-indexed capability map. `--json` for LLM agents / MCP consumption. Same content as this doc, but machine-readable and version-accurate at runtime. |
| NAS layout | `kiroshi nas assess --root <dir>` / `benchmark` / `shard` | Assess reports shard balance; benchmark measures per-disk throughput; shard partitions a dataset across spindles. Run BEFORE seeding so the topology matches where data lives. Feed benchmark results to `kiroshi bench calibrate` to auto-set `concurrency`. |
| Measure true throughput | `kiroshi bench rate --dir <outputs>` | TRUE throughput from output-file mtimes (not wall-clock, which lies under concurrency). Use after a job to report honest end-to-end rate. |
| Calibrate concurrency | `kiroshi bench calibrate --samples '1=50,2=95,4=140,8=150,16=130'` | Turns throughput-vs-concurrency samples (from `nas benchmark` or observation) into a recommended per-disk `concurrency`. Bias: conservative (85% of peak), balanced (90%), aggressive (100%). Paste the result into `[[storage.disk]]`. |
| Launch a runner on another machine | `kiroshi remote join <host> --task <m:f>` | SSH-based, durable, interpreter-aware. Adds a worker box to the mesh. |
| Join this machine as a runner | `kiroshi join <coordinator-url>` | Lighter-weight remote launch. |
| Preflight | `kiroshi doctor` | Run on a new node: checks python, deps, disk, firewall, config. |
| Process list / stop | `kiroshi ps` / `kiroshi stop` | `ps` lists locally-registered Kiroshi processes; `stop` asks one to drain+exit. |
| Tray UI | `kiroshi tray` | System-tray status icon (needs the `tray` extra; runs windowless via `pythonw`). |
| Autostart | `kiroshi autostart` | Registers the tray to launch at login (currently `HKCU\Run`). |
| Firewall | `kiroshi firewall install` | Idempotent Windows Firewall rules for the Coordinator's inbound ports. |
| Windows service | `kiroshi service install` | NSSM-based Coordinator/Runner service (needs admin). |
| Package install helpers | `kiroshi install` / `kiroshi uninstall` | Wire up a machine (pip install + config scaffold) or tear it down. Idempotent. |

## HTTP endpoints an agent will actually use

Hit these directly from a task or orchestration script (token-gated via `?token=` or header):

- `GET /status` → `{total, pending, leased, done, failed, rate_per_s, eta_s, disk_inflight}` — fleet counts.
- `GET /metrics/export?job=<job>&state=done&limit=100000` → lightweight `{rows:[{subjob_id,metrics,state,job,disk}]}` for a whole job. **Use this** to find which items a stage has finished (the pipeline coordinator does).
- `POST /seed` (body: `{gigs:[{subjob_id,spec}], job, label}`) → enqueue; dedups by `subjob_id`. (`gigs` is a frozen wire-compat key; entries are sub-jobs.)
- `GET /runners` → registered runners + heartbeats (authoritative for "is my runner alive" — more reliable than `Get-CimInstance` cross-session).
- `GET /advisories` → structured warnings (`nas.throughput_collapse`, `nas.disk_saturation`, `sub-job.failure_spike`, …). Poll this to detect problems.
- `GET /storage` → the loaded topology (disks, roots, budgets).
- `GET /subjobs?job=<j>&state=done&limit=2000` → dashboard-shaped sub-job rows. **Supports `subjob_id_re` + `error_re`** regex params for server-side filtering.
- `POST /requeue` (body: `{state}`) → return failed/leased sub-jobs to pending.
- `GET /task/meta?task=<module:fn>` → task's declared metadata (docstring, expected spec keys). Introspect BEFORE seeding to know what to put in `spec`.
- `GET /task/source?task=<module:fn>` → the task's source. Useful when an agent needs to reason about behavior without the local checkout.
- `GET /healthz` → liveness (no auth).

## Storage topology (`kiroshi.local.toml`)

Per-disk routing + concurrency budgets. Each `[[storage.disk]]` has `read` /
`write` roots, a `match` pattern tested against the sub-job `subjob_id`, and a
`concurrency` cap. The Coordinator leases at most `sum(concurrency)` sub-jobs and never
over-saturates one spindle. **A sub-job whose `subjob_id` matches no disk gets `disk=None`
(uncapped)** — usually a bug; either give it a `match` or set explicit
`read_root`/`write_root` in the sub-job `spec` (which bypasses topology routing
entirely). Local operator configs use the `.local.` infix (`*.local.toml`) and
are git-ignored.

### Fast I/O by default → the fail-closed gate (you can't accidentally run slow)

Job creation (`kiroshi seed`, `kiroshi run`, MCP `seed_gigs`) is **fail-closed**
on I/O: if a job's declared paths are on a genuine slow-path trade-off, Kiroshi
**refuses to create it** and re-transmits the fast alternative. It never
silently rewrites your paths — you fix the spec (so it always matches reality),
or you acknowledge the trade-off with a specific token. Blocking reasons and
their tokens:

| Reason | Token | Fix (preferred) |
|--------|-------|-----------------|
| Reading a cached/FUSE share when a direct spindle share exists | `no_direct_share` | point `read_root` at the disk's `read`/`direct_path` |
| Writing to a parity-protected array (RMW bottleneck) | `parity_write` | write to the NVMe/SSD cache tier |
| Writing to a RAW/direct disk path (`/mnt/diskN`), bypassing the pooled share — dup/shadow data-loss risk on Unraid/mergerfs | `direct_disk_write` | write to the disk's cached/user share |
| UNC path with no SMB creds (redirector fallback) | `no_smb_creds` | set `KIROSHI_NAS_USER`/`PASS` |
| NAS path matching no `[[storage.disk]]` rule (unroutable) | `unclassified_nas` | add a topology `match` rule |

Acknowledge with `--io-ack <token>` (CLI, repeatable) or `io_ack=["<token>"]`
(MCP) — a deliberate, recorded choice. The gate passes silently for local paths,
demo jobs, an already-fast path, or when no topology is configured (it can't
judge, so it won't block). Emergency override for a false positive:
`KIROSHI_IO_GATE=0`. Disk **budget** (per-spindle concurrency) is applied
automatically from the declared folders even without a shard token — Kiroshi
fills blanks and enforces, but never overwrites your intent.

### `advise_io` — the same classifier, as guidance

You don't need to memorize the storage physics. `advise_io` (MCP tool; also
printed by `kiroshi doctor`, `seed`, `run`, and `remote probe`) classifies a
job's `read_root`/`write_root` (plus a sample `src_path`/`dst_path`) against the
topology and tells you, with **no benchmarking** (these are static facts):
- **Input on NVMe** → already optimal, push concurrency high.
- **Input on an HDD** → shard across spindles and read the **direct per-spindle
  share** (`disk.read` / `direct_path`), not the FUSE `/mnt/user` pool, which
  serializes reads across disks.
- **Output on a parity array** → every write is a read-modify-write through the
  parity spindle (a fleet-global bottleneck); keep write concurrency modest and
  prefer an NVMe cache tier if configured.
- **UNC path, no SMB creds** → Kiroshi falls back to the Windows redirector
  (slow, and dead from a service/SSH logon); set `KIROSHI_NAS_USER/PASS`.

The dual-path routing this recommends is applied automatically at lease time
(`inject_roots`) once a disk's `match` rule hits, so the main job of `advise_io`
is to catch the cases where a job's own `read_root`/`write_root` or an
un-matched shard would put you on a slow path.

## Multi-stage work → use `kiroshi pipeline`

Do NOT write an external "cascade seeder" that polls a DB and seeds the next
stage. Declare it:

```toml
[[edges]]
from = "reduce"
to   = "encode"
kind = "each"          # per-item: encode X unlocks when reduce X is done

[[edges]]
from = "reduce"
to   = "codebook"
kind = "quorum"
k    = 4000            # barrier: build the global codebook once >= 4000 done

[[edges]]
from = "codebook"
to   = "encode"
kind = "artifact"      # gate: encode stays blocked until the codebook file exists
```

`kiroshi pipeline validate spec.toml` prints the DAG with no I/O. See
`docs/PIPELINE.md` for when one-job-vs-many is correct (TL;DR: keep stages
separate when outputs are persisted deliverables or there's a map→reduce→map
barrier like a codebook).

## Task authoring cheat sheet

A task is a **module-level** `def run(spec: dict) -> dict` returning
`{"status": "ok"|"skipped", "metrics": {...}}`. Raise → sub-job fails and Kiroshi
retries. Runners import the task; the Coordinator never does.

**Optional but powerful — the `enumerate_gigs` hook** (the hook name is a
frozen ABI term; the *concept* is "enumerate sub-jobs"):

```python
# in your task module
def enumerate_gigs(args: dict):
    """Yield {subjob_id, spec} — 'kiroshi run <task> --enumerate ...' calls this
    so a task can fan out its own sub-jobs (one source → many outputs), no
    external sub-jobs.jsonl file needed."""
    for path in kfs.walk(args["read_root"]):
        yield {"subjob_id": path, "spec": {"src": path, "dst": ...}}

def run(spec: dict) -> dict:
    ...
```

**KFS — the FS abstraction tasks use** (`from kiroshi import kfs`):

- `kfs.exists(dst)`  → skip-if-output-exists idempotency
- `kfs.open(path, "rb")`  → uniform local + UNC + mapped-drive
- `kfs.walk(root)`  → streaming (huge SMB trees) with reconnect
- `kfs.atomic_write(dst) as fh: fh.write(...)`  → crash-safe writes
- `kfs.makedirs(dir)`  → make parents (atomic_write does NOT auto-mkdir)
- `kfs.backend(path)` / `kfs.smb_diagnostics(server)` for debugging

**SMB creds for scheduled tasks / services** (no interactive session, no
mapped drives) — set these in the runner's environment:

- `KIROSHI_SMB_USER`, `KIROSHI_SMB_PASS`  → explicit creds
- `KIROSHI_SMB_AUTH`  → `ntlm` (default) / `negotiate` / `kerberos`
- `KIROSHI_SMB_ENCRYPT`  → `1` to force SMB3 payload encryption

**Path helpers** (`from kiroshi import paths`) — for resolving sub-job I/O:

- `paths.gig_read_root(spec)` / `paths.gig_write_root(spec)`  → honors
  `spec["read_root"]` / `write_root` overrides, else falls back to topology
- `paths.confined_join(root, rel)`  → safe join that refuses `..` escapes

**Resource governor — cross-mesh coordination for shared budgets**
(`from kiroshi.resource import ResourceClient`):

```python
rc = ResourceClient(coordinator_url, token)
with rc.acquire(disk="disk3", mode="write"):   # blocks until parity slot free
    ...
with rc.acquire(budget="hf_download"):         # named budget
    ...
```

Fail-open if the Coordinator is unreachable (the task keeps working). Use for
resources the built-in per-disk topology doesn't already cover.

**Runner "hidden gems" for long-running / memory-leaky tasks:**

- `--max-tasks-per-child N`  → recycle a worker process after N tasks
  (releases numpy/torch memory a task didn't free)
- `--gc-between-tasks`  → force `gc.collect()` between tasks
- `--gig-timeout SECONDS`  → hard-kill a sub-job that stalls
- `--heartbeat SECONDS`  → lease-renewal cadence (default OK for most)
- `--retries N`  → per-sub-job retry budget (default 3)

**Per-sub-job resource profiling** (automatic, needs `pip install kiroshi[profiler]`):

- Each completed sub-job carries a `metrics.proc` summary: `cpu_pct_mean`,
  `cpu_pct_peak`, `rss_peak_mb`, `read_mb`, `write_mb`, `wall_s`, `samples`.
- Visible in `/subjob/{id}` and `/subjobs` — answers *"what did this sub-job actually use?"*
- Disable with `KIROSHI_PROFILER=0` env var. Soft dep — works without psutil
  (just no proc summary).

## Advisories — the specific codes to watch for

`GET /advisories` returns `{active: [{code, disk, severity, detail, ...}]}`.
The codes an agent will actually see:

| code | trigger | usually means |
|---|---|---|
| `nas.thrash` | per-disk read+write both saturated | reduce concurrency for that disk |
| `nas.disk_saturation` | one disk pinned at capacity | route around it, or lower per-disk `concurrency` |
| `nas.throughput_collapse` | fleet throughput dropped ≥95% | a share is disconnected; check `/runners` and `kfs.smb_diagnostics` |
| `nas.parity_write_pressure` | writes queued behind parity | HDD array; stage hot data to NVMe |
| `nas.single_spindle` | I/O concentrated on 1 disk while others idle | spread reads — check topology match patterns or shard plan |
| `nas.latency_bound` | throughput low but no resource at ceiling | round-trip latency (SMB metadata) or lock contention — batch ops or stage to NVMe |
| `disk.at_ceiling` | a disk at its benchmarked peak MB/s | this is the disk, not the code — stage to NVMe or spread |
| `host.cpu_bound` | CPU ≥ 90% sustained | add workers/nodes or optimize the task |
| `host.mem_pressure` | MEM ≥ 90% | reduce per-worker memory or add RAM |
| `sub-job.failure_spike` | failed-rate spike | a systemic error; check `recent_errors` in `/status` |

Severities: `SEVERITY_INFO`, `SEVERITY_WARN`, `SEVERITY_CRIT`. Every advisory
has a stable `fingerprint` so a dashboard can dedup across polls.

## Coordination decision log — debugging underutilization

When a node is idle or throughput is lower than expected, the **decision log**
tells you *why* the coordinator gave each host the sub-jobs it did. Three endpoints
(also available as MCP tools):

1. **`GET /decisions/summary`** (MCP: `scheduling_summary`) — the first call.
   Shows per-host grant ratio over a window and which hosts are **starved**
   (requested > 0 but grant_ratio ≈ 0). Each host's `main_reason` is the
   dominant binding constraint.

2. **`GET /lease/decisions`** (MCP: `lease_decisions`) — raw per-lease-call
   records: requested vs granted, `binding_reason`, fair-share ceiling,
   per-disk budget snapshot. Filter by `host` or `reason`.

3. **`GET /subjob/trace?subjob_id=...`** (MCP: `job_trace`) — one sub-job's full
   coordination timeline: SEEDED → LEASED → COMPLETED/FAILED/EXPIRED.

The `binding_reason` enum disambiguates the cause at a glance:

| reason | meaning | action |
|---|---|---|
| `GRANTED_FULL` | host got everything it asked for | healthy — no action |
| `NO_PENDING` | nothing pending in the queue | job is done or not seeded |
| `FAIR_SHARE_CAP` | host already holds its proportional slice | expected with fair-share on; not a bug |
| `DISK_BUDGET_FULL` | all candidate disks at their in-flight cap | add disks, raise `concurrency`, or stage to NVMe |
| `CAPACITY_ZERO` | runner asked for 0 (spinning up/draining) | transient — check runner health |

The `/status` JSON also carries a `scheduling` block (same data as
`/decisions/summary` with a 120s window) for one-glance dashboard visibility.

## Job model — ONE Coordinator, jobs are groups

The mesh has **one** long-lived Coordinator: the `kiroshi-coordinator` Windows service on
Chronos (port 8787, `C:\ProgramData\Kiroshi\jobs.db`, beacons, topology-aware
via `C:\ProgramData\Kiroshi\kiroshi.toml`). It IS "Kiroshi." Everything —
`--coordinator auto`, Cursor/Kilo MCP, the dashboard — points at this one.

A "job" (reduce30, slerp, dvq, …) is **not a new Coordinator/port/db**. It's a
`--group` inside the persistent Coordinator. To launch one:

```bash
# 1) seed the sub-jobs into the persistent Coordinator as a named group
kiroshi seed --coordinator auto --jobs sub-jobs.jsonl --group reduce30 --label "88-DoF reduce"
# 2) start runners anywhere, pointed at the same one Coordinator
kiroshi runner --coordinator auto --task scripts.mesh.tasks.reduce_pose:run --workers N
# 3) observe everything (all groups) in one place
kiroshi status --coordinator auto           # or MCP status / the dashboard
```

Groups give per-job dashboards, `/metrics/export?job=…`, `--group` filters
on `jobs`/`requeue`, and pipeline edges — all the isolation a job needs,
with none of the fragmentation. **Do NOT** spin `kiroshi coordinator … --port 88xx
--force-second-coordinator --no-beacon` per job: that was the old anti-pattern
(it made `auto`/MCP resolve to an empty Coordinator and split the disk budget). The
storage topology lives on the persistent Coordinator once, so jobs inherit the
`cache_nvme` budget automatically.

> Migration note: any pre-existing per-job Coordinator (e.g. reduce30 on 8800)
> can keep running until it drains — don't migrate a live queue. The *next*
> job seeds into 8787 with `--group`, and the old port retires itself.

## Gotchas an agent MUST know

1. **Split-brain guard.** A Coordinator refuses to start if another is discoverable
   on the LAN. This is a FEATURE: one mesh = one Coordinator. `--force-second-coordinator`
   is for deliberately isolated *meshes* (a different NAS / disjoint topology),
   NOT for jobs — running a second Coordinator "for a parallel job" splits
   the queue + per-disk budget in two (both saturate the shared NAS) and makes
   the job invisible to `--coordinator auto` / MCP. Jobs are GROUPS in the
   one persistent Coordinator (see "Job model"). If you hit the guard, you almost
   certainly want `--group`, not a second Coordinator.
2. **Cache-only vs array SMB shares (Unraid).** A share configured
   `shareUseCache=only` may STILL route writes to an HDD array if the share
   folder already physically exists on a parity disk. **Prefer a direct
   `/mnt/cache/...` SMB share** (one that points straight at the cache pool)
   that bypasses the shfs FUSE layer, so writes are guaranteed to land on
   NVMe. Verify with `stat`/`ls` on
   the NAS, not just the SMB client.
3. **NVMe vs HDD read cost.** Reading many small files over SMB from an HDD
   array is metadata-op-bound (1–4 MB/s/stream). Stage hot source data onto
   NVMe first (a one-time copy), then point the topology `read` roots at the
   NVMe share. Building codebooks? Run the builder **on the NAS host** (raw
   `/mnt/cache` reads) — ~100× faster than pulling over SMB.
4. **Idempotency + resume.** Tasks should skip if the output already exists
   (`kfs.exists(dst)`). Combined with the persistent Coordinator `.db`, this makes
   kill/restart free — the runner re-leases pending sub-jobs and skips done ones.
5. **One task per runner.** A runner is bound to a single `--task`; for
   multiple stages use multiple runners (and `kiroshi pipeline` to coordinate).
6. **`Get-CimInstance` cmdline is often null cross-session** on Windows — don't
   trust process filtering to find task-launched runners; use `/runners`.
7. **Visible console windows.** Launching `.cmd` via `schtasks /it` or WMI pops
   a blank window. Use a hidden VBS launcher (`wscript _hidden.vbs the.cmd`) or
   `pythonw.exe` (the tray already does this).
8. **pytest may be broken** in a given env (`pluggy` missing). Test files here
   ship a zero-dependency `if __name__ == "__main__"` runner — invoke
   `python tests/test_X.py` and read the `N/N passed` line.
