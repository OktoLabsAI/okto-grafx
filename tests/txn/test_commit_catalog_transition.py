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
from okto_grafx.domain.txn.records import decode_page_write, encode_page_write_record
from okto_grafx.domain.recovery.decision import CommittedReplay, committed_replay
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
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


def redo_fixture(count: int) -> tuple[Images, CommitCatalogStore, tuple[CommitCatalogPageImage, ...], int, CommitCatalogEntry]:
    images, planner = stack()
    initial_stream = CommitCatalogPageImage(DATA, 0, images.pages[DATA, 0])
    previous = 7
    for i in range(count):
        sequence = 10 + i * 2
        plan = planner.plan_append(entry(sequence))
        raw = stamped(plan.images + ((initial_stream,) if i == 0 else ()), sequence)
        images.pages.update({(image.file, image.page_index): image.raw for image in raw})
        previous = sequence
    value = entry(previous + 5)
    raw = stamped(planner.plan_append(value).images + ((initial_stream,) if count == 0 else ()), value.identity.sequence)
    if count == 0:
        images.pages.clear()  # First append must be recoverable solely from complete WAL images.

    def read(file: str, index: int) -> bytes:
        try:
            return images.read(file, index)
        except KeyError:
            raise GrafxCorruptionDetected("Missing immutable predecessor page.", field="test_missing_page") from None

    store = CommitCatalogStore(read, database_uuid=UUID, page_size=512)
    return images, store, raw, previous, value


@pytest.mark.parametrize("count", [0, 1, 7, 13])
def test_redo_checks_every_partial_page_cut_without_reading_overwritten_tails(count: int) -> None:
    images, store, raw, previous, value = redo_fixture(count)
    baseline = dict(images.pages)
    targets = {(image.file, image.page_index) for image in raw}
    for mask in range(1 << len(raw)):
        images.pages = dict(baseline)
        for i, image in enumerate(raw):
            if mask & (1 << i):
                images.pages[image.file, image.page_index] = image.raw
        for torn in (False, True):
            if torn:
                for location in targets:
                    images.pages[location] = bytes(512)
            before = dict(images.pages)
            images.reads.clear()
            for _ in range(2):
                assert store.validate_redo_images(raw, previous_sequence=previous, sequence=value.identity.sequence, activation_sequence=7) == value
            assert images.pages == before
            assert not targets.intersection(images.reads)
            assert len(images.reads) <= 6


@pytest.mark.parametrize("count", [0, 1, 7, 13])
def test_incomplete_wal_set_is_refused_even_if_all_physical_pages_already_landed(count: int) -> None:
    images, store, raw, previous, value = redo_fixture(count)
    images.pages.update({(image.file, image.page_index): image.raw for image in raw})
    before = dict(images.pages)
    for missing in range(len(raw)):
        with pytest.raises(GrafxCorruptionDetected):
            store.validate_redo_images(raw[:missing] + raw[missing + 1:], previous_sequence=previous, sequence=value.identity.sequence, activation_sequence=7)
    assert images.pages == before


def test_missing_existing_stream_header_is_not_an_empty_history() -> None:
    images, store, raw, previous, value = redo_fixture(1)
    del images.pages[DATA, 0]
    with pytest.raises(GrafxCorruptionDetected):
        store.validate_redo_images(raw, previous_sequence=previous, sequence=value.identity.sequence, activation_sequence=7)


def test_first_redo_record_cannot_skip_a_previous_writing_commit() -> None:
    _images, store, raw, _previous, value = redo_fixture(0)
    with pytest.raises(GrafxCorruptionDetected):
        store.validate_redo_images(raw, previous_sequence=8, sequence=value.identity.sequence, activation_sequence=7)


@pytest.mark.parametrize("damage", ["missing", "future_stamp"])
def test_immutable_older_directory_block_must_still_be_proved(damage: str) -> None:
    images, store, raw, previous, value = redo_fixture(13)
    if damage == "missing":
        del images.pages[DIR, 1]
    else:
        page = Page.from_bytes(images.pages[DIR, 1])
        page.page_lsn = value.identity.sequence
        images.pages[DIR, 1] = page.to_bytes()
    before = dict(images.pages)
    with pytest.raises(GrafxCorruptionDetected):
        store.validate_redo_images(raw, previous_sequence=previous, sequence=value.identity.sequence, activation_sequence=7)
    assert images.pages == before


def test_later_redo_cannot_recreate_the_immutable_stream_header() -> None:
    images, store, raw, previous, value = redo_fixture(1)
    extra = stamped((CommitCatalogPageImage(DATA, 0, images.pages[DATA, 0]),), value.identity.sequence)
    with pytest.raises(GrafxCorruptionDetected) as failure:
        store.validate_redo_images(raw + extra, previous_sequence=previous, sequence=value.identity.sequence, activation_sequence=7)
    assert failure.value.details["field"] == "redo_initialization"


def test_first_maximum_redo_then_next_append_need_no_mutable_tail_reads() -> None:
    images = Images()
    # Prepare privately from an empty logical catalog; no physical header exists.
    virtual, planner = stack()
    metadata = CommitMetadata(
        actor="a" * 16384, origin="o" * 16384, correlation_id="c" * 16384,
        reason="r" * 16354, limits=MetadataLimits(max_bytes=65536, max_string_bytes=16384),
    ).canonical_bytes
    first = CommitCatalogEntry(CommitId(UUID, 10), assign_commit_time(Timestamp(10), None), metadata)
    first_images = stamped(planner.plan_append(first).images + (CommitCatalogPageImage(DATA, 0, virtual.pages[DATA, 0]),), 10)
    store = CommitCatalogStore(images.read, database_uuid=UUID, page_size=512)
    assert len(first.encode()) == 65596 and len(first_images) <= 154
    assert store.validate_redo_images(first_images, previous_sequence=7, sequence=10, activation_sequence=7) == first
    assert images.reads == [] and images.pages == {}
    images.pages.update({(image.file, image.page_index): image.raw for image in first_images})
    plan = store.plan_append(entry(20))
    next_images = stamped(plan.images, 20)
    for image in next_images:
        images.pages[image.file, image.page_index] = bytes(512)
    images.reads.clear()
    before = dict(images.pages)
    assert store.validate_redo_images(next_images, previous_sequence=10, sequence=20, activation_sequence=7) == entry(20)
    assert len(images.reads) <= 153
    assert images.pages == before


def replay_fixture() -> tuple[Images, CommitCatalogStore, tuple[WalRecord, ...]]:
    images, store = stack()
    initial_stream = CommitCatalogPageImage(DATA, 0, images.pages[DATA, 0])
    records: list[WalRecord] = []
    for i, sequence in enumerate((12, 22, 32)):
        value = entry(sequence)
        if i == 1:
            value = replace(value, kind=CommitKind.MAINTENANCE)
        plan = store.plan_append(value)
        raw = stamped(plan.images + ((initial_stream,) if i == 0 else ()), sequence)
        for j, image in enumerate(raw):
            encoded = encode_page_write_record(image.file, image.page_index, image.raw, compress=i % 2 == 1)
            records.append(WalRecord(
                WalRecordType.WRITE_PAGE, encoded.payload, lsn=sequence - len(raw) + j,
                epoch=i + 1, txn_id=7, flags=encoded.flags, format_version=encoded.format_version,
            ))
        records.append(WalRecord(WalRecordType.COMMIT, lsn=sequence, epoch=i + 1, txn_id=7))
        images.pages.update({(image.file, image.page_index): image.raw for image in raw})
    images.reads.clear()
    return images, store, tuple(records)


@pytest.mark.parametrize("physical", ["empty", "applied", "torn"])
def test_multicommit_range_uses_prior_wal_overlays_and_keeps_epoch_qualified_owners(physical: str) -> None:
    images, store, records = replay_fixture()
    if physical == "empty":
        images.pages.clear()
    elif physical == "torn":
        images.pages = {key: bytes(512) for key in images.pages}
    before = dict(images.pages)
    result = store.validate_redo(committed_replay(records), previous_sequence=7, activation_sequence=7)
    assert [record.identity.sequence for record in result] == [12, 22, 32]
    assert [record.kind for record in result] == [CommitKind.DATA, CommitKind.MAINTENANCE, CommitKind.DATA]
    assert images.reads == [] and images.pages == before


def test_range_after_checkpoint_only_reads_the_immutable_stream_header() -> None:
    images, store, records = replay_fixture()
    images.pages[DIR, 0] = bytes(512)
    images.pages[DATA, 1] = bytes(512)
    before = dict(images.pages)
    result = store.validate_redo(committed_replay(r for r in records if r.lsn > 12), previous_sequence=12, activation_sequence=7)
    assert [record.identity.sequence for record in result] == [22, 32]
    assert set(images.reads) == {(DATA, 0)} and len(images.reads) <= 2
    assert images.pages == before


@pytest.mark.parametrize("missing", ["whole_commit", "journal_effects"])
def test_no_middle_commit_can_disappear_from_the_journal_range(missing: str) -> None:
    images, store, records = replay_fixture()
    offered = tuple(r for r in records if r.epoch != 2 or (missing == "journal_effects" and r.record_type == WalRecordType.COMMIT))
    before = dict(images.pages)
    with pytest.raises(GrafxCorruptionDetected):
        store.validate_redo(committed_replay(offered), previous_sequence=7, activation_sequence=7)
    assert images.pages == before


def test_range_crossing_activation_requires_its_commit_boundary() -> None:
    _images, store, records = replay_fixture()
    with pytest.raises(GrafxCorruptionDetected) as failure:
        store.validate_redo(committed_replay(records), previous_sequence=0, activation_sequence=7)
    assert failure.value.details["field"] == "redo_activation"
    activation = WalRecord(WalRecordType.COMMIT, lsn=7, epoch=0, txn_id=7)
    result = store.validate_redo(committed_replay((activation, *records)), previous_sequence=0, activation_sequence=7)
    assert [record.identity.sequence for record in result] == [12, 22, 32]


def test_replay_image_count_is_admitted_before_any_decompression(monkeypatch: pytest.MonkeyPatch) -> None:
    images, store, records = replay_fixture()
    offered = [replace(records[0], lsn=i + 1) for i in range(155)]
    offered.append(WalRecord(WalRecordType.COMMIT, lsn=200, epoch=1, txn_id=7))
    decoded: list[bool] = []

    def forbidden_decode(*args: object, **kwargs: object) -> None:
        decoded.append(True)
        raise AssertionError("Admission must precede image decoding.")

    monkeypatch.setattr("okto_grafx.engine.commit_catalog_store.decode_page_write", forbidden_decode)
    with pytest.raises(GrafxTransactionBudgetExceeded):
        store.validate_redo(committed_replay(offered), previous_sequence=7, activation_sequence=7)
    assert decoded == [] and images.reads == []


def test_coherent_rewrite_of_earlier_wal_prefix_is_refused() -> None:
    images, store, records = replay_fixture()
    offered = list(records)
    for i, record in enumerate(offered):
        if record.epoch != 2 or record.record_type != WalRecordType.WRITE_PAGE:
            continue
        decoded = decode_page_write(record.payload, format_version=record.format_version, flags=record.flags)
        if decoded.page_index != 1:
            continue
        page = Page.from_bytes(decoded.image)
        body = bytearray(page.read_slot(1))
        if decoded.file == DATA:
            body[:60] = entry(11).encode()
        else:
            pack_into("<Q", body, 0, 11)
            pack_into("<q", body, 24, 11)
        page.update_slot(1, bytes(body))
        encoded = encode_page_write_record(decoded.file, decoded.page_index, page.to_bytes(), compress=True)
        offered[i] = replace(record, payload=encoded.payload, flags=encoded.flags, format_version=encoded.format_version)
    before = dict(images.pages)
    with pytest.raises(GrafxCorruptionDetected) as failure:
        store.validate_redo(committed_replay(offered), previous_sequence=7, activation_sequence=7)
    assert failure.value.details["field"] == "append_prefix"
    assert images.pages == before


@pytest.mark.parametrize("watermark", [True, -1])
def test_empty_redo_still_requires_a_valid_exact_watermark(watermark: int) -> None:
    images, store = stack()
    images.reads.clear()
    with pytest.raises(GrafxCorruptionDetected):
        store.validate_redo(CommittedReplay(last_committed_lsn=watermark), previous_sequence=1, activation_sequence=7)
    assert images.reads == []


@pytest.mark.parametrize("last", [0, 7])
def test_empty_checkpoint_replay_has_no_journal_or_host_work(last: int) -> None:
    images, store = stack()
    images.reads.clear()
    assert store.validate_redo(CommittedReplay(last_committed_lsn=last), previous_sequence=7, activation_sequence=7) == ()
    assert images.reads == []
