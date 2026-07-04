# Kiroshi naming migration & glossary

Kiroshi dropped the old "Cyberpunk" vocabulary (Fixer / campaign / gig) in
favor of plain operator terms. This is the canonical glossary plus the exact
old→new mapping and the handful of **deliberately frozen** legacy names.

## Glossary (the vocabulary)

| Term            | Meaning                                                                 |
|-----------------|-------------------------------------------------------------------------|
| **Kiroshi**     | The whole app / mesh work-queue system.                                 |
| **job**         | One workload, e.g. "convert 1k files to 88-DoF" or "shard a folder".    |
| **coordinator** | The single brain that allocates sub-jobs across the mesh and logs the decisions. Formerly **Fixer**. |
| **sub-job**     | One unit of a job (e.g. 10 files), leased to a node. Formerly **gig**.  |
| **mesh / node** | The worker machines that lease + run sub-jobs.                          |

There is **one coordinator** (singleton, machine-locked). Per-job decisions are
filtered from the shared decision log, not from separate coordinators.

## Renamed (primary name → what it replaced)

| New (use this)                       | Old (still works — alias)                    |
|--------------------------------------|----------------------------------------------|
| `kiroshi coordinator`                | `kiroshi fixer`                              |
| `--coordinator <url>`                | `--fixer <url>`                              |
| `--coordinator-port`                 | `--fixer-port`                               |
| `--force-second-coordinator`         | `--force-second-fixer`                       |
| `--subjob-timeout`                   | `--gig-timeout`                              |
| `KIROSHI_COORDINATOR_HOST` / `_PORT` | `KIROSHI_FIXER_HOST` / `_PORT`               |
| `[coordinator]` config section       | `[fixer]`                                    |
| `enumerate_subjobs` task hook        | `enumerate_gigs`                             |
| `paths.subjob_read_root` / `_write_root` | `paths.gig_read_root` / `_write_root`    |
| DB table `jobs` / `subjobs`; column `job` | `campaigns` / `jobs`; column `grp`      |
| routes `/subjobs`, `/subjob/{id}`, `/subjob/trace?subjob_id=` | `/jobs`, `/job/{id}`, `/job/trace?job_id=` |

Internal identifiers followed suit (`coordinator_url`, `_cmd_coordinator`,
`discover_coordinator`, `check_singleton_coordinator`, `args.coordinator`, …).

## Deliberately frozen (still say "fixer" / "gig" on purpose)

These are **not** oversights — renaming them would break wire/infra compat with
no upside:

- **CLI/env aliases** above (`--fixer`, `kiroshi fixer`, `KIROSHI_FIXER_*`,
  `[fixer]`): kept so existing scripts/services keep running. Hidden from help.
- **Windows service name `kiroshi-fixer`** (`DEFAULT_FIXER_SERVICE`): renaming =
  service reinstall on every node. Left as-is.
- **Firewall rule name `FIXER_RULE_NAME`** ("Kiroshi Coordinator HTTP"): the
  *identifier* stays so existing installed rules are still matched + cleaned.
- **UDP discovery beacon magic (`kiroshi-fixer`)**: a wire token both sides must
  agree on; frozen so mixed-version nodes still discover each other.
- **Lease wire key `"gigs"`, `LeaseResult.gigs`**: on-the-wire field read by
  every runner + dashboard. Frozen.
- **`enumerate_gigs` / `gig_read_root` / `gig_write_root`**: task-ABI names used
  by compute task modules in other repos (e.g. Pose_MBPE). The new
  `enumerate_subjobs` / `subjob_*` names are the preferred aliases; the `gig_*`
  originals stay valid so task code needs no lockstep change.
- **advisory code `gig.failure_spike`**: an advisory contract string matched by
  dashboards/MCP clients. Frozen.

## Rule of thumb

Write new code, docs, and scripts with the **new** names. The old names remain
as thin aliases only for backward compatibility and will not be removed while
external task repos and installed services still reference them.
