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
from okto_grafx.domain.errors import (
    GrafxDeviceFull,
    GrafxError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.page import PageType
from okto_grafx.domain.txn.commit_state import CommitState
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.commit_redo import CommitRedo
from okto_grafx.engine.commit_state_store import CommitStateStore
from okto_grafx.engine.index_manager import IndexManager

SEGMENT_BYTES: int = 64 * 1024
"""Small enough that sixty one-row commits span many segments."""


def _segments(path: Path) -> list[str]:
    return sorted(
        os.path.basename(name) for name in glob.glob(str(path / "wal" / "*.wal"))
    )


def _people(database: object, *, at_least: int = 0) -> int:
    rows = database.execute("MATCH (p:P) RETURN p.id").rows
    return sum(1 for (identity,) in rows if identity >= at_least)


def _schema(database: object) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")


def _seed_local_checkpoint_prefix(database: object) -> None:
    """Leave one ordinary local update above a checkpoint-based applied-prefix seed."""
    _schema(database)
    with database.begin("write") as txn:
        txn.execute("CREATE (:P {id: 1, name: 'before'})")
    database.checkpoint()
    assert database._transactions._local_applied_prefix is not None, type(
        database._transactions._index_manager
    )
    with database.begin("write") as txn:
        txn.execute("MATCH (p:P {id: 1}) SET p.name = 'after'")


def test_checkpoint_validates_but_does_not_reapply_its_exact_local_dml_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkpoint-based local witness removes only redundant physical/logical dispatch."""
    root = tmp_path / "db"
    database = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    preflighted: list[int] = []
    applied: list[int] = []
    real_preflight = CommitRedo.preflight
    real_apply = CommitRedo.apply
    try:
        _seed_local_checkpoint_prefix(database)
        prefix = database._transactions._local_applied_prefix
        assert prefix is not None
        assert prefix.applied_through_lsn == database.transactions.published_lsn()

        def observe_preflight(
            redo: CommitRedo, replay: object, *args: object, **kwargs: object
        ) -> object:
            preflighted.append(len(replay.effects))
            return real_preflight(redo, replay, *args, **kwargs)

        def observe_apply(
            redo: CommitRedo, replay: object, *args: object, **kwargs: object
        ) -> object:
            applied.append(len(replay.effects))
            return real_apply(redo, replay, *args, **kwargs)

        monkeypatch.setattr(CommitRedo, "preflight", observe_preflight)
        monkeypatch.setattr(CommitRedo, "apply", observe_apply)

        database.checkpoint()

        assert any(count > 0 for count in preflighted), (
            "the shortcut must retain complete payload preflight"
        )
        assert sum(count > 0 for count in preflighted) == 1, (
            "the complete strict preflight already validates every index effect"
        )
        assert not any(count > 0 for count in applied), (
            "the already-applied local prefix must not be dispatched again"
        )
        assert database.execute("MATCH (p:P {id: 1}) RETURN p.name").rows == (
            ("after",),
        )
        assert database.verify("all").findings == ()
    finally:
        database.close()

    reopened = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        assert reopened.execute("MATCH (p:P {id: 1}) RETURN p.name").rows == (
            ("after",),
        )
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()


def test_identity_refill_extends_the_exact_local_checkpoint_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local CN-1 subcommit remains eligible after its flushed row commit follows it."""
    root = tmp_path / "db"
    database = connect(
        str(root),
        wal_segment_bytes=SEGMENT_BYTES,
        checkpoint_interval_records=1_000_000,
    )
    nonempty_applies: list[int] = []
    real_apply = CommitRedo.apply
    try:
        _schema(database)
        with database.begin("write") as txn:
            txn.execute("CREATE (:P {id: 1, name: 'seed'})")
        database.checkpoint()

        # More than the default 64-record identity lease forces the private CN-1 floor
        # reservation before this one public transaction can materialise all rows.
        with database.begin("write") as txn:
            txn.executemany(
                "CREATE (:P {id: $id, name: $name})",
                (
                    {"id": identity, "name": f"row-{identity}"}
                    for identity in range(2, 82)
                ),
            )

        prefix = database._transactions._local_applied_prefix
        assert prefix is not None
        assert prefix.applied_through_lsn == database.transactions.published_lsn()

        local_redo = database._transactions._commit_redo

        def observe_apply(
            redo: CommitRedo, replay: object, *args: object, **kwargs: object
        ) -> object:
            if redo is local_redo and replay.effects:
                nonempty_applies.append(len(replay.effects))
            return real_apply(redo, replay, *args, **kwargs)

        monkeypatch.setattr(CommitRedo, "apply", observe_apply)
        database.checkpoint()

        assert nonempty_applies == []
        assert len(database.execute("MATCH (p:P) RETURN p.id").rows) == 81
        assert database.verify("all").findings == ()
    finally:
        database.close()

    reopened = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        assert len(reopened.execute("MATCH (p:P) RETURN p.id").rows) == 81
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()


def test_foreign_commit_revokes_the_local_checkpoint_replay_shortcut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign suffix takes the unchanged complete replay path."""
    root = tmp_path / "db"
    local = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    foreign = None
    nonempty_applies: list[int] = []
    real_apply = CommitRedo.apply
    try:
        _seed_local_checkpoint_prefix(local)
        foreign = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
        with foreign.begin("write") as txn:
            txn.execute("MATCH (p:P {id: 1}) SET p.name = 'foreign'")

        local_redo = local._transactions._commit_redo

        def observe_apply(
            redo: CommitRedo, replay: object, *args: object, **kwargs: object
        ) -> object:
            if redo is local_redo and replay.effects:
                nonempty_applies.append(len(replay.effects))
            return real_apply(redo, replay, *args, **kwargs)

        monkeypatch.setattr(CommitRedo, "apply", observe_apply)
        local.checkpoint()

        assert nonempty_applies
        assert local.execute("MATCH (p:P {id: 1}) RETURN p.name").rows == (
            ("foreign",),
        )
        assert local.verify("all").findings == ()
    finally:
        if foreign is not None:
            foreign.close()
        local.close()


def test_catalog_commit_revokes_the_local_checkpoint_replay_shortcut(
    tmp_path: Path,
) -> None:
    """DDL cannot inherit a DML-only physical-application witness."""
    database = connect(str(tmp_path / "db"), wal_segment_bytes=SEGMENT_BYTES)
    try:
        _seed_local_checkpoint_prefix(database)
        assert database._transactions._local_applied_prefix is not None

        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Q(id INT64, PRIMARY KEY(id))")

        assert database._transactions._local_applied_prefix is None
        database.checkpoint()
        assert database.verify("all").findings == ()
    finally:
        database.close()


def test_failed_data_barrier_revokes_the_local_checkpoint_replay_shortcut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed checkpoint retry always returns to canonical redo."""
    database = connect(str(tmp_path / "db"), wal_segment_bytes=SEGMENT_BYTES)
    original_barrier = BufferPool.durability_barrier
    refused = False
    try:
        _seed_local_checkpoint_prefix(database)
        assert database._transactions._local_applied_prefix is not None

        def fail_first_barrier(pool: BufferPool, file: str | None = None) -> None:
            nonlocal refused
            if pool is database._pool and not refused:
                refused = True
                raise GrafxDeviceFull("The checkpoint data barrier was refused.", file=file)
            original_barrier(pool, file)

        monkeypatch.setattr(BufferPool, "durability_barrier", fail_first_barrier)
        with pytest.raises(GrafxDeviceFull):
            database.checkpoint()

        assert refused
        assert database._transactions._local_applied_prefix is None
    finally:
        database.close()


def test_local_applied_prefix_never_replaces_missing_wal_lineage(tmp_path: Path) -> None:
    """Even an exact local witness refuses before publishing over a missing segment."""
    root = tmp_path / "db"
    database = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        _schema(database)
        with database.begin("write") as txn:
            txn.execute("CREATE (:P {id: 1, name: 'seed'})")
        database.checkpoint()
        for revision in range(12):
            with database.begin("write") as txn:
                txn.execute(
                    "MATCH (p:P {id: 1}) SET p.name = $name",
                    {"name": f"revision-{revision}"},
                )
        assert database._transactions._local_applied_prefix is not None
        segments = _segments(root)
        assert len(segments) >= 3
        victim = root / "wal" / segments[1]
        state_path = root / "control" / "commit.state"
        state_before = state_path.read_bytes()

        victim.unlink()
        with pytest.raises(GrafxError):
            database.checkpoint()

        assert state_path.read_bytes() == state_before
        assert database.transactions.recovery_required is True
    finally:
        database.close()


def test_a_checkpoint_reclaims_the_segments_the_database_no_longer_needs(
    tmp_path: Path,
) -> None:
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


@pytest.mark.parametrize("retain_lease", (False, True), ids=("per-call", "retained"))
def test_a_foreign_writer_commits_during_the_checkpoint_data_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retain_lease: bool,
) -> None:
    """Phase B holds neither COMMIT_SECTION nor the writer lease.

    The foreign commit U is newer than the immutable target T barriered by the checkpoint. Phase
    C must preserve U as the published tail, publish only T as the checkpoint, and open indexes
    against U. Running the retained-lease variant proves phase A forces a turnover rather than
    silently carrying its cached guard across the barrier.
    """
    root = tmp_path / "db"
    checkpointer = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    writer = None
    barrier_entered = threading.Event()
    release_barrier = threading.Event()
    checkpoint_done = threading.Event()
    checkpoint_failures: list[BaseException] = []
    original_barrier = BufferPool.durability_barrier
    paused = False
    try:
        _schema(checkpointer)
        with checkpointer.begin("write") as txn:
            txn.execute("CREATE (:P {id: 1, name: 'before'})")
        target = checkpointer.transactions.published_state().last_committed_lsn
        # Open after the schema exists so this test isolates the checkpoint split. A handle
        # opened before a foreign DDL currently has a separate registry-refresh defect: it can
        # write the heap without staging the index effect. That defect is real, but it is not a
        # valid source of an INDEX_WRITE for the phase-B concurrency scenario exercised here.
        writer = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
        checkpointer._transactions._retain_lease = retain_lease

        def pause_first_checkpoint_barrier(
            pool: BufferPool, file: str | None = None
        ) -> None:
            nonlocal paused
            if pool is checkpointer._pool and not paused:
                paused = True
                barrier_entered.set()
                if not release_barrier.wait(timeout=5.0):
                    raise AssertionError("the test did not release checkpoint phase B")
            original_barrier(pool, file)

        def run_checkpoint() -> None:
            try:
                checkpointer.checkpoint()
            except BaseException as failure:  # pragma: no cover - asserted below
                checkpoint_failures.append(failure)
            finally:
                checkpoint_done.set()

        monkeypatch.setattr(
            BufferPool, "durability_barrier", pause_first_checkpoint_barrier
        )
        checkpoint_thread = threading.Thread(
            target=run_checkpoint, name="concurrent-data-barrier"
        )
        checkpoint_thread.start()
        assert barrier_entered.wait(timeout=5.0)

        with writer.begin("write") as txn:
            txn.execute("MATCH (p:P {id: 1}) SET p.name = 'during-barrier'")
        current = writer.transactions.published_state().last_committed_lsn
        assert current > target
        assert writer.execute("MATCH (p:P) RETURN p.id, p.name").rows == (
            (1, "during-barrier"),
        )
        assert writer.execute("MATCH (p:P {id: 1}) RETURN p.name").rows == (
            ("during-barrier",),
        )
        assert not checkpoint_done.is_set(), (
            "the checkpoint must still be inside phase B"
        )

        release_barrier.set()
        checkpoint_thread.join(timeout=10.0)
        assert checkpoint_done.is_set()
        assert checkpoint_failures == []
        published = checkpointer.transactions.published_state()
        assert published.last_committed_lsn == current
        assert published.last_csn == current
        assert published.checkpoint_lsn == target
        assert checkpointer.execute("MATCH (p:P) RETURN p.id, p.name").rows == (
            (1, "during-barrier"),
        )
        assert checkpointer.stale_indexes == ()
        assert checkpointer._indexes.index("pk_P").built_through_lsn >= current
        assert checkpointer.verify("all").findings == ()
    finally:
        release_barrier.set()
        if writer is not None:
            writer.close()
        checkpointer.close()

    reopened = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        assert reopened.execute("MATCH (p:P) RETURN p.id, p.name").rows == (
            (1, "during-barrier"),
        )
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()


def test_a_checkpoint_finishing_after_a_newer_checkpoint_never_regresses_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow target T preserves a checkpoint of U published while T was in phase B."""
    root = tmp_path / "db"
    slow = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    faster = None
    barrier_entered = threading.Event()
    release_barrier = threading.Event()
    slow_done = threading.Event()
    slow_failures: list[BaseException] = []
    original_barrier = BufferPool.durability_barrier
    paused = False
    try:
        _schema(slow)
        with slow.begin("write") as txn:
            txn.execute("CREATE (:P {id: 1, name: 'target-t'})")
        target = slow.transactions.published_state().last_committed_lsn
        faster = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)

        def pause_slow_barrier(pool: BufferPool, file: str | None = None) -> None:
            nonlocal paused
            if pool is slow._pool and not paused:
                paused = True
                barrier_entered.set()
                if not release_barrier.wait(timeout=5.0):
                    raise AssertionError("the test did not release the slow checkpoint")
            original_barrier(pool, file)

        def run_slow_checkpoint() -> None:
            try:
                slow.checkpoint()
            except BaseException as failure:  # pragma: no cover - asserted below
                slow_failures.append(failure)
            finally:
                slow_done.set()

        monkeypatch.setattr(BufferPool, "durability_barrier", pause_slow_barrier)
        slow_thread = threading.Thread(
            target=run_slow_checkpoint, name="slow-target-t-checkpoint"
        )
        slow_thread.start()
        assert barrier_entered.wait(timeout=5.0)

        with faster.begin("write") as txn:
            txn.execute("CREATE (:P {id: 2, name: 'target-u'})")
        newer = faster.transactions.published_state().last_committed_lsn
        assert newer > target
        faster.checkpoint()
        assert faster.transactions.published_state().checkpoint_lsn == newer
        assert not slow_done.is_set()

        release_barrier.set()
        slow_thread.join(timeout=10.0)
        assert slow_done.is_set()
        assert slow_failures == []
        published = slow.transactions.published_state()
        assert published.last_committed_lsn == newer
        assert published.last_csn == newer
        assert published.checkpoint_lsn == newer
        assert slow.execute("MATCH (p:P) RETURN p.id").rows == ((1,), (2,))
        assert slow.verify("all").findings == ()
    finally:
        release_barrier.set()
        if faster is not None:
            faster.close()
        slow.close()

    reopened = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        assert reopened.execute("MATCH (p:P) RETURN p.id").rows == ((1,), (2,))
        assert reopened.transactions.published_state().checkpoint_lsn == newer
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()


def test_a_checkpoint_adopts_ddl_already_retired_by_a_newer_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty phase-C redo still synchronizes indexes introduced during phase B."""
    root = tmp_path / "db"
    slow = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    newer = None
    barrier_entered = threading.Event()
    release_barrier = threading.Event()
    slow_done = threading.Event()
    slow_failures: list[BaseException] = []
    original_barrier = BufferPool.durability_barrier
    paused = False
    try:
        assert slow.attached_indexes == ()

        def pause_slow_barrier(pool: BufferPool, file: str | None = None) -> None:
            nonlocal paused
            if pool is slow._pool and not paused:
                paused = True
                barrier_entered.set()
                if not release_barrier.wait(timeout=5.0):
                    raise AssertionError("the test did not release the slow checkpoint")
            original_barrier(pool, file)

        def run_slow_checkpoint() -> None:
            try:
                slow.checkpoint()
            except BaseException as failure:  # pragma: no cover - asserted below
                slow_failures.append(failure)
            finally:
                slow_done.set()

        monkeypatch.setattr(BufferPool, "durability_barrier", pause_slow_barrier)
        slow_thread = threading.Thread(
            target=run_slow_checkpoint, name="slow-pre-ddl-checkpoint"
        )
        slow_thread.start()
        assert barrier_entered.wait(timeout=5.0)

        newer = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
        _schema(newer)
        with newer.begin("write") as txn:
            txn.execute("CREATE (:P {id: 7, name: 'foreign-ddl'})")
        newer.checkpoint()
        newest_state = newer.transactions.published_state()
        assert newest_state.checkpoint_lsn == newest_state.last_committed_lsn
        assert not slow_done.is_set()

        release_barrier.set()
        slow_thread.join(timeout=10.0)
        assert slow_done.is_set()
        assert slow_failures == []
        assert slow.transactions.published_state() == newest_state
        assert slow.attached_indexes == ("pk_P",)
        assert slow.execute("MATCH (p:P {id: 7}) RETURN p.name").rows == (
            ("foreign-ddl",),
        )
        assert slow.verify("all").findings == ()
    finally:
        release_barrier.set()
        if newer is not None:
            newer.close()
        slow.close()

    reopened = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        assert reopened.attached_indexes == ("pk_P",)
        assert reopened.execute("MATCH (p:P {id: 7}) RETURN p.name").rows == (
            ("foreign-ddl",),
        )
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()


def test_a_checkpoint_with_a_live_local_snapshot_keeps_the_monolithic_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The split cannot expose a reader pin that phase B is unable to refresh."""
    root = tmp_path / "db"
    checkpointer = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    writer = None
    reader = None
    writer_txn = None
    barrier_entered = threading.Event()
    release_barrier = threading.Event()
    checkpoint_done = threading.Event()
    commit_done = threading.Event()
    checkpoint_failures: list[BaseException] = []
    commit_failures: list[BaseException] = []
    original_barrier = BufferPool.durability_barrier
    paused = False
    try:
        _schema(checkpointer)
        checkpointer.checkpoint()
        writer = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
        reader = checkpointer.begin("read")
        writer_txn = writer.begin("write")
        writer_txn.execute("CREATE (:P {id: 9, name: 'waited'})")

        def pause_checkpoint_barrier(pool: BufferPool, file: str | None = None) -> None:
            nonlocal paused
            if pool is checkpointer._pool and not paused:
                paused = True
                barrier_entered.set()
                if not release_barrier.wait(timeout=5.0):
                    raise AssertionError(
                        "the test did not release the monolithic checkpoint"
                    )
            original_barrier(pool, file)

        def run_checkpoint() -> None:
            try:
                checkpointer.checkpoint()
            except BaseException as failure:  # pragma: no cover - asserted below
                checkpoint_failures.append(failure)
            finally:
                checkpoint_done.set()

        def run_commit() -> None:
            try:
                writer_txn.commit()
            except BaseException as failure:  # pragma: no cover - asserted below
                commit_failures.append(failure)
            finally:
                commit_done.set()

        monkeypatch.setattr(BufferPool, "durability_barrier", pause_checkpoint_barrier)
        checkpoint_thread = threading.Thread(
            target=run_checkpoint, name="checkpoint-with-live-snapshot"
        )
        checkpoint_thread.start()
        assert barrier_entered.wait(timeout=5.0)

        commit_thread = threading.Thread(target=run_commit, name="foreign-writer")
        commit_thread.start()
        assert not commit_done.wait(timeout=0.2), (
            "a checkpoint with a live local snapshot released its cross-process fence"
        )

        release_barrier.set()
        checkpoint_thread.join(timeout=10.0)
        commit_thread.join(timeout=10.0)
        assert checkpoint_done.is_set() and commit_done.is_set()
        assert checkpoint_failures == []
        assert commit_failures == []
        reader.rollback()
        reader = None
        writer_txn = None
        assert checkpointer.execute("MATCH (p:P {id: 9}) RETURN p.name").rows == (
            ("waited",),
        )
        assert checkpointer.verify("all").findings == ()
    finally:
        release_barrier.set()
        if reader is not None and reader.active:
            reader.rollback()
        if writer_txn is not None and writer_txn.active:
            writer_txn.rollback()
        if writer is not None:
            writer.close()
        checkpointer.close()


def test_a_split_checkpoint_barriers_every_existing_paged_file_it_flushed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future paged extension cannot silently fall outside phase B's named inventory."""
    database = connect(str(tmp_path / "db"), wal_segment_bytes=SEGMENT_BYTES)
    custom_file = "extension/custom.dat"
    barrier_files: list[str | None] = []
    original_barrier = BufferPool.durability_barrier
    try:
        _schema(database)
        database._storage.create(custom_file, exclusive=True)
        page = database._pool.allocate(custom_file, int(PageType.FREE))
        database._pool.unpin(custom_file, page.page_index, dirty=True, page=page)

        def record_barrier(pool: BufferPool, file: str | None = None) -> None:
            if pool is database._pool:
                barrier_files.append(file)
            original_barrier(pool, file)

        monkeypatch.setattr(BufferPool, "durability_barrier", record_barrier)
        database.checkpoint()

        assert barrier_files.count(custom_file) == 1
        assert None not in barrier_files
        assert {"heap.dat", "catalog.dat", custom_file}.issubset(barrier_files)
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

        assert tuple(table.name for table in checkpointer.catalog.catalog.tables()) == (
            "P",
        )
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
            store: CommitStateStore,
            state: CommitState,
            *,
            previous: CommitState,
            previous_was_damaged: bool = False,
        ) -> None:
            if threading.current_thread().name == "latching-commit":
                raise GrafxDeviceFull(
                    "The test refuses publication after the commit barrier.",
                    file="control/commit.state",
                )
            original_publish(
                store,
                state,
                previous=previous,
                previous_was_damaged=previous_was_damaged,
            )

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
                    raise AssertionError(
                        "the test did not release checkpoint inventory"
                    )
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
            # IndexManager.open now remains inside TransactionManager's checkpoint fence. The
            # outer participant section independently keeps this same-handle commit from reaching
            # its barrier while the public inventory is still being settled.
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


@pytest.mark.parametrize("operation", ("checkpoint", "recover"))
def test_maintenance_index_inventory_fences_a_foreign_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Published position and index certificates are one cross-process photograph.

    The old checkpoint/recovery postlude evaluated ``published_lsn()`` before entering
    ``IndexManager.open``. A foreign commit in that exact gap advanced page 0 beyond the captured
    number, so the postlude persisted ``INDEX_FLAG_STALE`` on a healthy index. A participant-local
    section cannot close that gap because the writer below owns a different participant.
    """
    root = tmp_path / operation
    maintainer = connect(str(root))
    writer = None
    inventory_entered = threading.Event()
    release_inventory = threading.Event()
    maintenance_done = threading.Event()
    commit_done = threading.Event()
    maintenance_failures: list[BaseException] = []
    commit_failures: list[BaseException] = []
    try:
        _schema(maintainer)
        maintainer.checkpoint()
        writer = connect(str(root))
        transaction = writer.begin("write")
        transaction.execute("CREATE (:P {id: 7, name: 'foreign'})")

        original_open = IndexManager.open
        maintained_indexes = maintainer._indexes

        def pause_stable_inventory(
            manager: IndexManager,
            published_lsn: int,
            *,
            persist_stale: bool = True,
            allow_ahead: bool = False,
        ) -> tuple[object, ...]:
            if (
                manager is maintained_indexes
                and threading.current_thread().name == "stable-maintenance-inventory"
            ):
                inventory_entered.set()
                if not release_inventory.wait(timeout=5.0):
                    raise AssertionError(
                        "the test did not release maintenance inventory"
                    )
            return original_open(
                manager,
                published_lsn,
                persist_stale=persist_stale,
                allow_ahead=allow_ahead,
            )

        def run_maintenance() -> None:
            try:
                getattr(maintainer, operation)()
            except BaseException as failure:
                maintenance_failures.append(failure)
            finally:
                maintenance_done.set()

        def run_commit() -> None:
            try:
                transaction.commit()
            except BaseException as failure:
                commit_failures.append(failure)
            finally:
                commit_done.set()

        monkeypatch.setattr(IndexManager, "open", pause_stable_inventory)
        maintenance_thread = threading.Thread(
            target=run_maintenance, name="stable-maintenance-inventory"
        )
        commit_thread = threading.Thread(
            target=run_commit, name="foreign-inventory-commit"
        )
        maintenance_thread.start()
        assert inventory_entered.wait(timeout=5.0)
        commit_thread.start()
        try:
            assert not commit_done.wait(timeout=0.2), (
                "a foreign commit crossed the stable index-inventory photograph"
            )
        finally:
            release_inventory.set()

        maintenance_thread.join(timeout=5.0)
        commit_thread.join(timeout=5.0)
        assert maintenance_done.is_set() and commit_done.is_set()
        assert maintenance_failures == []
        assert commit_failures == []
        assert maintainer.stale_indexes == ()
        assert maintainer.indexes.index("pk_P").stale is False
        assert _people(maintainer) == 1
        assert maintainer.verify("all").findings == ()
    finally:
        release_inventory.set()
        if writer is not None:
            writer.close()
        maintainer.close()

    reopened = connect(str(root))
    try:
        assert reopened.stale_indexes == ()
        assert _people(reopened) == 1
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()


def test_startup_index_inventory_fences_a_foreign_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connect cannot compare a captured LSN with an index header a writer moves later."""
    root = tmp_path / "startup"
    seeded = connect(str(root))
    try:
        _schema(seeded)
        seeded.checkpoint()
    finally:
        seeded.close()

    writer = connect(str(root))
    transaction = writer.begin("write")
    transaction.execute("CREATE (:P {id: 8, name: 'foreign'})")
    inventory_entered = threading.Event()
    release_inventory = threading.Event()
    connect_done = threading.Event()
    commit_done = threading.Event()
    connected: list[object] = []
    connect_failures: list[BaseException] = []
    commit_failures: list[BaseException] = []
    original_open = IndexManager.open

    def pause_startup_inventory(
        manager: IndexManager,
        published_lsn: int,
        *,
        persist_stale: bool = True,
        allow_ahead: bool = False,
    ) -> tuple[object, ...]:
        if threading.current_thread().name == "stable-startup-inventory":
            inventory_entered.set()
            if not release_inventory.wait(timeout=5.0):
                raise AssertionError("the test did not release startup inventory")
        return original_open(
            manager,
            published_lsn,
            persist_stale=persist_stale,
            allow_ahead=allow_ahead,
        )

    def run_connect() -> None:
        try:
            connected.append(connect(str(root)))
        except BaseException as failure:
            connect_failures.append(failure)
        finally:
            connect_done.set()

    def run_commit() -> None:
        try:
            transaction.commit()
        except BaseException as failure:
            commit_failures.append(failure)
        finally:
            commit_done.set()

    monkeypatch.setattr(IndexManager, "open", pause_startup_inventory)
    connect_thread = threading.Thread(
        target=run_connect, name="stable-startup-inventory"
    )
    commit_thread = threading.Thread(target=run_commit, name="foreign-startup-commit")
    try:
        connect_thread.start()
        assert inventory_entered.wait(timeout=5.0)
        commit_thread.start()
        assert not commit_done.wait(timeout=0.2), (
            "a foreign commit crossed the startup index-inventory photograph"
        )
    finally:
        release_inventory.set()
        connect_thread.join(timeout=5.0)
        commit_thread.join(timeout=5.0)

    try:
        assert connect_done.is_set() and commit_done.is_set()
        assert connect_failures == []
        assert commit_failures == []
        assert len(connected) == 1
        database = connected[0]
        assert database.stale_indexes == ()  # type: ignore[attr-defined]
        assert _people(database) == 1
        assert database.verify("all").findings == ()  # type: ignore[attr-defined]
    finally:
        for database in connected:
            database.close()  # type: ignore[attr-defined]
        writer.close()

    reopened = connect(str(root))
    try:
        assert reopened.stale_indexes == ()
        assert _people(reopened) == 1
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()


_OTHER_PROCESS = r"""
import sys, time
sys.path.insert(0, sys.argv[2])
from okto_grafx import connect
db = connect(
    sys.argv[1],
    wal_segment_bytes=65536,
    descriptor_revalidation=sys.argv[3],
)
for identity in range(215, 230):
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
"""


_CHECKPOINT_CRASH_PROCESS = r"""
import os, sys
sys.path.insert(0, sys.argv[2])
from okto_grafx import connect
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.wal_manager import WalManager

db = connect(sys.argv[1], wal_segment_bytes=65536)
phase = sys.argv[3]
if phase == "before_data_barrier":
    original = BufferPool.durability_barrier
    def crash_before(pool, file=None):
        if pool is db._pool:
            os._exit(71)
        return original(pool, file)
    BufferPool.durability_barrier = crash_before
elif phase == "after_data_barrier":
    original = BufferPool.durability_barrier
    expected = 2 + len(db._indexes.indexes())
    observed = 0
    def crash_after(pool, file=None):
        global observed
        result = original(pool, file)
        if pool is db._pool:
            observed += 1
            if observed == expected:
                os._exit(72)
        return result
    BufferPool.durability_barrier = crash_after
elif phase == "after_publish":
    original = WalManager.recycle
    def crash_before_recycle(manager, *args, **kwargs):
        if manager is db._wal:
            os._exit(73)
        return original(manager, *args, **kwargs)
    WalManager.recycle = crash_before_recycle
else:
    os._exit(98)

db.checkpoint()
os._exit(99)
"""


@pytest.mark.parametrize(
    ("phase", "exit_code", "checkpoint_published"),
    (
        ("before_data_barrier", 71, False),
        ("after_data_barrier", 72, False),
        ("after_publish", 73, True),
    ),
)
def test_checkpoint_crash_windows_preserve_the_proved_prefix(
    tmp_path: Path,
    phase: str,
    exit_code: int,
    checkpoint_published: bool,
) -> None:
    """A process death exposes either the old checkpoint or the completely proved target."""
    root = tmp_path / phase
    source = str(Path(connect.__code__.co_filename).resolve().parents[1])
    seeded = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        _schema(seeded)
        seeded.checkpoint()
        previous = seeded.transactions.published_state().checkpoint_lsn
        for identity in range(1, 9):
            with seeded.begin("write") as txn:
                txn.execute(f"CREATE (:P {{id: {identity}, name: 'kept'}})")
        target = seeded.transactions.published_state().last_committed_lsn
        assert target > previous
    finally:
        seeded.close()

    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHECKPOINT_CRASH_PROCESS,
            str(root),
            source,
            phase,
        ],
        check=False,
        timeout=120,
    )
    assert crashed.returncode == exit_code

    reopened = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        state = reopened.transactions.published_state()
        assert state.last_committed_lsn == target
        assert state.last_csn == target
        assert state.checkpoint_lsn == (target if checkpoint_published else previous)
        assert _people(reopened) == 8
        assert reopened.execute("MATCH (p:P {id: 8}) RETURN p.name").rows == (
            ("kept",),
        )
        assert reopened.verify("all").findings == ()
        reopened.checkpoint()
    finally:
        reopened.close()

    cold = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        assert _people(cold) == 8
        assert cold.verify("all").findings == ()
    finally:
        cold.close()


@pytest.mark.parametrize("descriptor_revalidation", ["strict", "generation"])
def test_a_checkpoint_never_reclaims_a_page_another_process_has_not_flushed(
    tmp_path: Path, descriptor_revalidation: str
) -> None:
    """The reason the checkpoint redoes the log onto the device before it publishes.

    The commit protocol applies a commit's page images to the committing process's OWN pool and
    deliberately does not put them on the platter (section 8.5 step 6). So another participant's
    committed pages may exist only in the log and in that participant's memory. A checkpoint
    that flushed only its own pool and then recycled the segments would destroy the only durable
    copy of those pages; if that participant then crashed, its acknowledged commits would be
    gone -- loss an ordinary caller reaches, with every commit having said ``durable=True``.

    So: process A commits fifteen rows, a reader pins that snapshot, and process B commits fifteen
    newer rows and holds them unflushed. Process C checkpoints and recycles only below the reader
    horizon; B is killed without ever flushing or closing; a cold reopen must still find all
    thirty. This test reads the outcome from a fourth participant's view of the device.
    """
    root = tmp_path / "db"
    source = str(Path(connect.__code__.co_filename).resolve().parents[1])
    database = connect(
        str(root),
        wal_segment_bytes=SEGMENT_BYTES,
        descriptor_revalidation=descriptor_revalidation,
    )
    try:
        _schema(database)
        for identity in range(200, 215):
            with database.begin("write") as txn:
                txn.execute(f"CREATE (:P {{id: {identity}, name: 'a'}})")
    finally:
        database.close()

    reader_database = connect(
        str(root),
        wal_segment_bytes=SEGMENT_BYTES,
        descriptor_revalidation=descriptor_revalidation,
    )
    reader = reader_database.begin("read")
    snapshot = reader.snapshot.read_lsn
    assert len(reader.execute("MATCH (p:P) RETURN p.id").rows) == 15

    other = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _OTHER_PROCESS,
            str(root),
            source,
            descriptor_revalidation,
        ],
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

        checkpointer = connect(
            str(root),
            wal_segment_bytes=SEGMENT_BYTES,
            descriptor_revalidation=descriptor_revalidation,
        )
        try:
            assert _people(checkpointer, at_least=200) == 30
            protected = {
                segment.name
                for segment in checkpointer.wal.segments()
                if segment.last_lsn > snapshot
            }
            assert protected, "the scenario needs WAL newer than the reader snapshot"
            report = checkpointer.checkpoint()
            assert report.recycled, (
                "the scenario needs the checkpoint to actually reclaim"
            )
            assert report.reader_present is True
            assert report.horizon_lsn == snapshot
            assert protected <= set(report.retained)
            assert len(reader.execute("MATCH (p:P) RETURN p.id").rows) == 15
        finally:
            checkpointer.close()
    finally:
        other.kill()
        other.wait(timeout=30)
        if reader.active:
            reader.rollback()
        reader_database.close()

    reopened = connect(
        str(root),
        wal_segment_bytes=SEGMENT_BYTES,
        descriptor_revalidation=descriptor_revalidation,
    )
    try:
        assert _people(reopened, at_least=200) == 30
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()
