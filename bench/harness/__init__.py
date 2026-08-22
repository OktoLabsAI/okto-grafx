"""The baseline and calibration harness (SPEC-M1 FR-15, AC-14, decision D5).

D5 states three RELATIVE ceilings against the LadybugDB 0.16 reference engine, not three absolute
latencies: durable commit within 10x, point read within 5x, open-with-replay within 3x. So every
number here is a ratio of two measurements taken on the same machine in the same session, and a
measurement is never a single number -- ``measure.py`` keeps every sample and reports the spread,
because a mean with no spread cannot be reproduced or argued with.

Modules
-------
``measure``     timing primitives: warm-up discarded, samples kept, median and spread reported.
``grafx_ops``   the three operations driven through the real engine, on the shipped adapters.
``ladybug_ops`` the same three operations on LadybugDB 0.16, run in a subprocess so a native
                crash in the reference engine is UNMEASURED here rather than a dead harness.
``calibrate``   runs both sides, computes the three multiples, publishes them through the
                ``MetricsSink`` port and writes ``bench/calibration.json``.
``gate``        the CI gate. It reads the published METRIC, not a log, and fails when a ceiling
                is exceeded -- with the 'consult JP' state FR-15 requires when durable commit
                exceeds 10x.
"""

from __future__ import annotations

__all__: list[str] = []
