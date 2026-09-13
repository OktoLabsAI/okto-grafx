"""Certified, bounded incident expansion; never fall back after adopting an index."""

from __future__ import annotations
from collections.abc import Callable
from typing import TYPE_CHECKING
from okto_grafx.domain.query.hybrid import HybridSearchOptions
from okto_grafx.domain.model.schema import TableDef

if TYPE_CHECKING:
    from okto_grafx.engine.database import Database, Transaction
    from okto_grafx.engine.index_manager import IndexStore, _IndexReadCertificate

__all__: list[str] = []

from okto_grafx.domain.errors import (
    GrafxConfigurationError, GrafxCorruptionDetected, GrafxQueryBudgetExceeded,
)
from okto_grafx.domain.index.definition import COLUMN_KEY_DERIVATION
from okto_grafx.domain.index.keys import bucket_of, index_key, record_id_key
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.engine.index_manager import edge_from_index_name, edge_to_index_name


def select_incident_indexes(database: Database, table: str, options: HybridSearchOptions) -> list[tuple[TableDef, int, IndexStore]] | None:
    """Select every required path before reading any candidates; absence keeps scan."""
    if options.graph_access == "scan":
        return None
    catalog = database._catalog.catalog
    selected = []
    missing = False
    for name in options.graph_relations:
        relation = catalog.table(name, kind="rel")
        if relation.kind != "rel" or relation.from_table != table or relation.to_table != table:
            raise GrafxConfigurationError(
                "Hybrid relations must join the target table to itself.", field="graph_relations",
            )
        positions = (0, 1) if options.graph_direction == "both" else (
            (0,) if options.graph_direction == "out" else (1,)
        )
        for position in positions:
            index_name = (edge_from_index_name if position == 0 else edge_to_index_name)(name)
            if not catalog.has_index_definition(index_name):
                missing = True
                continue
            selected.append((relation, position, index_name))
    if missing:
        return None
    paths = []
    for relation, position, name in selected:
        store = database._indexes.active_index(name)
        definition = store.definition
        if (definition.table_id != relation.table_id or definition.positions != (position,)
                or definition.key_derivation != COLUMN_KEY_DERIVATION
                or definition.visibility is not IndexVisibility.EXACT):
            raise GrafxCorruptionDetected("Invalid hybrid endpoint index.", field="graph_index")
        paths.append((relation, position, store))
    return paths


def incident_distances(database: Database, reader: Transaction, identity: IndexStore,
                       paths: list[tuple[TableDef, int, IndexStore]], options: HybridSearchOptions,
                       allowed: frozenset[int] | None, check: Callable[[], None],
                       reserve: Callable[[int], None]) -> tuple[dict[int, int], int]:
    """BFS touches selected endpoint buckets, preserving MVCC and endpoint witnesses.

    The edge budget counts unique visible relationship identities encountered, not
    every relationship in the database. Native physical candidates/pages are also
    bounded by the temporary logical-memory envelope, including certificate retries.
    """
    snapshot = reader._context.snapshot
    distance = dict.fromkeys(options.graph_seeds, 0)
    frontier = set(distance)
    seen_edges = set()
    reserve(len(distance) * 128)
    for depth in range(1, options.graph_hops + 1):
        if not frontier:
            break
        following = set()
        for relation, position, store in paths:
            keys = tuple(index_key((rid,), (0,)) for rid in sorted(frontier))
            groups = {}
            for key in keys:
                groups.setdefault(bucket_of(key, store.definition.bucket_count), set()).add(key)
            reserve(len(keys) * 128)
            for bucket, wanted in groups.items():
                temporary = 0

                def charge(amount: int) -> None:
                    """Reserve temporary candidate memory and retain its release amount."""
                    nonlocal temporary
                    reserve(amount)
                    temporary += amount

                def visit() -> None:
                    """Check cancellation and charge every attempted native chain page."""
                    check()
                    charge(128)

                def confirm(certificate: _IndexReadCertificate) -> list[tuple[int, int, int]]:
                    """Validate MVCC edge identities and both endpoints under one certificate."""
                    database._indexes._prepare_heap_view(store.file, certificate)
                    # Reserve worst-case bounded capture before the native walk. Empty,
                    # collision-only and invisible-history pages still get visit checks.
                    cap = max(1, options.max_memory_bytes // 1024)
                    charge(cap * 256)
                    _, entries = store._scan_bucket(
                        bucket, keys=frozenset(wanted), max_matches=cap, visit=visit,
                    )
                    rows = []
                    identities = set()
                    for entry in entries:
                        check()
                        version = database._heap.read(entry.ref)
                        if version.table_id != relation.table_id:
                            raise GrafxCorruptionDetected("Foreign hybrid edge.", field="table_id")
                        if not snapshot.visible(version.xmin, version.xmax):
                            continue
                        if store.definition.entry_key_for_record(version.record_id, version.values) != entry.key:
                            continue
                        if version.record_id in identities:
                            raise GrafxCorruptionDetected("Duplicate visible hybrid edge.", field="record_id")
                        identities.add(version.record_id)
                        rows.append((version.record_id, *version.values[:2]))
                    endpoints = tuple(dict.fromkeys(rid for _, a, b in rows for rid in (a, b)))
                    counts = database._indexes.validated_identity_counts_many(
                        identity, tuple(record_id_key(rid) for rid in endpoints), snapshot,
                    )
                    if any(count != 1 for count in counts):
                        raise GrafxCorruptionDetected("Invalid hybrid edge endpoint.", field="graph_endpoint")
                    return rows

                try:
                    rows = store._stable_view(store._require_exact_read_lsn(snapshot), confirm)
                    for rid, a, b in rows:
                        edge = (relation.table_id, rid)
                        if edge not in seen_edges:
                            if len(seen_edges) >= options.max_graph_edges:
                                raise GrafxQueryBudgetExceeded("Hybrid edge budget exceeded.", resource="hybrid_edges")
                            reserve(128)
                            seen_edges.add(edge)
                        if allowed is not None and (a not in allowed or b not in allowed):
                            continue
                        end = b if position == 0 else a
                        if end not in distance and end not in following:
                            reserve(128)
                            following.add(end)
                finally:
                    reserve(-temporary)
            reserve(-len(keys) * 128)
        distance.update((rid, depth) for rid in following)
        frontier = following
    return distance, len(seen_edges)
