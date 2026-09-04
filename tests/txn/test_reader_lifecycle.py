"""Reader registration and refresh (carried finding CF-2, amendment A46, SPEC-M1 BR-10).

Two obligations this component was given by name.

CF-2: ``register_reader`` publishes before the registration is visible elsewhere, so a horizon
pass in that window can miss a brand-new reader. C5 must order registration against snapshot
selection so a reader is never handed a snapshot the horizon has already passed.

A46: ``ReaderRegistration`` has no ``renew_if_due``. A reader that misses the stall threshold is
pruned by other participants, so C5 schedules the refreshes itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.engine.coordination import DEFAULT_RENEWAL_FRACTION, recyclable_horizon
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.wal_manager import WalManager
from txn_support import Stack, TracingCoordinator, build_stack, make_page_image

HEAP = "heap.dat"


def _real_wal(device: object, clock: object, metrics: object) -> WalManager:
    """Build the segmented WAL whose checkpoint door includes recycling."""
    wal = WalManager(
        device,  # type: ignore[arg-type]
        clock,  # type: ignore[arg-type]
        metrics,  # type: ignore[arg-type]
        segment_bytes=1 << 20,
        descriptor="hash-v1;partitions_per_table=8",
    )
    wal.open()
    return wal


def _commit_one(stack: Stack, page_index: int, payload: bytes = b"row") -> int:
    """Commit one page through this participant and return the commit number."""
    txn = stack.manager.begin("write")
    txn.owner._stage_page_image(txn,
        HEAP, page_index, make_page_image(stack.codec, [payload], page_index=page_index)
    )
    txn.note_write(stack.manager.partition_of(1, payload))
    return stack.manager.commit(txn).csn


# --- CF-2: the order of registration against selection ------------------------------------------


def test_a_reader_pins_its_snapshot_before_the_snapshot_is_chosen(
    make_stack, database_root: Path
) -> None:
    """CF-2. A commit that lands during the registration window must not outrun the snapshot.

    The probe fires when the registration is published, which is the one instant that separates
    the two possible orders: with registration FIRST the snapshot has not been chosen yet and
    the second reading picks the commit up; with registration LAST it has, and the reader walks
    away with a snapshot the horizon has already passed.
    """
    reader_side = make_stack()
    writer_side = make_stack()
    fired: list[int] = []

    def advance(pinned: int) -> None:
        if fired:
            return
        fired.append(pinned)
        _commit_one(writer_side, page_index=3, payload=b"during")

    trail: list[str] = []
    manager = TransactionManager(
        reader_side.wal,
        reader_side.pool,
        reader_side.heap,
        reader_side.catalog,
        TracingCoordinator(reader_side.coordinator, trail, on_register=advance),
        reader_side.clock,
        reader_side.metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    txn = manager.begin("read")
    assert fired, "the probe never ran, so this test proved nothing"
    horizon = recyclable_horizon(
        writer_side.coordinator.reader_horizon(), writer_side.manager.published_lsn()
    )
    assert horizon <= txn.snapshot.read_lsn
    assert txn.snapshot.read_lsn == writer_side.manager.published_lsn()


def test_the_pin_is_never_above_the_snapshot_it_stands_for(stack: Stack) -> None:
    """A pin above the snapshot would release segments the transaction still needs."""
    for page in (3, 4, 5):
        _commit_one(stack, page_index=page, payload=bytes([page]))
    txn = stack.manager.begin("read")
    horizon = stack.coordinator.reader_horizon()
    assert horizon is not None
    assert horizon <= txn.snapshot.read_lsn


def test_every_open_transaction_holds_the_horizon_down(make_stack) -> None:
    """BR-10: a live reader's snapshot is what bounds recycling, in any process."""
    reader_side = make_stack()
    writer_side = make_stack()
    _commit_one(writer_side, page_index=3)
    early = reader_side.manager.begin("read")
    for page in (4, 5, 6):
        _commit_one(writer_side, page_index=page, payload=bytes([page]))
    # CE-2: the writer participant keeps its own standing pin at the floor of its first
    # begin, so the horizon is BOUNDED BY the early reader's snapshot rather than equal to
    # it -- which is all BR-10 requires: no segment the early reader needs is recyclable.
    horizon = writer_side.coordinator.reader_horizon()
    assert horizon is not None and horizon <= early.snapshot.read_lsn
    assert writer_side.manager.published_lsn() > early.snapshot.read_lsn


def test_a_writer_also_pins_the_snapshot_it_reads_under(make_stack) -> None:
    """A write transaction reads too, so its snapshot is pinned exactly like a reader's."""
    first = make_stack()
    second = make_stack()
    _commit_one(first, page_index=3)
    txn = second.manager.begin("write")
    # CE-2: BOTH participants hold standing pins now -- the writer that ran _commit_one keeps
    # its own at the floor it began under -- so the horizon is bounded by the write txn's
    # snapshot rather than equal to it. The per-participant CF-2 pin-before-snapshot property
    # is pinned in test_reader_participant_pin.py.
    horizon = first.coordinator.reader_horizon()
    assert horizon is not None and horizon <= txn.snapshot.read_lsn
    second.manager.rollback(txn)
    # CE-2: the pin belongs to the participant, not the transaction; rollback keeps it.
    after = first.coordinator.reader_horizon()
    assert after is not None and after <= txn.snapshot.read_lsn


def test_finishing_a_transaction_keeps_the_participant_pin(make_stack) -> None:
    # CE-2 deliberately rewrote this pin: finishing a transaction used to unregister its
    # per-transaction reader; the registration now belongs to the PARTICIPANT and only
    # close() withdraws it (E-CE2-1). What finishing releases is the FLOOR -- the pin may
    # advance past the finished snapshot at the next due refresh or checkpoint.
    reader_side = make_stack()
    writer_side = make_stack()
    _commit_one(writer_side, page_index=3)
    txn = reader_side.manager.begin("read")
    assert writer_side.coordinator.reader_horizon() is not None
    reader_side.manager.commit(txn)
    assert writer_side.coordinator.reader_horizon() is not None
    reader_side.manager.close()
    # The writer participant ran _commit_one, so its OWN standing pin remains until it too
    # closes -- only then is the directory free of registrations.
    writer_side.manager.close()
    assert writer_side.coordinator.reader_horizon() is None


# --- A46: the refresh schedule this component owns -----------------------------------------------


def test_a_long_read_survives_the_stall_threshold_when_the_manager_is_driven(
    database_root: Path,
) -> None:
    """A46: without a refresh the reader is pruned and its snapshot pin is silently released."""
    reader_side = build_stack(
        database_root, owner_id="reader", reader_stall_threshold=15.0
    )
    observer = build_stack(database_root, owner_id="observer")
    txn = reader_side.manager.begin("read")
    pinned = txn.snapshot.read_lsn
    assert observer.coordinator.reader_horizon() == pinned
    # Ten six-second ticks model the CE-2 acceptance reader held for a full minute.
    for _step in range(10):
        reader_side.clock.advance(6.0)
        observer.clock.advance(6.0)
        reader_side.manager.refresh_due_readers()
        observer.coordinator.reader_horizon()
    assert observer.coordinator.reader_horizon() == pinned


def test_a_long_read_that_is_never_refreshed_is_pruned_by_another_participant(
    database_root: Path,
) -> None:
    """The counterfactual: the pruning window A46 warns about is real and this bench reaches it."""
    reader_side = build_stack(
        database_root, owner_id="reader", reader_stall_threshold=15.0
    )
    observer = build_stack(database_root, owner_id="observer")
    reader_side.manager.begin("read")
    # The observer has to SEE the heartbeat before it can call it still: C3 measures a stall
    # against its own first sighting, so a participant that never looked cannot prune.
    assert observer.coordinator.reader_horizon() is not None
    for _step in range(6):
        observer.clock.advance(6.0)
        observer.coordinator.reader_horizon()
    assert observer.coordinator.reader_horizon() is None


def test_the_refresh_interval_is_a_fraction_of_the_stall_threshold(
    database_root: Path,
) -> None:
    """Two refreshes may fail before anybody else could call the reader stalled."""
    stack = build_stack(database_root, reader_stall_threshold=15.0)
    assert stack.manager.reader_stall_threshold == 15.0
    assert stack.manager.refresh_interval == pytest.approx(15.0 * DEFAULT_RENEWAL_FRACTION)


def test_a_manager_told_nothing_about_the_threshold_refreshes_at_every_opportunity(
    database_root: Path,
) -> None:
    """The conservative default: never be the reason a reader misses a threshold nobody named."""
    stack = build_stack(database_root, reader_stall_threshold=None)
    assert stack.manager.refresh_interval == 0.0
    stack.manager.begin("read")
    assert stack.manager.refresh_due_readers() == 1
    assert stack.manager.refresh_due_readers() == 1


def test_a_refresh_that_is_not_due_yet_does_nothing(database_root: Path) -> None:
    stack = build_stack(database_root, reader_stall_threshold=15.0)
    stack.manager.begin("read")
    assert stack.manager.refresh_due_readers(stack.clock.monotonic()) == 0
    assert stack.manager.refresh_due_readers(stack.clock.monotonic() + 5.1) == 1


def test_refreshing_with_no_open_transaction_is_free(stack: Stack) -> None:
    assert stack.manager.refresh_due_readers() == 0


def test_an_unusable_stall_threshold_is_refused_at_construction(
    database_root: Path,
) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        build_stack(database_root, reader_stall_threshold=0.0)
    assert raised.value.details["field"] == "reader_stall_threshold"


def test_an_unusable_commit_lock_timeout_is_refused_at_construction(
    database_root: Path,
) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        build_stack(database_root, commit_lock_timeout=-1.0)
    assert raised.value.details["field"] == "commit_lock_timeout"


def test_the_lease_timeout_defaults_to_the_commit_lock_timeout(database_root: Path) -> None:
    stack = build_stack(database_root, commit_lock_timeout=3.5, lease_timeout=None)
    assert stack.manager.lease_timeout == 3.5


def test_a_failed_registration_leaves_no_transaction_behind(database_root: Path) -> None:
    """A begin that cannot publish its pin must not leave a half-open transaction counted."""

    class _RefusingCoordinator(TracingCoordinator):
        def register_reader(self, snapshot_lsn: int):  # noqa: ANN201, D102
            raise GrafxConfigurationError("registration refused for this probe.")

    stack = build_stack(database_root)
    manager = TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        _RefusingCoordinator(stack.coordinator, []),
        stack.clock,
        stack.metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
    )
    with pytest.raises(GrafxConfigurationError):
        manager.begin("read")
    assert manager.open_transactions == 0


# --- CF-11: the number BR-10 recycling needs --------------------------------------------------


def test_the_recyclable_horizon_is_the_checkpoint_when_no_reader_is_live(
    stack: Stack,
) -> None:
    """BR-10: the absence of readers is not a licence to recycle everything, only the checkpoint."""
    assert stack.coordinator.reader_horizon() is None
    assert stack.manager.recyclable_horizon() == stack.manager.published_state().checkpoint_lsn


@pytest.mark.parametrize("with_open_reader", [False, True])
def test_checkpoint_scans_the_reader_registry_once(
    make_stack: object,
    monkeypatch: pytest.MonkeyPatch,
    with_open_reader: bool,
) -> None:
    """Split and monolithic checkpoint reuse one exact reader-horizon observation."""
    stack = make_stack(wal_factory=_real_wal)  # type: ignore[operator]
    trail: list[str] = []
    traced = TracingCoordinator(stack.coordinator, trail)
    monkeypatch.setattr(stack.manager, "_coordinator", traced)
    transaction = stack.manager.begin("read") if with_open_reader else None
    trail.clear()

    stack.manager.checkpoint()

    assert trail.count("reader_horizon") == 1
    if transaction is not None:
        stack.manager.rollback(transaction)


def test_a_live_reader_holds_the_recyclable_horizon_down(make_stack) -> None:
    """The pin of a live reader bounds recycling, which is the whole of BR-10."""
    writer = make_stack()
    reader = make_stack()
    for page in (3, 4, 5):
        txn = writer.manager.begin("write")
        txn.owner._stage_page_image(txn,
            "heap.dat", page, make_page_image(writer.codec, [bytes([page])], page_index=page)
        )
        txn.note_write(writer.manager.partition_of(1, bytes([page])))
        writer.manager.commit(txn)
    early = reader.manager.begin("read")
    horizon = writer.manager.recyclable_horizon()
    assert horizon <= early.snapshot.read_lsn, "a live reader is never recycled out from under"
    reader.manager.rollback(early)


def test_the_horizon_never_passes_a_snapshot_this_manager_handed_out(make_stack) -> None:
    """The property CF-2 asks for, stated against the number recycling actually consumes."""
    writer = make_stack()
    reader = make_stack()
    txn = writer.manager.begin("write")
    txn.owner._stage_page_image(txn, "heap.dat", 3, make_page_image(writer.codec, [b"x"], page_index=3))
    txn.note_write(writer.manager.partition_of(1, b"x"))
    writer.manager.commit(txn)
    live = reader.manager.begin("read")
    assert writer.manager.recyclable_horizon() <= live.snapshot.read_lsn
    reader.manager.rollback(live)
