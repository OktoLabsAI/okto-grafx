"""Closed Pulse selectors for downstream non-temporal product proofs.

Each method delegates to native Grafx pytest cases and checks every exact JUnit
case. The selected cases execute real databases; these methods do not run or
approve a timed qualification campaign.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tests.pulse.rf0205_authority_evidence import _verify_junit


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
_TIMEOUT_SECONDS = 240
_FTS_WRITE = "tests/bench/test_fts_qualification.py::test_write_cost_slices_require_durable_acks_and_reopen_exactly"
_CASES = {
    "rf09_quota_ack_occ": (
        *("tests/bench/test_ingestion_campaign.py::"
          f"test_matrix_central_recipes_preserve_rows_payload_acks_and_one_mib[{shape}]"
          for shape in ("unwind", "statements", "executemany", "chunks", "transactions")),
        "tests/bench/test_ingestion_campaign.py::test_matrix_chunk_recipe_keeps_each_durable_prefix_atomic",
        "tests/bench/test_ingestion_campaign.py::"
        "test_recipe_occ_conflict_rolls_back_without_implicit_replay_then_explicit_retry",
    ),
    "rf10_eight_recipes": tuple(
        "tests/bench/test_read_lifecycle_qualification.py::"
        f"test_all_eight_real_read_recipes_cover_budget_and_release_ownership[rf10-{family}-{cache}]"
        for family in ("read_autocommit", "read_cursor", "read_materialized", "read_shared")
        for cache in ("warm", "reopen")
    ),
    "rf03": (
        "tests/query/test_connected_anchor_seek.py::test_exact_connected_fixture_reduces_anchor_scan_only",
    ),
    "rf05": (
        "tests/bench/test_pagination_campaign.py::test_rf05_qualified_target_executes_at_declared_scale_and_memory_budget",
        *("tests/bench/test_pagination_campaign.py::"
          f"test_rf05_case_has_independent_oracle_and_runtime_regime[{case}]"
          for case in ("first", "membership", "dynamic_membership", "keyset", "skip", "no_index")),
        *("tests/bench/test_pagination_campaign.py::"
          f"test_rf05_other_closed_pages_keep_order_cursor_and_fallback_under_quota[{case}]"
          for case in ("membership", "dynamic_membership", "keyset", "skip", "no_index")),
        "tests/query/test_label_keyset_pagination.py::test_null_timestamp_and_null_keyset_keep_canonical_order",
    ),
    "rf06": (
        "tests/bench/test_rf06_split_memory.py::test_fixed_split_matrix_has_same_traversal_quota_in_each_pair",
        *("tests/bench/test_quota_qualification.py::"
          f"test_128_landings_reduce_real_certificate_work_with_same_quota_and_fixture[{field}]"
          for field in ("max_intermediate_rows", "max_traversal_paths", "max_traversal_expansions")),
        *("tests/bench/test_rf06_split_memory.py::"
          f"test_real_scalar_control_releases_budget_after_repeated_reads[{field}]"
          for field in ("max_intermediate_rows", "max_traversal_paths", "max_traversal_expansions")),
    ),
    "rf08": (
        "tests/bench/test_uniqueness_qualification.py::test_target_and_scalar_oracle_share_rows_but_not_certificate_work",
        "tests/bench/test_uniqueness_qualification.py::test_control_and_catalog_are_explicit",
        "tests/bench/test_uniqueness_qualification.py::test_failed_duplicate_has_no_ack_or_persisted_prefix",
        "tests/query/test_batched_pk_uniqueness.py::test_quota_uses_existing_scalar_memory_frontier",
        "tests/query/test_batched_pk_uniqueness.py::test_intermediate_row_quota_keeps_first_error_in_scalar_order",
        "tests/query/test_batched_pk_uniqueness.py::test_executemany_does_not_advance_generator_after_failure",
    ),
    "rf09_memory": (
        "tests/bench/test_ingestion_campaign.py::test_sequence_observes_retained_growth_and_partial_call_cleanup",
        "tests/bench/test_ingestion_campaign.py::test_sequence_keeps_live_ledger_between_statements_without_summing_peaks",
        "tests/bench/test_ingestion_campaign.py::test_sequence_foreign_owner_and_overlapping_scope_remain_unavailable",
    ),
    "rf10_memory": (
        "tests/bench/test_read_lifecycle_qualification.py::test_multi_statement_recipe_uses_sequence_memory_observer[read_autocommit]",
        "tests/bench/test_read_lifecycle_qualification.py::test_multi_statement_recipe_uses_sequence_memory_observer[read_shared]",
    ),
    "rf11_write": tuple(
        f"{_FTS_WRITE}[{case}-{acks}-{mode}-{history}]"
        for case, acks in (("insert", 1), ("update", 1), ("delete", 1), ("external_mixed", 4))
        for mode, history in (("wal", 0), ("durable", 8))
    ),
}


class DownstreamStructuralEvidence(unittest.TestCase):
    def _budgeted_target(self, scenario, *, expected_rows: int,
                         expected_sha256: str) -> dict:
        import okto_grafx
        from bench.harness.campaign_fixtures import build_fixture, oracle
        from bench.harness.campaign_schema import canonical_hash
        from bench.harness.campaign_worker import measure_statement
        from okto_grafx import connect

        self.assertEqual(Path(okto_grafx.__file__).resolve().parents[1],
                         (ROOT / "src").resolve())
        budget = 4096
        scenario = replace(scenario, query_memory_budget_bytes=budget)
        with TemporaryDirectory(prefix="grafx-budgeted-target-") as directory:
            with connect(Path(directory) / "db", codec="pure", vector_math="pure",
                         query_memory_budget_bytes=budget) as db:
                self.assertEqual(db._queries._query_memory_budget_bytes, budget)
                build_fixture(db, scenario)
                sample = measure_statement(db, scenario, 5)
                self.assertEqual(db.transactions.open_transactions, 0)
                with db._queries._observe_logical_memory() as settled:
                    self.assertEqual(db.execute("RETURN 42").rows, ((42,),))
                state = settled.result()
                self.assertEqual(state["baseline_retained_bytes"], 0)
                self.assertEqual(state["logical_memory_peak_bytes"], 0)
        expected = canonical_hash(oracle(scenario))
        self.assertEqual(sample["rows"], expected_rows)
        self.assertEqual(expected, expected_sha256)
        self.assertEqual(sample["result_hash"], expected)
        self.assertEqual(sample["expected_result_hash"], expected)
        memory = sample["memory"]
        self.assertIsNone(memory["unavailable_reason"])
        self.assertEqual(memory["baseline_retained_bytes"], 0)
        self.assertIs(type(memory["logical_memory_peak_bytes"]), int)
        # The observer sums simultaneous operator/owner tariffs. The public
        # quota is per operator, so that aggregate may exceed 4096.
        self.assertGreaterEqual(memory["logical_memory_peak_bytes"], 0)
        return sample["statistics"]

    def test_rf02_budgeted_scalar_seek(self) -> None:
        from bench.harness.rf02_qualification import matrix

        scenario = next(item for item in matrix() if item.name == "rf02-scalar-n1024-k16")
        statistics = self._budgeted_target(
            scenario, expected_rows=16,
            expected_sha256="1b172a670aa051c865ac01865bce5059ef63307a11cee0a9274496785611baba",
        )
        self.assertEqual(statistics["index_probe_keys"], 16)
        self.assertEqual(statistics["physical_node_versions_examined"], 16)
        self.assertEqual(statistics.get("rows_scanned", 0), 0)

    def test_rf03_budgeted_connected_anchor(self) -> None:
        from bench.harness.connected_qualification import matrix

        scenario = next(item for item in matrix() if item.name == "rf03-connected-n1024-k16")
        statistics = self._budgeted_target(
            scenario, expected_rows=32,
            expected_sha256="3bd41d31ecf6113e36439f54fc8ed3e35567d785ac7c9cfd5417cfffb5cf7daf",
        )
        # Anchor probes and destination identity lookups share this counter.
        self.assertEqual(statistics["index_probe_keys"], 48)
        self.assertLess(statistics["physical_node_versions_examined"], scenario.nodes)
        self.assertGreater(statistics["physical_edge_versions_examined"], 0)

    def _check(self, key: str) -> None:
        import okto_grafx

        self.assertEqual(Path(okto_grafx.__file__).resolve().parents[1],
                         (ROOT / "src").resolve())
        selectors = _CASES[key]
        with TemporaryDirectory(prefix=f"grafx-{key}-structural-") as directory:
            report = Path(directory) / "junit.xml"
            command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                       "-o", "addopts=", "--maxfail=1", "--tb=short",
                       f"--junitxml={report}", *selectors]
            try:
                completed = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8",
                                           errors="replace", capture_output=True,
                                           timeout=_TIMEOUT_SECONDS, check=False)
            except subprocess.TimeoutExpired as exc:
                self.fail(f"{key} structural cases exceeded {_TIMEOUT_SECONDS}s: {exc}")
            diagnostic = (completed.stdout + "\n" + completed.stderr)[-8000:]
            self.assertEqual(completed.returncode, 0, diagnostic)
            self.assertTrue(report.is_file(), diagnostic)
            _verify_junit(report, selectors)

    def test_rf03_anchor_work_and_oracle(self) -> None:
        self._check("rf03")

    def test_rf05_ordered_work_and_memory(self) -> None:
        self._check("rf05")

    def test_rf06_split_memory_and_quota(self) -> None:
        self._check("rf06")

    def test_rf08_certificate_ack_and_reopen(self) -> None:
        self._check("rf08")

    def test_rf09_sequence_memory(self) -> None:
        self._check("rf09_memory")

    def test_rf09_quota_ack_occ(self) -> None:
        self._check("rf09_quota_ack_occ")

    def test_rf10_sequence_memory(self) -> None:
        self._check("rf10_memory")

    def test_rf10_eight_real_recipes(self) -> None:
        self._check("rf10_eight_recipes")

    def test_rf11_write_ack_and_reopen(self) -> None:
        self._check("rf11_write")
