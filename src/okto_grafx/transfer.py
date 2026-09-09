"""Versioned logical graph transfer through snapshots and ordinary transactions.

Local artifact IO belongs to this composition layer. No physical page, WAL record,
writer lease, old UUID or commit history is transplanted into the target database.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.api import connect
from okto_grafx.backup import _destination, _promote, _put
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxRecoveryRefused,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.index.fulltext import (
    TextIndexOptions,
    decode_options,
    is_fulltext,
)
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import (
    Value,
    ValueType,
    VectorValue,
    decode_values,
    encode_values,
)
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.query.plan import (
    CreateNodeTable,
    CreateRelTable,
    CreateVectorSpace,
)
from okto_grafx.engine.database import Database
from okto_grafx.engine.query_engine import QueryEngine

__all__ = [
    "RecordIdMapping",
    "TransferLimits",
    "TransferReport",
    "export_graph",
    "import_graph",
]

_FORMAT = "okto-grafx-logical-1"
_ROW = struct.Struct("<QI")
_MANIFEST_BYTES = 4 * 1024 * 1024


def _refuse(reason: str) -> GrafxRecoveryRefused:
    """Construct a typed refusal that never implies the source needs repair."""
    return GrafxRecoveryRefused(
        "Logical graph transfer refused; no destination was promoted.",
        operation="logical_transfer",
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class TransferLimits:
    """Artifact/work bounds, not a process RSS cap; checked before publication."""

    max_bytes: int = 256 * 1024 * 1024
    max_rows: int = 100_000
    max_row_bytes: int = 8 * 1024 * 1024
    batch_rows: int = 256

    def __post_init__(self) -> None:
        """Reject non-integer, empty and internally inconsistent budgets."""
        for name, value in asdict(self).items():
            if type(value) is not int or value <= 0:
                raise GrafxConfigurationError(
                    "A transfer limit must be a positive integer.", field=name
                )
        if self.max_row_bytes > self.max_bytes or self.max_row_bytes > 0xFFFFFFFF:
            raise GrafxConfigurationError(
                "max_row_bytes exceeds its enclosing bound.", field="max_row_bytes"
            )
        if self.batch_rows > self.max_rows:
            raise GrafxConfigurationError(
                "batch_rows exceeds max_rows.", field="batch_rows"
            )


@dataclass(frozen=True, slots=True)
class RecordIdMapping:
    """One current logical identity remap, qualified by the stable table name."""

    table: str
    source_record_id: int
    target_record_id: int


@dataclass(frozen=True, slots=True)
class TransferReport:
    """Completed artifact/import evidence, not a mapping of historical commits."""

    destination: str
    source_database_uuid: str
    source_snapshot_lsn: int
    target_database_uuid: str | None
    tables: int
    rows: int
    bytes: int
    manifest_sha256: str
    record_id_mapping: tuple[RecordIdMapping, ...] = ()


def _limits(value: TransferLimits | None) -> TransferLimits:
    """Detach and revalidate the public limits before any IO."""
    if value is None:
        return TransferLimits()
    if type(value) is not TransferLimits:
        raise GrafxConfigurationError("limits must be TransferLimits.", field="limits")
    return TransferLimits(**asdict(value))


def _json(value: object) -> bytes:
    """Encode the logical manifest canonically; non-finite schema values refuse."""
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _schema(catalog: Catalog) -> dict:
    """Project schema meaning, never physical catalog bytes or artifact nonces."""
    tables = []
    for table in catalog.tables():
        item = {
            key: getattr(table, key)
            for key in (
                "table_id",
                "name",
                "kind",
                "primary_key",
                "from_table",
                "to_table",
                "schema_version",
            )
        }
        item["columns"] = [
            dict(
                name=c.name,
                type=c.type.name,
                nullable=c.nullable,
                vector_space=c.vector_space,
            )
            for c in table.columns
        ]
        tables.append(item)
    spaces = []
    for space in catalog.spaces():
        item = asdict(space)
        item["metric"] = space.metric.value
        spaces.append(item)
    indexes = []
    for index in catalog.index_definitions():
        if index.automatic:
            continue
        active = [g for g in index.generations if g.planner_eligible]
        if len(active) != 1:
            raise _refuse("index_not_active")
        indexes.append(
            dict(
                name=index.name,
                table=index.table_name,
                columns=[
                    catalog.table(index.table_name).columns[p].name
                    for p in index.positions
                ],
                layout=index.layout.value,
                bucket_count=active[0].bucket_count,
                expected_cardinality=index.expected_cardinality,
                fulltext=asdict(decode_options(index.key_derivation))
                if is_fulltext(index.key_derivation)
                else None,
            )
        )
    return dict(tables=tables, spaces=spaces, indexes=indexes)


def _tables(schema: dict) -> tuple[tuple[TableDef, ...], tuple[EmbeddingSpaceDef, ...]]:
    """Validate detached logical definitions using the native catalog invariants."""
    if type(schema) is not dict or set(schema) != {"tables", "spaces", "indexes"}:
        raise _refuse("schema_invalid")
    tables = []
    spaces = []
    catalog = Catalog()
    for raw in schema["spaces"]:
        if (
            type(raw["space_id"]) is not int
            or type(raw["dimension"]) is not int
            or type(raw["normalized"]) is not bool
        ):
            raise _refuse("schema_invalid")
        space = EmbeddingSpaceDef(**{**raw, "metric": DistanceMetric(raw["metric"])})
        catalog.add_space(replace(space, state="active"))
        spaces.append(space)
    for raw in schema["tables"]:
        if type(raw["table_id"]) is not int or type(raw["schema_version"]) is not int:
            raise _refuse("schema_invalid")
        if any(type(c["nullable"]) is not bool for c in raw["columns"]):
            raise _refuse("schema_invalid")
        columns = tuple(
            ColumnDef(**{**c, "type": ValueType[c["type"]]}) for c in raw["columns"]
        )
        table = TableDef(**{**raw, "columns": columns})
        # The import compiler currently recreates v1 immutable table definitions.
        if table.schema_version != 1:
            raise _refuse("unsupported_schema_version")
        tables.append(table)
    for table in sorted(tables, key=lambda t: t.kind != "node"):
        catalog.add_table(table)
    for space in spaces:
        if not space.is_active:
            catalog.retire_space(space.name)
    for index in schema["indexes"]:
        if set(index) != {
            "name",
            "table",
            "columns",
            "layout",
            "bucket_count",
            "expected_cardinality",
            "fulltext",
        }:
            raise _refuse("schema_invalid")
        if index["layout"] not in ("hash", "ordered"):
            raise _refuse("unsupported_index_layout")
        if index["fulltext"] is not None:
            TextIndexOptions(
                **{
                    **index["fulltext"],
                    "field_weights": tuple(index["fulltext"]["field_weights"]),
                }
            )
    return tuple(tables), tuple(spaces)


def _rows(
    storage: LocalStorageDevice, item: dict, table: TableDef, limits: TransferLimits
) -> Iterator[tuple[int, tuple[Value, ...]]]:
    """Read one bounded row at a time and verify framing, count and whole-file digest."""
    size = storage.file_size(item["file"])
    if size != item["bytes"] or size > limits.max_bytes:
        raise _refuse("object_size")
    digest = hashlib.sha256()
    offset = count = 0
    seen = set()
    while offset < size:
        header = storage.read_log(item["file"], offset, _ROW.size)
        if len(header) != _ROW.size:
            raise _refuse("row_truncated")
        record_id, length = _ROW.unpack(header)
        if not 0 < record_id < 0xFFFFFFFFFFFFFFFF or record_id in seen:
            raise _refuse("row_identity")
        if length > limits.max_row_bytes or offset + _ROW.size + length > size:
            raise _refuse("row_size")
        payload = storage.read_log(item["file"], offset + _ROW.size, length)
        if len(payload) != length:
            raise _refuse("row_truncated")
        endpoint_bytes = 16 if table.kind == "rel" else 0
        if length < endpoint_bytes:
            raise _refuse("row_truncated")
        values, end = decode_values(
            payload, len(table.columns) - (2 if endpoint_bytes else 0), endpoint_bytes
        )
        if endpoint_bytes:
            values = (*struct.unpack_from("<QQ", payload), *values)
        if end != length:
            raise _refuse("row_trailing_bytes")
        seen.add(record_id)
        count += 1
        if count > limits.max_rows:
            raise _refuse("row_budget")
        offset += _ROW.size + length
        digest.update(header)
        digest.update(payload)
        yield record_id, values
    if count != item["rows"] or digest.hexdigest() != item["sha256"]:
        raise _refuse("object_checksum_or_count")


def _manifest(
    storage: LocalStorageDevice, limits: TransferLimits
) -> tuple[dict, bytes, tuple[TableDef, ...], tuple[EmbeddingSpaceDef, ...]]:
    """Read an exact versioned manifest; reject unknown fields and path injection."""
    size = storage.file_size("manifest.json")
    if not 0 < size <= min(_MANIFEST_BYTES, limits.max_bytes):
        raise _refuse("manifest_size")

    def unique(pairs: list[tuple[str, object]]) -> dict:
        """Reject ambiguous duplicate JSON keys rather than choosing a winner."""
        result = {}
        for key, value in pairs:
            if key in result:
                raise _refuse("manifest_duplicate_key")
            result[key] = value
        return result

    raw = storage.read_log("manifest.json", 0, size)
    manifest = json.loads(raw, object_pairs_hook=unique)
    if type(manifest) is not dict or set(manifest) != {
        "format",
        "value_codec",
        "source_uuid",
        "snapshot_lsn",
        "schema",
        "objects",
        "history",
    }:
        raise _refuse("manifest_fields")
    if (
        manifest["format"] != _FORMAT
        or manifest["value_codec"] != "grafx-value-v1"
        or manifest["history"] != "current-state-only"
    ):
        raise _refuse("unsupported_format")
    uuid = manifest["source_uuid"]
    if type(uuid) is not str or len(uuid) != 32 or bytes.fromhex(uuid).hex() != uuid:
        raise _refuse("source_identity")
    lsn = manifest["snapshot_lsn"]
    if type(lsn) is not int or not 0 <= lsn < 0xFFFFFFFFFFFFFFFF:
        raise _refuse("snapshot_lsn")
    tables, spaces = _tables(manifest["schema"])
    if type(manifest["objects"]) is not list or len(manifest["objects"]) != len(tables):
        raise _refuse("object_coverage")
    total = size
    count = 0
    for position, (item, table) in enumerate(
        zip(manifest["objects"], tables, strict=True)
    ):
        if type(item) is not dict or set(item) != {
            "table_id",
            "file",
            "bytes",
            "rows",
            "sha256",
        }:
            raise _refuse("object_fields")
        if (
            item["file"] != f"rows/{position:08d}.bin"
            or type(item["table_id"]) is not int
            or item["table_id"] != table.table_id
        ):
            raise _refuse("object_identity")
        if any(type(item[k]) is not int or item[k] < 0 for k in ("bytes", "rows")):
            raise _refuse("object_size")
        if type(item["sha256"]) is not str or len(item["sha256"]) != 64:
            raise _refuse("object_checksum")
        total += item["bytes"]
        count += item["rows"]
    if total > limits.max_bytes or count > limits.max_rows:
        raise _refuse("artifact_budget")
    return manifest, raw, tables, spaces


def _report(
    destination: Path,
    manifest: dict,
    raw: bytes,
    target_uuid: str | None = None,
    mapping: tuple[RecordIdMapping, ...] = (),
) -> TransferReport:
    """Describe only an artifact/directory whose publication has completed."""
    return TransferReport(
        str(destination),
        manifest["source_uuid"],
        manifest["snapshot_lsn"],
        target_uuid,
        len(manifest["objects"]),
        sum(i["rows"] for i in manifest["objects"]),
        len(raw) + sum(i["bytes"] for i in manifest["objects"]),
        hashlib.sha256(raw).hexdigest(),
        mapping,
    )


def _row_payload(table: TableDef, values: tuple[Value, ...]) -> bytes:
    """Preserve unsigned relationship identities outside the signed scalar value codec."""
    if table.kind == "rel":
        return struct.pack("<QQ", *values[:2]) + encode_values(values[2:])
    return encode_values(values)


def export_graph(
    database: Database,
    destination: str | os.PathLike[str],
    *,
    limits: TransferLimits | None = None,
) -> TransferReport:
    """Stream all current logical schema/rows/vectors from one fixed reader snapshot.

    Writers keep their normal protocol. Concurrent DDL causes a typed refusal, not
    mixed-schema output. A failed attempt leaves no published artifact; start a new
    attempt rather than resuming an expired reader cursor. Physical history and
    index generations are not exported. Custom active index declarations are.
    """
    budget = _limits(limits)
    source = (
        Path(database._storage.root).resolve(strict=True)
        if type(database._storage) is LocalStorageDevice
        else Path(destination).absolute().parent / ".nonlocal-source"
    )
    target = _destination(destination, source=source)
    with (
        database._public_operation("logical export"),
        tempfile.TemporaryDirectory(
            prefix=f".{target.name}.incomplete-", dir=target.parent
        ) as temp,
    ):
        staging = Path(temp) / "artifact"
        with database._transactions.page_access_section(fresh_read_view=True):
            schema_before_snapshot = _schema(database._catalog.catalog)
        with LocalStorageDevice(staging) as artifact, database.begin("read") as reader:
            with database._transactions.page_access_section(
                transaction=reader._context
            ):
                catalog = database._catalog.catalog.copy()
                schema = _schema(catalog)
                if schema != schema_before_snapshot:
                    raise _refuse("schema_changed")
            manifest = dict(
                format=_FORMAT,
                value_codec="grafx-value-v1",
                source_uuid=database.identity.database_uuid.hex(),
                snapshot_lsn=reader.snapshot.read_lsn,
                schema=schema,
                objects=[],
                history="current-state-only",
            )
            total = count = 0
            for position, table in enumerate(catalog.tables()):
                name = f"rows/{position:08d}.bin"
                artifact.create(name)
                digest = hashlib.sha256()
                rows = size = 0
                cursor = None
                while True:
                    page = reader.scan_rows_v1(
                        table.name, limit=budget.batch_rows, cursor=cursor
                    )
                    for row in page.rows:
                        payload = _row_payload(table, row.values)
                        if len(payload) > budget.max_row_bytes:
                            raise _refuse("row_size")
                        framed = _ROW.pack(row.record_id, len(payload)) + payload
                        total += len(framed)
                        count += 1
                        if total > budget.max_bytes or count > budget.max_rows:
                            raise _refuse("artifact_budget")
                        artifact.append_log(name, framed)
                        digest.update(framed)
                        rows += 1
                        size += len(framed)
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                artifact.durable_barrier(name)
                manifest["objects"].append(
                    dict(
                        table_id=table.table_id,
                        file=name,
                        bytes=size,
                        rows=rows,
                        sha256=digest.hexdigest(),
                    )
                )
            with database._transactions.page_access_section(
                transaction=reader._context, fresh_read_view=True
            ):
                if _schema(database._catalog.catalog) != schema:
                    raise _refuse("schema_changed")
            raw = _json(manifest)
            if len(raw) > _MANIFEST_BYTES or total + len(raw) > budget.max_bytes:
                raise _refuse("artifact_budget")
            _put(artifact, "manifest.json", raw)
            _manifest(artifact, budget)
            for item, table in zip(manifest["objects"], catalog.tables(), strict=True):
                for _ in _rows(artifact, item, table, budget):
                    pass
        _promote(staging, target)
    return _report(target, manifest, raw)


def _install_schema(
    database: Database,
    tables: tuple[TableDef, ...],
    spaces: tuple[EmbeddingSpaceDef, ...],
) -> None:
    """Reuse the native DDL journal, attachment rollback and WAL staging, not save()."""
    engine = database._queries
    if type(engine) is not QueryEngine:
        raise GrafxUnsupportedOperation(
            "Logical import requires the native schema compiler.",
            operation="logical_import",
        )
    with database.begin("write") as transaction:
        context = transaction._context
        with database._transactions.page_access_section(transaction=context):
            database._public_contexts.setdefault(context.txn_id, context)
            for space in spaces:
                engine._schema(
                    CreateVectorSpace(
                        space.name,
                        space.dimension,
                        space.metric,
                        space.normalized,
                        space.storage_dtype,
                    ),
                    context,
                    {},
                )
            for table in sorted(tables, key=lambda t: t.kind != "node"):
                plan = (
                    CreateNodeTable(table.name, table.columns, table.primary_key)
                    if table.kind == "node"
                    else CreateRelTable(
                        table.name, table.from_table, table.to_table, table.columns
                    )
                )
                engine._schema(plan, context, {})


def _remap(values: tuple[Value, ...], spaces: dict[int, int]) -> tuple[Value, ...]:
    """Remap vector identities even inside nested lists/maps without touching UUID values."""

    def visit(value: Value) -> Value:
        """Rebuild only container/vector values whose identities may need remapping."""
        if isinstance(value, VectorValue):
            return VectorValue(value.values, spaces[value.space_ref], value.dtype)
        if isinstance(value, (list, tuple)):
            return tuple(visit(v) for v in value)
        if isinstance(value, dict):
            return {visit(k): visit(v) for k, v in value.items()}
        return value

    return tuple(visit(v) for v in values)


def _stage_rows(
    database: Database, table: TableDef, rows: list[tuple[int, tuple[Value, ...]]]
) -> None:
    """Stage a private import batch through normal row quotas, OCC and commit validation."""
    with database.begin("write") as transaction:
        with database._transactions.page_access_section(
            transaction=transaction._context
        ):
            for record_id, values in rows:
                transaction._context.stage_row_insert(
                    table, values, record_id=record_id
                )


def _import_into(
    database: Database,
    storage: LocalStorageDevice,
    manifest: dict,
    tables: tuple[TableDef, ...],
    spaces: tuple[EmbeddingSpaceDef, ...],
    limits: TransferLimits,
) -> tuple[RecordIdMapping, ...]:
    """Import nodes before edges, then rebuild indexes and restore retired-space state."""
    _install_schema(database, tables, spaces)
    target_catalog = database._catalog.catalog
    space_map = {s.space_id: target_catalog.space(s.name).space_id for s in spaces}
    identities: dict[tuple[str, int], int] = {}
    expected: dict[tuple[str, int], bytes] = {}
    objects = {
        t.name: item for t, item in zip(tables, manifest["objects"], strict=True)
    }
    for table in sorted(tables, key=lambda t: t.kind != "node"):
        target_table = target_catalog.table(table.name)
        batch = []
        for record_id, values in _rows(storage, objects[table.name], table, limits):
            new_id = len(identities) + 1
            identities[table.name, record_id] = new_id
            values = _remap(values, space_map)
            if table.kind == "rel":
                if any(type(v) is not int for v in values[:2]):
                    raise _refuse("endpoint_invalid")
                try:
                    values = (
                        identities[table.from_table, values[0]],
                        identities[table.to_table, values[1]],
                        *values[2:],
                    )
                except KeyError as failure:
                    raise _refuse("endpoint_missing") from failure
            expected[table.name, new_id] = hashlib.sha256(
                encode_values(values)
            ).digest()
            batch.append((new_id, values))
            if len(batch) >= limits.batch_rows:
                _stage_rows(database, target_table, batch)
                batch = []
        if batch:
            _stage_rows(database, target_table, batch)
    for index in manifest["schema"]["indexes"]:
        if index["fulltext"] is not None:
            settings = {
                **index["fulltext"],
                "field_weights": tuple(index["fulltext"]["field_weights"]),
            }
            database.create_text_index(
                index["name"],
                index["table"],
                tuple(index["columns"]),
                options=TextIndexOptions(**settings),
                bucket_count=index["bucket_count"],
            )
            continue
        options = (
            {}
            if index["layout"] == "ordered"
            else dict(bucket_count=index["bucket_count"])
        )
        database.create_index(
            index["name"],
            index["table"],
            index["columns"],
            layout=index["layout"],
            **options,
        )
    # Retire only after values have been imported through the normal active-space validator.
    # Preserve creation metadata too; these edits occur on a detached catalog staged in WAL.
    with database.begin("write") as transaction:
        context = transaction._context
        with database._transactions.page_access_section(transaction=context):
            catalog = database._catalog.catalog.copy()
            for space in spaces:
                current = catalog.space(space.name)
                catalog._install_space(
                    replace(current, created_at_wall=space.created_at_wall)
                )
                if not space.is_active:
                    catalog.retire_space(space.name)
            for page, image in database._catalog.stage(catalog):
                database._transactions._stage_page_image(
                    context, database._catalog.file, page, image
                )
    with database.begin("read") as reader:
        for table in tables:
            cursor = None
            while True:
                page = reader.scan_rows_v1(
                    table.name, limit=limits.batch_rows, cursor=cursor
                )
                for row in page.rows:
                    digest = expected.pop((table.name, row.record_id), None)
                    if digest != hashlib.sha256(encode_values(row.values)).digest():
                        raise _refuse("import_readback_mismatch")
                cursor = page.next_cursor
                if cursor is None:
                    break
    if expected or database.verify("all").findings:
        raise _refuse("import_verification_failed")
    return tuple(
        RecordIdMapping(table, source, target)
        for (table, source), target in identities.items()
    )


def import_graph(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    limits: TransferLimits | None = None,
) -> TransferReport:
    """Verify a logical artifact and publish a separately writable fresh-UUID database.

    Destination must not exist. Batches commit only inside a private sibling; errors
    never publish a partial graph. Retry a failed attempt from the immutable artifact,
    not from private staging. Mapping memory is bounded by max_rows. A completed
    artifact can be imported repeatedly into different new directories. The input is
    checksummed, not authenticated; keep it in a trusted, non-writable directory.
    """
    budget = _limits(limits)
    root = Path(source).resolve(strict=True)
    target = _destination(destination, source=root)
    try:
        with LocalStorageDevice(root, create_root=False) as storage:
            manifest, raw, tables, spaces = _manifest(storage, budget)
            # Exhaust every stream before even creating a target database. A second
            # verification during import detects changed source bytes before promotion.
            for item, table in zip(manifest["objects"], tables, strict=True):
                for _ in _rows(storage, item, table, budget):
                    pass
            with tempfile.TemporaryDirectory(
                prefix=f".{target.name}.incomplete-", dir=target.parent
            ) as temp:
                staging = Path(temp) / "database"
                with connect(staging) as database:
                    mapping = _import_into(
                        database, storage, manifest, tables, spaces, budget
                    )
                    uuid = database.identity.database_uuid.hex()
                    if uuid == manifest["source_uuid"]:
                        raise _refuse("target_identity_not_fresh")
                    database.checkpoint()
                with connect(staging, read_only=True) as reopened:
                    if (
                        reopened.identity.database_uuid.hex() != uuid
                        or reopened.verify("all").findings
                    ):
                        raise _refuse("reopen_verification_failed")
                _promote(staging, target)
    except (KeyError, TypeError, ValueError, OverflowError, struct.error) as failure:
        raise _refuse("artifact_invalid") from failure
    return _report(target, manifest, raw, uuid, mapping)
