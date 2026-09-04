"""Selective exact vector search from query-owned immutable heap witnesses (VEC-4)."""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxVectorValidationError,
)
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.domain.vector.filter import CandidateFilter, RecordIdFilter
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.vector_engine import VectorHnswIndex

from .conftest import FIXTURE_READ_LSN, SnapshotDouble, VectorFixture


@dataclass
class _Work:
    """Structural work performed by one exact vector search."""

    walks: int = 0
    heap_reads: int = 0
    bucket_pages: int = 0

    def clear(self) -> None:
        """Forget one measurement without replacing monkeypatched closures."""
        self.walks = 0
        self.heap_reads = 0
        self.bucket_pages = 0


def _instrument(
    monkeypatch: pytest.MonkeyPatch, index: VectorHnswIndex, heap: HeapStore
) -> _Work:
    """Count only work performed by the selected fixture's index and heap."""
    work = _Work()
    original_walk = VectorHnswIndex.walk
    original_read_if = HeapStore.read_if
    original_entries_on = VectorHnswIndex._entries_on

    def counted_walk(self: VectorHnswIndex) -> tuple[object, ...]:
        if self is index:
            work.walks += 1
        return original_walk(self)

    def counted_read_if(self: HeapStore, ref: RecordRef, accept: object) -> object:
        if self is heap:
            work.heap_reads += 1
        return original_read_if(self, ref, accept)  # type: ignore[arg-type]

    def counted_entries_on(
        self: VectorHnswIndex, page_index: int
    ) -> tuple[object, ...]:
        if self is index:
            work.bucket_pages += 1
        return original_entries_on(self, page_index)

    monkeypatch.setattr(VectorHnswIndex, "walk", counted_walk)
    monkeypatch.setattr(HeapStore, "read_if", counted_read_if)
    monkeypatch.setattr(VectorHnswIndex, "_entries_on", counted_entries_on)
    return work


def _seed(
    database: VectorFixture, count: int = 64
) -> tuple[object, object, dict[int, RecordRef]]:
    """Create one exact-search corpus, including deterministic score ties."""
    space = database.create_space("vectors", 4)
    table = database.create_table("items", space.name)
    refs: dict[int, RecordRef] = {}
    for record_id in range(1, count + 1):
        values = (
            (1.0, 0.0, 0.0, 0.0)
            if record_id <= 4
            else (1.0, record_id / count, (count - record_id) / count, 0.0)
        )
        refs[record_id] = database.insert_row(
            table, record_id, record_id % 3, space, values, csn=1
        )
    return space, table, refs


def _seal(
    database: VectorFixture,
    space: object,
    table: object,
    snapshot: Snapshot,
    witnesses: tuple[tuple[int, RecordRef], ...],
) -> CandidateFilter:
    """Seal witnesses using the private query/engine integration door."""
    sealed = database.engine._seal_materialized_candidates(
        witnesses,
        space=getattr(space, "name"),
        table_id=getattr(table, "table_id"),
        position=getattr(table, "column_positions")["embedding"],
        snapshot=snapshot,
        candidate_count=len({record_id for record_id, _ref in witnesses}),
    )
    assert sealed is not None
    return sealed


def _search(
    database: VectorFixture,
    candidate_filter: CandidateFilter,
    snapshot: object,
    *,
    k: int = 3,
    query: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0),
) -> object:
    """Run one exact search through the public engine operation."""
    return database.engine.search(
        space="vectors",
        query=query,
        k=k,
        snapshot=snapshot,  # type: ignore[arg-type]
        candidate_filter=candidate_filter,
    )


def _device_image(database: VectorFixture) -> dict[str, bytes]:
    """Copy every durable byte so a search-side write cannot hide in an unchanged size."""
    return {
        file: database.device.read_log(file, 0, database.device.file_size(file))
        for file in database.device.list_files()
    }


def test_sealed_refs_match_the_canonical_oracle_and_bound_structural_work(
    database: VectorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ties/top-k stay byte-for-byte equal while candidate heap work is proportional to R."""
    space, table, refs = _seed(database)
    snapshot = Snapshot(FIXTURE_READ_LSN)
    index = database.engine.index("vectors")
    index.live_count()  # keep cold count derivation outside the structural measurement
    work = _instrument(monkeypatch, index, database.heap)
    record_ids = frozenset((1, 2, 3, 4))

    canonical = _search(database, RecordIdFilter(record_ids), snapshot)
    canonical_pages = work.bucket_pages
    assert work.walks == 1
    assert work.heap_reads == 64

    work.clear()
    sealed = _seal(
        database,
        space,
        table,
        snapshot,
        tuple((record_id, refs[record_id]) for record_id in (4, 3, 2, 1)),
    )
    before = _device_image(database)
    targeted = _search(database, sealed, snapshot)

    assert targeted == canonical
    assert _device_image(database) == before
    assert [hit.record_id for hit in targeted.hits] == [1, 2, 3]
    assert work.walks == 0
    assert work.heap_reads == len(record_ids)
    assert 0 < work.bucket_pages < canonical_pages


def test_empty_and_selectivity_boundary_have_a_fixed_conservative_gate(
    database: VectorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sealed empty set is free; 16/64 is admitted and 17/64 falls back."""
    space, table, refs = _seed(database)
    snapshot = Snapshot(FIXTURE_READ_LSN)
    index = database.engine.index("vectors")
    index.live_count()
    work = _instrument(monkeypatch, index, database.heap)

    empty = _seal(database, space, table, snapshot, ())
    assert _search(database, empty, snapshot).hits == ()
    assert work == _Work()

    for count in (1, 4, 16):
        work.clear()
        sealed = _seal(
            database,
            space,
            table,
            snapshot,
            tuple((record_id, refs[record_id]) for record_id in range(1, count + 1)),
        )
        _search(database, sealed, snapshot)
        assert work.walks == 0
        assert work.heap_reads == count

    work.clear()
    broad_ids = frozenset(range(1, 18))
    broad = database.engine._seal_materialized_candidates(
        tuple((record_id, refs[record_id]) for record_id in broad_ids),
        space=getattr(space, "name"),
        table_id=getattr(table, "table_id"),
        position=getattr(table, "column_positions")["embedding"],
        snapshot=snapshot,
        candidate_count=len(broad_ids),
    )
    assert broad is None
    _search(database, RecordIdFilter(broad_ids), snapshot)
    assert work.walks == 1
    assert work.heap_reads == 64


def test_untrusted_or_misbound_filters_retain_the_canonical_scan(
    database: VectorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public filters, custom snapshots and carrier metadata drift cannot authorize refs."""
    space, table, refs = _seed(database)
    snapshot = Snapshot(FIXTURE_READ_LSN)
    index = database.engine.index("vectors")
    index.live_count()
    work = _instrument(monkeypatch, index, database.heap)
    public = RecordIdFilter(frozenset((1,)))

    _search(database, public, snapshot)
    assert (work.walks, work.heap_reads) == (1, 64)

    sealed = _seal(database, space, table, snapshot, ((1, refs[1]),))
    work.clear()
    _search(database, sealed, SnapshotDouble(FIXTURE_READ_LSN))
    assert (work.walks, work.heap_reads) == (1, 64)

    work.clear()
    misbound = replace(sealed, read_lsn=FIXTURE_READ_LSN - 1)
    _search(database, misbound, snapshot)
    assert (work.walks, work.heap_reads) == (1, 64)


def test_live_space_cost_boundary_declines_four_refs_below_sixteen(
    database: VectorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The corpus half of ``4 * R <= min(E, B)`` is an enforced boundary too."""
    space, table, refs = _seed(database, count=16)
    snapshot = Snapshot(FIXTURE_READ_LSN)
    index = database.engine.index("vectors")
    index.live_count()
    work = _instrument(monkeypatch, index, database.heap)
    witnesses = tuple((record_id, refs[record_id]) for record_id in (1, 2, 3, 4))
    sealed = _seal(database, space, table, snapshot, witnesses)

    _search(database, sealed, snapshot)
    assert (work.walks, work.heap_reads) == (0, 4)

    database.delete_row(
        table,
        refs[16],
        16,
        space,
        (1.0, 1.0, 0.0, 0.0),
        csn=2,
    )
    work.clear()
    _search(database, sealed, snapshot)
    assert work.walks == 1


def test_incomplete_or_ambiguous_proofs_fall_back_before_scoring(
    database: VectorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrong refs, absent index entries and distinct versions never produce a fast result."""
    space, table, refs = _seed(database)
    snapshot = Snapshot(FIXTURE_READ_LSN)
    index = database.engine.index("vectors")
    index.live_count()
    work = _instrument(monkeypatch, index, database.heap)

    canonical = _search(database, RecordIdFilter(frozenset((1,))), snapshot)
    work.clear()
    wrong_ref = _seal(database, space, table, snapshot, ((1, refs[2]),))
    assert _search(database, wrong_ref, snapshot) == canonical
    assert work.walks == 1
    assert work.heap_reads == 65

    missing_ref = database.insert_row(
        table,
        65,
        0,
        space,
        (0.0, 0.0, 0.0, 1.0),
        csn=1,
        apply_index=False,
    )
    expected_missing = _search(database, RecordIdFilter(frozenset((65,))), snapshot)
    work.clear()
    missing = _seal(database, space, table, snapshot, ((65, missing_ref),))
    assert _search(database, missing, snapshot) == expected_missing
    assert work.walks == 1
    assert work.heap_reads == 65

    null_ref = database.heap.insert(table, 66, (66, 0, None), 1)
    expected_null = _search(database, RecordIdFilter(frozenset((66,))), snapshot)
    work.clear()
    nullable = _seal(database, space, table, snapshot, ((66, null_ref),))
    assert _search(database, nullable, snapshot) == expected_null
    assert work.walks == 1

    database.delete_row(
        table,
        refs[3],
        3,
        space,
        (1.0, 0.0, 0.0, 0.0),
        csn=2,
    )
    expected_deleted = _search(database, RecordIdFilter(frozenset((3,))), snapshot)
    work.clear()
    deleted = _seal(database, space, table, snapshot, ((3, refs[3]),))
    assert _search(database, deleted, snapshot) == expected_deleted
    assert work.walks == 1

    historical = Snapshot(1)
    expected_historical = _search(database, RecordIdFilter(frozenset((3,))), historical)
    work.clear()
    old_visible = _seal(database, space, table, historical, ((3, refs[3]),))
    assert _search(database, old_visible, historical) == expected_historical
    assert work.walks == 0
    assert work.heap_reads == 1

    duplicate_ref = database.insert_row(table, 1, 0, space, (0.0, 1.0, 0.0, 0.0), csn=1)
    ambiguous = database.engine._seal_materialized_candidates(
        ((1, refs[1]), (1, duplicate_ref)),
        space=getattr(space, "name"),
        table_id=getattr(table, "table_id"),
        position=getattr(table, "column_positions")["embedding"],
        snapshot=snapshot,
        candidate_count=1,
    )
    assert ambiguous is None
    work.clear()
    with pytest.raises(GrafxIndexError) as raised:
        _search(database, RecordIdFilter(frozenset((1,))), snapshot)
    assert raised.value.details["field"] == "record_id"
    assert work.walks == 1


def test_validation_precedes_work_and_selected_corruption_is_not_masked(
    database: VectorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad query dimensions do no I/O; corruption in an authenticated bucket remains visible."""
    space, table, refs = _seed(database)
    snapshot = Snapshot(FIXTURE_READ_LSN)
    index = database.engine.index("vectors")
    index.live_count()
    sealed = _seal(database, space, table, snapshot, ((1, refs[1]),))
    work = _instrument(monkeypatch, index, database.heap)

    with pytest.raises(GrafxVectorValidationError) as targeted_error:
        _search(database, sealed, snapshot, query=(1.0, 0.0))
    with pytest.raises(GrafxVectorValidationError) as canonical_error:
        _search(
            database,
            RecordIdFilter(frozenset((1,))),
            snapshot,
            query=(1.0, 0.0),
        )
    assert targeted_error.value.details == canonical_error.value.details
    assert work == _Work()

    def corrupted_bucket(self: VectorHnswIndex, witnesses: object) -> bool:
        raise GrafxCorruptionDetected(
            "selected vector bucket is corrupt", field="selected_bucket"
        )

    monkeypatch.setattr(
        VectorHnswIndex,
        "_authenticate_exact_witnesses_unchecked",
        corrupted_bucket,
    )
    with pytest.raises(GrafxCorruptionDetected) as raised:
        _search(database, sealed, snapshot)
    assert raised.value.details["field"] == "selected_bucket"
    assert work.walks == 0
    assert work.heap_reads == 1


def test_repeated_identical_witnesses_collapse_but_conflicting_identity_declines(
    database: VectorFixture,
) -> None:
    """Join fanout is harmless, while one ref claimed by two record ids is never sealed."""
    space, table, refs = _seed(database, count=8)
    snapshot = Snapshot(FIXTURE_READ_LSN)
    repeated = _seal(database, space, table, snapshot, ((1, refs[1]), (1, refs[1])))
    assert repeated.cardinality == 1
    assert len(getattr(repeated, "witnesses")) == 1
    assert (
        database.engine._seal_materialized_candidates(
            ((1, refs[1]), (2, refs[1])),
            space=getattr(space, "name"),
            table_id=getattr(table, "table_id"),
            position=getattr(table, "column_positions")["embedding"],
            snapshot=snapshot,
            candidate_count=2,
        )
        is None
    )

    class BroadWitnesses:
        """Expose accidental iteration when the fixed bucket pre-gate already declines."""

        def __iter__(self) -> object:
            raise AssertionError("a broad filter must decline before copying refs")

    assert (
        database.engine._seal_materialized_candidates(
            BroadWitnesses(),
            space=getattr(space, "name"),
            table_id=getattr(table, "table_id"),
            position=getattr(table, "column_positions")["embedding"],
            snapshot=snapshot,
            candidate_count=17,
        )
        is None
    )


def test_a_stale_index_refuses_before_a_sealed_candidate_can_run(
    database: VectorFixture,
) -> None:
    """The private carrier does not bypass the existing freshness refusal."""
    space, table, refs = _seed(database, count=8)
    snapshot = Snapshot(FIXTURE_READ_LSN)
    sealed = _seal(database, space, table, snapshot, ((1, refs[1]),))
    database.engine.index("vectors").mark_stale("forced VEC-4 freshness test")

    with pytest.raises(GrafxIndexError) as raised:
        _search(database, sealed, snapshot)
    assert raised.value.details["field"] == "stale"
