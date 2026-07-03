# AGENTS.md — Kiroshi capability map for LLM agents

> Task-indexed guide so an LLM can use Kiroshi fully without reading the source.
> Run `kiroshi capabilities --json` for the machine-readable version.

## Mental model

Kiroshi is a **work-stealing mesh** for embarrassingly parallel file-shaped jobs:

- A **Fixer** is the coordinator: HTTP API + SQLite job store + web dashboard.
  It owns the gig queue, leases gigs to runners, budgets I/O per disk/host via
  a storage topology, and emits **advisories** when something goes wrong.
- A **Runner** is a worker process bound to **one task** (`module:function`).
  It leases gigs from the Fixer, runs the task, reports results. Many runners
  across many machines share one Fixer's queue.
- A **gig** = `{job_id, spec}`. `job_id` is the dedup key (re-seeding the same
  id is a no-op). `spec` is opaque to Kiroshi — the task interprets it.
- **Auth:** every HTTP endpoint is token-gated. Pass `?token=<MESH_TOKEN>` on
  the URL or `Authorization: Bearer <MESH_TOKEN>` header. `/healthz` is the
  only exception. First thing an agent trips on if it forgets.

There is **no central scheduler guessing what to run** — you seed gigs, runners
pull them. For *dependent* multi-stage work, declare a **pipeline** (below) so
the staggering is first-class instead of hand-rolled.

## Subcommands (task → command → when)

| task | command | when to use / NOT |
|---|---|---|
| Run one task across the mesh | `kiroshi run <task> --enumerate --lan` | Quick one-shot; the "front door." Avoid for long campaigns — use fixer+runner+seed for durability/resume. |
| Run the coordinator | `kiroshi fixer --db X.db --port 8800` | One per mesh. State persists in the `.db`; kill/restart is safe and resumes. **Gotcha:** a split-brain guard refuses to start if another Fixer is discoverable — use `--force-second-fixer` only when deliberately running a second one (e.g. a parallel campaign on a new port). |
| Run a worker | `kiroshi runner --fixer <url> --task <m:f> --workers N` | Bind ONE task per runner. `--capacity` caps how many gigs it holds leased (prevents hoarding the whole disk budget). `--syspath` adds import roots for the task code. |
| Enqueue gigs | `kiroshi seed --fixer <url> --jobs gigs.jsonl --group <slug> --label "<desc>"` | Dedups by `job_id`. Use `--group` to name a campaign (dashboard + `/metrics/export` filter on it). |
| Stage data between tiers | `kiroshi stage --from <src> --to <dst> [--pattern glob] [--fixer <url>]` | Budgeted, resumable parallel copy (HDD→NVMe, remote fetch, prefetch). Shares the mesh I/O budget via ResourceClient; skips already-copied files. Local mode (no `--fixer`) runs in-process like `kiroshi run`; mesh mode seeds gigs for a `kiroshi.staging:run` runner. |
| Snapshot | `kiroshi status --fixer <url>` | Counts only. For per-gig detail use HTTP `/jobs` or `/metrics/export`. |
| Search jobs by regex | `kiroshi jobs --fixer <url> --grep '<regex>' [--field job_id\|error] [--state failed] [--group <slug>]` | Server-side regex filter (no 100k-row download). Find specific gigs on large campaigns — e.g. `--grep 'PermissionError' --field error --state failed`. `--json` for machine output. |
| Return stuck/failed gigs to pending | `kiroshi requeue --fixer <url> --state failed` | Bumps attempts; respects max-retries. Use after fixing a systemic error so failed gigs re-run. |
| **Multi-stage dependent work** | `kiroshi pipeline run spec.toml` | **USE THIS** instead of hand-rolling a cascade-seeder. Declares stages + typed edges (`each` / `quorum:k` / `all` / `artifact`). See `docs/PIPELINE.md` + `examples/pipeline.example.toml`. |
| Discover Kiroshi's own features | `kiroshi capabilities [--json]` | Task-indexed capability map. `--json` for LLM agents / MCP consumption. Same content as this doc, but machine-readable and version-accurate at runtime. |
| NAS layout | `kiroshi nas assess --root <dir>` / `benchmark` / `shard` | Assess reports shard balance; benchmark measures per-disk throughput; shard partitions a dataset across spindles. Run BEFORE seeding so the topology matches where data lives. Feed benchmark results to `kiroshi bench calibrate` to auto-set `concurrency`. |
| Measure true throughput | `kiroshi bench rate --dir <outputs>` | TRUE throughput from output-file mtimes (not wall-clock, which lies under concurrency). Use after a campaign to report honest end-to-end rate. |
| Calibrate concurrency | `kiroshi bench calibrate --samples '1=50,2=95,4=140,8=150,16=130'` | Turns throughput-vs-concurrency samples (from `nas benchmark` or observation) into a recommended per-disk `concurrency`. Bias: conservative (85% of peak), balanced (90%), aggressive (100%). Paste the result into `[[storage.disk]]`. |
| Launch a runner on another machine | `kiroshi remote join <host> --task <m:f>` | SSH-based, durable, interpreter-aware. Adds a worker box to the mesh. |
| Join this machine as a runner | `kiroshi join <fixer-url>` | Lighter-weight remote launch. |
| Preflight | `kiroshi doctor` | Run on a new node: checks python, deps, disk, firewall, config. |
| Process list / stop | `kiroshi ps` / `kiroshi stop` | `ps` lists locally-registered Kiroshi processes; `stop` asks one to drain+exit. |
| Tray UI | `kiroshi tray` | System-tray status icon (needs the `tray` extra; runs windowless via `pythonw`). |
| Autostart | `kiroshi autostart` | Registers the tray to launch at login (currently `HKCU\Run`). |
| Firewall | `kiroshi firewall install` | Idempotent Windows Firewall rules for the Fixer's inbound ports. |
| Windows service | `kiroshi service install` | NSSM-based Fixer/Runner service (needs admin). |
| Package install helpers | `kiroshi install` / `kiroshi uninstall` | Wire up a machine (pip install + config scaffold) or tear it down. Idempotent. |

## HTTP endpoints an agent will actually use

Hit these directly from a task or orchestration script (token-gated via `?token=` or header):

- `GET /status` → `{total, pending, leased, done, failed, rate_per_s, eta_s, disk_inflight}` — fleet counts.
- `GET /metrics/export?grp=<group>&state=done&limit=100000` → lightweight `{rows:[{job_id,metrics,state,grp,disk}]}` for a whole campaign. **Use this** to find which items a stage has finished (the pipeline coordinator does).
- `POST /seed` (body: `{gigs:[{job_id,spec}], group, label}`) → enqueue; dedups by `job_id`.
- `GET /runners` → registered runners + heartbeats (authoritative for "is my runner alive" — more reliable than `Get-CimInstance` cross-session).
- `GET /advisories` → structured warnings (`nas.throughput_collapse`, `nas.disk_saturation`, `gig.failure_spike`, …). Poll this to detect problems.
- `GET /storage` → the loaded topology (disks, roots, budgets).
- `GET /jobs?grp=<g>&state=done&limit=2000` → dashboard-shaped job rows. **Now supports `job_id_re` + `error_re`** regex params for server-side filtering.
- `POST /requeue` (body: `{state}`) → return failed/leased gigs to pending.
- `GET /task/meta?task=<module:fn>` → task's declared metadata (docstring, expected spec keys). Introspect BEFORE seeding to know what to put in `spec`.
- `GET /task/source?task=<module:fn>` → the task's source. Useful when an agent needs to reason about behavior without the local checkout.
- `GET /healthz` → liveness (no auth).

## Storage topology (`kiroshi.local.toml`)

Per-disk routing + concurrency budgets. Each `[[storage.disk]]` has `read` /
`write` roots, a `match` pattern tested against the gig `job_id`, and a
`concurrency` cap. The Fixer leases at most `sum(concurrency)` gigs and never
over-saturates one spindle. **A gig whose `job_id` matches no disk gets `disk=None`
(uncapped)** — usually a bug; either give it a `match` or set explicit
`read_root`/`write_root` in the gig `spec` (which bypasses topology routing
entirely). Local operator configs use the `.local.` infix (`*.local.toml`) and
are git-ignored.

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
`{"status": "ok"|"skipped", "metrics": {...}}`. Raise → gig fails and Kiroshi
retries. Runners import the task; the Fixer never does.

**Optional but powerful — the `enumerate_gigs` hook:**

```python
# in your task module
def enumerate_gigs(args: dict):
    """Yield {job_id, spec} — 'kiroshi run <task> --enumerate ...' calls this
    so a task can fan out its own gigs (one source → many outputs), no
    external gigs.jsonl file needed."""
    for path in kfs.walk(args["read_root"]):
        yield {"job_id": path, "spec": {"src": path, "dst": ...}}

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

**Path helpers** (`from kiroshi import paths`) — for resolving gig I/O:

- `paths.gig_read_root(spec)` / `paths.gig_write_root(spec)`  → honors
  `spec["read_root"]` / `write_root` overrides, else falls back to topology
- `paths.confined_join(root, rel)`  → safe join that refuses `..` escapes

**Resource governor — cross-mesh coordination for shared budgets**
(`from kiroshi.resource import ResourceClient`):

```python
rc = ResourceClient(fixer_url, token)
with rc.acquire(disk="disk3", mode="write"):   # blocks until parity slot free
    ...
with rc.acquire(budget="hf_download"):         # named budget
    ...
```

Fail-open if the Fixer is unreachable (the task keeps working). Use for
resources the built-in per-disk topology doesn't already cover.

**Runner "hidden gems" for long-running / memory-leaky tasks:**

- `--max-tasks-per-child N`  → recycle a worker process after N tasks
  (releases numpy/torch memory a task didn't free)
- `--gc-between-tasks`  → force `gc.collect()` between tasks
- `--gig-timeout SECONDS`  → hard-kill a gig that stalls
- `--heartbeat SECONDS`  → lease-renewal cadence (default OK for most)
- `--retries N`  → per-gig retry budget (default 3)

## Advisories — the specific codes to watch for

`GET /advisories` returns `{active: [{code, disk, severity, detail, ...}]}`.
The codes an agent will actually see:

| code | trigger | usually means |
|---|---|---|
| `nas.thrash` | per-disk read+write both saturated | reduce concurrency for that disk |
| `nas.disk_saturation` | one disk pinned at capacity | route around it, or lower per-disk `concurrency` |
| `nas.throughput_collapse` | fleet throughput dropped ≥95% | a share is disconnected; check `/runners` and `kfs.smb_diagnostics` |
| `nas.parity_write_pressure` | writes queued behind parity | HDD array; stage hot data to NVMe |
| `gig.failure_spike` | failed-rate spike | a systemic error; check `recent_errors` in `/status` |

Severities: `SEVERITY_INFO`, `SEVERITY_WARN`, `SEVERITY_CRIT`. Every advisory
has a stable `fingerprint` so a dashboard can dedup across polls.

## Gotchas an agent MUST know

1. **Split-brain guard.** A Fixer refuses to start if another is discoverable
   on the LAN. Use `--force-second-fixer` ONLY when deliberately running a
   second campaign on a different port; otherwise find and stop the existing one.
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
   (`kfs.exists(dst)`). Combined with the persistent Fixer `.db`, this makes
   kill/restart free — the runner re-leases pending gigs and skips done ones.
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
