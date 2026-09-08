"""Logical import/fork mappings and offline physical restore keep distinct identities."""

import shutil

import pytest

from okto_grafx import CommitId, CommitMetadata, connect, prepare_commit_import
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxTransactionBudgetExceeded
from okto_grafx.domain.txn.commit_catalog import decode_commit_catalog_entry


def initialize(db):
    db.ensure_identity_indexes()
    db.enable_commit_history()
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")


def test_logical_transfer_mapping_is_atomic_and_survives_restore(tmp_path):
    with connect(tmp_path / "source", page_size=512) as source:
        initialize(source)
        with source.begin(metadata=CommitMetadata(actor="origin", attributes={"nested": [True, "é"]})) as tx:
            tx.execute("CREATE (:P {id:1})")
        source_id = CommitId(source.identity.database_uuid, tx.report.csn)
        entry = source.lookup_commit(source_id)
        # A versioned/checksummed logical envelope, not serialized WAL pages.
        exported = entry.encode()
        decoded = decode_commit_catalog_entry(exported)
        transfer = prepare_commit_import(decoded)
    with connect(tmp_path / "target", page_size=512) as target:
        initialize(target)
        assert target.identity.database_uuid != source_id.database_uuid
        with target.begin(metadata=transfer.metadata) as tx:
            tx.execute("CREATE (:P {id:1})")
        target_id = CommitId(target.identity.database_uuid, tx.report.csn)
        mapping = transfer.mapping(target.lookup_commit(target_id))
        assert mapping.source == source_id and mapping.target == target_id
        assert target.lookup_commit(target_id).metadata.attributes["source_attributes"]["nested"] == (True, "é")
        target.checkpoint()
    # Both original handles are closed. Restore of the same lineage is opened
    # only read-only here; it is not a second independent writable fork.
    shutil.copytree(tmp_path / "target", tmp_path / "restored")
    with connect(tmp_path / "restored", page_size=512, read_only=True) as restored:
        assert restored.identity.database_uuid == target_id.database_uuid
        assert transfer.mapping(restored.lookup_commit(target_id)) == mapping
        assert restored.verify().clean
        assert restored.execute("MATCH (p:P) RETURN count(p)").rows == ((1,),)


def test_transfer_budget_privacy_and_wrong_target_refusal(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        initialize(db)
        with db.begin(metadata=CommitMetadata(attributes={"value": "x" * 4000})) as tx:
            tx.execute("CREATE (:P {id:1})")
        entry = db.lookup_commit(CommitId(db.identity.database_uuid, tx.report.csn))
        from okto_grafx import MetadataLimits
        with pytest.raises(GrafxTransactionBudgetExceeded):
            prepare_commit_import(entry, limits=MetadataLimits(max_bytes=100))
        transfer = prepare_commit_import(entry, preserve_metadata=False)
        assert set(transfer.metadata.attributes) == {"grafx_source_commit"}
        assert transfer.metadata.actor is None
        with pytest.raises(GrafxConfigurationError):
            transfer.mapping(entry)
        with pytest.raises(GrafxConfigurationError):
            prepare_commit_import(entry, preserve_metadata=1)
