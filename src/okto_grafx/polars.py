"""Optional explicit metadata-bearing Polars interop over native Arrow staging."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator
from typing import TYPE_CHECKING

from okto_grafx.arrow import ArrowVectorType, to_arrow_batches, import_arrow_batches
from okto_grafx.tabular import _arrow, _schema, _match_schema, _limit, _charge
from okto_grafx.engine.database import Transaction, QueryCursor, ExecuteManyReport
from okto_grafx.engine.query_engine import QueryResult
from okto_grafx.errors import (
    GrafxConfigurationError,
    GrafxUnsupportedOperation,
    GrafxQueryBudgetExceeded,
)

if TYPE_CHECKING:
    from polars import DataFrame
    from pyarrow import Schema
    from pyarrow import RecordBatch

__all__ = ["PolarsFrame", "to_polars", "import_polars"]


@dataclass(frozen=True, slots=True)
class PolarsFrame:
    """Frame plus explicit Arrow metadata; do not mutate the frame during consumption."""

    frame: DataFrame
    arrow_schema: Schema


def _polars():
    try:
        import polars as pl
    except ImportError as failure:
        raise GrafxUnsupportedOperation(
            "Install okto-grafx[polars] for Polars interop.", field="polars"
        ) from failure
    return pl


def to_polars(
    source: QueryResult | QueryCursor,
    *,
    types: tuple[str | ArrowVectorType, ...],
    batch_rows: int = 256,
    max_batch_bytes: int = 16 * 1024 * 1024,
    max_rows: int = 100_000,
    max_bytes: int = 64 * 1024 * 1024,
) -> PolarsFrame:
    """Materialize an explicitly typed frame and retain its native schema separately."""
    if type(source) not in (QueryResult, QueryCursor):
        raise GrafxConfigurationError(
            "Polars export requires a native result/cursor.", field="source"
        )
    _limit("max_rows", max_rows)
    _limit("max_bytes", max_bytes)
    pa, pl = _arrow(), _polars()
    schema = _schema(source.columns, types, pa)
    batches, count, charge = [], 0, 4096
    if charge > max_bytes:
        raise GrafxQueryBudgetExceeded(
            "Polars frame budget exceeded.", resource="polars_frame"
        )
    for batch in to_arrow_batches(
        source, types=types, batch_rows=batch_rows, max_batch_bytes=max_batch_bytes
    ):
        count += batch.num_rows
        charge += _charge(batch)
        if count > max_rows or charge > max_bytes:
            raise GrafxQueryBudgetExceeded(
                "Polars frame budget exceeded.", resource="polars_frame"
            )
        batches.append(batch)
    try:
        return PolarsFrame(
            pl.from_arrow(pa.Table.from_batches(batches, schema=schema), rechunk=False),
            schema,
        )
    except (pa.ArrowException, pl.exceptions.PolarsError) as failure:
        raise GrafxUnsupportedOperation(
            "Polars cannot represent this declared table.", field="types"
        ) from failure


def import_polars(
    transaction: Transaction,
    statement: str,
    frame: PolarsFrame,
    *,
    types: tuple[str | ArrowVectorType, ...],
    max_batch_rows: int = 256,
    max_batch_bytes: int = 16 * 1024 * 1024,
    max_rows: int = 1_000_000,
    max_batches: int = 4096,
) -> ExecuteManyReport:
    """Stage one metadata-bearing eager frame atomically, with caller-owned commit."""
    pa, pl = _arrow(), _polars()
    if (
        type(frame) is not PolarsFrame
        or type(frame.frame) is not pl.DataFrame
        or type(frame.arrow_schema) is not pa.Schema
    ):
        raise GrafxConfigurationError(
            "Expected PolarsFrame with explicit Arrow schema.", field="frame"
        )
    _limit("max_batch_rows", max_batch_rows, 65536)
    for name, value in (
        ("max_batch_bytes", max_batch_bytes),
        ("max_rows", max_rows),
        ("max_batches", max_batches),
    ):
        _limit(name, value)
    schema = _schema(tuple(frame.frame.columns), types, pa)
    _match_schema(frame.arrow_schema, schema, types)
    if frame.frame.height > max_rows:
        raise GrafxQueryBudgetExceeded(
            "Polars row bound exceeded.", resource="polars_import"
        )

    def batches() -> Iterator[RecordBatch]:
        """Normalize only documented Arrow offset-width differences, inside the savepoint."""
        try:
            for offset in range(0, max(1, frame.frame.height), max_batch_rows):
                raw = frame.frame.slice(offset, max_batch_rows).to_arrow()
                for actual, expected in zip(raw.schema, schema):
                    allowed = actual.type == expected.type
                    allowed |= pa.types.is_large_string(
                        actual.type
                    ) and pa.types.is_string(expected.type)
                    allowed |= (
                        pa.types.is_large_binary(actual.type)
                        or pa.types.is_binary(actual.type)
                    ) and (
                        pa.types.is_binary(expected.type)
                        or pa.types.is_fixed_size_binary(expected.type)
                    )
                    if not allowed:
                        raise GrafxUnsupportedOperation(
                            "Polars dtype differs from explicit type.",
                            field="types",
                            column=actual.name,
                        )
                if _charge(raw) > max_batch_bytes:
                    raise GrafxQueryBudgetExceeded(
                        "Polars batch bound exceeded.", resource="polars_import"
                    )
                yield from raw.cast(schema, safe=True).to_batches(
                    max_chunksize=max_batch_rows
                )
        except (pa.ArrowException, pl.exceptions.PolarsError) as failure:
            raise GrafxUnsupportedOperation(
                "Invalid Polars table for declared types.", field="types"
            ) from failure

    return import_arrow_batches(
        transaction,
        statement,
        batches(),
        types=types,
        max_batch_rows=max_batch_rows,
        max_batch_bytes=max_batch_bytes,
        max_rows=max_rows,
        max_batches=max_batches,
    )
