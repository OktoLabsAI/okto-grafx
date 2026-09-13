"""Versioned SET/REMOVE labels through the public statement/result doors."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("typed", [False, True])
def test_set_remove_labels_owner_snapshot_properties_and_reopen(tmp_path, codec, typed):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        if typed:
            db.maintenance.ensure_identity_indexes()
            with db.begin("write") as tx:
                tx.execute("CREATE NODE TABLE N(id INT64, value ANY, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1, value:'safe'})")
        before = db.execute("MATCH (n) RETURN n").rows[0][0]
        reader = db.begin("read")
        try:
            with db.begin("write") as tx:
                result = tx.execute("MATCH (n) SET n:B:A:B RETURN labels(n), n:A:B, n")
                assert result.rows[0][:2] == (("A", "B", "N"), True)
                assert result.rows[0][2].labels == ("A", "B", "N")
                assert result.rows[0][2].identity == before.identity
                assert result.rows[0][2].to_dict()["labels"] == ["A", "B", "N"]
                tx.execute("MATCH (n) SET n.value='updated'")
                assert tx.execute("MATCH (n) RETURN labels(n), n.value").rows == ((("A", "B", "N"), "updated"),)
                tx.execute("MATCH (n) REMOVE n:A:N:B")
                assert tx.execute("MATCH (n) RETURN labels(n), n:N").rows == (((), False),)
                tx.execute("MATCH (n) SET n:`á weird`:Z")
                assert tx.execute("MATCH (n) RETURN labels(n)").rows == ((("Z", "á weird"),),)
            assert reader.execute("MATCH (n) RETURN labels(n), n.value").rows == ((("N",), "safe"),)
        finally:
            reader.rollback()
        current = db.execute("MATCH (n) RETURN n").rows[0][0]
        assert current.labels == ("Z", "á weird") and current.identity == before.identity
    with connect(path) as db:
        assert db.execute("MATCH (n) RETURN labels(n), n.value").rows == ((("Z", "á weird"), "updated"),)
        assert not db.verify("all").findings


def test_held_created_nodes_and_mixed_set_order(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("CREATE (n:N {id:1}) SET n:B, n.seen=labels(n), n:A RETURN n, n.seen, labels(n)")
            assert result.rows[0][0].labels == ("A", "B", "N")
            assert result.rows[0][1:] == (("B", "N"), ("A", "B", "N"))
            tx.execute("MATCH (n) REMOVE n:A, n.seen, n:N")
            assert tx.execute("MATCH (n) RETURN labels(n), n.seen").rows == ((("B",), None),)
        assert db.execute("MATCH (n) RETURN labels(n)").rows == ((("B",),),)


def test_refused_statement_and_rollback_preserve_labels_and_catalog(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1})")
        tx = db.begin("write")
        try:
            tx.execute("MATCH (n) SET n:Good")
            with pytest.raises(GrafxError):
                tx.execute("MATCH (n) SET n:Discarded, n.invalid=0.0/0.0")
            assert tx.execute("MATCH (n) RETURN labels(n)").rows == ((("Good", "N"),),)
            assert "Discarded" not in db._queries._working_catalog(tx._context).table("N").node_label_candidates
        finally:
            tx.rollback()
        assert db.execute("MATCH (n) RETURN labels(n)").rows == ((("N",),),)
        assert not db.verify("all").findings


def test_null_is_noop_and_relationship_or_scalar_is_refused(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            assert tx.execute("WITH null AS n SET n:A RETURN n").rows == ((None,),)
            assert tx.execute("WITH null AS n REMOVE n:A RETURN n").rows == ((None,),)
            with pytest.raises(GrafxError):
                tx.execute("WITH 1 AS n SET n:A")
            tx.execute("CREATE (:N {id:1})-[:R]->(:N {id:2})")
            with pytest.raises(GrafxError):
                tx.execute("MATCH ()-[r:R]->() SET r:A")
        assert db.execute("MATCH (n) RETURN labels(n)").rows == ((("N",),), (("N",),))


def test_label_matches_span_physical_owners_and_filter_removed_pk_hits(tmp_path):
    with connect(tmp_path / "db") as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE M(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1}), (:N {id:2}), (:M {id:3})")
        with db.begin("write") as tx:
            tx.execute("MATCH (n) WHERE n.id IN [1,3] SET n:Shared")
            tx.execute("MATCH (n:N {id:2}) REMOVE n:N")
            assert tx.execute("MATCH (n:Shared) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))
            assert tx.execute("MATCH (n:N:Shared) RETURN n.id").rows == ((1,),)
            assert tx.execute("MATCH (n:N {id:2}) RETURN n").rows == ()
            assert tx.execute("MATCH (n:N) WHERE n.id IN $ids RETURN n.id", {"ids": [1,2]}).rows == ((1,),)
            assert tx.execute("MATCH (n:NoSuch:Shared) RETURN n").rows == ()
        assert db.execute("MATCH (n:Shared) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))
        assert db.execute("MATCH (n:N {id:2}) RETURN n").rows == ()
        assert db.execute("MATCH (n) WHERE n.id=2 RETURN labels(n)").rows == (((),),)


def test_label_added_then_rematched_in_the_same_statement(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("CREATE (n:N {id:1}) SET n:X WITH n MATCH (n:X) RETURN labels(n)")
            assert result.rows == ((("N", "X"),),)
            result = tx.execute("MATCH (n) SET n:Y WITH n MATCH (m:Y) RETURN labels(m)")
            assert result.rows == ((("N", "X", "Y"),),)


def test_label_changes_preserve_edges_and_optional_null_extension(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1})-[:R]->(:M {id:2})")
        original = db.execute("MATCH ()-[r:R]->() RETURN r").rows[0][0]
        with db.begin("write") as tx:
            tx.execute("MATCH (n) SET n:Shared")
            tx.execute("MATCH (n:M) REMOVE n:M")
        result = db.execute("MATCH p=(a:Shared)-[:R]->(b:Shared) RETURN p, labels(a), labels(b)")
        assert result.rows[0][1:] == (("N", "Shared"), ("Shared",))
        assert result.rows[0][0].relationships[0].identity == original.identity
        assert result.rows[0][0].nodes[0].identity == original.source
        assert result.rows[0][0].nodes[1].identity == original.target
        assert db.execute("MATCH (a:Shared)-[:R]->(b:M) RETURN b").rows == ()
        assert db.execute("MATCH (a:N) OPTIONAL MATCH (a)-[:R]->(b:M) RETURN a.id,b").rows == ((1,None),)
        assert db.execute("MATCH (a:N) OPTIONAL MATCH (a)-[:R]->(b:Shared) RETURN b.id").rows == ((2,),)


def test_spilled_node_labels_survive_sort_distinct_and_entity_results(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("UNWIND range(1,30) AS id CREATE (n:N {id:id}) SET n:B:A")
    with connect(path, query_memory_budget_bytes=2048) as db:
        result = db.execute("MATCH (n) WITH n ORDER BY n.id DESC RETURN n, labels(n)")
        assert len(result.rows) == 30
        assert [row[0].properties["id"] for row in result.rows] == list(range(30,0,-1))
        assert all(row[0].labels == row[1] == ("A", "B", "N") for row in result.rows)
        assert db.execute("MATCH (n) RETURN DISTINCT labels(n)").rows == ((("A", "B", "N"),),)


@pytest.mark.parametrize("text", ["MATCH (n) SET n:B:A,n.x=1", "MATCH (n) REMOVE n:A,n.x,n:B"])
def test_label_mutation_ast_round_trip(text):
    from okto_grafx.domain.query import parse
    statement = parse(text)
    assert parse(statement.describe()) == statement


def test_noop_labels_do_not_activate_metadata_or_stage_row_versions(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1})")
        with db.begin("write") as tx:
            assert tx.execute("MATCH (n) SET n:N:N REMOVE n:Missing RETURN labels(n)").rows == ((("N",),),)
            assert tx._context.row_intents == []
        assert not db._catalog.catalog.requires_capability("node_labels_v1")


def test_independent_handles_keep_old_labels_and_conflict_on_same_node(tmp_path):
    from okto_grafx.domain.errors import GrafxWriteConflict
    path = tmp_path / "db"
    with connect(path) as first:
        first.maintenance.ensure_identity_indexes()
        with first.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, value STRING, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1,value:'safe'})")
            tx.execute("MATCH (n) SET n:A")
        with connect(path) as second:
            reader = second.begin("read")
            loser = second.begin("write")
            try:
                assert reader.execute("MATCH (n:A) RETURN labels(n)").rows == ((("A", "N"),),)
                loser.execute("MATCH (n:N) SET n.value='loser'")
                with first.begin("write") as winner:
                    winner.execute("MATCH (n) REMOVE n:A SET n:B")
                assert reader.execute("MATCH (n:A) RETURN labels(n)").rows == ((("A", "N"),),)
                with pytest.raises(GrafxWriteConflict):
                    loser.commit()
            finally:
                reader.rollback()
                if loser.active:
                    loser.rollback()
            assert second.execute("MATCH (n:B) RETURN labels(n),n.value").rows == ((("B", "N"), "safe"),)


@pytest.mark.parametrize("labels", [["A"], ("B","A"), ("A","A"), ("",)])
def test_detached_metadata_rejects_noncanonical_labels(labels):
    from okto_grafx.domain.query.entity_identity import EntityIdentity, EntityProvenance
    from okto_grafx.domain.query.entity_values import NodeValue
    with pytest.raises(GrafxError):
        NodeValue(EntityIdentity(b"d" * 16, 1, "node", record_id=1), "A", {},
                  EntityProvenance(10,1,1), node_labels=labels)


def test_conditional_merge_actions_use_the_same_label_mutation_door(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            assert tx.execute("MERGE (n:N {id:1}) ON CREATE SET n:A ON MATCH SET n:B RETURN labels(n)").rows == ((("A", "N"),),)
            assert tx.execute("MERGE (n:N {id:1}) ON CREATE SET n:A ON MATCH SET n:B RETURN labels(n)").rows == ((("A", "B", "N"),),)
        assert db.execute("MATCH (n) RETURN labels(n)").rows == ((("A", "B", "N"),),)


@pytest.mark.parametrize("schema_write", [False, True])
def test_procedure_label_admission_needs_schema_authority_only_for_new_candidates(tmp_path, schema_write):
    from okto_grafx.extensions import ExtensionRegistry, TabularProcedure
    def callback(writer):
        writer.execute("MATCH (n) SET n:New")
    permissions = frozenset({"schema", "graph"})
    procedure = TabularProcedure("app.label", (), (), callback, mode="write",
                                  schema_write=schema_write, required_permissions=permissions)
    extensions = ExtensionRegistry(trusted=True, procedures=(procedure,), procedure_permissions=permissions)
    with connect(tmp_path / "db", extensions=extensions) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1})")
        with db.begin("write") as tx:
            if schema_write:
                tx.execute("CALL app.label()")
            else:
                with pytest.raises(GrafxError):
                    tx.execute("CALL app.label()")
                assert tx.execute("MATCH (n) RETURN labels(n)").rows == ((("N",),),)
                tx.execute("MATCH (n) SET n:New REMOVE n:New")
                tx.execute("CALL app.label()")
            assert tx.execute("MATCH (n) RETURN labels(n)").rows == ((("N", "New"),),)


def test_direct_ast_remove_cannot_smuggle_a_property_assignment():
    from dataclasses import replace
    from okto_grafx.domain.query import parse, analyze, Literal
    query = parse("MATCH (n) REMOVE n.x")
    original, = query.updating_clauses
    changed = replace(original, items=(replace(original.items[0], value=Literal(7)),))
    forged = replace(query, updating_clauses=(changed,), clause_pipeline=tuple(
        changed if clause is original else clause for clause in query.clause_pipeline))
    with pytest.raises(GrafxError):
        analyze(forged)


def test_read_only_spilled_labels_do_not_reacquire_heap_authority(monkeypatch):
    from types import SimpleNamespace
    from okto_grafx.domain.model.record import HeapVersion
    from okto_grafx.domain.model.schema import TableDef, ColumnDef
    from okto_grafx.domain.model.value import ValueType
    from okto_grafx.engine import query_engine as module
    table = TableDef(1, "N", "node", (ColumnDef("id", ValueType.INT64),), extra_node_labels=("A",))
    version = HeapVersion(record_id=1, xmin=1, xmax=0, values=(1,), prev=None,
                          schema_version=1, deleted=False, table_id=1, node_labels=("A", "N"))
    binding = module.RowBinding("n", table, None, version)
    intents, held = (), module._RevisionList()
    context = SimpleNamespace(resolve_binding=lambda value: value, txn=SimpleNamespace(row_intents=intents),
                              pending_token=lambda value: None,
                              staged_rows=held, node_label_overlays={1:{}},
                              node_label_overlay_revision=(id(intents), 0, None, 0, held.rewrite_revision))
    def forbidden(*args):
        raise AssertionError("An unchanged read-only spill needs no heap authority")
    monkeypatch.setattr(module, "_unwound_entity", forbidden)
    assert module._current_node_labels(context, binding) == ("A", "N")
