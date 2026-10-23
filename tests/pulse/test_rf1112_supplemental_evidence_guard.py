"""The supplemental bridge must reject incomplete parametrized evidence."""

import importlib.util
from pathlib import Path

import pytest

BRIDGE = Path(__file__).with_name("rf1112_supplemental_evidence.py")
SPEC = importlib.util.spec_from_file_location("rf1112_bridge_guard", BRIDGE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
_verify_junit_cases = MODULE._verify_junit_cases


_BASE = "tests/bench/test_capacity_operational_proofs.py::test_checkpoint"
_SELECTORS = tuple(f"{_BASE}[{interval}]" for interval in (128, 512, 2048))


def _junit(tmp_path: Path, names: tuple[str, ...], *, body: str = "",
           skipped: int = 0, failures: int = 0) -> Path:
    cases = "".join(
        '<testcase classname="tests.bench.test_capacity_operational_proofs" '
        f'name="{name}">{body}</testcase>'
        for name in names
    )
    path = tmp_path / "result.xml"
    path.write_text(
        '<testsuites><testsuite '
        f'tests="{len(names)}" errors="0" failures="{failures}" skipped="{skipped}">'
        f"{cases}</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def test_exact_three_parameter_cases_are_accepted(tmp_path):
    names = tuple(f"test_checkpoint[{interval}]" for interval in (128, 512, 2048))
    _verify_junit_cases(_junit(tmp_path, names), _SELECTORS)


def test_missing_or_duplicate_parameter_case_is_rejected(tmp_path):
    names = tuple(f"test_checkpoint[{interval}]" for interval in (128, 512))
    with pytest.raises(ValueError, match="incomplete"):
        _verify_junit_cases(_junit(tmp_path, names), _SELECTORS)
    with pytest.raises(ValueError, match="exact selector coverage"):
        _verify_junit_cases(_junit(tmp_path, (*names, names[0])), _SELECTORS)


@pytest.mark.parametrize("body,skipped,failures", [
    ("<skipped/>", 1, 0),
    ("<failure/>", 0, 1),
])
def test_skip_and_failure_are_rejected(tmp_path, body, skipped, failures):
    selector = (_SELECTORS[0],)
    path = _junit(tmp_path, ("test_checkpoint[128]",), body=body,
                  skipped=skipped, failures=failures)
    with pytest.raises(ValueError, match="skips|failures"):
        _verify_junit_cases(path, selector)
