"""Deterministic RF01 product proof without a latency claim.

This is deliberately smaller than the temporal campaign. Each control runs on a
real database with an independent fixture oracle. Runtime work counters and the
candidate's logical-memory observer must agree with the executed path. Timing
fields produced inside the existing worker are ignored: a successful result
never certifies a speedup or the temporal campaign's sample coverage.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from bench.harness.campaign_fixtures import Scenario, build_fixture, oracle
from bench.harness.campaign_schema import canonical_hash
from bench.harness.campaign_worker import measure_statement


SCHEMA = "grafx-structural-qualification/v1"
QUOTA = 4 * 1024 * 1024
CONTROLS = (
    Scenario("constant", "constant", nodes=8, edges_per_node=0),
    Scenario("reader-short-churn0", "node_scan", nodes=64, edges_per_node=0,
             cursor_batch_size=1),
    Scenario("edges-n64-uniform-x1", "edge_scan", nodes=64, edges_per_node=1),
    Scenario("pk-seek-control", "pk_seek", nodes=128, edges_per_node=0,
             keys=(1, 7, 7, 31, 999)),
    Scenario("landing-control", "landing", nodes=128, edges_per_node=0,
             fanout=64),
)
EXPECTED = {
    "constant": (1, "94a131fdbab3cce53e6feb01911a5eaeb938fb673a80cf407e6a5ee65e015372"),
    "reader-short-churn0": (64, "53b3c0898c7b0150800824fa9b2e3f397b442c671cdb2985b26ca972dffe1cfd"),
    "edges-n64-uniform-x1": (64, "6c01e448ecde9349abfc7c2415005e5ea6f86803933592803d7da530b70d5e39"),
    "pk-seek-control": (4, "0e390ee8be7c9026558063c524cf3e9946e7d02fe343028b0d2ebc70d2ea2d3d"),
    "landing-control": (64, "7defa8c53b7793b4aede4e0ff94496079fd21f364b6fa44fd62953e1f4acfaf1"),
    "landing-budgeted-guard": (8, "f7e6613423b647c4dee0b4185c777384733d37d05411e9d40cc1e73270759c4f"),
    "write-ack-reopen": (4, "63671a26eacc4531e9188fbb1db1ca7937977289d1f84d8311f57d376152c440"),
}


def _assert_positive(stats: dict, *names: str) -> None:
    for name in names:
        value = stats.get(name, 0)
        if type(value) is not int or value <= 0:
            raise AssertionError(f"executed work counter absent: {name}")


def run_rf01() -> dict:
    """Execute each RF01 path once, plus a separate budgeted landing guard.

    The budgeted landing cannot execute the batch path. It proves the exact
    fallback, memory bound and answer, while the unbudgeted control above proves
    that the batch path really ran. No wall-clock statistic enters the report.
    """
    import okto_grafx
    from okto_grafx import connect

    source = Path(__file__).resolve().parents[2] / "src"
    if Path(okto_grafx.__file__).resolve().parents[1] != source.resolve():
        raise AssertionError("structural proof imported a Grafx package outside its source snapshot")

    results = []
    with TemporaryDirectory(prefix="grafx-rf01-structural-") as directory:
        root = Path(directory)
        for selected in CONTROLS:
            spec = replace(selected, query_memory_budget_bytes=QUOTA) if selected.family != "landing" else selected
            budget = None if spec.family == "landing" else QUOTA
            with connect(root / spec.name, codec="pure", vector_math="pure",
                         query_memory_budget_bytes=budget) as db:
                build_fixture(db, spec)
                sample = measure_statement(db, spec, 5)
                if db.transactions.open_transactions != 0:
                    raise AssertionError(f"{spec.name}: leaked transaction")
            expected_hash = canonical_hash(oracle(spec))
            if (sample["rows"], expected_hash) != EXPECTED[spec.name] or sample["result_hash"] != expected_hash or sample["expected_result_hash"] != expected_hash:
                raise AssertionError(f"{spec.name}: independent oracle mismatch")
            stats = sample["statistics"]
            if spec.family == "node_scan":
                _assert_positive(stats, "physical_node_versions_examined")
            elif spec.family == "edge_scan":
                _assert_positive(stats, "physical_edge_versions_examined")
            elif spec.family in ("pk_seek", "landing"):
                _assert_positive(stats, "index_probe_keys", "certificate_attempts",
                                 "physical_node_versions_examined")
            if spec.family == "landing":
                _assert_positive(stats, "landing_candidates", "landing_batches")
                if sample["memory"]["logical_memory_peak_bytes"] is not None or sample["memory"]["unavailable_reason"] != "query_memory_budget_disabled":
                    raise AssertionError("unbudgeted landing must not pretend to measure logical memory")
            else:
                memory = sample["memory"]
                peak = memory["logical_memory_peak_bytes"]
                if (memory["unavailable_reason"] is not None or type(peak) is not int
                        or not 0 <= peak <= QUOTA or memory["baseline_retained_bytes"] != 0):
                    raise AssertionError(f"{spec.name}: quota or memory observer invalid: {memory}")
            results.append({
                "scenario": spec.name,
                "oracle_sha256": expected_hash,
                "rows": sample["rows"],
                "statistics": {key: value for key, value in stats.items()
                               if key in {"physical_node_versions_examined",
                                          "physical_edge_versions_examined", "index_probe_keys",
                                          "certificate_attempts", "landing_candidates",
                                          "landing_batches"} or key.startswith("fallback_reason_")},
                "logical_memory": sample["memory"],
            })

        guard = Scenario("landing-budgeted-guard", "landing", nodes=32,
                         edges_per_node=0, fanout=8, query_memory_budget_bytes=QUOTA)
        with connect(root / guard.name, codec="pure", vector_math="pure",
                     query_memory_budget_bytes=QUOTA) as db:
            build_fixture(db, guard)
            sample = measure_statement(db, guard, 5)
            if db.transactions.open_transactions != 0:
                raise AssertionError("budgeted landing leaked transaction")
        expected_hash = canonical_hash(oracle(guard))
        if (sample["rows"], expected_hash) != EXPECTED[guard.name] or sample["result_hash"] != expected_hash or sample["expected_result_hash"] != expected_hash:
            raise AssertionError("budgeted landing oracle mismatch")
        stats = sample["statistics"]
        if stats.get("fallback_reason_memory_guard") != 1 or stats.get("landing_batches", 0) != 0:
            raise AssertionError("budgeted landing did not use the required scalar fallback")
        memory = sample["memory"]
        peak = memory["logical_memory_peak_bytes"]
        if (memory["unavailable_reason"] is not None or type(peak) is not int
                or not 0 <= peak <= QUOTA or memory["baseline_retained_bytes"] != 0):
            raise AssertionError("budgeted landing memory proof invalid")
        results.append({"scenario": guard.name, "oracle_sha256": expected_hash,
                        "rows": sample["rows"],
                        "statistics": {"fallback_reason_memory_guard": 1, "landing_batches": 0},
                        "logical_memory": memory})

        write = Scenario("write-ack-reopen", "write", nodes=1, edges_per_node=0,
                         write_rows=4, payload_bytes=64,
                         query_memory_budget_bytes=QUOTA)
        write_root = root / write.name
        with connect(write_root, codec="pure", vector_math="pure",
                     query_memory_budget_bytes=QUOTA) as db:
            build_fixture(db, write)
            sample = measure_statement(db, write, 5)
            if sample["commit_ns"] is None or db.transactions.open_transactions != 0:
                raise AssertionError("write transaction did not complete with an ACK")
        expected_hash = canonical_hash(oracle(write))
        if (sample["rows"], expected_hash) != EXPECTED[write.name] or sample["result_hash"] != expected_hash or sample["expected_result_hash"] != expected_hash:
            raise AssertionError("write ACK oracle mismatch")
        with connect(write_root, codec="pure", vector_math="pure",
                     query_memory_budget_bytes=QUOTA) as recovered:
            if canonical_hash(recovered.execute("MATCH (n:W) RETURN n.id,n.payload ORDER BY n.id").rows) != expected_hash:
                raise AssertionError("write was not visible after fresh recovery")
            recovered.checkpoint()
        with connect(write_root, read_only=True, codec="pure", vector_math="pure",
                     query_memory_budget_bytes=QUOTA) as reopened:
            readback = reopened.execute("MATCH (n:W) RETURN n.id,n.payload ORDER BY n.id")
            if canonical_hash(readback.rows) != expected_hash or reopened.verify("all").findings:
                raise AssertionError("write was not durable after a fresh read-only open")
        results.append({"scenario": write.name, "oracle_sha256": expected_hash,
                        "rows": sample["rows"], "durable_ack": True,
                        "read_only_reopen_verified": True})

    return {"schema_version": SCHEMA, "decision": "passed",
            "qualification": "structural_functional_consistency_only",
            "latency": "unqualified", "controls": results}


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    print(json.dumps(run_rf01(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
