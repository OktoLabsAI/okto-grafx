"""Closed unittest selectors for Pulse evidence over existing pytest tests.

This module is deliberately outside pytest's ``test_*.py`` collection. Pulse's
external runner loads one unittest method by name. Each method executes a fixed
set of native Grafx pytest cases in a child process and checks its JUnit report.
No performance campaign or manual architecture inspection is represented here.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET


_ROOT = Path(__file__).resolve().parents[2]
_CHILD_TIMEOUT_SECONDS = 100


class _NativePytestEvidence(unittest.TestCase):
    def _assert_native_cases(self, *selectors: str) -> None:
        self.assertTrue(selectors)
        for selector in selectors:
            self.assertTrue(selector.startswith(("tests/query/", "tests/bench/")))
            self.assertIn(".py::test_", selector)
            self.assertNotIn("..", selector)
        with TemporaryDirectory(prefix="grafx-pulse-functional-") as directory:
            report = Path(directory) / "results.xml"
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                "--maxfail=1",
                f"--junitxml={report}",
                *selectors,
            ]
            try:
                result = subprocess.run(
                    command,
                    cwd=_ROOT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=_CHILD_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                self.fail(f"Native Grafx cases exceeded {_CHILD_TIMEOUT_SECONDS}s: {exc.cmd[-len(selectors):]}")
            diagnostic = (result.stdout + "\n" + result.stderr)[-4000:]
            self.assertEqual(result.returncode, 0, diagnostic)
            self.assertTrue(report.is_file(), "Native pytest JUnit report missing")
            root = ET.parse(report).getroot()
            cases = root.findall(".//testcase")
            self.assertGreaterEqual(len(cases), len(selectors), diagnostic)
            for case in cases:
                self.assertIsNone(case.find("failure"), diagnostic)
                self.assertIsNone(case.find("error"), diagnostic)
                self.assertIsNone(case.find("skipped"), diagnostic)


class RF06FunctionalEvidence(_NativePytestEvidence):
    def test_quota_main(self) -> None:
        self._assert_native_cases(
            "tests/query/test_quota_bounded_landings.py::test_quota_window_matches_scalar_outcome",
            "tests/query/test_quota_bounded_landings.py::test_invisible_candidates_do_not_spend_path_or_intermediate_quota",
            "tests/bench/test_quota_qualification.py::test_native_refusal_and_success_equal_scalar",
            "tests/bench/test_quota_qualification.py::test_duplicate_destinations_preserve_edges_and_visibility",
        )

    def test_quota_boundaries(self) -> None:
        self._assert_native_cases(
            "tests/query/test_quota_bounded_landings.py::test_expansion_boundary_does_not_validate_next_destination",
            "tests/bench/test_quota_qualification.py::test_corruption_after_zero_remaining_does_not_replace_quota",
            "tests/bench/test_quota_qualification.py::test_native_quota_error_artifact_retains_code_and_details",
        )

    def test_quota_compatibility(self) -> None:
        self._assert_native_cases(
            "tests/query/test_quota_bounded_landings.py::test_wide_quota_groups_are_bounded_with_no_double_charge",
            "tests/bench/test_quota_qualification.py::test_real_batches_and_multiplicity",
        )

    def test_quota_authority(self) -> None:
        self._assert_native_cases(
            "tests/bench/test_quota_qualification.py::test_configured_memory_and_cursor_close_keep_scalar_guard",
            "tests/bench/test_quota_qualification.py::test_worker_refuses_unapplied_quota",
            "tests/query/test_batched_landing_traversal.py::test_batch_certificate_transition_retries_all_or_refuses",
        )

    def test_quota_consumption(self) -> None:
        self._assert_native_cases(
            "tests/query/test_quota_bounded_landings.py::test_nested_pattern_cannot_borrow_outer_statement_quota_window",
            "tests/query/test_batched_landing_without_vectors.py::test_streaming_or_open_consumers_keep_the_scalar_landing",
            "tests/query/test_batched_landing_without_vectors.py::test_owner_staged_write_keeps_the_scalar_overlay",
            "tests/query/test_streaming_landing_guard.py::test_limit_filter_distinct_never_validate_later_destination",
        )


class RF08FunctionalEvidence(_NativePytestEvidence):
    def test_uniqueness_main(self) -> None:
        self._assert_native_cases(
            "tests/query/test_batched_pk_uniqueness.py::test_128_creates_use_two_certified_groups",
            "tests/query/test_batched_pk_uniqueness.py::test_failed_group_replays_scalar_in_order",
            "tests/query/test_batched_pk_uniqueness.py::test_exhausted_group_certificate_does_not_reset_retry_budget",
        )

    def test_uniqueness_boundaries(self) -> None:
        self._assert_native_cases(
            "tests/query/test_batched_pk_uniqueness.py::test_failed_statement_rolls_back_all_occurrences",
            "tests/query/test_batched_pk_uniqueness.py::test_prior_statement_and_deleted_key_reconciliation",
            "tests/query/test_batched_pk_uniqueness.py::test_first_error_matches_scalar_oracle",
            "tests/query/test_batched_pk_uniqueness.py::test_duplicate_after_batch_boundary_has_no_visible_prefix",
            "tests/query/test_batched_pk_uniqueness.py::test_later_corrupt_key_does_not_mask_prior_duplicate",
            "tests/query/test_batched_pk_uniqueness.py::test_replaced_key_and_new_statement_use_fresh_authority",
        )

    def test_uniqueness_concurrency(self) -> None:
        self._assert_native_cases(
            "tests/query/test_batched_pk_uniqueness.py::test_two_processes_cannot_commit_same_keys",
        )

    def test_uniqueness_authority(self) -> None:
        self._assert_native_cases(
            "tests/query/test_batched_pk_uniqueness.py::test_unproved_shapes_keep_scalar_path",
            "tests/query/test_batched_pk_uniqueness.py::test_missing_many_capability_uses_scalar",
            "tests/query/test_batched_pk_uniqueness.py::test_stale_index_preserves_uniqueness",
            "tests/query/test_batched_pk_uniqueness.py::test_case_colliding_table_does_not_read_other_index",
            "tests/query/test_batched_pk_uniqueness.py::test_custom_heap_port_keeps_scalar_validation",
        )
