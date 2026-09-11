"""Native UNION entity identity across branches, aggregates and subquery scopes."""

import pytest

from okto_grafx import NodeValue, RelationshipValue, connect


@pytest.fixture(params=[None, 8192], ids=["memory", "spill"])
def db(request, tmp_path):
    with connect(tmp_path / "graph", query_memory_budget_bytes=request.param) as database:
        with database.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, value INT64, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id INT64, value INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM A TO A, value INT64)")
            tx.execute("CREATE (a:A {id:1,value:10})-[:R {value:7}]->(:A {id:2,value:20})")
            tx.execute("CREATE (:B {id:1,value:30})")
        yield database


@pytest.mark.parametrize("operator,expected", [("UNION", [("A", 1), ("A", 2), ("B", 1)]),
                                               ("UNION ALL", [("A", 1), ("A", 2), ("B", 1), ("A", 1)])])
def test_branch_composition_retains_table_incarnation_and_exact_multiplicity(db, operator, expected):
    result = db.execute(f"MATCH (n:A) RETURN n AS entity {operator} MATCH (n:B) RETURN n AS entity "
                        f"{operator} MATCH (n:A {{id:1}}) RETURN n AS entity")
    entities = [row[0] for row in result.rows]
    assert [(n.label, n.properties["id"]) for n in entities] == expected
    assert all(type(n) is NodeValue for n in entities)
    assert len({n.identity for n in entities}) == 3


@pytest.mark.parametrize("expression", ["n", "[n,{entity:n}]", "collect(n)", "collect(DISTINCT n)", "min(n)", "max(n)"])
def test_nested_and_aggregate_entities_survive_union_and_duplicate_elimination(db, expression):
    left = f"MATCH (n:A {{id:1}}) RETURN {expression} AS entity"
    right = f"MATCH (n:B {{id:1}}) RETURN {expression} AS entity"
    rows = db.execute(f"{left} UNION {right} UNION {left}").rows

    def leaves(value):
        if type(value) is NodeValue:
            return [value]
        if isinstance(value, dict):
            return [n for item in value.values() for n in leaves(item)]
        if isinstance(value, (tuple, list)):
            return [n for item in value for n in leaves(item)]
        return []

    assert len(rows) == 2
    for row, label, number in zip(rows, ("A", "B"), (10, 30), strict=True):
        found = leaves(row)
        assert found and all(n.label == label and n.properties["value"] == number for n in found)
        assert len({n.identity for n in found}) == 1
        assert all(not n.provenance.pending and n.provenance.version_lsn is not None for n in found)


def test_entities_and_scalars_do_not_collapse_when_local_numeric_ids_equal(db):
    rows = db.execute("MATCH (n:A {id:1}) RETURN n AS x UNION RETURN 1 AS x UNION RETURN true AS x").rows
    assert len(rows) == 3
    assert type(rows[0][0]) is NodeValue
    assert type(rows[1][0]) is int and rows[1][0] == 1
    assert type(rows[2][0]) is bool and rows[2][0] is True


def test_relationships_keep_endpoint_identity_and_distinguish_node_kind(db):
    rows = db.execute("MATCH (a:A)-[r:R]->(b:A) RETURN r AS x UNION "
                      "MATCH (a:A)-[r:R]->(b:A) RETURN r AS x UNION MATCH (n:A {id:1}) RETURN n AS x").rows
    assert len(rows) == 2
    edge, node = rows[0][0], rows[1][0]
    assert type(edge) is RelationshipValue and type(node) is NodeValue
    assert edge.source == node.identity
    assert edge.identity != node.identity
    assert edge.properties == {"value": 7}


def test_union_subquery_exports_entity_table_alternatives_for_later_property_access(db):
    assert db.execute("CALL () { MATCH (n:A) RETURN n UNION MATCH (n:B) RETURN n } "
                      "RETURN label(n) AS label,n.id AS id,n.value AS value ORDER BY label,id").rows == (
                          ("A", 1, 10), ("A", 2, 20), ("B", 1, 30))


def test_imported_entity_remains_bound_in_union_subquery_and_nullable_output(db):
    rows = db.execute("MATCH (n:A {id:1}) CALL (n) { RETURN n AS item UNION RETURN n AS item } "
                      "RETURN item,n").rows
    assert len(rows) == 1 and rows[0][0] == rows[0][1]
    assert db.execute("CALL () { MATCH (n:A {id:1}) RETURN n UNION RETURN null AS n } "
                      "RETURN n.id AS id").rows == ((1,), (None,))


def test_union_sees_private_updates_and_insertions_without_publishing_them(db):
    old = db.begin("read")
    try:
        with db.begin("write") as tx:
            tx.execute("MATCH (n:A {id:1}) SET n.value=99")
            created = tx.execute("CREATE (n:A {id:3,value:40}) RETURN n").rows[0][0]
            observed = tx.execute("MATCH (n:A) RETURN n UNION MATCH (n:A) RETURN n").rows
            assert len(observed) == 3
            assert {n.properties["id"]: n.properties["value"] for (n,) in observed} == {1: 99, 2: 20, 3: 40}
            assert next(n for (n,) in observed if n.properties["id"] == 3) == created
            assert old.execute("MATCH (n:A) RETURN n.id,n.value ORDER BY n.id").rows == ((1, 10), (2, 20))
        assert old.execute("MATCH (n:A) RETURN n.id,n.value ORDER BY n.id").rows == ((1, 10), (2, 20))
    finally:
        old.rollback()
