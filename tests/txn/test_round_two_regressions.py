"""The four blocking defects of the C5 round-2 blind review, each pinned by the shape that found it.

Every test here reads the outcome from the DEVICE or from a participant that was not the one
doing the writing -- a fresh stack over the same directory, the log itself, a second participant.
A commit's own report of itself is never the assertion (LESSONS L16, L23).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.domain.errors import GrafxError, GrafxWriteConflict
from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.txn import Snapshot, WalRecordType
from okto_grafx.engine.index_manager import IndexManager, ProximityIndex
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.verifier import Verifier
from okto_grafx.engine.wal_manager import WalManager
from shared_device import SharedDirectoryDevice
from txn_support import DEFAULT_PAGE_SIZE, ManualClock, Stack, build_stack

HEAP = "heap.dat"


def _table(table_id: int = 1, name: str = "person") -> TableDef:
    return TableDef(
        table_id=table_id,
        name=name,
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="label", type=ValueType.STRING),
        ),
        primary_key="id",
        from_table=None,
        to_table=None,
    )


def _registered(stack: Stack, definition: TableDef) -> TableDef:
    stack.catalog.catalog.add_table(definition)
    stack.catalog.save()
    return definition


def _real_wal(segment_bytes: int):
    def factory(device: object, clock: object, metrics: object) -> WalManager:
        manager = WalManager(
            device,  # type: ignore[arg-type]
            clock,  # type: ignore[arg-type]
            metrics,  # type: ignore[arg-type]
            segment_bytes=segment_bytes,
            descriptor="hash-v1;partitions_per_table=8",
        )
        manager.open()
        return manager

    return factory


def _participant(
    root: Path, owner: str, clock: ManualClock, *, segment_bytes: int = 1 << 20
) -> Stack:
    return build_stack(
        root,
        storage=SharedDirectoryDevice(root, page_size=DEFAULT_PAGE_SIZE),
        owner_id=owner,
        clock=clock,
        wal_factory=_real_wal(segment_bytes),
        commit_lock_timeout=5.0,
    )


def _insert(stack: Stack, table: TableDef, identity: int, label: str = "x") -> object:
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (identity, label))
    txn.note_write(stack.manager.partition_of(table.table_id, str(identity).encode()))
    return stack.manager.commit(txn)


def _rows(stack: Stack, table: TableDef) -> list[tuple[object, ...]]:
    lsn = stack.manager.published_lsn()
    return sorted(version.values for _ref, version in stack.heap.scan(table, Snapshot(lsn)))


# --- B1: a refusal on the second intent of a batch abandons the first ---------------------------


def test_a_refusal_inside_the_row_batch_leaves_no_phantom_row(tmp_path: Path) -> None:
    """Two intents; the heap refuses the second. The first must not survive as a row.

    ``_write_rows`` wrote intents one by one and a refusal inside it returned nothing, so the
    rows already written were never abandoned: they sat in the shared pool under the provisional
    stamp, which the next commit of anyone flushed and published -- a row no transaction ever
    committed, visible to every participant.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    writer = _participant(root, "a", clock)
    table = _registered(writer, _table())

    doomed = writer.manager.begin("write")
    doomed.stage_row_insert(table, (1, "ada"))
    doomed.stage_row_insert(table, ("not-an-int", "bad"))
    doomed.note_write(writer.manager.partition_of(table.table_id, b"1"))
    doomed.note_write(writer.manager.partition_of(table.table_id, b"2"))
    with pytest.raises(GrafxError):
        writer.manager.commit(doomed)
    writer.manager.rollback(doomed)

    _insert(writer, table, 2, "bob")  # an unrelated commit: it flushes the heap and publishes

    other = _participant(root, "b", clock)
    assert _rows(other, table) == [(2, "bob")]
    assert _rows(writer, table) == [(2, "bob")]


# --- B2: an abandoned attempt never writes its stale frame over another participant's commit ----


def test_an_abandoned_attempt_never_overwrites_what_another_participant_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late page-half refusal in P2 cannot make a durable P1 row disappear.

    The refused attempt's pages stayed DIRTY in P2's pool holding the page as it looked during the
    attempt. The next read view, or P2's next flush, wrote them back -- over the page P1 had since
    committed and put on the device. P1's acknowledged row was gone and ``verify()`` agreed.

    A historical write to a page materialised from the current durable view is deliberately no
    longer a conflict: that fresh image already contains the write.  Inject exactly one refusal
    on the second OCC pass instead, after row materialisation, so this remains a direct regression
    for abandonment rather than depending on the obsolete false-conflict behaviour.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    p1 = _participant(root, "p1", clock)
    table = _registered(p1, _table())
    _insert(p1, table, 1, "p1")
    p2 = _participant(root, "p2", clock)

    refused = p2.manager.begin("write")
    refused.stage_row_insert(table, (3, "p2-x"))
    refused.note_write(p2.manager.partition_of(table.table_id, b"3"))
    _insert(p1, table, 2, "p1")  # lands on the same page after P2's snapshot

    original_find_conflict = TransactionManager._find_conflict
    injected = [False]

    def refuse_materialized_page(self, txn, **kwargs):  # noqa: ANN001, ANN003, ANN202
        conflict = original_find_conflict(self, txn, **kwargs)
        if (
            self is not p2.manager
            or injected[0]
            or conflict is not None
            or kwargs.get("baseline_lsn") is None
            or not txn.row_refs
        ):
            return conflict
        interested = kwargs.get("interested_partitions")
        assert interested, "the hostile pass was reached without a materialised page"
        injected[0] = True
        return (min(interested),)

    monkeypatch.setattr(TransactionManager, "_find_conflict", refuse_materialized_page)
    with pytest.raises(GrafxWriteConflict):
        p2.manager.commit(refused)
    assert injected[0], "the refusal did not exercise the post-materialisation OCC pass"

    _insert(p1, table, 4, "p1")  # acknowledged durable

    retry = p2.manager.retry(refused)
    retry.stage_row_insert(table, (5, "p2-y"))
    retry.note_write(p2.manager.partition_of(table.table_id, b"5"))
    p2.manager.commit(retry)

    fresh = _participant(root, "fresh", clock)
    assert _rows(fresh, table) == [(1, "p1"), (2, "p1"), (4, "p1"), (5, "p2-y")]
    verifier = Verifier(fresh.pool, fresh.metrics, heap=fresh.heap, catalog=fresh.catalog)
    assert verifier.verify("all").findings == ()


# --- B3: a second commit after a retryable refusal carries ONE set of index records --------------


def test_a_recommit_after_a_retryable_refusal_stages_its_index_changes_once(
    tmp_path: Path,
) -> None:
    """The log refuses the batch once (device full, retryable); the same txn commits again.

    Index staging appended to the transaction and to each index's staging area, and a refusal
    after staging undid neither: the second commit staged AGAIN and carried both sets, the first
    stamped with a number the log never assigned, and applied live entries for versions that were
    abandoned. Now every index change in the log carries a commit number the log assigned.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    device = FaultInjectingStorageDevice(
        SharedDirectoryDevice(root, page_size=DEFAULT_PAGE_SIZE), seed=5
    )
    base = build_stack(
        root, storage=device, clock=clock, wal_factory=_real_wal(1 << 20), owner_id="idx"
    )
    indexes = IndexManager(base.pool, base.heap, base.metrics)
    manager = TransactionManager(
        base.wal,
        base.pool,
        base.heap,
        base.catalog,
        base.coordinator,
        base.clock,
        base.metrics,
        indexes,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    table = _registered(base, _table())
    index = indexes.register(
        ProximityIndex(
            IndexDefinition(
                name="p_prox",
                table_id=table.table_id,
                table_name=table.name,
                positions=(0,),
                visibility=IndexVisibility.PROXIMITY,
            ),
            base.pool,
            base.metrics,
        )
    )

    first = manager.begin("write")
    first.stage_row_insert(table, (1, "a"))
    first.note_write(manager.partition_of(1, b"1"))
    manager.commit(first)
    ref_1 = first.row_refs[0]

    again = manager.begin("write")
    again.stage_row_update(table, ref_1, (1, "a2"))
    again.stage_row_insert(table, (2, "b"))
    again.note_write(manager.partition_of(1, b"1"))
    again.note_write(manager.partition_of(1, b"2"))
    device.clear_trail()
    device.fill_device_on("append_log", 2)
    with pytest.raises(GrafxError) as refusal:
        manager.commit(again)
    assert refusal.value.retryable is True
    device.disarm()
    assert again.pending_records == [], "the refused attempt must leave nothing staged behind"

    report = manager.commit(again)
    commit_lsns = {
        record.lsn
        for record in base.wal.read_from(1)
        if record.record_type == WalRecordType.COMMIT
    }
    index_records = [
        record
        for record in base.wal.read_from(1)
        if record.record_type == WalRecordType.INDEX_WRITE
    ]
    # First commit: 1 insert. Second commit: an update (delete + insert) and an insert = 3.
    # The refused attempt staged 3 as well; had they survived, there would be 7.
    assert len(index_records) == 4
    # A batch that opens a segment stamps one below its COMMIT (the log inserts a segment header
    # into the batch; visibility-equivalent, recorded as punch list). What must never appear is
    # the number the REFUSED attempt predicted, which no COMMIT is near.
    assigned = commit_lsns | {lsn - 1 for lsn in commit_lsns}
    for entry in index.walk():
        assert entry.born_csn in assigned, (entry, commit_lsns)
        if entry.dead_csn:
            assert entry.dead_csn in assigned
    assert report.csn in commit_lsns
    verifier = Verifier(
        base.pool, base.metrics, heap=base.heap, catalog=base.catalog, indexes=indexes.indexes()
    )
    assert verifier.verify("all").findings == ()


# --- B4: validation refuses when the log no longer holds the commits above the snapshot ---------


def test_validation_refuses_rather_than_reading_a_log_a_checkpoint_recycled(
    tmp_path: Path,
) -> None:
    """P1 holds W open past the stall threshold; P2 checkpoints and recycles; W must not commit.

    W read partition 7 and then decided something. P2 committed a conflicting row in partition 7
    at a number above W's snapshot, then many more, then checkpointed: P1's reader pin had gone
    stale and was pruned, the horizon passed W's snapshot, and the segment holding the conflicting
    COMMIT was recycled. ``read_from`` then silently started at the first retained segment and
    optimistic validation, answered from a log with a hole in it, let W commit over it.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    p1 = _participant(root, "p1", clock, segment_bytes=2048)
    p2 = _participant(root, "p2", clock, segment_bytes=2048)
    table = _registered(p1, _table())
    part7 = p1.manager.partition_of(table.table_id, b"7")
    other = next(
        key
        for key in range(8, 200)
        if p1.manager.partition_of(table.table_id, str(key).encode()) != part7
    )
    for identity in range(1, 10):
        _insert(p1, table, identity)

    stale = p1.manager.begin("write")
    stale.note_read(part7)
    stale.stage_row_insert(table, (500, "decided-from-a-stale-read"))
    stale.note_write(p1.manager.partition_of(table.table_id, b"500"))

    conflicting = p2.manager.begin("write")
    conflicting.stage_row_insert(table, (7, "p2-conflict"))
    conflicting.note_write(part7)
    p2.manager.commit(conflicting)
    for identity in range(100, 160):
        filler = p2.manager.begin("write")
        filler.stage_row_insert(table, (identity, "p2-other"))
        filler.note_write(p2.manager.partition_of(table.table_id, str(other).encode()))
        p2.manager.commit(filler)

    # The hole is cut into the log directly. A checkpoint cuts the same hole when P1's reader
    # pin has gone stale and been pruned -- that is how the critic found it -- but whether a pin
    # is prunable depends on heartbeat timing the test must not rest on. The guard under test
    # does not care how the records left the log, only that they are not there to be read.
    report = p2.wal.recycle(p2.manager.published_lsn())
    assert report.recycled, "the scenario needs the conflicting segment to be gone"
    assert p2.wal.segments()[0].first_lsn > stale.snapshot.read_lsn + 1, report

    p1.wal.refresh()
    with pytest.raises(GrafxWriteConflict) as refusal:
        p1.manager.commit(stale)
    assert "lowest_retained_lsn" in refusal.value.details


# --- the P4 window, index half: a commit refused after the barrier is redone, not left behind ----


def test_a_commit_whose_index_apply_fails_after_the_barrier_is_redone_from_the_log(
    tmp_path: Path,
) -> None:
    """The index apply raises once, post-barrier. The rows are durable; the index must not lag.

    Left to a later commit's publication, the rows became visible with no index entry behind
    them -- a lookup answering fewer rows than exist, and ``verify()`` reporting
    ``index_entry_missing``. The commit is redone from the log through the idempotent doors
    recovery uses, and published.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    base = build_stack(root, clock=clock, wal_factory=_real_wal(1 << 20), owner_id="p4")
    blown = {"left": 1}

    class RefusingOnce(IndexManager):
        """The real manager, whose first commit-time apply refuses after the barrier."""

        def commit(self, txn: object, csn: object) -> int:
            if blown["left"]:
                blown["left"] -= 1
                raise GrafxError("simulated post-barrier failure of the index apply")
            return super().commit(txn, csn)

    indexes = RefusingOnce(base.pool, base.heap, base.metrics)
    manager = TransactionManager(
        base.wal, base.pool, base.heap, base.catalog, base.coordinator, base.clock,
        base.metrics, indexes, partitions_per_table=8, commit_lock_timeout=5.0,
    )
    table = _registered(base, _table())
    index = indexes.register(
        ProximityIndex(
            IndexDefinition(
                name="p_prox", table_id=table.table_id, table_name=table.name,
                positions=(0,), visibility=IndexVisibility.PROXIMITY,
            ),
            base.pool, base.metrics,
        )
    )
    txn = manager.begin("write")
    txn.stage_row_insert(table, (1, "a"))
    txn.stage_row_insert(table, (2, "b"))
    txn.note_write(manager.partition_of(1, b"1"))
    txn.note_write(manager.partition_of(1, b"2"))
    with pytest.raises(GrafxError) as refusal:
        manager.commit(txn)
    assert refusal.value.details.get("committed") is True or "committed" in refusal.value.message

    # Durable AND applied: both entries are live in the index, the state is published, and the
    # verifier agrees on the whole database.
    assert sum(1 for entry in index.walk() if not entry.dead_csn) == 2
    assert manager.published_lsn() >= 1
    verifier = Verifier(
        base.pool, base.metrics, heap=base.heap, catalog=base.catalog, indexes=indexes.indexes()
    )
    assert verifier.verify("all").findings == ()
    # And the next commit is ordinary.
    later = manager.begin("write")
    later.stage_row_insert(table, (3, "c"))
    later.note_write(manager.partition_of(1, b"3"))
    manager.commit(later)
    assert sum(1 for entry in index.walk() if not entry.dead_csn) == 3
