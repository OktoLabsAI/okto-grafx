"""Run both sides of the D5 comparison, publish the three multiples, freeze the calibration.

SPEC-M1 FR-15 and AC-14 in one command:

* measure Okto Grafx and LadybugDB 0.16 on the same three operations, on the same machine, in the
  same session, keeping every sample;
* compute the three multiples and compare them to the D5 ceilings -- 10x durable commit, 5x point
  read, 3x open with replay;
* publish each multiple through the ``MetricsSink`` port as
  ``oktografx_baseline_ceiling_multiple{ceiling=...}``, because the CI gate must read the METRIC
  and not a log (SPEC-M1 OR-4);
* write ``calibration.json``: the frozen ``partitions_per_table`` default, the three multiples,
  every sample behind them and the environment they were taken in;
* return an exit status that says what happened, including the ``consult_jp`` state FR-15 requires
  when durable commit exceeds 10x -- at which point nothing else proceeds, because the alternative
  (a fair lease) changes D1 and is the user's decision, not this harness's.

A missing baseline is never a zero. If LadybugDB cannot be imported, crashes or times out, the
ceiling it belongs to is UNMEASURED, the status says so, and the exit code is not success.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from bench.harness.measure import Measurement, Ratio, from_samples, ratio

__all__ = [
    "CEILINGS",
    "CalibrationResult",
    "STATUS_CONSULT_JP",
    "STATUS_EXCEEDED",
    "STATUS_MET",
    "STATUS_UNMEASURED",
    "calibrate",
    "publish",
    "run_baseline",
]

CEILINGS: dict[str, float] = {
    "durable_commit": 10.0,
    "point_read": 5.0,
    "open_replay": 3.0,
}
"""The three relative ceilings of decision D5, keyed by the label of the published metric."""

METRIC_NAME: str = "oktografx_baseline_ceiling_multiple"
"""The metric the CI gate reads. Its label domain is frozen in the C8 catalog."""

STATUS_MET: str = "ceilings_met"
"""Every measured multiple is within its ceiling."""

STATUS_EXCEEDED: str = "ceiling_exceeded"
"""A ceiling other than durable commit was exceeded; the build is red."""

STATUS_CONSULT_JP: str = "consult_jp"
"""Durable commit exceeded 10x. FR-15: the pipeline stops here and the decision is the user's."""

STATUS_UNMEASURED: str = "unmeasured"
"""A side of a comparison could not be taken; nothing is claimed about that ceiling."""

BASELINE_TIMEOUT_SECONDS: float = 600.0
"""Every baseline subprocess is bounded, so a hang in the reference engine is a red result."""


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """The outcome of one calibration run."""

    status: str
    """``ceilings_met``, ``ceiling_exceeded``, ``consult_jp`` or ``unmeasured``."""

    ratios: tuple[Ratio, ...]
    """One ratio per D5 ceiling, in the order of :data:`CEILINGS`."""

    partitions_per_table: int
    """The default this run freezes (CONTRACT section 5, SPEC-M1 FR-15)."""

    environment: dict[str, str]
    """Where and when this was taken: platform, interpreter, engine versions, timestamp."""

    notes: tuple[str, ...] = ()
    """Everything a reader of the numbers must know before believing them."""

    @property
    def exit_code(self) -> int:
        """Return the exit code: 0 met, 1 a ceiling missed or the pipeline stopped, 2 unmeasured."""
        if self.status == STATUS_MET:
            return 0
        if self.status == STATUS_UNMEASURED:
            return 2
        return 1

    def to_dict(self) -> dict[str, object]:
        """Return the calibration as the artefact that is committed."""
        return {
            "format": "okto-grafx-calibration",
            "version": 1,
            "status": self.status,
            "exit_code": self.exit_code,
            "partitions_per_table": self.partitions_per_table,
            "environment": dict(self.environment),
            "notes": list(self.notes),
            "ceilings": [item.to_dict() for item in self.ratios],
        }


def run_baseline(
    operation: str,
    *,
    root: Path,
    iterations: int,
    warmup: int,
    extra: Sequence[str] = (),
    timeout: float = BASELINE_TIMEOUT_SECONDS,
) -> tuple[Measurement, dict[str, object]]:
    """Take one baseline reading in a child process and return it with the raw payload.

    A72/A75.2: the child prints one JSON document. Anything else -- a crash, a timeout, an empty
    stream, a document that does not parse -- returns an UNMEASURED measurement naming what
    happened, never an empty sample list that would read as instant.
    """
    project_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    # A94: a spawned interpreter must not resolve the package through an editable install that
    # points somewhere else. Both roots are pinned here.
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(project_root), str(project_root / "src")]
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        sys.executable,
        "-m",
        "bench.harness.ladybug_ops",
        "--operation",
        operation,
        "--root",
        str(root),
        "--iterations",
        str(iterations),
        "--warmup",
        str(warmup),
        *extra,
    ]
    try:
        finished = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            cwd=str(project_root),
        )
    except subprocess.TimeoutExpired:
        return (
            from_samples(
                f"ladybug {operation}",
                (),
                unmeasured=f"the baseline worker did not finish within {timeout:g} s",
            ),
            {},
        )
    if finished.returncode != 0:
        tail = (finished.stderr or finished.stdout).strip().splitlines()[-3:]
        return (
            from_samples(
                f"ladybug {operation}",
                (),
                unmeasured=(
                    f"the baseline worker exited with {finished.returncode}: "
                    + " | ".join(tail)
                ),
            ),
            {},
        )
    line = finished.stdout.strip().splitlines()[-1] if finished.stdout.strip() else ""
    try:
        payload = json.loads(line)
    except ValueError:
        return (
            from_samples(
                f"ladybug {operation}",
                (),
                unmeasured="the baseline worker printed no readable JSON document",
            ),
            {},
        )
    if operation == "crash":
        return from_samples(f"ladybug {operation}", ()), payload
    samples = payload.get("samples", [])
    if not samples:
        return (
            from_samples(
                f"ladybug {operation}",
                (),
                unmeasured="the baseline worker kept no sample",
            ),
            payload,
        )
    return (
        from_samples(
            f"ladybug {operation}",
            samples,
            warmup=payload.get("warmup", ()),
            detail=str(payload.get("detail", "")),
        ),
        payload,
    )


def _ladybug_version() -> str:
    """Return the installed reference engine version, or why there is none."""
    try:
        import ladybug
    except Exception as error:  # noqa: BLE001 - the reason belongs in the artefact
        return f"absent ({type(error).__name__})"
    return str(getattr(ladybug, "__version__", "unknown"))


def calibrate(
    *,
    workspace: Path,
    iterations: int = 30,
    warmup: int = 5,
    replay_records: int = 2000,
    read_rows: int = 512,
    partitions_per_table: int = 64,
) -> CalibrationResult:
    """Measure both engines and return the three D5 multiples with their spread."""
    from bench.harness import grafx_ops

    notes: list[str] = []
    grafx_root = workspace / "grafx"
    baseline_root = workspace / "ladybug"
    grafx_root.mkdir(parents=True, exist_ok=True)
    baseline_root.mkdir(parents=True, exist_ok=True)

    subject_commit = grafx_ops.measure_durable_commit(
        grafx_root, iterations=iterations, warmup=warmup
    )
    subject_read = grafx_ops.measure_point_read(
        grafx_root,
        iterations=max(iterations, 200),
        warmup=max(warmup, 50),
        rows=read_rows,
    )
    subject_replay = grafx_ops.measure_open_with_replay(
        grafx_root, iterations=iterations, warmup=warmup, records=replay_records
    )

    baseline_commit, _ = run_baseline(
        "durable_commit",
        root=baseline_root / "commit",
        iterations=iterations,
        warmup=warmup,
    )
    baseline_read, _ = run_baseline(
        "point_read",
        root=baseline_root / "read",
        iterations=max(iterations, 200),
        warmup=max(warmup, 50),
        extra=("--rows", str(read_rows)),
    )
    _, crashed = run_baseline(
        "crash",
        root=baseline_root / "crash",
        iterations=1,
        warmup=0,
        extra=("--records", str(replay_records)),
    )
    if crashed.get("crashed"):
        baseline_replay, _ = run_baseline(
            "open_replay",
            root=baseline_root / "replay",
            iterations=iterations,
            warmup=warmup,
            extra=(
                "--crashed",
                str(crashed["crashed"]),
                "--records",
                str(replay_records),
            ),
        )
    else:
        baseline_replay = from_samples(
            "ladybug open_replay",
            (),
            unmeasured="no crashed database could be produced, so nothing was replayed",
        )

    ratios = (
        ratio(
            "durable_commit",
            CEILINGS["durable_commit"],
            subject_commit,
            baseline_commit,
        ),
        ratio("point_read", CEILINGS["point_read"], subject_read, baseline_read),
        ratio("open_replay", CEILINGS["open_replay"], subject_replay, baseline_replay),
    )

    notes.append(
        "The point-read comparison favours Okto Grafx: the reference is asked for a row by "
        "primary key and pays a parse, a plan and an index probe, while the Okto Grafx side "
        "reads one row by RecordRef through a warm buffer pool. There is no query surface to "
        "measure yet (C10/C11 are later waves)."
    )
    notes.append(
        "The open-with-replay comparison is a LOWER BOUND on the Okto Grafx side: the recovery "
        "manager (C6) does not exist in the tree yet, so the measured work is WalManager.open "
        "plus a full checksum-verifying scan of the log, with nothing re-applied to the heap."
    )
    notes.append(
        "Every number is a median of the kept samples with the full sample list beside it. The "
        "p95 and best-case multiples are reported too, because a ceiling met at the median and "
        "missed at the tail is a different fact."
    )
    notes.append(
        "WHAT THE DURABLE-COMMIT NUMBER MEASURES: the samples are taken against ONE database "
        f"that is not reset between them, so kept sample k (0-based) pays for {warmup} + k "
        f"commits already in the log -- the series spans {warmup} to {warmup + iterations - 1} "
        "prior commits. Commit cost on this tree RISES with that count, so the median is a "
        "reading of a rising curve, not a stable per-commit cost, and it moves with the sample "
        "count. It is an honest reading of the operation as a caller meets it -- committing into "
        "a database that already holds commits -- and it is not a constant. A number to be "
        "compared across builds must fix the prior-commit count, and this harness does not: the "
        "count is stated here instead."
    )

    status = STATUS_MET
    if any(not item.ok for item in ratios):
        status = STATUS_UNMEASURED
    else:
        commit_ratio = ratios[0]
        if not commit_ratio.met:
            status = STATUS_CONSULT_JP
            notes.append(
                "FR-15: the durable-commit multiple exceeds 10x, so the pipeline stops in the "
                "'consult JP' state. Trading it for a fair lease would change D1 and is not a "
                "decision this harness may take."
            )
        elif any(not item.met for item in ratios):
            status = STATUS_EXCEEDED

    environment = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "python": sys.version.split()[0],
        "ladybug": _ladybug_version(),
        # WHICH implementation answered is part of the measurement, not of the machine. The
        # durable-commit multiple is 17x with the pure-Python checksum and 5x with the native one
        # on the same box (the accelerator is optional, `okto-grafx[accel]`), so an artefact that
        # records the multiple without recording this says two different things with one number.
        "checksum": _checksum_implementation(),
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "workspace": str(workspace),
    }
    return CalibrationResult(
        status=status,
        ratios=ratios,
        partitions_per_table=partitions_per_table,
        environment=environment,
        notes=tuple(notes),
    )


def _checksum_implementation() -> str:
    """Return the CRC-32C implementation this run measured, or why it could not be asked.

    Never raises: an artefact missing one descriptive field is worth more than a calibration that
    could not be written, and the ceiling verdict does not depend on this value.
    """
    try:
        from okto_grafx.domain.page.checksum import crc32c_implementation

        return str(crc32c_implementation())
    except Exception as failure:  # noqa: BLE001 - a description, never a verdict
        return f"unknown ({type(failure).__name__})"


def publish(result: CalibrationResult, destination: Path) -> str:
    """Publish every measured multiple through the MetricsSink port and write the document.

    The gate reads what this writes (SPEC-M1 OR-4). Publication goes through the real C8 adapter
    and the frozen C8 catalog, so a multiple published under a name or a label the contract does
    not allow is refused here rather than reaching a dashboard.
    """
    from okto_grafx.adapters.metrics_json import JsonMetricsSink
    from okto_grafx.engine.metrics_catalog import metric

    written: list[str] = []
    sink = JsonMetricsSink(written.append, indent=2)
    sink.register(metric(METRIC_NAME))
    for item in result.ratios:
        if item.ok:
            sink.set_gauge(METRIC_NAME, item.multiple, {"ceiling": item.ceiling})
    document = sink.publish()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return document


def format_result(result: CalibrationResult) -> str:
    """Return the human-readable calibration report."""
    lines = ["D5 calibration against LadybugDB 0.16 (SPEC-M1 FR-15, AC-14)"]
    lines.append(f"  status: {result.status.upper()} (exit {result.exit_code})")
    lines.append(f"  partitions_per_table frozen at: {result.partitions_per_table}")
    for key, value in sorted(result.environment.items()):
        lines.append(f"  {key}: {value}")
    for item in result.ratios:
        if not item.ok:
            lines.append(f"  {item.ceiling}: UNMEASURED -- {item.unmeasured}")
            continue
        verdict = "MET" if item.met else "MISSED"
        lines.append(
            f"  {item.ceiling}: {item.multiple:.2f}x of a {item.limit:g}x ceiling -- {verdict}"
        )
        lines.append(
            f"    okto grafx  median {item.subject.median * 1e3:.3f} ms  "
            f"p95 {item.subject.p95 * 1e3:.3f} ms  min {item.subject.minimum * 1e3:.3f} ms  "
            f"spread {item.subject.relative_spread * 100:.1f}%  n={item.subject.iterations}"
        )
        lines.append(
            f"    ladybug     median {item.baseline.median * 1e3:.3f} ms  "
            f"p95 {item.baseline.p95 * 1e3:.3f} ms  min {item.baseline.minimum * 1e3:.3f} ms  "
            f"spread {item.baseline.relative_spread * 100:.1f}%  n={item.baseline.iterations}"
        )
        lines.append(
            f"    multiple at p95 {item.multiple_p95:.2f}x, at best case {item.multiple_best:.2f}x"
        )
    for note in result.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one calibration from the command line."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m bench.harness",
        description="Measure the three D5 multiples against LadybugDB 0.16 and publish them.",
    )
    parser.add_argument(
        "--iterations", type=int, default=30, help="samples to keep per operation"
    )
    parser.add_argument(
        "--warmup", type=int, default=5, help="samples to discard per operation"
    )
    parser.add_argument(
        "--records", type=int, default=2000, help="records the replayed log holds"
    )
    parser.add_argument(
        "--rows", type=int, default=512, help="rows in the point-read corpus"
    )
    parser.add_argument(
        "--partitions-per-table",
        type=int,
        default=64,
        help="the default this run freezes",
    )
    parser.add_argument("--out", default=None, help="write calibration.json here")
    parser.add_argument(
        "--metrics", default=None, help="write the published metrics document here"
    )
    parser.add_argument(
        "--workspace", default=None, help="a directory to build databases in"
    )
    parser.add_argument(
        "--vector-recall",
        action="store_true",
        help="run the C13 recall stage and append its section and gauge (SPEC-VEC FR-8)",
    )
    parser.add_argument(
        "--recall-profile",
        default="smoke",
        choices=("smoke", "full", "tiny"),
        help="the frozen recall profile; smoke on pushes, full on the schedule",
    )
    parser.add_argument(
        "--recall-gt",
        default="auto",
        choices=("auto", "pure"),
        help="oracle mode: auto follows the profile, pure forces the canonical fsum oracle",
    )
    arguments = parser.parse_args(argv)

    workspace = (
        Path(arguments.workspace)
        if arguments.workspace
        else Path(tempfile.mkdtemp(prefix="okto-grafx-bench-"))
    )
    result = calibrate(
        workspace=workspace,
        iterations=arguments.iterations,
        warmup=arguments.warmup,
        replay_records=arguments.records,
        read_rows=arguments.rows,
        partitions_per_table=arguments.partitions_per_table,
    )
    print(format_result(result))
    if arguments.metrics:
        publish(result, Path(arguments.metrics))
    if arguments.out:
        Path(arguments.out).write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    # The vector recall stage is FINAL and ADDITIVE (C13 v4): every legacy output above is
    # already on disk, unconditionally. A recall failure exits non-zero with those intact;
    # success appends the section to --out first and the gauge to --metrics LAST, so a gauge
    # can never exist without its section.
    if arguments.vector_recall:
        from bench.harness.recall_wiring import append_vector_recall

        vector_exit = append_vector_recall(
            profile=arguments.recall_profile,
            gt_mode=arguments.recall_gt,
            out=Path(arguments.out) if arguments.out else None,
            metrics=Path(arguments.metrics) if arguments.metrics else None,
            workspace=workspace,
        )
        if vector_exit != 0:
            return vector_exit
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
