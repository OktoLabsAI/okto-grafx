"""Bounded logical copy into an existing store with one native commit and indexed receipts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from collections.abc import Iterator
import hashlib
import json
import re
import struct

from okto_grafx.domain.txn.commit_identity import CommitId
from okto_grafx.domain.index.keys import index_key
from okto_grafx.domain.model.schema import (
    TableDef,
    EmbeddingSpaceDef,
    ColumnDef,
    encode_tuple,
)
from okto_grafx.domain.model.value import (
    ValueType,
    VectorValue,
    encode_values,
    decode_values,
)
from okto_grafx.domain.txn.commit_metadata import (
    CommitMetadata,
    capture_commit_metadata,
    decode_commit_metadata,
)
from okto_grafx.engine.database import Database, Transaction
from okto_grafx.engine.index_manager import primary_key_index_name
from okto_grafx.errors import (
    GrafxConfigurationError,
    GrafxLedgerError,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
    GrafxWriteConflict,
)
from okto_grafx.transfer import _remap

__all__ = [
    "CopyLimits",
    "CopyTable",
    "CopyPackage",
    "CopyReceipt",
    "capture_copy",
    "prepare_copy_target",
    "copy_graph",
]

_LEDGER = "_grafx_copy_receipts_v1"
_OWNER = "grafx-copy-receipts-v1"
_PROOF = "grafx_copy_v1"
_COLUMNS = (
    ColumnDef("key", ValueType.STRING, nullable=False),
    ColumnDef("digest", ValueType.STRING),
    ColumnDef("source", ValueType.STRING),
    ColumnDef("target", ValueType.STRING),
    ColumnDef("rows", ValueType.INT64),
)


def _invalid(field):
    return GrafxConfigurationError("Invalid bounded copy input.", field=field)


def _ledger_error(reason):
    return GrafxLedgerError(
        "Catalog copy receipt refused.", operation="catalog_copy", reason=reason
    )


@dataclass(frozen=True, slots=True)
class CopyLimits:
    """Whole-package logical bounds, not process RSS or durable transaction quota overrides."""

    max_rows: int = 10_000
    max_bytes: int = 32 * 1024 * 1024
    max_row_bytes: int = 4 * 1024 * 1024
    max_tables: int = 64

    def __post_init__(self) -> None:
        for name, ceiling in (
            ("max_rows", 1_000_000),
            ("max_bytes", 2**30),
            ("max_row_bytes", 2**30),
            ("max_tables", 256),
        ):
            if (
                type(getattr(self, name)) is not int
                or not 1 <= getattr(self, name) <= ceiling
            ):
                raise _invalid(name)
        if self.max_row_bytes > self.max_bytes:
            raise _invalid("max_row_bytes")


@dataclass(frozen=True, slots=True)
class CopyTable:
    """One immutable schema plus native encoded rows; record IDs remain source-qualified."""

    schema: TableDef
    rows: tuple[tuple[int, bytes], ...]


@dataclass(frozen=True, slots=True)
class CopyPackage:
    """Detached retry input; checksummed, not authenticated or a physical backup."""

    source_commit: CommitId
    tables: tuple[CopyTable, ...]
    spaces: tuple[EmbeddingSpaceDef, ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class CopyReceipt:
    """One proven target commit; replayed=True adds no new target effects."""

    source_commit: CommitId
    target_commit: CommitId
    request_sha256: str
    rows: int
    replayed: bool
    skipped_rows: int = 0


def _limits(limits):
    if type(limits) is not CopyLimits:
        raise _invalid("limits")
    return CopyLimits(**asdict(limits))


def _shape(table):
    return (
        table.name,
        table.kind,
        table.primary_key,
        table.from_table,
        table.to_table,
        tuple((c.name, c.type.name, c.nullable, c.vector_space) for c in table.columns),
    )


def _json(value):
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _digest(source, tables, spaces, limits):
    digest = hashlib.sha256(b"grafx-copy-package-v1")
    size = 0
    count = 0

    def feed(raw: bytes) -> None:
        """Charge one length-delimited package component before hashing it."""
        nonlocal size
        size += len(raw) + 8
        if size > limits.max_bytes:
            raise _invalid("max_bytes")
        digest.update(struct.pack("<Q", len(raw)))
        digest.update(raw)

    feed(source.to_token().encode("ascii"))
    feed(
        _json(
            [
                (
                    s.space_id,
                    s.name,
                    s.dimension,
                    s.metric.value,
                    s.normalized,
                    s.storage_dtype,
                    s.state,
                    s.created_at_wall,
                )
                for s in spaces
            ]
        )
    )
    for item in tables:
        feed(
            _json(
                (_shape(item.schema), item.schema.table_id, item.schema.schema_version)
            )
        )
        for record_id, raw in item.rows:
            count += 1
            if count > limits.max_rows or len(raw) > limits.max_row_bytes:
                raise _invalid("row_limits")
            feed(struct.pack("<Q", record_id) + raw)
    return digest.hexdigest()


def _capture_schema(table):
    if type(table) is not TableDef or any(
        type(c) is not ColumnDef for c in table.columns
    ):
        raise _invalid("schema")
    return replace(table, columns=tuple(replace(c) for c in table.columns))


def _values(table, raw):
    values, end = decode_values(raw, len(table.columns))
    if end != len(raw) or encode_values(values) != raw:
        raise _invalid("row_values")
    encode_tuple(table, values)  # native positional types/nullability; no writes
    return values


def _validate(package, limits):
    if type(package) is not CopyPackage or type(package.source_commit) is not CommitId:
        raise _invalid("package")
    source = CommitId(
        package.source_commit.database_uuid, package.source_commit.sequence
    )
    if (
        type(package.tables) is not tuple
        or not 1 <= len(package.tables) <= limits.max_tables
    ):
        raise _invalid("tables")
    if type(package.spaces) is not tuple or len(package.spaces) > 256:
        raise _invalid("spaces")
    spaces = tuple(
        replace(s) if type(s) is EmbeddingSpaceDef else None for s in package.spaces
    )
    if (
        any(s is None for s in spaces)
        or len({s.space_id for s in spaces}) != len(spaces)
        or len({s.name for s in spaces}) != len(spaces)
    ):
        raise _invalid("spaces")
    tables = []
    count = 0
    byte_count = 0
    identities = set()
    names = set()
    for item in package.tables:
        if type(item) is not CopyTable or type(item.rows) is not tuple:
            raise _invalid("tables")
        table = _capture_schema(item.schema)
        if table.kind == "node" and table.primary_key is None:
            raise GrafxUnsupportedOperation(
                "Copy v1 requires node primary keys for logical endpoint resolution.",
                operation="catalog_copy",
            )
        if table.name.lower().startswith("_grafx_") or table.name in names:
            raise _invalid("table_name")
        names.add(table.name)
        count += len(item.rows)
        if count > limits.max_rows:
            raise _invalid("max_rows")
        for row in item.rows:
            if type(row) is not tuple or len(row) != 2:
                raise _invalid("row")
            rid, raw = row
            if type(rid) is not int or not 0 < rid < 2**64 or type(raw) is not bytes:
                raise _invalid("row")
            byte_count += len(raw)
            if len(raw) > limits.max_row_bytes or byte_count > limits.max_bytes:
                raise _invalid("row_limits")
            if (table.name, rid) in identities:
                raise _invalid("duplicate_record_id")
            identities.add((table.name, rid))
            _values(table, raw)
        tables.append(CopyTable(table, item.rows))
    node_ids = {
        (item.schema.name, rid)
        for item in tables
        if item.schema.kind == "node"
        for rid, _ in item.rows
    }
    node_tables = {item.schema.name for item in tables if item.schema.kind == "node"}
    for item in tables:
        if item.schema.kind == "rel":
            if (
                item.schema.from_table not in node_tables
                or item.schema.to_table not in node_tables
            ):
                raise _invalid("endpoint_tables")
            for _, raw in item.rows:
                values = _values(item.schema, raw)
                if (
                    type(values[0]) is not int
                    or type(values[1]) is not int
                    or (item.schema.from_table, values[0]) not in node_ids
                    or (item.schema.to_table, values[1]) not in node_ids
                ):
                    raise _invalid("endpoint_closure")
    actual = _digest(source, tables, spaces, limits)
    if type(package.sha256) is not str or package.sha256 != actual:
        raise _invalid("package_sha256")
    return CopyPackage(source, tuple(tables), spaces, actual)


def capture_copy(
    source: Transaction, *, tables: tuple[str, ...], limits: CopyLimits = CopyLimits(),
    record_ids: dict[str, tuple[int, ...]] | None = None, history: str = "refuse",
) -> CopyPackage:
    """Capture tables or explicit RID subsets from one native read snapshot.

    A subset must name every selected table and include all selected relationship
    endpoints. Node IDs use active identity indexes; other tables use a bounded
    scan charged against the same row/byte limits. Missing IDs refuse. Empty tuples
    select no rows. Native system history requires explicit
    ``history='current-only'``; history is never copied or invented at the target.
    """
    limits = _limits(limits)
    if type(source) is not Transaction or not source.active or source.mode != "read":
        raise GrafxTransactionStateError(
            "Copy capture requires an active native read transaction."
        )
    if (
        type(tables) is not tuple
        or not 1 <= len(tables) <= limits.max_tables
        or any(type(t) is not str for t in tables)
        or len(set(tables)) != len(tables)
    ):
        raise _invalid("tables")
    if type(history) is not str or history not in ("refuse", "current-only"):
        raise _invalid("history")
    if record_ids is not None:
        if type(record_ids) is not dict or set(record_ids) != set(tables):
            raise _invalid("record_ids")
        record_ids = dict(record_ids)
        for ids in record_ids.values():
            if (type(ids) is not tuple or len(ids) > limits.max_rows
                    or any(type(rid) is not int or not 0 < rid < 2**64 for rid in ids)
                    or len(set(ids)) != len(ids)):
                raise _invalid("record_ids")
        if sum(map(len, record_ids.values())) > limits.max_rows:
            raise _invalid("max_rows")
    history = source.commit_history(limit=1)
    identity = CommitId(history.database_uuid, history.read_sequence)
    if source.lookup_commit(identity) is None:
        raise GrafxUnsupportedOperation(
            "Source snapshot needs tracked commit provenance.", operation="capture_copy"
        )
    db = source._database
    with db._transactions.page_access_section(transaction=source._context):
        catalog = db._catalog.catalog
        schemas = tuple(_capture_schema(catalog.table(name)) for name in sorted(tables))
        spaces = tuple(replace(s) for s in catalog.spaces())
        if history != "current-only" and any(s.table_id in {key for key, _, _ in catalog.system_history_tables()} for s in schemas):
            raise GrafxUnsupportedOperation("Temporal tables require history='current-only' for logical copy.", operation="capture_copy")
    copied = []
    count = 0
    byte_count = 0
    for schema in schemas:
        rows = []
        cursor = None
        from okto_grafx.domain.index.catalog import identity_index_name
        from okto_grafx.domain.index.keys import record_id_key
        if record_ids is not None and catalog.has_index_definition(identity_index_name(schema.table_id)):
            with db._transactions.page_access_section(transaction=source._context):
                for rid in sorted(record_ids[schema.name]):
                    found = db._indexes.lookup_versions(identity_index_name(schema.table_id), record_id_key(rid), source.snapshot)
                    if len(found) != 1:
                        raise _invalid("missing_record_id")
                    raw = encode_values(found[0][1].values)
                    count += 1
                    byte_count += len(raw)
                    if count > limits.max_rows or len(raw) > limits.max_row_bytes or byte_count > limits.max_bytes:
                        raise _invalid("row_limits")
                    rows.append((rid, raw))
            copied.append(CopyTable(schema, tuple(rows)))
            continue
        while True:
            if record_ids is not None and not record_ids[schema.name]:
                break
            page = source.scan_rows_v1(
                schema.name,
                limit=min(256, limits.max_rows + 1 - count),
                cursor=cursor,
                max_batch_bytes=limits.max_bytes,
            )
            for row in page.rows:
                raw = encode_values(row.values)
                count += 1
                byte_count += len(raw)
                if (
                    count > limits.max_rows
                    or len(raw) > limits.max_row_bytes
                    or byte_count > limits.max_bytes
                ):
                    raise _invalid("row_limits")
                if record_ids is None or row.record_id in record_ids[schema.name]:
                    rows.append((row.record_id, raw))
            cursor = page.next_cursor
            if cursor is None:
                break
        if record_ids is not None and {rid for rid, _ in rows} != set(record_ids[schema.name]):
            raise _invalid("missing_record_id")
        copied.append(CopyTable(schema, tuple(sorted(rows))))
    with db._transactions.page_access_section(transaction=source._context):
        if (
            any(db._catalog.catalog.table(s.name) != s for s in schemas)
            or tuple(db._catalog.catalog.spaces()) != spaces
        ):
            raise GrafxWriteConflict(
                "Schema changed during copy capture; recapture explicitly."
            )
    package = CopyPackage(
        identity, tuple(copied), spaces, _digest(identity, copied, spaces, limits)
    )
    return _validate(package, limits)


def _schema(tx):
    db = tx._database
    with db._transactions.page_access_section(transaction=tx._context):
        catalog = db._catalog.catalog
        if not catalog.has_table(_LEDGER):
            raise GrafxUnsupportedOperation(
                "Call prepare_copy_target explicitly first.", operation="catalog_copy"
            )
        table = catalog.table(_LEDGER)
        if (
            table.kind != "node"
            or table.primary_key != "key"
            or table.columns != _COLUMNS
        ):
            raise _ledger_error("schema_mismatch")


def _row(tx, key):
    db = tx._database
    with db._transactions.page_access_section(transaction=tx._context):
        db._transactions._require_current_active(tx._context)
        name = primary_key_index_name(_LEDGER)
        table = db._catalog.catalog.table(_LEDGER)
        definition = db._indexes.active_index(name).definition
        if (
            definition.table_id != table.table_id
            or definition.table_name != table.name
            or definition.positions != (0,)
        ):
            raise _ledger_error("index_identity_mismatch")
        # Exact-index validation returns the heap version inside the certificate.
        rows = db._indexes.lookup_versions(name, index_key((key,), (0,)), tx.snapshot)
        if len(rows) > 1:
            raise _ledger_error("duplicate_key")
        return rows[0][1] if rows else None


def _owner(tx):
    _schema(tx)
    owner = _row(tx, "owner")
    expected = ("owner", _OWNER, "", tx._database.identity.database_uuid.hex(), 0)
    if owner is None or owner.values != expected:
        raise _ledger_error("owner_mismatch")


def prepare_copy_target(database: Database) -> None:
    """Explicitly enable existing provenance capabilities and initialize the ordinary receipt ledger."""
    if type(database) is not Database or database.read_only or database.closed:
        raise _invalid("database")
    database.ensure_identity_indexes()
    database.enable_commit_history()
    with database.begin("write") as tx:
        with database._transactions.page_access_section(transaction=tx._context):
            exists = database._catalog.catalog.has_table(_LEDGER)
        if exists:
            _owner(tx)
            tx.rollback()
            return
        tx.execute(
            f"CREATE NODE TABLE {_LEDGER}(key STRING, digest STRING, source STRING, target STRING, rows INT64, PRIMARY KEY(key))"
        )
        tx.execute(
            f"CREATE (:{_LEDGER} {{key:'owner', digest:$d, source:'', target:$t, rows:0}})",
            {"d": _OWNER, "t": database.identity.database_uuid.hex()},
        )


def _request(package, target_uuid, key, metadata, conflict="fail"):
    if type(key) is not str or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}", key
    ):
        raise _invalid("idempotency_key")
    raw = capture_commit_metadata(metadata)
    original = CommitMetadata() if raw is None else decode_commit_metadata(raw)
    if _PROOF in original.attributes:
        raise _invalid("metadata")
    digest = hashlib.sha256(
        _json(("grafx-copy-request-v1", package.sha256, target_uuid.hex(), conflict, key))
        + original.canonical_bytes
    ).hexdigest()
    recorded = CommitMetadata(
        actor=original.actor,
        origin=original.origin,
        correlation_id=original.correlation_id,
        reason=original.reason,
        attributes={**dict(original.attributes), _PROOF: digest},
    )
    return digest, recorded


def _receipt(tx, package, key, digest, recorded, conflict="fail"):
    _owner(tx)
    row = _row(tx, "request:" + key)
    if row is None:
        return None
    uuid = tx._database.identity.database_uuid
    count = sum(len(t.rows) for t in package.tables)
    applied = row.values[4]
    if type(applied) is not int or not 0 <= applied <= count or conflict == "fail" and applied != count:
        raise _ledger_error("receipt_row_count")
    expected = (
        "request:" + key,
        digest,
        package.source_commit.to_token(),
        uuid.hex(),
        applied,
    )
    if row.values != expected:
        raise _ledger_error("idempotency_input_mismatch")
    identity = CommitId(uuid, row.xmin)
    evidence = tx.lookup_commit(identity)
    if (
        evidence is None
        or evidence.metadata is None
        or evidence.metadata != recorded
    ):
        raise _ledger_error("commit_proof_mismatch")
    return CopyReceipt(package.source_commit, identity, digest, applied, True, count - applied)


def _stage(tx, package, conflict="fail"):
    db = tx._database
    with db._transactions.page_access_section(transaction=tx._context):
        catalog = db._catalog.catalog
        targets = {}
        for item in package.tables:
            target = catalog.table(item.schema.name)
            if _shape(target) != _shape(item.schema):
                raise _invalid("target_schema")
            targets[item.schema.name] = target
        used = set()

        def visit(value: object) -> None:
            """Collect embedding-space references from all nested copied values."""
            if isinstance(value, VectorValue):
                used.add(value.space_ref)
            elif isinstance(value, (list, tuple)):
                for v in value:
                    visit(v)
            elif isinstance(value, dict):
                for k, v in value.items():
                    visit(k)
                    visit(v)

        for item in package.tables:
            for _, raw in item.rows:
                visit(_values(item.schema, raw))
        mapping = {}
        for space in package.spaces:
            if space.space_id not in used:
                continue
            target = catalog.space(space.name)
            if (
                target.dimension,
                target.metric,
                target.normalized,
                target.storage_dtype,
            ) != (
                space.dimension,
                space.metric,
                space.normalized,
                space.storage_dtype,
            ) or not target.is_active:
                raise _invalid("target_space")
            mapping[space.space_id] = target.space_id
        if used != mapping.keys():
            raise _invalid("source_space")
    # All mutation goes through ordinary query statements: raw intent staging
    # alone does not perform query uniqueness checks or note OCC partitions.
    identities = {}
    total_created = 0
    for item in sorted(package.tables, key=lambda t: t.schema.kind != "node"):
        table = item.schema
        offset = 0 if table.kind == "node" else 2
        props = ", ".join(
            f"{c.name}:$p{i}" for i, c in enumerate(table.columns[offset:], offset)
        )
        if table.kind == "node":
            statement = f"CREATE (:{table.name} {{{props}}})"
        else:
            left, right = targets[table.from_table], targets[table.to_table]
            statement = (
                f"MATCH (a:{left.name}), (b:{right.name}) "
                f"WHERE a.{left.primary_key}=$source AND b.{right.primary_key}=$target "
                f"CREATE (a)-[:{table.name} {{{props}}}]->(b)"
            )

        skipped = set()
        if conflict == "skip" and table.kind == "node":
            seen_keys = set()
            for rid, raw in item.rows:
                values = _remap(_values(table, raw), mapping)
                pk = values[table.column_index(table.primary_key)]
                key = index_key((pk,), (0,))
                present = key in seen_keys or bool(tx.execute(
                    f"MATCH (n:{table.name}) WHERE n.{table.primary_key}=$key RETURN n.{table.primary_key}", {"key": pk}).rows)
                seen_keys.add(key)
                if present:
                    skipped.add(rid)
        admitted = []
        def parameters() -> Iterator[dict[str, object]]:
            """Remap each detached row into the target statement's native parameters."""
            for rid, raw in item.rows:
                values = _remap(_values(table, raw), mapping)
                row = {f"p{i}": value for i, value in enumerate(values) if i >= offset}
                if table.kind == "node":
                    identities[table.name, rid] = values[
                        table.column_index(table.primary_key)
                    ]
                    if rid in skipped:
                        continue
                else:
                    row["source"] = identities[table.from_table, values[0]]
                    row["target"] = identities[table.to_table, values[1]]
                admitted.append(rid)
                yield row

        report = tx.executemany(statement, parameters())
        counter = "rows_created" if table.kind == "node" else "relationships_created"
        if report.statistics.get(counter, 0) != len(admitted):
            raise _ledger_error("endpoint_or_row_count_mismatch")
        total_created += len(admitted)
    return total_created


def copy_graph(
    package: CopyPackage,
    target: Database,
    *,
    idempotency_key: str,
    conflict: str = "fail",
    metadata: CommitMetadata | None = None,
    limits: CopyLimits = CopyLimits(),
) -> CopyReceipt:
    """Copy into pre-existing compatible tables with data+receipt in one native durable commit.

    Retry the same package/key after an uncertain ACK. No automatic retries, schema
    creation, receipt expiration or cross-store atomicity; target preparation is explicit.
    """
    if type(target) is not Database:
        raise _invalid("target")
    return _copy_with_begin(
        package,
        target,
        target.begin,
        idempotency_key=idempotency_key,
        conflict=conflict,
        metadata=metadata,
        limits=limits,
    )


def _copy_with_begin(
    package,
    target,
    begin,
    *,
    idempotency_key,
    conflict="fail",
    metadata=None,
    limits=CopyLimits(),
):
    """Shared composition for standalone and lifecycle-tracked session transactions."""
    limits = _limits(limits)
    package = _validate(package, limits)
    if type(target) is not Database or target.closed or target.read_only:
        raise _invalid("target")
    if type(conflict) is not str or conflict not in ("fail", "skip"):
        raise GrafxUnsupportedOperation(
            "Copy supports conflict='fail' or 'skip'.", operation="catalog_copy"
        )
    if target.identity.database_uuid == package.source_commit.database_uuid:
        raise _invalid("same_store")
    digest, recorded = _request(
        package, target.identity.database_uuid, idempotency_key, metadata, conflict
    )
    with begin("write", metadata=recorded) as tx:
        if tx._database is not target:
            raise GrafxTransactionStateError(
                "Target attachment changed before copy began."
            )
        prior = _receipt(tx, package, idempotency_key, digest, recorded, conflict)
        if prior is not None:
            tx.rollback()
            return prior
        count = _stage(tx, package, conflict)
        tx.execute(
            f"CREATE (:{_LEDGER} {{key:$k, digest:$d, source:$s, target:$t, rows:$n}})",
            {
                "k": "request:" + idempotency_key,
                "d": digest,
                "s": package.source_commit.to_token(),
                "t": target.identity.database_uuid.hex(),
                "n": count,
            },
        )
    if tx.report is None or not tx.report.durable:
        raise _ledger_error("commit_not_durable")
    return CopyReceipt(
        package.source_commit,
        CommitId(target.identity.database_uuid, tx.report.csn),
        digest,
        count,
        False,
        sum(len(t.rows) for t in package.tables) - count,
    )
