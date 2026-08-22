"""Read one pytest junit report and say, per test, whether it was OBSERVED TO RUN.

Why the junit report and not the terminal output
------------------------------------------------
Amendment A75.1: under ``capture_output`` with no TTY pytest emits no summary line at all, so a
reading scraped from stdout can be empty on a run that failed. ``--junitxml`` is pytest's own
machine-readable record, it survives capture, and it cannot be confused with a wrapper's exit
status. Amendment A75.2 completes the rule: a report that is missing or unparseable is
**UNMEASURED**, never zero. Every failure to read one is returned as a :class:`ReadFailure` and
the caller must treat it as red -- see ``bench/coverage/matrix.py``.

What counts as an execution
---------------------------
pytest writes one ``<testcase>`` per reported node. A node that did not run carries a
``<skipped>`` child; a node that ran carries nothing, or ``<failure>``, or ``<error>``. So:

* no ``<skipped>`` child                      -> EXECUTED (the body ran, whatever the outcome)
* ``<skipped type="pytest.skip">``            -> SKIPPED (it did not run here)
* ``<skipped type="pytest.xfail">``           -> DECLARED_DEBT (registered debt, A54 marker 3)

A failing test counts as executed: it ran, and it turns the build red on its own. An error in
setup also counts as reported-and-red. Neither can be used to make a test quietly disappear,
which is the only thing this module exists to detect.

Identity across families
------------------------
pytest derives the ``classname``/``name`` pair from the node id by a deterministic transform
(``_pytest.junitxml.mangle_test_address``): the path separator becomes a dot, ``.py`` is dropped,
``::`` separates the rest. Node ids use ``/`` on every platform, so ``classname + "::" + name``
is the same string on Windows and on POSIX for the same test, which is exactly what a
cross-family intersection needs. The default junit family (``xunit2``) drops the ``file``
attribute, so the mangled address is used as the identity and the file path is recorded only when
the report carries it. That path is NOT identity material: under ``xunit1`` pytest writes it with
the host's own separator, so the same test is reported with backslashes on Windows and with
forward slashes on POSIX. It is normalised on the way in, it makes messages actionable, and
``matrix.py`` uses it for one decision the mangled address cannot make -- which module a
collection-level entry stands for (see below).

Collection-level entries
------------------------
When a module is skipped during collection -- a module-level ``pytest.importorskip`` for an
optional extra, which A54 explicitly keeps legal -- there is no test item to report, so pytest
writes a single ``<testcase>`` whose ``classname`` is EMPTY and whose ``name`` is the dotted
module. Under ``xunit1`` that entry still carries ``file``, the module's own source path, and
that -- not the dotted name -- is what ``matrix.py`` resolves it against: a module whose own file
ran tests on another job is covered, and a module skipped on every job is a real disappearance
and is named. The dotted name alone would not do, because ``pkg.test_optional`` is a prefix both
of a class inside ``pkg/test_optional.py`` and of every module under a directory
``pkg/test_optional/``, and only the second is a different module.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ElementTree
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "Entry",
    "JobReport",
    "Outcome",
    "ReadFailure",
    "family_of_platform",
    "merge_reports",
    "read_report",
]


class Outcome(str, Enum):
    """What one job observed about one node."""

    EXECUTED = "executed"
    """The body ran on this job: passed, failed or errored. It did not disappear."""

    SKIPPED = "skipped"
    """The node did not run on this job."""

    DECLARED_DEBT = "declared_debt"
    """The node is an x-fail: a registered debt rather than a silent absence (A54)."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One node as one job reported it."""

    identity: str
    """``classname::name`` -- pytest's own mangled address, identical across families."""

    outcome: Outcome
    """Whether this job observed the node execute."""

    module_level: bool
    """True when the entry is a collection-level report for a whole module, not a test."""

    module: str
    """The dotted module the entry belongs to, as far as the report reveals it."""

    detail: str
    """The skip message, or an empty string. Reported, never used to decide anything (A35/A54)."""

    location: str
    """The source path when the report carries one, otherwise an empty string."""


@dataclass(frozen=True, slots=True)
class JobReport:
    """Every node one matrix job reported, with the job's declared identity."""

    job: str
    """The matrix job label, ``<family>/<profile>``."""

    family: str
    """The D9 operating-system family this job ran on: ``windows`` or ``posix``."""

    profile: str
    """Which installation profile the job used, for example ``extras`` or ``no-extras``."""

    path: str
    """The report file this was read from."""

    entries: tuple[Entry, ...]
    """One entry per reported node."""

    declared_tests: int
    """The ``tests`` attribute pytest wrote on the suite, for cross-checking the entry count."""

    observed_platform: str
    """``sys.platform`` of the job, when the sidecar environment file records it."""

    def executed(self) -> frozenset[str]:
        """Return the identities this job observed execute."""
        return frozenset(
            entry.identity for entry in self.entries if entry.outcome is Outcome.EXECUTED
        )


@dataclass(frozen=True, slots=True)
class ReadFailure:
    """A report that could not be read, which is UNMEASURED and never zero (A75.2)."""

    job: str
    """The matrix job label the missing report belongs to."""

    path: str
    """Where the report was expected."""

    reason: str
    """Why it could not be used."""


_FAMILY_OF_PLATFORM: dict[str, str] = {
    "win32": "windows",
    "cygwin": "windows",
    "linux": "posix",
    "darwin": "posix",
    "freebsd": "posix",
}
"""D9 reads the world as two families; this maps a reported ``sys.platform`` onto them."""


def family_of_platform(platform_name: str) -> str:
    """Return the D9 family of a ``sys.platform`` value, or an empty string if unknown."""
    for prefix, family in _FAMILY_OF_PLATFORM.items():
        if platform_name.startswith(prefix):
            return family
    return ""


def _module_of(classname: str, name: str) -> str:
    """Return the dotted module of an entry.

    For a collection-level entry the whole name IS the module. For a test the module is a prefix
    of ``classname``, and the report does not say where it ends -- a class adds a segment that
    looks exactly like a package, and a package adds one that looks exactly like a class. Nothing
    here can tell them apart, and nothing here tries: this value is for messages only.
    ``matrix.py`` resolves a collection-level entry against :attr:`Entry.location`, the source
    file pytest recorded, because that is the one thing the two spellings do not share.
    """
    if not classname:
        return name
    return classname


def _outcome_of(testcase: ElementTree.Element) -> tuple[Outcome, str]:
    """Return what this ``<testcase>`` observed, and the message it carried."""
    skipped = testcase.find("skipped")
    if skipped is None:
        return Outcome.EXECUTED, ""
    message = skipped.get("message", "")
    if skipped.get("type", "") == "pytest.xfail":
        return Outcome.DECLARED_DEBT, message
    return Outcome.SKIPPED, message


def _read_environment(report_path: Path) -> str:
    """Return the ``sys.platform`` a job recorded beside its report, or an empty string.

    The family label of a job comes from the CI matrix, which is a file this component owns --
    but a label is still a claim. When the job also writes ``<report>.env.json`` the claim is
    CHECKED against the interpreter that produced the report, so a mislabelled matrix entry
    cannot make one family stand in for two.
    """
    sidecar = report_path.with_suffix(report_path.suffix + ".env.json")
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    platform_name = payload.get("sys_platform", "")
    return platform_name if isinstance(platform_name, str) else ""


def read_report(job: str, family: str, profile: str, path: str) -> JobReport | ReadFailure:
    """Read one junit report, or say why it cannot be used.

    Never raises for a bad file: an unreadable report is a :class:`ReadFailure`, because the one
    thing this check must never do is treat "I could not count" as "there was nothing to count".
    """
    report_path = Path(path)
    try:
        raw = report_path.read_bytes()
    except OSError as error:
        return ReadFailure(job=job, path=path, reason=f"the report could not be read: {error}")
    if not raw.strip():
        return ReadFailure(job=job, path=path, reason="the report is empty")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as error:
        return ReadFailure(job=job, path=path, reason=f"the report is not valid XML: {error}")

    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        return ReadFailure(job=job, path=path, reason="the report holds no <testsuite> element")

    entries: list[Entry] = []
    declared = 0
    for suite in suites:
        try:
            declared += int(suite.get("tests", "0"))
        except ValueError:
            return ReadFailure(
                job=job, path=path, reason="the suite element has an unreadable test count"
            )
        for testcase in suite.iter("testcase"):
            classname = testcase.get("classname", "")
            name = testcase.get("name", "")
            if not name:
                return ReadFailure(
                    job=job, path=path, reason="a <testcase> element carries no name"
                )
            outcome, detail = _outcome_of(testcase)
            entries.append(
                Entry(
                    identity=f"{classname}::{name}",
                    outcome=outcome,
                    module_level=not classname,
                    module=_module_of(classname, name),
                    detail=detail,
                    location=testcase.get("file", "").replace("\\", "/"),
                )
            )

    if not entries:
        return ReadFailure(
            job=job, path=path, reason="the report holds no <testcase> element, so nothing ran"
        )

    return JobReport(
        job=job,
        family=family,
        profile=profile,
        path=str(report_path),
        entries=tuple(entries),
        declared_tests=declared,
        observed_platform=_read_environment(report_path),
    )


def merge_reports(
    job: str, family: str, profile: str, parts: Sequence[JobReport | ReadFailure]
) -> JobReport | ReadFailure:
    """Return one job's reports merged into one, or the first failure among them.

    One job may write more than one report: a suite split into shards so that a slow leg cannot
    take the whole reading down with it writes one per shard, which is the shape C0's wheel-build
    fixture forced on this build -- under contention it exceeded the session timeout and pytest
    died WITHOUT writing any junit, and three whole-repo readings were lost to it.

    Merging fails closed. A job whose second shard produced no report is a job that was only
    partly observed, and the tests in the missing shard would otherwise be absent from the
    population rather than reported as never run -- which is the exact hole this check exists to
    close. So any unreadable part makes the whole job UNMEASURED (A75.2).
    """
    if not parts:
        return ReadFailure(job=job, path="", reason="no report was found for this job")
    for part in parts:
        if isinstance(part, ReadFailure):
            return part
    good = [part for part in parts if isinstance(part, JobReport)]
    if len(good) == 1:
        return good[0]
    entries: list[Entry] = []
    for part in good:
        entries.extend(part.entries)
    platforms = {part.observed_platform for part in good if part.observed_platform}
    if len(platforms) > 1:
        return ReadFailure(
            job=job,
            path=", ".join(part.path for part in good),
            reason=(
                "the shards of this job recorded different platforms "
                f"({sorted(platforms)}), so they are not one job"
            ),
        )
    return JobReport(
        job=job,
        family=family,
        profile=profile,
        path=", ".join(part.path for part in good),
        entries=tuple(entries),
        declared_tests=sum(part.declared_tests for part in good),
        observed_platform=next(iter(platforms), ""),
    )
