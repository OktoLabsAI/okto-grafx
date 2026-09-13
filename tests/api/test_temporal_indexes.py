"""Native temporal index keys, uniqueness, snapshots and definition-time refusals."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from okto_grafx import connect, DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue
from okto_grafx.domain.index.keys import index_key
from okto_grafx.domain.model.value import encode_values
from okto_grafx.domain.query.plan import IndexSeek, plan_nodes
from okto_grafx.errors import GrafxError
from tests.api.test_temporal_transfer import VALUES, TYPES

OTHER = (DateValue(1970, 1, 1), LocalTimeValue(0), TimeValue(LocalTimeValue(0), 0),
         LocalDateTimeValue(DateValue(1970, 1, 1), LocalTimeValue(0)),
         DateTimeValue.from_epoch_parts(0), DurationValue())
QUERY = "MATCH(n:N) WHERE n.v=$v RETURN n.id ORDER BY n.id"


def seek(db, value):
    assert any(isinstance(node, IndexSeek) for node in plan_nodes(db.explain(QUERY)))
    return db.execute(QUERY, {"v": value}).rows


@pytest.mark.parametrize("kind,old,new", zip(TYPES, VALUES, OTHER))
@pytest.mark.parametrize("layout", ["hash", "sparse_hash", "posting_hash"])
def test_temporal_exact_index_lifecycle_and_independent_reader(tmp_path, kind, old, new, layout):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute(f"CREATE NODE TABLE N(id INT64,v {kind},PRIMARY KEY(id))")
            tx.execute("CREATE(:N {id:1,v:$v}),(:N {id:2,v:$v}),(:N {id:3})", {"v": old})
        db.create_index("by_v", "N", ("v",), layout=layout, bucket_count=4)
        assert seek(db, old) == ((1,), (2,))
        assert seek(db, new) == ()
        with db.begin("read") as reader:
            assert reader.execute(QUERY, {"v": old}).rows == ((1,), (2,))
            with connect(tmp_path / "db") as writer, writer.begin() as tx:
                tx.execute("MATCH(n:N {id:2}) SET n.v=$v", {"v": new})
            assert reader.execute(QUERY, {"v": old}).rows == ((1,), (2,))
            assert reader.execute(QUERY, {"v": new}).rows == ()
        assert seek(db, old) == ((1,),)
        assert seek(db, new) == ((2,),)
        writer = db.begin()
        try:
            writer.execute("MATCH(n:N {id:1}) SET n.v=$v", {"v": new})
            assert writer.execute(QUERY, {"v": new}).rows == ((1,), (2,))
        finally:
            writer.rollback()
        assert seek(db, old) == ((1,),)
        with db.begin() as tx:
            tx.execute("MATCH(n:N {id:1}) DELETE n")
        assert seek(db, old) == ()
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:4,v:$v})", {"v": old})
        assert seek(db, old) == ((4,),)
        assert not db.verify("all").findings
        db.checkpoint()
    with connect(tmp_path / "db", read_only=True) as db:
        assert seek(db, old) == ((4,),)
        assert seek(db, new) == ((2,),)


@pytest.mark.parametrize("old,new", zip(VALUES, OTHER))
@pytest.mark.parametrize("layout", ["hash", "sparse_hash", "posting_hash"])
def test_any_temporal_property_index_refusal_keeps_scan_semantics(tmp_path, old, new, layout):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,v ANY,PRIMARY KEY(id))")
            tx.execute("CREATE(:N {id:1,v:$v}),(:N {id:2,v:'unrelated'})", {"v": old})
        before = db.transactions.published_state().last_committed_lsn
        definitions = db._catalog.catalog.index_definitions()
        with pytest.raises(GrafxError, match="ANY"):
            db.create_index("by_v", "N", ("v",), layout=layout, bucket_count=4)
        assert db.transactions.published_state().last_committed_lsn == before
        assert db._catalog.catalog.index_definitions() == definitions
        assert not any(isinstance(node, IndexSeek) for node in plan_nodes(db.explain(QUERY)))
        assert db.execute(QUERY, {"v": old}).rows == ((1,),)
        with db.begin() as tx:
            tx.execute("MATCH(n:N {id:1}) SET n.v=$v", {"v": new})
        assert db.execute(QUERY, {"v": old}).rows == ()
        assert db.execute(QUERY, {"v": new}).rows == ((1,),)
        assert not db.verify("all").findings


@pytest.mark.parametrize("kind,old,new", zip(TYPES, VALUES, OTHER))
@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_temporal_primary_key_uniqueness_merge_and_reopen(tmp_path, kind, old, new, codec):
    with connect(tmp_path / "db", codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute(f"CREATE NODE TABLE N(v {kind},id INT64,PRIMARY KEY(v))")
            tx.execute("CREATE(:N {v:$v,id:1})", {"v": old})
        assert seek(db, old) == ((1,),)
        with db.begin() as tx:
            tx.execute("CREATE(:N {v:$v,id:2})", {"v": new})
            with pytest.raises(GrafxError):
                tx.execute("CREATE(:N {v:$v,id:3})", {"v": old})
            assert tx.execute(QUERY, {"v": new}).rows == ((2,),)
            tx.execute("MERGE(n:N {v:$v}) SET n.id=4", {"v": old})
        assert seek(db, old) == ((4,),)
        assert seek(db, new) == ((2,),)
        assert not db.verify("all").findings
        db.checkpoint()
    with connect(tmp_path / "db", codec=codec, read_only=True) as db:
        assert seek(db, old) == ((4,),)
        assert seek(db, new) == ((2,),)


@pytest.mark.parametrize("kind", TYPES)
@pytest.mark.parametrize("index_kind", ["ordered", "fulltext"])
def test_unsupported_temporal_index_refuses_before_catalog_publication(tmp_path, kind, index_kind):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute(f"CREATE NODE TABLE N(id STRING,v {kind},PRIMARY KEY(id))")
        before = db.transactions.published_state().last_committed_lsn
        definitions = db._catalog.catalog.index_definitions()
        with pytest.raises(GrafxError):
            if index_kind == "ordered":
                db.create_index("unsupported", "N", ("v", "id"), layout="ordered")
            else:
                db.create_text_index("unsupported", "N", ("v",), bucket_count=4)
        assert db.transactions.published_state().last_committed_lsn == before
        assert db._catalog.catalog.index_definitions() == definitions
        assert not db.verify("all").findings


def test_temporal_key_bytes_retain_zone_offset_and_duration_components():
    pairs = (
        (DateTimeValue.from_epoch_parts(0, zone="UTC"), DateTimeValue.from_epoch_parts(0, zone="Etc/UTC")),
        (DateTimeValue.from_epoch_parts(0), DateTimeValue.from_epoch_parts(0, offset_seconds=3600)),
        (TimeValue(LocalTimeValue(0), 0), TimeValue(LocalTimeValue(3600 * 10**9), 3600)),
        (DurationValue(days=1), DurationValue(seconds=86400)),
    )
    for left, right in pairs:
        assert left != right
        assert index_key((left,), (0,)) != index_key((right,), (0,))


def composite(db, layout):
    db.ensure_identity_indexes()
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE N(id INT64," + ",".join(f"v{i} {kind}" for i, kind in enumerate(TYPES)) + ",PRIMARY KEY(id))")
        tx.execute("CREATE(:N {id:1," + ",".join(f"v{i}:$v{i}" for i in range(6)) + "})",
                   {f"v{i}": value for i, value in enumerate(VALUES)})
    db.create_index("composite", "N", tuple(f"v{i}" for i in range(6)), layout=layout, bucket_count=4)


def composite_seek(db, values):
    query = "MATCH(n:N) WHERE " + " AND ".join(f"n.v{i}=$v{i}" for i in range(6)) + " RETURN n.id"
    assert any(isinstance(node, IndexSeek) for node in plan_nodes(db.explain(query)))
    return db.execute(query, {f"v{i}": value for i, value in enumerate(values)}).rows


@pytest.mark.parametrize("layout", ["hash", "sparse_hash", "posting_hash"])
def test_composite_temporal_index_rehash_rebuild_and_reopen(tmp_path, layout):
    with connect(tmp_path / "db") as db:
        composite(db, layout)
        assert composite_seek(db, VALUES) == ((1,),)
        first = db.indexes.index("composite").active_nonce
        rehashed = db.rehash_index("composite", bucket_count=8)
        assert rehashed.active_nonce != first and rehashed.bucket_count == 8
        assert composite_seek(db, VALUES) == ((1,),)
        rebuilt = db.rebuild_index("composite")
        assert rebuilt.active_nonce != rehashed.active_nonce
        assert composite_seek(db, VALUES) == ((1,),)
        assert composite_seek(db, OTHER) == ()
        assert not db.verify("all").findings
        db.checkpoint()
    with connect(tmp_path / "db", read_only=True) as db:
        assert composite_seek(db, VALUES) == ((1,),)


@pytest.mark.parametrize("layout", ["hash", "sparse_hash", "posting_hash"])
@pytest.mark.parametrize("cut", ["before_commit", "before_apply"])
def test_temporal_composite_key_and_heap_recover_same_durable_outcome(tmp_path, layout, cut):
    with connect(tmp_path / "db") as db:
        composite(db, layout)
        db.checkpoint()
    code = """
import os, sys
from okto_grafx import Transaction, connect
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.domain.model.value import decode_values
path, cut, encoded = sys.argv[1:]
values, _ = decode_values(bytes.fromhex(encoded), 6)
with connect(path) as db:
    commit, apply = Transaction.commit, TransactionManager._apply_images
    active = None
    def interrupted_commit(tx):
        global active
        active = tx
        if cut == 'before_commit':
            os._exit(71)
        return commit(tx)
    def interrupted_apply(manager, images):
        if active is not None and active._context.state.value == 'committed':
            os._exit(73)
        return apply(manager, images)
    Transaction.commit = interrupted_commit
    TransactionManager._apply_images = interrupted_apply
    with db.begin() as tx:
        tx.execute('MATCH(n:N {id:1}) SET ' + ','.join(f'n.v{i}=$v{i}' for i in range(6)),
                   {f'v{i}': value for i, value in enumerate(values)})
"""
    proc = subprocess.run([sys.executable, "-c", code, str(tmp_path / "db"), cut, encode_values(OTHER).hex()],
                          env={**os.environ, "PYTHONPATH": str(Path("src").resolve())},
                          capture_output=True, timeout=60)
    assert proc.returncode == (71 if cut == "before_commit" else 73), proc.stderr.decode()
    expected, absent = (VALUES, OTHER) if cut == "before_commit" else (OTHER, VALUES)
    for _ in range(2):
        with connect(tmp_path / "db") as db:
            assert composite_seek(db, expected) == ((1,),)
            assert composite_seek(db, absent) == ()
            assert db.execute("MATCH(n:N) RETURN " + ",".join(f"n.v{i}" for i in range(6))).rows == (expected,)
            assert not db.verify("all").findings


@pytest.mark.parametrize("kind,value", zip(TYPES, VALUES))
def test_concurrent_temporal_primary_key_writers_do_not_publish_duplicates(tmp_path, kind, value):
    with connect(tmp_path / "db") as first:
        first.ensure_identity_indexes()
        with first.begin() as tx:
            tx.execute(f"CREATE NODE TABLE N(v {kind},id INT64,PRIMARY KEY(v))")
        with connect(tmp_path / "db") as second:
            a, b = first.begin(), second.begin()
            try:
                a.execute("CREATE(:N {v:$v,id:1})", {"v": value})
                b.execute("CREATE(:N {v:$v,id:2})", {"v": value})
                a.commit()
                with pytest.raises(GrafxError):
                    b.commit()
            finally:
                if a.active:
                    a.rollback()
                if b.active:
                    b.rollback()
        assert seek(first, value) == ((1,),)
        assert not first.verify("all").findings
