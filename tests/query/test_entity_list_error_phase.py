"""Heterogeneous selections retain runtime type checks and statement atomicity."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError


@pytest.mark.parametrize("function", ["labels", "properties", "keys", "type", "startNode", "endNode"])
@pytest.mark.parametrize("selection", ["items[1]", "[n,1][1]", "items[$slot]"])
def test_heterogeneous_entity_function_argument_refuses_during_execution(tmp_path, function, selection):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1})")
        with db.begin("write") as tx:
            tx.execute("CREATE (:Safe {id:9})")
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute(f"MATCH (n:N) SET n.changed=true WITH n,[n,1] AS items "
                           f"RETURN {function}({selection})", {"slot":1})
            assert failure.value.details["query_phase"] == "execution"
            assert failure.value.details["reason"] == "entity_function_argument_type"
            assert tx.execute("MATCH (n:N) RETURN n.changed").rows == ((None,),)
            assert tx.execute("MATCH (n:Safe) RETURN n.id").rows == ((9,),)


def test_heterogeneous_type_check_is_not_evaluated_for_an_empty_stream(tmp_path):
    with connect(tmp_path / "db") as db:
        assert db.execute("MATCH (n:Missing) WITH [n,1] AS items RETURN labels(items[1])").rows == ()


@pytest.mark.parametrize("query", ["RETURN labels([1,2][0])", "RETURN labels([1,2]['bad'])", "RETURN labels(1)"])
def test_proven_scalar_and_invalid_index_keep_compile_time_admission(tmp_path, query):
    with connect(tmp_path / "db") as db:
        with pytest.raises(GrafxPlanError):
            db.explain(query)
