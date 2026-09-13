"""Typed decimal equality seeks prove exact canonical p/s before using durable keys."""

import pytest

from okto_grafx import connect, DecimalValue
from okto_grafx.errors import GrafxError
from okto_grafx.domain.query.plan import IndexSeek, plan_nodes


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("layout", ["hash", "sparse_hash", "posting_hash"])
def test_exact_typed_decimal_probes_use_index_not_scan_and_survive_updates(tmp_path, monkeypatch, codec, layout):
    path = tmp_path / "db"
    query = "MATCH (n:N) WHERE n.amount=$v RETURN n.id ORDER BY n.id"
    with connect(path, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,amount DECIMAL(12,3),PRIMARY KEY(id))")
            for index, text in enumerate(("1.23", "1.25", "0.1", "1"), 1):
                tx.execute("CREATE (:N {id:$id,amount:decimal($v,12,3)})", {"id": index, "v": text})
        db.create_index("by_amount", "N", ("amount",), layout=layout, bucket_count=4)
        assert any(isinstance(node, IndexSeek) for node in plan_nodes(db.explain(query)))
        def forbidden(*args, **kwargs):
            raise AssertionError("Typed DECIMAL equality must not fall back to a table scan")
        with monkeypatch.context() as patch:
            patch.setattr("okto_grafx.engine.query_engine._logical_node_rows_for_input", forbidden)
            for value, expected in ((DecimalValue(123, 3, 2), ((1,),)),
                                    (DecimalValue(12300, 5, 4), ((1,),)),
                                    (1.25, ((2,),)), (0.1, ()),
                                    (DecimalValue(1, 1, 1), ((3,),)), (1, ((4,),)),
                                    (True, ()), (float("nan"), ()), (float("inf"), ()), (10**15, ())):
                assert db.execute(query, {"v": value}).rows == expected
        with db.begin("read") as reader, connect(path, codec=codec) as other:
            assert reader.execute(query, {"v": 1.25}).rows == ((2,),)
            with other.begin() as writer:
                writer.execute("MATCH (n:N {id:2}) SET n.amount=decimal('2.5',3,1)")
            assert reader.execute(query, {"v": 1.25}).rows == ((2,),)
            assert reader.execute(query, {"v": 2.5}).rows == ()
        with db.begin() as tx:
            tx.execute("MATCH (n:N {id:1}) SET n.amount=decimal('2.5',3,1)")
            assert tx.execute(query, {"v": 2.5}).rows == ((1,), (2,))
        tx = db.begin()
        tx.execute("MATCH (n:N {id:1}) DETACH DELETE n")
        tx.rollback()
        assert not db.verify("all").findings
    with connect(path, codec=codec) as db:
        assert db.execute(query, {"v": 2.5}).rows == ((1,), (2,))
        assert db.execute(query, {"v": DecimalValue(100, 3, 2)}).rows == ((4,),)
        assert not db.verify("all").findings


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_decimal_primary_key_normalization_uniqueness_and_merge(tmp_path, codec):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(key DECIMAL(38,0),name STRING,PRIMARY KEY(key))")
            tx.execute("CREATE (:N {key:decimal('100000000000000000001',38,0),name:'first'})")
            with pytest.raises(GrafxError):
                tx.execute("CREATE (:N {key:decimal('100000000000000000001.0',38,1),name:'duplicate'})")
            tx.execute("MERGE (n:N {key:decimal('100000000000000000001.0',38,1)}) ON MATCH SET n.name='matched'")
            assert tx.execute("MATCH (n:N) RETURN count(n)").rows == ((1,),)
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (n:N) WHERE n.key=$v RETURN n.name", {"v": DecimalValue(1000000000000000000010, 38, 1)}).rows == (("matched",),)
        assert db.execute("MATCH (n:N) WHERE n.key=1e20 RETURN n.name").rows == ()
        assert not db.verify("all").findings


def test_decimal_probe_against_an_int64_pk_keeps_cross_family_scan_semantics(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1})")
        assert db.execute("MATCH (n:N) WHERE n.id=$v RETURN n.id", {"v": DecimalValue(100, 3, 2)}).rows == ((1,),)
        assert db.execute("MATCH (n:N) WHERE n.id=$v RETURN n.id", {"v": DecimalValue(101, 3, 2)}).rows == ()


@pytest.mark.parametrize("kind", ["ordered", "fulltext"])
def test_unsupported_index_family_refuses_before_publication(tmp_path, kind):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id STRING,amount DECIMAL(10,3),PRIMARY KEY(id))")
        before = db.transactions.published_state().last_committed_lsn
        definitions = db._catalog.catalog.index_definitions()
        with pytest.raises(GrafxError):
            if kind == "ordered":
                db.create_index("unsupported", "N", ("amount", "id"), layout="ordered")
            else:
                db.create_text_index("unsupported", "N", ("amount",), bucket_count=4)
        assert db.transactions.published_state().last_committed_lsn == before
        assert db._catalog.catalog.index_definitions() == definitions
        assert not db.verify("all").findings


def test_composite_decimal_key_and_parameterized_primary_key_memo_identity(tmp_path):
    from dataclasses import replace
    from okto_grafx.engine.query_engine import _primary_key_table_identity
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(a DECIMAL(10,2),b DECIMAL(12,3),PRIMARY KEY(a))")
            tx.execute("CREATE (:N {a:decimal('1.25',3,2),b:decimal('2.5',3,1)})")
        db.create_index("composite", "N", ("a", "b"), bucket_count=4)
        query = "MATCH (n:N) WHERE n.a=$a AND n.b=$b RETURN n.a,n.b"
        assert db.execute(query, {"a": 1.25, "b": DecimalValue(250, 3, 2)}).rows == ((DecimalValue(125, 10, 2), DecimalValue(2500, 12, 3)),)
        assert db.execute(query, {"a": 1.25, "b": 2.51}).rows == ()
        table = db._catalog.catalog.table("N")
        altered = replace(table, columns=(replace(table.columns[0], decimal_scale=3), table.columns[1]))
        assert _primary_key_table_identity(table, 0) != _primary_key_table_identity(altered, 0)
