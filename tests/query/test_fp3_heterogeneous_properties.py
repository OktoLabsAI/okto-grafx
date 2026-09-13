"""Polymorphic properties preserve per-row types, lazy semantics and rollback."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxPlanError


@pytest.fixture
def graph(tmp_path):
    with connect(tmp_path / "graph") as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Root(count INT64)")
            tx.execute("CREATE NODE TABLE Text(id INT64,var STRING)")
            tx.execute("CREATE NODE TABLE Int(id INT64,var INT64)")
            tx.execute("CREATE NODE TABLE Missing(id INT64)")
            tx.execute("CREATE REL TABLE GROUP T(FROM Root TO Text, FROM Root TO Int, FROM Root TO Missing)")
            tx.execute("CREATE(r:Root {count:0}), (a:Int {id:1,var:0}), (b:Text {id:2,var:'text'}), (c:Missing {id:3}) "
                       "CREATE(r)-[:T]->(a), (r)-[:T]->(b), (r)-[:T]->(c)")
        yield db


@pytest.mark.parametrize("projection,expected", [
    ("n.var", ((0,),('text',),(None,))),
    ("n.var > 'te'", ((None,),(True,),(None,))),
    ("n.var > 'te' OR n.var IS NOT NULL", ((True,),(True,),(None,))),
    ("n.var = 0", ((True,),(False,),(None,))),
    ("coalesce(n.var,'absent')", ((0,),('text',),('absent',))),
    ("CASE WHEN n:Text THEN upper(n.var) ELSE 'not text' END", (('not text',),('TEXT',),('not text',))),
])
def test_native_values_and_operators_keep_per_row_semantics(graph, projection, expected):
    query = f"MATCH(:Root)-->(n) RETURN {projection} AS v ORDER BY n.id"
    result = graph.execute(query)
    assert result.rows == expected
    with graph.query(query).cursor(batch_size=1) as cursor:
        assert tuple(cursor) == expected


def test_optional_or_and_nested_composition_do_not_drop_non_null_numeric_rows(graph):
    query = "MATCH(:Root)-->(n) WHERE n.var > 'te' OR n.var IS NOT NULL RETURN n.id ORDER BY n.id"
    assert graph.execute(query).rows == ((1,),(2,))
    assert graph.execute("MATCH(r:Root) RETURN [(r)-->(n) WHERE n.var > 'te' OR n.var IS NOT NULL | n.var] AS vs").rows == ((('text',0),),)
    assert graph.execute("MATCH(:Root)-->(n) CALL(n) { RETURN coalesce(n.var,'absent') AS v } "
                         "RETURN v ORDER BY n.id").rows == ((0,),('text',),('absent',))


def test_invalid_dynamic_function_refuses_at_execution_not_from_unselected_table_schema(graph):
    assert graph.execute("MATCH(:Root)-->(n) WHERE n:Text RETURN upper(n.var)").rows == (('TEXT',),)
    with pytest.raises(GrafxError):
        graph.execute("MATCH(:Root)-->(n) RETURN upper(n.var)")


def test_late_dynamic_arithmetic_error_restores_earlier_rows_of_the_statement(graph, tmp_path):
    with graph.begin("write") as tx:
        tx.execute("MATCH(r:Root) SET r.count=99")
        with pytest.raises(GrafxError):
            tx.execute("MATCH(r:Root) MATCH(n) WHERE n:Int OR n:Text "
                       "WITH r,n ORDER BY n.id SET r.count=n.var+1 RETURN r.count")
        assert tx.execute("MATCH(r:Root) RETURN r.count").rows == ((99,),)
    with connect(tmp_path / "graph") as reader:
        assert reader.execute("MATCH(r:Root) RETURN r.count").rows == ((99,),)
        assert not reader.verify('all').findings


def test_stored_column_types_remain_strict(graph):
    with graph.begin("write") as tx:
        with pytest.raises(GrafxError):
            tx.execute("MATCH(n:Int) SET n.var='not an integer'")
        assert tx.execute("MATCH(n:Int) RETURN n.var").rows == ((0,),)


def test_path_property_refusal_has_explicit_planning_evidence(graph):
    with pytest.raises(GrafxPlanError) as error:
        graph.execute("MATCH(n) MATCH p=(n)-[*]->() WHERE p.name='x' RETURN p")
    assert error.value.details['reason'] == 'path_property_type'
    assert error.value.details['query_phase'] == 'planning'
