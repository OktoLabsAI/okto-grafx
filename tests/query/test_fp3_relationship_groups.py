"""A native logical relationship type spans physical pairs, never table-local ids."""

from dataclasses import FrozenInstanceError

import pytest

import okto_grafx
from okto_grafx.errors import GrafxError
from okto_grafx.domain.query.parser import parse
from okto_grafx.engine.query_engine import QueryEngine
from okto_grafx.catalog_copy import capture_copy
from okto_grafx.migrations import SchemaMigration, migrate_schema
from okto_grafx.transfer import export_graph


DDL = "CREATE REL TABLE GROUP REL(FROM A TO B, FROM B TO C, num INT64)"


def prepare(db):
    with db.begin("write") as tx:
        for name in ("A", "B", "C"):
            tx.execute(f"CREATE NODE TABLE {name}(id INT64, PRIMARY KEY(id))")
    db.maintenance.ensure_identity_indexes()


def populate(db):
    with db.begin("write") as tx:
        tx.execute(DDL)
    with db.begin("write") as tx:
        tx.execute("CREATE (a:A {id:1})-[:REL {num:1}]->(b:B {id:1})-[:REL {num:2}]->(c:C {id:1})")


@pytest.fixture(params=["memory", "file"])
def graph(request, tmp_path):
    with okto_grafx.connect(":memory:" if request.param == "memory" else tmp_path / "db") as db:
        prepare(db)
        populate(db)
        yield db


def test_ddl_round_trip_and_closed_immutable_pairs():
    statement = parse(DDL)
    assert statement.endpoint_pairs == (("A", "B"), ("B", "C"))
    assert parse(statement.describe()) == statement
    with pytest.raises(FrozenInstanceError):
        statement.endpoint_pairs = ()


@pytest.mark.parametrize("pattern", ["(a:A)-[:REL*2..2]->(b)", "(b)<-[:REL*2..2]-(a:A)", "(a:A)-[:REL*2..2]-(b:C)"])
def test_two_edges_share_type_without_collapsing_identity(graph, pattern):
    rows = graph.execute(f"MATCH p={pattern} RETURN p,relationships(p),nodes(p),length(p)").rows
    assert len(rows) == 1
    path, edges, nodes, length = rows[0]
    assert path.relationships == edges and path.nodes == nodes and length == 2
    assert [edge.label for edge in edges] == ["REL", "REL"]
    assert {edge.properties["num"] for edge in edges} == {1, 2}
    assert len({edge.identity for edge in edges}) == 2
    assert len({edge.identity.record_id for edge in edges}) == 1
    assert len({node.identity for node in nodes}) == 3


@pytest.mark.parametrize("types", ["REL", "REL|REL", "REL|Missing"])
def test_logical_selection_and_type_scalar_have_no_duplicate_member_scans(graph, types):
    rows = graph.execute(f"MATCH ()-[r:{types}]->() RETURN type(r),r.num ORDER BY r.num").rows
    assert rows == (("REL", 1), ("REL", 2))
    assert graph.execute("MATCH ()-[r]->() RETURN type(r),r.num ORDER BY r.num").rows == rows


def test_optional_bound_targets_zero_paths_and_subquery_exports(graph):
    assert graph.execute("MATCH (a:A) OPTIONAL MATCH (a)-[r:REL]->(c:C) RETURN r").rows == ((None,),)
    assert graph.execute("MATCH (a:A) MATCH (c:C) OPTIONAL MATCH (a)-[r:REL]->(c) RETURN r").rows == ((None,),)
    assert graph.execute("MATCH p=(a)-[:REL*0..0]->(b) RETURN count(p)").rows == ((3,),)
    assert graph.execute("CALL () { MATCH ()-[r:REL]->() RETURN r } RETURN type(r),r.num ORDER BY r.num").rows == (("REL", 1), ("REL", 2))


def test_catalog_view_preserves_logical_metadata_after_close(graph):
    view = graph.catalog.catalog
    groups = view.relationship_types()
    assert len(groups) == 1 and groups[0].name == "REL"
    members = view.relationship_tables("REL")
    assert {(table.from_table, table.to_table) for table in members} == {("A", "B"), ("B", "C")}
    for member in members:
        assert view.relationship_type_name(member.table_id) == "REL"
        assert view.relationship_tables(member.name) == ()
        assert graph.execute(f"MATCH ()-[r:{member.name}]->() RETURN r").rows == ()
    assert not view.has_table("REL")
    graph.close()
    assert view.relationship_types() == groups
    assert view.relationship_tables("REL") == members


def test_reopen_and_verify_preserve_group_values(tmp_path):
    path = tmp_path / "db"
    with okto_grafx.connect(path) as db:
        prepare(db)
        populate(db)
        previous = db.execute("MATCH ()-[r:REL]->() RETURN r ORDER BY r.num").rows
        assert db.verify("all").findings == ()
    with okto_grafx.connect(path) as db:
        assert db.execute("MATCH ()-[r:REL]->() RETURN r ORDER BY r.num").rows == previous
        assert db.catalog.catalog.relationship_types()[0].name == "REL"
        assert db.verify("all").findings == ()


def test_legacy_catalog_refusal_precedes_member_creation():
    with okto_grafx.connect(":memory:") as db:
        with db.begin("write") as tx:
            for name in ("A", "B", "C"):
                tx.execute(f"CREATE NODE TABLE {name}(id INT64)")
        before = db.catalog.catalog.tables()
        with db.begin("write") as tx:
            with pytest.raises(GrafxError) as error:
                tx.execute(DDL)
            assert error.value.details["remedy"] == "maintenance.ensure_identity_indexes"
            tx.execute("CREATE (:A {id:7})")
        assert db.catalog.catalog.tables() == before
        assert db.execute("MATCH (a:A) RETURN a.id").rows == ((7,),)


def test_group_ddl_rollback_and_late_attachment_failure_are_atomic(tmp_path, monkeypatch):
    with okto_grafx.connect(tmp_path / "db") as db:
        prepare(db)
        before = db.catalog.catalog.tables()
        original = QueryEngine._attach_endpoint_indexes
        calls = 0

        def fail_second(self, *args, **kwargs):
            nonlocal calls
            original(self, *args, **kwargs)
            calls += 1
            if calls == 2:
                raise RuntimeError("second member fault")

        with db.begin("write") as tx:
            with monkeypatch.context() as patch:
                patch.setattr(QueryEngine, "_attach_endpoint_indexes", fail_second)
                with pytest.raises(GrafxError) as failure:
                    tx.execute(DDL)
                assert isinstance(failure.value.__cause__, RuntimeError)
                assert str(failure.value.__cause__) == "second member fault"
            tx.execute("CREATE (:A {id:7})")
        assert db.catalog.catalog.tables() == before
        assert db.catalog.catalog.relationship_types() == ()
        with db.begin("write") as tx:
            tx.execute(DDL)
            tx.rollback()
        assert db.catalog.catalog.tables() == before
        with db.begin("write") as tx:
            tx.execute(DDL)
        assert len(db.catalog.catalog.relationship_types()) == 1
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("ddl", [
    "CREATE REL TABLE GROUP REL(FROM A TO B, FROM A TO B, num INT64)",
    "CREATE REL TABLE GROUP REL(FROM A TO Missing, num INT64)",
])
def test_bad_group_ddl_preserves_prior_successful_statements(ddl):
    with okto_grafx.connect(":memory:") as db:
        prepare(db)
        before = db.catalog.catalog.tables()
        with db.begin("write") as tx:
            tx.execute("CREATE (:A {id:7})")
            with pytest.raises(GrafxError):
                tx.execute(ddl)
        assert db.catalog.catalog.tables() == before
        assert db.catalog.catalog.relationship_types() == ()
        assert db.execute("MATCH (a:A) RETURN a.id").rows == ((7,),)


def test_group_name_can_equal_a_node_label():
    with okto_grafx.connect(":memory:") as db:
        prepare(db)
        with db.begin("write") as tx:
            tx.execute("CREATE REL TABLE GROUP A(FROM A TO B, num INT64)")
            tx.execute("CREATE(a:A {id:1})-[:A {num:7}]->(:B {id:2})")
        assert db.execute("MATCH(a:A)-[r:A]->(b:B) RETURN a.id,type(r),r.num,b.id").rows == ((1,"A",7,2),)
        assert db.catalog.catalog.table("A", kind="node").kind == "node"
        assert db.catalog.catalog.relationship_tables("A")[0].kind == "rel"
        assert db.verify("all").findings == ()


def test_group_with_one_member_still_has_one_logical_name():
    with okto_grafx.connect(":memory:") as db:
        prepare(db)
        with db.begin("write") as tx:
            tx.execute("CREATE REL TABLE GROUP ONLY(FROM A TO B, num INT64)")
        with db.begin("write") as tx:
            tx.execute("CREATE (:A {id:1})-[:ONLY {num:3}]->(:B {id:1})")
        assert db.execute("MATCH ()-[r:ONLY]->() RETURN type(r),r.num").rows == (("ONLY", 3),)
        assert len(db.catalog.catalog.relationship_tables("ONLY")) == 1


def test_member_selection_for_merge_set_delete_and_failed_create(graph):
    with graph.begin("write") as tx:
        rows = tx.execute("MATCH (a:A),(b:B) MERGE (a)-[r:REL {num:1}]->(b) RETURN type(r),r.num").rows
        assert rows == (("REL", 1),)
        tx.execute("MATCH (a:A)-[r:REL]->(b:B) SET r.num=11")
        assert tx.execute("MATCH ()-[r:REL]->() RETURN r.num ORDER BY r.num").rows == ((2,), (11,))
        tx.execute("MATCH (b:B)-[r:REL]->(c:C) DELETE r")
        with pytest.raises(GrafxError):
            tx.execute("CREATE (:A {id:2})-[:REL {num:3}]->(:C {id:2})")
    assert graph.execute("MATCH ()-[r:REL]->() RETURN type(r),r.num").rows == (("REL", 11),)
    assert graph.execute("MATCH (a:A) RETURN a.id").rows == ((1,),)


def test_distinct_and_union_keep_same_local_id_in_two_members(graph):
    query = "MATCH ()-[r:REL]->() RETURN r UNION MATCH ()-[r:REL]->() RETURN r"
    rows = graph.execute(query).rows
    assert len(rows) == 2 and len({row[0].identity for row in rows}) == 2
    assert {row[0].label for row in rows} == {"REL"}
    assert len(graph.execute(query.replace(" UNION ", " UNION ALL ")).rows) == 4


def test_read_participant_opened_before_group_adopts_only_committed_schema(tmp_path):
    path = tmp_path / "db"
    with okto_grafx.connect(path) as writer:
        prepare(writer)
        with okto_grafx.connect(path) as reader:
            captured = reader.catalog.catalog
            assert captured.relationship_types() == ()
            populate(writer)
            assert reader.execute("MATCH ()-[r:REL]->() RETURN type(r),r.num ORDER BY r.num").rows == (("REL", 1), ("REL", 2))
            assert captured.relationship_types() == ()
            assert reader.catalog.catalog.relationship_types()[0].name == "REL"


def test_migration_preview_and_execution_preserve_logical_groups(tmp_path):
    with okto_grafx.connect(tmp_path / "db") as db:
        prepare(db)
        migration = (SchemaMigration(1, (DDL,)),)
        migrate_schema(db, migration, namespace="groups", dry_run=True)
        assert db.catalog.catalog.relationship_types() == ()
        migrate_schema(db, migration, namespace="groups")
        assert db.catalog.catalog.relationship_types()[0].name == "REL"
        migrate_schema(db, migration, namespace="groups")  # Idempotent ledger, not a duplicate DDL.


def test_transfer_and_copy_capture_preserve_groups(tmp_path):
    with okto_grafx.connect(tmp_path / "db") as db:
        prepare(db)
        db.enable_commit_history()
        populate(db)
        export_graph(db, tmp_path / "export")
        assert (tmp_path / "export" / "manifest.json").exists()
        members = tuple(table.name for table in db.catalog.catalog.relationship_tables("REL"))
        with db.begin("read") as tx:
            package = capture_copy(tx, tables=("A", "B", "C") + members)
            assert {item.logical_type for item in package.tables if item.schema.kind == "rel"} == {"REL"}
        assert db.execute("MATCH ()-[r:REL]->() RETURN count(r)").rows == ((2,),)
