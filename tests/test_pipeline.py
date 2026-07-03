"""Tests for kiroshi.pipeline — the typed-edge pipeline primitive.

Focus on the PURE core (item correlation + edge resolution) plus the
coordinator's edge dispatch with injected fake I/O (no network, no sqlite).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.pipeline import (  # noqa: E402
    item_key, Edge, resolve_each, quorum_met, artifacts_ready,
    render_spec, build_gigs, Stage, Pipeline, PipelineCoordinator,
)


# ---- item_key: correlate the same clip across stages --------------------

def test_item_key_strips_shard_prefix_and_ext():
    assert item_key("shard_03/naturalistic/train/0/1/CLIP.npz") == "CLIP"

def test_item_key_strips_flat_ext():
    assert item_key("CLIP.npz") == "CLIP"

def test_item_key_strips_resolution_fanout_prefix():
    # dvq fans out as "<res>-<clip>.npz"
    assert item_key("high-CLIP.npz") == "CLIP"
    assert item_key("low-CLIP.npz") == "CLIP"
    assert item_key("mid-CLIP.npz") == "CLIP"

def test_item_key_keeps_hyphenated_clip_names():
    # a long hyphen segment is part of the name, not a res tag
    assert item_key("V00_S0001-take2.npz") == "V00_S0001-take2"


# ---- resolve_each: per-item fan-through ---------------------------------

def test_resolve_each_seeds_only_new_done_items():
    up = {"a", "b", "c"}
    have = {"a"}
    assert resolve_each(up, have) == ["b", "c"]

def test_resolve_each_empty_when_downstream_caught_up():
    assert resolve_each({"a", "b"}, {"a", "b", "x"}) == []

def test_resolve_each_is_sorted_deterministic():
    assert resolve_each({"z", "a", "m"}, set()) == ["a", "m", "z"]


# ---- quorum / all barriers ----------------------------------------------

def test_quorum_met_threshold():
    assert quorum_met(4000, 0, "quorum", 4000) is True
    assert quorum_met(3999, 0, "quorum", 4000) is False

def test_all_needs_full_count():
    assert quorum_met(100, 100, "all", 0) is True
    assert quorum_met(99, 100, "all", 0) is False
    assert quorum_met(0, 0, "all", 0) is False   # nothing seeded yet


# ---- artifact gate ------------------------------------------------------

def test_artifacts_ready(tmp_path=None):
    import tempfile, os
    d = tempfile.mkdtemp()
    p = os.path.join(d, "cb.npz")
    assert artifacts_ready((p,)) is False
    open(p, "wb").close()
    assert artifacts_ready((p,)) is True
    assert artifacts_ready(()) is False          # empty gate is never "ready"


# ---- spec rendering + gig building --------------------------------------

def test_render_spec_substitutes_stem_and_clip():
    tpl = {"src_path": "{clip}", "dst_path": "out/{clip}", "root": "R", "n": 3}
    got = render_spec(tpl, "CLIP")
    assert got == {"src_path": "CLIP.npz", "dst_path": "out/CLIP.npz", "root": "R", "n": 3}

def test_build_gigs_shapes_job_ids():
    gigs = build_gigs(["A", "B"], "{clip}", {"src_path": "{clip}"})
    assert gigs == [
        {"job_id": "A.npz", "spec": {"src_path": "A.npz"}},
        {"job_id": "B.npz", "spec": {"src_path": "B.npz"}},
    ]

def test_build_gigs_resolution_fanout_prefix():
    gigs = build_gigs(["A"], "high-{clip}", {"src_path": "{clip}", "res": "high"})
    assert gigs[0]["job_id"] == "high-A.npz"
    assert gigs[0]["spec"]["src_path"] == "A.npz"


# ---- Edge validation ----------------------------------------------------

def test_edge_rejects_unknown_kind():
    try:
        Edge("a", "b", "sometimes")
        assert False, "should have raised"
    except ValueError:
        pass

def test_quorum_edge_requires_k():
    try:
        Edge("a", "b", "quorum", k=0)
        assert False
    except ValueError:
        pass


# ---- Coordinator dispatch with fake I/O ---------------------------------

class _FakeMesh:
    """In-memory stand-in for Fixers: tracks done-sets + seeded gigs."""
    def __init__(self, done):
        self.done = done                 # {group: set(job_id)}
        self.seeded: dict[str, list] = {}  # {group: [gigs]}
        self.ran: list[list[str]] = []

    def get(self, url):
        # parse grp + state out of the query
        grp = url.split("grp=")[1].split("&")[0]
        if "/status" in url:
            return {"done": len(self.done.get(grp, set())), "total": 999999}
        # /metrics/export
        if "state=done" in url:
            ids = self.done.get(grp, set())
        else:
            ids = set(g["job_id"] for g in self.seeded.get(grp, []))
        return {"rows": [{"job_id": j} for j in ids]}

    def post(self, url, payload):
        grp = payload["group"]
        self.seeded.setdefault(grp, []).extend(payload["gigs"])

    def run(self, cmd):
        self.ran.append(cmd)
        return 0


def _pipe():
    stages = {
        "reduce30": Stage("reduce30", "http://f", "g_reduce"),
        "slerp": Stage("slerp", "http://f", "g_slerp",
                       job_id_template="{clip}",
                       spec_template={"src_path": "{clip}", "dst_path": "s/{clip}"}),
    }
    edges = [Edge("reduce30", "slerp", "each")]
    return Pipeline(stages=stages, edges=edges, token="T", poll_s=1)


def test_coordinator_each_seeds_downstream_delta():
    mesh = _FakeMesh(done={"g_reduce": {"shard_01/x/A.npz", "shard_02/y/B.npz"}})
    pipe = _pipe()
    coord = PipelineCoordinator(pipe, log=lambda m: None,
                                http_get=mesh.get, http_post=mesh.post, runner=mesh.run)
    coord.tick()
    seeded = {g["job_id"] for g in mesh.seeded["g_slerp"]}
    assert seeded == {"A.npz", "B.npz"}
    # second tick: nothing new (downstream now has both) -> no dup growth
    # (simulate the fixer dedup by feeding seeded back as "have")
    coord.tick()
    assert len(mesh.seeded["g_slerp"]) == 2


def test_coordinator_barrier_runs_command_once_when_quorum_met():
    stages = {
        "reduce30": Stage("reduce30", "http://f", "g_reduce"),
        "cb": Stage("cb", "http://f", "g_cb", command=["build"], produces=()),
    }
    edges = [Edge("reduce30", "cb", "quorum", k=3)]
    pipe = Pipeline(stages=stages, edges=edges, token="T", poll_s=1)
    # only 2 done -> below quorum
    mesh = _FakeMesh(done={"g_reduce": {"A", "B"}})
    coord = PipelineCoordinator(pipe, log=lambda m: None,
                                http_get=mesh.get, http_post=mesh.post, runner=mesh.run)
    coord.tick()
    assert mesh.ran == []                # quorum not met
    # now 3 done -> quorum met -> command runs once
    mesh.done["g_reduce"] = {"A", "B", "C"}
    coord.tick()
    assert mesh.ran == [["build"]]
    coord.tick()                          # already fired -> not re-run
    assert mesh.ran == [["build"]]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fail = 0
    for t in tests:
        try:
            t(); print(f"PASS  {t.__name__}")
        except Exception as exc:
            print(f"FAIL  {t.__name__}: {exc!r}"); fail += 1
    print(f"\n{len(tests)-fail}/{len(tests)} passed")
    sys.exit(fail)
