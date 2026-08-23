"""The page a heap append RELINKS, and the three things that never knew about it.

Found by a multi-process smoke test, not by this suite: three participants appending to one table
left the heap unreadable inside a minute, and ``MATCH (n:Item) RETURN count(*)`` in a fresh
process -- after every writer had exited cleanly and recovery had replayed -- refused with
``corruption_detected`` naming a page "reachable from a chain but never written". Nothing had
crashed. One process could never reproduce it, because one process never abandons an append and
then appends again against a tail another attempt has already moved.

One cause, three consequences. ``_pages_touched_by`` re-derives the pages a commit changed from
where its ROWS landed, and a heap append changes a page no row lands on: the previous last page,
whose ``next_page`` is what makes the new page reachable. So the link was

  * never LOGGED -- a change no redo could reproduce;
  * never DECLARED -- two commits could rewrite one tail page and neither conflict;
  * never UNDONE -- and this is the one that corrupted databases. A refused attempt left the pool
    holding a tail page pointing at the page it had just abandoned, and the next commit of any
    participant flushed that link to the device.

Where an assertion is about stored ROWS it reads them from the device or from a participant that
did not do the writing (LESSONS L16, L23). The reuse and settle tests deliberately read the
writing pool's own cache instead: what they assert is a property OF that pool, and reading it from
anywhere else would be asserting something different.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.domain.errors import GrafxError, GrafxWriteConflict
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page import HEADER_PAGE_INDEX, PageType
from okto_grafx.domain.txn import Snapshot, WalRecordType
from okto_grafx.domain.txn.records import decode_page_write
from okto_grafx.domain.verify.findings import FindingKind
from okto_grafx.engine.verifier import Verifier
from okto_grafx.engine.wal_manager import WalManager
from shared_device import SharedDirectoryDevice
from txn_support import DEFAULT_PAGE_SIZE, ManualClock, Stack, build_stack

HEAP = "heap.dat"
FILLER = "x" * 200


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


def _real_wal(segment_bytes: int = 1 << 20):
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
    root: Path, owner: str, clock: ManualClock, *, frames: int = 1024
) -> Stack:
    """Build a participant, optionally on a pool too small to hold the work it is given.

    The frame budget is a parameter because it decides a REGIME, not a speed. A pool large enough
    to hold everything an attempt touches never evicts, and an eviction is what used to take a
    page out of the answer the commit measured -- so a suite that only ever ran with the default
    budget could not see the defect at all (LESSONS L24, L30).
    """
    return build_stack(
        root,
        storage=SharedDirectoryDevice(root, page_size=DEFAULT_PAGE_SIZE),
        owner_id=owner,
        clock=clock,
        wal_factory=_real_wal(),
        commit_lock_timeout=5.0,
        budget_bytes=frames * DEFAULT_PAGE_SIZE,
    )


def _registered(stack: Stack, definition: TableDef) -> TableDef:
    stack.catalog.catalog.add_table(definition)
    stack.catalog.save()
    return definition


def _insert(stack: Stack, table: TableDef, identity: int, label: str = FILLER) -> None:
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (identity, label))
    txn.note_write(stack.manager.partition_of(table.table_id, str(identity).encode()))
    stack.manager.commit(txn)


def _fill_until_a_second_page(stack: Stack, table: TableDef, start: int = 1) -> int:
    """Insert until the heap has grown past one data page, and return the next free identity.

    The count is discovered rather than assumed: a row size and a page size that agree today
    would stop crossing the boundary the day either changes, and the test would then pass while
    proving nothing.
    """
    identity = start
    while stack.storage.page_count(HEAP) < 3 and identity < start + 400:
        _insert(stack, table, identity)
        identity += 1
    assert stack.storage.page_count(HEAP) >= 3, "the heap never grew a second data page"
    return identity


def _rows(stack: Stack, table: TableDef) -> list[tuple[object, ...]]:
    lsn = stack.manager.published_lsn()
    return sorted(version.values for _ref, version in stack.heap.scan(table, Snapshot(lsn)))


def _verify(stack: Stack):
    """Walk everything this stack opened and return the report."""
    return Verifier(
        stack.pool, stack.metrics, heap=stack.heap, catalog=stack.catalog
    ).verify("all")


# --- the defect: a refused append leaves a link to the page it abandoned ------------------------


def test_a_refused_append_leaves_no_chain_link_to_the_page_it_abandoned(
    tmp_path: Path,
) -> None:
    """A guard on the end state, and NOT a regression test -- it passes without the fix too.

    That is recorded here rather than left for someone to discover, because a test believed to
    pin a defect and which does not is worse than no test: the next person to touch this code
    reads a green run as proof of something it never proved. Scripting the interleaving three
    processes fall into took several tries and none of them reproduced it. What does reproduce
    it is ``tests/smoke/test_concurrent_writers.py``, which fails without the fix; what pins the
    CAUSE is the two tests below, which fail without it as well.

    It is weaker than "passes without the fix" already says, and measuring it is what showed that:
    both participants here declare the SAME key partition, so the refusal lands on the row half of
    validation, before ``_write_rows`` runs. No append is ever refused in it -- ``_abandon_rows``
    is reached with ``rows=0``. The test that reaches the page half, and therefore the undo this
    file exists for, is ``test_a_refusal_on_the_PAGE_half_undoes_the_link_the_append_had_already_
    made`` below.

    What this one is worth keeping for: it is the cheap end-to-end walk -- refusal, retry, a third
    participant advancing the extent, a fresh participant reading the device -- over a path the
    others do not take.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    p1 = _participant(root, "p1", clock)
    table = _registered(p1, _table())
    next_id = _fill_until_a_second_page(p1, table)

    p2 = _participant(root, "p2", clock)
    doomed = p2.manager.begin("write")
    for offset in range(60):
        doomed.stage_row_insert(table, (next_id + offset, FILLER))
        doomed.note_write(p2.manager.partition_of(table.table_id, b"contended"))

    # P1 takes the partition after P2's snapshot, so P2's commit is refused on the row half.
    winner = p1.manager.begin("write")
    winner.stage_row_insert(table, (9001, "p1-wins"))
    winner.note_write(p1.manager.partition_of(table.table_id, b"contended"))
    p1.manager.commit(winner)

    with pytest.raises(GrafxWriteConflict):
        p2.manager.commit(doomed)
    p2.manager.rollback(doomed)

    # P2 retries, which is what a caller told "retryable" does. Its own commit flushes ITS pool,
    # and before the fix that pool still held the tail page pointing at the page the refused
    # attempt had abandoned -- so the retry is what carried the bad link to the device. A test
    # that let some OTHER participant do the flushing never reproduced this, because the stale
    # frame was never in that participant's pool.
    _insert(p2, table, 9002, "after")

    # And now the ingredient a two-participant story is missing. The walk follows next_page to
    # NO_PAGE and its only bound is _chain_limit(), so what decides whether a dangling link is
    # ever FOLLOWED is which tail an append starts from -- and that comes from the extent hint,
    # which the abandonment reverted. Somebody else's append moves the hint past the abandoned
    # page, and that participant is itself the first to walk into the page nobody wrote.
    for identity in range(9100, 9160):
        _insert(p1, table, identity)

    witness = _participant(root, "witness", clock)
    stored = _rows(witness, table)          # must not raise corruption_detected
    assert (9001, "p1-wins") in stored
    assert (9002, "after") in stored
    assert not any(values[0] == next_id for values in stored), "an abandoned row is readable"

    assert _verify(witness).findings == ()


def test_a_refused_append_leaves_nothing_of_itself_dirty_in_the_pool(tmp_path: Path) -> None:
    """The invariant the fix rests on, asserted directly rather than through its consequence.

    Measured and recorded: this one kills no mutant on the changed surface either, for the same
    reason the first test in this file does not -- its refusal lands on the ROW half, so the
    attempt under test writes nothing and both sides of the assertion are the same empty value.
    It stays because the invariant is worth stating where a reader will look for it; what HOLDS
    the invariant is `test_a_refusal_on_the_PAGE_half_...` below, over eight buffer budgets.

    A commit attempt that is refused must leave the pool exactly as it found it. Anything of the
    attempt left dirty is written back by the next flush of ANY participant, at which point the
    device carries a state no transaction ever committed. Asserting the invariant rather than one
    of its symptoms is what makes this test survive a future site that dirties a fourth page.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    p1 = _participant(root, "p1", clock)
    table = _registered(p1, _table())
    next_id = _fill_until_a_second_page(p1, table)

    p2 = _participant(root, "p2", clock)
    p2.pool.forget_modified()
    settled = p2.pool.modified_pages()

    doomed = p2.manager.begin("write")
    for offset in range(60):
        doomed.stage_row_insert(table, (next_id + offset, FILLER))
        doomed.note_write(p2.manager.partition_of(table.table_id, b"contended"))

    winner = p1.manager.begin("write")
    winner.stage_row_insert(table, (9001, "p1-wins"))
    winner.note_write(p1.manager.partition_of(table.table_id, b"contended"))
    p1.manager.commit(winner)

    with pytest.raises(GrafxWriteConflict):
        p2.manager.commit(doomed)
    p2.manager.rollback(doomed)

    assert p2.pool.modified_pages() == settled


@pytest.mark.parametrize("frames", [4, 5, 6, 8, 12, 64, 1024])
def test_the_page_an_append_relinks_is_carried_by_the_log(tmp_path: Path, frames: int) -> None:
    """The link must be in the log, or no redo can reproduce it -- at EVERY buffer budget.

    A commit that allocates a page writes that page AND changes the previous last page, whose
    ``next_page`` is the only thing that makes the new one reachable. The log carried the first
    and not the second, so replaying rebuilt a heap whose every table ended one page early --
    with the rows of that page in the log, present and unreachable.

    The budget matrix is the point of this test and not decoration. The first fix measured the
    pages an attempt changed by reading the pool's DIRTY frames twice; a page the attempt changed
    and the pool then evicted was written back and marked clean, so it left the measurement and
    the log with it. One commit of 400 rows against a 256-frame budget replayed to 2 of 402 rows.
    A single-budget version of this test passed throughout, because the default pool is large
    enough never to evict.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    stack = _participant(root, "solo", clock, frames=frames)
    table = _registered(stack, _table())
    next_id = _fill_until_a_second_page(stack, table)

    before = stack.manager.published_lsn()
    pages_before = stack.storage.page_count(HEAP)
    txn = stack.manager.begin("write")
    for offset in range(60):
        txn.stage_row_insert(table, (next_id + offset, FILLER))
        txn.note_write(stack.manager.partition_of(table.table_id, b"grow"))
    stack.manager.commit(txn)
    assert stack.storage.page_count(HEAP) > pages_before, "this commit did not allocate a page"

    logged = {
        decode_page_write(record.payload).page_index
        for record in stack.wal.read_from(before + 1)
        if record.record_type == WalRecordType.WRITE_PAGE
        and decode_page_write(record.payload).file == HEAP
    }
    # Whatever page the chain now reaches the new one THROUGH must be among them. It is found by
    # walking the device, so the test names no page number of its own.
    reached_through = set()
    for index in range(stack.storage.page_count(HEAP)):
        with stack.pool.pinned(HEAP, index) as page:
            if page.page_type == int(PageType.HEAP) and page.next_page >= pages_before:
                reached_through.add(index)
    assert reached_through, "no page links forward into the pages this commit allocated"
    assert reached_through <= logged, (
        f"pages {sorted(reached_through - logged)} carry the link to the new page and the log "
        f"holds no image of them"
    )


# --- the leak the same omission left behind -----------------------------------------------------


def test_a_page_an_attempt_allocated_and_abandoned_is_handed_out_again(tmp_path: Path) -> None:
    """A refused append must not cost the file a page for the life of the database.

    G6 forbids shrinking a data file, so every refused append that had already grown the file
    left one all-zero page behind for good. Under three participants that was ten to twenty-four
    such pages a minute, each of them a ``page_unwritten`` finding -- a database in which nothing
    had gone wrong reporting itself unclean, which is the false integrity incident A11-revised
    exists to prevent.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    stack = _participant(root, "solo", clock)

    grown = stack.storage.page_count(HEAP)
    first = stack.pool.allocate(HEAP, int(PageType.HEAP))
    index = first.page_index
    stack.pool.unpin(HEAP, index, dirty=True)
    assert stack.storage.page_count(HEAP) == grown + 1

    stack.pool.discard(HEAP, index)          # what an abandoned attempt does
    second = stack.pool.allocate(HEAP, int(PageType.HEAP))
    reused = second.page_index
    stack.pool.unpin(HEAP, reused, dirty=True)

    assert reused == index, "the abandoned page was not handed out again"
    assert stack.storage.page_count(HEAP) == grown + 1, "the file grew for a page it already had"


def test_a_page_already_on_the_device_is_never_handed_out_again(tmp_path: Path) -> None:
    """The narrow condition on reuse, asserted from the side that would lose data if it widened.

    Only a page this pool grew the file for and never wrote back may be reclaimed: such a page
    has never been readable by anybody. A page that reached the device may be referenced by
    anything that has read it since, and handing its index out again would give two tables the
    same page. Discarding it must therefore reclaim nothing.

    This one passes with the production change REVERTED as well, because without the change
    nothing is ever reclaimed and the assertion is trivially true. It earns its place all the
    same: it is the only test here that kills the mutant "drop the withdrawal from `_grown` in
    `_write_back`", which is the line that keeps reuse narrow.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    stack = _participant(root, "solo", clock)

    page = stack.pool.allocate(HEAP, int(PageType.HEAP))
    index = page.page_index
    stack.pool.unpin(HEAP, index, dirty=True)
    stack.pool.flush(HEAP)                   # now it is real
    grown = stack.storage.page_count(HEAP)

    stack.pool.discard(HEAP, index)
    fresh = stack.pool.allocate(HEAP, int(PageType.HEAP))
    stack.pool.unpin(HEAP, fresh.page_index, dirty=True)

    assert fresh.page_index != index, "a page that was on the device was handed out again"
    assert stack.storage.page_count(HEAP) == grown + 1

def test_a_page_abandoned_and_never_reused_is_written_out_as_the_free_page_it_is(
    tmp_path: Path,
) -> None:
    """The residual leak, closed at the only moment it becomes permanent.

    While the pool lives, a page an attempt allocated and abandoned costs nothing: the next
    allocation hands it out again. When the pool stops it can never be reused, and left all zeros
    it is indistinguishable from a page a crash left half-allocated -- so ``verify()`` reports
    ``page_unwritten`` and a database in which nothing went wrong stops reporting clean. Under
    four writer processes that happened about once a run.

    Writing the page settles the ambiguity in the honest direction rather than teaching verify to
    look away: the page IS free, and a FREE page with a valid checksum says so.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    stack = _participant(root, "solo", clock)
    # A real database first. verify("all") speaks about every file this stack opened, and one
    # that nothing has written yet has no header -- so a bare stack answers with findings that
    # have nothing to do with the question being asked.
    table = _registered(stack, _table())
    _insert(stack, table, 1)
    before = {finding.kind for finding in _verify(stack).findings}

    page = stack.pool.allocate(HEAP, int(PageType.HEAP))
    index = page.page_index
    stack.pool.unpin(HEAP, index, dirty=True)
    stack.pool.discard(HEAP, index)                      # what an abandoned attempt does
    assert stack.storage.read_page(HEAP, index) == b"\x00" * stack.storage.page_size
    assert FindingKind.PAGE_UNWRITTEN in {
        finding.kind for finding in _verify(stack).findings
    }, "the abandoned page should be reported while it is still all zeros"

    assert stack.pool.settle_abandoned() == 1
    raw = stack.storage.read_page(HEAP, index)
    assert raw != b"\x00" * stack.storage.page_size, "the abandoned page is still all zeros"

    with stack.pool.pinned(HEAP, index) as settled:
        assert settled.page_type == int(PageType.FREE)
        assert settled.seq % 2 == 0, "a durable image carries an even sequence counter (A21)"

    # Compared against the state BEFORE the page was abandoned, so this asserts the settle
    # removed what the abandonment added and nothing else -- rather than asserting a clean
    # database, which would be asserting something this bare stack was never going to give.
    after = {finding.kind for finding in _verify(stack).findings}
    assert after == before, f"settling changed the findings: {before} -> {after}"
    assert FindingKind.PAGE_UNWRITTEN not in after


def test_settling_a_pool_with_nothing_abandoned_writes_nothing(tmp_path: Path) -> None:
    """The door is a no-op when there is nothing to settle, so close() pays nothing for it.

    Asserted because the alternative -- a door that writes a page every time it is called -- would
    turn every close into a write of pages that are not free, and the failure would be silent.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    stack = _participant(root, "solo", clock)
    _registered(stack, _table())
    _insert(stack, _table(), 1)

    assert stack.pool.settle_abandoned() == 0


def test_a_page_settled_as_free_is_not_handed_out_again_by_this_pool(tmp_path: Path) -> None:
    """Settling ends the reuse, and that is deliberate rather than an oversight.

    A settled page is on the device, and the rule the reclaim rests on is that only a page which
    has NEVER been readable may be handed out again. Settling makes it readable, so it leaves the
    reuse list at the same moment. The cost is one page, once, in a pool that is closing anyway.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    stack = _participant(root, "solo", clock)

    page = stack.pool.allocate(HEAP, int(PageType.HEAP))
    index = page.page_index
    stack.pool.unpin(HEAP, index, dirty=True)
    stack.pool.discard(HEAP, index)
    stack.pool.settle_abandoned()

    fresh = stack.pool.allocate(HEAP, int(PageType.HEAP))
    stack.pool.unpin(HEAP, fresh.page_index, dirty=True)
    assert fresh.page_index != index


def test_growing_a_file_to_an_index_never_spends_a_page_it_meant_to_add(
    tmp_path: Path,
) -> None:
    """``grow_to`` must lengthen the file by exactly what it says it did.

    Its loop waits on ``page_count`` and its return value is how much the file grew, so a
    hand-out from the reuse list satisfies neither: measured before this was closed,
    ``grow_to(pool, HEAP, 4)`` on a 2-page file with one abandoned page returned 4 while the file
    grew by 3, and the abandoned page was spent on the way. Recovery is the caller -- it grows a
    file to reach an index a log record names -- so a return value that describes something other
    than the growth is a number C6 routes decisions on.
    """
    from okto_grafx.engine.buffer_pool import grow_to

    root = tmp_path / "db"
    clock = ManualClock()
    stack = _participant(root, "solo", clock)

    spare = stack.pool.allocate(HEAP, int(PageType.HEAP))
    index = spare.page_index
    stack.pool.unpin(HEAP, index, dirty=True)
    stack.pool.discard(HEAP, index)              # one page waiting on the reuse list

    before = stack.storage.page_count(HEAP)
    target = before + 3
    reported = grow_to(stack.pool, HEAP, target)
    after = stack.storage.page_count(HEAP)

    assert after - before == reported, (
        f"grow_to said it added {reported} pages and the file grew by {after - before}"
    )
    assert after > target - 1


@pytest.mark.parametrize("frames", [4, 12, 24, 40, 59, 62, 64, 1024])
def test_a_refusal_on_the_PAGE_half_undoes_the_link_the_append_had_already_made(
    tmp_path: Path, frames: int
) -> None:
    """The undo half of the fix, reached by the only refusal that can reach it.

    Every other test in this file lands its refusal on the ROW half -- the first
    ``_find_conflict``, which runs BEFORE ``_write_rows``. Nothing has been appended at that point,
    so nothing has been relinked, and ``_abandon_rows`` is handed an empty batch: measured, it is
    reached twice in this whole file, both times with ``rows=0``. Deleting the undo entirely left
    the full 7879-test suite green.

    Reaching it needs the SECOND ``_find_conflict``, the page half. That means the two
    participants must declare DIFFERENT key partitions -- so the row half cannot refuse -- and
    must still collide on a page, which any two row-writing commits do, because every one of them
    writes the table directory on heap page 0. P2 then gets all the way through ``_write_rows``:
    it allocates a page, points the old tail at it, and is refused afterwards.

    What must not survive that refusal is the link. Left in P2's pool it is written to the device
    by P2's own retry, and a walk then follows it into a page nobody ever wrote.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    p1 = _participant(root, "p1", clock, frames=frames)
    table = _registered(p1, _table())
    next_id = _fill_until_a_second_page(p1, table)

    p2 = _participant(root, "p2", clock, frames=frames)
    doomed = p2.manager.begin("write")            # P2's snapshot is taken here
    for offset in range(60):                      # enough to need a page of its own
        doomed.stage_row_insert(table, (next_id + offset, FILLER))
    doomed.note_write(p2.manager.partition_of(table.table_id, b"p2-only"))

    winner = p1.manager.begin("write")
    winner.stage_row_insert(table, (9001, "p1-wins"))
    winner.note_write(p1.manager.partition_of(table.table_id, b"p1-only"))
    p1.manager.commit(winner)

    with pytest.raises(GrafxWriteConflict):
        p2.manager.commit(doomed)                 # refused on the PAGE half, after the append
    p2.manager.rollback(doomed)

    # 1. The participant that was refused can still read its own table.
    assert any(values[0] == 9001 for values in _rows(p2, table))

    # 2. Its retry -- what a caller told "retryable" does -- commits, and flushes ITS pool.
    _insert(p2, table, 9002, "after")
    # And P2 finishes, which is when a pool says what the pages it abandoned actually are. The
    # retry reused one of them; the rest are still all zeros until this runs, and reporting them
    # is CORRECT until it does -- an unwritten page in a live process is exactly the state a
    # crash would leave. Closing is what turns that question into an answer.
    p2.pool.settle_abandoned()

    # 3. A participant that did none of the writing, reading the device.
    witness = _participant(root, "witness", clock)
    stored = _rows(witness, table)
    assert (9001, "p1-wins") in stored
    assert (9002, "after") in stored
    # The WHOLE doomed batch, not just its first row. The pages an eviction had already written
    # carry the LAST rows of the batch, so an assertion on the first id passed at every budget
    # while rows 2 to 60 sat readable on the device.
    doomed_ids = set(range(next_id, next_id + 60))
    leaked = sorted(values[0] for values in stored if values[0] in doomed_ids)
    assert not leaked, f"rows of a refused transaction are readable: {leaked[:8]}"
    assert _verify(witness).findings == ()


def test_allocating_with_reuse_disabled_never_spends_an_abandoned_page(
    tmp_path: Path,
) -> None:
    """``reuse=False`` must lengthen the file, whatever is waiting on the reuse list.

    Three callers depend on it and each for its own reason: ``grow_to`` and
    ``IndexStore._grow_buckets`` wait on a FILE's length, so a hand-out that lengthens nothing
    sends them round again and spends a page on the way; ``write_chain`` builds a list of distinct
    pages that the pool must not inject a duplicate into. Asserting the parameter rather than each
    of the three is deliberate -- it is one behaviour, and a test per caller would leave the
    fourth caller unguarded.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    stack = _participant(root, "solo", clock)

    spare = stack.pool.allocate(HEAP, int(PageType.HEAP))
    abandoned = spare.page_index
    stack.pool.unpin(HEAP, abandoned, dirty=True)
    stack.pool.discard(HEAP, abandoned)          # one page waiting to be reused

    before = stack.storage.page_count(HEAP)
    page = stack.pool.allocate(HEAP, int(PageType.HEAP), reuse=False)
    stack.pool.unpin(HEAP, page.page_index, dirty=True)

    assert page.page_index != abandoned, "reuse=False spent the abandoned page"
    assert stack.storage.page_count(HEAP) == before + 1, "reuse=False did not lengthen the file"

    # And the abandoned page is still waiting, not lost: reuse=False declines it, never drops it.
    reused = stack.pool.allocate(HEAP, int(PageType.HEAP))
    stack.pool.unpin(HEAP, reused.page_index, dirty=True)
    assert reused.page_index == abandoned


def test_the_reserved_header_page_is_never_offered_for_reuse(tmp_path: Path) -> None:
    """Page 0 is the reserved file header of every paged file (A2) and is reclaimable by nobody.

    It cannot reach the reclaim today: the first ``begin()`` invalidates the whole pool, writes
    page 0 back and takes it out of the grown set for good. That is soundness by scheduling. This
    asserts it by construction, because the cost of the guard is one comparison and the cost of
    the scheduling changing is a pool handing out the page that says what the file is.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    stack = _participant(root, "fresh", clock)
    pool = stack.pool

    # Force the state the guard is for, rather than waiting for a schedule that produces it.
    pool._grown.add((HEAP, HEADER_PAGE_INDEX))
    pool._reclaim(HEAP, HEADER_PAGE_INDEX)

    assert HEADER_PAGE_INDEX not in pool._abandoned.get(HEAP, []), (
        "the reserved header page was put on the reuse list"
    )


def test_one_attempt_taking_two_pages_never_gets_the_same_index_twice(
    tmp_path: Path,
) -> None:
    """A page handed out of the reuse list must LEAVE it, or one index gets two owners.

    Every other reuse test here takes exactly one page from the list, and so does the smoke
    workload -- eight rows of 120 bytes fit on one 8192-byte page. That regime cannot see this:
    the second hand-out is what collides. A retry that re-stages a multi-row batch takes two,
    and then two pages of the same chain are the same page, which `refuse_endless_chain` meets
    as ``the page chain of table 'Item' returns to page 7`` -- reproduced through `connect()`
    with three writer processes.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    stack = _participant(root, "solo", clock)

    spare = stack.pool.allocate(HEAP, int(PageType.HEAP))
    abandoned = spare.page_index
    stack.pool.unpin(HEAP, abandoned, dirty=True)
    stack.pool.discard(HEAP, abandoned)          # exactly one page waiting

    first = stack.pool.allocate(HEAP, int(PageType.HEAP))
    stack.pool.unpin(HEAP, first.page_index, dirty=True)
    second = stack.pool.allocate(HEAP, int(PageType.HEAP))
    stack.pool.unpin(HEAP, second.page_index, dirty=True)

    assert first.page_index == abandoned, "the abandoned page was not reused at all"
    assert second.page_index != first.page_index, (
        "the reuse list handed the same index out twice"
    )


def test_a_page_dirty_before_the_attempt_is_not_taken_back_with_it(tmp_path: Path) -> None:
    """The mark subtracts, and this is the direction where getting it wrong LOSES data.

    `_attempt_pages` is the difference between two readings of the pool, and the mark is the
    first one. Without the subtraction the undo of a refused attempt takes back pages that were
    already dirty when it started -- changes the attempt never made and has no business
    reverting.

    Two things had to be arranged for this to be observable, and both say something about the
    regime. The refusal is a heap refusal rather than a write conflict, because a conflict needs
    another participant to commit and that moves the read-view token, which drops every frame and
    empties the mark. And the budget is large, so nothing is evicted and the undo takes the
    DISCARD path -- on the write-back path an over-included page is merely written, which hides
    the mistake rather than answering it.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    stack = _participant(root, "solo", clock, frames=1024)
    table = _registered(stack, _table())

    doomed = stack.manager.begin("write")
    # Dirtied AFTER the transaction opened: begin() refreshes the read view, and that writes back
    # and drops every frame, so anything dirtied before it is gone by the time the mark is taken.
    _registered(stack, _table(table_id=2, name="other"))
    dirty_before = stack.pool.modified_pages("catalog.dat")
    assert dirty_before, "the setup did not leave a page dirty before the attempt"

    doomed.stage_row_insert(table, (1, "fine"))
    doomed.stage_row_insert(table, ("not-an-int", "bad"))   # the heap refuses this one
    doomed.note_write(stack.manager.partition_of(table.table_id, b"1"))
    with pytest.raises(GrafxError):
        stack.manager.commit(doomed)
    stack.manager.rollback(doomed)

    for _file, page_index in dirty_before:
        assert stack.pool.is_resident("catalog.dat", page_index), (
            f"catalog page {page_index} was dirty before the attempt and the undo took it"
        )

    # And it still reaches the device when this participant next writes, which is the consequence.
    stack.pool.flush()
    witness = _participant(root, "witness", clock)
    assert witness.catalog.catalog.table("other").table_id == 2


def test_the_relinked_page_is_declared_as_well_as_logged(tmp_path: Path) -> None:
    """CF-14's second consequence, held on its own rather than through a symptom.

    A page image replaces the WHOLE page, so two commits that write one page conflict however
    disjoint the rows they thought they were touching were -- and that rule is only as good as
    the set that declares them. The page an append relinks was in nobody's interest set, so two
    participants could rewrite one tail page and neither be refused. The commit section makes
    that hard to observe as a symptom today, which is exactly why the declaration is asserted
    directly: a guard whose protection comes from somewhere else is a guard nothing tests.
    """
    root = tmp_path / "db"
    clock = ManualClock()
    stack = _participant(root, "solo", clock)
    table = _registered(stack, _table())
    next_id = _fill_until_a_second_page(stack, table)

    pages_before = stack.storage.page_count(HEAP)

    txn = stack.manager.begin("write")
    for offset in range(60):
        txn.stage_row_insert(table, (next_id + offset, FILLER))
    txn.note_write(stack.manager.partition_of(table.table_id, b"grow"))
    stack.manager.commit(txn)
    # The set the commit validated against, read from the transaction it belongs to.
    declared = set(txn.write_partitions)

    assert stack.storage.page_count(HEAP) > pages_before, "this commit allocated no page"

    from okto_grafx.domain.txn.partitions import page_partition

    reached_through = set()
    for index in range(stack.storage.page_count(HEAP)):
        with stack.pool.pinned(HEAP, index) as page:
            if page.page_type == int(PageType.HEAP) and page.next_page >= pages_before:
                reached_through.add(index)
    assert reached_through, "no page links forward into the pages this commit allocated"
    missing = [
        index
        for index in reached_through
        if page_partition(HEAP, index) not in declared
    ]
    assert not missing, f"pages {missing} carry the link and were declared by nobody"
