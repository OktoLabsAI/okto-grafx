"""Closed functional evidence for RF02, RF03 and RF05 authority scenarios.

Pulse loads one unittest method. Each method runs only the committed native
pytest cases named below; no timing or manual inspection is inferred.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from xml.etree import ElementTree


__test__ = False
_ROOT = Path(__file__).resolve().parents[2]
_TIMEOUT_SECONDS = 100
_CASES = {
    "rf02": (
        "tests/query/test_unwind_scalar_seek.py::test_old_snapshot_and_new_snapshot_see_their_own_versions",
        "tests/query/test_unwind_scalar_seek.py::test_external_connection_keeps_old_unwind_snapshot_and_fresh_visibility",
        "tests/query/test_unwind_scalar_seek.py::test_stale_or_absent_authority_keeps_existing_seek_contract[False]",
        "tests/query/test_unwind_scalar_seek.py::test_stale_or_absent_authority_keeps_existing_seek_contract[True]",
        "tests/query/test_unwind_scalar_seek.py::test_generation_retry_after_first_key_never_replays_prefix",
    ),
    "rf03": (
        "tests/query/test_connected_anchor_seek.py::test_owner_overlay_uses_existing_fallback",
        "tests/query/test_connected_anchor_seek.py::test_old_reader_preserves_snapshot_and_parallel_edge_multiplicity",
        "tests/query/test_connected_anchor_seek.py::test_connected_anchor_authority_falls_back_to_canonical_scan[stale]",
        "tests/query/test_connected_anchor_seek.py::test_connected_anchor_authority_falls_back_to_canonical_scan[missing_many]",
        "tests/query/test_node_in_seek.py::test_missing_multi_key_door_executes_the_retained_scan",
        "tests/query/test_node_in_seek.py::test_a_stale_store_executes_the_retained_scan",
        "tests/query/test_connected_anchor_seek.py::test_connected_anchor_uses_existing_multi_key_door",
    ),
    "rf05": (
        "tests/query/test_label_keyset_pagination.py::test_owner_changes_and_stale_index_preserve_fallback",
        "tests/query/test_label_keyset_pagination.py::test_missing_index_uses_canonical_scan",
        "tests/query/test_label_keyset_pagination.py::test_keyset_ties_exact_sequence_and_old_snapshot_with_external_writer",
    ),
}


def _verify_junit(path: Path, selectors: tuple[str, ...]) -> None:
    """Require every exact case once, with no extra, skipped or failed case."""
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites or root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("JUnit document has no test suite")
    totals = {key: 0 for key in ("tests", "errors", "failures", "skipped")}
    for suite in suites:
        for key in totals:
            value = int(suite.attrib[key])
            if value < 0:
                raise ValueError("JUnit suite has negative counts")
            totals[key] += value
    cases = root.findall(".//testcase")
    if totals["tests"] != len(selectors) or len(cases) != len(selectors):
        raise ValueError("JUnit selector coverage incomplete")
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
        raise ValueError("JUnit selector coverage incomplete or outside closed set")


class RF0205AuthorityEvidence(unittest.TestCase):
    __test__ = False

    def _check(self, key: str) -> None:
        selectors = _CASES[key]
        for selector in selectors:
            self.assertTrue(selector.startswith("tests/query/"))
            self.assertIn(".py::test_", selector)
            self.assertNotIn("..", selector)
        with TemporaryDirectory(prefix="grafx-authority-") as directory:
            report = Path(directory) / "junit.xml"
            command = [
                sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "-o", "addopts=", "--maxfail=1", "--tb=short",
                f"--junitxml={report}", *selectors,
            ]
            try:
                result = subprocess.run(
                    command, cwd=_ROOT, text=True, encoding="utf-8",
                    errors="replace", capture_output=True, timeout=_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                self.fail(f"Functional authority cases for {key} timed out: {exc}")
            diagnostic = (result.stdout + "\n" + result.stderr)[-8000:]
            self.assertEqual(result.returncode, 0, diagnostic)
            try:
                _verify_junit(report, selectors)
            except (OSError, ElementTree.ParseError, ValueError) as exc:
                self.fail(f"Invalid functional authority receipt for {key}: {exc}")

    def test_rf02_authority(self) -> None:
        self._check("rf02")

    def test_rf03_authority(self) -> None:
        self._check("rf03")

    def test_rf05_authority(self) -> None:
        self._check("rf05")
