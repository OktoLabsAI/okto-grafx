"""The cross-family coverage check, probed end to end (LESSONS L4, A86, SPEC-M1 TR-8).

A86: every gate rule needs at least one probe that goes through the real gate, end to end, on a
planted input -- a real subprocess, a real collection, a real session -- and asserts the OUTCOME.
So the flagship probes here do not call a helper: they run REAL pytest sessions over a planted
mini-suite, take pytest's own junit reports from those sessions, and run the real command line
``python -m bench.coverage`` as a subprocess, asserting its exit code and the name it prints.

The one thing a single machine cannot supply is a second operating-system family, so the planted
sessions stand the family in through an environment variable: one session runs as the Windows job
of the matrix, one as the POSIX job, and the reports are labelled accordingly. Everything else --
the sessions, the skips, the reports, the checker -- is real. The genuinely cross-platform reading
is the matrix in ``.github/workflows/ci.yml``; this is the proof that the rule fires on the input
that matrix produces.

Two directions are asserted, always: the planted matrix goes RED and names the offender, and the
SAME matrix without the plant goes green. A check that cannot fail certifies nothing, and a check
that always fails certifies nothing either.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from bench.coverage.junit import Outcome, ReadFailure, read_report
from bench.coverage.matrix import (
    MINIMUM_JOB_SHARE,
    REQUIRED_FAMILIES,
    Verdict,
    STATUS_COVERED,
    STATUS_UNMEASURED,
    STATUS_VIOLATIONS,
    evaluate,
    read_debt_file,
)

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
"""The repository root, from which the checker is run exactly as CI runs it."""

CHECKER_TIMEOUT_SECONDS: float = 120.0
"""A bound on every subprocess here: a hang must become a failure, never a wait (A42/A88)."""

PLANTED_HEAD: str = '''"""A planted mini-suite whose tests cover the outcomes the rule distinguishes."""

import os

import pytest

FAMILY = os.environ.get("PLANTED_FAMILY", "")


def test_runs_everywhere() -> None:
    """Execute on every job, so the matrix has a control that is always covered."""
    assert FAMILY in ("windows", "posix")


@pytest.mark.skipif(FAMILY != "windows", reason="planted: this family only")
def test_runs_on_one_family_only() -> None:
    """Execute on one family only: declared partial coverage, not a failure (TR-8)."""
    assert FAMILY == "windows"
'''
"""The half of the planted suite that is honest: one test everywhere, one on one family."""

PLANTED_VANISHING_TEST: str = '''

@pytest.mark.skipif(FAMILY in ("windows", "posix"), reason="planted: skipped on every family")
def test_skipped_on_every_family() -> None:
    """Never execute anywhere. This is the test the coverage check exists to find."""
    raise AssertionError("a test that never runs cannot fail, which is the whole problem")
'''
"""The plant: a skip condition that is true on both families, and legal on each of them."""

PLANTED_INI: str = """[pytest]
addopts = --timeout=60 --timeout-method=thread
"""


def _run_planted_session(directory: Path, family: str, report: Path) -> subprocess.CompletedProcess[str]:
    """Run one real pytest session over the planted suite, standing in for one matrix job."""
    environment = dict(os.environ)
    environment["PLANTED_FAMILY"] = family
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(directory),
            "-c",
            str(directory / "pytest.ini"),
            "-p",
            "no:cacheprovider",
            # Exactly what ci.yml passes. Under xunit1 every <testcase> carries its source file,
            # collection-level entries included, and that attribute is what resolves a module
            # entry against its own module rather than against its dotted name.
            "-o",
            "junit_family=xunit1",
            "--junitxml",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=CHECKER_TIMEOUT_SECONDS,
        env=environment,
        cwd=str(directory),
    )


def _plant(directory: Path, *, include_the_vanishing_test: bool) -> None:
    """Write the planted mini-suite, with or without the test that runs nowhere."""
    source = PLANTED_HEAD + (PLANTED_VANISHING_TEST if include_the_vanishing_test else "")
    (directory / "test_planted.py").write_text(source, encoding="utf-8")
    (directory / "pytest.ini").write_text(PLANTED_INI, encoding="utf-8")


def _record_platform(report: Path, platform: str) -> None:
    """Write the sidecar a real matrix leg writes beside its report."""
    report.with_suffix(report.suffix + ".env.json").write_text(
        json.dumps({"sys_platform": platform}), encoding="utf-8"
    )


def _run_checker(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run the real command line, resolving imports from this tree and nothing else (A94)."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "bench.coverage", *arguments],
        capture_output=True,
        text=True,
        timeout=CHECKER_TIMEOUT_SECONDS,
        env=environment,
        cwd=str(PROJECT_ROOT),
    )


def _file_of(classname: str, name: str) -> str:
    """Return the source file a planted case lives in, the way pytest records it under xunit1.

    A collection-level entry (empty classname) names its module, so the module IS the file. A
    test's file is its module's file, and the default here assumes the classname is the module --
    true for every case but a test inside a class, which passes its file explicitly through
    ``files``. Planting this is not a convenience: the whole distinction between a module and a
    directory named after it lives in this attribute, so a helper that omitted it would model a
    report the CI matrix never writes.
    """
    dotted = name if not classname else classname
    return dotted.replace(".", "/") + ".py"


def _write_report(
    path: Path,
    cases: Sequence[tuple[str, str, str]],
    *,
    tests: int | None = None,
    platform: str | None = "",
    files: Mapping[str, str] | None = None,
) -> None:
    """Write a junit report holding the cases, and the platform its leg ran on.

    ``platform`` defaults to the one the file name implies, because a leg that records no
    platform is UNMEASURED by design and most probes here want a readable matrix. Pass
    ``None`` to write no sidecar and exercise that refusal. ``files`` overrides the source file
    of individual cases, keyed by ``classname::name``.
    """
    body: list[str] = []
    overrides = dict(files or {})
    for classname, name, outcome in cases:
        source = overrides.get(f"{classname}::{name}", _file_of(classname, name))
        opening = f'<testcase classname="{classname}" name="{name}" file="{source}" time="0.1">'
        if outcome == "pass":
            body.append(opening + "</testcase>")
        elif outcome == "skip":
            body.append(
                opening + '<skipped type="pytest.skip" message="planted skip"/></testcase>'
            )
        elif outcome == "xfail":
            body.append(
                opening + '<skipped type="pytest.xfail" message="planted debt"/></testcase>'
            )
        elif outcome == "fail":
            body.append(opening + '<failure message="planted failure"/></testcase>')
        elif outcome == "error":
            body.append(opening + '<error message="planted error"/></testcase>')
        else:  # pragma: no cover - a typo in a test is a test bug, and it must be loud
            raise AssertionError(f"unknown planted outcome {outcome!r}")
    count = len(cases) if tests is None else tests
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" '
        f'tests="{count}" time="1.0">' + "".join(body) + "</testsuite></testsuites>\n",
        encoding="utf-8",
    )
    if platform is None:
        return
    recorded = platform or ("linux" if "posix" in path.name else "win32")
    path.with_suffix(path.suffix + ".env.json").write_text(
        json.dumps({"sys_platform": recorded}), encoding="utf-8"
    )


def _matrix(directory: Path, windows: Sequence[tuple[str, str, str]], posix: Sequence[tuple[str, str, str]]) -> list[str]:
    """Write a two-family matrix of planted reports and return the checker arguments."""
    windows_report = directory / "windows.xml"
    posix_report = directory / "posix.xml"
    _write_report(windows_report, windows)
    _write_report(posix_report, posix)
    return [
        "--report",
        f"windows:planted={windows_report}",
        "--report",
        f"posix:planted={posix_report}",
    ]


# --- the flagship probes: real sessions, real reports, the real command line -------------------


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_a_test_skipped_on_every_family_turns_the_check_red_and_is_named(tmp_path: Path) -> None:
    """The L4 case, end to end: two real pytest sessions, and the checker names the vanished test."""
    suite = tmp_path / "suite"
    suite.mkdir()
    _plant(suite, include_the_vanishing_test=True)
    windows_report = tmp_path / "windows.xml"
    posix_report = tmp_path / "posix.xml"

    windows_session = _run_planted_session(suite, "windows", windows_report)
    posix_session = _run_planted_session(suite, "posix", posix_report)
    _record_platform(windows_report, "win32")
    _record_platform(posix_report, "linux")
    # Both sessions are GREEN: every skip is legal pytest, and nothing in the session can see
    # that this test ran on no family. That is exactly why the check has to be cross-run.
    assert windows_session.returncode == 0, windows_session.stdout[-2000:]
    assert posix_session.returncode == 0, posix_session.stdout[-2000:]

    result = _run_checker(
        [
            "--report",
            f"windows:planted={windows_report}",
            "--report",
            f"posix:planted={posix_report}",
        ]
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "RAN ON NO FAMILY" in result.stdout
    assert "test_planted::test_skipped_on_every_family" in result.stdout
    # The control test and the one-family test must NOT be named as offenders.
    assert "test_planted::test_runs_everywhere" not in result.stdout.split("RAN ON NO FAMILY")[1]


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_the_same_matrix_without_the_vanishing_test_is_green(tmp_path: Path) -> None:
    """The control for the probe above: the red came from the plant, not from the harness."""
    suite = tmp_path / "suite"
    suite.mkdir()
    _plant(suite, include_the_vanishing_test=False)
    windows_report = tmp_path / "windows.xml"
    posix_report = tmp_path / "posix.xml"
    _run_planted_session(suite, "windows", windows_report)
    _run_planted_session(suite, "posix", posix_report)
    _record_platform(windows_report, "win32")
    _record_platform(posix_report, "linux")

    result = _run_checker(
        [
            "--report",
            f"windows:planted={windows_report}",
            "--report",
            f"posix:planted={posix_report}",
        ]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "COVERED" in result.stdout
    # The test that ran on one family only is reported, never silenced (TR-8).
    assert "declared partial coverage" in result.stdout
    assert "test_planted::test_runs_on_one_family_only" in result.stdout


def _plant_the_sibling_directory(root: Path) -> None:
    """Plant a module that runs nowhere beside a directory that shares its stem.

    ``pkg/test_optional_extra.py`` is skipped at collection on every family, and
    ``pkg/test_optional_extra/test_helper.py`` is an ordinary passing test. pytest mangles the
    helper's address into the classname ``pkg.test_optional_extra.test_helper``, which has the
    vanished module's dotted name as a prefix -- the same shape a test CLASS inside the module
    would produce. The directory is one ``mkdir`` inside a component's own ``tests/<area>/**``
    scope, so this is a member of the satisfying set that a test author can create from the
    files they are already editing, which A89 says an attribution rule must not permit.

    ``pkg/test_windows_only.py`` is the control in the same tree: a module skipped at collection
    on one family that runs its OWN tests on the other, which must stay covered.
    """
    package = root / "pkg"
    (package / "test_optional_extra").mkdir(parents=True)
    (root / "pytest.ini").write_text(PLANTED_INI, encoding="utf-8")
    (package / "test_control.py").write_text(
        "def test_control() -> None:\n    assert True\n", encoding="utf-8"
    )
    (package / "test_optional_extra.py").write_text(
        "import pytest\n\n"
        'pytest.importorskip("okto_grafx_extra_that_no_job_installs")\n\n\n'
        "def test_needs_the_extra() -> None:\n"
        '    raise AssertionError("this body runs on no family, which is the whole problem")\n',
        encoding="utf-8",
    )
    (package / "test_optional_extra" / "test_helper.py").write_text(
        "def test_helper() -> None:\n    assert True\n", encoding="utf-8"
    )
    (package / "test_windows_only.py").write_text(
        "import os\n\nimport pytest\n\n"
        'if os.environ.get("PLANTED_FAMILY", "") != "windows":\n'
        '    pytest.skip("planted: this module runs on the windows leg", '
        "allow_module_level=True)\n\n\n"
        "def test_only_on_windows() -> None:\n    assert True\n",
        encoding="utf-8",
    )


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_a_sibling_directory_named_after_a_module_does_not_cover_it(tmp_path: Path) -> None:
    """A module that ran on NO family must not be certified by a different module beside it.

    End to end on two real sessions: ``pkg/test_optional_extra.py`` is skipped at collection on
    both families, so its body runs nowhere, while ``pkg/test_optional_extra/test_helper.py``
    passes on both. Resolving the module entry by dotted prefix certifies the vanished module
    from the helper's classname; resolving it by the file pytest recorded does not. The control
    module in the same tree -- skipped at collection on one family, running its own tests on the
    other -- must stay covered, so the fix cannot be "call every module entry a violation".
    """
    suite = tmp_path / "suite"
    suite.mkdir()
    _plant_the_sibling_directory(suite)
    windows_report = tmp_path / "windows.xml"
    posix_report = tmp_path / "posix.xml"

    windows_session = _run_planted_session(suite, "windows", windows_report)
    posix_session = _run_planted_session(suite, "posix", posix_report)
    _record_platform(windows_report, "win32")
    _record_platform(posix_report, "linux")
    assert windows_session.returncode == 0, windows_session.stdout[-2000:]
    assert posix_session.returncode == 0, posix_session.stdout[-2000:]

    result = _run_checker(
        [
            "--report",
            f"windows:planted={windows_report}",
            "--report",
            f"posix:planted={posix_report}",
        ]
    )
    assert result.returncode == 1, result.stdout + result.stderr
    offenders = result.stdout.split("RAN ON NO FAMILY")[1]
    assert "::pkg.test_optional_extra  [" in offenders
    # The helper that merely lives in a directory of that name is not the module, and the module
    # skipped on one family that ran its own tests on the other is still covered.
    assert "::pkg.test_windows_only  [" not in result.stdout
    assert "test_helper" not in offenders


# --- the rule, probed through the command line on planted reports -----------------------------


def test_a_missing_report_is_unmeasured_and_never_a_pass(tmp_path: Path) -> None:
    """A count that could not be taken is not a count of zero (A75.2)."""
    present = tmp_path / "windows.xml"
    _write_report(present, [("mod", "test_one", "pass")])
    result = _run_checker(
        [
            "--report",
            f"windows:planted={present}",
            "--report",
            f"posix:planted={tmp_path / 'absent.xml'}",
        ]
    )
    assert result.returncode == 2, result.stdout
    assert "UNMEASURED" in result.stdout
    assert "could not be read" in result.stdout


def test_an_unparseable_report_is_unmeasured(tmp_path: Path) -> None:
    """A truncated report is a failure to measure, not an empty matrix."""
    broken = tmp_path / "posix.xml"
    broken.write_text("<testsuite><testcase classname='m' name='t'>", encoding="utf-8")
    good = tmp_path / "windows.xml"
    _write_report(good, [("mod", "test_one", "pass")])
    result = _run_checker(
        ["--report", f"windows:planted={good}", "--report", f"posix:planted={broken}"]
    )
    assert result.returncode == 2, result.stdout
    assert "not valid XML" in result.stdout


def test_a_report_with_no_testcase_is_unmeasured(tmp_path: Path) -> None:
    """An empty suite means the session collected nothing, which certifies nothing."""
    empty = tmp_path / "posix.xml"
    empty.write_text('<testsuite name="pytest" tests="0"></testsuite>', encoding="utf-8")
    good = tmp_path / "windows.xml"
    _write_report(good, [("mod", "test_one", "pass")])
    result = _run_checker(
        ["--report", f"windows:planted={good}", "--report", f"posix:planted={empty}"]
    )
    assert result.returncode == 2, result.stdout
    assert "nothing ran" in result.stdout


def test_one_family_alone_cannot_certify_the_matrix(tmp_path: Path) -> None:
    """A matrix that lost a family is UNMEASURED; a single family may not stand for both (G4)."""
    only = tmp_path / "windows.xml"
    _write_report(only, [("mod", "test_one", "pass")])
    result = _run_checker(["--report", f"windows:planted={only}"])
    assert result.returncode == 2, result.stdout
    assert "required family 'posix'" in result.stdout


def test_a_job_whose_platform_contradicts_its_label_is_unmeasured(tmp_path: Path) -> None:
    """The family label is a claim from the workflow file, so it is checked against the run."""
    windows_report = tmp_path / "windows.xml"
    posix_report = tmp_path / "posix.xml"
    _write_report(windows_report, [("mod", "test_one", "pass")])
    # The POSIX job actually ran on Windows: two Windows jobs wearing one POSIX label would
    # certify a family that never ran.
    _write_report(posix_report, [("mod", "test_one", "pass")], platform="win32")
    result = _run_checker(
        [
            "--report",
            f"windows:planted={windows_report}",
            "--report",
            f"posix:planted={posix_report}",
        ]
    )
    assert result.returncode == 2, result.stdout
    assert "sys.platform" in result.stdout


def test_a_debt_file_that_cannot_be_read_is_unmeasured(tmp_path: Path) -> None:
    """An unreadable allowlist must not silently become an empty one."""
    arguments = _matrix(tmp_path, [("mod", "test_one", "pass")], [("mod", "test_one", "pass")])
    result = _run_checker([*arguments, "--debt", str(tmp_path / "no-such-file.txt")])
    assert result.returncode == 2, result.stdout
    assert "debt file could not be read" in result.stdout


def test_the_offender_is_in_the_json_artefact(tmp_path: Path) -> None:
    """The verdict is machine-readable, so a later job can act on it without scraping text."""
    arguments = _matrix(
        tmp_path,
        [("mod", "test_one", "pass"), ("mod", "test_gone", "skip")],
        [("mod", "test_one", "pass"), ("mod", "test_gone", "skip")],
    )
    artefact = tmp_path / "verdict.json"
    result = _run_checker([*arguments, "--json", str(artefact)])
    assert result.returncode == 1, result.stdout
    verdict = json.loads(artefact.read_text(encoding="utf-8"))
    assert verdict["status"] == "violations"
    assert verdict["exit_code"] == 1
    assert [entry["identity"] for entry in verdict["never_ran"]] == ["mod::test_gone"]
    assert verdict["population"] == 2
    assert verdict["covered"] == 1


def test_a_bad_argument_is_a_diagnosis_and_not_a_traceback(tmp_path: Path) -> None:
    """Nothing escapes the command line: a malformed request reads as UNMEASURED."""
    result = _run_checker(["--report", "this-is-not-a-label"])
    assert result.returncode == 2, result.stdout
    assert "Traceback" not in result.stderr
    assert "is not FAMILY:PROFILE=PATH" in result.stdout


# --- the rule, probed directly on the categories it distinguishes ------------------------------


def _verdict(
    tmp_path: Path,
    windows: Sequence[tuple[str, str, str]],
    posix: Sequence[tuple[str, str, str]],
    debt: frozenset[str] = frozenset(),
    files: Mapping[str, str] | None = None,
) -> Verdict:
    """Evaluate one planted two-family matrix in process."""
    windows_report = tmp_path / "windows.xml"
    posix_report = tmp_path / "posix.xml"
    _write_report(windows_report, windows, files=files)
    _write_report(posix_report, posix, files=files)
    reports = [
        read_report(job="windows/planted", family="windows", profile="planted", path=str(windows_report)),
        read_report(job="posix/planted", family="posix", profile="planted", path=str(posix_report)),
    ]
    return evaluate(reports, debt=debt)


def test_a_failing_test_counts_as_executed(tmp_path: Path) -> None:
    """A test that ran and failed did not vanish; the failure is red on its own."""
    verdict = _verdict(
        tmp_path, [("mod", "test_one", "fail")], [("mod", "test_one", "skip")]
    )
    assert verdict.status == STATUS_COVERED
    assert verdict.covered == 1


def test_an_xfail_everywhere_is_a_violation_until_it_is_declared(tmp_path: Path) -> None:
    """Registered debt is still debt: it must be listed, not merely marked."""
    cases = [("mod", "test_one", "pass"), ("mod", "test_debt", "xfail")]
    verdict = _verdict(tmp_path, cases, cases)
    assert verdict.status == STATUS_VIOLATIONS
    assert [node.identity for node in verdict.never_ran] == ["mod::test_debt"]
    assert verdict.never_ran[0].debt is True

    declared = _verdict(tmp_path, cases, cases, debt=frozenset({"mod::test_debt"}))
    assert declared.status == STATUS_COVERED
    assert [node.identity for node in declared.declared_debt] == ["mod::test_debt"]


def test_a_debt_entry_that_ran_is_reported_as_stale_and_is_not_a_failure(tmp_path: Path) -> None:
    """Paying off a debt must never turn the build red."""
    verdict = _verdict(
        tmp_path,
        [("mod", "test_one", "pass")],
        [("mod", "test_one", "pass")],
        debt=frozenset({"mod::test_one", "mod::test_that_no_report_mentions"}),
    )
    assert verdict.status == STATUS_COVERED
    assert verdict.stale_debt == ("mod::test_one", "mod::test_that_no_report_mentions")


def test_a_module_skipped_on_every_job_is_named_by_its_module(tmp_path: Path) -> None:
    """A module-level importorskip no job satisfies is a whole module that ran nowhere."""
    cases = [("mod", "test_one", "pass"), ("", "pkg.test_optional", "skip")]
    verdict = _verdict(tmp_path, cases, cases)
    assert verdict.status == STATUS_VIOLATIONS
    assert [node.identity for node in verdict.never_ran] == ["::pkg.test_optional"]


def test_a_module_skipped_on_one_job_is_covered_by_the_tests_it_ran_on_another(tmp_path: Path) -> None:
    """The honest optional-extra case: skipped where the extra is absent, run where it is not."""
    verdict = _verdict(
        tmp_path,
        [("pkg.test_optional", "test_needs_the_extra", "pass")],
        [("", "pkg.test_optional", "skip")],
    )
    assert verdict.status == STATUS_COVERED, verdict.never_ran
    assert verdict.covered == 2


def test_a_test_of_a_class_covers_its_module_entry(tmp_path: Path) -> None:
    """A class adds a segment to the classname; module resolution must still see the module.

    The class lives in the module's own file, which is what makes it the module -- and what
    tells it apart from a same-named directory holding a different module.
    """
    verdict = _verdict(
        tmp_path,
        [("pkg.test_optional.TestGroup", "test_inside_a_class", "pass")],
        [("", "pkg.test_optional", "skip")],
        files={"pkg.test_optional.TestGroup::test_inside_a_class": "pkg/test_optional.py"},
    )
    assert verdict.status == STATUS_COVERED, verdict.never_ran


def test_a_module_that_only_looks_like_a_prefix_does_not_cover_it(tmp_path: Path) -> None:
    """``pkg.test_optional_extra`` is a different module from ``pkg.test_optional``."""
    verdict = _verdict(
        tmp_path,
        [("pkg.test_optional_extra", "test_one", "pass")],
        [("", "pkg.test_optional", "skip")],
    )
    assert verdict.status == STATUS_VIOLATIONS
    assert [node.identity for node in verdict.never_ran] == ["::pkg.test_optional"]


def test_a_module_entry_with_no_source_file_is_unmeasured_and_not_a_violation(
    tmp_path: Path,
) -> None:
    """Undecidable is UNMEASURED, not red and not green (A75.2).

    Under pytest's DEFAULT junit family (``xunit2``) the ``file`` attribute is dropped, so a
    collection-level entry arrives with nothing but its dotted name -- and a dotted name cannot
    tell a class inside the module from a different module in a directory of the same stem. The
    check must not guess in either direction: calling it covered is the hole B1 closed, and
    calling it a violation would turn every default-family report into a false red, which is how
    a gate earns the reputation that gets it bypassed. It says it could not read the report, and
    names the option that fixes it.
    """
    windows = tmp_path / "windows.xml"
    posix = tmp_path / "posix.xml"
    for path, platform_name in ((windows, "win32"), (posix, "linux")):
        path.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<testsuite name="pytest" errors="0" failures="0" skipped="1" tests="2">'
            '<testcase classname="pkg.test_one" name="test_one" time="0.1"></testcase>'
            '<testcase classname="" name="pkg.test_optional" time="0.0">'
            '<skipped message="collection skipped"/></testcase></testsuite>\n',
            encoding="utf-8",
        )
        _record_platform(path, platform_name)
    result = _run_checker(
        ["--report", f"windows:planted={windows}", "--report", f"posix:planted={posix}"]
    )
    assert result.returncode == 2, result.stdout
    assert "UNMEASURED" in result.stdout
    assert "junit_family=xunit1" in result.stdout


def test_a_matrix_that_reported_no_readable_node_is_unmeasured_and_never_covered(
    tmp_path: Path,
) -> None:
    """A population of zero certifies nothing, so it is exit 2 and never exit 0 (A75.2).

    "Every reported node ran somewhere" is true of the empty set, and the vacuous reading is a
    green build over nothing -- which is the failure mode this whole check exists to prevent.
    The shape is reachable: a matrix in which every module failed to import writes junit full of
    collection errors, and collection errors are deliberately excluded from the population, so
    both legs arrive with entries and none of them is a node. Read through the real command line,
    because the exit code is the claim; and asserted in the other direction with the same
    reports plus one test that ran, so the refusal comes from the emptiness and not the harness.
    """
    errors_only = [("", "pkg.test_one", "error"), ("", "pkg.test_two", "error")]
    result = _run_checker(_matrix(tmp_path, errors_only, errors_only))
    assert result.returncode == 2, (
        "a matrix that reported nothing but collection errors was certified COVERED; a count "
        f"of zero is a failure to count, not a suite in which everything ran.\n{result.stdout}"
    )
    assert "UNMEASURED" in result.stdout
    assert "certifies nothing" in result.stdout

    verdict = _verdict(tmp_path, errors_only, errors_only)
    assert verdict.status == STATUS_UNMEASURED
    assert verdict.population == 0
    assert verdict.exit_code == 2

    with_one_node = [*errors_only, ("pkg.test_three", "test_one", "pass")]
    green = _verdict(tmp_path, with_one_node, with_one_node)
    assert green.status == STATUS_COVERED, green.unmeasured
    assert green.population == 1


def test_partial_coverage_is_reported_and_is_not_a_failure(tmp_path: Path) -> None:
    """TR-8: a test that runs on one family only is declared partial coverage, never silence."""
    verdict = _verdict(
        tmp_path,
        [("mod", "test_windows_only", "pass"), ("mod", "test_both", "pass")],
        [("mod", "test_windows_only", "skip"), ("mod", "test_both", "pass")],
    )
    assert verdict.status == STATUS_COVERED
    assert [node.identity for node in verdict.partial] == ["mod::test_windows_only"]
    assert verdict.partial[0].executed_on == ("windows",)


def test_the_reason_string_has_no_vote(tmp_path: Path) -> None:
    """A75/A54: the message is reported, and it decides nothing."""
    windows_report = tmp_path / "windows.xml"
    windows_report.write_text(
        '<testsuite name="pytest" tests="1">'
        '<testcase classname="mod" name="test_one">'
        '<skipped type="pytest.skip" message="platform_specific optional_dependency pending "/>'
        "</testcase></testsuite>",
        encoding="utf-8",
    )
    _record_platform(windows_report, "win32")
    posix_report = tmp_path / "posix.xml"
    _write_report(posix_report, [("mod", "test_one", "skip")])
    verdict = evaluate(
        [
            read_report(job="w", family="windows", profile="p", path=str(windows_report)),
            read_report(job="p", family="posix", profile="p", path=str(posix_report)),
        ]
    )
    assert verdict.status == STATUS_VIOLATIONS
    assert [node.identity for node in verdict.never_ran] == ["mod::test_one"]


# --- the constants that decide whether the gate exists at all (A56) ----------------------------


def test_a_job_that_collapsed_cannot_let_the_others_certify_the_matrix(tmp_path: Path) -> None:
    """A report with three entries beside one with a hundred is a hole, not a small suite."""
    windows = [("mod", f"test_{index}", "pass") for index in range(20)]
    posix = [("mod", "test_0", "pass")]
    verdict = _verdict(tmp_path, windows, posix)
    assert verdict.status == STATUS_UNMEASURED
    assert any("collapsed" in reason for reason in verdict.unmeasured)


def test_a_profile_difference_of_a_few_tests_is_not_a_collapse(tmp_path: Path) -> None:
    """The floor must sit far below a legitimate difference between two profiles."""
    windows = [("mod", f"test_{index}", "pass") for index in range(20)]
    posix = [("mod", f"test_{index}", "pass") for index in range(18)]
    verdict = _verdict(tmp_path, windows, posix)
    assert verdict.status == STATUS_COVERED, verdict.unmeasured


def test_the_quorum_floor_is_pinned() -> None:
    """A56: lowering this constant to zero switches the collapse rule off in one token."""
    assert MINIMUM_JOB_SHARE == 0.5


def test_a_leg_whose_shards_are_all_present_is_read_as_one_job(tmp_path: Path) -> None:
    """A suite split so a slow shard cannot kill the reading still reads as one leg."""
    artefacts = tmp_path / "artifacts"
    for family, platform in (("windows", "win32"), ("posix", "linux")):
        leg = artefacts / f"junit-{family}-bare"
        leg.mkdir(parents=True)
        _write_report(leg / "shard-1.xml", [("mod", "test_one", "pass")], platform=platform)
        _write_report(leg / "shard-2.xml", [("mod", "test_two", "pass")], platform=platform)
    result = _run_checker(["--reports-dir", str(artefacts)])
    assert result.returncode == 0, result.stdout
    assert "reported=2" in result.stdout


def test_a_leg_that_wrote_no_junit_at_all_is_unmeasured(tmp_path: Path) -> None:
    """C0's wheel-build fixture killed pytest before it wrote junit; that must never read green."""
    artefacts = tmp_path / "artifacts"
    good = artefacts / "junit-windows-bare"
    good.mkdir(parents=True)
    _write_report(good / "report.xml", [("mod", "test_one", "pass")])
    (artefacts / "junit-posix-bare").mkdir(parents=True)
    result = _run_checker(["--reports-dir", str(artefacts)])
    assert result.returncode == 2, result.stdout
    assert "no .xml report" in result.stdout


def test_a_leg_missing_one_shard_is_unmeasured_not_partly_covered(tmp_path: Path) -> None:
    """Merging fails closed: the tests in a lost shard must not vanish from the population."""
    from bench.coverage.junit import ReadFailure as Failure
    from bench.coverage.junit import merge_reports

    good = tmp_path / "shard-1.xml"
    _write_report(good, [("mod", "test_one", "pass")])
    parts = [
        read_report(job="j", family="windows", profile="bare", path=str(good)),
        read_report(job="j", family="windows", profile="bare", path=str(tmp_path / "gone.xml")),
    ]
    merged = merge_reports("j", "windows", "bare", parts)
    assert isinstance(merged, Failure)


def test_a_leg_that_records_no_platform_cannot_certify_its_family(tmp_path: Path) -> None:
    """The family label is written by the workflow; unverified, it is just a password (A89)."""
    windows_report = tmp_path / "windows.xml"
    posix_report = tmp_path / "posix.xml"
    _write_report(windows_report, [("mod", "test_one", "pass")])
    _write_report(posix_report, [("mod", "test_one", "pass")], platform=None)
    result = _run_checker(
        [
            "--report",
            f"windows:planted={windows_report}",
            "--report",
            f"posix:planted={posix_report}",
        ]
    )
    assert result.returncode == 2, result.stdout
    assert "only a claim" in result.stdout


def test_a_platform_of_no_known_family_is_unmeasured(tmp_path: Path) -> None:
    """An unknown platform is not quietly folded into whichever family was claimed."""
    windows_report = tmp_path / "windows.xml"
    posix_report = tmp_path / "posix.xml"
    _write_report(windows_report, [("mod", "test_one", "pass")])
    _write_report(posix_report, [("mod", "test_one", "pass")], platform="vms")
    verdict = evaluate(
        [
            read_report(job="w", family="windows", profile="p", path=str(windows_report)),
            read_report(job="p", family="posix", profile="p", path=str(posix_report)),
        ]
    )
    assert verdict.status == STATUS_UNMEASURED
    assert any("no D9 family" in reason for reason in verdict.unmeasured)


def test_the_required_families_are_pinned() -> None:
    """Narrowing this set to one family would let half a matrix certify the whole of it."""
    assert REQUIRED_FAMILIES == ("windows", "posix")


def test_only_an_absent_skipped_element_reads_as_an_execution() -> None:
    """Pin the discriminator: widening it to accept a skip would empty the rule in one token."""
    assert [outcome.value for outcome in Outcome] == ["executed", "skipped", "declared_debt"]


def test_a_read_failure_is_a_value_and_not_an_exception(tmp_path: Path) -> None:
    """The reader never raises, so a caller cannot accidentally treat a crash as a clean run."""
    failure = read_report(job="j", family="windows", profile="p", path=str(tmp_path / "gone.xml"))
    assert isinstance(failure, ReadFailure)
    assert failure.job == "j"


def test_the_debt_file_of_this_repository_parses() -> None:
    """The file the workflow passes must be readable, or every run is UNMEASURED."""
    identities, error = read_debt_file(str(PROJECT_ROOT / "bench" / "coverage_debt.txt"))
    assert error == ""
    assert isinstance(identities, frozenset)


def test_the_check_reports_unmeasured_when_it_is_handed_nothing() -> None:
    """No reports at all is the most obvious way to certify a suite nobody ran."""
    verdict = evaluate([], problems=["no report was given; nothing was measured"])
    assert verdict.status == STATUS_UNMEASURED
    assert verdict.exit_code == 2
