"""Own implicit DDL adds index authority without replacing captured generations."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxIndexError
from okto_grafx.engine.query_engine import QueryEngine

QUERY = ("UNWIND $events AS event MATCH(y:Year {year:event.year}) "
         "MERGE(e:Event {id:event.id}) MERGE(y)<-[:IN]-(e) RETURN e.id AS x ORDER BY x")
EVENTS = {"events":[{"year":2016,"id":1},{"year":2016,"id":2}]}


def test_implicit_edge_builds_endpoint_index_and_repeated_merge_is_idempotent(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Year {year:2016})")
        with db.begin("write") as tx:
            assert tx.execute(QUERY, EVENTS).rows == ((1,),(2,))
            assert tx.execute(QUERY, EVENTS).rows == ((1,),(2,))
        assert db.verify("all").findings == ()
    with connect(path) as db:
        assert db.execute("MATCH(e:Event)-[r:IN]->(y:Year) RETURN e.id,y.year ORDER BY e.id").rows == ((1,2016),(2,2016))


def test_implicit_index_and_schema_rollback_after_late_failure(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Year {year:2016})")
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute(QUERY.replace("RETURN e.id AS x ORDER BY x", "RETURN 1/(e.id-2)"), EVENTS)
            assert not db.catalog.catalog.has_table("Event")
            assert tx.execute(QUERY, EVENTS).rows == ((1,),(2,))
        assert db.verify("all").findings == ()


def test_authority_refusal_during_implicit_ddl_is_not_bypassed(tmp_path, monkeypatch):
    original = QueryEngine._statement_index_authority
    def refuse_new(self, catalog, *, txn=None, statement=None):
        if statement is None and catalog.relationship_tables("IN"):
            raise GrafxIndexError("Injected new index authority refusal", field="index_authority")
        return original(self, catalog, txn=txn, statement=statement)
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Year {year:2016})")
        with db.begin("write") as tx:
            with monkeypatch.context() as patch:
                patch.setattr(QueryEngine, "_statement_index_authority", refuse_new)
                with pytest.raises(GrafxIndexError):
                    tx.execute(QUERY, EVENTS)
            assert tx.execute(QUERY, EVENTS).rows == ((1,),(2,))
        assert db.verify("all").findings == ()


def test_adoption_keeps_captured_stores_and_does_not_reopen_omitted_existing_authority():
    from types import SimpleNamespace
    from okto_grafx.engine.query_engine import _IndexAuthorityProjection, _adopt_implicit_schema
    def index(name):
        return SimpleNamespace(definition=SimpleNamespace(name=name,registry_key=name))
    captured, replacement, omitted, added = index("captured"),index("captured"),index("omitted"),index("new")
    prior = _IndexAuthorityProjection.build((captured,))
    fresh = _IndexAuthorityProjection.build((replacement,omitted,added))
    old_catalog = SimpleNamespace(
        has_index_definition=lambda name: name in {"captured","omitted"},
        index_definition=lambda name: SimpleNamespace(active_generation=lambda: 1))
    new_catalog = object()
    engine = SimpleNamespace(_working_catalog=lambda txn:new_catalog,
                             _statement_index_authority=lambda catalog,txn:fresh)
    endpoint_cache = {"captured":captured}
    context = SimpleNamespace(schema=lambda:old_catalog,txn=object(),index_authority=prior,
                              endpoint_identity_indexes=endpoint_cache, procedure_parent=None)
    _adopt_implicit_schema(engine, context)
    assert context.index_authority.named("captured") is captured
    assert context.index_authority.named("new") is added
    assert context.index_authority.named("omitted") is None
    assert context.index_authority.planning_indexes == prior.planning_indexes
    assert context.endpoint_identity_indexes is endpoint_cache
    assert context.catalog is new_catalog
