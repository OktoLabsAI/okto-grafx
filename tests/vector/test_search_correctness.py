"""What a similarity search must never get wrong (SPEC-VEC FR-9, BR-2, AC-5, VTS-7, VTS-8).

A search that omits a vector it should return, or returns one that was deleted, is a wrong
result. This module is where that is measured: against brute force for completeness, against the
snapshot for visibility, and against a heap that disagrees with the index for the difference
between the two visibility contracts.

The last of those is the one worth reading carefully. The exact regime asks the HEAP whether a
candidate exists, so a stale index entry cannot put a removed row into its answer. The
approximate regime asks the versioned index, which is what makes a traversal affordable, and a
divergence between the two is therefore a state it cannot see -- so the divergence is DETECTED
and named by ``verify_index`` rather than papered over by a second guard that would make the
first unprovable (amendment A67).
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.domain.vector.planner import REGIME_APPROXIMATE, REGIME_EXACT

from .conftest import (
    RecordingMetrics,
    TransactionDouble,
    SnapshotDouble,
    StepClock,
    VectorFixture,
    seeded_vectors,
)

DIMENSION: int = 8
CORPUS: int = 40


def _database(
    metrics: RecordingMetrics, clock: StepClock, *, threshold: int
) -> tuple[VectorFixture, list[tuple[float, ...]], list[RecordRef]]:
    """Return a database holding a seeded corpus with one row per vector."""
    database = VectorFixture(
        metrics=metrics, clock=clock, exact_scan_threshold=threshold, ef_search=CORPUS
    )
    space = database.create_space("space", DIMENSION, storage_dtype="float64")
    table = database.create_table("Chunk", "space")
    corpus = seeded_vectors(CORPUS, DIMENSION, seed=0x5AFE)
    refs = [
        database.insert_row(table, index + 1, index % 3, space, values, csn=10 + index)
        for index, values in enumerate(corpus)
    ]
    return database, corpus, refs


def _truth(database: VectorFixture, corpus, query, k: int, admitted=None) -> list[int]:
    """Return the ground truth ranking, computed with no index in the path at all."""
    candidates = [
        (index + 1, values)
        for index, values in enumerate(corpus)
        if admitted is None or (index + 1) in admitted
    ]
    return [
        identifier
        for identifier, _score in database.math.top_k(
            query, candidates, k, DistanceMetric.COSINE
        )
    ]


# --- completeness -----------------------------------------------------------------------------


@pytest.mark.parametrize("position", [0, 7, 19, 33])
def test_the_exact_regime_returns_the_ground_truth_for_every_query(
    metrics: RecordingMetrics, clock: StepClock, position: int
) -> None:
    """AC-5: recall is 1.0 by construction, measured against brute force rather than claimed."""
    database, corpus, _refs = _database(metrics, clock, threshold=CORPUS)
    query = corpus[position]
    result = database.engine.search(
        space="space", query=query, k=8, snapshot=SnapshotDouble(1000)
    )
    assert result.regime == REGIME_EXACT
    assert [hit.record_id for hit in result.hits] == _truth(database, corpus, query, 8)


def test_the_exact_regime_returns_the_ground_truth_of_the_filtered_set(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The filtered set is what is scanned, so the truth is the truth of that set."""
    database, corpus, _refs = _database(metrics, clock, threshold=CORPUS)
    admitted = frozenset({2, 5, 8, 13, 21, 34})
    query = corpus[0]
    result = database.engine.search(
        space="space",
        query=query,
        k=4,
        snapshot=SnapshotDouble(1000),
        candidate_filter=RecordIdFilter.of(admitted),
    )
    assert [hit.record_id for hit in result.hits] == _truth(
        database, corpus, query, 4, admitted
    )


def test_a_result_never_holds_the_same_record_twice(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """One snapshot sees one version of a record, so a ranking cannot name it twice."""
    database, corpus, refs = _database(metrics, clock, threshold=CORPUS)
    table = database.table
    assert table is not None
    space = database.engine.space("space")
    database.delete_row(table, refs[0], 1, space, corpus[0], csn=100)
    new_ref = database.insert_row(table, 1, 0, space, corpus[0], csn=100)
    assert new_ref != refs[0]
    result = database.engine.search(
        space="space", query=corpus[0], k=CORPUS, snapshot=SnapshotDouble(1000)
    )
    identifiers = [hit.record_id for hit in result.hits]
    assert len(identifiers) == len(set(identifiers))


# --- visibility -------------------------------------------------------------------------------


def test_a_deleted_row_never_appears_in_either_regime(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The sharpest rule of the component, asserted on both sides of the threshold."""
    for threshold, regime in ((CORPUS, REGIME_EXACT), (0, REGIME_APPROXIMATE)):
        database, corpus, refs = _database(metrics, clock, threshold=threshold)
        table = database.table
        assert table is not None
        space = database.engine.space("space")
        database.delete_row(table, refs[3], 4, space, corpus[3], csn=200)
        result = database.engine.search(
            space="space", query=corpus[3], k=CORPUS, snapshot=SnapshotDouble(1000)
        )
        assert result.regime == regime
        assert 4 not in {hit.record_id for hit in result.hits}
        assert len(result.hits) == CORPUS - 1


def test_a_reader_on_an_old_snapshot_keeps_its_answer_while_writes_commit(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """FR-9 under concurrent writing: the view a reader opened with is the view it keeps.

    The interleaving is expressed as commit numbers rather than as threads, because that is what
    a concurrent write IS to a snapshot -- and because a seeded, ordered interleaving is
    reproducible while a race is not.
    """
    database, corpus, refs = _database(metrics, clock, threshold=CORPUS)
    table = database.table
    assert table is not None
    space = database.engine.space("space")
    reader = SnapshotDouble(60)
    query = corpus[2]
    before = database.engine.search(space="space", query=query, k=10, snapshot=reader)
    for step in range(6):
        database.delete_row(table, refs[step], step + 1, space, corpus[step], csn=200 + step)
        extra = tuple(component * 0.5 for component in corpus[step])
        database.insert_row(table, 500 + step, 0, space, extra, csn=300 + step)
    after = database.engine.search(space="space", query=query, k=10, snapshot=reader)
    assert [hit.record_id for hit in before.hits] == [hit.record_id for hit in after.hits]
    assert [hit.score for hit in before.hits] == [hit.score for hit in after.hits]


def test_a_newer_reader_sees_exactly_what_the_older_one_cannot(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The other side of the same rule: a later snapshot is entitled to the later writes."""
    database, corpus, refs = _database(metrics, clock, threshold=CORPUS)
    table = database.table
    assert table is not None
    space = database.engine.space("space")
    database.delete_row(table, refs[0], 1, space, corpus[0], csn=200)
    older = database.engine.search(
        space="space", query=corpus[0], k=CORPUS, snapshot=SnapshotDouble(199)
    )
    newer = database.engine.search(
        space="space", query=corpus[0], k=CORPUS, snapshot=SnapshotDouble(200)
    )
    assert 1 in {hit.record_id for hit in older.hits}
    assert 1 not in {hit.record_id for hit in newer.hits}


def test_an_updated_row_shows_one_version_per_snapshot(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """An update is a delete and an insert at one commit, so exactly one side is ever visible."""
    database, corpus, refs = _database(metrics, clock, threshold=CORPUS)
    table = database.table
    assert table is not None
    space = database.engine.space("space")
    replacement = tuple(-component for component in corpus[4])
    database.delete_row(table, refs[4], 5, space, corpus[4], csn=250)
    database.insert_row(table, 5, 0, space, replacement, csn=250)
    old_view = database.engine.search(
        space="space", query=corpus[4], k=1, snapshot=SnapshotDouble(249)
    )
    new_view = database.engine.search(
        space="space", query=replacement, k=1, snapshot=SnapshotDouble(250)
    )
    assert old_view.hits[0].record_id == 5
    assert new_view.hits[0].record_id == 5
    assert old_view.hits[0].ref != new_view.hits[0].ref


def test_a_filter_that_refuses_a_record_keeps_it_out_of_both_regimes(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The predicate decides membership, and it is asked about every candidate in both plans."""
    for threshold in (CORPUS, 0):
        database, corpus, _refs = _database(metrics, clock, threshold=threshold)
        admitted = frozenset({index for index in range(1, CORPUS + 1) if index % 5 == 0})
        result = database.engine.search(
            space="space",
            query=corpus[0],
            k=CORPUS,
            snapshot=SnapshotDouble(1000),
            candidate_filter=RecordIdFilter.of(admitted),
        )
        assert {hit.record_id for hit in result.hits} <= admitted


# --- the two visibility contracts, side by side -------------------------------------------------


def test_the_exact_regime_asks_the_heap_and_ignores_a_stale_index_entry(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The heap is the authority under this contract, so a stale entry changes no answer.

    This is the difference between the two visibility contracts, made visible. The exact regime
    reads the row and applies the snapshot to it, so an index entry whose tombstone never arrived
    cannot put the deleted row into the answer.
    """
    database, corpus, refs = _database(metrics, clock, threshold=CORPUS)
    table = database.table
    assert table is not None
    space = database.engine.space("space")
    database.delete_row(
        table, refs[6], 7, space, corpus[6], csn=300, apply_index=False
    )
    assert database.engine.index("space").live_count() == CORPUS
    result = database.engine.search(
        space="space", query=corpus[6], k=CORPUS, snapshot=SnapshotDouble(1000)
    )
    assert result.regime == REGIME_EXACT
    assert 7 not in {hit.record_id for hit in result.hits}


def test_a_divergence_between_the_index_and_the_heap_is_reported_by_the_registry(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """FR-6: the walk names the disagreement the traversal contract cannot see by itself.

    The detector is the framework's, shared with every other index of the database, which is one
    of the things adopting its store bought. A vector index does not need a verifier of its own.
    """
    database, corpus, refs = _database(metrics, clock, threshold=CORPUS)
    table = database.table
    assert table is not None
    space = database.engine.space("space")
    assert database.registry.verify() == ()
    database.delete_row(
        table, refs[6], 7, space, corpus[6], csn=300, apply_index=False
    )
    findings = database.registry.verify()
    assert findings
    assert all(finding.index == database.engine.index("space").name for finding in findings)


def test_a_consistent_index_produces_no_findings(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """A detector that always fires is not a detector; the clean case must be clean."""
    database, corpus, refs = _database(metrics, clock, threshold=CORPUS)
    table = database.table
    assert table is not None
    space = database.engine.space("space")
    database.delete_row(table, refs[1], 2, space, corpus[1], csn=300)
    assert database.registry.verify() == ()


def test_a_row_the_index_never_received_is_reported_as_an_omission(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """An omission is a wrong answer under either contract, and is reported as one."""
    database, corpus, _refs = _database(metrics, clock, threshold=CORPUS)
    table = database.table
    assert table is not None
    space = database.engine.space("space")
    database.insert_row(
        table, 999, 0, space, corpus[0], csn=400, apply_index=False
    )
    findings = database.registry.verify()
    assert findings
    assert any("missing" in finding.kind or "omission" in finding.kind for finding in findings)


def test_an_entry_whose_row_the_heap_cannot_supply_refuses_the_search(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The graph is built from the heap, so a row it cannot read is a typed refusal, not a crash.

    The refusal names the entry rather than letting a corruption from the heap escape as though
    the index had asked for something impossible.
    """
    database, corpus, _refs = _database(metrics, clock, threshold=0)
    index = database.engine.index("space")
    txn = TransactionDouble()
    index.stage_insert(txn, index.key_for(corpus[0]), RecordRef(999, 3), 500)
    index.commit(txn, 500)
    with pytest.raises(GrafxIndexError) as failure:
        database.engine.search(
            space="space", query=corpus[0], k=3, snapshot=SnapshotDouble(1000)
        )
    assert failure.value.details["field"] == "ref"
    assert failure.value.details["page"] == 999


# --- the hybrid shape a query planner will use --------------------------------------------------


def test_a_filtered_search_is_one_pass_and_needs_no_second_visit_per_candidate(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """VTS-7 at this component's boundary: the caller hands over a set and gets the answer.

    The whole point of the filter seam is that the caller never fetches more than it wants and
    then throws rows away. The assertion is that a selective filter, evaluated inside the search,
    produces exactly the brute-force answer of the filtered set in one call.
    """
    database, corpus, _refs = _database(metrics, clock, threshold=0)
    admitted = frozenset({4, 9, 16, 25, 36})
    query = corpus[1]
    result = database.engine.search(
        space="space",
        query=query,
        k=3,
        snapshot=SnapshotDouble(1000),
        candidate_filter=RecordIdFilter.of(admitted),
    )
    assert result.regime == REGIME_APPROXIMATE
    assert [hit.record_id for hit in result.hits] == _truth(
        database, corpus, query, 3, admitted
    )


def test_the_reference_of_every_hit_points_at_the_row_that_was_ranked(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """A hit carries the physical location, so a caller can read the row without searching again."""
    database, corpus, refs = _database(metrics, clock, threshold=CORPUS)
    result = database.engine.search(
        space="space", query=corpus[0], k=5, snapshot=SnapshotDouble(1000)
    )
    for hit in result.hits:
        version = database.heap.read(hit.ref)
        assert version.record_id == hit.record_id
    assert result.hits[0].ref == refs[0]


def test_an_entry_pointing_at_a_location_the_heap_cannot_read_is_reported(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """A walk must report a location it could not read rather than failing the whole pass."""
    database, corpus, _refs = _database(metrics, clock, threshold=CORPUS)
    index = database.engine.index("space")
    txn = TransactionDouble()
    index.stage_insert(txn, index.key_for(corpus[0]), RecordRef(999, 3), 500)
    index.commit(txn, 500)
    findings = database.registry.verify()
    assert findings
    assert any(finding.ref == RecordRef(999, 3) for finding in findings)


def test_an_entry_whose_stored_vector_drifted_from_the_page_is_reported(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """FR-6 asks for index-heap divergence detection, and a drifted vector is one shape of it.

    The key of an entry IS the encoded vector, so an entry whose components disagree with the row
    is filed under a key the row no longer carries -- which is exactly what the framework's walk
    compares. Deriving the key the framework's way is what makes this detectable at all.
    """
    database, corpus, refs = _database(metrics, clock, threshold=CORPUS)
    index = database.engine.index("space")
    drifted = tuple(component + 1.0 for component in corpus[0])
    txn = TransactionDouble()
    index.stage_insert(txn, index.key_for(drifted), refs[0], 500)
    index.commit(txn, 500)
    findings = database.registry.verify()
    assert findings
    assert any("divergence" in finding.kind for finding in findings)


# --- guards a rewrite of this suite dropped, restored after the battery named them (A82) --------


@pytest.mark.parametrize(
    ("threshold", "regime"), [(1000, REGIME_EXACT), (0, REGIME_APPROXIMATE)]
)
def test_two_visible_versions_of_one_record_are_refused_rather_than_ranked_twice(
    metrics: RecordingMetrics, clock: StepClock, threshold: int, regime: str
) -> None:
    """A ranking that named one record twice would be a wrong result, not a detectable one.

    Both regimes are asserted because each reaches the state through its own door: the scan finds
    the second version by reading the heap, the traversal finds it as a second node in the graph.
    A guard on one path proves nothing about the other (amendment A66).

    This test existed, was lost when the suite was rewritten onto the index framework, and was
    restored when the battery reported both guards surviving. That is what A82 is for.
    """
    database, corpus, _refs = _database(metrics, clock, threshold=threshold)
    table = database.table
    assert table is not None
    space = database.engine.space("space")
    clean = database.engine.search(
        space="space", query=corpus[1], k=1, snapshot=SnapshotDouble(399)
    )
    assert clean.regime == regime

    duplicate = database.insert_row(table, 1, 0, space, corpus[0], csn=400)
    assert duplicate is not None
    with pytest.raises(GrafxIndexError) as failure:
        database.engine.search(
            space="space", query=corpus[0], k=5, snapshot=SnapshotDouble(1000)
        )
    assert failure.value.details["field"] == "record_id"
    assert failure.value.details["value"] == 1


def test_a_tie_is_broken_by_the_record_and_not_by_the_order_versions_entered(
    metrics: RecordingMetrics, clock: StepClock
) -> None:
    """The port breaks a tie by candidate identifier, and the candidate here is the record.

    The graph orders equal scores by its own internal node number, which follows the order the
    versions entered the index. That is not the caller's rule: two records that tie come back by
    ascending record identifier, whichever entered first. The two agree in every ordinary corpus,
    so the difference is only visible when the two orders are made to disagree -- which is why
    the higher record identifier is inserted first here.
    """
    database = VectorFixture(metrics=metrics, clock=clock, exact_scan_threshold=0, ef_search=16)
    space = database.create_space("space", 3)
    table = database.create_table("Chunk", "space")
    values = (1.0, 0.0, 0.0)
    database.insert_row(table, 9, 0, space, values, csn=10)
    database.insert_row(table, 2, 0, space, values, csn=11)
    index = database.engine.index("space")
    scored, _stats = index.search(values, 2, SnapshotDouble(1000))
    assert [item.record_id for item in scored] == [2, 9]
    assert scored[0].score == scored[1].score
    result = database.engine.search(
        space="space", query=values, k=2, snapshot=SnapshotDouble(1000)
    )
    assert [hit.record_id for hit in result.hits] == [2, 9]
