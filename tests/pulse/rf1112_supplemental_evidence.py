"""Closed Pulse unittest selectors for RF11/RF12 functional guards.

The selected pytest functions are committed source tests. These methods never
launch a timed campaign or approve a configuration change.
"""

from collections import Counter
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
    "rf11_contract": (
        "tests/bench/test_fts_contract_guards.py::"
        "test_default_wal_and_optin_durable_keep_exact_ranking_and_capability_boundary",
    ),
    "rf11_setup_count": (
        "tests/bench/test_fts_setup_observations.py::"
        "test_report_counts_one_shared_setup_per_process_and_three_per_arm",
    ),
    "rf11_setup_refusal": (
        "tests/bench/test_fts_setup_observations.py::"
        "test_qualification_rejects_forged_independence_and_changed_shared_setup",
    ),
    "rf12_foreign_checkpoint": tuple(
        "tests/bench/test_capacity_operational_proofs.py::"
        f"test_foreign_commit_survives_checkpoint_with_retained_reader[{interval}]"
        for interval in (128, 512, 2048)
    ),
    "rf12_hash_skew": (
        "tests/bench/test_capacity_operational_proofs.py::"
        "test_hash_skew_diagnosis_is_bounded_read_only_and_cannot_authorize_rehash",
    ),
}


def _verify_junit_cases(path: Path, selectors: tuple[str, ...]) -> None:
    """Require each exact pytest case once, including every parameter value."""
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
    if totals["tests"] != len(cases) or totals["tests"] != len(selectors):
        raise ValueError("JUnit case count is incomplete or inconsistent")
    if any(totals[key] for key in ("errors", "failures", "skipped")):
        raise ValueError("JUnit suite contains errors, failures or skips")
    expected = Counter(
        (source.removesuffix(".py").replace("/", "."), name)
        for source, name in (selector.split("::", 1) for selector in selectors)
    )
    observed = Counter()
    for case in cases:
        if any(case.find(tag) is not None for tag in ("error", "failure", "skipped")):
            raise ValueError("JUnit case contains error, failure or skip")
        observed[(case.attrib.get("classname", ""), case.attrib.get("name", ""))] += 1
    if observed != expected:
        raise ValueError("JUnit exact selector coverage incomplete")


class RF1112SupplementalEvidence(unittest.TestCase):
    """One authenticated runner method per independent contract boundary."""

    __test__ = False

    def _check(self, key: str) -> None:
        selectors = _SELECTORS[key]
        with tempfile.TemporaryDirectory(prefix="grafx-rf1112-evidence-") as temp:
            junit = Path(temp) / "junit.xml"
            command = [
                sys.executable, "-m", "pytest", "-q", "-o", "addopts=",
                "--maxfail=1", "--tb=short", "-p", "no:cacheprovider",
                f"--junitxml={junit}", *selectors,
            ]
            try:
                completed = subprocess.run(
                    command, cwd=_ROOT, text=True, encoding="utf-8", errors="replace",
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=_TIMEOUT_SECONDS, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                self.fail(f"Selector set {key} timed out after {_TIMEOUT_SECONDS}s: {exc}")
            self.assertEqual(
                completed.returncode, 0,
                f"Selector set {key} failed (exit {completed.returncode}):\n"
                f"{completed.stdout[-8000:]}",
            )
            try:
                _verify_junit_cases(junit, selectors)
            except (OSError, ElementTree.ParseError, ValueError) as exc:
                self.fail(f"Selector set {key} has invalid JUnit evidence: {exc}")

    def test_rf11_default_and_optin_exact_ranking(self):
        self._check("rf11_contract")

    def test_rf11_setup_observation_count(self):
        self._check("rf11_setup_count")

    def test_rf11_setup_observation_refusals(self):
        self._check("rf11_setup_refusal")

    def test_rf12_foreign_checkpoint_with_retained_reader(self):
        self._check("rf12_foreign_checkpoint")

    def test_rf12_bounded_read_only_hash_skew_diagnosis(self):
        self._check("rf12_hash_skew")
