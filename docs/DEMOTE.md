# Demote: write-back NVMe → sharded HDD, flushed when the array is idle

## The problem

A compute job writes fast to the NVMe cache tier (`//alexandria/pipe_nvme/...`).
Those bytes eventually belong on the HDD parity array, laid out in a
**deterministic per-spindle shard layout** (`/mnt/diskN/LubuN/<dataset>/shard_0N/...`)
so later reads spread across all 7 heads instead of hammering one.

Two things native Unraid can't do for you:

1. Its mover flushes cache → array by *its own* allocation policy; it will **not**
   place files into a custom `shard_0N` layout.
2. So to get deterministic sharding you must write **direct `/mnt/diskN` paths**,
   which the `iohint` write-danger gate refuses by default (data-loss footgun).

## The model: a write-back cache tier, expressed on the destination

You declare the *eventual* home when you create the job, using a glob whose `*`
means "fan out across every matching spindle":

```
write     = "//alexandria/pipe_nvme/MonologDataset"    # write here now (fast)
demote_to = "/mnt/user/Lubu*/MonologDataset"           # flush to sharded HDD when idle
```

`Lubu*` binds to the existing `LubuN ↔ diskN` 1:1 layout: shard *k* → `diskK/LubuK`.
The **assignment rule is the bin-pack we already have** (`kiroshi nas shard` /
`plan_shard`) — demote never invents placement; it reuses (or freezes) a shard plan.

## Execution: reuse three existing pieces + one new gate

| Need | Reuses |
|---|---|
| Which file → which disk | `nascli.plan_shard` (greedy bin-pack) + a persisted plan JSON |
| Copy w/ resume, skip-if-exists, budget | `staging._copy_file` + `ResourceClient` |
| Physical `diskN/LubuN/shard_0N` dest | `nascli` physical layout (promoted from `nas_apply_shard_plan.py`) |
| **Flush only when HDD quiet** (NEW) | coordinator idle-gate on `/lease` + `IOWatcher` |

`kiroshi demote` is sugar: it builds/loads the shard plan, seeds one copy sub-job
per file (dst = its assigned physical disk path), and marks the job **idle-gated**.
A runner bound to `kiroshi.demote:run` leases those gigs — but only when the gate
opens.

## The idle gate (the only net-new logic)

A job-level config, stored in the `jobs` table, evaluated at `/lease`:

```
idle_gate = { disks = ["disk1".."disk7"], util_pct = 15, sustain_min = 30 }
```

Semantics (hysteresis, in `idlegate.py`, pure + unit-tested):

- Read `IOWatcher.snapshot()` → per-disk rolling `util_pct` (5-min window).
- `cur = max(util_pct over gate disks)`.
- If `cur <= util_pct`: mark quiet (`quiet_since = now` if not already set).
- If `cur > util_pct`: **reset** `quiet_since = None` (any breach restarts the clock).
- **Admit** leases only when `quiet_since` set AND `now - quiet_since >= sustain`.
- Evaluated on every demote `/lease` poll (runners poll every few seconds), so the
  gate is self-correcting: the moment the array gets busy again, the next poll sees
  high util and stops admitting new gigs. In-flight gigs (one small file each)
  finish; nothing is preempted.

Lease decisions record `binding_reason = IDLE_GATE_WAIT` while closed, so `status`
and the decision log show *why* the demote job is parked.

### Fail-open, loudly

If the coordinator has no HDD I/O telemetry (e.g. it runs on Windows, or the
topology declares no HDD disks) the gate **cannot** judge idle. It then admits
with `binding_reason = IDLE_GATE_NO_TELEMETRY` rather than stalling forever, and
`kiroshi demote` warns at seed time. The coordinator is meant to run on the NAS
(Linux, `/proc/diskstats`), where telemetry is always available.

## The one deliberate policy: direct-disk write ack

Deterministic sharding *requires* bypassing FUSE on the write (see above). So the
demote mover is a **trusted, ack-carrying writer**: it writes direct `/mnt/diskN`
paths on purpose and carries the `direct_disk_write` io_ack, because the shard plan
is the authority for placement. This is Option A (deterministic layout). Option B
(let mergerfs place files via `/mnt/user`, no ack, no deterministic sharding) is
not what we build — it gives up the per-spindle read balance that is the whole point.

## Discoverability (how a vibe-coding LLM finds this)

1. **Declarative field first** — `demote_to` + `idle_gate` on job creation. One
   field turns it on; the coordinator enforces. Nothing imperative to remember.
2. **`capabilities.py` + `AGENTS.md`** — a cold agent reads `kiroshi://capabilities.json`
   and sees the `demote` entry before doing anything.
3. **MCP** — a `demote` tool (builds plan + seeds idle-gated gigs) for agents that
   drive Kiroshi over MCP.
