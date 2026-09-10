"""Optional local Parquet interop, not a graph backup or external query engine."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile
from typing import TYPE_CHECKING

from okto_grafx.arrow import ArrowVectorType, to_arrow_batches, import_arrow_batches
from okto_grafx.tabular import _arrow, _schema, _match_schema, _limit, _charge
from okto_grafx.engine.database import Transaction, QueryCursor, ExecuteManyReport
from okto_grafx.engine.query_engine import QueryResult
from okto_grafx.errors import GrafxConfigurationError, GrafxUnsupportedOperation, GrafxQueryBudgetExceeded

if TYPE_CHECKING:
    from pyarrow import RecordBatch

__all__ = ["ParquetExportReport", "read_parquet_batches", "import_parquet", "write_parquet"]


@dataclass(frozen=True, slots=True)
class ParquetExportReport:
    """One complete newly published local file; not a database durability receipt."""

    path: str
    rows: int
    batches: int
    bytes: int


def _regular(info):
    return stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 1024


def _root_check(root):
    for parent in (root, *root.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise GrafxUnsupportedOperation("Parquet paths cannot traverse links or reparse points.", field="path")
    info = root.stat()
    return info.st_dev, info.st_ino


def _path(path, allowed_root):
    if not isinstance(path, (str, os.PathLike)) or not isinstance(allowed_root, (str, os.PathLike)) or not str(allowed_root):
        raise GrafxConfigurationError("Parquet requires an explicit path and local root.", field="path")
    if any(str(value).startswith(("\\\\", "//")) for value in (path, allowed_root)):
        raise GrafxUnsupportedOperation("Parquet supports local files only.", field="path")
    root = Path(allowed_root).absolute()
    identity = _root_check(root)
    root = root.resolve(strict=True)
    raw = Path(path)
    if str(raw).startswith(("\\\\", "//")) or str(root).startswith(("\\\\", "//")):
        raise GrafxUnsupportedOperation("Parquet supports local files only.", field="path")
    if raw.is_absolute():
        if raw.parent != root:
            raise GrafxUnsupportedOperation("Parquet file must be directly inside allowed_root.", field="path")
        name = raw.name
    else:
        name = str(raw)
    if not name or name in (".", "..") or any(c in name for c in ("/", "\\", ":", "\x00")):
        raise GrafxUnsupportedOperation("Parquet requires a single local file name.", field="path")
    target = root / name
    if target.exists() or target.is_symlink():
        if not _regular(target.lstat()):
            raise GrafxUnsupportedOperation("Parquet requires a regular non-link file.", field="path")
    return target, root, identity


def _unchanged_root(root, identity):
    if _root_check(root) != identity:
        raise GrafxUnsupportedOperation("Parquet directory identity changed.", field="path")


def _options(max_batch_rows, max_batch_bytes, max_rows, max_batches, max_file_bytes):
    _limit("max_batch_rows", max_batch_rows, 65536)
    for name, value in (("max_batch_bytes", max_batch_bytes), ("max_rows", max_rows),
                        ("max_batches", max_batches), ("max_file_bytes", max_file_bytes)):
        _limit(name, value)


def _physical_schema(schema, types, pa):
    # Parquet fixed-size lists with an outer NULL do not round-trip in Arrow.
    # Persist a tagged variable-list encoding, keeping native shape in metadata.
    return pa.schema([pa.field(field.name, pa.list_(field.type.value_type),
                               metadata={**field.metadata, b"grafx.parquet.vector": b"list-v1"})
                      if type(kind) is ArrowVectorType else field
                      for field, kind in zip(schema, types)])


def _read_schema(observed, schema, types, pa):
    physical = _physical_schema(schema, types, pa)
    expected = []
    for actual, native, stored, kind in zip(observed, schema, physical, types):
        encoding = (actual.metadata or {}).get(b"grafx.parquet.vector")
        if encoding is not None and (type(kind) is not ArrowVectorType or encoding != b"list-v1"):
            raise GrafxUnsupportedOperation("Unknown Parquet vector encoding.", field="types")
        expected.append(stored if encoding else native)
    _match_schema(observed, pa.schema(expected), types)


def read_parquet_batches(path: str | os.PathLike[str], *, allowed_root: str | os.PathLike[str],
                         types: tuple[str | ArrowVectorType, ...], max_batch_rows: int = 256,
                         max_batch_bytes: int = 16 * 1024 * 1024, max_rows: int = 1_000_000,
                         max_batches: int = 4096, max_file_bytes: int = 256 * 1024 * 1024,
                         max_row_group_bytes: int = 64 * 1024 * 1024) -> Iterator[RecordBatch]:
    """Read typed batches from one permitted local file; close the iterator on early exit."""
    _options(max_batch_rows, max_batch_bytes, max_rows, max_batches, max_file_bytes)
    _limit("max_row_group_bytes", max_row_group_bytes)
    pa = _arrow()
    import pyarrow.parquet as pq
    try:
        target, root, identity = _path(path, allowed_root)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(target, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not _regular(before):
                raise GrafxUnsupportedOperation("Parquet input is not a regular file.", field="path")
            if before.st_size > max_file_bytes:
                raise GrafxQueryBudgetExceeded("Parquet file bound exceeded.", resource="parquet_file")
            _unchanged_root(root, identity)
            with pq.ParquetFile(stream, memory_map=False, pre_buffer=False,
                                thrift_string_size_limit=1048576, thrift_container_size_limit=100000) as source:
                schema = _schema(tuple(source.schema_arrow.names), types, pa)
                _read_schema(source.schema_arrow, schema, types, pa)
                if source.metadata.num_rows > max_rows or source.num_row_groups > max_batches:
                    raise GrafxQueryBudgetExceeded("Parquet metadata exceeds row/group bounds.", resource="parquet_import")
                for group in range(source.num_row_groups):
                    if source.metadata.row_group(group).total_byte_size > max_row_group_bytes:
                        raise GrafxQueryBudgetExceeded("Parquet row-group bound exceeded.", resource="parquet_import", row_group=group)
                rows = 0
                for index, batch in enumerate(source.iter_batches(batch_size=max_batch_rows, use_threads=False)):
                    rows += batch.num_rows
                    if index >= max_batches or rows > max_rows or _charge(batch) > max_batch_bytes:
                        raise GrafxQueryBudgetExceeded("Parquet batch bound exceeded.", resource="parquet_import", batch=index)
                    _read_schema(batch.schema, schema, types, pa)
                    normalized = batch.cast(schema)
                    if _charge(normalized) > max_batch_bytes:
                        raise GrafxQueryBudgetExceeded("Parquet native batch bound exceeded.", resource="parquet_import")
                    yield normalized
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise GrafxUnsupportedOperation("Parquet input changed during consumption.", field="path")
                _unchanged_root(root, identity)
    except (OSError, pa.ArrowException) as failure:
        raise GrafxUnsupportedOperation("Local Parquet read failed.", operation="read_parquet", field="path") from failure


def import_parquet(transaction: Transaction, statement: str, path: str | os.PathLike[str], *,
                   allowed_root: str | os.PathLike[str], types: tuple[str | ArrowVectorType, ...],
                   max_batch_rows: int = 256, max_batch_bytes: int = 16 * 1024 * 1024,
                   max_rows: int = 1_000_000, max_batches: int = 4096,
                   max_file_bytes: int = 256 * 1024 * 1024,
                   max_row_group_bytes: int = 64 * 1024 * 1024) -> ExecuteManyReport:
    """Stage one complete local Parquet import atomically; never commit or retry for the caller."""
    with closing(read_parquet_batches(path, allowed_root=allowed_root, types=types,
            max_batch_rows=max_batch_rows, max_batch_bytes=max_batch_bytes, max_rows=max_rows,
            max_batches=max_batches, max_file_bytes=max_file_bytes, max_row_group_bytes=max_row_group_bytes)) as batches:
        return import_arrow_batches(transaction, statement, batches, types=types, max_batch_rows=max_batch_rows,
                                    max_batch_bytes=max_batch_bytes, max_rows=max_rows, max_batches=max_batches)


def write_parquet(source: QueryResult | QueryCursor, path: str | os.PathLike[str], *,
                  allowed_root: str | os.PathLike[str], types: tuple[str | ArrowVectorType, ...],
                  batch_rows: int = 256, max_batch_bytes: int = 16 * 1024 * 1024,
                  max_rows: int = 1_000_000, max_batches: int = 4096,
                  max_file_bytes: int = 256 * 1024 * 1024) -> ParquetExportReport:
    """Publish a complete new Parquet file atomically without overwrite; caller owns the cursor."""
    _options(batch_rows, max_batch_bytes, max_rows, max_batches, max_file_bytes)
    if type(source) not in (QueryResult, QueryCursor):
        raise GrafxConfigurationError("Parquet export requires a native result or cursor.", field="source")
    pa = _arrow()
    import pyarrow.parquet as pq
    schema = _schema(source.columns, types, pa)
    physical = _physical_schema(schema, types, pa)
    temporary = None
    try:
        target, root, identity = _path(path, allowed_root)
        if target.exists():
            raise GrafxUnsupportedOperation("Parquet destination already exists; no overwrite.", field="path")
        descriptor, name = tempfile.mkstemp(prefix=".grafx-parquet-", dir=root)
        temporary = Path(name)
        rows = count = 0
        with os.fdopen(descriptor, "w+b") as stream:
            with pq.ParquetWriter(stream, physical, compression=None) as writer:
                for batch in to_arrow_batches(source, types=types, batch_rows=batch_rows, max_batch_bytes=max_batch_bytes):
                    count += 1
                    rows += batch.num_rows
                    if count > max_batches or rows > max_rows or _charge(batch) > max_batch_bytes:
                        raise GrafxQueryBudgetExceeded("Parquet export bound exceeded.", resource="parquet_export", batch=count - 1)
                    writer.write_batch(batch.cast(physical), row_group_size=batch_rows)
                    if stream.tell() > max_file_bytes:
                        raise GrafxQueryBudgetExceeded("Parquet file bound exceeded.", resource="parquet_file")
            stream.flush()
            size = os.fstat(stream.fileno()).st_size
            if size > max_file_bytes:
                raise GrafxQueryBudgetExceeded("Parquet file bound exceeded.", resource="parquet_file")
            os.fsync(stream.fileno())
        _unchanged_root(root, identity)
        os.link(temporary, target)  # Atomic no-replace publication on the same local filesystem.
        return ParquetExportReport(str(target), rows, count, size)
    except (OSError, pa.ArrowException) as failure:
        raise GrafxUnsupportedOperation("Local Parquet export failed; no overwrite was attempted.", operation="write_parquet", field="path") from failure
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as failure:
                raise GrafxUnsupportedOperation("Parquet temporary cleanup failed; inspect the local root.", operation="write_parquet", field="path") from failure
