"""Paged commit history plans: bounded append/seek, no independent durable writes."""

from __future__ import annotations

import math
from pathlib import Path
from struct import pack_into

import pytest

from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.domain.errors import (
    GrafxConfigurationError, GrafxCorruptionDetected, GrafxSchemaVersionMismatch,
    GrafxTransactionBudgetExceeded,
)
from okto_grafx.domain.ids import PROVISIONAL_CSN
from okto_grafx.domain.model.value import Timestamp
from okto_grafx.domain.page import Page
from okto_grafx.domain.txn.commit_catalog import CommitCatalogEntry
from okto_grafx.domain.txn.commit_catalog import CommitKind
from okto_grafx.domain.txn.commit_identity import CommitId, assign_commit_time
from okto_grafx.domain.txn.commit_metadata import CommitMetadata, MetadataLimits
from okto_grafx.engine.commit_catalog_store import (
    COMMIT_DIRECTORY_FILE as DIR,
    COMMIT_STREAM_FILE as DATA,
    CommitCatalogHead,
    CommitCatalogPlan,
    CommitCatalogStore,
)


UUID = bytes(range(16))


class Images:
    def __init__(self) -> None:
        self.pages: dict[tuple[str, int], bytes] = {}
        self.reads: list[tuple[str, int]] = []

    def read(self, file: str, index: int) -> bytes:
        self.reads.append((file, index))
        try:
            return self.pages[file, index]
        except KeyError:
            raise GrafxCorruptionDetected("Missing test page.") from None

    def apply(self, plan: CommitCatalogPlan) -> None:
        for image in plan.images:
            self.pages[image.file, image.page_index] = image.raw


def entry(sequence: int, *, large: bool = False) -> CommitCatalogEntry:
    metadata = CommitMetadata(
        attributes={str(i): "x" * 16000 for i in range(4)},
        limits=MetadataLimits(max_bytes=65536, max_string_bytes=16384),
    ) if large else None
    return CommitCatalogEntry(
        CommitId(UUID, sequence), assign_commit_time(Timestamp(sequence), None),
        metadata.canonical_bytes if metadata else None,
    )


def stack(page_size: int = 512) -> tuple[Images, CommitCatalogStore]:
    images = Images()
    store = CommitCatalogStore(images.read, database_uuid=UUID, page_size=page_size)
    images.apply(store.plan_initialize(activation_sequence=7))
    return images, store


@pytest.mark.parametrize("page_size", [512, 1024, 8192, 32768])
def test_append_and_cold_lookup_cross_page_boundaries(page_size: int) -> None:
    images, store = stack(page_size)
    values = [entry(10), entry(20, large=True), entry(30)]
    for value in values:
        before = dict(images.pages)
        plan = store.plan_append(value)
        assert images.pages == before  # Planning cannot write anything.
        images.apply(plan)
    cold = CommitCatalogStore(images.read, database_uuid=UUID, page_size=page_size)
    assert cold.verify().entry_count == 3
    for value in values:
        assert cold.lookup(value.identity, read_lsn=30) == value
    assert cold.lookup(values[-1].identity, read_lsn=20) is None
    assert cold.lookup(CommitId(UUID, 15), read_lsn=30) is None


def test_append_work_and_seek_reads_do_not_scan_retained_history() -> None:
    images, store = stack()
    for sequence in range(10, 2010, 2):
        images.reads.clear()
        plan = store.plan_append(entry(sequence))
        assert len(plan.images) <= 4
        assert len(images.reads) <= 8
        images.apply(plan)
    images.reads.clear()
    assert store.lookup(CommitId(UUID, 888), read_lsn=3000) == entry(888)
    assert len(images.reads) <= math.ceil(math.log2(1000)) + 8
    assert store.verify().entry_count == 1000


def test_reapplying_complete_images_does_not_duplicate_history() -> None:
    images, store = stack()
    plan = store.plan_append(entry(10, large=True))
    images.apply(plan)
    once = dict(images.pages)
    images.apply(plan)
    assert images.pages == once
    assert store.verify().entry_count == 1


def mutate_slot(images: Images, file: str, index: int, slot: int, offset: int, value: bytes) -> None:
    page = Page.from_bytes(images.pages[file, index])
    payload = bytearray(page.read_slot(slot))
    payload[offset:offset + len(value)] = value
    page.update_slot(slot, bytes(payload))
    images.pages[file, index] = page.to_bytes()  # Deliberately valid page checksum.


@pytest.mark.parametrize("file,index,slot,offset,value", [
    (DIR, 0, 1, 12, bytes(16)),  # foreign directory header UUID
    (DATA, 0, 1, 12, bytes(16)),  # foreign stream header UUID
    (DIR, 1, 0, 12, bytes(16)),
    (DATA, 1, 0, 12, bytes(16)),
    (DIR, 0, 1, 28, (8).to_bytes(8, "little")),  # inconsistent legacy horizon
    (DATA, 0, 1, 28, (8).to_bytes(8, "little")),
    (DATA, 0, 1, 36, (1).to_bytes(8, "little")),  # stream header must be static
    (DIR, 0, 1, 36, PROVISIONAL_CSN.to_bytes(8, "little")),
    (DIR, 0, 1, 44, (59).to_bytes(8, "little")),
    (DIR, 0, 1, 52, (11).to_bytes(8, "little")),
    (DIR, 0, 1, 60, (11).to_bytes(8, "little")),
    (DIR, 1, 0, 28, (1).to_bytes(8, "little")),  # wrong ordinal base
    (DATA, 1, 0, 28, (1).to_bytes(8, "little")),  # wrong stream base
    (DIR, 1, 1, 0, (7).to_bytes(8, "little")),
    (DIR, 1, 1, 8, (1).to_bytes(8, "little")),
    (DIR, 1, 1, 16, (59).to_bytes(4, "little")),
    (DIR, 1, 1, 20, (1).to_bytes(4, "little")),
    (DIR, 1, 1, 24, (9).to_bytes(8, "little")),
    (DATA, 1, 1, 0, b"broken!!"),  # valid page CRC cannot legitimize bad record CRC
    (DIR, 0, 1, 0, b"broken!!"),
    (DIR, 1, 0, 10, (2).to_bytes(2, "little")),  # swapped logical role
])
def test_checksum_valid_corruption_is_not_absence(
    file: str, index: int, slot: int, offset: int, value: bytes,
) -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    mutate_slot(images, file, index, slot, offset, value)
    before = dict(images.pages)
    with pytest.raises(GrafxCorruptionDetected):
        store.lookup(CommitId(UUID, 10), read_lsn=10)
    with pytest.raises(GrafxCorruptionDetected):
        store.verify()
    assert images.pages == before


@pytest.mark.parametrize("file,index,slot", [(DIR, 0, 1), (DATA, 0, 1), (DIR, 1, 0), (DATA, 1, 0)])
def test_intact_unknown_page_versions_refuse_as_unsupported(file: str, index: int, slot: int) -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    mutate_slot(images, file, index, slot, 8, (2).to_bytes(2, "little"))
    with pytest.raises(GrafxSchemaVersionMismatch):
        store.verify()


@pytest.mark.parametrize("damage", ["crc", "truncated", "missing", "odd", "flags", "next", "dead_slot"])
def test_invalid_physical_page_is_never_treated_as_missing_commit(damage: str) -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    raw = images.pages[DATA, 1]
    page = Page.from_bytes(raw)
    if damage == "crc":
        images.pages[DATA, 1] = raw[:40] + bytes([raw[40] ^ 1]) + raw[41:]
    elif damage == "truncated":
        images.pages[DATA, 1] = raw[:-1]
    elif damage == "missing":
        del images.pages[DATA, 1]
    else:
        if damage == "odd":
            page.seq = 1
        elif damage == "flags":
            page.flags = 1
        elif damage == "next":
            page.next_page = 1
        else:
            page.free_slot(1)
        images.pages[DATA, 1] = page.to_bytes()
    with pytest.raises(GrafxCorruptionDetected):
        store.verify()


def test_full_verify_checks_cross_directory_page_order_not_only_local_pages() -> None:
    images, store = stack()
    for sequence in range(10, 290, 10):
        images.apply(store.plan_append(entry(sequence)))
    # A 512-byte page holds thirteen 32-byte directory entries. Make the first
    # sequence on page two precede page one's last, preserving page-local order.
    mutate_slot(images, DIR, 2, 1, 0, (125).to_bytes(8, "little"))
    with pytest.raises(GrafxCorruptionDetected) as failure:
        store.verify()
    assert failure.value.details["field"] == "directory_order"


def test_swapped_fragments_and_truncated_coverage_refuse() -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10, large=True)))
    images.pages[DATA, 1], images.pages[DATA, 2] = images.pages[DATA, 2], images.pages[DATA, 1]
    with pytest.raises(GrafxCorruptionDetected):
        store.verify()


@pytest.mark.parametrize("sequence", [0, 7, 10])
def test_duplicate_or_backward_append_produces_no_effect(sequence: int) -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    before = dict(images.pages)
    with pytest.raises(GrafxConfigurationError):
        store.plan_append(entry(sequence))
    assert images.pages == before


def test_append_refuses_clock_regression_and_foreign_identity_before_effects() -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    backward = CommitCatalogEntry(CommitId(UUID, 20), assign_commit_time(Timestamp(9), None))
    foreign = CommitCatalogEntry(CommitId(bytes(16), 20), assign_commit_time(Timestamp(20), None))
    for candidate in (backward, foreign):
        with pytest.raises(GrafxConfigurationError):
            store.plan_append(candidate)
    assert store.verify().entry_count == 1


@pytest.mark.parametrize("read_lsn", [True, -1, PROVISIONAL_CSN, 1.5, "10"])
def test_invalid_snapshot_input_is_not_coerced(read_lsn: object) -> None:
    _images, store = stack()
    with pytest.raises(GrafxConfigurationError):
        store.lookup(CommitId(UUID, 10), read_lsn=read_lsn)  # type: ignore[arg-type]


def test_empty_legacy_boundary_and_signed_clock_are_preserved() -> None:
    images, store = stack()
    assert store.verify().activation_sequence == 7
    assert store.lookup(CommitId(UUID, 7), read_lsn=10) is None
    first = CommitCatalogEntry(CommitId(UUID, 10), assign_commit_time(Timestamp(-50), None))
    images.apply(store.plan_append(first))
    second = CommitCatalogEntry(CommitId(UUID, 20), assign_commit_time(Timestamp(-100), Timestamp(-50)))
    images.apply(store.plan_append(second))
    assert store.lookup(first.identity, read_lsn=10) == first
    assert store.lookup(second.identity, read_lsn=20) == second
    assert store.verify().last_ordered_micros == -49


def test_published_head_with_any_missing_image_refuses_until_complete() -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    before = dict(images.pages)
    plan = store.plan_append(entry(20, large=True))
    head_image = plan.images[-1]
    for missing in range(len(plan.images) - 1):
        images.pages = dict(before)
        images.pages[head_image.file, head_image.page_index] = head_image.raw
        for position, image in enumerate(plan.images[:-1]):
            if position != missing:
                images.pages[image.file, image.page_index] = image.raw
        with pytest.raises(GrafxCorruptionDetected):
            store.verify()
        images.apply(plan)
        assert store.verify().entry_count == 2


def test_append_preserves_prefix_and_snapshot_results_after_growth() -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    original = Page.from_bytes(images.pages[DATA, 1]).read_slot(1)
    for sequence in range(20, 400, 10):
        images.apply(store.plan_append(entry(sequence)))
    assert Page.from_bytes(images.pages[DATA, 1]).read_slot(1).startswith(original)
    assert store.lookup(CommitId(UUID, 10), read_lsn=10) == entry(10)
    assert store.lookup(CommitId(UUID, 20), read_lsn=10) is None


def test_plans_reopen_through_real_storage_port_without_private_io(tmp_path: Path) -> None:
    # Test-only materialization, NOT an example of transactional publication.
    def apply(device: LocalStorageDevice, plan: CommitCatalogPlan) -> None:
        for image in plan.images:
            if not device.exists(image.file):
                device.create(image.file)
            count = device.page_count(image.file)
            if count <= image.page_index:
                device.allocate(image.file, image.page_index + 1 - count)
            device.write_page(image.file, image.page_index, image.raw)
        device.durable_barrier()

    with LocalStorageDevice(tmp_path, page_size=512) as device:
        store = CommitCatalogStore(device.read_page, database_uuid=UUID, page_size=512)
        apply(device, store.plan_initialize(activation_sequence=7))
        apply(device, store.plan_append(entry(10, large=True)))
        apply(device, store.plan_append(entry(20)))
    with LocalStorageDevice(tmp_path, page_size=512, create_root=False) as device:
        cold = CommitCatalogStore(device.read_page, database_uuid=UUID, page_size=512)
        assert cold.verify().entry_count == 2
        assert cold.lookup(CommitId(UUID, 10), read_lsn=10) == entry(10, large=True)


def test_no_runtime_capability_or_redo_target_enabled() -> None:
    from okto_grafx.domain.model.catalog import Catalog
    from okto_grafx.domain.txn.records import is_redoable_page_file

    assert "commit_catalog_v1" not in Catalog().required_capabilities()
    assert not is_redoable_page_file(DIR)
    assert not is_redoable_page_file(DATA)


def test_declared_page_address_exhaustion_refuses_before_any_block_read() -> None:
    images, store = stack()
    head_page = Page.from_bytes(images.pages[DIR, 0])
    raw = bytearray(head_page.read_slot(1))
    count = (2**32 - 1) * 13
    pack_into("<QQQq", raw, 36, count, count * 60, count + 7, 10)
    head_page.update_slot(1, bytes(raw))
    images.pages[DIR, 0] = head_page.to_bytes()
    images.reads.clear()
    with pytest.raises(GrafxCorruptionDetected):
        store.verify()
    assert images.reads == [(DIR, 0)]


def test_healthy_store_capacity_exhaustion_is_budget_not_corruption(monkeypatch: pytest.MonkeyPatch) -> None:
    images, store = stack()
    stream_bytes = (2**32 - 2) * (512 - 76)
    count = stream_bytes // 60
    head = CommitCatalogHead(7, count, stream_bytes, count + 7, 10)
    monkeypatch.setattr(store, "read_head", lambda: head)
    images.reads.clear()
    with pytest.raises(GrafxTransactionBudgetExceeded) as failure:
        store.plan_append(entry(count + 8))
    assert failure.value.details["field"] == "commit_catalog_pages"
    assert images.reads == []


def test_generic_header_corruption_does_not_echo_embedded_text() -> None:
    images, store = stack()
    mutate_slot(images, DIR, 0, 0, 0, b"SECRET!!")
    with pytest.raises(GrafxCorruptionDetected) as failure:
        store.verify()
    assert "SECRET" not in str(failure.value)
    assert "SECRET" not in repr(failure.value.details)


def test_plan_is_detached_from_caller_entry_and_does_not_disclose_metadata() -> None:
    images, store = stack()
    value = CommitCatalogEntry(
        CommitId(UUID, 10), assign_commit_time(Timestamp(10), None),
        CommitMetadata(reason="private reason marker").canonical_bytes,
    )
    plan = store.plan_append(value)
    object.__setattr__(value, "metadata_bytes", b"broken")
    object.__setattr__(value.identity, "sequence", 50)
    images.apply(plan)
    loaded = store.lookup(CommitId(UUID, 10), read_lsn=10)
    assert loaded is not None and loaded.metadata is not None
    assert loaded.metadata.reason == "private reason marker"
    assert "private reason marker" not in repr(plan)


def test_maximum_record_has_bounded_page_plan() -> None:
    images, store = stack()
    metadata = CommitMetadata(
        actor="x" * 16384, origin="x" * 16384, correlation_id="x" * 16384,
        reason="x" * 16354, limits=MetadataLimits(max_bytes=65536, max_string_bytes=16384),
    )
    assert len(metadata.canonical_bytes) == 65536
    value = CommitCatalogEntry(
        CommitId(UUID, 10), assign_commit_time(Timestamp(10), None), metadata.canonical_bytes,
    )
    assert len(value.encode()) == 65596
    plan = store.plan_append(value)
    assert len(plan.images) == math.ceil(65596 / (512 - 76)) + 2
    images.apply(plan)
    assert store.lookup(value.identity, read_lsn=10) == value


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("kind", [CommitKind.DATA, CommitKind.MAINTENANCE])
def test_prepared_append_retargets_final_csn_without_host_io(large: bool, kind: CommitKind) -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    metadata = entry(20, large=large).metadata_bytes
    observed = Timestamp(5)  # Wall clock regressed below the last durable ordered time.
    prepared = store.prepare_append(
        expected_last_sequence=10, observed_at=observed, metadata_bytes=metadata, kind=kind,
    )
    read_count = len(images.reads)
    object.__setattr__(observed, "micros", 100000)  # Caller mutation cannot move the captured time.
    # Ordinary page/index effects and a COMMIT contribute 18 records; a segment
    # roll adds one more LSN. The journal adds its known full-image cardinality.
    predicted = 10 + 18 + prepared.image_count
    initial = prepared.bind(predicted)
    final = prepared.bind(predicted + 1)
    assert len(initial.images) == len(final.images) == prepared.image_count
    assert [len(i.raw) for i in initial.images] == [len(i.raw) for i in final.images]
    assert [(i.file, i.page_index) for i in initial.images] == [(i.file, i.page_index) for i in final.images]
    assert len(images.reads) == read_count
    assert prepared.bind(predicted + 1) == final
    images.apply(final)
    record = store.lookup(CommitId(UUID, predicted + 1), read_lsn=predicted + 1)
    assert record is not None
    assert record.kind is kind
    assert record.metadata_bytes == metadata
    assert record.timing.observed_at.micros == 5
    assert record.timing.ordered_at.micros == 11
    assert record.timing.clock_adjusted
    assert store.lookup(CommitId(UUID, predicted), read_lsn=predicted + 1) is None
    assert store.verify().entry_count == 2


@pytest.mark.parametrize("expected", [7, 11])
def test_preparation_refuses_valid_head_that_does_not_match_published_control(expected: int) -> None:
    images, store = stack()
    images.apply(store.plan_append(entry(10)))
    before = dict(images.pages)
    with pytest.raises(GrafxCorruptionDetected) as failure:
        store.prepare_append(expected_last_sequence=expected, observed_at=Timestamp(10))
    assert failure.value.details["field"] == "published_coverage"
    assert images.pages == before


@pytest.mark.parametrize("invalid", ["metadata", "clock", "kind", "baseline"])
def test_invalid_preparation_is_refused_before_first_storage_read(invalid: str) -> None:
    images, store = stack()
    images.reads.clear()
    with pytest.raises((GrafxConfigurationError, GrafxCorruptionDetected)):
        store.prepare_append(
            expected_last_sequence=True if invalid == "baseline" else 7,
            observed_at="now" if invalid == "clock" else Timestamp(10),  # type: ignore[arg-type]
            kind=1 if invalid == "kind" else CommitKind.DATA,  # type: ignore[arg-type]
            metadata_bytes=b"broken" if invalid == "metadata" else None,
        )
    assert images.reads == []


def test_prepared_append_is_bounded_by_tail_not_total_history() -> None:
    images, store = stack()
    for sequence in range(10, 2010, 2):
        images.apply(store.plan_append(entry(sequence)))
    images.reads.clear()
    prepared = store.prepare_append(expected_last_sequence=2008, observed_at=Timestamp(3000))
    assert len(images.reads) <= 5
    captured_reads = list(images.reads)
    # A later host-side replacement cannot change this attempt's bytes. It also
    # cannot make them authoritative: the caller still owes staging/OCC/fence checks.
    images.pages = {}
    assert prepared.bind(2020).head.last_sequence == 2020
    assert images.reads == captured_reads


def test_preparation_clock_overflow_refuses_without_effects() -> None:
    images, store = stack()
    final_time = CommitCatalogEntry(
        CommitId(UUID, 10), assign_commit_time(Timestamp(2**63 - 1), None),
    )
    images.apply(store.plan_append(final_time))
    before = dict(images.pages)
    with pytest.raises(GrafxConfigurationError):
        store.prepare_append(expected_last_sequence=10, observed_at=Timestamp(0))
    assert images.pages == before
