"""Optional bounded scalar/vector Arrow interop with explicit ownership and atomic import."""

from __future__ import annotations

from collections.abc import Iterator, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxUnsupportedOperation, GrafxQueryBudgetExceeded
from okto_grafx.domain.model.value import Timestamp, Uuid, VectorValue, MAX_VECTOR_DIMENSION, encode_value
from okto_grafx.engine.database import QueryCursor, Transaction, ExecuteManyReport
from okto_grafx.engine.query_engine import QueryResult

if TYPE_CHECKING:
    from pyarrow import RecordBatch

__all__ = ["ArrowVectorType", "to_arrow_batches", "import_arrow_batches"]


@dataclass(frozen=True, slots=True)
class ArrowVectorType:
    """Explicit store-local vector identity, dimension and native precision for Arrow."""

    space_ref: int
    dimension: int
    dtype: str = "float32"

    def __post_init__(self) -> None:
        if type(self.space_ref) is not int or not 1 <= self.space_ref <= 0xFFFFFFFF:
            raise GrafxConfigurationError("Invalid vector space reference.", field="space_ref")
        if type(self.dimension) is not int or not 1 <= self.dimension <= MAX_VECTOR_DIMENSION:
            raise GrafxConfigurationError("Invalid vector dimension.", field="dimension")
        if type(self.dtype) is not str or self.dtype not in ("float32", "float64"):
            raise GrafxConfigurationError("Vector dtype must be float32 or float64.", field="dtype")


def _metadata(kind):
    if type(kind) is ArrowVectorType:
        return {b"grafx.type": b"VECTOR_F32" if kind.dtype == "float32" else b"VECTOR_F64",
                b"grafx.space_ref": str(kind.space_ref).encode("ascii"),
                b"grafx.dimension": str(kind.dimension).encode("ascii"),
                b"grafx.dtype": kind.dtype.encode("ascii")}
    return {b"grafx.type": kind.encode("ascii")}


def _arrow_type(kind, pa, scalars):
    if type(kind) is ArrowVectorType:
        return pa.list_(pa.float32() if kind.dtype == "float32" else pa.float64(), kind.dimension)
    return scalars[kind]


def import_arrow_batches(
    transaction: Transaction, statement: str, batches: Iterable[RecordBatch], *,
    types: tuple[str | ArrowVectorType, ...], max_batch_rows: int = 65536,
    max_batch_bytes: int = 16 * 1024 * 1024, max_rows: int = 1_000_000,
    max_batches: int = 4096,
) -> ExecuteManyReport:
    """Atomically stage typed scalar/vector batches as named parameters, without committing.

    One executemany savepoint covers the whole call. Any later malformed batch or
    bound refusal discards this call, preserving prior transaction staging. The
    caller owns source iteration, transaction lifetime, commit and retries.
    """
    if type(transaction) is not Transaction:
        raise GrafxConfigurationError("Arrow import needs a native transaction.", field="transaction")
    for name, value, maximum in (("max_batch_rows", max_batch_rows, 65536),
            ("max_batch_bytes", max_batch_bytes, 2**31), ("max_rows", max_rows, 2**31),
            ("max_batches", max_batches, 2**31)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise GrafxConfigurationError("Invalid Arrow import bound.", field=name)
    allowed = {"BOOL", "INT64", "DOUBLE", "STRING", "BYTES", "TIMESTAMP", "UUID"}
    if (type(types) is not tuple or not 1 <= len(types) <= 256
            or any(type(kind) is not ArrowVectorType and (type(kind) is not str or kind not in allowed) for kind in types)):
        raise GrafxConfigurationError("Arrow import requires 1..256 explicit native types.", field="types")
    try:
        import pyarrow as pa
    except ImportError as failure:
        raise GrafxUnsupportedOperation("Install okto-grafx[arrow] for Arrow import.", field="arrow") from failure
    expected = {"BOOL": pa.bool_(), "INT64": pa.int64(), "DOUBLE": pa.float64(),
                "STRING": pa.string(), "BYTES": pa.binary(), "TIMESTAMP": pa.timestamp("us", tz="UTC"),
                "UUID": pa.binary(16)}

    def parameters() -> Iterator[dict[str, object]]:
        """Validate/copy at most one bounded input batch inside the native savepoint."""
        names = None
        rows = 0
        for batch_index, batch in enumerate(batches):
            if batch_index >= max_batches:
                raise GrafxQueryBudgetExceeded("Arrow import batch count exceeded.", resource="arrow_import")
            if type(batch) is not pa.RecordBatch or batch.num_columns != len(types):
                raise GrafxConfigurationError("Arrow input must contain typed RecordBatches.", field="batches", batch=batch_index)
            current = tuple(batch.schema.names)
            if (len(set(current)) != len(current) or any(not name or len(name) > 256 for name in current)
                    or (names is not None and current != names)):
                raise GrafxConfigurationError("Arrow parameter names must be unique and stable.", field="columns", batch=batch_index)
            names = current
            for field, kind in zip(batch.schema, types):
                metadata = field.metadata or {}
                required = _metadata(kind)
                bad_metadata = (any(metadata.get(k) != v for k, v in required.items())
                                if type(kind) is ArrowVectorType else
                                b"grafx.type" in metadata and metadata[b"grafx.type"] != required[b"grafx.type"])
                if field.type != _arrow_type(kind, pa, expected) or bad_metadata:
                    raise GrafxUnsupportedOperation("Arrow schema differs from explicit native types.", field="types", column=field.name, batch=batch_index)
            multiplier = 16 if any(type(kind) is ArrowVectorType for kind in types) else 4
            charge = 256 + 256 * len(types) + 80 * batch.num_rows * len(types) + multiplier * batch.nbytes
            rows += batch.num_rows
            if batch.num_rows > max_batch_rows or charge > max_batch_bytes or rows > max_rows:
                raise GrafxQueryBudgetExceeded("Arrow import rows/logical memory exceeded.", resource="arrow_import", batch=batch_index)
            for row in range(batch.num_rows):
                values = {}
                for column, (name, kind) in enumerate(zip(names, types)):
                    scalar = batch.column(column)[row]
                    value = (None if not scalar.is_valid else
                             VectorValue(tuple(scalar.as_py()), kind.space_ref, kind.dtype) if type(kind) is ArrowVectorType else
                             Timestamp(scalar.cast(pa.int64()).as_py()) if kind == "TIMESTAMP" else
                             Uuid(scalar.as_py()) if kind == "UUID" else scalar.as_py())
                    encode_value(value)
                    values[name] = value
                yield values

    return transaction.executemany(statement, parameters())


def to_arrow_batches(
    source: QueryResult | QueryCursor, *, types: tuple[str | ArrowVectorType, ...], batch_rows: int = 256,
    max_batch_bytes: int = 16 * 1024 * 1024,
) -> Iterator[RecordBatch]:
    """Yield copied typed batches; caller owns cursor lifetime and already-emitted batches."""
    if type(source) not in (QueryResult, QueryCursor):
        raise GrafxConfigurationError("Arrow source must be a native result or cursor.", field="source")
    allowed = {"BOOL": bool, "INT64": int, "DOUBLE": float, "STRING": str, "BYTES": bytes,
               "TIMESTAMP": Timestamp, "UUID": Uuid}
    if (type(types) is not tuple or len(types) != len(source.columns)
            or any(type(kind) is not ArrowVectorType and (type(kind) is not str or kind not in allowed) for kind in types)):
        raise GrafxConfigurationError("Arrow export needs one supported native type per column.", field="types")
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
    schema = pa.schema([pa.field(name, _arrow_type(kind, pa, arrow_types), nullable=True,
                                metadata=_metadata(kind))
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
                vector = type(kind) is ArrowVectorType
                if value is not None and type(value) is not (VectorValue if vector else allowed[kind]):
                    raise GrafxUnsupportedOperation("Arrow value differs from the explicit scalar schema.", field="types", column=source.columns[column])
                if vector and value is not None and (value.space_ref != kind.space_ref or value.dtype != kind.dtype
                                                      or len(value.values) != kind.dimension):
                    raise GrafxUnsupportedOperation("Vector differs from its explicit Arrow descriptor.", field="types", column=source.columns[column])
                cost += 64 + (128 + 32 * kind.dimension if vector else
                              4 * len(value) if type(value) is str else len(value) if type(value) is bytes else 16)
                if cost > max_batch_bytes:
                    raise GrafxQueryBudgetExceeded("Arrow logical batch budget exceeded.", resource="arrow_batch")
                if value is not None:
                    try:
                        encode_value(value)  # native range/encoding validation, no coercion
                    except Exception as failure:
                        raise GrafxUnsupportedOperation("Arrow source violates the native scalar contract.", field="types") from failure
                columns[column].append(value.values if type(value) is VectorValue else
                                       value.micros if type(value) is Timestamp else value.raw if type(value) is Uuid else value)
        try:
            arrays = [pa.array(values, type=_arrow_type(kind, pa, arrow_types), safe=True, from_pandas=False)
                      for values, kind in zip(columns, types)]
            batch = pa.RecordBatch.from_arrays(arrays, schema=schema)
        except (pa.ArrowException, OverflowError, ValueError) as failure:
            raise GrafxUnsupportedOperation("Arrow cannot represent this typed batch.", field="arrow") from failure
        yield batch
