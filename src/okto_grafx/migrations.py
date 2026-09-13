"""Versioned application DDL with a transactional, checksum-verified ledger.

This is application metadata, not a substitute for the engine's disk-format upgrades.
Dry-run builds only detached domain catalogs: it never executes or rolls back DDL.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from okto_grafx.domain.errors import (
    GrafxConfigurationError, GrafxLedgerError, GrafxUnsupportedOperation,
    GrafxWriteConflict, GrafxPlanError,
)
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.relationship_type import RelationshipTypeDef
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef, is_identifier
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.query.ast import (
    CreateNodeTableStatement, CreateRelTableStatement, CreateVectorSpaceStatement,
)
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.plan import CreateNodeTable, CreateRelTable, CreateVectorSpace
from okto_grafx.domain.query.planner import build_plan
from okto_grafx.engine.database import Database, Transaction

__all__ = ["SchemaMigration", "MigrationReport", "migrate_schema"]

_PREFIX = "_grafx_migrations_"
_MAX_VERSIONS = 1024
_MAX_TEXT_BYTES = 1024 * 1024
_COLUMNS = (ColumnDef("version", ValueType.INT64, nullable=False), ColumnDef("checksum", ValueType.STRING))


@dataclass(frozen=True, slots=True)
class SchemaMigration:
    """One immutable, contiguous application version and its exact DDL strings."""

    version: int
    statements: tuple[str, ...]

    def __post_init__(self) -> None:
        """Capture only bounded immutable input; do not execute or bind schema here."""
        if type(self.version) is not int or not 1 <= self.version <= _MAX_VERSIONS:
            raise GrafxConfigurationError("Migration version must be in 1..1024.", field="version")
        if (type(self.statements) is not tuple or not 1 <= len(self.statements) <= 64
                or any(type(s) is not str or not s for s in self.statements)):
            raise GrafxConfigurationError("statements requires 1..64 nonempty strings in a tuple.", field="statements")
        if sum(len(s.encode("utf-8")) for s in self.statements) > _MAX_TEXT_BYTES:
            raise GrafxConfigurationError("Migration text exceeds 1 MiB.", field="statements")

    @property
    def checksum(self) -> str:
        """SHA-256 of version and exact text, including whitespace, in canonical JSON v1."""
        return hashlib.sha256(json.dumps(
            ["grafx-application-migration-v1", self.version, self.statements],
            ensure_ascii=True, separators=(",", ":"),
        ).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Observed versions and versions committed by this invocation; dry-run writes none.

    applied_lsns pairs only newly applied versions with their local COMMIT LSNs.
    They are database-local positions, not globally qualified commit identities.
    """

    namespace: str
    dry_run: bool
    previously_applied: tuple[int, ...]
    applied: tuple[int, ...]
    pending: tuple[int, ...]
    applied_lsns: tuple[tuple[int, int], ...]


def _ledger_error(reason: str) -> GrafxLedgerError:
    return GrafxLedgerError("Application migration ledger refused: " + reason,
                            operation="migrate_schema", reason=reason)


def _catalog(database: Database, transaction: Transaction) -> Catalog:
    """Capture complete native schema metadata in the migration's fenced snapshot.

    Rebuilding tables alone loses capability/group/index authority and makes a
    preview describe a different schema from the one execution will mutate.
    The copy has private dictionaries and grants no storage/transaction handle.
    """
    with database._transactions.page_access_section(transaction=transaction._context):
        return database._catalog.catalog.copy()


def _history(tx: Transaction, catalog: Catalog, ledger: str, owner: str,
             plan: tuple[SchemaMigration, ...]) -> tuple[int, ...]:
    if not catalog.has_table(ledger, kind="node"):
        return ()
    table = catalog.table(ledger, kind="node")
    if (table.kind != "node" or table.columns != _COLUMNS or table.primary_key != "version"):
        raise _ledger_error("unexpected_ledger_schema")
    rows = tx.execute(
        f"MATCH (m:{ledger}) RETURN m.version, m.checksum ORDER BY m.version LIMIT {len(plan) + 2}"
    ).rows
    if not rows or rows[0] != (0, owner):
        raise _ledger_error("missing_or_invalid_ownership")
    if len(rows) - 1 > len(plan):
        raise _ledger_error("database_ahead_of_plan")
    for expected, row in enumerate(rows[1:], 1):
        if row != (expected, plan[expected - 1].checksum):
            raise _ledger_error("history_gap_or_checksum_mismatch")
    return tuple(range(1, len(rows)))


def _simulate(catalog: Catalog, migrations: tuple[SchemaMigration, ...]) -> None:
    """Use the native planner/domain validators on an unconnected catalog copy."""
    for migration in migrations:
        for statement in migration.statements:
            node = build_plan(parse(statement), catalog=catalog).root
            if isinstance(node, CreateNodeTable):
                catalog.add_table(TableDef(table_id=catalog.next_table_id(), name=node.name,
                    kind="node", columns=node.columns, primary_key=node.primary_key))
            elif isinstance(node, CreateRelTable):
                if node.endpoint_pairs:
                    members = []
                    for source, target in node.endpoint_pairs:
                        key = catalog.next_table_id()
                        catalog.add_table(TableDef(key, f"_gx_rel_{key:08x}", "rel", node.columns,
                                                   from_table=source, to_table=target))
                        members.append(key)
                    catalog.add_relationship_type(RelationshipTypeDef(node.name, tuple(members)))
                else:
                    catalog.add_table(TableDef(table_id=catalog.next_table_id(), name=node.name,
                        kind="rel", columns=node.columns, from_table=node.from_table, to_table=node.to_table))
            elif isinstance(node, CreateVectorSpace):
                catalog.add_space(EmbeddingSpaceDef(space_id=catalog.next_space_id(), name=node.name,
                    dimension=node.dimension, metric=node.metric, normalized=node.normalized,
                    storage_dtype=node.storage_dtype))
            else:
                raise GrafxUnsupportedOperation("Migration requires additive schema DDL.", operation="migrate_schema")


def migrate_schema(database: Database, migrations: tuple[SchemaMigration, ...], *,
                   namespace: str, dry_run: bool = False, max_attempts: int = 3) -> MigrationReport:
    """Validate/apply a complete ordered 1..N plan, atomically per version.

    Supports CREATE NODE TABLE, CREATE REL TABLE and CREATE VECTOR SPACE only.
    dry_run validates checksum history and native schema plans without writing DDL,
    WAL or ledger rows. It cannot predict device failures, resource admission or
    subsequent concurrent schema changes. Retry only explicit OCC conflicts;
    earlier committed versions remain if a later version fails. Supply the same
    full plan again to resume. No autonomous background job or downgrade exists.
    """
    if type(namespace) is not str or len(namespace) > 32 or not is_identifier(namespace):
        raise GrafxConfigurationError("namespace requires an ASCII identifier of at most 32 characters.", field="namespace")
    if type(dry_run) is not bool or type(max_attempts) is not int or not 1 <= max_attempts <= 16:
        raise GrafxConfigurationError("dry_run must be bool and max_attempts in 1..16.", field="migration_options")
    if (type(migrations) is not tuple or len(migrations) > _MAX_VERSIONS
            or any(type(m) is not SchemaMigration or m.version != i
                   for i, m in enumerate(migrations, 1))):
        raise GrafxConfigurationError("Supply the complete contiguous tuple of versions 1..N (at most 1024).", field="migrations")
    if sum(len(s.encode("utf-8")) for m in migrations for s in m.statements) > _MAX_TEXT_BYTES:
        raise GrafxConfigurationError("Migration plan text exceeds 1 MiB.", field="migrations")
    for migration in migrations:
        for text in migration.statements:
            statement = parse(text)
            if not isinstance(statement, (CreateNodeTableStatement, CreateRelTableStatement, CreateVectorSpaceStatement)):
                raise GrafxUnsupportedOperation("Only additive CREATE NODE/REL TABLE or VECTOR SPACE is migratable.", operation="migrate_schema")
            if statement.name.lower().startswith(_PREFIX):
                raise GrafxConfigurationError("Migration ledger names are reserved.", field="name")
    ledger = _PREFIX + namespace
    owner = hashlib.sha256(("grafx-migration-ledger-v1:" + namespace).encode("ascii")).hexdigest()
    applied: list[int] = []
    lsns: list[tuple[int, int]] = []
    while True:
        for attempt in range(max_attempts):
            try:
                with database.begin("read" if dry_run else "write") as tx:
                    catalog = _catalog(database, tx)
                    if database.transactions.published_state().last_committed_lsn != tx.snapshot.read_lsn:
                        raise GrafxWriteConflict("Schema moved after opening the migration snapshot.")
                    seen = _history(tx, catalog, ledger, owner, migrations)
                    if database.transactions.published_state().last_committed_lsn != tx.snapshot.read_lsn:
                        raise GrafxWriteConflict("Schema moved while observing migration history.")
                    remaining = migrations[len(seen):]
                    _simulate(catalog.copy(), remaining)
                    if dry_run or not remaining:
                        return MigrationReport(namespace, dry_run,
                            tuple(v for v in seen if v not in applied), tuple(applied),
                            tuple(m.version for m in remaining), tuple(lsns))
                    if not catalog.has_table(ledger, kind="node"):
                        tx.execute(f"CREATE NODE TABLE {ledger}(version INT64, checksum STRING, PRIMARY KEY(version))")
                        tx.execute(f"CREATE (:{ledger} {{version:0, checksum:$hash}})", {"hash": owner})
                    next_step = remaining[0]
                    for statement in next_step.statements:
                        tx.execute(statement)
                    tx.execute(f"CREATE (:{ledger} {{version:$v, checksum:$hash}})",
                               {"v": next_step.version, "hash": next_step.checksum})
                report = tx.report
                if report is None or not report.durable:
                    raise _ledger_error("commit_not_durable")
                applied.append(next_step.version)
                lsns.append((next_step.version, report.csn))
                break
            except (GrafxWriteConflict, GrafxPlanError, GrafxLedgerError) as failure:
                # A foreign DDL may become visible between the observation and
                # native statement binding. Retry only with evidence of movement;
                # never classify a stable checksum/schema error as transient.
                moved = database.transactions.published_state().last_committed_lsn != tx.snapshot.read_lsn
                if (not isinstance(failure, GrafxWriteConflict) and not moved) or attempt + 1 == max_attempts:
                    raise
