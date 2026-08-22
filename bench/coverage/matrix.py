"""The cross-family coverage rule: every test must be OBSERVED TO RUN on at least one job.

The rule, in full
-----------------
Given one junit report per job of the CI matrix, each job labelled with the D9 family it ran on:

1. **Population.** Every node any job reported. A node no job reported is invisible here by
   construction -- catching a test that vanished from collection on every family is a different
   check, and C0's conftest already fails a session in which a collected node produces no report
   and a session in which an expected module contributes nothing (A69). This check starts from
   what the matrix actually reported and asks a question that check cannot ask.

2. **Covered.** A node is covered when at least one job reports it EXECUTED. Passed, failed or
   errored all count: they ran, and a failure turns the build red on its own. Nothing about the
   skip condition, the marker or the reason string enters this decision.

3. **Module-level entries.** A module skipped during collection produces one entry for the whole
   module and no entries for its tests. Such an entry is covered when a test **of that same
   source file** executed on any job -- the module ran somewhere, under the identity of its
   tests. A module skipped on every job is named as the offender, since none of its tests ran
   anywhere. The resolution is by FILE, never by dotted prefix: ``pkg.test_optional`` is a
   dotted prefix of ``pkg.test_optional.test_helper``, which under pytest's address mangling is
   either a test class inside that module or an entirely different module inside a directory
   that merely shares the module's stem. A prefix rule cannot tell them apart, so a module that
   ran nowhere would be certified by a sibling a test author can create from inside their own
   ``tests/<area>/**`` scope -- exactly the member-of-the-satisfying-set hole A89 names. Under
   ``junit_family=xunit1`` pytest writes ``file`` on every ``<testcase>``, collection-level
   entries included, so the two are told apart by the only thing that distinguishes them. A
   collection-level entry that carries no file is UNMEASURED (rule 7), never resolved by name.

4. **Never ran anywhere -> BLOCKING.** Every node in the population that no job observed to
   execute fails the check and is NAMED in the report, unless its identity is listed in the
   pinned debt file. This is the whole guarantee: a test skipped on Windows and skipped on POSIX
   has left the suite, and no author can make it look otherwise by writing a better condition.

5. **Declared debt.** The only tolerated never-ran node is one listed in the debt file
   (``bench/coverage_debt.txt``). That file is owned by C13 and lives outside every other
   component's write scope, so a test author cannot add a member to the satisfying set from the
   file they are already editing -- which is precisely the property A89 says an attribution rule
   needs and the seven defeated rounds of the skip gate never had. x-fail and ``pending`` are
   REPORTED as debt because the report should say what kind of debt it is, but they are not
   decided differently: an x-fail that runs nowhere must be listed like anything else. An entry
   in the debt file that did run is reported as stale so the file can be pruned; that is a note,
   not a failure, because deleting a debt must never turn the build red.

6. **Partial coverage.** A node executed on some families but not all required ones is reported
   under "declared partial coverage" with the families that ran it, which is what SPEC-M1 TR-8
   asks for: a test that runs on one family only is reported, never silenced. It is not a
   failure -- ``platform_specific`` tests are legitimate by G4 -- but it is never invisible.

7. **UNMEASURED.** A missing, empty or unparseable report; a required family with no job; a job
   that recorded no platform beside its report, or whose recorded ``sys.platform`` contradicts the
   family it was labelled with; a job that reported far fewer nodes than its siblings; a missing
   debt file. The platform is REQUIRED rather than checked-when-present: the family label comes
   from the workflow file, and a check whose central claim rests on a label nobody verifies is the
   same shape as the attribution rules that were defeated seven times. To that list is added a
   collection-level entry that carries no source file: rule 3 resolves such an entry against the
   file it names, and a report that omits the file leaves the entry undecidable by anything but
   its dotted name -- which is the hole rule 3 exists to close. And a POPULATION OF ZERO, however
   it arises: "no node ran nowhere" is true of an empty set, so a matrix whose every module failed
   to import -- collection errors are reported but never counted as tests -- would otherwise be
   handed a green certificate over nothing. Each of these ends the check with status ``unmeasured`` and a non-zero exit, because a
   count of zero and a failure to count are the same value in most encodings and opposite facts
   (A75.2).

What this rule can and cannot catch is stated in the module docstring of ``__main__.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path

from bench.coverage.junit import Entry, JobReport, Outcome, ReadFailure, family_of_platform

__all__ = [
    "MINIMUM_JOB_SHARE",
    "REQUIRED_FAMILIES",
    "STATUS_COVERED",
    "STATUS_UNMEASURED",
    "STATUS_VIOLATIONS",
    "Verdict",
    "evaluate",
    "format_verdict",
    "read_debt_file",
]

STATUS_COVERED: str = "covered"
"""Every reported node ran somewhere; exit code 0."""

STATUS_VIOLATIONS: str = "violations"
"""At least one node ran on no job; exit code 1."""

STATUS_UNMEASURED: str = "unmeasured"
"""The check could not be taken; exit code 2. Never reported as a pass (A75.2)."""

MINIMUM_JOB_SHARE: float = 0.5
"""How much of the largest job a job must report before the matrix may be believed.

A job that collapses early -- an import error in a sibling component, a runner that died --
still writes a junit report, and a report with three entries in it would let the OTHER jobs
certify the whole suite: every test they ran is covered, and the tests nobody ran are simply
absent from the population. That is precisely the shape this check exists to refuse, so a job
reporting less than half of the largest job's node count makes the reading UNMEASURED instead.
Profiles differ by a handful of optional-dependency tests and families by a handful of
platform-specific ones, so the floor is far below any legitimate difference and far above a
collapse.
"""

REQUIRED_FAMILIES: tuple[str, ...] = ("windows", "posix")
"""The D9 families that must both be present before any coverage claim is made (G4/TR-8).

Pinned rather than derived from the reports given: deriving it would let a matrix that shipped
one family certify itself, which is the exact failure this check exists to prevent.
"""


@dataclass(frozen=True, slots=True)
class NodeCoverage:
    """One node, and where it was seen."""

    identity: str
    """``classname::name``, pytest's own mangled address."""

    executed_on: tuple[str, ...]
    """The families that observed it execute, sorted."""

    jobs_seen: tuple[str, ...]
    """Every job that reported it at all, sorted."""

    debt: bool
    """True when every job that reported it recorded an x-fail rather than a plain skip."""

    detail: str
    """A message from one of the reports, to make the line actionable."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """The outcome of one cross-family coverage check."""

    status: str
    """``covered``, ``violations`` or ``unmeasured``."""

    never_ran: tuple[NodeCoverage, ...] = ()
    """Nodes that executed on no job and are not listed as debt. The blocking category."""

    declared_debt: tuple[NodeCoverage, ...] = ()
    """Nodes that executed nowhere and are listed in the debt file."""

    stale_debt: tuple[str, ...] = ()
    """Debt entries that did run, or that no report mentions, so the file can be pruned."""

    partial: tuple[NodeCoverage, ...] = ()
    """Nodes that ran on some required families but not all (TR-8 partial coverage)."""

    collection_errors: tuple[NodeCoverage, ...] = ()
    """Modules a job reported as a collection error rather than a test."""

    unmeasured: tuple[str, ...] = ()
    """Why the check could not be taken, when the status says so."""

    jobs: tuple[str, ...] = ()
    """The job labels that were read."""

    families: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Family -> the jobs that reported for it."""

    population: int = 0
    """How many distinct nodes the matrix reported."""

    covered: int = 0
    """How many of them were observed to execute somewhere."""

    per_job_totals: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    """Job -> counts of executed, skipped and declared-debt entries, for the record."""

    @property
    def exit_code(self) -> int:
        """Return the process exit code this verdict deserves."""
        if self.status == STATUS_COVERED:
            return 0
        if self.status == STATUS_VIOLATIONS:
            return 1
        return 2

    def to_dict(self) -> dict[str, object]:
        """Return the verdict as plain data, for a machine-readable artefact."""
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "jobs": list(self.jobs),
            "families": {family: list(jobs) for family, jobs in self.families.items()},
            "population": self.population,
            "covered": self.covered,
            "never_ran": [
                {
                    "identity": node.identity,
                    "jobs_seen": list(node.jobs_seen),
                    "declared_debt": node.debt,
                    "detail": node.detail,
                }
                for node in self.never_ran
            ],
            "declared_debt": [node.identity for node in self.declared_debt],
            "stale_debt": list(self.stale_debt),
            "partial": [
                {"identity": node.identity, "executed_on": list(node.executed_on)}
                for node in self.partial
            ],
            "collection_errors": [node.identity for node in self.collection_errors],
            "unmeasured": list(self.unmeasured),
            "per_job_totals": {
                job: dict(counts) for job, counts in self.per_job_totals.items()
            },
        }


def read_debt_file(path: str | None) -> tuple[frozenset[str], str]:
    """Return the pinned debt identities and an error string that is empty on success.

    Format: one identity per line, ``#`` starts a comment, blank lines ignored. A path that was
    named but cannot be read is an error rather than an empty set -- an unreadable allowlist that
    silently became empty would turn every declared debt into a blocking failure, and a check that
    cries wolf is a check that gets bypassed.
    """
    if path is None:
        return frozenset(), ""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        return frozenset(), f"the debt file could not be read: {error}"
    identities: set[str] = set()
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            identities.add(entry)
    return frozenset(identities), ""


def _covers_module(locations: AbstractSet[str], executed_locations: AbstractSet[str]) -> bool:
    """Return True when a test of this module's OWN source file executed somewhere.

    ``locations`` are the files the jobs recorded for one collection-level entry (normally one,
    since every leg runs from the repository root); ``executed_locations`` are the files of the
    tests that were observed to execute. Identity of file, not a dotted prefix: pytest mangles
    ``pkg/test_optional/test_helper.py::test_helper`` into the classname
    ``pkg.test_optional.test_helper``, which a prefix rule cannot tell from a test class inside
    ``pkg/test_optional.py``. Resolving by file means a module that never ran can only be
    certified by a test that lives in that same module -- and an author cannot conjure one from
    a neighbouring file, which is the property A89 asks of every attribution rule.
    """
    return bool(locations & executed_locations)


def evaluate(
    reports: Sequence[JobReport | ReadFailure],
    *,
    debt: frozenset[str] = frozenset(),
    problems: Sequence[str] = (),
    required_families: Sequence[str] = REQUIRED_FAMILIES,
) -> Verdict:
    """Apply the cross-family rule to the reports of one matrix run."""
    unmeasured: list[str] = []
    good: list[JobReport] = []
    for report in reports:
        if isinstance(report, ReadFailure):
            unmeasured.append(f"{report.job}: {report.reason} ({report.path})")
        else:
            good.append(report)
    unmeasured.extend(problems)

    families: dict[str, list[str]] = {}
    for report in good:
        families.setdefault(report.family, []).append(report.job)
        if not report.observed_platform:
            unmeasured.append(
                f"{report.job}: the leg recorded no platform beside its report, so its family "
                f"label {report.family!r} is only a claim. Write "
                f"'{{\"sys_platform\": sys.platform}}' to <report>.env.json in the job that "
                "produced it; a label the check cannot verify is exactly the password this "
                "check exists to do without (A89)"
            )
            continue
        observed = family_of_platform(report.observed_platform)
        if not observed:
            unmeasured.append(
                f"{report.job}: recorded sys.platform {report.observed_platform!r}, which "
                "belongs to no D9 family this check knows"
            )
        elif observed != report.family:
            unmeasured.append(
                f"{report.job}: labelled family {report.family!r} but the run recorded "
                f"sys.platform {report.observed_platform!r}, which is family {observed!r}"
            )
    if good:
        largest = max(len(report.entries) for report in good)
        for report in good:
            share = len(report.entries) / largest if largest else 0.0
            if share < MINIMUM_JOB_SHARE:
                unmeasured.append(
                    f"{report.job}: reported {len(report.entries)} node(s) against a largest job "
                    f"of {largest}; a job that collapsed cannot be told from one that ran, so the "
                    "matrix is not readable"
                )
    for family in required_families:
        if family not in families:
            unmeasured.append(
                f"no job reported for required family {family!r}; coverage across the D9 matrix "
                "cannot be established from one family (G4, TR-8)"
            )
    for report in good:
        for entry in report.entries:
            if entry.module_level and entry.outcome is not Outcome.EXECUTED and not entry.location:
                unmeasured.append(
                    f"{report.job}: the collection-level entry for module {entry.module!r} "
                    "records no source file, so the module it stands for can only be guessed "
                    "from its dotted name -- and a dotted name does not distinguish a test "
                    "class inside that module from a different module in a directory of the "
                    "same stem. Run pytest with '-o junit_family=xunit1' so every <testcase> "
                    "carries its file; resolving this entry by name would certify a module "
                    "that ran nowhere (A89)"
                )

    per_job_totals: dict[str, dict[str, int]] = {}
    for report in good:
        counts = {"executed": 0, "skipped": 0, "declared_debt": 0}
        for entry in report.entries:
            counts[entry.outcome.value] += 1
        counts["reported"] = len(report.entries)
        counts["declared_by_pytest"] = report.declared_tests
        per_job_totals[report.job] = counts

    if unmeasured:
        return Verdict(
            status=STATUS_UNMEASURED,
            unmeasured=tuple(unmeasured),
            jobs=tuple(sorted(report.job for report in good)),
            families={family: tuple(sorted(jobs)) for family, jobs in sorted(families.items())},
            per_job_totals=per_job_totals,
        )

    executed_families: dict[str, set[str]] = {}
    jobs_seen: dict[str, set[str]] = {}
    debt_only: dict[str, bool] = {}
    details: dict[str, str] = {}
    module_level: dict[str, Entry] = {}
    module_locations: dict[str, set[str]] = {}
    errored_modules: dict[str, Entry] = {}
    executed_locations: set[str] = set()

    for report in good:
        for entry in report.entries:
            identity = entry.identity
            jobs_seen.setdefault(identity, set()).add(report.job)
            if entry.module_level:
                if entry.outcome is Outcome.EXECUTED:
                    errored_modules[identity] = entry
                else:
                    module_level[identity] = entry
                    module_locations.setdefault(identity, set()).add(entry.location)
            if entry.outcome is Outcome.EXECUTED:
                executed_families.setdefault(identity, set()).add(report.family)
                if not entry.module_level and entry.location:
                    executed_locations.add(entry.location)
                debt_only[identity] = False
            else:
                debt_only[identity] = debt_only.get(identity, True) and (
                    entry.outcome is Outcome.DECLARED_DEBT
                )
                if entry.detail and identity not in details:
                    details[identity] = entry.detail

    never_ran: list[NodeCoverage] = []
    declared_debt: list[NodeCoverage] = []
    partial: list[NodeCoverage] = []
    covered = 0
    required = tuple(required_families)

    for identity in sorted(jobs_seen):
        if identity in errored_modules and identity not in module_level:
            # A collection error is loudly red on its own job; it is reported, not counted as a
            # test that ran and not counted as one that vanished.
            continue
        ran_on = tuple(sorted(executed_families.get(identity, ())))
        if identity in module_level and not ran_on:
            if _covers_module(module_locations[identity], executed_locations):
                covered += 1
                continue
        node = NodeCoverage(
            identity=identity,
            executed_on=ran_on,
            jobs_seen=tuple(sorted(jobs_seen[identity])),
            debt=debt_only.get(identity, False),
            detail=details.get(identity, ""),
        )
        if ran_on:
            covered += 1
            if any(family not in ran_on for family in required):
                partial.append(node)
            continue
        if identity in debt:
            declared_debt.append(node)
        else:
            never_ran.append(node)

    mentioned = set(jobs_seen)
    ran_somewhere = {identity for identity, seen in executed_families.items() if seen}
    stale_debt = tuple(
        sorted(entry for entry in debt if entry in ran_somewhere or entry not in mentioned)
    )

    collection_only = set(errored_modules) - set(module_level)
    population = len(mentioned) - len(collection_only)
    if population == 0:
        # "No node ran nowhere" is satisfied vacuously by a matrix that reported no readable node
        # at all, and the vacuous reading is exit 0 -- a green certificate over nothing, which is
        # the exact failure mode this check exists to prevent. Reachable today: a matrix in which
        # every module failed to import writes junit full of collection errors, all of which are
        # excluded from the population by design. A count of zero and a failure to count are the
        # same value and opposite facts (A75.2), so this is UNMEASURED.
        unmeasured.append(
            f"the matrix reported {len(mentioned)} entry(ies) and none of them is a node this "
            f"check can read ({len(collection_only)} collection error(s), which are reported but "
            "never counted as tests). A population of zero certifies nothing: it is a failure to "
            "count, not a suite in which everything ran"
        )
    status = STATUS_VIOLATIONS if never_ran else STATUS_COVERED
    if unmeasured:
        status = STATUS_UNMEASURED
    return Verdict(
        status=status,
        unmeasured=tuple(unmeasured),
        never_ran=tuple(never_ran),
        declared_debt=tuple(declared_debt),
        stale_debt=stale_debt,
        partial=tuple(partial),
        collection_errors=tuple(
            NodeCoverage(
                identity=identity,
                executed_on=(),
                jobs_seen=tuple(sorted(jobs_seen[identity])),
                debt=False,
                detail=entry.detail,
            )
            for identity, entry in sorted(errored_modules.items())
        ),
        jobs=tuple(sorted(report.job for report in good)),
        families={family: tuple(sorted(jobs)) for family, jobs in sorted(families.items())},
        population=population,
        covered=covered,
        per_job_totals=per_job_totals,
    )


def format_verdict(verdict: Verdict, *, limit: int = 50) -> str:
    """Return the human-readable report, naming every offender up to ``limit``."""
    lines: list[str] = []
    lines.append("cross-family coverage check (LESSONS L4, SPEC-M1 TR-8, CONTRACT G4/D9)")
    lines.append(f"  status: {verdict.status.upper()} (exit {verdict.exit_code})")
    if verdict.jobs:
        lines.append(f"  jobs read: {len(verdict.jobs)}")
        for family, jobs in sorted(verdict.families.items()):
            lines.append(f"    family {family}: {', '.join(jobs)}")
    for job in verdict.jobs:
        counts = verdict.per_job_totals.get(job, {})
        lines.append(
            f"    {job}: reported={counts.get('reported', 0)} "
            f"executed={counts.get('executed', 0)} skipped={counts.get('skipped', 0)} "
            f"declared_debt={counts.get('declared_debt', 0)}"
        )
    if verdict.status == STATUS_UNMEASURED:
        lines.append("  the check was NOT taken; a missing count is not a count of zero (A75.2):")
        for reason in verdict.unmeasured:
            lines.append(f"    {reason}")
        return "\n".join(lines)

    lines.append(f"  population: {verdict.population} node(s) reported by the matrix")
    lines.append(f"  observed to run on at least one job: {verdict.covered}")
    if verdict.never_ran:
        lines.append(
            f"  RAN ON NO FAMILY: {len(verdict.never_ran)} node(s). Each was reported by the "
            "matrix and executed nowhere, so it is not in the suite:"
        )
        for node in verdict.never_ran[:limit]:
            kind = "x-fail everywhere" if node.debt else "skipped everywhere"
            lines.append(
                f"    {node.identity}  [{kind}; reported by {', '.join(node.jobs_seen)}]"
                + (f"  {node.detail}" if node.detail else "")
            )
        if len(verdict.never_ran) > limit:
            lines.append(f"    ... and {len(verdict.never_ran) - limit} more")
    if verdict.declared_debt:
        lines.append(
            f"  declared debt (listed in the debt file, ran nowhere): "
            f"{len(verdict.declared_debt)}"
        )
        for node in verdict.declared_debt[:limit]:
            lines.append(f"    {node.identity}")
    if verdict.stale_debt:
        lines.append("  stale debt entries (they ran, or nothing reported them):")
        for identity in verdict.stale_debt[:limit]:
            lines.append(f"    {identity}")
    if verdict.collection_errors:
        lines.append(f"  collection errors reported: {len(verdict.collection_errors)}")
        for node in verdict.collection_errors[:limit]:
            lines.append(f"    {node.identity}  [{', '.join(node.jobs_seen)}]")
    if verdict.partial:
        lines.append(
            f"  declared partial coverage (TR-8): {len(verdict.partial)} node(s) ran on some "
            "families and not others"
        )
        for node in verdict.partial[:limit]:
            lines.append(f"    {node.identity}  ran on: {', '.join(node.executed_on)}")
        if len(verdict.partial) > limit:
            lines.append(f"    ... and {len(verdict.partial) - limit} more")
    return "\n".join(lines)
