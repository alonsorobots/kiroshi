"""``kiroshi nas`` — assess + benchmark storage topology (PLAN §7.6, M8 N4/N5).

Two read-mostly tools that take the guesswork out of shard-aware scheduling:

* **assess** — walks a dataset root and reports balance: bytes/files per shard,
  skew, and whether shards map cleanly to declared disks. Tells you whether a
  layout will actually parallelize *before* you run anything. Read-only.
* **benchmark** — measures per-disk read throughput at increasing concurrency
  levels, finds the thrash knee (where over-parallelizing one HDD goes *slower*),
  and recommends ``concurrency`` per disk. The empirical number behind the budget.

Both use ``os.scandir`` (3-10x faster than ``Path.iterdir`` on Windows) and
``kfs.walk`` for SMB roots, so they work over the same data plane the runners use.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from . import kfs
from .storage import DiskConfig, match_disk


# --------------------------------------------------------------- assess
def _shard_of(rel: str, depth: int) -> str:
    """The shard key for a file: the first ``depth`` path components."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    return "/".join(parts[:depth]) if parts else "(root)"


def assess_layout(root: str, *, depth: int = 1, pattern: Optional[str] = None,
                  disks: Optional[list[DiskConfig]] = None) -> dict[str, Any]:
    """Walk ``root`` (local or SMB via kfs) and report per-shard balance + a
    throughput-readiness verdict.

    With ``pattern`` (a glob like ``*.npz``) only matching files are counted and a
    format check is included. With ``disks`` (the topology) per-disk byte/file
    distribution is computed and a readiness verdict flags issues that would stop
    sharded I/O from maximizing throughput: data concentrated on one spindle,
    unmatched shards, severe skew, or a format mismatch. Read-only — never writes.
    """
    shards: dict[str, dict[str, int]] = {}
    total_bytes = 0
    total_files = 0
    total_all_files = 0  # for the format check (counted regardless of pattern)
    base = str(root).rstrip("/\\")

    for dirpath, _dirs, files in kfs.walk(root):
        rel_dir = dirpath[len(base):].lstrip("\\/") if dirpath.startswith(base) \
            else dirpath
        for fname in files:
            total_all_files += 1
            if pattern and not fnmatch.fnmatch(fname, pattern):
                continue
            full = os.path.join(dirpath, fname)
            try:
                size = os.path.getsize(full) if not kfs.is_unc(full) \
                    else _smb_size(full)
            except OSError:
                continue
            shard = _shard_of(os.path.join(rel_dir, fname), depth)
            s = shards.setdefault(shard, {"bytes": 0, "files": 0})
            s["bytes"] += size
            s["files"] += 1
            total_bytes += size
            total_files += 1

    if not shards:
        issues = ["no files found"]
        if pattern and total_all_files > 0:
            issues = [f"No files match pattern '{pattern}' — wrong root or format? "
                      f"({total_all_files} files exist but none match)"]
        return {"shards": {}, "total_bytes": 0, "total_files": 0,
                "total_all_files": total_all_files, "skew_ratio": 0.0,
                "verdict": "empty", "unmatched_shards": [],
                "disk_coverage": {}, "disk_distribution": {},
                "pattern": pattern, "readiness": {"ok": False, "issues": issues}}

    sizes = [s["bytes"] for s in shards.values()]
    skew = (max(sizes) / min(sizes)) if min(sizes) > 0 else float("inf")

    if skew <= 1.25:
        verdict = "well-balanced"
    elif skew <= 2.0:
        verdict = "slightly-skewed"
    else:
        verdict = "skewed"

    # Per-disk distribution: bytes + files per spindle (the throughput-readiness core).
    unmatched: list[str] = []
    coverage: dict[str, int] = {}
    disk_dist: dict[str, dict[str, int]] = {}
    if disks:
        for shard_name, sdata in shards.items():
            disk_id = None
            for d in disks:
                if match_disk(shard_name, d.match):
                    disk_id = d.id
                    break
            if disk_id:
                coverage[disk_id] = coverage.get(disk_id, 0) + 1
                dd = disk_dist.setdefault(disk_id, {"bytes": 0, "files": 0})
                dd["bytes"] += sdata["bytes"]
                dd["files"] += sdata["files"]
            else:
                unmatched.append(shard_name)

    readiness = _readiness(
        pattern, total_files, total_all_files, shards, total_bytes, skew,
        disks, disk_dist, unmatched)

    return {"shards": shards, "total_bytes": total_bytes, "total_files": total_files,
            "total_all_files": total_all_files, "skew_ratio": skew, "verdict": verdict,
            "unmatched_shards": unmatched, "disk_coverage": coverage,
            "disk_distribution": disk_dist, "pattern": pattern,
            "readiness": readiness}


def _readiness(pattern, total_files, total_all_files, shards, total_bytes, skew,
               disks, disk_dist, unmatched) -> dict[str, Any]:
    """Combine all checks into a throughput-readiness verdict with actionable issues."""
    checks: list[dict[str, Any]] = []
    issues: list[str] = []

    # 1. FORMAT — do the files match the expected pattern?
    if pattern:
        if total_files == 0:
            checks.append({"name": "format", "ok": False,
                           "detail": f"0 files match '{pattern}' (of {total_all_files} total)"})
            issues.append(f"No files match pattern '{pattern}' — wrong root or format?")
        else:
            pct = 100.0 * total_files / total_all_files if total_all_files else 100.0
            ok = pct >= 90.0
            checks.append({"name": "format", "ok": ok,
                           "detail": f"{total_files}/{total_all_files} files match '{pattern}' ({pct:.0f}%)"})
            if not ok:
                issues.append(f"Only {pct:.0f}% of files match '{pattern}' — non-matching "
                              f"files will be ignored by the task.")

    # 2. DISTRIBUTION — is data actually spread across spindles?
    if disks and disk_dist:
        ndisks_with_data = len(disk_dist)
        ndisks_total = len(disks)
        if ndisks_with_data == 1:
            checks.append({"name": "distribution", "ok": False,
                           "detail": f"all data on 1 of {ndisks_total} disk(s)"})
            issues.append("All data is on a single disk — only 1 spindle will be busy. "
                          "Distribute it: `kiroshi nas shard <root> --disks ...`")
        else:
            # concentration: does one disk hold a disproportionate share?
            max_share = max((d["bytes"] for d in disk_dist.values()), default=0)
            max_pct = 100.0 * max_share / total_bytes if total_bytes else 0
            the_disk = next((did for did, d in disk_dist.items()
                             if d["bytes"] == max_share), "?")
            ok = max_pct <= 60.0
            checks.append({"name": "distribution", "ok": ok,
                           "detail": f"data across {ndisks_with_data}/{ndisks_total} disks; "
                                     f"largest={the_disk} at {max_pct:.0f}%"})
            if not ok:
                issues.append(f"Disk '{the_disk}' holds {max_pct:.0f}% of the data — it "
                              f"will be the bottleneck. Rebalance: "
                              f"`kiroshi nas shard <root> --rebalance`")

    # 3. COVERAGE — do all shards map to a declared disk?
    if disks is not None:
        if unmatched:
            ok = False
            n = len(unmatched)
            sample = ", ".join(unmatched[:5]) + (" ..." if n > 5 else "")
            checks.append({"name": "coverage", "ok": ok,
                           "detail": f"{n} shard(s) match no disk: {sample}"})
            issues.append(f"{n} shard(s) match no disk (will be uncapped): {sample}. "
                          f"Fix the match rules or move the data.")
        else:
            checks.append({"name": "coverage", "ok": True,
                           "detail": "all shards map to a disk"})

    # 4. BALANCE — is the per-shard skew manageable?
    ok = skew <= 3.0
    checks.append({"name": "balance", "ok": ok,
                   "detail": f"shard skew {skew:.2f}:1"})
    if not ok:
        issues.append(f"Shard skew is {skew:.1f}:1 — one shard dominates and its disk "
                      f"finishes long after the rest. Rebalance: "
                      f"`kiroshi nas shard <root> --rebalance`")

    return {"ok": len(issues) == 0, "issues": issues, "checks": checks}


def _smb_size(path: str) -> int:
    try:
        st = kfs._smbclient().path.stat_info(kfs._to_unc(path))  # type: ignore[attr-defined]
        return int(st.file_size) if hasattr(st, "file_size") else 0
    except Exception:  # noqa: BLE001
        return 0


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def print_assessment(report: dict[str, Any], disks: Optional[list[DiskConfig]]) -> None:
    shards = report["shards"]
    if not shards:
        print("[assess] no files found under the root.", flush=True)
        if report.get("pattern") and report.get("total_all_files", 0) > 0:
            print(f"  ({report['total_all_files']} files exist but none match "
                  f"pattern '{report['pattern']}')", flush=True)
        return
    pat = f" pattern={report['pattern']}" if report.get("pattern") else ""
    print(f"[assess] {report['total_files']} files, {_fmt_bytes(report['total_bytes'])} "
          f"across {len(shards)} shard(s){pat}  ->  {report['verdict']} "
          f"(skew {report['skew_ratio']:.2f}:1)", flush=True)
    print(flush=True)

    # --- per-shard table ---
    print(f"  {'shard':<28} {'files':>8} {'bytes':>12} {'%total':>7}", flush=True)
    print(f"  {'-' * 28} {'-' * 8} {'-' * 12} {'-' * 7}", flush=True)
    for name in sorted(shards, key=lambda s: shards[s]["bytes"], reverse=True):
        s = shards[name]
        pct = 100.0 * s["bytes"] / report["total_bytes"] if report["total_bytes"] else 0
        print(f"  {name:<28} {s['files']:>8} {_fmt_bytes(s['bytes']):>12} {pct:>6.1f}%",
              flush=True)

    # --- per-disk distribution (the throughput-readiness core) ---
    dist = report.get("disk_distribution", {})
    if disks and dist:
        print(flush=True)
        print("  per-disk distribution:", flush=True)
        print(f"  {'disk':<12} {'kind':<6} {'files':>8} {'bytes':>12} {'%data':>7}", flush=True)
        print(f"  {'-' * 12} {'-' * 6} {'-' * 8} {'-' * 12} {'-' * 7}", flush=True)
        for d in disks:
            dd = dist.get(d.id)
            if dd:
                pct = 100.0 * dd["bytes"] / report["total_bytes"] if report["total_bytes"] else 0
                print(f"  {d.id:<12} {d.kind:<6} {dd['files']:>8} "
                      f"{_fmt_bytes(dd['bytes']):>12} {pct:>6.1f}%", flush=True)
            else:
                print(f"  {d.id:<12} {d.kind:<6} {'(no data)':>8}", flush=True)
        if report["unmatched_shards"]:
            print(f"  WARNING: {len(report['unmatched_shards'])} shard(s) match NO disk "
                  f"(will be uncapped): {', '.join(report['unmatched_shards'][:8])}"
                  + (" ..." if len(report["unmatched_shards"]) > 8 else ""), flush=True)

    # --- readiness verdict ---
    rd = report.get("readiness", {})
    checks = rd.get("checks", [])
    if checks:
        print(flush=True)
        print("  readiness checks:", flush=True)
        for c in checks:
            mark = "OK " if c["ok"] else "!! "
            print(f"    {mark} {c['name']:<14} {c['detail']}", flush=True)
    print(flush=True)
    if rd.get("ok"):
        print("  => READY for sharded throughput.", flush=True)
    else:
        print(f"  => NEEDS ATTENTION ({len(rd.get('issues', []))} issue(s)):", flush=True)
        for i, issue in enumerate(rd.get("issues", []), 1):
            print(f"     {i}. {issue}", flush=True)


# --------------------------------------------------------------- benchmark
_DEFAULT_LEVELS = (1, 2, 4, 6, 8, 12, 16)


def _read_file(path: str) -> int:
    """Read a file fully, return bytes read."""
    with kfs.open(path, "rb") as f:
        return sum(len(chunk) for chunk in iter(lambda: f.read(1024 * 1024), b""))


def _sweep_disk(read_path: str, file_path: str, levels: tuple[int, ...],
                seconds: float) -> dict[int, float]:
    """Measure aggregate read MB/s at each concurrency level for one disk."""
    results: dict[int, float] = {}
    for n in levels:
        deadline = time.time() + seconds
        total_bytes = 0
        reps = 0
        while time.time() < deadline:
            with ThreadPoolExecutor(max_workers=n) as pool:
                futs = [pool.submit(_read_file, file_path) for _ in range(n)]
                for f in futs:
                    total_bytes += f.result()
            reps += 1
        elapsed = seconds
        mbs = (total_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
        results[n] = mbs
    return results


def benchmark_disks(disks: list[DiskConfig], *, size_mb: int = 64,
                    levels: tuple[int, ...] = _DEFAULT_LEVELS,
                    seconds: float = 3.0) -> list[dict[str, Any]]:
    """Benchmark each disk's read throughput at increasing concurrency.

    Creates a temp file of ``size_mb`` on each disk's ``write`` path, reads it from
    the ``read`` path at each level, finds the peak (the thrash knee for HDDs), and
    returns ``[{disk_id, kind, results, best_concurrency, peak_mbs}]``. Cleans up
    temp files.
    """
    reports: list[dict[str, Any]] = []
    for d in disks:
        if not d.read or not d.write:
            print(f"[benchmark] {d.id}: skipping (no read or write path configured)",
                  flush=True)
            continue
        tmp_name = f"__kiroshi_bench_{d.id}.bin"
        write_path = f"{d.write.rstrip('/')}/{tmp_name}"
        read_path = f"{d.read.rstrip('/')}/{tmp_name}"
        print(f"[benchmark] {d.id} (kind={d.kind}): writing {size_mb}MB temp file...",
              flush=True)
        try:
            _write_temp(write_path, size_mb)
            print(f"[benchmark] {d.id}: sweeping concurrency {levels}...", flush=True)
            results = _sweep_disk(read_path, read_path, levels, seconds)
            best_n = max(results, key=lambda n: results[n])
            peak = results[best_n]
            reports.append({"disk_id": d.id, "kind": d.kind, "results": results,
                            "best_concurrency": best_n, "peak_mbs": peak})
        except Exception as e:  # noqa: BLE001
            print(f"[benchmark] {d.id}: FAILED ({e})", flush=True)
            reports.append({"disk_id": d.id, "kind": d.kind, "results": {},
                            "best_concurrency": None, "peak_mbs": 0, "error": str(e)})
        finally:
            try:
                kfs.remove(write_path)
            except Exception:  # noqa: BLE001
                pass
            try:
                kfs.remove(read_path)
            except Exception:  # noqa: BLE001
                pass
    return reports


def _write_temp(path: str, size_mb: int) -> None:
    chunk = b"\0" * (1024 * 1024)
    with kfs.open(path, "wb") as f:
        for _ in range(size_mb):
            f.write(chunk)


def print_benchmark(reports: list[dict[str, Any]]) -> None:
    if not reports:
        print("[benchmark] no disks benchmarked.", flush=True)
        return
    for r in reports:
        did = r["disk_id"]
        if r.get("error"):
            print(f"\n  {did} (kind={r['kind']}): ERROR — {r['error']}", flush=True)
            continue
        print(f"\n  {did} (kind={r['kind']}):", flush=True)
        print(f"    {'concurrency':>12} {'MB/s':>10} {'bar':>30}", flush=True)
        results = r["results"]
        peak = max(results.values()) if results else 1
        for n in sorted(results):
            mbs = results[n]
            barlen = int(28 * mbs / peak) if peak > 0 else 0
            marker = " <-- BEST" if n == r["best_concurrency"] else ""
            print(f"    {n:>12} {mbs:>9.1f}  {'#' * barlen}{marker}", flush=True)
        bc = r["best_concurrency"]
        print(f"    recommended concurrency = {bc}  (peak {r['peak_mbs']:.1f} MB/s)",
              flush=True)
    print(flush=True)
    print("Add to kiroshi.local.toml:", flush=True)
    for r in reports:
        if r.get("best_concurrency"):
            print(f"  # {r['disk_id']} (kind={r['kind']}): benchmarked peak at "
                  f"concurrency={r['best_concurrency']}", flush=True)


# --------------------------------------------------------------- shard (the doer)
def _collect_files(root: str) -> list[tuple[str, int]]:
    """Walk ``root`` and return ``[(rel_path, size_bytes), ...]`` (rel to root)."""
    base = str(root).rstrip("/\\")
    out: list[tuple[str, int]] = []
    for dirpath, _dirs, files in kfs.walk(root):
        for fname in files:
            full = os.path.join(dirpath, fname)
            try:
                size = os.path.getsize(full) if not kfs.is_unc(full) \
                    else _smb_size(full)
            except OSError:
                size = 0
            rel = full[len(base):].lstrip("\\/")
            out.append((rel.replace("\\", "/"), size))
    return out


def plan_shard(files: list[tuple[str, int]], n_disks: int) -> list[list[tuple[str, int]]]:
    """Greedy bin-pack: sort by size descending, place each in the least-loaded bin.

    This minimizes the max-bin / min-bin ratio (the skew) so round-robin leasing =
    balanced load across spindles. Largest-first greedy is within ~1.22x of optimal
    for bin-packing and runs in O(n log n).
    """
    sorted_files = sorted(files, key=lambda f: f[1], reverse=True)
    bins: list[list[tuple[str, int]]] = [[] for _ in range(n_disks)]
    bin_sizes = [0] * n_disks
    for rel, size in sorted_files:
        idx = min(range(n_disks), key=lambda i: bin_sizes[i])
        bins[idx].append((rel, size))
        bin_sizes[idx] += size
    return bins


def _move_file(src: str, dst: str) -> None:
    """Move a file, creating parent dirs. Uses os.rename (instant) if same FS,
    else shutil.move (copy+delete). Works for local paths; for SMB use kfs."""
    if kfs.is_unc(src) or kfs.is_unc(dst):
        # SMB: copy then delete (no atomic rename across shares)
        with kfs.open(src, "rb") as r, kfs.open(dst, "wb") as w:
            shutil.copyfileobj(r, w)
        kfs.remove(src)
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            os.rename(src, dst)
        except OSError:
            shutil.move(src, dst)


def execute_shard(root: str, bins: list[list[tuple[str, int]]], *,
                  dest: Optional[str] = None, dry_run: bool = False,
                  rebalance: bool = False) -> dict[str, int]:
    """Move files into ``shard_NN/`` dirs according to the bin-pack plan.

    In **rebalance** mode, files already in their target shard are skipped (only
    misplaced files move). Returns ``{moved, skipped, errors}``.
    """
    dest = dest or root
    dest_base = str(dest).rstrip("/\\")
    root_base = str(root).rstrip("/\\")
    moved = skipped = errors = 0

    for disk_idx, files in enumerate(bins):
        shard_name = f"shard_{disk_idx + 1:02d}"
        for rel, _size in files:
            # In rebalance mode, check if the file is already in the right shard.
            current_shard = rel.split("/")[0] if "/" in rel else ""
            if rebalance and current_shard == shard_name:
                skipped += 1
                continue
            # Skip files that are already inside *a* shard dir in initial mode too
            # (they were part of a previous shard attempt) — only move flat files.
            if not rebalance and current_shard.startswith("shard_"):
                skipped += 1
                continue

            src = os.path.join(root_base, rel.replace("/", os.sep))
            # Preserve the relative path under the shard dir (minus any old shard prefix)
            inner = rel.split("/", 1)[1] if rel.split("/")[0].startswith("shard_") else rel
            dst = os.path.join(dest_base, shard_name, inner.replace("/", os.sep))

            if dry_run:
                print(f"    {rel}  ->  {shard_name}/{inner}", flush=True)
                moved += 1
                continue
            try:
                _move_file(src, dst)
                moved += 1
            except Exception as e:  # noqa: BLE001
                print(f"    ERROR moving {rel}: {e}", flush=True)
                errors += 1
    return {"moved": moved, "skipped": skipped, "errors": errors}


def emit_shard_config(n_disks: int, *,
                      kind: str = "hdd",
                      read_tmpl: Optional[str] = None,
                      write_tmpl: Optional[str] = None) -> str:
    """Emit ``[[storage.disk]]`` blocks matching the shard layout."""
    lines: list[str] = []
    for i in range(1, n_disks + 1):
        shard = f"shard_{i:02d}"
        did = f"disk{i}"
        ctx = {"n": i, "disk": did, "shard": shard}
        lines.append("[[storage.disk]]")
        lines.append(f'id = "{did}"')
        lines.append(f'kind = "{kind}"')
        lines.append(f'match = "{shard}"')
        if read_tmpl:
            lines.append(f'read = "{read_tmpl.format(**ctx)}"')
        if write_tmpl:
            lines.append(f'write = "{write_tmpl.format(**ctx)}"')
        lines.append("")
    return "\n".join(lines)


def print_shard_plan(bins: list[list[tuple[str, int]]], total_bytes: int) -> None:
    """Show the bin-pack plan: per-disk bytes, files, % and the skew."""
    sizes = [sum(s for _, s in b) for b in bins]
    total = sum(sizes)
    for i, b in enumerate(bins):
        shard = f"shard_{i + 1:02d}"
        pct = 100.0 * sizes[i] / total if total else 0
        print(f"  {shard} (disk{i+1}): {len(b)} files, {_fmt_bytes(sizes[i])} "
              f"({pct:.1f}%)", flush=True)
    if sizes:
        skew = max(sizes) / min(sizes) if min(sizes) > 0 else float("inf")
        print(f"  -> skew {skew:.2f}:1", flush=True)


# --------------------------------------------------------------- probe
def probe_nas(server: str, *, shares: Optional[list[str]] = None,
              pattern: Optional[str] = None, n: int = 7) -> list[DiskConfig]:
    """Best-effort discovery of a NAS's per-disk shares to scaffold a topology.

    Tries each candidate share name (explicit ``shares``, a ``pattern`` like
    ``disk{1..7}``, or the default ``disk1..diskN``), checks if it's accessible
    via kfs, and for each found share checks for a ``{share}_direct`` variant (the
    Unraid fast-read pattern). Returns a list of :class:`DiskConfig` you can edit.
    """
    if shares:
        candidates = shares
    elif pattern:
        candidates = _expand_probe_pattern(pattern, n)
    else:
        candidates = [f"disk{i}" for i in range(1, n + 1)]

    found: list[DiskConfig] = []
    for name in candidates:
        share_path = f"//{server}/{name}"
        try:
            if not kfs.exists(share_path + "/"):
                continue
        except Exception:  # noqa: BLE001
            continue
        direct = f"//{server}/{name}_direct"
        has_direct = False
        try:
            has_direct = kfs.exists(direct + "/")
        except Exception:  # noqa: BLE001
            pass
        found.append(DiskConfig(
            id=name,
            kind="hdd",
            read=direct if has_direct else share_path,
            write=share_path,
            match=f"shard_{len(found) + 1:02d}",  # placeholder; shard tool fills this
        ))
        print(f"[probe] found share '{name}'"
              + (f" (+ direct variant '{name}_direct')" if has_direct else ""), flush=True)
    if not found:
        print(f"[probe] no accessible shares found on {server!r} with candidates "
              f"{candidates[:8]}...", flush=True)
    return found


def _expand_probe_pattern(pattern: str, n: int) -> list[str]:
    """``disk{1..7}`` -> ``['disk1', ..., 'disk7']``; ``vol{1..3}`` -> ``['vol1', ...]``."""
    if "{" not in pattern:
        return [pattern]
    prefix, _, rest = pattern.partition("{")
    rng, _, suffix = rest.partition("}")
    if ".." not in rng:
        return [pattern]
    start_s, _, end_s = rng.partition("..")
    try:
        start, end = int(start_s), int(end_s)
    except ValueError:
        return [pattern]
    return [f"{prefix}{i}{suffix}" for i in range(start, end + 1)]


def print_probe_topology(disks: list[DiskConfig]) -> None:
    if not disks:
        return
    print(flush=True)
    print("Suggested topology (edit paths + kind, then paste into kiroshi.local.toml):",
          flush=True)
    print(flush=True)
    n = len(disks)
    print(emit_shard_config(n, kind="hdd",
                            read_tmpl="{disk}" if any(d.read for d in disks) else None,
                            write_tmpl="{disk}" if any(d.write for d in disks) else None),
          flush=True)
    # Override with actual discovered paths
    print("# actual discovered paths:", flush=True)
    for d in disks:
        print(f"#   {d.id}: read={d.read}  write={d.write}", flush=True)
