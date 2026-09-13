"""Native entity output, transaction identity and late-conversion rollback."""

import json

import pytest

from okto_grafx import EntityIdentity, NodeValue, RelationshipValue, connect
from okto_grafx.cli.output import jsonable
from okto_grafx.errors import GrafxPlanError


def schema(db):
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE N(id INT64, v INT64, PRIMARY KEY(id))")
        tx.execute("CREATE NODE TABLE Other(id INT64, v INT64, PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE R(FROM N TO N, weight INT64)")


def test_pending_identity_is_stable_across_aliases_nested_results_and_later_statements(tmp_path):
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin("write") as tx:
            created, nested = tx.execute("CREATE (n:N {id:1,v:7}) RETURN n,[n,{node:n}]").rows[0]
            assert type(created) is NodeValue
            assert nested[0] == nested[1]["node"] == created
            again = tx.execute("MATCH (n:N {id:1}) WITH n AS other RETURN other").rows[0][0]
            assert again == created
            assert created.identity.record_id is None and created.identity.provisional_id is not None
            assert created.provenance.pending and created.provenance.version_lsn is None
            changed = tx.execute("MATCH (n:N) SET n.v=8 RETURN n").rows[0][0]
            assert changed == created and changed.properties["v"] == 8
            assert created.properties["v"] == 7
        durable = db.execute("MATCH (n:N) RETURN n").rows[0][0]
        assert durable != created
        assert durable.identity.committed and not durable.provenance.pending
        assert durable.properties == {"id": 1, "v": 8}
        assert type(durable.identity) is EntityIdentity


def test_qualified_relationship_endpoints_work_before_and_after_commit(tmp_path):
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (a:N {id:1,v:2})-[r:R {weight:9}]->(b:N {id:2,v:3})")
            a, r, b = tx.execute("MATCH (a:N)-[r:R]->(b:N) RETURN a,r,b").rows[0]
            assert type(r) is RelationshipValue
            assert r.source == a.identity and r.target == b.identity
            assert r.properties == {"weight": 9}
            assert r.provenance.pending
        a, r, b = db.execute("MATCH (a:N)-[r:R]->(b:N) RETURN a,r,b").rows[0]
        assert r.source == a.identity and r.target == b.identity
        assert r.identity.committed and not r.provenance.pending
        assert r.label == "R"
        assert json.loads(json.dumps(jsonable(r))) == r.to_dict()


def test_create_relationship_return_uses_the_same_endpoint_identities():
    with connect(":memory:") as db:
        schema(db)
        with db.begin("write") as tx:
            a, r, b = tx.execute("CREATE (a:N {id:1,v:2})-[r:R {weight:9}]->(b:N {id:2,v:3}) RETURN a,r,b").rows[0]
            assert r.source == a.identity and r.target == b.identity
            again = tx.execute("MATCH (a:N)-[r:R]->(b:N) RETURN a,r,b").rows[0]
            assert again == (a, r, b)


def test_typed_polymorphic_optional_and_distinct_results_use_one_node_representation():
    with connect(":memory:") as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1,v:7}),(:Other {id:1,v:7})")
        typed = db.execute("MATCH (n:N) RETURN n").rows[0][0]
        across = [row[0] for row in db.execute("MATCH (n) RETURN n").rows]
        assert len(across) == len(set(across)) == 2
        assert all(type(n) is NodeValue for n in across)
        assert typed in across
        duplicated = db.execute("UNWIND [1,2] AS i MATCH (n) RETURN DISTINCT n").rows
        assert {row[0] for row in duplicated} == set(across)
        assert db.execute("OPTIONAL MATCH (n:N {id:99}) RETURN n").rows == ((None,),)


def test_cursor_returns_detached_entities_and_keeps_old_snapshot_while_writer_commits(tmp_path):
    with connect(tmp_path / "db") as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("UNWIND [1,2,3] AS i CREATE (:N {id:i,v:7})")
        with db.query("MATCH (n:N) RETURN n ORDER BY n.id").cursor(batch_size=1) as cursor:
            first = cursor.fetchmany()[0][0]
            with db.begin("write") as tx:
                tx.execute("MATCH (n:N) SET n.v=8")
            second = cursor.fetchmany()[0][0]
            assert first.properties["v"] == second.properties["v"] == 7
            assert first.provenance.read_lsn == second.provenance.read_lsn
        assert db.execute("MATCH (n:N) RETURN n ORDER BY n.id").rows[0][0].properties["v"] == 8
    assert first.properties == {"id": 1, "v": 7}
    assert second.properties == {"id": 2, "v": 7}


def test_independent_handles_do_not_collide_on_process_local_pending_tokens(tmp_path):
    path = tmp_path / "db"
    with connect(path) as first:
        schema(first)
        with connect(path) as second:
            a, b = first.begin("write"), second.begin("write")
            try:
                left = a.execute("CREATE (n:N {id:1,v:7}) RETURN n").rows[0][0]
                right = b.execute("CREATE (n:N {id:2,v:7}) RETURN n").rows[0][0]
                assert left.identity.database_uuid == right.identity.database_uuid
                assert left.identity.provisional_id != right.identity.provisional_id
                assert left != right
            finally:
                a.rollback()
                b.rollback()


def test_cancelled_insert_observation_does_not_become_another_insert_identity():
    with connect(":memory:") as db:
        schema(db)
        with db.begin("write") as tx:
            deleted = tx.execute("CREATE (n:N {id:1,v:7}) DELETE n RETURN n").rows[0][0]
            created = tx.execute("CREATE (n:N {id:1,v:7}) RETURN n").rows[0][0]
            assert deleted != created
            assert deleted.provenance.pending
        assert db.execute("MATCH (n:N) RETURN count(*) AS c").rows == ((1,),)


def test_late_materialization_failure_rolls_back_private_publication_and_allows_next_statement(tmp_path, monkeypatch):
    from okto_grafx.engine import query_engine
    path = tmp_path / "db"
    with connect(path) as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1,v:7})")

            def fail(*args, **kwargs):
                raise GrafxPlanError("injected entity conversion failure")

            with monkeypatch.context() as patch:
                patch.setattr(query_engine, "NodeValue", fail)
                with pytest.raises(GrafxPlanError, match="injected"):
                    tx.execute("UNWIND [2,3] AS i CREATE (n:N {id:i,v:8}) RETURN n")
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)
            created = tx.execute("CREATE (n:N {id:2,v:9}) RETURN n").rows[0][0]
            assert tx.execute("MATCH (n:N {id:2}) RETURN n").rows[0][0] == created
    with connect(path) as db:
        assert db.execute("MATCH (n:N) RETURN n.id,n.v ORDER BY n.id").rows == ((1, 7), (2, 9))


def test_native_entities_preserve_provenance_and_pending_identity_through_real_spill(tmp_path, monkeypatch):
    from okto_grafx.adapters.query_spill_local import LocalQuerySpillFactory
    opened = []
    original = LocalQuerySpillFactory.open

    def recording(factory, budget):
        opened.append(True)
        return original(factory, budget)

    monkeypatch.setattr(LocalQuerySpillFactory, "open", recording)
    with connect(tmp_path / "db", query_memory_budget_bytes=4096) as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("UNWIND range(1,25) AS i CREATE (:N {id:i,v:7})")
        with db.begin("write") as tx:
            plain = tx.execute("MATCH (n:N) RETURN n").rows
            ordered = tx.execute("MATCH (n:N) RETURN DISTINCT n ORDER BY n.id DESC").rows
            assert [row[0].properties["id"] for row in ordered] == list(range(25, 0, -1))
            assert {row[0] for row in ordered} == {row[0] for row in plain}
            assert {row[0].provenance for row in ordered} == {row[0].provenance for row in plain}
            tx.execute("MATCH (n:N) SET n.v=8")
            updated = tx.execute("MATCH (n:N) RETURN n ORDER BY n.id").rows
            assert all(n.provenance.pending and n.properties["v"] == 8 for (n,) in updated)
            created = tx.execute("CREATE (n:N {id:26,v:9}) RETURN n").rows[0][0]
            ordered = tx.execute("MATCH (n:N) RETURN n ORDER BY n.id DESC").rows
            assert ordered[0][0] == created
            assert ordered[0][0].provenance == created.provenance
        assert opened


def test_public_result_string_budget_failure_also_rolls_back_native_entity_writes(tmp_path):
    path = tmp_path / "db"
    with connect(path, max_query_value_characters=1024) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, text STRING, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            text = "x" * 1025
            with pytest.raises(GrafxPlanError):
                tx.execute(f"CREATE (n:N {{id:1,text:'{text}'}}) RETURN n")
            assert tx.execute("MATCH (n:N) RETURN count(*) AS c").rows == ((0,),)
            tx.execute("CREATE (:N {id:2,text:'safe'})")
    with connect(path) as db:
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((2,),)


def test_spill_cannot_claim_a_source_version_newer_than_the_read_snapshot(tmp_path, monkeypatch):
    from okto_grafx.engine import query_engine
    from okto_grafx.errors import GrafxCorruptionDetected
    original = query_engine._SpillRowCodec._detach

    def future(codec, value):
        encoded = original(codec, value)
        if isinstance(value, query_engine.RowBinding):
            return (*encoded[:7], query_engine._spill_pack_internal(codec._context.snapshot.read_lsn + 1), encoded[8])
        return encoded

    with connect(tmp_path / "db", query_memory_budget_bytes=4096) as db:
        schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1,v:7})")
        with monkeypatch.context() as patch:
            patch.setattr(query_engine._SpillRowCodec, "_detach", future)
            with pytest.raises(GrafxCorruptionDetected):
                db.execute("MATCH (n:N) RETURN n ORDER BY n.id")
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)


def test_public_result_annotations_expose_entities_without_widening_parameter_types():
    from typing import get_type_hints
    from okto_grafx import QueryCursor, QueryResult, QueryValue
    assert QueryValue is not None
    assert "NodeValue" in str(get_type_hints(QueryResult)["rows"])
    assert "RelationshipValue" in str(get_type_hints(QueryCursor.fetchone)["return"])
