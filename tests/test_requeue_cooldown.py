"""Anti-thrash cooldown: a host that requeues a gig (e.g. it structurally
cannot complete it -- a GPU generation that can't decode a given codec)
must not be able to immediately re-lease that exact gig in a tight loop.
Reproduces, then fixes, a live-observed pathology: the same subjob_id was
leased-and-requeued by one host 16 times in ~70 seconds, because nothing
stopped it from re-grabbing what it just gave back before another host got
a turn.

The cooldown is host-scoped only: a DIFFERENT host must be able to lease the
gig immediately, with zero delay -- this is what actually "routes" the gig
elsewhere, not the cooldown itself.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiroshi.jobstore import JobStore  # noqa: E402


def _store(**kw) -> JobStore:
    return JobStore(":memory:", max_retries=3, **kw)


class TestCooldownUnitLevel:
    def test_same_host_cannot_immediately_release_after_requeue(self):
        store = _store()
        store.seed([{"subjob_id": "g1", "spec": {}}])
        res = store.lease("r1", "hostA", capacity=1, ttl=60)
        assert [g["subjob_id"] for g in res.gigs] == ["g1"]

        store.complete([{"subjob_id": "g1", "status": "requeue", "metrics": {}}])

        # Same host, immediately again: must NOT get g1 back (only candidate
        # is g1, so an empty grant proves it's excluded, not just unlucky).
        res2 = store.lease("r1", "hostA", capacity=1, ttl=60)
        assert res2.gigs == []

    def test_different_host_gets_it_immediately_no_delay(self):
        store = _store()
        store.seed([{"subjob_id": "g1", "spec": {}}])
        store.lease("r1", "hostA", capacity=1, ttl=60)
        store.complete([{"subjob_id": "g1", "status": "requeue", "metrics": {}}])

        # hostB is NOT on cooldown for g1 -- must get it right away. This is
        # the actual "routes to a capable host" mechanism: the cooldown only
        # ever restricts the host that just gave it back, never anyone else.
        res = store.lease("r2", "hostB", capacity=1, ttl=60)
        assert [g["subjob_id"] for g in res.gigs] == ["g1"]

    def test_cooldown_expires_and_original_host_can_lease_again(self):
        store = _store()
        store.REQUEUE_COOLDOWN_S = 0.05  # short, for a fast test
        store.seed([{"subjob_id": "g1", "spec": {}}])
        store.lease("r1", "hostA", capacity=1, ttl=60)
        store.complete([{"subjob_id": "g1", "status": "requeue", "metrics": {}}])

        assert store.lease("r1", "hostA", capacity=1, ttl=60).gigs == []
        time.sleep(0.1)
        res = store.lease("r1", "hostA", capacity=1, ttl=60)
        assert [g["subjob_id"] for g in res.gigs] == ["g1"]

    def test_repro_of_the_live_pathology_is_now_broken(self):
        """The exact scenario observed live: a single host with no
        competing pollers, hammering /lease on a queue of two gigs it can
        never complete. Before the fix this looped forever, leasing the same
        pair every call. After the fix, once both are on cooldown, this same
        host gets nothing back -- proving the loop is broken, not just
        slowed down."""
        store = _store()
        store.seed([
            {"subjob_id": "g1", "spec": {}},
            {"subjob_id": "g2", "spec": {}},
        ])
        for _ in range(16):
            res = store.lease("r1", "hostA", capacity=2, ttl=60)
            if not res.gigs:
                break
            store.complete([
                {"subjob_id": g["subjob_id"], "status": "requeue", "metrics": {}}
                for g in res.gigs
            ])
        # Must have stopped getting work well before 16 iterations (the
        # observed live loop count) -- both gigs go on cooldown after their
        # first requeue, so hostA is starved out within 1-2 rounds.
        final = store.lease("r1", "hostA", capacity=2, ttl=60)
        assert final.gigs == []

    def test_attempts_not_burned_by_requeue_even_with_cooldown(self):
        """The cooldown is purely a lease-selection filter; it must not
        change requeue's existing "free, no retry consumed" guarantee."""
        store = _store()
        store.seed([{"subjob_id": "g1", "spec": {}}])
        store.lease("r1", "hostA", capacity=1, ttl=60)
        store.complete([{"subjob_id": "g1", "status": "requeue", "metrics": {}}])
        row = store.job("g1")
        assert row["attempts"] == 0
        assert row["state"] == "pending"

    def test_cooldown_never_blocks_a_normal_successful_gig(self):
        """Sanity: a gig that's never been requeued behaves exactly as
        before -- the cooldown dict starts empty and only ever grows from
        actual requeue events."""
        store = _store()
        store.seed([{"subjob_id": "g1", "spec": {}}])
        res = store.lease("r1", "hostA", capacity=1, ttl=60)
        assert [g["subjob_id"] for g in res.gigs] == ["g1"]
        store.complete([{"subjob_id": "g1", "status": "ok", "metrics": {}}])
        assert store.job("g1")["state"] == "done"

    def test_prune_removes_expired_entries(self):
        store = _store()
        store.REQUEUE_COOLDOWN_S = 0.05
        # Lease g1 and g2 together so both have a real host on their row
        # before either is requeued (avoids the ordering trap: once g1's
        # cooldown expires it becomes leasable again and, being older, wins
        # FIFO ordering over a fresh g2 -- lease deterministically instead
        # of relying on order across two separate lease() calls).
        store.seed([{"subjob_id": "g1", "spec": {}}, {"subjob_id": "g2", "spec": {}}])
        store.lease("r1", "hostA", capacity=2, ttl=60)
        store.complete([{"subjob_id": "g1", "status": "requeue", "metrics": {}}])
        assert "hostA:g1" in store._requeue_cooldowns
        time.sleep(0.1)  # let g1's entry expire
        # g2 is still leased (never completed); requeue it now -- this is
        # the "any subsequent requeue event" that should sweep g1's entry.
        store.complete([{"subjob_id": "g2", "status": "requeue", "metrics": {}}])
        assert "hostA:g1" not in store._requeue_cooldowns  # pruned
        assert "hostA:g2" in store._requeue_cooldowns  # fresh, not expired


class TestCooldownDiskAwarePath:
    """held-frames-4fps (the job that hit this live) uses the disk-concurrency
    branch of lease() -- a separate code path from the plain one above, with
    its own row-selection logic. Must be covered independently; a fix that
    only worked on the plain path wouldn't have fixed the actual campaign."""

    def test_same_host_excluded_under_disk_concurrency(self, tmp_path):
        store = JobStore(str(tmp_path / "d.db"), max_retries=3)
        store.seed([{"subjob_id": "g1", "spec": {}, "disk": "d1"}])
        budget = {"d1": 5}
        res = store.lease("r1", "hostA", capacity=1, ttl=60, disk_concurrency=budget)
        assert [g["subjob_id"] for g in res.gigs] == ["g1"]

        store.complete([{"subjob_id": "g1", "status": "requeue", "metrics": {}}])

        res2 = store.lease("r1", "hostA", capacity=1, ttl=60, disk_concurrency=budget)
        assert res2.gigs == []

    def test_different_host_gets_it_immediately_under_disk_concurrency(self, tmp_path):
        store = JobStore(str(tmp_path / "d2.db"), max_retries=3)
        store.seed([{"subjob_id": "g1", "spec": {}, "disk": "d1"}])
        budget = {"d1": 5}
        store.lease("r1", "hostA", capacity=1, ttl=60, disk_concurrency=budget)
        store.complete([{"subjob_id": "g1", "status": "requeue", "metrics": {}}])

        res = store.lease("r2", "hostB", capacity=1, ttl=60, disk_concurrency=budget)
        assert [g["subjob_id"] for g in res.gigs] == ["g1"]

    def test_cooldown_does_not_break_per_disk_budget_accounting(self, tmp_path):
        """The excluded gig must not phantom-count against the disk budget
        it's no longer a candidate for -- other pending work on the same
        disk should still lease normally up to budget."""
        store = JobStore(str(tmp_path / "d3.db"), max_retries=3)
        store.seed([
            {"subjob_id": "g1", "spec": {}, "disk": "d1"},
            {"subjob_id": "g2", "spec": {}, "disk": "d1"},
        ])
        budget = {"d1": 5}
        store.lease("r1", "hostA", capacity=1, ttl=60, disk_concurrency=budget)  # takes g1
        store.complete([{"subjob_id": "g1", "status": "requeue", "metrics": {}}])

        # hostA is now excluded from g1 but g2 is untouched and still under
        # budget -- must be leasable normally.
        res = store.lease("r1", "hostA", capacity=2, ttl=60, disk_concurrency=budget)
        assert [g["subjob_id"] for g in res.gigs] == ["g2"]


class TestCooldownHttpLevel:
    """Same behavior through the real /lease and /complete HTTP endpoints,
    matching the style of test_lease.py."""

    @staticmethod
    def _client(**kw):
        from fastapi.testclient import TestClient
        from kiroshi.coordinator import create_app
        app = create_app(JobStore(":memory:", max_retries=3), token=None, **kw)
        return TestClient(app)

    def test_http_requeue_then_release_excluded_for_same_host(self):
        with self._client() as c:
            c.post("/seed", json={"gigs": [{"subjob_id": "g1", "spec": {}}]})
            r = c.post("/lease", json={"runner_id": "r1", "host": "hostA", "capacity": 1})
            lease_id = r.json()["lease_id"]
            assert [g["subjob_id"] for g in r.json()["gigs"]] == ["g1"]

            c.post("/complete", json={
                "lease_id": lease_id,
                "results": [{"subjob_id": "g1", "status": "requeue"}],
            })

            r2 = c.post("/lease", json={"runner_id": "r1", "host": "hostA", "capacity": 1})
            assert r2.json()["gigs"] == []

            r3 = c.post("/lease", json={"runner_id": "r2", "host": "hostB", "capacity": 1})
            assert [g["subjob_id"] for g in r3.json()["gigs"]] == ["g1"]
