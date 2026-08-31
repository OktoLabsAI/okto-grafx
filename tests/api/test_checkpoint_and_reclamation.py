"""The checkpoint door, and the log reclamation it drives (BR-10, CF-11).

Until this door existed, ``WalManager.recycle`` and ``recyclable_horizon`` had no caller anywhere
in ``src`` and the published checkpoint never moved: the log grew without bound, and recovery
replayed it from the first record every time. The tests here drive the public door and read the
DEVICE -- the segment files in ``wal/`` -- rather than any component's own report of itself.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxDeviceFull, GrafxError, GrafxUnsupportedOperation
from okto_grafx.domain.txn.commit_state import CommitState
from okto_grafx.engine.commit_state_store import CommitStateStore
from okto_grafx.engine.index_manager import IndexManager

SEGMENT_BYTES: int = 64 * 1024
"""Small enough that sixty one-row commits span many segments."""


def _segments(path: Path) -> list[str]:
    return sorted(os.path.basename(name) for name in glob.glob(str(path / "wal" / "*.wal")))


def _people(database: object, *, at_least: int = 0) -> int:
    rows = database.execute("MATCH (p:P) RETURN p.id").rows
    return sum(1 for (identity,) in rows if identity >= at_least)


def _schema(database: object) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")


def test_a_checkpoint_reclaims_the_segments_the_database_no_longer_needs(tmp_path: Path) -> None:
    root = tmp_path / "db"
    database = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        _schema(database)
        for identity in range(1, 61):
            with database.begin("write") as txn:
                txn.execute(f"CREATE (:P {{id: {identity}, name: 'n{identity}'}})")
        before = _segments(root)
        assert len(before) > 5, "the scenario needs a log that spans several segments"
        report = database.checkpoint()
        after = _segments(root)
        assert len(report.recycled) == len(before) - len(after)
        assert len(after) < len(before)
        assert set(after).isdisjoint(report.recycled)
        assert _people(database) == 60
    finally:
        database.close()
    reopened = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        assert _people(reopened) == 60
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()


def test_a_second_checkpoint_with_nothing_new_reclaims_nothing(tmp_path: Path) -> None:
    root = tmp_path / "db"
    database = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        _schema(database)
        for identity in range(1, 31):
            with database.begin("write") as txn:
                txn.execute(f"CREATE (:P {{id: {identity}, name: 'n'}})")
        database.checkpoint()
        held = _segments(root)
        second = database.checkpoint()
        assert second.recycled == ()
        assert _segments(root) == held
    finally:
        database.close()


def test_a_read_only_database_cannot_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "db"
    database = connect(str(root))
    _schema(database)
    database.checkpoint()
    database.close()
    reader = connect(str(root), read_only=True)
    try:
        with pytest.raises(GrafxUnsupportedOperation) as refusal:
            reader.checkpoint()
        assert refusal.value.details["field"] == "read_only"
    finally:
        reader.close()


def test_a_long_lived_checkpointer_adopts_foreign_schema_before_logical_redo(
    tmp_path: Path,
) -> None:
    """A checkpoint can replay an index introduced after its own handle opened.

    Participant A starts with an empty catalog. Participant B then commits both the DDL that
    declares a primary-key index and a row whose WAL contains a logical write for that index.
    Replaying only the pages in A is insufficient: logical redo then names an index A has never
    registered and refuses the checkpoint. The catalog must be adopted and its indexes
    synchronized after catalog-page redo and before logical-index redo, just as startup recovery
    does.
    """
    root = tmp_path / "db"
    checkpointer = connect(str(root), page_size=512, wal_segment_bytes=SEGMENT_BYTES)
    writer = connect(str(root), page_size=512, wal_segment_bytes=SEGMENT_BYTES)
    try:
        with writer.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
        with writer.begin("write") as txn:
            txn.execute("CREATE (:P {id: 7, name: 'foreign'})")

        assert checkpointer.attached_indexes == ()
        checkpointer.checkpoint()

        assert tuple(table.name for table in checkpointer.catalog.catalog.tables()) == ("P",)
        assert checkpointer.indexes.index("pk_P").name == "pk_P"
        assert checkpointer.attached_indexes == ("pk_P",)
        assert checkpointer.execute("MATCH (p:P) RETURN p.id").rows == ((7,),)
        assert checkpointer.verify("all").findings == ()
    finally:
        writer.close()
        checkpointer.close()


def test_checkpoint_index_inventory_is_serialized_with_a_post_barrier_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public checkpoint postlude cannot straddle a recovery-required latch."""
    database = connect(str(tmp_path / "db"))
    inventory_entered = threading.Event()
    release_inventory = threading.Event()
    checkpoint_done = threading.Event()
    commit_done = threading.Event()
    checkpoint_results: list[object] = []
    checkpoint_failures: list[BaseException] = []
    commit_failures: list[BaseException] = []
    try:
        _schema(database)
        database.checkpoint()
        transaction = database.begin("write")
        transaction.execute("CREATE (:P {id: 7, name: 'durable'})")

        original_publish = CommitStateStore.publish
        original_open = IndexManager.open

        def fail_commit_publication(
            store: CommitStateStore, state: CommitState
        ) -> None:
            if threading.current_thread().name == "latching-commit":
                raise GrafxDeviceFull(
                    "The test refuses publication after the commit barrier.",
                    file="control/commit.state",
                )
            original_publish(store, state)

        def pause_checkpoint_inventory(
            manager: IndexManager,
            published_lsn: int,
            *,
            persist_stale: bool = True,
            allow_ahead: bool = False,
        ) -> tuple[object, ...]:
            if threading.current_thread().name == "checkpoint-postlude":
                inventory_entered.set()
                if not release_inventory.wait(timeout=5.0):
                    raise AssertionError("the test did not release checkpoint inventory")
            return original_open(
                manager,
                published_lsn,
                persist_stale=persist_stale,
                allow_ahead=allow_ahead,
            )

        def run_checkpoint() -> None:
            try:
                checkpoint_results.append(database.checkpoint())
            except BaseException as failure:
                checkpoint_failures.append(failure)
            finally:
                checkpoint_done.set()

        def run_commit() -> None:
            try:
                transaction.commit()
            except BaseException as failure:
                commit_failures.append(failure)
            finally:
                commit_done.set()

        monkeypatch.setattr(CommitStateStore, "publish", fail_commit_publication)
        monkeypatch.setattr(IndexManager, "open", pause_checkpoint_inventory)

        checkpoint_thread = threading.Thread(
            target=run_checkpoint, name="checkpoint-postlude"
        )
        checkpoint_thread.start()
        assert inventory_entered.wait(timeout=5.0)

        commit_thread = threading.Thread(target=run_commit, name="latching-commit")
        commit_thread.start()
        try:
            # IndexManager.open is after TransactionManager.checkpoint returned. Holding the same
            # participant section here must nevertheless keep commit from reaching its barrier.
            assert not commit_done.wait(timeout=0.2)
            # WAL and transaction publication observations now join this same section. Asking
            # for either here would correctly wait behind the paused checkpoint instead of
            # reading a straddled cached snapshot; commit_done is the non-blocking evidence.
        finally:
            release_inventory.set()

        checkpoint_thread.join(timeout=5.0)
        commit_thread.join(timeout=5.0)
        assert checkpoint_done.is_set() and commit_done.is_set()
        assert checkpoint_failures == []
        assert len(checkpoint_results) == 1
        assert len(commit_failures) == 1
        assert isinstance(commit_failures[0], GrafxError)
        assert commit_failures[0].details["committed"] is True  # type: ignore[union-attr]
        assert database.transactions.recovery_required is True
    finally:
        release_inventory.set()
        database.close()


_OTHER_PROCESS = r'''
import sys, time
sys.path.insert(0, sys.argv[2])
from okto_grafx import connect
db = connect(sys.argv[1], wal_segment_bytes=65536)
for identity in range(200, 230):
    with db.begin("write") as txn:
        txn.execute(f"CREATE (:P {{id: {identity}, name: 'b'}})")
# CE-2 keeps one deferred reader registration per participant. This process has no open
# transaction now, so a due explicit tick advances its standing pin to the published floor and
# lets the other participant exercise REDO+recycle immediately rather than waiting the 15 s TTL.
db._transactions.refresh_due_readers(
    time.monotonic() + db._transactions.refresh_interval
)
print("COMMITTED", flush=True)
time.sleep(60)   # hold the pages in memory; the test kills this process before it flushes
'''


def test_a_checkpoint_never_reclaims_a_page_another_process_has_not_flushed(
    tmp_path: Path,
) -> None:
    """The reason the checkpoint redoes the log onto the device before it publishes.

    The commit protocol applies a commit's page images to the committing process's OWN pool and
    deliberately does not put them on the platter (section 8.5 step 6). So another participant's
    committed pages may exist only in the log and in that participant's memory. A checkpoint
    that flushed only its own pool and then recycled the segments would destroy the only durable
    copy of those pages; if that participant then crashed, its acknowledged commits would be
    gone -- loss an ordinary caller reaches, with every commit having said ``durable=True``.

    So: process B commits thirty rows and holds them unflushed; process A checkpoints and
    recycles; B is killed without ever flushing or closing; a cold reopen must still find all
    thirty. This test reads the outcome from a third process's view of the device.
    """
    root = tmp_path / "db"
    source = str(Path(connect.__code__.co_filename).resolve().parents[1])
    database = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    _schema(database)
    database.close()

    other = subprocess.Popen(
        [sys.executable, "-c", _OTHER_PROCESS, str(root), source],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert other.stdout is not None
        deadline = time.monotonic() + 120
        line = ""
        while time.monotonic() < deadline and not line:
            line = other.stdout.readline().strip()
        assert line == "COMMITTED", f"the other process did not commit: {line!r}"

        checkpointer = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
        try:
            assert _people(checkpointer, at_least=200) == 30
            report = checkpointer.checkpoint()
            assert report.recycled, "the scenario needs the checkpoint to actually reclaim"
        finally:
            checkpointer.close()
    finally:
        other.kill()
        other.wait(timeout=30)

    reopened = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        assert _people(reopened, at_least=200) == 30
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()
