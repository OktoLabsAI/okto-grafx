"""The 32-byte page header and the file header page (CONTRACT.md section 6.3, amendment A2).

The header is a frozen on-disk format, so these tests pin the offsets and the widths rather
than the behaviour: a field that moves by one byte makes every database written by an earlier
build unreadable, and nothing else in the suite would notice.
"""

from __future__ import annotations

import struct

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ids import NO_LSN, NO_PAGE
from okto_grafx.domain.page import (
    DEFAULT_PAGE_SIZE,
    FILE_HEADER_MAGIC,
    MAX_PAGE_SIZE,
    MAX_U16,
    MAX_U32,
    MAX_U64,
    MIN_PAGE_SIZE,
    PAGE_HEADER_SIZE,
    SLOT_ENTRY_SIZE,
    FileHeader,
    FileHeaderPage,
    FileKind,
    Page,
    PageHeader,
    PageType,
    decode_slot_entry,
    encode_slot_entry,
    is_unwritten_image,
    validate_page_size,
)

FIELD_OFFSETS: tuple[tuple[str, int, str], ...] = (
    ("checksum", 0, "<I"),
    ("page_type", 4, "<H"),
    ("flags", 6, "<H"),
    ("page_lsn", 8, "<Q"),
    ("seq", 16, "<I"),
    ("slot_count", 20, "<H"),
    ("free_start", 22, "<H"),
    ("free_end", 24, "<H"),
    ("reserved", 26, "<H"),
    ("next_page", 28, "<I"),
)
"""Every header field with the offset and the little-endian format the contract froze."""


def test_the_header_is_exactly_thirty_two_bytes() -> None:
    assert PAGE_HEADER_SIZE == 32
    assert len(PageHeader().encode()) == PAGE_HEADER_SIZE
    assert SLOT_ENTRY_SIZE == 4


@pytest.mark.parametrize(
    ("field", "offset", "layout"), FIELD_OFFSETS, ids=[row[0] for row in FIELD_OFFSETS]
)
def test_every_field_sits_where_the_contract_puts_it(
    field: str, offset: int, layout: str
) -> None:
    marker = {"<H": 0xBEEF, "<I": 0xDEADBEEF, "<Q": 0x0123456789ABCDEF}[layout]
    zeroed = {name: 0 for name, _offset, _layout in FIELD_OFFSETS}
    header = PageHeader(**{**zeroed, field: marker})
    raw = header.encode()
    assert struct.unpack_from(layout, raw, offset)[0] == marker
    # Every other byte of the header stays zero, which is what pins the offset.
    blanked = bytearray(raw)
    blanked[offset : offset + struct.calcsize(layout)] = bytes(struct.calcsize(layout))
    assert bytes(blanked) == bytes(PAGE_HEADER_SIZE)


def test_the_header_round_trips_at_the_maximum_of_every_field() -> None:
    header = PageHeader(
        page_type=MAX_U16,
        flags=MAX_U16,
        page_lsn=MAX_U64,
        seq=MAX_U32,
        slot_count=MAX_U16,
        free_start=MAX_U16,
        free_end=MAX_U16,
        reserved=MAX_U16,
        next_page=MAX_U32,
        checksum=MAX_U32,
    )
    assert PageHeader.decode(header.encode()) == header


def test_the_header_round_trips_at_the_minimum_of_every_field() -> None:
    header = PageHeader(
        page_type=0,
        flags=0,
        page_lsn=NO_LSN,
        seq=0,
        slot_count=0,
        free_start=0,
        free_end=0,
        reserved=0,
        next_page=0,
        checksum=0,
    )
    assert PageHeader.decode(header.encode()) == header


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("page_type", MAX_U16 + 1),
        ("flags", -1),
        ("page_lsn", MAX_U64 + 1),
        ("seq", MAX_U32 + 1),
        ("slot_count", -1),
        ("free_start", MAX_U16 + 1),
        ("next_page", MAX_U32 + 1),
        ("checksum", MAX_U32 + 1),
    ],
)
def test_a_field_outside_its_width_is_refused(field: str, value: int) -> None:
    with pytest.raises(GrafxCorruptionDetected):
        PageHeader(**{field: value}).encode()


def test_a_field_that_is_not_an_integer_is_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        PageHeader(page_lsn="7").encode()  # type: ignore[arg-type]
    with pytest.raises(GrafxCorruptionDetected):
        PageHeader(slot_count=True).encode()  # type: ignore[arg-type]


def test_decoding_a_short_header_is_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        PageHeader.decode(bytes(PAGE_HEADER_SIZE - 1))


def test_a_slot_entry_round_trips() -> None:
    raw = encode_slot_entry(1234, 56)
    assert len(raw) == SLOT_ENTRY_SIZE
    assert decode_slot_entry(raw, 0) == (1234, 56)


def test_a_slot_entry_outside_sixteen_bits_is_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        encode_slot_entry(MAX_U16 + 1, 0)


def test_the_page_type_values_are_the_ones_the_contract_lists() -> None:
    assert [int(member) for member in PageType] == [0, 1, 2, 3, 4, 5, 6, 7, 8]
    assert PageType.FREE == 0
    assert PageType.META == 1
    assert PageType.HEAP == 2
    assert PageType.CATALOG == 3
    assert PageType.INDEX_HASH == 4
    assert PageType.INDEX_HNSW == 5
    assert PageType.OVERFLOW == 6
    assert PageType.CONTROL_SLOT == 7
    assert PageType.CONTROL_HEADER == 8


@pytest.mark.parametrize("page_size", [MIN_PAGE_SIZE, 512, 1024, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE])
def test_an_accepted_page_size_is_returned_unchanged(page_size: int) -> None:
    assert validate_page_size(page_size) == page_size


@pytest.mark.parametrize(
    "page_size",
    [0, -8192, 256, MIN_PAGE_SIZE - 1, MAX_PAGE_SIZE + 1, 8191, 65536, 3000],
)
def test_a_page_size_the_layout_cannot_address_is_refused(page_size: int) -> None:
    with pytest.raises(GrafxConfigurationError):
        validate_page_size(page_size)


def test_a_page_size_that_is_not_an_integer_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError):
        validate_page_size("8192")  # type: ignore[arg-type]
    with pytest.raises(GrafxConfigurationError):
        validate_page_size(True)  # type: ignore[arg-type]


# --- the reserved header page ------------------------------------------------------------------


def test_the_file_header_round_trips() -> None:
    header = FileHeader(
        kind=FileKind.HEAP, page_size=512, root_page=7, payload_length=123456789
    )
    assert FileHeader.decode(header.encode()) == header
    assert header.encode().startswith(FILE_HEADER_MAGIC)


def test_a_file_header_of_foreign_bytes_is_refused() -> None:
    raw = bytearray(FileHeader(kind=FileKind.HEAP, page_size=512).encode())
    raw[0:8] = b"NOTGRAFX"
    with pytest.raises(GrafxCorruptionDetected):
        FileHeader.decode(bytes(raw))


def test_a_file_header_from_a_future_format_is_refused() -> None:
    raw = bytearray(FileHeader(kind=FileKind.HEAP, page_size=512).encode())
    raw[8:10] = (99).to_bytes(2, "little")
    with pytest.raises(GrafxSchemaVersionMismatch):
        FileHeader.decode(bytes(raw))


def test_a_file_header_with_an_unknown_kind_is_refused() -> None:
    raw = bytearray(FileHeader(kind=FileKind.HEAP, page_size=512).encode())
    raw[10:12] = (77).to_bytes(2, "little")
    with pytest.raises(GrafxCorruptionDetected):
        FileHeader.decode(bytes(raw))


def test_the_header_page_is_page_zero_and_of_type_meta() -> None:
    page = Page(int(PageType.FREE), page_size=512, page_index=0)
    header = FileHeader(kind=FileKind.CATALOG, page_size=512)
    FileHeaderPage.initialize(page, header)
    assert page.page_type == int(PageType.META)
    assert FileHeaderPage.read(page) == header


def test_the_header_page_can_be_rewritten_in_place() -> None:
    page = Page(page_size=512)
    FileHeaderPage.initialize(page, FileHeader(kind=FileKind.CATALOG, page_size=512))
    page.insert_slot(b"a directory entry that must survive")
    updated = FileHeader(
        kind=FileKind.CATALOG, page_size=512, root_page=4, payload_length=99
    )
    FileHeaderPage.write(page, updated)
    assert FileHeaderPage.read(page) == updated
    assert page.read_slot(1) == b"a directory entry that must survive"


def test_reading_a_header_from_a_page_of_another_type_is_refused() -> None:
    page = Page(int(PageType.HEAP), page_size=512)
    page.insert_slot(FileHeader(kind=FileKind.HEAP, page_size=512).encode())
    with pytest.raises(GrafxCorruptionDetected):
        FileHeaderPage.read(page)


def test_reading_a_header_from_an_empty_meta_page_is_refused() -> None:
    page = Page(int(PageType.META), page_size=512)
    with pytest.raises(GrafxCorruptionDetected):
        FileHeaderPage.read(page)


def test_a_header_page_of_the_wrong_page_size_is_refused() -> None:
    page = Page(page_size=512)
    with pytest.raises(GrafxCorruptionDetected):
        FileHeaderPage.initialize(page, FileHeader(kind=FileKind.HEAP, page_size=1024))


def test_the_default_next_page_is_the_end_of_a_chain() -> None:
    assert PageHeader().next_page == NO_PAGE
    assert Page(page_size=512).next_page == NO_PAGE


# --- amendment A20: one page size range, agreed on both sides ------------------------------------


def test_the_page_size_range_is_the_one_amendment_a20_froze() -> None:
    assert MIN_PAGE_SIZE == 512
    assert MAX_PAGE_SIZE == 32768


def test_the_composition_root_and_the_layout_accept_exactly_the_same_sizes() -> None:
    # A size accepted by one and refused by the other is an integration failure waiting for the
    # first non-default deployment, so the two are compared directly rather than by eye.
    from okto_grafx.runtime.config import DatabaseConfig

    for candidate in (128, 256, 511, 512, 1024, 4096, 8192, 32768, 65536, 3000):
        layout_accepts = True
        try:
            validate_page_size(candidate)
        except GrafxConfigurationError:
            layout_accepts = False
        config_accepts = True
        try:
            DatabaseConfig(path=":memory:", page_size=candidate)
        except Exception:  # noqa: BLE001 - any refusal counts as a refusal here
            config_accepts = False
        assert layout_accepts == config_accepts, candidate


def test_the_smallest_accepted_page_still_holds_a_header_and_a_slot() -> None:
    page = Page(int(PageType.HEAP), page_size=MIN_PAGE_SIZE)
    assert page.free_space() == MIN_PAGE_SIZE - PAGE_HEADER_SIZE
    page.insert_slot(b"x" * 64)
    assert len(page.to_bytes()) == MIN_PAGE_SIZE


# --- an allocated page nobody has written --------------------------------------------------------


def test_a_page_of_zeros_is_recognised_as_never_written() -> None:
    assert is_unwritten_image(bytes(512), 512)
    assert is_unwritten_image(bytes(8192), 8192)


def test_a_buffer_of_the_wrong_length_is_never_an_unwritten_page() -> None:
    assert not is_unwritten_image(bytes(511), 512)
    assert not is_unwritten_image(bytes(513), 512)
    assert not is_unwritten_image(b"", 512)
    assert not is_unwritten_image(bytes(512), 0)


def test_a_page_with_any_content_at_all_is_not_unwritten() -> None:
    for position in (0, 4, 16, 31, 32, 200, 511):
        raw = bytearray(512)
        raw[position] = 1
        assert not is_unwritten_image(bytes(raw), 512)


def test_a_slot_directory_entry_is_never_unpacked_out_of_a_short_buffer() -> None:
    """D1, the same shape in the other exported decoder.

    struct answers a short buffer with a raw struct.error, which carries no code, no retry flag
    and no location. This function is public, so any caller can reach it with any bytes.
    """
    entry = encode_slot_entry(32, 8)
    assert decode_slot_entry(entry, 0) == (32, 8)
    for raw, position in ((b"", 0), (entry[:3], 0), (entry, 1), (entry, -1), (entry, 4)):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            decode_slot_entry(raw, position)
        assert raised.value.details["field"] == "slot_entry"


def test_a_page_size_stored_in_a_file_header_is_damage_and_not_configuration() -> None:
    """The page size of a file is stored IN that file, so a value the layout cannot encode is
    damaged bytes.

    Delegating the classification along with the predicate made every door answer
    configuration_error for a corrupt header, and FR-8/FR-10 route quarantine off
    corruption_detected -- so the one state that most needs quarantining could never reach it.
    """
    for stored in (0, 100, 3000, 1 << 40):
        with pytest.raises(GrafxCorruptionDetected) as raised:
            FileHeader(kind=FileKind.HEAP, page_size=stored)
        assert raised.value.details["field"] == "page_size", stored
        assert raised.value.code == "corruption_detected", stored
    # The same predicate answering for a CONFIGURED page size stays a configuration error.
    with pytest.raises(GrafxConfigurationError):
        validate_page_size(100)
