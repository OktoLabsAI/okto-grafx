"""The read-view drop is exempted only for a token this manager itself published (CQ-2/QW-4)."""

from __future__ import annotations

from pathlib import Path

from okto_grafx.domain.txn.commit_state import CommitState
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.wal_manager import WalManager
from txn_support import Stack, build_stack, make_page_image

HEAP = "heap.dat"


def _committed_lsn_of(token: object) -> object:
    """Return the LSN carried by both legacy scalar and CE-3 composite view tokens."""

    return getattr(token, "last_committed_lsn", token)


def _real_wal(device: object, clock: object, metrics: object) -> WalManager:
    """Build C4's log over the same device, so checkpoint's committed replay can read it."""
    manager = WalManager(
        device,  # type: ignore[arg-type]
        clock,  # type: ignore[arg-type]
        metrics,  # type: ignore[arg-type]
        segment_bytes=1 << 20,
        descriptor="hash-v1;partitions_per_table=8",
    )
    manager.open()
    return manager


def _commit_one(stack: Stack, page_index: int, payload: bytes = b"row") -> int:
    txn = stack.manager.begin("write")
    txn.owner._stage_page_image(
        txn,
        HEAP,
        page_index,
        make_page_image(stack.codec, [payload], page_index=page_index),
    )
    txn.note_write(stack.manager.partition_of(1, payload))
    return stack.manager.commit(txn).csn


def _warm(stack: Stack, page_index: int) -> None:
    page = stack.pool.pin(HEAP, page_index)
    stack.pool.unpin(HEAP, page_index, page=page)


def test_a_begin_after_this_managers_own_commit_keeps_the_pool_frames(
    database_root: Path,
) -> None:
    stack = build_stack(database_root)
    _commit_one(stack, 3)
    _warm(stack, 3)
    assert stack.pool.is_resident(HEAP, 3)

    txn = stack.manager.begin("read")
    try:
        # The token moved only by this manager's own publication; the frames are the very
        # committed state this pool produced, so the view keeps them.
        assert stack.pool.is_resident(HEAP, 3)
    finally:
        stack.manager.rollback(txn)


def test_a_begin_after_a_foreign_commit_still_drops_everything(
    database_root: Path,
) -> None:
    stack = build_stack(database_root, owner_id="participant-a")
    _commit_one(stack, 3, b"a-row")
    _warm(stack, 3)

    foreign = build_stack(database_root, owner_id="participant-b")
    _commit_one(foreign, 4, b"b-row")

    txn = stack.manager.begin("read")
    try:
        assert not stack.pool.is_resident(HEAP, 3)  # foreign token: full drop, as today
        page = stack.pool.pin(HEAP, 4)
        try:
            assert page.read_slot(0) == b"b-row"  # and the foreign commit is visible
        finally:
            stack.pool.unpin(HEAP, 4, page=page)
    finally:
        stack.manager.rollback(txn)


def test_a_gap_completed_publication_is_never_own(
    database_root: Path,
    monkeypatch,
) -> None:
    # _complete_committed_gap publishes a FOREIGN commit through _publish; remembering that
    # LSN as own would let the very next view keep frames that predate the foreign commit.
    # The mutant that marks own there records own=True for the gap-published token and fails.
    stack = build_stack(database_root, owner_id="participant-a", wal_factory=_real_wal)
    _commit_one(stack, 3, b"a-row")

    foreign = build_stack(
        database_root, owner_id="participant-b", wal_factory=_real_wal
    )
    committed = _commit_one(foreign, 4, b"b-row")
    durable = foreign.manager._read_commit_state()
    # Simulate the foreign participant dying after its WAL commit became durable and before
    # commit.state was published: roll the publication back to what preceded it.
    rolled = CommitState(
        last_committed_lsn=durable.last_committed_lsn - 1,
        last_csn=durable.last_csn - 1,
        checkpoint_lsn=durable.checkpoint_lsn,
    )
    foreign.manager._commit_state_store.publish(rolled)

    calls: list[tuple[object, bool]] = []
    original = BufferPool.begin_read_view

    def recording(self, token=None, *args, **kwargs):
        calls.append((token, bool(kwargs.get("own", False))))
        return original(self, token, *args, **kwargs)

    monkeypatch.setattr(BufferPool, "begin_read_view", recording)

    # The checkpoint completes the foreign gap and publishes the foreign LSN.
    stack.manager.checkpoint()
    assert stack.manager.published_lsn() == committed

    txn = stack.manager.begin("read")
    stack.manager.rollback(txn)

    gap_token_calls = [
        own for token, own in calls if _committed_lsn_of(token) == committed
    ]
    assert gap_token_calls, "no read view was taken over the gap-published token"
    assert not any(
        gap_token_calls
    )  # the foreign LSN is never own, wherever it is passed


def test_a_checkpoint_publication_forfeits_the_own_provenance(
    database_root: Path,
) -> None:
    # Every generic _publish clears the remembered number; only the next
    # _publish_commit_state re-arms it. A checkpoint therefore costs one full
    # drop on the next begin -- provenance over thrift.
    stack = build_stack(database_root, wal_factory=_real_wal)
    committed = _commit_one(stack, 3)
    assert stack.manager._own_published_lsn == committed

    stack.manager.checkpoint()  # publishes checkpoint_lsn beside the same commit numbers

    assert stack.manager._own_published_lsn is None


def test_a_token_returning_to_an_old_own_number_is_not_own(
    database_root: Path,
    monkeypatch,
) -> None:
    # A foreign recovery can republish exactly the number this manager once produced. The
    # provenance was forfeited the moment a view met a token without it, so the returning
    # number must be met with own=False and a full drop.
    stack = build_stack(database_root, owner_id="participant-a")
    own_lsn = _commit_one(stack, 3, b"a-row")

    foreign = build_stack(database_root, owner_id="participant-b")
    _commit_one(foreign, 4, b"b-row")

    calls: list[tuple[object, bool]] = []
    original = BufferPool.begin_read_view

    def recording(self, token=None, *args, **kwargs):
        calls.append((token, bool(kwargs.get("own", False))))
        return original(self, token, *args, **kwargs)

    monkeypatch.setattr(BufferPool, "begin_read_view", recording)

    txn = stack.manager.begin("read")  # foreign token: provenance forfeited here
    stack.manager.rollback(txn)
    assert stack.manager._own_published_lsn is None

    # The foreign recovery republishes exactly the old own number.
    durable = foreign.manager._read_commit_state()
    foreign.manager._commit_state_store.publish(
        CommitState(
            last_committed_lsn=own_lsn,
            last_csn=own_lsn,
            checkpoint_lsn=durable.checkpoint_lsn,
        )
    )
    _warm(stack, 3)
    txn = stack.manager.begin("read")
    try:
        returning = [own for token, own in calls if _committed_lsn_of(token) == own_lsn]
        assert returning, "no view was taken over the returning token"
        assert not any(returning)  # own=False: the old number has no provenance left
        assert not stack.pool.is_resident(HEAP, 3)  # and the view dropped the frames
    finally:
        stack.manager.rollback(txn)


def test_the_own_token_is_compared_by_equality_never_by_order(
    database_root: Path,
) -> None:
    # A recovery can republish a smaller LSN and a foreign checkpoint republishes the same
    # last_committed_lsn; only strict equality with the remembered publication may exempt.
    stack = build_stack(database_root, owner_id="participant-a")
    _commit_one(stack, 3, b"a-row")
    _warm(stack, 3)

    foreign = build_stack(database_root, owner_id="participant-b")
    _commit_one(foreign, 4, b"b-row")
    _commit_one(foreign, 5, b"c-row")

    txn = stack.manager.begin("read")
    try:
        # Token above the remembered own publication: foreign, everything dropped.
        assert not stack.pool.is_resident(HEAP, 3)
    finally:
        stack.manager.rollback(txn)


def test_real_wal_foreign_begin_discards_only_the_changed_heap_page(
    database_root: Path,
) -> None:
    stack = build_stack(database_root, owner_id="participant-a", wal_factory=_real_wal)
    _commit_one(stack, 3, b"stable-page")
    _commit_one(stack, 4, b"old-page")
    established = stack.manager.begin("read")
    stack.manager.rollback(established)
    _warm(stack, 3)
    _warm(stack, 4)

    foreign = build_stack(
        database_root, owner_id="participant-b", wal_factory=_real_wal
    )
    _commit_one(foreign, 4, b"foreign-page")

    current = stack.manager.begin("read")
    try:
        assert stack.pool.is_resident(HEAP, 3), (
            "the bounded WAL delta dropped an unrelated heap page"
        )
        assert not stack.pool.is_resident(HEAP, 4)
        page = stack.pool.pin(HEAP, 4)
        try:
            assert page.read_slot(0) == b"foreign-page"
        finally:
            stack.pool.unpin(HEAP, 4, page=page)
    finally:
        stack.manager.rollback(current)


def test_real_wal_commit_refresh_discards_only_the_foreign_page(
    database_root: Path,
) -> None:
    stack = build_stack(database_root, owner_id="participant-a", wal_factory=_real_wal)
    _commit_one(stack, 3, b"stable-page")
    _commit_one(stack, 4, b"old-page")
    established = stack.manager.begin("read")
    stack.manager.rollback(established)
    _warm(stack, 3)
    _warm(stack, 4)

    local_payload = b"local-disjoint"
    foreign_payload = next(
        candidate
        for candidate in (b"foreign-a", b"foreign-b", b"foreign-c", b"foreign-d")
        if stack.manager.partition_of(1, candidate)
        != stack.manager.partition_of(1, local_payload)
    )
    local = stack.manager.begin("write")
    local.owner._stage_page_image(
        local,
        HEAP,
        5,
        make_page_image(stack.codec, [local_payload], page_index=5),
    )
    local.note_write(stack.manager.partition_of(1, local_payload))

    foreign = build_stack(
        database_root, owner_id="participant-b", wal_factory=_real_wal
    )
    _commit_one(foreign, 4, foreign_payload)

    report = stack.manager.commit(local)

    assert report.durable
    assert stack.pool.is_resident(HEAP, 3), (
        "the commit-path refresh dropped an unrelated heap page"
    )
    assert not stack.pool.is_resident(HEAP, 4)


def test_real_wal_fresh_page_access_discards_only_the_changed_page(
    database_root: Path,
) -> None:
    stack = build_stack(database_root, owner_id="participant-a", wal_factory=_real_wal)
    _commit_one(stack, 3, b"stable-page")
    _commit_one(stack, 4, b"old-page")
    established = stack.manager.begin("read")
    stack.manager.rollback(established)
    _warm(stack, 3)
    _warm(stack, 4)

    foreign = build_stack(
        database_root, owner_id="participant-b", wal_factory=_real_wal
    )
    _commit_one(foreign, 4, b"foreign-page")

    with stack.manager.page_access_section(fresh_read_view=True):
        assert stack.pool.is_resident(HEAP, 3)
        assert not stack.pool.is_resident(HEAP, 4)
