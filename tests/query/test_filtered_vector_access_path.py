"""Focused contract for the Pulse-shaped filtered-vector access path.

The optimization under test is deliberately narrower than arbitrary predicate pushdown.  Pulse
searches one node table, rejects NULL embeddings first, applies total row-local eligibility
predicates, and asks for a bounded similarity page.  A catalog-v2 identity index can turn each
record id visited by the vector engine back into the exact snapshot-visible ``RowBinding`` without
materialising the complete ``FilterRows(NodeScan(SingleRow))`` child first.

These tests keep the fallback doors just as visible as the fast door.  An unsupported predicate,
a structural or historical snapshot, an intermediate-row budget, catalog v1, or a stale identity
generation must execute the canonical child.  The first access-path test is intentionally a red
contract until the corresponding source optimization lands.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxQueryBudgetExceeded
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index.catalog import identity_index_name
from okto_grafx.domain.model.record import HeapVersion
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.query.plan import FilterRows, NodeScan, SingleRow, VectorSearch
from okto_grafx.engine import query_engine as query_engine_module
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.vector_engine import VectorHnswIndex

from .stack import SnapshotDouble


SPACE = "pulse_v1"
TABLE = "Chunk"
QUERY_VECTOR = [1.0, 0.0, 0.0, 0.0]

PULSE_FILTER = (
    "n.embedding IS NOT NULL "
    "AND ($include_superseded = true OR n.superseded_by IS NULL) "
    "AND ($graph_layer = 'all' OR n.graph_layer = $graph_layer) "
    "AND coalesce(n.revocation_reason, '') <> 'projection_removed' "
    "AND coalesce(n.revocation_reason, '') <> 'source_deleted'"
)

PULSE_QUERY = (
    f"MATCH (n:{TABLE}) WHERE {PULSE_FILTER} "
    f"AND similarity(n.embedding, $query, space => '{SPACE}') >= $threshold "
    "RETURN n.id, similarity_score() AS score "
    "ORDER BY score DESC LIMIT $search_k"
)

# A semantically neutral CASE keeps the same result but is intentionally outside the narrow,
# total Pulse predicate grammar.  It proves that arbitrary closed predicates do not become
# callbacks merely because they happen to read only the vector variable.
ARBITRARY_QUERY = PULSE_QUERY.replace(
    "n.embedding IS NOT NULL ",
    "n.embedding IS NOT NULL "
    "AND CASE WHEN n.id IS NULL THEN false ELSE true END ",
    1,
)

PARAMETERS = {
    "include_superseded": False,
    "graph_layer": "canonical",
    "query": QUERY_VECTOR,
    "threshold": -1.0,
    "search_k": 3,
}

ROWS: tuple[tuple[str, str, str | None, str | None, list[float] | None], ...] = (
    ("best", "canonical", None, None, [1.0, 0.0, 0.0, 0.0]),
    ("second", "canonical", None, None, [0.8, 0.6, 0.0, 0.0]),
    # Each remaining row is rejected by one independent Pulse eligibility arm.  Some carry a
    # better vector than ``second`` so a post-filtered top-k implementation would be exposed.
    ("working", "working", None, None, [0.99, 0.01, 0.0, 0.0]),
    ("superseded", "canonical", "best", None, [0.98, 0.02, 0.0, 0.0]),
    ("projection", "canonical", None, "projection_removed", [0.97, 0.03, 0.0, 0.0]),
    ("source", "canonical", None, "source_deleted", [0.96, 0.04, 0.0, 0.0]),
    ("without-vector", "canonical", None, None, None),
)


def _open_database(
    path: Path,
    *,
    catalog_v2: bool = True,
    max_intermediate_rows: int | None = None,
    vector_exact_scan_threshold: int = 4096,
) -> okto_grafx.Database:
    """Create one realistic Pulse-shaped table and populate all filter arms."""

    database = okto_grafx.connect(
        path,
        vector_exact_scan_threshold=vector_exact_scan_threshold,
        max_intermediate_rows=max_intermediate_rows,
    )
    try:
        # Activating an empty database before DDL is the Pulse candidate contract: every table
        # subsequently created receives its RecordId identity generation without migrating an
        # existing path behind a reader.
        if catalog_v2:
            database.ensure_identity_indexes()
        with database.begin("write") as schema:
            schema.execute(
                f"CREATE VECTOR SPACE {SPACE} {{dimension: 4, metric: 'cosine'}}"
            )
            schema.execute(
                f"CREATE NODE TABLE {TABLE}("
                "id STRING, graph_layer STRING, superseded_by STRING, "
                "revocation_reason STRING, "
                f"embedding VECTOR({SPACE}), PRIMARY KEY(id))"
            )
            # Identity generations intentionally cover relationship endpoints.  Pulse vector
            # node types are graph participants, so keep the fixture in that same domain.
            schema.execute(f"CREATE REL TABLE Related(FROM {TABLE} TO {TABLE})")
        if catalog_v2:
            # Empty-database activation establishes catalog v2.  The second idempotent call is
            # what creates the identity generation for the table that now exists.
            database.ensure_identity_indexes()
        with database.begin("write") as writer:
            for identifier, layer, superseded_by, revocation_reason, embedding in ROWS:
                writer.execute(
                    f"CREATE (:{TABLE} {{"
                    "id: $id, graph_layer: $layer, superseded_by: $superseded_by, "
                    "revocation_reason: $revocation_reason, embedding: $embedding})",
                    {
                        "id": identifier,
                        "layer": layer,
                        "superseded_by": superseded_by,
                        "revocation_reason": revocation_reason,
                        "embedding": embedding,
                    },
                )
    except BaseException:
        database.close()
        raise
    return database


def _ids(result: object) -> tuple[str, ...]:
    rows = getattr(result, "rows")
    return tuple(row[0] for row in rows)


def _assert_canonical_scan(result: object) -> None:
    statistics = getattr(result, "statistics")
    assert statistics["rows_scanned"] == len(ROWS)


def test_pulse_query_has_the_single_table_filtered_vector_shape(tmp_path: Path) -> None:
    database = _open_database(tmp_path / "shape")
    try:
        plan = database.explain(PULSE_QUERY)
        search = next(node for node in plan.walk() if type(node) is VectorSearch)

        assert type(search.child) is FilterRows
        assert type(search.child.child) is NodeScan
        assert type(search.child.child.child) is SingleRow
        assert search.variable == search.child.child.variable == "n"
        assert search.bounded
        assert search.property_key == "embedding"
    finally:
        database.close()


def test_exact_pulse_filter_avoids_child_materialisation_and_preserves_ranking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selective default-threshold filter is exact without a preceding heap scan."""

    database = _open_database(tmp_path / "exact-fast")
    try:
        canonical = database.execute(ARBITRARY_QUERY, PARAMETERS)
        _assert_canonical_scan(canonical)
        assert _ids(canonical) == ("best", "second")

        original_scan = HeapStore.scan

        def no_candidate_scan(
            self: HeapStore,
            table: TableDef,
            snapshot: object,
        ) -> Iterator[tuple[object, HeapVersion]]:
            if self is database._heap and table.name == TABLE:  # noqa: SLF001
                pytest.fail("eligible filtered vector search materialised its NodeScan child")
            yield from original_scan(self, table, snapshot)  # type: ignore[misc]

        monkeypatch.setattr(HeapStore, "scan", no_candidate_scan)
        accelerated = database.execute(PULSE_QUERY, PARAMETERS)

        assert accelerated.rows == canonical.rows
        assert accelerated.statistics["vector_regime_exact"] == 1
        assert "rows_scanned" not in accelerated.statistics
    finally:
        database.close()


def test_approximate_pulse_filter_is_lazy_and_preserves_filtered_hnsw_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crossing the threshold must not rebuild the admitted ids as an O(N) set."""

    database = _open_database(
        tmp_path / "approximate-fast",
        vector_exact_scan_threshold=0,
    )
    try:
        canonical = database.execute(ARBITRARY_QUERY, PARAMETERS)
        _assert_canonical_scan(canonical)
        assert canonical.statistics["vector_regime_exact"] == 0

        original_scan = HeapStore.scan

        def no_candidate_scan(
            self: HeapStore,
            table: TableDef,
            snapshot: object,
        ) -> Iterator[tuple[object, HeapVersion]]:
            if self is database._heap and table.name == TABLE:  # noqa: SLF001
                pytest.fail("eligible approximate vector filter materialised its NodeScan child")
            yield from original_scan(self, table, snapshot)  # type: ignore[misc]

        monkeypatch.setattr(HeapStore, "scan", no_candidate_scan)
        accelerated = database.execute(PULSE_QUERY, PARAMETERS)

        assert accelerated.rows == canonical.rows
        assert accelerated.statistics["vector_regime_exact"] == 0
        assert accelerated.statistics["vector_filtered_accesses"] == 1
        assert "rows_scanned" not in accelerated.statistics
    finally:
        database.close()


def test_empty_pulse_filter_keeps_query_argument_and_identity_short_circuit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proven empty filtered set does not validate an unused malformed query vector."""

    database = _open_database(tmp_path / "empty-filter")
    try:
        def unexpected_identity_preflight(*_args: object, **_kwargs: object) -> object:
            pytest.fail("an empty filtered proof consulted an unused identity index")

        monkeypatch.setattr(
            query_engine_module,
            "_endpoint_identity_index",
            unexpected_identity_preflight,
        )
        result = database.execute(
            PULSE_QUERY,
            {
                **PARAMETERS,
                "graph_layer": "no-such-layer",
                "query": "not-a-vector",
            },
        )

        assert result.rows == ()
        assert "vector_filtered_accesses" not in result.statistics
        assert "rows_scanned" not in result.statistics
    finally:
        database.close()


def test_approximate_filter_refuses_vector_identity_ref_divergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale HNSW bridge ref is refused even when that row would not be returned."""

    database = _open_database(
        tmp_path / "ref-divergence",
        vector_exact_scan_threshold=0,
    )
    original = query_engine_module._visible_identity_with_ref

    def mismatched_identity(
        engine: object,
        context: object,
        table: TableDef,
        record_id: int,
    ) -> tuple[RecordRef, HeapVersion] | None:
        resolved = original(engine, context, table, record_id)  # type: ignore[arg-type]
        if resolved is None:
            return None
        ref, version = resolved
        if version.values[0] == "working":
            # This high-scoring row is rejected by graph_layer. Detecting its divergent ref proves
            # authentication happens during admission, not only during hit materialisation.
            return RecordRef(page=ref.page + 1, slot=ref.slot), version
        return resolved

    try:
        monkeypatch.setattr(
            query_engine_module,
            "_visible_identity_with_ref",
            mismatched_identity,
        )

        with pytest.raises(GrafxCorruptionDetected) as refused:
            database.execute(PULSE_QUERY, PARAMETERS)

        assert refused.value.details["field"] == "vector_filter_identity"
        assert "identity_page" in refused.value.details
    finally:
        database.close()


def test_large_filtered_k_falls_back_before_widening_hnsw(
    tmp_path: Path,
) -> None:
    """The private fast path keeps its ef bound; canonical materialisation clamps large k."""

    database = _open_database(
        tmp_path / "large-k-fallback",
        vector_exact_scan_threshold=0,
    )
    try:
        result = database.execute(PULSE_QUERY, {**PARAMETERS, "search_k": 100_000})

        assert _ids(result) == ("best", "second")
        _assert_canonical_scan(result)
        assert "vector_filtered_accesses" not in result.statistics
    finally:
        database.close()


def test_preexisting_approximate_search_keeps_legacy_index_call_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy VectorHnswIndex adapter need not accept the private ref-aware keyword."""

    database = _open_database(
        tmp_path / "legacy-search-seam",
        vector_exact_scan_threshold=0,
    )
    original = VectorHnswIndex.search
    calls = 0

    def legacy_search(
        self: VectorHnswIndex,
        query: object,
        k: int,
        snapshot: object,
        admits: object = None,
        *,
        ef: int | None = None,
    ) -> object:
        """Expose the pre-batch signature and delegate to the built-in implementation."""
        nonlocal calls
        calls += 1
        return original(self, query, k, snapshot, admits, ef=ef)  # type: ignore[arg-type]

    try:
        monkeypatch.setattr(VectorHnswIndex, "search", legacy_search)
        result = database.execute(ARBITRARY_QUERY, PARAMETERS)

        assert _ids(result) == ("best", "second")
        assert calls == 1
    finally:
        database.close()


def test_header_ref_walk_is_complete_beyond_bucket_count(tmp_path: Path) -> None:
    """The low-allocation ref walk returns every live entry, not one per bucket."""

    database = _open_database(tmp_path / "multi-entry-buckets")
    try:
        with database.begin("write") as writer:
            for offset in range(80):
                writer.execute(
                    f"CREATE (:{TABLE} {{"
                    "id: $id, graph_layer: 'canonical', superseded_by: null, "
                    "revocation_reason: null, embedding: $embedding})",
                    {"id": f"bulk-{offset:03d}", "embedding": QUERY_VECTOR},
                )

        index = database._vectors.index(SPACE)  # noqa: SLF001
        refs = index._entry_refs_from_headers()  # noqa: SLF001
        visited: list[RecordRef] = []

        def retain_every_ref(ref: RecordRef) -> bool:
            """Collect the complete incremental sequence."""
            visited.append(ref)
            return True

        exhausted = index._visit_entry_refs_until(retain_every_ref)  # noqa: SLF001
        stopped: list[RecordRef] = []
        stop_after = 65

        def stop_at_bound(ref: RecordRef) -> bool:
            """Stop after crossing more entries than the bucket directory holds."""
            stopped.append(ref)
            return len(stopped) < stop_after

        reached_bound = index._visit_entry_refs_until(stop_at_bound)  # noqa: SLF001

        assert index.live_count() > index.definition.bucket_count
        assert len(refs) == index.live_count()
        assert len({ref.encode() for ref in refs}) == len(refs)
        assert exhausted is True
        assert visited == list(refs)
        assert reached_bound is False
        assert stopped == list(refs[:stop_after])
    finally:
        database.close()


def test_arbitrary_row_local_predicate_keeps_the_canonical_child(tmp_path: Path) -> None:
    database = _open_database(tmp_path / "arbitrary")
    try:
        result = database.execute(ARBITRARY_QUERY, PARAMETERS)

        assert _ids(result) == ("best", "second")
        _assert_canonical_scan(result)
    finally:
        database.close()


def test_structural_snapshot_keeps_the_canonical_child(tmp_path: Path) -> None:
    database = _open_database(tmp_path / "structural-snapshot")
    reader = database.begin("read")
    try:
        native = reader._context.snapshot  # noqa: SLF001
        reader._context._snapshot = SnapshotDouble(native.read_lsn)  # noqa: SLF001

        result = reader.execute(PULSE_QUERY, PARAMETERS)

        assert _ids(result) == ("best", "second")
        _assert_canonical_scan(result)
    finally:
        reader.rollback()
        database.close()


def test_historical_snapshot_keeps_the_canonical_child(tmp_path: Path) -> None:
    database = _open_database(tmp_path / "historical")
    reader = database.begin("read")
    try:
        with database.begin("write") as writer:
            writer.execute(
                f"CREATE (:{TABLE} {{"
                "id: 'later', graph_layer: 'canonical', superseded_by: null, "
                "revocation_reason: null, embedding: $embedding})",
                {"embedding": QUERY_VECTOR},
            )

        result = reader.execute(PULSE_QUERY, PARAMETERS)

        assert _ids(result) == ("best", "second")
        _assert_canonical_scan(result)
    finally:
        reader.rollback()
        database.close()


def test_intermediate_row_budget_retains_node_scan_admission(tmp_path: Path) -> None:
    database = _open_database(
        tmp_path / "budget",
        max_intermediate_rows=2,
    )
    try:
        with pytest.raises(GrafxQueryBudgetExceeded) as refused:
            database.execute(PULSE_QUERY, PARAMETERS)

        assert refused.value.details["field"] == "max_intermediate_rows"
        assert refused.value.details["operator"] == "NodeScan"
    finally:
        database.close()


def test_catalog_v1_keeps_the_canonical_child(tmp_path: Path) -> None:
    database = _open_database(tmp_path / "catalog-v1", catalog_v2=False)
    try:
        result = database.execute(PULSE_QUERY, PARAMETERS)

        assert _ids(result) == ("best", "second")
        _assert_canonical_scan(result)
    finally:
        database.close()


def test_stale_identity_generation_keeps_the_canonical_child(tmp_path: Path) -> None:
    database = _open_database(tmp_path / "stale-identity")
    try:
        table = database._catalog.catalog.table(TABLE)  # noqa: SLF001
        identity = database._indexes.index(  # noqa: SLF001
            identity_index_name(table.table_id)
        )
        identity.mark_stale("forced by filtered-vector fallback test", persist=False)

        result = database.execute(PULSE_QUERY, PARAMETERS)

        assert _ids(result) == ("best", "second")
        _assert_canonical_scan(result)
    finally:
        database.close()
