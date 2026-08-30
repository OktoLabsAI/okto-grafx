"""The two visibility rules, and the boundary between them (SPEC-M1 SD-3, BR-11; FR-12).

Everything here is about one question: what may a caller rely on when an index answers?

An EXACT index answers with candidates and the heap decides. A PROXIMITY index answers with
results and the entry decides. The tests are written so that each rule fails if the OTHER one is
applied: an exact index that filtered by stamp would omit a row an older snapshot is entitled to,
and a proximity index that did not filter would return a deleted one.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index import (
    IndexChange,
    IndexEntry,
    IndexOperation,
    IndexVisibility,
    entry_visible,
    is_reclaimable,
    wal_record_for,
)
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.page import PageType
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexManager, ProximityIndex

from .conftest import Database, SnapshotDouble, TransactionDouble

BORN: int = 10
"""The commit number every row in this module is created at."""

ENDED: int = 20
"""The commit number a row is deleted or superseded at."""


class CountingHeap:
    """A heap store that counts how many times an index made it read a version.

    A proximity lookup that consulted the heap would be correct and pointless -- the whole reason
    for versioned entries is that a proximity structure cannot afford a heap read per candidate.
    Counting is the only way to state that as a test rather than as a comment.
    """

    def __init__(self, heap: HeapStore) -> None:
        self._heap = heap
        self.reads: int = 0

    @property
    def catalog(self) -> object:
        """Return the catalog store the real heap uses."""
        return self._heap.catalog

    @property
    def file(self) -> str:
        """Return the companion file whose reads this wrapper counts."""
        return self._heap.file

    def read(self, ref: RecordRef) -> object:
        """Return the version at that location and count the read."""
        self.reads += 1
        return self._heap.read(ref)

    def scan_all(self, table: TableDef) -> object:
        """Yield every stored version of the table, exactly as the real heap does."""
        return self._heap.scan_all(table)

    def committed_high_water(self, table: TableDef) -> int:
        """Delegate the header-only freshness scan without counting logical row reads."""
        return self._heap.committed_high_water(table)


def _insert(
    heap: HeapStore,
    manager: IndexManager,
    table: TableDef,
    record_id: int,
    name: str,
    csn: int = BORN,
) -> RecordRef:
    """Insert one row and commit the index change it owes, at one commit number."""
    txn = TransactionDouble(txn_id=record_id)
    ref = heap.insert(table, record_id, (record_id, name), csn)
    manager.stage_row_insert(txn, table.table_id, ref, (record_id, name), csn)
    manager.commit(txn, csn)
    return ref


def _delete(
    heap: HeapStore,
    manager: IndexManager,
    table: TableDef,
    record_id: int,
    name: str,
    ref: RecordRef,
    csn: int = ENDED,
) -> None:
    """Delete one row and commit the index change it owes."""
    txn = TransactionDouble(txn_id=record_id + 1000)
    heap.delete(table, ref, csn)
    manager.stage_row_delete(txn, table.table_id, ref, (record_id, name), csn)
    manager.commit(txn, csn)


def _key(index: HashIndex | ProximityIndex, record_id: int, name: str) -> bytes:
    """Return the key the index files a row under."""
    return index.definition.key_for((record_id, name))


# --- the exact contract ---------------------------------------------------------------------


def test_an_exact_lookup_returns_every_row_the_snapshot_can_see(
    heap_store: HeapStore,
    manager: IndexManager,
    exact_index: HashIndex,
    person_table: TableDef,
) -> None:
    first = _insert(heap_store, manager, person_table, 1, "Ada")
    second = _insert(heap_store, manager, person_table, 2, "Ada")
    other = _insert(heap_store, manager, person_table, 3, "Grace")

    found = manager.lookup(exact_index.name, _key(exact_index, 1, "Ada"), SnapshotDouble(BORN))

    assert found == (first, second), "both rows carry this key, and neither may be omitted"
    assert other not in found
    assert manager.lookup(
        exact_index.name, _key(exact_index, 3, "Grace"), SnapshotDouble(BORN)
    ) == (other,)


def test_an_exact_index_offers_a_deleted_row_as_a_candidate_and_the_validation_drops_it(
    heap_store: HeapStore,
    manager: IndexManager,
    exact_index: HashIndex,
    person_table: TableDef,
) -> None:
    """The superset property and the reason validation is mandatory, in one test.

    The raw index still names the row after it was deleted -- that IS the exact contract -- and
    the validated door does not. A framework that returned the raw hits would return a deleted
    row to a caller, which is the wrong result SD-3 exists to prevent.
    """
    ref = _insert(heap_store, manager, person_table, 1, "Ada")
    _delete(heap_store, manager, person_table, 1, "Ada", ref)
    key = _key(exact_index, 1, "Ada")
    after = SnapshotDouble(ENDED)

    assert exact_index.lookup(key, after) == (ref,), "the entry is still there: it is a candidate"
    assert manager.lookup(exact_index.name, key, after) == ()


def test_an_exact_index_still_answers_a_snapshot_older_than_the_delete(
    heap_store: HeapStore,
    manager: IndexManager,
    exact_index: HashIndex,
    person_table: TableDef,
) -> None:
    """Removing the entry at delete time would take the row from every older reader.

    This is the test that forbids the obvious implementation of ``stage_delete``. A snapshot
    opened before the delete is entitled to the row, and the index is the only way it will be
    found by key.
    """
    ref = _insert(heap_store, manager, person_table, 1, "Ada")
    _delete(heap_store, manager, person_table, 1, "Ada", ref)
    key = _key(exact_index, 1, "Ada")

    assert manager.lookup(exact_index.name, key, SnapshotDouble(ENDED - 1)) == (ref,)
    assert manager.lookup(exact_index.name, key, SnapshotDouble(ENDED)) == ()


def test_an_exact_lookup_hides_a_row_committed_after_the_snapshot_opened(
    heap_store: HeapStore,
    manager: IndexManager,
    exact_index: HashIndex,
    person_table: TableDef,
) -> None:
    _insert(heap_store, manager, person_table, 1, "Ada")
    key = _key(exact_index, 1, "Ada")

    assert manager.lookup(exact_index.name, key, SnapshotDouble(BORN - 1)) == ()
    assert len(manager.index(exact_index.name).candidates(key)) == 1


def test_an_exact_lookup_drops_a_candidate_whose_row_no_longer_carries_the_key(
    heap_store: HeapStore,
    manager: IndexManager,
    exact_index: HashIndex,
    person_table: TableDef,
) -> None:
    """An entry filed under a key the row has lost is stale, not a result.

    The heap keeps the old version and writes a new one, so the entry that pointed at the OLD
    version is still true about it. What must not happen is the old key answering with the NEW
    version, and that is what re-deriving the key from the heap prevents.
    """
    ref = _insert(heap_store, manager, person_table, 1, "Ada")
    key = _key(exact_index, 1, "Ada")
    store = manager.index(exact_index.name)
    txn = TransactionDouble(txn_id=99)
    # File the new version under the OLD key on purpose: this is the shape a missed update
    # leaves behind, and the validation is what has to notice.
    new_ref = heap_store.update(person_table, ref, (1, "Grace"), ENDED)
    store.stage_insert(txn, key, new_ref, 0)
    manager.commit(txn, ENDED)

    assert new_ref in store.lookup(key, SnapshotDouble(ENDED))
    assert manager.lookup(exact_index.name, key, SnapshotDouble(ENDED)) == ()


def test_the_exact_validation_reads_the_heap_and_the_proximity_lookup_does_not(
    pool_and_heap_manager: tuple[IndexManager, CountingHeap, TableDef, HeapStore],
) -> None:
    manager, counting, table, heap = pool_and_heap_manager
    exact = manager.index("person_by_name")
    near = manager.index("person_near_name")
    _insert(heap, manager, table, 1, "Ada")

    counting.reads = 0
    manager.lookup(near.name, _key(near, 1, "Ada"), SnapshotDouble(BORN))
    assert counting.reads == 0, "a proximity answer must not cost a heap read per candidate"

    manager.lookup(exact.name, _key(exact, 1, "Ada"), SnapshotDouble(BORN))
    assert counting.reads == 1


@pytest.fixture
def pool_and_heap_manager(
    pool: object,
    heap_store: HeapStore,
    metrics: object,
    person_table: TableDef,
) -> tuple[IndexManager, CountingHeap, TableDef, HeapStore]:
    """Return a manager whose heap counts the reads an index makes it perform."""
    counting = CountingHeap(heap_store)
    manager = IndexManager(pool, counting, metrics)  # type: ignore[arg-type]
    from .conftest import exact_definition, proximity_definition

    manager.register(HashIndex(exact_definition(person_table), pool, metrics))
    manager.register(ProximityIndex(proximity_definition(person_table), pool, metrics))
    return manager, counting, person_table, heap_store


# --- the proximity contract -----------------------------------------------------------------


def test_a_proximity_lookup_answers_exactly_what_the_snapshot_may_see(
    heap_store: HeapStore,
    manager: IndexManager,
    proximity_index: ProximityIndex,
    person_table: TableDef,
) -> None:
    ref = _insert(heap_store, manager, person_table, 1, "Ada")
    key = _key(proximity_index, 1, "Ada")

    assert manager.lookup(proximity_index.name, key, SnapshotDouble(BORN - 1)) == ()
    assert manager.lookup(proximity_index.name, key, SnapshotDouble(BORN)) == (ref,)


def test_a_proximity_lookup_never_returns_a_tombstoned_row(
    heap_store: HeapStore,
    manager: IndexManager,
    proximity_index: ProximityIndex,
    person_table: TableDef,
) -> None:
    """The one that would be a wrong result: no heap read stands behind this answer."""
    ref = _insert(heap_store, manager, person_table, 1, "Ada")
    _delete(heap_store, manager, person_table, 1, "Ada", ref)
    key = _key(proximity_index, 1, "Ada")

    assert manager.lookup(proximity_index.name, key, SnapshotDouble(ENDED)) == ()
    assert manager.lookup(proximity_index.name, key, SnapshotDouble(ENDED - 1)) == (ref,)


def test_a_proximity_index_answers_the_whole_structure_under_one_snapshot(
    heap_store: HeapStore,
    manager: IndexManager,
    proximity_index: ProximityIndex,
    person_table: TableDef,
) -> None:
    """The door a navigating structure uses: the same rule, without a key.

    A graph does not look a key up, it walks. This is what keeps a vector index from re-deriving
    the visibility rule, or from consulting the heap because the keyed door was the only one.
    """
    first = _insert(heap_store, manager, person_table, 1, "Ada")
    second = _insert(heap_store, manager, person_table, 2, "Grace", csn=BORN + 5)

    assert tuple(
        entry.ref for entry in proximity_index.visible_entries(SnapshotDouble(BORN))
    ) == (first,)
    assert set(
        entry.ref for entry in proximity_index.visible_entries(SnapshotDouble(BORN + 5))
    ) == {first, second}


# --- the boundary ----------------------------------------------------------------------------


def test_a_snapshot_cannot_decide_an_unversioned_entry(person_table: TableDef) -> None:
    """The structural half of SD-3: an exact entry has nothing for a snapshot to read."""
    entry = IndexEntry(key=b"k", ref=RecordRef(2, 1), versioned=False)

    with pytest.raises(GrafxIndexError) as refused:
        entry_visible(entry, SnapshotDouble(BORN))

    assert refused.value.details["field"] == "versioned"
    assert refused.value.details["visibility"] == IndexVisibility.EXACT.value


def test_an_exact_entry_cannot_carry_a_birth_stamp() -> None:
    with pytest.raises(GrafxIndexError) as refused:
        IndexEntry(key=b"k", ref=RecordRef(2, 1), versioned=False, born_csn=7)

    assert refused.value.details["field"] == "born_csn"
    assert refused.value.details["versioned"] is False


def test_a_proximity_entry_cannot_be_built_without_a_birth_stamp() -> None:
    with pytest.raises(GrafxIndexError) as refused:
        IndexEntry(key=b"k", ref=RecordRef(2, 1), versioned=True)

    assert refused.value.details["field"] == "born_csn"
    assert refused.value.details["versioned"] is True


def test_the_two_kinds_write_pages_of_different_types(
    exact_index: HashIndex, proximity_index: ProximityIndex
) -> None:
    """A page says which contract wrote it, so a raw verifier needs nothing else."""
    assert exact_index.page_type == int(PageType.INDEX_HASH)
    assert proximity_index.page_type == int(PageType.INDEX_HNSW)


def test_an_index_class_refuses_a_definition_of_the_other_contract(
    pool: object, metrics: object, person_table: TableDef
) -> None:
    from .conftest import exact_definition, proximity_definition

    with pytest.raises(GrafxIndexError) as exact_refused:
        ProximityIndex(exact_definition(person_table), pool, metrics)  # type: ignore[arg-type]
    assert exact_refused.value.details["value"] == IndexVisibility.EXACT.value

    with pytest.raises(GrafxIndexError) as proximity_refused:
        HashIndex(proximity_definition(person_table), pool, metrics)  # type: ignore[arg-type]
    assert proximity_refused.value.details["value"] == IndexVisibility.PROXIMITY.value


def test_a_record_shaped_for_the_other_contract_is_refused_rather_than_stored(
    database: Database,
) -> None:
    """The redo path takes records off a device, so the SHAPE is checked, not just the name.

    Staging stamps every change with the index's own visibility class, so a change can carry the
    wrong one only if the bytes were damaged or were written when this name meant a different
    index. Storing it would put an entry of the wrong shape in the file, and the shape is what
    the whole read path is built on: an unversioned entry carries no birth stamp, so every
    snapshot comparison over that bucket raises instead of answering.

    The last assertion is the one that says why refusing matters rather than merely tidies. An
    index that SWALLOWS such a record is broken and silent -- ``lookup`` on that key and
    ``visible_entries`` for the whole index both raise, while ``verify()`` reports nothing at
    all. Refusing turns that into an index that still reads, and a heap row with no entry, which
    is precisely the omission ``verify()`` is built to name.
    """
    ref = database.insert(1, "Ada", BORN)
    key = database.key(1, "Ada")
    exact_shaped = IndexChange(
        index=database.proximity.name,
        operation=IndexOperation.INSERT,
        key=key,
        ref=ref,
        csn=0,
        versioned=False,
    )

    with pytest.raises(GrafxCorruptionDetected) as refused:
        database.proximity.apply(wal_record_for(exact_shaped).with_lsn(BORN))

    assert refused.value.details["field"] == "versioned"
    assert refused.value.details["index"] == database.proximity.name
    assert database.proximity.walk() == (), "nothing of the wrong shape reached the file"
    # No commit was published: ask only for the durable prefix this deliberately empty index
    # can certify.  A later snapshot must now fail closed instead of silently trusting it.
    assert database.proximity.lookup(key, SnapshotDouble(0)) == ()
    assert [f.kind for f in database.manager.verify("person_near_name")] == ["missing_entry"]


def test_a_versioned_record_is_refused_by_an_exact_index(database: Database) -> None:
    """The same guard from the other side, so neither direction can pass for the other."""
    ref = database.insert(1, "Ada", BORN)
    proximity_shaped = IndexChange(
        index=database.exact.name,
        operation=IndexOperation.INSERT,
        key=database.key(1, "Ada"),
        ref=ref,
        csn=BORN,
        versioned=True,
    )

    with pytest.raises(GrafxCorruptionDetected) as refused:
        database.exact.apply(wal_record_for(proximity_shaped).with_lsn(BORN))

    assert refused.value.details["field"] == "versioned"
    assert database.exact.walk() == ()


# --- reclamation ------------------------------------------------------------------------------


def test_an_entry_that_was_never_ended_is_never_reclaimable() -> None:
    entry = IndexEntry(key=b"k", ref=RecordRef(2, 1), versioned=True, born_csn=1)

    assert not is_reclaimable(entry, 0)
    assert not is_reclaimable(entry, 2**63)


@pytest.mark.parametrize(
    ("horizon", "released"),
    [(ENDED - 1, False), (ENDED, True), (ENDED + 1, True)],
)
def test_the_horizon_releases_an_entry_exactly_when_no_snapshot_can_see_it(
    horizon: int, released: bool
) -> None:
    """The inclusive comparison, pinned on both sides of the boundary.

    At ``horizon == ENDED`` the lowest live snapshot reads at ENDED, and a version ended at ENDED
    is already invisible to it, so the entry is releasable. One below that, a snapshot reading at
    ENDED - 1 still sees the row, and releasing would take it away.
    """
    entry = IndexEntry(
        key=b"k", ref=RecordRef(2, 1), versioned=True, born_csn=BORN, dead_csn=ENDED
    )

    assert is_reclaimable(entry, horizon) is released
    assert SnapshotDouble(horizon).visible(BORN, ENDED) is not released
