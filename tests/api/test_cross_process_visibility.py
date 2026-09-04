"""A long-lived participant must keep seeing what other processes commit (D1).

Found by C9's round-3 blind critic and routed to C2: `LocalStorageDevice` cached the descriptor of
``control/commit.state``, and another process's ``atomic_replace`` moved the directory entry from
under it. The cached handle kept reading the REPLACED file -- so a process that had once read the
published state never learned of anyone else's commits, answered stale rows for ever, and had every
commit of its own refused as a conflict with a world it could not see. Every multi-process test before
this one used a FRESH process for the read, which is the safe regime (LESSONS L23, L24).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxTransactionStateError
from okto_grafx.domain.index import IndexOperation, change_of
from okto_grafx.domain.wal.record import WalRecordType

_CHILD = r'''
import sys
sys.path.insert(0, sys.argv[2])
from okto_grafx import connect
db = connect(sys.argv[1])
for identity in range(2, 6):
    with db.begin("write") as txn:
        txn.execute(f"CREATE (:P {{id: {identity}}})")
print(db.transactions.published_lsn(), flush=True)
db.close()
'''

_VERIFY_CHILD = r'''
import sys
sys.path.insert(0, sys.argv[2])
from okto_grafx import connect
db = connect(sys.argv[1])
for batch in range(5):
    with db.begin("write") as txn:
        for identity in range(2 + batch * 10, 12 + batch * 10):
            txn.execute(f"CREATE (:P {{id: {identity}, name: 'child-{identity}'}})")
print(db.transactions.published_lsn(), flush=True)
db.close()
'''


def test_a_long_lived_process_sees_what_another_process_commits(tmp_path: Path) -> None:
    root = str(tmp_path / "db")
    source = str(Path(connect.__code__.co_filename).resolve().parents[1])
    parent = connect(root)
    try:
        with parent.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE (:P {id: 1})")
        # The parent has READ the published state once; its descriptor is now cached.
        assert sorted(parent.execute("MATCH (p:P) RETURN p.id").rows) == [(1,)]
        before = parent.transactions.published_lsn()

        child = subprocess.run(
            [sys.executable, "-c", _CHILD, root, source],
            capture_output=True, text=True, timeout=180,
        )
        assert child.returncode == 0, child.stderr[-500:]
        published_by_child = int(child.stdout.strip())
        assert published_by_child > before

        # The same handle, without reconnecting: the new rows and the new number.
        assert sorted(parent.execute("MATCH (p:P) RETURN p.id").rows) == [
            (1,), (2,), (3,), (4,), (5,),
        ]
        assert parent.transactions.published_lsn() == published_by_child

        # And it can still commit: its snapshot is current, so nothing conflicts.
        with parent.begin("write") as txn:
            txn.execute("CREATE (:P {id: 100})")
        assert sorted(parent.execute("MATCH (p:P) RETURN p.id").rows) == [
            (1,), (2,), (3,), (4,), (5,), (100,),
        ]
    finally:
        parent.close()
    fresh = connect(root)
    try:
        assert len(fresh.execute("MATCH (p:P) RETURN p.id").rows) == 6
        assert fresh.verify("all").findings == ()
    finally:
        fresh.close()


def test_verify_on_a_long_lived_handle_walks_the_latest_foreign_commit(
    tmp_path: Path,
) -> None:
    """Verification must refresh its logical walk to the published cross-process view.

    The page pass reads the device directly, but the record and index passes walk stores backed
    by this handle's buffer pool.  Warming those stores before another process commits used to
    leave ``verify("all")`` describing the old heap/index image while its page pass described the
    new device image.  A clean-but-incomplete report is not a valid integrity verdict.
    """
    root = str(tmp_path / "db")
    source = str(Path(connect.__code__.co_filename).resolve().parents[1])
    parent = connect(root)
    try:
        with parent.begin("write") as txn:
            txn.execute(
                "CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))"
            )
            txn.execute("CREATE (:P {id: 1, name: 'parent'})")

        # Populate this handle's heap frames before the foreign commits.  The next verification
        # must not reuse this one-row logical view for any of its passes.
        assert parent.execute("MATCH (p:P) RETURN p.id").rows == ((1,),)

        before = parent.transactions.published_lsn()
        child = subprocess.run(
            [sys.executable, "-c", _VERIFY_CHILD, root, source],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert child.returncode == 0, child.stderr[-500:]
        published_by_child = int(child.stdout.strip())
        assert published_by_child > before

        # This is intentionally the first operation on the parent after the child exits.  verify
        # itself owns the obligation to establish a fresh read view at the published LSN.
        live = parent.verify("all")
        assert parent.transactions.published_lsn() == published_by_child
    finally:
        parent.close()

    reopened = connect(root)
    try:
        cold = reopened.verify("all")
    finally:
        reopened.close()

    assert cold.clean is True
    assert cold.records_checked == 51
    assert cold.index_entries_checked == 51
    assert live.clean is True
    assert live.findings == ()
    assert live.records_checked == cold.records_checked
    assert live.index_entries_checked == cold.index_entries_checked


def test_a_writer_opened_before_foreign_ddl_adopts_its_index_before_commit(
    tmp_path: Path,
) -> None:
    """A foreign table cannot receive a heap-only commit from an old participant.

    The first handle opens an empty database, so its process-local index registry is empty.
    Another handle then publishes both a keyed table and its first row.  The old handle must
    adopt that durable index inside the commit section before materialising its own update and
    insert; otherwise its WAL contains heap pages but no index effects and a cold recovery can
    falsely certify the resulting short index as fresh.
    """
    root = tmp_path / "foreign-ddl-index"
    options = {"page_size": 512, "checkpoint_interval_records": 1_000_000}
    old = connect(root, **options)
    publisher = connect(root, **options)
    fresh = None
    try:
        assert old.indexes.indexes() == ()
        with publisher.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
            txn.execute("CREATE (:P {id: 1, name: 'before'})")

        before = publisher.transactions.published_lsn()
        with old.begin("write") as txn:
            txn.execute("MATCH (p:P) WHERE p.id = 1 SET p.name = 'after'")
            txn.execute("CREATE (:P {id: 2, name: 'inserted'})")

        index_records = tuple(
            record
            for record in old._wal.read_from(before + 1)
            if record.record_type == int(WalRecordType.INDEX_WRITE)
        )
        changes = tuple(change_of(record) for record in index_records)
        assert [change.index for change in changes] == ["pk_P", "pk_P", "pk_P"]
        assert sorted(change.operation for change in changes) == [
            IndexOperation.INSERT,
            IndexOperation.INSERT,
            IndexOperation.TOMBSTONE,
        ]
        assert not old.indexes.index("pk_P").stale

        expected = [(1, "after"), (2, "inserted")]
        assert sorted(old.execute("MATCH (p:P) RETURN p.id, p.name").rows) == expected
        assert old.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id, p.name").rows == (
            (1, "after"),
        )
        assert old.execute("MATCH (p:P) WHERE p.id = 2 RETURN p.id, p.name").rows == (
            (2, "inserted"),
        )
        assert old.verify("all").clean is True
        assert old.verify("all").findings == ()

        # Open while both participants remain live and before either performs a close-time
        # checkpoint.  This exercises recovery from the retained WAL, including INDEX_WRITE.
        fresh = connect(root, **options)
        assert fresh.stale_indexes == ()
        assert sorted(fresh.execute("MATCH (p:P) RETURN p.id, p.name").rows) == expected
        assert fresh.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id, p.name").rows == (
            (1, "after"),
        )
        assert fresh.execute("MATCH (p:P) WHERE p.id = 2 RETURN p.id, p.name").rows == (
            (2, "inserted"),
        )
        assert fresh.verify("all").clean is True
        assert fresh.verify("all").findings == ()
    finally:
        if fresh is not None:
            fresh.close()
        publisher.close()
        old.close()

    with connect(root, **options) as cold:
        assert cold.stale_indexes == ()
        assert sorted(cold.execute("MATCH (p:P) RETURN p.id, p.name").rows) == [
            (1, "after"),
            (2, "inserted"),
        ]
        assert cold.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id, p.name").rows == (
            (1, "after"),
        )
        assert cold.execute("MATCH (p:P) WHERE p.id = 2 RETURN p.id, p.name").rows == (
            (2, "inserted"),
        )
        assert cold.verify("all").clean is True
        assert cold.verify("all").findings == ()


def test_a_foreign_persistent_index_missing_after_sync_refuses_before_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final inventory proof fails closed if dynamic registration does not happen."""
    root = tmp_path / "foreign-ddl-index-invariant"
    options = {"page_size": 512, "checkpoint_interval_records": 1_000_000}
    old = connect(root, **options)
    publisher = connect(root, **options)
    pending = None
    try:
        with publisher.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
            txn.execute("CREATE (:P {id: 1, name: 'before'})")

        # The ordinary read boundary now also adopts foreign catalog authority.  Disable that
        # same callback before begin so this test still exercises the commit-time fail-closed
        # inventory proof rather than succeeding through the earlier safety door.
        monkeypatch.setattr(old._transactions, "_index_sync", lambda: ())
        pending = old.begin("write")
        pending.execute("MATCH (p:P) WHERE p.id = 1 SET p.name = 'never-published'")
        before_published = old.transactions.published_lsn()
        before_wal = old._wal.last_lsn

        with pytest.raises(GrafxTransactionStateError) as refused:
            pending.commit()
        assert refused.value.details["field"] == "index_registry"
        assert refused.value.details["indexes"] == ["pk_P"]
        assert old.transactions.published_lsn() == before_published
        assert old._wal.last_lsn == before_wal
        assert old.execute("MATCH (p:P) RETURN p.id, p.name").rows == ((1, "before"),)
    finally:
        if pending is not None and pending.active:
            pending.rollback()
        publisher.close()
        old.close()


def test_foreign_row_dml_does_not_rescan_the_index_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proved heap/index-only WAL delta keeps the read boundary O(changed effects)."""

    root = tmp_path / "foreign-row-no-index-sync"
    options = {"page_size": 512, "checkpoint_interval_records": 1_000_000}
    publisher = connect(root, **options)
    observer = None
    try:
        with publisher.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE (:P {id: 1})")
        observer = connect(root, **options)
        original_sync = observer._transactions._index_sync
        sync_calls = 0

        def counted_sync() -> object:
            nonlocal sync_calls
            sync_calls += 1
            assert original_sync is not None
            return original_sync()

        monkeypatch.setattr(observer._transactions, "_index_sync", counted_sync)
        # The first view remains conservative because a foreign DDL could have landed between
        # assembly and this statement.  When the persisted catalog image is unchanged, however,
        # that proof must not repeat the full index inventory/header-open pass from connect().
        assert observer.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id").rows == (
            (1,),
        )
        assert sync_calls == 0

        with publisher.begin("write") as txn:
            txn.execute("CREATE (:P {id: 2})")

        assert observer.execute("MATCH (p:P) WHERE p.id = 2 RETURN p.id").rows == (
            (2,),
        )
        assert sync_calls == 0

        # Checkpoint movement makes the bounded-WAL proof decline, but catalog bytes did not
        # change.  The authority-image comparison must avoid a full index inventory/open pass.
        publisher.checkpoint()
        assert observer.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id").rows == (
            (1,),
        )
        assert sync_calls == 0
    finally:
        if observer is not None:
            observer.close()
        publisher.close()


def test_a_row_commit_with_unchanged_authority_skips_the_registry_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit keeps its final inventory proof without repeating catalog adoption."""

    database = connect(
        tmp_path / "unchanged-authority-commit",
        checkpoint_interval_records=1_000_000,
    )
    pending = None
    try:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")

        pending = database.begin("write")
        pending.execute("CREATE (:P {id: 1})")
        original_sync = database._transactions._index_sync
        sync_calls = 0

        def counted_sync() -> object:
            nonlocal sync_calls
            sync_calls += 1
            assert original_sync is not None
            return original_sync()

        monkeypatch.setattr(database._transactions, "_index_sync", counted_sync)

        pending.commit()
        assert sync_calls == 0
        assert database.execute("MATCH (p:P) RETURN p.id").rows == ((1,),)
        assert database.verify("all").clean is True
    finally:
        if pending is not None and pending.active:
            pending.rollback()
        database.close()


def test_a_speculative_index_with_a_reused_table_id_never_indexes_foreign_rows(
    tmp_path: Path,
) -> None:
    """A local DDL loser and a foreign winner may allocate the same numeric table id.

    Index ownership is the complete catalog identity, not its reusable integer alone.  The
    speculative ``pk_Q`` must therefore remain available to its declaring transaction without
    receiving either observations or WAL effects for the durable foreign table ``P``.
    """
    root = tmp_path / "foreign-ddl-speculative-index"
    options = {"page_size": 512, "checkpoint_interval_records": 1_000_000}
    old = connect(root, **options)
    publisher = None
    fresh = None
    local_ddl = old.begin("write")
    try:
        local_ddl.execute("CREATE NODE TABLE Q(id INT64, PRIMARY KEY(id))")
        assert old._indexes.index("pk_Q").definition.table_id == 1

        publisher = connect(root, **options)
        with publisher.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE (:P {id: 1})")
        assert publisher.catalog.catalog.table("P").table_id == 1

        # Before the next statement, the raw registry still contains only speculative pk_Q,
        # which must be withheld from the public committed inventory even though Q and P reuse
        # table id 1.  The read boundary then adopts durable pk_P alongside (not over) pk_Q.
        assert tuple(index.name for index in old._indexes.indexes()) == ("pk_Q",)
        assert old.indexes.indexes() == ()
        assert old.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id").rows == ((1,),)
        assert tuple(index.name for index in old._indexes.indexes()) == ("pk_P", "pk_Q")
        assert tuple(index.name for index in old.indexes.indexes()) == ("pk_P",)
        coexistence = old.verify("all")
        assert coexistence.clean is True
        assert coexistence.findings == ()
        assert tuple(index.name for index in old._indexes.indexes()) == ("pk_P", "pk_Q")

        before = publisher.transactions.published_lsn()
        with old.begin("write") as txn:
            txn.execute("CREATE (:P {id: 2})")

        changes = tuple(
            change_of(record)
            for record in old._wal.read_from(before + 1)
            if record.record_type == int(WalRecordType.INDEX_WRITE)
        )
        assert [change.index for change in changes] == ["pk_P"]

        local_ddl.rollback()
        assert tuple(index.name for index in old.indexes.indexes()) == ("pk_P",)
        assert sorted(old.execute("MATCH (p:P) RETURN p.id").rows) == [(1,), (2,)]
        assert old.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id").rows == ((1,),)
        assert old.execute("MATCH (p:P) WHERE p.id = 2 RETURN p.id").rows == ((2,),)
        live = old.verify("all")
        assert live.clean is True
        assert live.findings == ()

        fresh = connect(root, **options)
        assert fresh.attached_indexes == ("pk_P",)
        assert fresh.stale_indexes == ()
        assert sorted(fresh.execute("MATCH (p:P) RETURN p.id").rows) == [(1,), (2,)]
        assert fresh.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id").rows == (
            (1,),
        )
        assert fresh.execute("MATCH (p:P) WHERE p.id = 2 RETURN p.id").rows == (
            (2,),
        )
        assert fresh.verify("all").clean is True
        assert fresh.verify("all").findings == ()
    finally:
        if local_ddl.active:
            local_ddl.rollback()
        if fresh is not None:
            fresh.close()
        if publisher is not None:
            publisher.close()
        old.close()

    with connect(root, **options) as cold:
        assert cold.attached_indexes == ("pk_P",)
        assert cold.stale_indexes == ()
        assert sorted(cold.execute("MATCH (p:P) RETURN p.id").rows) == [(1,), (2,)]
        assert cold.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id").rows == ((1,),)
        assert cold.execute("MATCH (p:P) WHERE p.id = 2 RETURN p.id").rows == ((2,),)
        assert cold.verify("all").clean is True
        assert cold.verify("all").findings == ()
