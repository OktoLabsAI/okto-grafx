"""Heterogeneous trails compared with a schema-independent small-graph oracle."""

from collections import Counter

import pytest

import okto_grafx
from okto_grafx.errors import GrafxPlanError, GrafxQueryBudgetExceeded, GrafxQueryCancelled

from . import test_fp3_polymorphic_hops as hop_fixture

graph = hop_fixture.graph


NODES = (("P", 1), ("P", 2), ("P", 3), ("Q", 1), ("Other", 1))
EDGES = (
    ("A", 1, ("P", 1), ("Q", 1)), ("B", 2, ("Q", 1), ("P", 2)),
    ("C", 3, ("P", 2), ("P", 2)), ("A", 4, ("P", 1), ("Q", 1)),
    ("D", 5, ("Other", 1), ("Q", 1)),
)


def oracle(direction, allowed, minimum, maximum, anchors=NODES):
    """Recursive exhaustive edge-subset enumeration, independent of Grafx plans."""
    result = []

    def visit(nodes, edges):
        if minimum <= len(edges) <= maximum:
            result.append((nodes, edges))
        if len(edges) == maximum:
            return
        for kind, weight, source, target in EDGES:
            key = (kind, weight)
            if key in edges or allowed is not None and kind not in allowed:
                continue
            choices = []
            if direction in ("out", "both") and nodes[-1] == source:
                choices.append(target)
            if direction in ("in", "both") and nodes[-1] == target and not (
                    direction == "both" and source == target):
                choices.append(source)
            for endpoint in choices:
                visit((*nodes, endpoint), (*edges, key))

    for anchor in anchors:
        visit((anchor,), ())
    return Counter(result)


def signature(path):
    assert type(path) is okto_grafx.PathValue
    return (tuple((node.label, node.properties["id"]) for node in path.nodes),
            tuple((edge.label, edge.properties["weight"]) for edge in path.relationships))


@pytest.mark.parametrize("direction", ["out", "in", "both"])
@pytest.mark.parametrize("types,allowed", [("", None), (":A|B", {"A", "B"}), (":A|Missing|A", {"A"})])
@pytest.mark.parametrize("minimum,maximum", [(0, 0), (0, 3), (1, 3), (2, 3)])
def test_ranges_equal_independent_exhaustive_oracle(graph, direction, types, allowed, minimum, maximum):
    middle = f"[r{types}*{minimum}..{maximum}]"
    hop = f"-{middle}->" if direction == "out" else f"<-{middle}-" if direction == "in" else f"-{middle}-"
    rows = graph.execute(f"MATCH p=(a){hop}(b) RETURN p,r,length(p),nodes(p),relationships(p)").rows
    assert Counter(signature(path) for path, *_ in rows) == oracle(direction, allowed, minimum, maximum)
    for path, edges, length, nodes, relationships in rows:
        assert path.relationships == edges == relationships
        assert path.nodes == nodes
        assert length == len(edges)
        assert len({edge.identity for edge in edges}) == length


def test_target_filter_does_not_prune_intermediate_node_tables(graph):
    rows = graph.execute("MATCH p=(a:P {id:1})-[:A|B*2..3]->(b:P {id:2}) RETURN p").rows
    assert len(rows) == 2
    assert all([n.label for n in p.nodes] == ["P", "Q", "P"] for (p,) in rows)
    assert graph.execute("MATCH (a:P {id:1}), (b:Other) MATCH (a)-[:A|B*0..3]->(b) RETURN a,b").rows == ()
    assert graph.execute("MATCH (a:P {id:1}) WITH a,null AS b OPTIONAL MATCH "
                         "p=(a)-[:A|B*0..3]->(b) RETURN b,p").rows == ((None, None),)


def test_zero_hop_aliases_optional_and_empty_alternatives(graph):
    assert graph.execute("MATCH p=(a:Other)-[:A|B*0..3]->(b) RETURN length(p),labels(b)").rows == ((0, ("Other",)),)
    assert graph.execute("MATCH p=(a:Other)-[:Missing|Absent*0..3]->(b) RETURN length(p),labels(b)").rows == ((0, ("Other",)),)
    assert graph.execute("MATCH (a:P {id:3}) OPTIONAL MATCH p=(a)-[*1..3]->(b) RETURN p,b").rows == ((None, None),)
    assert len(graph.execute("CALL () { MATCH p=(a:P {id:1})-[:A|B*0..3]->(b) RETURN p } "
                             "WITH p AS q RETURN q").rows) == 5


def test_path_prefix_and_clause_wide_uniqueness(graph):
    rows = graph.execute("MATCH p=(a:P {id:1})-[first:A]->(b)-[rest:A|B|C*0..3]-(c) RETURN p").rows
    assert rows
    assert all(len({edge.identity for edge in path.relationships}) == len(path.relationships) for (path,) in rows)
    assert all(path.relationships[0].label == "A" for (path,) in rows)
    assert graph.execute("MATCH (a:P {id:2})-[r:C*1..2]-(b), (a)-[s:C|B*1..2]-(b) "
                         "WHERE size(s)=1 RETURN r,s").rows == ()


def test_range_elements_have_native_polymorphic_properties(graph):
    rows = graph.execute("MATCH (a:P {id:1})-[r:A|B*2..3]->(b) "
                         "RETURN type(r[0]),type(r[1]),r[0].missing,properties(r[1])").rows
    assert rows == (("A", "B", None, {"weight": 2}),) * 2


@pytest.mark.parametrize("suffix", [
    "WITH r AS edges RETURN type(edges)",
    "CALL (r) { RETURN r AS edges } RETURN type(edges)",
    "CALL (r) { RETURN r AS edges UNION RETURN null AS edges } RETURN type(edges)",
])
def test_relationship_lists_never_become_single_edge_types_after_composition(graph, suffix):
    with pytest.raises(GrafxPlanError) as raised:
        graph.execute("MATCH (a:P {id:1})-[r:A|B*2..3]->(b) " + suffix)
    assert raised.value.details.get("reason") == "entity_function_argument_type"
    assert raised.value.details.get("query_phase") == "planning"


def test_relationship_lists_export_as_lists_with_native_elements(graph):
    rows = graph.execute("MATCH (a:P {id:1})-[r:A|B*2..3]->(b) "
                         "CALL (r) { RETURN r AS edges UNION RETURN null AS edges } "
                         "RETURN size(edges),edges").rows
    assert len(rows) == 4
    assert sum(edges is None for _, edges in rows) == 2
    for length, edges in rows:
        if edges is not None:
            assert length == len(edges) == 2
            assert all(type(edge) is okto_grafx.RelationshipValue for edge in edges)


def test_heterogeneous_range_plan_is_detached(graph):
    query = "MATCH p=(a:P {id:1})-[:A|B*0..3]->(b) RETURN p"
    first = graph.execute(query)
    plan = next(node for node in first.plan.walk() if node.label == "TraverseRelationshipAlternatives")
    assert plan.min_hops == 0 and plan.max_hops == 3
    assert {table.name for table in plan.tables} == {"A", "B"}
    assert any(node.label == "IndexSeek" for node in first.plan.walk())
    object.__setattr__(plan, "tables", ())
    assert graph.execute(query).rows == first.rows


def test_owner_overlay_snapshot_cancel_and_rollback(graph):
    query = "MATCH p=(a:P {id:1})-[:A|B|C*1..3]->(b) RETURN p"
    before = graph.execute(query).rows
    with graph.begin("read") as reader:
        assert reader.execute(query).rows == before
        writer = graph.begin("write")
        try:
            writer.execute("MATCH ()-[r:C]->() DELETE r")
            assert len(writer.execute(query).rows) < len(before)
            assert reader.execute(query).rows == before
        finally:
            writer.rollback()
        assert reader.execute(query).rows == before
    token = okto_grafx.CancellationToken()
    with graph.query(query).cursor(batch_size=1, cancellation=token) as cursor:
        assert cursor.fetchone()
        token.cancel()
        with pytest.raises(GrafxQueryCancelled):
            cursor.fetchone()
    assert graph.transactions.open_transactions == 0


def test_range_budget_is_cumulative_across_tables(graph):
    with okto_grafx.connect(graph.path, max_traversal_expansions=1) as reader:
        with pytest.raises(GrafxQueryBudgetExceeded):
            reader.execute("MATCH p=(a:P {id:1})-[:A|B*2..3]->(b) RETURN p")
        assert reader.transactions.open_transactions == 0


def test_late_failure_after_heterogeneous_walk_rolls_back(graph, monkeypatch):
    import okto_grafx.engine.query_engine as engine

    original = engine._write_assignments
    applied = []

    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        applied.append(True)
        return result

    monkeypatch.setattr(engine, "_write_assignments", observed)
    with graph.begin("write") as tx:
        tx.execute("CREATE (:Other {id:99})")
        with pytest.raises(GrafxPlanError):
            tx.execute("MATCH p=(a:P {id:1})-[:A|B*2..3]->(b) UNWIND [a,1] AS item "
                       "SET a.id=11 RETURN properties(item),p")
        assert applied
        assert tx.execute("MATCH (a:P) RETURN a.id ORDER BY a.id").rows == ((1,), (2,), (3,))
        assert tx.execute("MATCH (a:Other {id:99}) RETURN a.id").rows == ((99,),)


@pytest.fixture
def long_chain(tmp_path):
    with okto_grafx.connect(tmp_path / "long-chain") as db:
        with db.begin("write") as tx:
            for i in range(32):
                tx.execute(f"CREATE NODE TABLE N{i}(id INT64, PRIMARY KEY(id))")
                tx.execute(f"CREATE (:N{i} {{id:{i}}})")
            for i in range(31):
                tx.execute(f"CREATE REL TABLE E{i}(FROM N{i} TO N{i+1})")
                tx.execute(f"MATCH (a:N{i}), (b:N{i+1}) CREATE (a)-[:E{i}]->(b)")
        yield db


def chain_query(hops, suffix="RETURN length(p)"):
    names = "|".join(f"E{i}" for i in range(31))
    return f"MATCH p=(a:N0)-[:{names}*{hops}]->(b) {suffix}"


def test_omitted_bound_detects_type_first_reachable_at_probe_depth(long_chain):
    with pytest.raises(GrafxQueryBudgetExceeded) as raised:
        long_chain.execute(chain_query(""))
    assert raised.value.details["field"] == "max_traversal_hops"
    assert raised.value.details["limit"] == 30
    assert raised.value.details["observed"] == 31
    assert raised.value.details["operator"] == "TraverseRelationshipAlternatives"
    assert long_chain.transactions.open_transactions == 0


def test_explicit_bound_and_limited_consumption_do_not_claim_full_enumeration(long_chain):
    assert long_chain.execute(chain_query("0..30")).rows == tuple((i,) for i in range(31))
    limited = long_chain.execute(chain_query("", "RETURN length(p) LIMIT 1"))
    assert limited.rows == ((1,),)
    plan = next(node for node in limited.plan.walk() if node.label == "TraverseRelationshipAlternatives")
    assert plan.upper_bound_omitted
    assert len(plan.tables) == 31  # E30 exists only at the completeness probe depth.
    with pytest.raises(GrafxQueryBudgetExceeded):
        long_chain.execute(chain_query("", "RETURN length(p) ORDER BY length(p) LIMIT 1"))


def test_probe_respects_owner_deletion_and_independent_reader(long_chain):
    with long_chain.begin("read") as reader:
        tx = long_chain.begin("write")
        try:
            tx.execute("MATCH ()-[r:E30]->() DELETE r")
            assert len(tx.execute(chain_query("")).rows) == 30
            with pytest.raises(GrafxQueryBudgetExceeded):
                reader.execute(chain_query(""))
        finally:
            tx.rollback()
    with pytest.raises(GrafxQueryBudgetExceeded):
        long_chain.execute(chain_query(""))


def test_range_spill_owns_all_native_path_components(graph, monkeypatch):
    from okto_grafx.adapters.query_spill_local import _Sorter

    original = _Sorter._write_pair
    written = []

    def observed(stream, record):
        original(stream, record)
        written.append(len(record[1]))

    monkeypatch.setattr(_Sorter, "_write_pair", staticmethod(observed))
    with okto_grafx.connect(graph.path, query_memory_budget_bytes=8192) as db:
        rows = db.execute("UNWIND range(1,100) AS i MATCH p=(a:P {id:1})-[:A|B*0..3]->(b) "
                          "RETURN i,p ORDER BY i DESC").rows
        assert len(rows) == 500
        assert [i for i, _ in rows] == [i for i in range(100, 0, -1) for _ in range(5)]
        expected = oracle("out", {"A", "B"}, 0, 3, anchors=(("P", 1),))
        assert Counter(signature(path) for _, path in rows) == Counter({key: count * 100 for key, count in expected.items()})
        assert written and sum(written) > 8192
        assert db.transactions.open_transactions == 0
