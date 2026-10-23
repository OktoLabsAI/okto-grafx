"""Explicit authenticated unittest selectors for RF02–RF06/RF08 timing."""
from __future__ import annotations

import unittest

from bench.harness.rf0208_operational import run, run_report


class RF0208TemporalEvidence(unittest.TestCase):
    def _partition(self, name):
        receipt = run(name)
        self.assertEqual(receipt["partition"], name)
        self.assertEqual(receipt["decision"], "passed")

    def test_rf02_scalar_unwind(self):
        self._partition("RF02")

    def test_rf03_connected_anchor(self):
        self._partition("RF03")

    def test_rf04_rf03_only(self):
        self._partition("RF04-RF03")

    def test_rf04_rf04_only(self):
        self._partition("RF04-RF04")

    def test_rf04_incremental(self):
        self._partition("RF04-INCREMENTAL")

    def test_rf04_combined(self):
        self._partition("RF04-COMBINED")

    def test_rf04_interaction_report(self):
        self.assertEqual(run_report()["decision"], "qualified")

    def test_rf05_ordered_page(self):
        self._partition("RF05")

    def test_rf06_split_memory(self):
        self._partition("RF06")

    def test_rf08_certified_create(self):
        self._partition("RF08")
