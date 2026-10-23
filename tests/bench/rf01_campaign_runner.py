"""Explicit unittest target for real qualification; never auto-collected by pytest."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class RF01CampaignRunner(unittest.TestCase):
    def test_historical_noop_qualification(self):
        from bench.harness.pulse_qualification import run
        self.assertEqual(run(ROOT)["decision"], "passed")


if __name__ == "__main__":
    unittest.main()
