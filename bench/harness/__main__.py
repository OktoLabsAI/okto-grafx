"""``python -m bench.harness`` runs one calibration; the gate is ``python -m bench.harness.gate``."""

from __future__ import annotations

import sys

from bench.harness.calibrate import main

if __name__ == "__main__":
    sys.exit(main())
