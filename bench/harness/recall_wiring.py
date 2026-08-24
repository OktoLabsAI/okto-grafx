"""Additive, fail-safe publication of the vector recall stage (C13 v4 §3, atomicity).

The harness computes and writes every legacy output — D5 ceilings, the published metrics
document, the calibration document — unconditionally, exactly as before. THEN this module runs
the recall stage and, only on complete success, appends in a FIXED order:

1. the ``vector_recall`` section into the calibration document (``--out``) — first;
2. the ``oktografx_vector_recall_ratio`` gauge into the metrics document (``--metrics``) — LAST.

The gauge is what the gate consumes, so no reachable state holds a gauge without its section:
a crash between (1) and (2) leaves section-without-gauge, which a ``--require-recall`` gate
reads as UNMEASURED and fails. Every write is read-modify-write through a temporary file and
``os.replace`` in the same directory, so a torn write can never corrupt a legacy document.
Any recall failure returns non-zero with the legacy outputs already intact on disk.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from bench.harness.recall import (
    RECALL_METRIC,
    RecallStageError,
    build_section,
    run_recall,
)


def _replace_json(path: Path, mutate: Callable[[dict[str, object]], None]) -> None:
    """Read a JSON document, apply one mutation, and atomically replace the file."""
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    scratch = path.with_name(path.name + ".c13.tmp")
    scratch.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(scratch, path)


def _append_section(out: Path, section: dict[str, object]) -> None:
    """Write step (1): the calibration document gains its ``vector_recall`` section."""

    def mutate(document: dict[str, object]) -> None:
        document["vector_recall"] = section

    _replace_json(out, mutate)


def _append_gauge(metrics: Path, value: float) -> None:
    """Write step (2), LAST: the metrics document gains the recall gauge the gate reads."""

    def mutate(document: dict[str, object]) -> None:
        entries = document.setdefault("metrics", [])
        if isinstance(entries, list):
            entries.append(
                {
                    "name": RECALL_METRIC,
                    "kind": "gauge",
                    "unit": "ratio",
                    "samples": [{"value": value}],
                }
            )

    _replace_json(metrics, mutate)


def append_vector_recall(
    *,
    profile: str,
    gt_mode: str,
    out: Path | None,
    metrics: Path | None,
    workspace: Path,
) -> int:
    """Run the recall stage and append its results in the fail-safe order; return exit code.

    Zero means both appends landed. Any failure — the worker refusing, a missing document, a
    torn append — returns non-zero WITHOUT touching what was already written before the
    failure point, so the legacy outputs always survive and a partial vector publication can
    only ever be section-without-gauge, never the reverse.
    """
    try:
        verdict = run_recall(profile, gt_mode=gt_mode, scratch=workspace / "recall")
    except RecallStageError as failure:
        print(f"vector recall: FAIL-CLOSED -- {failure}")
        return 3
    section = build_section(verdict)
    try:
        if out is not None:
            _append_section(out, section)
        if metrics is not None:
            _append_gauge(metrics, float(verdict["gauge"]))  # type: ignore[arg-type]
    except OSError as failure:
        print(f"vector recall: publication failed after measurement -- {failure}")
        return 3
    home = next(iter(section["observed"]))  # type: ignore[call-overload]
    print(
        f"vector recall: profile {profile} mean recall@k "
        f"{float(verdict['gauge']):.4f} ({home}); section first, gauge last."  # type: ignore[arg-type]
    )
    return 0
