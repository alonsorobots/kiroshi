# Kiroshi — Plan & Architecture

> A zero-broker, work-stealing **mesh runner for Windows** (and friends). Point it
> at a Python function, run a coordinator on one box, join from the others, and
> watch the whole fleet chew through an embarrassingly-parallel workload — live.
>
> **Public, open-source repo.** No machine-specific paths, IPs, hostnames, or
> personal info in committed files. All such pointers live in the gitignored
> `LOCAL_CONTEXT.md` (read that first if you're picking this up in a new chat).

---

## 1. Lore & naming

Night City metaphor, used consistently across the API/CLI/dashboard:

| Concept | Kiroshi term | Lore |
|---|---|---|
| Coordinator (hands out work) | **Fixer** | A fixer dispatches gigs to runners. |
| Worker node (pulls + executes) | **Runner** | A netrunner takes the gig and does the job. |
| Unit of work | **Gig** (a.k.a. job) | The contract being fulfilled. |
| Live dashboard | **Kiroshi** (the optics) | Kiroshi Optics overlay everything you see — observe the whole mesh. |

Internal class names stay descriptive (`Coordinator`, `Worker`, `Job`) with themed
CLI verbs (`kiroshi fixer`, `kiroshi runner`) so it's fun *and* legible.

---

## 2. Why Kiroshi exists (the gap)

There is **no good "I have 2–4 Windows rigs + a NAS, run my Python function across
all of them, resumable, zero-broker" tool**:

- **Ray** — no Windows multi-node support.
- **Celery / Dramatiq** — require a broker (Redis/RabbitMQ); painful on Windows.
- **Dask** — central scheduler is a SPOF; heavier than needed for batch jobs.
- **Hand-rolled SSH fan-out** — no resume, no self-heal, no live view.

Kiroshi is the natural sibling to **at-field** (a Windows GPU/thermal watchdog by
the same author): at-field keeps each rig **alive**; Kiroshi keeps each rig
**busy**. "Windows AI homelab" tooling. A Runner can later learn to respect
at-field's pause state.

**Design north star:** `pip install kiroshi`, decorate/point at a function,
`kiroshi fixer` on one box, `kiroshi runner --fixer <host>` on the others. Output
existence is the source of truth, so everything is resumable and idempotent.

---

## 3. Architecture — two layers

Kiroshi cleanly separates **cross-node coordination** (the new part) from
**within-node execution** (battle-tested patterns; see §5).

```
                   ┌─────────────────────────────────────────┐
                   │  FIXER (coordinator)  — one box           │
                   │  FastAPI + local SQLite (WAL)             │
                   │  /seed /lease /complete /heartbeat /status│
                   │  lease-TTL reaper (self-heal)             │
                   │  GET /  → live Kiroshi dashboard          │
                   └───────────────┬───────────────────────────┘
                                   │  HTTP (batch lease/complete/heartbeat)
            ┌──────────────────────┼──────────────────────┐
            ▼                      ▼                      ▼
   ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
   │ RUNNER (host A) │   │ RUNNER (host B) │   │ RUNNER (host C) │
   │ pull loop       │   │ pull loop       │   │ pull loop       │
   │ local ProcessPool   │ local ProcessPool   │ local ProcessPool
   │ bounded window  │   │ bounded window  │   │ bounded window  │
   │ per-item retry  │   │ per-item retry  │   │ per-item retry  │
   │ atomic writes ──┼───┼─────► NAS ◄─────┼───┼── atomic writes │
   └─────────────────┘   └─────────────────┘   └─────────────────┘
```

- **Cross-node** = work-stealing pull. A Runner leases a **batch** of gigs
  (e.g. 200), so HTTP + SQLite overhead is amortized to ~0. No node is told
  "you do shard X"; fast nodes simply pull more. This *is* "least-busy"
  scheduling for free.
- **Within-node** = each Runner is essentially a local CPU scheduler:
  `ProcessPoolExecutor` (bypass the GIL), bounded submission window (avoid the
  Windows pipe deadlock), per-item retry with backoff, atomic async writes,
  and output-exists skip.

### Why not SQLite on the NAS / shared FS?
SMB/network SQLite locking is unreliable across machines. The job store lives
**locally on the Fixer**; coordination is over HTTP. Workers never touch the DB.

---

## 4. Core concepts & gig lifecycle

A **Gig** has a stable `job_id` (deterministic — e.g. a clip's relative path or a
hash of its spec) and an opaque `spec` (JSON the task function understands).

```
            seed
             │
             ▼
        ┌─────────┐   lease (batch)   ┌─────────┐
        │ PENDING ├──────────────────►│ LEASED  │
        └─────────┘                   └────┬────┘
             ▲                              │
   reaper:   │  lease TTL expires           │ complete
   re-queue  └──────────────────────────────┤
             │                              ▼
             │ failed & retries<max    ┌─────────┐
             └─────────────────────────│  DONE   │
                                       │ /FAILED │
                                       └─────────┘
```

- **Idempotent seed** — re-seeding the same `job_id` is a no-op.
- **Output-exists = truth** — before executing, a Runner (or the Fixer at seed
  time) can mark a gig DONE if its output already exists on the NAS. Free resume.
- **Lease TTL + reaper** — a Runner that dies mid-batch has its leases reclaimed
  and re-queued automatically (self-heal). Heartbeats extend the TTL.
- **Bounded retries** — failed gigs re-queue up to `max_retries`, then land in
  FAILED with the error recorded (reported, never silently dropped).

### Coordinator endpoints
All endpoints except the open HTML shells (`/`, `/ui/*`, `/p/*`, `/healthz`)
require the mesh token (see §11).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/seed` | Enqueue gigs idempotently (list of `{job_id, spec}`). |
| `POST` | `/lease` | `{runner_id, host, capacity}` → up to `capacity` pending gigs, marked leased with a `lease_id` + TTL. |
| `POST` | `/complete` | `{lease_id, results:[{job_id, status, error, metrics}]}` → DONE/FAILED, re-queue under retry budget. |
| `POST` | `/heartbeat` | `{runner_id, lease_id, stats}` → extend TTL, record per-host liveness/throughput. |
| `POST` | `/register` | `{runner_id, host, launch_command, task, workers, pid, log_path}` → record a Runner + its full launch command. |
| `POST` | `/requeue` | Return failed/stuck gigs to pending. |
| `GET`  | `/status` | JSON snapshot (queue depth, per-host throughput, ETA, errors). |
| `GET`  | `/runners` | Registered Runners (launch command, pid, liveness). |
| `GET`  | `/groups` | Per-campaign rollup ("jobs"): counts, timing, launch command(s). |
| `GET`  | `/jobs` · `/history` | Gig rows (live / all) with launch command attached. |
| `GET`  | `/job/{id}` | One gig's full detail (spec, metrics, error, timing). |
| `GET`  | `/metrics/history` | Throughput + per-group time-series (the rate curves). |
| `GET`  | `/healthz` | Unauthenticated liveness (`{ok, auth}`). |
| `GET`  | `/` · `/ui/jobs` · `/ui/history` · `/ui/job` | Live Kiroshi dashboard + console pages. |

---

## 5. Inherited patterns (ported clean, **not** imported)

These are distilled from a sibling single-node pipeline repo (the "reference repo";
see `LOCAL_CONTEXT.md` for its location and exact file/line pointers). **Kiroshi
re-implements them from scratch with zero dependency on that repo** — one day that
repo might depend on Kiroshi, not the other way around.

| # | Pattern | The hard-won insight | Where it lands in Kiroshi |
|---|---|---|---|
| 1 | **ProcessPool, never ThreadPool** for CPU work | GIL → ~6% CPU on a 32-core box vs true multi-core. | `worker.py` local executor. |
| 2 | **Bounded submission window** (`max_pending = workers*2`) | Submitting 100k+ futures at once **deadlocks on Windows** (pipe buffer fills, main thread blocks on `WaitForMultipleObjects`). Sliding-window refill. | `worker.py`. |
| 3 | **TRUE throughput from output-file mtimes** | `(last_mtime − first_mtime) / items`. Wall-clock & per-item timers lie under concurrency. THE canonical metric. | `bench.py`, surfaced on dashboard. |
| 4 | **Storage dual-path** (read root vs write root) | On NAS: read via the per-disk "direct" share (bypass FUSE → 30–40× metadata, 6–9× reads); write via the cached user share. | `paths.py` / `config.py`. |
| 5 | **Output-existence = resume truth** + done-markers | Re-runs skip finished work for free. | jobstore seed-time + worker pre-check. |
| 6 | **Per-item retry w/ exponential backoff** | Transient SMB/network blips retried, not fatal; failures reported. | `worker.py` + `/complete`. |
| 7 | **Atomic writes** (`.tmp` → `os.replace`, fsync) | Never leave partial files on crash/power-loss. | `atomic.py`. |
| 8 | **Async background writer** (queue + backpressure) | Don't block compute on disk I/O. | `worker.py` (optional writer thread). |
| 9 | **Per-host × per-task config**, hostname auto-detect, `_DEFAULT` fallback | Self-documenting tuning; keep the sweep numbers in comments. | `config.py`. |
| 10 | **Disk-aware throttle** (concurrent ops per physical disk) | HDD head-thrash kills throughput; cap concurrency per spindle. | optional in `worker.py` for shared-disk writes. |
| 11 | **Fast JSON** (orjson w/ transparent stdlib fallback) | 2–17× ser/deser. | `jsonio.py`. |
| 12 | **Memory-efficient enumeration** | Stream the manifest to disk (never hold 100k+ paths in RAM); `os.scandir` not `iterdir`; folder-exists not per-file stat. | `seed`/enumeration helpers. |
| 13 | **PYTHONPATH propagation for spawn** | Windows `spawn` starts fresh interpreters without the parent's `sys.path`. | `worker.py` pool init. |
| 14 | **Bulk copy via robocopy + NVMe staging** (disk-aware parallel) | Fast, integrity-safe Windows bulk copy. | optional `kiroshi copy` util (later). |
| 15 | **CORE principle:** all logic in modules, minimal glue, defaults work OOTB | The "lego" philosophy. | whole package. |

The persistence model (run Fixer + Runners as **NSSM-wrapped Windows services**,
one-command installer) is inherited from at-field; see `LOCAL_CONTEXT.md`.

---

## 6. First consumer: a motion-tokenization `.dat` build

Kiroshi is built standalone, but its first real workload (run from a separate
project, **not** vendored here) validates the design end-to-end. Abstractly:

- ~100k+ short motion clips on a NAS, each a canonical-quaternion array.
- **Per-clip task** (CPU-bound, embarrassingly parallel):
  1. Probe true per-clip timebase (source media may be VFR; trust nothing).
  2. **SLERP-resample** quaternions to a uniform target FPS (two targets: 4 fps
     and 8 fps). 8 fps is robust; 4 fps has large geodesic error on fast hand
     motion — a quality caveat, not a blocker.
  3. Encode via a density-adaptive spherical quantizer (codebook on NAS).
  4. Emit the binary `.dat` input (+ sidecar metadata), atomically, to the NAS.
- Output existence per `(clip, fps)` = resume truth.

The concrete script paths, codebook locations, NAS shares, and host roster for
this consumer are in `LOCAL_CONTEXT.md`. The **task adapter** for it lives in the
consumer project and is registered with Kiroshi via `--task module:function`
(see §7), keeping Kiroshi domain-agnostic.

---

## 7. Repo layout (target)

```
kiroshi/
  pyproject.toml
  README.md
  PLAN.md                 # this file (public)
  LOCAL_CONTEXT.md        # gitignored — machine-specific pointers
  src/kiroshi/
    __init__.py
    config.py             # HostConfig, mesh config, hostname auto-detect
    paths.py              # read/write root resolution (UNC, no drive letters)
    jsonio.py             # orjson + fallback
    atomic.py             # atomic_write, fsync, atomic_path (rename-on-success)
    bench.py              # TRUE throughput from output mtimes
    jobstore.py           # SQLite (WAL) gig store: seed/lease/complete/heartbeat/reap/stats
    pool.py               # LocalPool: ProcessPool + bounded window + per-gig timeout
                          #   (force-kills hung children) + BrokenProcessPool recovery
    coordinator.py        # Fixer: FastAPI app + reaper thread + optional /p custom views
    worker.py             # Runner: pull loop wrapping LocalPool + graceful drain
    tasks.py              # task resolution ("module:function") + Task contract
    cli.py                # `kiroshi fixer|runner|seed|status`
    dashboard/
      index.html          # themed live view (polls /status, links /pages)
  examples/
    sleep_task.py         # trivial CPU task to smoke-test the mesh
    motion_resample.py    # REAL task: SLERP fps-resample of quaternion clips (numpy)
  tests/
    test_hardening.py     # crash recovery + per-gig timeout + retry (no deps; runs anywhere)
    test_motion.py        # SLERP round-trip correctness + resume (numpy-guarded)
```

### Custom per-task views (and the eventual tray)
A task can ship its own HTML visualization (e.g. a SLERP-quality viewer). Point the
Fixer at a folder of `*.html` with ``kiroshi fixer --pages-dir <dir>``; each page is
served at ``/p/<name>`` and auto-linked from the dashboard header (via ``/pages``).
This is the seam a future **at-field-style tray** plugs into: the tray just opens
these coordinator URLs (the live dashboard + any per-task view) and manages the
Fixer/Runner services. Kiroshi stays headless + scriptable; the tray is optional UX.

### Task contract
A task is a **module-level** function (picklable for `spawn`):

```python
def run(spec: dict) -> dict:
    """spec -> result. Must be importable as 'module:function'.
    Return {"status": "ok"} or raise; the Runner handles retry/atomic-write."""
    ...
```

Selected at runtime: `kiroshi runner --task mypkg.mytask:run`. The Fixer never
imports the task — only Runners do.

---

## 7.5 The front door — `run` / `join` / `install`

The original surface started at the power-user layer (`fixer` + `seed` + `runner`
+ `service` — four commands). The design north star is **one command to run your
function across the mesh, with every knob still reachable underneath.** Three verbs:

| Verb | What it does | Layer |
|---|---|---|
| `kiroshi run <task> [inputs]` | Enumerate inputs → start a Fixer (loopback) + a local Runner in-process → seed → render a live terminal progress bar → print where outputs landed + the dashboard URL. Add `--lan` to bind `0.0.0.0` so other machines can join. | "just run my function" |
| `kiroshi join` | On another machine: discover the Fixer, present the token, (consent to) fetch the task, register as an auto-start Runner service, start pulling. | "add a machine" |
| `kiroshi install` | Make it permanent: Fixer as a boot-start service + tray autostart on login. | "keep it running" |

`run` and `run --lan` are the **same code path** — the only difference is the bind
address. There is no separate "mesh mode": the Fixer already binds loopback by
default (secure), and `--lan` simply opens the door, exactly like `kiroshi fixer
--host 0.0.0.0`. You scale 1→N machines without changing your workflow.

Design notes:
- **In-process Fixer + Runner.** `run` starts the FastAPI Fixer in a background
  thread and a Runner in another, both in the launching process. Ctrl-C drains
  and exits. The coordinator is *ephemeral* — for a permanent one use `install`.
- **Persistent DB, not `:memory:`.** `run` uses a real SQLite file (default
  `<state_dir>/run-<slug>.db`) so the lease reaper / retry / resume survive a
  crash+restart of the launcher. Resume also comes for free from output-existence
  when the task implements the skip check (§5).
- **The progress bar reads the in-process store directly** (`store.stats()`), so it
  shows *aggregate* progress across every joined machine — the payoff of
  distribution, visible where you launched it. TTY → live `\r` bar; non-TTY →
  periodic status lines.
- **Auth follows the bind.** Loopback `run` defaults to no-auth (only this box can
  reach it). `--lan` generates + persists a mesh token and prints it, so joiners
  have a one-time join code (same model as `fixer`).

### Enumeration contract
`run` turns *inputs* into *gigs*. Three ways, simplest first:

1. `--items "<glob>"` — one gig per matching path; the spec is `{"path": <rel>}`.
   Zero task code; covers the trivial one-file-one-gig case.
2. `--jobs file.jsonl` — explicit gig list (same as `seed --jobs`).
3. `--enumerate` — call the task module's **enumeration hook**:

   ```python
   def enumerate_gigs(args: dict) -> Iterator[dict]:
       """Kiroshi enumeration contract. `args` are the pass-through tokens after
       `--` on the `kiroshi run` line (repeated flags become lists). Yield
       {"job_id": str, "spec": dict, "group"?: str} — one per unit of work."""
   ```

   Invoked as `kiroshi run pkg.task:run --enumerate -- --read-root //nas --fps 4 --fps 8`.
   The task owns its fan-out (e.g. one expensive read → a 4-fps **and** an 8-fps
   gig) — a generic globber can't infer that. This is the seam that makes `run`
   work for real workloads, not just toys.

### Task-code distribution (for `join`) — PLANNED, not yet built
The deepest friction in adding a machine is getting the *task code* onto it.
Planned model, **opt-in and consent-gated** (see `SECURITY.md`): a `run --lan`
Fixer can hold the task source and serve it to a joining Runner over the
token-gated API; `join` shows the code's hash and asks the operator to approve
before writing/importing it. Single-file tasks join with no checkout; multi-module
tasks use `--task-repo <url> --task-deps "…"` (clone + pip install) or are
pre-installed. **This changes the threat model (a rogue Fixer → RCE on Runners),
so it is never silent.**

---

## 8. Build milestones

- **M0 — Skeleton:** ✅ package, config, jsonio, atomic, bench, jobstore,
  coordinator, worker, cli, example task, minimal dashboard. Smoke-tested.
- **M1 — Self-heal & resume:** ✅ lease-TTL reaper, output-exists skip,
  idempotent seed, graceful Runner drain (SIGINT). Multi-Runner on one box.
- **M2 — Real mesh:** ✅ Fixer + cross-host Runners over HTTP; kill-a-Runner
  self-heal + resume. Zero-config discovery (`--fixer auto`) survives DHCP drift.
- **M3 — First consumer:** ✅ motion `.dat` adapter demonstrated cross-host
  on a Windows GPU box reading/writing NAS over UNC, Smart-App-Control-compatible.
  Full-corpus sweep pending.
- **M3.5 — Hardening & UX (this pass):** ✅ mesh-token auth + threat model,
  hardened discovery, runner registration + launch-command capture, rate-curve
  time-series, Jobs/History/per-job console pages + custom job pages, terminal
  logging, process registry + at-field pause-awareness, system tray.
- **M4 — Persistence:** ✅ NSSM-wrap Fixer + Runners as Windows services
  (at-field pattern); `kiroshi service install|uninstall|status` + `scripts\*.ps1`
  elevation shims. Auto-start, rotating logs, crash auto-restart. Enforces the
  NAS-credential rule: Fixer→LocalSystem, NAS-bound Runner→real user account
  (refuses LocalSystem without `--force`). Launches via `python -m kiroshi`.
- **M4.5 — Always-on + tray autostart:** ✅ `kiroshi install` (one command:
  Fixer boot-start service + tray login-autostart via `HKCU\Run`); `kiroshi
  uninstall`; `kiroshi autostart on|off|status`; tray self-registers on launch,
  left-click opens the dashboard, tooltip shows live campaign progress. Fixed the
  Windows quoting bug (`shlex.quote` single-quotes break NSSM `AppParameters`).
- **M4.6 — The front door (`kiroshi run`):** the one-command path — enumerate
  (`--items` glob / `--jobs` / `--enumerate` hook) → in-process Fixer (loopback)
  + local Runner → live terminal progress bar (aggregate over the mesh) →
  end-of-run output location + dashboard URL. `--lan` opens it to other machines.
  Persistent run DB so self-heal/resume survive a launcher restart. (§7.5)
- **M5 — `kiroshi join` (scoped):** ✅ one command on a new machine — discover the
  Fixer, **mutually authenticate** it, ensure the task (pre-installed, or
  **consent-gated + hash-pinned** fetch of a `run --serve-task` single-file task),
  then run a Runner foreground or `--service` (auto-start). Assumes Python +
  `pip install kiroshi` is the single prerequisite (no Python/conda bootstrapping).
  Endpoints `/task/meta` + `/task/source` (token-gated); consent + pinning per
  SECURITY.md §6.5. End-to-end verified: a fresh-state-dir joiner fetched+approved
  code and joined the mesh with no checkout. A one-click `.exe` join (at-field's
  NSIS model) and `--task-repo` (multi-module) remain later steps.
- **M6 — Package & polish:** docs, examples, `pip install kiroshi`, dashboard
  theming, optional at-field pause-awareness, optional `kiroshi copy` (robocopy).

---

## 11. Security model (mesh token)

Kiroshi is a *mesh*: the Fixer binds a routable address so other machines can
join, which means the coordination API is exposed on the LAN. Because it hands
out arbitrary task execution and is **open source** (protocol is public), the LAN
is treated as **hostile** and every data/control endpoint requires a shared
**mesh token** (`security.py`, constant-time compare). The Fixer auto-generates +
persists a token (`<state_dir>/mesh.token`) if you don't supply one; Runners get
it via `--token`/`KIROSHI_TOKEN` (a one-time "join code"). Discovery leaks no
hostname and no secret, and is solicited-only by default. Full threat model +
OSS-exposure analysis: **`SECURITY.md`**.

## 12. Observability & at-field integration

- **Launch-command capture** — Fixer/Runner record their full argv; Runners
  `/register` theirs so it surfaces per job (live + history).
- **Rate curves** — the Fixer samples throughput + per-campaign done-counts into
  a ring buffer (`/metrics/history`); the dashboard renders hand-rolled SVG
  curves (at-field style, no chart libs). The **Jobs** view groups gigs into
  campaigns (by `job_id` prefix) with a pill progress bar + per-job curve.
- **Terminal logging** — each process tees stdout+stderr to a rotating log under
  the state dir (`logsetup.py`).
- **Process registry** (`processreg.py`) — every Fixer/Runner advertises a JSON
  manifest (pid, role, launch command, graceful-stop hook), also mirrored into
  at-field's `clients/kiroshi` dir if present. `kiroshi stop` / the tray drops a
  stop-sentinel for a clean drain before any hard kill — this is the "register
  with at-field for emergency shutdown" seam (at-field has no opt-in API; it
  kills `python.exe` trees, so Kiroshi *advertises* its processes instead).
- **at-field pause-awareness** (`atfield.py`) — Runners watch `pause.sentinel`
  and stop leasing while a rig is being protected.
- **Tray** (`tray.py`, optional `[tray]` extra) — status icon + menu that opens
  the console pages (token injected) and triggers local graceful-stop.

## 9. Key decisions (log)

- **Standalone repo from day 1**, pip-installable; first consumer is external.
- **Zero dependency on the reference pipeline repo.** Patterns are re-implemented,
  not imported. (Reference repo may adopt Kiroshi later.)
- **Batch-lease + local ProcessPool** execution model (not one-gig-at-a-time).
- **Name: `kiroshi`** (PyPI-free; the all-seeing optics → the mesh dashboard).
- **Fully OSS** like at-field → strict PII hygiene; pointers only in
  `LOCAL_CONTEXT.md`.

---

## 10. Where to look next

- `LOCAL_CONTEXT.md` (gitignored) — every absolute path, NAS share, hostname,
  the reference-repo file/line pointers, the consumer-project script paths, and
  the originating chat transcript.
- `README.md` — user-facing quickstart.
- Start at `src/kiroshi/jobstore.py` + `coordinator.py` + `worker.py` to extend.
