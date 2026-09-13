"""Missing schema in a read is no match, not a fabricated persistent table."""

import pytest

import okto_grafx
from okto_grafx.errors import GrafxPlanError, GrafxQueryBudgetExceeded, GrafxQueryCancelled


@pytest.fixture
def graph(tmp_path):
    with okto_grafx.connect(tmp_path / "missing") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE Q(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM P TO P)")
            tx.execute("CREATE (:P {id:1}), (:P {id:2}), (:Q {id:1})")
            tx.execute("MATCH (a:P {id:1}), (b:P {id:2}) CREATE (a)-[:E]->(b)")
        yield db


def test_original_graph9_null_query_on_empty_schema(tmp_path):
    with okto_grafx.connect(tmp_path / "empty") as db:
        query = ("OPTIONAL MATCH (n:DoesNotExist) OPTIONAL MATCH (n)-[r:NOT_THERE]->() "
                 "RETURN properties(n), properties(r), properties(null)")
        assert db.execute(query).rows == ((None, None, None),)
        with pytest.raises(GrafxPlanError) as raised:
            db.execute("MATCH (r) RETURN type(r)")
        assert raised.value.details["reason"] == "entity_function_argument_type"


@pytest.mark.parametrize("pattern", [
    "(n:Missing)", "(a:P)-[r:Missing]->(b:P)",
    "(a:Missing)-[r:E]->(b:P)", "(a:P)-[r:E]->(b:Missing)",
])
def test_positive_absent_patterns_empty_and_aggregate_zero(graph, pattern):
    assert graph.execute(f"MATCH {pattern} RETURN 1").rows == ()
    assert graph.execute(f"MATCH {pattern} RETURN count(*)").rows == ((0,),)


@pytest.mark.parametrize("suffix", [
    "OPTIONAL MATCH (a)-[r:Missing]->(b:P) RETURN a.id,b,r ORDER BY a.id",
    "OPTIONAL MATCH (a)-[r:E]->(b:Missing) RETURN a.id,b,r ORDER BY a.id",
])
def test_optional_does_not_null_existing_anchor(graph, suffix):
    assert graph.execute("MATCH (a:P) " + suffix).rows == ((1, None, None), (2, None, None))


@pytest.mark.parametrize("direction", ["-[r:Missing*0..]->", "<-[r:Missing*0..]-", "-[r:Missing*0..]-"])
def test_absent_relationship_zero_range_captures_native_path(graph, direction):
    rows = graph.execute(f"MATCH p=(a:P){direction}(b:P) "
                         "RETURN a,b,r,p,length(p) ORDER BY a.id").rows
    assert len(rows) == 2
    for a, b, edges, path, length in rows:
        assert a == b
        assert edges == ()
        assert type(path) is okto_grafx.PathValue
        assert path.nodes == (a,)
        assert path.relationships == ()
        assert length == 0


def test_zero_hop_uses_qualified_identity_and_bound_target(graph):
    assert graph.execute("MATCH (a:P), (b:Q) MATCH (a)-[:Missing*0..]->(b) RETURN a,b").rows == ()
    assert graph.execute("MATCH (a:P), (b:P) MATCH (a)-[:Missing*0..]->(b) "
                         "RETURN a.id,b.id ORDER BY a.id").rows == ((1, 1), (2, 2))
    assert graph.execute("MATCH (a:P) WITH a,null AS b OPTIONAL MATCH "
                         "p=(a)-[:Missing*0..]->(b) RETURN a.id,b,p ORDER BY a.id").rows == (
                             (1, None, None), (2, None, None))


def test_zero_hop_composes_with_prefix_and_polymorphic_anchor(graph):
    rows = graph.execute("MATCH p=(a:P {id:1})-[:E]->(b:P)-[:Missing*0..]->(c:P) "
                         "RETURN p,b,c,length(p)").rows
    assert len(rows) == 1
    p, b, c, length = rows[0]
    assert b == c == p.nodes[-1]
    assert length == 1
    assert len(p.nodes) == 2
    rows = graph.execute("MATCH p=(a)-[:Missing*0..]->(b) RETURN a,b,p").rows
    assert len(rows) == 3
    assert all(a == b == p.nodes[0] for a, b, p in rows)


def test_missing_table_plan_is_invalidated_after_ddl(graph):
    query = "OPTIONAL MATCH (n:Missing) RETURN n.id"
    assert graph.execute(query).rows == ((None,),)
    with graph.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Missing(id INT64, PRIMARY KEY(id))")
        tx.execute("CREATE (:Missing {id:7})")
    assert graph.execute(query).rows == ((7,),)


def test_impossible_pattern_consumes_prior_statement_writes(graph):
    with graph.begin("write") as tx:
        assert tx.execute("MATCH (a:P) SET a.id=a.id+10 WITH a "
                          "MATCH (b:Missing) RETURN b").rows == ()
    assert graph.execute("MATCH (a:P) RETURN a.id ORDER BY a.id").rows == ((11,), (12,))


@pytest.mark.parametrize("query", [
    "MATCH (n:Missing) RETURN not_defined",
    "MATCH (n:Missing) RETURN labels(1)",
    "WITH 1 AS n MATCH (n:Missing) RETURN n",
    "WITH 1 AS n OPTIONAL MATCH (n:Missing) RETURN n",
    "MATCH (a:P) WITH a,1 AS b MATCH (a)-[:Missing*0..]->(b) RETURN b",
])
def test_absent_reads_do_not_waive_compile_errors(graph, query):
    with pytest.raises(GrafxPlanError):
        graph.execute(query)


def test_multiple_missing_labels_are_a_valid_empty_conjunction_not_a_compile_error(graph):
    before = graph.catalog.catalog.tables()
    assert graph.execute("MATCH (n:Missing:Other) RETURN n").rows == ()
    assert graph.execute("OPTIONAL MATCH (n:Missing:Other) RETURN n").rows == ((None,),)
    assert graph.catalog.catalog.tables() == before


@pytest.mark.parametrize("query", [
    "MATCH (n:Missing), (a:E) RETURN a",
    "MATCH (:E)-[:Missing]->(:P) RETURN 1",
    "MATCH (:Missing)-[:P]->(:P) RETURN 1",
])
def test_other_kind_name_does_not_populate_an_absent_namespace(graph, query):
    assert graph.execute(query).rows == ()


def test_implicit_label_creation_is_still_refused_by_the_read_transaction(graph):
    from okto_grafx.errors import GrafxTransactionStateError

    # The model is now valid, but the read door must reject the write plan before
    # its lazy schema creation, rather than rejecting the label during planning.
    with pytest.raises(GrafxTransactionStateError) as failure:
        graph.execute("CREATE (:Missing {id:1})")
    assert failure.value.code == "transaction_state"
    assert not graph.catalog.catalog.has_table("Missing")


def test_optional_missing_entities_cross_alias_and_subquery(graph):
    assert graph.execute("OPTIONAL MATCH (n:Missing) WITH n AS m "
                         "RETURN properties(m),labels(m),m.id").rows == ((None, None, None),)
    assert graph.execute("MATCH (a:P) CALL (a) { OPTIONAL MATCH (a)-[r:Missing]->(b:P) "
                         "RETURN r,b } RETURN a.id,r,b ORDER BY a.id").rows == (
                             (1, None, None), (2, None, None))


@pytest.mark.parametrize("query", [
    "MATCH (n:Missing) RETURN type(n)",
    "MATCH (n) RETURN type(n)",
    "OPTIONAL MATCH (n:Missing) WITH n AS a RETURN type(a)",
    "MATCH (a:P)-[r:Missing]->(b:P) RETURN labels(r)",
    "OPTIONAL MATCH (a:P)-[r:Missing]->(b:P) WITH r AS e RETURN labels(e)",
    "OPTIONAL MATCH (a:P)-[r:Missing]->(b:P) CALL (r) { RETURN r AS e } RETURN labels(e)",
    "CALL () { OPTIONAL MATCH (n:Missing) RETURN n UNION RETURN null AS n } RETURN type(n)",
    "CALL () { OPTIONAL MATCH (n:Missing) RETURN n UNION OPTIONAL MATCH (m:Absent) RETURN m AS n } RETURN type(n)",
])
def test_empty_candidate_tables_do_not_erase_entity_kind(graph, query):
    with pytest.raises(GrafxPlanError) as raised:
        graph.execute(query)
    assert raised.value.details.get("reason") == "entity_function_argument_type"


def test_zero_hop_target_predicate_is_applied(graph):
    assert graph.execute("MATCH (a:P)-[:Missing*0..]->(b:P {id:2}) RETURN a.id,b.id").rows == ((2, 2),)
    assert graph.execute("MATCH (a:P)-[:Missing*0..]->(b:Missing) RETURN a.id").rows == ()
    assert graph.execute("MATCH (a:P)-[:Missing*0..]->(b:Q) RETURN a.id").rows == ()


def test_zero_hop_plan_detached_and_index_anchor_retained(graph):
    query = "MATCH p=(a:P {id:1})-[:Missing*0..]->(b:P) RETURN p"
    first = graph.execute(query)
    second = graph.execute(query)
    one = next(node for node in first.plan.walk() if node.label == "ZeroHopRelationship")
    two = next(node for node in second.plan.walk() if node.label == "ZeroHopRelationship")
    assert one is not two
    assert any(node.label == "IndexSeek" for node in first.plan.walk())
    object.__setattr__(one, "target_bound", "false")
    assert graph.execute(query).rows == second.rows


@pytest.mark.parametrize("tail", [
    "OPTIONAL MATCH (n)-[r:E]->(m:P) RETURN n,r,m",
    "OPTIONAL MATCH p=(n)-[r:Missing*0..]->(m:P) RETURN n,r,p",
    "CALL (n) { OPTIONAL MATCH (n)-[r:E]->(m:P) RETURN r,m } RETURN n,r,m",
])
def test_null_anchor_from_missing_table_is_never_rebound(graph, tail):
    assert graph.execute("OPTIONAL MATCH (n:Missing) " + tail).rows == ((None, None, None),)


def test_absent_relationship_plan_recompiles_after_schema_creation(graph):
    query = "MATCH p=(a:P)-[:Missing*0..1]->(b:P) RETURN length(p)"
    assert graph.execute(query).rows == ((0,), (0,))
    with graph.begin("write") as tx:
        tx.execute("CREATE REL TABLE Missing(FROM P TO P)")
        tx.execute("MATCH (a:P {id:1}), (b:P {id:2}) CREATE (a)-[:Missing]->(b)")
    assert sorted(graph.execute(query).rows) == [(0,), (0,), (1,)]


def test_zero_hop_cursor_snapshot_and_cancellation(graph):
    token = okto_grafx.CancellationToken()
    with graph.query("MATCH p=(a:P)-[:Missing*0..]->(b:P) RETURN p ORDER BY a.id").cursor(
            batch_size=1, cancellation=token) as cursor:
        assert cursor.fetchone()[0].nodes[0].properties["id"] == 1
        with graph.begin("write") as tx:
            tx.execute("MATCH (n:P {id:2}) SET n.id=12")
        assert cursor.fetchone()[0].nodes[0].properties["id"] == 2
        token.cancel()
        with pytest.raises(GrafxQueryCancelled):
            cursor.fetchone()
    assert graph.transactions.open_transactions == 0


def test_zero_hop_budget_and_limit_do_not_expand_edges(tmp_path):
    with okto_grafx.connect(tmp_path / "budget", max_traversal_paths=1) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:P {id:1}), (:P {id:2})")
        result = db.execute("MATCH p=(a:P)-[:Missing*0..]->(b:P) RETURN p LIMIT 1")
        assert len(result.rows) == 1
        assert result.statistics.get("traversal_expansions", 0) == 0
        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            db.execute("MATCH p=(a:P)-[:Missing*0..]->(b:P) RETURN p")
        assert raised.value.details["field"] == "max_traversal_paths"
        assert db.transactions.open_transactions == 0


def test_late_failure_after_zero_hop_rolls_back_observed_writes(graph, monkeypatch):
    import okto_grafx.engine.query_engine as engine

    original = engine._write_assignments
    applied = []

    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        applied.append(True)
        return result

    monkeypatch.setattr(engine, "_write_assignments", observed)
    with graph.begin("write") as tx:
        tx.execute("CREATE (:Q {id:99})")
        with pytest.raises(GrafxPlanError):
            tx.execute("MATCH p=(a:P {id:1})-[:Missing*0..]->(b:P) "
                       "UNWIND [a,1] AS item SET a.id=11 RETURN properties(item)")
        assert applied
        assert tx.execute("MATCH (a:P) RETURN a.id ORDER BY a.id").rows == ((1,), (2,))
        assert tx.execute("MATCH (a:Q {id:99}) RETURN a.id").rows == ((99,),)
