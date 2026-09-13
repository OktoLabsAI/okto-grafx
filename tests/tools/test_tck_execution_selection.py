"""Package selection never removes cases or derives scope from their failures."""

import json
import sys

import pytest

from tests.tools.test_tck_ledger import report
from tools import check_opencypher
from tools.tck_ledger import build_ledger


def test_owner_requires_verified_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["check", "--checkout", str(tmp_path),
                                    "--output", str(tmp_path / "report.json"), "--owner", "FP-2"])
    with pytest.raises(SystemExit) as caught:
        check_opencypher.main()
    assert caught.value.code == 2


@pytest.mark.parametrize("owner,selected", [("FP-2", 0), ("FP-4", 1)])
def test_owner_selection_keeps_entire_source_inventory(monkeypatch, tmp_path, owner, selected):
    from tools import tck_native, tck_stateful

    current = report()
    ledger = build_ledger(current)
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger), encoding="utf-8")
    output = tmp_path / "report.json"
    executed = []

    class Backend:
        def __init__(self, **kwargs):
            pass

        def close(self):
            pass

    def execute(case, backend):
        executed.append(case["id"])
        return {"conformance": "passed"}

    current["graph_fixtures"] = {}
    monkeypatch.setattr(tck_native, "NativeScenarioBackend", Backend)
    monkeypatch.setattr(tck_stateful, "run_stateful_case", execute)
    monkeypatch.setattr(check_opencypher, "inventory", lambda _: current)
    monkeypatch.setattr(sys, "argv", ["check", "--checkout", str(tmp_path), "--output", str(output),
                                    "--owner", owner, "--verify-ledger", str(path), "--execute-stateful"])
    assert check_opencypher.main() == 0
    observed = json.loads(output.read_text(encoding="utf-8"))
    assert observed["case_count"] == len(observed["cases"]) == 1
    assert observed["execution_selection"]["case_count"] == selected
    assert len(executed) == selected
    assert observed["cases"][0]["conformance"] == ("passed" if selected else "not_run")
