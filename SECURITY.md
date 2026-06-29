# Kiroshi — Security Model & Threat Analysis

Kiroshi runs **Python functions across multiple machines** and coordinates them
over HTTP. That is inherently sensitive to operate, so security is a first-class
design constraint — not an afterthought.

This document is written for an **open-source release**: assume an attacker has
read every line of this repo and knows exactly how the protocol works. The only
thing they must *not* have is your **mesh token**.

> **TL;DR.** Every coordination endpoint requires a shared **mesh token**. Both
> sides authenticate: a Runner cryptographically **verifies the Fixer** (HMAC
> challenge) before sending its token or running any work, so a rogue Fixer can't
> hijack `--fixer auto`. The Fixer **binds loopback by default** (you opt into LAN
> exposure explicitly). The dashboard escapes untrusted strings; the token is
> **redacted from on-disk logs** and from captured launch commands. The remaining
> inherent limitation is **no TLS** — on an untrusted network, run Kiroshi over a
> private overlay (WireGuard/Tailscale) or behind TLS.

---

## 0. The question that matters: does running Kiroshi expose my machines?

Two different risks, kept separate on purpose:

### (a) Additive risk — what Kiroshi *itself* opens up
Kiroshi adds **one listening service** (the Fixer's HTTP port; optionally a UDP
discovery port) and a pool of **Runners that execute a chosen task function**.
Everything that listens or executes is gated and hardened:

| Surface | Control |
|---|---|
| Fixer HTTP API (hands out work, mutates queue, discloses topology) | **Mesh token required** on every data/control endpoint; constant-time compare. |
| Runner executing a rogue Fixer's specs / leaking its token via `--fixer auto` | **Mutual auth:** Runner verifies the Fixer with an HMAC challenge *before* sending credentials or executing anything (`/auth/challenge`, `security.prove/verify_proof`). Fails closed. |
| Network exposure | Fixer **defaults to `127.0.0.1`**; `0.0.0.0` is an explicit, warned opt-in. |
| Dashboard token theft via stored XSS | All untrusted fields (`job_id`, runner-reported `host`, error text) are **HTML-escaped**. |
| Token at rest | **Redacted** from teed logs and from launch commands shown in the UI; only ever stored in the token file. |
| Custom per-job pages (`/p/`) | **Token-gated** (not world-readable). |
| Task reading/writing outside its data roots | The shipped example task **confines paths to `KIROSHI_READ_ROOT`/`WRITE_ROOT`** (rejects absolute + `..` escape). |

Not present (verified): no `pickle`/`eval`/`yaml.unsafe_load` on network data
(JSON only), no SQL injection (fully parameterized), no path traversal in static
serving (Starlette normalizes).

### (b) Inherent risk — does Kiroshi *require* an insecure setup to work?
**No.** Kiroshi does not require you to disable a firewall, share credentials
broadly, or trust the LAN. It runs fine bound to loopback, or over a private
overlay, or behind TLS. Its **defaults assume a trusted segment** (plain HTTP),
which is convenient but not zero-trust. The one thing it does *not* provide
itself is **transport encryption** — so on an untrusted network you must wrap it
(overlay/TLS). That is a deployment choice, not an inherent vulnerability in the
design. See §6–§7.

---

## 1. Assets we protect

| Asset | Why it matters |
|---|---|
| **Runner execution** | A Runner runs the operator's task against attacker-influenceable `spec` data. The task defines what a spec can *do*; confine it (see §6). |
| **Job queue / outputs** | Stealing/poisoning gigs wastes compute or corrupts shared-NAS outputs. |
| **Topology / operational info** | Hostnames, file paths, error strings, task names — useful for lateral movement. |
| **Mesh token** | The single secret gating the whole mesh. |

## 2. Trust boundary

The trust boundary is the **mesh token, not the LAN.** Home/office LANs routinely
contain untrusted devices (IoT, guests, a compromised laptop), so:

- **The LAN is treated as hostile.** Reaching the Fixer's port is not enough; you
  must present the token *and* (for Runners) prove the Fixer holds it too.
- Protocol, ports, and beacon format are assumed public knowledge.

## 3. Mutual authentication (closes the `--fixer auto` hijack)

A shared bearer token alone only proves *Runner → Fixer*. Without the reverse, a
LAN attacker could stand up a **rogue Fixer**, win UDP discovery, and (1) harvest
the bearer token a Runner sends, and (2) feed it arbitrary specs to execute.

Kiroshi closes this with a challenge before any trust is extended:

1. Runner picks a random `nonce`, calls `GET /auth/challenge?nonce=…` **with no
   Authorization header**.
2. Fixer returns `HMAC-SHA256(token, nonce)` — provable only by a holder of the
   same token, and it never reveals the token.
3. Runner verifies the proof (constant time). Only then does it register, lease,
   and execute. A Fixer that fails — or claims "no auth" while the Runner holds a
   token — is refused, and the Runner re-discovers (auto mode).

Caveat: this defends impersonation and discovery hijacking. It does **not** by
itself stop a full on-path MITM on cleartext HTTP (no channel binding) — that's
what the overlay/TLS recommendation in §6 is for.

## 4. The mesh token

- **Resolution:** `--token` → `KIROSHI_TOKEN` env → token file
  (`<state_dir>/mesh.token`) → (Fixer only) auto-generate + persist.
- **State dir:** `%PROGRAMDATA%\Kiroshi` (Windows) / `~/.kiroshi` (POSIX),
  overridable with `KIROSHI_STATE_DIR`. Windows relies on the ProgramData ACL;
  POSIX chmods the token file `600`.
- **Never written to logs.** The Fixer prints the token to the console only; the
  log tee scrubs it (and any `--token`/`--password` in launch commands).
- **Transport:** `Authorization: Bearer` (preferred) / `X-Kiroshi-Token` / `?token=`
  (browser convenience — prefer the header for scripts; query strings can land in
  history/proxy logs, so the dashboard strips it from the URL immediately).
- **Distribution:** copy the token the Fixer prints, then `set KIROSHI_TOKEN=…`
  (or `--token`) on each Runner. The only manual step; treat it like an SSH key.
- **Rotation:** delete `<state_dir>/mesh.token` (or set a new `KIROSHI_TOKEN`) and
  restart the Fixer; redistribute. Old tokens get `401` and back off.

## 5. Binding & network exposure

- The Fixer **binds `127.0.0.1` by default** (secure). To form a real mesh, pass
  `--host 0.0.0.0`; Kiroshi prints a clear NOTE that the API is now LAN-reachable
  and tells you not to port-forward it.
- `--no-auth` on a non-loopback bind prints a loud `DANGER` (open RCE-influence
  surface) — don't.
- **Do not port-forward `8787`/`8788` to the internet.** For off-site Runners use
  a VPN/WireGuard/Tailscale overlay (private addresses) — that also gives you the
  encryption Kiroshi doesn't provide natively.
- Local stop actions (tray) go through the on-disk **process registry**, not the
  network.

## 6. What a frontier startup should do (deployment posture)

Kiroshi can meet a high bar, but the *operator* owns these:

1. **Put the mesh on a private overlay or TLS.** WireGuard/Tailscale gives you
   encryption + identity + network isolation; or terminate TLS in front of the
   Fixer. Never expose the raw port publicly.
2. **Treat the token as a secret.** Out-of-band distribution, rotation, never in
   git, never in `--task`/`--token` flags that get logged (Kiroshi redacts, but
   prefer env/secret stores).
3. **Confine your task.** The task function defines the capability a `spec`
   grants. Reject absolute/`..` paths (the example shows how), validate inputs,
   write only under your output root, and run Runners as a **least-privilege
   account** (and, on Windows, the *user* account that holds NAS creds — never
   LocalSystem for NAS work).
4. **Run the Fixer on a trusted host**, ideally as a service under a dedicated
   account; keep `--no-auth` for throwaway local dev only.
5. **Audit dependencies** (FastAPI/uvicorn/requests) and pin versions.

With (1)–(4), the residual exposure is "a token-holder can flood/poison the
queue" (a DoS by an insider with the secret) — the same trust you place in anyone
holding an SSH key to the cluster.

## 6.5 Planned: task-code distribution for `join` (NOT YET IMPLEMENTED)

The `kiroshi join` front-door verb (PLAN §7.5) aims to make adding a machine one
command. The hard part is getting the **task code** onto the new machine. The
planned mechanism — a `run --lan` Fixer serving the task source to a joining
Runner — is a **deliberate, recorded change to the threat model**, documented here
*before* it ships so the decision is reviewable.

**Why it's sensitive.** Today the protocol ships only **specs (JSON data)**; task
code is local and trusted on each Runner. Every guarantee above assumes that. The
moment a Fixer can hand a Runner **executable code**, a rogue or compromised Fixer
becomes **remote code execution on every Runner** — potentially as `LocalSystem`
for a service-installed Runner. That is a qualitative escalation, not a footnote.

**Required controls (all of them, or it doesn't ship):**

1. **Opt-in, never silent.** Code distribution is off by default. A plain
   `kiroshi run` / `runner` never sends or accepts code; only an explicit
   `run --serve-task` (Fixer side) + an interactive/confirmed `join` (Runner side)
   enable it.
2. **Operator consent at the Runner.** `join` displays the code's SHA-256 and a
   summary ("1 file, N bytes, from <fixer ip>") and requires a `[y/N]` approval
   (or an explicit `--accept-task-hash <sha256>` for scripted installs) before the
   code is ever written to disk or imported.
3. **Hash pinning.** The approved hash is recorded; the Runner refuses any later
   source whose hash differs, so a Fixer (or on-path MITM) can't swap the code
   after consent. Re-approval is required to change it.
4. **Still token-gated + mutually authenticated.** Fetching the source requires the
   mesh token and passes through the existing `/auth/challenge` mutual-auth, so a
   rogue Fixer can't even offer code without the token.
5. **Confinement unchanged.** The fetched task still runs under the Runner's
   (least-privilege) account and confines paths to its data roots (§6).

Multi-module / heavy-dependency tasks bypass code-shipping entirely: `join
--task-repo <url> --task-deps "…"` clones + `pip install`s (operator chose the
URL), or the task is pre-installed (the current model). Until all of (1)–(3) land,
**`join` requires the task to be pre-installed** — exactly today's behavior.

## 7. Remaining risks / non-goals (honest)

- **No built-in TLS.** Specs/results/token travel in cleartext on plain HTTP. The
  token prevents *action* and mutual auth prevents *impersonation*, but not
  confidentiality or on-path tampering. → overlay/TLS (§6).
- **Tasks are arbitrary code by design.** Kiroshi does not sandbox the task; the
  operator chooses it. Only run tasks you control, and confine their file/network
  access.
- **DoS from a token holder.** One shared secret; any holder can flood the queue.
- **at-field client manifests** under `%PROGRAMDATA%` are world-readable and hold
  launch commands (no secrets — those are redacted). Don't put credentials in flags.

## 8. Hardening checklist

- [ ] Let the Fixer auto-generate a token; never run `--no-auth` in production.
- [ ] Distribute the token out-of-band; never commit it.
- [ ] Keep the default loopback bind unless you need the mesh; then use `0.0.0.0`
      **on a private overlay**, never a forwarded public port.
- [ ] Leave discovery solicited-only (default); `--no-beacon` if you pin URLs.
- [ ] Confine your task to its data roots; run Runners least-privilege.
- [ ] Keep secrets out of `--task`/launch flags (surfaced in the UI; redacted but
      still avoid).

## 9. Reporting a vulnerability

Open a private security advisory on the repository rather than a public issue.
Include a reproduction and the affected version.
