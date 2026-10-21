"""RF12 capacity recipes; diagnostics never authorize pruning or change defaults.

Timing is only the read query. Fixture writes, external writer, checkpoints,
storage hashes and bloat census are outside that interval and explicitly named.
The decision helper is a review template, not an authenticated qualification.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import subprocess
import sys
import time
from pathlib import Path

from bench.harness.campaign_schema import canonical_hash


def matrix(scenario_type):
    slices = [(64, 512, 64, 32, case) for case in ("retained", "released", "external")]
    slices += [(buffer, 512, 64, 32, "released") for buffer in (16, 256)]
    slices += [(64, interval, 64, 32, "released") for interval in (128, 2048)]
    # The 64/32 external slice above is the reference. Keep one-dimensional
    # controls beside the mixed lease/batch interaction slices.
    slices += [(64, 512, lease, batch, "external") for lease, batch in (
        (1, 32), (32, 32), (128, 32), (512, 32),
        (64, 1), (64, 128), (64, 512),
        (1, 1), (32, 128), (512, 512))]
    return [scenario_type(f"rf12-{case}-buf{buffer}-cp{interval}-lease{lease}-batch{batch}",
                          "capacity", nodes=batch, edges_per_node=0, write_rows=batch,
                          capacity_case=case, buffer_budget_bytes=buffer << 20,
                          checkpoint_interval_records=interval, identity_lease_size=lease,
                          query_memory_budget_bytes=1 << 20)
            for buffer, interval, lease, batch, case in slices]


def validate(s):
    if s.capacity_case not in ("retained", "released", "external"):
        raise ValueError("invalid capacity case")
    for value in (s.buffer_budget_bytes, s.checkpoint_interval_records,
                  s.identity_lease_size, s.write_rows, s.query_memory_budget_bytes):
        if type(value) is not int or value <= 0:
            raise ValueError("capacity config must be explicit positive integers")
    if s.nodes != s.write_rows or s.reader != "short" or s.writers != 1 or s.churn_percent:
        raise ValueError("capacity recipe owns its retained reader and one update batch")
    if s.cursor_batch_size or s.cache != "warm":
        raise ValueError("capacity recipe uses a materialized warm read")


def connect_options(s):
    validate(s)
    return {key: getattr(s, key) for key in (
        "buffer_budget_bytes", "checkpoint_interval_records", "identity_lease_size",
        "query_memory_budget_bytes")}


def oracle(s):
    validate(s)
    note = "before" if s.capacity_case == "retained" else "after"
    return tuple((i, note) for i in range(s.nodes))


def query_for(s):
    validate(s)
    return "MATCH(n:P) RETURN n.id,n.note ORDER BY n.id", {}


def contract(s):
    validate(s)
    return {"result": "exact ordered INT64 ids and STRING notes",
            "snapshot": "before update" if s.capacity_case == "retained" else "after update",
            "transaction": "one read statement; update batch commits before measurement",
            "durability": "default synchronous ACK; explicit checkpoints outside query timing",
            "errors": "success required; no replay",
            "case": s.capacity_case, "diagnostic": "read-only; horizon eligibility is not vacuum safety",
            "memory": "query logical peak, process lifetime RSS, and slot bytes are distinct",
            "timing": "read query only; no attribution of write/checkpoint speed to this query",
            "comparison_scope": comparison_scope(s)}


def comparison_scope(s):
    """Label which external settings can support a one-variable comparison."""
    if s.capacity_case != "external":
        return "retained/released reader or single-setting capacity slice"
    lease_changed = s.identity_lease_size != 64
    batch_changed = s.write_rows != 32
    if lease_changed and batch_changed:
        return "lease_and_batch_interaction; descriptive only"
    if lease_changed:
        return "lease_only_against_external_64_32"
    if batch_changed:
        return "batch_only_against_external_64_32"
    return "external_64_32_reference"


def build_fixture(db, s):
    validate(s)
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE P(id INT64, note STRING, PRIMARY KEY(id))")
    with db.begin("write") as tx:
        tx.execute("UNWIND $ids AS id CREATE (:P {id:id,note:'before'})", {"ids": list(range(s.nodes))})
    db.checkpoint()


def _image(db):
    device = db._storage
    return tuple((name, hashlib.sha256(device.read_log(name, 0, device.file_size(name))).hexdigest())
                 for name in sorted(device.list_files()))


def _census(db):
    before = _image(db)
    report = asdict(db.maintenance.bloat("P"))
    if before != _image(db):
        raise ValueError("diagnostic changed storage")
    return report


def _update(db, s):
    if s.capacity_case != "external":
        with db.begin("write") as tx:
            tx.execute("MATCH(n:P) SET n.note='after'")
        return
    import okto_grafx
    source = Path(okto_grafx.__file__).resolve().parents[1]
    code = """
import sys,json
sys.path.insert(0,sys.argv[1])
from okto_grafx import connect
with connect(sys.argv[2],codec='pure',vector_math='pure',**json.loads(sys.argv[3])) as db:
    with db.begin('write') as tx:
        tx.execute("MATCH(n:P) SET n.note='after'")
    db.checkpoint()
print('ACK',flush=True)
"""
    import json
    process = subprocess.run(
        [sys.executable, "-E", "-P", "-B", "-c", code, str(source),
         str(db.path), json.dumps(connect_options(s))],
        capture_output=True, text=True, timeout=45,
    )
    if process.returncode or process.stdout.strip() != "ACK":
        raise ValueError("external capacity writer failed: " + process.stderr[-2000:])


def measure(db, s, index, measure_statement):
    validate(s)
    effective = {"buffer_budget_bytes": db._pool.budget_bytes,
                 "identity_lease_size": db._transactions._identity_lease_size,
                 "checkpoint_interval_records": db._checkpoint_interval_records,
                 "query_memory_budget_bytes": db._queries._query_memory_budget_bytes}
    if effective != connect_options(s):
        raise ValueError("capacity runtime configuration mismatch")
    started = time.perf_counter_ns()
    reader = db.begin("read")
    failure = None
    try:
        reader.execute("MATCH(n:P) RETURN count(n)")
        _update(db, s)
        db.checkpoint()
        before = _census(db)
        sample = measure_statement(db, s, index, reader=reader) if s.capacity_case == "retained" else None
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if reader.active:
            try:
                reader.rollback()
            except BaseException as cleanup:
                if failure is None:
                    raise
                failure.add_note(f"capacity reader cleanup failed: {type(cleanup).__name__}")
    db.checkpoint()
    after = _census(db)
    preparation_ns = time.perf_counter_ns() - started
    if sample is None:
        sample = measure_statement(db, s, index)
    sample["capacity"] = {"before_close": before, "after_close": after,
                          "diagnostic_storage_unchanged": True,
                          "configuration": effective,
                          "minimum_reservation_estimate": max(s.identity_lease_size, s.write_rows),
                          "reservation_scope": "lower bound max(lease,batch), not measured allocation or RSS",
                          "lifecycle_ns": preparation_ns,
                          "lifecycle_scope": "update/checkpoint/census/reader lifecycle; includes read only for retained case",
                          "external_writer_ack": s.capacity_case == "external"}
    validate_sample(sample, s)
    return sample


def validate_sample(sample, s):
    raw = sample.get("capacity", {})
    if raw.get("diagnostic_storage_unchanged") is not True:
        raise ValueError("capacity diagnostic lacks read-only proof")
    if raw.get("configuration") != connect_options(s):
        raise ValueError("capacity configuration mismatch")
    for phase in ("before_close", "after_close"):
        report = raw.get(phase, {})
        if (report.get("vacuum_safety_established") is not False
                or report.get("ended_versions") != s.nodes
                or report.get("stored_versions") != 2 * s.nodes):
            raise ValueError("capacity census or vacuum assertion invalid")
    if (raw["before_close"].get("horizon_retained_versions") != s.nodes
            or raw["before_close"].get("horizon_eligible_versions") != 0
            or raw["after_close"].get("horizon_retained_versions") != 0
            or raw["after_close"].get("horizon_eligible_versions") != s.nodes):
        raise ValueError("capacity retained/released horizon mismatch")
    if raw.get("minimum_reservation_estimate") != max(s.identity_lease_size, s.write_rows):
        raise ValueError("capacity lease estimate ignores batch")
    if raw.get("external_writer_ack") is not (s.capacity_case == "external"):
        raise ValueError("external writer ACK mismatch")


def decision_template(report, *, objective):
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("a local objective is required")
    reviewable = (isinstance(report, dict) and report.get("decision") == "tradeoff_reported"
                  and report.get("calibration_valid") is True and report.get("noisy") is False)
    return {"schema": "grafx-capacity-decision/v1", "objective": objective,
            "report_sha256": canonical_hash(report) if report is not None else None,
            "decision": "review_local_tradeoffs" if reviewable else "new_research",
            "template_only": True, "change_defaults": False, "authorize_vacuum": False,
            "comparison_scope": "one-variable external controls support local review; mixed lease/batch slices describe interactions only",
            "requires": ["recompose raw campaign and semantic gates", "3 pairs/5 warmups/20 retained",
                         "calibration/CPU/dispersion", "per-case RSS/logical/slot bytes and WAL/disk costs",
                         "match same-load single-variable pairs before attributing a benefit",
                         "explicit local objective and acceptance; no universal recommendation"]}
