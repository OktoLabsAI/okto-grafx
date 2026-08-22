"""Command line of the cross-family coverage check: ``python -m bench.coverage``.

    python -m bench.coverage \
        --report windows:extras=artifacts/junit-windows-extras/report.xml \
        --report posix:no-extras=artifacts/junit-posix-no-extras/report.xml \
        --debt bench/coverage_debt.txt --json coverage.json

or, against the artefact directory a matrix run downloads into:

    python -m bench.coverage --reports-dir artifacts --debt bench/coverage_debt.txt

Exit codes: ``0`` every reported node ran somewhere; ``1`` at least one node ran nowhere, each
named on stdout; ``2`` the check could not be taken (UNMEASURED, A75.2). No exception leaves this
module: an unreadable input is a diagnosis, not a traceback.

What this check CAN catch
-------------------------
* A test that is skipped on Windows and skipped on POSIX -- the L4 case. It is named, with the
  jobs that reported it, whatever its skip condition, marker or reason string says.
* A test whose ``skipif`` condition is true on every platform the matrix runs, including the
  shapes that defeated the static gate: a condition no target satisfies, a rebound name, a
  fabricated module, a dotted suffix. None of them enters this decision.
* A module-level ``importorskip`` for an optional extra that no job in the matrix installs, so
  the module never runs anywhere. The check names the module. It resolves such an entry against
  the SOURCE FILE pytest recorded for it, so a different module that merely lives in a directory
  named after it -- ``pkg/test_optional/test_helper.py`` beside ``pkg/test_optional.py`` -- does
  not cover it. This needs ``-o junit_family=xunit1``, which ``ci.yml`` passes; a report whose
  collection-level entries carry no file is UNMEASURED rather than resolved by name.
* A matrix that quietly lost a family: a missing family is UNMEASURED, not a pass.
* A report that is missing, empty or unparseable, and a job whose recorded ``sys.platform``
  contradicts the family it claims.

What this check CANNOT catch
----------------------------
* A test that is never COLLECTED on any family. It is absent from every report, so it is not in
  the population. C0's conftest owns that question within a session (A69: a collected node that
  produces no report, and an expected module that contributes nothing, both fail the session).
  The two checks are complementary and neither subsumes the other.
* A test that runs but asserts nothing. Execution is observed, not usefulness.
* A test whose parametrisation makes its node id platform-dependent: the Windows id and the
  POSIX id are different nodes here, and each must run somewhere on its own.
* Anything about a job the matrix did not run. This check reads what the matrix reported; the
  matrix definition is in ``.github/workflows/ci.yml`` and is part of what a reviewer must read.
* A test deleted outright. Deletion is visible in the diff, not in a coverage report.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from bench.coverage.junit import JobReport, ReadFailure, merge_reports, read_report
from bench.coverage.matrix import (
    REQUIRED_FAMILIES,
    Verdict,
    evaluate,
    format_verdict,
    read_debt_file,
)

__all__ = ["build_parser", "collect_reports", "main"]

_ARTEFACT_PREFIX = "junit-"


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser of the coverage check."""
    parser = argparse.ArgumentParser(
        prog="python -m bench.coverage",
        description=(
            "Fail when a test was observed to run on no job of the CI matrix "
            "(LESSONS L4, SPEC-M1 TR-8, CONTRACT G4/D9)."
        ),
    )
    parser.add_argument(
        "--report",
        action="append",
        default=[],
        metavar="FAMILY:PROFILE=PATH",
        help="one junit report, labelled with the D9 family and the installation profile",
    )
    parser.add_argument(
        "--reports-dir",
        default=None,
        metavar="DIR",
        help=(
            "directory of downloaded artefacts, each named junit-<family>-<profile> and holding "
            "one .xml report per shard of that leg"
        ),
    )
    parser.add_argument(
        "--debt",
        default=None,
        metavar="PATH",
        help="the pinned list of node ids allowed to run nowhere (bench/coverage_debt.txt)",
    )
    parser.add_argument(
        "--require-family",
        action="append",
        default=[],
        metavar="FAMILY",
        help=f"family that must be present; defaults to {' and '.join(REQUIRED_FAMILIES)}",
    )
    parser.add_argument(
        "--json",
        default=None,
        metavar="PATH",
        help="write the verdict as JSON as well as printing it",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="how many offenders to name per category (all of them are in the JSON)",
    )
    return parser


def _parse_report_argument(argument: str) -> tuple[str, str, str, tuple[str, ...]] | str:
    """Return ``(job, family, profile, paths)`` for one ``--report``, or an error string."""
    label, separator, path = argument.partition("=")
    if not separator or not path:
        return f"--report {argument!r} is not FAMILY:PROFILE=PATH"
    family, separator, profile = label.partition(":")
    if not separator or not family or not profile:
        return f"--report {argument!r} is not FAMILY:PROFILE=PATH"
    return f"{family}/{profile}", family, profile, (path,)


def _scan_directory(
    directory: str,
) -> tuple[list[tuple[str, str, str, tuple[str, ...]]], list[str]]:
    """Return the jobs found under an artefact directory, and any complaints about it."""
    root = Path(directory)
    if not root.is_dir():
        return [], [f"--reports-dir {directory!r} is not a directory"]
    found: list[tuple[str, str, str, tuple[str, ...]]] = []
    problems: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.startswith(_ARTEFACT_PREFIX):
            continue
        label = child.name[len(_ARTEFACT_PREFIX) :]
        family, separator, profile = label.partition("-")
        if not separator or not family or not profile:
            problems.append(
                f"artefact {child.name!r} is not named junit-<family>-<profile>, so the family "
                "it stands for cannot be established"
            )
            continue
        reports = sorted(child.glob("*.xml"))
        if not reports:
            problems.append(
                f"artefact {child.name!r} holds no .xml report; a leg that produced no junit is "
                "UNMEASURED rather than empty (A75.2)"
            )
            continue
        # One leg may write more than one report when the suite is sharded so that a slow shard
        # cannot take the whole reading with it. They are merged into one job.
        found.append(
            (f"{family}/{profile}", family, profile, tuple(str(report) for report in reports))
        )
    if not found and not problems:
        problems.append(f"no junit-<family>-<profile> artefact under {directory!r}")
    return found, problems


def collect_reports(
    report_arguments: Sequence[str], reports_dir: str | None
) -> tuple[list[JobReport | ReadFailure], list[str]]:
    """Read every report named on the command line or found in the artefact directory."""
    jobs: list[tuple[str, str, str, tuple[str, ...]]] = []
    problems: list[str] = []
    for argument in report_arguments:
        parsed = _parse_report_argument(argument)
        if isinstance(parsed, str):
            problems.append(parsed)
        else:
            jobs.append(parsed)
    if reports_dir is not None:
        found, scan_problems = _scan_directory(reports_dir)
        jobs.extend(found)
        problems.extend(scan_problems)
    reports: list[JobReport | ReadFailure] = [
        merge_reports(
            job,
            family,
            profile,
            [
                read_report(job=job, family=family, profile=profile, path=part)
                for part in paths
            ],
        )
        for job, family, profile, paths in jobs
    ]
    if not jobs and not problems:
        problems.append("no report was given; nothing was measured")
    return reports, problems


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check and return the exit code; no exception escapes."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        reports, problems = collect_reports(arguments.report, arguments.reports_dir)
        debt, debt_error = read_debt_file(arguments.debt)
        if debt_error:
            problems.append(debt_error)
        required = tuple(arguments.require_family) or REQUIRED_FAMILIES
        verdict = evaluate(
            reports, debt=debt, problems=problems, required_families=required
        )
    except Exception as error:  # noqa: BLE001 - a crash here must read as UNMEASURED, not as a pass
        verdict = Verdict(
            status="unmeasured",
            unmeasured=(f"the coverage check itself failed: {type(error).__name__}: {error}",),
        )
    print(format_verdict(verdict, limit=arguments.limit))
    if arguments.json:
        try:
            Path(arguments.json).write_text(
                json.dumps(verdict.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError as error:
            print(f"the JSON verdict could not be written: {error}")
            return max(verdict.exit_code, 2)
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
