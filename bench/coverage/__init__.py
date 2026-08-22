"""The cross-family coverage check (LESSONS L4, SPEC-M1 TR-8, CONTRACT G4/D9).

The guarantee this package provides is observational, not analytical. It does not ask whether a
skip is honest -- that question is asked of code the author writes, and C0's skip-attribution
gate was defeated in seven consecutive rounds because the author can always add one more member
to the satisfying set. It asks instead, of every test the matrix reported: **was it observed to
execute on at least one job?** A test skipped on Windows and skipped on POSIX has vanished from
the suite, and an intersection of two lists of executed node ids sees that whatever the skip
condition says.

Read ``matrix.py`` for the exact rule and its three categories, ``junit.py`` for how an execution
is distinguished from a skip in pytest's own report, and ``__main__.py`` for the command line the
CI matrix calls.
"""

from __future__ import annotations

from bench.coverage.junit import Entry, JobReport, Outcome, ReadFailure, read_report
from bench.coverage.matrix import Verdict, evaluate, format_verdict

__all__ = [
    "Entry",
    "JobReport",
    "Outcome",
    "ReadFailure",
    "Verdict",
    "evaluate",
    "format_verdict",
    "read_report",
]
