"""The authority bridge cannot certify a partial or skipped native run."""

import importlib.util
from pathlib import Path

import pytest


BRIDGE = Path(__file__).with_name("rf0205_authority_evidence.py")
SPEC = importlib.util.spec_from_file_location("rf0205_authority_guard", BRIDGE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SELECTORS = (
    "tests/query/test_unwind_scalar_seek.py::test_old_snapshot_and_new_snapshot_see_their_own_versions",
    "tests/query/test_unwind_scalar_seek.py::test_generation_retry_after_first_key_never_replays_prefix",
)


def _report(tmp_path, names, *, skipped=False):
    path = tmp_path / "junit.xml"
    cases = "".join(
        '<testcase classname="tests.query.test_unwind_scalar_seek" '
        f'name="{name}">{"<skipped/>" if skipped else ""}</testcase>'
        for name in names
    )
    path.write_text(
        f'<testsuites><testsuite tests="{len(names)}" errors="0" failures="0" '
        f'skipped="{len(names) if skipped else 0}">{cases}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def test_exact_cases_pass(tmp_path):
    MODULE._verify_junit(_report(tmp_path, tuple(s.split("::")[1] for s in SELECTORS)), SELECTORS)


def test_missing_case_refused(tmp_path):
    with pytest.raises(ValueError, match="coverage incomplete"):
        MODULE._verify_junit(_report(tmp_path, (SELECTORS[0].split("::")[1],)), SELECTORS)


def test_wrong_case_refused(tmp_path):
    with pytest.raises(ValueError, match="outside closed set"):
        MODULE._verify_junit(_report(tmp_path, ("test_wrong", SELECTORS[1].split("::")[1])), SELECTORS)


def test_skipped_case_refused(tmp_path):
    with pytest.raises(ValueError, match="skips"):
        MODULE._verify_junit(_report(tmp_path, tuple(s.split("::")[1] for s in SELECTORS), skipped=True), SELECTORS)
