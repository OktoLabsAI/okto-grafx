"""Faults in real first-journal publication recover whole data/history outcomes."""

import pytest
from okto_grafx import CommitMetadata, CommitId

from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice, SimulatedCrash
from okto_grafx.engine.commit_catalog_store import CommitCatalogStore
from test_vacuum_recovery import open_database


def prepared():
    memory = MemoryStorageDevice(page_size=512)
    fault = FaultInjectingStorageDevice(memory, seed=20260908)
    db = open_database(fault, namespace=memory)
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
        tx.execute("CREATE (:P {id:1,name:'old'})")
    db.ensure_identity_indexes()
    with db.begin("write") as tx:
        db._transactions.prepare_commit_catalog_activation(tx._context)
    db.checkpoint()
    return memory, fault, db


def operation(db, metadata=None):
    with db.begin("write", metadata=metadata) as tx:
        tx.execute("MATCH (p:P {id:1}) SET p.name='new'")


@pytest.mark.parametrize("metadata", [None, CommitMetadata(actor="fault-test", attributes={"case": [1, "é"]})])
def test_first_journal_publication_crash_cuts(metadata):
    memory, fault, db = prepared()
    try:
        points = fault.enumerate_write_points(lambda _device: operation(db, metadata))
        targets = [p for p in points if
                   (p.method == "write_page" and p.file in {"commits.dir", "commits.dat", "control/commit.state"})
                   or (p.method in {"append_log", "durable_barrier"} and str(p.file).startswith("wal/"))]
        assert any(p.file == "commits.dat" for p in targets)
        assert any(p.file == "commits.dir" for p in targets)
    finally:
        db.close()
        memory.close()
    for point in targets:
        for moment in ("before", "after"):
            memory, fault, db = prepared()
            try:
                fault.clear_trail()
                fault.crash_at(point.call_index, moment=moment)
                with pytest.raises(SimulatedCrash):
                    operation(db, metadata)
                fault.disarm()
                with open_database(fault, namespace=memory) as recovered:
                    rows = recovered.execute("MATCH (p:P) RETURN p.name").rows
                    assert rows in {(("old",),), (("new",),)}
                    sequence = recovered._transactions.published_state().last_committed_lsn
                    activation = recovered._catalog.catalog.commit_catalog_activation
                    if rows == (("new",),):
                        store = CommitCatalogStore(fault.read_page,
                            database_uuid=recovered._transactions._database_uuid, page_size=512)
                        assert store.verify().last_sequence == sequence > activation
                        assert store.read_head().entry_count == 1
                        identity = CommitId(recovered.identity.database_uuid, sequence)
                        entry = recovered.lookup_commit(identity)
                        assert entry.metadata == metadata
                    else:
                        assert sequence == activation
                    recovered.checkpoint()
                with open_database(fault, namespace=memory) as again:
                    assert again.execute("MATCH (p:P) RETURN p.name").rows == rows
                    if rows == (("new",),):
                        assert again.lookup_commit(identity).metadata == metadata
            finally:
                memory.close()
