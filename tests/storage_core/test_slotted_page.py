"""The slotted page: filling it, freeing from it, compacting it and proving its bytes.

Two properties matter more than any other here and are tested from several directions:

* a slot id is stable forever. Freeing, compacting and relocating a payload may move bytes, but
  the id of a slot is the low half of a RecordRef, so it may never be renumbered or reused;
* an image encodes exactly page_size bytes and decodes back to the same page, and a page whose
  header contradicts its directory is refused rather than interpreted.
"""

from __future__ import annotations

import struct
from collections.abc import Callable

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import NO_PAGE
from okto_grafx.domain.page import (
    PAGE_HEADER_SIZE,
    crc32c,
    SLOT_ENTRY_SIZE,
    Page,
    PageFullError,
    PageType,
    chunk_capacity,
    join_chunks,
    split_payload,
)
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE
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


def test_a_slot_view_is_read_only_and_names_the_live_payload_without_a_copy() -> None:
    page = build()
    slot = page.insert_slot(b"payload")

    view = page.slot_view(slot)

    assert isinstance(view, memoryview)
    assert view.readonly
    assert bytes(view) == b"payload"
    with pytest.raises(TypeError):
        view[0] = ord("P")


def test_the_cached_slot_view_tracks_every_buffer_replacement() -> None:
    page = build()
    discarded = page.insert_slot(b"discarded")
    kept = page.insert_slot(b"kept")
    page.free_slot(discarded)
    page.compact()
    assert bytes(page.slot_view(kept)) == b"kept"

    page.clear()
    fresh = page.insert_slot(b"fresh")
    assert bytes(page.slot_view(fresh)) == b"fresh"

    replacement = build()
    replacement.insert_slot(b"replacement")
    page.replace_with(replacement)
    assert bytes(page.slot_view(0)) == b"replacement"

    clone = page.copy()
    page.update_slot(0, b"changed")
    assert bytes(page.slot_view(0)) == b"changed"
    assert bytes(clone.slot_view(0)) == b"replacement"


def test_iter_slot_views_skips_free_slots_and_reuses_read_only_page_views() -> None:
    page = build()
    first = page.insert_slot(b"first")
    freed = page.insert_slot(b"gone")
    last = page.insert_slot(b"last")
    page.free_slot(freed)

    views = list(page.iter_slot_views())

    assert [(slot, bytes(view)) for slot, view in views] == [
        (first, b"first"),
        (last, b"last"),
    ]
    assert all(view.readonly for _slot, view in views)


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
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        page.read_slot(first)
    assert raised.value.details["field"] == "freed_slot"
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
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        page.read_slot(slot)
    assert raised.value.details["field"] == "slot"
    assert raised.value.details["slot_count"] == 1


def test_a_slot_id_that_is_not_an_integer_is_refused() -> None:
    page = build()
    page.insert_slot(b"only")
    with pytest.raises(GrafxConfigurationError) as raised:
        page.read_slot("0")  # type: ignore[arg-type]
    assert raised.value.details["field"] == "slot"


# --- C6/C7: an ordinary state must not be able to start a quarantine ---------------------------


def every_door_that_reads_a_slot(page: Page, slot: int) -> tuple[Callable[[], object], ...]:
    """Return every public door that resolves a slot id through the same decision.

    A66.1: the unit is the path, not the function. Four doors reach _entry, and a fix applied to
    one of them would leave three ways for a freed slot to reach the damage machinery.
    """
    return (
        lambda: page.read_slot(slot),
        lambda: page.slot_length(slot),
        lambda: page.update_slot(slot, b"x"),
        lambda: page.free_slot(slot),
    )


def test_reading_a_freed_slot_can_never_start_a_quarantine() -> None:
    """C7 frees slots on purpose, so a freed slot is an ordinary state of an ordinary page.

    corruption_detected is not a severity, it is a route: FR-8 and FR-10 turn it into truncation,
    quarantine and a forensic ledger entry. Putting a normal state on that route is why C6's
    verifier had to guard every read with is_slot_free instead of trusting the exception.
    """
    for door_index in range(4):
        page = build()
        slot = page.insert_slot(b"payload")
        page.insert_slot(b"neighbour")
        page.free_slot(slot)
        door = every_door_that_reads_a_slot(page, slot)[door_index]
        with pytest.raises(GrafxError) as raised:
            door()
        assert not isinstance(raised.value, GrafxCorruptionDetected), door_index
        assert raised.value.code == "unsupported_operation", door_index
        assert raised.value.details["field"] == "freed_slot", door_index


def test_a_slot_that_does_not_exist_can_never_start_a_quarantine() -> None:
    """The sibling decision: a slot id out of range is the same statement in another spelling.

    A page decoded from an image has exactly as many entries as the image carried, so an id
    outside that range never came from the page -- it came from the caller.
    """
    for door_index in range(4):
        page = build()
        page.insert_slot(b"only")
        door = every_door_that_reads_a_slot(page, 7)[door_index]
        with pytest.raises(GrafxError) as raised:
            door()
        assert not isinstance(raised.value, GrafxCorruptionDetected), door_index
        assert raised.value.code == "unsupported_operation", door_index


def test_a_freed_slot_still_refuses_rather_than_resolving_to_a_neighbour() -> None:
    """The reclassification must not have turned a refusal into an absence.

    A reference naming a freed slot has to FAIL to resolve; resolving it to whatever moved into
    those bytes is the silent-wrong-answer this page format exists to prevent.
    """
    page = build()
    first = page.insert_slot(b"first")
    second = page.insert_slot(b"second")
    page.free_slot(first)
    page.compact()
    with pytest.raises(GrafxError):
        page.read_slot(first)
    assert page.read_slot(second) == b"second"


def test_a_payload_that_is_not_bytes_is_refused() -> None:
    """Classified the way the slot id beside it is: an argument of the wrong type says nothing
    about any byte on any page, so it must not reach the quarantine route."""
    page = build()
    with pytest.raises(GrafxConfigurationError) as raised:
        page.insert_slot("text")  # type: ignore[arg-type]
    assert raised.value.details["field"] == "payload"
    assert not isinstance(raised.value, GrafxCorruptionDetected)


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


# --- a pristine page is absence; anything else is damage ----------------------------------------


def test_a_page_the_device_allocated_and_nobody_wrote_is_pristine() -> None:
    assert Page(page_size=PAGE_SIZE).is_pristine()
    decoded = Page.from_bytes(Page(page_size=PAGE_SIZE).to_bytes(), page_size=PAGE_SIZE)
    assert decoded.is_pristine()


@pytest.mark.parametrize(
    "touch",
    [
        lambda page: page.insert_slot(b"x"),
        lambda page: setattr(page, "page_type", int(PageType.HEAP)),
        lambda page: setattr(page, "next_page", 4),
    ],
    ids=["a slot", "a type", "a chain"],
)
def test_any_touch_at_all_makes_a_page_no_longer_pristine(touch: object) -> None:
    page = build()
    page.page_type = int(PageType.FREE)
    touch(page)  # type: ignore[operator]
    assert not page.is_pristine()


def test_a_free_page_that_was_written_to_is_not_pristine() -> None:
    # The one the reviewer asked for: no live slot, but the payload area was used, so the page
    # carries the residue of something. That is damage being read as absence.
    page = build()
    slot = page.insert_slot(b"a payload that was here")
    page.free_slot(slot)
    page.page_type = int(PageType.FREE)
    assert page.slot_count == 1
    assert not page.is_pristine()
    page.compact()
    assert page.free_start == PAGE_HEADER_SIZE
    assert not page.is_pristine(), "the freed directory entry is still a trace"


def test_clearing_a_page_makes_it_pristine_again() -> None:
    page = build()
    page.insert_slot(b"payload")
    page.next_page = 9
    page.clear()
    page.page_type = int(PageType.FREE)
    page.next_page = NO_PAGE
    assert page.is_pristine()


def test_a_page_with_no_slots_but_a_used_payload_area_is_not_pristine() -> None:
    """The exact shape the engine can never produce, which is why it must be read as damage.

    clear() always resets free_start, so no sanctioned path leaves a page with zero slots and a
    payload area that has been used. Bytes that say otherwise came from somewhere else, and
    treating them as an untouched page is how a reserve-in-place overwrites something real.
    """
    image = bytearray(Page(page_size=PAGE_SIZE).to_bytes())
    struct.pack_into("<H", image, 22, PAGE_HEADER_SIZE + 64)  # free_start past the header
    image[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + 64] = b"R" * 64
    struct.pack_into("<I", image, 0, crc32c(bytes(image[4:])))
    decoded = Page.from_bytes(bytes(image), page_size=PAGE_SIZE)
    assert decoded.slot_count == 0
    assert decoded.page_type == int(PageType.FREE)
    assert decoded.free_start == PAGE_HEADER_SIZE + 64
    assert not decoded.is_pristine()


def test_a_page_a_log_record_was_applied_to_is_never_pristine() -> None:
    # bootstrap() reserves a pristine page 0 in place. A page carrying a page_lsn has had a WAL
    # record applied to it, which is the loudest possible statement that it is not untouched,
    # and overwriting it would throw away exactly what recovery had just replayed.
    page = Page(int(PageType.FREE), page_size=PAGE_SIZE)
    page.page_lsn = 4242
    assert not page.is_pristine()
    flagged = Page(int(PageType.FREE), page_size=PAGE_SIZE)
    flagged.flags = 1
    assert not flagged.is_pristine()


def test_a_page_the_pool_allocated_and_flushed_is_still_pristine() -> None:
    # The write counter must stay out of the question: a redo-grown page 0 carries seq 2 while
    # holding nothing at all, and refusing to reserve it would wedge the file this predicate
    # exists to repair.
    page = Page(int(PageType.FREE), page_size=PAGE_SIZE)
    page.seq = 2
    assert page.is_pristine()
    page.seq = 4
    assert page.is_pristine()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("record_id", 1 << 64),
        ("record_id", -1),
        ("record_id", "1"),
        ("xmin", True),
        ("payload_len", 2.0),
        ("xmin", 1 << 64),
        ("xmax", -1),
        ("prev_version", 1 << 64),
        ("payload_len", 1 << 32),
        ("schema_version", 1 << 16),
        ("flags", 256),
        ("reserved", -1),
    ],
)
def test_a_record_header_field_that_cannot_be_packed_is_refused(field: str, value: int) -> None:
    """D7: A41 asked for every encoder fed by disk-sourced integers, and half the pair was tested.

    TableExtent.encode has its test; this one did not, so its range checks could be deleted with
    the suite still green and a raw struct.error would leave update() and delete().

    The class is configuration_error and not corruption_detected. Everything this guard sees came
    from a caller -- a header decoded from a page was unpacked by struct and cannot be out of
    range -- and corruption_detected is the code FR-8 and FR-10 route to truncation, quarantine
    and a forensic ledger entry. A caller's arithmetic must not be able to manufacture an
    integrity incident about a database nothing has touched (A11-revised, D5 round 8).
    """
    from okto_grafx.domain.model.record import RecordHeader

    fields = {"record_id": 1, "xmin": 1, "xmax": 0, "prev_version": 0, "payload_len": 0,
              "schema_version": 1, "flags": 0, "reserved": 0}
    assert len(RecordHeader(**fields).encode()) == RECORD_HEADER_SIZE
    broken = dict(fields)
    broken[field] = value
    with pytest.raises(GrafxConfigurationError) as raised:
        RecordHeader(**broken).encode()
    assert raised.value.details["field"] == field
    assert raised.value.code == "configuration_error"
    assert not isinstance(raised.value, GrafxCorruptionDetected)


def test_an_image_longer_than_a_page_is_refused_even_when_its_checksum_matches() -> None:
    """D6: the checksum covers bytes 4 to page_size, so anything past the page is not covered.

    A trailing excess therefore leaves the stored checksum matching, and the length check is the
    only thing standing between that buffer and a page decoded out of the middle of it.
    """
    page = build()
    page.insert_slot(b"payload")
    image = page.to_bytes()
    for excess in (1, 45, PAGE_SIZE):
        longer = image + bytes(excess)
        assert crc32c(longer[4:PAGE_SIZE]) == int.from_bytes(longer[0:4], "little"), (
            "the checksum really does still match"
        )
        with pytest.raises(GrafxCorruptionDetected) as raised:
            Page.from_bytes(longer, page_size=PAGE_SIZE)
        assert raised.value.details["field"] == "page_image"
        assert raised.value.details["value"] == PAGE_SIZE + excess


def test_a_slot_count_that_cannot_fit_the_page_is_named_by_the_check_that_finds_it() -> None:
    """D5: free_end and free_start refuse the same image for other reasons, so the type alone
    could not say which check answered.
    """
    page = build()
    page.insert_slot(b"payload")
    image = bytearray(page.to_bytes())
    struct.pack_into("<H", image, 20, 500)          # slot_count
    struct.pack_into("<H", image, 24, PAGE_SIZE - 500 * SLOT_ENTRY_SIZE & 0xFFFF)
    struct.pack_into("<I", image, 0, crc32c(bytes(image[4:PAGE_SIZE])))
    with pytest.raises(GrafxCorruptionDetected) as raised:
        Page.from_bytes(bytes(image), page_size=PAGE_SIZE)
    assert raised.value.details["field"] == "slot_count"
    assert raised.value.details["directory_bytes"] == 500 * SLOT_ENTRY_SIZE


@pytest.mark.parametrize("bad", [True, False, 3.9, "2", None])
def test_a_page_type_that_is_not_an_unsigned_integer_is_refused(bad: object) -> None:
    """DEF-5: the int() wrapper meant the guard never saw what it was refusing.

    A bool became 1 and a float was truncated, while every sibling field refused both, and the
    docstring beside it said a bool is refused with everything else.
    """
    with pytest.raises(GrafxCorruptionDetected) as raised:
        Page(bad, page_size=PAGE_SIZE)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "page_type"
    page = build()
    with pytest.raises(GrafxCorruptionDetected):
        page.page_type = bad  # type: ignore[assignment]
