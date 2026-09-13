"""Versioned logical graph transfer through snapshots and ordinary transactions.

Local artifact IO belongs to this composition layer. No physical page, WAL record,
writer lease, old UUID or commit history is transplanted into the target database.
"""

from __future__ import annotations

from okto_grafx.domain.model.stored_types import stored_type_to_json, stored_type_from_json
from okto_grafx.domain.model.node_labels import decode_node_labels, encode_node_labels, validate_node_labels

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
from okto_grafx.domain.model.relationship_type import RelationshipTypeDef
from okto_grafx.domain.index.fulltext import (
    TextIndexOptions,
    decode_options,
    is_fulltext,
)
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef, SchemaType, encode_tuple
from okto_grafx.domain.model.value import (
    TEMPORAL_VALUE_TYPES,
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
from okto_grafx.engine.query_engine import QueryEngine, _AttachRelationshipType

__all__ = [
    "RecordIdMapping",
    "TransferLimits",
    "TransferReport",
    "export_graph",
    "import_graph",
]

_FORMAT = "okto-grafx-logical-1"
_MODEL_FORMAT = "okto-grafx-logical-2"
_NAMESPACE_FORMAT = "okto-grafx-logical-3"
_LABEL_FORMAT = "okto-grafx-logical-4"
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
    """One current logical identity remap, qualified by table name and entity kind."""

    table: str
    source_record_id: int
    target_record_id: int
    kind: str | None = None


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
                **({"decimal_precision": c.decimal_precision,
                    "decimal_scale": c.decimal_scale} if c.type is ValueType.DECIMAL else {}),
                **({"stored_type": stored_type_to_json(c.stored_type)} if c.stored_type is not None else {}),
            )
            for c in table.columns
        ]
        if table.flexible_properties:
            item.update(flexible_properties=True, unlabeled=table.unlabeled)
        if table.kind == "node" and catalog.requires_capability("node_labels_v1"):
            item["extra_node_labels"] = list(table.extra_node_labels)
        # Logical transfer writes complete decoded current tuples into a fresh
        # schema; source physical decode layouts are neither needed nor portable.
        if table.schema_layouts:
            item["schema_version"] = 1
        tables.append(item)
    spaces = []
    for space in catalog.spaces():
        item = asdict(space)
        item["metric"] = space.metric.value
        spaces.append(item)
    indexes = []
    namespace_overlap = catalog._has_namespace_overlap() or catalog.requires_capability("node_labels_v1")
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
                    catalog.table_by_id(index.table_id).columns[p].name
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
        if namespace_overlap:
            indexes[-1]["table_kind"] = catalog.table_by_id(index.table_id).kind
    result = dict(tables=tables, spaces=spaces, indexes=indexes)
    if catalog.relationship_types():
        result["relationship_types"] = [
            dict(name=group.name, members=[catalog.table_by_id(key).name for key in group.table_ids])
            for group in catalog.relationship_types()
        ]
    return result


def _tables(schema: dict) -> tuple[tuple[TableDef, ...], tuple[EmbeddingSpaceDef, ...]]:
    """Validate detached logical definitions using the native catalog invariants."""
    if type(schema) is not dict or set(schema) not in (
        {"tables", "spaces", "indexes"}, {"tables", "spaces", "indexes", "relationship_types"},
    ):
        raise _refuse("schema_invalid")
    if any(type(schema[key]) is not list for key in schema):
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
            ColumnDef(**{**c, "type": SchemaType.ANY if c["type"] == "ANY" else ValueType[c["type"]],
                         **({"stored_type": stored_type_from_json(c["stored_type"])} if "stored_type" in c else {})})
            for c in raw["columns"]
        )
        labels = {}
        if "extra_node_labels" in raw:
            if raw["kind"] != "node" or type(raw["extra_node_labels"]) is not list:
                raise _refuse("node_label_schema")
            labels["extra_node_labels"] = validate_node_labels(tuple(raw["extra_node_labels"]))
        table = TableDef(**{**raw, "columns": columns, **labels})
        # The import compiler currently recreates v1 immutable table definitions.
        if table.schema_version != 1:
            raise _refuse("unsupported_schema_version")
        tables.append(table)
    if (schema.get("relationship_types") or _physical_overlap(tables)
            or any("extra_node_labels" in raw for raw in schema["tables"]) or any(
        c.stored_type is not None or c.type in (SchemaType.ANY, ValueType.DECIMAL) or c.type in TEMPORAL_VALUE_TYPES
        for table in tables for c in table.columns
    )):
        catalog.upgrade_index_catalog(())
    for table in sorted(tables, key=lambda t: t.kind != "node"):
        catalog.add_table(table)
    for group in schema.get("relationship_types", ()):
        if (type(group) is not dict or set(group) != {"name", "members"}
                or type(group["members"]) is not list or not group["members"]
                or any(type(name) is not str for name in group["members"])):
            raise _refuse("relationship_type_invalid")
        catalog.add_relationship_type(RelationshipTypeDef(
            group["name"], tuple(sorted(catalog.table(name, kind="rel").table_id for name in group["members"])),
        ))
    for space in spaces:
        if not space.is_active:
            catalog.retire_space(space.name)
    for index in schema["indexes"]:
        fields = {
            "name",
            "table",
            "columns",
            "layout",
            "bucket_count",
            "expected_cardinality",
            "fulltext",
        }
        if type(index) is not dict or set(index) not in (fields, fields | {"table_kind"}):
            raise _refuse("schema_invalid")
        if "table_kind" in index and index["table_kind"] not in ("node", "rel"):
            raise _refuse("index_table_kind")
        indexed = catalog.table(index["table"], kind=index.get("table_kind"))
        if index["fulltext"] is None and indexed.kind != "node":
            raise _refuse("index_table_kind")
        if index["layout"] not in ("hash", "ordered", "sparse_hash", "posting_hash"):
            raise _refuse("unsupported_index_layout")
        if index["fulltext"] is not None:
            TextIndexOptions(
                **{
                    **index["fulltext"],
                    "field_weights": tuple(index["fulltext"]["field_weights"]),
                }
            )
    return tuple(tables), tuple(spaces)


def _physical_overlap(tables) -> bool:
    """Cross-kind spelling, never a substitute for full catalog validation."""
    return bool({t.name for t in tables if t.kind == "node"}
                & {t.name for t in tables if t.kind == "rel"})


def _schema_overlap(tables, groups) -> bool:
    return _physical_overlap(tables) or bool(
        {t.name for t in tables if t.kind == "node"} & {g["name"] for g in groups})


def _rows(
    storage: LocalStorageDevice, item: dict, table: TableDef, limits: TransferLimits
) -> Iterator[tuple[int, tuple[Value, ...], tuple[str, ...] | None]]:
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
        labels = None
        value_offset = endpoint_bytes
        if item.get("node_labels", False):
            if table.kind != "node" or not payload or payload[0] not in (0, 1):
                raise _refuse("node_label_frame")
            value_offset = 1
            if payload[0] == 1:
                labels, value_offset = decode_node_labels(payload, value_offset)
                if not table.admits_node_labels(labels):
                    raise _refuse("node_label_admission")
        values, end = decode_values(
            payload, len(table.columns) - (2 if endpoint_bytes else 0), value_offset
        )
        if endpoint_bytes:
            values = (*struct.unpack_from("<QQ", payload), *values)
        if end != length:
            raise _refuse("row_trailing_bytes")
        # Value-v1 also transports transient expression values. Stored schema
        # admission (including finite ANY/bag rules) must pass before import opens
        # a destination/workspace, not only when a later private batch is staged.
        if encode_tuple(table, values) != encode_values(values):
            # This is a stored row, not an assignment that may rescale DECIMAL.
            # Numerically equal frames with a different declaration must refuse.
            raise _refuse("row_schema_encoding")
        seen.add(record_id)
        count += 1
        if count > limits.max_rows:
            raise _refuse("row_budget")
        offset += _ROW.size + length
        digest.update(header)
        digest.update(payload)
        yield record_id, values, labels
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
        manifest["format"] not in (_FORMAT, _MODEL_FORMAT, _NAMESPACE_FORMAT, _LABEL_FORMAT)
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
    labeled = manifest["format"] == _LABEL_FORMAT
    for raw_table, table in zip(manifest["schema"]["tables"], tables, strict=True):
        if ("extra_node_labels" in raw_table) != (labeled and table.kind == "node"):
            raise _refuse("labels_require_format_4")
    if manifest["format"] not in (_NAMESPACE_FORMAT, _LABEL_FORMAT) and (
        _schema_overlap(tables, manifest["schema"].get("relationship_types", ()))
        or any("table_kind" in i for i in manifest["schema"]["indexes"])
    ):
        raise _refuse("namespaces_require_format_3")
    if manifest["format"] in (_NAMESPACE_FORMAT, _LABEL_FORMAT) and any(
        "table_kind" not in i for i in manifest["schema"]["indexes"]
    ):
        raise _refuse("index_table_kind")
    if manifest["format"] == _FORMAT and (
        "relationship_types" in manifest["schema"] or any(table.flexible_properties for table in tables)
    ):
        raise _refuse("model_requires_format_2")
    if type(manifest["objects"]) is not list or len(manifest["objects"]) != len(tables):
        raise _refuse("object_coverage")
    total = size
    count = 0
    for position, (item, table) in enumerate(
        zip(manifest["objects"], tables, strict=True)
    ):
        fields = {
            "table_id",
            "file",
            "bytes",
            "rows",
            "sha256",
        }
        if type(item) is not dict or set(item) != (fields | {"node_labels"} if labeled else fields):
            raise _refuse("object_fields")
        if labeled and (type(item["node_labels"]) is not bool or item["node_labels"] != (table.kind == "node")):
            raise _refuse("object_label_model")
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


def _row_payload(table: TableDef, values: tuple[Value, ...], *,
                 node_labels: tuple[str, ...] | None = None, labels_format: bool = False) -> bytes:
    """Frame membership explicitly and retain unsigned relationship endpoint identities."""
    if table.kind == "rel":
        if node_labels is not None:
            raise _refuse("node_label_frame")
        return struct.pack("<QQ", *values[:2]) + encode_values(values[2:])
    if node_labels is not None and (not labels_format or not table.admits_node_labels(node_labels)):
        raise _refuse("node_label_admission")
    prefix = (b"\0" if node_labels is None else b"\1" + encode_node_labels(node_labels)) if labels_format else b""
    return prefix + encode_values(values)


def export_graph(
    database: Database,
    destination: str | os.PathLike[str],
    *,
    limits: TransferLimits | None = None,
    history: str = "refuse",
) -> TransferReport:
    """Stream all current logical schema/rows/vectors from one fixed reader snapshot.

    Native node labels use format 4. Otherwise overlapping node/relationship namespaces use format 3.
    flexible models/logical groups use format 2 and ordinary typed ungrouped
    schemas retain format 1. Model/endpoint authority is preserved.
    Writers keep their normal protocol. Concurrent DDL causes a typed refusal, not
    mixed-schema output. A failed attempt leaves no published artifact; start a new
    attempt rather than resuming an expired reader cursor. Physical history and
    index generations are not exported. Custom active index declarations are.
    """
    budget = _limits(limits)
    if history not in ("refuse", "current-only") or type(history) is not str:
        raise GrafxConfigurationError("history must be refuse or current-only.", field="history")
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
            if database._catalog.catalog.system_history_tables() and history != "current-only":
                raise _refuse("system_history_requires_explicit_current_only")
            schema_before_snapshot = _schema(database._catalog.catalog)
        with LocalStorageDevice(staging) as artifact, database.begin("read") as reader:
            with database._transactions.page_access_section(
                transaction=reader._context
            ):
                catalog = database._catalog.catalog.copy()
                if catalog.system_history_tables() and history != "current-only":
                    raise _refuse("system_history_requires_explicit_current_only")
                schema = _schema(catalog)
                if schema != schema_before_snapshot:
                    raise _refuse("schema_changed")
            manifest = dict(
                format=(_LABEL_FORMAT if catalog.requires_capability("node_labels_v1") else
                        _NAMESPACE_FORMAT if catalog._has_namespace_overlap() else
                        _MODEL_FORMAT if catalog.relationship_types() or any(t.flexible_properties for t in catalog.tables()) else _FORMAT),
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
                        table.name, kind=table.kind, limit=budget.batch_rows, cursor=cursor
                    )
                    for row in page.rows:
                        payload = _row_payload(table, row.values, node_labels=row.node_labels,
                                               labels_format=manifest["format"] == _LABEL_FORMAT)
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
                        **({"node_labels": table.kind == "node"} if manifest["format"] == _LABEL_FORMAT else {}),
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
    groups: tuple[dict, ...] = (),
    *, labels_format: bool = False,
) -> None:
    """Reuse the native DDL journal, attachment rollback and WAL staging, not save()."""
    if labels_format or groups or _physical_overlap(tables) or any(
        c.stored_type is not None or c.type in (SchemaType.ANY, ValueType.DECIMAL) or c.type in TEMPORAL_VALUE_TYPES
        for table in tables for c in table.columns
    ):
        database.maintenance.ensure_identity_indexes()
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
                    CreateNodeTable(table.name, table.columns, table.primary_key,
                                    flexible_properties=table.flexible_properties, unlabeled=table.unlabeled)
                    if table.kind == "node"
                    else CreateRelTable(
                        table.name, table.from_table, table.to_table, table.columns,
                        flexible_properties=table.flexible_properties,
                    )
                )
                engine._schema(plan, context, {})
                if labels_format and table.kind == "node":
                    target = engine._working_catalog(context).table(table.name, kind="node")
                    engine._admit_node_labels(context, target.table_id, table.extra_node_labels)
            for group in groups:
                engine._schema(_AttachRelationshipType(group["name"], tuple(group["members"])), context, {})


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
    database: Database, table: TableDef, rows: list[tuple[int, tuple[Value, ...], tuple[str, ...] | None]]
) -> None:
    """Stage a private import batch through normal row quotas, OCC and commit validation."""
    with database.begin("write") as transaction:
        with database._transactions.page_access_section(
            transaction=transaction._context
        ):
            for record_id, values, labels in rows:
                transaction._context.stage_row_insert(
                    table, values, record_id=record_id, node_labels=labels
                )


def _import_into(
    database: Database,
    storage: LocalStorageDevice,
    manifest: dict,
    tables: tuple[TableDef, ...],
    spaces: tuple[EmbeddingSpaceDef, ...],
    limits: TransferLimits,
    *,
    resume: bool = False,
) -> tuple[RecordIdMapping, ...]:
    """Import nodes before edges, then rebuild indexes and restore retired-space state."""
    if not resume or not database._catalog.catalog.tables():
        _install_schema(database, tables, spaces, tuple(manifest["schema"].get("relationship_types", ())),
                        labels_format=manifest["format"] == _LABEL_FORMAT)
    target_catalog = database._catalog.catalog
    space_map = {s.space_id: target_catalog.space(s.name).space_id for s in spaces}
    identities: dict[tuple[str, str, int], int] = {}
    expected: dict[tuple[str, str, int], bytes] = {}
    objects = {
        t.table_id: item for t, item in zip(tables, manifest["objects"], strict=True)
    }
    existing = {}
    if resume:
        from okto_grafx.transfer_resume import validated_inventory

        existing = validated_inventory(
            database, storage, manifest, tables, spaces, limits
        )
    for table in sorted(tables, key=lambda t: t.kind != "node"):
        target_table = target_catalog.table(table.name, kind=table.kind)
        batch = []
        prior = existing.get((table.kind, table.name), ())
        ordinal = 0
        next_id = 0
        for record_id, values, labels in _rows(storage, objects[table.table_id], table, limits):
            if ordinal < len(prior):
                new_id = prior[ordinal]
            else:
                # A prior batch can advance the durable identity floor beyond a
                # dense integer sequence. Never synthesize IDs below that floor.
                if not batch:
                    with database._transactions.page_access_section(fresh_read_view=True):
                        next_id = database._heap.next_record_id(target_table)
                new_id = next_id
                next_id += 1
            identities[table.kind, table.name, record_id] = new_id
            values = _remap(values, space_map)
            if table.kind == "rel":
                if any(type(v) is not int for v in values[:2]):
                    raise _refuse("endpoint_invalid")
                try:
                    values = (
                        identities["node", table.from_table, values[0]],
                        identities["node", table.to_table, values[1]],
                        *values[2:],
                    )
                except KeyError as failure:
                    raise _refuse("endpoint_missing") from failure
            expected[table.kind, table.name, new_id] = hashlib.sha256(
                _row_payload(table, values, node_labels=labels, labels_format=objects[table.table_id].get("node_labels", False))
            ).digest()
            if not resume or ordinal >= len(prior):
                batch.append((new_id, values, labels))
            ordinal += 1
            if len(batch) >= limits.batch_rows:
                _stage_rows(database, target_table, batch)
                batch = []
        if batch:
            _stage_rows(database, target_table, batch)
    for index in manifest["schema"]["indexes"]:
        if resume and database._catalog.catalog.has_index_definition(index["name"]):
            continue  # validated_inventory proved the complete existing declaration
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
                kind=index.get("table_kind"),
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
                if not space.is_active and current.is_active:
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
                    table.name, kind=table.kind, limit=limits.batch_rows, cursor=cursor
                )
                for row in page.rows:
                    digest = expected.pop((table.kind, table.name, row.record_id), None)
                    if digest != hashlib.sha256(_row_payload(table, row.values, node_labels=row.node_labels,
                            labels_format=objects[table.table_id].get("node_labels", False))).digest():
                        raise _refuse("import_readback_mismatch")
                cursor = page.next_cursor
                if cursor is None:
                    break
    if expected or database.verify("all").findings:
        raise _refuse("import_verification_failed")
    return tuple(
        RecordIdMapping(table, source, target, kind=kind)
        for (kind, table, source), target in identities.items()
    )


def import_graph(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    limits: TransferLimits | None = None,
    resume_directory: str | os.PathLike[str] | None = None,
) -> TransferReport:
    """Verify a logical artifact and publish a separately writable fresh-UUID database.

    Accepts formats 1-4, validating stored values, node labels, physical kind and logical
    relationship membership before creation; table/record identities and endpoints
    are remapped.
    Destination must not exist. Batches commit only inside a private sibling; errors
    never publish a partial graph. Optional resume_directory retains a locked private
    workspace and verifies durable prefixes before continuing the same artifact;
    otherwise retry starts fresh. Mapping memory is bounded by max_rows. A completed
    artifact can be imported repeatedly into different new directories. The input is
    checksummed, not authenticated; keep it in a trusted, non-writable directory.
    """
    budget = _limits(limits)
    root = Path(source).resolve(strict=True)
    if resume_directory is not None:
        from okto_grafx.transfer_resume import resume_import

        try:
            return resume_import(root, destination, resume_directory, budget)
        except (KeyError, TypeError, ValueError, OverflowError, struct.error) as failure:
            raise _refuse("artifact_invalid") from failure
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
