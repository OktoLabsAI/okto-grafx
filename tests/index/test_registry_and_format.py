"""The registry, the definition, the change format and the metrics (FR-12, BR-11, FR-14, G7).

Nothing here is about visibility. It is the surface around it: which index answers to which name,
what a definition binds a file to, what a log record carries, and which of the frozen metrics this
component is allowed to emit.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index import (
    COLUMN_KEY_DERIVATION,
    DEFAULT_BUCKET_COUNT,
    MAX_BUCKET_COUNT,
    MIN_BUCKET_COUNT,
    RECORD_ID_KEY_DERIVATION,
    RECORD_ID_KEY_FORMAT_VERSION,
    IndexChange,
    IndexDefinition,
    IndexOperation,
    IndexVisibility,
    bucket_of,
    change_of,
    index_file,
    index_key,
    record_id_key,
    validate_bucket_count,
    wal_record_for,
)
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.wal.record import WalRecordType
from okto_grafx.engine.index_manager import (
    RECONCILIATION_TOTAL,
    TOMBSTONE_BACKLOG,
    HashIndex,
    IndexManager,
    IndexStore,
    ProximityIndex,
)

from .conftest import (
    TEST_BUCKET_COUNT,
    Database,
    MemoryDevice,
    RecordingMetrics,
    SnapshotDouble,
    TransactionDouble,
    build_database,
    damage_index_header,
    exact_definition,
    make_pool,
    proximity_definition,
)

BORN: int = 10
ENDED: int = 20


# --- the registry ------------------------------------------------------------------------------


def test_two_indexes_whose_names_differ_only_in_case_are_one_file(
    database: Database,
) -> None:
    """A name becomes a file name, and one platform folds case while the other does not."""
    twin = IndexDefinition.on(
        database.table,
        name="Person_By_Name",
        columns=("name",),
        visibility=IndexVisibility.EXACT,
        bucket_count=TEST_BUCKET_COUNT,
    )

    with pytest.raises(GrafxIndexError) as refused:
        database.manager.register(HashIndex(twin, database.pool, database.metrics))

    assert refused.value.details["field"] == "name"
    assert refused.value.details["index"] == "person_by_name"


def test_an_unknown_index_name_is_refused_with_the_names_that_exist(
    database: Database,
) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        database.manager.index("nothing_here")

    assert refused.value.details["field"] == "name"
    assert "person_by_name" in refused.value.message


def test_the_registry_answers_by_table(database: Database) -> None:
    assert {
        index.name for index in database.manager.indexes_for(database.table.table_id)
    } == {
        "person_by_name",
        "person_near_name",
    }
    assert database.manager.indexes_for(9999) == ()


def test_the_registry_lists_indexes_in_a_fixed_order(database: Database) -> None:
    """A report that changed order between runs would be unusable as evidence."""
    assert [index.name for index in database.manager.indexes()] == [
        "person_by_name",
        "person_near_name",
    ]


def test_registering_something_that_is_not_an_index_is_refused(
    database: Database,
) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        database.manager.register(object())  # type: ignore[arg-type]

    assert refused.value.details["field"] == "index"


def test_register_existing_only_never_repairs_an_incomplete_file(
    database: Database,
) -> None:
    """Inspection mode must not turn a zero-length/torn index into a created one."""
    candidate = HashIndex(
        exact_definition(database.table, name="incomplete"),
        database.pool,
        database.metrics,
    )
    database.device.create(candidate.file)

    with pytest.raises(GrafxIndexError) as refused:
        database.manager.register(candidate, existing_only=True, persist_stale=False)

    assert refused.value.details["field"] == "file"
    assert database.device.page_count(candidate.file) == 0


def test_register_reuses_a_proved_directory_entry_without_rechecking_exists(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = HashIndex(
        exact_definition(database.table, name="proved_present"),
        database.pool,
        database.metrics,
    )
    candidate.create()

    def redundant_exists(file: str) -> bool:
        raise AssertionError(f"rechecked the proved directory entry {file!r}")

    monkeypatch.setattr(database.device, "exists", redundant_exists)

    assert (
        database.manager.register(
            candidate,
            existing_only=True,
            persist_stale=False,
            proved_present=True,
        )
        is candidate
    )


def test_registering_a_store_that_does_not_answer_the_contract_is_refused(
    database: Database, person_table: TableDef
) -> None:
    """``IndexStore`` is the shared store and deliberately not an index: it has no lookup."""
    bare = IndexStore(
        exact_definition(database.table, name="bare_store"),
        database.pool,
        database.metrics,
    )

    with pytest.raises(GrafxIndexError) as refused:
        database.manager.register(bare)

    assert refused.value.details["field"] == "contract"


def test_an_update_stages_both_halves_on_every_index(database: Database) -> None:
    """The old entry has to end and a new one has to exist, whatever the key did."""
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(
        txn, database.table.table_id, ref, (1, "Ada"), BORN
    )
    database.manager.commit(txn, BORN)
    new_ref = database.heap.update(database.table, ref, (1, "Ada"), ENDED)

    update = TransactionDouble(txn_id=4)
    records = database.manager.stage_row_update(
        update, database.table.table_id, ref, (1, "Ada"), new_ref, (1, "Ada"), ENDED
    )
    database.manager.commit(update, ENDED)

    assert [change_of(record).operation for record in records] == [
        IndexOperation.TOMBSTONE,
        IndexOperation.INSERT,
        IndexOperation.TOMBSTONE,
        IndexOperation.INSERT,
    ]
    key = database.key(1, "Ada")
    assert database.manager.lookup("person_by_name", key, SnapshotDouble(ENDED)) == (
        new_ref,
    )
    assert database.manager.lookup("person_near_name", key, SnapshotDouble(ENDED)) == (
        new_ref,
    )
    assert database.manager.lookup("person_near_name", key, SnapshotDouble(BORN)) == (
        ref,
    )


def test_an_exact_candidate_pointing_at_another_table_is_damage_and_not_a_miss(
    database: Database,
) -> None:
    """Dropping it silently would make an index that lost its file look merely empty."""
    from okto_grafx.domain.model.schema import ColumnDef
    from okto_grafx.domain.model.value import ValueType

    other = TableDef(
        table_id=database.catalog.catalog.next_table_id(),
        name="Company",
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
        primary_key="id",
    )
    database.catalog.catalog.add_table(other)
    database.catalog.save()
    foreign = database.heap.insert(other, 1, (1,), BORN)
    txn = TransactionDouble(txn_id=5)
    key = database.key(1, "Ada")
    database.exact.stage_insert(txn, key, foreign, 0)
    database.exact.commit(txn, BORN)

    with pytest.raises(GrafxCorruptionDetected) as refused:
        database.manager.lookup("person_by_name", key, SnapshotDouble(BORN))

    assert refused.value.details["field"] == "table_id"


# --- the definition ------------------------------------------------------------------------------


def test_a_definition_names_the_columns_a_table_really_has(
    person_table: TableDef,
) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        IndexDefinition.on(
            person_table,
            name="by_nothing",
            columns=("nothing",),
            visibility=IndexVisibility.EXACT,
        )

    assert refused.value.details["field"] == "columns"
    assert "'name'" in refused.value.message


@pytest.mark.parametrize(
    "changed",
    [
        {"name": "other_name"},
        {"table_id": 77},
        {"positions": (0,)},
        {"visibility": IndexVisibility.PROXIMITY},
        {"bucket_count": TEST_BUCKET_COUNT + 1},
    ],
)
def test_every_field_that_changes_what_a_file_means_changes_the_digest(
    person_table: TableDef, changed: dict[str, object]
) -> None:
    """A field left out of the digest is a file that can be opened under the wrong question."""
    base = exact_definition(person_table)
    fields = {
        "name": base.name,
        "table_id": base.table_id,
        "table_name": base.table_name,
        "positions": base.positions,
        "visibility": base.visibility,
        "bucket_count": base.bucket_count,
    }
    fields.update(changed)

    assert IndexDefinition(**fields).digest() != base.digest()  # type: ignore[arg-type]


def test_a_definition_names_its_file_the_way_the_contract_does(
    person_table: TableDef,
) -> None:
    assert exact_definition(person_table).file == "index/person_by_name.idx"
    assert index_file("thing") == "index/thing.idx"


def test_an_index_name_that_could_not_be_a_file_is_refused() -> None:
    for candidate in ("", "with space", "with/slash", "1leading", "with.dot"):
        with pytest.raises(GrafxIndexError) as refused:
            index_file(candidate)
        assert refused.value.details["field"] == "name"


def test_the_bucket_bounds_are_the_declared_ones() -> None:
    """Amendment A68: assert the literal, so the expectation cannot slide with the constant."""
    assert MIN_BUCKET_COUNT == 1
    assert MAX_BUCKET_COUNT == 4096
    assert DEFAULT_BUCKET_COUNT == 64
    for outside in (0, MAX_BUCKET_COUNT + 1, -1, True, 1.5):
        with pytest.raises(GrafxIndexError):
            validate_bucket_count(outside)


def test_a_key_is_the_encoding_of_the_columns_in_the_order_they_were_named(
    person_table: TableDef,
) -> None:
    forward = IndexDefinition.on(
        person_table,
        name="by_both",
        columns=("id", "name"),
        visibility=IndexVisibility.EXACT,
    )
    backward = IndexDefinition.on(
        person_table,
        name="by_both_reversed",
        columns=("name", "id"),
        visibility=IndexVisibility.EXACT,
    )

    # Every named column has to be IN the key, not only the first one: two rows that agree on
    # the first column and differ on the second must not share a key, or a two-column index
    # answers with rows it was never asked about. Asserting only that the two orders differ is
    # satisfied by a key built from one column, whichever one it is (amendment A71).
    assert forward.key_for((1, "Ada")) != forward.key_for((1, "Grace"))
    assert forward.key_for((1, "Ada")) != forward.key_for((2, "Ada"))
    assert forward.key_for((1, "Ada")) != backward.key_for((1, "Ada"))
    assert forward.key_for((1, "Ada")) == index_key((1, "Ada"), (0, 1))


def test_a_key_position_outside_the_row_is_refused() -> None:
    with pytest.raises(GrafxIndexError) as refused:
        index_key((1,), (5,))

    assert refused.value.details["field"] == "positions"


def test_identity_key_covers_the_full_usable_unsigned_domain() -> None:
    """RecordId is u64 even though the public scalar INT64 codec is signed."""

    assert record_id_key(1) == struct.pack("<BQ", RECORD_ID_KEY_FORMAT_VERSION, 1)
    assert record_id_key(1 << 63) == struct.pack(
        "<BQ", RECORD_ID_KEY_FORMAT_VERSION, 1 << 63
    )
    assert record_id_key((1 << 64) - 2) == struct.pack(
        "<BQ", RECORD_ID_KEY_FORMAT_VERSION, (1 << 64) - 2
    )


@pytest.mark.parametrize("record_id", [True, 1.0, 0, -1, (1 << 64) - 1, 1 << 64])
def test_identity_key_refuses_non_record_ids(record_id: object) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        record_id_key(record_id)

    assert refused.value.details["field"] == "record_id"


def test_identity_definition_derives_from_record_id_not_values(
    person_table: TableDef,
) -> None:
    identity = IndexDefinition(
        name="id_Person",
        table_id=person_table.table_id,
        table_name=person_table.name,
        positions=(),
        visibility=IndexVisibility.EXACT,
        bucket_count=TEST_BUCKET_COUNT,
        key_derivation=RECORD_ID_KEY_DERIVATION,
    )

    assert identity.key_for_record(1 << 63, (1, "Ada")) == record_id_key(1 << 63)
    assert identity.entry_key_for_record(7, (2, "Grace")) == record_id_key(7)
    assert identity.owes_entry_for_record(7, ()) is True
    with pytest.raises(GrafxIndexError) as refused:
        identity.key_for((1, "Ada"))
    assert refused.value.details["field"] == "key_derivation"


def test_identity_definition_refuses_column_positions(person_table: TableDef) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        IndexDefinition(
            name="id_Person",
            table_id=person_table.table_id,
            table_name=person_table.name,
            positions=(0,),
            visibility=IndexVisibility.EXACT,
            key_derivation=RECORD_ID_KEY_DERIVATION,
        )

    assert refused.value.details["field"] == "positions"


def test_a_visibility_class_is_parsed_from_the_word_the_contract_uses() -> None:
    assert IndexVisibility.parse("exact") is IndexVisibility.EXACT
    assert IndexVisibility.parse("proximity") is IndexVisibility.PROXIMITY
    with pytest.raises(GrafxIndexError) as refused:
        IndexVisibility.parse("approximate")
    assert refused.value.details["field"] == "visibility"


# --- the change format ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "record_type", "csn", "versioned"),
    [
        (IndexOperation.INSERT, WalRecordType.INDEX_WRITE, 0, False),
        (IndexOperation.INSERT, WalRecordType.INDEX_WRITE, 7, True),
        (IndexOperation.TOMBSTONE, WalRecordType.INDEX_WRITE, 9, True),
        (IndexOperation.REMOVE, WalRecordType.INDEX_RECONCILE, 11, True),
    ],
)
def test_a_change_survives_the_log_and_keeps_its_record_type(
    operation: IndexOperation, record_type: WalRecordType, csn: int, versioned: bool
) -> None:
    change = IndexChange(
        index="by_name",
        operation=operation,
        key=b"key",
        ref=RecordRef(3, 4),
        csn=csn,
        versioned=versioned,
    )

    record = wal_record_for(change, epoch=2, txn_id=5)

    assert record.record_type == int(record_type)
    assert change_of(record) == change


def test_replaying_a_reconciliation_restores_its_horizon(database: Database) -> None:
    """INDEX_RECONCILE carries the proof that makes an absent old entry legitimate.

    The removal itself is idempotent and may already be absent. Replay must still restore the
    horizon from the logical record; otherwise verification reports correctly reclaimed entries
    as missing after a crash.
    """
    record = wal_record_for(
        IndexChange(
            index=database.proximity.name,
            operation=IndexOperation.REMOVE,
            key=b"already-absent",
            ref=RecordRef(3, 4),
            csn=ENDED,
            versioned=True,
        ),
        epoch=2,
        txn_id=5,
    ).with_lsn(30)

    assert database.manager.apply(record) is True
    assert database.proximity.reconciled_through_lsn == ENDED
    assert database.proximity.built_through_lsn == 30


def test_a_reset_carries_no_key_and_declares_a_position() -> None:
    change = IndexChange(
        index="by_name",
        operation=IndexOperation.RESET,
        ref=RecordRef(18, 0),
        csn=42,
    )

    assert change_of(wal_record_for(change)) == change
    with pytest.raises(GrafxIndexError) as refused:
        IndexChange(index="by_name", operation=IndexOperation.RESET, key=b"k", csn=42)
    assert refused.value.details["field"] == "key"
    for invalid in (RecordRef(17, 0), RecordRef(18, 1)):
        with pytest.raises(GrafxIndexError) as token_refused:
            IndexChange(
                index="by_name",
                operation=IndexOperation.RESET,
                ref=invalid,
                csn=42,
            )
        assert token_refused.value.details["field"] == "rebuild_token"


def test_a_legacy_reset_with_a_zero_token_remains_decodable() -> None:
    legacy = IndexChange(index="by_name", operation=IndexOperation.RESET, csn=42)

    assert change_of(wal_record_for(legacy)) == legacy
    assert legacy.ref == RecordRef(0, 0)


def test_a_payload_that_decodes_into_an_illegal_change_is_damage() -> None:
    """Bytes off a device are damage, never a caller mistake (amendment A11-revised)."""
    change = IndexChange(
        index="by_name",
        operation=IndexOperation.TOMBSTONE,
        key=b"k",
        ref=RecordRef(3, 4),
        csn=9,
        versioned=True,
    )
    payload = bytearray(change.encode())
    payload[12:20] = bytes(8)

    with pytest.raises(GrafxCorruptionDetected) as refused:
        IndexChange.decode(bytes(payload))

    assert refused.value.details["operation"] == "TOMBSTONE"
    assert refused.value.details["field"] == "csn"


def test_a_damaged_visibility_byte_is_refused_even_though_the_digest_matches(
    database: Database,
) -> None:
    """The visibility check has to stand on its own, and only damage can make it.

    Every ordinary route to a visibility disagreement carries a definition whose DIGEST also
    disagrees, so ``open()`` refuses on the digest and this check is never reached -- masked, and
    invisible in a green run. The one input that reaches it is a file whose stored visibility
    byte changed while the digest bytes beside it did not, which is damage rather than a caller
    mistake, and it must not be treated as a different index: an exact file read as a proximity
    one would have every entry decoded against a stamp it does not carry.
    """
    database.pool.flush(database.exact.file)
    damaged = damage_index_header(database.device, database.exact.file, 2, 2)

    assert damaged.visibility is IndexVisibility.PROXIMITY
    assert damaged.digest == database.exact.definition.digest(), (
        "the digest must still match, or the digest check would be what refuses"
    )
    cold = HashIndex(
        exact_definition(database.table),
        make_pool(database.device, RecordingMetrics()),
        RecordingMetrics(),
    )

    with pytest.raises(GrafxIndexError) as refused:
        cold.open()

    assert refused.value.details["field"] == "visibility"
    assert refused.value.details["file"] == database.exact.file


def test_an_unversioned_insert_carrying_a_birth_stamp_is_damage() -> None:
    """The other half of the stamp rule, and the half no honest encoder can produce.

    An exact entry has no birth stamp by construction, so a payload whose flags say unversioned
    while its csn says a commit created it is describing an entry that cannot exist. Accepting it
    would silently DROP the number -- the constructor stores ``NO_CSN`` for an unversioned
    entry -- and a stamp dropped in silence is a record whose meaning changed between the log and
    the index, which is the one thing the redo path may never do.
    """
    change = IndexChange(
        index="by_name",
        operation=IndexOperation.INSERT,
        key=b"k",
        ref=RecordRef(3, 4),
        csn=0,
        versioned=False,
    )
    payload = bytearray(change.encode())
    payload[12:20] = (7).to_bytes(8, "little")

    with pytest.raises(GrafxCorruptionDetected) as refused:
        IndexChange.decode(bytes(payload))

    assert refused.value.details["field"] == "csn"
    assert refused.value.details["operation"] == "INSERT"


def test_a_change_of_an_unknown_operation_is_damage() -> None:
    change = IndexChange(index="by_name", operation=IndexOperation.RESET, csn=1)
    payload = bytearray(change.encode())
    payload[2] = 99

    with pytest.raises(GrafxCorruptionDetected) as refused:
        IndexChange.decode(bytes(payload))

    assert refused.value.details["field"] == "operation"


def test_a_change_from_a_later_format_is_refused() -> None:
    change = IndexChange(index="by_name", operation=IndexOperation.RESET, csn=1)
    payload = bytearray(change.encode())
    payload[0] = 9

    with pytest.raises(GrafxCorruptionDetected) as refused:
        IndexChange.decode(bytes(payload))

    assert refused.value.details["field"] == "format_version"


def test_a_removal_may_not_travel_under_the_write_record_type() -> None:
    """The record type is the only thing telling a replay that a cleanup was logged."""
    from okto_grafx.domain.wal.record import WalRecord

    change = IndexChange(
        index="by_name",
        operation=IndexOperation.REMOVE,
        key=b"k",
        ref=RecordRef(1, 1),
        csn=3,
    )
    disguised = WalRecord(
        record_type=int(WalRecordType.INDEX_WRITE), payload=change.encode(), lsn=4
    )

    with pytest.raises(GrafxCorruptionDetected) as refused:
        change_of(disguised)

    assert refused.value.details["operation"] == "REMOVE"


# --- metrics -----------------------------------------------------------------------------------


def test_the_metrics_this_component_emits_are_the_frozen_ones() -> None:
    """G7: a metric name is a contract, and this component invents none."""
    assert TOMBSTONE_BACKLOG == "oktografx_vector_tombstone_backlog"
    assert RECONCILIATION_TOTAL == "oktografx_vector_reconciliation_total"


def test_a_proximity_index_reports_its_backlog_and_its_passes(
    database: Database,
) -> None:
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(
        txn, database.table.table_id, ref, (1, "Ada"), BORN
    )
    database.manager.stage_row_delete(
        txn, database.table.table_id, ref, (1, "Ada"), ENDED
    )
    database.manager.commit(txn, ENDED)

    assert database.metrics.values_of(TOMBSTONE_BACKLOG)[-1] == 1.0

    database.manager.note_reconciled(ENDED)

    assert database.metrics.values_of(RECONCILIATION_TOTAL) == [1.0]


def test_a_seeded_backlog_tracks_changes_without_another_metric_walk(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gauge walks once, then uses proved deltas until its paged view is rebased."""
    walks: dict[int, int] = {}
    original_walk = IndexStore.walk

    def counted_walk(index: IndexStore):
        walks[id(index)] = walks.get(id(index), 0) + 1
        return original_walk(index)

    monkeypatch.setattr(IndexStore, "walk", counted_walk)
    index = database.proximity
    ref = RecordRef(1, 1)

    inserting = TransactionDouble(txn_id=101)
    index.stage_insert(inserting, b"ada", ref, BORN)
    index.commit(inserting, BORN)
    assert walks[id(index)] == 1

    ending = TransactionDouble(txn_id=102)
    index.stage_delete(ending, b"ada", ref, ENDED)
    index.commit(ending, ENDED)
    repeated = TransactionDouble(txn_id=103)
    index.stage_delete(repeated, b"ada", ref, ENDED + 1)
    index.commit(repeated, ENDED + 1)

    assert walks[id(index)] == 1
    assert database.metrics.values_of(TOMBSTONE_BACKLOG)[-2:] == [1.0, 1.0]

    # A foreign-generation rebase invalidates only the derived process-local count. The next
    # gauge pays one exact walk, then becomes incremental again.
    index._cache_rebased()  # noqa: SLF001 - discriminates the foreign-adoption invalidator
    index.note_reconciled(ENDED + 1)
    index.note_reconciled(ENDED + 1)
    assert walks[id(index)] == 2

    sweeping = TransactionDouble(txn_id=104)
    index.reconcile(ENDED + 1, sweeping)
    semantic_walks = walks[id(index)]
    index.commit(sweeping, ENDED + 2)
    index.note_reconciled(ENDED + 1)

    assert walks[id(index)] == semantic_walks
    assert database.metrics.values_of(TOMBSTONE_BACKLOG)[-1] == 0.0


def test_reset_and_reopen_reaffirm_the_backlog_per_store(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RESET proves zero; a different handle seeds its own count instead of sharing it."""
    walks: dict[int, int] = {}
    original_walk = IndexStore.walk

    def counted_walk(index: IndexStore):
        walks[id(index)] = walks.get(id(index), 0) + 1
        return original_walk(index)

    monkeypatch.setattr(IndexStore, "walk", counted_walk)
    index = database.proximity
    ref = RecordRef(2, 1)
    changing = TransactionDouble(txn_id=111)
    index.stage_insert(changing, b"grace", ref, BORN)
    index.stage_delete(changing, b"grace", ref, ENDED)
    index.commit(changing, ENDED)
    assert walks[id(index)] == 1

    index.mark_stale("metric reset proof")
    rebuilding = TransactionDouble(txn_id=112)
    index.stage_reset(rebuilding, index.built_through_lsn)
    index.commit(rebuilding, ENDED + 1)
    assert walks[id(index)] == 1
    assert index._tombstone_backlog_count == 0  # noqa: SLF001 - derived-state invariant

    isolated_metrics = RecordingMetrics()
    isolated = ProximityIndex(
        proximity_definition(database.table, name="person_near_alias"),
        database.pool,
        isolated_metrics,
    )
    isolated.create()
    isolated.note_reconciled(ENDED + 1)
    changing_again = TransactionDouble(txn_id=113)
    index.stage_insert(changing_again, b"hopper", RecordRef(3, 1), ENDED + 2)
    index.stage_delete(changing_again, b"hopper", RecordRef(3, 1), ENDED + 3)
    index.commit(changing_again, ENDED + 3)

    assert isolated._tombstone_backlog_count == 0  # noqa: SLF001 - separate file, separate count
    assert index._tombstone_backlog_count == 1  # noqa: SLF001 - separate file, separate count

    reopened_metrics = RecordingMetrics()
    reopened_pool = make_pool(database.device, reopened_metrics)
    reopened = ProximityIndex(index.definition, reopened_pool, reopened_metrics)
    reopened.open()
    reopened.note_reconciled(ENDED + 3)
    reopened.note_reconciled(ENDED + 3)

    assert walks[id(reopened)] == 1
    assert reopened._tombstone_backlog_count == 1  # noqa: SLF001 - reopened store seeds itself
    assert index._tombstone_backlog_count == 1  # noqa: SLF001 - stores stay isolated


@pytest.mark.parametrize(
    ("operation", "method"),
    (
        (IndexOperation.TOMBSTONE, "_rewrite"),
        (IndexOperation.REMOVE, "_erase"),
        (IndexOperation.RESET, "_reset"),
    ),
)
def test_an_interrupted_backlog_transition_becomes_unknown(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    operation: IndexOperation,
    method: str,
) -> None:
    """A possibly part-applied rewrite, erase, or reset is never reflected by a guessed count."""
    index = database.proximity
    ref = RecordRef(4, 1)
    seeding = TransactionDouble(txn_id=121)
    index.stage_insert(seeding, b"lovelace", ref, BORN)
    if operation is IndexOperation.REMOVE:
        index.stage_delete(seeding, b"lovelace", ref, ENDED)
    index.commit(seeding, ENDED)
    assert index._tombstone_backlog_count is not None  # noqa: SLF001 - precondition

    change = IndexChange(
        index=index.name,
        operation=operation,
        key=b"" if operation is IndexOperation.RESET else b"lovelace",
        ref=RecordRef(0, 0) if operation is IndexOperation.RESET else ref,
        csn=ENDED,
        versioned=True,
    )
    if operation is IndexOperation.RESET:
        index.mark_stale("interrupted reset")

    def interrupted(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("completed then interrupted")

    monkeypatch.setattr(IndexStore, method, interrupted)
    with pytest.raises(RuntimeError, match="completed then interrupted"):
        index._apply_change(change, ENDED)  # noqa: SLF001 - exact transition under test

    assert index._tombstone_backlog_count is None  # noqa: SLF001 - fail-closed metric state


def test_an_exact_index_reports_no_vector_metric(
    person_table: TableDef, pool: object, device: MemoryDevice
) -> None:
    """The two names describe the proximity path, and an exact index is not on it."""
    metrics = RecordingMetrics()
    index = HashIndex(exact_definition(person_table), pool, metrics)  # type: ignore[arg-type]
    index.create()
    index.note_reconciled(5)

    assert metrics.values_of(TOMBSTONE_BACKLOG) == []
    assert metrics.values_of(RECONCILIATION_TOTAL) == []


def test_a_disabled_sink_is_never_asked_to_record_anything(
    person_table: TableDef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DoD item 6: the hot path pays nothing when metrics are off."""
    device = MemoryDevice()
    metrics = RecordingMetrics(enabled=False)
    pool = make_pool(device, metrics)
    index = ProximityIndex(proximity_definition(person_table), pool, metrics)
    index.create()
    monkeypatch.setattr(
        IndexStore,
        "walk",
        lambda _index: pytest.fail("disabled metrics must not seed the backlog"),
    )
    txn = TransactionDouble(txn_id=1)
    index.stage_insert(txn, b"k", RecordRef(1, 1), BORN)
    index.commit(txn, BORN)
    index.note_reconciled(BORN)

    assert metrics.calls == []
    assert metrics.registered == {}


def test_the_metrics_are_registered_before_they_are_emitted(database: Database) -> None:
    assert set(database.metrics.registered) >= {TOMBSTONE_BACKLOG, RECONCILIATION_TOTAL}


# --- the key derivation ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DigestDefinition(IndexDefinition):
    """A definition whose key is a fixed-size digest of the value, not the value itself.

    This is the shape a vector index needs and the reason the seam exists. Keeping it in the test
    suite rather than in the framework is deliberate: C7 owns the SEAM, and the component whose
    values are large owns the derivation.
    """

    def key_for(self, values: Sequence[object]) -> bytes:
        """Return a digest of the named column, whatever its size."""
        material = repr(values[self.positions[0]]).encode("utf-8")
        return hashlib.blake2b(material, digest_size=16).digest()


def _digest_definition(
    table: TableDef, *, name: str = "person_by_digest"
) -> DigestDefinition:
    """Return a digest-keyed exact index over the name column of the table."""
    return DigestDefinition(
        name=name,
        table_id=table.table_id,
        table_name=table.name,
        positions=(1,),
        visibility=IndexVisibility.EXACT,
        bucket_count=TEST_BUCKET_COUNT,
        key_derivation="value_digest_v1",
    )


def test_the_column_key_derivation_is_the_declared_one() -> None:
    """Amendment A68: the literal, because a later change to it invalidates every stored index."""
    assert COLUMN_KEY_DERIVATION == "columns"
    assert (
        IndexDefinition(
            name="i",
            table_id=1,
            table_name="T",
            positions=(0,),
            visibility=IndexVisibility.EXACT,
        ).key_derivation
        == "columns"
    )


def test_the_key_derivation_travels_in_the_definition_digest(
    person_table: TableDef,
) -> None:
    """Two derivations over the same columns produce different bytes for the same row.

    A file written under one and opened under the other would look up keys that were never
    written and answer with nothing, while every structural check passed. The digest is what
    makes that impossible rather than merely unlikely.
    """
    columns = exact_definition(person_table, name="person_by_digest", columns=("name",))
    digested = _digest_definition(person_table)

    assert columns.digest() != digested.digest()


def test_a_definition_that_declares_a_derivation_it_does_not_implement_is_refused(
    person_table: TableDef,
) -> None:
    """Silently handing it column keys would build the index under a rule it never claimed."""
    declared = IndexDefinition(
        name="person_by_digest",
        table_id=person_table.table_id,
        table_name=person_table.name,
        positions=(1,),
        visibility=IndexVisibility.EXACT,
        key_derivation="value_digest_v1",
    )

    with pytest.raises(GrafxIndexError) as refused:
        declared.key_for((1, "Ada"))

    assert refused.value.details["field"] == "key_derivation"
    assert refused.value.details["value"] == "value_digest_v1"


def test_a_key_derivation_must_be_nameable(person_table: TableDef) -> None:
    for candidate in ("", "with space", "with/slash", 7):
        with pytest.raises(GrafxIndexError) as refused:
            IndexDefinition(
                name="i",
                table_id=1,
                table_name="T",
                positions=(0,),
                visibility=IndexVisibility.EXACT,
                key_derivation=candidate,  # type: ignore[arg-type]
            )
        assert refused.value.details["field"] == "key_derivation"


def test_a_declared_derivation_keeps_the_key_the_same_size_at_any_dimension(
    person_table: TableDef,
) -> None:
    """The reason the seam exists: an entry is never split, so a key-sized value caps dimension.

    At the 512-byte minimum page size the key budget is 449 bytes, which refuses ``DOUBLE[384]``
    outright and makes ``MAX_VECTOR_DIMENSION`` unreachable at every page size. A declared
    derivation decouples the two: the key stays the same size whether the value is 384 floats or
    16384.
    """
    definition = _digest_definition(person_table)
    small = definition.key_for((1, tuple(float(n) for n in range(384))))
    large = definition.key_for((1, tuple(float(n) for n in range(16384))))

    assert len(small) == len(large) == 16


def test_a_declared_derivation_still_detects_a_value_that_drifted(
    person_table: TableDef,
) -> None:
    """The free drift detector is preserved exactly: a changed value digests differently."""
    definition = _digest_definition(person_table)
    original = tuple(float(n) for n in range(384))
    changed = original[:-1] + (999.0,)

    assert definition.key_for((1, original)) != definition.key_for((1, changed))


def test_verification_re_derives_a_key_with_the_rule_the_definition_declares(
    database: Database,
) -> None:
    """The end of the seam that matters: verify asks the definition, it does not assume.

    A verifier that re-derived with the column rule would compare a digest against an encoded
    value, disagree on every entry, and report a clean index as wholly diverged -- for precisely
    the index kind whose results a caller cannot eyeball.
    """
    index = HashIndex(
        _digest_definition(database.table), database.pool, database.metrics
    )
    database.manager.register(index)
    ref = database.insert(1, "Ada", BORN)
    txn = TransactionDouble(txn_id=3)
    database.manager.stage_row_insert(
        txn, database.table.table_id, ref, (1, "Ada"), BORN
    )
    database.manager.commit(txn, BORN)

    assert database.manager.verify("person_by_digest") == ()
    assert database.manager.lookup(
        "person_by_digest", index.definition.key_for((1, "Ada")), SnapshotDouble(BORN)
    ) == (ref,)


def test_a_row_that_drifted_under_a_declared_derivation_is_still_reported(
    database: Database,
) -> None:
    """The other side, so the clean answer above cannot be satisfied by never comparing."""
    index = HashIndex(
        _digest_definition(database.table), database.pool, database.metrics
    )
    database.manager.register(index)
    ref = database.insert(1, "Ada", BORN)
    key = index.definition.key_for((1, "Ada"))
    moved = database.heap.update(database.table, ref, (1, "Grace"), ENDED)
    txn = TransactionDouble(txn_id=3)
    index.stage_insert(txn, key, moved, 0)
    index.commit(txn, ENDED)

    kinds = {finding.kind for finding in database.manager.verify("person_by_digest")}

    assert "stale_entry" in kinds


# --- isolation -----------------------------------------------------------------------------------


def test_two_databases_keep_their_own_indexes(database: Database) -> None:
    """BR-8 and FR-13, stated for this component: no module-level state anywhere."""
    other = build_database(name="other")
    txn = TransactionDouble(txn_id=1)
    ref = database.insert(1, "Ada", BORN)
    database.manager.stage_row_insert(
        txn, database.table.table_id, ref, (1, "Ada"), BORN
    )
    database.manager.commit(txn, BORN)

    assert other.entries() == {"person_by_name": (), "person_near_name": ()}
    assert other.manager.verify() == ()


def test_a_bucket_is_decided_by_the_key_and_the_count_alone() -> None:
    assert bucket_of(b"Ada", 4) == bucket_of(b"Ada", 4)
    assert 0 <= bucket_of(b"Ada", 4) < 4


def test_a_manager_starts_with_no_published_position(database: Database) -> None:
    fresh = IndexManager(database.pool, database.heap, database.metrics)

    assert fresh.published_lsn == 0
    assert fresh.indexes() == ()
