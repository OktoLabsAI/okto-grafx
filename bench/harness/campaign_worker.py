"""Isolated RF01 worker. Imports the requested source, not an installed wheel.

One private handle per sample; setup, open, explain, priming, hashes, statistics
copy and filesystem accounting are outside the timed interval. Cursor creation,
first next(), all remaining pulls and close are included. Explain is explicitly
not the lazy expression-compilation boundary. Writes time commit through ACK.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import gc
import importlib.metadata
import json
import multiprocessing
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

# Script entry intentionally uses the current harness with either historical
# engine. Never import okto_grafx before the requested engine source is pinned.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.harness.campaign_fixtures import Scenario, apply_churn, build_fixture, contract, oracle, query_for
from bench.harness.campaign_schema import CampaignError, canonical_hash, load_json


def unavailable_memory(reason):
    return {"logical_memory_peak_bytes": None, "baseline_retained_bytes": 0,
            "coverage": "unavailable", "unavailable_reason": reason}


def rss_peak():
    import psutil
    info = psutil.Process().memory_info()
    if hasattr(info, "peak_wset"):
        return info.peak_wset, "process_lifetime_peak_working_set_bytes"
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024), "process_lifetime_ru_maxrss_bytes"


def sample_record(spec, index, rows, statistics, *, elapsed_ns, cpu_ns, prepare_ns,
                  memory, rss_peak_bytes, first_row_ns=None, consume_all_ns=None,
                  commit_ns=None, selected_plan=None, rss_scope="process_lifetime_peak_bytes"):
    scenario = Scenario.from_dict(spec)
    query, params = query_for(scenario)
    expected = oracle(scenario)
    actual_hash = canonical_hash(rows)
    if actual_hash != canonical_hash(expected):
        raise CampaignError(f"{scenario.name}: independent oracle mismatch: actual={actual_hash} expected={canonical_hash(expected)}")
    return {"scenario_id": scenario.name, "seed": scenario.seed, "index": index,
            "kind": "warmup" if index < 5 else "retained",
            "config_hash": canonical_hash(spec), "query_hash": canonical_hash(query),
            "parameters_hash": canonical_hash(params), "contract_hash": canonical_hash(contract(scenario)),
            "result_hash": actual_hash, "expected_result_hash": canonical_hash(expected), "rows": len(rows),
            "cache_state": {"plan": "explained; lazy compilation may occur during execution",
                            "handle": "private reopened; primed" if scenario.cache == "warm" else "private reopened; unprimed",
                            "fts": scenario.cache, "process": "fresh per arm; reused across samples", "os": "uncontrolled"},
            "selected_plan": selected_plan or ["synthetic-test-plan"],
            "plan_scope": "explain operator tree includes unexecuted alternatives; execution evidence is statistics",
            "effective_regime": {"evidence": "runtime counters only; planned alternatives are not counted",
                                 "fallback": {k: v for k, v in statistics.items() if k.startswith("fallback_") and v}},
            "statistics": dict(statistics), "elapsed_ns": elapsed_ns, "cpu_ns": cpu_ns,
            "prepare_ns": prepare_ns, "prepare_boundary": "explain_parse_plan_not_lazy_expression_compile",
            "first_row_ns": first_row_ns, "consume_all_ns": elapsed_ns if consume_all_ns is None else consume_all_ns,
            "commit_ns": commit_ns, "memory": memory, "rss_peak_bytes": rss_peak_bytes, "rss_scope": rss_scope,
            "wal_delta_bytes": 0, "disk_delta_bytes": 0, "checkpoint": "fixture checkpoint before measurements",
            "wal_scope": "allocated file bytes in WAL paths; not logical bytes appended or fsync count",
            "handle_scope": "private process/sample; no preexisting cursor or concurrent statement",
            "cpu_scope": "current process; process_time_ns; OS timer granularity applies"}


def _fts_rows(db, scenario):
    # Project membership intentionally. Never claim this proves scores/ranking.
    result = db.search_text(index="campaign_text", query=scenario.term, k=scenario.nodes)
    return tuple(sorted((hit.record_id,) for hit in result.hits)), result


def measure_statement(db, scenario, index, *, reader=None):
    query, params = query_for(scenario)
    begin = time.perf_counter_ns()
    if scenario.family == "fts":
        plan = ["native_search_text_membership"]
    else:
        plan = [type(node).__name__ for node in db.explain(query).walk()]
    prepare_ns = time.perf_counter_ns() - begin
    observe = getattr(db._queries, "_observe_logical_memory", None)
    memory_scope = observe() if observe is not None else nullcontext(None)
    first = commit = None
    stats = {}
    with memory_scope as observation:
        cpu_start = time.process_time_ns()
        start = time.perf_counter_ns()
        if scenario.family == "fts":
            rows, result = _fts_rows(db, scenario)
        elif scenario.family == "write":
            tx = db.begin("write")
            try:
                result = tx.execute(query, {"rows": [{"id": i, "payload": "x" * scenario.payload_bytes}
                                                      for i in range(scenario.write_rows)]})
                ack_start = time.perf_counter_ns()
                tx.commit()
                commit = time.perf_counter_ns() - ack_start
            except BaseException:
                tx.rollback()
                raise
            rows = ()  # Verified below, outside timing and after durable ACK.
        elif scenario.cursor_batch_size:
            if reader is not None:
                raise CampaignError("public cursors own their snapshot; cannot attach retained reader")
            query_api = db.query(query, params)
            with query_api.cursor(batch_size=scenario.cursor_batch_size) as cursor:
                iterator = iter(cursor)
                sentinel = object()
                head = next(iterator, sentinel)
                first = None if head is sentinel else time.perf_counter_ns() - start
                rows = (() if head is sentinel else (head,)) + tuple(iterator)
            result = cursor
        else:
            result = reader.execute(query, params) if reader is not None else db.execute(query, params)
            rows = result.rows
        elapsed = time.perf_counter_ns() - start
        cpu = time.process_time_ns() - cpu_start
    memory = observation.result() if observation is not None else unavailable_memory("revision_has_no_logical_memory_observer")
    if scenario.family == "fts":
        memory = unavailable_memory("native_fts_memory_not_covered_by_query_budget_observer")
        stats = {"postings_visited": result.postings_visited}
    else:
        stats = dict(result.statistics)
    if scenario.family == "write":
        rows = db.execute("MATCH (n:W) RETURN n.id,n.payload ORDER BY n.id").rows
    peak, scope = rss_peak()
    return sample_record(scenario.to_dict(), index, rows, stats, elapsed_ns=elapsed, cpu_ns=cpu,
                         prepare_ns=prepare_ns, memory=memory, rss_peak_bytes=peak,
                         first_row_ns=first, commit_ns=commit, selected_plan=plan, rss_scope=scope)


def _sizes(root):
    files = [p for p in Path(root).rglob("*") if p.is_file()]
    return sum(p.stat().st_size for p in files), sum(p.stat().st_size for p in files if "wal" in p.relative_to(root).as_posix().lower())


def _git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def executor_environment():
    """Shared environment identity for measured workers and calibration."""
    import psutil
    return {"python": sys.version, "dependencies": {name: importlib.metadata.version(name)
            for name in ("numpy", "google-crc32c", "psutil")}, "os": platform.platform(),
            "cpu": platform.processor() or "unknown", "cpu_count": os.cpu_count(),
            "ram_bytes": psutil.virtual_memory().total, "gc_enabled": gc.isenabled()}


def _writer_child(root, spec, number, barrier, queue):
    """Spawn target with disjoint keys; any conflict/error remains a failed run."""
    import okto_grafx
    scenario = Scenario.from_dict(spec)
    try:
        with okto_grafx.connect(root, query_memory_budget_bytes=scenario.query_memory_budget_bytes) as db:
            observe = getattr(db._queries, "_observe_logical_memory", None)
            scope = observe() if observe else nullcontext(None)
            records = [{"id": number * scenario.write_rows + i, "payload": "x" * scenario.payload_bytes}
                       for i in range(scenario.write_rows)]
            barrier.wait(timeout=60)
            with scope as observation:
                cpu_start, start = time.process_time_ns(), time.perf_counter_ns()
                tx = db.begin("write")
                try:
                    result = tx.execute(query_for(scenario)[0], {"rows": records})
                    ack_start = time.perf_counter_ns()
                    tx.commit()
                    ack_end = time.perf_counter_ns()
                except BaseException:
                    tx.rollback()
                    raise
                elapsed = time.perf_counter_ns() - start
                cpu = time.process_time_ns() - cpu_start
            peak, rss_scope = rss_peak()
            queue.put({"writer": number, "process_id": os.getpid(), "elapsed_ns": elapsed,
                       "commit_ns": ack_end-ack_start, "cpu_ns": cpu,
                       "statistics": dict(result.statistics), "rss_peak_bytes": peak, "rss_scope": rss_scope,
                       "memory": observation.result() if observation else unavailable_memory("revision_has_no_logical_memory_observer")})
    except BaseException as exc:
        queue.put({"writer": number, "error": type(exc).__name__, "message": str(exc)})
        try:
            barrier.abort()
        except Exception:
            pass


def measure_concurrent_write(root, scenario, index):
    """Independent writer processes. Keep individual peaks; never sum them."""
    import okto_grafx
    ctx = multiprocessing.get_context("spawn")
    barrier, queue = ctx.Barrier(scenario.writers + 1), ctx.Queue()
    children = [ctx.Process(target=_writer_child, args=(str(root), scenario.to_dict(), i, barrier, queue))
                for i in range(scenario.writers)]
    try:
        for child in children:
            child.start()
        # Startup/open is excluded; begin/execute/commit through all ACKs included.
        start = time.perf_counter_ns()
        barrier.wait(timeout=60)
        release = time.perf_counter_ns()
        writers = [queue.get(timeout=120) for _ in children]
        elapsed = time.perf_counter_ns() - release
        if any("error" in writer for writer in writers):
            raise CampaignError(f"writer failed: {writers}")
        for child in children:
            child.join(timeout=10)
            if child.exitcode != 0:
                raise CampaignError("writer exit failed")
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join()
        queue.close()
    with okto_grafx.connect(root, query_memory_budget_bytes=scenario.query_memory_budget_bytes) as db:
        rows = db.execute("MATCH (n:W) RETURN n.id,n.payload ORDER BY n.id").rows
    stats = {}
    for writer in writers:
        for key, value in writer["statistics"].items():
            stats[key] = stats.get(key, 0) + value
    peak, rss_scope = rss_peak()
    sample = sample_record(scenario.to_dict(), index, rows, stats, elapsed_ns=elapsed,
                           cpu_ns=sum(w["cpu_ns"] for w in writers), prepare_ns=release-start,
                           memory=unavailable_memory("concurrent_process_peaks_reported_per_writer_not_aggregated"),
                           rss_peak_bytes=peak, rss_scope=rss_scope,
                           commit_ns=max(w["commit_ns"] for w in writers), selected_plan=["concurrent_native_UNWIND_writers"])
    sample.update(writers=sorted(writers, key=lambda w: w["writer"]),
                  prepare_boundary="spawn_open_and_barrier_ready_not_query_compile",
                  commit_boundary="maximum individual durable commit duration; individual ACKs in writers",
                  cpu_scope="sum of writer CPU times; not coordinator CPU",
                  elapsed_boundary="barrier release to all writer results; includes result IPC")
    return sample


def run_worker(request, output):
    import psutil
    source = Path(request["source_root"]).resolve()
    if "okto_grafx" in sys.modules:
        raise CampaignError("engine already imported before source selection")
    sys.path.insert(0, str(source / "src"))
    import okto_grafx
    package = Path(okto_grafx.__file__).resolve()
    if not package.is_relative_to(source / "src"):
        raise CampaignError("engine import provenance mismatch")
    revision = _git(source, "rev-parse", "HEAD")
    if revision != request["revision"] or _git(source, "status", "--porcelain"):
        raise CampaignError("source must be clean at the requested revision")
    report = {"arm": request["arm"], "pair": request["pair"], "run_order": request["run_order"],
              "process_id": os.getpid(), "invocation_id": uuid.uuid4().hex, "revision": revision,
              "source_clean": True, "package_file": str(package), "source_root": str(source),
              "environment": {**executor_environment(),
                              "disk": {"root": str(output.parent.anchor), "free_bytes": shutil.disk_usage(output.parent).free}},
              "cpu_before_percent": psutil.cpu_percent(interval=.2), "samples": []}
    output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    try:
        for spec in request["scenarios"]:
            scenario = Scenario.from_dict(spec)
            options = {"query_memory_budget_bytes": scenario.query_memory_budget_bytes}
            with tempfile.TemporaryDirectory(prefix="fixture-", dir=output.parent) as directory:
                root = Path(directory) / "db"
                independent = scenario.family == "write" or scenario.reader == "retained"
                if not independent:
                    with okto_grafx.connect(root, **options) as db:
                        build_fixture(db, scenario)
                for index in range(25):
                    sample_root = root if not independent else Path(directory) / f"independent-{index}"
                    with okto_grafx.connect(sample_root, **options) as db:
                        if independent:
                            build_fixture(db, scenario, apply_updates=scenario.reader != "retained")
                        report["metrics_sink"] = type(db._queries._metrics).__name__
                        if db._queries._metrics.enabled:
                            raise CampaignError("default metrics sink unexpectedly enabled")
                        if scenario.family == "write" and scenario.writers > 1:
                            continue_sample = False
                        else:
                            continue_sample = True
                        if scenario.cache == "warm" and scenario.family != "write":
                            query, params = query_for(scenario)
                            if scenario.family == "fts":
                                _fts_rows(db, scenario)
                            else:
                                db.execute(query, params)
                        before, wal_before = _sizes(sample_root)
                        if continue_sample:
                            if scenario.reader == "retained":
                                with db.begin("read") as reader:
                                    reader.execute("MATCH (n:N) RETURN count(n)")
                                    apply_churn(db, scenario)
                                    sample = measure_statement(db, scenario, index, reader=reader)
                            else:
                                sample = measure_statement(db, scenario, index)
                    if not continue_sample:
                        sample = measure_concurrent_write(sample_root, scenario, index)
                    after, wal_after = _sizes(sample_root)
                    sample.update(disk_delta_bytes=after-before, wal_delta_bytes=wal_after-wal_before)
                    report["samples"].append(sample)
                    output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
                    # Write results are rechecked through a new handle after ACK.
                    if scenario.family == "write":
                        with okto_grafx.connect(sample_root, **options) as reopened:
                            if canonical_hash(reopened.execute("MATCH (n:W) RETURN n.id,n.payload ORDER BY n.id").rows) != canonical_hash(oracle(scenario)):
                                raise CampaignError("write reopen oracle mismatch")
            output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
        report["cpu_after_percent"] = psutil.cpu_percent(interval=.2)
        if _git(source, "rev-parse", "HEAD") != revision or _git(source, "status", "--porcelain"):
            raise CampaignError("source changed during worker execution")
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
        raise
    output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    run_worker(load_json(args.request.read_text(encoding="utf-8")), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
