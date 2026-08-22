"""Driving the tool as a real process, the way an operator and a script do (C12).

Everything else in this suite calls :func:`okto_grafx.cli.entry.main` in-process, which is the
right level for behaviour but cannot see the two things only a process has: the exit status a
shell reads, and what the interpreter prints on its own way out. LESSONS L12 is the reason both
are here -- a component verified only against its own harness has been verified against its model
of the world.

Every child pins ``PYTHONPATH`` to this checkout (A94). A subprocess inherits an editable install,
which can resolve ``okto_grafx`` to a different tree than the one under test, and a reading taken
that way measures whatever happens to be installed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from okto_grafx import __version__
from okto_grafx.cli.exits import FINDINGS, OK, REFUSED, USAGE

from tests.cli.conftest import TABLE_STATEMENT, CliRunner, append_blank_page, child_environment

CHILD_TIMEOUT_SECONDS: float = 120.0
"""Bound on every child. A88: a hang must become a failure, never an indefinite wait."""

TEST_TIMEOUT_SECONDS: int = 600
"""Per-test bound for the tests in this module, which spawn real interpreters.

LESSONS L11: the project's 60-second bound is roughly twice the slowest ordinary test, and a
test that spawns two child interpreters can approach it on a machine running a dozen other
suites. Firing is ALWAYS the bad outcome here -- pytest-timeout's thread method hard-exits and
destroys the junit report with it, so the reading becomes UNMEASURED (A75.2) rather than
informative. The bound's only job is therefore to keep a genuine wedge finite, which is why it
is ten times the project default and not a duration this module is trying to police.
"""


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    """Run the tool as a real process and return what it did."""
    return subprocess.run(
        [sys.executable, "-m", "okto_grafx.cli", *argv],
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_SECONDS,
        env=child_environment(),
        check=False,
    )


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_the_module_entry_point_answers_a_shell() -> None:
    finished = _run("--version")
    assert finished.returncode == OK
    assert finished.stdout.strip() == f"oktografx {__version__}"
    assert finished.stderr == ""


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_a_real_process_reports_the_documented_status_for_a_clean_database(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "db")
    made = _run("query", path, TABLE_STATEMENT, "--write", "--create")
    assert made.returncode == OK, made.stderr
    verified = _run("verify", path)
    assert verified.returncode == OK
    assert "CLEAN" in verified.stdout


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_a_real_process_reports_the_documented_status_for_a_damaged_database(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "db")
    assert _run("query", path, TABLE_STATEMENT, "--write", "--create").returncode == OK
    append_blank_page(path, "heap.dat")
    verified = _run("verify", path)
    assert verified.returncode == FINDINGS
    assert "DAMAGED" in verified.stdout
    assert "Traceback" not in verified.stdout + verified.stderr


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_a_real_process_refuses_a_path_that_is_not_a_database(tmp_path: Path) -> None:
    target = tmp_path / "absent"
    finished = _run("status", str(target))
    assert finished.returncode == REFUSED
    assert not target.exists()
    assert "Traceback" not in finished.stdout + finished.stderr


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_a_real_process_refuses_a_command_line_it_cannot_read() -> None:
    finished = _run("frobnicate")
    assert finished.returncode == USAGE
    assert "Unknown command" in finished.stderr
    assert finished.stdout == ""


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_a_real_process_prints_nothing_on_the_wrong_stream() -> None:
    # A script reading stdout must get the answer and only the answer, so a refusal that lands
    # there would be parsed as a report.
    finished = _run("verify", "--scope", "banana")
    assert finished.returncode == USAGE
    assert finished.stdout == ""
    assert finished.stderr.strip()


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
@pytest.mark.parametrize("argv", [("--help",), ("status", "--help"), ("ledger", "--help")])
def test_asking_a_real_process_for_help_succeeds(argv: tuple[str, ...]) -> None:
    finished = _run(*argv)
    assert finished.returncode == OK
    assert finished.stdout.strip()
    assert finished.stderr == ""


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_a_real_process_survives_a_reader_that_closes_the_pipe(tmp_path: Path) -> None:
    # `oktografx metrics db | head -1` closes the pipe under a long report. The interpreter must
    # not print a BrokenPipeError underneath an answer the operator already has.
    path = str(tmp_path / "db")
    assert _run("query", path, TABLE_STATEMENT, "--write", "--create").returncode == OK
    producer = subprocess.Popen(
        [sys.executable, "-m", "okto_grafx.cli", "metrics", path, "--metrics", "openmetrics"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=child_environment(),
    )
    try:
        assert producer.stdout is not None
        assert producer.stderr is not None
        producer.stdout.readline()
        producer.stdout.close()
        producer.wait(timeout=CHILD_TIMEOUT_SECONDS)
        errors = producer.stderr.read()
    finally:
        if producer.poll() is None:  # pragma: no cover - only on a genuine hang
            producer.kill()
            producer.wait(timeout=CHILD_TIMEOUT_SECONDS)
        for stream in (producer.stdout, producer.stderr):
            if stream is not None and not stream.closed:
                stream.close()
    assert "Traceback" not in errors
    assert "BrokenPipeError" not in errors
    assert "Exception ignored" not in errors


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_a_real_process_answers_the_same_way_the_in_process_call_does(
    tmp_path: Path, cli: CliRunner
) -> None:
    # L12: a harness that is looser than the real thing certifies nothing about the real thing.
    # The two paths are compared on one database, so a divergence has somewhere to show.
    path = str(tmp_path / "db")
    assert _run("query", path, TABLE_STATEMENT, "--write", "--create").returncode == OK
    child = _run("verify", path, "--json")
    inside = cli("verify", path, "--json")
    assert child.returncode == inside.code
    import json

    from_child = json.loads(child.stdout)
    assert from_child["verdict"] == inside.document["verdict"]
    assert from_child["exit_code"] == inside.document["exit_code"]


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_every_test_in_this_module_carries_its_own_bound() -> None:
    # LESSONS L11, applied structurally rather than remembered: a test added here later would
    # inherit the project's 60-second bound, and whichever side of it that lands on would be
    # decided by how many sibling suites happen to be running. A73 -- make the mistake
    # unrepresentable rather than corrected once.
    import ast

    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=__file__)
    unbounded: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        bounds = [
            decorator
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "timeout"
        ]
        if not bounds:
            unbounded.append(node.name)
    assert not unbounded, f"these tests spawn interpreters with no bound of their own: {unbounded}"
    # And the rule proves it can fail: a module-level function without the decorator is found.
    planted = ast.parse("def test_planted() -> None:\n    pass\n")
    assert any(
        isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and not node.decorator_list
        for node in planted.body
    )


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_the_package_exposes_one_callable_entry_point() -> None:
    # A console script names ``module:function``; this pins that the supported spellings resolve
    # and that the module is not shadowed by the function it defines.
    import okto_grafx.cli
    import okto_grafx.cli.entry

    assert callable(okto_grafx.cli.main)
    assert okto_grafx.cli.main is okto_grafx.cli.entry.main
    assert okto_grafx.cli.entry.__name__ == "okto_grafx.cli.entry"
