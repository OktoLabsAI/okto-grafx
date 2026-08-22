"""The CI leg's verdict, probed on the real workflow file (A75, A86, CONTRACT G4).

``.github/workflows/ci.yml`` runs the suite with the session allowed to exit non-zero, so that a
red suite still uploads its junit report and still feeds the cross-family coverage check. The
leg's own result is then taken by a verification step. That step is a gate, and a gate is only a
gate if it can fail: this module extracts the step's script from the workflow file ITSELF -- not a
copy, not a paraphrase -- and runs it as a real subprocess over a planted leg, exactly the way the
runner does.

The case that matters is the one junit cannot express. pytest fails a session for reasons that
never reach the report: C0's per-session gate names unattributed skips and modules that vanished
from collection in a terminal section and then sets ``session.exitstatus = 1``, leaving a junit
whose ``failures`` and ``errors`` are both zero. A verification step that reads only those two
counters therefore reports GREEN on a session pytest failed, and every round spent hardening that
gate buys nothing in CI. So the recorded exit status is part of the verdict, and this module holds
the probe that says so.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
"""The repository root, which holds the workflow this module reads."""

WORKFLOW: Path = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
"""The real CI definition. The probe below runs the step out of this file, never a copy."""

VERIFICATION_HEREDOC: str = "python - <<'PYTHON'"
"""How the verification step opens its inline script inside the workflow's ``run:`` block."""

STEP_TIMEOUT_SECONDS: float = 60.0
"""A bound on the subprocess: a hang must become a failure, never a wait (A42/A88)."""

CLEAN_JUNIT: str = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="1" tests="2" '
    'time="1.0">'
    '<testcase classname="tests.txn.test_planted" name="test_control" '
    'file="tests/txn/test_planted.py" time="0.1"></testcase>'
    '<testcase classname="tests.txn.test_planted" name="test_unattributed" '
    'file="tests/txn/test_planted.py" time="0.1">'
    '<skipped type="pytest.skip" message="planted: no attributing marker at all"/>'
    "</testcase></testsuite></testsuites>\n"
)
"""What C0's gate leaves behind: two tests, one skip, zero failures and zero errors.

This is not a hypothetical shape. It is what a real session writes when the gate fires: the gate
runs in ``pytest_sessionfinish``, long after the report's counters are decided, and it has no way
to add a failure to them even if it wanted to.
"""


def _verification_script() -> str:
    """Return the verification step's script, read out of the real workflow file.

    Extracted textually rather than through a YAML parser: PyYAML is not a declared dependency of
    this project, and a probe that needs an undeclared import would skip on the leg that lacks it
    -- which is precisely the disappearance the rest of this suite exists to refuse.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert VERIFICATION_HEREDOC in workflow, (
        f"{WORKFLOW} no longer runs its leg verification as an inline python heredoc, so this "
        "probe is reading nothing; re-point it at whatever decides a leg's result now"
    )
    body = workflow[workflow.index(VERIFICATION_HEREDOC) :]
    lines: list[str] = []
    for line in body.splitlines()[1:]:
        if line.strip() == "PYTHON":
            break
        lines.append(line)
    script = textwrap.dedent("\n".join(lines))
    assert script.strip(), "the verification step's script is empty"
    return script


def _run_leg(directory: Path, *, junit: str | None, status: str | None) -> subprocess.CompletedProcess[str]:
    """Plant one leg's artefacts and run the real verification step over them."""
    script = directory / "verify_leg.py"
    script.write_text(_verification_script(), encoding="utf-8")
    if junit is not None:
        (directory / "report.xml").write_text(junit, encoding="utf-8")
    if status is not None:
        (directory / "pytest-status.txt").write_text(status, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=STEP_TIMEOUT_SECONDS,
        cwd=str(directory),
    )


@pytest.mark.timeout(120)
def test_a_leg_whose_session_failed_without_writing_junit_is_not_green(tmp_path: Path) -> None:
    """A non-zero pytest status that junit does not explain must fail the leg.

    Both directions, because a step that always failed would certify nothing either: the same
    junit with a status of 0 is a leg that passed, and with a status of 1 it is a leg that C0's
    per-session gate failed and the report cannot show.
    """
    failed = tmp_path / "failed"
    failed.mkdir()
    result = _run_leg(failed, junit=CLEAN_JUNIT, status="1\n")
    assert result.returncode != 0, (
        "the leg reads GREEN although pytest exited 1: C0's per-session gate sets "
        "session.exitstatus without writing a <failure>, so a verdict taken from junit counts "
        f"alone discards it.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    complaint = result.stdout + result.stderr
    assert "pytest exited 1" in complaint, complaint

    passed = tmp_path / "passed"
    passed.mkdir()
    control = _run_leg(passed, junit=CLEAN_JUNIT, status="0\n")
    assert control.returncode == 0, control.stdout + control.stderr

    # The other half of the same guard: a status that was never recorded is not a status of zero.
    # The suite step always writes the file, so an absent one means the runner died inside it --
    # UNMEASURED, and the leg must not be certified from the junit that happens to be on disk.
    lost = tmp_path / "lost"
    lost.mkdir()
    missing = _run_leg(lost, junit=CLEAN_JUNIT, status=None)
    assert missing.returncode != 0, missing.stdout + missing.stderr

    # And the producer must exist: the step above can only read a status some earlier step wrote,
    # and the two halves live in different steps of the same file, so nothing but this ties them.
    workflow = WORKFLOW.read_text(encoding="utf-8")
    suite_step = workflow[workflow.index("Run the whole suite") : workflow.index(
        VERIFICATION_HEREDOC
    )]
    assert "> pytest-status.txt" in suite_step, (
        "the suite step no longer records pytest's exit status, so the verification step above "
        "has nothing to read and every leg fails on a missing file"
    )
