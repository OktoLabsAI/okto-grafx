"""Logical view dependency hashes retain both physical kinds with identical names."""

from dataclasses import asdict
import hashlib
import json

import pytest

from okto_grafx import connect
from okto_grafx.domain.model.schema import ColumnDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.errors import GrafxConfigurationError, GrafxLedgerError


def seed(db):
    db.ensure_identity_indexes()
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE R(id INT64,body STRING,PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE R(FROM R TO R,body STRING)")
        tx.execute("CREATE(a:R {id:1,body:'node'})-[:R {body:'edge'}]->(a)")
    db.views.prepare()


QUERY = "MATCH(n:R)-[e:R]->(m:R) RETURN n.body AS node,e.body AS edge,m.id AS target"


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("changed_kind", ["node", "rel"])
def test_each_physical_dependency_is_retained_and_schema_change_refuses(tmp_path, codec, changed_kind):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        seed(db)
        view = db.views.create("both", query=QUERY)
        expected = tuple(sorted(("R", hashlib.sha256(json.dumps(
            asdict(db._catalog.catalog.table("R", kind=kind)), ensure_ascii=True,
            sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest())
            for kind in ("node", "rel")))
        assert view.dependencies == expected and len(view.dependencies) == 2
        assert db.views.execute("both").rows == (("node", "edge", 1),)
        # Reader's definition snapshot must not be rewritten by an ordinary data update.
        with db.begin("read") as old:
            with db.begin("write") as tx:
                tx.execute("MATCH(n:R) SET n.body='new-node'")
                tx.execute("MATCH(:R)-[e:R]->(:R) SET e.body='new-edge'")
            assert db.views.execute("both", snapshot=old).rows == (("node", "edge", 1),)
        assert db.views.execute("both").rows == (("new-node", "new-edge", 1),)
        db.add_nullable_column((changed_kind, "R"), ColumnDef("extra", ValueType.STRING))
        before = db.transactions.published_state().last_committed_lsn
        with pytest.raises(GrafxLedgerError) as failure:
            db.views.execute("both")
        assert failure.value.details["reason"] == "stale_dependency"
        assert db.transactions.published_state().last_committed_lsn == before
        replacement = db.views.create("both", query=QUERY, replace=True)
        assert replacement.sha256 != view.sha256
        assert len(set(replacement.dependencies) & set(view.dependencies)) == 1
        assert db.views.execute("both").rows == (("new-node", "new-edge", 1),)
        db.checkpoint()
    with connect(path, codec=codec, read_only=True) as db:
        assert db.views.get("both") == replacement
        assert db.views.execute("both").rows == (("new-node", "new-edge", 1),)
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("registry_first", [False, True])
def test_owned_view_registry_does_not_consume_same_named_edge(tmp_path, codec, registry_first):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE(:N {id:1})")
        if registry_first:
            db.views.prepare()
        with db.begin("write") as tx:
            tx.execute("CREATE REL TABLE _grafx_views_v1(FROM N TO N,body STRING)")
            tx.execute("MATCH(n:N) CREATE(n)-[:_grafx_views_v1 {body:'untouched'}]->(n)")
        db.views.prepare()
        before = db.transactions.published_state().last_committed_lsn
        db.views.prepare()
        assert db.transactions.published_state().last_committed_lsn == before
        view = db.views.create("nodes", query="MATCH(n:N) RETURN n.id")
        assert db.views.execute("nodes").rows == ((1,),)
        assert db.execute("MATCH(:N)-[e:_grafx_views_v1]->(:N) RETURN e.body").rows == (("untouched",),)
        db.checkpoint()
    with connect(path, codec=codec, read_only=True) as db:
        assert db.views.get("nodes") == view
        assert db.views.execute("nodes").rows == ((1,),)


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_old_incomplete_dependency_inventory_refuses_until_explicit_replace(tmp_path, codec):
    from okto_grafx.views import _body, _hash

    with connect(tmp_path / "db", codec=codec) as db:
        seed(db)
        complete = db.views.create("both", query=QUERY)
        incomplete_body = _body(QUERY, (), complete.columns, complete.dependencies[:1])
        with db.begin("write") as tx:
            tx.execute("MATCH(v:_grafx_views_v1 {name:'v_both'}) SET v.body=$body,v.sha256=$hash",
                       {"body": incomplete_body, "hash": _hash(incomplete_body)})
        before = db.transactions.published_state().last_committed_lsn
        with pytest.raises(GrafxLedgerError) as failure:
            db.views.execute("both")
        assert failure.value.details["reason"] == "stale_dependency"
        assert db.transactions.published_state().last_committed_lsn == before
        assert db.views.create("both", query=QUERY, replace=True) == complete
        assert db.views.execute("both").rows == (("node", "edge", 1),)


def test_dependency_budget_counts_physical_tables_not_distinct_spellings(tmp_path, monkeypatch):
    from dataclasses import dataclass
    from types import SimpleNamespace

    from okto_grafx.domain.model.schema import TableDef
    import okto_grafx.views as views

    @dataclass
    class PlanNode:
        tables: tuple

        def walk(self):
            return (self,)

    # 65 different physical identities, but only 33 names. No fake graph storage:
    # this tests only detached plan-dependency accounting and its configured bound.
    definitions = tuple(TableDef(i + 1, f"T{i // 2}", "node" if i % 2 == 0 else "rel",
        (ColumnDef("id", ValueType.INT64),) if i % 2 == 0 else (
            ColumnDef("_from", ValueType.INT64, nullable=False),
            ColumnDef("_to", ValueType.INT64, nullable=False)),
        from_table=None if i % 2 == 0 else f"T{i // 2}",
        to_table=None if i % 2 == 0 else f"T{i // 2}") for i in range(65))
    with connect(tmp_path / "db") as db, db.begin("read") as tx:
        monkeypatch.setattr(views, "build_plan", lambda *a, **k: SimpleNamespace(
            root=PlanNode(definitions), columns=("x",)))
        with pytest.raises(GrafxConfigurationError) as failure:
            views._binding(tx, "RETURN 1 AS x", ())
        assert failure.value.details["field"] == "dependencies"
