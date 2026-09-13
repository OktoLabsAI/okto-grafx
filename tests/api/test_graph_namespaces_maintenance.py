"""Maintenance and append-only DDL select physical identity, never a first name match."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.model.schema import ColumnDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.errors import GrafxError


def seed(db):
    db.ensure_identity_indexes()
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE R(id INT64, value STRING, PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE R(FROM R TO R, value STRING)")
        tx.execute("CREATE(a:R {id:1,value:'node-old'})-[r:R {value:'edge-old'}]->(a)")
    with db.begin("write") as tx:
        tx.execute("MATCH(n:R) SET n.value='node-new'")
        tx.execute("MATCH()-[r:R]->() SET r.value='edge-new'")
    db.checkpoint()


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_bloat_census_keeps_same_named_tables_separate(tmp_path, codec):
    path = tmp_path / codec
    with connect(path,codec=codec) as db:
        seed(db)
        before = db.transactions.published_state().last_committed_lsn
        all_tables = db.maintenance.bloat()
        assert {item.table for item in all_tables.tables} == {("node","R"),("rel","R")}
        assert len({item.table_id for item in all_tables.tables}) == 2
        for kind in ("node","rel"):
            selected = db.maintenance.bloat((kind,"R"))
            assert len(selected.tables) == 1
            assert selected.tables[0].table == (kind,"R")
            assert selected.tables[0].table_id == db.catalog.catalog.table("R",kind=kind).table_id
            assert selected.ended_versions == 1
        with pytest.raises(GrafxError) as failure:
            db.maintenance.bloat("R")
        assert failure.value.details["reason"] == "ambiguous_table_name"
        assert db.transactions.published_state().last_committed_lsn == before
    with connect(path,codec=codec,read_only=True) as db:
        assert db.maintenance.bloat(("rel","R")).ended_versions == 1


@pytest.mark.parametrize("kind", ["node", "rel"])
@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_vacuum_reclaims_only_selected_owner_and_reopens(tmp_path, kind, codec):
    path = tmp_path / "db"
    with connect(path,codec=codec) as db:
        seed(db)
        before = db.transactions.published_state().last_committed_lsn
        capabilities = db._catalog.catalog.required_capabilities()
        with pytest.raises(GrafxError) as failure:
            db.maintenance.vacuum("R",confirm_quiescent=True)
        assert failure.value.details["reason"] == "ambiguous_table_name"
        assert db.transactions.published_state().last_committed_lsn == before
        assert db._catalog.catalog.required_capabilities() == capabilities
        report = db.maintenance.vacuum((kind,"R"),confirm_quiescent=True)
        assert report.complete and report.reclaimed_versions == 1
        assert len(report.tables) == 1 and report.tables[0].table == (kind,"R")
        untouched = "rel" if kind == "node" else "node"
        assert db.maintenance.bloat((untouched,"R")).ended_versions == 1
        assert db.verify("all").findings == ()
    with connect(path,codec=codec) as db:
        assert db.execute("MATCH(n:R) RETURN n.id,n.value").rows == ((1,"node-new"),)
        assert db.execute("MATCH()-[r:R]->() RETURN r.value").rows == (("edge-new",),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("kind", ["node", "rel"])
@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_nullable_column_targets_one_namespace(tmp_path, kind, codec):
    path = tmp_path / "db"
    with connect(path,codec=codec) as db:
        seed(db)
        before = db.transactions.published_state().last_committed_lsn
        with pytest.raises(GrafxError) as failure:
            db.add_nullable_column("R",ColumnDef("extra",ValueType.STRING))
        assert failure.value.details["reason"] == "ambiguous_table_name"
        assert db.transactions.published_state().last_committed_lsn == before
        updated = db.add_nullable_column((kind,"R"),ColumnDef("extra",ValueType.STRING))
        assert updated.kind == kind and updated.schema_version == 2
        untouched = "rel" if kind == "node" else "node"
        assert db.catalog.catalog.table("R",kind=untouched).schema_version == 1
        query = "MATCH(n:R)" if kind == "node" else "MATCH()-[n:R]->()"
        assert db.execute(query+" RETURN n.extra").rows == ((None,),)
        with db.begin("write") as tx:
            tx.execute(query+" SET n.extra='new'")
        assert db.verify("all").findings == ()
    with connect(path,codec=codec) as db:
        assert db.execute(query+" RETURN n.extra").rows == (("new",),)
        assert db.catalog.catalog.table("R",kind=untouched).schema_version == 1
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("selector", [("edge","R"),("node", ""),["node","R"],("node","R",1),17])
def test_invalid_selectors_refuse_before_mutation(tmp_path, selector):
    with connect(tmp_path / "db") as db:
        seed(db)
        before = db.transactions.published_state().last_committed_lsn
        for operation in (
            lambda: db.maintenance.bloat(selector),
            lambda: db.maintenance.vacuum(selector,confirm_quiescent=True),
            lambda: db.add_nullable_column(selector,ColumnDef("extra",ValueType.STRING)),
        ):
            with pytest.raises(GrafxError):
                operation()
        assert db.transactions.published_state().last_committed_lsn == before


def test_qualified_internal_table_names_do_not_bypass_column_guard(tmp_path):
    with connect(tmp_path / "db") as db:
        seed(db)
        with pytest.raises(GrafxError):
            db.add_nullable_column(("node","_grafx_views_v1"),ColumnDef("extra",ValueType.STRING))
