"""Pulse unittest bridge for closed RF09–RF12 functional pytest selectors.

The module name intentionally does not start with ``test_`` so the regular
pytest suite does not launch a second pytest process. These methods cover
functional assertions only; they never launch timed qualification campaigns
or stand in for the manual architecture inspections.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree


__test__ = False
_ROOT = Path(__file__).resolve().parents[2]
_TIMEOUT_SECONDS = 100
_SELECTORS = {
    "rf09_main": (
        "tests/api/test_ingestion_performance_recipes.py::test_caught_batch_failure_is_lazy_and_preserves_only_prior_work",
        "tests/api/test_ingestion_performance_recipes.py::test_equal_atomic_contracts_survive_reopen",
    ),
    "rf09_boundaries": (
        "tests/api/test_ingestion_performance_recipes.py::test_documented_retry_recomputes_from_fresh_snapshot",
        "tests/api/test_ingestion_performance_recipes.py::test_recipe_never_replays_explicit_refusal_or_acknowledged_commit",
        "tests/bench/test_ingestion_campaign.py::test_refused_ack_is_not_counted_or_replayed",
    ),
    "rf09_compatibility": (
        "tests/api/test_ingestion_performance_recipes.py::test_chunks_have_explicit_durable_prefix_not_whole_import_atomicity",
        "tests/api/test_ingestion_performance_recipes.py::test_budget_refusal_does_not_silently_chunk_or_retry",
        "tests/bench/test_ingestion_campaign.py::test_chunk_failure_keeps_only_confirmed_prefix_and_closes_failed_transaction",
    ),
    "rf09_integration": (
        "tests/bench/test_ingestion_campaign.py::test_matrix_covers_dimensions_and_refuses_implicit_partial_progress",
        "tests/bench/test_ingestion_campaign.py::test_recipe_confirms_real_atomic_units_and_reopens",
        "tests/bench/test_ingestion_campaign.py::test_concurrent_proof_has_individual_memory_and_actual_acks",
    ),
    "rf10_main": (
        "tests/api/test_read_snapshot_recipes.py::test_related_reads_keep_the_snapshot_and_new_transaction_sees_publication",
    ),
    "rf10_boundaries": (
        "tests/api/test_read_snapshot_recipes.py::test_materialized_rows_survive_handle_close_and_partial_cursor_releases_owner",
    ),
    "rf10_compatibility": (
        "tests/api/test_read_snapshot_recipes.py::test_cleanup_preserves_primary_failure_and_releases_stream",
    ),
    "rf10_integration": (
        "tests/api/test_read_snapshot_recipes.py::test_sixteen_primary_keys_explicitly_choose_one_or_sixteen_transactions",
        "tests/bench/test_read_lifecycle_qualification.py::test_read_recipes_and_separate_diagnostics",
    ),
    "rf11_main": (
        "tests/bench/test_fts_qualification.py::test_ranked_fts_oracle_and_source_are_real_for_wal_and_durable",
        "tests/bench/test_fts_qualification.py::test_retained_reader_uses_before_update_corpus_and_score",
        "tests/bench/test_fts_qualification.py::test_wal_delta_source_requires_cached_seed_and_exact_new_score",
    ),
    "rf11_boundaries": (
        "tests/bench/test_fts_qualification_adversarial.py::test_external_writer_keeps_retained_scores_and_corpus",
        "tests/bench/test_fts_qualification_adversarial.py::test_invalid_persisted_summary_refuses_instead_of_census",
    ),
    "rf11_compatibility": (
        "tests/bench/test_fts_qualification.py::test_matrix_covers_statistics_corpus_cache_and_snapshot_slices",
        "tests/bench/test_fts_qualification.py::test_invalid_wal_history_and_large_common_slice_rejected",
    ),
    "rf11_integration": (
        "tests/bench/test_fts_qualification.py::test_ranked_fts_oracle_and_source_are_real_for_wal_and_durable",
        "tests/bench/test_fts_qualification.py::test_matrix_covers_statistics_corpus_cache_and_snapshot_slices",
    ),
    "rf12_main": (
        "tests/bench/test_capacity_qualification.py::test_real_capacity_recipe_keeps_snapshot_and_diagnostic_contract",
    ),
    "rf12_boundaries": (
        "tests/bench/test_capacity_qualification.py::test_real_capacity_recipe_keeps_snapshot_and_diagnostic_contract",
    ),
    "rf12_compatibility": (
        "tests/bench/test_capacity_qualification.py::test_external_lease_and_batch_interactions_have_one_variable_controls",
        "tests/bench/test_capacity_qualification.py::test_equal_work_external_transaction_batches_have_durable_ack_and_reopen",
    ),
    "rf12_integration": (
        "tests/bench/test_capacity_qualification.py::test_capacity_catalog_has_relevant_slices_without_cartesian_explosion",
        "tests/bench/test_capacity_qualification.py::test_decision_template_keeps_missing_or_noisy_evidence_inconclusive",
        "tests/bench/test_capacity_qualification.py::test_capacity_cannot_claim_universal_optimization",
    ),
}


def _verify_junit(path: Path, selectors: tuple[str, ...]) -> None:
    """Reject an incomplete or mis-scoped pytest run even when it exits zero."""
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites or root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("JUnit document has no test suite")
    totals = {key: 0 for key in ("tests", "errors", "failures", "skipped")}
    for suite in suites:
        for key in totals:
            try:
                value = int(suite.attrib[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"JUnit suite lacks integer {key}") from exc
            if value < 0:
                raise ValueError(f"JUnit suite has negative {key}")
            totals[key] += value
    cases = root.findall(".//testcase")
    if totals["tests"] == 0 or totals["tests"] != len(cases):
        raise ValueError("JUnit case count is empty or inconsistent")
    if any(totals[key] for key in ("errors", "failures", "skipped")):
        raise ValueError("JUnit suite contains errors, failures or skips")
    expected = {(path.removesuffix(".py").replace("/", "."), name)
                for path, name in (selector.split("::", 1) for selector in selectors)}
    observed = set()
    for case in cases:
        if any(case.find(tag) is not None for tag in ("error", "failure", "skipped")):
            raise ValueError("JUnit case contains error, failure or skip")
        classname = case.attrib.get("classname", "")
        name = case.attrib.get("name", "").split("[", 1)[0]
        identity = (classname, name)
        if identity not in expected:
            raise ValueError(f"JUnit case outside closed selector set: {classname}.{name}")
        observed.add(identity)
    if observed != expected:
        raise ValueError("JUnit selector coverage incomplete")


class RF0912FunctionalEvidence(unittest.TestCase):
    """Exact, inspectable delegation to pre-existing pytest functions."""

    __test__ = False

    def _check(self, key: str) -> None:
        selectors = _SELECTORS[key]
        with tempfile.TemporaryDirectory(prefix="grafx-functional-") as temp:
            junit = Path(temp) / "junit.xml"
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-o",
                "addopts=",
                "--maxfail=1",
                "--tb=short",
                "-p",
                "no:cacheprovider",
                f"--junitxml={junit}",
                *selectors,
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=_ROOT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                self.fail(f"Functional selector set {key} timed out after {_TIMEOUT_SECONDS}s: {exc}")
            self.assertEqual(
                completed.returncode,
                0,
                f"Functional selector set {key} failed (exit {completed.returncode}):\n"
                f"{completed.stdout[-8000:]}",
            )
            try:
                _verify_junit(junit, selectors)
            except (OSError, ElementTree.ParseError, ValueError) as exc:
                self.fail(f"Functional selector set {key} has invalid JUnit evidence: {exc}")

    def test_rf09_main(self):
        self._check("rf09_main")

    def test_rf09_boundaries(self):
        self._check("rf09_boundaries")

    def test_rf09_compatibility(self):
        self._check("rf09_compatibility")

    def test_rf09_integration(self):
        self._check("rf09_integration")

    def test_rf10_main(self):
        self._check("rf10_main")

    def test_rf10_boundaries(self):
        self._check("rf10_boundaries")

    def test_rf10_compatibility(self):
        self._check("rf10_compatibility")

    def test_rf10_integration(self):
        self._check("rf10_integration")

    def test_rf11_main(self):
        self._check("rf11_main")

    def test_rf11_boundaries(self):
        self._check("rf11_boundaries")

    def test_rf11_compatibility(self):
        self._check("rf11_compatibility")

    def test_rf11_integration(self):
        self._check("rf11_integration")

    def test_rf12_main(self):
        self._check("rf12_main")

    def test_rf12_boundaries(self):
        self._check("rf12_boundaries")

    def test_rf12_compatibility(self):
        self._check("rf12_compatibility")

    def test_rf12_integration(self):
        self._check("rf12_integration")
