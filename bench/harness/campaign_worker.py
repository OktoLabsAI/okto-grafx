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
import hashlib
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
from bench.harness import read_lifecycle_qualification
from bench.harness.campaign_schema import CampaignError, canonical_hash, load_json
from bench.harness.campaign_schema import HISTORICAL_SCOPE, validate_candidate_memory
from bench.harness import fts_qualification, rf04_qualification, uniqueness_qualification


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
            "memory_budget_bytes": scenario.query_memory_budget_bytes,
            "wal_delta_bytes": 0, "disk_delta_bytes": 0, "checkpoint": "fixture checkpoint before measurements",
            "wal_scope": "allocated file bytes in WAL paths; not logical bytes appended or fsync count",
            "handle_scope": "private process/sample; no preexisting cursor or concurrent statement",
            "cpu_scope": "current process; process_time_ns; OS timer granularity applies"}


def _fts_rows(db, scenario):
    # Project membership intentionally. Never claim this proves scores/ranking.
    result = db.search_text(index="campaign_text", query=scenario.term, k=scenario.nodes)
    return tuple(sorted((hit.record_id,) for hit in result.hits)), result


def measure_statement(db, scenario, index, *, reader=None):
    from bench.harness.quota_qualification import connection_options
    for field, limit in connection_options(scenario).items():
        if getattr(db._queries, "_" + field, None) != limit:
            raise CampaignError("runtime traversal quota differs from scenario")
    if db._queries._query_memory_budget_bytes != scenario.query_memory_budget_bytes:
        raise CampaignError("runtime logical memory budget differs from scenario")
    if scenario.family == "write":
        return measure_single_write(db, scenario, index)
    if scenario.family == uniqueness_qualification.FAMILY:
        return measure_unique_create(db, scenario, index)
    query, params = query_for(scenario)
    begin = time.perf_counter_ns()
    if scenario.family == "fts":
        plan = ["native_search_text_membership"]
    elif scenario.family == fts_qualification.FAMILY:
        plan = ["native_search_text_ranked"]
    else:
        plan = [type(node).__name__ for node in db.explain(query).walk()]
    prepare_ns = time.perf_counter_ns() - begin
    observation_name = ("_observe_logical_memory_sequence" if scenario.family in
                        {"read_autocommit", "read_shared"} else "_observe_logical_memory")
    observe = getattr(db._queries, observation_name, None)
    # Unbudgeted landing cannot yield a logical-memory measurement. Activating
    # the candidate-only observer would still charge identity checks and owner
    # accounting inside the timed query even though its result must be null.
    # The independent budgeted lifecycle proof retains the real observer.
    unbudgeted_landing = scenario.family == "landing" and scenario.query_memory_budget_bytes is None
    if unbudgeted_landing and getattr(db._queries, "_memory_observation", None) is not None:
        raise CampaignError("unbudgeted landing requires no preexisting logical observation")
    native_fts = scenario.family == fts_qualification.FAMILY
    memory_scope = observe() if observe is not None and not (unbudgeted_landing or native_fts) else nullcontext(None)
    first = commit = None
    stats = {}
    with memory_scope as observation:
        cpu_start = time.process_time_ns()
        start = time.perf_counter_ns()
        if scenario.family in read_lifecycle_qualification.FAMILIES:
            if reader is not None:
                raise CampaignError("read recipes own their snapshot")
            rows, result, first = read_lifecycle_qualification.execute(db, scenario, start)
        elif scenario.family == "fts":
            rows, result = _fts_rows(db, scenario)
        elif native_fts:
            rows, result = fts_qualification.rows(db, scenario, reader=reader)
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
    missing_reason = ("query_memory_budget_disabled" if observe is not None and unbudgeted_landing
                      else "revision_has_no_logical_memory_observer")
    memory = observation.result() if observation is not None else unavailable_memory(missing_reason)
    if memory["logical_memory_peak_bytes"] is None and memory["unavailable_reason"] == "query_memory_budget_disabled":
        # The observer identifies its implementation even when disabled. Raw
        # samples describe effective coverage, not that implementation label.
        # Preserve the null peak, reason and measured baseline verbatim.
        memory = dict(memory, coverage="unavailable")
    if native_fts:
        memory = unavailable_memory("native_fts_memory_not_covered_by_query_budget_observer")
        stats = fts_qualification.statistics(result)
        if result.corpus_documents != fts_qualification.corpus_documents(scenario):
            raise CampaignError("FTS corpus count differs from fixture")
        if result.statistics_regime != fts_qualification.expected_source(scenario):
            raise CampaignError("FTS statistics source does not cover the declared recipe")
        if (result.statistics_wal_records > 0) != (scenario.fts_updates == 1):
            raise CampaignError("FTS WAL delta evidence differs from declared recipe")
    elif scenario.family == "fts":
        memory = unavailable_memory("native_fts_memory_not_covered_by_query_budget_observer")
        stats = {"postings_visited": result.postings_visited}
    else:
        stats = dict(result.statistics)
    if scenario.family in read_lifecycle_qualification.FAMILIES:
        # Separate diagnostic repeat after timing and logical-memory observation.
        # Never claim these are manager counts from inside the measured interval.
        stats.update(read_lifecycle_qualification.diagnose(db, scenario))
    if scenario.family == "write":
        rows = db.execute("MATCH (n:W) RETURN n.id,n.payload ORDER BY n.id").rows
    peak, scope = rss_peak()
    sample = sample_record(scenario.to_dict(), index, rows, stats, elapsed_ns=elapsed, cpu_ns=cpu,
                         prepare_ns=prepare_ns, memory=memory, rss_peak_bytes=peak,
                         first_row_ns=first, commit_ns=commit, selected_plan=plan, rss_scope=scope)
    if scenario.rf04_case is not None:
        sample["rf04_access"] = rf04_qualification.access(sample)
    if native_fts:
        sample["fts_source"] = {"statistics_regime": result.statistics_regime,
                                "statistics_mode": scenario.fts_statistics_mode,
                                "history_entries": scenario.fts_history,
                                "hits": len(rows)}
    return sample


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


def source_identity(source, revision):
    """Bind loaded Python files to exact immutable Git blobs; do not relabel."""
    import okto_grafx.engine.query_engine as engine
    import okto_grafx.domain.query.memory as memory
    hashes = {}
    for relative, module in (("src/okto_grafx/engine/query_engine.py", engine),
                             ("src/okto_grafx/domain/query/memory.py", memory)):
        expected = (source / relative).resolve()
        if Path(module.__file__).resolve() != expected:
            raise CampaignError("logical observer import provenance mismatch")
        blob = subprocess.check_output(["git", "-C", str(source), "show", f"{revision}:{relative}"])
        crlf, lf = bytes((13, 10)), bytes((10,))
        if expected.read_bytes().replace(crlf, lf) != blob.replace(crlf, lf):
            raise CampaignError("logical observer source differs from committed blob")
        hashes[relative] = hashlib.sha256(blob).hexdigest()
    return {"source_tree": _git(source, "rev-parse", f"{revision}^{{tree}}"),
            "engine_source_hashes": hashes,
            "logical_observer_available": callable(getattr(engine.QueryEngine, "_observe_logical_memory", None))}


def collect_candidate_memory(run, budgets, directory):
    """Separate untimed lifecycle proof on private handles, never a benchmark.

    Observe real constructor-created operator budgets without changing their
    admission/release methods. Preserve simultaneous observations and individual
    peaks rather than summing sequential peaks. Each budget is tested on the same
    handle across twenty success/error/cancel cycles, with no manual cleanup of
    the counters being verified.
    """
    from unittest.mock import patch
    from okto_grafx import CancellationToken, connect
    from okto_grafx.errors import GrafxQueryError, GrafxQueryCancelled
    from okto_grafx.domain.query.memory import LogicalMemoryBudget
    groups = []
    original_init = LogicalMemoryBudget.__init__
    for budget in sorted(budgets):
        records = []
        operators = []
        def tracking_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            operators.append(self)
        with connect(Path(directory) / f"memory-proof-{budget}", query_memory_budget_bytes=budget) as db:
            engine = db._queries
            if not callable(getattr(engine, "_observe_logical_memory", None)):
                raise CampaignError("candidate logical memory observer unavailable")
            def retained():
                return engine._owner_budget._used_bytes + engine._endpoint_budget._used_bytes
            with patch.object(LogicalMemoryBudget, "__init__", tracking_init):
                for mode in ("success", "error", "cancel"):
                    for index in range(20):
                        operators.clear()
                        handle_before = retained()
                        error_code = None
                        with engine._observe_logical_memory() as observation:
                            before = observation.current
                            parameters = {"items": list(range(8, 0, -1)), "zero": 0}
                            if mode == "success":
                                result = db.execute("UNWIND $items AS i RETURN i ORDER BY i", parameters)
                                if result.rows != tuple((i,) for i in range(1, 9)):
                                    raise CampaignError("memory proof success oracle failed")
                            elif mode == "error":
                                try:
                                    db.execute("UNWIND $items AS i WITH i ORDER BY i RETURN 1 / $zero", parameters)
                                except GrafxQueryError as exc:
                                    error_code = exc.code
                            else:
                                token = CancellationToken()
                                with db.query("UNWIND $items AS i RETURN i ORDER BY i", parameters).cursor(batch_size=1, cancellation=token) as cursor:
                                    if next(cursor) != (1,):
                                        raise CampaignError("memory proof cursor oracle failed")
                                    token.cancel()
                                    try:
                                        next(cursor)
                                    except GrafxQueryCancelled as exc:
                                        error_code = exc.code
                            after = observation.current
                        records.append({"mode": mode, "index": index, "live_before_bytes": before,
                                        "live_after_bytes": after, "handle_before_bytes": handle_before,
                                        "handle_after_bytes": retained(), "memory": observation.result(),
                                        "operator_budgets": [{"operator": item._operator, "limit_bytes": item.limit,
                                                               "peak_bytes": item.peak} for item in operators],
                                        "error_code": error_code, "open_transactions_after": db.transactions.open_transactions})
        groups.append({"budget_bytes": budget, "records": records})
    body = {"schema_version": "grafx-candidate-memory/v1", "revision": run["revision"],
            "process_id": run["process_id"], "invocation_id": run["invocation_id"],
            "engine_source_hashes": run["engine_source_hashes"],
            "procedure": {"id": "rf01-logical-lifecycle-v1", "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
            "budgets": groups}
    evidence = {"body": body, "sha256": canonical_hash(body)}
    validate_candidate_memory(dict(run, candidate_memory_evidence=evidence), budgets)
    return evidence


def error_record(exc):
    from okto_grafx.errors import GrafxError

    result = {"type": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, GrafxError):
        result.update(code=exc.code, details=dict(exc.details))
    if hasattr(exc, "evidence"):
        result["write_failure"] = exc.evidence
    return result


def rf06_memory_retention_proof(db, scenario):
    """Untimed same-handle scalar proof; it never measures target batch memory."""
    from bench.harness.quota_qualification import expected_outcome

    if (scenario.query_memory_budget_bytes is None or scenario.role != "control"
            or expected_outcome(scenario) != "success"):
        raise CampaignError("RF06 retention proof requires a successful budgeted scalar control")
    observe = getattr(db._queries, "_observe_logical_memory", None)
    if not callable(observe):
        raise CampaignError("RF06 scalar memory observer unavailable")
    query, params = query_for(scenario)
    records = []
    for index in range(5):
        before = db._queries._owner_budget._used_bytes + db._queries._endpoint_budget._used_bytes
        with observe() as observation:
            result = db.execute(query, params)
            if result.rows != oracle(scenario) or result.statistics.get("landing_batches", 0):
                raise CampaignError("RF06 scalar memory proof diverged from oracle or used batching")
        after = db._queries._owner_budget._used_bytes + db._queries._endpoint_budget._used_bytes
        memory = observation.result()
        if (after != before or db.transactions.open_transactions != 0
                or memory["logical_memory_peak_bytes"] is None
                or memory["logical_memory_peak_bytes"] > scenario.query_memory_budget_bytes):
            raise CampaignError("RF06 scalar memory proof retained budget or leaked a transaction")
        records.append({"index": index, "retained_before_bytes": before,
                        "retained_after_bytes": after, "open_transactions_after": 0,
                        "memory": memory})
    return {"scope": "five untimed repetitions on one budgeted scalar handle; not target batch allocation",
            "records": records}


class WriteCampaignFailure(CampaignError):
    def __init__(self, evidence):
        self.evidence = evidence
        super().__init__("write failed; durable prefix retained in write_failure; no replay")


def execute_write_recipe(db, scenario, records, *, failure_evidence=None, writer_id=0):
    """Use existing APIs; collect only returned ACKs, never replay a failed commit."""
    query = query_for(scenario)[0]
    width = (1 if scenario.write_shape == "transactions" else
             scenario.chunk_rows if scenario.write_shape == "chunks" else len(records))
    stats, reports, commit_ns = {}, [], 0
    audit = failure_evidence if failure_evidence is not None else {}
    audit.update(confirmed_prefix=[], failed_transaction=None, cleanup_error=None)
    for offset in range(0, len(records), max(1, width)):
        group = records[offset:offset + width]
        tx = None
        primary = None
        phase = "begin"
        try:
            tx = db.begin("write")
            phase = "execute"
            if scenario.write_shape == "executemany":
                results = (tx.executemany(query, iter(group)),)
            elif scenario.write_shape in ("statements", "transactions"):
                results = (tx.execute(query, record) for record in group)
            else:
                results = (tx.execute(query, {"rows": group}),)
            for result in results:
                for key, value in result.statistics.items():
                    stats[key] = stats.get(key, 0) + value
            ack_start = time.perf_counter_ns()
            phase = "commit"
            report = tx.commit()
            ack_duration_ns = time.perf_counter_ns() - ack_start
            commit_ns += ack_duration_ns
            reports.append({"csn": int(report.csn), "durable": report.durable, "wrote": report.wrote,
                            "writer": writer_id, "offset": offset, "rows": len(group),
                            "ack_duration_ns": ack_duration_ns})
            if report.durable is not True or report.wrote is not True:
                raise CampaignError("write returned no durable ACK; campaign invalid, no replay")
            audit["confirmed_prefix"].append(reports[-1])
        except BaseException as exc:
            primary = exc
            audit["primary_error"] = error_record(exc)
            audit["failed_transaction"] = {"offset": offset, "rows": len(group), "phase": phase,
                                           "outcome": "not_assumed_aborted_or_durable"}
            raise
        finally:
            # A commit refusal may leave the transaction active. Confirmed and
            # uncertain outcomes never receive replay from this measurement.
            if tx is not None and tx.active:
                try:
                    tx.rollback()
                except BaseException as cleanup:
                    audit["cleanup_error"] = error_record(cleanup)
                    if primary is None:
                        raise
    return stats, reports, commit_ns


def _measure_writer(db, scenario, records, *, writer_id=0):
    sequence = getattr(db._queries, "_observe_logical_memory_sequence", None)
    observe = sequence or (getattr(db._queries, "_observe_logical_memory", None)
                           if scenario.write_shape == "unwind" else None)
    audit = {}
    try:
        with (observe() if observe else nullcontext(None)) as observation:
            cpu_start, start = time.process_time_ns(), time.perf_counter_ns()
            stats, reports, commit_ns = execute_write_recipe(
                db, scenario, records, failure_evidence=audit, writer_id=writer_id)
            elapsed, cpu = time.perf_counter_ns() - start, time.process_time_ns() - cpu_start
    except BaseException as exc:
        audit.setdefault("primary_error", error_record(exc))
        raise WriteCampaignFailure(audit) from exc
    memory = observation.result() if observation else unavailable_memory("revision_has_no_sequence_memory_observer")
    peak, rss_scope = rss_peak()
    return {"elapsed_ns": elapsed, "cpu_ns": cpu, "commit_ns": commit_ns,
            "started_ns": start,
            "memory_budget_bytes": scenario.query_memory_budget_bytes,
            "statistics": stats, "commit_reports": reports, "memory": memory,
            "rss_peak_bytes": peak, "rss_scope": rss_scope}


def write_summary(scenario, reports, elapsed):
    return {"shape": scenario.write_shape, "partial_progress": scenario.partial_progress,
            "atomic_unit": contract(scenario)["transaction"],
            "business_rows": scenario.write_rows * scenario.writers,
            "commits": len(reports), "durable_acks": sum(r["durable"] is True for r in reports),
            "commit_reports": reports,
            "rows_per_second": scenario.write_rows * scenario.writers * 1e9 / elapsed,
            "durable_acks_per_second": sum(r["durable"] is True for r in reports) * 1e9 / elapsed,
            "throughput_boundary": "sample elapsed interval through all returned durable ACKs"}


def measure_single_write(db, scenario, index):
    prepare = time.perf_counter_ns()
    plan = [type(n).__name__ for n in db.explain(query_for(scenario)[0]).walk()]
    prepare = time.perf_counter_ns() - prepare
    records = [{"id": i, "payload": "x" * scenario.payload_bytes} for i in range(scenario.write_rows)]
    measured = _measure_writer(db, scenario, records)
    rows = db.execute("MATCH (n:W) RETURN n.id,n.payload ORDER BY n.id").rows
    sample = sample_record(scenario.to_dict(), index, rows, measured["statistics"],
                           elapsed_ns=measured["elapsed_ns"], cpu_ns=measured["cpu_ns"],
                           prepare_ns=prepare, memory=measured["memory"],
                           rss_peak_bytes=measured["rss_peak_bytes"], rss_scope=measured["rss_scope"],
                           commit_ns=measured["commit_ns"], selected_plan=plan)
    sample["write"] = write_summary(scenario, measured["commit_reports"], measured["elapsed_ns"])
    return sample


def measure_unique_create(db, scenario, index):
    """Time one materialized CREATE through its returned durable ACK."""
    query, params = query_for(scenario)
    prepare_start = time.perf_counter_ns()
    plan = [type(node).__name__ for node in db.explain(query).walk()]
    prepare_ns = time.perf_counter_ns() - prepare_start
    observe = getattr(db._queries, "_observe_logical_memory", None)
    retained_before = None
    if scenario.query_memory_budget_bytes is None:
        # Enabling the observer here adds tariffs to the timed path yet cannot
        # return a valid logical peak without a quota. Report the null honestly.
        memory_scope = nullcontext(None)
    else:
        if observe is None:
            raise CampaignError("RF08 quota control requires a logical-memory observer")
        retained_before = (db._queries._owner_budget._used_bytes
                           + db._queries._endpoint_budget._used_bytes)
        memory_scope = observe()
    with memory_scope as observation:
        tx = db.begin("write")
        try:
            cpu_start, start = time.process_time_ns(), time.perf_counter_ns()
            result = tx.execute(query, params)
            ack_start = time.perf_counter_ns()
            ack = tx.commit()
            commit_ns = time.perf_counter_ns() - ack_start
            elapsed_ns, cpu_ns = time.perf_counter_ns() - start, time.process_time_ns() - cpu_start
        except BaseException:
            if tx.active:
                tx.rollback()
            raise
    if ack.durable is not True or ack.wrote is not True:
        raise CampaignError("RF08 write returned no durable ACK")
    if result.rows != oracle(scenario):
        raise CampaignError("RF08 CREATE statement rows differ from independent IDs")
    rows = db.execute(uniqueness_qualification.READBACK).rows
    memory = (observation.result() if observation is not None else
              unavailable_memory("query_memory_budget_disabled"))
    if observation is not None and memory["logical_memory_peak_bytes"] is None:
        raise CampaignError("RF08 quota control yielded no logical-memory peak")
    peak, scope = rss_peak()
    sample = sample_record(scenario.to_dict(), index, rows, result.statistics,
                           elapsed_ns=elapsed_ns, cpu_ns=cpu_ns, prepare_ns=prepare_ns,
                           memory=memory, rss_peak_bytes=peak, rss_scope=scope,
                           commit_ns=commit_ns, selected_plan=plan)
    sample["unique_create"] = {
        "ids": scenario.write_rows, "statement_rows": len(result.rows),
        "readback_rows": len(rows), "ack_count": 1, "atomic_unit": "one transaction",
        "ack": {"csn": int(ack.csn), "durable": ack.durable, "wrote": ack.wrote},
        "reopened": False,
    }
    if retained_before is not None:
        sample["unique_create"]["quota_retention"] = {
            "retained_before_bytes": retained_before,
            "retained_after_bytes": (db._queries._owner_budget._used_bytes
                                     + db._queries._endpoint_budget._used_bytes),
            "open_transactions_after": db.transactions.open_transactions,
        }
    return sample


def _writer_child(root, spec, number, barrier, queue):
    """Spawn target with disjoint keys; any conflict/error remains a failed run."""
    import okto_grafx
    scenario = Scenario.from_dict(spec)
    measured = None
    try:
        with okto_grafx.connect(root, query_memory_budget_bytes=scenario.query_memory_budget_bytes) as db:
            records = [{"id": number * scenario.write_rows + i, "payload": "x" * scenario.payload_bytes}
                       for i in range(scenario.write_rows)]
            barrier.wait(timeout=60)
            measured = _measure_writer(db, scenario, records, writer_id=number)
            queue.put(dict(measured, writer=number, process_id=os.getpid()))
    except BaseException as exc:
        failure = error_record(exc)
        if measured is not None:
            failure["confirmed_before_close_failure"] = measured["commit_reports"]
        queue.put({"writer": number, "error": failure})
        try:
            barrier.abort()
        except Exception:
            pass


class _BarrierTimestamp:
    """Spawn-picklable action runs before the barrier releases any writer."""
    def __init__(self, shared):
        self.shared = shared

    def __call__(self):
        self.shared.value = time.perf_counter_ns()


def measure_concurrent_write(root, scenario, index):
    """Independent writer processes. Keep individual peaks; never sum them."""
    import okto_grafx
    ctx = multiprocessing.get_context("spawn")
    stamp = ctx.Value("q", 0)
    barrier, queue = ctx.Barrier(scenario.writers + 1, action=_BarrierTimestamp(stamp)), ctx.Queue()
    children = [ctx.Process(target=_writer_child, args=(str(root), scenario.to_dict(), i, barrier, queue))
                for i in range(scenario.writers)]
    try:
        for child in children:
            child.start()
        # Startup/open is excluded; begin/execute/commit through all ACKs included.
        start = time.perf_counter_ns()
        barrier.wait(timeout=60)
        release = stamp.value
        writers = [queue.get(timeout=120) for _ in children]
        elapsed = time.perf_counter_ns() - release
        if any("error" in writer for writer in writers):
            raise WriteCampaignFailure({"writers": writers, "sample_qualified": False})
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
                           commit_ns=max(w["commit_ns"] for w in writers), selected_plan=["concurrent_native_" + scenario.write_shape])
    sample.update(writers=sorted(writers, key=lambda w: w["writer"]),
                  barrier_release_ns=release,
                  prepare_boundary="spawn_open_and_barrier_ready_not_query_compile",
                  commit_boundary="maximum individual durable commit duration; individual ACKs in writers",
                  cpu_scope="sum of writer CPU times; not coordinator CPU",
                  elapsed_boundary="barrier release to all writer results; includes result IPC")
    sample["write"] = write_summary(scenario, [r for w in sample["writers"] for r in w["commit_reports"]], elapsed)
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
    historical = request.get("qualification_scope") == HISTORICAL_SCOPE
    unbudgeted = request.get("memory_regime") == "unbudgeted_landing"
    from bench.harness.quota_qualification import SPLIT_MEMORY_REGIME, qualification_matrix
    rf06_split = request.get("memory_regime") == SPLIT_MEMORY_REGIME
    if unbudgeted:
        proof_budget = request.get("candidate_memory_budget_bytes")
        if not historical or type(proof_budget) is not int or proof_budget <= 0:
            raise CampaignError("unbudgeted worker requires historical scope and positive separate proof budget")
        if any(Scenario.from_dict(spec).family != "landing" or Scenario.from_dict(spec).query_memory_budget_bytes is not None for spec in request["scenarios"]):
            raise CampaignError("unbudgeted worker requires landing without temporal quota")
        report.update(memory_regime="unbudgeted_landing", candidate_memory_budget_bytes=proof_budget)
    if rf06_split:
        if historical or request["scenarios"] != [item.to_dict() for item in qualification_matrix(Scenario)]:
            raise CampaignError("RF06 worker requires the exact split-memory quota matrix")
        report["memory_regime"] = SPLIT_MEMORY_REGIME
    if historical:
        report.update(source_identity(source, revision))
    output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    try:
        for spec in request["scenarios"]:
            scenario = Scenario.from_dict(spec)
            options = {"query_memory_budget_bytes": scenario.query_memory_budget_bytes}
            if scenario.family == "capacity":
                from bench.harness.capacity_qualification import connect_options
                options.update(connect_options(scenario))
            from bench.harness.quota_qualification import connection_options
            measured_options = {**options, **connection_options(scenario)}
            with tempfile.TemporaryDirectory(prefix="fixture-", dir=output.parent) as directory:
                root = Path(directory) / "db"
                independent = scenario.family in ("write", "capacity", uniqueness_qualification.FAMILY) or scenario.reader == "retained" or (
                    scenario.family == fts_qualification.FAMILY and (
                        scenario.fts_updates > 0 or scenario.fts_write_case != "none"))
                shared_fts_setup = None
                if not independent:
                    setup_start = time.perf_counter_ns()
                    setup_options = ({"query_memory_budget_bytes": None} if rf06_split else options)
                    with okto_grafx.connect(root, **setup_options) as db:
                        build_fixture(db, scenario)
                    if scenario.family == fts_qualification.FAMILY:
                        setup_bytes, setup_wal = _sizes(root)
                        shared_fts_setup = {"elapsed_ns": time.perf_counter_ns() - setup_start,
                                            "disk_allocated_bytes": setup_bytes,
                                            "wal_allocated_bytes": setup_wal,
                                            "scope": "seed_insert_index_creation_checkpoint_and_close",
                                            "observation_scope": "one_fixture_per_process",
                                            "observation_id": f"{report['invocation_id']}:{scenario.name}:shared"}
                for index in range(25):
                    sample_root = root if not independent else Path(directory) / f"independent-{index}"
                    sample_fts_setup = None
                    if independent and scenario.family == fts_qualification.FAMILY:
                        setup_start = time.perf_counter_ns()
                        with okto_grafx.connect(sample_root, **options) as setup_db:
                            build_fixture(setup_db, scenario)
                        setup_bytes, setup_wal = _sizes(sample_root)
                        sample_fts_setup = {"elapsed_ns": time.perf_counter_ns() - setup_start,
                                            "disk_allocated_bytes": setup_bytes,
                                            "wal_allocated_bytes": setup_wal,
                                            "scope": "seed_insert_index_creation_checkpoint_and_close",
                                            "observation_scope": "one_fixture_per_sample",
                                            "observation_id": f"{report['invocation_id']}:{scenario.name}:{index}"}
                    fts_setup = sample_fts_setup or shared_fts_setup
                    fts_update = None
                    if scenario.family == fts_qualification.FAMILY and scenario.fts_write_case != "none":
                        before_write, wal_before_write = _sizes(sample_root)
                        update_start = time.perf_counter_ns()
                        with okto_grafx.connect(sample_root, **measured_options) as maintenance_db:
                            commit_times = fts_qualification.apply_write_case(
                                maintenance_db, scenario, sample_root)
                        update_elapsed = time.perf_counter_ns() - update_start
                        updated, updated_wal = _sizes(sample_root)
                        fts_update = {"elapsed_ns": update_elapsed,
                                      "durable_acks": len(commit_times),
                                      "commit_ns": list(commit_times),
                                      "disk_allocated_delta_bytes": updated - before_write,
                                      "wal_allocated_delta_bytes": updated_wal - wal_before_write,
                                      "scope": "separate_durable_units_before_cold_reopen",
                                      "write_case": scenario.fts_write_case,
                                      "writer_origin": "second_handle" if scenario.fts_write_case == "external_mixed" else "same_handle"}
                    with okto_grafx.connect(sample_root, **measured_options) as db:
                        if independent and scenario.family != fts_qualification.FAMILY:
                            build_fixture(db, scenario, apply_updates=scenario.reader != "retained")
                        report["metrics_sink"] = type(db._queries._metrics.sink.inner).__name__
                        if db._queries._metrics.enabled:
                            raise CampaignError("default metrics sink unexpectedly enabled")
                        if scenario.family == "write" and scenario.writers > 1:
                            continue_sample = False
                        else:
                            continue_sample = True
                        if scenario.cache == "warm" and scenario.family not in ("write", "capacity", uniqueness_qualification.FAMILY):
                            query, params = query_for(scenario)
                            if scenario.family in read_lifecycle_qualification.FAMILIES:
                                read_lifecycle_qualification.execute(db, scenario)
                            elif scenario.family == "fts":
                                _fts_rows(db, scenario)
                            elif scenario.family == fts_qualification.FAMILY:
                                fts_qualification.rows(db, scenario)
                            else:
                                db.execute(query, params)
                        before, wal_before = _sizes(sample_root)
                        if scenario.family == fts_qualification.FAMILY and scenario.fts_updates == 1:
                            update_start = time.perf_counter_ns()
                            commit_times = fts_qualification.apply_updates(db, scenario)
                            update_elapsed = time.perf_counter_ns() - update_start
                            updated, updated_wal = _sizes(sample_root)
                            fts_update = {"elapsed_ns": update_elapsed, "durable_acks": len(commit_times),
                                          "commit_ns": list(commit_times),
                                          "disk_allocated_delta_bytes": updated - before,
                                          "wal_allocated_delta_bytes": updated_wal - wal_before,
                                          "scope": "one_update_transaction_after_cached_seed"}
                            before, wal_before = updated, updated_wal
                        if continue_sample:
                            if scenario.family == "capacity":
                                from bench.harness.capacity_qualification import measure
                                sample = measure(db, scenario, index, measure_statement)
                            elif scenario.reader == "retained":
                                with db.begin("read") as reader:
                                    reader.execute("MATCH (n:N) RETURN count(n)")
                                    update_start = time.perf_counter_ns()
                                    commit_times = apply_churn(db, scenario)
                                    if scenario.family == fts_qualification.FAMILY:
                                        update_elapsed = time.perf_counter_ns() - update_start
                                        updated, updated_wal = _sizes(sample_root)
                                        fts_update = {"elapsed_ns": update_elapsed,
                                                      "durable_acks": len(commit_times),
                                                      "commit_ns": list(commit_times),
                                                      "disk_allocated_delta_bytes": updated - before,
                                                      "wal_allocated_delta_bytes": updated_wal - wal_before,
                                                      "scope": "four_separate_update_transactions_after_reader_pin"}
                                    before, wal_before = _sizes(sample_root)
                                    sample = measure_statement(db, scenario, index, reader=reader)
                            else:
                                sample = measure_statement(db, scenario, index)
                            if scenario.family == fts_qualification.FAMILY:
                                sample["fts_setup"] = fts_setup
                                if fts_update is not None:
                                    sample["fts_maintenance"] = fts_update
                    if scenario.family in read_lifecycle_qualification.FAMILIES:
                        sample["statistics"]["diagnostic_handle_close_complete"] = int(db.close_complete)
                        if not db.close_complete:
                            raise CampaignError("read recipe handle did not finish closing")
                    if not continue_sample:
                        sample = measure_concurrent_write(sample_root, scenario, index)
                    after, wal_after = _sizes(sample_root)
                    sample.update(disk_delta_bytes=after-before, wal_delta_bytes=wal_after-wal_before)
                    if scenario.family == uniqueness_qualification.FAMILY:
                        # A read-only open requires a completed checkpoint. Do
                        # recovery/checkpoint outside the timed ACK interval.
                        with okto_grafx.connect(sample_root, **options) as recovery:
                            recovery.checkpoint()
                        with okto_grafx.connect(sample_root, read_only=True, **options) as reopened:
                            if canonical_hash(reopened.execute(uniqueness_qualification.READBACK).rows) != canonical_hash(oracle(scenario)):
                                raise CampaignError("RF08 reopen oracle mismatch")
                            if reopened.verify("all").findings:
                                raise CampaignError("RF08 reopen verification found corruption")
                        sample["unique_create"]["reopened"] = True
                    if scenario.family == fts_qualification.FAMILY and scenario.fts_write_case != "none":
                        with okto_grafx.connect(sample_root, **options) as recovery:
                            recovery.checkpoint()
                        with okto_grafx.connect(sample_root, read_only=True, **options) as reopened:
                            found, _ = fts_qualification.rows(reopened, scenario)
                            if canonical_hash(found) != canonical_hash(oracle(scenario)):
                                raise CampaignError("RF11 read-only reopen oracle mismatch")
                            if reopened.verify("all").findings:
                                raise CampaignError("RF11 reopen verification found corruption")
                        sample["fts_maintenance"]["read_only_reopen_verified"] = True
                    report["samples"].append(sample)
                    output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
                    # Write results are rechecked through a new handle after ACK.
                    if scenario.family == "write":
                        with okto_grafx.connect(sample_root, **options) as reopened:
                            if canonical_hash(reopened.execute("MATCH (n:W) RETURN n.id,n.payload ORDER BY n.id").rows) != canonical_hash(oracle(scenario)):
                                raise CampaignError("write reopen oracle mismatch")
                if rf06_split and scenario.role == "control":
                    with okto_grafx.connect(root, **measured_options) as proof_db:
                        proof = rf06_memory_retention_proof(proof_db, scenario)
                    proof.update(arm=report["arm"], revision=report["revision"],
                                 invocation_id=report["invocation_id"], process_id=report["process_id"],
                                 scenario_id=scenario.name,
                                 scenario_hash=canonical_hash(scenario.to_dict()),
                                 query_memory_budget_bytes=scenario.query_memory_budget_bytes)
                    report.setdefault("rf06_memory_proofs", {})[scenario.name] = proof
            output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
        report["cpu_after_percent"] = psutil.cpu_percent(interval=.2)
        if historical and request["arm"] == "B":
            with tempfile.TemporaryDirectory(prefix="memory-evidence-", dir=output.parent) as directory:
                budgets = {proof_budget} if unbudgeted else {Scenario.from_dict(spec).query_memory_budget_bytes for spec in request["scenarios"]}
                report["candidate_memory_evidence"] = collect_candidate_memory(report, budgets, directory)
        if _git(source, "rev-parse", "HEAD") != revision or _git(source, "status", "--porcelain"):
            raise CampaignError("source changed during worker execution")
    except BaseException as exc:
        report["error"] = error_record(exc)
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
