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
import math
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


def _strip_stale_gauge(metrics: Path) -> None:
    """Write step (0): remove EVERY existing recall gauge entry before anything else runs.

    A rerun over a metrics document that already carries the gauge would otherwise leave a
    STALE value satisfying the gate if this run crashed between the section append and the
    new gauge append. Stripping first turns every crash window into gauge-ABSENT -- which a
    ``--require-recall`` gate fails as UNMEASURED -- and a happy rerun ends with exactly one
    entry. Duplicates are removed wholesale; the write is the same atomic read-modify-write
    through a temporary file and ``os.replace`` as every other append. When the document
    holds NO recall entry -- the first run, and every legacy document -- this is a strict
    no-op: no rewrite, no format churn, no mtime change, so a failure before any append
    still leaves both documents byte-identical. The document is guaranteed readable by the
    caller's pre-validation, so nothing is swallowed here: any residual failure propagates
    and aborts the stage BEFORE the worker is spawned.

    Stale boundary, stated precisely: os.replace is atomic, so a failure BEFORE the replace
    leaves the OLD document -- stale gauge included -- fully intact. In that case this
    stage exits 3 and the workflow stops before the gate step ever runs, which is the
    containment this cycle relies on. A contract that ran the gate INDEPENDENTLY of the
    stage exit would need generation binding between section and gauge; that evolution is
    recorded here rather than half-built.
    """
    document = json.loads(metrics.read_text(encoding="utf-8"))
    entries = document.get("metrics") if isinstance(document, dict) else None
    if not isinstance(entries, list) or not any(
        isinstance(entry, dict) and entry.get("name") == RECALL_METRIC
        for entry in entries
    ):
        return

    def mutate(document: dict[str, object]) -> None:
        stale = document.get("metrics")
        if isinstance(stale, list):
            document["metrics"] = [
                entry
                for entry in stale
                if not (isinstance(entry, dict) and entry.get("name") == RECALL_METRIC)
            ]

    _replace_json(metrics, mutate)


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
    timeout_seconds: float | None = None,
) -> int:
    """Run the recall stage and append its results in the fail-safe order; return exit code.

    Zero means both appends landed. Any failure — the worker refusing, a missing document, a
    torn append — returns non-zero WITHOUT touching what was already written before the
    failure point, so the legacy outputs always survive and a partial vector publication can
    only ever be section-without-gauge, never the reverse.
    """
    if metrics is not None and out is None:
        print(
            "vector recall stage: REFUSED -- --metrics without --out would publish the "
            "gauge with no section; the gauge must be the LAST artifact, never the only one."
        )
        return 3
    for label, path in (("--out", out), ("--metrics", metrics)):
        if path is None:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as failure:
            print(
                f"vector recall stage: REFUSED -- {label} {path} is not a readable JSON "
                f"document ({failure}); nothing was run and nothing was written."
            )
            return 3
        if not isinstance(document, dict):
            print(
                f"vector recall stage: REFUSED -- {label} {path} holds "
                f"{type(document).__name__}, not an object; nothing was run."
            )
            return 3
        if (
            label == "--metrics"
            and "metrics" in document
            and not isinstance(document["metrics"], list)
        ):
            print(
                f"vector recall stage: REFUSED -- {label} {path} carries a 'metrics' key "
                "that is not a list; a gauge could never land there. Nothing was run."
            )
            return 3
    try:
        if metrics is not None:
            _strip_stale_gauge(metrics)
        verdict = run_recall(
            profile,
            gt_mode=gt_mode,
            scratch=workspace / "recall",
            timeout_seconds=timeout_seconds,
        )
    except RecallStageError as failure:
        print(f"vector recall: FAIL-CLOSED -- {failure}")
        return 3
    except (OSError, ValueError, TypeError, RuntimeError) as failure:
        # An ORDINARY exception from the strip or the worker is still a stage failure:
        # typed line, exit 3, no traceback -- and because the strip precedes the spawn,
        # a strip failure aborts before any measurement begins.
        print(f"vector recall: stage failed before publication -- {failure}")
        return 3
    # The verdict's gauge is validated BEFORE any publication: an exact non-bool number,
    # convertible without OverflowError (a huge integer raises inside float() itself),
    # finite, in [0, 1]. A worker that emitted anything else did not measure a ratio, and
    # normalizing junk into the metrics document would hand the gate a lie -- so nothing
    # vectorial is written and the stage exits 3 typed, with out/metrics exactly as the
    # strip left them. The value is deliberately NOT interpolated into the message: a
    # huge integer's repr can itself raise past CPython's digit limit.
    gauge = verdict.get("gauge") if isinstance(verdict, dict) else None
    gauge_valid = not isinstance(gauge, bool) and type(gauge) in (int, float)
    gauge_value = 0.0
    if gauge_valid:
        try:
            gauge_value = float(gauge)  # type: ignore[arg-type]
        except OverflowError:
            gauge_valid = False
        else:
            gauge_valid = math.isfinite(gauge_value) and 0.0 <= gauge_value <= 1.0
    if not gauge_valid:
        print(
            "vector recall: FAIL-CLOSED -- the worker verdict's gauge is not a finite "
            "ratio in [0, 1]; nothing vectorial was published."
        )
        return 3
    try:
        section = build_section(verdict)
        if out is not None:
            _append_section(out, section)
        if metrics is not None:
            _append_gauge(metrics, gauge_value)
    except (OSError, ValueError, TypeError, RuntimeError) as failure:
        print(f"vector recall: publication failed after measurement -- {failure}")
        return 3
    home = next(iter(section["observed"]))  # type: ignore[call-overload]
    print(
        f"vector recall: profile {profile} mean recall@k "
        f"{gauge_value:.4f} ({home}); section first, gauge last."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the vector stage as its own CI step, fail-closed by exit code.

    The CI job runs the legacy harness first (its own step, with the legacy wheel-absence
    tolerance), then THIS entrypoint without any tolerance: a vector failure fails the job
    before the gate step ever runs, and the appends keep the fixed order — section first,
    gauge last.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m bench.harness.recall_wiring")
    parser.add_argument("--profile", required=True, choices=("smoke", "full", "tiny"))
    parser.add_argument("--gt", default="auto", choices=("auto", "pure"))
    parser.add_argument(
        "--out", default=None, help="calibration document to append into"
    )
    parser.add_argument(
        "--metrics", default=None, help="metrics document to append into"
    )
    parser.add_argument(
        "--workspace", required=True, help="scratch directory for the worker"
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
        help="override the per-profile wall-clock ceiling (defaults to PROFILE_TIMEOUTS)",
    )
    arguments = parser.parse_args(argv)
    return append_vector_recall(
        profile=arguments.profile,
        gt_mode=arguments.gt,
        out=Path(arguments.out) if arguments.out else None,
        metrics=Path(arguments.metrics) if arguments.metrics else None,
        workspace=Path(arguments.workspace),
        timeout_seconds=arguments.timeout_seconds,
    )


if __name__ == "__main__":
    import sys

    sys.exit(main())
