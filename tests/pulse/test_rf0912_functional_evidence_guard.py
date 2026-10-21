"""The Pulse bridge must reject skipped and incomplete pytest observations."""

import importlib.util
from pathlib import Path

import pytest


BRIDGE = Path(__file__).with_name("rf0912_functional_evidence.py")
SPEC = importlib.util.spec_from_file_location("rf0912_bridge_guard", BRIDGE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SELECTORS = ("tests/api/test_example.py::test_a",)


def _write(tmp_path, *, skipped=0, body=""):
    path = tmp_path / "result.xml"
    path.write_text(
        '<testsuites><testsuite tests="1" errors="0" failures="0" '
        f'skipped="{skipped}">'
        '<testcase classname="tests.api.test_example" name="test_a">'
        f"{body}</testcase></testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def test_valid_single_selector_is_accepted(tmp_path):
    MODULE._verify_junit(_write(tmp_path), SELECTORS)


def test_skipped_case_is_rejected_even_if_process_would_exit_zero(tmp_path):
    with pytest.raises(ValueError, match="skips"):
        MODULE._verify_junit(_write(tmp_path, skipped=1, body="<skipped/>"), SELECTORS)


def test_missing_selector_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="coverage incomplete"):
        MODULE._verify_junit(_write(tmp_path), (*SELECTORS, "tests/api/test_other.py::test_b"))
