"""Verify the new export_metrics + list_jobs(job) filter in jobstore."""
import sys, tempfile, os
sys.path.insert(0, "src")
from kiroshi.jobstore import JobStore

d = tempfile.mkdtemp()
s = JobStore(os.path.join(d, "t.db"))
# seed two groups
s.seed([{"subjob_id": "a/1", "spec": {}}, {"subjob_id": "a/2", "spec": {}}, {"subjob_id": "a/3", "spec": {}}],
       job="grpA")
s.seed([{"subjob_id": "b/1", "spec": {}}, {"subjob_id": "b/2", "spec": {}}], job="grpB")

# lease + complete all of grpA with metrics
lease = s.lease(runner_id="r1", host="h", capacity=10, ttl=60.0)
ids = [g["subjob_id"] for g in lease.gigs]
s.complete([{"subjob_id": j, "status": "ok",
             "metrics": {"worst_planar_mm": float(i), "worst_planar_section": [i, i+10]}}
            for i, j in enumerate(ids)])

# list_jobs with job filter
a_jobs = s.list_jobs(job="grpA")
b_jobs = s.list_jobs(job="grpB")
assert all(j["job"] == "grpA" for j in a_jobs), "grpA filter leaked"
assert all(j["job"] == "grpB" for j in b_jobs), "grpB filter leaked"
assert len(a_jobs) == 3 and len(b_jobs) == 2, f"counts wrong: {len(a_jobs)},{len(b_jobs)}"
print(f"OK list_jobs(job): grpA={len(a_jobs)} grpB={len(b_jobs)}")

# export_metrics for grpA
exp = s.export_metrics(job="grpA")
assert exp[0]["state"] == "done" and "metrics" in exp[0]
assert len(exp) == 3, f"export count {len(exp)}"
# ordered by subjob_id
assert [r["subjob_id"] for r in exp] == sorted(r["subjob_id"] for r in exp), "not ordered"
worst = max(exp, key=lambda r: r["metrics"]["worst_planar_mm"])
print(f"OK export_metrics(grpA): {len(exp)} rows, worst={worst['subjob_id']} mm={worst['metrics']['worst_planar_mm']}")
print("PASS: export_metrics + job filter work")
