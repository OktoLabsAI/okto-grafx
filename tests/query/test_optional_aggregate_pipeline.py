"""Ordered optional incident reads and native aggregate WITH stages."""
from dataclasses import replace

import pytest
import okto_grafx
from okto_grafx.domain.query import parse, analyze
from okto_grafx.errors import GrafxPlanError, GrafxQueryBudgetExceeded


QUERY = (
    "MATCH (n:Item {id: $nid}) "
    "OPTIONAL MATCH (n)-[r_out]->() "
    "WITH n, COUNT(r_out) AS out_deg "
    "OPTIONAL MATCH (n)<-[r_in]-() "
    "WITH n, out_deg, COUNT(r_in) AS in_deg "
    "OPTIONAL MATCH (n)<-[c:Challenges]-() "
    "RETURN n.id, out_deg, in_deg, "
    "CASE WHEN COUNT(c) = 0 THEN 0.0 "
    "ELSE SUM(COALESCE(c.confidence, $default_conf)) END"
)


@pytest.fixture(params=[None, 4096], ids=["memory", "spill"])
def graph(tmp_path, request):
    with okto_grafx.connect(tmp_path / "graph", query_memory_budget_bytes=request.param) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Item(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE Other(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE Links(FROM Item TO Item)")
            tx.execute("CREATE REL TABLE Challenges(FROM Item TO Item, confidence DOUBLE)")
            for key in ("a", "b", "isolated"):
                tx.execute("CREATE (:Item {id:$id})", {"id": key})
            tx.execute("CREATE (:Other {id:'other'})")
            for _ in range(3):
                tx.execute("MATCH (a:Item {id:'a'}), (b:Item {id:'b'}) CREATE (a)-[:Links]->(b)")
            for confidence in (0.25, None):
                tx.execute("MATCH (a:Item {id:'a'}), (b:Item {id:'b'}) "
                           "CREATE (b)-[:Challenges {confidence:$confidence}]->(a)",
                           {"confidence": confidence})
        yield db


def test_order_survives_parse_describe_and_round_trip():
    query = parse(QUERY)
    assert query.read_clause_order == ("match", "match", "with", "match", "with", "match")
    assert parse(query.describe()) == query
    analyze(query)


@pytest.mark.parametrize("key, expected", [
    ("a", (("a", 3, 2, 0.75),)),
    ("b", (("b", 2, 3, 0.0),)),
    ("isolated", (("isolated", 0, 0, 0.0),)),
    ("missing", ()),
])
def test_degrees_do_not_multiply_and_missing_edges_null_extend(graph, key, expected):
    assert graph.execute(QUERY, {"nid": key, "default_conf": 0.5}).rows == expected


def test_typed_relationship_without_compatible_endpoint_is_empty_optional(graph):
    assert graph.execute(QUERY.replace("n:Item", "n:Other"),
                         {"nid": "other", "default_conf": 0.5}).rows == (("other", 0, 0, 0.0),)


def test_aggregate_with_scope_filter_and_empty_global_group(graph):
    assert graph.execute("MATCH (n:Item) WITH count(n) AS total WHERE total > 1 RETURN total").rows == ((3,),)
    assert graph.execute("MATCH (n:Item {id:'missing'}) WITH count(n) AS total RETURN total").rows == ((0,),)
    with pytest.raises(GrafxPlanError):
        graph.execute("MATCH (n:Item) WITH count(n) AS total RETURN n.id")


def test_aggregate_with_retains_grouped_entity_properties(graph):
    assert graph.execute(
        "MATCH (n:Item) OPTIONAL MATCH (n)-[edge]->() "
        "WITH n, count(edge) AS degree WHERE degree > 0 "
        "RETURN n.id, degree ORDER BY n.id"
    ).rows == (("a", 3), ("b", 2))


def test_nested_aggregates_are_rejected_before_execution(graph):
    with pytest.raises(GrafxPlanError):
        graph.execute("MATCH (n:Item) WITH count(sum(n.id)) AS invalid RETURN invalid")


def test_incident_pipeline_respects_row_budget_and_releases_reader(graph):
    with okto_grafx.connect(graph.path, max_intermediate_rows=2) as bounded:
        with pytest.raises(GrafxQueryBudgetExceeded):
            bounded.execute(QUERY, {"nid": "a", "default_conf": 0.5})
        assert bounded.execute("MATCH (n:Item {id:'a'}) RETURN n.id").rows == (("a",),)


@pytest.mark.parametrize("budget", [None, 32768])
def test_complete_scoring_projection_is_native_and_schema_generic(tmp_path, budget):
    properties = (
        "source_confidence DOUBLE, query_hits INT64, last_queried_at TIMESTAMP, "
        "relevance_score DOUBLE, priority_boost DOUBLE, attestation_count INT64, "
        "revocation_reason STRING, pre_cancellation_relevance_score DOUBLE"
    )
    projection = (
        "RETURN n.source_confidence, out_deg, in_deg, "
        "n.query_hits, n.last_queried_at, n.relevance_score, "
        "CASE WHEN COUNT(c) = 0 THEN 0.0 "
        "ELSE SUM(COALESCE(c.confidence, $default_conf)) END, "
        "n.priority_boost, n.attestation_count, n.revocation_reason, "
        "n.pre_cancellation_relevance_score"
    )
    with okto_grafx.connect(tmp_path / "full", query_memory_budget_bytes=budget) as db:
        with db.begin("write") as tx:
            tx.execute(f"CREATE NODE TABLE Item(id STRING, {properties}, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE Challenges(FROM Item TO Item, confidence DOUBLE)")
            tx.execute("CREATE (:Item {id:'item'})")
        query = QUERY.split("RETURN", 1)[0] + projection
        assert db.execute(query, {"nid": "item", "default_conf": 0.5}).rows == (
            (None, 0, 0, None, None, None, 0.0, None, None, None, None),
        )


def test_pipeline_preserves_snapshot_and_owner_overlay(graph):
    params = {"nid": "a", "default_conf": 0.5}
    with graph.begin("read") as reader:
        assert reader.execute(QUERY, params).rows == (("a", 3, 2, 0.75),)
        with graph.begin("write") as writer:
            writer.execute("MATCH (a:Item {id:'a'}), (b:Item {id:'b'}) CREATE (a)-[:Links]->(b)")
            assert writer.execute(QUERY, params).rows == (("a", 4, 2, 0.75),)
        assert reader.execute(QUERY, params).rows == (("a", 3, 2, 0.75),)
    assert graph.execute(QUERY, params).rows == (("a", 4, 2, 0.75),)


@pytest.mark.parametrize("order", [("with", "match"), ("wrong",), [], ("match",)])
def test_forged_reading_order_is_refused(order):
    with pytest.raises(GrafxPlanError):
        analyze(replace(parse(QUERY), read_clause_order=order))


def test_dropped_anchor_and_reused_edge_names_are_refused():
    with pytest.raises(GrafxPlanError):
        analyze(parse(QUERY.replace("WITH n, COUNT", "WITH COUNT", 1)))
    with pytest.raises(GrafxPlanError):
        analyze(parse(QUERY.replace("r_in", "r_out")))


def test_vector_search_does_not_cross_new_optional_projection_barriers():
    query = QUERY.replace(
        "OPTIONAL MATCH", "WHERE similarity(n.embedding, $vector, space => 'space') > 0.5 OPTIONAL MATCH", 1
    )
    with pytest.raises(GrafxPlanError, match="projection barriers"):
        analyze(parse(query))
