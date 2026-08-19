"""The identifier vocabulary and the RecordRef packing (CONTRACT.md section 3)."""

from __future__ import annotations

import dataclasses

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import (
    MAX_PAGE_INDEX,
    MAX_SLOT_ID,
    NO_CSN,
    NO_LSN,
    NO_PAGE,
    NULL_REF,
    RecordRef,
)


def test_sentinels_hold_the_contract_values() -> None:
    assert NO_LSN == 0
    assert NO_CSN == 0
    assert NO_PAGE == 0xFFFFFFFF
    assert MAX_PAGE_INDEX == 0xFFFFFFFF
    assert MAX_SLOT_ID == 0xFFFF


def test_record_ref_is_a_frozen_value_with_slots() -> None:
    reference = RecordRef(page=4, slot=9)
    assert dataclasses.is_dataclass(reference)
    assert not hasattr(reference, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        reference.page = 5  # type: ignore[misc]


def test_record_ref_equality_and_hashing() -> None:
    assert RecordRef(1, 2) == RecordRef(1, 2)
    assert RecordRef(1, 2) != RecordRef(2, 1)
    assert len({RecordRef(1, 2), RecordRef(1, 2), RecordRef(2, 1)}) == 2


def test_encode_is_the_documented_packing() -> None:
    assert RecordRef(page=0, slot=0).encode() == 0
    assert RecordRef(page=1, slot=0).encode() == 1 << 16
    assert RecordRef(page=3, slot=7).encode() == (3 << 16) | 7


@pytest.mark.parametrize(
    "reference",
    [
        RecordRef(0, 0),
        RecordRef(0, MAX_SLOT_ID),
        RecordRef(MAX_PAGE_INDEX, 0),
        RecordRef(MAX_PAGE_INDEX, MAX_SLOT_ID),
        RecordRef(1, 1),
        RecordRef(123456, 4321),
        NULL_REF,
    ],
)
def test_encode_decode_round_trip_including_boundaries(reference: RecordRef) -> None:
    assert RecordRef.decode(reference.encode()) == reference


def test_the_packing_is_injective_over_the_slot_boundary() -> None:
    lower = RecordRef(page=0, slot=MAX_SLOT_ID)
    upper = RecordRef(page=1, slot=0)
    assert lower.encode() + 1 == upper.encode()
    assert RecordRef.decode(lower.encode()) == lower
    assert RecordRef.decode(upper.encode()) == upper


def test_null_ref_is_the_no_page_sentinel() -> None:
    assert NULL_REF == RecordRef(NO_PAGE, 0)
    assert NULL_REF.encode() == NO_PAGE << 16
    assert RecordRef.decode(NULL_REF.encode()) == NULL_REF


@pytest.mark.parametrize(
    "reference",
    [
        RecordRef(-1, 0),
        RecordRef(0, -1),
        RecordRef(MAX_PAGE_INDEX + 1, 0),
        RecordRef(0, MAX_SLOT_ID + 1),
    ],
)
def test_encode_refuses_a_location_outside_the_range(reference: RecordRef) -> None:
    with pytest.raises(GrafxCorruptionDetected) as raised:
        reference.encode()
    assert raised.value.details["page"] == reference.page
    assert raised.value.details["slot"] == reference.slot


def test_encode_refuses_a_non_integer_location() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        RecordRef(page="4", slot=0).encode()  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "reference",
    [
        RecordRef(True, False),
        RecordRef(False, True),
        RecordRef(True, 0),
        RecordRef(0, True),
    ],
)
def test_encode_refuses_a_boolean_location(reference: RecordRef) -> None:
    # bool is a subclass of int, so True would silently encode as page 1 or slot 1. Every other
    # C0 validator rejects it; this one does too.
    with pytest.raises(GrafxCorruptionDetected):
        reference.encode()


def test_decode_refuses_a_boolean_value() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        RecordRef.decode(True)  # type: ignore[arg-type]
    with pytest.raises(GrafxCorruptionDetected):
        RecordRef.decode(False)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", [-1, (MAX_PAGE_INDEX << 16 | MAX_SLOT_ID) + 1])
def test_decode_refuses_a_value_outside_the_range(raw: int) -> None:
    with pytest.raises(GrafxCorruptionDetected) as raised:
        RecordRef.decode(raw)
    assert raised.value.details["raw"] == raw


def test_decode_refuses_a_non_integer_value() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        RecordRef.decode("0")  # type: ignore[arg-type]


def test_decode_of_zero_is_the_first_slot_of_the_first_page() -> None:
    assert RecordRef.decode(0) == RecordRef(page=0, slot=0)
