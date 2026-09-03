"""Hit-driven heap materialisation for an unfiltered bounded vector query (P1.5)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxQueryBudgetExceeded,
)
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.model.record import HeapVersion
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.vector_engine import VectorEngine

from .stack import (
    FIXTURE_READ_LSN,
    QueryStack,
    TransactionDouble,
    build_query_stack,
    vector,
)

DIRECT = (
    "MATCH (n:Chunk) "
    "WHERE similarity(n.embedding, $q, space => 'minilm_v2') > -2.0 "
    "RETURN n.id, similarity_score() AS score ORDER BY score DESC LIMIT 3"
)
FILTERED = DIRECT.replace("WHERE similarity", "WHERE n.layer >= 0 AND similarity")
SELECTIVE = DIRECT.replace("WHERE similarity", "WHERE n.layer = 1 AND similarity")


def _seed(stack: QueryStack, count: int = 8) -> None:
    """Install a small deterministic ranking with every vector distinct."""
    for offset in range(count):
        record_id = offset + 1
        components = (1.0, offset / 10.0, (count - offset) / 20.0, 0.0)
        ref = stack.insert(
            "Chunk",
            record_id,
            (record_id, offset % 2, vector(components)),
            csn=1,
        )
        stack.add_vector(record_id, ref, components, csn=1)


def _transaction(
    stack: QueryStack, read_lsn: int = FIXTURE_READ_LSN
) -> TransactionDouble:
    """Return the production snapshot shape inside the otherwise recording test transaction."""
    transaction = stack.transaction(read_lsn)
    transaction.snapshot = Snapshot(read_lsn)  # type: ignore[assignment]
    return transaction


def _count_point_materialisations(
    monkeypatch: pytest.MonkeyPatch,
) -> list[RecordRef]:
    """Count only the query layer's post-search ref revalidations."""
    observed: list[RecordRef] = []
    original = HeapStore._revalidate_visible_ref

    def counted(
        self: HeapStore,
        table: object,
        ref: RecordRef,
        record_id: int,
        snapshot: object,
    ) -> object:
        """Record and delegate one exact physical witness check."""
        observed.append(ref)
        return original(self, table, ref, record_id, snapshot)  # type: ignore[arg-type]

    monkeypatch.setattr(HeapStore, "_revalidate_visible_ref", counted)
    return observed


def test_exact_whole_table_search_materialises_only_the_returned_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact oracle may scan vectors, but the query child no longer decodes N rows twice."""
    stack = build_query_stack(
        vector_nullable=False,
        vector_exact_scan_threshold=32,
    )
    _seed(stack)
    materialised = _count_point_materialisations(monkeypatch)

    direct = stack.engine.execute(
        DIRECT,
        _transaction(stack),
        {"q": [1.0, 0.0, 0.0, 0.0]},
    )

    assert direct.statistics["vector_regime_exact"] == 1
    assert direct.statistics["vector_direct_accesses"] == 1
    assert direct.statistics["vector_rows_materialized"] == 3
    assert "rows_scanned" not in direct.statistics
    assert len(materialised) == 3 < 8

    # A semantically neutral row predicate makes FilterRows the candidate authority and proves
    # the fallback still produces the identical ranking and scores.
    canonical = stack.engine.execute(
        FILTERED,
        _transaction(stack),
        {"q": [1.0, 0.0, 0.0, 0.0]},
    )
    assert canonical.rows == direct.rows
    assert canonical.statistics["rows_scanned"] == 8
    assert "vector_direct_accesses" not in canonical.statistics


def test_approximate_whole_table_search_is_k_point_reads_not_n(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sparse/nullable table is direct only once its approximate regime is unequivocal."""
    stack = build_query_stack(vector_exact_scan_threshold=2)
    _seed(stack)
    materialised = _count_point_materialisations(monkeypatch)

    direct = stack.engine.execute(
        DIRECT,
        _transaction(stack),
        {"q": [1.0, 0.0, 0.0, 0.0]},
    )
    assert direct.statistics["vector_regime_exact"] == 0
    assert direct.statistics["vector_direct_accesses"] == 1
    assert direct.statistics["vector_rows_materialized"] == 3
    assert len(materialised) == 3 < 8

    canonical = stack.engine.execute(
        FILTERED,
        _transaction(stack),
        {"q": [1.0, 0.0, 0.0, 0.0]},
    )
    assert canonical.rows == direct.rows
    assert canonical.statistics["vector_regime_exact"] == 0
    assert canonical.statistics["rows_scanned"] == 8
    assert "vector_direct_accesses" not in canonical.statistics

    selective = stack.engine.execute(
        SELECTIVE,
        stack.transaction(),
        {"q": [1.0, 0.0, 0.0, 0.0]},
    )
    assert all(record_id % 2 == 0 for record_id, _score in selective.rows)
    assert selective.statistics["rows_scanned"] == 8
    assert "vector_direct_accesses" not in selective.statistics


@pytest.mark.parametrize(
    ("nullable", "threshold"),
    [(False, 32), (True, 0)],
)
def test_score_ties_keep_the_canonical_order(nullable: bool, threshold: int) -> None:
    """Replacing the child scan cannot make equal scores acquire a new tie order."""
    stack = build_query_stack(
        vector_nullable=nullable,
        vector_exact_scan_threshold=threshold,
    )
    for record_id in range(1, 7):
        components = (1.0, 0.0, 0.0, 0.0)
        ref = stack.insert(
            "Chunk", record_id, (record_id, 1, vector(components)), csn=1
        )
        stack.add_vector(record_id, ref, components, csn=1)

    arguments = {"q": [1.0, 0.0, 0.0, 0.0]}
    direct = stack.engine.execute(DIRECT, _transaction(stack), arguments)
    canonical = stack.engine.execute(FILTERED, _transaction(stack), arguments)
    assert direct.rows == canonical.rows
    assert direct.statistics["vector_direct_accesses"] == 1


def test_historical_snapshot_and_small_sparse_set_keep_the_canonical_path() -> None:
    """No current-frontier or nullable-cardinality guess is allowed to select an access path."""
    exact = build_query_stack(
        vector_nullable=False,
        vector_exact_scan_threshold=32,
    )
    _seed(exact)
    historical = exact.engine.execute(
        DIRECT,
        _transaction(exact, read_lsn=999),
        {"q": [1.0, 0.0, 0.0, 0.0]},
    )
    assert historical.statistics["rows_scanned"] == 8
    assert "vector_direct_accesses" not in historical.statistics

    structural = exact.engine.execute(
        DIRECT,
        exact.transaction(),
        {"q": [1.0, 0.0, 0.0, 0.0]},
    )
    assert structural.rows == historical.rows
    assert structural.statistics["rows_scanned"] == 8
    assert "vector_direct_accesses" not in structural.statistics

    sparse = build_query_stack(vector_exact_scan_threshold=32)
    _seed(sparse, count=4)
    small = sparse.engine.execute(
        DIRECT,
        _transaction(sparse),
        {"q": [1.0, 0.0, 0.0, 0.0]},
    )
    assert small.statistics["rows_scanned"] == 4
    assert "vector_direct_accesses" not in small.statistics

    approximate = build_query_stack(vector_exact_scan_threshold=0)
    _seed(approximate, count=4)
    wide_k = approximate.engine.execute(
        DIRECT.replace("LIMIT 3", "LIMIT 10"),
        _transaction(approximate),
        {"q": [1.0, 0.0, 0.0, 0.0]},
    )
    assert wide_k.statistics["rows_scanned"] == 4
    assert "vector_direct_accesses" not in wide_k.statistics


def test_frontier_count_is_bound_to_the_planned_table_and_column() -> None:
    """A healthy count from another physical pair is not this scan's candidate proof."""
    stack = build_query_stack(
        vector_nullable=False,
        vector_exact_scan_threshold=32,
    )
    _seed(stack, count=4)
    chunk = stack.table("Chunk")
    person = stack.table("Person")
    snapshot = Snapshot(FIXTURE_READ_LSN)

    assert (
        stack.vectors.snapshot_frontier_live_count(
            "minilm_v2",
            snapshot,
            table_id=chunk.table_id,
            position=chunk.column_positions["embedding"],
        )
        == 4
    )
    assert (
        stack.vectors.snapshot_frontier_live_count(
            "minilm_v2",
            snapshot,
            table_id=person.table_id,
            position=0,
        )
        is None
    )
    assert (
        stack.vectors.snapshot_frontier_live_count(
            "minilm_v2",
            snapshot,
            table_id=chunk.table_id,
            position=0,
        )
        is None
    )


def test_intermediate_row_budget_preserves_the_child_admission_point() -> None:
    """Installing an access path cannot erase a configured physical-operator refusal."""
    stack = build_query_stack(
        vector_nullable=False,
        vector_exact_scan_threshold=32,
        max_intermediate_rows=2,
    )
    _seed(stack, count=4)
    with pytest.raises(GrafxQueryBudgetExceeded) as raised:
        stack.engine.execute(
            DIRECT,
            _transaction(stack),
            {"q": [1.0, 0.0, 0.0, 0.0]},
        )
    assert raised.value.details["field"] == "max_intermediate_rows"
    assert raised.value.details["operator"] == "NodeScan"


def test_empty_child_keeps_its_pre_argument_short_circuit() -> None:
    """The optional index proof must not make an empty query validate unused arguments."""
    stack = build_query_stack(vector_nullable=False, vector_exact_scan_threshold=32)
    result = stack.engine.execute(
        DIRECT,
        _transaction(stack),
        {"q": "not a vector"},
    )
    assert result.rows == ()
    assert "vector_direct_accesses" not in result.statistics


def test_stale_index_declines_the_shortcut_then_keeps_the_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable proof never turns a stale subset into an authoritative access path."""
    stack = build_query_stack(
        vector_nullable=False,
        vector_exact_scan_threshold=32,
    )
    _seed(stack, count=4)
    stack.vectors.index("minilm_v2").mark_stale("forced by access-path test")
    scanned: list[RecordRef] = []
    original_scan = HeapStore.scan

    def counted_scan(
        self: HeapStore, table: TableDef, snapshot: object
    ) -> Iterator[tuple[RecordRef, HeapVersion]]:
        """Record rows emitted by the canonical child after the direct proof declines."""
        for ref, version in original_scan(self, table, snapshot):  # type: ignore[arg-type]
            scanned.append(ref)
            yield ref, version

    monkeypatch.setattr(HeapStore, "scan", counted_scan)

    with pytest.raises(GrafxIndexError) as raised:
        stack.engine.execute(
            DIRECT,
            _transaction(stack),
            {"q": [1.0, 0.0, 0.0, 0.0]},
        )
    assert raised.value.details["field"] == "stale"
    assert len(scanned) == 4


@pytest.mark.parametrize("failure", ["missing", "wrong_table"])
def test_a_direct_hit_with_no_valid_heap_witness_fails_closed(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """The ref carried by an ANN hit is a proof to revalidate, never an unchecked locator."""
    stack = build_query_stack(
        vector_nullable=False,
        vector_exact_scan_threshold=32,
    )
    _seed(stack, count=4)
    person_ref = stack.insert("Person", 99, (99, "Ada", 36, "London"), csn=1)
    original = VectorEngine.search

    def broken_search(self: VectorEngine, **arguments: object) -> object:
        """Replace one otherwise valid hit reference with a missing/foreign witness."""
        result = original(self, **arguments)  # type: ignore[arg-type]
        bad_ref = RecordRef(page=10_000, slot=1) if failure == "missing" else person_ref
        first = replace(result.hits[0], ref=bad_ref)
        return replace(result, hits=(first, *result.hits[1:]))

    monkeypatch.setattr(VectorEngine, "search", broken_search)
    with pytest.raises(GrafxCorruptionDetected):
        stack.engine.execute(
            DIRECT,
            _transaction(stack),
            {"q": [1.0, 0.0, 0.0, 0.0]},
        )


@pytest.mark.parametrize("failure", ["wrong_record", "invisible"])
def test_a_direct_hit_with_a_corrupt_or_invisible_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A decodable ref still has to prove the hit's identity and snapshot visibility."""
    stack = build_query_stack(
        vector_nullable=False,
        vector_exact_scan_threshold=32,
    )
    _seed(stack, count=4)
    bad_ref = stack.insert(
        "Chunk",
        99,
        (99, 1, vector((0.0, 0.0, 0.0, 1.0))),
        csn=1 if failure == "wrong_record" else FIXTURE_READ_LSN + 1,
    )
    original = VectorEngine.search

    def broken_search(self: VectorEngine, **arguments: object) -> object:
        """Replace one otherwise valid hit with a decodable but invalid witness."""
        result = original(self, **arguments)  # type: ignore[arg-type]
        first = replace(
            result.hits[0],
            ref=bad_ref,
            record_id=99 if failure == "invisible" else result.hits[0].record_id,
        )
        return replace(result, hits=(first, *result.hits[1:]))

    monkeypatch.setattr(VectorEngine, "search", broken_search)
    expected = GrafxCorruptionDetected if failure == "wrong_record" else GrafxIndexError
    with pytest.raises(expected) as raised:
        stack.engine.execute(
            DIRECT,
            _transaction(stack),
            {"q": [1.0, 0.0, 0.0, 0.0]},
        )
    assert raised.value.details["field"] == (
        "record_id" if failure == "wrong_record" else "vector_hit_visibility"
    )


def test_a_mutable_direct_hit_cannot_switch_the_row_staged_for_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reference checked at the adapter boundary is the exact one a write consumes."""
    stack = build_query_stack(
        vector_nullable=False,
        vector_exact_scan_threshold=32,
    )
    _seed(stack, count=4)
    table = stack.table("Chunk")
    visible_refs = tuple(
        ref for ref, _version in stack.heap.scan(table, Snapshot(FIXTURE_READ_LSN))
    )
    original = VectorEngine.search
    exposed: list[object] = []

    class SwitchingHit:
        """Return a different valid ref on a second read, exposing a check/use split."""

        def __init__(
            self,
            *,
            validated_ref: RecordRef,
            substituted_ref: RecordRef,
            record_id: int,
            score: float,
        ) -> None:
            self.validated_ref = validated_ref
            self.substituted_ref = substituted_ref
            self._record_id = record_id
            self._score = score
            self.ref_reads = 0
            self.record_id_reads = 0
            self.score_reads = 0

        @property
        def ref(self) -> RecordRef:
            self.ref_reads += 1
            if self.ref_reads == 1:
                return self.validated_ref
            return self.substituted_ref

        @property
        def record_id(self) -> int:
            self.record_id_reads += 1
            return self._record_id

        @property
        def score(self) -> float:
            self.score_reads += 1
            return self._score

    def switching_search(self: VectorEngine, **arguments: object) -> object:
        """Wrap the one requested hit in a stateful adapter-boundary object."""
        result = original(self, **arguments)  # type: ignore[arg-type]
        source = result.hits[0]
        substituted_ref = next(ref for ref in visible_refs if ref != source.ref)
        mutable = SwitchingHit(
            validated_ref=source.ref,
            substituted_ref=substituted_ref,
            record_id=source.record_id,
            score=source.score,
        )
        exposed.append(mutable)
        return replace(result, hits=(mutable,))  # type: ignore[arg-type]

    monkeypatch.setattr(VectorEngine, "search", switching_search)
    transaction = _transaction(stack)
    result = stack.engine.execute(
        "MATCH (n:Chunk) "
        "WHERE similarity(n.embedding, $q, space => 'minilm_v2') > -2.0 "
        "DELETE n "
        "RETURN n.id, similarity_score() AS score ORDER BY score DESC LIMIT 1",
        transaction,
        {"q": [1.0, 0.0, 0.0, 0.0]},
    )

    assert result.statistics["vector_direct_accesses"] == 1
    assert len(transaction.row_intents) == 1
    mutable = exposed[0]
    assert isinstance(mutable, SwitchingHit)
    assert transaction.row_intents[0].reference == mutable.validated_ref
    assert transaction.row_intents[0].reference != mutable.substituted_ref
    assert mutable.ref_reads == 1
    assert mutable.record_id_reads == 1
    assert mutable.score_reads == 1


def test_cold_reopen_keeps_the_direct_approximate_path(tmp_path: Path) -> None:
    """Both the cardinality cache and HNSW picture may be cold without widening trust."""
    root = tmp_path / "database"
    with okto_grafx.connect(root, vector_exact_scan_threshold=0) as database:
        with database.begin("write") as txn:
            txn.execute(
                "CREATE VECTOR SPACE minilm_v2 {dimension: 4, metric: 'cosine'}"
            )
            txn.execute(
                "CREATE NODE TABLE Chunk("
                "id INT64, layer INT64, embedding VECTOR(minilm_v2), PRIMARY KEY(id))"
            )
        with database.begin("write") as txn:
            for record_id in range(1, 5):
                txn.execute(
                    "CREATE (:Chunk {id: $id, layer: 1, embedding: $embedding})",
                    {
                        "id": record_id,
                        "embedding": [1.0, record_id / 10.0, 0.0, 0.0],
                    },
                )

    with okto_grafx.connect(root, vector_exact_scan_threshold=0) as reopened:
        with reopened.begin("read") as reader:
            result = reader.execute(
                DIRECT,
                {"q": [1.0, 0.0, 0.0, 0.0]},
            )
        assert len(result.rows) == 3
        assert result.statistics["vector_regime_exact"] == 0
        assert result.statistics["vector_direct_accesses"] == 1
        assert result.statistics["vector_rows_materialized"] == 3
        assert reopened.verify("all").findings == ()


def test_reader_that_becomes_historical_falls_back_and_keeps_its_snapshot(
    tmp_path: Path,
) -> None:
    """A concurrent commit advances the frontier, never the rows of an open reader."""
    root = tmp_path / "database"
    with okto_grafx.connect(root, vector_exact_scan_threshold=0) as database:
        with database.begin("write") as txn:
            txn.execute(
                "CREATE VECTOR SPACE minilm_v2 {dimension: 4, metric: 'cosine'}"
            )
            txn.execute(
                "CREATE NODE TABLE Chunk("
                "id INT64, layer INT64, embedding VECTOR(minilm_v2), PRIMARY KEY(id))"
            )
        with database.begin("write") as txn:
            for record_id in range(1, 4):
                txn.execute(
                    "CREATE (:Chunk {id: $id, layer: 1, embedding: $embedding})",
                    {
                        "id": record_id,
                        "embedding": [1.0, record_id / 10.0, 0.0, 0.0],
                    },
                )

        old_reader = database.begin("read")
        try:
            with database.begin("write") as writer:
                writer.execute(
                    "CREATE (:Chunk {id: 4, layer: 1, embedding: $embedding})",
                    {"embedding": [1.0, 0.0, 0.0, 0.0]},
                )

            old_result = old_reader.execute(
                DIRECT,
                {"q": [1.0, 0.0, 0.0, 0.0]},
            )
            assert 4 not in {record_id for record_id, _score in old_result.rows}
            assert old_result.statistics["rows_scanned"] == 3
            assert "vector_direct_accesses" not in old_result.statistics

            with database.begin("read") as current_reader:
                current_result = current_reader.execute(
                    DIRECT,
                    {"q": [1.0, 0.0, 0.0, 0.0]},
                )
            assert current_result.statistics["vector_direct_accesses"] == 1
        finally:
            old_reader.rollback()


def test_commit_after_frontier_proof_cannot_enter_the_fixed_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer between the count proof and search cannot widen a hit-driven reader."""
    root = tmp_path / "database"
    with okto_grafx.connect(root, vector_exact_scan_threshold=0) as database:
        with database.begin("write") as txn:
            txn.execute(
                "CREATE VECTOR SPACE minilm_v2 {dimension: 4, metric: 'cosine'}"
            )
            txn.execute(
                "CREATE NODE TABLE Chunk("
                "id INT64, layer INT64, embedding VECTOR(minilm_v2), PRIMARY KEY(id))"
            )
        with database.begin("write") as txn:
            for record_id in range(1, 4):
                txn.execute(
                    "CREATE (:Chunk {id: $id, layer: 1, embedding: $embedding})",
                    {
                        "id": record_id,
                        "embedding": [1.0, record_id / 10.0, 0.0, 0.0],
                    },
                )

        original = VectorEngine.snapshot_frontier_live_count
        committed = False

        def count_then_commit(
            self: VectorEngine,
            space: str,
            snapshot: object,
            *,
            table_id: int,
            position: int,
        ) -> int | None:
            """Advance the index only after the old frontier count was certified."""
            nonlocal committed
            count = original(
                self,
                space,
                snapshot,  # type: ignore[arg-type]
                table_id=table_id,
                position=position,
            )
            if self is database._vectors and not committed:
                committed = True
                with database.begin("write") as writer:
                    writer.execute(
                        "CREATE (:Chunk {id: 4, layer: 1, embedding: $embedding})",
                        {"embedding": [1.0, 0.0, 0.0, 0.0]},
                    )
            return count

        monkeypatch.setattr(
            VectorEngine, "snapshot_frontier_live_count", count_then_commit
        )
        with database.begin("read") as reader:
            result = reader.execute(
                DIRECT,
                {"q": [1.0, 0.0, 0.0, 0.0]},
            )

        assert committed is True
        assert result.statistics["vector_direct_accesses"] == 1
        assert 4 not in {record_id for record_id, _score in result.rows}
