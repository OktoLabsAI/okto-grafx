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
    # controls beside the mixed lease/batch interaction slices. Batch size
    # changes total fixture rows here, so those slices show load scaling,
    # not a same-work causal setting comparison.
    slices += [(64, 512, lease, batch, "external") for lease, batch in (
        (1, 32), (32, 32), (128, 32), (512, 32),
        (64, 1), (64, 128), (64, 512),
        (1, 1), (32, 128), (512, 512))]
    result = [scenario_type(f"rf12-{case}-buf{buffer}-cp{interval}-lease{lease}-batch{batch}",
                          "capacity", nodes=batch, edges_per_node=0, write_rows=batch,
                          capacity_case=case, buffer_budget_bytes=buffer << 20,
                          checkpoint_interval_records=interval, identity_lease_size=lease,
                          query_memory_budget_bytes=1 << 20)
            for buffer, interval, lease, batch, case in slices]
    # These four cuts keep all 512 records and every setting fixed except
    # the number of records in each durable external-writer transaction.
    result.extend(scenario_type(f"rf12-external-total512-lease64-txbatch{batch}",
                                "capacity", nodes=512, edges_per_node=0, write_rows=512,
                                capacity_case="external", capacity_batch_size=batch,
                                query_memory_budget_bytes=1 << 20)
                  for batch in (1, 32, 128, 512))
    return result


def qualification_partitions(scenario_type):
    """The closed RF12 matrix: 18 participations, 15 distinct scenarios.

    Repeated controls are intentional. A runner must execute each partition in
    its own campaign, including a fresh observation of that partition's control.
    The six remaining catalog cases are descriptive load/interaction cuts.
    """
    catalog = {scenario.name: scenario for scenario in matrix(scenario_type)}
    names = {
        "Q1": [f"rf12-{case}-buf64-cp512-lease64-batch32"
               for case in ("retained", "released", "external")],
        "Q2": [f"rf12-released-buf{budget}-cp512-lease64-batch32"
               for budget in (16, 64, 256)],
        "Q3": [f"rf12-released-buf64-cp{interval}-lease64-batch32"
               for interval in (128, 512, 2048)],
        "Q4": [f"rf12-external-buf64-cp512-lease{lease}-batch32"
               for lease in (1, 32, 64, 128, 512)],
        "Q5": [f"rf12-external-total512-lease64-txbatch{batch}"
               for batch in (1, 32, 128, 512)],
    }
    return {partition: tuple(catalog[name] for name in members)
            for partition, members in names.items()}


def validate_qualification_partition(scenarios):
    """Refuse a partial, mixed, or edited RF12 temporal qualification."""
    from bench.harness.campaign_fixtures import Scenario

    selected = list(scenarios)
    if not selected or any(s.family != "capacity" for s in selected):
        raise ValueError("RF12 qualification requires one capacity-only partition")
    by_name = {s.name: s.to_dict() for s in selected}
    if len(by_name) != len(selected):
        raise ValueError("RF12 qualification contains duplicate scenarios")
    for partition, members in qualification_partitions(Scenario).items():
        expected = {s.name: s.to_dict() for s in members}
        if by_name == expected:
            return partition
    raise ValueError("RF12 qualification requires exactly one unmodified Q1-Q5 partition")


def qualification_report(campaigns, *, objective):
    """Recompose all five campaigns; a report is never authority to tune defaults.

    ``campaigns`` maps Q1..Q5 to raw campaigns, not to self-declared reports.
    The common campaign validator recomputes semantic, provenance, memory and
    temporal gates for every partition before this aggregate can be complete.
    """
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("a local objective is required")
    if not isinstance(campaigns, dict) or set(campaigns) != {"Q1", "Q2", "Q3", "Q4", "Q5"}:
        raise ValueError("RF12 qualification requires all five independent partitions")
    from bench.harness.campaign_fixtures import Scenario
    from bench.harness.campaign_schema import compare_campaign

    partitions = qualification_partitions(Scenario)
    result = {}
    invocations = set()
    common_revisions = None
    for partition in partitions:
        campaign = campaigns[partition]
        if not isinstance(campaign, dict) or campaign.get("intent") != "qualification":
            raise ValueError(f"RF12 {partition} requires qualification raw")
        if validate_qualification_partition(Scenario.from_dict(s) for s in campaign.get("scenarios", ())) != partition:
            raise ValueError(f"RF12 {partition} contains the wrong scenarios")
        protocol = campaign.get("protocol", {})
        if (campaign.get("calibrated_executor") is not True
                or type(protocol.get("max_cpu_percent")) not in (int, float)
                or protocol["max_cpu_percent"] > 20):
            raise ValueError(f"RF12 {partition} requires a calibrated executor and CPU ceiling <=20%")
        report = compare_campaign(campaign)
        if (report["decision"] != "tradeoff_reported" or report["calibration_valid"] is not True
                or report["noisy"] is not False):
            raise ValueError(f"RF12 {partition} did not pass temporal qualification")
        run_ids = {run["invocation_id"] for run in campaign["runs"]}
        if invocations & run_ids:
            raise ValueError("RF12 partitions reused process invocations or reference raw")
        invocations.update(run_ids)
        revisions = {arm: {run["revision"] for run in campaign["runs"] if run["arm"] == arm}
                     for arm in ("A", "B")}
        if any(len(values) != 1 for values in revisions.values()):
            raise ValueError("RF12 partition has unstable source revision")
        if common_revisions is None:
            common_revisions = revisions
        elif revisions != common_revisions:
            raise ValueError("RF12 partitions must reuse immutable baseline/candidate revisions")
        result[partition] = {
            "campaign_hash": report["campaign_hash"],
            "scenario_names": [s.name for s in partitions[partition]],
            "scenario_count": len(partitions[partition]),
            "decision": report["decision"],
            "comparison_scope": [c["capacity_tradeoff"]["comparison_scope"]
                                 for c in report["comparisons"]],
        }
    return {
        "schema": "grafx-rf12-qualification/v1",
        "objective": objective.strip(),
        "source_revisions": {arm: next(iter(values)) for arm, values in common_revisions.items()},
        "partitions": result,
        "participations": sum(item["scenario_count"] for item in result.values()),
        "distinct_scenarios": len({name for item in result.values() for name in item["scenario_names"]}),
        "decision": "review_local_tradeoffs",
        "qualification_scope": "temporal_partitions_only",
        "checkpoint_foreign_writer_proof": "separate_functional_verification_required",
        "architecture_inspection": "separate_verification_required",
        "change_defaults": False,
        "authorize_vacuum": False,
        "authorize_rehash": False,
        "limits": ["Q1 does not isolate reader ownership latency",
                   "Q2 buffer budget is not process RSS",
                   "Q3 read timing is not isolated checkpoint cost or cadence",
                   "Q4 keeps 32 rows while lease changes",
                   "Q5 keeps 512 rows while transaction size changes",
                   "HASH skew requires a separate investigation and spec"],
    }


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
    if (type(s.capacity_batch_size) is not int or s.capacity_batch_size < 0
            or s.capacity_batch_size > s.write_rows
            or (s.capacity_batch_size and s.capacity_case != "external")):
        raise ValueError("capacity transaction batch requires an external writer and cannot exceed total rows")


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
            "external_transaction_batch_size": s.capacity_batch_size or s.write_rows,
            "comparison_scope": comparison_scope(s)}


def comparison_scope(s):
    """Distinguish same-work lease controls from batch workload scaling."""
    if s.capacity_case != "external":
        return "retained/released reader or single-setting capacity slice"
    if s.capacity_batch_size:
        return "transaction_batch_size_only_same_512_rows_and_lease64" if (
            s.write_rows == 512 and s.identity_lease_size == 64) else "custom_batch_slice_requires_matched_total"
    lease_changed = s.identity_lease_size != 64
    batch_changed = s.write_rows != 32
    if lease_changed and batch_changed:
        return "lease_and_batch_load_interaction; descriptive only"
    if lease_changed:
        return "lease_only_against_external_64_32"
    if batch_changed:
        return "batch_load_slice_same_lease_64; unequal work"
    return "external_64_32_reference"


def validate_batch_selection(scenarios):
    """A qualified batch contrast needs the fixed 512-row reference and peer."""
    selected = [s for s in scenarios if s.family == "capacity" and s.capacity_batch_size]
    if not selected:
        return
    from bench.harness.campaign_fixtures import Scenario
    fixed = {s.name: s.to_dict() for s in matrix(Scenario) if s.capacity_batch_size}
    if (len(selected) < 2 or 32 not in {s.capacity_batch_size for s in selected}
            or any(fixed.get(s.name) != s.to_dict() for s in selected)):
        raise ValueError("RF12 batch qualification requires equal 512-row load and the 32-row reference")


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
        return None
    import okto_grafx
    source = Path(okto_grafx.__file__).resolve().parents[1]
    code = """
import sys,json,time
sys.path.insert(0,sys.argv[1])
from okto_grafx import connect
total=int(sys.argv[4]); batch=int(sys.argv[5]) or total
acks=[]; started=time.perf_counter_ns()
with connect(sys.argv[2],codec='pure',vector_math='pure',**json.loads(sys.argv[3])) as db:
    for offset in range(0,total,batch):
        rows=min(batch,total-offset)
        tx=db.begin('write')
        try:
            if int(sys.argv[5]):
                tx.execute("UNWIND $ids AS id MATCH (n:P {id:id}) SET n.note='after'",
                           {'ids':list(range(offset,offset+rows))})
            else:
                tx.execute("MATCH(n:P) SET n.note='after'")
            ack_start=time.perf_counter_ns()
            report=tx.commit()
            duration=time.perf_counter_ns()-ack_start
            if not report.durable or not report.wrote:
                raise RuntimeError('external writer did not return a durable write ACK')
            acks.append({'offset':offset,'rows':rows,'ack_duration_ns':duration,'durable':True})
        except BaseException:
            if tx.active:
                tx.rollback()
            raise
    db.checkpoint()
print(json.dumps({'total_rows':total,'elapsed_ns':time.perf_counter_ns()-started,
                  'acks':acks,'scope':'external child execute, durable commits and checkpoint; separate from read timing'}),flush=True)
"""
    import json
    process = subprocess.run(
        [sys.executable, "-E", "-P", "-B", "-c", code, str(source),
         str(db.path), json.dumps(connect_options(s)), str(s.write_rows), str(s.capacity_batch_size)],
        capture_output=True, text=True, timeout=600,
    )
    if process.returncode:
        raise ValueError("external capacity writer failed: " + process.stderr[-2000:])
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("external capacity writer returned no structured ACK proof") from exc


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
        external_write = _update(db, s)
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
    from okto_grafx import connect
    expected_after = tuple((i, "after") for i in range(s.nodes))
    with connect(db.path, read_only=True, **connect_options(s)) as reopened:
        if (reopened.execute(query_for(s)[0]).rows != expected_after
                or reopened.verify("all").findings):
            raise ValueError("capacity post-reopen rows or storage verification diverged")
    preparation_ns = time.perf_counter_ns() - started
    if sample is None:
        sample = measure_statement(db, s, index)
    sample["capacity"] = {"before_close": before, "after_close": after,
                          "diagnostic_storage_unchanged": True,
                          "configuration": effective,
                          "minimum_reservation_estimate": max(s.identity_lease_size, s.capacity_batch_size or s.write_rows),
                          "reservation_scope": "lower bound max(lease,transaction rows), not measured allocation or RSS",
                          "lifecycle_ns": preparation_ns,
                          "lifecycle_scope": "update/checkpoint/census/reader lifecycle; includes read only for retained case",
                          "external_writer_ack": s.capacity_case == "external",
                          "external_write": external_write,
                          "reopen_verified": True,
                          "reopened_rows_hash": canonical_hash(expected_after)}
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
    if raw.get("minimum_reservation_estimate") != max(s.identity_lease_size, s.capacity_batch_size or s.write_rows):
        raise ValueError("capacity reservation estimate ignores transaction size")
    if raw.get("external_writer_ack") is not (s.capacity_case == "external"):
        raise ValueError("external writer ACK mismatch")
    expected_after = tuple((i, "after") for i in range(s.nodes))
    if raw.get("reopen_verified") is not True or raw.get("reopened_rows_hash") != canonical_hash(expected_after):
        raise ValueError("capacity post-reopen oracle or verification proof missing")
    proof = raw.get("external_write")
    if s.capacity_case != "external":
        if proof is not None:
            raise ValueError("internal capacity writer cannot claim external ACKs")
        return
    if not isinstance(proof, dict) or proof.get("total_rows") != s.write_rows:
        raise ValueError("external writer total load differs from scenario")
    elapsed = proof.get("elapsed_ns")
    if type(elapsed) is not int or elapsed <= 0:
        raise ValueError("external writer duration missing")
    batch = s.capacity_batch_size or s.write_rows
    expected_units = [(offset, min(batch, s.write_rows - offset))
                      for offset in range(0, s.write_rows, batch)]
    acks = proof.get("acks")
    if not isinstance(acks, list) or len(acks) != len(expected_units):
        raise ValueError("external writer durable ACK count differs from transaction batches")
    durations = []
    for ack, (offset, rows) in zip(acks, expected_units):
        if (not isinstance(ack, dict) or ack.get("offset") != offset or ack.get("rows") != rows
                or ack.get("durable") is not True or type(ack.get("ack_duration_ns")) is not int
                or ack["ack_duration_ns"] <= 0):
            raise ValueError("external writer ACK lacks durable atomic-unit proof")
        durations.append(ack["ack_duration_ns"])
    if sum(durations) > elapsed:
        raise ValueError("external writer ACK durations exceed separate write interval")


def decision_template(report, *, objective):
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("a local objective is required")
    reviewable = (isinstance(report, dict) and report.get("decision") == "tradeoff_reported"
                  and report.get("calibration_valid") is True and report.get("noisy") is False)
    return {"schema": "grafx-capacity-decision/v1", "objective": objective,
            "report_sha256": canonical_hash(report) if report is not None else None,
            "decision": "review_local_tradeoffs" if reviewable else "new_research",
            "template_only": True, "change_defaults": False, "authorize_vacuum": False,
            "comparison_scope": "same-work lease and fixed-512 transaction-batch controls support local review; legacy batch-load and mixed slices remain descriptive",
            "requires": ["recompose raw campaign and semantic gates", "3 pairs/5 warmups/20 retained",
                         "calibration/CPU/dispersion", "per-case RSS/logical/slot bytes and WAL/disk costs",
                         "match equal-work single-variable pairs before attributing a setting benefit; legacy batch-load slices are descriptive",
                         "explicit local objective and acceptance; no universal recommendation"]}
