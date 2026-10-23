"""Pulse unittest entry for the non-temporal RF01 product proof."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tests.pulse.rf0205_authority_evidence import _verify_junit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


class RF01StructuralEvidence(unittest.TestCase):
    def test_structural_qualification(self) -> None:
        import okto_grafx
        from bench.harness.structural_qualification import run_rf01

        self.assertEqual(Path(okto_grafx.__file__).resolve().parents[1], (ROOT / "src").resolve())
        receipt = run_rf01()
        self.assertEqual(receipt["decision"], "passed")
        self.assertEqual(receipt["latency"], "unqualified")
        self.assertEqual(len(receipt["controls"]), 7)

    def test_memory_retention_and_spill_cleanup(self) -> None:
        """Run all four real quota termination paths, including cancellation."""
        selectors = tuple(
            "tests/query/test_rf01_memory_retention.py::"
            f"test_repeated_statements_leave_no_logical_retention_or_spill[{ending}]"
            for ending in ("exhaust", "close", "cancel", "budget_error")
        )
        with TemporaryDirectory(prefix="grafx-rf01-memory-") as directory:
            report = Path(directory) / "junit.xml"
            command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                       "-o", "addopts=", "--maxfail=1", "--tb=short",
                       f"--junitxml={report}", *selectors]
            completed = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8",
                                       errors="replace", capture_output=True, timeout=90,
                                       check=False)
            diagnostic = (completed.stdout + "\n" + completed.stderr)[-8000:]
            self.assertEqual(completed.returncode, 0, diagnostic)
            self.assertTrue(report.is_file(), diagnostic)
            _verify_junit(report, selectors)


if __name__ == "__main__":
    unittest.main()
