"""Explicit bounded local CSV/JSONL ingestion, not external query scans or COPY."""

from __future__ import annotations

import base64
import csv
import json
import math
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from typing import NoReturn

from okto_grafx.parquet import _path, _regular, _unchanged_root
from okto_grafx.tabular import _limit
from okto_grafx.projections import _Work
from okto_grafx.domain.query.control import CancellationToken
from okto_grafx.domain.model.value import Timestamp, Uuid, encode_value
from okto_grafx.domain.model.temporal_interchange import TEMPORAL_CLASSES_BY_NAME, temporal_from_json_value
from okto_grafx.domain.model.decimal_interchange import decimal_from_json_value
from okto_grafx.domain.model.stored_types import StoredType, encode_stored_type, decode_stored_type
from okto_grafx.collection_json import collection_from_json_value
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.engine.database import Transaction, ExecuteManyReport
from okto_grafx.errors import (
    GrafxConfigurationError,
    GrafxUnsupportedOperation,
    GrafxQueryBudgetExceeded,
)

__all__ = [
    "TextImportLimits",
    "read_csv_batches",
    "import_csv",
    "read_jsonl_batches",
    "import_jsonl",
]


@dataclass(frozen=True, slots=True)
class TextImportLimits:
    """Local input and logical batch limits; not a process RSS or transaction-size promise."""

    batch_rows: int = 256
    max_batch_bytes: int = 16 * 1024 * 1024
    max_rows: int = 1_000_000
    max_batches: int = 4096
    max_file_bytes: int = 256 * 1024 * 1024
    max_record_bytes: int = 1024 * 1024
    max_field_bytes: int = 65536
    max_work: int = 10_000_000

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            _limit(
                name,
                getattr(self, name),
                65536 if name in ("batch_rows", "max_field_bytes") else 2**31,
            )


def _declarations(columns, types, limits):
    if (
        type(columns) is not tuple
        or not 1 <= len(columns) <= 256
        or any(type(c) is not str or not c or len(c) > 256 for c in columns)
        or len(set(columns)) != len(columns)
    ):
        raise GrafxConfigurationError(
            "Declare 1..256 unique column names.", field="columns"
        )
    if (
        type(types) is not tuple
        or len(types) != len(columns)
        or any(
            type(t) is not StoredType and (type(t) is not str
            or t not in ("BOOL", "INT64", "DOUBLE", "STRING", "BYTES", "UUID", "TIMESTAMP", "DECIMAL", *TEMPORAL_CLASSES_BY_NAME))
            for t in types
        )
    ):
        raise GrafxConfigurationError(
            "Declare one supported scalar or collection type per column.", field="types"
        )
    if type(limits) is not TextImportLimits:
        raise GrafxConfigurationError("Expected TextImportLimits.", field="limits")
    owned = []
    for kind in types:
        if type(kind) is StoredType:
            kind = decode_stored_type(encode_stored_type(kind))
            if kind.kind not in ("LIST", "MAP", "ARRAY", "STRUCT"):
                raise GrafxConfigurationError("StoredType text declarations require a collection root.", field="types")
        owned.append(kind)
    return tuple(owned)


def _value(value, kind, text, row, column, limits):
    try:
        if type(kind) is StoredType:
            if type(value) is str:
                if len(value) > limits.max_field_bytes or len(value.encode("utf-8")) > limits.max_field_bytes:
                    raise GrafxQueryBudgetExceeded("Collection field bound exceeded.", resource="text_field", row=row, column=column)
                value = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_nonfinite_json)
            return collection_from_json_value(kind, value, max_bytes=limits.max_field_bytes)
        if value is None:
            return None
        if type(value) is str and len(value.encode("utf-8")) > limits.max_field_bytes:
            raise GrafxQueryBudgetExceeded(
                "Text field bound exceeded.",
                resource="text_field",
                row=row,
                column=column,
            )
        if kind in TEMPORAL_CLASSES_BY_NAME or kind == "DECIMAL":
            if type(value) is str:
                value = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_nonfinite_json)
            # Canonical native admission bounds the shape before serializing its
            # primitive JSON fields to charge an object-valued JSONL cell.
            native = decimal_from_json_value(value) if kind == "DECIMAL" else temporal_from_json_value(kind, value)
            if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > limits.max_field_bytes:
                raise GrafxQueryBudgetExceeded("Native text field bound exceeded.", resource="text_field", row=row, column=column)
            value = native
        elif kind == "STRING":
            if type(value) is not str:
                raise ValueError("expected string")
        elif kind == "BOOL":
            if text:
                if value not in ("true", "false"):
                    raise ValueError("expected true/false")
                value = value == "true"
            elif type(value) is not bool:
                raise ValueError("expected boolean")
        elif kind in ("INT64", "TIMESTAMP"):
            if text:
                if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None:
                    raise ValueError("expected decimal integer")
                value = int(value)
            if type(value) is not int or not -(2**63) <= value < 2**63:
                raise ValueError("expected signed int64")
            if kind == "TIMESTAMP":
                value = Timestamp(value)
        elif kind == "DOUBLE":
            if text:
                if (
                    re.fullmatch(
                        r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value
                    )
                    is None
                ):
                    raise ValueError("expected finite decimal")
                value = float(value)
            if type(value) not in (float, int):
                raise ValueError("expected number")
            converted = float(value)
            if not math.isfinite(converted) or (
                type(value) is int and int(converted) != value
            ):
                raise ValueError("nonfinite/lossy integer conversion")
            value = converted
        elif kind == "BYTES":
            if type(value) is not str:
                raise ValueError("expected base64 string")
            value = base64.b64decode(value, validate=True)
        elif kind == "UUID":
            if type(value) is not str:
                raise ValueError("expected canonical UUID string")
            parsed = uuid.UUID(value)
            if str(parsed) != value:
                raise ValueError("expected canonical lowercase UUID")
            value = Uuid(parsed.bytes)
        encode_value(value)
        return value
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError, SchemaMismatchError) as failure:
        raise GrafxUnsupportedOperation(
            "Invalid typed text field.", field="types", row=row, column=column
        ) from failure


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys at every depth, including encoded native scalar cells."""
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _nonfinite_json(value: str) -> NoReturn:
    raise ValueError("nonstandard numeric constant")


def _lines(path, allowed_root, limits, work):
    """Read bounded UTF-8 physical lines with complete final source identity checks."""
    try:
        target, root, identity = _path(path, allowed_root)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(target, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not _regular(before):
                raise GrafxUnsupportedOperation(
                    "Expected regular local file.", field="path"
                )
            if before.st_size > limits.max_file_bytes:
                raise GrafxQueryBudgetExceeded(
                    "Text file bound exceeded.", resource="text_file"
                )
            _unchanged_root(root, identity)
            total = 0
            while True:
                work.step()
                raw = stream.readline(limits.max_record_bytes + 1)
                if not raw:
                    break
                total += len(raw)
                if len(raw) > limits.max_record_bytes or total > limits.max_file_bytes:
                    raise GrafxQueryBudgetExceeded(
                        "Text record/file bound exceeded.", resource="text_file"
                    )
                yield raw.decode("utf-8")
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_dev,
                after.st_ino,
            ):
                raise GrafxUnsupportedOperation(
                    "Text source changed during consumption.", field="path"
                )
            if target.stat().st_ino != before.st_ino:
                raise GrafxUnsupportedOperation(
                    "Text source was replaced.", field="path"
                )
            _unchanged_root(root, identity)
    except (OSError, UnicodeError) as failure:
        raise GrafxUnsupportedOperation(
            "Local UTF-8 text read failed.", field="path"
        ) from failure


def _batches(rows, columns, types, limits, work, text, null_token=None):
    batch, charge, count, batches = [], 4096, 0, 0
    for row_number, values in enumerate(rows, 1):
        work.step(len(columns))
        count += 1
        if count > limits.max_rows:
            raise GrafxQueryBudgetExceeded(
                "Text row bound exceeded.", resource="text_rows", row=row_number
            )
        if len(values) != len(columns):
            raise GrafxUnsupportedOperation(
                "Text row has wrong column count.", field="columns", row=row_number
            )
        converted = {
            name: _value(
                None if text and value == null_token else value,
                kind,
                text,
                row_number,
                name,
                limits,
            )
            for name, kind, value in zip(columns, types, values)
        }
        for name, kind in zip(columns, types, strict=True):
            if type(kind) is StoredType:
                work.step(len(encode_value(converted[name])))
        charge += 512 * len(columns) + sum(
            16 * len(encode_value(value)) for value in converted.values()
        )
        if charge > limits.max_batch_bytes:
            raise GrafxQueryBudgetExceeded(
                "Text logical batch bound exceeded.",
                resource="text_batch",
                row=row_number,
            )
        batch.append(converted)
        if len(batch) == limits.batch_rows:
            batches += 1
            if batches > limits.max_batches:
                raise GrafxQueryBudgetExceeded(
                    "Text batch count exceeded.", resource="text_batches"
                )
            yield tuple(batch)
            batch, charge = [], 4096
    if batch:
        if batches >= limits.max_batches:
            raise GrafxQueryBudgetExceeded(
                "Text batch count exceeded.", resource="text_batches"
            )
        yield tuple(batch)


def read_csv_batches(
    path: str | os.PathLike[str],
    *,
    allowed_root: str | os.PathLike[str],
    columns: tuple[str, ...],
    types: tuple[str | StoredType, ...],
    delimiter: str = ",",
    null_token: str = "\\N",
    limits: TextImportLimits = TextImportLimits(),
    cancellation: CancellationToken | None = None,
) -> Iterator[tuple[dict[str, object], ...]]:
    """Read header-required UTF-8 CSV with double quotes; close the iterator on early exit."""
    types = _declarations(columns, types, limits)
    if (
        type(delimiter) is not str
        or len(delimiter) != 1
        or not delimiter.isascii()
        or delimiter in '\r\n"\x00'
    ):
        raise GrafxConfigurationError(
            "Expected one ASCII delimiter other than quote/newline/NUL.",
            field="delimiter",
        )
    if (
        type(null_token) is not str
        or not null_token
        or len(null_token.encode("utf-8")) > limits.max_field_bytes
    ):
        raise GrafxConfigurationError(
            "Expected nonempty bounded NULL token.", field="null_token"
        )
    work = _Work(limits.max_work, cancellation)
    work.step(0)
    with closing(_lines(path, allowed_root, limits, work)) as lines:
        record_bytes = [0]

        def bounded_lines() -> Iterator[str]:
            """Charge multiline CSV records without changing process-global csv limits."""
            for line in lines:
                record_bytes[0] += len(line.encode("utf-8"))
                if record_bytes[0] > limits.max_record_bytes:
                    raise GrafxQueryBudgetExceeded(
                        "CSV multiline record bound exceeded.", resource="text_record"
                    )
                yield line

        reader = csv.reader(bounded_lines(), delimiter=delimiter, strict=True)
        try:
            if tuple(next(reader, ())) != columns:
                raise GrafxUnsupportedOperation(
                    "CSV header must exactly match declared columns.",
                    field="columns",
                    row=0,
                )

            def rows() -> Iterator[list[str]]:
                """Reset the aggregate bound at each CSV logical record."""
                while True:
                    record_bytes[0] = 0
                    row = next(reader, None)
                    if row is None:
                        return
                    yield row

            yield from _batches(rows(), columns, types, limits, work, True, null_token)
        except csv.Error as failure:
            raise GrafxUnsupportedOperation(
                "Malformed CSV record.", field="csv", line=reader.line_num
            ) from failure


def read_jsonl_batches(
    path: str | os.PathLike[str],
    *,
    allowed_root: str | os.PathLike[str],
    columns: tuple[str, ...],
    types: tuple[str | StoredType, ...],
    limits: TextImportLimits = TextImportLimits(),
    cancellation: CancellationToken | None = None,
) -> Iterator[tuple[dict[str, object], ...]]:
    """Read one typed row per UTF-8 line; missing and duplicate keys are errors.

    Explicit temporal/DECIMAL types accept canonical tagged objects or encoded JSON text;
    StoredType collection declarations decode exact nested values. Undeclared
    nested properties and implicit ISO/zone conversion are not inferred.
    """
    types = _declarations(columns, types, limits)
    work = _Work(limits.max_work, cancellation)
    work.step(0)

    with closing(_lines(path, allowed_root, limits, work)) as lines:

        def rows() -> Iterator[tuple[object, ...]]:
            """Localize malformed JSON without leaking a partially staged import."""
            for index, line in enumerate(lines, 1):
                try:
                    obj = json.loads(
                        line, object_pairs_hook=_unique_object, parse_constant=_nonfinite_json
                    )
                    if type(obj) is not dict or set(obj) != set(columns):
                        raise ValueError("object keys differ")
                    if any(type(kind) is not StoredType and (type(obj[name]) is list or type(obj[name]) is dict and kind not in (*TEMPORAL_CLASSES_BY_NAME, "DECIMAL"))
                           for name, kind in zip(columns, types)):
                        raise ValueError("nested value")
                except (ValueError, RecursionError) as failure:
                    raise GrafxUnsupportedOperation(
                        "Malformed typed JSONL object.", field="jsonl", row=index
                    ) from failure
                yield tuple(obj[c] for c in columns)

        yield from _batches(rows(), columns, types, limits, work, False)


def _import(transaction, statement, source):
    if type(transaction) is not Transaction:
        raise GrafxConfigurationError(
            "Expected native transaction.", field="transaction"
        )
    with closing(source):
        return transaction.executemany(
            statement, (row for batch in source for row in batch)
        )


def import_csv(
    transaction: Transaction,
    statement: str,
    path: str | os.PathLike[str],
    *,
    allowed_root: str | os.PathLike[str],
    columns: tuple[str, ...],
    types: tuple[str | StoredType, ...],
    delimiter: str = ",",
    null_token: str = "\\N",
    limits: TextImportLimits = TextImportLimits(),
    cancellation: CancellationToken | None = None,
) -> ExecuteManyReport:
    """Atomically stage one complete CSV file; caller owns transaction, commit and retry."""
    return _import(
        transaction,
        statement,
        read_csv_batches(
            path,
            allowed_root=allowed_root,
            columns=columns,
            types=types,
            delimiter=delimiter,
            null_token=null_token,
            limits=limits,
            cancellation=cancellation,
        ),
    )


def import_jsonl(
    transaction: Transaction,
    statement: str,
    path: str | os.PathLike[str],
    *,
    allowed_root: str | os.PathLike[str],
    columns: tuple[str, ...],
    types: tuple[str | StoredType, ...],
    limits: TextImportLimits = TextImportLimits(),
    cancellation: CancellationToken | None = None,
) -> ExecuteManyReport:
    """Atomically stage one complete JSON Lines file with caller-owned commit/retry."""
    return _import(
        transaction,
        statement,
        read_jsonl_batches(
            path,
            allowed_root=allowed_root,
            columns=columns,
            types=types,
            limits=limits,
            cancellation=cancellation,
        ),
    )
