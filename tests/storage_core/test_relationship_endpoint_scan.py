"""Projected relationship scans keep the full heap refusal surface without row objects."""

from __future__ import annotations

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ids import NO_PAGE, RecordRef
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.record import (
    RECORD_HEADER_SIZE,
    RecordHeader,
    decode_overflow_pointer,
)
from okto_grafx.domain.model.schema import ColumnDef, TableDef, relationship_row
from okto_grafx.domain.model.value import ValueType
from okto_grafx.engine import heap_store as heap_module
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore

from .conftest import SnapshotDouble


def _relationship_table(
    catalog: CatalogStore,
    _person: TableDef,
    *,
    name: str = "Knows",
) -> TableDef:
    """Install the small relationship table used by projected-scan tests."""
    table = TableDef(
        table_id=catalog.catalog.next_table_id(),
        name=name,
        kind="rel",
        columns=(ColumnDef(name="note", type=ValueType.STRING, nullable=False),),
        from_table=_person.name,
        to_table=_person.name,
    )
    catalog.catalog.add_table(table)
    catalog.save()
    return table


def _at(read_lsn: int) -> SnapshotDouble:
    return SnapshotDouble(read_lsn)


def _failure(call: object) -> tuple[type[BaseException], str, dict[str, object]]:
    """Return the stable public fingerprint of one failed scan."""
    try:
        call()  # type: ignore[operator]
    except (
        GrafxCorruptionDetected,
        GrafxSchemaVersionMismatch,
        SchemaMismatchError,
    ) as failure:
        return type(failure), failure.message, dict(failure.details)
    raise AssertionError("the damaged relationship was accepted")


def _rewrite_header(
    pool: BufferPool,
    heap: HeapStore,
    ref: RecordRef,
    **changes: object,
) -> RecordHeader:
    """Replace only one record header and return the replacement."""
    _table_id, content = heap._read_slot(ref)
    header = replace(RecordHeader.decode(content), **changes)
    with pool.pinned(heap.file, ref.page) as page:
        page.update_slot(ref.slot, header.encode() + content[RECORD_HEADER_SIZE:])
    return header


def test_projected_scan_matches_full_scan_and_materialises_only_visible_endpoints(
    heap_store: HeapStore,
    catalog_store: CatalogStore,
    person_table: TableDef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _relationship_table(catalog_store, person_table)
    visible = heap_store.insert(table, 1, relationship_row(11, 22, ("visible",)), xmin=10)
    heap_store.insert(table, 2, relationship_row(33, 44, ("future",)), xmin=50)
    expected = [
        (ref, tuple(version.values[:2]))
        for ref, version in heap_store.scan(table, _at(20))
    ]

    def forbidden_full_tuple(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the endpoint scan constructed a full tuple")

    monkeypatch.setattr(heap_module, "decode_tuple", forbidden_full_tuple)

    assert list(heap_store.scan_relationship_endpoints(table, _at(20))) == expected == [
        (visible, (11, 22))
    ]


def test_an_invisible_relationship_never_reads_its_corrupt_payload(
    pool: BufferPool,
    heap_store: HeapStore,
    catalog_store: CatalogStore,
    person_table: TableDef,
) -> None:
    table = _relationship_table(catalog_store, person_table)
    ref = heap_store.insert(table, 1, relationship_row(11, 22, ("future",)), xmin=50)
    _table_id, content = heap_store._read_slot(ref)
    broken = bytearray(content)
    broken[RECORD_HEADER_SIZE + 18] = 0xFF
    with pool.pinned(heap_store.file, ref.page) as page:
        page.update_slot(ref.slot, bytes(broken))

    assert list(heap_store.scan_relationship_endpoints(table, _at(20))) == []
    assert list(heap_store.scan(table, _at(20))) == []
    assert _failure(
        lambda: list(heap_store.scan_relationship_endpoints(table, _at(100)))
    ) == _failure(lambda: list(heap_store.scan(table, _at(100))))


def test_overflow_projection_preserves_extra_bytes_slots_and_header_bits(
    pool: BufferPool,
    heap_store: HeapStore,
    catalog_store: CatalogStore,
    person_table: TableDef,
) -> None:
    table = _relationship_table(catalog_store, person_table)
    note = "x" * 5001
    ref = heap_store.insert(table, 1, relationship_row(11, 22, (note,)), xmin=10)
    _table_id, content = heap_store._read_slot(ref)
    stored = RecordHeader.decode(content)
    assert stored.has_overflow
    first = decode_overflow_pointer(content, RECORD_HEADER_SIZE)
    last = first
    while True:
        with pool.pinned(heap_store.file, last) as page:
            following = page.next_page
        if following == NO_PAGE:
            break
        last = following

    with pool.pinned(heap_store.file, last) as page:
        chunk = page.read_slot(0)
        page.update_slot(0, chunk + b"ignored chain suffix")
        page.insert_slot(b"ignored second slot")
    _rewrite_header(
        pool,
        heap_store,
        ref,
        flags=stored.flags | 0x80,
        reserved=0xA5,
    )

    assert list(heap_store.scan_relationship_endpoints(table, _at(100))) == [
        (ref, (11, 22))
    ]
    assert next(heap_store.scan(table, _at(100)))[1].values == (11, 22, note)


def test_truncated_overflow_refusal_matches_the_full_scan(
    pool: BufferPool,
    heap_store: HeapStore,
    catalog_store: CatalogStore,
    person_table: TableDef,
) -> None:
    table = _relationship_table(catalog_store, person_table)
    ref = heap_store.insert(
        table,
        1,
        relationship_row(11, 22, ("x" * 5001,)),
        xmin=10,
    )
    _table_id, content = heap_store._read_slot(ref)
    first = decode_overflow_pointer(content, RECORD_HEADER_SIZE)
    with pool.pinned(heap_store.file, first) as page:
        assert page.next_page != NO_PAGE
        page.next_page = NO_PAGE

    assert _failure(
        lambda: list(heap_store.scan_relationship_endpoints(table, _at(100)))
    ) == _failure(lambda: list(heap_store.scan(table, _at(100))))


@pytest.mark.parametrize("damage", ("schema", "payload-length", "previous-reference"))
def test_header_refusal_precedes_projected_tuple_decode_just_as_in_full_scan(
    damage: str,
    pool: BufferPool,
    heap_store: HeapStore,
    catalog_store: CatalogStore,
    person_table: TableDef,
) -> None:
    table = _relationship_table(catalog_store, person_table)
    ref = heap_store.insert(table, 1, relationship_row(11, 22, ("valid",)), xmin=10)
    _table_id, content = heap_store._read_slot(ref)
    stored = RecordHeader.decode(content)
    if damage == "schema":
        _rewrite_header(
            pool,
            heap_store,
            ref,
            schema_version=stored.schema_version + 1,
        )
    elif damage == "payload-length":
        _rewrite_header(
            pool,
            heap_store,
            ref,
            payload_len=stored.payload_len + 1,
        )
    else:
        _rewrite_header(
            pool,
            heap_store,
            ref,
            prev_version=1 << 60,
        )

    assert _failure(
        lambda: list(heap_store.scan_relationship_endpoints(table, _at(100)))
    ) == _failure(lambda: list(heap_store.scan(table, _at(100))))


def test_projected_relationship_scan_refuses_a_node_table(
    heap_store: HeapStore,
    person_table: TableDef,
) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        list(heap_store.scan_relationship_endpoints(person_table, _at(100)))
    assert raised.value.details["field"] == "kind"
