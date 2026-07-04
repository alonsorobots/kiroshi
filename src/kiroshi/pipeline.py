"""kiroshi.pipeline — declarative multi-stage pipelines with typed edges.

Promotes the ad-hoc "cascade seeder" pattern (poll an upstream campaign's
done gigs, seed the next campaign) into a first-class, *tested* primitive.

A pipeline is a set of STAGES connected by typed EDGES. The edge type is the
one piece of "privileged" dependency knowledge — and it is DECLARED, never
inferred by the scheduler:

  each        downstream gig for item X unlocks the instant upstream item X
              is done. The common map->map fan-through.
  quorum:k    a BARRIER: fire the downstream *action* once >= k upstream
              items are done (map->reduce — e.g. build a global codebook
              from a corpus sample of the reduced clips).
  all         quorum where k == the full upstream item count.
  artifact    a gate: a stage's gigs stay blocked until a named file exists
              (e.g. the DVQ stage waits for the codebook the quorum stage
              produced).

The coordinator stays dumb: every tick it just applies the declared edge
semantics. This module deliberately has two halves:

  * PURE core (``item_key``, ``resolve_each``, ``quorum_met``, ``render_spec``)
    — no I/O, unit-tested in tests/test_pipeline.py.
  * a thin HTTP/loop shell (``PipelineCoordinator``) that talks to one or
    more Fixers over the existing ``/metrics/export`` + ``/seed`` endpoints.

Why many stages instead of one fused job? Because (1) every stage output is a
persisted deliverable, (2) stages have different resource profiles the mesh
routes independently, and (3) a map->reduce->map BARRIER (the codebook) can
never be fused into a per-item job. See the module tests + docs/PIPELINE.md.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, Optional

try:  # py3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore


# --------------------------------------------------------------------------
# PURE CORE  (no I/O — unit-tested)
# --------------------------------------------------------------------------

def item_key(subjob_id: str) -> str:
    """Correlate the same logical item across stages.

    Stages carry an item under different subjob_ids (``shard_03/a/b/CLIP.npz``
    in reduce30, ``CLIP.npz`` in slerp, ``high-CLIP.npz`` in dvq). The stable
    key is the clip stem: basename, drop a leading ``<res>-`` fan-out prefix,
    drop the extension.
    """
    name = PurePosixPath(str(subjob_id)).name
    # strip a fan-out prefix like "high-", "low-", "mid-" (res tags)
    if "-" in name:
        head, rest = name.split("-", 1)
        if head and rest and head.isalnum() and len(head) <= 8:
            name = rest
    if name.endswith(".npz"):
        name = name[:-4]
    return name


@dataclass(frozen=True)
class Edge:
    """A typed dependency from ``upstream`` stage into ``downstream`` stage."""
    upstream: str
    downstream: str
    kind: str                      # "each" | "quorum" | "all" | "artifact"
    k: int = 0                     # for kind == "quorum"
    artifact: tuple[str, ...] = () # for kind == "artifact": paths that must exist

    def __post_init__(self) -> None:
        if self.kind not in ("each", "quorum", "all", "artifact"):
            raise ValueError(f"unknown edge kind {self.kind!r}")
        if self.kind == "quorum" and self.k <= 0:
            raise ValueError("quorum edge needs k > 0")


def resolve_each(up_done_keys: set[str], down_have_keys: set[str]) -> list[str]:
    """Items whose downstream gig should be seeded now (upstream done, not yet
    present downstream). Sorted for deterministic seeding."""
    return sorted(up_done_keys - down_have_keys)


def quorum_met(up_done: int, up_total: int, kind: str, k: int) -> bool:
    """Has the barrier for a ``quorum``/``all`` edge been reached?"""
    if kind == "all":
        return up_total > 0 and up_done >= up_total
    if kind == "quorum":
        return up_done >= k
    raise ValueError(f"quorum_met called on non-barrier kind {kind!r}")


def artifacts_ready(paths: tuple[str, ...]) -> bool:
    """True once every gated artifact exists (used by ``artifact`` edges)."""
    return bool(paths) and all(os.path.exists(p) for p in paths)


def render_spec(template: dict[str, Any], stem: str) -> dict[str, Any]:
    """Fill a downstream gig-spec template for one item.

    Any string value containing ``{stem}`` / ``{clip}`` is substituted.
    ``{clip}`` expands to ``<stem>.npz``; ``{stem}`` to the bare stem.
    """
    clip = f"{stem}.npz"
    out: dict[str, Any] = {}
    for key, val in template.items():
        if isinstance(val, str):
            out[key] = val.replace("{stem}", stem).replace("{clip}", clip)
        else:
            out[key] = val
    return out


def build_gigs(stems: list[str], subjob_id_template: str, spec_template: dict[str, Any],
               ) -> list[dict[str, Any]]:
    """Turn a list of item stems into concrete gigs for one downstream stage."""
    gigs = []
    for stem in stems:
        jid = subjob_id_template.replace("{stem}", stem).replace("{clip}", f"{stem}.npz")
        gigs.append({"subjob_id": jid, "spec": render_spec(spec_template, stem)})
    return gigs


# --------------------------------------------------------------------------
# SPEC MODEL
# --------------------------------------------------------------------------

@dataclass
class Stage:
    name: str
    fixer: str                              # base URL of the Fixer hosting this stage
    job: str                              # campaign job slug on that Fixer
    task: Optional[str] = None              # task ident (informational; runners bind it)
    label: str = ""
    # For gig-producing stages: how to shape a downstream gig.
    subjob_id_template: str = "{clip}"
    spec_template: dict[str, Any] = field(default_factory=dict)
    # For a barrier/reduce stage: a shell command to run once the quorum trips.
    command: Optional[list[str]] = None
    produces: tuple[str, ...] = ()          # artifact paths the command writes


@dataclass
class Pipeline:
    stages: dict[str, Stage]
    edges: list[Edge]
    token: str = ""
    poll_s: int = 60

    @staticmethod
    def from_toml(path: str) -> "Pipeline":
        if tomllib is None:  # pragma: no cover
            raise RuntimeError("tomllib unavailable (need Python 3.11+)")
        with open(path, "rb") as fh:
            doc = tomllib.load(fh)
        stages: dict[str, Stage] = {}
        for name, s in (doc.get("stages") or {}).items():
            stages[name] = Stage(
                name=name,
                fixer=s["fixer"],
                job=s["job"],
                task=s.get("task"),
                label=s.get("label", ""),
                subjob_id_template=s.get("subjob_id_template", "{clip}"),
                spec_template=s.get("spec", {}),
                command=s.get("command"),
                produces=tuple(s.get("produces", [])),
            )
        edges: list[Edge] = []
        for e in (doc.get("edges") or []):
            edges.append(Edge(
                upstream=e["from"], downstream=e["to"], kind=e["kind"],
                k=int(e.get("k", 0)), artifact=tuple(e.get("artifact", [])),
            ))
        p = doc.get("pipeline", {})
        return Pipeline(stages=stages, edges=edges,
                        token=p.get("token", ""), poll_s=int(p.get("poll_s", 60)))


# --------------------------------------------------------------------------
# COORDINATOR  (thin HTTP + loop shell)
# --------------------------------------------------------------------------

class PipelineCoordinator:
    """Applies the declared edges each tick. HTTP-only against Fixers; the
    barrier ``command`` runs locally (e.g. an ssh to a build host)."""

    def __init__(self, pipe: Pipeline, log: Callable[[str], None] = print,
                 http_get: Optional[Callable] = None,
                 http_post: Optional[Callable] = None,
                 runner: Optional[Callable] = None):
        self.pipe = pipe
        self.log = log
        # injectable I/O for testing; default to requests + subprocess
        self._get = http_get or self._default_get
        self._post = http_post or self._default_post
        self._run = runner or self._default_run
        self._barrier_fired: set[str] = set()

    # -- default I/O impls (kept out of the pure core) --
    def _default_get(self, url: str) -> dict:
        import requests
        return requests.get(url, timeout=30).json()

    def _default_post(self, url: str, payload: dict) -> None:
        import requests
        requests.post(url, json=payload, timeout=60).raise_for_status()

    def _default_run(self, cmd: list[str]) -> int:
        return subprocess.run(cmd, timeout=3600).returncode

    # -- HTTP helpers --
    def _done_keys(self, stage: Stage) -> set[str]:
        url = (f"{stage.fixer}/metrics/export?job={stage.job}"
               f"&state=done&limit=300000&token={self.pipe.token}")
        rows = self._get(url).get("rows", [])
        return {item_key(r["subjob_id"]) for r in rows}

    def _have_keys(self, stage: Stage) -> set[str]:
        # everything already seeded downstream, any state
        url = (f"{stage.fixer}/metrics/export?job={stage.job}"
               f"&state=pending,leased,done,failed&limit=300000&token={self.pipe.token}")
        rows = self._get(url).get("rows", [])
        return {item_key(r["subjob_id"]) for r in rows}

    def _done_count(self, stage: Stage) -> int:
        url = f"{stage.fixer}/status?token={self.pipe.token}&job={stage.job}"
        try:
            return int(self._get(url).get("done", 0))
        except Exception:
            return len(self._done_keys(stage))

    def _seed(self, stage: Stage, gigs: list[dict]) -> None:
        url = f"{stage.fixer}/seed?token={self.pipe.token}"
        BATCH = 1000
        for i in range(0, len(gigs), BATCH):
            self._post(url, {"gigs": gigs[i:i+BATCH],
                             "job": stage.job, "label": stage.label})

    # -- one tick over all edges --
    def tick(self) -> None:
        st = self.pipe.stages
        for edge in self.pipe.edges:
            up, down = st[edge.upstream], st[edge.downstream]
            try:
                if edge.kind in ("each",):
                    self._tick_each(edge, up, down)
                elif edge.kind in ("quorum", "all"):
                    self._tick_barrier(edge, up, down)
                elif edge.kind == "artifact":
                    # gate handled inside _tick_each of the *feeding* edge via
                    # artifacts_ready; a standalone artifact edge just logs.
                    if not artifacts_ready(edge.artifact):
                        self.log(f"gate {up.name}->{down.name}: waiting on {edge.artifact}")
            except Exception as exc:  # never let one edge kill the loop
                self.log(f"edge {edge.upstream}->{edge.downstream} error: {exc!r}")

    def _blocking_artifacts(self, downstream: str) -> tuple[str, ...]:
        for e in self.pipe.edges:
            if e.downstream == downstream and e.kind == "artifact":
                return e.artifact
        return ()

    def _tick_each(self, edge: Edge, up: Stage, down: Stage) -> None:
        gate = self._blocking_artifacts(down.name)
        if gate and not artifacts_ready(gate):
            self.log(f"A {up.name}->{down.name}: gated on artifacts {gate}")
            return
        up_done = self._done_keys(up)
        down_have = self._have_keys(down)
        stems = resolve_each(up_done, down_have)
        if not stems:
            self.log(f"A {up.name}->{down.name}: idle (up_done={len(up_done)})")
            return
        gigs = build_gigs(stems, down.subjob_id_template, down.spec_template)
        self._seed(down, gigs)
        self.log(f"A {up.name}->{down.name}: seeded {len(gigs)}")

    def _tick_barrier(self, edge: Edge, up: Stage, down: Stage) -> None:
        if down.name in self._barrier_fired or artifacts_ready(down.produces):
            return
        up_done = self._done_count(up)
        up_total = 0
        if edge.kind == "all":
            try:
                url = f"{up.fixer}/status?token={self.pipe.token}&job={up.job}"
                up_total = int(self._get(url).get("total", 0))
            except Exception:
                up_total = 0
        if not quorum_met(up_done, up_total, edge.kind, edge.k):
            self.log(f"B {up.name}->{down.name}: waiting (done={up_done} k={edge.k})")
            return
        if down.command:
            self.log(f"B {up.name}->{down.name}: quorum met -> running {down.name} command")
            rc = self._run(down.command)
            # success = clean exit AND (if artifacts declared) they now exist.
            ok = (rc == 0) and (artifacts_ready(down.produces) if down.produces else True)
            if ok:
                self._barrier_fired.add(down.name)
                self.log(f"B {down.name}: complete"
                         + (f" -> {down.produces}" if down.produces else ""))
            else:
                self.log(f"B {down.name}: command rc={rc}; will retry next tick")

    def run(self) -> None:
        self.log(f"pipeline coordinator start: {len(self.pipe.stages)} stages, "
                 f"{len(self.pipe.edges)} edges, poll={self.pipe.poll_s}s")
        while True:
            try:
                self.tick()
            except Exception as exc:
                self.log(f"tick error: {exc!r}")
            time.sleep(self.pipe.poll_s)
