"""CE-2: one reader registration per participant, with a deferred, monotone pin.

The counting family pins what the change removes: a begin inside the refresh interval
publishes nothing, and finishing a transaction withdraws nothing. The safety family pins what
may never move: the pin stays at or below every open snapshot (BR-10), a pruned registration
is recreated by the next due refresh, the checkpoint advances the participant's own pin so it
never blocks its own recycling, and close withdraws the registration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from txn_support import ManualClock, Stack, build_stack, make_page_image

from okto_grafx.engine.commit_state_store import CommitStateStore
from okto_grafx.engine.wal_manager import WalManager

HEAP = "heap.dat"


def _real_wal(device: object, clock: object, metrics: object) -> WalManager:
    """Build the real log so checkpoint's committed replay can read it."""
    manager = WalManager(
        device,  # type: ignore[arg-type]
        clock,  # type: ignore[arg-type]
        metrics,  # type: ignore[arg-type]
        segment_bytes=1 << 20,
        descriptor="hash-v1;partitions_per_table=8",
    )
    manager.open()
    return manager


def _segmented_wal(segment_bytes: int):
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


def _commit_one(stack: Stack, page_index: int, payload: bytes = b"row") -> int:
    transaction = stack.manager.begin("write")
    transaction.owner._stage_page_image(
        transaction,
        HEAP,
        page_index,
        make_page_image(stack.codec, [payload], page_index=page_index),
    )
    transaction.note_write(stack.manager.partition_of(1, payload))
    return stack.manager.commit(transaction).csn


class _CoordinatorSpy:
    """Count the reader doors of one live coordinator without changing what they answer."""

    def __init__(self, coordinator) -> None:
        self.registers = 0
        self.refreshes = 0
        self.unregisters = 0
        original_register = coordinator.register_reader
        original_refresh = coordinator.refresh_reader
        original_unregister = coordinator.unregister_reader

        def counting_register(snapshot_lsn):
            self.registers += 1
            return original_register(snapshot_lsn)

        def counting_refresh(handle):
            self.refreshes += 1
            return original_refresh(handle)

        def counting_unregister(handle):
            self.unregisters += 1
            return original_unregister(handle)

        coordinator.register_reader = counting_register
        coordinator.refresh_reader = counting_refresh
        coordinator.unregister_reader = counting_unregister

    @property
    def publishes(self) -> int:
        return self.registers + self.refreshes


@pytest.fixture()
def stack(tmp_path: Path):
    return build_stack(tmp_path)


def test_two_begins_share_one_participant_registration(stack) -> None:
    spy = _CoordinatorSpy(stack.coordinator)
    first = stack.manager.begin("read")
    second = stack.manager.begin("read")
    assert spy.registers == 1, (
        "a participant registers ONE reader; transactions borrow its pin"
    )
    assert stack.coordinator.reader_horizon() is not None
    assert stack.coordinator.reader_horizon() <= first.snapshot.read_lsn
    assert stack.coordinator.reader_horizon() <= second.snapshot.read_lsn
    stack.manager.rollback(second)
    stack.manager.rollback(first)


def test_interval_zero_finish_refreshes_the_pin_for_another_open_transaction(
    tmp_path: Path,
) -> None:
    """Compatibility mode must not let finishing one transaction strand another's pin."""
    clock = ManualClock()
    reader = build_stack(
        tmp_path,
        owner_id="reader",
        clock=clock,
        reader_stall_threshold=None,
    )
    observer = build_stack(tmp_path, owner_id="observer", clock=clock)
    spy = _CoordinatorSpy(reader.coordinator)
    first = reader.manager.begin("read")
    second = reader.manager.begin("read")
    refreshes_before_finish = spy.refreshes
    assert observer.coordinator.reader_horizon() == first.snapshot.read_lsn

    clock.advance(14.0)
    reader.manager.commit(first)
    assert spy.refreshes == refreshes_before_finish + 1
    assert observer.coordinator.reader_horizon() == second.snapshot.read_lsn

    # The observer measures liveness from the finishing-door publication, not the original
    # registration. Without that refresh it would prune the still-open second transaction here.
    clock.advance(14.0)
    assert observer.coordinator.reader_horizon() == second.snapshot.read_lsn
    reader.manager.rollback(second)
    reader.manager.close()
    observer.manager.close()


def test_clock_failure_before_first_pin_publication_leaves_no_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed begin cannot publish a participant record the manager never adopts."""
    stack = build_stack(tmp_path, reader_stall_threshold=15.0)
    manager_type = type(stack.manager)
    original = manager_type._monotonic
    failure = RuntimeError("clock failed before participant publication")

    def fail_for_this_manager(manager) -> float:  # noqa: ANN001
        if manager is stack.manager:
            raise failure
        return original(manager)

    monkeypatch.setattr(manager_type, "_monotonic", fail_for_this_manager)
    with pytest.raises(RuntimeError, match="clock failed before participant publication"):
        stack.manager.begin("read")

    assert stack.manager.open_transactions == 0
    assert stack.coordinator.reader_horizon() is None
    readers = tmp_path / "control" / "readers"
    assert not readers.exists() or not tuple(readers.glob("*.reader"))


def test_finishing_a_transaction_withdraws_nothing(stack) -> None:
    spy = _CoordinatorSpy(stack.coordinator)
    transaction = stack.manager.begin("read")
    stack.manager.commit(transaction)
    assert spy.unregisters == 0, (
        "commit must not unregister the participant pin; only close does"
    )
    assert stack.coordinator.reader_horizon() is not None, (
        "the participant pin outlives the transaction"
    )


def test_a_begin_inside_the_refresh_interval_publishes_nothing(tmp_path: Path) -> None:
    # The interval this test depends on is declared, as the A46 schedule tests do: a stall
    # threshold of 15 s gives the production refresh cadence of 5 s. (The bare default keeps
    # an interval of zero -- every door refreshes -- which is safe but never deferred.)
    stack = build_stack(tmp_path, reader_stall_threshold=15.0)
    first = stack.manager.begin("read")
    stack.manager.commit(first)
    spy = _CoordinatorSpy(stack.coordinator)
    second = stack.manager.begin("read")
    assert spy.publishes == 0, (
        "a begin within the refresh interval trusts the standing registration"
    )
    stack.manager.commit(second)
    assert spy.unregisters == 0


def test_a_begin_inside_the_refresh_interval_reads_the_published_floor_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A visible standing pin makes the second CF-2 read redundant only in the stable regime."""

    stack = build_stack(tmp_path, reader_stall_threshold=15.0)
    first = stack.manager.begin("read")
    stack.manager.commit(first)
    original = CommitStateStore.read
    reads = 0

    def counted(store: CommitStateStore):
        nonlocal reads
        if store is stack.manager._commit_state_store:
            reads += 1
        return original(store)

    monkeypatch.setattr(CommitStateStore, "read", counted)
    second = stack.manager.begin("read")
    assert reads == 1
    stack.manager.commit(second)


def test_a_due_begin_keeps_the_second_cf2_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Republishing a due pin preserves the window in which a foreign commit can land."""

    stack = build_stack(tmp_path, reader_stall_threshold=15.0)
    first = stack.manager.begin("read")
    stack.manager.commit(first)
    stack.clock.advance(6.0)
    original = CommitStateStore.read
    reads = 0

    def counted(store: CommitStateStore):
        nonlocal reads
        if store is stack.manager._commit_state_store:
            reads += 1
        return original(store)

    monkeypatch.setattr(CommitStateStore, "read", counted)
    second = stack.manager.begin("read")
    assert reads == 2
    stack.manager.commit(second)


def test_an_operation_longer_than_the_interval_republishes_at_commit(
    tmp_path: Path,
) -> None:
    # The CE-2 acceptance line verbatim: for an operation longer than the refresh interval,
    # the overdue pin is REPUBLISHED at commit -- the publication is moved, never removed.
    # This is the liveness half: a >5s transaction must not let the participant heartbeat go
    # quiet long enough for a foreign observer's prune (three intervals) to land.
    stack = build_stack(tmp_path, reader_stall_threshold=15.0)
    transaction = stack.manager.begin("read")
    stack.clock.advance(6.0)  # past the 5 s cadence, mid-operation
    spy = _CoordinatorSpy(stack.coordinator)
    stack.manager.commit(transaction)
    assert spy.publishes >= 1, (
        "an overdue pin must be republished at commit (moved, not removed)"
    )
    assert spy.unregisters == 0


def test_an_operation_longer_than_the_interval_republishes_at_rollback(
    tmp_path: Path,
) -> None:
    stack = build_stack(tmp_path, reader_stall_threshold=15.0)
    transaction = stack.manager.begin("read")
    stack.clock.advance(6.0)
    spy = _CoordinatorSpy(stack.coordinator)
    stack.manager.rollback(transaction)
    assert spy.publishes == 1
    assert spy.unregisters == 0
    assert stack.coordinator.reader_horizon() is not None


def test_a_due_refresh_advances_the_pin_only_to_the_open_floor(tmp_path: Path) -> None:
    # BR-10 half: the pin may advance, but never past the oldest open snapshot.
    stack = build_stack(tmp_path, reader_stall_threshold=15.0)
    early = stack.manager.begin("read")
    floor = early.snapshot.read_lsn
    committed = _commit_one(stack, 3, b"later")
    assert committed > floor
    stack.clock.advance(6.0)
    stack.manager.refresh_due_readers(stack.clock.monotonic())
    horizon = stack.coordinator.reader_horizon()
    assert horizon == floor, (
        "the participant pin passed an open snapshot"
    )
    stack.manager.rollback(early)


def test_a_due_begin_advances_the_pin_to_the_current_floor(tmp_path: Path) -> None:
    stack = build_stack(tmp_path, reader_stall_threshold=15.0)
    first = stack.manager.begin("read")
    initial = stack.coordinator.reader_horizon()
    stack.manager.commit(first)
    committed = _commit_one(stack, 3, b"advance")
    stack.clock.advance(6.0)
    second = stack.manager.begin("read")
    later = stack.coordinator.reader_horizon()
    assert initial is not None and later == committed > initial, (
        "a due begin renewed an obsolete pin instead of advancing it to its published floor"
    )
    assert second.snapshot.read_lsn == committed
    stack.manager.rollback(second)


def test_a_due_public_tick_advances_an_idle_participant_pin(tmp_path: Path) -> None:
    stack = build_stack(tmp_path, reader_stall_threshold=15.0)
    first = stack.manager.begin("read")
    initial = first.snapshot.read_lsn
    stack.manager.commit(first)
    committed = _commit_one(stack, 3, b"tick")
    stack.clock.advance(6.0)
    spy = _CoordinatorSpy(stack.coordinator)
    assert stack.manager.refresh_due_readers(stack.clock.monotonic()) == 1
    assert spy.refreshes == 1
    assert stack.coordinator.reader_horizon() == committed > initial


def test_a_failed_finish_never_leaves_an_active_snapshot_behind_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = build_stack(tmp_path, reader_stall_threshold=15.0)
    transaction = stack.manager.begin("read")
    stack.clock.advance(6.0)
    spy = _CoordinatorSpy(stack.coordinator)

    def refuse_settlement(_self: object, _csn: int) -> None:
        raise RuntimeError("settlement refused")

    monkeypatch.setattr(type(transaction), "mark_committed", refuse_settlement)
    with pytest.raises(RuntimeError, match="settlement refused"):
        stack.manager.commit(transaction)
    horizon = stack.coordinator.reader_horizon()
    assert spy.refreshes == 1
    assert transaction.active
    assert horizon is not None and horizon <= transaction.snapshot.read_lsn


def test_a_pruned_registration_is_recreated_by_the_next_due_refresh(stack) -> None:
    # A foreign observer prunes a registration whose heartbeat stalled; the participant's
    # next due refresh must republish it (BR-10 forbids losing a live reader's pin for good).
    transaction = stack.manager.begin("read")
    handle_ids = list(stack.coordinator._readers)
    assert len(handle_ids) == 1
    reader_file = stack.coordinator._reader_file(handle_ids[0])
    stack.storage.remove(reader_file)  # the foreign prune, at the storage level
    stack.clock.advance(6.0)
    stack.manager.refresh_due_readers(stack.clock.monotonic())
    assert stack.storage.exists(reader_file), (
        "a due refresh must recreate a pruned registration"
    )
    assert stack.coordinator.reader_horizon() is not None
    stack.manager.rollback(transaction)


def test_the_checkpoint_is_not_blocked_by_the_participants_own_stale_pin(
    tmp_path: Path,
) -> None:
    # The participant's pin is never pruned by its own coordinator, so the checkpoint must
    # advance it before computing the recyclable horizon -- otherwise the manager would hold
    # its own recycling hostage forever.
    stack = build_stack(tmp_path, wal_factory=_real_wal)
    transaction = stack.manager.begin("write")
    stack.manager.rollback(transaction)
    stack.manager.checkpoint()
    state = stack.manager.published_state()
    assert stack.manager.recyclable_horizon() == state.checkpoint_lsn, (
        "the participant's own stale pin held the recyclable horizon down"
    )


def test_close_withdraws_the_participant_registration(tmp_path: Path) -> None:
    stack = build_stack(tmp_path)
    spy = _CoordinatorSpy(stack.coordinator)
    transaction = stack.manager.begin("read")
    stack.manager.commit(transaction)
    stack.manager.close()
    assert spy.unregisters >= 1, "close must withdraw the participant registration"
    assert stack.coordinator.reader_horizon() is None


def test_a_reader_pin_is_visible_before_its_snapshot_is_chosen(stack) -> None:
    # CF-2 must survive CE-2 whole: whatever registration answers for this participant, it is
    # visible at or below the floor BEFORE the snapshot is selected, so no horizon can pass a
    # snapshot this manager hands out.
    transaction = stack.manager.begin("read")
    horizon = stack.coordinator.reader_horizon()
    assert horizon is not None and horizon <= transaction.snapshot.read_lsn
    stack.manager.rollback(transaction)


def test_a_sixty_second_reader_survives_five_foreign_checkpoints(
    tmp_path: Path,
) -> None:
    """CE-2/F1: a driven long reader keeps every post-snapshot WAL segment alive."""
    root = tmp_path / "long-reader"
    clock = ManualClock()
    wal_factory = _segmented_wal(2048)
    reader = build_stack(
        root,
        owner_id="reader",
        clock=clock,
        reader_stall_threshold=15.0,
        wal_factory=wal_factory,
    )
    writer = build_stack(
        root,
        owner_id="writer",
        clock=clock,
        reader_stall_threshold=15.0,
        wal_factory=wal_factory,
    )
    transaction = reader.manager.begin("read")
    snapshot = transaction.snapshot.read_lsn
    protected: set[str] = set()

    for step in range(10):
        for offset in range(2):
            page = 3 + step * 2 + offset
            _commit_one(writer, page, bytes([page]))
        clock.advance(6.0)
        assert reader.manager.refresh_due_readers(clock.monotonic()) == 1
        protected.update(
                segment.name
            for segment in writer.wal.segments()
            if segment.last_lsn > snapshot
        )
        if step % 2:
            writer.manager.checkpoint()
            retained = {segment.name for segment in writer.wal.segments()}
            assert protected <= retained, (
                "a checkpoint recycled WAL newer than the live reader's snapshot"
            )
        assert writer.coordinator.reader_horizon() == snapshot

    # Finishing releases the snapshot floor but not the participant registration. One due tick
    # (5 s in production; the acceptance allows two) advances it to the published floor.
    reader.manager.rollback(transaction)
    clock.advance(reader.manager.refresh_interval)
    assert reader.manager.refresh_due_readers(clock.monotonic()) == 1
    assert writer.coordinator.reader_horizon() == writer.manager.published_lsn()
    writer.manager.checkpoint()
    assert set(protected) - {segment.name for segment in writer.wal.segments()}
    reader.manager.close()
    writer.manager.close()


def test_a_crashed_reader_holds_recycling_until_its_full_ttl(
    tmp_path: Path,
) -> None:
    """CE-2/F1 kill analogue: a silent participant is retained through 15 s, then pruned."""
    root = tmp_path / "crashed-reader"
    clock = ManualClock()
    wal_factory = _segmented_wal(2048)
    reader = build_stack(
        root,
        owner_id="reader-to-kill",
        clock=clock,
        reader_stall_threshold=15.0,
        wal_factory=wal_factory,
    )
    writer = build_stack(
        root,
        owner_id="observer",
        clock=clock,
        reader_stall_threshold=15.0,
        wal_factory=wal_factory,
    )
    transaction = reader.manager.begin("read")
    snapshot = transaction.snapshot.read_lsn
    for page in range(3, 19):
        _commit_one(writer, page, bytes([page]))
    protected = {
        segment.name
        for segment in writer.wal.segments()
        if segment.last_lsn > snapshot
    }
    assert len(protected) > 2, "the crash scenario needs several protected segments"
    writer.manager.checkpoint()
    assert protected <= {segment.name for segment in writer.wal.segments()}
    assert writer.coordinator.reader_horizon() == snapshot

    # LocalProcessCoordinator prunes only after the threshold, not at it. This is the exact
    # boundary a real kill -9 leaves behind: no unregister and no further heartbeat.
    clock.advance(15.0)
    assert writer.coordinator.reader_horizon() == snapshot
    clock.advance(0.001)
    advanced = writer.coordinator.reader_horizon()
    assert advanced is not None and advanced > snapshot
    writer.manager.checkpoint()
    retained = {segment.name for segment in writer.wal.segments()}
    assert protected - retained, "the dead reader still blocked recycling after its TTL"

    # The object remains only so this in-process analogue can release resources; no operation is
    # allowed to resume the logically killed participant after its WAL protection was reclaimed.
    reader.manager.close()
    writer.manager.close()
