# Hello, mesh — a 5-minute Kiroshi walkthrough

The shortest path from "I have a Python function" to "it's running across
my machines." Three flavors, pick one:

---

## Flavor 1 — one-shot local (dev iteration)

You have `mypkg/mytask.py` with `def run(spec: dict) -> dict`. Run it across
your local cores against an inline list of items:

```
kiroshi run mypkg.mytask:run --items a.txt b.txt c.txt --workers 8
```

Kiroshi starts an in-process Fixer + Runner, drives the work, prints a
summary, exits. That's it. Nothing else installed.

If your task defines `enumerate_gigs(args)` (see `examples/task_minimal.py`),
skip the item list:

```
kiroshi run mypkg.mytask:run --enumerate -- --read-root ./in --write-root ./out
```

The `--` separator passes everything after it to `enumerate_gigs` as `args`.

---

## Flavor 2 — one-shot LAN (small ad-hoc mesh)

Same as above but bind the Fixer to the LAN so other boxes can join:

```
# on the "coordinator" box:
kiroshi run mypkg.mytask:run --enumerate --lan -- --read-root //nas/in --write-root //nas/out

# on any other box:
kiroshi remote join user@coordinator --task mypkg.mytask:run --workers 8
```

Runners on the other machines auto-discover the Fixer (UDP beacon) and pull
work. Handy for a spare laptop.

---

## Flavor 3 — durable production (multi-day campaigns)

Three long-running processes, resumable, dashboarded, monitored.

```
# 1. Coordinator (one, anywhere with a persistent disk):
kiroshi fixer --db campaign.db --host 0.0.0.0 --port 8800

# 2. Workers (one per machine — bind to ONE task):
kiroshi runner --fixer http://coordinator:8800 --task mypkg.mytask:run \
               --workers 16 --capacity 24 --gig-timeout 300 \
               --max-tasks-per-child 100        # recycle worker memory

# 3. Enqueue work (dedups by job_id — safe to re-run):
kiroshi seed --fixer http://coordinator:8800 \
             --jobs gigs.jsonl --group my-campaign \
             --label "First run of my thing"

# 4. Watch (browser or CLI):
#   Dashboard:  http://coordinator:8800/?token=<MESH_TOKEN>
#   CLI:        kiroshi status --fixer http://coordinator:8800
#   Search:     kiroshi jobs --fixer http://coordinator:8800 --grep 'PermissionError' --field error --state failed
#   Alerts:     curl "http://coordinator:8800/advisories?token=<TOKEN>"
```

**Kill any of the three and restart it — the campaign resumes.** The Fixer
persists state in the SQLite DB; the Runner's tasks are idempotent
(skip-if-output-exists); the Seed is deduplicated by `job_id`.

---

## Multi-stage / dependent work

If your workload is `stage A → stage B → stage C` (with per-item deps, or a
map→reduce→map barrier like "build a codebook from all A outputs, then use
it in every C"), do **not** hand-roll a polling cascade. Declare it:

```
kiroshi pipeline validate my_pipeline.toml   # print the DAG, no I/O
kiroshi pipeline run      my_pipeline.toml   # coordinator loop
```

See `examples/pipeline.example.toml` and `docs/PIPELINE.md` for the edge
kinds (`each` / `quorum:k` / `all` / `artifact`).

---

## Staging data between storage tiers

Before a compute stage can run fast, you often need to copy a dataset from a
slow tier (HDD array) to a fast tier (NVMe cache). `kiroshi stage` does this
as a budgeted, resumable mesh job — replacing hand-rolled parallel rsync:

```
# local (in-process, like 'kiroshi run'):
kiroshi stage --from //NAS/array/data --to //NAS/cache/data --workers 8

# mesh (seed gigs; a runner distributes the copies):
kiroshi stage --from //NAS/array/data --to //NAS/cache/data \
              --fixer http://coordinator:8800 --group warm-cache
kiroshi runner --fixer http://coordinator:8800 --task kiroshi.staging:run --workers 16
```

Files already copied (same size) are skipped — kill and restart is free.

## Measuring + calibrating throughput

Don't guess per-disk `concurrency` — measure it:

```
# 1. benchmark a disk at increasing concurrency:
kiroshi nas benchmark --root //NAS/disk1 --concurrency 1,2,4,8,16

# 2. turn the results into a recommendation:
kiroshi bench calibrate --samples '1=50,2=95,4=140,8=150,16=130' --bias balanced
#   -> recommended concurrency = 4 (paste into [[storage.disk]])

# 3. after a campaign, report TRUE end-to-end throughput:
kiroshi bench rate --dir //NAS/cache/outputs --pattern '*.npz'
#   or over HTTP (no FS access needed):
kiroshi bench rate --fixer http://coordinator:8800 --group my-campaign --token <TOKEN>
```

---

## Where to go next

- **`AGENTS.md`** — task-indexed capability map (this doc's parent).
- **`kiroshi capabilities --json`** — the machine-readable version.
- **`kiroshi doctor`** — preflight on any new node (python, deps, disk, firewall).
- **`kiroshi tray` + `kiroshi autostart on --mode scheduled`** — a system-
  tray lens on the mesh that self-restarts within ~1 min if it ever dies.
- **`kiroshi mcp`** — expose Kiroshi to LLM agents (Claude Desktop / Cursor
  / custom clients) as typed tools + resources. `pip install kiroshi[mcp]`.
