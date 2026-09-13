"""Reference qualification must distinguish engine differences from broken observations."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from okto_grafx import DateValue, DecimalValue
from tools import qualify_parity_references as tool


def test_fixed_inventory_has_all_literal_extensions_and_unique_ids():
    cases = tool.scenarios()
    assert len(cases) == len({case["id"] for case in cases}) == 16
    assert {case["id"] for case in cases if case["id"].startswith("FP4-")} == {
        "FP4-UNIT-CARDINALITY", "FP4-RETURNING-ZERO", "FP4-LEADING-WITH-IMPORT",
    }


def test_tagged_values_preserve_types_scale_and_container_multiplicity():
    assert tool.tagged(True) != tool.tagged(1)
    assert tool.tagged(1) != tool.tagged(1.0)
    assert tool.tagged([1, 1]) != tool.tagged([1])
    assert tool.tagged([1]) == tool.tagged((1,))
    assert tool.tagged(Decimal("1.2500")) == tool.tagged(DecimalValue(12500, 12, 4))
    assert tool.tagged(Decimal("1.25")) != tool.tagged(Decimal("1.2500"))
    assert tool.tagged(date(2024, 2, 29)) == tool.tagged(DateValue(2024, 2, 29))
    assert tool.tagged({"num": 1}) != tool.tagged({"num": "1"})
    with pytest.raises(tool.ObservationError):
        tool.tagged(object())


@pytest.mark.parametrize("failure,after,expected", [
    (RuntimeError("null in primary key"), [[1]], "matches"),
    (RuntimeError("null in primary key"), [[2]], "differs"),
    (RuntimeError("lease timeout"), [[1]], "differs"),
    (tool.ObservationError("null in unsupported transport"), [[1]], "unavailable"),
])
def test_unrelated_errors_effect_changes_and_harness_errors_cannot_pass(monkeypatch, tmp_path, failure, after, expected):
    class Native:
        def __init__(self, *args):
            pass

        def execute(self, query):
            if query == "operation":
                raise failure
            return {"columns": ["id"], "rows": tool.tagged(after)}

        def close(self):
            pass

    monkeypatch.setattr(tool, "Native", Native)
    case = {"id": "case", "query": "operation", "error_contains": "null", "after": ("control", ["id"], [[1]])}
    assert tool.observe("grafx", tmp_path, case)["comparison"] == expected


def test_package_proof_checks_native_binary_resources_and_byte_drift(tmp_path, monkeypatch):
    package = tmp_path / "installed/example"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"# installed\n")
    (package / "native.pyd").write_bytes(b"native bytes")
    bundled = package.with_name("example.libs")
    bundled.mkdir()
    (bundled / "runtime.dll").write_bytes(b"bundled runtime")
    wheel = tmp_path / "example.whl"
    with ZipFile(wheel, "w") as archive:
        for path in package.iterdir():
            archive.writestr("example/" + path.name, path.read_bytes())
        archive.writestr("example.libs/runtime.dll", b"bundled runtime")
    monkeypatch.setattr(tool.sys, "prefix", str(tmp_path / "installed"))
    module = SimpleNamespace(__name__="example", __file__=str(package / "__init__.py"))
    assert len(tool.package_proof(module, wheel)["files"]) == 3
    (package / "native.pyd").write_bytes(b"different native bytes")
    with pytest.raises(AssertionError, match="differs"):
        tool.package_proof(module, wheel)
    (package / "native.pyd").write_bytes(b"native bytes")
    (bundled / "runtime.dll").write_bytes(b"changed auxiliary DLL")
    with pytest.raises(AssertionError, match="differs"):
        tool.package_proof(module, wheel)


@pytest.mark.parametrize("case", tool.scenarios(), ids=lambda case: case["id"])
def test_grafx_matches_independent_reference_scenario_oracles(tmp_path, case):
    observed = tool.observe("grafx", tmp_path, case)
    assert observed["comparison"] == "matches", observed
