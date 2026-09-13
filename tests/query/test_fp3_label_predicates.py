"""Node-label expressions are native predicates, not multi-label storage admission."""

from dataclasses import replace

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError
from okto_grafx.domain.query.ast import LabelPredicate, free_variables
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.analysis import analyze


@pytest.fixture
def graph():
    with connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE Other(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO Other)")
            tx.execute("CREATE(a:N {id:1}), (b:Other {id:2}) CREATE(a)-[:R]->(b)")
        yield db


@pytest.mark.parametrize("query,expected", [
    ("MATCH(n) RETURN n.id,n:N,n:Other ORDER BY n.id", ((1,True,False),(2,False,True))),
    ("MATCH(n:N) RETURN n:N:N,n:N:Other,n:n,n:Missing", ((True,False,False,False),)),
    ("MATCH(n) WHERE NOT n:Other RETURN n.id", ((1,),)),
    ("MATCH(n) WHERE n:N OR n:Other RETURN n.id ORDER BY n.id", ((1,),(2,))),
    ("MATCH(n) WITH n AS x WHERE x:N RETURN x:N", ((True,),)),
    ("MATCH(n:N) OPTIONAL MATCH(n)-[:Missing]->(m) RETURN m:N", ((None,),)),
    ("MATCH(n:N) RETURN [(n)-->(m) | m:Other] AS xs", (((True,),),)),
    ("MATCH p=(:N)-->() RETURN [n IN nodes(p) | n:N] AS xs", (((True,False),),)),
    ("MATCH(n:N) CALL(n) { RETURN n:N AS yes } RETURN yes", ((True,),)),
    ("MATCH(n:N) RETURN (CASE WHEN true THEN n ELSE null END):N", ((True,),)),
    ("MATCH(n:N) RETURN [n][0]:N", ((True,),)),
    ("MATCH(:N)-[r:R]->() RETURN r:R,r:N,r:r,r:R:R,r:R:N", ((True,False,False,True,False),)),
    ("RETURN null:N, NOT null:N, null:N IS NULL", ((None,None,True),)),
    ("MATCH(n) RETURN n:N AS yes, count(*) AS c ORDER BY yes", ((False,1),(True,1))),
    ("MATCH(n) RETURN coalesce(n:N,false) AS yes ORDER BY yes", ((False,),(True,))),
])
def test_expression_and_composed_query_results(graph, query, expected):
    result = graph.execute(query)
    assert result.rows == expected
    assert result.plan is not None
    with graph.query(query).cursor(batch_size=1) as cursor:
        assert tuple(cursor) == expected


@pytest.mark.parametrize("query", [
    "RETURN 1:N", "RETURN 'N':N", "RETURN []:N", "RETURN {}:N",
    "MATCH p=(:N)-->() RETURN p:N",
    "MATCH(n:N) RETURN (n:N):N",
])
def test_statically_wrong_subjects_refuse_even_without_selected_rows(graph, query):
    with pytest.raises(GrafxPlanError) as error:
        graph.execute(query)
    assert error.value.details["reason"] == "label_argument_type"
    assert error.value.details["query_phase"] == "planning"


def test_dynamic_subjects_validate_at_use_and_preserve_lazy_case(graph):
    assert graph.execute("RETURN $value:N", {"value":None}).rows == ((None,),)
    assert graph.execute("RETURN CASE WHEN false THEN $value:N ELSE true END", {"value":1}).rows == ((True,),)
    with pytest.raises(GrafxPlanError) as error:
        graph.execute("RETURN $value:N", {"value":1})
    assert error.value.details["query_phase"] == "execution"


def test_quoted_labels_roundtrip_and_source_column_names(graph):
    statement = parse("MATCH(n:N) RETURN n:`A`` B`:N AS result")
    expression = statement.return_clause.items[0].expression
    assert isinstance(expression, LabelPredicate)
    assert expression.labels == ("A` B", "N")
    assert free_variables(expression) == ("n",)
    assert parse(statement.describe()).return_clause.items[0].expression == expression
    result = graph.execute("MATCH(n:N) RETURN n : `N`")
    assert result.columns == ("n : `N`",) and result.rows == ((True,),)


@pytest.mark.parametrize("labels", [[], (1,), (), ("",)])
def test_hand_built_label_inventories_cannot_bypass_admission(labels):
    statement = parse("RETURN null:N")
    item = statement.return_clause.items[0]
    malformed = replace(statement, return_clause=replace(statement.return_clause,
        items=(replace(item, expression=replace(item.expression, labels=labels)),)))
    with pytest.raises(GrafxPlanError):
        analyze(malformed)


def test_late_dynamic_label_failure_rolls_back_statement_and_reopens(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxPlanError):
                tx.execute("UNWIND $items AS item CREATE(n:N {id:item.id}) RETURN item.value:N",
                           {"items":[{"id":1,"value":None},{"id":2,"value":1}]})
            assert tx.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
    with connect(path) as db:
        assert db.execute("MATCH(n:N) RETURN n.id, n:N").rows == ((99,True),)
        assert not db.verify("all").findings


def test_label_result_can_be_stored_as_boolean_without_changing_node_labels(tmp_path):
    path = tmp_path / "bool"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,flag BOOL,PRIMARY KEY(id))")
            assert tx.execute("CREATE(n:N {id:1}) SET n.flag=n:N RETURN n.flag").rows == ((True,),)
    with connect(path) as db:
        assert db.execute("MATCH(n:N) RETURN labels(n),n.flag,n:Other").rows == ((('N',),True,False),)
        assert not db.verify("all").findings
