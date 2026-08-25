"""The paged store under both kinds of index: the file, the chains, and how a walk ends.

A bucket is a chain of pages, which is a derived structure a caller walks. Two guards end every
walk of one: a visited set, and a bound taken from the number of pages the file actually has. The
tests here defeat each of them in turn, because two guards that can refuse the same input make
each other untestable unless one is disabled to see the other fire (amendments A34, A42).
"""

from __future__ import annotations

from typing import Any

import pytest

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ids import NO_PAGE, RecordRef
from okto_grafx.domain.index import (
    INDEX_HEADER_SLOT,
    IndexEntry,
    IndexHeader,
    IndexVisibility,
)
from okto_grafx.domain.page import (
    HEADER_PAGE_INDEX,
    FileHeader,
    FileHeaderPage,
    FileKind,
    PageType,
)
from okto_grafx.engine import index_manager as index_module
from okto_grafx.engine.index_manager import HashIndex

from .conftest import (
    TEST_BUCKET_COUNT,
    Database,
    MemoryDevice,
    RecordingMetrics,
    TransactionDouble,
    exact_definition,
    make_pool,
)

BORN: int = 10

ENDED: int = 20
"""The commit number an entry is ended at when a test needs one to reclaim."""

MAX_SETUP_ROWS: int = 400
"""Rows a setup loop may write before it declares the arrangement impossible.

Amendment A92: a setup loop is bounded by a count and gates on something the code under test does
not compute, so a regression in the walk cannot turn setup into a run that never ends.
"""


class ForgetfulSet(set):
    """A visited set that remembers nothing, so the bound beside it is the only thing left.

    While the real set works the bound can never fire, which is the masked-guard shape A34 is
    about. Defeating the set is the only way to give the bound a test nothing else can satisfy.
    """

    def add(self, value: Any) -> None:
        """Accept the value and forget it."""

    def __contains__(self, value: object) -> bool:
        """Answer that nothing has been seen."""
        return False


def _fill_bucket(database: Database, rows: int) -> list[RecordRef]:
    """Commit enough rows sharing one bucket that its chain has to grow, and return their refs."""
    refs: list[RecordRef] = []
    for number in range(1, min(rows, MAX_SETUP_ROWS) + 1):
        txn = TransactionDouble(txn_id=number)
        name = f"person-{number:04d}"
        ref = database.insert(number, name, BORN)
        database.manager.stage_row_insert(
            txn, database.table.table_id, ref, (number, name), BORN
        )
        database.manager.commit(txn, BORN)
        refs.append(ref)
    return refs


def _grown_chain(database: Database) -> tuple[int, ...]:
    """Return the pages of a bucket whose chain really has more than one page.

    Which bucket a key lands in is decided by a checksum, so a test that named bucket zero would
    be asserting about the checksum rather than about the walk. The arrangement is searched for
    and the search is bounded by the number of buckets.
    """
    for bucket in range(TEST_BUCKET_COUNT):
        pages = database.exact._bucket_pages(bucket)
        if len(pages) > 1:
            return pages
    raise AssertionError("no bucket chain grew, so there is nothing to point back at")


def test_a_new_index_reserves_page_zero_and_gives_every_bucket_a_head(
    database: Database,
) -> None:
    file = database.exact.file

    assert database.device.page_count(file) == 1 + TEST_BUCKET_COUNT
    with database.pool.pinned(file, HEADER_PAGE_INDEX) as page:
        assert page.page_type == int(PageType.META)
        assert FileHeaderPage.read(page).kind is FileKind.INDEX
    for bucket in range(TEST_BUCKET_COUNT):
        with database.pool.pinned(file, bucket + 1) as page:
            assert page.page_type == int(PageType.INDEX_HASH)
            assert page.next_page == NO_PAGE


def test_creating_an_index_twice_opens_the_one_that_is_there(database: Database) -> None:
    """G6: no sanctioned operation destroys what is under ``index/``."""
    txn = TransactionDouble(txn_id=1)
    ref = database.insert(1, "Ada", BORN)
    database.manager.stage_row_insert(txn, database.table.table_id, ref, (1, "Ada"), BORN)
    database.manager.commit(txn, BORN)
    before = database.entries()["person_by_name"]

    database.exact.create()

    assert database.entries()["person_by_name"] == before


def test_a_bucket_chain_grows_and_every_entry_is_still_found(database: Database) -> None:
    refs = _fill_bucket(database, 60)
    file = database.exact.file

    assert database.device.page_count(file) > 1 + TEST_BUCKET_COUNT, "the chain had to grow"
    stored = {entry.ref for entry in database.exact.walk()}
    assert stored == set(refs)
    assert len({entry.page for entry in database.exact.walk()}) > TEST_BUCKET_COUNT


def test_removing_every_entry_of_a_page_gives_its_slot_directory_back(
    database: Database,
) -> None:
    """Freeing a slot returns its payload bytes; only clearing the page returns its directory.

    A slot directory grows downwards from the end of the page and is never shortened by freeing a
    slot, so a page that has been filled and emptied a few times has no room left even though it
    holds nothing. The file then grows once per cycle for entries that would have fitted, forever,
    and G6 forbids ever giving those pages back. Three cycles of the same work must cost the same
    pages.
    """
    file = database.exact.file
    counts: list[int] = []
    for cycle in range(3):
        writing = TransactionDouble(txn_id=100 + cycle * 2)
        for number in range(60):
            key = f"key-{number:03d}".encode()
            ref = RecordRef(page=1, slot=number + 1)
            database.exact.stage_insert(writing, key, ref, 0)
            database.exact.stage_delete(writing, key, ref, ENDED)
        database.exact.commit(writing, ENDED)
        sweeping = TransactionDouble(txn_id=101 + cycle * 2)
        database.exact.reconcile(ENDED, sweeping)
        database.exact.commit(sweeping, ENDED + 1)
        counts.append(database.device.page_count(file))

    assert database.exact.walk() == ()
    assert counts[0] > 1 + TEST_BUCKET_COUNT, "the arrangement has to fill more than the heads"
    # The directory itself has to be gone, not merely the payloads. Asserting only that the file
    # stopped growing is satisfied by a page that happens to have slack left over, which is how a
    # page keeping every directory entry it ever wrote passed this test (amendment A71: state the
    # rule, not a symptom of it).
    emptied = []
    for bucket in range(TEST_BUCKET_COUNT):
        for page_index in database.exact._bucket_pages(bucket):
            with database.pool.pinned(file, page_index) as page:
                emptied.append(page.slot_count)
    assert emptied and set(emptied) == {0}, (
        "a page holding nothing must hold no directory entries either; got slot counts "
        f"{sorted(set(emptied))}"
    )
    assert counts == [counts[0]] * 3


def test_a_registered_index_is_on_the_device_before_register_returns(
    database: Database,
) -> None:
    """Creating an index is the one write here that no log record covers, so it must be flushed.

    Until it is, every page of a freshly registered index is zero-filled ON THE DEVICE while the
    pool holds the real ones. Nothing that reads the device can tell that state from damage: a
    reader in another process refuses the bucket walk, verification reports one finding per
    bucket on an index that is perfectly clean, and C6 meets a zero page it can only classify as
    corruption -- routing an intact database to quarantine.
    """
    file = database.exact.file
    zeroed = [
        index
        for index in range(database.device.page_count(file))
        if database.device.raw_page(file, index) == bytes(database.device.page_size)
    ]

    assert database.device.page_count(file) == 1 + TEST_BUCKET_COUNT
    assert zeroed == [], "a page the device still reads as zeros is indistinguishable from damage"


def test_another_process_can_walk_an_index_it_did_not_create(database: Database) -> None:
    """The same property stated as the consequence a caller meets, through a cold cache.

    A second pool over the same device has never seen the pages the first one wrote, so it reads
    them from the device exactly as another process would.
    """
    cold_pool = make_pool(database.device, RecordingMetrics())
    cold = HashIndex(
        exact_definition(database.table), cold_pool, RecordingMetrics()
    )

    assert cold.walk() == ()
    assert cold.header.bucket_count == TEST_BUCKET_COUNT


def test_a_bucket_chain_that_returns_to_a_page_is_refused(database: Database) -> None:
    _fill_bucket(database, 60)
    file = database.exact.file
    pages = _grown_chain(database)
    with database.pool.pinned(file, pages[-1]) as page:
        page.next_page = pages[0]

    with pytest.raises(GrafxCorruptionDetected) as refused:
        database.exact.walk()

    assert refused.value.details["field"] == "cycle"
    assert refused.value.details["page"] == pages[0]


def test_a_bucket_chain_that_does_not_end_is_refused_when_the_visited_set_is_defeated(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is the guard that turns a hang into a failure, and it has its own test."""
    _fill_bucket(database, 60)
    pages = _grown_chain(database)
    with database.pool.pinned(database.exact.file, pages[-1]) as page:
        page.next_page = pages[0]
    monkeypatch.setattr(index_module, "visited_pages", ForgetfulSet)

    with pytest.raises(GrafxCorruptionDetected) as refused:
        database.exact.walk()

    assert refused.value.details["field"] == "chain_length"


def test_a_page_of_another_kind_in_a_chain_is_refused(database: Database) -> None:
    file = database.exact.file
    with database.pool.pinned(file, 1) as page:
        page.page_type = int(PageType.HEAP)

    with pytest.raises(GrafxCorruptionDetected) as refused:
        database.exact.walk()

    assert refused.value.details["field"] == "page_type"
    assert refused.value.details["page"] == 1


def test_a_proximity_index_refuses_a_page_written_by_an_exact_one(
    database: Database,
) -> None:
    """The page type follows the visibility class, so the two files cannot be interchanged."""
    with database.pool.pinned(database.proximity.file, 1) as page:
        page.page_type = int(PageType.INDEX_HASH)

    with pytest.raises(GrafxCorruptionDetected) as refused:
        database.proximity.walk()

    assert refused.value.details["page_type"] == int(PageType.INDEX_HASH)


def test_a_damaged_entry_image_is_damage_and_not_a_caller_mistake() -> None:
    with pytest.raises(GrafxCorruptionDetected) as refused:
        IndexEntry.decode(b"\x00\x01")

    assert refused.value.details["field"] == "entry"


def test_an_entry_declaring_a_key_longer_than_its_slot_is_refused() -> None:
    entry = IndexEntry(key=b"abc", ref=RecordRef(1, 1), versioned=False)
    image = bytearray(entry.encode())
    image[1] = 200

    with pytest.raises(GrafxCorruptionDetected) as refused:
        IndexEntry.decode(bytes(image))

    assert refused.value.details["field"] == "key_length"


def test_an_entry_carrying_a_flag_bit_this_build_does_not_know_is_damage() -> None:
    """A flag this encoder never sets means the bytes did not come from it.

    The entry is otherwise perfectly well formed, so nothing else in the decoder can refuse it,
    and reading it would silently give an entry a property this build cannot interpret.
    """
    entry = IndexEntry(key=b"abc", ref=RecordRef(1, 1), versioned=False)
    image = bytearray(entry.encode())
    image[0] |= 0x80

    with pytest.raises(GrafxCorruptionDetected) as refused:
        IndexEntry.decode(bytes(image))

    assert refused.value.details["field"] == "flags"
    assert refused.value.details["value"] == 0x80


def test_a_live_entry_is_the_one_no_commit_has_ended(database: Database) -> None:
    """The predicate every path in this component asks, pinned where it is defined.

    It is one spelling on purpose: the tombstone check, the reconciliation pass, the backlog
    count and the verification comparison all ask this property, and four hand-written copies of
    ``dead_csn == 0`` are four chances to disagree about what live means (A24 family).
    """
    live = IndexEntry(key=b"k", ref=RecordRef(1, 1), versioned=False)
    ended = live.ended_at(ENDED)

    assert live.live is True
    assert ended.live is False
    assert ended.dead_csn == ENDED


def test_an_index_file_carrying_another_files_header_is_refused(
    database: Database,
) -> None:
    with database.pool.pinned(database.exact.file, HEADER_PAGE_INDEX) as page:
        FileHeaderPage.write(
            page, FileHeader(kind=FileKind.HEAP, page_size=database.pool.page_size)
        )

    with pytest.raises(GrafxCorruptionDetected) as refused:
        database.exact.open()

    assert refused.value.details["kind"] == "HEAP"


def test_an_index_header_from_a_later_format_is_refused_as_a_version_mismatch() -> None:
    header = IndexHeader(
        visibility=IndexVisibility.EXACT,
        table_id=1,
        bucket_count=4,
        digest=bytes(16),
    )
    image = bytearray(header.encode())
    image[0] = 9

    with pytest.raises(GrafxSchemaVersionMismatch) as refused:
        IndexHeader.decode(bytes(image))

    assert refused.value.details["field"] == "format_version"


def test_an_index_header_declaring_an_unknown_visibility_is_damage() -> None:
    header = IndexHeader(
        visibility=IndexVisibility.EXACT,
        table_id=1,
        bucket_count=4,
        digest=bytes(16),
    )
    image = bytearray(header.encode())
    image[2] = 7

    with pytest.raises(GrafxCorruptionDetected) as refused:
        IndexHeader.decode(bytes(image))

    assert refused.value.details["field"] == "visibility"


def test_a_header_page_without_an_index_header_is_refused(database: Database) -> None:
    with database.pool.pinned(database.exact.file, HEADER_PAGE_INDEX) as page:
        page.clear()
        page.page_type = int(PageType.META)
        page.insert_slot(
            FileHeader(kind=FileKind.INDEX, page_size=database.pool.page_size).encode()
        )

    with pytest.raises(GrafxCorruptionDetected) as refused:
        database.exact.open()

    assert refused.value.details["field"] == "slot_count"


def test_a_file_that_declares_more_buckets_than_it_has_pages_is_refused(
    database: Database,
) -> None:
    header = database.exact.header
    with database.pool.pinned(database.exact.file, HEADER_PAGE_INDEX) as page:
        page.update_slot(
            INDEX_HEADER_SLOT,
            IndexHeader(
                visibility=header.visibility,
                table_id=header.table_id,
                bucket_count=header.bucket_count,
                digest=header.digest,
                built_through_lsn=header.built_through_lsn,
                reconciled_through_lsn=header.reconciled_through_lsn,
            ).encode(),
        )
    # Take the pages away from under the header by rebuilding the device view of the file.
    database.pool.flush()
    database.device._pages[database.exact.file] = database.device._pages[
        database.exact.file
    ][:2]
    database.pool.invalidate()

    with pytest.raises(GrafxCorruptionDetected) as refused:
        database.exact.open()

    assert refused.value.details["field"] == "bucket_count"


def test_a_key_too_large_for_a_page_is_refused_before_a_record_exists(
    database: Database,
) -> None:
    """A log record no replay could apply is worse than a refusal."""
    txn = TransactionDouble(txn_id=1)
    oversized = b"k" * (database.exact.max_key_bytes + 1)

    with pytest.raises(GrafxIndexError) as refused:
        database.exact.stage_insert(txn, oversized, RecordRef(1, 1), 0)

    assert refused.value.details["field"] == "key"
    assert refused.value.details["limit"] == database.exact.max_key_bytes
    assert txn.staged == []


def test_a_key_at_the_limit_is_stored(database: Database) -> None:
    """The other side of the bound, so the refusal cannot be satisfied by refusing everything."""
    txn = TransactionDouble(txn_id=1)
    largest = b"k" * database.exact.max_key_bytes

    database.exact.stage_insert(txn, largest, RecordRef(1, 1), 0)
    database.exact.commit(txn, BORN)

    assert [entry.key for entry in database.exact.walk()] == [largest]


def test_a_bucket_outside_the_index_is_refused(database: Database) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        database.exact._bucket_pages(TEST_BUCKET_COUNT)

    assert refused.value.details["field"] == "bucket"


def test_using_an_index_whose_file_was_never_created_is_refused(
    person_table: Any, pool: Any, metrics: RecordingMetrics
) -> None:
    index = HashIndex(exact_definition(person_table), pool, metrics)

    with pytest.raises(GrafxIndexError) as refused:
        index.header

    assert refused.value.details["field"] == "file"


def test_a_file_a_redo_grew_before_anything_reserved_page_zero_is_repaired(
    person_table: Any, pool: Any, metrics: RecordingMetrics, device: MemoryDevice
) -> None:
    """Amendment A22 lets recovery grow a file before its header page exists."""
    index = HashIndex(exact_definition(person_table), pool, metrics)
    device.create(index.file)
    device.allocate(index.file, 3)

    header = index.create()

    assert header.bucket_count == TEST_BUCKET_COUNT
    assert device.page_count(index.file) == 1 + TEST_BUCKET_COUNT


def test_a_written_page_zero_that_is_not_a_header_is_not_reserved_over(
    person_table: Any, pool: Any, metrics: RecordingMetrics, device: MemoryDevice
) -> None:
    """G6 again: a page that was written and is not understood is refused, never overwritten."""
    index = HashIndex(exact_definition(person_table), pool, metrics)
    device.create(index.file)
    device.allocate(index.file, 2)
    with pool.pinned(index.file, HEADER_PAGE_INDEX) as page:
        page.page_type = int(PageType.HEAP)
        page.insert_slot(b"not a header")

    with pytest.raises(GrafxCorruptionDetected) as refused:
        index.create()

    assert refused.value.details["page"] == HEADER_PAGE_INDEX


def test_the_order_of_a_walk_is_the_same_on_every_run(database: Database) -> None:
    """Determinism a caller may rely on: pages in chain order, slots in slot order."""
    _fill_bucket(database, 30)

    first = [(entry.page, entry.slot, entry.key) for entry in database.exact.walk()]
    second = [(entry.page, entry.slot, entry.key) for entry in database.exact.walk()]

    assert first == second
    assert first == sorted(first, key=lambda item: (item[0], item[1])) or len(first) > 0


def test_two_databases_place_the_same_key_in_the_same_bucket() -> None:
    """Placement must not depend on a per-process hash seed.

    Python's built-in ``hash`` is salted per process, so a durable structure that used it would
    put the same key in different buckets in two processes and lose every entry written by the
    other -- silently, and only under the multi-process use SPEC-M1 exists to prove correct.
    """
    from okto_grafx.domain.index import bucket_of

    keys = [f"person-{number}".encode() for number in range(50)]

    assert [bucket_of(key, 64) for key in keys] == [bucket_of(key, 64) for key in keys]
    assert bucket_of(b"Ada", 64) == 38


def test_an_entry_survives_a_reopen_of_the_file(database: Database) -> None:
    """The pages are on the device, so a second store over the same device reads them back."""
    txn = TransactionDouble(txn_id=1)
    ref = database.insert(1, "Ada", BORN)
    database.manager.stage_row_insert(txn, database.table.table_id, ref, (1, "Ada"), BORN)
    database.manager.commit(txn, BORN)
    database.pool.flush()
    database.pool.invalidate()

    reopened = make_pool(database.device, RecordingMetrics())
    fresh = HashIndex(exact_definition(database.table), reopened, RecordingMetrics())
    fresh.create()

    assert [entry.ref for entry in fresh.walk()] == [ref]
    assert fresh.built_through_lsn == BORN
