"""Execute the literal modern-query examples in the frozen supplemental profile.

The other supplemental entries are compound contracts, covered by their owning
feature/fault/consumer suites; this file does not turn those entries into passes.
"""

import json
from pathlib import Path

import pytest

from okto_grafx import connect


_PROFILE = Path(__file__).resolve().parents[2] / "docs/conformance/EXTENSION_SCENARIOS_V1.json"
_CASES = tuple(item for item in json.loads(_PROFILE.read_text(encoding="utf-8"))["scenarios"]
               if "query" in item)


def test_literal_query_inventory_is_complete():
    assert {item["id"] for item in _CASES} == {
        "FP4-UNIT-CARDINALITY", "FP4-RETURNING-ZERO", "FP4-LEADING-WITH-IMPORT",
    }
    assert len(_CASES) == 3


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_frozen_query_columns_rows_effects_and_reopen(tmp_path, codec, case):
    path = tmp_path / "graph"
    with connect(path, codec=codec) as db:
        with db.begin("write") as tx:
            for statement in case.get("setup", ()):
                tx.execute(statement)
        before = db.execute("MATCH(n) RETURN count(n)").rows[0][0]
        with db.begin("write") as tx:
            result = tx.execute(case["query"])
            assert result.columns == tuple(case["columns"])
            assert result.rows == tuple(tuple(row) for row in case["rows"])
        expected_nodes = before + case.get("effects", {}).get("nodes_added", 0)
        assert db.execute("MATCH(n) RETURN count(n)").rows == ((expected_nodes,),)
        assert db.execute("MATCH()-[r]->() RETURN count(r)").rows == ((0,),)
        assert db.verify("all").findings == ()
        db.checkpoint()
    with connect(path, codec="pure", read_only=True) as reopened:
        assert reopened.execute("MATCH(n) RETURN count(n)").rows == ((expected_nodes,),)
        if case["id"] == "FP4-UNIT-CARDINALITY":
            assert reopened.execute("MATCH(n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (2,))
        assert reopened.verify("all").findings == ()
