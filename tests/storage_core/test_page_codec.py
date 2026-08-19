"""The version 1 page codec and what it refuses to interpret (CONTRACT.md sections 4.5, 6.3).

Decoding is where a damaged file either becomes an error or becomes a wrong answer. Every case
below takes a page image that was valid, breaks exactly one thing about it, and asserts that the
codec names the damage instead of handing back a page whose slots point at bytes that are not
theirs.
"""

from __future__ import annotations

import struct

import pytest

from okto_grafx.adapters.codec_v1 import PAGE_CODEC_FORMAT_VERSION, PageCodecV1
from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.page import (
    PAGE_HEADER_SIZE,
    SLOT_ENTRY_SIZE,
    Page,
    PageType,
    crc32c,
)
from okto_grafx.domain.ports.codec import PageCodec

PAGE_SIZE: int = 512


def sample_page() -> Page:
    """Return a page with three slots, the shape every corruption test starts from."""
    page = Page(int(PageType.HEAP), page_size=PAGE_SIZE, page_index=4)
    page.page_lsn = 77
    page.insert_slot(b"alpha")
    page.insert_slot(b"beta")
    page.insert_slot(b"gamma")
    return page


def reseal(image: bytearray) -> bytes:
    """Recompute the checksum of a mutated image, so the structural check is what fails."""
    checksum = crc32c(bytes(image[4:]))
    image[0:4] = checksum.to_bytes(4, "little")
    return bytes(image)


def test_the_codec_satisfies_the_port() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    assert isinstance(codec, PageCodec)
    assert codec.format_version == PAGE_CODEC_FORMAT_VERSION == 1
    assert codec.page_size == PAGE_SIZE


def test_an_encoded_page_is_exactly_page_size_bytes() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    for page in (Page(page_size=PAGE_SIZE), sample_page()):
        assert len(codec.encode_page(page)) == PAGE_SIZE


def test_an_encoded_page_carries_a_correct_checksum() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    image = codec.encode_page(sample_page())
    stored = struct.unpack_from("<I", image, 0)[0]
    assert stored == codec.checksum(image[4:])


def test_a_page_round_trips_through_the_codec() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    page = sample_page()
    decoded = codec.decode_page(codec.encode_page(page))
    assert decoded == page
    assert [payload for _slot, payload in decoded.iter_slots()] == [b"alpha", b"beta", b"gamma"]


def test_encoding_a_page_of_another_size_is_refused() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    with pytest.raises(GrafxCorruptionDetected):
        codec.encode_page(Page(page_size=1024))


def test_encoding_something_that_is_not_a_page_is_refused() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    with pytest.raises(GrafxCorruptionDetected):
        codec.encode_page(b"raw bytes")  # type: ignore[arg-type]


def test_decoding_an_image_of_the_wrong_length_is_refused() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    with pytest.raises(GrafxCorruptionDetected):
        codec.decode_page(bytes(PAGE_SIZE - 1))
    with pytest.raises(GrafxCorruptionDetected):
        codec.decode_page(bytes(PAGE_SIZE + 1))


def test_a_flipped_payload_byte_fails_the_checksum() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    image = bytearray(codec.encode_page(sample_page()))
    image[PAGE_HEADER_SIZE] ^= 0x01
    with pytest.raises(GrafxCorruptionDetected) as raised:
        codec.decode_page(bytes(image))
    assert raised.value.details["field"] == "checksum"
    assert "stored_checksum" in raised.value.details
    assert "computed_checksum" in raised.value.details
    assert raised.value.retryable is False


def test_a_flipped_checksum_byte_fails_the_checksum() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    image = bytearray(codec.encode_page(sample_page()))
    image[0] ^= 0xFF
    with pytest.raises(GrafxCorruptionDetected):
        codec.decode_page(bytes(image))


def test_an_all_zero_page_is_not_a_valid_page() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    with pytest.raises(GrafxCorruptionDetected):
        codec.decode_page(bytes(PAGE_SIZE))


def test_skipping_verification_still_proves_the_structure() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    image = bytearray(codec.encode_page(sample_page()))
    image[PAGE_HEADER_SIZE] ^= 0x01
    decoded = codec.decode_page(bytes(image), verify=False)
    assert decoded.read_slot(0) != b"alpha"
    # The structural checks are not optional, even when the checksum is not consulted.
    broken = bytearray(codec.encode_page(sample_page()))
    struct.pack_into("<H", broken, 20, 999)
    with pytest.raises(GrafxCorruptionDetected):
        codec.decode_page(bytes(broken), verify=False)


def test_a_slot_count_that_does_not_fit_the_page_is_refused() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    image = bytearray(codec.encode_page(sample_page()))
    struct.pack_into("<H", image, 20, 500)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        codec.decode_page(reseal(image))
    assert raised.value.details["field"] in {"slot_count", "free_end"}


def test_a_free_end_that_contradicts_the_slot_count_is_refused() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    image = bytearray(codec.encode_page(sample_page()))
    struct.pack_into("<H", image, 24, PAGE_SIZE - SLOT_ENTRY_SIZE)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        codec.decode_page(reseal(image))
    assert raised.value.details["field"] == "free_end"


def test_a_free_start_below_the_header_is_refused() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    image = bytearray(codec.encode_page(sample_page()))
    struct.pack_into("<H", image, 22, 4)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        codec.decode_page(reseal(image))
    assert raised.value.details["field"] in {"free_start", "slot_entry"}


def test_a_free_start_beyond_the_directory_is_refused() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    image = bytearray(codec.encode_page(sample_page()))
    struct.pack_into("<H", image, 22, PAGE_SIZE - 1)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        codec.decode_page(reseal(image))
    assert raised.value.details["field"] == "free_start"


def test_a_slot_that_points_past_the_payload_area_is_refused() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    image = bytearray(codec.encode_page(sample_page()))
    struct.pack_into("<HH", image, PAGE_SIZE - SLOT_ENTRY_SIZE, PAGE_HEADER_SIZE, 400)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        codec.decode_page(reseal(image))
    assert raised.value.details["field"] == "slot_entry"
    assert raised.value.details["slot"] == 0


def test_a_slot_that_points_into_the_header_is_refused() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    image = bytearray(codec.encode_page(sample_page()))
    struct.pack_into("<HH", image, PAGE_SIZE - SLOT_ENTRY_SIZE, 0, 4)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        codec.decode_page(reseal(image))
    assert raised.value.details["field"] == "slot_entry"


def test_two_slots_that_overlap_are_refused() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    page = sample_page()
    image = bytearray(codec.encode_page(page))
    # Slot 1 is stretched backwards so that it covers the bytes of slot 0.
    struct.pack_into("<HH", image, PAGE_SIZE - 2 * SLOT_ENTRY_SIZE, PAGE_HEADER_SIZE + 1, 8)
    with pytest.raises(GrafxCorruptionDetected) as raised:
        codec.decode_page(reseal(image))
    assert raised.value.details["field"] == "slot_entry"


def test_a_decoded_page_can_be_told_which_index_it_came_from() -> None:
    codec = PageCodecV1(PAGE_SIZE)
    image = bytearray(codec.encode_page(sample_page()))
    image[PAGE_HEADER_SIZE] ^= 0xFF
    with pytest.raises(GrafxCorruptionDetected) as raised:
        codec.decode_page(bytes(image), page_index=17)
    assert raised.value.details["page"] == 17


def test_the_codec_refuses_an_unusable_page_size() -> None:
    from okto_grafx.domain.errors import GrafxConfigurationError

    with pytest.raises(GrafxConfigurationError):
        PageCodecV1(1000)
