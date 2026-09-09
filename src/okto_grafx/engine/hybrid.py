"""Native FTS/vector fusion on one reader, without an embedding provider or second authority."""

from __future__ import annotations
from dataclasses import asdict
from typing import TYPE_CHECKING

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
    GrafxQueryBudgetExceeded,
    GrafxCorruptionDetected,
)
from okto_grafx.domain.index.fulltext import TextSearchLimits
from okto_grafx.domain.query.hybrid import (
    HybridHit,
    HybridSearchOptions,
    HybridSearchResult,
)
from okto_grafx.domain.query.control import CancellationToken, _read_control
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.domain.index.catalog import identity_index_name
from okto_grafx.domain.index.keys import record_id_key
from okto_grafx.domain.index.definition import RECORD_ID_KEY_DERIVATION
from okto_grafx.engine.public_views import _vector_query_snapshot

__all__: list[str] = []

if TYPE_CHECKING:
    from collections.abc import Sequence
    from okto_grafx.engine.database import Database, Transaction


def search_hybrid(
    database: Database,
    reader: Transaction,
    *,
    table: str,
    index: str | None,
    query: str,
    space: str | None,
    vector: Sequence[float],
    k: int,
    options: HybridSearchOptions | None,
    filter: RecordIdFilter | None,
    text_limits: TextSearchLimits | None,
    timeout_seconds: float | None,
    cancellation: CancellationToken | None,
) -> HybridSearchResult:
    """Fuse fixed candidate windows; fail-closed by default, missing sources opt-in only."""
    if reader._database is not database or not reader.active or reader.mode != "read":
        raise GrafxTransactionStateError(
            "Hybrid search requires an owned active reader.", operation="search_hybrid"
        )
    if type(options) not in (HybridSearchOptions, type(None)):
        raise GrafxConfigurationError("Expected HybridSearchOptions.", field="options")
    selected = (
        HybridSearchOptions()
        if options is None
        else HybridSearchOptions(**asdict(options))
    )
    if (
        type(table) is not str
        or type(k) is not int
        or not 1 <= k <= selected.candidate_k
    ):
        raise GrafxConfigurationError("Require table and 1<=k<=candidate_k.", field="k")
    if (
        type(query) is not str
        or (index is not None and type(index) is not str)
        or (space is not None and type(space) is not str)
    ):
        raise GrafxConfigurationError("Invalid hybrid source selector.", field="source")
    if filter is not None and (
        type(filter) is not RecordIdFilter or type(filter.record_ids) is not frozenset
    ):
        raise GrafxConfigurationError(
            "Expected immutable RecordIdFilter.", field="filter"
        )
    allowed = None if filter is None else filter.record_ids
    if allowed is not None and any(
        type(v) is not int or not 0 < v < 2**64 - 1 for v in allowed
    ):
        raise GrafxConfigurationError("Invalid allowed identity.", field="filter")
    control = _read_control(database._clock, timeout_seconds, cancellation)
    retained = (
        (0 if allowed is None else len(allowed) * 64)
        + selected.candidate_k * 1024
        + len(selected.graph_seeds) * 128
    )

    def check() -> None:
        """Cooperatively bound graph retention and phase transitions, including empty answers."""
        if retained > selected.max_memory_bytes:
            raise GrafxQueryBudgetExceeded(
                "Hybrid retention budget exceeded.", resource="hybrid_memory"
            )
        if control is not None:
            control.check()

    # Native capture enforces finite components and dimension bounds before coordination.
    wanted_vector = _vector_query_snapshot(vector) if selected.vector_weight else ()
    with database._public_operation("search_hybrid"):
        with database._transactions.page_access_section(transaction=reader._context):
            if (
                reader._database is not database
                or not reader.active
                or reader.mode != "read"
            ):
                raise GrafxTransactionStateError(
                    "Hybrid search requires an owned active reader.",
                    operation="search_hybrid",
                )
            reader._require_batch_idle("search_hybrid")
            check()
            catalog = database._catalog.catalog
            target = catalog.table(table)
            if target.kind != "node":
                raise GrafxConfigurationError(
                    "Hybrid target must be a node table.", field="table"
                )
            lexical = {}
            vectors = {}
            errors = []
            lexical_coverage = None
            lexical_regime = vector_regime = "disabled"
            if selected.lexical_weight:
                if index is None or not catalog.has_index_definition(index):
                    errors.append(("lexical", "missing_index"))
                    lexical_regime = "unavailable"
                else:
                    definition = catalog.index_definition(index)
                    if definition.table_id != target.table_id:
                        raise GrafxConfigurationError(
                            "FTS belongs to another table.", field="index"
                        )
                    from okto_grafx.engine.fulltext import search_text

                    result = search_text(
                        database,
                        reader,
                        index=index,
                        query=query,
                        k=selected.candidate_k,
                        filter=filter,
                        limits=text_limits,
                        k1=1.2,
                        b=0.75,
                        timeout_seconds=None,
                        cancellation=None,
                        _control=control,
                    )
                    lexical = {
                        hit.record_id: (rank, hit.score)
                        for rank, hit in enumerate(result.hits, 1)
                    }
                    lexical_regime = result.regime
                    lexical_coverage = result.index_built_through_commit
            check()
            if selected.vector_weight:
                owners = [
                    (t.table_id, c.name)
                    for t in catalog.tables()
                    for c in t.columns
                    if space is not None and c.vector_space == space
                ]
                if not owners:
                    errors.append(("vector", "missing_space_binding"))
                    vector_regime = "unavailable"
                elif len(owners) != 1 or owners[0][0] != target.table_id:
                    raise GrafxConfigurationError(
                        "Hybrid vector space must bind once to the target table.",
                        field="space",
                    )
                else:
                    result = database.search_vectors(
                        reader,
                        space=space,
                        query=wanted_vector,
                        k=selected.candidate_k,
                        candidate_filter=filter,
                    )
                    for rank, hit in enumerate(result.hits, 1):
                        if hit.record_id in vectors:
                            raise GrafxCorruptionDetected(
                                "Duplicate hybrid vector identity.", field="record_id"
                            )
                        vectors[hit.record_id] = (rank, hit.score)
                    vector_regime = result.regime
            check()
            if errors and (
                not selected.allow_partial
                or (
                    lexical_regime in ("unavailable", "disabled")
                    and vector_regime in ("unavailable", "disabled")
                )
            ):
                raise GrafxUnsupportedOperation(
                    "A requested hybrid source is unavailable.", sources=tuple(errors)
                )
            sources = [
                set(hits)
                for hits, regime in (
                    (lexical, lexical_regime),
                    (vectors, vector_regime),
                )
                if regime not in ("unavailable", "disabled")
            ]
            ids = (
                set.union(*sources)
                if selected.fusion == "union"
                else set.intersection(*sources)
            )
            distance = {}
            visited = 0
            if selected.graph_weight or selected.graph_filter:
                if allowed is not None and not set(selected.graph_seeds) <= allowed:
                    raise GrafxConfigurationError(
                        "Graph seeds must satisfy the candidate filter.",
                        field="graph_seeds",
                    )
                adjacency = {}
                identity_name = identity_index_name(target.table_id)
                if not catalog.has_index_definition(identity_name):
                    raise GrafxUnsupportedOperation(
                        "Graph evidence requires ensure_identity_indexes() before reading.",
                        field="identity_index",
                    )
                identity = database._indexes.active_index(identity_name)
                if (
                    identity.definition.table_id != target.table_id
                    or identity.definition.key_derivation != RECORD_ID_KEY_DERIVATION
                ):
                    raise GrafxCorruptionDetected(
                        "Invalid hybrid identity index.", field="identity_index"
                    )
                seed_counts = database._indexes.validated_identity_counts_many(
                    identity,
                    tuple(record_id_key(rid) for rid in selected.graph_seeds),
                    reader._context.snapshot,
                )
                if any(count != 1 for count in seed_counts):
                    raise GrafxConfigurationError(
                        "Every graph seed must name one visible target row.",
                        field="graph_seeds",
                    )
                for name in selected.graph_relations:
                    relation = catalog.table(name)
                    if (
                        relation.kind != "rel"
                        or relation.from_table != table
                        or relation.to_table != table
                    ):
                        raise GrafxConfigurationError(
                            "Hybrid graph relations must join the target table to itself.",
                            field="graph_relations",
                        )
                    cursor = None
                    while True:
                        page = reader.scan_rows_v1(
                            name,
                            limit=min(128, selected.max_graph_edges - visited + 1),
                            cursor=cursor,
                        )
                        # Validate bounded endpoint batches through native exact certificates;
                        # raw relationship rows alone are not proof of visible graph nodes.
                        endpoints = tuple(
                            dict.fromkeys(
                                rid for row in page.rows for rid in row.values[:2]
                            )
                        )
                        counts = database._indexes.validated_identity_counts_many(
                            identity,
                            tuple(record_id_key(rid) for rid in endpoints),
                            reader._context.snapshot,
                        )
                        if any(count != 1 for count in counts):
                            raise GrafxCorruptionDetected(
                                "Hybrid relationship has missing/duplicate visible endpoints.",
                                field="graph_endpoint",
                            )
                        for row in page.rows:
                            visited += 1
                            if visited > selected.max_graph_edges:
                                raise GrafxQueryBudgetExceeded(
                                    "Hybrid edge budget exceeded.",
                                    resource="hybrid_edges",
                                )
                            a, b = row.values[:2]
                            if allowed is not None and (
                                a not in allowed or b not in allowed
                            ):
                                continue
                            for start, end in (
                                ((a, b),)
                                if selected.graph_direction == "out"
                                else (
                                    ((b, a),)
                                    if selected.graph_direction == "in"
                                    else ((a, b), (b, a))
                                )
                            ):
                                adjacency.setdefault(start, set()).add(end)
                                retained += 128
                            check()
                        cursor = page.next_cursor
                        if cursor is None:
                            break
                distance = {rid: 0 for rid in selected.graph_seeds}
                frontier = set(distance)
                for depth in range(1, selected.graph_hops + 1):
                    following = {
                        end
                        for start in frontier
                        for end in adjacency.get(start, ())
                        if end not in distance
                    }
                    retained += len(following) * 128
                    check()
                    distance.update((rid, depth) for rid in following)
                    frontier = following
                if selected.graph_filter:
                    ids &= distance.keys()
            hits = []
            for rid in ids:
                check()
                lr, ls = lexical.get(rid, (None, None))
                vr, vs = vectors.get(rid, (None, None))
                depth = distance.get(rid)
                score = (
                    (selected.lexical_weight / (selected.rrf_k + lr) if lr else 0)
                    + (selected.vector_weight / (selected.rrf_k + vr) if vr else 0)
                    + (
                        selected.graph_weight / (selected.rrf_k + depth + 1)
                        if depth is not None
                        else 0
                    )
                )
                hits.append(HybridHit(table, rid, score, lr, ls, vr, vs, depth))
            return HybridSearchResult(
                tuple(sorted(hits, key=lambda hit: (-hit.score, hit.record_id))[:k]),
                reader.snapshot.read_lsn,
                "rrf_v1_" + selected.fusion,
                "partial" if errors else "complete",
                lexical_regime,
                vector_regime,
                len(lexical),
                len(vectors),
                visited,
                tuple(errors),
                lexical_coverage,
            )
