"""On-demand verification against seeded corruption (FR-11, AC-12).

The rule this module exists to police is the one CONTRACT.md section 13 calls out by name: a
verifier that certifies damaged state is worse than no verifier. So every test here seeds ONE
class of damage and asserts the verifier reports it -- and the clean-database tests assert not
only that the findings are empty but that the walk actually happened.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.ids import NO_CSN, NO_PAGE, PROVISIONAL_CSN, RecordRef
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RecordHeader
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page.layout import PageType
from okto_grafx.domain.page.slotted import Page
from okto_grafx.domain.verify.findings import (
    SCOPE_ALL,
    SCOPE_INDEXES,
    SCOPE_PAGES,
    SCOPE_RECORDS,
    FindingKind,
    FindingLocation,
    VerificationFinding,
    VerificationReport,
    scope_covers,
)
from okto_grafx.domain.verify.routing import (
    PAGE_REFUSAL_ROUTES,
    REDO_ANSWERS_IT,
    UNCLASSIFIED,
    route_page_refusal,
)
from okto_grafx.engine.verifier import (
    CHECKSUM_FAILURES_TOTAL,
    CHECKSUM_VERIFICATIONS_TOTAL,
    Verifier,
)

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.heap_store import EXTENT_FIRST_SLOT, TableExtent

from .conftest import HEAP_FILE, PAGE_SIZE, Stack

CATALOG_FILE = "catalog.dat"


def _table() -> TableDef:
    """Return one small node table."""
    return TableDef(
        table_id=1,
        name="Person",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="name", type=ValueType.STRING),
        ),
        primary_key="id",
        from_table=None,
        to_table=None,
    )


def _populate(stack: Stack, rows: int = 3) -> TableDef:
    """Put a table and some rows into the database, and return the table."""
    table = _table()
    stack.catalog.catalog.add_table(table)
    stack.catalog.save()
    for index in range(rows):
        stack.heap.insert(
            table,
            index + 1,
            (index + 1, f"row-{index}"),
            xmin=index + 1,
        )
    stack.pool.flush()
    return table


def _rewrite_page(stack: Stack, file: str, page_index: int, mutate) -> None:
    """Read a page off the device, let a test change it, and put it back through the codec."""
    stack.pool.flush()
    raw = stack.storage.read_page(file, page_index)  # type: ignore[attr-defined]
    page = stack.codec.decode_page(raw, verify=True)
    mutate(page)
    stack.storage.write_page(file, page_index, stack.codec.encode_page(page))  # type: ignore[attr-defined]
    stack.pool.invalidate()


def _rewrite_device_counter(
    stack: Stack,
    table: TableDef,
    next_record_id: int,
    *,
    invalidate: bool,
) -> int:
    """Replace one valid device-resident extent counter and return its physical slot."""
    stack.pool.flush()
    raw = stack.storage.read_page(HEAP_FILE, 0)  # type: ignore[attr-defined]
    page = stack.codec.decode_page(raw, verify=True)
    changed = -1
    for slot, payload in page.iter_slots():
        if slot < EXTENT_FIRST_SLOT:
            continue
        extent = TableExtent.decode(payload)
        if extent.table_id == table.table_id:
            page.update_slot(
                slot, replace(extent, next_record_id=next_record_id).encode()
            )
            changed = slot
            break
    assert changed >= EXTENT_FIRST_SLOT, "the table has no physical directory entry"
    stack.storage.write_page(  # type: ignore[attr-defined]
        HEAP_FILE, 0, stack.codec.encode_page(page)
    )
    if invalidate:
        stack.pool.invalidate()
    return changed


def _append_device_extent(stack: Stack, extent: TableExtent) -> int:
    """Append one checksum-valid heap directory entry without consulting HeapStore."""
    stack.pool.flush()
    raw = stack.storage.read_page(HEAP_FILE, 0)  # type: ignore[attr-defined]
    page = stack.codec.decode_page(raw, verify=True)
    slot = page.insert_slot(extent.encode())
    stack.storage.write_page(  # type: ignore[attr-defined]
        HEAP_FILE, 0, stack.codec.encode_page(page)
    )
    stack.pool.invalidate()
    return slot


def _free_device_extent(stack: Stack, table: TableDef) -> int:
    """Free one checksum-valid heap directory slot and return its physical location."""
    stack.pool.flush()
    raw = stack.storage.read_page(HEAP_FILE, 0)  # type: ignore[attr-defined]
    page = stack.codec.decode_page(raw, verify=True)
    changed = -1
    for slot, payload in page.iter_slots():
        if slot >= EXTENT_FIRST_SLOT and TableExtent.decode(payload).table_id == table.table_id:
            page.free_slot(slot)
            changed = slot
            break
    assert changed >= EXTENT_FIRST_SLOT, "the table has no physical directory entry"
    stack.storage.write_page(  # type: ignore[attr-defined]
        HEAP_FILE, 0, stack.codec.encode_page(page)
    )
    stack.pool.invalidate()
    return changed


def _rewrite_first_record_header(stack: Stack, table: TableDef, **changes: int) -> None:
    """Rewrite one reachable header while preserving its payload and a valid page checksum."""
    reference = next(iter(stack.heap.scan_all(table)))[0]

    def rewrite(page: Page) -> None:
        content = page.read_slot(reference.slot)
        header = RecordHeader.decode(content)
        page.update_slot(
            reference.slot,
            replace(header, **changes).encode() + content[RECORD_HEADER_SIZE:],
        )

    _rewrite_page(stack, HEAP_FILE, reference.page, rewrite)


def _append_orphan_header(stack: Stack, table: TableDef, record_id: int) -> int:
    """Write a checksum-valid heap page that claims a table but is outside its chain."""
    page = stack.pool.allocate(HEAP_FILE, int(PageType.HEAP))
    page_index = page.page_index
    try:
        page.insert_slot(table.table_id.to_bytes(4, "little"))
        page.insert_slot(RecordHeader(record_id=record_id, xmin=1).encode())
    finally:
        stack.pool.unpin(HEAP_FILE, page_index, dirty=True)
    stack.pool.flush()
    return page_index


def _append_physical_heap_page(
    stack: Stack,
    descriptor: bytes | None,
    payload: bytes | None,
) -> int:
    """Write a heap page without using HeapStore's descriptor/chain policy.

    ``descriptor=None`` with a payload leaves slot 0 allocated and then frees it, making the
    descriptor unreadable while preserving the adversarial record bytes in slot 1. With both
    arguments ``None`` the page has no slot 0 at all.
    """
    page = stack.pool.allocate(HEAP_FILE, int(PageType.HEAP))
    page_index = page.page_index
    try:
        if descriptor is not None:
            page.insert_slot(descriptor)
        elif payload is not None:
            page.insert_slot(b"descriptor-to-free")
        if payload is not None:
            page.insert_slot(payload)
        if descriptor is None and payload is not None:
            page.free_slot(0)
    finally:
        stack.pool.unpin(HEAP_FILE, page_index, dirty=True)
    stack.pool.flush()
    return page_index


# --- the report shape ------------------------------------------------------------------------------


def test_the_scopes_are_a_closed_set() -> None:
    assert scope_covers(SCOPE_ALL, SCOPE_PAGES) is True
    assert scope_covers(SCOPE_PAGES, SCOPE_RECORDS) is False
    with pytest.raises(GrafxConfigurationError):
        scope_covers("everything", SCOPE_PAGES)
    with pytest.raises(GrafxConfigurationError):
        VerificationReport(scope="everything")


def test_a_finding_must_name_a_kind_and_a_detail() -> None:
    with pytest.raises(GrafxConfigurationError):
        VerificationFinding(kind="", location=FindingLocation(), detail="something")
    with pytest.raises(GrafxConfigurationError):
        VerificationFinding(kind=FindingKind.PAGE_TORN, location=FindingLocation(), detail="")
    with pytest.raises(GrafxConfigurationError):
        VerificationFinding(kind=FindingKind.PAGE_TORN, location="heap.dat", detail="x")  # type: ignore[arg-type]


def test_a_location_describes_itself_in_en_us() -> None:
    location = FindingLocation(file=HEAP_FILE, page=4, slot=2, lsn=9, index="by_name")
    described = location.describe()
    assert "file 'heap.dat'" in described and "page 4" in described and "slot 2" in described
    assert FindingLocation().describe() == "the database"


def test_a_report_that_checked_nothing_is_not_clean() -> None:
    empty = VerificationReport(scope=SCOPE_ALL)
    assert empty.findings == () and empty.clean is False
    walked = VerificationReport(scope=SCOPE_ALL, pages_checked=3)
    assert walked.clean is True


def test_a_verifier_needs_a_real_pool(stack: Stack) -> None:
    with pytest.raises(GrafxConfigurationError):
        Verifier("not a pool", stack.metrics)  # type: ignore[arg-type]


def test_a_scope_that_is_not_one_of_the_four_is_refused(stack: Stack) -> None:
    with pytest.raises(GrafxConfigurationError) as caught:
        stack.verifier().verify("everything")
    assert caught.value.details["field"] == "scope"


# --- a clean database --------------------------------------------------------------------------------


def test_a_clean_database_produces_an_empty_report_and_a_walk_that_happened(
    stack: Stack,
) -> None:
    _populate(stack)
    report = stack.verifier().verify(SCOPE_ALL)
    assert report.findings == ()
    assert report.pages_checked > 0 and report.records_checked == 3
    assert report.clean is True
    assert set(report.files_checked) == {HEAP_FILE, CATALOG_FILE}


def test_each_scope_walks_only_what_it_names(stack: Stack) -> None:
    _populate(stack)
    verifier = stack.verifier()
    pages = verifier.verify(SCOPE_PAGES)
    assert pages.pages_checked > 0 and pages.records_checked == 0
    records = verifier.verify(SCOPE_RECORDS)
    assert records.records_checked == 3 and records.pages_checked == 0
    indexes = verifier.verify(SCOPE_INDEXES)
    assert indexes.index_entries_checked == 0 and indexes.pages_checked == 0


def test_a_durable_counter_equal_to_the_highest_physical_id_is_reported(
    stack: Stack,
) -> None:
    """The field names the NEXT id, so equality would hand an existing identity out again."""
    table = _populate(stack)
    extent_slot = _rewrite_device_counter(stack, table, 3, invalidate=True)
    before = stack.storage.read_page(HEAP_FILE, 0)  # type: ignore[attr-defined]

    report = stack.verifier().verify(SCOPE_RECORDS)

    found = report.findings_of(FindingKind.RECORD_ID_COUNTER)
    assert len(found) == 1
    assert found[0].location == FindingLocation(
        file=HEAP_FILE, page=0, slot=extent_slot
    )
    assert "next_record_id 3" in found[0].detail
    assert "id 3" in found[0].detail
    assert report.records_checked == 3, (
        "the independent oracle must not double-count records"
    )
    assert stack.storage.read_page(HEAP_FILE, 0) == before  # type: ignore[attr-defined]


def test_the_counter_oracle_reads_page_zero_from_the_device_not_the_cache(
    stack: Stack,
) -> None:
    """A good resident page zero must not certify lower, checksum-valid device bytes."""
    table = _populate(stack)
    assert stack.heap.next_record_id(table) == 4, "prime the resident good image"
    _rewrite_device_counter(stack, table, 2, invalidate=False)
    assert stack.heap.next_record_id(table) == 4, "the pool still holds the old image"

    report = stack.verifier().verify(SCOPE_RECORDS)

    found = report.findings_of(FindingKind.RECORD_ID_COUNTER)
    assert len(found) == 1
    assert "next_record_id 2" in found[0].detail and "id 3" in found[0].detail


def test_the_same_lower_durable_counter_is_found_after_the_pool_is_invalidated(
    stack: Stack,
) -> None:
    """The relation itself is checked, independently of the stale-cache regression above."""
    table = _populate(stack)
    _rewrite_device_counter(stack, table, 2, invalidate=True)
    assert stack.heap.next_record_id(table) == 2

    report = stack.verifier().verify(SCOPE_RECORDS)

    assert len(report.findings_of(FindingKind.RECORD_ID_COUNTER)) == 1


def test_a_counter_finding_uses_its_own_extent_slot_in_a_multi_table_directory(
    stack: Stack,
) -> None:
    first = _populate(stack)
    second = replace(_table(), table_id=2, name="Company")
    stack.catalog.catalog.add_table(second)
    stack.catalog.save()
    stack.heap.insert(second, 1, (1, "second-row"), xmin=4)
    stack.pool.flush()
    violated_slot = _rewrite_device_counter(stack, first, 2, invalidate=True)

    raw = stack.storage.read_page(HEAP_FILE, 0)  # type: ignore[attr-defined]
    page = stack.codec.decode_page(raw, verify=True)
    extent_slots = {
        TableExtent.decode(payload).table_id: slot
        for slot, payload in page.iter_slots()
        if slot >= EXTENT_FIRST_SLOT
    }
    assert extent_slots == {first.table_id: 1, second.table_id: 2}
    assert violated_slot == 1

    report = stack.verifier().verify(SCOPE_RECORDS)

    found = report.findings_of(FindingKind.RECORD_ID_COUNTER)
    assert len(found) == 1
    assert found[0].location == FindingLocation(file=HEAP_FILE, page=0, slot=1)
    assert "table 1" in found[0].detail
    assert "next_record_id 2" in found[0].detail and "id 3" in found[0].detail


@pytest.mark.parametrize(
    ("xmin", "xmax"),
    (
        (2, 3),
        (PROVISIONAL_CSN, NO_CSN),
        (NO_CSN, NO_CSN),
    ),
    ids=("ended", "provisional", "no-csn"),
)
def test_every_decodable_physical_lifetime_constrains_the_counter(
    stack: Stack, xmin: int, xmax: int
) -> None:
    """Visibility cannot hide an id from a counter whose sole job is preventing its reuse."""
    table = _populate(stack)
    _rewrite_first_record_header(stack, table, record_id=90, xmin=xmin, xmax=xmax)

    report = stack.verifier().verify(SCOPE_RECORDS)

    found = report.findings_of(FindingKind.RECORD_ID_COUNTER)
    assert len(found) == 1
    assert "id 90" in found[0].detail


def test_a_decodable_header_on_an_orphan_page_constrains_the_counter(
    stack: Stack,
) -> None:
    """Following the table chain would miss the precise residue leasing must not reuse."""
    table = _populate(stack)
    orphan = _append_orphan_header(stack, table, 91)

    report = stack.verifier().verify(SCOPE_RECORDS)

    assert [
        finding.location.page for finding in report.findings_of(FindingKind.ORPHAN_PAGE)
    ] == [orphan]
    counters = report.findings_of(FindingKind.RECORD_ID_COUNTER)
    assert len(counters) == 1 and "id 91" in counters[0].detail
    assert report.records_checked == 3, (
        "the oracle pass is not a second public record count"
    )


def test_the_physical_high_water_is_a_maximum_not_the_last_header_seen(
    stack: Stack,
) -> None:
    """A high id in an earlier slot must not be overwritten by lower later ids."""
    table = _populate(stack)
    _rewrite_first_record_header(stack, table, record_id=100)
    _rewrite_device_counter(stack, table, 50, invalidate=True)

    found = (
        stack.verifier()
        .verify(SCOPE_RECORDS)
        .findings_of(FindingKind.RECORD_ID_COUNTER)
    )

    assert len(found) == 1 and "id 100" in found[0].detail


def test_an_empty_or_ahead_counter_is_legal(stack: Stack) -> None:
    """Gaps are sanctioned; only reuse, never density, is an integrity failure."""
    table = _populate(stack)
    _rewrite_device_counter(stack, table, 100, invalidate=True)
    assert stack.verifier().verify(SCOPE_RECORDS).findings == ()

    empty = TableDef(
        table_id=2,
        name="Empty",
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
        primary_key="id",
        from_table=None,
        to_table=None,
    )
    stack.catalog.catalog.add_table(empty)
    stack.catalog.save()
    # Create its extent without leaving a record behind: allocation spends the range and an
    # empty table may therefore begin arbitrarily far ahead.
    assert stack.heap.allocate_record_id(empty) == 1
    _rewrite_device_counter(stack, empty, 500, invalidate=True)
    assert stack.verifier().verify(SCOPE_RECORDS).findings == ()


@pytest.mark.parametrize("scope", (SCOPE_RECORDS, SCOPE_ALL))
@pytest.mark.parametrize(
    "descriptor",
    (None, b"\x01\x00\x00", b"\x01\x00\x00\x00\xff"),
    ids=("freed", "short", "long"),
)
def test_an_untrustworthy_descriptor_is_located_without_inventing_a_counter_owner(
    stack: Stack, scope: str, descriptor: bytes | None
) -> None:
    """A high id behind malformed ownership metadata must not be assigned to another table."""
    table = _populate(stack)
    assert stack.heap.next_record_id(table) == 4
    page_index = _append_physical_heap_page(
        stack,
        descriptor,
        RecordHeader(record_id=99, xmin=1).encode(),
    )
    before = stack.storage.read_page(HEAP_FILE, page_index)  # type: ignore[attr-defined]

    report = stack.verifier().verify(scope)

    missing = report.findings_at(
        FindingKind.PAGE_DESCRIPTOR_MISSING, HEAP_FILE, page_index
    )
    assert len(missing) == 1
    assert report.findings_at(FindingKind.ORPHAN_PAGE, HEAP_FILE, page_index) == ()
    assert report.findings_of(FindingKind.RECORD_ID_COUNTER) == ()
    assert report.records_checked == 3
    assert report.clean is False
    assert stack.storage.read_page(HEAP_FILE, page_index) == before  # type: ignore[attr-defined]


@pytest.mark.parametrize("scope", (SCOPE_RECORDS, SCOPE_ALL))
def test_a_physical_heap_page_with_no_descriptor_slot_is_never_clean(
    stack: Stack, scope: str
) -> None:
    _populate(stack)
    page_index = _append_physical_heap_page(stack, None, None)

    report = stack.verifier().verify(scope)

    assert len(
        report.findings_at(
            FindingKind.PAGE_DESCRIPTOR_MISSING, HEAP_FILE, page_index
        )
    ) == 1
    assert report.findings_of(FindingKind.RECORD_ID_COUNTER) == ()
    assert report.clean is False


@pytest.mark.parametrize("scope", (SCOPE_RECORDS, SCOPE_ALL))
@pytest.mark.parametrize(
    "payload",
    (RecordHeader(record_id=99, xmin=1).encode(), b"short-record-header"),
    ids=("decodable-header", "short-header"),
)
def test_an_exact_descriptor_without_a_directory_owner_is_a_located_orphan(
    stack: Stack, scope: str, payload: bytes
) -> None:
    _populate(stack)
    page_index = _append_physical_heap_page(
        stack, (999).to_bytes(4, "little"), payload
    )

    report = stack.verifier().verify(scope)

    orphans = report.findings_at(FindingKind.ORPHAN_PAGE, HEAP_FILE, page_index)
    assert len(orphans) == 1
    assert "table 999" in orphans[0].detail
    assert report.findings_at(
        FindingKind.PAGE_DESCRIPTOR_MISSING, HEAP_FILE, page_index
    ) == ()
    assert report.findings_of(FindingKind.RECORD_ID_COUNTER) == ()
    assert report.records_checked == 3
    assert report.clean is False


@pytest.mark.parametrize("scope", (SCOPE_RECORDS, SCOPE_ALL))
def test_a_freed_extent_reports_each_physical_orphan_page_only_once(
    stack: Stack, scope: str
) -> None:
    table = _populate(stack)
    physical_page = stack.heap.pages_of(table)[0]
    assert _free_device_extent(stack, table) == 1

    report = stack.verifier().verify(scope)

    orphans = report.findings_at(FindingKind.ORPHAN_PAGE, HEAP_FILE, physical_page)
    assert len(orphans) == 1
    assert report.clean is False


@pytest.mark.parametrize("scope", (SCOPE_RECORDS, SCOPE_ALL))
def test_an_extent_and_page_without_a_catalog_table_are_both_located(
    stack: Stack, scope: str
) -> None:
    _populate(stack)
    page_index = _append_physical_heap_page(
        stack,
        (999).to_bytes(4, "little"),
        RecordHeader(record_id=99, xmin=1).encode(),
    )
    extent_slot = _append_device_extent(
        stack,
        TableExtent(
            table_id=999,
            first_page=page_index,
            last_page=page_index,
            page_count=1,
            next_record_id=100,
        ),
    )

    report = stack.verifier().verify(scope)

    missing_table = [
        finding
        for finding in report.findings_of(FindingKind.TABLE_UNREADABLE)
        if finding.location.slot == extent_slot
    ]
    assert len(missing_table) == 1
    assert "catalog" in missing_table[0].detail
    assert len(report.findings_at(FindingKind.ORPHAN_PAGE, HEAP_FILE, page_index)) == 1
    assert report.findings_of(FindingKind.RECORD_ID_COUNTER) == ()
    assert report.records_checked == 3
    assert report.clean is False


@pytest.mark.parametrize("scope", (SCOPE_RECORDS, SCOPE_ALL))
def test_duplicate_directory_extents_are_unreadable_and_never_choose_a_counter(
    stack: Stack, scope: str
) -> None:
    table = _populate(stack)
    extent = stack.heap.extent_of(table)
    assert extent is not None
    duplicate_slot = _append_device_extent(
        stack, replace(extent, next_record_id=1)
    )

    report = stack.verifier().verify(scope)

    duplicates = [
        finding
        for finding in report.findings_of(FindingKind.TABLE_UNREADABLE)
        if finding.location.slot == duplicate_slot
    ]
    assert len(duplicates) == 1
    assert "duplicate heap directory entries" in duplicates[0].detail
    assert report.findings_of(FindingKind.RECORD_ID_COUNTER) == ()
    assert report.records_checked == 3
    assert report.clean is False


def test_an_intentionally_absent_catalog_does_not_invent_missing_table_findings(
    stack: Stack,
) -> None:
    _populate(stack)
    page_index = _append_physical_heap_page(
        stack,
        (999).to_bytes(4, "little"),
        RecordHeader(record_id=99, xmin=1).encode(),
    )
    _append_device_extent(
        stack,
        TableExtent(
            table_id=999,
            first_page=page_index,
            last_page=page_index,
            page_count=1,
            next_record_id=100,
        ),
    )

    report = stack.verifier(catalog=None).verify(SCOPE_RECORDS)

    assert report.findings_of(FindingKind.TABLE_UNREADABLE) == ()
    assert report.findings_of(FindingKind.ORPHAN_PAGE) == ()
    assert report.findings_of(FindingKind.RECORD_ID_COUNTER) == ()


def test_the_record_counter_check_belongs_only_to_records_and_all(stack: Stack) -> None:
    table = _populate(stack)
    _rewrite_device_counter(stack, table, 2, invalidate=True)

    pages = stack.verifier().verify(SCOPE_PAGES)
    indexes = stack.verifier().verify(SCOPE_INDEXES)
    records = stack.verifier().verify(SCOPE_RECORDS)
    everything = stack.verifier().verify(SCOPE_ALL)

    assert pages.findings_of(FindingKind.RECORD_ID_COUNTER) == ()
    assert indexes.findings_of(FindingKind.RECORD_ID_COUNTER) == ()
    assert len(records.findings_of(FindingKind.RECORD_ID_COUNTER)) == 1
    assert len(everything.findings_of(FindingKind.RECORD_ID_COUNTER)) == 1
    assert pages.records_checked == indexes.records_checked == 0


def test_the_page_walk_counts_every_checksum_it_took(stack: Stack) -> None:
    _populate(stack)
    report = stack.verifier().verify(SCOPE_PAGES)
    taken = stack.metrics.counter(CHECKSUM_VERIFICATIONS_TOTAL, kind="page")
    assert taken >= report.pages_checked
    assert stack.metrics.counter(CHECKSUM_FAILURES_TOTAL, kind="page") == 0.0


# --- seeded corruption, class by class (AC-12) ------------------------------------------------------


def test_a_damaged_page_checksum_is_reported_with_its_location(stack: Stack) -> None:
    _populate(stack)
    stack.pool.flush()
    raw = bytearray(stack.storage.read_page(HEAP_FILE, 1))  # type: ignore[attr-defined]
    raw[100] ^= 0xFF
    stack.storage.write_page(HEAP_FILE, 1, bytes(raw))  # type: ignore[attr-defined]
    stack.pool.invalidate()
    report = stack.verifier().verify(SCOPE_PAGES)
    assert report.clean is False
    damaged = report.findings_of(FindingKind.PAGE_CHECKSUM)
    assert len(damaged) == 1
    assert damaged[0].location.file == HEAP_FILE and damaged[0].location.page == 1
    assert stack.metrics.counter(CHECKSUM_FAILURES_TOTAL, kind="page") == 1.0


def test_a_page_left_mid_write_is_reported_as_torn(stack: Stack) -> None:
    _populate(stack)
    _rewrite_page(stack, HEAP_FILE, 1, lambda page: setattr(page, "seq", page.seq + 1))
    report = stack.verifier().verify(SCOPE_PAGES)
    torn = report.findings_of(FindingKind.PAGE_TORN)
    assert torn and torn[0].location.page == 1


def test_a_page_declaring_a_type_the_layout_does_not_define_is_reported(stack: Stack) -> None:
    _populate(stack)
    _rewrite_page(stack, HEAP_FILE, 1, lambda page: setattr(page, "page_type", 99))
    report = stack.verifier().verify(SCOPE_PAGES)
    wrong = report.findings_of(FindingKind.PAGE_TYPE)
    assert wrong and wrong[0].location.page == 1


def test_a_page_zero_that_is_not_a_header_page_is_reported(stack: Stack) -> None:
    _populate(stack)
    _rewrite_page(stack, HEAP_FILE, 0, lambda page: setattr(page, "page_type", int(PageType.HEAP)))
    report = stack.verifier().verify(SCOPE_PAGES)
    assert report.findings_of(FindingKind.FILE_HEADER)


def test_a_record_whose_declared_length_disagrees_with_its_slot_is_reported(
    stack: Stack,
) -> None:
    table = _populate(stack)
    ref = next(iter(stack.heap.scan_all(table)))[0]

    def shrink(page: Page) -> None:
        """Rewrite the record header to claim a payload longer than the slot holds."""
        content = bytearray(page.read_slot(ref.slot))
        header = RecordHeader.decode(bytes(content))
        content[0:RECORD_HEADER_SIZE] = RecordHeader(
            record_id=header.record_id,
            xmin=header.xmin,
            xmax=header.xmax,
            prev_version=header.prev_version,
            payload_len=header.payload_len + 32,
            schema_version=header.schema_version,
            flags=header.flags,
        ).encode()
        page.update_slot(ref.slot, bytes(content))

    _rewrite_page(stack, HEAP_FILE, ref.page, shrink)
    report = stack.verifier().verify(SCOPE_RECORDS)
    assert report.clean is False
    lengths = report.findings_of(FindingKind.RECORD_LENGTH)
    assert lengths and lengths[0].location.slot == ref.slot


def test_a_freed_slot_is_not_reported_as_a_damaged_record(stack: Stack) -> None:
    """A page can hold freed slots; reading one would raise, and reporting it would be a lie."""
    table = _populate(stack, rows=3)
    ref = list(stack.heap.scan_all(table))[1][0]

    def free(page: Page) -> None:
        """Free the middle slot, leaving the page perfectly valid."""
        page.free_slot(ref.slot)

    _rewrite_page(stack, HEAP_FILE, ref.page, free)
    report = stack.verifier().verify(SCOPE_RECORDS)
    assert report.findings == ()
    assert report.records_checked == 2
    assert report.clean is True


def test_a_record_that_ends_before_it_begins_is_reported(stack: Stack) -> None:
    table = _populate(stack)
    ref = next(iter(stack.heap.scan_all(table)))[0]

    def invert(page: Page) -> None:
        """Give the record an end older than its beginning."""
        content = bytearray(page.read_slot(ref.slot))
        header = RecordHeader.decode(bytes(content))
        content[0:RECORD_HEADER_SIZE] = RecordHeader(
            record_id=header.record_id,
            xmin=10,
            xmax=2,
            prev_version=header.prev_version,
            payload_len=header.payload_len,
            schema_version=header.schema_version,
            flags=header.flags,
        ).encode()
        page.update_slot(ref.slot, bytes(content))

    _rewrite_page(stack, HEAP_FILE, ref.page, invert)
    report = stack.verifier().verify(SCOPE_RECORDS)
    lifetimes = report.findings_of(FindingKind.RECORD_LIFETIME)
    assert lifetimes and lifetimes[0].location.page == ref.page


def test_a_page_of_a_table_unreachable_from_its_chain_is_reported_as_an_orphan(
    stack: Stack,
) -> None:
    """The independent oracle: a full scan sees a page the chain walk cannot reach.

    This is the case C1's punch list records as having been certified clean by a check that
    followed the same links the damage had rewritten. Here the two answers come from different
    places -- one walks the chain, the other reads every page's own descriptor -- so damaging one
    cannot make the other agree with it.
    """
    table = _populate(stack, rows=1)
    chain = stack.heap.pages_of(table)
    assert chain
    stack.pool.flush()
    orphan = stack.storage.allocate(HEAP_FILE, 1)  # type: ignore[attr-defined]
    page = Page(int(PageType.HEAP), page_size=PAGE_SIZE, page_index=orphan)
    page.insert_slot(table.table_id.to_bytes(4, "little"))
    page.next_page = NO_PAGE
    stack.storage.write_page(HEAP_FILE, orphan, stack.codec.encode_page(page))  # type: ignore[attr-defined]
    stack.pool.invalidate()
    report = stack.verifier().verify(SCOPE_RECORDS)
    assert report.clean is False
    orphans = report.findings_of(FindingKind.ORPHAN_PAGE)
    assert orphans and orphans[0].location.page == orphan


def test_a_directory_hint_that_disagrees_with_the_chain_is_reported_as_drift(
    stack: Stack,
) -> None:
    table = _populate(stack, rows=1)
    extent = stack.heap.extent_of(table)
    assert extent is not None
    stack.pool.flush()

    def bend(page: Page) -> None:
        """Rewrite the directory entry so its tail names a page the chain does not end at."""
        from okto_grafx.engine.heap_store import TableExtent

        for slot in range(1, page.slot_count):
            stored = TableExtent.decode(page.read_slot(slot))
            if stored.table_id == table.table_id:
                page.update_slot(
                    slot,
                    TableExtent(
                        table_id=stored.table_id,
                        first_page=stored.first_page,
                        last_page=stored.last_page,
                        page_count=stored.page_count + 5,
                    ).encode(),
                )
                return

    _rewrite_page(stack, HEAP_FILE, 0, bend)
    report = stack.verifier().verify(SCOPE_RECORDS)
    drift = report.findings_of(FindingKind.EXTENT_DRIFT)
    assert drift and "drift" in drift[0].detail


def test_a_directory_tail_that_is_not_the_chain_tail_is_reported_on_its_own(
    stack: Stack,
) -> None:
    """A62: the tail check and the count check must each have a case only it can answer.

    The sibling test bends the page COUNT, which the count check catches, so it says nothing
    about the tail check. This one bends only ``last_page``, leaving the count correct, so the
    tail check is the single guard that can produce a finding here.
    """
    from okto_grafx.engine.heap_store import TableExtent

    table = _populate(stack, rows=1)
    chain = stack.heap.pages_of(table)
    assert len(chain) == 1
    stack.pool.flush()

    def bend(page: Page) -> None:
        """Point the directory tail at a page the chain does not end at."""
        for slot in range(1, page.slot_count):
            stored = TableExtent.decode(page.read_slot(slot))
            if stored.table_id == table.table_id:
                page.update_slot(
                    slot,
                    TableExtent(
                        table_id=stored.table_id,
                        first_page=stored.first_page,
                        last_page=stored.last_page + 7,
                        page_count=stored.page_count,
                    ).encode(),
                )
                return

    _rewrite_page(stack, HEAP_FILE, 0, bend)
    report = stack.verifier().verify(SCOPE_RECORDS)
    drift = report.findings_of(FindingKind.EXTENT_DRIFT)
    assert len(drift) == 1
    assert "as the tail" in drift[0].detail


def test_a_table_whose_chain_cannot_be_walked_is_reported_and_the_walk_goes_on(
    stack: Stack,
) -> None:
    table = _populate(stack, rows=1)
    chain = stack.heap.pages_of(table)
    _rewrite_page(stack, HEAP_FILE, chain[0], lambda page: setattr(page, "next_page", chain[0]))
    report = stack.verifier().verify(SCOPE_RECORDS)
    assert report.clean is False
    assert report.findings_of(FindingKind.TABLE_UNREADABLE)


def test_a_catalog_that_cannot_be_read_is_reported_rather_than_raised(stack: Stack) -> None:
    _populate(stack)
    stack.pool.flush()
    raw = bytearray(stack.storage.read_page(CATALOG_FILE, 1))  # type: ignore[attr-defined]
    raw[80] ^= 0xFF
    stack.storage.write_page(CATALOG_FILE, 1, bytes(raw))  # type: ignore[attr-defined]
    stack.pool.invalidate()
    report = stack.verifier().verify(SCOPE_RECORDS)
    assert report.findings_of(FindingKind.CATALOG_UNREADABLE)


# --- routing C1's four page states (the ask this component made, spent) ---------------------


def test_the_page_refusal_routes_are_the_four_keys_c1_publishes() -> None:
    """A56/A68: pinned as an EXACT set, so widening it fails rather than passing quietly.

    Widening this map is the single edit that would let a page state nobody has classified be
    reported as one that has been, which is how a verifier comes to certify damage.
    """
    assert set(PAGE_REFUSAL_ROUTES) == {
        "unwritten_page",
        "page_type",
        "missing_page_descriptor",
        "freed_slot",
    }
    assert REDO_ANSWERS_IT == frozenset({"unwritten_page"})


def test_a_page_nobody_wrote_is_reported_as_awaiting_its_redo_and_not_as_damage() -> None:
    kind, is_damage = route_page_refusal(
        GrafxCorruptionDetected("never written", field="unwritten_page", page=4)
    )
    assert kind == FindingKind.PAGE_UNWRITTEN
    assert is_damage is False


def test_a_page_written_with_the_wrong_type_is_damage() -> None:
    kind, is_damage = route_page_refusal(
        GrafxCorruptionDetected("wrong type", field="page_type", page=4)
    )
    assert kind == FindingKind.PAGE_TYPE
    assert is_damage is True


def test_a_page_with_no_descriptor_is_damage_and_has_its_own_kind() -> None:
    kind, is_damage = route_page_refusal(
        GrafxCorruptionDetected("no descriptor", field="missing_page_descriptor", page=4)
    )
    assert kind == FindingKind.PAGE_DESCRIPTOR_MISSING
    assert is_damage is True


def test_a_refusal_this_build_cannot_route_is_named_unclassified_and_never_guessed() -> None:
    for failure in (
        GrafxCorruptionDetected("something new", field="a_state_from_a_later_build"),
        GrafxCorruptionDetected("no field at all"),
    ):
        kind, is_damage = route_page_refusal(failure)
        assert kind == UNCLASSIFIED
        assert is_damage is True


def test_routing_something_that_is_not_a_grafx_error_is_refused() -> None:
    from okto_grafx.domain.errors import GrafxUnsupportedOperation

    with pytest.raises(GrafxUnsupportedOperation):
        route_page_refusal(ValueError("not a Grafx error"))  # type: ignore[arg-type]


def test_an_unwritten_page_in_a_chain_is_reported_as_awaiting_redo(stack: Stack) -> None:
    """End to end through the real heap: the state a crash mid-allocation leaves."""
    table = _populate(stack, rows=1)
    chain = stack.heap.pages_of(table)
    stack.pool.flush()
    stranded = stack.storage.allocate(HEAP_FILE, 1)  # type: ignore[attr-defined]
    _rewrite_page(stack, HEAP_FILE, chain[-1], lambda page: setattr(page, "next_page", stranded))
    report = stack.verifier().verify(SCOPE_RECORDS)
    awaiting = report.findings_of(FindingKind.PAGE_UNWRITTEN)
    assert awaiting, [finding.kind for finding in report.findings]
    assert "replaying the log repairs it" in awaiting[0].detail
    assert report.findings_of(FindingKind.TABLE_UNREADABLE) == ()


def test_a_heap_page_with_no_descriptor_is_reported_as_damage(stack: Stack) -> None:
    """The third state C1 delivered a key for, routed to its own finding rather than guessed."""
    table = _populate(stack, rows=1)
    chain = stack.heap.pages_of(table)
    stack.pool.flush()
    bare = stack.storage.allocate(HEAP_FILE, 1)  # type: ignore[attr-defined]
    written = Page(int(PageType.HEAP), page_size=PAGE_SIZE, page_index=bare)
    written.next_page = NO_PAGE
    stack.storage.write_page(HEAP_FILE, bare, stack.codec.encode_page(written))  # type: ignore[attr-defined]
    _rewrite_page(stack, HEAP_FILE, chain[-1], lambda page: setattr(page, "next_page", bare))
    report = stack.verifier().verify(SCOPE_RECORDS)
    assert len(
        report.findings_at(FindingKind.PAGE_DESCRIPTOR_MISSING, HEAP_FILE, bare)
    ) == 1
    assert report.findings_of(FindingKind.TABLE_UNREADABLE) == ()
    assert report.clean is False


# --- index against heap ------------------------------------------------------------------------------


class _Entry:
    """One index entry, shaped like the one C7's walk yields."""

    def __init__(self, ref: RecordRef, *, page: int = 1, slot: int = 0) -> None:
        """Keep the heap location this entry points at and where the entry itself lives."""
        self.key = b"k"
        self.ref = ref
        self.versioned = False
        self.page = page
        self.slot = slot


class _Index:
    """A secondary index standing in for C7's, walked exactly as the contract says."""

    def __init__(
        self, name: str, entries: list[_Entry], definition: object | None = None
    ) -> None:
        """Keep the entries this index will yield and the definition it publishes."""
        self.name = name
        self.file = ""
        self.definition = definition
        self._entries = entries

    def walk(self) -> list[_Entry]:
        """Yield every stored entry, for verification."""
        return list(self._entries)


def test_an_index_entry_that_resolves_in_the_heap_is_not_a_finding(stack: Stack) -> None:
    table = _populate(stack, rows=1)
    ref = next(iter(stack.heap.scan_all(table)))[0]
    index = _Index("by_id", [_Entry(ref)])
    report = stack.verifier(indexes=[index]).verify(SCOPE_INDEXES)
    assert report.index_entries_checked == 1
    assert report.clean is True


def test_an_index_entry_that_points_nowhere_in_the_heap_is_reported(stack: Stack) -> None:
    _populate(stack, rows=1)
    index = _Index("by_id", [_Entry(RecordRef(900, 4))])
    report = stack.verifier(indexes=[index]).verify(SCOPE_INDEXES)
    assert report.clean is False
    unresolved = report.findings_of(FindingKind.INDEX_ENTRY_UNRESOLVED)
    assert unresolved and unresolved[0].location.index == "by_id"


def test_an_index_entry_with_no_heap_location_at_all_is_reported_as_malformed(
    stack: Stack,
) -> None:
    """A62: an entry carrying no reference must be tellable from one that fails to resolve."""
    _populate(stack, rows=1)
    index = _Index("by_id", [_Entry(RecordRef(NO_PAGE, 0))])
    report = stack.verifier(indexes=[index]).verify(SCOPE_INDEXES)
    assert report.findings_of(FindingKind.INDEX_ENTRY_MALFORMED)
    assert report.findings_of(FindingKind.INDEX_ENTRY_UNRESOLVED) == ()


def test_an_index_that_cannot_be_walked_is_reported_rather_than_raised(stack: Stack) -> None:
    class Broken:
        """An index whose walk is not offered at all."""

        name = "broken"

    report = stack.verifier(indexes=[Broken()]).verify(SCOPE_INDEXES)
    assert report.findings_of(FindingKind.INDEX_UNREADABLE)


def test_a_live_row_with_no_index_entry_is_reported(stack: Stack) -> None:
    """The other half of AC-12: a missing entry makes a row that exists disappear (BR-11)."""
    from okto_grafx.domain.index.definition import IndexDefinition
    from okto_grafx.domain.index.keys import index_key
    from okto_grafx.domain.index.visibility import IndexVisibility

    table = _populate(stack, rows=2)
    definition = IndexDefinition(
        name="by_id",
        table_id=table.table_id,
        table_name=table.name,
        positions=(0,),
        visibility=IndexVisibility.EXACT,
    )
    rows = list(stack.heap.scan_all(table))
    assert len(rows) == 2
    kept_ref, kept_version = rows[0]
    entries = [_Entry(kept_ref)]
    entries[0].key = index_key(kept_version.values, definition.positions)
    index = _Index("by_id", entries, definition)
    report = stack.verifier(indexes=[index]).verify(SCOPE_INDEXES)
    assert report.clean is False
    missing = report.findings_of(FindingKind.INDEX_ENTRY_MISSING)
    assert len(missing) == 1
    assert missing[0].location.page == rows[1][0].page
    assert missing[0].location.slot == rows[1][0].slot


def test_an_index_that_covers_every_live_row_reports_nothing(stack: Stack) -> None:
    from okto_grafx.domain.index.definition import IndexDefinition
    from okto_grafx.domain.index.keys import index_key
    from okto_grafx.domain.index.visibility import IndexVisibility

    table = _populate(stack, rows=2)
    definition = IndexDefinition(
        name="by_id",
        table_id=table.table_id,
        table_name=table.name,
        positions=(0,),
        visibility=IndexVisibility.EXACT,
    )
    entries = []
    for ref, version in stack.heap.scan_all(table):
        entry = _Entry(ref)
        entry.key = index_key(version.values, definition.positions)
        entries.append(entry)
    index = _Index("by_id", entries, definition)
    report = stack.verifier(indexes=[index]).verify(SCOPE_INDEXES)
    assert report.findings == ()
    assert report.index_entries_checked == 2


def test_an_index_that_publishes_no_definition_is_only_checked_from_its_entries(
    stack: Stack,
) -> None:
    table = _populate(stack, rows=2)
    ref = next(iter(stack.heap.scan_all(table)))[0]
    index = _Index("by_id", [_Entry(ref)])
    report = stack.verifier(indexes=[index]).verify(SCOPE_INDEXES)
    assert report.findings_of(FindingKind.INDEX_ENTRY_MISSING) == ()
    assert report.clean is True


def test_the_indexes_scope_walks_every_index_it_was_given(stack: Stack) -> None:
    table = _populate(stack, rows=1)
    ref = next(iter(stack.heap.scan_all(table)))[0]
    first = _Index("by_id", [_Entry(ref)])
    second = _Index("by_name", [_Entry(ref), _Entry(RecordRef(900, 1))])
    report = stack.verifier(indexes=[first, second]).verify(SCOPE_ALL)
    assert report.index_entries_checked == 3
    assert len(report.findings_of(FindingKind.INDEX_ENTRY_UNRESOLVED)) == 1


# --- the verifier never changes what it describes -------------------------------------------------------


def test_verification_leaves_every_file_byte_identical(stack: Stack) -> None:
    from .conftest import digest_of_file

    _populate(stack)
    stack.pool.flush()
    before = (digest_of_file(stack.storage, HEAP_FILE), digest_of_file(stack.storage, CATALOG_FILE))
    stack.verifier().verify(SCOPE_ALL)
    stack.pool.flush()
    after = (digest_of_file(stack.storage, HEAP_FILE), digest_of_file(stack.storage, CATALOG_FILE))
    assert after == before


def test_the_page_walk_reads_the_device_and_not_the_cache(stack: Stack) -> None:
    """A cached page would hide damage on the platter, so the walk must not use one."""
    _populate(stack)
    stack.pool.flush()
    with stack.pool.pinned(HEAP_FILE, 1) as page:
        assert page.page_type == int(PageType.HEAP)
    raw = bytearray(stack.storage.read_page(HEAP_FILE, 1))  # type: ignore[attr-defined]
    raw[120] ^= 0xFF
    stack.storage.write_page(HEAP_FILE, 1, bytes(raw))  # type: ignore[attr-defined]
    # The pool is deliberately NOT invalidated: the good image is still resident.
    report = stack.verifier().verify(SCOPE_PAGES)
    assert report.findings_of(FindingKind.PAGE_CHECKSUM)


def test_a_database_with_no_stores_wired_still_verifies_its_pages(stack: Stack) -> None:
    _populate(stack)
    report = Verifier(stack.pool, stack.metrics).verify(SCOPE_ALL)
    assert report.pages_checked > 0 and report.records_checked == 0
    assert report.clean is True


# --- an allocated page nobody wrote is not damage (round-3 blocking defect B1) ---------------------


def test_a_page_allocated_and_never_written_is_not_reported_as_damage(stack: Stack) -> None:
    """The state a crash between PAGE_ALLOC and the WRITE_PAGE that was to follow it leaves.

    Reached through C1's own door -- ``pool.allocate`` -- rather than by writing zeros, so the
    image under test is the one an ordinary crash produces. Handing those bytes to the codec
    answers ``corruption_detected``, which FR-8/FR-10 route to truncation, quarantine and a
    forensic ledger entry; the page is intact, and A11-revised exists to stop exactly that false
    integrity incident. The clean half is asserted FIRST so the second half cannot be satisfied by
    a database that was already reporting findings.
    """
    _populate(stack)
    stack.pool.flush()
    assert stack.verifier().verify(SCOPE_ALL).findings == ()

    allocated = stack.pool.allocate(HEAP_FILE, 1).page_index
    for scope in (SCOPE_PAGES, SCOPE_RECORDS, SCOPE_ALL):
        report = stack.verifier().verify(scope)
        assert report.findings_of(FindingKind.PAGE_CHECKSUM) == (), [
            (finding.kind, finding.detail) for finding in report.findings
        ]
        awaiting = report.findings_of(FindingKind.PAGE_UNWRITTEN)
        assert len(awaiting) == 1, [(finding.kind, finding.location.page) for finding in report.findings]
        assert awaiting[0].location.file == HEAP_FILE
        assert awaiting[0].location.page == allocated
        assert "replaying the log repairs it" in awaiting[0].detail
        assert len(report.findings) == 1, [finding.kind for finding in report.findings]


def test_the_checksum_counters_do_not_move_for_a_page_that_was_never_written(
    stack: Stack,
) -> None:
    """No checksum was verified and none failed, so neither series may claim one.

    This is the assertion only the unwritten branch can satisfy (A62): a page routed through the
    codec moves both counters, whatever finding it produces afterwards.
    """
    _populate(stack)
    stack.pool.flush()
    stack.pool.allocate(HEAP_FILE, 1)
    before_taken = stack.metrics.counter(CHECKSUM_VERIFICATIONS_TOTAL, kind="page")
    before_failed = stack.metrics.counter(CHECKSUM_FAILURES_TOTAL, kind="page")
    report = stack.verifier().verify(SCOPE_PAGES)
    taken = stack.metrics.counter(CHECKSUM_VERIFICATIONS_TOTAL, kind="page") - before_taken
    assert stack.metrics.counter(CHECKSUM_FAILURES_TOTAL, kind="page") == before_failed
    assert taken == report.pages_checked - 1


def test_one_damaged_page_image_is_reported_once_however_many_walks_read_it(
    stack: Stack,
) -> None:
    """The page walk and the structural scan behind ``records`` both decode every heap page.

    Under ``scope="all"`` the same damaged image was reported by each of them, so a caller
    counting findings counted one damaged page as two. Asserted at the PAGE rather than over the
    whole report, because two damaged pages and one page reported twice are the same number under
    ``findings_of``.
    """
    table = _populate(stack)
    stack.pool.flush()
    damaged = stack.heap.pages_of(table)[0]
    raw = bytearray(stack.storage.read_page(HEAP_FILE, damaged))  # type: ignore[attr-defined]
    raw[100] ^= 0xFF
    stack.storage.write_page(HEAP_FILE, damaged, bytes(raw))  # type: ignore[attr-defined]
    stack.pool.invalidate()
    report = stack.verifier().verify(SCOPE_ALL)
    at_page = report.findings_at(FindingKind.PAGE_CHECKSUM, HEAP_FILE, damaged)
    assert len(at_page) == 1, [
        (finding.kind, finding.location.page) for finding in report.findings
    ]


def test_the_ledger_of_reported_pages_lives_for_one_call_and_not_for_the_verifier(
    stack: Stack,
) -> None:
    """A set that outlived the call would silence the finding on the NEXT run of the verifier.

    That is the opposite failure to the duplicate and a worse one -- a verifier that reports
    damage once and certifies it clean for ever after -- so the same verifier object is asked
    twice and must answer the same both times.
    """
    _populate(stack)
    stack.pool.flush()
    stack.pool.allocate(HEAP_FILE, 1)
    verifier = stack.verifier()
    first = verifier.verify(SCOPE_ALL)
    second = verifier.verify(SCOPE_ALL)
    assert len(first.findings_of(FindingKind.PAGE_UNWRITTEN)) == 1
    assert len(second.findings_of(FindingKind.PAGE_UNWRITTEN)) == 1
    assert [finding.kind for finding in first.findings] == [
        finding.kind for finding in second.findings
    ]


def test_an_all_zero_page_zero_is_a_missing_file_header_and_not_awaiting_a_redo(
    stack: Stack,
) -> None:
    """The one page where "a redo is already coming for it" is false.

    A file header is written when the file is created, never from the log, so nothing replays
    it. The state is reachable through ordinary device doors -- create the file, grow it, crash
    before the header write -- and calling it awaiting-redo would promise a repair that never
    arrives. Narrowing the verdict for DATA pages must not quietly narrow it here.
    """
    device = MemoryStorageDevice(page_size=PAGE_SIZE)
    device.create("heap.dat", exclusive=False)
    device.allocate("heap.dat", 1)
    pool = BufferPool(
        device,  # type: ignore[arg-type]
        PageCodecV1(PAGE_SIZE),
        stack.metrics,  # type: ignore[arg-type]
        budget_bytes=PAGE_SIZE * 16,
        db_label="page-zero",
    )
    report = Verifier(pool, stack.metrics).verify(SCOPE_PAGES)
    assert report.findings_of(FindingKind.PAGE_UNWRITTEN) == ()
    assert report.findings_of(FindingKind.PAGE_CHECKSUM) == ()
    header = report.findings_of(FindingKind.FILE_HEADER)
    assert len(header) == 1, [finding.kind for finding in report.findings]
    assert header[0].location.page == 0
    assert "replaying the log" not in header[0].detail
    assert "written when the file is created" in header[0].detail
