"""Native label metadata retains row identity, version history and durable admission."""

from dataclasses import replace
import struct

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected, GrafxSchemaVersionMismatch, GrafxRecoveryRefused, GrafxTransactionStateError
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model import catalog as catalog_module
from okto_grafx.domain.model.node_labels import NODE_LABELS_CAPABILITY, NODE_LABELS_CAPABILITY_BIT, encode_node_labels
from okto_grafx.domain.model.schema import ColumnDef, TableDef, encode_tuple
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RECORD_FLAG_NODE_LABELS, RecordHeader
from okto_grafx.domain.page import Page, crc32c
from okto_grafx.domain.recovery.decision import committed_replay
from okto_grafx.domain.txn.records import encode_page_write_record
from okto_grafx.domain.wal.commit import CommitPayload
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.engine.commit_redo import CommitRedo
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from .conftest import SnapshotDouble, MemoryDevice, RecordingMetrics, make_pool
from .test_catalog_replay_images import records, staged


def state(*, labels=("X",), activate=True):
    result = Catalog()
    result.upgrade_index_catalog(())
    result.add_table(TableDef(1, "N", "node", (ColumnDef("id", ValueType.INT64), ColumnDef("body", ValueType.STRING))))
    if activate:
        result.extend_node_labels(1, labels)
    return result


def install(catalog_store, **kwargs):
    catalog_store.adopt(state(**kwargs))
    catalog_store.save()
    return catalog_store.catalog.table("N")


def test_catalog_has_exact_capability_and_canonical_table_metadata():
    plain = state(activate=False)
    labels = state(labels=("X", "N", "X"))
    body = bytearray(plain.serialize()[:-4])
    bits = struct.unpack_from("<Q", body, 28)[0]
    struct.pack_into("<Q", body, 28, bits | NODE_LABELS_CAPABILITY_BIT)
    body += b"GXL1\x01\0\x01\0X"
    assert labels.serialize() == bytes(body) + struct.pack("<I", crc32c(bytes(body)))
    assert labels.requires_capability(NODE_LABELS_CAPABILITY)
    assert labels.table("N").extra_node_labels == ("X",)
    assert labels.table("N").node_label_candidates == ("N", "X")
    assert Catalog.deserialize(labels.serialize()) == labels
    assert Catalog.deserialize(plain.serialize()) == plain


def test_candidate_growth_is_owned_monotone_and_does_not_redefine_physical_schema():
    original = state()
    copied = original.copy()
    old = copied.table("N")
    new = copied.extend_node_labels(1, ("é", "Z"))
    assert (new.table_id, new.name, new.columns, new.schema_version) == (old.table_id, old.name, old.columns, old.schema_version)
    assert original.table("N").extra_node_labels == ("X",)
    assert new.extra_node_labels == ("X", "Z", "é")
    assert copied.extend_node_labels(1, ()) is new
    assert Catalog.deserialize(copied.serialize()) == copied
    before = copied.serialize()
    with pytest.raises(GrafxConfigurationError):
        copied.extend_node_labels(1, ("a" * 65527,))  # Valid alone, too large in the candidate union.
    assert copied.serialize() is before


@pytest.mark.parametrize("labels", [["X"], ("X", "X"), ("Z", "A"), ("",)])
def test_noncanonical_table_candidates_refuse(labels):
    with pytest.raises(GrafxConfigurationError):
        replace(state().table("N"), extra_node_labels=labels)


def test_relationship_candidate_metadata_is_never_admitted():
    with pytest.raises(GrafxConfigurationError):
        TableDef(2, "R", "rel", (), from_table="N", to_table="N", extra_node_labels=("X",))


def test_candidate_cache_is_owned_and_not_rebuilt_per_heap_row(catalog_store, heap_store, monkeypatch):
    from okto_grafx.domain.model import schema as schema_module

    table = install(catalog_store)
    assert table.node_label_candidates is table.node_label_candidates
    assert not table.admits_node_labels(("absent",))

    def forbidden(*args, **kwargs):
        pytest.fail("A row operation must not sort/encode all catalog label candidates again")

    monkeypatch.setattr(schema_module, "normalize_node_labels", forbidden)
    ref = heap_store.insert(table, 1, (1, "body"), xmin=10, node_labels=("N", "X"))
    for _ in range(3):
        assert heap_store.read(ref).node_labels == ("N", "X")
        assert heap_store.read_landing(ref).node_labels == ("N", "X")


def test_constructor_refuses_an_oversized_union_with_the_implicit_label():
    # The extra-label frame fits alone, but the physical base N makes its full
    # candidate union unrepresentable. Reject when constructing, not at row read.
    with pytest.raises(GrafxConfigurationError):
        replace(state().table("N"), extra_node_labels=("a" * 65527,))


@pytest.mark.parametrize("method", ["insert", "insert_reserved", "insert_initial_reserved", "update"])
@pytest.mark.parametrize("forgery", ["extra_candidate", "different_name", "unknown_table"])
def test_caller_table_cannot_forge_label_authority(catalog_store, heap_store, method, forgery):
    table = install(catalog_store)
    initial = (heap_store.insert(table, 1, (1, "before"), xmin=10, node_labels=("N",))
               if method != "insert_initial_reserved" else None)
    if method == "insert_reserved":
        assert heap_store.allocate_record_id(table) == 2
    changes = {"extra_node_labels": ("Y",)}
    if forgery == "different_name":
        changes["name"] = "other"
    if forgery == "unknown_table":
        changes["table_id"] = 999
    fake = replace(table, **changes)
    before = list(heap_store.scan_all(table)), heap_store.next_record_id(table), heap_store.pages_of(table)
    args = (fake, initial if method == "update" else 2, (2, "after"))
    kwargs = {"next_record_id": 3} if method == "insert_initial_reserved" else {}
    expected = (GrafxTransactionStateError if method == "insert_reserved" and forgery == "unknown_table"
                else GrafxConfigurationError)
    with pytest.raises(expected):
        getattr(heap_store, method)(*args, xmin=20, node_labels=("Y",), **kwargs)
    after = list(heap_store.scan_all(table)), heap_store.next_record_id(table), heap_store.pages_of(table)
    assert after == before


def test_stale_table_candidates_do_not_override_current_native_authority(catalog_store, heap_store):
    table = install(catalog_store)
    before_growth = table
    grown = catalog_store.catalog.copy()
    grown.extend_node_labels(table.table_id, ("Y",))
    catalog_store.adopt(grown)
    catalog_store.save()
    ref = heap_store.insert(before_growth, 1, (1, "body"), xmin=10, node_labels=("Y",))
    assert heap_store.read(ref).node_labels == ("Y",)
    assert list(heap_store.scan_projected(before_growth, SnapshotDouble(10), frozenset()))[0][1].node_labels == ("Y",)


def test_read_projection_cannot_supply_candidates_to_bless_corrupt_membership(catalog_store, heap_store, pool):
    table = install(catalog_store)
    ref = heap_store.insert(table, 1, (1, "body"), xmin=10, node_labels=("N", "X"))
    with pool.pinned(heap_store.file, ref.page) as page:
        raw = bytearray(page.read_slot(ref.slot))
        raw[RECORD_HEADER_SIZE + 11] = ord("Y")
        page.update_slot(ref.slot, bytes(raw))
    fake = replace(table, extra_node_labels=("X", "Y"))
    with pytest.raises(GrafxCorruptionDetected):
        list(heap_store.scan_projected(fake, SnapshotDouble(10), frozenset()))
    staged = catalog_store.catalog.copy()
    staged.extend_node_labels(table.table_id, ("Y",))
    with heap_store._node_label_write_scope(staged):
        with pytest.raises(GrafxTransactionStateError):
            with heap_store._node_label_write_scope(staged):
                pytest.fail("Nested materialization cannot replace an active scope")
        with pytest.raises(GrafxCorruptionDetected):
            list(heap_store.scan_projected(fake, SnapshotDouble(10), frozenset()))
    assert heap_store._node_label_write_catalog is None


def test_unknown_catalog_reader_refuses_before_replay_changes_any_page(monkeypatch):
    pool, store, images = staged(state(), 1000)
    before = store.read_from_pages()
    writes = list(pool.storage.write_calls)
    monkeypatch.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS", catalog_module._KNOWN_CAPABILITY_BITS & ~NODE_LABELS_CAPABILITY_BIT)
    with pytest.raises(GrafxSchemaVersionMismatch):
        CommitRedo(pool).apply(committed_replay(records(images, 1000)))
    assert store.read_from_pages() == before and pool.storage.write_calls == writes


def test_candidate_growth_requires_commit_and_replay_is_idempotent():
    initial = state()
    later = initial.copy()
    later.extend_node_labels(1, ("Z",))
    pool, store, first = staged(initial, 1000)
    _, _, second = staged(later, 2000)
    before = store.read_from_pages()
    CommitRedo(pool).apply(committed_replay(records(first, 1000)[:-1]))
    assert store.read_from_pages() == before
    replay = committed_replay((*records(first, 1000), *records(second, 2000)))
    CommitRedo(pool).apply(replay)
    assert store.read_from_pages() == later
    CommitRedo(pool).apply(replay)
    assert store.read_from_pages() == later


def test_committed_candidate_shrink_is_refused_before_any_page_effect():
    pool, store, first = staged(state(labels=("X", "Z")), 1000)
    _, _, second = staged(state(labels=("X",)), 2000)
    before = store.read_from_pages()
    writes = list(pool.storage.write_calls)
    with pytest.raises(GrafxRecoveryRefused, match="node-label candidates"):
        CommitRedo(pool).apply(committed_replay((*records(first, 1000), *records(second, 2000))))
    assert store.read_from_pages() == before and pool.storage.write_calls == writes


def test_capability_cannot_disappear_even_when_candidate_sets_are_unchanged():
    activated = state(labels=())  # Explicit empty per-row sets still need bit 28.
    downgraded = state(activate=False)
    assert activated.table("N").node_label_candidates == downgraded.table("N").node_label_candidates
    pool, store, first = staged(activated, 1000)
    _, _, second = staged(downgraded, 2000)
    before = store.read_from_pages()
    writes = list(pool.storage.write_calls)
    with pytest.raises(GrafxRecoveryRefused, match="node-label capability"):
        CommitRedo(pool).apply(committed_replay((*records(first, 1000), *records(second, 2000))))
    assert store.read_from_pages() == before and pool.storage.write_calls == writes


@pytest.mark.parametrize("labels", [(), ("N",), ("N", "X")])
@pytest.mark.parametrize("body", ["inline", "overflow-" * 300], ids=["inline", "overflow"])
@pytest.mark.parametrize("compress", [False, True])
def test_label_pages_replay_only_with_commit_and_survive_cold_reopen(catalog_store, heap_store, pool, labels, body, compress):
    """Real heap/catalog bytes with synthetic committed WAL, not a process-crash qualification."""
    table = install(catalog_store)
    ref = heap_store.insert(table, 1, (1, body), xmin=1000, node_labels=labels)
    pool.flush()
    images = []
    for file in (catalog_store.file, heap_store.file):
        for index in range(pool.storage.page_count(file)):
            page = Page.from_bytes(pool.storage.read_page(file, index))
            page.page_lsn = 1000
            page.seq = 2
            images.append((file, index, page.to_bytes()))
    log = []
    for i, (file, index, raw) in enumerate(images):
        encoded = encode_page_write_record(file, index, raw, compress=compress)
        log.append(WalRecord(WalRecordType.WRITE_PAGE, encoded.payload,
            lsn=1000-len(images)+i, epoch=1, txn_id=7,
            format_version=encoded.format_version, flags=encoded.flags))
    terminal = WalRecord(WalRecordType.COMMIT, CommitPayload.build(snapshot_lsn=0).encode(),
                         lsn=1000, epoch=1, txn_id=7)
    destination = make_pool(MemoryDevice(page_size=512), RecordingMetrics())
    redo = CommitRedo(destination)
    redo.apply(committed_replay(log))
    assert destination.storage.list_files() == ()
    replay = committed_replay((*log, terminal))
    redo.flush(redo.apply(replay))
    destination.invalidate()
    reopened_catalog = CatalogStore(destination)
    reopened_catalog.load()
    reopened_heap = HeapStore(destination, reopened_catalog)
    assert reopened_heap.read(ref).node_labels == labels
    assert reopened_heap.read(ref).values == (1, body)
    assert list(reopened_heap.scan(table, SnapshotDouble(999))) == []
    assert len(list(reopened_heap.scan(table, SnapshotDouble(1000)))) == 1
    redo.flush(redo.apply(replay))
    destination.invalidate()
    assert reopened_heap.read(ref).node_labels == labels
    assert len(list(reopened_heap.scan_all(table))) == 1


@pytest.mark.parametrize("method", ["insert", "insert_reserved", "insert_initial_reserved"])
@pytest.mark.parametrize("labels", [(), ("N",), ("N", "X")])
@pytest.mark.parametrize("body", ["short", "overflow-" * 300], ids=["inline", "overflow"])
def test_all_heap_insert_doors_preserve_metadata_inline_and_overflow(catalog_store, heap_store, pool, method, labels, body):
    table = install(catalog_store)
    kwargs = {"next_record_id": 2} if method == "insert_initial_reserved" else {}
    if method == "insert_reserved":
        assert heap_store.allocate_record_id(table) == 1
    ref = getattr(heap_store, method)(table, 1, (1, body), xmin=10, node_labels=labels, **kwargs)
    row = heap_store.read(ref)
    assert row.record_id == 1 and row.table_id == table.table_id and row.values == (1, body)
    assert row.node_labels == labels
    assert row.stored_payload_bytes == len(encode_node_labels(labels)) + len(encode_tuple(table, (1, body)))
    assert heap_store.read_landing(ref).node_labels == labels
    projected = list(heap_store.scan_projected(table, SnapshotDouble(10), frozenset({0})))
    assert projected[0][1].node_labels == labels and projected[0][1].values[0] == 1
    pool.flush()
    pool.invalidate()
    reopened_catalog = CatalogStore(pool)
    reopened_catalog.load()
    reopened = HeapStore(pool, reopened_catalog)
    assert reopened.read(ref).node_labels == labels
    assert reopened.read(ref).values == (1, body)


def test_label_and_property_updates_keep_identity_and_earlier_snapshots(catalog_store, heap_store, pool):
    table = install(catalog_store)
    first = heap_store.insert(table, 1, (1, "before"), xmin=10)
    assert heap_store.read(first).node_labels is None  # Historical implicit singleton N.
    second = heap_store.update(table, first, (1, "before"), xmin=20, node_labels=("N", "X"))
    third = heap_store.update(table, second, (1, "after"), xmin=30)  # Property-only write preserves labels.
    fourth = heap_store.update(table, third, (1, "after"), xmin=40, node_labels=())
    for lsn, expected_labels, expected_body in [(10, None, "before"), (20, ("N", "X"), "before"),
                                               (30, ("N", "X"), "after"), (40, (), "after")]:
        rows = list(heap_store.scan(table, SnapshotDouble(lsn)))
        assert len(rows) == 1
        row = rows[0][1]
        assert row.record_id == 1 and row.table_id == table.table_id
        assert row.node_labels == expected_labels and row.values == (1, expected_body)
    assert heap_store.read(second).prev == first
    assert heap_store.read(third).prev == second
    assert heap_store.read(fourth).prev == third
    heap_store.delete(table, fourth, xmax=50)
    assert list(heap_store.scan(table, SnapshotDouble(50))) == []
    assert list(heap_store.scan(table, SnapshotDouble(40)))[0][1].node_labels == ()
    pool.flush()
    pool.invalidate()
    assert heap_store.read(fourth).node_labels == ()


def test_nullable_append_keeps_label_metadata_and_validates_old_schema(catalog_store, heap_store):
    table = install(catalog_store)
    ref = heap_store.insert(table, 1, (1, "body"), xmin=10, node_labels=("N", "X"))
    catalog_store.catalog.add_nullable_column("N", ColumnDef("later", ValueType.STRING))
    catalog_store.save()
    row = heap_store.read(ref)
    assert row.values == (1, "body", None) and row.node_labels == ("N", "X")


@pytest.mark.parametrize("labels,activate", [((), False), (("Y",), True), (("X", "N"), True), (("N", "N"), True)])
def test_refused_metadata_does_not_allocate_an_extent_or_identity(catalog_store, heap_store, labels, activate):
    table = install(catalog_store, activate=activate)
    assert heap_store.next_record_id(table) == 1
    with pytest.raises(GrafxConfigurationError):
        heap_store.insert(table, 1, (1, "body"), xmin=10, node_labels=labels)
    assert heap_store.next_record_id(table) == 1 and heap_store.pages_of(table) == ()


@pytest.mark.parametrize("mutation", ["magic", "unknown_label", "duplicate", "missing_capability"])
def test_corrupt_metadata_is_refused_even_when_no_properties_are_projected(catalog_store, heap_store, pool, mutation):
    table = install(catalog_store)
    ref = heap_store.insert(table, 1, (1, "body"), xmin=10, node_labels=("N", "X"))
    if mutation == "missing_capability":
        catalog_store.adopt(state(activate=False))
    else:
        with pool.pinned(heap_store.file, ref.page) as page:
            raw = bytearray(page.read_slot(ref.slot))
            header = RecordHeader.decode(raw)
            assert header.flags & RECORD_FLAG_NODE_LABELS
            if mutation == "magic":
                raw[RECORD_HEADER_SIZE] = ord("?")
            else:
                raw[RECORD_HEADER_SIZE + 11] = ord("Y" if mutation == "unknown_label" else "N")
            page.update_slot(ref.slot, bytes(raw))
    with pytest.raises(GrafxCorruptionDetected):
        list(heap_store.scan_projected(table, SnapshotDouble(10), frozenset()))
    with pytest.raises(GrafxCorruptionDetected):
        heap_store.read_landing(ref)
