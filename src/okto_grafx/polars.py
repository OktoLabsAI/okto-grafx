"""Optional explicit metadata-bearing Polars interop over native Arrow staging."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator
from typing import TYPE_CHECKING
from okto_grafx.domain.model.temporal_interchange import TEMPORAL_CLASSES_BY_NAME
from okto_grafx.domain.model.stored_types import StoredType, validate_typed_value
from okto_grafx.domain.model.value import MAX_VALUE_DEPTH
from okto_grafx._arrow_values import (
    _own_collections, _polars_collection_layout, _collection_from_arrow,
    _collection_to_arrow, _CollectionBudget,
    _collection_schema_charge,
)

from okto_grafx.arrow import ArrowVectorType, ArrowDecimalType, to_arrow_batches, import_arrow_batches
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


def _temporal_layout(actual, expected, pa):
    """Polars loses child nullability and widens UTF-8 offsets, not coordinates.

    Names, order and integer widths must remain exact. Native import validates
    each non-null struct after the safe cast, including required child values.
    """
    return (pa.types.is_struct(actual) and len(actual) == len(expected)
            and all(a.name == e.name and (
                a.type == e.type or e.name == "zone" and pa.types.is_large_string(a.type)
                and pa.types.is_string(e.type)) for a, e in zip(actual, expected)))


def _collection_series_arrow(series, pa, pl, budget, depth=0):
    """Avoid the Arrow C Data schema recursion ceiling without converting host dates.

    Export shallow leaves and reconstruct LIST/STRUCT buffers, preserving masks.
    Charge occurrences before Polars filtering or Python offset construction.
    """
    if depth > 3 * MAX_VALUE_DEPTH + 4:
        raise GrafxUnsupportedOperation("Polars collection schema exceeds native layout depth.", field="types")
    budget.add(128 + 128 * len(series) + 16 * series.estimated_size())
    dtype = series.dtype
    if isinstance(dtype, pl.List):
        lengths = series.list.len()
        offsets = [0]
        for length in lengths.to_list():
            offsets.append(offsets[-1] + (length or 0))
        # Polars explode inserts placeholders for empty/NULL lists; remove those
        # parents first. Real NULL elements within a nonempty list remain.
        flattened = series.filter(lengths > 0).explode()
        children = _collection_series_arrow(flattened, pa, pl, budget, depth + 1)
        return pa.ListArray.from_arrays(pa.array(offsets, type=pa.int32()), children,
                                        mask=series.is_null().to_arrow())
    if isinstance(dtype, pl.Struct) and dtype.fields:
        children = [_collection_series_arrow(series.struct.field(field.name), pa, pl, budget, depth + 1)
                    for field in dtype.fields]
        return pa.StructArray.from_arrays(children, names=[field.name for field in dtype.fields],
                                          mask=series.is_null().to_arrow())
    return series.to_arrow()


def to_polars(
    source: QueryResult | QueryCursor,
    *,
    types: tuple[str | ArrowVectorType | ArrowDecimalType | StoredType, ...],
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
    types = _own_collections(types)
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
    types: tuple[str | ArrowVectorType | ArrowDecimalType | StoredType, ...],
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
    types = _own_collections(types)
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
                segment = frame.frame.slice(offset, max_batch_rows)
                conversion_budget = _CollectionBudget(max_batch_bytes - _collection_schema_charge(types))
                raw = pa.Table.from_arrays([
                    _collection_series_arrow(segment[name], pa, pl, conversion_budget)
                    if type(kind) is StoredType else segment[name].to_arrow()
                    for name, kind in zip(segment.columns, types)], names=segment.columns)
                for actual, expected, kind in zip(raw.schema, schema, types):
                    allowed = actual.type == expected.type
                    if type(kind) is StoredType:
                        allowed = _polars_collection_layout(actual.type, expected.type, pa)
                    elif kind in TEMPORAL_CLASSES_BY_NAME:
                        allowed |= _temporal_layout(actual.type, expected.type, pa)
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
                charge = _charge(raw) + _collection_schema_charge(types)
                if charge > max_batch_bytes:
                    raise GrafxQueryBudgetExceeded(
                        "Polars batch bound exceeded.", resource="polars_import"
                    )
                # Arrow cannot cast Polars list-of-entries back to MAP. Rebuild
                # declared collections through bounded exact native values, not
                # dtype inference or lossy map-to-dict convenience conversions.
                arrays = []
                budget = _CollectionBudget(max_batch_bytes - charge)
                for column, field, kind in zip(raw.columns, schema, types):
                    if type(kind) is StoredType:
                        values = []
                        for scalar in column:
                            value = _collection_from_arrow(kind, scalar, pa, budget)
                            validate_typed_value(kind, value)
                            values.append(_collection_to_arrow(kind, value))
                        arrays.append(pa.chunked_array([pa.array(values, type=field.type, safe=True)], type=field.type))
                    else:
                        arrays.append(column.cast(field.type, safe=True))
                yield from pa.Table.from_arrays(arrays, schema=schema).to_batches(
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
