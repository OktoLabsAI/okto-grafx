"""Begin-time capture, retry and publication; metadata is not an idempotency key."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxUnsupportedOperation, GrafxWriteConflict
from okto_grafx.domain.txn.commit_identity import CommitId
from okto_grafx.domain.txn.commit_metadata import CommitMetadata
from okto_grafx.engine.commit_catalog_store import CommitCatalogStore


def activate(db):
    db.ensure_identity_indexes()
    with db.begin() as tx:
        db._transactions.prepare_commit_catalog_activation(tx._context)
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
    with db.begin() as tx:
        tx.execute("CREATE (:P {id:1, name:'old'})")


def begin(db, metadata):
    return db._public_transaction(db._transactions.begin("write", metadata=metadata))


def lookup(db, sequence):
    uuid = db._transactions._database_uuid
    return CommitCatalogStore(db._storage.read_page, database_uuid=uuid, page_size=512).lookup(
        CommitId(uuid, sequence), read_lsn=sequence,
    )


def test_capture_survives_caller_change_occ_retry_checkpoint_and_reopen(tmp_path):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        activate(db)
        metadata = CommitMetadata(actor="caller", attributes={"nested": [1, 2]})
        captured = metadata.canonical_bytes
        tx = begin(db, metadata)
        tx.execute("MATCH (p:P {id:1}) SET p.name='mine'")
        object.__setattr__(metadata, "_canonical", CommitMetadata(actor="changed").canonical_bytes)
        with connect(root, page_size=512) as other:
            with other.begin() as other_tx:
                other_tx.execute("MATCH (p:P {id:1}) SET p.name='foreign'")
        with pytest.raises(GrafxWriteConflict):
            tx.commit()
        tx = db.retry(tx)
        tx.execute("MATCH (p:P {id:1}) SET p.name='mine'")
        report = tx.commit()
        assert lookup(db, report.csn).metadata_bytes == captured
        assert db._transactions._commit_metadata == {}
        db.checkpoint()
    with connect(root, page_size=512) as db:
        assert lookup(db, report.csn).metadata_bytes == captured
        assert db.execute("MATCH (p:P) RETURN p.name").rows == (("mine",),)


@pytest.mark.parametrize("mode", ["read", "write"])
def test_bad_metadata_refuses_before_begin_io(tmp_path, monkeypatch, mode):
    with connect(tmp_path / "db", page_size=512) as db:
        def forbidden(*args, **kwargs):
            pytest.fail("metadata admission reached begin IO")
        monkeypatch.setattr(type(db._transactions), "_begin_in_section", forbidden)
        with pytest.raises(GrafxConfigurationError):
            db._transactions.begin(mode, metadata={"actor": "wrong type"})
        forged = CommitMetadata(actor="valid")
        object.__setattr__(forged, "_canonical", b"broken")
        with pytest.raises(GrafxConfigurationError):
            db._transactions.begin(mode, metadata=forged)


def test_no_write_and_rollback_do_not_publish_metadata(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        with pytest.raises(GrafxUnsupportedOperation):
            begin(db, CommitMetadata(actor="untracked"))
        activate(db)
        before = db._transactions.published_state().last_committed_lsn
        tx = begin(db, CommitMetadata(reason="no write"))
        assert not tx.commit().wrote
        tx = begin(db, CommitMetadata(reason="aborted"))
        tx.execute("MATCH (p:P) SET p.name='not persisted'")
        tx.rollback()
        assert db._transactions.published_state().last_committed_lsn == before
        assert db._transactions._commit_metadata == {}
