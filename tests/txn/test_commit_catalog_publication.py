"""Native publication after explicit private activation, not a sidecar ACK."""

import pytest

from okto_grafx import connect
from okto_grafx.engine.commit_catalog_store import CommitCatalogStore
from okto_grafx.domain.txn.commit_identity import CommitId


def activate(db):
    db.ensure_identity_indexes()
    with db.begin("write") as tx:
        db._transactions.prepare_commit_catalog_activation(tx._context)
    return db._catalog.catalog.commit_catalog_activation


@pytest.mark.parametrize("compression", [False, True])
@pytest.mark.parametrize("segment_bytes", [4096, 65536])
def test_native_journal_tracks_data_schema_and_identity_floor_reopens(tmp_path, compression, segment_bytes):
    root = tmp_path / "db"
    with connect(root, page_size=512, wal_segment_bytes=segment_bytes, identity_lease_size=1) as db:
        db.ensure_identity_indexes()
        if compression:
            db.enable_wal_page_compression()
        activation = activate(db)
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
        for i in range(3):
            with db.begin("write") as tx:
                tx.execute("CREATE (:P {id:$i,name:$n})", {"i": i, "n": "x" * 1000})
        sequence = db._transactions.published_state().last_committed_lsn
        uuid = db._transactions._database_uuid
        store = CommitCatalogStore(db._storage.read_page, database_uuid=uuid, page_size=512)
        assert store.verify().last_sequence == sequence
        assert store.lookup(CommitId(uuid, sequence), read_lsn=sequence).identity.sequence == sequence
        assert store.read_head().activation_sequence == activation
        if segment_bytes == 4096:
            from okto_grafx.domain.wal.record import WalRecordType
            assert any(record.record_type == int(WalRecordType.SEGMENT_HEADER)
                       for record in db._wal.read_from(activation + 1))
        db.checkpoint()
    with connect(root, page_size=512, wal_segment_bytes=segment_bytes, identity_lease_size=1) as db:
        assert db.execute("MATCH (p:P) RETURN count(p)").rows == ((3,),)
        store = CommitCatalogStore(db._storage.read_page, database_uuid=uuid, page_size=512)
        assert store.verify().last_sequence == sequence
        with db.begin("write") as tx:
            tx.execute("MATCH (p:P {id:0}) SET p.name='after'")
        assert store.verify().last_sequence > sequence
