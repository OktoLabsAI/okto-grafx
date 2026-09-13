"""Detached temporal image prototype, not native publication or crash certification."""

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError, GrafxCorruptionDetected, GrafxError, GrafxQueryBudgetExceeded,
)
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page import Page
from okto_grafx.domain.txn.commit_identity import CommitId
from okto_grafx.domain.txn.records import is_redoable_page_file
from okto_grafx.engine.system_history_store import HistoryChange, SystemHistoryStore

IDENTITY = bytes(range(16))
TABLE = TableDef(1, "N", "node", (ColumnDef("id", ValueType.INT64),
                                ColumnDef("text", ValueType.STRING)), primary_key="id")


def store_fixture(page_size=512):
    pages = {}
    store = SystemHistoryStore(lambda file, index: pages[file, index],
                               database_uuid=IDENTITY, page_size=page_size)
    image = store.initialize(10)
    pages[image.file, image.page_index] = image.raw
    return store, pages


def install(pages, images):
    for image in images:
        pages[image.file, image.page_index] = image.raw


def test_event_images_capture_schema_lineage_and_do_not_write():
    store, pages = store_fixture()
    initial = dict(pages)
    changes = (HistoryChange(TABLE, 4, 1, (1, "a" * 3000)),)
    prepared = store.prepare(changes, expected_sequence=10, page_count=1)
    images = prepared.bind(20)
    assert len(images) == prepared.image_count > 2
    assert pages == initial
    install(pages, images)
    batches = store.read_batches(expected_sequence=20, page_count=len(pages))
    assert batches[0][0].sequence == 20
    assert batches[0][1] == changes
    for sequence, change in ((30, HistoryChange(TABLE, 4, 2, (1, "updated"))),
                             (40, HistoryChange(TABLE, 4, 3, ())),
                             (50, HistoryChange(TABLE, 99, 1, (1, "recreated")))):
        plan = store.prepare((change,), expected_sequence=sequence - 10, page_count=len(pages))
        install(pages, plan.bind(sequence))
    result = store.read_batches(expected_sequence=50, page_count=len(pages))
    assert [batch[0].sequence for batch in result] == [20, 30, 40, 50]
    assert [batch[1][0].record_id for batch in result] == [4, 4, 4, 99]
    assert result[0][1][0].values == (1, "a" * 3000)


def test_prepared_rebinding_is_fixed_size_and_repeated_application_identical():
    store, pages = store_fixture()
    plan = store.prepare((HistoryChange(TABLE, 1, 1, (1, "value")),),
                         expected_sequence=10, page_count=1)
    earlier, later = plan.bind(20), plan.bind(21)
    assert len(earlier) == len(later) == plan.image_count
    assert all(len(image.raw) == 512 for image in earlier + later)
    install(pages, later)
    first = dict(pages)
    install(pages, later)
    assert pages == first
    assert store.read_batches(expected_sequence=21, page_count=len(pages))[0][0].sequence == 21
    with pytest.raises(GrafxConfigurationError):
        plan.bind(10)


@pytest.mark.parametrize("mutation", ["missing", "reorder", "foreign", "future", "payload", "old_head"])
def test_incomplete_or_changed_pictures_fail_closed(mutation):
    store, pages = store_fixture()
    initial = dict(pages)
    plan = store.prepare((HistoryChange(TABLE, 1, 1, (1, "a" * 1800)),),
                         expected_sequence=10, page_count=1)
    images = plan.bind(20)
    install(pages, images)
    key = (images[0].file, 1)
    if mutation == "missing":
        pages[key] = Page(page_size=512).to_bytes()
    elif mutation == "reorder":
        pages[key] = pages[key[0], 2]
    elif mutation == "foreign":
        store.database_uuid = bytes(reversed(IDENTITY))
    elif mutation == "old_head":
        pages.update(initial)
    else:
        page = Page.from_bytes(pages[key])
        if mutation == "future":
            page.page_lsn = 21
        else:
            raw = bytearray(page.read_slot(0))
            raw[-1] ^= 1
            page.update_slot(0, raw)
        pages[key] = page.to_bytes()  # valid CRC: structure/digest must still refuse
    with pytest.raises(GrafxCorruptionDetected):
        store.read_batches(expected_sequence=20, page_count=len(pages))


@pytest.mark.parametrize("change", [HistoryChange(TABLE, True, 1, (1, "a")),
    HistoryChange(TABLE, 0, 1, (1, "a")), HistoryChange(TABLE, 1, 4, (1, "a")),
    HistoryChange(TABLE, 1, 3, (1, "a")), HistoryChange(TABLE, 1, 1, ("bad", "a"))])
def test_invalid_change_has_no_image_effects(change):
    store, pages = store_fixture()
    initial = dict(pages)
    with pytest.raises(GrafxError):
        store.prepare((change,), expected_sequence=10, page_count=1)
    assert pages == initial


def test_unsettled_effects_and_bounds_refuse_without_native_activation():
    store, pages = store_fixture()
    change = HistoryChange(TABLE, 1, 1, (1, "a"))
    with pytest.raises(GrafxConfigurationError):
        store.prepare((change, change), expected_sequence=10, page_count=1)
    with pytest.raises(GrafxConfigurationError):
        store.read_batches(expected_sequence=10, page_count=1, max_pages=0)
    with pytest.raises(GrafxCorruptionDetected):
        store.prepare((change,), expected_sequence=11, page_count=1)
    assert not is_redoable_page_file("system-history.dat")


def test_prior_schema_is_self_contained_and_empty_batch_is_valid():
    store, pages = store_fixture()
    plan = store.prepare((HistoryChange(TABLE, 1, 1, (1, "a")),), expected_sequence=10, page_count=1)
    install(pages, plan.bind(20))
    evolved = replace(TABLE, columns=(*TABLE.columns, ColumnDef("extra", ValueType.STRING)),
                      schema_version=2, schema_layouts=((1, 2),))
    plan = store.prepare((HistoryChange(evolved, 1, 2, (1, "a", None)),),
                         expected_sequence=20, page_count=len(pages))
    install(pages, plan.bind(30))
    install(pages, store.prepare((), expected_sequence=30, page_count=len(pages)).bind(40))
    batches = store.read_batches(expected_sequence=40, page_count=len(pages))
    assert batches[0][1][0].table.schema_version == 1
    assert batches[1][1][0].table.schema_version == 2
    assert batches[2][1] == ()


@pytest.mark.parametrize("budget", [{"max_bytes": 10}, {"max_changes": 1}])
def test_total_read_budget_refuses_without_a_partial_result(budget):
    store, pages = store_fixture()
    changes = tuple(HistoryChange(TABLE, n, 1, (n, "value")) for n in (1, 2))
    install(pages, store.prepare(changes, expected_sequence=10, page_count=1).bind(20))
    with pytest.raises(GrafxQueryBudgetExceeded):
        store.read_batches(expected_sequence=20, page_count=len(pages), **budget)


def test_append_transition_reads_only_predecessor_and_never_installs_images():
    store, pages = store_fixture()
    install(pages, store.prepare((HistoryChange(TABLE, 1, 1, (1, "old")),),
                                expected_sequence=10, page_count=1).bind(20))
    before = dict(pages)
    changes = (HistoryChange(TABLE, 1, 2, (1, "x" * 1800)),)
    images = store.prepare(changes, expected_sequence=20, page_count=len(pages)).bind(30)
    reads = []

    def read(file, number):
        reads.append((file, number))
        assert number == 0  # No O(retained history) read or missing-chunk fallback.
        return pages[file, number]

    store.read_page = read
    assert store.validate_append_images(tuple(reversed(images)), previous_sequence=20,
                                        page_count=len(pages), commit=CommitId(IDENTITY, 30)) == changes
    assert reads == [(images[0].file, 0)]
    assert pages == before


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "old_chunk", "gap", "file",
    "short", "future_chunk", "prior_chain", "activation", "ordinal", "head_only", "splice"])
def test_append_transition_refuses_valid_crc_but_incomplete_or_spliced_batch(mutation):
    store, pages = store_fixture()
    install(pages, store.prepare((HistoryChange(TABLE, 1, 1, (1, "old")),),
                                expected_sequence=10, page_count=1).bind(20))
    before = dict(pages)
    count = len(pages)
    changes = (HistoryChange(TABLE, 1, 2, (1, "x" * 1800)),)
    plan = store.prepare(changes, expected_sequence=20, page_count=count)
    images = list(plan.bind(30))
    if mutation == "missing":
        images.pop()
    elif mutation == "duplicate":
        images.append(images[-1])
    elif mutation == "old_chunk":
        images[1] = replace(images[1], page_index=1)
    elif mutation == "gap":
        images[-1] = replace(images[-1], page_index=images[-1].page_index + 1)
    elif mutation == "file":
        images[-1] = replace(images[-1], file="heap.dat")
    elif mutation == "short":
        images[-1] = replace(images[-1], raw=images[-1].raw[:-1])
    elif mutation == "head_only":
        images = images[:1]
    elif mutation == "splice":
        other = store.prepare((HistoryChange(TABLE, 1, 2, (1, "y" * 1800)),),
                              expected_sequence=20, page_count=count).bind(30)
        images[-1] = other[-1]
    elif mutation == "prior_chain":
        images = list(replace(plan, previous_digest=b"z" * 32).bind(30))
    elif mutation == "activation":
        images = list(replace(plan, activation=11).bind(30))
    elif mutation == "ordinal":
        # Forge an entirely internally consistent batch at the wrong ordinal.
        images = list(replace(plan, batch_count=0, first_page=1, previous_digest=bytes(32),
                              activation=20).bind(30))
    else:
        page = Page.from_bytes(images[-1].raw)
        page.page_lsn = 31
        images[-1] = replace(images[-1], raw=page.to_bytes())
    with pytest.raises(GrafxCorruptionDetected):
        store.validate_append_images(tuple(images), previous_sequence=20,
                                     page_count=count, commit=CommitId(IDENTITY, 30))
    assert pages == before


@pytest.mark.parametrize("commit", [CommitId(bytes(reversed(IDENTITY)), 20), CommitId(IDENTITY, 10), 20])
def test_transition_invalid_coordinate_refuses_before_io(commit):
    store, pages = store_fixture()
    images = store.prepare((), expected_sequence=10, page_count=1).bind(20)

    def no_read(file, index):
        pytest.fail("Invalid coordinate must refuse before any storage access.")

    store.read_page = no_read
    with pytest.raises(GrafxConfigurationError):
        store.validate_append_images(images, previous_sequence=10, page_count=1, commit=commit)


def test_transition_does_not_treat_already_applied_root_as_its_own_predecessor():
    store, pages = store_fixture()
    images = store.prepare((), expected_sequence=10, page_count=1).bind(20)
    assert store.validate_append_images(images, previous_sequence=10, page_count=1,
                                        commit=CommitId(IDENTITY, 20)) == ()
    install(pages, images)
    with pytest.raises(GrafxCorruptionDetected):
        store.validate_append_images(images, previous_sequence=10, page_count=1,
                                     commit=CommitId(IDENTITY, 20))
