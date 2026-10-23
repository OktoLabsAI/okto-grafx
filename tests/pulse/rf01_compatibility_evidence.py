"""Closed RF01 compatibility proof for an authenticated Pulse runner.

The selected native tests exercise the same statements with no-op, active and
failing metric sinks, plus cancellation, error, durable ACK/reopen, physical
scan/fallback, and logical quota cleanup. This does not certify latency.
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
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
_SELECTORS = (
    "tests/bench/test_rf01_evidence.py::RF01Evidence::test_compatibility_functional",
    "tests/bench/test_rf01_evidence.py::RF01Evidence::test_physical_scan",
    "tests/bench/test_rf01_evidence.py::RF01Evidence::test_fallback_regimes",
    *(
        "tests/query/test_rf01_memory_retention.py::"
        f"test_repeated_statements_leave_no_logical_retention_or_spill[{ending}]"
        for ending in ("exhaust", "close", "cancel", "budget_error")
    ),
)


def _junit_cases(path: Path) -> Counter[tuple[str, str]]:
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if root.tag not in {"testsuite", "testsuites"} or not suites:
        raise ValueError("JUnit suite is absent")
    totals = Counter()
    for suite in suites:
        for key in ("tests", "errors", "failures", "skipped"):
            count = int(suite.attrib[key])
            if count < 0:
                raise ValueError("JUnit count is negative")
            totals[key] += count
    cases = root.findall(".//testcase")
    if totals["tests"] != len(_SELECTORS) or len(cases) != len(_SELECTORS):
        raise ValueError("JUnit case count differs from the closed selector set")
    if any(totals[key] for key in ("errors", "failures", "skipped")):
        raise ValueError("JUnit includes an error, failure or skip")
    observed = Counter()
    for case in cases:
        if any(case.find(tag) is not None for tag in ("error", "failure", "skipped")):
            raise ValueError("JUnit case includes an error, failure or skip")
        observed[(case.attrib.get("classname", ""), case.attrib.get("name", ""))] += 1
    return observed


class RF01CompatibilityEvidence(unittest.TestCase):
    __test__ = False

    def test_native_compatibility_contract(self) -> None:
        import okto_grafx

        self.assertEqual(Path(okto_grafx.__file__).resolve().parents[1],
                         (ROOT / "src").resolve())
        expected = Counter()
        for selector in _SELECTORS:
            parts = selector.split("::")
            source = ROOT / parts[0]
            self.assertTrue(source.is_file())
            self.assertEqual(source.resolve().is_relative_to(ROOT.resolve()), True)
            module = parts[0].removesuffix(".py").replace("/", ".")
            expected[(".".join((module, *parts[1:-1])), parts[-1])] += 1
        with TemporaryDirectory(prefix="grafx-rf01-compat-") as directory:
            report = Path(directory) / "junit.xml"
            command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                       "-o", "addopts=", "--maxfail=1", "--tb=short",
                       f"--junitxml={report}", *_SELECTORS]
            try:
                completed = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8",
                                           errors="replace", capture_output=True, timeout=100,
                                           check=False)
            except subprocess.TimeoutExpired as exc:
                self.fail(f"RF01 native compatibility tests exceeded 100 s: {exc}")
            diagnostic = (completed.stdout + "\n" + completed.stderr)[-8000:]
            self.assertEqual(completed.returncode, 0, diagnostic)
            self.assertTrue(report.is_file(), diagnostic)
            try:
                observed = _junit_cases(report)
            except (OSError, ElementTree.ParseError, ValueError) as exc:
                self.fail(f"Invalid RF01 native JUnit receipt: {exc}")
            self.assertEqual(observed, expected, diagnostic)


if __name__ == "__main__":
    unittest.main()
