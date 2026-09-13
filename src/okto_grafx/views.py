"""Bounded durable logical read views composed over native transactions."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
import hashlib
import json
from typing import TYPE_CHECKING, Mapping

from okto_grafx.domain.model.schema import (
    ColumnDef,
    TableDef,
    encode_tuple,
    is_identifier,
)
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.query.ast import (
    Query,
    UnionQuery,
    NodePattern,
    RelationshipPattern,
    SubqueryClause,
)
from okto_grafx.domain.query.analysis import analyze
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.effects import is_deterministic
from okto_grafx.domain.query.planner import build_plan
from okto_grafx.domain.query.scopes import _owned_nodes
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxLedgerError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.txn.context import TransactionMode

if TYPE_CHECKING:
    from okto_grafx.engine.database import Database, Transaction
    from okto_grafx.engine.query_engine import QueryResult

__all__ = ["ViewParameter", "ViewDefinition", "LogicalViews"]

_TABLE = "_grafx_views_v1"
_OWNER = "grafx-logical-views-v1"
_COLUMNS = (
    ColumnDef("name", ValueType.STRING, nullable=False),
    ColumnDef("body", ValueType.STRING),
    ColumnDef("sha256", ValueType.STRING),
)
_TYPES = (
    ValueType.BOOL,
    ValueType.INT64,
    ValueType.DOUBLE,
    ValueType.STRING,
    ValueType.BYTES,
    ValueType.TIMESTAMP,
    ValueType.UUID,
)


def _bad(field):
    return GrafxConfigurationError("Invalid bounded logical view input.", field=field)


def _refuse(reason):
    return GrafxLedgerError(
        "Logical view refused.", operation="logical_view", reason=reason
    )


def _name(name):
    if not is_identifier(name) or len(name) > 64 or name.startswith("_"):
        raise _bad("name")
    return name


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash(text):
    return hashlib.sha256(text.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class ViewParameter:
    """An exact native scalar type; all declared arguments are required, even if nullable."""

    name: str
    type: ValueType
    nullable: bool = False

    def __post_init__(self) -> None:
        _name(self.name)
        if (
            type(self.type) is not ValueType
            or self.type not in _TYPES
            or type(self.nullable) is not bool
        ):
            raise _bad("parameter_type")


@dataclass(frozen=True, slots=True)
class ViewDefinition:
    """Detached definition; sorted name/hash pairs retain every physical dependency.

    Names can repeat across kinds. Each complete schema hash includes kind and
    physical identity; callers must not collapse these pairs into a name-keyed dict.
    """

    name: str
    query: str
    parameters_schema: tuple[ViewParameter, ...]
    columns: tuple[str, ...]
    dependencies: tuple[tuple[str, str], ...]
    sha256: str


def _parsed(query, parameters):
    if type(query) is not str or not query or len(query) > 65536:
        raise _bad("query")
    try:
        if len(query.encode("utf-8")) > 65536:
            raise _bad("query")
    except UnicodeEncodeError as exc:
        raise _bad("query") from exc
    if (
        type(parameters) is not tuple
        or len(parameters) > 32
        or any(type(p) is not ViewParameter for p in parameters)
        or len({p.name for p in parameters}) != len(parameters)
    ):
        raise _bad("parameters_schema")
    statement = parse(query)
    if not is_deterministic(statement):
        raise GrafxUnsupportedOperation(
            "Logical views require deterministic expressions.", operation="logical_view",
        )
    # Audit every nested read, not just the outer RETURN branches. Otherwise a CALL
    # could hide an unlabelled scan whose dependencies expand after unrelated DDL.
    branches = []
    pending = [statement]
    while pending:
        branch = pending.pop()
        if isinstance(branch, UnionQuery):
            pending.extend(reversed(branch.branches()))
        else:
            branches.append(branch)
            if isinstance(branch, Query):
                pending.extend(clause.query for clause in branch.ordered_clauses()
                               if isinstance(clause, SubqueryClause))
    if any(
        not isinstance(b, Query) or b.writes or b.return_clause is None
        for b in branches
    ):
        raise GrafxUnsupportedOperation(
            "Views require read-only MATCH/RETURN queries.", operation="logical_view"
        )
    # Include expression-local reads (EXISTS/pattern predicates/comprehensions),
    # not only top-level MATCH and CALL. Never enter parameter/literal payloads.
    for node in _owned_nodes(statement):
        if isinstance(node, Query) and node.writes:
            raise GrafxUnsupportedOperation("Views require read-only queries.", operation="logical_view")
        if isinstance(node, (NodePattern, RelationshipPattern)):
            names = node.labels if isinstance(node, NodePattern) else node.types
            if not names:
                raise GrafxUnsupportedOperation(
                    "View patterns require explicit base labels and types.", operation="logical_view",
                )
            if any(name.startswith("_grafx_") for name in names):
                raise GrafxUnsupportedOperation(
                    "Views cannot depend on reserved metadata tables.", operation="logical_view",
                )
    analysis = analyze(statement)
    if set(analysis.parameters) != {p.name for p in parameters}:
        raise _bad("parameters_schema")
    return statement


def _binding(tx, query, parameters):
    statement = _parsed(query, parameters)
    catalog = tx._database._catalog.catalog
    plan = build_plan(statement, catalog=catalog)
    dependencies = {}

    def record(table: TableDef) -> None:
        """Record a bounded schema dependency and refuse reserved metadata owners."""
        if table.name.startswith("_grafx_"):
            raise GrafxUnsupportedOperation(
                "Views cannot depend on reserved metadata tables.", operation="logical_view",
            )
        dependencies[table.table_id] = (table.name, _hash(_json(asdict(table))))
        if len(dependencies) > 64:
            raise _bad("dependencies")

    for node in plan.root.walk():
        for field in fields(node):
            value = getattr(node, field.name)
            candidates = value if isinstance(value, tuple) else (value,)
            for table in candidates:
                if isinstance(table, TableDef):
                    record(table)
    # Some expression subplans are compiled only at execution, so PlanNode.walk
    # alone cannot prove their schema closure. Explicit syntax supplies kind;
    # catalog resolution supplies physical identity and complete logical groups.
    for node in _owned_nodes(statement):
        if isinstance(node, NodePattern):
            for name in node.labels:
                if catalog.has_table(name, kind="node"):
                    record(catalog.table(name, kind="node"))
        elif isinstance(node, RelationshipPattern):
            for name in node.types:
                for table in catalog.relationship_tables(name):
                    record(table)
    return plan.columns, tuple(sorted(dependencies.values()))


def _body(query, parameters, columns, dependencies):
    text = _json(
        [
            1,
            query,
            [[p.name, int(p.type), p.nullable] for p in parameters],
            columns,
            dependencies,
        ]
    )
    if len(text) > 262144:
        raise _bad("definition_bytes")
    return text


def _decode(name, body, digest):
    if (
        type(body) is not str
        or len(body) > 262144
        or not body.isascii()
        or _hash(body) != digest
    ):
        raise _refuse("definition_checksum")
    try:
        version, query, raw_parameters, columns, dependencies = json.loads(body)
        if (
            type(version) is not int
            or version != 1
            or len(raw_parameters) > 32
            or len(dependencies) > 64
        ):
            raise ValueError
        parameters = tuple(
            ViewParameter(n, ValueType(t), nullable)
            for n, t, nullable in raw_parameters
        )
        _parsed(query, parameters)
        if (
            not isinstance(columns, list)
            or any(type(c) is not str for c in columns)
            or any(
                not isinstance(d, list)
                or len(d) != 2
                or any(type(v) is not str for v in d)
                for d in dependencies
            )
        ):
            raise ValueError
        result = ViewDefinition(
            _name(name),
            query,
            parameters,
            tuple(columns),
            tuple(map(tuple, dependencies)),
            digest,
        )
        if _body(query, parameters, result.columns, result.dependencies) != body:
            raise ValueError
        return result
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise _refuse("definition_encoding") from exc


def _schema(tx):
    catalog = tx._database._catalog.catalog
    if not catalog.has_table(_TABLE, kind="node"):
        raise _refuse("not_prepared")
    table = catalog.table(_TABLE, kind="node")
    if table.kind != "node" or table.columns != _COLUMNS or table.primary_key != "name":
        raise _refuse("unexpected_schema")
    rows = tx.execute(
        f"MATCH (v:{_TABLE}) WHERE v.name=$name RETURN v.body,v.sha256 LIMIT 2",
        {"name": "_owner"},
    ).rows
    if rows != ((_OWNER, _hash(_OWNER)),):
        raise _refuse("invalid_owner")


def _get(tx, name):
    rows = tx.execute(
        f"MATCH (v:{_TABLE}) WHERE v.name=$name RETURN v.body,v.sha256 LIMIT 2",
        {"name": "v_" + name},
    ).rows
    if len(rows) > 1:
        raise _refuse("duplicate_definition")
    return None if not rows else _decode(name, *rows[0])


class LogicalViews:
    """Database-owned logical view operations; no separately closable resources or caches."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @contextmanager
    def _read(self, snapshot):
        from okto_grafx.engine.database import Transaction

        if snapshot is not None:
            if (
                not isinstance(snapshot, Transaction)
                or snapshot._database is not self._database
                or snapshot._context.mode is not TransactionMode.READ
            ):
                raise _bad("snapshot")
            snapshot._require_active()
            yield snapshot
        else:
            with self._database.begin("read") as tx:
                yield tx

    def prepare(self) -> None:
        """Create the owned registry in one native transaction; repeat adds no commit."""
        db = self._database
        with db.begin("write") as tx:
            with db._transactions.page_access_section(transaction=tx._context):
                exists = db._catalog.catalog.has_table(_TABLE, kind="node")
                if exists:
                    _schema(tx)
            if exists:
                tx.rollback()
                return
            tx.execute(
                f"CREATE NODE TABLE {_TABLE}(name STRING, body STRING, sha256 STRING, PRIMARY KEY(name))"
            )
            tx.execute(
                f"CREATE (:{_TABLE} {{name:'_owner',body:$body,sha256:$hash}})",
                {"body": _OWNER, "hash": _hash(_OWNER)},
            )

    def create(
        self,
        name: str,
        *,
        query: str,
        parameters_schema: tuple[ViewParameter, ...] = (),
        replace: bool = False,
    ) -> ViewDefinition:
        """Validate and atomically create/replace one definition; no automatic OCC retry."""
        _name(name)
        _parsed(query, parameters_schema)
        if type(replace) is not bool:
            raise _bad("replace")
        db = self._database
        with db.begin("write") as tx:
            with db._transactions.page_access_section(transaction=tx._context):
                _schema(tx)
                previous = _get(tx, name)
                if previous is not None and not replace:
                    raise _refuse("already_exists")
                columns, dependencies = _binding(tx, query, parameters_schema)
                body = _body(query, parameters_schema, columns, dependencies)
                digest = _hash(body)
                values = {"name": "v_" + name, "body": body, "hash": digest}
                if previous is None:
                    tx.execute(
                        f"CREATE (:{_TABLE} {{name:$name,body:$body,sha256:$hash}})",
                        values,
                    )
                else:
                    tx.execute(
                        f"MATCH (v:{_TABLE}) WHERE v.name=$name SET v.body=$body,v.sha256=$hash",
                        values,
                    )
        return ViewDefinition(
            name, query, parameters_schema, columns, dependencies, digest
        )

    def get(
        self, name: str, *, snapshot: Transaction | None = None
    ) -> ViewDefinition | None:
        """Inspect one checked definition at a read snapshot; None means no visible name."""
        _name(name)
        with self._read(snapshot) as tx:
            with self._database._transactions.page_access_section(
                transaction=tx._context
            ):
                _schema(tx)
                return _get(tx, name)

    def list(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
        snapshot: Transaction | None = None,
    ) -> tuple[ViewDefinition, ...]:
        """Bounded lexicographic introspection; reuse one snapshot across stable pages."""
        if after is not None:
            _name(after)
        if type(limit) is not int or not 1 <= limit <= 1024:
            raise _bad("limit")
        with self._read(snapshot) as tx:
            with self._database._transactions.page_access_section(
                transaction=tx._context
            ):
                _schema(tx)
                rows = tx.execute(
                    f"MATCH (v:{_TABLE}) WHERE v.name>$after RETURN v.name,v.body,v.sha256 ORDER BY v.name LIMIT {limit}",
                    {"after": "v_" + (after or "")},
                ).rows
                if any(not row[0].startswith("v_") for row in rows):
                    raise _refuse("unexpected_registry_row")
                return tuple(
                    _decode(name[2:], body, digest) for name, body, digest in rows
                )

    def drop(self, name: str) -> bool:
        """Atomically remove a definition; absent returns False without a new commit."""
        _name(name)
        db = self._database
        with db.begin("write") as tx:
            with db._transactions.page_access_section(transaction=tx._context):
                _schema(tx)
                exists = _get(tx, name) is not None
                if exists:
                    tx.execute(
                        f"MATCH (v:{_TABLE}) WHERE v.name=$name DELETE v",
                        {"name": "v_" + name},
                    )
            if not exists:
                tx.rollback()
        return exists

    def execute(
        self,
        name: str,
        parameters: Mapping[str, object] | None = None,
        *,
        snapshot: Transaction | None = None,
        timeout_seconds: float | None = None,
    ) -> QueryResult:
        """Execute a validated read definition at one native snapshot, never open a writer."""
        _name(name)
        if parameters is not None and (
            not isinstance(parameters, Mapping) or len(parameters) > 32
        ):
            raise _bad("parameters")
        values = {} if parameters is None else dict(parameters)
        with self._read(snapshot) as tx:
            with self._database._transactions.page_access_section(
                transaction=tx._context
            ):
                _schema(tx)
                definition = _get(tx, name)
                if definition is None:
                    raise _refuse("not_found")
                if set(values) != {p.name for p in definition.parameters_schema}:
                    raise _bad("parameters")
                for p in definition.parameters_schema:
                    encode_tuple(
                        TableDef(
                            1,
                            "parameter",
                            "node",
                            (ColumnDef(p.name, p.type, nullable=p.nullable),),
                        ),
                        (values[p.name],),
                    )
                columns, dependencies = _binding(
                    tx, definition.query, definition.parameters_schema
                )
                if (
                    columns != definition.columns
                    or dependencies != definition.dependencies
                ):
                    raise _refuse("stale_dependency")
                return tx.execute(
                    definition.query, values, timeout_seconds=timeout_seconds
                )
