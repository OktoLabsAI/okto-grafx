"""Independent trail oracle for native variable-length path capture."""

import pytest

import okto_grafx
from okto_grafx import PathValue
from okto_grafx.errors import GrafxError, GrafxPlanError, GrafxQueryBudgetExceeded


EDGES = ((11, 1, 2), (12, 1, 2), (23, 2, 3), (31, 3, 1), (22, 2, 2))


@pytest.fixture(params=[None, 8192], ids=["memory", "spill"])
def graph(tmp_path, request):
    with okto_grafx.connect(tmp_path / "trails", query_memory_budget_bytes=request.param) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE P(id INT64, mark INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM P TO P, id INT64)")
            for identifier in (1, 2, 3, 4):
                tx.execute("CREATE (:P {id:$id})", {"id": identifier})
            for identifier, source, target in EDGES:
                tx.execute("MATCH (a:P {id:$a}),(b:P {id:$b}) CREATE (a)-[:E {id:$id}]->(b)",
                           {"a": source, "b": target, "id": identifier})
        yield db


def oracle(start, minimum, maximum, direction, records=EDGES):
    def walk(nodes, edges):
        if minimum <= len(edges) <= maximum:
            yield nodes, edges
        if len(edges) == maximum:
            return
        for identifier, source, target in records:
            if identifier in edges:
                continue
            destinations = set()
            if direction in ("out", "both") and source == nodes[-1]:
                destinations.add(target)
            if direction in ("in", "both") and target == nodes[-1]:
                destinations.add(source)
            for destination in destinations:
                yield from walk((*nodes, destination), (*edges, identifier))
    return sorted(walk((start,), ()))


def observation(path):
    assert type(path) is PathValue
    return tuple(n.properties["id"] for n in path.nodes), tuple(r.properties["id"] for r in path.relationships)


@pytest.mark.parametrize("direction,edge", [("out", "-[r:E*0..3]->"), ("in", "<-[r:E*0..3]-"), ("both", "-[r:E*0..3]-")])
def test_named_ranges_match_independent_trail_oracle(graph, direction, edge):
    rows = graph.execute(f"MATCH p=(a:P {{id:1}}){edge}(b:P) RETURN p,length(p),r ORDER BY length(p)").rows
    assert sorted(observation(row[0]) for row in rows) == oracle(1, 0, 3, direction)
    for path, length, relationships in rows:
        assert length == len(path.relationships)
        assert relationships == path.relationships


def test_exact_one_written_range_returns_relationship_list(graph):
    rows = graph.execute("MATCH p=(a:P {id:1})-[r:E*1..1]->(b:P) RETURN p,r").rows
    assert len(rows) == 2
    assert all(relationships == path.relationships for path, relationships in rows)


def test_zero_length_binds_anchor_and_empty_edge_list_including_isolated_nodes(graph):
    rows = graph.execute("MATCH p=(a:P)-[r:E*0]->(b:P) RETURN a.id,b.id,p,r ORDER BY a.id").rows
    assert len(rows) == 4
    for identifier, target, path, relationships in rows:
        assert target == identifier
        assert observation(path) == ((identifier,), ())
        assert relationships == ()


def test_named_segments_concatenate_without_repeating_junction_node(graph):
    rows = graph.execute("MATCH p=(a:P {id:1})-[:E]->(b:P)-[:E*0..2]->(c:P) RETURN p").rows
    assert sorted(observation(row[0]) for row in rows) == oracle(1, 1, 3, "out")


def test_zero_segment_followed_by_real_segment(graph):
    rows = graph.execute("MATCH p=(a:P {id:1})-[:E*0]->(b:P)-[:E]->(c:P) RETURN p").rows
    assert sorted(observation(row[0]) for row in rows) == oracle(1, 1, 1, "out")


def test_bound_target_filters_zero_length_and_cycles(graph):
    rows = graph.execute("MATCH (a:P {id:1}) MATCH p=(a)-[:E*0..3]->(a) RETURN p").rows
    assert sorted(observation(row[0]) for row in rows) == [item for item in oracle(1, 0, 3, "out") if item[0][-1] == 1]


def test_optional_impossible_range_null_extends_whole_path(graph):
    assert graph.execute("MATCH (a:P {id:4}) OPTIONAL MATCH p=(a)-[r:E*1..3]->(b:P) "
                         "RETURN p,r,b").rows == ((None, None, None),)


def test_zero_to_one_cross_table_path_keeps_qualified_target_identity(graph):
    with graph.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Q(id INT64, PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE X(FROM P TO Q)")
        tx.execute("MATCH (a:P {id:1}) CREATE (a)-[:X]->(:Q {id:1})")
    rows = graph.execute("MATCH p=(a:P {id:1})-[:X*0..1]->(b) RETURN p,b ORDER BY length(p)").rows
    assert len(rows) == 2
    assert [row[1].label for row in rows] == ["P", "Q"]
    assert all(row[0].nodes[-1] == row[1] for row in rows)


def test_scan_driven_undirected_paths_count_self_loops_once(graph):
    rows = graph.execute("MATCH p=(a:P)-[:E*0..2]-(b:P) RETURN p").rows
    expected = sorted(item for identifier in (1, 2, 3, 4) for item in oracle(identifier, 0, 2, "both"))
    assert sorted(observation(row[0]) for row in rows) == expected


def test_pending_self_loop_and_rollback_preserve_independent_reader(graph):
    query = "MATCH p=(a:P {id:1})-[:E*0..2]-(b:P) RETURN p"
    expected = oracle(1, 0, 2, "both")
    tx = graph.begin("write")
    try:
        tx.execute("MATCH (a:P {id:1}) CREATE (a)-[:E {id:99}]->(a)")
        own = tx.execute(query).rows
        assert sorted(observation(row[0]) for row in own) == oracle(1, 0, 2, "both", (*EDGES, (99, 1, 1)))
        assert sorted(observation(row[0]) for row in graph.execute(query).rows) == expected
        pending = [edge for row in own for edge in row[0].relationships if edge.properties["id"] == 99]
        assert pending and all(edge.provenance.pending for edge in pending)
    finally:
        tx.rollback()
    assert sorted(observation(row[0]) for row in graph.execute(query).rows) == expected


def test_variable_path_cursor_keeps_snapshot_across_writer_commit_and_closes(graph):
    query = "MATCH p=(a:P {id:1})-[:E*0..3]->(b:P) RETURN p"
    with graph.query(query).cursor(batch_size=1) as cursor:
        first = cursor.fetchone()
        with graph.begin("write") as tx:
            tx.execute("MATCH (a:P)-[r:E]->(b:P) WHERE r.id=11 DELETE r")
        observed = [first, *cursor]
        assert sorted(observation(row[0]) for row in observed) == oracle(1, 0, 3, "out")
    assert graph.transactions.open_transactions == 0
    assert sorted(observation(row[0]) for row in graph.execute(query).rows) == oracle(
        1, 0, 3, "out", tuple(edge for edge in EDGES if edge[0] != 11))


def test_cursor_close_does_not_expand_unconsumed_branches(graph, monkeypatch):
    import okto_grafx.engine.query_engine as engine

    original = engine._edge_steps
    started = 0
    closed = 0

    def instrumented(*args, **kwargs):
        access = original(*args, **kwargs)

        def steps(identity):
            nonlocal started, closed
            started += 1
            try:
                yield from access(identity)
            finally:
                closed += 1
        return steps

    monkeypatch.setattr(engine, "_edge_steps", instrumented)
    with graph.query("MATCH p=(a:P {id:1})-[:E*0..3]->(b:P) RETURN p").cursor(batch_size=1) as cursor:
        assert len(cursor.fetchone()[0]) == 0
        assert started == 0
    assert closed == started == 0
    with graph.query("MATCH p=(a:P {id:1})-[:E*1..3]->(b:P) RETURN p").cursor(batch_size=1) as cursor:
        assert len(cursor.fetchone()[0]) == 1
        assert started == 1
    assert closed == started == 1
    assert graph.transactions.open_transactions == 0


def test_zero_length_is_valid_outside_relationship_endpoint_tables(graph):
    with graph.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Q(id INT64, PRIMARY KEY(id))")
        tx.execute("CREATE (:Q {id:9})")
    rows = graph.execute("MATCH p=(a:Q)-[:E*0..1]->(b) RETURN p,b").rows
    assert len(rows) == 1
    assert rows[0][1].label == "Q" and len(rows[0][0]) == 0
    rows = graph.execute("MATCH p=(a)-[:E*0]->(b) RETURN p,b").rows
    assert sorted((row[1].label, row[1].properties["id"]) for row in rows) == [
        ("P", 1), ("P", 2), ("P", 3), ("P", 4), ("Q", 9),
    ]
    assert all(len(row[0]) == 0 and row[0].nodes[0] == row[1] for row in rows)


def test_zero_path_is_charged_against_configured_path_quota(tmp_path):
    with okto_grafx.connect(tmp_path / "quota", max_traversal_paths=1) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM P TO P)")
            tx.execute("CREATE (:P {id:1})-[:E]->(:P {id:2})")
        query = "MATCH p=(a:P {id:1})-[:E*0..1]->(b:P) RETURN p"
        assert len(db.execute(query + " LIMIT 1").rows[0][0]) == 0
        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            db.execute(query)
        assert raised.value.details["field"] == "max_traversal_paths"
        assert db.transactions.open_transactions == 0


def test_late_failure_after_range_driven_writes_rolls_back_whole_statement(graph, monkeypatch):
    import okto_grafx.engine.query_engine as engine

    original = engine._write_assignments
    applied = []

    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        applied.append(True)
        return result

    monkeypatch.setattr(engine, "_write_assignments", observed)
    with graph.begin("write") as tx:
        tx.execute("CREATE (:P {id:99})")
        with pytest.raises(GrafxError):
            tx.execute("MATCH p=(a:P {id:1})-[:E*0..3]->(b:P) "
                       "SET a.mark=length(p) RETURN 10/(b.id-2)")
        assert applied, "A planning refusal is not evidence of rollback after writes."
        assert tx.execute("MATCH (a:P {id:1}) RETURN a.mark").rows == ((None,),)
        assert tx.execute("MATCH (a:P {id:99}) RETURN a.id").rows == ((99,),)
    assert graph.execute("MATCH (a:P {id:1}) RETURN a.mark").rows == ((None,),)
    assert graph.execute("MATCH (a:P {id:99}) RETURN a.id").rows == ((99,),)


@pytest.mark.parametrize("bounds", ["*0", "*1..1", "*0..3"])
def test_written_ranges_are_not_admitted_as_relationship_creation(graph, bounds):
    with pytest.raises(GrafxPlanError) as raised:
        graph.explain(f"MATCH (a:P {{id:1}}) CREATE (a)-[:E{bounds}]->(a)")
    assert raised.value.details["field"] == "hops"


@pytest.mark.parametrize("query", [
    "WITH null AS a OPTIONAL MATCH p=(a)-[:E*0..3]->(b:P) RETURN p,b",
    "WITH null AS a WITH a AS c OPTIONAL MATCH p=(c)-[:E*0..3]->(b:P) RETURN p,b",
    "CALL () { RETURN null AS a } OPTIONAL MATCH p=(a)-[:E*0..3]->(b:P) RETURN p,b",
    "WITH null AS a CALL (a) { OPTIONAL MATCH p=(a)-[:E*0..3]->(b:P) RETURN p,b } RETURN p,b",
])
def test_null_anchor_does_not_become_a_zero_length_path(graph, query):
    assert graph.execute(query).rows == ((None, None),)
