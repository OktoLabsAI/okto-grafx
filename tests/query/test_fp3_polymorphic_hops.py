"""One-hop relationship alternatives use generic bindings, never a Pulse template."""

import pytest

import okto_grafx
from okto_grafx.errors import GrafxPlanError, GrafxQueryBudgetExceeded, GrafxQueryCancelled


@pytest.fixture(params=[None, 8192], ids=["memory", "spill-budget"])
def graph(tmp_path, request):
    with okto_grafx.connect(tmp_path / "hops", query_memory_budget_bytes=request.param) as db:
        with db.begin("write") as tx:
            for name in ("P", "Q", "Other"):
                tx.execute(f"CREATE NODE TABLE {name}(id INT64, PRIMARY KEY(id))")
            for name, source, target in (("A", "P", "Q"), ("B", "Q", "P"), ("C", "P", "P"), ("D", "Other", "Q")):
                tx.execute(f"CREATE REL TABLE {name}(FROM {source} TO {target}, weight INT64)")
            tx.execute("CREATE (:P {id:1}), (:P {id:2}), (:P {id:3}), (:Q {id:1}), (:Other {id:1})")
            for name, source, a, target, b, weight in (
                    ("A", "P", 1, "Q", 1, 1), ("B", "Q", 1, "P", 2, 2),
                    ("C", "P", 2, "P", 2, 3), ("A", "P", 1, "Q", 1, 4), ("D", "Other", 1, "Q", 1, 5)):
                tx.execute(f"MATCH (a:{source} {{id:$a}}), (b:{target} {{id:$b}}) "
                           f"CREATE (a)-[:{name} {{weight:$w}}]->(b)", {"a": a, "b": b, "w": weight})
        yield db


@pytest.mark.parametrize("hop,count", [("-[r]->", 5), ("<-[r]-", 5), ("-[r]-", 9)])
def test_untyped_all_tables_qualified_endpoints_and_multiplicity(graph, hop, count):
    rows = graph.execute(f"MATCH p=(a){hop}(b) RETURN p,a,r,b,type(r),labels(a),properties(r)").rows
    assert len(rows) == count
    assert len({r.identity for _, _, r, *_ in rows}) == 5
    for path, a, r, b, kind, labels, props in rows:
        assert path.nodes == (a, b)
        assert path.relationships == (r,)
        assert (a.identity, b.identity) in ((r.source, r.target), (r.target, r.source))
        assert r.label == kind
        assert labels == (a.label,)
        assert props == dict(r.properties)


def test_alternatives_deduplicate_types_not_parallel_edges(graph):
    rows = graph.execute("MATCH (a)-[r:A|B|A|Missing]->(b) RETURN type(r),r.weight ORDER BY r.weight").rows
    assert rows == (("A", 1), ("B", 2), ("A", 4))
    assert graph.execute("OPTIONAL MATCH (a)-[r:Missing|Absent]->(b) RETURN a,r,b").rows == ((None, None, None),)
    assert graph.execute("MATCH (a)-[r:A|B*1..1]->(b) RETURN size(r)").rows == ((1,), (1,), (1,))


def test_bound_targets_labels_predicates_and_null_anchors(graph):
    assert graph.execute("MATCH (a:P), (b:Q) MATCH (a)-[r]->(b) RETURN a.id,b.id ORDER BY a.id").rows == ((1, 1), (1, 1))
    assert graph.execute("MATCH (a)-[r]->(b:P {id:2}) RETURN type(r) ORDER BY type(r)").rows == (("B",), ("C",))
    assert graph.execute("MATCH (a:P) WITH a,null AS b OPTIONAL MATCH (a)-[r]->(b) "
                         "RETURN a.id,r,b ORDER BY a.id").rows == ((1, None, None), (2, None, None), (3, None, None))
    assert graph.execute("OPTIONAL MATCH (a:Missing) OPTIONAL MATCH (a)-[r]->(b) RETURN a,r,b").rows == ((None, None, None),)


def test_optional_complete_clause_preserves_outer_input(graph):
    rows = graph.execute("UNWIND [1,3] AS id MATCH (a:P {id:id}) "
                         "OPTIONAL MATCH p=(a)-[r:A|B]->(b) WHERE r.weight>2 "
                         "RETURN a.id,r.weight,b.id,length(p) ORDER BY a.id").rows
    assert rows == ((1, 4, 1, 1), (3, None, None, None))
    assert graph.execute("MATCH (a:P {id:1}) OPTIONAL MATCH (a)-[r]->(b), (b)-[s:D]->(c) "
                         "RETURN a.id,r,b,s,c").rows == ((1, None, None, None, None),)


def test_named_segments_and_clause_wide_trail_identity(graph):
    rows = graph.execute("MATCH p=(a)-[r:A|D]->(b)-[s:B]->(c) RETURN p").rows
    assert len(rows) == 3
    assert all(len(p.relationships) == 2 and len(p.nodes) == 3 for (p,) in rows)
    assert graph.execute("MATCH p=(a:P {id:2})-[r]-()-[s]-(a) RETURN p").rows == ()
    assert graph.execute("MATCH (a:P {id:2})-[r]-(b), (a)-[s]-(b) "
                         "WHERE type(r)='C' AND type(s)='C' RETURN r,s").rows == ()
    assert len(graph.execute("MATCH (a:P {id:2})-[r:C]-(a) MATCH (a)-[s:C]-(a) RETURN r,s").rows) == 1


def test_alias_subquery_union_and_pending_reader_isolation(graph):
    query = "MATCH p=(a)-[r]->(b) RETURN p"
    before = graph.execute(query).rows
    assert len(graph.execute("CALL () { MATCH p=(a)-[:A]->(b) RETURN p UNION "
                             "MATCH p=(a)-[:B]->(b) RETURN p } WITH p AS q RETURN q").rows) == 3
    assert graph.execute("MATCH (a:Other) CALL (a) { MATCH (a)-[r]->(b) RETURN r,b } "
                         "RETURN type(r),labels(b)").rows == (("D", ("Q",)),)
    with graph.begin("read") as reader:
        assert reader.execute(query).rows == before
        with graph.begin("write") as tx:
            tx.execute("MATCH (a:P {id:3}), (b:Q) CREATE (a)-[:A {weight:6}]->(b)")
            pending = tx.execute(query).rows
            assert len(pending) == 6
            assert any(p.relationships[0].provenance.pending for (p,) in pending)
            assert reader.execute(query).rows == before
        assert reader.execute(query).rows == before
    assert len(graph.execute(query).rows) == 6


def test_missing_properties_conflicting_families_and_entity_kinds(graph):
    assert graph.execute("MATCH ()-[r]->() RETURN r.absent").rows == ((None,),) * 5
    with pytest.raises(GrafxPlanError):
        graph.execute("MATCH ()-[r]->() RETURN labels(r)")
    with graph.begin("write") as tx:
        tx.execute("CREATE REL TABLE Conflict(FROM P TO Q, weight STRING)")
    with pytest.raises(GrafxPlanError, match="tables do not agree"):
        graph.execute("MATCH ()-[r]->() RETURN r.weight")
    assert len(graph.execute("MATCH ()-[r:A|B]->() RETURN r.weight").rows) == 3


def test_generic_hop_keeps_index_anchor_and_detached_public_plan(graph):
    result = graph.execute("MATCH p=(a:P {id:1})-[r]->(b) RETURN p")
    assert any(node.label == "IndexSeek" for node in result.plan.walk())
    operator = next(node for node in result.plan.walk() if node.label == "TraverseAnyRelationship")
    assert operator.path_variable == "p"
    object.__setattr__(operator, "tables", ())
    assert graph.execute("MATCH p=(a:P {id:1})-[r]->(b) RETURN p").rows == result.rows


def test_cursor_cancel_releases_generic_hops(graph):
    token = okto_grafx.CancellationToken()
    with graph.query("MATCH p=(a)-[r]->(b) RETURN p").cursor(batch_size=1, cancellation=token) as cursor:
        assert cursor.fetchone()
        token.cancel()
        with pytest.raises(GrafxQueryCancelled):
            cursor.fetchone()
    assert graph.transactions.open_transactions == 0


def test_native_empty_catalog_optional(tmp_path):
    with okto_grafx.connect(tmp_path / "empty") as db:
        assert db.execute("OPTIONAL MATCH ()-[r]->() RETURN r.missing,type(r)").rows == ((None, None),)
        assert db.execute("MATCH ()-[r]->() RETURN count(*)").rows == ((0,),)


def test_global_expansion_budget_is_shared_across_alternatives(graph):
    with okto_grafx.connect(graph.path, max_traversal_expansions=1) as reader:
        with pytest.raises(GrafxQueryBudgetExceeded):
            reader.execute("MATCH (a:P {id:1})-[r:A|B]->(b) RETURN r")
        assert reader.transactions.open_transactions == 0


def test_late_error_after_polymorphic_capture_rolls_back_observed_set(graph, monkeypatch):
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
            tx.execute("MATCH p=(a:P {id:1})-[r]->(b) UNWIND [a,1] AS item "
                       "SET a.id=11 RETURN properties(item),p")
        assert applied
        assert tx.execute("MATCH (n:P) RETURN n.id ORDER BY n.id").rows == ((1,), (2,), (3,))
        assert tx.execute("MATCH (n:Other {id:99}) RETURN n.id").rows == ((99,),)


def test_generic_paths_spill_without_losing_endpoint_identity(graph, monkeypatch):
    from okto_grafx.adapters.query_spill_local import _Sorter

    original = _Sorter._write_pair
    written = []

    def observed(stream, record):
        original(stream, record)
        written.append(len(record[1]))

    monkeypatch.setattr(_Sorter, "_write_pair", staticmethod(observed))
    with okto_grafx.connect(graph.path, query_memory_budget_bytes=8192) as reader:
        rows = reader.execute("UNWIND range(1,100) AS i MATCH p=(a:P {id:1})-[:A|B]->(b) "
                              "RETURN i,p ORDER BY i DESC").rows
        assert len(rows) == 200
        assert [i for i, _ in rows] == [i for i in range(100, 0, -1) for _ in range(2)]
        assert len({p for _, p in rows}) == 2
        assert written and sum(written) > 8192
        assert reader.transactions.open_transactions == 0


def test_alternatives_plan_tracks_later_ddl(graph):
    query = "MATCH ()-[r:A|Later]->() RETURN type(r)"
    assert graph.execute(query).rows == (("A",), ("A",))
    with graph.begin("write") as tx:
        tx.execute("CREATE REL TABLE Later(FROM P TO Q)")
        tx.execute("MATCH (a:P {id:3}), (b:Q) CREATE (a)-[:Later]->(b)")
    assert sorted(graph.execute(query).rows) == [("A",), ("A",), ("Later",)]
