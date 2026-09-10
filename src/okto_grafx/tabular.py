"""Optional typed Pandas interop layered on native Arrow staging."""

from __future__ import annotations

from typing import TYPE_CHECKING
from collections.abc import Iterator

from okto_grafx.arrow import ArrowVectorType, _arrow_type, _metadata, import_arrow_batches, to_arrow_batches
from okto_grafx.engine.database import Transaction, QueryCursor, ExecuteManyReport
from okto_grafx.engine.query_engine import QueryResult
from okto_grafx.errors import GrafxConfigurationError, GrafxUnsupportedOperation, GrafxQueryBudgetExceeded

if TYPE_CHECKING:
    from pandas import DataFrame
    from pyarrow import RecordBatch

__all__ = ["to_pandas", "import_pandas"]


def _arrow():
    try:
        import pyarrow as pa
    except ImportError as failure:
        raise GrafxUnsupportedOperation("Install okto-grafx[arrow] for typed tabular interop.", field="arrow") from failure
    return pa


def _pandas():
    try:
        import pandas as pd
    except ImportError as failure:
        raise GrafxUnsupportedOperation("Install okto-grafx[pandas] for Pandas interop.", field="pandas") from failure
    return pd


def _limit(name, value, maximum=2**31):
    if type(value) is not int or not 1 <= value <= maximum:
        raise GrafxConfigurationError("Invalid tabular bound.", field=name)
    return value


def _schema(names, types, pa):
    scalar = {"BOOL": pa.bool_(), "INT64": pa.int64(), "DOUBLE": pa.float64(), "STRING": pa.string(),
              "BYTES": pa.binary(), "TIMESTAMP": pa.timestamp("us", tz="UTC"), "UUID": pa.binary(16)}
    if (type(types) is not tuple or not 1 <= len(types) <= 256 or len(names) != len(types)
            or any(type(t) is not ArrowVectorType and (type(t) is not str or t not in scalar) for t in types)):
        raise GrafxConfigurationError("Explicit supported types must match all columns.", field="types")
    if any(type(n) is not str or not n or len(n) > 256 for n in names) or len(set(names)) != len(names):
        raise GrafxConfigurationError("Column names must be unique nonempty strings.", field="columns")
    return pa.schema([pa.field(n, _arrow_type(t, pa, scalar), metadata=_metadata(t)) for n, t in zip(names, types)])


def _match_schema(observed, expected, types):
    if observed.names != expected.names:
        raise GrafxUnsupportedOperation("Tabular columns differ from the declared schema.", field="columns")
    for actual, wanted, kind in zip(observed, expected, types):
        metadata = actual.metadata or {}
        required = wanted.metadata or {}
        bad = (any(metadata.get(k) != v for k, v in required.items()) if type(kind) is ArrowVectorType else
               b"grafx.type" in metadata and metadata[b"grafx.type"] != required[b"grafx.type"])
        if actual.type != wanted.type or bad:
            raise GrafxUnsupportedOperation("Tabular field differs from its declared native type.", field="types", column=actual.name)


def _charge(batch):
    return 256 + 256 * batch.num_columns + 80 * batch.num_rows * batch.num_columns + 16 * batch.nbytes


def to_pandas(source: QueryResult | QueryCursor, *, types: tuple[str | ArrowVectorType, ...],
              batch_rows: int = 256, max_batch_bytes: int = 16 * 1024 * 1024,
              max_rows: int = 100_000, max_bytes: int = 64 * 1024 * 1024) -> DataFrame:
    """Materialize an explicitly typed Arrow-backed frame; never infer dtypes or close a cursor."""
    if type(source) not in (QueryResult, QueryCursor):
        raise GrafxConfigurationError("Pandas export requires a native result or cursor.", field="source")
    _limit("max_rows", max_rows)
    _limit("max_bytes", max_bytes)
    pa, pd = _arrow(), _pandas()
    schema = _schema(source.columns, types, pa)
    batches, rows, charge = [], 0, 4096
    if charge > max_bytes:
        raise GrafxQueryBudgetExceeded("Pandas frame budget exceeded.", resource="pandas_frame")
    for batch in to_arrow_batches(source, types=types, batch_rows=batch_rows, max_batch_bytes=max_batch_bytes):
        rows += batch.num_rows
        charge += _charge(batch)
        if rows > max_rows or charge > max_bytes:
            raise GrafxQueryBudgetExceeded("Pandas frame budget exceeded.", resource="pandas_frame")
        batches.append(batch)
    frame = pa.Table.from_batches(batches, schema=schema).to_pandas(types_mapper=pd.ArrowDtype)
    frame.attrs["grafx.arrow_schema"] = schema
    return frame


def import_pandas(transaction: Transaction, statement: str, frame: DataFrame, *,
                  types: tuple[str | ArrowVectorType, ...], max_batch_rows: int = 256,
                  max_batch_bytes: int = 16 * 1024 * 1024, max_rows: int = 1_000_000,
                  max_batches: int = 4096) -> ExecuteManyReport:
    """Stage one Arrow-backed DataFrame atomically; require explicit dtypes and vector metadata."""
    pa, pd = _arrow(), _pandas()
    if type(frame) is not pd.DataFrame:
        raise GrafxConfigurationError("Expected an exact DataFrame.", field="frame")
    _limit("max_batch_rows", max_batch_rows, 65536)
    _limit("max_batch_bytes", max_batch_bytes)
    _limit("max_rows", max_rows)
    _limit("max_batches", max_batches)
    schema = _schema(tuple(frame.columns), types, pa)
    for name, field in zip(frame.columns, schema):
        dtype = frame[name].dtype
        if type(dtype) is not pd.ArrowDtype or dtype.pyarrow_dtype != field.type:
            raise GrafxUnsupportedOperation("DataFrame columns must use the exact declared ArrowDtype.", field="types", column=name)
    observed = frame.attrs.get("grafx.arrow_schema")
    if observed is not None:
        if type(observed) is not pa.Schema:
            raise GrafxConfigurationError("grafx.arrow_schema must be an Arrow Schema.", field="metadata")
        _match_schema(observed, schema, types)
    elif any(type(t) is ArrowVectorType for t in types):
        raise GrafxUnsupportedOperation("Vector DataFrames require grafx.arrow_schema metadata.", field="metadata")
    if len(frame) > max_rows:
        raise GrafxQueryBudgetExceeded("Pandas import row bound exceeded.", resource="pandas_import")

    def batches() -> Iterator[RecordBatch]:
        """Expose bounded Arrow-backed slices inside the native staging savepoint."""
        for start in range(0, len(frame), max_batch_rows):
            segment = frame.iloc[start:start + max_batch_rows]
            arrays = [segment[name].array.__arrow_array__() for name in segment.columns]
            charge = 256 + 256 * len(types) + 80 * len(segment) * len(types) + 16 * sum(a.nbytes for a in arrays)
            if charge > max_batch_bytes:
                raise GrafxQueryBudgetExceeded("Pandas batch bound exceeded.", resource="pandas_import")
            yield pa.RecordBatch.from_arrays([a.combine_chunks() for a in arrays], schema=schema)

    return import_arrow_batches(transaction, statement, batches(), types=types, max_batch_rows=max_batch_rows,
                                max_batch_bytes=max_batch_bytes, max_rows=max_rows, max_batches=max_batches)
