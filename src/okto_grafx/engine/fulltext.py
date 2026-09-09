"""Native FTS over catalog-managed exact hash generations and snapshot-validated heap rows."""

from __future__ import annotations

import math
import struct
from collections.abc import Iterator, Callable
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxQueryBudgetExceeded,
    GrafxTransactionStateError,
)
from okto_grafx.domain.index.fulltext import (
    TextIndexOptions,
    TextSearchLimits,
    TextHit,
    TextSearchResult,
    analyze,
    decode_options,
    is_fulltext,
    TextAnalysisMemo,
    query_term_frequencies,
)
from okto_grafx.domain.index.keys import bucket_of
from okto_grafx.domain.index.entry import IndexEntry
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.query.control import (
    CancellationToken,
    _ReadControl,
    _read_control,
)
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.engine.index_manager import _IndexReadCertificate
from okto_grafx.engine.fulltext_stats import advance_statistics
from okto_grafx.engine.fulltext_durable import snapshot_statistics

if TYPE_CHECKING:
    from okto_grafx.engine.database import Database, Transaction
    from okto_grafx.engine.public_views import IndexView

__all__: list[str] = []


def create_text_index(
    database: Database,
    name: str,
    table: str,
    columns: tuple[str, ...],
    *,
    options: TextIndexOptions | None,
    bucket_count: int,
) -> IndexView:
    """Publish a complete nonced generation through the existing detached build protocol."""
    if (
        type(columns) is not tuple
        or not columns
        or any(type(c) is not str for c in columns)
    ):
        raise GrafxConfigurationError(
            "columns must be a nonempty tuple of names.", field="columns"
        )
    if type(name) is not str or type(table) is not str:
        raise GrafxConfigurationError(
            "Index and table names must be strings.", field="name"
        )
    if options is None:
        selected = TextIndexOptions(field_weights=(1.0,) * len(columns))
    elif type(options) is TextIndexOptions:
        selected = TextIndexOptions(**asdict(options))
    else:
        raise GrafxConfigurationError(
            "options must be TextIndexOptions.", field="options"
        )
    with database._public_operation("create_text_index"):
        database._require_open()
        database._require_writable("create full-text index")
        if selected.statistics_mode == "durable":
            from okto_grafx.engine.fulltext_durable import require_statistics_capacity
            require_statistics_capacity(selected.derivation(), database._pool.page_size)
        with database._transactions.page_access_section(fresh_read_view=True):
            definition = database._catalog.catalog.table(table)
            positions = tuple(definition.column_index(column) for column in columns)
            if definition.kind != "node" or any(
                definition.columns[p].type is not ValueType.STRING for p in positions
            ):
                raise GrafxConfigurationError(
                    "Full-text indexes require STRING node columns.", field="columns"
                )
            if len(positions) != len(selected.field_weights):
                raise GrafxConfigurationError(
                    "One weight is required per field.", field="field_weights"
                )
        with database.begin("write") as transaction:
            database._transactions.prepare_custom_exact_index(
                transaction._context,
                name=name,
                table_name=table,
                positions=positions,
                bucket_count=bucket_count,
                expected_cardinality=None,
                key_derivation=selected.derivation(),
            )
        database._refresh_index_inventory()
        return database._committed_index_receipt(name)


def search_text(
    database: Database,
    reader: Transaction,
    *,
    index: str,
    query: str,
    k: int,
    filter: RecordIdFilter | None,
    limits: TextSearchLimits | None,
    k1: float,
    b: float,
    timeout_seconds: float | None,
    cancellation: CancellationToken | None,
    _control: _ReadControl | None = None,
    _memory: Callable[[int], None] | None = None,
) -> TextSearchResult:
    """Return BM25 matches only after one complete pre/post index/heap certificate."""
    if reader._database is not database or not reader.active or reader.mode != "read":
        raise GrafxTransactionStateError(
            "Text search requires an active reader of this database.",
            operation="search_text",
        )
    reader._require_batch_idle("search_text")
    if (
        type(index) is not str
        or type(query) is not str
        or type(k) is not int
        or not 1 <= k <= 10_000
    ):
        raise GrafxConfigurationError(
            "Invalid index, query or k (1..10000).", field="search_text"
        )
    if (
        type(k1) not in (int, float)
        or not 0 < k1 <= 100
        or type(b) not in (int, float)
        or not 0 <= b <= 1
    ):
        raise GrafxConfigurationError(
            "BM25 requires 0<k1<=100 and 0<=b<=1.", field="bm25"
        )
    if limits is not None and type(limits) is not TextSearchLimits:
        raise GrafxConfigurationError(
            "limits must be TextSearchLimits.", field="limits"
        )
    budget = (
        TextSearchLimits() if limits is None else TextSearchLimits(**asdict(limits))
    )
    allowed = None
    if filter is not None:
        if (
            type(filter) is not RecordIdFilter
            or type(filter.record_ids) is not frozenset
        ):
            raise GrafxConfigurationError(
                "filter must be RecordIdFilter.", field="filter"
            )
        if len(filter.record_ids) * 64 > budget.max_memory_bytes:
            raise GrafxQueryBudgetExceeded(
                "Filter exceeds text-search memory budget.", resource="text_memory"
            )
        if any(
            type(rid) is not int or not 0 < rid < 0xFFFFFFFFFFFFFFFF
            for rid in filter.record_ids
        ):
            raise GrafxConfigurationError("Invalid filter RecordId.", field="filter")
        allowed = frozenset(filter.record_ids)
    control = _control or _read_control(database._clock, timeout_seconds, cancellation)
    with database._public_operation("search_text"):
        database._require_open()
        with database._transactions.page_access_section(transaction=reader._context):
            store = database._indexes.active_index(index)
            definition = store.definition
            if not is_fulltext(definition.key_derivation):
                raise GrafxIndexError(
                    "The named index is not a full-text index.", field="index"
                )
            options = decode_options(definition.key_derivation)
            query_options = replace(
                options,
                max_document_tokens=min(
                    options.max_document_tokens, budget.max_query_tokens
                ),
            )
            terms = tuple(sorted(set(analyze(query, query_options))))
            if len(terms) > budget.max_query_tokens:
                raise GrafxQueryBudgetExceeded(
                    "Query token budget exceeded.", resource="text_query_tokens"
                )
            snapshot = reader._context.snapshot
            table = database._catalog.catalog.table_by_id(definition.table_id)
            visited = 0
            memory = 0 if allowed is None else len(allowed) * 64
            analysis = TextAnalysisMemo(min(1_048_576, budget.max_memory_bytes // 4))
            wal_reservation = 0

            def check(charge: int = 0, *, posting: bool = False) -> None:
                """Charge attempted work, including certificate retries, before more work."""
                nonlocal visited, memory
                visited += int(posting)
                memory += charge
                if _memory is not None:
                    _memory(memory + analysis.retained_bytes + wal_reservation)
                if (
                    visited > budget.max_postings
                    or memory + analysis.retained_bytes > budget.max_memory_bytes
                ):
                    raise GrafxQueryBudgetExceeded(
                        "Full-text search work/memory budget exceeded.",
                        resource="text_search",
                    )
                if control is not None:
                    control.check()

            def entries(bucket: int) -> Iterator[IndexEntry]:
                """Visit page-bounded postings without materializing an unbounded bucket."""
                for page in store._bucket_pages(bucket):
                    check()
                    for entry in store._entries_on(page):
                        check(posting=True)
                        yield entry

            def confirm(
                certificate: _IndexReadCertificate,
            ) -> tuple[TextSearchResult, tuple, tuple[int, tuple[int, ...]]]:
                """Execute statistics, candidates and ranking under the same certificate."""
                nonlocal memory, wal_reservation
                memory = 0 if allowed is None else len(allowed) * 64
                database._indexes._prepare_heap_view(store.file, certificate)
                cache_key = (store.file, certificate, snapshot.read_lsn)
                cached = database._text_stats_cache.get(cache_key)
                statistics_regime = (
                    "snapshot_cache" if cached is not None else "full_census"
                )
                if cached is None:
                    cached = snapshot_statistics(store, snapshot.read_lsn)
                    if cached is not None:
                        statistics_regime = "durable_summary"
                wal_records = 0
                if cached is None:
                    def reserve_wal(amount: int) -> None:
                        """Charge bounded WAL capture while it coexists with lexical state."""
                        nonlocal wal_reservation
                        wal_reservation = amount
                        check()

                    try:
                        incremental = advance_statistics(
                            database, store, snapshot.read_lsn, budget, check,
                            reserve=reserve_wal if _memory is not None else None,
                        )
                    finally:
                        wal_reservation = 0
                    if incremental is not None:
                        cached, wal_records = incremental
                        statistics_regime = "wal_delta"
                if cached is None:
                    count = 0
                    totals = [0] * len(definition.positions)
                    seen = set()
                    for bucket in range(definition.bucket_count):
                        for entry in entries(bucket):
                            if not entry.key.startswith(b"\x00"):
                                continue
                            version = database._heap.read(entry.ref)
                            if version.table_id != definition.table_id:
                                raise GrafxCorruptionDetected(
                                    "Foreign row in full-text statistics.",
                                    file=store.file,
                                )
                            if not snapshot.visible(version.xmin, version.xmax):
                                continue
                            fields = analysis.fields(
                                version.values, definition.positions, options
                            )
                            expected = b"\x00" + struct.pack(
                                "<" + "I" * len(fields), *(len(f) for f in fields)
                            )
                            if entry.key != expected or version.record_id in seen:
                                raise GrafxCorruptionDetected(
                                    "Invalid/duplicate full-text statistics.",
                                    file=store.file,
                                )
                            check(64)
                            seen.add(version.record_id)
                            count += 1
                            totals = [
                                a + len(f) for a, f in zip(totals, fields, strict=True)
                            ]
                    memory -= len(seen) * 64
                    cached = (count, tuple(totals))
                count, totals = cached
                frequencies = {}
                matches = {}
                for term in terms:
                    key = b"\x01" + term.encode("utf-8")
                    docs = set()
                    for entry in entries(bucket_of(key, definition.bucket_count)):
                        if entry.key != key:
                            continue
                        version = database._heap.read(entry.ref)
                        if version.table_id != definition.table_id:
                            raise GrafxCorruptionDetected(
                                "Foreign row in full-text postings.", file=store.file
                            )
                        if not snapshot.visible(version.xmin, version.xmax):
                            continue
                        fields = analysis.fields(
                            version.values, definition.positions, options
                        )
                        if not any(term in f for f in fields):
                            raise GrafxCorruptionDetected(
                                "Full-text posting differs from its heap row.",
                                file=store.file,
                            )
                        rid = version.record_id
                        if rid in docs:
                            raise GrafxCorruptionDetected(
                                "Duplicate visible full-text posting.", file=store.file
                            )
                        check(64)
                        docs.add(rid)
                        if allowed is not None and rid not in allowed:
                            continue
                        if rid not in matches:
                            if len(matches) >= budget.max_candidates:
                                raise GrafxQueryBudgetExceeded(
                                    "Full-text candidate budget exceeded.",
                                    resource="text_candidates",
                                )
                            check(
                                128
                                + sum(
                                    64 + len(t.encode("utf-8"))
                                    for f in fields
                                    for t in f
                                )
                            )
                            matches[rid] = fields
                    frequencies[term] = len(docs)
                    memory -= len(docs) * 64
                if any(df > count for df in frequencies.values()):
                    raise GrafxCorruptionDetected(
                        "Full-text statistics omit posting documents.", file=store.file
                    )
                hits = []
                for rid, fields in matches.items():
                    frequency_bytes = 128 + len(fields) * (64 + 64 * len(terms))
                    check(frequency_bytes)
                    term_counts = query_term_frequencies(fields, terms)
                    score = 0.0
                    matched_fields = set()
                    matched_terms = set()
                    for term in terms:
                        df = frequencies[term]
                        idf = math.log1p((count - df + 0.5) / (df + 0.5))
                        for position, tokens in enumerate(fields):
                            tf = term_counts[position][term]
                            if not tf:
                                continue
                            average = (
                                totals[position] / count
                                if count and totals[position]
                                else 1.0
                            )
                            score += (
                                options.field_weights[position]
                                * idf
                                * tf
                                * (k1 + 1)
                                / (tf + k1 * (1 - b + b * len(tokens) / average))
                            )
                            matched_fields.add(
                                table.columns[definition.positions[position]].name
                            )
                            matched_terms.add(term)
                    del term_counts
                    memory -= frequency_bytes
                    hits.append(
                        TextHit(
                            rid,
                            score,
                            tuple(sorted(matched_fields)),
                            tuple(sorted(matched_terms)),
                        )
                    )
                chosen = tuple(
                    sorted(hits, key=lambda hit: (-hit.score, hit.record_id))[:k]
                )
                explanation = sum(
                    sum(
                        len(v.encode("utf-8"))
                        for v in (*hit.matched_fields, *hit.matched_terms)
                    )
                    for hit in chosen
                )
                if explanation > budget.max_explanation_bytes:
                    raise GrafxQueryBudgetExceeded(
                        "Full-text explanation budget exceeded.",
                        resource="text_explanation",
                    )
                return (
                    TextSearchResult(
                        chosen,
                        "exact_index",
                        certificate.header.built_through_lsn,
                        snapshot.read_lsn,
                        visited,
                        len(matches),
                        count,
                        statistics_regime,
                        wal_records,
                    ),
                    cache_key,
                    cached,
                )

            if control is not None:
                control.check()
            result, key, stats = store._stable_view(snapshot.read_lsn, confirm)
            if control is not None:
                control.check()
            if len(database._text_stats_cache) >= 8:
                database._text_stats_cache.pop(next(iter(database._text_stats_cache)))
            database._text_stats_cache[key] = stats
            return result
