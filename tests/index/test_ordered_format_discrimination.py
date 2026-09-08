"""Binary fences for the persistent ordered exact-index layout."""

from __future__ import annotations

import struct

import pytest

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ids import NO_PAGE
from okto_grafx.domain.index.catalog import (
    IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY,
    ORDERED_SECONDARY_INDEXES_V1_CAPABILITY,
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
)
from okto_grafx.domain.index.definition import (
    ORDERED_KEY_DERIVATION,
    IndexDefinition,
)
from okto_grafx.domain.index.header import (
    ORDERED_INDEX_HEADER_FORMAT_VERSION,
    ORDERED_INDEX_HEADER_SIZE,
    IndexHeader,
)
from okto_grafx.domain.index.layout import IndexLayout
from okto_grafx.domain.index.ordered_root import (
    FIRST_ORDERED_TREE_PAGE,
    ORDERED_ROOT_DESCRIPTOR_FORMAT_VERSION,
    ORDERED_ROOT_DESCRIPTOR_SIZE,
    ORDERED_ROOT_PAGE_A,
    ORDERED_ROOT_PAGE_B,
    OrderedRootDescriptor,
    decode_ordered_root_page,
    make_ordered_root_page,
    select_ordered_root,
)
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model import catalog as catalog_module
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page import Page, PageType, crc32c
from okto_grafx.engine.index_manager import HashIndex


_DIGEST = bytes.fromhex("00112233445566778899aabbccddeeff")
_ORDERED_HEADER = struct.Struct("<HBBB3xIIQQ16sQ")
_ROOT_FORMAT_OFFSET = 8
_CHECKSUM = struct.Struct("<I")


def _ordered_runtime_definition() -> IndexDefinition:
    return IndexDefinition(
        name="by_cursor",
        table_id=7,
        table_name="Event",
        positions=(1, 0),
        visibility=IndexVisibility.EXACT,
        bucket_count=1,
        key_derivation=ORDERED_KEY_DERIVATION,
        artifact_nonce=41,
        layout=IndexLayout.ORDERED,
    )


def _ordered_catalog_definition() -> CatalogIndexDefinition:
    return CatalogIndexDefinition(
        name="by_cursor",
        table_id=7,
        table_name="Event",
        positions=(1, 0),
        visibility=IndexVisibility.EXACT,
        key_derivation=ORDERED_KEY_DERIVATION,
        layout=IndexLayout.ORDERED,
        generations=(IndexGenerationDescriptor(41, 1, "active"),),  # type: ignore[arg-type]
    )


def _ordered_catalog() -> Catalog:
    catalog = Catalog()
    catalog.add_table(
        TableDef(
            table_id=7,
            name="Event",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.STRING, nullable=False),
                ColumnDef(name="created_at", type=ValueType.TIMESTAMP, nullable=False),
            ),
            primary_key="id",
        )
    )
    catalog.upgrade_index_catalog()
    catalog.add_index_definition(_ordered_catalog_definition())
    return catalog


def _root(**changes: object) -> OrderedRootDescriptor:
    fields: dict[str, object] = {
        "artifact_nonce": 41,
        "generation": 3,
        "root_page": 19,
        "height": 2,
        "entry_count": 127,
        "applied_through_lsn": 91,
        "reconciled_through_lsn": 83,
        "definition_digest": _DIGEST,
    }
    fields.update(changes)
    return OrderedRootDescriptor(**fields)  # type: ignore[arg-type]


def _with_root_checksum(raw: bytearray) -> bytes:
    body = bytes(raw[: -_CHECKSUM.size])
    return body + _CHECKSUM.pack(crc32c(body))


def test_layout_and_derivation_are_exact_discriminators() -> None:
    assert IndexLayout.parse("hash") is IndexLayout.HASH
    assert IndexLayout.parse("ordered") is IndexLayout.ORDERED

    with pytest.raises(GrafxIndexError):
        IndexLayout.parse("ORDERED")
    with pytest.raises(GrafxIndexError):
        IndexDefinition(
            name="by_cursor",
            table_id=7,
            table_name="Event",
            positions=(1, 0),
            visibility=IndexVisibility.EXACT,
            bucket_count=1,
            key_derivation=ORDERED_KEY_DERIVATION,
        )


def test_established_hash_definition_digest_remains_byte_exact() -> None:
    definition = IndexDefinition(
        name="by_email",
        table_id=7,
        table_name="Person",
        positions=(2,),
        visibility=IndexVisibility.EXACT,
        bucket_count=64,
    )

    assert definition.layout is IndexLayout.HASH
    assert definition.digest().hex() == "0a673368de431700fb4a2fd015e6763b"


def test_ordered_catalog_round_trip_sets_the_required_capability() -> None:
    catalog = _ordered_catalog()
    raw = catalog.serialize()
    restored = Catalog.deserialize(raw)

    assert catalog.required_capabilities() == (
        IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY,
        ORDERED_SECONDARY_INDEXES_V1_CAPABILITY,
    )
    assert restored == catalog
    assert restored.index_definition("by_cursor").layout is IndexLayout.ORDERED
    assert (
        restored.active_index_definitions()[0].key_derivation
        == ORDERED_KEY_DERIVATION
    )


def test_a_pre_ordered_v2_reader_refuses_before_parsing_index_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _ordered_catalog().serialize()
    monkeypatch.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS", 0b111)

    with pytest.raises(GrafxSchemaVersionMismatch) as refused:
        Catalog.deserialize(raw)

    assert refused.value.details["unsupported"] == 0b1000


def test_ordered_header_round_trip_carries_an_explicit_layout() -> None:
    header = IndexHeader(
        visibility=IndexVisibility.EXACT,
        table_id=7,
        bucket_count=1,
        digest=_DIGEST,
        built_through_lsn=91,
        reconciled_through_lsn=83,
        artifact_nonce=41,
        format_version=ORDERED_INDEX_HEADER_FORMAT_VERSION,
        layout=IndexLayout.ORDERED,
    )
    encoded = header.encode()

    assert ORDERED_INDEX_HEADER_SIZE == _ORDERED_HEADER.size
    assert len(encoded) == ORDERED_INDEX_HEADER_SIZE
    assert _ORDERED_HEADER.unpack(encoded)[2] == 2
    assert IndexHeader.decode(encoded) == header


def test_header_refuses_unknown_layout_and_future_writes() -> None:
    encoded = bytearray(
        IndexHeader(
            visibility=IndexVisibility.EXACT,
            table_id=7,
            bucket_count=1,
            digest=_DIGEST,
            artifact_nonce=41,
            format_version=ORDERED_INDEX_HEADER_FORMAT_VERSION,
            layout=IndexLayout.ORDERED,
        ).encode()
    )
    encoded[3] = 0x7F

    with pytest.raises(GrafxCorruptionDetected) as damaged:
        IndexHeader.decode(encoded)
    assert damaged.value.details["field"] == "layout"

    zero = bytearray(encoded)
    struct.pack_into("<H", zero, 0, 0)
    with pytest.raises(GrafxCorruptionDetected) as invalid_version:
        IndexHeader.decode(zero)
    assert invalid_version.value.details["field"] == "format_version"

    with pytest.raises(GrafxSchemaVersionMismatch):
        IndexHeader(
            visibility=IndexVisibility.EXACT,
            table_id=7,
            bucket_count=1,
            digest=_DIGEST,
            format_version=ORDERED_INDEX_HEADER_FORMAT_VERSION + 1,
        )

    with pytest.raises(GrafxIndexError):
        IndexHeader(
            visibility=IndexVisibility.PROXIMITY,
            table_id=7,
            bucket_count=1,
            digest=_DIGEST,
            format_version=ORDERED_INDEX_HEADER_FORMAT_VERSION,
            layout=IndexLayout.ORDERED,
        )


def test_hash_store_refuses_an_ordered_definition_before_touching_storage() -> None:
    definition = _ordered_runtime_definition()

    with pytest.raises(GrafxIndexError) as refused:
        HashIndex(definition, object(), object())  # type: ignore[arg-type]

    assert refused.value.details == {
        "field": "layout",
        "value": "ordered",
        "index": "by_cursor",
    }


def test_root_descriptor_round_trip_pins_independent_publication_pages() -> None:
    descriptor = _root()
    encoded = descriptor.encode()

    assert ORDERED_ROOT_PAGE_A != ORDERED_ROOT_PAGE_B
    assert ORDERED_ROOT_PAGE_A < FIRST_ORDERED_TREE_PAGE
    assert ORDERED_ROOT_PAGE_B < FIRST_ORDERED_TREE_PAGE
    assert len(encoded) == ORDERED_ROOT_DESCRIPTOR_SIZE
    assert OrderedRootDescriptor.decode(encoded) == descriptor


def test_root_descriptor_refuses_torn_and_future_images_fail_closed() -> None:
    damaged = bytearray(_root().encode())
    damaged[24] ^= 0xFF
    with pytest.raises(GrafxCorruptionDetected) as torn:
        OrderedRootDescriptor.decode(damaged)
    assert torn.value.details["field"] == "checksum"

    future = bytearray(_root().encode())
    struct.pack_into(
        "<H",
        future,
        _ROOT_FORMAT_OFFSET,
        ORDERED_ROOT_DESCRIPTOR_FORMAT_VERSION + 1,
    )
    with pytest.raises(GrafxSchemaVersionMismatch) as refused:
        OrderedRootDescriptor.decode(_with_root_checksum(future))
    assert refused.value.details["field"] == "format_version"


def test_root_descriptor_requires_coherent_empty_and_nonempty_shapes() -> None:
    empty = _root(root_page=NO_PAGE, height=0, entry_count=0)
    assert OrderedRootDescriptor.decode(empty.encode()) == empty

    with pytest.raises(GrafxIndexError):
        _root(root_page=NO_PAGE, height=1, entry_count=0)
    with pytest.raises(GrafxIndexError):
        _root(root_page=ORDERED_ROOT_PAGE_A, height=1, entry_count=1)


def test_independent_root_pages_select_the_newest_and_alternate_publication() -> None:
    first = _root(generation=3)
    page_a = make_ordered_root_page(first, ORDERED_ROOT_PAGE_A, page_size=512)
    page_b = make_ordered_root_page(first, ORDERED_ROOT_PAGE_B, page_size=512)

    equal = select_ordered_root(page_a, page_b)
    assert equal.descriptor == first
    assert equal.page_index == ORDERED_ROOT_PAGE_A
    assert equal.publication_page == ORDERED_ROOT_PAGE_B

    second = _root(generation=4, root_page=29, entry_count=131)
    page_b = make_ordered_root_page(second, ORDERED_ROOT_PAGE_B, page_size=512)
    newer = select_ordered_root(page_a, page_b)
    assert newer.descriptor == second
    assert newer.page_index == ORDERED_ROOT_PAGE_B
    assert newer.publication_page == ORDERED_ROOT_PAGE_A


def test_one_damaged_root_degrades_but_two_or_split_brain_refuse() -> None:
    descriptor = _root()
    healthy = make_ordered_root_page(descriptor, ORDERED_ROOT_PAGE_B, page_size=512)
    damaged = Page(
        int(PageType.INDEX_ORDERED_LEAF),
        page_size=512,
        page_index=ORDERED_ROOT_PAGE_A,
    )
    damaged.insert_slot(b"not-a-root")

    selected = select_ordered_root(damaged, healthy)
    assert selected.descriptor == descriptor
    assert selected.damaged_pages == (ORDERED_ROOT_PAGE_A,)
    assert selected.publication_page == ORDERED_ROOT_PAGE_A

    with pytest.raises(GrafxCorruptionDetected) as both:
        select_ordered_root(damaged, None)
    assert both.value.details["damaged_pages"] == (
        ORDERED_ROOT_PAGE_A,
        ORDERED_ROOT_PAGE_B,
    )

    disagreement = make_ordered_root_page(
        _root(root_page=31), ORDERED_ROOT_PAGE_A, page_size=512
    )
    with pytest.raises(GrafxCorruptionDetected) as split_brain:
        select_ordered_root(disagreement, healthy)
    assert split_brain.value.details["field"] == "generation"


def test_root_page_watermark_and_future_format_remain_fail_closed() -> None:
    descriptor = _root()
    page = make_ordered_root_page(descriptor, ORDERED_ROOT_PAGE_A, page_size=512)
    page.page_lsn = descriptor.applied_through_lsn + 1
    with pytest.raises(GrafxCorruptionDetected) as mismatch:
        decode_ordered_root_page(page)
    assert mismatch.value.details["field"] == "page_lsn"

    future = bytearray(descriptor.encode())
    struct.pack_into(
        "<H",
        future,
        _ROOT_FORMAT_OFFSET,
        ORDERED_ROOT_DESCRIPTOR_FORMAT_VERSION + 1,
    )
    future_page = Page(
        int(PageType.INDEX_ORDERED_ROOT),
        page_size=512,
        page_index=ORDERED_ROOT_PAGE_A,
        page_lsn=descriptor.applied_through_lsn,
    )
    future_page.insert_slot(_with_root_checksum(future))
    with pytest.raises(GrafxSchemaVersionMismatch):
        select_ordered_root(future_page, None)
