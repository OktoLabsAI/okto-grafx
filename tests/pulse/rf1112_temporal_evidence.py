"""Exact authenticated unittest selectors for the ten RF11/RF12 partitions."""
from __future__ import annotations

import unittest

from bench.harness.rf1112_operational import run


class RF1112TemporalEvidence(unittest.TestCase):
    def _partition(self, name):
        receipt = run(name)
        self.assertEqual(receipt["partition"], name)
        self.assertEqual(receipt["decision"], "tradeoff_reported")

    def test_rf11_p1_source_cache(self):
        self._partition("P1")

    def test_rf11_p2_history(self):
        self._partition("P2")

    def test_rf11_p3_medium(self):
        self._partition("P3")

    def test_rf11_p4_large(self):
        self._partition("P4")

    def test_rf11_p5_write(self):
        self._partition("P5")

    def test_rf12_q1_retention(self):
        self._partition("Q1")

    def test_rf12_q2_buffer(self):
        self._partition("Q2")

    def test_rf12_q3_checkpoint(self):
        self._partition("Q3")

    def test_rf12_q4_lease(self):
        self._partition("Q4")

    def test_rf12_q5_transaction(self):
        self._partition("Q5")
