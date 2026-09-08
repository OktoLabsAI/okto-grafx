"""Validate a journal append against an independently established predecessor view."""

from __future__ import annotations

from dataclasses import replace
from struct import pack_into

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxTransactionBudgetExceeded
from okto_grafx.domain.page import Page
from okto_grafx.domain.model.value import Timestamp
from okto_grafx.domain.txn.commit_catalog import CommitCatalogEntry, CommitKind
from okto_grafx.domain.txn.commit_identity import CommitId, CommitTime, assign_commit_time
from okto_grafx.domain.txn.commit_metadata import CommitMetadata, MetadataLimits
from okto_grafx.engine.commit_catalog_store import (
    COMMIT_DIRECTORY_FILE as DIR, COMMIT_STREAM_FILE as DATA,
    CommitCatalogPageImage, CommitCatalogPlan, CommitCatalogStore,
)


UUID = bytes(range(16))


class Images:
    def __init__(self) -> None:
        self.pages: dict[tuple[str, int], bytes] = {}
        self.reads: list[tuple[str, int]] = []

    def read(self, file: str, index: int) -> bytes:
        self.reads.append((file, index))
        return self.pages[file, index]

    def apply(self, plan: CommitCatalogPlan) -> None:
        self.pages.update({(image.file, image.page_index): image.raw for image in plan.images})


def stack(page_size: int = 512) -> tuple[Images, CommitCatalogStore]:
    images = Images()
    store = CommitCatalogStore(images.read, database_uuid=UUID, page_size=page_size)
    images.apply(store.plan_initialize(activation_sequence=7))
    return images, store


def entry(sequence: int, *, large: bool = False) -> CommitCatalogEntry:
    metadata = CommitMetadata(reason="x" * 4000).canonical_bytes if large else None
    return CommitCatalogEntry(CommitId(UUID, sequence), assign_commit_time(Timestamp(sequence), None), metadata)


def stamped(images: tuple[CommitCatalogPageImage, ...], sequence: int, *, seq: int = 0) -> tuple[CommitCatalogPageImage, ...]:
    result = []
    for image in images:
        page = Page.from_bytes(image.raw, page_index=image.page_index)
        page.page_lsn = sequence
        page.seq = seq
        result.append(replace(image, raw=page.to_bytes()))
    return tuple(result)


@pytest.mark.parametrize("page_size", [512, 8192])
@pytest.mark.parametrize("count", [0, 1, 7, 13, 128])
@pytest.mark.parametrize("large", [False, True])
def test_exact_append_transition_is_bounded_and_read_only(page_size: int, count: int, large: bool) -> None:
    images, store = stack(page_size)
    for i in range(count):
        images.apply(store.plan_append(entry(10 + i * 2)))
    previous = 8 + count * 2 if count else 7
    expected = entry(previous + 5, large=large)
    plan = store.plan_append(expected)
    raw = stamped(plan.images, previous + 5, seq=2)
    before = dict(images.pages)
    images.reads.clear()
    actual = store.validate_append_images(raw, previous_sequence=previous, sequence=previous + 5, activation_sequence=7)
    assert actual == expected
    assert len(images.reads) <= 5  # Only predecessor heads, last directory/record.
    assert images.pages == before


@pytest.mark.parametrize("remove", [0, 1, 2])
def test_missing_image_cannot_fall_back_to_predecessor_tail(remove: int) -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    raw = stamped(store.plan_append(entry(20)).images, 20)
    assert len(raw) == 3
    before = dict(images.pages)
    with pytest.raises(GrafxCorruptionDetected):
        store.validate_append_images(raw[:remove] + raw[remove + 1:], previous_sequence=10, sequence=20, activation_sequence=7)
    assert images.pages == before


@pytest.mark.parametrize("kind", ["duplicate", "extra", "wrong_file", "wrong_stamp", "wrong_commit", "wrong_previous", "wrong_activation"])
def test_invalid_transition_refuses_without_writing(kind: str) -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    raw = stamped(store.plan_append(entry(20)).images, 20)
    previous, sequence, activation = 10, 20, 7
    if kind == "duplicate":
        raw += (raw[0],)
    elif kind == "extra":
        raw += (CommitCatalogPageImage(DATA, 0, images.pages[DATA, 0]),)
    elif kind == "wrong_file":
        raw = (replace(raw[0], file="heap.dat"), *raw[1:])
    elif kind == "wrong_stamp":
        raw = stamped(raw, 19)
    elif kind == "wrong_commit":
        sequence = 21
    elif kind == "wrong_previous":
        previous = 9
    else:
        activation = 8
    before = dict(images.pages)
    with pytest.raises(GrafxCorruptionDetected):
        store.validate_append_images(raw, previous_sequence=previous, sequence=sequence, activation_sequence=activation)
    assert images.pages == before


@pytest.mark.parametrize("target", [DATA, DIR])
def test_crc_valid_rewrite_of_historical_prefix_is_not_an_append(target: str) -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    raw = list(stamped(store.plan_append(entry(20)).images, 20))
    for i, image in enumerate(raw):
        if image.file == target and image.page_index == 1:
            page = Page.from_bytes(image.raw)
            body = bytearray(page.read_slot(1))
            if target == DATA:
                # A complete, independently valid replacement old record, same size.
                old = entry(9).encode()
                body[:len(old)] = old
            else:
                pack_into("<Q", body, 0, 9)  # Plausible old sequence, valid ordering.
            page.update_slot(1, bytes(body))
            raw[i] = replace(image, raw=page.to_bytes())
    with pytest.raises(GrafxCorruptionDetected) as failure:
        store.validate_append_images(tuple(raw), previous_sequence=10, sequence=20, activation_sequence=7)
    assert failure.value.details["field"] == "append_prefix"


def test_excessive_transition_cardinality_refuses_before_storage_read() -> None:
    images, store = stack()
    raw = stamped(store.plan_append(entry(10)).images, 10)
    images.reads.clear()
    with pytest.raises(GrafxTransactionBudgetExceeded):
        store.validate_append_images(raw * 100, previous_sequence=7, sequence=10, activation_sequence=7)
    assert images.reads == []


def test_monotone_but_incorrect_logical_clock_jump_is_not_a_valid_transition() -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    incorrect = CommitCatalogEntry(CommitId(UUID, 20), CommitTime(Timestamp(20), Timestamp(100), True))
    raw = stamped(store.plan_append(incorrect).images, 20)
    with pytest.raises(GrafxCorruptionDetected) as failure:
        store.validate_append_images(raw, previous_sequence=10, sequence=20, activation_sequence=7)
    assert failure.value.details["field"] == "append_time"


@pytest.mark.parametrize("observed", [-100, 10, 11])
@pytest.mark.parametrize("kind", [CommitKind.DATA, CommitKind.MAINTENANCE])
def test_clock_tie_regression_and_maintenance_use_the_exact_logical_rule(observed: int, kind: CommitKind) -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    value = CommitCatalogEntry(CommitId(UUID, 20), assign_commit_time(Timestamp(observed), Timestamp(10)), kind=kind)
    raw = stamped(store.plan_append(value).images, 20)
    assert store.validate_append_images(raw, previous_sequence=10, sequence=20, activation_sequence=7) == value


def test_maximum_record_after_partial_tail_and_maximum_predecessor_is_bounded() -> None:
    images, store = stack()
    for sequence in range(10, 24, 2):
        images.apply(store.plan_append(entry(sequence)))
    metadata = CommitMetadata(
        actor="a" * 16384, origin="o" * 16384, correlation_id="c" * 16384,
        reason="r" * 16354, limits=MetadataLimits(max_bytes=65536, max_string_bytes=16384),
    ).canonical_bytes
    assert len(metadata) == 65536
    previous = 22
    for sequence in (30, 40):
        value = CommitCatalogEntry(CommitId(UUID, sequence), assign_commit_time(Timestamp(sequence), None), metadata)
        plan = store.plan_append(value)
        assert len(value.encode()) == 65596 and len(plan.images) <= 154
        images.reads.clear()
        assert store.validate_append_images(stamped(plan.images, sequence), previous_sequence=previous, sequence=sequence, activation_sequence=7) == value
        assert len(images.reads) <= 156  # Previous 64-KiB record, headers/directory, at most one split tail.
        images.apply(plan)
        previous = sequence


def test_applied_head_cannot_be_reinterpreted_as_the_predecessor() -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    plan = store.plan_append(entry(20))
    raw = stamped(plan.images, 20)
    for _ in range(2):
        assert store.validate_append_images(raw, previous_sequence=10, sequence=20, activation_sequence=7) == entry(20)
    images.apply(plan)
    before = dict(images.pages)
    with pytest.raises(GrafxCorruptionDetected) as failure:
        store.validate_append_images(raw, previous_sequence=10, sequence=20, activation_sequence=7)
    assert failure.value.details["field"] == "published_coverage"
    assert images.pages == before
