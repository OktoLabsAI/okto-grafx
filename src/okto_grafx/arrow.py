"""Optional bounded Arrow export of detached results or caller-owned read cursors."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxUnsupportedOperation, GrafxQueryBudgetExceeded
from okto_grafx.domain.model.value import Timestamp, Uuid, encode_value
from okto_grafx.engine.database import QueryCursor
from okto_grafx.engine.query_engine import QueryResult

if TYPE_CHECKING:
    from pyarrow import RecordBatch

__all__ = ["to_arrow_batches"]


def to_arrow_batches(
    source: QueryResult | QueryCursor, *, types: tuple[str, ...], batch_rows: int = 256,
    max_batch_bytes: int = 16 * 1024 * 1024,
) -> Iterator[RecordBatch]:
    """Yield copied typed batches; caller owns cursor lifetime and already-emitted batches."""
    if type(source) not in (QueryResult, QueryCursor):
        raise GrafxConfigurationError("Arrow source must be a native result or cursor.", field="source")
    allowed = {"BOOL": bool, "INT64": int, "DOUBLE": float, "STRING": str, "BYTES": bytes,
               "TIMESTAMP": Timestamp, "UUID": Uuid}
    if (type(types) is not tuple or len(types) != len(source.columns)
            or any(type(kind) is not str or kind not in allowed for kind in types)):
        raise GrafxConfigurationError("Arrow export needs one supported scalar type per column.", field="types")
    if type(batch_rows) is not int or not 1 <= batch_rows <= 65536:
        raise GrafxConfigurationError("batch_rows must be 1..65536.", field="batch_rows")
    if type(max_batch_bytes) is not int or not 1 <= max_batch_bytes <= 2**31:
        raise GrafxConfigurationError("Invalid Arrow batch budget.", field="max_batch_bytes")
    try:
        import pyarrow as pa
    except ImportError as failure:
        raise GrafxUnsupportedOperation("Install okto-grafx[arrow] for Arrow export.", field="arrow") from failure
    arrow_types = {"BOOL": pa.bool_(), "INT64": pa.int64(), "DOUBLE": pa.float64(),
                   "STRING": pa.string(), "BYTES": pa.binary(), "TIMESTAMP": pa.timestamp("us", tz="UTC"),
                   "UUID": pa.binary(16)}
    schema = pa.schema([pa.field(name, arrow_types[kind], nullable=True,
                                metadata={b"grafx.type": kind.encode("ascii")})
                        for name, kind in zip(source.columns, types)])
    position = 0
    while True:
        if type(source) is QueryCursor:
            rows = source.fetchmany(batch_rows)
        else:
            rows = source.rows[position:position + batch_rows]
            position += len(rows)
        if not rows:
            return
        if not types:
            raise GrafxUnsupportedOperation("Arrow export requires columns for non-empty rows.", field="types")
        cost = 256 + len(types) * 256
        columns = [[] for _ in types]
        for row in rows:
            if type(row) is not tuple or len(row) != len(types):
                raise GrafxConfigurationError("Malformed Arrow source row.", field="rows")
            for column, (value, kind) in enumerate(zip(row, types)):
                if value is not None and type(value) is not allowed[kind]:
                    raise GrafxUnsupportedOperation("Arrow value differs from the explicit scalar schema.", field="types", column=source.columns[column])
                cost += 64 + (4 * len(value) if type(value) is str else len(value) if type(value) is bytes else 16)
                if cost > max_batch_bytes:
                    raise GrafxQueryBudgetExceeded("Arrow logical batch budget exceeded.", resource="arrow_batch")
                if value is not None:
                    try:
                        encode_value(value)  # native range/encoding validation, no coercion
                    except Exception as failure:
                        raise GrafxUnsupportedOperation("Arrow source violates the native scalar contract.", field="types") from failure
                columns[column].append(value.micros if type(value) is Timestamp else value.raw if type(value) is Uuid else value)
        try:
            arrays = [pa.array(values, type=arrow_types[kind], safe=True, from_pandas=False)
                      for values, kind in zip(columns, types)]
            batch = pa.RecordBatch.from_arrays(arrays, schema=schema)
        except (pa.ArrowException, OverflowError, ValueError) as failure:
            raise GrafxUnsupportedOperation("Arrow cannot represent this typed batch.", field="arrow") from failure
        yield batch
