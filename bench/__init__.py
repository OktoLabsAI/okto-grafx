"""Bench, calibration and CI tooling for Okto Grafx (component C13).

This package is test and measurement infrastructure. It is NOT part of the shipped wheel:
``pyproject.toml`` discovers packages only under ``src`` (``[tool.setuptools.packages.find]``),
so nothing here reaches an artefact, and ``tests/foundation/test_packaging.py`` asserts the
built wheel contains ``okto_grafx`` and a single ``.dist-info`` and nothing else, building from
the real tree rather than a pruned copy (SPEC-M1 TR-9, amendment A60/A65).

Two subpackages, two jobs:

``bench.coverage``
    The cross-family coverage check (LESSONS L4, SPEC-M1 TR-8). It reads the junit reports of
    every job in the CI matrix and asks one observational question of each test: was it seen to
    RUN, on at least one job? A test skipped on every family has left the suite, and no skip
    condition, marker or reason string can hide that from an intersection of executed node ids.

``bench.harness``
    The baseline calibration harness (SPEC-M1 FR-15, AC-14, decision D5). It measures Okto Grafx
    and the LadybugDB 0.16 reference on the same operations, reports the three relative multiples
    of D5 with their spread, and publishes them through the ``MetricsSink`` port under
    ``oktografx_baseline_ceiling_multiple``.
"""

from __future__ import annotations

__all__: list[str] = []
