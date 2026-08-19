"""The slotted page: filling it, freeing from it, compacting it and proving its bytes.

Two properties matter more than any other here and are tested from several directions:

* a slot id is stable forever. Freeing, compacting and relocating a payload may move bytes, but
  the id of a slot is the low half of a RecordRef, so it may never be renumbered or reused;
* an image encodes exactly page_size bytes and decodes back to the same page, and a page whose
  header contradicts its directory is refused rather than interpreted.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import NO_PAGE
from okto_grafx.domain.page import (
    PAGE_HEADER_SIZE,
    SLOT_ENTRY_SIZE,
    Page,
    PageFullError,
    PageType,
    chunk_capacity,
    join_chunks,
    split_payload,
)
from okto_grafx.domain.rand import SplitMix64

PAGE_SIZE: int = 512


def build(page_type: int = int(PageType.HEAP)) -> Page:
    """Return an empty page of the size these tests use."""
    return Page(page_type, page_size=PAGE_SIZE, page_index=1)


def test_a_fresh_page_is_empty_and_offers_all_of_its_space() -> None:
    page = build()
    assert page.slot_count == 0
    assert page.free_start == PAGE_HEADER_SIZE
    assert page.free_end == PAGE_SIZE
    assert page.free_space() == PAGE_SIZE - PAGE_HEADER_SIZE
    assert not page.dirty
    assert list(page.iter_slots()) == []


def test_inserting_returns_consecutive_slot_ids_and_reads_back() -> None:
    page = build()
    payloads = [b"", b"a", b"bb" * 10, bytes(range(64))]
    slots = [page.insert_slot(payload) for payload in payloads]
    assert slots == [0, 1, 2, 3]
    for slot, payload in zip(slots, payloads):
        assert page.read_slot(slot) == payload
        assert page.slot_length(slot) == len(payload)
    assert list(page.iter_slots()) == list(zip(slots, payloads))
    assert page.dirty


def test_the_free_counters_follow_every_insertion() -> None:
    page = build()
    page.insert_slot(b"x" * 10)
    assert page.free_start == PAGE_HEADER_SIZE + 10
    assert page.free_end == PAGE_SIZE - SLOT_ENTRY_SIZE
    assert page.free_space() == PAGE_SIZE - PAGE_HEADER_SIZE - 10 - SLOT_ENTRY_SIZE
    assert page.header().free_start == page.free_start
    assert page.header().free_end == page.free_end
    assert page.header().slot_count == 1


def test_a_page_can_be_filled_exactly_to_the_last_byte() -> None:
    page = build()
    exact = PAGE_SIZE - PAGE_HEADER_SIZE - SLOT_ENTRY_SIZE
    slot = page.insert_slot(b"F" * exact)
    assert page.free_space() == 0
    assert page.free_start == page.free_end
    assert page.read_slot(slot) == b"F" * exact
    image = page.to_bytes()
    assert len(image) == PAGE_SIZE
    assert Page.from_bytes(image, page_size=PAGE_SIZE, page_index=1) == page
    with pytest.raises(PageFullError):
        page.insert_slot(b"")


def test_one_byte_more_than_exactly_full_does_not_fit() -> None:
    page = build()
    exact = PAGE_SIZE - PAGE_HEADER_SIZE - SLOT_ENTRY_SIZE
    assert page.can_fit(exact)
    assert not page.can_fit(exact + 1)
    with pytest.raises(PageFullError) as raised:
        page.insert_slot(b"F" * (exact + 1))
    assert raised.value.details["requested"] == exact + 1 + SLOT_ENTRY_SIZE
    assert page.slot_count == 0


def test_filling_a_page_with_records_stops_at_the_boundary() -> None:
    page = build()
    record = b"r" * 32
    inserted = 0
    while page.can_fit(len(record)):
        page.insert_slot(record)
        inserted += 1
    assert inserted == (PAGE_SIZE - PAGE_HEADER_SIZE) // (32 + SLOT_ENTRY_SIZE)
    with pytest.raises(PageFullError):
        page.insert_slot(record)
    assert all(payload == record for _slot, payload in page.iter_slots())


def test_a_freed_slot_keeps_its_id_and_refuses_to_be_read() -> None:
    page = build()
    first = page.insert_slot(b"first")
    second = page.insert_slot(b"second")
    reclaimable = page.free_slot(first)
    assert reclaimable == len(b"first")
    assert page.is_slot_free(first)
    assert not page.is_slot_free(second)
    assert page.slot_count == 2
    with pytest.raises(GrafxCorruptionDetected):
        page.read_slot(first)
    assert list(page.iter_slots()) == [(second, b"second")]
    assert page.live_slots() == (second,)


def test_freeing_zeroes_the_payload_so_the_image_keeps_no_residue() -> None:
    page = build()
    slot = page.insert_slot(b"a secret that must not survive")
    page.free_slot(slot)
    assert b"secret" not in page.to_bytes()


def test_an_id_of_a_freed_slot_is_never_handed_out_again() -> None:
    page = build()
    first = page.insert_slot(b"one")
    page.free_slot(first)
    second = page.insert_slot(b"two")
    assert second != first
    page.compact()
    third = page.insert_slot(b"three")
    assert third not in (first, second)
    assert page.is_slot_free(first)


def test_compacting_closes_the_gaps_and_keeps_every_id() -> None:
    page = build()
    slots = [page.insert_slot(bytes([index]) * 16) for index in range(6)]
    for slot in (1, 3):
        page.free_slot(slots[slot])
    before = page.free_start
    reclaimed = page.compact()
    assert reclaimed == 32
    assert page.free_start == before - 32
    assert page.free_start == PAGE_HEADER_SIZE + 4 * 16
    for slot in (0, 2, 4, 5):
        assert page.read_slot(slots[slot]) == bytes([slot]) * 16
    for slot in (1, 3):
        assert page.is_slot_free(slots[slot])


def test_compacting_a_page_with_no_gap_reclaims_nothing() -> None:
    page = build()
    page.insert_slot(b"a" * 20)
    page.insert_slot(b"b" * 20)
    assert page.compact() == 0


def test_a_fragmented_page_makes_room_by_compacting_before_it_refuses() -> None:
    page = build()
    filler = b"z" * 100
    slots = [page.insert_slot(filler) for _ in range(4)]
    page.free_slot(slots[0])
    page.free_slot(slots[2])
    assert page.free_space() < 150
    slot = page.insert_slot(b"w" * 150)
    assert page.read_slot(slot) == b"w" * 150
    assert page.read_slot(slots[1]) == filler
    assert page.read_slot(slots[3]) == filler


def test_updating_a_slot_in_place_keeps_the_offset() -> None:
    page = build()
    slot = page.insert_slot(b"1234567890")
    other = page.insert_slot(b"neighbour")
    free_start = page.free_start
    page.update_slot(slot, b"abcde")
    assert page.read_slot(slot) == b"abcde"
    assert page.slot_length(slot) == 5
    assert page.free_start == free_start
    assert page.read_slot(other) == b"neighbour"
    assert b"67890" not in page.to_bytes()


def test_updating_a_slot_to_the_same_size_is_in_place() -> None:
    page = build()
    slot = page.insert_slot(b"12345")
    free_start = page.free_start
    page.update_slot(slot, b"abcde")
    assert page.free_start == free_start
    assert page.read_slot(slot) == b"abcde"


def test_updating_a_slot_to_a_larger_payload_relocates_it() -> None:
    page = build()
    slot = page.insert_slot(b"small")
    neighbour = page.insert_slot(b"neighbour")
    page.update_slot(slot, b"L" * 100)
    assert page.read_slot(slot) == b"L" * 100
    assert page.read_slot(neighbour) == b"neighbour"
    assert page.live_slots() == (slot, neighbour)


def test_an_update_that_cannot_fit_leaves_the_page_untouched() -> None:
    page = build()
    slot = page.insert_slot(b"small")
    neighbour = page.insert_slot(b"n" * 400)
    with pytest.raises(PageFullError):
        page.update_slot(slot, b"L" * 400)
    assert page.read_slot(slot) == b"small"
    assert page.read_slot(neighbour) == b"n" * 400


def test_an_update_that_only_fits_after_compacting_still_succeeds() -> None:
    page = build()
    slot = page.insert_slot(b"small")
    filler = page.insert_slot(b"f" * 200)
    page.free_slot(filler)
    page.insert_slot(b"tail")
    page.update_slot(slot, b"L" * 200)
    assert page.read_slot(slot) == b"L" * 200


@pytest.mark.parametrize("slot", [-1, 1, 99])
def test_a_slot_that_does_not_exist_is_refused(slot: int) -> None:
    page = build()
    page.insert_slot(b"only")
    with pytest.raises(GrafxCorruptionDetected):
        page.read_slot(slot)


def test_a_slot_id_that_is_not_an_integer_is_refused() -> None:
    page = build()
    page.insert_slot(b"only")
    with pytest.raises(GrafxCorruptionDetected):
        page.read_slot("0")  # type: ignore[arg-type]


def test_a_payload_that_is_not_bytes_is_refused() -> None:
    page = build()
    with pytest.raises(GrafxCorruptionDetected):
        page.insert_slot("text")  # type: ignore[arg-type]


def test_the_header_fields_round_trip_through_an_image() -> None:
    page = build(int(PageType.CATALOG))
    page.page_lsn = 987654321
    page.seq = 42
    page.flags = 0x0F0F
    page.next_page = 12345
    slot = page.insert_slot(b"payload")
    decoded = Page.from_bytes(page.to_bytes(), page_size=PAGE_SIZE, page_index=1)
    assert decoded.page_type == int(PageType.CATALOG)
    assert decoded.page_lsn == 987654321
    assert decoded.seq == 42
    assert decoded.flags == 0x0F0F
    assert decoded.next_page == 12345
    assert decoded.read_slot(slot) == b"payload"
    assert decoded == page
    assert not decoded.dirty


def test_an_image_is_stable_under_a_decode_and_encode_round_trip() -> None:
    page = build()
    for index in range(10):
        page.insert_slot(bytes([index]) * (index + 1))
    page.free_slot(4)
    image = page.to_bytes()
    assert Page.from_bytes(image, page_size=PAGE_SIZE).to_bytes() == image


def test_every_header_change_marks_the_page_dirty() -> None:
    for change in (
        lambda page: setattr(page, "page_lsn", 5),
        lambda page: setattr(page, "seq", 4),
        lambda page: setattr(page, "flags", 1),
        lambda page: setattr(page, "next_page", 9),
        lambda page: setattr(page, "page_type", int(PageType.OVERFLOW)),
        lambda page: page.insert_slot(b"x"),
        lambda page: page.clear(),
    ):
        page = build()
        page.dirty = False
        change(page)
        assert page.dirty


def test_clearing_drops_every_slot_but_keeps_the_header() -> None:
    page = build()
    page.page_lsn = 77
    page.insert_slot(b"gone")
    page.clear()
    assert page.slot_count == 0
    assert page.free_start == PAGE_HEADER_SIZE
    assert page.page_lsn == 77
    assert b"gone" not in page.to_bytes()


def test_replace_with_adopts_the_content_and_keeps_the_index() -> None:
    target = Page(int(PageType.FREE), page_size=PAGE_SIZE, page_index=9)
    source = Page(int(PageType.HEAP), page_size=PAGE_SIZE, page_index=3)
    source.page_lsn = 1000
    source.insert_slot(b"redone")
    target.replace_with(source)
    assert target.page_index == 9
    assert target.page_type == int(PageType.HEAP)
    assert target.page_lsn == 1000
    assert target.read_slot(0) == b"redone"
    assert target.dirty


def test_replace_with_a_page_of_another_size_is_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        Page(page_size=512).replace_with(Page(page_size=1024))


def test_a_page_index_outside_thirty_two_bits_is_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        Page(page_size=PAGE_SIZE, page_index=1 << 32)


def test_two_pages_with_the_same_content_are_equal() -> None:
    first = build()
    second = build()
    first.insert_slot(b"same")
    second.insert_slot(b"same")
    assert first == second
    assert first != Page(page_size=PAGE_SIZE)
    assert first != "not a page"


def test_the_repr_names_what_a_reader_needs() -> None:
    page = build()
    page.insert_slot(b"x")
    text = repr(page)
    assert "page_index=1" in text
    assert "slot_count=1" in text


# --- overflow ------------------------------------------------------------------------------


def test_the_chunk_capacity_is_a_page_minus_its_header_and_one_slot() -> None:
    assert chunk_capacity(PAGE_SIZE) == PAGE_SIZE - PAGE_HEADER_SIZE - SLOT_ENTRY_SIZE


def test_splitting_and_joining_a_payload_is_the_identity() -> None:
    generator = SplitMix64(2026)
    payload = bytes(generator.next_below(256) for _ in range(5000))
    capacity = chunk_capacity(PAGE_SIZE)
    chunks = split_payload(payload, capacity)
    assert len(chunks) == 11
    assert all(len(chunk) <= capacity for chunk in chunks)
    assert all(len(chunk) == capacity for chunk in chunks[:-1])
    assert join_chunks(chunks) == payload


def test_a_payload_of_exactly_one_chunk_is_one_page() -> None:
    capacity = chunk_capacity(PAGE_SIZE)
    assert split_payload(b"x" * capacity, capacity) == (b"x" * capacity,)
    assert len(split_payload(b"x" * (capacity + 1), capacity)) == 2


def test_an_empty_payload_still_has_one_chunk() -> None:
    assert split_payload(b"", 100) == (b"",)
    assert join_chunks(split_payload(b"", 100)) == b""


def test_a_capacity_below_one_is_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        split_payload(b"x", 0)


def test_a_chunk_fits_the_page_it_was_measured_for() -> None:
    payload = b"y" * 4096
    chunks = split_payload(payload, chunk_capacity(PAGE_SIZE))
    for position, chunk in enumerate(chunks):
        page = Page(int(PageType.OVERFLOW), page_size=PAGE_SIZE, page_index=position + 1)
        page.insert_slot(chunk)
        page.next_page = position + 2 if position + 1 < len(chunks) else NO_PAGE
        assert len(page.to_bytes()) == PAGE_SIZE
