# Kiroshi pipelines — dependent multi-stage work

Kiroshi's core is a mesh work-queue: a Coordinator hands gigs to Runners, budgeting
per-disk/per-host. That's the right primitive for **one** stage of embarrassingly
parallel work. Real datasets need **several** stages with dependencies:

```
canonical 30fps ──reduce──▶ 88-DoF@30fps ──slerp──▶ 88-DoF@4fps ──encode──▶ DVQ×3
                              │
                              └──(corpus sample)──▶ codebook ──gates──▶ encode
```

Before `kiroshi pipeline`, the only way to stagger these was an external
"cascade" script that polled one campaign's done gigs and seeded the next.
That works but is bespoke, untested glue. `kiroshi pipeline` makes it a
first-class, tested primitive.

## One job or many?

**Many stages** is correct when any of these hold (they usually do):

1. **Each stage output is a wanted, persisted deliverable.**
2. **Stages have different resource profiles** the mesh should route
   independently (a CPU/IK-heavy reduce vs. a light table-lookup encode).
3. **There is a map → reduce → map barrier** — a *global* artifact
   (a codebook, a normalization table, a learned quantizer) aggregated from
   many items, then consumed by every downstream item. This can **never** be
   fused into a per-item job.

Fusing stages into one gig only wins for a pure map→map chain where you do
**not** want the intermediate persisted — then fusion avoids re-reading/
re-writing the intermediate to shared storage. Otherwise, keep them separate.

## Typed edges — the one piece of declared knowledge

The scheduler should not *infer* "may B start now, or must it wait for all of
A?". You **declare** it, once, per edge:

| kind        | semantics                                                            | use for |
|-------------|----------------------------------------------------------------------|---------|
| `each`      | downstream item X unlocks the instant upstream item X is done        | map→map fan-through |
| `quorum:k`  | **barrier**: run the downstream action once ≥ k upstream items done  | build a global artifact from a corpus sample |
| `all`       | **barrier**: run once *every* upstream item is done                  | strict reduce |
| `artifact`  | **gate**: downstream stays blocked until named file(s) exist         | consumers of a barrier's output |

The coordinator stays dumb: each tick it applies the declared semantics. All
the pipeline's dependency knowledge lives in the spec.

## Spec + commands

See [`examples/pipeline.example.toml`](../examples/pipeline.example.toml).

```
kiroshi pipeline validate my_pipeline.toml   # print the DAG, no I/O
kiroshi pipeline run      my_pipeline.toml   # coordinator loop
kiroshi pipeline run      my_pipeline.toml --once   # single tick (cron-style)
```

A **source** stage (seeded/served externally) needs only `coordinator` + `group`;
the pipeline just observes its done set. A **map** stage adds a
`job_id_template` + `[stages.X.spec]` template (`{clip}` / `{stem}` are
substituted per item). A **barrier/reduce** stage carries a `command` (run
once when its quorum trips) and `produces` (the artifact paths it writes,
which `artifact` edges then gate on).

## Implementation notes

- Pure core (`item_key`, `resolve_each`, `quorum_met`, `render_spec`,
  `build_gigs`) is I/O-free and unit-tested (`tests/test_pipeline.py`).
- The coordinator talks to Coordinators over the existing `/metrics/export`
  (done/seeded sets) and `/seed` (dedups by `job_id`) endpoints — no new
  Coordinator surface, works cross-host.
- Barrier `command`s run wherever the coordinator runs (e.g. an `ssh` to a
  build host that reads the corpus from local NVMe — far faster than pulling
  it back over SMB).
- Everything is idempotent + resumable: state lives in the Coordinators' job DBs
  and the on-disk artifacts; kill and relaunch the coordinator freely.
