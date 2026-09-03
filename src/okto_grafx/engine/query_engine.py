"""The query engine: parse, plan, explain and execute (CONTRACT.md section 8.9).

Four doors, and the first three are pure. :meth:`QueryEngine.parse` reads text into a statement,
:meth:`QueryEngine.plan` binds that statement to the catalog and returns ONE operator tree, and
:meth:`QueryEngine.explain` is the two of them together with nothing in between -- so the tree a
caller inspects is the tree the executor walks, which is the whole substance of SPEC-VEC AC-7.

:meth:`QueryEngine.execute` is the only door that touches state, and it touches it through the
components that own it: the heap for rows, the index framework for lookups, the vector subsystem
for similarity, the catalog store for schema. Nothing here reads a page directly and nothing here
decides visibility on its own -- the snapshot the transaction carries is the only view, and every
row that reaches a caller passed through it.

**How the two index contracts are honoured.** An index seek asks the index manager's paired
``lookup``/``lookup_versions`` doors, which are where C7's dual rule of CONTRACT.md section 8.7 is
applied: an EXACT hit is a candidate validated against the heap under this snapshot, while a
PROXIMITY hit is already decided by its birth stamp and tombstone. The paired exact door returns
the immutable version that discharged that proof, avoiding a second heap read. This engine does
not reimplement either visibility rule; it records the contract in the plan and dispatches to the
component that owns it.

**How the similarity operator stays one pass.** The candidate set is the rows the operator's CHILD
produced, handed to the vector subsystem as a
:class:`~okto_grafx.domain.vector.filter.RecordIdFilter`, so the two-regime planner chooses its
regime from the real filtered cardinality rather than from a guess, and no vector is fetched for a
row the query had already excluded (SPEC-VEC FR-4, BR-6, AC-7).

**What this engine refuses rather than guesses.** Writing rows needs three clauses that do not
exist yet, and each refusal names the one it waits on rather than answering with a row that would
be wrong:

* **A stored relationship carries no endpoint identity.** CONTRACT.md section 7.2 gives a
  relationship table its two endpoint TABLES and its property columns, and fixes no place for the
  identities of the two rows a stored relationship connects.
* **Nothing allocates a record identity.** ``RecordId`` is "stable logical identity across
  versions" (section 3) and no component assigns one.
* **A row cannot be written at its commit number.** ``HeapStore.insert`` takes ``xmin`` when the
  row is written, and the commit number is assigned inside ``TransactionManager.commit``
  (section 8.5 step 3.4). No commit hook exists -- the transaction manager holds an
  ``index_manager`` and never calls ``commit(txn, csn)`` on it -- so a row written before the
  commit carries a birth stamp no snapshot rule can make correct.

Schema statements have none of those problems and are implemented: a catalog change is a whole
structure rather than a versioned row, and ``CatalogStore.save()`` already returns the pages of
its chain, which is exactly what section 8.5 step 4 needs in order to log the write.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from math import isnan
from typing import cast

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxEmbeddingSpaceMismatch,
    GrafxError,
    GrafxPlanError,
    GrafxQueryBudgetExceeded,
    GrafxQueryError,
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import NO_CSN, RecordId, RecordRef
from okto_grafx.domain.index.keys import index_key
from okto_grafx.domain.index.definition import (
    automatic_index_definitions,
    index_definition_matches_table,
)
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.engine.index_manager import (
    edge_from_index_name,
    edge_to_index_name,
    primary_key_index,
    primary_key_index_name,
    relationship_endpoint_indexes,
)
from okto_grafx.domain.model.record import HeapVersion
from okto_grafx.domain.model.schema import (
    ENDPOINT_COLUMNS,
    ENDPOINT_COLUMN_COUNT,
    ColumnDef,
    EmbeddingSpaceDef,
    TableDef,
    encode_tuple,
)
from okto_grafx.domain.model.value import (
    INT64_MAX,
    INT64_MIN,
    Timestamp,
    Value,
    ValueType,
    VectorValue,
    encode_value,
    value_type_of,
)
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricsSink
from okto_grafx.domain.txn.context import (
    SIZING_ENDPOINT,
    PendingRowRef,
    RowIntent,
    RowOperation,
)
from okto_grafx.domain.txn.intents import reduce_row_intents
from okto_grafx.domain.query.analysis import Aggregation, QueryAnalysis, analyze
from okto_grafx.domain.query.ast import (
    Direction,
    BinaryOperation,
    CaseExpression,
    Expression,
    FunctionCall,
    ListExpression,
    Literal,
    MapExpression,
    NullCheck,
    Parameter,
    Property,
    SortItem,
    Statement,
    Subscript,
    UnaryOperation,
    Variable,
    walk,
)
from okto_grafx.domain.query.limits import (
    MAX_NAME_CHARACTERS,
    MAX_PROJECTION_ITEMS,
    MAX_RENDERED_QUERY_CHARACTERS,
)
from okto_grafx.domain.query.parser import parse as parse_text
from okto_grafx.domain.query.plan import (
    AggregateRows,
    AllNodesScan,
    CreateNodeTable,
    CreatedNode,
    CreatedRelationship,
    CreateRelationships,
    CreateRelTable,
    CreateVectorSpace,
    DeleteEntities,
    DistinctRows,
    EagerRows,
    FilterRows,
    IndexSeek,
    LimitRows,
    MergePattern,
    NodeScan,
    OptionalRows,
    PlanNode,
    ProduceResults,
    ProjectRows,
    PropertyAssignment,
    RelationshipScan,
    SetProperties,
    SingleRow,
    SkipRows,
    SortRows,
    TraverseAnyRelationship,
    TraverseRelationship,
    UnionRows,
    UnwindRows,
    VectorSearch,
    WithRows,
    validate_plan,
)
from okto_grafx.domain.query.planner import (
    union_common_type,
    SCORE_COLUMN,
    PlannedQuery,
    build_plan,
    case_comparison_type,
    case_result_type,
    coalesce_result_type,
    subscript_argument_types,
)
from okto_grafx.domain.query.tokens import (
    AGGREGATE_FUNCTIONS,
    COALESCE_FUNCTION,
    SIMILARITY_FUNCTION,
    SIMILARITY_SCORE_FUNCTION,
    LABEL_FUNCTION,
    SIZE_FUNCTION,
    STRING_SPLIT_FUNCTION,
    TIMESTAMP_FUNCTION,
)
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.metrics_catalog import MetricEmitter, metric

__all__ = [
    "MISSING_COMMIT_HOOK",
    "MISSING_RECORD_IDENTITY",
    "PHASE_EXECUTE",
    "PHASE_PARSE",
    "PHASE_PLAN",
    "QUERY_METRICS",
    "QueryEngine",
    "QueryResult",
    "RowBinding",
    "materialise_row",
]

PHASE_PARSE: str = "parse"
"""The phase that reads text into a statement."""

PHASE_PLAN: str = "plan"
"""The phase that binds a statement to the catalog and chooses the operator tree."""

PHASE_EXECUTE: str = "execute"
"""The phase that walks the tree and produces rows."""

_PHASE_DURATION = metric("oktografx_query_phase_duration_seconds").name
_ROWS_RETURNED = metric("oktografx_query_rows_returned_count").name
_ERRORS_TOTAL = metric("oktografx_query_errors_total").name

# An endpoint locator is derived, transaction-local acceleration.  These two ceilings are its
# complete memory contract: identifiers and the exact page-chain proof share one budget, and a
# refusal silently returns the caller to the canonical lookup rather than refusing the query.
# The estimates deliberately overcharge Python objects.  They are not persisted or exposed as a
# database setting because changing either value may affect cost, never answers or durability.
_ENDPOINT_LOCATOR_MAX_BYTES: int = 64 * 1024 * 1024
_ENDPOINT_LOCATOR_MAX_ENTRIES: int = 1_000_000
_ENDPOINT_LOCATOR_MEMO_BYTES: int = 1_024
_ENDPOINT_LOCATOR_TABLE_BYTES: int = 512
_ENDPOINT_LOCATOR_IDENTITY_BYTES: int = 384
_ENDPOINT_LOCATOR_PAGE_BYTES: int = 192
_ENDPOINT_LOCATOR_CURSOR_BYTES_PER_PAGE: int = 32
_ENDPOINT_LOCATOR_CURSOR_BASE_BYTES: int = 2_048

# A landing result retains a decoded HeapVersion, unlike the endpoint locator above.  These
# ceilings are consequently smaller and shared by every open transaction on this engine.  The
# serialized payload is charged at sixteen times its size, plus fixed Python-object overhead.  The
# factor remains conservative even for maps of many small scalar pairs, whose dict/key/value
# object graph can be much wider than its compact encoding.  Exhaustion changes cost only: the
# table's retained results are discarded and subsequent identities use the canonical D-02 door.
_OWNER_LANDING_MAX_BYTES: int = 32 * 1024 * 1024
_OWNER_LANDING_MAX_ENTRIES: int = 131_072
_OWNER_LANDING_MEMO_BYTES: int = 1_024
_OWNER_LANDING_TABLE_BYTES: int = 512
_OWNER_LANDING_VIEW_BASE_BYTES: int = 1_024
_OWNER_LANDING_OVERLAY_ENTRY_BYTES: int = 192
_OWNER_LANDING_FINGERPRINT_ENTRY_BYTES: int = 256
_OWNER_LANDING_RESULT_BASE_BYTES: int = 512
_OWNER_LANDING_MISS_BYTES: int = 192
_OWNER_LANDING_PAYLOAD_MULTIPLIER: int = 16

QUERY_METRICS: tuple[MetricDescriptor, ...] = tuple(
    metric(name)
    for name in (
        "oktografx_query_phase_duration_seconds",
        "oktografx_query_rows_returned_count",
        "oktografx_query_errors_total",
    )
)
"""The three metrics of CONTRACT.md section 9 this component emits, taken by lookup (G7)."""

MISSING_RECORD_IDENTITY: str = (
    "Nothing in this build assigns the RecordId of a new row. CONTRACT.md section 3 calls it a "
    "stable logical identity across versions and no component allocates one, so a write would "
    "have to invent an identity rule that the heap, the indexes and recovery must all agree on."
)
"""Why a write refuses on identity."""

MISSING_COMMIT_HOOK: str = (
    "A row cannot be written at its commit number: HeapStore.insert takes xmin when the row is "
    "written and the commit number is assigned inside the commit itself (CONTRACT.md section "
    "8.5 step 3.4). The transaction manager holds an index_manager and never calls its "
    "commit(txn, csn), so no component applies staged work at the number the log assigned. A row "
    "written before that number exists carries a birth stamp no snapshot rule can make correct."
)
"""Why a write refuses on visibility."""


@dataclass(frozen=True, slots=True)
class RowBinding:
    """One variable of a result row, bound to one version of one stored row."""

    variable: str
    table: TableDef
    ref: object
    version: HeapVersion
    polymorphic: bool = False
    """Whether the pattern that bound this row named no label."""

    @property
    def record_id(self) -> int:
        """Return the stable identity of the row this binding names."""
        return self.version.record_id

    def value(self, key: str) -> Value:
        """Return one property of this row, refusing a column its table does not declare.

        Unless the pattern named no label. A polymorphic match reads one name across tables
        that were never obliged to carry the same columns, so a column this row's table does
        not declare is null here rather than a refusal -- refusing would make a query answer
        for one table and fail for the next one in the same scan.
        """
        position = self.table.column_positions.get(key)
        if position is not None:
            if position >= len(self.version.values):
                return None
            return self.version.values[position]
        if self.polymorphic:
            return None
        raise GrafxPlanError(
            f"Table {self.table.name!r} has no column named {key!r}, so {self.variable}.{key} "
            "reads nothing.",
            field="column",
            value=key,
            table=self.table.name,
        )

    def describe(self) -> str:
        """Return a short en-US rendering of this binding."""
        return f"({self.variable}:{self.table.name} #{self.record_id})"


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    """One capability-free, opaque identity carried by a projected path."""

    offset: int
    table: int


@dataclass(frozen=True, slots=True)
class _PathNodeValue:
    """One detached node inside the engine's private path result marker."""

    identity: _PathIdentity
    label: str
    properties: tuple[tuple[str, Value], ...]


@dataclass(frozen=True, slots=True)
class _PathRelationshipValue:
    """One detached relationship inside the engine's private path result marker."""

    source: _PathIdentity
    target: _PathIdentity
    label: str
    identity: _PathIdentity
    properties: tuple[tuple[str, Value], ...]


@dataclass(frozen=True, slots=True)
class _PathValue:
    """A nominal one-hop path marker consumed only by the public result snapshot."""

    nodes: tuple[_PathNodeValue, _PathNodeValue]
    relationships: tuple[_PathRelationshipValue]


@dataclass(frozen=True, slots=True)
class QueryResult:
    """The rows one statement produced, with the plan that produced them.

    Columns are positional and carry the names the RETURN clause gave them. A statement that
    returns nothing -- a schema statement, a write with no RETURN -- carries no columns and no
    rows, and reports what it changed in :attr:`statistics`.
    """

    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Value, ...], ...] = ()
    plan: PlanNode | None = None
    statistics: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Seal the structural invariants that make every result lossless and inspectable.

        Deep value detachment belongs to the public facade because doing it here would execute a
        hostile result container while the query engine still holds its page-access section.  The
        DTO itself nevertheless owns its exact outer tuples and statistics dictionary and refuses
        malformed arity before :meth:`dictionaries` could silently truncate it with ``zip``.
        """
        if not issubclass(type(self.columns), tuple):
            raise GrafxPlanError(
                "Query result columns must be a tuple.",
                field="columns",
                value="not_a_tuple",
            )
        columns = tuple(tuple.__iter__(self.columns))
        if len(columns) > MAX_PROJECTION_ITEMS:
            raise GrafxPlanError(
                f"A query result may carry at most {MAX_PROJECTION_ITEMS} columns.",
                field="columns",
                value=len(columns),
                limit=MAX_PROJECTION_ITEMS,
            )
        exact_columns: list[str] = []
        seen: set[str] = set()
        for position, column in enumerate(columns):
            if not issubclass(type(column), str):
                raise GrafxPlanError(
                    "Every query result column must be text.",
                    field="columns",
                    value=position,
                )
            plain = str.__str__(column)
            if not plain:
                raise GrafxPlanError(
                    "A query result column name cannot be empty.",
                    field="columns",
                    value=position,
                )
            if len(plain) > MAX_RENDERED_QUERY_CHARACTERS:
                raise GrafxPlanError(
                    f"A query result column may carry at most "
                    f"{MAX_RENDERED_QUERY_CHARACTERS} "
                    "characters.",
                    field="columns",
                    value=len(plain),
                    limit=MAX_RENDERED_QUERY_CHARACTERS,
                )
            if plain in seen:
                raise GrafxPlanError(
                    f"A query result cannot contain duplicate column {plain!r}.",
                    field="columns",
                    value=plain,
                )
            seen.add(plain)
            exact_columns.append(plain)

        if not issubclass(type(self.rows), tuple):
            raise GrafxPlanError(
                "Query result rows must be a tuple.",
                field="rows",
                value="not_a_tuple",
            )
        exact_rows: list[tuple[Value, ...]] = []
        for position, row in enumerate(tuple.__iter__(self.rows)):
            if not issubclass(type(row), tuple):
                raise GrafxPlanError(
                    "Every query result row must be a tuple.",
                    field="rows",
                    value=position,
                )
            plain_row = tuple(tuple.__iter__(row))
            if len(plain_row) != len(exact_columns):
                raise GrafxPlanError(
                    "Every query result row must have exactly one value per column.",
                    field="rows",
                    value=len(plain_row),
                    expected=len(exact_columns),
                    row=position,
                )
            exact_rows.append(plain_row)

        if not issubclass(type(self.statistics), dict):
            raise GrafxPlanError(
                "Query result statistics must be a dictionary.",
                field="statistics",
                value="not_a_dict",
            )
        exact_statistics: dict[str, int] = {}
        for position, (name, count) in enumerate(dict.items(self.statistics)):
            if not issubclass(type(name), str):
                raise GrafxPlanError(
                    "Every query result statistic name must be text.",
                    field="statistics",
                    value=position,
                )
            plain_name = str.__str__(name)
            if not plain_name or len(plain_name) > MAX_NAME_CHARACTERS:
                raise GrafxPlanError(
                    f"A query result statistic name must contain between 1 and "
                    f"{MAX_NAME_CHARACTERS} characters.",
                    field="statistics",
                    value=len(plain_name),
                    limit=MAX_NAME_CHARACTERS,
                )
            if plain_name in exact_statistics:
                raise GrafxPlanError(
                    "Query result statistic names must be unique.",
                    field="statistics",
                    value=plain_name,
                )
            if type(count) is bool or not issubclass(type(count), int):
                raise GrafxPlanError(
                    "Every query result statistic must be an integer.",
                    field="statistics",
                    value=plain_name,
                )
            plain_count = int.__int__(count)
            if not 0 <= plain_count <= INT64_MAX:
                raise GrafxPlanError(
                    "A query result statistic must fit in a non-negative signed 64-bit "
                    "integer.",
                    field="statistics",
                    value=plain_name,
                    count=plain_count,
                    limit=INT64_MAX,
                )
            exact_statistics[plain_name] = plain_count

        if self.plan is not None:
            try:
                validate_plan(self.plan)
            except GrafxPlanError:
                raise
            except Exception as failure:  # noqa: BLE001 - untrusted public plan value
                raise GrafxPlanError(
                    "The query result carries a malformed plan.",
                    field="plan",
                    value="malformed",
                ) from failure
        object.__setattr__(self, "columns", tuple(exact_columns))
        object.__setattr__(self, "rows", tuple(exact_rows))
        object.__setattr__(self, "statistics", exact_statistics)

    def __len__(self) -> int:
        """Return how many rows this result carries."""
        return len(self.rows)

    def __iter__(self) -> Iterator[tuple[Value, ...]]:
        """Iterate the rows in the order the query produced them."""
        return iter(self.rows)

    def dictionaries(self) -> tuple[dict[str, Value], ...]:
        """Return the rows as mappings from column name to value."""
        return tuple(dict(zip(self.columns, row)) for row in self.rows)

    def describe(self) -> str:
        """Return a short en-US summary of this result."""
        return f"{len(self.rows)} rows over columns {', '.join(self.columns) or 'none'}"


@dataclass(slots=True)
class _Row:
    """One row travelling through the operator tree.

    ``bindings`` maps a variable to what a pattern bound it to, and carries the similarity score
    under its own key. ``computed`` is filled by aggregation, when the variables are gone and the
    only readable things are the grouping keys and the aggregates. ``columns`` is filled by the
    projection, so an ORDER BY key that names an output alias is answered from what was projected
    rather than evaluated a second time against something the projection dropped.
    """

    bindings: dict[str, object]
    computed: dict[Expression, object] | None = None
    columns: dict[str, object] | None = None


_WRITE_PLAN_NODES: tuple[type[PlanNode], ...] = (
    CreateNodeTable,
    CreateRelTable,
    CreateVectorSpace,
    CreateRelationships,
    MergePattern,
    SetProperties,
    DeleteEntities,
)
"""Operators a snapshot-owning result cursor must never execute incrementally."""


def _plan_writes(root: PlanNode) -> bool:
    """Return whether ``root`` contains an operator that can stage durable work.

    A result cursor may be closed before exhaustion.  Incremental execution is therefore safe
    only for a read plan: otherwise closing after the first batch would have to choose between
    committing a prefix and silently discarding a statement.  The planner has only unary and
    binary operator links, so this bounded walk covers the complete executable tree without
    inspecting expression objects that happen to be dataclasses too.
    """
    pending: list[PlanNode] = [root]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        identity = id(node)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(node, _WRITE_PLAN_NODES):
            return True
        for name in ("child", "left", "right"):
            child = getattr(node, name, None)
            if isinstance(child, PlanNode):
                pending.append(child)
    return False


class _QueryResultCursor:
    """One internal, pull-driven execution over a fixed transaction snapshot.

    This object never crosses the public facade.  It deliberately returns detached projected
    value tuples, not ``_Row`` instances, and retains neither a page pin nor a page-access
    section between pulls.  The public cursor owns the transaction that owns the snapshot and
    closes both together.
    """

    __slots__ = (
        "_active_seconds",
        "_closed",
        "_context",
        "_engine",
        "_reported",
        "_rows",
        "_seen",
        "columns",
        "plan",
    )

    def __init__(
        self,
        *,
        engine: QueryEngine,
        root: ProduceResults,
        context: _Context,
        rows: Iterator[_Row],
        initial_seconds: float = 0.0,
    ) -> None:
        self._engine = engine
        self._context = context
        self._rows = rows
        self._closed = False
        self._reported = False
        self._seen = 0
        self._active_seconds = initial_seconds
        self.columns = root.columns
        self.plan = root

    @property
    def closed(self) -> bool:
        """Return whether this execution can produce another batch."""
        return self._closed

    @property
    def statistics(self) -> dict[str, int]:
        """Return a snapshot of counters accumulated by rows consumed so far."""
        return dict(self._context.statistics)

    def fetch(self, limit: int) -> tuple[tuple[tuple[Value, ...], ...], bool]:
        """Return at most ``limit`` projected rows and whether exhaustion was observed.

        A batch containing exactly ``limit`` rows does not pull a hidden look-ahead row merely
        to discover EOF.  Consequently a caller must either request the next batch or close its
        context manager.  That rule prevents an early-closing cursor from evaluating work the
        caller never requested and keeps ``max_result_rows`` charged at the single delivery
        boundary.
        """
        if self._closed:
            return (), True
        started = self._engine._reading()
        produced: list[tuple[Value, ...]] = []
        exhausted = False
        try:
            while len(produced) < limit:
                try:
                    row = next(self._rows)
                except StopIteration:
                    exhausted = True
                    break
                observed = self._seen + 1
                configured = self._engine._max_result_rows
                if configured is not None and observed > configured:
                    raise GrafxQueryBudgetExceeded(
                        f"Query would exceed max_result_rows: limit {configured}, "
                        f"observed {observed}.",
                        field="max_result_rows",
                        limit=configured,
                        observed=observed,
                    )
                produced.append(_projected(row, self.columns))
                self._seen = observed
        except GrafxError as failure:
            self._engine._count_error(failure)
            self._accumulate_active(started)
            try:
                self._finish()
            except BaseException as cleanup_failure:
                failure.add_note(
                    "Query cursor cleanup also failed with "
                    f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                )
            raise
        except BaseException as failure:
            self._accumulate_active(started)
            try:
                self._finish()
            except BaseException as cleanup_failure:
                failure.add_note(
                    "Query cursor cleanup also failed with "
                    f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                )
            raise
        self._accumulate_active(started)
        if exhausted:
            self._finish()
        return tuple(produced), exhausted

    def close(self) -> None:
        """Stop the operator tree and release all statement-local retained state."""
        self._finish()

    def _finish(self) -> None:
        """Perform the idempotent internal half of cursor settlement."""
        if self._closed:
            return
        self._closed = True
        failure: BaseException | None = None
        close = getattr(self._rows, "close", None)
        if callable(close):
            try:
                close()
            except BaseException as caught:
                failure = caught
        # Cursor plans are statically read-only, so release cannot transfer row writes.  Calling
        # the common release door still clears any statement-local admission lists if a future
        # read operator starts recording dependencies.
        try:
            moved = self._context.release()
            if moved:
                raise GrafxTransactionStateError(
                    "A read-only query cursor produced staged writes.",
                    field="cursor",
                    value="write_plan",
                    rows=moved,
                )
        except BaseException as caught:
            if failure is None:
                failure = caught
            else:
                failure.add_note(
                    "Query context release also failed with "
                    f"{type(caught).__name__}: {caught}"
                )
        if not self._reported and self._engine._metrics.enabled:
            self._reported = True
            try:
                self._engine._metrics.observe(
                    _PHASE_DURATION,
                    self._active_seconds,
                    {"phase": PHASE_EXECUTE},
                )
                self._engine._metrics.observe(_ROWS_RETURNED, float(self._seen))
            except BaseException as caught:
                if failure is None:
                    failure = caught
                else:
                    failure.add_note(
                        "Query cursor metrics publication also failed with "
                        f"{type(caught).__name__}: {caught}"
                    )
        if failure is not None:
            raise failure

    def _accumulate_active(self, started: float) -> None:
        """Add one pull's engine-active time without counting consumer pauses."""
        if not self._engine._metrics.enabled:
            return
        elapsed = self._engine._clock.monotonic() - started
        self._active_seconds += elapsed if elapsed > 0.0 else 0.0


_HELD_INSERT: str = "insert"
_HELD_UPDATE: str = "update"
_HELD_DELETE: str = "delete"


@dataclass(slots=True, frozen=True)
class _HeldRow:
    """One row change a statement has built and not yet handed to the transaction.

    The operation travels with the row rather than living in three parallel lists, because the
    order a statement asked for them in is part of the answer: a statement that updates a row and
    then deletes it writes two stamps, and they are only correct in that order.
    """

    operation: str
    table: TableDef
    values: tuple[Value, ...] | None
    identity: int | None
    reference: object
    token: int | None = None


@dataclass(slots=True)
class _Context:
    """What every operator of one running statement needs."""

    engine: QueryEngine
    txn: object
    parameters: dict[str, Value]
    analysis: QueryAnalysis
    statistics: dict[str, int]
    coalesce_types: dict[int, ValueType | None]
    case_types: dict[int, ValueType | None]
    # Instants whose argument the call already made knowable, read once here rather
    # than parsed again for every row.  Keyed by the call, so nothing replaces the
    # parameter the caller passed.
    timestamp_values: dict[FunctionCall, object]
    # The catalog this statement was PLANNED against. Inside a transaction that has declared
    # schema of its own, that is the transaction's working copy, and execution must resolve
    # tables and spaces from the same picture the planner did -- a row materialised for a table
    # whose vector space exists only in the working copy cannot ask the live catalog for it.
    catalog: Catalog | None = None
    result_node: PlanNode | None = None
    union_coercions: tuple[bool, ...] = ()
    intermediate_rows: dict[int, int] = field(default_factory=dict)
    traversal_expansions: int = 0
    traversal_paths: int = 0
    staged_rows: list[_HeldRow] = field(default_factory=list)
    staged_partitions: list[tuple[int, bytes]] = field(default_factory=list)
    staged_reads: list[tuple[int, bytes]] = field(default_factory=list)
    tokens_issued: int = 0
    pending_tokens: dict[int, int] = field(default_factory=dict)
    cancelled_insert_tokens: set[int] = field(default_factory=set)
    ends_held: set[object] = field(default_factory=set)
    _ends_staged: frozenset[object] | None = None
    path_identities: dict[tuple[object, ...], int] = field(default_factory=dict)
    path_identities_issued: int = 0

    def already_ended(self, reference: object) -> bool:
        """Answer whether this exact version has already been told to stop.

        Two questions with one answer. A statement's write runs once per ROW of its pipeline, so
        a Cartesian naming the same node three times reaches the delete three times; and an
        earlier statement of the same transaction may already have ended a row this one finds,
        because both read the same snapshot and a snapshot cannot see either one's uncommitted
        work. Either repeat stages a second end at a number the version had already stopped at,
        counts a row that does not exist, and spends statement budget on a write nobody asked
        for -- which is how a statement that fits inside `max_statement_writes` gets refused for
        exceeding it.

        The reference is the key because it names one stored VERSION, which is exactly what an
        end is written to. Record numbers are per table and would need the table beside them;
        the reference already carries that distinction.

        A transaction that does not describe its row intents narrows this to the statement it is
        asked from. The statement's own ends are always known.
        """
        if reference in self.ends_held:
            return True
        if self._ends_staged is None:
            intents = getattr(self.txn, "row_intents", None) or ()
            self._ends_staged = frozenset(
                intent.reference
                for intent in intents
                if getattr(intent, "operation", None) is RowOperation.DELETE
            )
        return reference in self._ends_staged

    def note_ended(self, reference: object) -> None:
        """Record that this statement is holding the end of that exact version."""
        self.ends_held.add(reference)

    def schema(self) -> Catalog:
        """Return the catalog this statement resolves names from."""
        if self.catalog is not None:
            return self.catalog
        return self.engine._catalog.catalog

    def token_for(self, binding: RowBinding) -> int:
        """Return the token that ties a pending binding to the held insert it stands for.

        The binding's VERSION object is the stable thing: the same RowBinding travels through
        every later clause of the statement, so its identity names the held row however many
        times the row's values are rewritten. Identifying the held row by its values instead
        was C10 round-3 B2: two created rows with identical values, and a second SET clause
        landed on the wrong one.
        """
        marker = id(binding.version)
        token = self.pending_tokens.get(marker)
        if token is None:
            self.tokens_issued += 1
            token = self.tokens_issued
            self.pending_tokens[marker] = token
        return token

    def hold(
        self,
        table: TableDef,
        values: tuple[Value, ...],
        key: bytes,
        identity: int | None,
        *,
        token: int | None = None,
    ) -> None:
        """Hold one row until the whole statement has been built without refusing.

        Per STATEMENT, not per pattern. A statement that stages its first pattern and then
        refuses on its second leaves the transaction carrying half of itself -- and a caller
        that catches the error and commits anyway would make that half durable. Holding
        everything until the statement is complete makes the refusal leave the transaction
        exactly as it found it.
        """
        self._require_statement_write_capacity()
        self.staged_rows.append(
            _HeldRow(_HELD_INSERT, table, values, identity, None, token)
        )
        self.staged_partitions.append((table.table_id, key))

    def hold_update(
        self,
        table: TableDef,
        reference: object,
        values: tuple[Value, ...],
        key: bytes,
        *,
        previous_keys: Sequence[bytes] = (),
    ) -> None:
        """Hold a new version of an existing row under the same statement discipline.

        ``previous_keys`` are the partition keys of the versions this update REPLACES. An update
        that changes the primary key used to declare only the new key's partition, so a
        concurrent writer of the same row -- which conflicts on the OLD key -- passed optimistic
        validation, and the heap refused it inside the commit section instead (C10 round-2 B2).
        Declaring both makes the two commits meet where step 3.3 can see them.
        """
        self._require_statement_write_capacity()
        self.staged_rows.append(_HeldRow(_HELD_UPDATE, table, values, None, reference))
        self.staged_partitions.append((table.table_id, key))
        for previous in previous_keys:
            if previous != key:
                self.staged_partitions.append((table.table_id, previous))

    def hold_delete(self, table: TableDef, reference: object, key: bytes) -> None:
        """Hold the end of an existing row under the same statement discipline."""
        self._require_statement_write_capacity()
        self.staged_rows.append(_HeldRow(_HELD_DELETE, table, None, None, reference))
        self.staged_partitions.append((table.table_id, key))

    def hold_read(self, table: TableDef, key: bytes) -> None:
        """Hold a dependency this statement's work rests on, under the statement's discipline.

        A row a statement READ and then depended on has to be declared, or optimistic validation
        cannot refuse the transaction that changed it underneath. Held rather than declared on
        the spot, exactly like the writes: a statement that refuses half way through must leave
        the transaction's interest set as it found it, and a guard published by a refused
        statement would make a LATER commit conflict over work nobody did.
        """
        entry = (table.table_id, key)
        if entry not in self.staged_reads:
            self.staged_reads.append(entry)

    def _require_statement_write_capacity(self) -> None:
        """Refuse the next logical write before this statement retains it."""
        limit = self.engine._max_statement_writes
        observed = len(self.staged_rows) + 1
        if limit is None or observed <= limit:
            return
        raise GrafxTransactionBudgetExceeded(
            f"Statement would exceed max_statement_writes: limit {limit}, "
            f"observed {observed}.",
            field="max_statement_writes",
            limit=limit,
            observed=observed,
            txn_id=getattr(self.txn, "txn_id", None),
        )

    def release(self) -> int:
        """Hand every held row to the transaction, in the order the statement built them.

        Order is kept because a statement may touch one row more than once -- update it and then
        delete it -- and the two stamps the transaction writes are only correct in the order they
        were asked for. Values were checked while held; transaction-wide row/byte admission may
        still refuse the handover, and the exact staging mark below then restores all prior work.
        """
        if not self.staged_rows and not self.staged_reads:
            return 0
        transaction = _require_write_transaction(self.txn)
        note_write = getattr(transaction, "note_write", None)
        manager = getattr(transaction, "owner", None)
        partition_of = getattr(manager, "partition_of", None)
        # All or nothing ON THE TRANSACTION as well as in this context. A refusal from the
        # transaction's own doors part-way through the handover -- the third held row refused
        # after the first two were staged -- used to leave those two on the transaction, and the
        # caller's commit made half a statement durable: a refused DELETE that deleted, a refused
        # CREATE that created (C10 round-3 B1). The transaction's own mark is what unwinds it.
        take_mark = getattr(transaction, "staging_mark", None)
        discard = getattr(transaction, "discard_since", None)
        settle = getattr(transaction, "settle_staging_mark", None)
        mark = take_mark() if callable(take_mark) else None
        try:
            for held in self.staged_rows:
                if held.operation is _HELD_INSERT:
                    transaction.stage_row_insert(
                        held.table, held.values or (), record_id=held.identity
                    )
                elif held.operation is _HELD_UPDATE:
                    transaction.stage_row_update(
                        held.table, held.reference, held.values or ()
                    )
                else:
                    transaction.stage_row_delete(held.table, held.reference)
            if partition_of is not None:
                note_read = getattr(transaction, "note_read", None)
                if note_read is not None:
                    for table_id, key in self.staged_reads:
                        note_read(partition_of(table_id, key))
                if note_write is not None:
                    for table_id, key in self.staged_partitions:
                        note_write(partition_of(table_id, key))
            if mark is not None and callable(settle):
                settle(mark)
        except BaseException:
            if mark is not None and callable(discard):
                discard(mark)
            raise
        moved = len(self.staged_rows)
        self.staged_rows.clear()
        self.staged_partitions.clear()
        self.staged_reads.clear()
        return moved

    @property
    def snapshot(self) -> object:
        """Return the view this statement reads under, refusing a transaction without one."""
        snapshot = getattr(self.txn, "snapshot", None)
        if snapshot is None or not hasattr(snapshot, "visible"):
            raise GrafxTransactionStateError(
                "A statement reads under the snapshot of its transaction, and the object "
                f"supplied carries none; got {type(self.txn).__name__}.",
                field="transaction",
                value=type(self.txn).__name__,
            )
        return snapshot

    def count(self, name: str, amount: int = 1) -> None:
        """Add to one statistic of this statement."""
        self.statistics[name] = self.statistics.get(name, 0) + amount

    def admit_intermediate(self, node: PlanNode) -> None:
        """Admit one row emitted by this physical operator during the statement."""
        limit = self.engine._max_intermediate_rows
        if limit is None:
            return
        identity = id(node)
        observed = self.intermediate_rows.get(identity, 0) + 1
        if observed > limit:
            raise GrafxQueryBudgetExceeded(
                f"Operator {node.label} would exceed max_intermediate_rows: "
                f"limit {limit}, observed {observed}.",
                field="max_intermediate_rows",
                limit=limit,
                observed=observed,
                operator=node.label,
            )
        self.intermediate_rows[identity] = observed

    def admit_traversal_expansion(self) -> None:
        """Charge one candidate edge before traversal performs work derived from it."""
        limit = self.engine._max_traversal_expansions
        if limit is None:
            return
        observed = self.traversal_expansions + 1
        if observed > limit:
            raise GrafxQueryBudgetExceeded(
                f"Query would exceed max_traversal_expansions: limit {limit}, "
                f"observed {observed}.",
                field="max_traversal_expansions",
                limit=limit,
                observed=observed,
            )
        self.traversal_expansions = observed
        self.count("traversal_expansions")

    def admit_traversal_path(self) -> None:
        """Charge one visible path before retaining it in a frontier or returning it."""
        limit = self.engine._max_traversal_paths
        if limit is None:
            return
        observed = self.traversal_paths + 1
        if observed > limit:
            raise GrafxQueryBudgetExceeded(
                f"Query would exceed max_traversal_paths: limit {limit}, observed {observed}.",
                field="max_traversal_paths",
                limit=limit,
                observed=observed,
            )
        self.traversal_paths = observed
        self.count("traversal_paths")


def _intent_table_ids(txn: object) -> frozenset[int]:
    """Return the tables whose indexes do not yet describe their owner's row view.

    Row intents are private to the transaction passed to :meth:`QueryEngine.execute`, so this
    set is owner-only by construction. Even an insert later cancelled by a delete keeps the
    table dirty until commit: planning from the raw intents preserves transaction budgets and
    avoids making plan safety depend on a second, planner-local reduction rule.
    """
    table_ids: set[int] = set()
    for intent in getattr(txn, "row_intents", ()):
        table_id = getattr(getattr(intent, "table", None), "table_id", None)
        if (
            isinstance(table_id, int)
            and not isinstance(table_id, bool)
            and table_id > 0
        ):
            table_ids.add(table_id)
    return frozenset(table_ids)


@dataclass(frozen=True, slots=True)
class _IndexSchemaEffect:
    """One exact speculative registry artifact installed by a schema statement."""

    artifact: object


@dataclass(frozen=True, slots=True)
class _IndexObservationEffect:
    """One statement-local empty observation, including idempotent adoption."""

    index: object
    txn: object
    created: bool


@dataclass(frozen=True, slots=True)
class _VectorMapEffect:
    """One exact vector mapping replacement and the mapping it displaced."""

    space: str
    attached: object
    previous: object | None
    owner: object


@dataclass(frozen=True, slots=True)
class _SkipEffect:
    """One owner token for a process-local unindexed-table diagnostic."""

    table: str
    owner: object


_SchemaEffect = _IndexSchemaEffect | _IndexObservationEffect | _VectorMapEffect | _SkipEffect


class _EndpointLocatorCapacity(Exception):
    """Internal signal that acceleration reached its complete memory allowance."""


class _EndpointLocatorStale(Exception):
    """Internal signal that a derived walk no longer describes the current heap view."""


class _EndpointLocatorBudget:
    """One shared, explicit and atomic allowance for every locator of one engine."""

    __slots__ = (
        "_guard",
        "_max_bytes",
        "_max_entries",
        "_used_bytes",
        "_used_entries",
    )

    def __init__(
        self,
        *,
        max_bytes: int,
        max_entries: int,
        guard: AbstractContextManager[object] | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._used_bytes = 0
        self._used_entries = 0
        # Mechanism belongs to the composition root.  The no-op default keeps direct core
        # construction deterministic; production hands this budget the same private RLock as
        # the endpoint memo registry.
        self._guard = nullcontext() if guard is None else guard

    def reserve(self, *, bytes_: int, entries: int) -> None:
        """Reserve derived state atomically, or ask the caller to use canonical lookup."""
        with self._guard:
            next_bytes = self._used_bytes + bytes_
            next_entries = self._used_entries + entries
            if next_bytes > self._max_bytes or next_entries > self._max_entries:
                raise _EndpointLocatorCapacity
            self._used_bytes = next_bytes
            self._used_entries = next_entries

    def release(self, *, bytes_: int, entries: int) -> None:
        """Return an exact reservation held by a closing locator."""
        with self._guard:
            self._used_bytes -= bytes_
            self._used_entries -= entries


@dataclass(slots=True)
class _EndpointLocatorSlot:
    """One paid table-registry entry and its non-overlapping lifecycle state."""

    state: str = "building"
    locator: _EndpointIdentityLocator | None = None
    active: bool = False


class _EndpointIdentityLocator:
    """Resolve identities by one resumable canonical prefix walk under one stable view.

    The map stores only the first snapshot-visible physical reference for an identity.  Its
    value is therefore a reusable ``identity -> (RecordRef, HeapVersion)`` proof door rather
    than an edge-specific cache; P1.5 may consume the same shape later without changing it.
    Payloads are never retained.  The requested candidate is fully decoded from its physical
    reference immediately before it is accepted.
    """

    __slots__ = (
        "_budget",
        "_charged_bytes",
        "_charged_entries",
        "_closed",
        "_cursor",
        "_epoch",
        "_first",
        "_heap",
        "_snapshot",
        "_table",
    )

    def __init__(
        self,
        *,
        heap: HeapStore,
        table: TableDef,
        snapshot: object,
        budget: _EndpointLocatorBudget,
        page_size: int,
    ) -> None:
        self._heap = heap
        self._table = table
        self._snapshot = snapshot
        self._budget = budget
        self._epoch = heap._derived_read_epoch()
        self._first: dict[RecordId, RecordRef] = {}
        self._cursor = None
        self._closed = False
        self._charged_bytes = 0
        self._charged_entries = 0
        # A paused cursor owns at most one page's header/ref tuple plus its fixed fields.  Thirty-
        # two times page_size is intentionally conservative for Python tuple/integer overhead.
        self._reserve(
            bytes_=(
                _ENDPOINT_LOCATOR_CURSOR_BASE_BYTES
                + page_size * _ENDPOINT_LOCATOR_CURSOR_BYTES_PER_PAGE
            ),
            entries=1,
        )
        try:
            self._cursor = heap._visible_record_cursor(
                table, snapshot, admit_page=self._admit_page
            )
        except BaseException:
            self.close()
            raise

    @property
    def table(self) -> TableDef:
        """Return the exact schema definition that vouches for this derived state."""
        return self._table

    @property
    def epoch(self) -> int:
        """Return the conservative heap epoch captured before the walk began."""
        return self._epoch

    def locate(self, record_id: RecordId) -> tuple[RecordRef, HeapVersion] | None:
        """Return the first canonical visible row, decoding only the requested candidate."""
        self._require_current()
        ref = self._first.get(record_id)
        while ref is None and self._cursor is not None:
            item = self._cursor.next_visible()
            if item is None:
                self._cursor = None
                break
            candidate_ref, candidate_id = item
            if candidate_id not in self._first:
                self._reserve(
                    bytes_=_ENDPOINT_LOCATOR_IDENTITY_BYTES,
                    entries=1,
                )
                self._first[candidate_id] = candidate_ref
            if candidate_id == record_id:
                ref = self._first[candidate_id]
        self._require_current()
        if ref is None:
            return None
        version = self._heap._revalidate_visible_ref(
            self._table, ref, record_id, self._snapshot
        )
        self._require_current()
        if version is None:
            # A fixed snapshot cannot make a previously visible header disappear without the
            # physical view changing.  If no epoch reports that change, fail closed instead of
            # remembering absence over a contradictory proof.
            raise GrafxCorruptionDetected(
                f"The canonical reference {ref.page}:{ref.slot} for record {record_id} of "
                f"{self._table.name!r} is not visible to the snapshot that selected it.",
                file=self._heap.file,
                page=ref.page,
                slot=ref.slot,
                table=self._table.name,
                table_id=self._table.table_id,
                record_id=record_id,
                field="snapshot_visibility",
            )
        return ref, version

    def close(self) -> None:
        """Discard all derived references and release their complete shared reservation."""
        if self._closed:
            return
        cursor = self._cursor
        if cursor is not None:
            cursor.close()
        self._cursor = None
        self._first.clear()
        self._closed = True
        self._budget.release(
            bytes_=self._charged_bytes, entries=self._charged_entries
        )
        self._charged_bytes = 0
        self._charged_entries = 0

    def _admit_page(self) -> None:
        """Charge one exact visited-page proof before the cursor grows it."""
        self._require_current()
        self._reserve(bytes_=_ENDPOINT_LOCATOR_PAGE_BYTES, entries=1)

    def _reserve(self, *, bytes_: int, entries: int) -> None:
        """Reserve and remember state so close can return it exactly once."""
        self._budget.reserve(bytes_=bytes_, entries=entries)
        self._charged_bytes += bytes_
        self._charged_entries += entries

    def _require_current(self) -> None:
        """Refuse a derived answer after relink, cache drop or recovery apply."""
        if self._closed or self._heap._derived_read_epoch() != self._epoch:
            raise _EndpointLocatorStale


class _EndpointTxnMemo:
    """All bounded endpoint locators owned by one exact transaction and schema picture."""

    __slots__ = (
        "_base_charged",
        "_retired",
        "budget",
        "locators",
        "schema",
        "snapshot",
        "txn",
    )

    def __init__(
        self,
        *,
        txn: object,
        snapshot: object,
        schema: Catalog,
        budget: _EndpointLocatorBudget,
    ) -> None:
        self.txn = txn
        self.snapshot = snapshot
        self.schema = schema
        self.budget = budget
        self.locators: dict[int, _EndpointLocatorSlot] = {}
        self._retired = False
        self._base_charged = False
        # This pays for the memo object, the engine's txn-id mapping entry and the empty table
        # registry before any of them becomes reachable.  Every later table slot is charged by
        # claim().  There is therefore no side container that can grow after saturation.
        budget.reserve(bytes_=_ENDPOINT_LOCATOR_MEMO_BYTES, entries=1)
        self._base_charged = True

    def claim(self, table_id: int) -> _EndpointLocatorSlot:
        """Install one paid construction slot; caller holds the injected registry guard."""
        if self._retired:
            raise _EndpointLocatorStale
        present = self.locators.get(table_id)
        if present is not None:
            return present
        slot = _EndpointLocatorSlot()
        self.budget.reserve(bytes_=_ENDPOINT_LOCATOR_TABLE_BYTES, entries=1)
        try:
            self.locators[table_id] = slot
        except BaseException:
            self.budget.release(bytes_=_ENDPOINT_LOCATOR_TABLE_BYTES, entries=1)
            raise
        return slot

    def install(
        self, table_id: int, slot: _EndpointLocatorSlot, locator: _EndpointIdentityLocator
    ) -> bool:
        """Publish a fully built locator only while its paid slot is still live."""
        if (
            self._retired
            or self.locators.get(table_id) is not slot
            or slot.state != "building"
        ):
            self._discard_slot(table_id, slot)
            return False
        slot.locator = locator
        slot.state = "ready"
        slot.active = True
        return True

    def acquire(self, table_id: int) -> tuple[str, _EndpointLocatorSlot | None]:
        """Lease one ready cursor to one caller without making page I/O a critical section."""
        if self._retired:
            return "retired", None
        slot = self.locators.get(table_id)
        if slot is None:
            return "missing", None
        if slot.state != "ready" or slot.active:
            return slot.state if not slot.active else "busy", slot
        slot.active = True
        return "ready", slot

    def finish(
        self,
        table_id: int,
        slot: _EndpointLocatorSlot,
        *,
        disposition: str,
    ) -> _EndpointIdentityLocator | None:
        """Finish one lease and return a locator that may now be closed outside the guard."""
        slot.active = False
        if self.locators.get(table_id) is not slot:
            return slot.locator
        if self._retired or disposition == "evict":
            return self._discard_slot(table_id, slot)
        if disposition == "disable":
            locator = slot.locator
            slot.locator = None
            slot.state = "disabled"
            return locator
        return None

    def abandon_build(
        self,
        table_id: int,
        slot: _EndpointLocatorSlot,
        *,
        disabled: bool,
    ) -> None:
        """Resolve a failed construction without allocating an unmetered disabled set."""
        if self.locators.get(table_id) is not slot:
            return
        if self._retired or not disabled:
            self._discard_slot(table_id, slot)
            return
        slot.state = "disabled"

    def is_disabled(self, table_id: int) -> bool:
        """Return whether this paid table slot permanently chose canonical lookup."""
        slot = self.locators.get(table_id)
        return slot is not None and slot.state == "disabled"

    def retire(self) -> tuple[_EndpointIdentityLocator, ...]:
        """Detach inactive state now and defer live/building slots to their owner."""
        self._retired = True
        closers: list[_EndpointIdentityLocator] = []
        for table_id, slot in tuple(self.locators.items()):
            if slot.active or slot.state == "building":
                continue
            locator = self._discard_slot(table_id, slot)
            if locator is not None:
                closers.append(locator)
        self._release_base_if_empty()
        return tuple(closers)

    def _discard_slot(
        self, table_id: int, slot: _EndpointLocatorSlot
    ) -> _EndpointIdentityLocator | None:
        """Remove exactly this table slot and return its locator without closing it."""
        if self.locators.get(table_id) is not slot:
            return slot.locator
        self.locators.pop(table_id)
        self.budget.release(bytes_=_ENDPOINT_LOCATOR_TABLE_BYTES, entries=1)
        locator = slot.locator
        slot.locator = None
        slot.state = "retired"
        self._release_base_if_empty()
        return locator

    def _release_base_if_empty(self) -> None:
        """Return the memo/map reservation once retirement has no outstanding owner."""
        if self._retired and not self.locators and self._base_charged:
            self.budget.release(bytes_=_ENDPOINT_LOCATOR_MEMO_BYTES, entries=1)
            self._base_charged = False


class _OwnerLandingCapacity(Exception):
    """Internal signal that decoded landing retention reached its complete allowance."""


class _OwnerLandingBudget:
    """One explicit, engine-wide allowance for transaction-local decoded landings."""

    __slots__ = (
        "_guard",
        "_max_bytes",
        "_max_entries",
        "_used_bytes",
        "_used_entries",
    )

    def __init__(
        self,
        *,
        max_bytes: int,
        max_entries: int,
        guard: AbstractContextManager[object] | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._used_bytes = 0
        self._used_entries = 0
        self._guard = nullcontext() if guard is None else guard

    def reserve(self, *, bytes_: int, entries: int) -> None:
        """Reserve before retaining an object, or leave the counters untouched."""
        with self._guard:
            next_bytes = self._used_bytes + bytes_
            next_entries = self._used_entries + entries
            if next_bytes > self._max_bytes or next_entries > self._max_entries:
                raise _OwnerLandingCapacity
            self._used_bytes = next_bytes
            self._used_entries = next_entries

    def release(self, *, bytes_: int, entries: int) -> None:
        """Release one exact reservation while transaction settlement is fail-safe."""
        with self._guard:
            self._used_bytes -= bytes_
            self._used_entries -= entries


def _owner_landing_result_bytes(
    table: TableDef, found: tuple[object, HeapVersion] | None
) -> int | None:
    """Return a conservative charge, or decline optional retention without changing the row."""
    if found is None:
        return _OWNER_LANDING_MISS_BYTES
    try:
        stored_bytes = len(encode_tuple(table, found[1].values))
    except (GrafxError, MemoryError):
        # Accounting is optional acceleration.  The version was already decoded and validated by
        # the heap (or built by the owner's validated intent reducer), so a failure to size a
        # second encoding must not replace that query answer with an accounting-only refusal.
        return None
    return (
        _OWNER_LANDING_RESULT_BASE_BYTES
        + stored_bytes * _OWNER_LANDING_PAYLOAD_MULTIPLIER
    )


def _owner_landing_fingerprint_charge(
    table: TableDef, fingerprint: tuple
) -> tuple[int, int] | None:
    """Conservatively meter the complete intent history retained for invalidation.

    The reduced overlay may contain one row after hundreds of updates to the same reference, but
    the content fingerprint deliberately retains every update so savepoint rollback cannot alias
    an earlier state.  Charge every history tuple plus each values object graph before a view can
    retain the fingerprint.  Failure to size this optional acceleration declines retention; it
    never replaces the query answer.
    """
    _schema_version, intents, held = fingerprint
    entries = len(intents) + len(held)
    payload_bytes = 0
    try:
        for history in (intents, held):
            for item in history:
                values = item[3]
                # RowIntent.DELETE uses the empty tuple and _HeldRow DELETE uses None as their
                # absence markers.  Both pay the fixed entry, but neither has a payload to size.
                if values is not None and item[0] not in (
                    RowOperation.DELETE,
                    _HELD_DELETE,
                ):
                    payload_bytes += len(
                        encode_tuple(table, cast(tuple[Value, ...], values))
                    )
    except (GrafxError, MemoryError):
        return None
    return (
        entries * _OWNER_LANDING_FINGERPRINT_ENTRY_BYTES
        + payload_bytes * _OWNER_LANDING_PAYLOAD_MULTIPLIER,
        entries,
    )


class _OwnerLandingView:
    """Resolve only requested node identities and retain them under an explicit budget.

    This is R1 of D-03.  Physical identities go through the canonical, snapshot-bound D-02
    locator; pending identities are answered only from this transaction's reduced insert view.
    ``changed`` and ``ended`` are then applied in the same order as the former full-table map.

    Results are memoized across statements only while the transaction, snapshot, schema,
    fingerprint and heap epoch still vouch for them.  Admission precedes every cache mutation.  If
    any result would exceed the shared bytes or entries ceiling, all decoded results of this table
    are discarded and this view becomes lookup-only.  The query is never refused for acceleration
    capacity.  When both this result cache and the D-02 prefix locator exceed their independent
    ceilings, the honest residual worst case is one canonical O(N) lookup per distinct landing;
    eliminating that case requires the separately governed persistent identity access path.
    """

    __slots__ = (
        "_active",
        "_base_bytes",
        "_base_entries",
        "_budget",
        "_cache",
        "_cache_bytes",
        "_cache_enabled",
        "_cache_entries",
        "_changed",
        "_ended",
        "_engine",
        "_epoch",
        "_fingerprint",
        "_guard",
        "_pending",
        "_retired",
        "_snapshot",
        "_table",
    )

    def __init__(
        self,
        *,
        engine: QueryEngine,
        context: _Context,
        table: TableDef,
        fingerprint: tuple,
        changed: dict[object, tuple[Value, ...] | None],
        inserted: Sequence[tuple[object, tuple[Value, ...]]],
        ended: frozenset[object],
        budget: _OwnerLandingBudget | None,
        guard: AbstractContextManager[object],
    ) -> None:
        pending_count = sum(
            isinstance(reference, PendingRowRef) for reference, _values in inserted
        )
        overlay_entries = len(changed) + pending_count + len(ended)
        fingerprint_bytes = 0
        fingerprint_entries = 0
        if budget is not None:
            fingerprint_charge = _owner_landing_fingerprint_charge(table, fingerprint)
            if fingerprint_charge is None:
                raise _OwnerLandingCapacity
            fingerprint_bytes, fingerprint_entries = fingerprint_charge
        base_bytes = (
            _OWNER_LANDING_VIEW_BASE_BYTES
            + overlay_entries * _OWNER_LANDING_OVERLAY_ENTRY_BYTES
            + fingerprint_bytes
        )
        base_entries = 1 + overlay_entries + fingerprint_entries
        if budget is not None:
            budget.reserve(bytes_=base_bytes, entries=base_entries)
        try:
            self._engine = engine
            self._table = table
            self._snapshot = context.snapshot
            # An uninstalled statement-local fallback is never compared for reuse and therefore
            # must not keep an unmetered history merely to answer this statement.
            self._fingerprint = fingerprint if budget is not None else ()
            self._epoch = engine.heap._derived_read_epoch()
            self._changed = changed
            self._ended = ended
            self._budget = budget
            self._guard = guard
            self._base_bytes = base_bytes if budget is not None else 0
            self._base_entries = base_entries if budget is not None else 0
            self._cache: dict[
                object, tuple[tuple[object, HeapVersion] | None, int]
            ] = {}
            self._cache_bytes = 0
            self._cache_entries = 0
            self._cache_enabled = budget is not None
            self._active = 0
            self._retired = False
            self._pending = {
                reference: values
                for reference, values in inserted
                if isinstance(reference, PendingRowRef)
            }
        except BaseException:
            if budget is not None:
                budget.release(bytes_=base_bytes, entries=base_entries)
            raise

    def matches(
        self,
        *,
        table: TableDef,
        snapshot: object,
        fingerprint: tuple,
        epoch: int,
    ) -> bool:
        """Return whether every authority captured by this derived view is still current."""
        return (
            not self._retired
            and self._table == table
            and self._snapshot is snapshot
            and self._fingerprint == fingerprint
            and self._epoch == epoch
        )

    def get(
        self, identity: object, context: _Context
    ) -> tuple[object, HeapVersion] | None:
        """Return one owner-visible identity, memoizing only after successful admission."""
        with self._guard:
            if self._retired:
                raise GrafxTransactionStateError(
                    "A transaction-local landing view was retired before its query finished.",
                    field="owner_landing_view",
                    table=self._table.name,
                    table_id=self._table.table_id,
                )
            cached = self._cache.get(identity)
            if cached is not None:
                return cached[0]
            self._active += 1
        lease_open = True
        try:
            # The identity door can walk and decode heap pages.  It is deliberately outside the
            # injected registry guard; only the immutable overlay references above are leased.
            found = self._resolve(identity, context)
            charge = _owner_landing_result_bytes(self._table, found)
            with self._guard:
                try:
                    if (
                        not self._retired
                        and self._cache_enabled
                        and charge is not None
                        and identity not in self._cache
                    ):
                        budget = self._budget
                        if budget is not None:
                            try:
                                budget.reserve(bytes_=charge, entries=1)
                            except _OwnerLandingCapacity:
                                self._discard_results_locked()
                                self._cache_enabled = False
                            else:
                                try:
                                    self._cache[identity] = (found, charge)
                                except BaseException:
                                    budget.release(bytes_=charge, entries=1)
                                    raise
                                self._cache_bytes += charge
                                self._cache_entries += 1
                finally:
                    self._leave_locked()
                    lease_open = False
            return found
        finally:
            if lease_open:
                with self._guard:
                    self._leave_locked()

    def close(self) -> None:
        """Drop payloads, overlays and their exact charges at transaction settlement."""
        with self._guard:
            if self._retired:
                return
            self._retired = True
            self._discard_results_locked()
            if self._active == 0:
                self._release_base_locked()

    def forfeit_retention(self) -> None:
        """Turn an uninstalled candidate into an unmetered, statement-local fallback view."""
        with self._guard:
            self._discard_results_locked()
            self._release_base_locked(clear_overlays=False)
            self._fingerprint = ()
            self._cache_enabled = False
            self._budget = None

    def _resolve(
        self, identity: object, context: _Context
    ) -> tuple[object, HeapVersion] | None:
        """Apply the former full-map overlay to one physical or pending identity."""
        if isinstance(identity, PendingRowRef):
            values = self._pending.get(identity)
            if values is None:
                return None
            return (
                identity,
                HeapVersion(
                    record_id=0,
                    xmin=NO_CSN,
                    xmax=NO_CSN,
                    values=values,
                    prev=None,
                    schema_version=self._table.schema_version,
                    deleted=False,
                    table_id=self._table.table_id,
                ),
            )

        physical = _visible_identity_with_ref(
            self._engine,
            context,
            self._table,
            cast(RecordId, identity),
        )
        if physical is None:
            return None
        ref, version = physical
        if ref in self._ended:
            return None
        if ref in self._changed:
            values = self._changed[ref]
            if values is None:
                return None
            version = replace(version, values=values)
        return ref, version

    def _leave_locked(self) -> None:
        """Finish one heap-I/O lease; caller holds the injected re-entrant guard."""
        self._active -= 1
        if self._retired and self._active == 0:
            self._release_base_locked()

    def _discard_results_locked(self) -> None:
        """Release every decoded result without disturbing the overlay needed for fallback."""
        if self._cache_entries == 0:
            self._cache.clear()
            return
        budget = self._budget
        if budget is not None:
            budget.release(bytes_=self._cache_bytes, entries=self._cache_entries)
        self._cache.clear()
        self._cache_bytes = 0
        self._cache_entries = 0

    def _release_base_locked(self, *, clear_overlays: bool = True) -> None:
        """Release the view/map charge after no in-flight resolver can observe its overlays."""
        budget = self._budget
        if budget is not None and self._base_entries:
            budget.release(bytes_=self._base_bytes, entries=self._base_entries)
        self._base_bytes = 0
        self._base_entries = 0
        if clear_overlays:
            self._changed.clear()
            self._pending.clear()
            self._ended = frozenset()
            self._fingerprint = ()


@dataclass(slots=True)
class _OwnerLandingSlot:
    """One paid table-registry slot whose view may be built outside the guard."""

    state: str = "building"
    view: _OwnerLandingView | None = None


class _OwnerLandingTxnMemo:
    """All bounded lazy landing views owned by one transaction and catalog picture."""

    __slots__ = (
        "_base_charged",
        "_budget",
        "_retired",
        "schema",
        "snapshot",
        "tables",
        "txn",
    )

    def __init__(
        self,
        *,
        txn: object,
        snapshot: object,
        schema: Catalog,
        budget: _OwnerLandingBudget,
    ) -> None:
        self.txn = txn
        self.snapshot = snapshot
        self.schema = schema
        self._budget = budget
        self.tables: dict[int, _OwnerLandingSlot] = {}
        self._retired = False
        self._base_charged = False
        # Pay for the memo object, engine txn-id entry and empty table registry before any is
        # reachable.  A saturated budget therefore cannot grow a side registry of fallbacks.
        budget.reserve(bytes_=_OWNER_LANDING_MEMO_BYTES, entries=1)
        self._base_charged = True

    def claim(self, table_id: int) -> _OwnerLandingSlot:
        """Install one paid build slot; caller holds the injected registry guard."""
        if self._retired:
            raise _OwnerLandingCapacity
        present = self.tables.get(table_id)
        if present is not None:
            return present
        self._budget.reserve(bytes_=_OWNER_LANDING_TABLE_BYTES, entries=1)
        slot = _OwnerLandingSlot()
        try:
            self.tables[table_id] = slot
        except BaseException:
            self._budget.release(bytes_=_OWNER_LANDING_TABLE_BYTES, entries=1)
            raise
        return slot

    def begin_rebuild(
        self, table_id: int, slot: _OwnerLandingSlot
    ) -> _OwnerLandingView | None:
        """Detach an invalid view while preserving its already-paid table slot."""
        if self._retired or self.tables.get(table_id) is not slot:
            return None
        previous = slot.view
        slot.view = None
        slot.state = "building"
        return previous

    def install(
        self, table_id: int, slot: _OwnerLandingSlot, view: _OwnerLandingView
    ) -> bool:
        """Publish a complete view only while its paid build slot is still current."""
        if (
            self._retired
            or self.tables.get(table_id) is not slot
            or slot.state != "building"
        ):
            return False
        slot.view = view
        slot.state = "ready"
        return True

    def abandon_build(self, table_id: int, slot: _OwnerLandingSlot) -> None:
        """Release a failed build slot without leaving an unmetered disabled marker."""
        if self.tables.get(table_id) is not slot:
            return
        self.tables.pop(table_id)
        slot.state = "retired"
        self._budget.release(bytes_=_OWNER_LANDING_TABLE_BYTES, entries=1)
        self._release_base_if_empty()

    def retire(self) -> tuple[_OwnerLandingView, ...]:
        """Detach all complete views and release registry charges at settlement."""
        self._retired = True
        closers: list[_OwnerLandingView] = []
        for table_id, slot in tuple(self.tables.items()):
            self.tables.pop(table_id)
            self._budget.release(bytes_=_OWNER_LANDING_TABLE_BYTES, entries=1)
            if slot.view is not None:
                closers.append(slot.view)
                slot.view = None
            slot.state = "retired"
        self._release_base_if_empty()
        return tuple(closers)

    def _release_base_if_empty(self) -> None:
        """Return the memo/map charge exactly once after retirement."""
        if self._retired and not self.tables and self._base_charged:
            self._budget.release(bytes_=_OWNER_LANDING_MEMO_BYTES, entries=1)
            self._base_charged = False


class QueryEngine:
    """The query surface of one database (CONTRACT.md section 8.9).

    Every collaborator arrives by construction. The catalog, the heap and the buffer pool are
    required, because a statement cannot be planned without a schema, answered without rows, or
    logged without page images. The index framework and the vector subsystem are optional, and a
    statement that needs one this composition lacks refuses with
    :class:`GrafxUnsupportedOperation` naming it rather than falling back to something slower and
    different -- an index seek quietly becoming a scan is a performance surprise, but a
    similarity search quietly becoming nothing is a wrong result.
    """

    __slots__ = (
        "_catalog",
        "_heap",
        "_pool",
        "_indexes",
        "_vectors",
        "_metrics",
        "_clock",
        "_skipped_indexes",
        "_working",
        "_owner_memo",
        "_owner_budget",
        "_endpoint_memo",
        "_endpoint_budget",
        "_endpoint_guard",
        "_txn_effects",
        "_skip_claims",
        "_durable_skips",
        "_schema_artifact_section",
        "_page_stager",
        "_max_statement_writes",
        "_max_result_rows",
        "_max_intermediate_rows",
        "_max_traversal_expansions",
        "_max_traversal_paths",
    )

    def __init__(
        self,
        *,
        catalog: CatalogStore,
        heap: HeapStore,
        pool: BufferPool,
        metrics: MetricsSink,
        clock: Clock,
        indexes: object = None,
        vectors: object = None,
        page_stager: Callable[[object, str, int, bytes], None] | None = None,
        schema_artifact_section: Callable[..., object] | None = None,
        endpoint_locator_guard: AbstractContextManager[object] | None = None,
        max_statement_writes: int | None = None,
        max_result_rows: int | None = None,
        max_intermediate_rows: int | None = None,
        max_traversal_expansions: int | None = None,
        max_traversal_paths: int | None = None,
    ) -> None:
        """Adopt one catalog, one heap, one pool and whichever engines this composition has."""
        self._catalog = catalog
        self._heap = heap
        self._pool = pool
        # Tables whose primary-key index could not be created. Reported rather than
        # raised: see _attach_primary_key_index.
        self._skipped_indexes: set[str] = set()
        # The working catalog of each open schema transaction: the COPY its DDL statements
        # mutate and stage, keyed by txn id. The live catalog is untouched until the commit
        # applies the staged images and the structure epoch makes it re-read. Entries are
        # dropped by settle_schema; a caller that drives the manager directly and never settles
        # leaks the copy until the process ends, which is memory, not a wrong answer -- txn ids
        # are never reused, so a stale entry can never be read.
        self._working: dict[int, Catalog] = {}
        # Each open transaction's bounded, lazy landing views.  They retain only identities a
        # traversal actually requested and die through settle_schema on commit, rollback or
        # retry.  The shared budget bounds all decoded payload retention across transactions.
        self._owner_memo: dict[int, _OwnerLandingTxnMemo] = {}
        # Endpoint identities are resolved by one bounded canonical prefix walk per table and
        # transaction.  Both endpoint refs and decoded owner landings are explicitly metered.
        # Its lock is separate from BufferPool's: holding the page-cache lock while entering the
        # heap would invert ownership.  Only registry/accounting phases enter this guard; the
        # locator and landing heap walks happen after it has been released.
        self._endpoint_guard = (
            nullcontext() if endpoint_locator_guard is None else endpoint_locator_guard
        )
        self._owner_budget = _OwnerLandingBudget(
            max_bytes=_OWNER_LANDING_MAX_BYTES,
            max_entries=_OWNER_LANDING_MAX_ENTRIES,
            guard=self._endpoint_guard,
        )
        self._endpoint_memo: dict[int, _EndpointTxnMemo] = {}
        self._endpoint_budget = _EndpointLocatorBudget(
            max_bytes=_ENDPOINT_LOCATOR_MAX_BYTES,
            max_entries=_ENDPOINT_LOCATOR_MAX_ENTRIES,
            guard=self._endpoint_guard,
        )
        # Every out-of-transaction effect each open schema transaction has made -- indexes
        # registered, spaces attached, skip-report entries, index files created -- in the order
        # it made them. The rollback undo. Pruning by table id against the live catalog was
        # tried first and fails exactly when it matters: a loser's table and the winner's table
        # both allocated their ids from the same committed pages, so the loser's registrations
        # looked alive and the documented retry was then refused by its own predecessor's
        # leftovers.
        self._txn_effects: dict[int, list[_SchemaEffect]] = {}
        self._skip_claims: dict[str, set[object]] = {}
        self._durable_skips: set[str] = set()
        self._indexes = indexes
        self._vectors = vectors
        self._page_stager = page_stager
        self._schema_artifact_section = schema_artifact_section
        self._max_statement_writes = _require_optional_positive_limit(
            "max_statement_writes", max_statement_writes
        )
        self._max_result_rows = _require_optional_positive_limit(
            "max_result_rows", max_result_rows
        )
        self._max_intermediate_rows = _require_optional_positive_limit(
            "max_intermediate_rows", max_intermediate_rows
        )
        self._max_traversal_expansions = _require_optional_positive_limit(
            "max_traversal_expansions", max_traversal_expansions
        )
        self._max_traversal_paths = _require_optional_positive_limit(
            "max_traversal_paths", max_traversal_paths
        )
        self._metrics = MetricEmitter(metrics)
        self._clock = clock
        if metrics.enabled:
            for descriptor in QUERY_METRICS:
                metrics.register(descriptor)

    # --- doors -------------------------------------------------------------------------------

    def parse(self, text: str) -> Statement:
        """Return the statement one query text denotes (CONTRACT.md section 8.9)."""
        started = self._reading()
        try:
            statement = parse_text(text)
        except GrafxError as failure:
            self._count_error(failure)
            raise
        self._observe(PHASE_PARSE, started)
        return statement

    def plan(self, statement: Statement, snapshot: object = None) -> PlanNode:
        """Return the ONE operator tree that answers a statement (CONTRACT.md section 8.9).

        The snapshot is accepted because the frozen signature names it, and it deliberately does
        not choose operators: a plan whose SHAPE depended on the view would make ``explain``
        describe a query nobody ran, and AC-7 asks a caller to inspect the tree that runs.
        Everything that does depend on the view -- which versions are visible, how large the
        filtered set turns out to be -- is decided while the plan runs, by the components that
        own those questions.
        """
        return self.planned(statement).root

    def explain(self, text: str) -> PlanNode:
        """Return the operator tree of one query text, without running it (SPEC-VEC AC-7)."""
        return self.plan(self.parse(text))

    def _planned_for(
        self, statement: Statement, txn: object, working: Catalog | None
    ) -> PlannedQuery:
        """Return the plan a statement RUNS with, seeing this transaction's own schema.

        The public ``planned``/``explain`` doors describe the database as committed, which is
        what an outside caller asks about. A statement running INSIDE a transaction that has
        already declared tables must be planned against that transaction's working catalog, or
        the second statement of the quick start's schema block fails to plan the table the first
        one declared. A table this transaction has already changed also withholds its indexes:
        only a scan can be safely combined with pending inserts and changed primary keys.
        """
        dirty_tables = _intent_table_ids(txn)
        if working is None and not dirty_tables:
            return self.planned(statement)
        started = self._reading()
        try:
            analysis = analyze(statement)
            plan = build_plan(
                statement,
                catalog=working if working is not None else self._catalog.catalog,
                indexes=self._index_definitions(
                    catalog=working if working is not None else self._catalog.catalog,
                    without_indexes_for=dirty_tables,
                ),
                analysis=analysis,
            )
        except GrafxError as failure:
            self._count_error(failure)
            raise
        self._observe(PHASE_PLAN, started)
        return plan

    def planned(self, statement: Statement) -> PlannedQuery:
        """Return the plan together with the analysis it was built from."""
        started = self._reading()
        try:
            analysis = analyze(statement)
            plan = build_plan(
                statement,
                catalog=self._catalog.catalog,
                indexes=self._index_definitions(catalog=self._catalog.catalog),
                analysis=analysis,
            )
        except GrafxError as failure:
            self._count_error(failure)
            raise
        self._observe(PHASE_PLAN, started)
        return plan

    def execute(
        self,
        text: str,
        txn: object,
        parameters: Mapping[str, object] | None = None,
    ) -> QueryResult:
        """Run one statement inside a transaction and return its rows."""
        return self._execute_parsed(self.parse(text), txn, parameters)

    def _execute_parsed(
        self,
        statement: Statement,
        txn: object,
        parameters: Mapping[str, object] | None = None,
    ) -> QueryResult:
        """Run a parsed statement while still planning against current transaction state.

        This internal door lets a bounded facade reuse syntax work for ``executemany``. Planning
        is intentionally repeated: earlier items can dirty tables, and their indexes must then be
        withheld so later items retain read-your-own-writes correctness.
        """
        working = self._working.get(getattr(txn, "txn_id", None))
        if working is not None and not self._txn_stages_catalog(txn):
            working = None
        plan = self._planned_for(statement, txn, working)
        started = self._reading()
        try:
            result = self._run(
                plan, txn, self._bind_parameters(plan, parameters), catalog=working
            )
        except GrafxError as failure:
            self._count_error(failure)
            raise
        self._observe(PHASE_EXECUTE, started)
        if self._metrics.enabled:
            self._metrics.observe(_ROWS_RETURNED, float(len(result.rows)))
        return result

    def open_cursor(
        self,
        text: str,
        txn: object,
        parameters: Mapping[str, object] | None = None,
    ) -> _QueryResultCursor:
        """Open a pull-driven cursor for one read statement under ``txn``'s snapshot.

        Incremental write execution is intentionally refused.  A caller may close a cursor at
        any batch boundary, while a statement that writes is atomic only after its whole
        pipeline has been evaluated and handed to the transaction.  ``execute`` remains the
        materialised door for those statements.
        """
        mode = getattr(txn, "mode", None)
        mode_name = getattr(mode, "value", mode)
        if mode_name != "read":
            failure = GrafxTransactionStateError(
                "A query cursor owns a read-only snapshot; open it from a read transaction.",
                field="cursor",
                value="write_transaction",
                mode=mode_name,
            )
            self._count_error(failure)
            raise failure
        statement = self.parse(text)
        working = self._working.get(getattr(txn, "txn_id", None))
        if working is not None and not self._txn_stages_catalog(txn):
            working = None
        plan = self._planned_for(statement, txn, working)
        started = self._reading()
        try:
            root = plan.root
            if not isinstance(root, ProduceResults) or not root.columns:
                raise GrafxUnsupportedOperation(
                    "A query cursor streams a read statement with a RETURN result; use "
                    "execute() for schema or non-returning statements.",
                    field="cursor",
                    value=root.label,
                )
            if _plan_writes(root):
                raise GrafxUnsupportedOperation(
                    "A query cursor cannot incrementally execute a statement that writes; "
                    "use execute() so the statement remains atomic.",
                    field="cursor",
                    value="write_plan",
                )
            bound = self._bind_parameters(plan, parameters)
            coalesce_types = _bound_coalesce_types(plan, bound)
            case_types = _bound_case_types(plan, bound)
            statistics: dict[str, int] = {}
            context = _Context(
                engine=self,
                txn=txn,
                parameters=bound,
                analysis=plan.analysis,
                statistics=statistics,
                coalesce_types=coalesce_types,
                timestamp_values={},
                case_types=case_types,
                catalog=working,
                result_node=root.child,
                union_coercions=_bound_union_columns(plan, bound),
            )
            _bind_timestamp_values(plan, context)
            _validate_bound_subscript_types(plan, bound)
            _validate_bound_label_arguments(plan, bound)
            rows = self._rows(root.child, context)
            elapsed = self._clock.monotonic() - started if self._metrics.enabled else 0.0
            return _QueryResultCursor(
                engine=self,
                root=root,
                context=context,
                rows=rows,
                initial_seconds=elapsed if elapsed > 0.0 else 0.0,
            )
        except GrafxError as failure:
            self._count_error(failure)
            raise

    def __repr__(self) -> str:
        """Return a short representation naming which engines this one was given."""
        present = [
            name
            for name, value in (("indexes", self._indexes), ("vectors", self._vectors))
            if value is not None
        ]
        return f"QueryEngine(with={present})"

    # --- planning support ---------------------------------------------------------------------

    def _index_definitions(
        self,
        *,
        catalog: Catalog,
        without_indexes_for: frozenset[int] = frozenset(),
    ) -> tuple[object, ...]:
        """Return usable index definitions, withholding tables that need an owner overlay."""
        if self._indexes is None:
            return ()
        listing = getattr(self._indexes, "indexes", None)
        if listing is None:
            return ()
        # A STALE index is withheld from the planner, and that is a correctness rule rather than
        # a policy. A stale index is a SUBSET of what the heap holds -- entries it never received
        # -- and being a subset is exactly what the EXACT contract cannot repair: validating a
        # candidate against the heap removes hits that should not be there and cannot invent ones
        # that are missing. So a plan built on one answers a keyed read with fewer rows than
        # exist. Withholding it plans the scan the engine planned before any index existed, which
        # is slower and right. The index says so itself through `stale`, and `Database.
        # stale_indexes` is where an operator sees which ones need rebuilding.
        tables = {
            (table.table_id, table.name): table for table in catalog.tables()
        }
        return tuple(
            index.definition
            for index in listing()
            if not getattr(index, "stale", False)
            and index.definition.table_id not in without_indexes_for
            and (
                (table := tables.get(
                    (index.definition.table_id, index.definition.table_name)
                ))
                is not None
                and index_definition_matches_table(index.definition, table)
            )
        )

    def _bind_parameters(
        self, plan: PlannedQuery, parameters: Mapping[str, object] | None
    ) -> dict[str, Value]:
        """Return the parameter values this statement needs, refusing a missing one.

        Refusing before anything runs is the point. A parameter discovered missing halfway
        through would leave a transaction carrying half a statement, and one silently defaulted
        to null would answer a different question from the one the caller asked.
        """
        supplied: Mapping[str, object] = parameters if parameters is not None else {}
        if not isinstance(supplied, Mapping):
            raise GrafxPlanError(
                "Parameters are supplied as a mapping of names to values; got "
                f"{type(supplied).__name__}.",
                field="parameters",
                value=type(supplied).__name__,
            )
        bound: dict[str, Value] = {}
        for name in plan.analysis.parameters:
            if name not in supplied:
                known = ", ".join(sorted(str(key) for key in supplied)) or "none"
                raise GrafxPlanError(
                    f"The query needs the parameter ${name}, and the call supplied {known}.",
                    field="parameter",
                    value=name,
                )
            bound[name] = supplied[name]  # type: ignore[assignment]
        _validate_parameter_maps(bound)
        return bound

    # --- running -----------------------------------------------------------------------------

    def _run(
        self,
        plan: PlannedQuery,
        txn: object,
        parameters: dict[str, Value],
        catalog: Catalog | None = None,
    ) -> QueryResult:
        """Walk the plan and produce the result."""
        root = plan.root
        statistics: dict[str, int] = {}
        if isinstance(root, (CreateNodeTable, CreateRelTable, CreateVectorSpace)):
            self._schema(root, txn, statistics)
            return QueryResult(plan=root, statistics=dict(statistics))
        if not isinstance(root, ProduceResults):
            raise GrafxPlanError(
                f"A plan is rooted at the result it produces; got {root.label}.",
                field="plan",
                value=root.label,
            )
        coalesce_types = _bound_coalesce_types(plan, parameters)
        case_types = _bound_case_types(plan, parameters)
        context = _Context(
            engine=self,
            txn=txn,
            parameters=parameters,
            analysis=plan.analysis,
            statistics=statistics,
            coalesce_types=coalesce_types,
            timestamp_values={},
            case_types=case_types,
            catalog=catalog,
            result_node=root.child if root.columns else None,
            union_coercions=_bound_union_columns(plan, parameters),
        )
        _bind_timestamp_values(plan, context)
        _validate_bound_subscript_types(plan, parameters)
        _validate_bound_label_arguments(plan, parameters)
        stream = self._rows(root.child, context)
        rows = self._collect_result_rows(stream) if root.columns else tuple(stream)
        context.release()
        produced: tuple[tuple[Value, ...], ...] = ()
        if root.columns:
            produced = tuple(_projected(row, root.columns) for row in rows)
        return QueryResult(
            columns=root.columns,
            rows=produced,
            plan=root,
            statistics=dict(statistics),
        )

    def _rows(self, node: PlanNode, context: _Context) -> Iterator[_Row]:
        """Return the rows one operator produces."""
        handler = _HANDLERS.get(type(node))
        if handler is None:
            raise GrafxUnsupportedOperation(
                f"The operator {node.label} has no implementation in this build.",
                field="operator",
                value=node.label,
            )
        rows = handler(self, node, context)
        if self._max_intermediate_rows is None or node is context.result_node:
            return rows
        return self._admit_intermediate_rows(node, context, rows)

    def _collect_result_rows(self, rows: Iterator[_Row]) -> tuple[_Row, ...]:
        """Collect public results incrementally, refusing before retaining row limit + 1."""
        limit = self._max_result_rows
        if limit is None:
            return tuple(rows)
        accepted: list[_Row] = []
        for row in rows:
            observed = len(accepted) + 1
            if observed > limit:
                raise GrafxQueryBudgetExceeded(
                    f"Query would exceed max_result_rows: limit {limit}, "
                    f"observed {observed}.",
                    field="max_result_rows",
                    limit=limit,
                    observed=observed,
                )
            accepted.append(row)
        return tuple(accepted)

    def _admit_intermediate_rows(
        self, node: PlanNode, context: _Context, rows: Iterator[_Row]
    ) -> Iterator[_Row]:
        """Refuse before an operator delivers row limit + 1 to its parent."""
        for row in rows:
            context.admit_intermediate(node)
            yield row

    # --- schema ------------------------------------------------------------------------------

    def _schema(self, node: PlanNode, txn: object, statistics: dict[str, int]) -> None:
        """Install one schema change and stage the catalog pages it wrote.

        A catalog change is a whole structure rather than a versioned row, so it needs none of
        the three clauses a row write waits on: no identity to allocate, no birth stamp to place
        and no endpoint format to agree. ``save()`` returns the pages of the chain it wrote,
        which is exactly what CONTRACT.md section 8.5 step 4 turns into log records.
        """
        mode = getattr(getattr(txn, "mode", None), "value", None)
        if mode is not None and mode != "write":
            # Refused by MODE, up front. The callable check below is satisfied by a READ
            # context too -- its refusal comes when stage_page_image is CALLED, which is after
            # the index registrations -- so db.execute("CREATE TABLE ...") registered indexes
            # first and refused second, and the legitimate write-door retry of the same DDL was
            # then refused for the life of the process.
            raise GrafxTransactionStateError(
                f"A schema statement writes, and this transaction was opened {mode!r}; open a "
                f"write transaction.",
                field="mode",
                value=mode,
            )
        stage = getattr(txn, "stage_page_image", None)
        if stage is None or not callable(stage):
            # Refused BEFORE any side effect -- the working copy, the index registration, the
            # vector attach -- so a caller that handed no write transaction leaves nothing
            # behind, and gets the taxonomy rather than an AttributeError from deep inside.
            raise GrafxTransactionStateError(
                "A statement that writes needs a write transaction to stage its pages on; the "
                f"object supplied is a {type(txn).__name__}.",
                field="transaction",
                value=type(txn).__name__,
            )
        # THE STATEMENT IS HELD UNTIL COMPLETE, like every row statement (see the hold
        # doctrine at the top of this module): a refusal must leave the transaction exactly as
        # it found it. Two things make that non-trivial here. The working catalog is MUTATED in
        # place, so the statement works on a CLONE and the clone is adopted only at the end -- a
        # refusal after `add_table` used to leave the phantom table in the remembered copy, and
        # a later INSERT in the same transaction then committed durable rows for a table no
        # catalog would ever describe, with verify() clean. And the index attaches install
        # state OUTSIDE the transaction (the registry, the vector engine's space map), so every
        # attach records its undo in a journal that a refusal replays in reverse -- by NAME,
        # never by pruning against a catalog, because another open transaction's attachments
        # are absent from every catalog this statement can see.
        base = self._working_catalog(txn)
        catalog = Catalog.deserialize(base.serialize())
        undo: list[_SchemaEffect] = []
        take_mark = getattr(txn, "staging_mark", None)
        discard = getattr(txn, "discard_since", None)
        settle = getattr(txn, "settle_staging_mark", None)
        mark = take_mark() if callable(take_mark) else None
        try:
            boundary = (
                self._schema_artifact_section(
                    sync_if=self._needs_committed_artifact_sync
                )
                if self._schema_artifact_section is not None
                else nullcontext()
            )
            with boundary:  # type: ignore[attr-defined]
                self._schema_change(node, txn, statistics, catalog, undo)
            if mark is not None and callable(settle):
                settle(mark)
        except BaseException as failure:
            if mark is not None and callable(discard):
                try:
                    discard(mark)
                except BaseException as unwind_failure:
                    try:
                        failure.add_note(
                            "Schema staging also failed to restore its transaction mark "
                            f"({type(unwind_failure).__name__}): {unwind_failure}"
                        )
                    except BaseException:
                        pass
            try:
                self._unwind_schema_statement(undo)
            except BaseException as unwind_failure:
                txn_id = getattr(txn, "txn_id", None)
                if isinstance(txn_id, int) and not isinstance(txn_id, bool):
                    self._txn_effects.setdefault(txn_id, []).extend(undo)
                try:
                    failure.add_note(
                        "Schema artifact unwind was deferred for transaction settlement "
                        f"({type(unwind_failure).__name__}): {unwind_failure}"
                    )
                except BaseException:
                    pass
            raise
        self._remember_working(txn, catalog)
        txn_id = getattr(txn, "txn_id", None)
        if isinstance(txn_id, int) and not isinstance(txn_id, bool):
            self._txn_effects.setdefault(txn_id, []).extend(undo)

    def _schema_change(
        self,
        node: PlanNode,
        txn: object,
        statistics: dict[str, int],
        catalog: Catalog,
        undo: list[_SchemaEffect],
    ) -> None:
        """Apply one schema statement to the working CLONE, journalling external effects."""
        if isinstance(node, CreateVectorSpace):
            catalog.add_space(
                EmbeddingSpaceDef(
                    space_id=catalog.next_space_id(),
                    name=node.name,
                    dimension=node.dimension,
                    metric=node.metric,
                    normalized=node.normalized,
                    storage_dtype=node.storage_dtype,
                    created_at_wall=self._clock.wall(),
                )
            )
            statistics["spaces_created"] = statistics.get("spaces_created", 0) + 1
        elif isinstance(node, CreateNodeTable):
            installed = catalog.add_table(
                TableDef(
                    table_id=catalog.next_table_id(),
                    name=node.name,
                    kind="node",
                    columns=node.columns,
                    primary_key=node.primary_key,
                )
            )
            self._attach_primary_key_index(installed, statistics, undo, txn)
            self._attach_vector_columns(installed, statistics, catalog, undo, txn)
            statistics["tables_created"] = statistics.get("tables_created", 0) + 1
        elif isinstance(node, CreateRelTable):
            installed = catalog.add_table(
                TableDef(
                    table_id=catalog.next_table_id(),
                    name=node.name,
                    kind="rel",
                    columns=node.columns,
                    from_table=node.from_table,
                    to_table=node.to_table,
                )
            )
            self._attach_endpoint_indexes(installed, statistics, undo, txn)
            statistics["tables_created"] = statistics.get("tables_created", 0) + 1
        else:  # pragma: no cover - the caller checked the type
            raise GrafxPlanError(
                f"The operator {node.label} is not a schema change.",
                field="operator",
                value=node.label,
            )
        # The staged door, not save(). save() wrote through the pool the moment it was
        # called, so a DDL that was then ROLLED BACK had already made its pages reachable: the
        # next flush of anyone carried them to the device, and the rolled-back table survived a
        # reopen -- an uncommitted schema change made durable, measured through connect() alone.
        # And save() returns the CHAIN pages only, so the file header reached the device outside
        # every log record and a crash between the barrier and the flush lost the schema with no
        # refusal anywhere. stage() answers with VALUES -- chain, freed pages, and page 0,
        # unconditionally -- and staging them on the transaction puts all three in the log.
        #
        # Restaging on a second DDL statement of the same transaction supersedes by page key,
        # which is complete because a schema only ever GROWS within a transaction (there is no
        # drop): the payload is monotonic, so a later staging never names fewer pages.
        staged = self._catalog.stage(catalog)
        for page_index, image in staged:
            self._stage_page_image(txn, self._catalog.file, page_index, image)
        statistics["pages_staged"] = statistics.get("pages_staged", 0) + len(staged)

    def _unwind_schema_statement(self, undo: list[_SchemaEffect]) -> None:
        """Reverse one refused schema statement's out-of-transaction effects, newest first.

        Runs while the refusal is already unwinding, so nothing here may raise. Each entry names
        exactly one thing this statement did -- an index registered, a space attached, a table
        added to the skip report -- so another open transaction's attachments are untouched.
        """
        boundary = (
            self._schema_artifact_section(
                sync_if=self._needs_committed_artifact_sync
            )
            if self._schema_artifact_section is not None
            else nullcontext()
        )
        with boundary:  # type: ignore[attr-defined]
            for effect in reversed(undo):
                try:
                    if isinstance(effect, _IndexObservationEffect):
                        discard = getattr(
                            self._indexes, "discard_schema_observation", None
                        )
                        if callable(discard):
                            discard(
                                effect.index,
                                effect.txn,
                                created=effect.created,
                            )
                    elif isinstance(effect, _IndexSchemaEffect):
                        drop = getattr(self._indexes, "discard_speculative", None)
                        if callable(drop):
                            drop(effect.artifact)
                    elif isinstance(effect, _VectorMapEffect):
                        settle = getattr(
                            self._vectors, "_settle_attachment", None
                        )
                        if callable(settle):
                            settle(
                                effect.space,
                                expected=effect.attached,
                                owner=effect.owner,
                                previous=effect.previous,
                                committed=False,
                            )
                    elif isinstance(effect, _SkipEffect):
                        claims = self._skip_claims.get(effect.table)
                        if claims is None or effect.owner not in claims:
                            continue
                        claims.remove(effect.owner)
                        if not claims:
                            self._skip_claims.pop(effect.table, None)
                            if effect.table not in self._durable_skips:
                                self._skipped_indexes.discard(effect.table)
                except GrafxError:
                    continue
            # A foreign process can publish an unindexed table while this process still owns a
            # speculative skip token.  Local refcounts cannot observe that publication, but the
            # freshly rebased catalog above can.  Recompute after token release so rollback never
            # erases the durable diagnostic merely because its publisher had another registry.
            self._refresh_durable_skips()

    def _needs_committed_artifact_sync(self) -> bool:
        """Detect a durable automatic artifact or vector-map mismatch."""
        if self._indexes is None:
            return False
        try:
            tables = tuple(self._catalog.catalog.tables())
        except (AttributeError, GrafxError):
            return False
        missing_for = getattr(
            self._indexes, "unregistered_persistent_indexes_for", None
        )
        if callable(missing_for) and missing_for(tables):
            return True
        vectors = self._vectors
        if vectors is None:
            return False
        mapped = getattr(vectors, "_by_space", {})
        for table in tables:
            for column in table.columns:
                if column.vector_space is None:
                    continue
                try:
                    space = self._catalog.catalog.space(column.vector_space)
                except GrafxError:
                    return True
                current = mapped.get(space.name)
                if current is None or not (
                    index_definition_matches_table(current.definition, table)
                    and current.space_id == space.space_id
                    and current.space_name == space.name
                    and current.dimension == space.dimension
                    and current.metric_of_space is space.metric
                    and current.storage_dtype == space.storage_dtype
                    and current.normalized == space.normalized
                ):
                    return True
        return False

    def _refresh_durable_skips(self) -> None:
        """Reconcile the skip diagnostic with the current durable catalog and registry."""
        if self._indexes is None:
            return
        try:
            registered = tuple(self._indexes.indexes())
            tables = tuple(self._catalog.catalog.tables())
        except (AttributeError, GrafxError):
            return
        durable: set[str] = set()
        for table in tables:
            try:
                definitions = automatic_index_definitions(table)
            except GrafxError:
                # An automatic scalar index name outside the identifier budget is precisely the
                # supported decline this diagnostic describes.
                durable.add(table.name)
                continue
            scalar = tuple(
                definition
                for definition in definitions
                if definition.visibility.value == "exact"
            )
            if scalar and any(
                not any(index.definition == expected for index in registered)
                for expected in scalar
            ):
                durable.add(table.name)
        self._durable_skips.clear()
        self._durable_skips.update(durable)
        self._skipped_indexes.clear()
        self._skipped_indexes.update(durable)
        self._skipped_indexes.update(self._skip_claims)

    def _working_catalog(self, txn: object) -> Catalog:
        """Return the catalog this transaction's schema statements build on.

        The SECOND statement of a schema transaction must see the first one's tables -- the
        quick start declares a space and the table that uses it in one block -- and the live
        catalog must not, because a refusal or a rollback would then leave the live catalog
        describing tables that never happened; measured through the public door, the rolled-back
        table survived a REOPEN, because save() had already pushed its pages into the pool.

        So each transaction gets a working COPY: fresh from the pages on its first schema
        statement, remembered across its later ones, discarded whole on rollback. The guard on
        the remembered copy is that the transaction still carries staged catalog images -- a
        transaction that somehow lost them gets a fresh read rather than a copy nothing else
        vouches for.
        """
        txn_id = getattr(txn, "txn_id", None)
        held = self._working.get(txn_id)
        if held is not None and self._txn_stages_catalog(txn):
            return held
        return self._catalog.read_from_pages()

    def _txn_stages_catalog(self, txn: object) -> bool:
        """Return True when this transaction holds staged images of the catalog file."""
        staged = getattr(txn, "staged_pages", None)
        if not callable(staged):
            return False
        return any(file == self._catalog.file for file, _index in staged())

    def _remember_working(self, txn: object, catalog: Catalog) -> None:
        """Keep this transaction's working catalog for its next schema statement."""
        txn_id = getattr(txn, "txn_id", None)
        if isinstance(txn_id, int) and not isinstance(txn_id, bool):
            self._working[txn_id] = catalog

    def settle_schema(self, txn_id: int, *, committed: bool) -> None:
        """Finish a transaction's schema bookkeeping, undoing its side effects on a rollback.

        The working copy is dropped either way. On a rollback the two side effects the DDL made
        OUTSIDE the transaction are undone as well: the indexes it registered (pruned by table
        id against the live catalog, which a rollback never touched) and the vector engine's
        per-space map. Nothing here may raise -- it runs while a rollback is already unwinding,
        and replacing its reason would hide why the transaction was abandoned at all.
        """
        working = self._working.get(txn_id)
        effects = self._txn_effects.get(txn_id)
        if committed:
            for effect in effects or ():
                if isinstance(effect, _IndexSchemaEffect):
                    settle_index = getattr(
                        self._indexes, "settle_speculative", None
                    )
                    if callable(settle_index):
                        settle_index(effect.artifact, committed=True)
                    continue
                if isinstance(effect, _VectorMapEffect):
                    settle = getattr(
                        self._vectors, "_settle_attachment", None
                    )
                    if callable(settle):
                        settle(
                            effect.space,
                            expected=effect.attached,
                            owner=effect.owner,
                            previous=effect.previous,
                            committed=True,
                        )
                    continue
                if not isinstance(effect, _SkipEffect):
                    continue
                self._durable_skips.add(effect.table)
                self._skipped_indexes.add(effect.table)
                claims = self._skip_claims.get(effect.table)
                if claims is not None:
                    claims.discard(effect.owner)
                    if not claims:
                        self._skip_claims.pop(effect.table, None)
            self._working.pop(txn_id, None)
            self._settle_owner_memo(txn_id)
            self._settle_endpoint_memo(txn_id)
            self._txn_effects.pop(txn_id, None)
            return
        # The transaction's own journal, replayed in reverse -- never a prune against a catalog.
        # A catalog prune was the first shape of this and it failed exactly when it mattered: a
        # loser's table and the winner's allocate their ids from the same committed pages, so by
        # table id the loser's registrations looked alive, and by ANY catalog another open
        # transaction's registrations look dead. The journal names precisely what this
        # transaction did, and nothing else.
        if working is not None or effects:
            self._unwind_schema_statement(effects or [])
        self._working.pop(txn_id, None)
        self._settle_owner_memo(txn_id)
        self._settle_endpoint_memo(txn_id)
        self._txn_effects.pop(txn_id, None)

    def _settle_owner_memo(self, txn_id: int) -> None:
        """Release one transaction's decoded landing state on every terminal path."""
        with self._endpoint_guard:
            memo = self._owner_memo.pop(txn_id, None)
            closers = () if memo is None else memo.retire()
        # A view can have an identity lookup in flight.  close() retires its cache immediately
        # and defers overlay release to that caller, without keeping this registry guard held.
        for view in closers:
            view.close()

    def _settle_endpoint_memo(self, txn_id: int) -> None:
        """Close one transaction's bounded derived walks on commit, rollback or retry."""
        with self._endpoint_guard:
            memo = self._endpoint_memo.pop(txn_id, None)
            closers = () if memo is None else memo.retire()
        # Cursor close is currently memory-only, but keeping collaborator work outside the
        # registry guard makes the phase boundary explicit and prevents a later close hook from
        # silently turning settlement into heap I/O under the lock.
        for locator in closers:
            locator.close()

    @property
    def skipped_indexes(self) -> tuple[str, ...]:
        """Return the tables whose primary-key index could not be created, in name order."""
        return tuple(sorted(self._skipped_indexes))

    def _stage_schema_index(
        self,
        candidate: object,
        txn: object,
        undo: list[_SchemaEffect],
    ) -> object:
        """Register/adopt one index and journal its object, nonce and empty observation."""
        register = getattr(self._indexes, "register_speculative", None)
        if not callable(register):
            register = getattr(self._indexes, "register")
            registered = register(
                candidate, complete_through=self._published_lsn_for_new_index()
            )
            artifact = None
        else:
            registered, artifact = register(
                candidate, complete_through=self._published_lsn_for_new_index()
            )
        if artifact is not None:
            undo.append(_IndexSchemaEffect(artifact))
        observe = getattr(self._indexes, "stage_schema_observation", None)
        if callable(observe):
            created = bool(observe(registered, txn))
        else:
            created = bool(registered.stage_empty_observation(txn))
        undo.append(
            _IndexObservationEffect(
                index=registered,
                txn=txn,
                created=created,
            )
        )
        return registered

    def _record_skipped_index(
        self, table_name: str, undo: list[_SchemaEffect]
    ) -> None:
        """Acquire one owner token for a speculative unindexed-table diagnostic."""
        owner = object()
        self._skip_claims.setdefault(table_name, set()).add(owner)
        self._skipped_indexes.add(table_name)
        undo.append(_SkipEffect(table=table_name, owner=owner))

    def _attach_primary_key_index(
        self,
        table: TableDef,
        statistics: dict[str, int],
        undo: list[_SchemaEffect],
        txn: object,
    ) -> None:
        """Create the index covering the primary key of a table this statement just created.

        This is where the pair exists for the first time, exactly as `_attach_vector_columns`
        argues for its own case: a table declares its primary key at creation, so the index is
        created here and re-adopted by every later open. Nothing else in the engine creates one,
        which would leave every keyed read on the table planning a full scan for ever.

        The table is EMPTY -- this is the statement that made it -- so the index covers everything
        there is to cover and is told so. Without that it would register behind the database's
        published position and be marked stale on a database that has ever committed anything,
        which is a rebuild for an index that has nothing to build.

        A composition without the index framework is not refused. An index is an accelerator here,
        not a semantic: the planner falls back to the scan it would have planned anyway, and
        refusing a CREATE TABLE because the composition has no C7 would take a working database
        away for a facility the caller never asked for. This is the opposite of the vector case,
        where the column would be permanently unsearchable and refusing is the only honest answer.
        """
        if self._indexes is None or table.primary_key is None:
            return
        try:
            index = primary_key_index(table, self._pool, self._metrics.sink)
            if index is None:
                return
            self._stage_schema_index(index, txn, undo)
        except GrafxIndexError as failure:
            if failure.details.get("field") not in {"name"}:
                raise
            # THE INDEX MAY NOT FAIL THE STATEMENT, and the first version of this let it.
            # `add_table` has already mutated the live catalog by the time this runs, so a
            # refusal here left the table INSTALLED and the statement REFUSED -- and the next
            # committed schema change wrote that table to disk, after which the re-adoption at
            # every later open raised the same refusal and the database could never be opened
            # again. Two ordinary inputs reach it through `connect()` alone: a table name of 126
            # characters or more, because `pk_` prepended to it exceeds the 128-character
            # identifier budget; and two tables whose names differ only by case, which the
            # catalog accepts as two tables and which fold to one index file name.
            #
            # So the accelerator declines instead. The table exists, its keyed reads plan the
            # scan they planned before any index existed, and the name is reported through
            # `Database.unindexed_tables` rather than being discovered as a slow query. This is
            # the same position the composition-without-C7 branch above already takes, and the
            # opposite of the vector case, where the column would be permanently unsearchable
            # and refusing is the only honest answer.
            statistics["indexes_skipped"] = statistics.get("indexes_skipped", 0) + 1
            self._record_skipped_index(table.name, undo)
            return
        except GrafxUnsupportedOperation:
            statistics["indexes_skipped"] = statistics.get("indexes_skipped", 0) + 1
            self._record_skipped_index(table.name, undo)
            return
        statistics["indexes_created"] = statistics.get("indexes_created", 0) + 1

    def _attach_endpoint_indexes(
        self,
        table: TableDef,
        statistics: dict[str, int],
        undo: list[_SchemaEffect],
        txn: object,
    ) -> None:
        """Create the two indexes covering the endpoints of a relationship table just declared.

        Same moment, same rules as the primary key's index: the pair exists when the table is
        declared, the table is EMPTY so the indexes cover everything there is to cover and are
        told so, and the accelerator DECLINES rather than failing the statement or a later open
        -- a table whose endpoint index cannot be created traverses by the scan it always did,
        and the name is reported through :attr:`skipped_indexes`.
        """
        if self._indexes is None:
            return
        try:
            for index in relationship_endpoint_indexes(
                table, self._pool, self._metrics.sink
            ):
                self._stage_schema_index(index, txn, undo)
        except GrafxIndexError as failure:
            if failure.details.get("field") not in {"name"}:
                raise
            statistics["indexes_skipped"] = statistics.get("indexes_skipped", 0) + 1
            self._record_skipped_index(table.name, undo)
            return
        except GrafxUnsupportedOperation:
            statistics["indexes_skipped"] = statistics.get("indexes_skipped", 0) + 1
            self._record_skipped_index(table.name, undo)
            return
        if getattr(table, "kind", None) == "rel":
            statistics["indexes_created"] = statistics.get("indexes_created", 0) + 2

    def _published_lsn_for_new_index(self) -> int:
        """Return the position a brand-new index over an empty table may claim to cover.

        ``IndexManager.published_lsn`` is a PROPERTY, and the first version of this asked
        ``callable()`` before reading it. A property read through ``getattr`` is already the int,
        so the test was always False and this always returned 0 -- which made
        ``advance_built_through`` a no-op on the position and left every table declared in a
        session after the first with an index that registered behind the published position, was
        marked stale on the spot, and could never be lifted, because ``_advance`` refuses to move
        a stale index. Exactly the failure the caller's docstring says it prevents. Every test of
        it created its table in the FIRST session, where the published position is 0 and the bug
        cannot show (LESSONS L24, L30).
        """
        published = getattr(self._indexes, "published_lsn", None)
        if isinstance(published, int) and not isinstance(published, bool):
            return published
        if callable(published):  # a composition whose manager exposes it as a method
            return int(published())
        return 0

    def _attach_vector_columns(
        self,
        table: TableDef,
        statistics: dict[str, int],
        catalog: Catalog | None = None,
        undo: list[_SchemaEffect] | None = None,
        txn: object | None = None,
    ) -> None:
        """Create the index of every embedding space a new table declares a column in.

        An index covers a (table, space) PAIR, and the vector subsystem says so explicitly: a
        space exists before any table declares a column in it, so ``create_space`` does not build
        an index and ``attach`` does. The moment a table declares the column is the moment the
        pair exists, and this is the statement that creates the table -- so this is where the
        call belongs. Nothing else in the engine calls it, which would leave a declared vector
        column permanently unsearchable.

        A table with a vector column and no vector subsystem is refused rather than created. The
        alternative is a table whose column can never be searched and whose refusal would arrive
        much later, at a query, naming nothing the caller did wrong.
        """
        spaces = tuple(
            str(column.vector_space)
            for column in table.columns
            if column.is_vector and column.vector_space is not None
        )
        if not spaces:
            return
        vectors = self.require_vectors()
        attach = getattr(vectors, "attach", None)
        if attach is None or not callable(attach):
            # A different condition from "there is no vector subsystem", and it carries a
            # different field so a test can tell which of the two answered (amendment A62).
            raise GrafxUnsupportedOperation(
                "The vector subsystem of this composition offers no way to attach an index to a "
                f"table, so the column of {table.name!r} could never be searched.",
                field="attach",
                value=type(vectors).__name__,
            )
        for space in spaces:
            attach_speculative = getattr(vectors, "_attach_speculative", None)
            if undo is not None and callable(attach_speculative):
                attached, artifact, previous, owner = attach_speculative(
                    table, space, catalog
                )
                if artifact is not None:
                    undo.append(_IndexSchemaEffect(artifact))
                undo.append(
                    _VectorMapEffect(
                        space=space,
                        attached=attached,
                        previous=previous,
                        owner=owner,
                    )
                )
            else:
                attached = attach(table, space, catalog)
                artifact = None
            if txn is not None:
                observe = getattr(self._indexes, "stage_schema_observation", None)
                created = (
                    bool(observe(attached, txn))
                    if callable(observe)
                    else bool(attached.stage_empty_observation(txn))
                )
                if undo is not None:
                    undo.append(
                        _IndexObservationEffect(
                            index=attached,
                            txn=txn,
                            created=created,
                        )
                    )
            statistics["indexes_attached"] = statistics.get("indexes_attached", 0) + 1

    def _stage(self, txn: object, file: str, pages: Sequence[int]) -> None:
        """Stage the current image of each page, so the commit turns it into a log record.

        A page changed but never staged reaches the device with no record behind it: invisible
        to redo, and to every other process until a checkpoint happens to write it. That is the
        durability hole CONTRACT.md section 8.5 exists to close, so a caller that hands this
        engine no write transaction is refused rather than allowed to write unlogged bytes.
        """
        stage = self._page_stager
        if stage is not None:
            for page_index in sorted(set(pages)):
                with self._pool.pinned(file, page_index) as page:
                    stage(txn, file, page_index, self._pool.codec.encode_page(page))
            return
        stage = getattr(txn, "stage_page_image", None)
        if stage is None or not callable(stage):
            raise GrafxTransactionStateError(
                "A statement that writes needs a write transaction to stage its pages on; the "
                f"object supplied is a {type(txn).__name__}.",
                field="transaction",
                value=type(txn).__name__,
            )
        for page_index in sorted(set(pages)):
            with self._pool.pinned(file, page_index) as page:
                stage(file, page_index, self._pool.codec.encode_page(page))

    def _stage_page_image(
        self, txn: object, file: str, page_index: int, image: bytes
    ) -> None:
        """Stage one physical image through the production capability or an isolated test port."""
        if self._page_stager is not None:
            self._page_stager(txn, file, page_index, image)
            return
        stage = getattr(txn, "stage_page_image", None)
        if stage is None or not callable(stage):
            raise GrafxTransactionStateError(
                "A statement that writes needs a write transaction to stage its pages on; the "
                f"object supplied is a {type(txn).__name__}.",
                field="transaction",
                value=type(txn).__name__,
            )
        stage(file, page_index, image)

    # --- collaborators -----------------------------------------------------------------------

    def require_indexes(self) -> object:
        """Return the index framework, refusing when this composition has none."""
        if self._indexes is None:
            raise GrafxUnsupportedOperation(
                "This database was composed without the index framework (C7), so an index seek "
                "cannot be answered.",
                field="component",
                value="indexes",
            )
        return self._indexes

    def require_vectors(self) -> object:
        """Return the vector subsystem, refusing when this composition has none."""
        if self._vectors is None:
            raise GrafxUnsupportedOperation(
                "This database was composed without the vector subsystem (C9), so a similarity "
                "search cannot be answered.",
                field="component",
                value="vectors",
            )
        return self._vectors

    @property
    def heap(self) -> HeapStore:
        """Return the heap store this engine reads rows from."""
        return self._heap

    @property
    def catalog(self) -> CatalogStore:
        """Return the catalog store this engine plans against."""
        return self._catalog

    # --- metrics -----------------------------------------------------------------------------

    def _reading(self) -> float:
        """Return a monotonic reading when anything is collecting, and zero when nothing is."""
        return self._clock.monotonic() if self._metrics.enabled else 0.0

    def _observe(self, phase: str, started: float) -> None:
        """Record how long one phase took."""
        if not self._metrics.enabled:
            return
        elapsed = self._clock.monotonic() - started
        self._metrics.observe(
            _PHASE_DURATION, elapsed if elapsed > 0.0 else 0.0, {"phase": phase}
        )

    def _count_error(self, failure: GrafxError) -> None:
        """Count one refused query under the error code it carries."""
        if self._metrics.enabled:
            self._metrics.increment(_ERRORS_TOTAL, 1.0, {"code": failure.code})


# --- operators --------------------------------------------------------------------------------


def _single_row(
    engine: QueryEngine, node: SingleRow, context: _Context
) -> Iterator[_Row]:
    """Produce the one empty row a bare RETURN reads."""
    yield _Row(bindings={})


def _unwind_rows(
    engine: QueryEngine, node: UnwindRows, context: _Context
) -> Iterator[_Row]:
    """Expand one list into rows, one element at a time.

    The list is read as the source of the statement, so a carrier that is not a list is
    refused before any row leaves this operator and therefore before anything downstream can
    stage a write. A string and a map are refused with it: both are iterable in Python and
    neither is a list, and quietly expanding one into characters or keys would answer a
    different query from the one that was written.

    Null INSIDE the list is an ordinary element and binds as null; only the carrier itself
    has to be a list. An empty list produces no rows, which is how a batch with nothing to do
    writes nothing rather than failing.
    """

    for row in engine._rows(node.child, context):
        carrier = _evaluate(node.expression, row, context)
        if not isinstance(carrier, (list, tuple)):
            named = "null" if carrier is None else type(carrier).__name__
            message = (
                f"{node.expression.describe()} is expanded by UNWIND, so it has to be a "
                f"list; got {named}."
            )
            raise GrafxPlanError(
                message,
                field="unwind",
                value=node.alias,
            )
        for element in carrier:
            yield _Row(bindings={**row.bindings, node.alias: element})


def _with_rows(
    engine: QueryEngine, node: WithRows, context: _Context
) -> Iterator[_Row]:
    """Project one stage and hand on a row bound to only the names it projected.

    Every item is evaluated against the row that ARRIVED, all of them before any is bound, so
    an item cannot read what another item of the same stage is producing.

    What leaves carries nothing else. A variable this stage did not name is gone from the row,
    which is what makes reading it below the stage a refusal rather than an accident of what
    the executor happened to still be holding. A matched row carried under its own name keeps
    the binding it had, so the clauses below still read its properties and still write it.
    """

    for row in engine._rows(node.child, context):
        projected = {
            item.name: _evaluate(item.expression, row, context) for item in node.items
        }
        yield _Row(bindings=projected)


def _all_nodes_scan(
    engine: QueryEngine, node: AllNodesScan, context: _Context
) -> Iterator[_Row]:
    """Produce every node row of every node table the plan named, under one name.

    One operator over many tables, not one operator per table: what comes out is a single
    stream, so the filter, the grouping, the ordering and the window above it see the whole set
    once. Each table contributes the same owner-only view a single-table scan gives -- committed
    rows under this snapshot, this transaction's own updates overlaid and its own deletes gone,
    and its own pending inserts appended -- and the tables are read in the order the plan fixed.
    """
    snapshot = context.snapshot
    views = [
        (table, *_transaction_row_view(context, table, include_held=False))
        for table in node.tables
    ]
    single_source = isinstance(node.child, SingleRow)
    for row in engine._rows(node.child, context):
        for table, changed, inserted in views:
            for ref, version in engine.heap.scan(table, snapshot):
                if ref in changed:
                    latest = changed[ref]
                    if latest is None:
                        continue
                    version = replace(version, values=latest)
                bindings = {} if single_source else dict(row.bindings)
                bindings[node.variable] = RowBinding(
                    variable=node.variable,
                    table=table,
                    ref=ref,
                    version=version,
                    polymorphic=True,
                )
                context.count("rows_scanned")
                yield _Row(bindings=bindings)
            for reference, values in inserted:
                bindings = {} if single_source else dict(row.bindings)
                bindings[node.variable] = _pending_binding(
                    node.variable, table, values, reference=reference, polymorphic=True
                )
                context.count("rows_scanned")
                yield _Row(bindings=bindings)


def _node_scan(
    engine: QueryEngine, node: NodeScan, context: _Context
) -> Iterator[_Row]:
    """Produce the owner's logical node rows, overlaid on its physical snapshot.

    The heap is deliberately scanned even when a primary-key index exists whenever this
    transaction has touched the table: an exact index describes only durable heap versions and
    cannot offer a pending insert or a pending row under its newly updated key. The settled view
    comes from the same reducer commit uses, so insert-update is one pending row and
    insert-delete is no row at all.
    """
    snapshot = context.snapshot
    changed, inserted = _transaction_row_view(context, node.table, include_held=False)
    single_source = isinstance(node.child, SingleRow)
    for row in engine._rows(node.child, context):
        for ref, version in engine.heap.scan(node.table, snapshot):
            if ref in changed:
                latest = changed[ref]
                if latest is None:
                    continue
                version = replace(version, values=latest)
            bindings = {} if single_source else dict(row.bindings)
            bindings[node.variable] = RowBinding(
                variable=node.variable, table=node.table, ref=ref, version=version
            )
            context.count("rows_scanned")
            yield _Row(bindings=bindings)
        for reference, values in inserted:
            bindings = {} if single_source else dict(row.bindings)
            bindings[node.variable] = _pending_binding(
                node.variable, node.table, values, reference=reference
            )
            context.count("rows_scanned")
            yield _Row(bindings=bindings)


def _index_lookup_versions(
    engine: QueryEngine,
    manager: object,
    name: str,
    key: bytes,
    snapshot: object,
    *,
    reuse_validated_version: bool,
    ended: Collection[object],
) -> Iterator[tuple[object, HeapVersion]]:
    """Yield index hits with their versions, reusing exact validation when available.

    ``IndexManager.lookup_versions`` is deliberately internal. Retaining every accepted version
    is safe only for a key whose semantic contract bounds its cardinality (currently an automatic
    primary key). General exact indexes -- notably relationship endpoint indexes for a hub --
    keep the lazy fallback so memory does not grow as ``degree * payload``. The fallback also
    preserves the existing collaborator boundary for a custom manager that only implements the
    frozen ``lookup`` door. ``LIMIT`` and owner overlays can stop before a later hit is read,
    exactly as before this optimisation.
    """
    lookup_versions = getattr(manager, "lookup_versions", None)
    if reuse_validated_version and callable(lookup_versions):
        yield from lookup_versions(name, key, snapshot)
        return
    lookup = getattr(manager, "lookup")
    for ref in lookup(name, key, snapshot):
        # The old fallback filtered this owner overlay before its second heap read. Preserve that
        # ordering: a row ended by this transaction is absent even if its stored bytes are now
        # corrupt, and observing that corruption here would expand the query's read surface.
        if ref in ended:
            continue
        yield ref, engine.heap.read(ref)


def _index_seek(
    engine: QueryEngine, node: IndexSeek, context: _Context
) -> Iterator[_Row]:
    """Produce one row per heap location the index offers for the key, per incoming row.

    The lookup goes through the index framework's own door, which is where the dual visibility
    rule of CONTRACT.md section 8.7 lives: an exact hit is validated against the heap under this
    snapshot before it is returned, a proximity hit is already decided. Deciding that here would
    be a second implementation of a rule that already has one, and the two would drift.
    """
    manager = engine.require_indexes()
    snapshot = context.snapshot
    arity = len(node.table.columns)
    positions = tuple(node.table.column_index(name) for name in node.key_columns)
    primary_position = (
        None
        if node.table.primary_key is None
        else node.table.column_index(node.table.primary_key)
    )
    reuse_validated_version = (
        node.visibility is IndexVisibility.EXACT
        and primary_position is not None
        and positions == (primary_position,)
        and node.index == primary_key_index_name(node.table.name)
    )
    ended = _ended_by_this_transaction(context)
    single_source = isinstance(node.child, SingleRow)
    for row in engine._rows(node.child, context):
        template: list[Value] = [None] * arity
        for position, expression in zip(positions, node.key_values):
            template[position] = _as_value(_evaluate(expression, row, context))
        key = index_key(template, positions)
        for ref, version in _index_lookup_versions(
            engine,
            manager,
            node.index,
            key,
            snapshot,
            reuse_validated_version=reuse_validated_version,
            ended=ended,
        ):
            if ref in ended:
                continue  # ended by this transaction: the same rule the scan applies
            bindings = {} if single_source else dict(row.bindings)
            bindings[node.variable] = RowBinding(
                variable=node.variable, table=node.table, ref=ref, version=version
            )
            context.count("rows_seeked")
            yield _Row(bindings=bindings)


_EDGE_LOOKUP_FAN_LIMIT: int = 64
"""Distinct starts from an unknown/bounded producer served before a grouped scan wins.

Chosen from the shape of the two costs, not tuned to a machine: a lookup costs a few bucket-page
reads however large the edge table is, and the grouped scan costs the whole edge table once.
NodeScan and AllNodesScan frontiers bypass this limit and scan immediately because their plan
already promises a whole-table walk. Unknown producers retain the conservative hybrid fallback."""


def _edge_steps(
    engine: QueryEngine,
    context: _Context,
    relationship: TableDef,
    from_table: TableDef,
    to_table: TableDef,
    outgoing: bool,
    incoming: bool,
    ended: frozenset[object] | set[object],
    changed: Mapping[object, tuple[Value, ...] | None],
    pending: Sequence[tuple[object, HeapVersion]] = (),
    *,
    bounded_frontier: bool = True,
) -> Callable[[object], Iterator[tuple[object, HeapVersion, TableDef, object]]]:
    """Return the function a traversal expands one frontier node with.

    Two regimes, chosen once per traversal and returning the same answer.

    **By index**, when every direction the pattern walks has its endpoint index present, owned
    by this table, and FRESH. A stored relationship row leads with its endpoints, so "the edges
    leaving this node" is exactly the question the ``ef_``/``et_`` indexes answer, and
    ``IndexManager.lookup`` discharges section 8.7 on the way: every candidate is validated
    against the heap under this snapshot. Endpoint hits deliberately remain ref-only here:
    retaining every decoded payload would make transient memory proportional to a hub's degree.
    Before this existed, a reverse hop into a well-referenced node of a 2500-node graph read all
    3600 edges and cost 1.46 s.

    **By one scan**, for a NodeScan/AllNodesScan frontier or when there is no usable index. A STALE
    index is a subset of the heap and the one thing validation cannot repair. The scan is taken
    ONCE and grouped by endpoint, so a frontier of F nodes costs O(E), not the O(F x E) the old
    per-node rescan paid; slower than the index for a bounded seek, and right, which is the same
    fallback rule the planner and the uniqueness check follow.
    """
    manager = engine._indexes
    lookup = getattr(manager, "lookup", None) if manager is not None else None

    def usable(name: str) -> str | None:
        """Return the name when that index is present, this table's own, and fresh."""
        try:
            index = manager.index(name)  # type: ignore[union-attr]
        except GrafxError:
            return None
        if (
            index.definition.table_id != relationship.table_id
            or index.definition.table_name != relationship.name
        ):
            return None  # a name collision, not this table's index
        if getattr(index, "stale", False):
            return None
        return name

    from_name = (
        usable(edge_from_index_name(relationship.name))
        if callable(lookup) and outgoing
        else None
    )
    to_name = (
        usable(edge_to_index_name(relationship.name))
        if callable(lookup) and incoming
        else None
    )
    indexed = ((not outgoing) or from_name) and ((not incoming) or to_name)
    if pending:
        # An endpoint index describes COMMITTED edges. An edge this transaction created is not in
        # it and cannot be put in it before the commit, so a lookup would answer a question about
        # a graph its owner is no longer looking at. The grouped scan can be overlaid; an index
        # answer cannot be, because what is missing from it leaves no trace to overlay.
        indexed = False
    snapshot = context.snapshot

    def owner_version(ref: object, version: HeapVersion) -> HeapVersion | None:
        """Overlay one committed edge with this transaction's latest property values.

        Relationship endpoints are layout, not properties.  Public SET refuses to name them,
        and this check is the fail-closed backstop for an intent staged through a lower-level
        collaborator: endpoint indexes and traversal direction describe the committed pair, so
        accepting a changed pair here would make the index and the row disagree.
        """
        if ref not in changed:
            return version
        values = changed[ref]
        if values is None:
            return None
        if values[:ENDPOINT_COLUMN_COUNT] != version.values[:ENDPOINT_COLUMN_COUNT]:
            raise GrafxTransactionStateError(
                f"An update of relationship table {relationship.name!r} may change properties "
                "but not its layout-owned endpoints.",
                field="endpoints",
                table=relationship.name,
                table_id=relationship.table_id,
                operation="relationship_update",
            )
        return replace(version, values=values)

    def by_index(
        record_id: object,
    ) -> Iterator[tuple[object, HeapVersion, TableDef, object]]:
        """Yield the node's edges from the endpoint indexes, validated against the heap."""
        if outgoing:
            context.count("edge_lookups")
            for ref, version in _index_lookup_versions(
                engine,
                manager,
                cast(str, from_name),
                index_key((cast(Value, record_id), None), (0,)),
                snapshot,
                reuse_validated_version=False,
                ended=ended,
            ):
                if ref in ended:
                    continue
                owner = owner_version(ref, version)
                if owner is None:
                    continue
                yield ref, owner, to_table, owner.values[1]
        if incoming:
            context.count("edge_lookups")
            for ref, version in _index_lookup_versions(
                engine,
                manager,
                cast(str, to_name),
                index_key((None, cast(Value, record_id)), (1,)),
                snapshot,
                reuse_validated_version=False,
                ended=ended,
            ):
                if ref in ended:
                    continue
                owner = owner_version(ref, version)
                if owner is None:
                    continue
                yield ref, owner, from_table, owner.values[0]

    maps: list[tuple[dict, dict]] = []

    def grouped() -> tuple[dict, dict]:
        """Build the by-endpoint edge maps ONCE, on the first caller that needs them."""
        if not maps:
            context.count("edge_scans")
            by_source: dict[object, list[tuple[object, HeapVersion]]] = {}
            by_target: dict[object, list[tuple[object, HeapVersion]]] = {}
            for ref, version in engine.heap.scan(relationship, snapshot):
                if ref in ended:
                    continue
                version = owner_version(ref, version)
                if version is None:
                    continue
                if outgoing:
                    by_source.setdefault(version.values[0], []).append((ref, version))
                if incoming:
                    by_target.setdefault(version.values[1], []).append((ref, version))
            for ref, version in pending:
                # Their endpoints may be pending identities themselves, which is exactly the key
                # the lazy owner landing view answers to, so a created edge between two created
                # nodes needs no special case here.
                if outgoing:
                    by_source.setdefault(version.values[0], []).append((ref, version))
                if incoming:
                    by_target.setdefault(version.values[1], []).append((ref, version))
            maps.append((by_source, by_target))
        return maps[0]

    def by_scan(
        record_id: object,
    ) -> Iterator[tuple[object, HeapVersion, TableDef, object]]:
        """Yield the node's edges from one grouped scan of the relationship table."""
        by_source, by_target = grouped()
        if outgoing:
            for ref, version in by_source.get(record_id, ()):
                yield ref, version, to_table, version.values[1]
        if incoming:
            for ref, version in by_target.get(record_id, ()):
                yield ref, version, from_table, version.values[0]

    if not indexed or not bounded_frontier:
        return by_scan

    # Indexed, WITH a fan limit -- and the limit is a cost model, not a hedge. Known scan-shaped
    # frontiers returned above without paying speculative probes. A seek or unknown producer pays
    # a handful of bucket probes and retains the conservative transition to one grouped scan if
    # it grows unexpectedly. The switch is by DISTINCT start nodes seen, so it is deterministic
    # for a given plan and data, and both regimes return the same tuples because the lookup
    # validates against the same snapshot the scan reads under.
    seen_starts: set[object] = set()

    def hybrid(
        record_id: object,
    ) -> Iterator[tuple[object, HeapVersion, TableDef, object]]:
        """Serve by index up to the fan limit, then by the grouped scan for good."""
        if isinstance(record_id, PendingRowRef):
            # A node this transaction created is not in any index, and its private identity is
            # not a stored value, so building a lookup key out of it asks the encoder to store a
            # promise. The scan answers the same question and answers it correctly: a node
            # created here can only be joined by edges created here, which the grouped maps
            # already carry.
            yield from by_scan(record_id)
            return
        if not maps:
            seen_starts.add(record_id)
            if len(seen_starts) <= _EDGE_LOOKUP_FAN_LIMIT:
                yield from by_index(record_id)
                return
        yield from by_scan(record_id)

    return hybrid


def _frontier_is_bounded(root: PlanNode, variable: str) -> bool:
    """Return whether ``root`` obtains ``variable`` without a whole-table node scan.

    Endpoint lookups are profitable when a seek or an already-bound producer supplies a small
    frontier. A NodeScan/AllNodesScan already promises to enumerate the table, so probing the
    endpoint index once per emitted node merely delays the grouped relationship scan that wins
    after the fan limit. Unknown producers deliberately keep the prior hybrid behaviour.
    """
    for planned in root.walk():
        if getattr(planned, "variable", None) != variable:
            continue
        if isinstance(planned, IndexSeek):
            return True
        if isinstance(planned, (NodeScan, AllNodesScan)):
            return False
    return True


def _planned_table_for_variable(root: PlanNode, variable: str) -> TableDef | None:
    """Resolve the node table a planned subtree binds for ``variable``, without reading data."""
    for planned in root.walk():
        if isinstance(planned, (NodeScan, IndexSeek)) and planned.variable == variable:
            return planned.table
        if (
            isinstance(planned, TraverseRelationship)
            and planned.target == variable
            and planned.target_table is not None
        ):
            return planned.target_table
        if isinstance(planned, RelationshipScan):
            if planned.from_variable == variable:
                return planned.from_table
            if planned.to_variable == variable:
                return planned.to_table
    return None


def _traverse(
    engine: QueryEngine, node: TraverseRelationship, context: _Context
) -> Iterator[_Row]:
    """Expand each incoming row along the relationship table, one hop or a bounded range.

    A stored relationship row leads with the two endpoints it connects (W5c): ``_from`` and
    ``_to`` at positions 0 and 1, each a RecordId, ahead of the properties. Traversal is a walk
    over those two columns under the snapshot -- an edge is followed only when the snapshot can
    see the edge AND the node it leads to, so a node deleted after the edge was written is not
    reached through it.

    openCypher's relationship isomorphism holds: no edge is used twice on one path, which is what
    stops a cycle from producing paths without end. Nodes MAY repeat, so ``(a)-[*2]->(a)`` is
    reachable along two distinct edges.

    A single hop binds the relationship variable to the edge; a range binds it to the tuple of
    edges the path took, in order, which is Cypher's list. A target already bound upstream --
    ``MATCH (a), (b) ... (a)-[]->(b)`` -- is a filter: the path counts only when it lands on that
    very row.
    """
    path_variable = node.path_variable
    charge_expansions = engine._max_traversal_expansions is not None
    charge_paths = engine._max_traversal_paths is not None
    if path_variable is not None:
        if type(path_variable) is not str or not path_variable:
            raise GrafxPlanError(
                "A projected path needs one non-empty exact variable name.",
                field="path_variable",
                value=repr(path_variable),
            )
        if (
            node.min_hops != 1
            or node.max_hops != 1
            or node.direction is not Direction.OUTGOING
            or node.relationship is None
        ):
            raise GrafxPlanError(
                "A projected path is exactly one named, typed, outgoing relationship hop.",
                field="path_variable",
                value=path_variable,
            )
    catalog = context.schema()
    relationship = node.table
    dirty_tables = _intent_table_ids(context.txn)
    # The owner reads its own staged work. Everything this transaction has done to the three
    # tables a hop touches -- a committed edge whose properties it replaced, an edge it created,
    # a node it created, updated or ended -- folds into one view here, and a reader outside the
    # transaction never reaches this code at all.
    relationship_changes: Mapping[object, tuple[Value, ...] | None] = {}
    pending_edges: tuple[tuple[object, HeapVersion], ...] = ()
    if relationship.table_id in dirty_tables:
        relationship_changes, pending_edges = _owner_edges(context, relationship)
    from_table = catalog.table(relationship.from_table)
    to_table = catalog.table(relationship.to_table)
    landing_views: dict[int, _OwnerLandingView] = {}

    ended = _ended_by_this_transaction(context)

    def node_at(table: TableDef, identity: object) -> tuple[object, HeapVersion] | None:
        """Return one owner-visible node through a single lazy view per landing table."""
        view = landing_views.get(table.table_id)
        if view is None:
            view = _owner_landing_view(engine, context, table, ended)
            landing_views[table.table_id] = view
        return view.get(identity, context)

    outgoing = node.direction in (Direction.OUTGOING, Direction.UNDIRECTED)
    incoming = node.direction in (Direction.INCOMING, Direction.UNDIRECTED)
    steps = _edge_steps(
        engine,
        context,
        relationship,
        from_table,
        to_table,
        outgoing,
        incoming,
        ended,
        relationship_changes,
        pending_edges,
        bounded_frontier=_frontier_is_bounded(node.child, node.source),
    )

    for row in engine._rows(node.child, context):
        start = row.bindings.get(node.source)
        if not isinstance(start, RowBinding):
            raise GrafxPlanError(
                f"The traversal from {node.source!r} found no bound row to start from.",
                field="variable",
                value=node.source,
            )
        bound_target = row.bindings.get(node.target) if node.target_bound else None
        # (current record id, current table, edges taken so far)
        frontier: list[tuple[object, TableDef, tuple[RowBinding, ...]]] = [
            (_overlay_identity(start), start.table, ())
        ]
        for depth in range(1, node.max_hops + 1):
            reached: list[tuple[object, TableDef, tuple[RowBinding, ...]]] = []
            for record_id, _table, path in frontier:
                taken = {edge.ref for edge in path}
                for ref, version, next_table, next_id in steps(record_id):
                    if charge_expansions:
                        context.admit_traversal_expansion()
                    if ref in taken:
                        continue
                    if (
                        isinstance(bound_target, RowBinding)
                        and _overlay_identity(bound_target) == next_id
                        and bound_target.table.table_id == next_table.table_id
                    ):
                        # The landing IS the bound row, which arrived through operators that
                        # already validated its visibility -- so even the lazy identity proof
                        # node_at would take to re-prove it is not paid.
                        landing = (bound_target.ref, bound_target.version)
                    else:
                        landing = node_at(next_table, next_id)
                    if landing is None:
                        continue
                    if charge_paths:
                        context.admit_traversal_path()
                    edge = RowBinding(
                        variable=node.relationship or "",
                        table=relationship,
                        ref=ref,
                        version=version,
                    )
                    extended = (*path, edge)
                    reached.append((next_id, next_table, extended))
                    if depth < node.min_hops:
                        continue
                    landing_ref, landing_version = landing
                    if isinstance(bound_target, RowBinding) and (
                        _overlay_identity(bound_target) != next_id
                        or bound_target.table.table_id != next_table.table_id
                    ):
                        continue
                    bindings = dict(row.bindings)
                    target_binding = RowBinding(
                        variable=node.target,
                        table=next_table,
                        ref=landing_ref,
                        version=landing_version,
                    )
                    bindings[node.target] = target_binding
                    if node.relationship is not None:
                        bindings[node.relationship] = (
                            edge
                            if node.max_hops == 1 and node.min_hops == 1
                            else extended
                        )
                    if path_variable is not None:
                        if path_variable in bindings:
                            raise GrafxPlanError(
                                "A projected path cannot reuse a node or relationship variable.",
                                field="path_variable",
                                value=path_variable,
                            )
                        bindings[path_variable] = _one_hop_path_value(
                            context, start, edge, target_binding
                        )
                    context.count("rows_scanned")
                    yield _Row(
                        bindings=bindings, computed=row.computed, columns=row.columns
                    )
            frontier = reached
            if not frontier:
                break


def _traverse_any(
    engine: QueryEngine, node: TraverseAnyRelationship, context: _Context
) -> Iterator[_Row]:
    """Expand one untyped hop across every table it could live in, in table order.

    The child is drawn ONCE and each of its rows is expanded across the tables, rather than the
    child being redrawn per table: the rows a caller receives are the same either way, but the
    work and the budget are not, and the freeze asks for one admission point.

    A single hop cannot revisit an edge, so the isomorphism bookkeeping a range needs is absent
    here by construction rather than by omission.
    """
    catalog = context.schema()
    charge_expansions = engine._max_traversal_expansions is not None
    charge_paths = engine._max_traversal_paths is not None
    ended = _ended_by_this_transaction(context)
    dirty_tables = _intent_table_ids(context.txn)
    landing_views: dict[int, _OwnerLandingView] = {}

    def node_at(table: TableDef, identity: object) -> tuple[object, HeapVersion] | None:
        """Return one owner-visible node through a single lazy view per landing table."""
        view = landing_views.get(table.table_id)
        if view is None:
            view = _owner_landing_view(engine, context, table, ended)
            landing_views[table.table_id] = view
        return view.get(identity, context)

    walkers = []
    for table in node.tables:
        changes: Mapping[object, tuple[Value, ...] | None] = {}
        pending: tuple[tuple[object, HeapVersion], ...] = ()
        if table.table_id in dirty_tables:
            changes, pending = _owner_edges(context, table)
        walkers.append(
            (
                table,
                _edge_steps(
                    engine,
                    context,
                    table,
                    catalog.table(table.from_table),
                    catalog.table(table.to_table),
                    True,
                    False,
                    ended,
                    changes,
                    pending,
                    bounded_frontier=_frontier_is_bounded(node.child, node.source),
                ),
            )
        )

    for row in engine._rows(node.child, context):
        start = row.bindings.get(node.source)
        if not isinstance(start, RowBinding):
            raise GrafxPlanError(
                f"The traversal from {node.source!r} found no bound row to start from.",
                field="variable",
                value=node.source,
            )
        identity = _overlay_identity(start)
        for table, steps in walkers:
            for ref, version, next_table, next_id in steps(identity):
                if charge_expansions:
                    context.admit_traversal_expansion()
                landing = node_at(next_table, next_id)
                if landing is None:
                    continue
                if charge_paths:
                    context.admit_traversal_path()
                landing_ref, landing_version = landing
                bindings = dict(row.bindings)
                bindings[node.target] = RowBinding(
                    variable=node.target,
                    table=next_table,
                    ref=landing_ref,
                    version=landing_version,
                )
                bindings[node.relationship] = RowBinding(
                    variable=node.relationship,
                    table=table,
                    ref=ref,
                    version=version,
                )
                context.count("rows_scanned")
                yield _Row(
                    bindings=bindings, computed=row.computed, columns=row.columns
                )


def _relationship_scan(
    engine: QueryEngine, node: RelationshipScan, context: _Context
) -> Iterator[_Row]:
    """Scan one relationship table once and bind both endpoints of each surviving edge.

    ST-1 (b). The rules are the traversal's, applied edge-first: the owner overlay folds this
    transaction's replaced pictures and pending edges in, an edge this transaction ended never
    matches, the r-only predicate is judged exactly as FilterRows judges one (false and unknown
    drop the row, a non-boolean refuses), and an edge counts only when BOTH its endpoints are
    visible under the snapshot -- resolved only for the rows the predicate kept. Every emitted
    row passes the same single admission point every scan uses.
    """
    relationship = node.table
    charge_expansions = engine._max_traversal_expansions is not None
    charge_paths = engine._max_traversal_paths is not None
    dirty_tables = _intent_table_ids(context.txn)
    changed: Mapping[object, tuple[Value, ...] | None] = {}
    pending: tuple[tuple[object, HeapVersion], ...] = ()
    if relationship.table_id in dirty_tables:
        changed, pending = _owner_edges(context, relationship)
    ended = _ended_by_this_transaction(context)
    landing_views: dict[int, _OwnerLandingView] = {}

    def node_at(table: TableDef, identity: object) -> tuple[object, HeapVersion] | None:
        """Resolve one endpoint against the transaction-private landing view."""
        view = landing_views.get(table.table_id)
        if view is None:
            view = _owner_landing_view(engine, context, table, ended)
            landing_views[table.table_id] = view
        return view.get(identity, context)

    def judged(version: HeapVersion, ref: object) -> RowBinding | None:
        """Return the edge binding when the predicate keeps this edge, refusing non-booleans."""
        edge = RowBinding(
            variable=node.relationship or "",
            table=relationship,
            ref=ref,
            version=version,
        )
        if node.predicate is None:
            return edge
        probe = _Row(bindings={node.relationship or "": edge})
        value = _evaluate(node.predicate, probe, context)
        if value is None:
            return None
        if not isinstance(value, bool):
            raise GrafxPlanError(
                "A WHERE predicate is a condition, not a value; "
                f"{node.predicate.describe()} produced {type(value).__name__}.",
                field="predicate",
                value=type(value).__name__,
            )
        return edge if value else None

    single_child = isinstance(node.child, SingleRow)
    for row in engine._rows(node.child, context):
        context.count("edge_scans")
        for ref, version in engine.heap.scan(relationship, context.snapshot):
            if charge_expansions:
                context.admit_traversal_expansion()
            if ref in ended:
                continue
            if ref in changed:
                latest = changed[ref]
                if latest is None:
                    continue
                if (
                    latest[:ENDPOINT_COLUMN_COUNT]
                    != version.values[:ENDPOINT_COLUMN_COUNT]
                ):
                    raise GrafxTransactionStateError(
                        f"An update of relationship table {relationship.name!r} may change "
                        "properties but not its layout-owned endpoints.",
                        field="endpoints",
                        table=relationship.name,
                        table_id=relationship.table_id,
                        operation="relationship_update",
                    )
                version = replace(version, values=latest)
            edge = judged(version, ref)
            if edge is None:
                continue
            landing_from = node_at(node.from_table, version.values[0])
            if landing_from is None:
                continue
            landing_to = node_at(node.to_table, version.values[1])
            if landing_to is None:
                continue
            if charge_paths:
                context.admit_traversal_path()
            bindings = {} if single_child else dict(row.bindings)
            bindings[node.from_variable] = RowBinding(
                variable=node.from_variable,
                table=node.from_table,
                ref=landing_from[0],
                version=landing_from[1],
            )
            bindings[node.to_variable] = RowBinding(
                variable=node.to_variable,
                table=node.to_table,
                ref=landing_to[0],
                version=landing_to[1],
            )
            if node.relationship is not None:
                bindings[node.relationship] = edge
            context.count("rows_scanned")
            yield _Row(bindings=bindings)
        for reference, version in pending:
            if charge_expansions:
                context.admit_traversal_expansion()
            edge = judged(version, reference)
            if edge is None:
                continue
            landing_from = node_at(node.from_table, version.values[0])
            if landing_from is None:
                continue
            landing_to = node_at(node.to_table, version.values[1])
            if landing_to is None:
                continue
            if charge_paths:
                context.admit_traversal_path()
            bindings = {} if single_child else dict(row.bindings)
            bindings[node.from_variable] = RowBinding(
                variable=node.from_variable,
                table=node.from_table,
                ref=landing_from[0],
                version=landing_from[1],
            )
            bindings[node.to_variable] = RowBinding(
                variable=node.to_variable,
                table=node.to_table,
                ref=landing_to[0],
                version=landing_to[1],
            )
            if node.relationship is not None:
                bindings[node.relationship] = edge
            context.count("rows_scanned")
            yield _Row(bindings=bindings)


def _filter_rows(
    engine: QueryEngine, node: FilterRows, context: _Context
) -> Iterator[_Row]:
    """Keep the rows whose predicate is true, dropping false and unknown alike.

    A predicate that is neither a boolean nor null is REFUSED rather than judged. Reading it as
    Python truthiness would keep every non-empty string and every non-zero number, which is a
    different query from the one the caller wrote; dropping the row silently would answer a
    mistyped predicate with an empty result and no reason. Null is different and is not an
    error: it is the unknown of the three-valued logic, and the row goes because unknown is not
    true -- which is also why this cannot be written as ``if value``.
    """
    for row in engine._rows(node.child, context):
        value = _evaluate(node.predicate, row, context)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise GrafxPlanError(
                "A WHERE predicate is a condition, not a value; "
                f"{node.predicate.describe()} produced {type(value).__name__}.",
                field="predicate",
                value=type(value).__name__,
            )
        if value:
            yield row


def _vector_search(
    engine: QueryEngine, node: VectorSearch, context: _Context
) -> Iterator[_Row]:
    """Score the candidate set the child produced, in one pass (SPEC-VEC FR-4, BR-6).

    The candidates are materialised because a filter must know its own cardinality: that number
    is what the two-regime planner of the vector subsystem chooses its regime from, and a filter
    that could not count itself would be treated as the whole space and would push every query
    into the approximate regime.
    """
    table = _planned_table_for_variable(node.child, node.variable)
    if table is not None and table.table_id in _intent_table_ids(context.txn):
        raise GrafxUnsupportedOperation(
            f"A similarity search over {table.name!r} cannot include rows this transaction has "
            "staged: its vector index describes only committed rows. Commit or roll back first; "
            "vector read-your-own-writes is not implemented in this build.",
            field="table",
            value=table.name,
            table_id=table.table_id,
            operation="similarity",
        )
    vectors = engine.require_vectors()
    candidates = tuple(engine._rows(node.child, context))
    by_record: dict[int, list[_Row]] = {}
    for row in candidates:
        binding = row.bindings.get(node.variable)
        if not isinstance(binding, RowBinding):
            raise GrafxPlanError(
                f"The similarity search reads {node.variable}, which the rows reaching it do "
                "not carry.",
                field="variable",
                value=node.variable,
            )
        by_record.setdefault(binding.record_id, []).append(row)
    if not by_record:
        return
    space = _space_name(node, candidates, context)
    query_vector = _query_vector(node, candidates, context)
    wanted = _neighbour_count(node, candidates, context, len(by_record))
    result = vectors.search(  # type: ignore[attr-defined]
        space=space,
        query=query_vector,
        k=wanted,
        snapshot=context.snapshot,
        candidate_filter=RecordIdFilter(frozenset(by_record)),
    )
    context.statistics["vector_regime_exact"] = context.statistics.get(
        "vector_regime_exact", 0
    ) + (1 if result.regime == "exact" else 0)
    context.count("vector_hits", len(result.hits))
    if by_record and not result.hits:
        # Silence here is the wrong answer to give a caller: the filter admitted rows and the
        # search came back with nothing, which is either a genuinely empty neighbourhood or an
        # index that does not hold the vectors the heap does. The statement still returns no
        # rows -- inventing some would be far worse -- but it says so, so the difference is
        # visible from outside instead of having to be isolated by bisection.
        context.count("vector_empty_over_candidates")
        context.statistics["vector_candidates_offered"] = len(by_record)
    threshold = _threshold_value(node, candidates, context)
    for hit in result.hits:
        if threshold is not None and not _passes(
            node.threshold_operator, hit.score, threshold
        ):
            continue
        for row in by_record.get(hit.record_id, ()):
            bindings = dict(row.bindings)
            bindings[node.score_column] = hit.score
            yield _Row(bindings=bindings)


def _space_name(node: VectorSearch, rows: Sequence[_Row], context: _Context) -> str:
    """Return the embedding space this search names, refusing one the column does not belong to.

    The comparison happens here as well as in the planner because a space named by a parameter is
    unknown until the value is bound. It is the same rule at the moment the name becomes known,
    which is what SPEC-VEC BR-1 asks for: refused before any distance is computed, never a
    ranking across two spaces.
    """
    probe = rows[0] if rows else _Row(bindings={})
    name = _evaluate(node.space, probe, context)
    if not isinstance(name, str):
        raise GrafxPlanError(
            f"An embedding space is named by text; got {type(name).__name__}.",
            field="space",
            value=type(name).__name__,
        )
    if node.column_space and name != node.column_space:
        raise GrafxEmbeddingSpaceMismatch(
            f"The column {node.variable}.{node.property_key} stores vectors of the embedding "
            f"space {node.column_space!r}, and this query searches {name!r}; comparing vectors "
            "across spaces is refused rather than ranked.",
            field="space",
            value=name,
            column_space=node.column_space,
        )
    return name


def _query_vector(
    node: VectorSearch, rows: Sequence[_Row], context: _Context
) -> tuple[float, ...]:
    """Return the reference vector of this search, refusing anything that is not one."""
    probe = rows[0] if rows else _Row(bindings={})
    value = _evaluate(node.query_vector, probe, context)
    components = getattr(value, "values", value)
    if isinstance(components, (str, bytes, bytearray)) or not isinstance(
        components, Sequence
    ):
        raise GrafxPlanError(
            "A similarity search compares against a sequence of numbers; got "
            f"{type(value).__name__}.",
            field="query",
            value=type(value).__name__,
        )
    numbers: list[float] = []
    for component in components:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise GrafxPlanError(
                f"A reference vector holds numbers; got {type(component).__name__}.",
                field="query",
                value=type(component).__name__,
            )
        numbers.append(float(component))
    return tuple(numbers)


def _neighbour_count(
    node: VectorSearch, rows: Sequence[_Row], context: _Context, candidates: int
) -> int:
    """Return how many neighbours to ask for: the fused bound, or every candidate."""
    if node.k is None:
        return max(candidates, 1)
    probe = rows[0] if rows else _Row(bindings={})
    wanted = _evaluate(node.k, probe, context)
    if isinstance(wanted, bool) or not isinstance(wanted, int):
        raise GrafxPlanError(
            f"A neighbour count is a whole number; got {type(wanted).__name__}.",
            field="k",
            value=type(wanted).__name__,
        )
    if wanted < 0:
        raise GrafxPlanError(
            f"A neighbour count is zero or more; got {wanted}.", field="k", value=wanted
        )
    return max(min(wanted, candidates), 1)


def _threshold_value(
    node: VectorSearch, rows: Sequence[_Row], context: _Context
) -> float | None:
    """Return the score floor of this search, or None when it declared none."""
    if node.threshold is None or node.threshold_operator is None:
        return None
    probe = rows[0] if rows else _Row(bindings={})
    value = _evaluate(node.threshold, probe, context)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GrafxPlanError(
            f"A similarity threshold is a number; got {type(value).__name__}.",
            field="threshold",
            value=type(value).__name__,
        )
    return float(value)


def _passes(operator: str | None, score: float, threshold: float) -> bool:
    """Return whether one score clears the floor the query set."""
    if operator == ">":
        return score > threshold
    if operator == ">=":
        return score >= threshold
    return True


def _aggregate_rows(
    engine: QueryEngine, node: AggregateRows, context: _Context
) -> Iterator[_Row]:
    """Produce one row per group, carrying the grouping keys and the aggregates."""
    groups: dict[object, tuple[list[object], dict[Expression, _Accumulator]]] = {}
    order: list[object] = []
    for row in engine._rows(node.child, context):
        keys = [_evaluate(item.expression, row, context) for item in node.grouping]
        signature = tuple(_freeze(key) for key in keys)
        state = groups.get(signature)
        if state is None:
            state = (list(keys), {})
            groups[signature] = state
            order.append(signature)
        for aggregation in node.aggregations:
            accumulator = state[1].get(aggregation.call)
            if accumulator is None:
                accumulator = _Accumulator(aggregation)
                state[1][aggregation.call] = accumulator
            accumulator.add(row, context)
    if not groups and not node.grouping:
        # An aggregate over no rows still answers: count is zero and the rest are null. Without
        # this a "how many are there" query over an empty table would return no row at all.
        computed: dict[Expression, object] = {
            aggregation.call: _Accumulator(aggregation).result()
            for aggregation in node.aggregations
        }
        yield _Row(bindings={}, computed=computed)
        return
    for signature in order:
        keys, accumulators = groups[signature]
        computed = {item.expression: value for item, value in zip(node.grouping, keys)}
        for aggregation in node.aggregations:
            accumulator = accumulators.get(aggregation.call)
            computed[aggregation.call] = (
                accumulator.result() if accumulator is not None else None
            )
        yield _Row(bindings={}, computed=computed)


class _Accumulator:
    """The running state of one aggregate over one group."""

    __slots__ = (
        "_aggregation",
        "_function",
        "_seen",
        "_values",
        "_count",
        "_total",
        "_extreme",
        "_extreme_key",
    )

    def __init__(self, aggregation: Aggregation) -> None:
        self._aggregation = aggregation
        self._function = aggregation.function
        self._seen: set[object] | None = set() if aggregation.call.distinct else None
        self._values: list[object] | None = (
            [] if self._function == "COLLECT" else None
        )
        self._count = 0
        self._total: float = 0.0
        self._extreme: object = None
        self._extreme_key: tuple[int, object] | None = None

    def add(self, row: _Row, context: _Context) -> None:
        """Fold one row into this aggregate."""
        call = self._aggregation.call
        if call.star:
            self._count += 1
            return
        value = _evaluate(call.arguments[0], row, context)
        if value is None:
            return
        if call.distinct:
            frozen = _freeze(value)
            seen = self._seen
            assert seen is not None
            if frozen in seen:
                return
            seen.add(frozen)
        self._count += 1
        name = self._function
        if name == "COLLECT":
            values = self._values
            assert values is not None
            values.append(value)
        elif name in ("MIN", "MAX"):
            key = _sort_key(value)
            extreme_key = self._extreme_key
            if extreme_key is None or (
                key < extreme_key if name == "MIN" else not key < extreme_key
            ):
                self._extreme = value
                self._extreme_key = key
        elif (
            name in ("SUM", "AVG")
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            self._total += float(value)

    def result(self) -> object:
        """Return what this aggregate reports for its group."""
        name = self._function
        if name == "COUNT":
            return self._count
        if name == "COLLECT":
            values = self._values
            assert values is not None
            return tuple(values)
        if not self._count:
            return None
        if name == "SUM":
            return self._total
        if name == "AVG":
            return self._total / self._count
        return self._extreme


def _project_rows(
    engine: QueryEngine, node: ProjectRows, context: _Context
) -> Iterator[_Row]:
    """Compute the projected columns of each row, keeping what it was projected from."""
    for row in engine._rows(node.child, context):
        columns = {
            item.name: _evaluate(item.expression, row, context) for item in node.items
        }
        yield _Row(bindings=row.bindings, computed=row.computed, columns=columns)


def _eager_rows(
    engine: QueryEngine, node: EagerRows, context: _Context
) -> Iterator[_Row]:
    """Draw every row of the child before yielding the first, so nothing above can stop it."""
    yield from tuple(engine._rows(node.child, context))


def _optional_rows(
    engine: QueryEngine, node: OptionalRows, context: _Context
) -> Iterator[_Row]:
    """Yield the child's rows, or one row binding the name to null when there were none.

    The flag is set on the first row rather than counted, because the operator streams: a query
    with a LIMIT above it must not have to draw the whole match to learn that the match was not
    empty.
    """
    matched = False
    for row in engine._rows(node.child, context):
        matched = True
        yield row
    if not matched:
        yield _Row(bindings={node.alias: None})


def _union_rows(
    engine: QueryEngine, node: UnionRows, context: _Context
) -> Iterator[_Row]:
    """Yield the left branch's rows and then the right branch's, under one set of names.

    Position, not name, is what joins the two: the right branch may have written different
    aliases, and its projection produced them in the order it wrote them. Each branch ends in a
    projection of exactly this arity, so the values of a row arrive in column order and the
    mapping is exact.

    Widening happens HERE, before the distinct above can look at anything, because 1 and 1.0
    are the same row of a widened column and two different rows of an unwidened one. Doing it
    after the distinct would answer both.
    """
    coercions = context.union_coercions
    for child in (node.left, node.right):
        for row in engine._rows(child, context):
            values = tuple((row.columns or {}).values())
            columns = {
                name: (
                    float(value)
                    if position < len(coercions)
                    and coercions[position]
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    else value
                )
                for position, (name, value) in enumerate(zip(node.columns, values))
            }
            yield _Row(bindings={}, computed=None, columns=columns)


def _distinct_rows(
    engine: QueryEngine, node: DistinctRows, context: _Context
) -> Iterator[_Row]:
    """Keep the first row of each distinct projection, in the order they arrived."""
    seen: set[object] = set()
    for row in engine._rows(node.child, context):
        columns = row.columns if row.columns is not None else {}
        signature = tuple(
            (name, _freeze(value)) for name, value in sorted(columns.items())
        )
        if signature in seen:
            continue
        seen.add(signature)
        yield row


def _sort_rows(
    engine: QueryEngine, node: SortRows, context: _Context
) -> Iterator[_Row]:
    """Order the rows by the keys the query named, most significant key last.

    Sorting one key at a time from the least significant is what lets each key carry its own
    direction while the sort stays stable, and a stable sort over a deterministic input is what
    makes the same query answer in the same order on every run.  A LIMIT lets the planner attach
    a physical retention bound.  That path consumes the same complete child stream but retains
    only SKIP + LIMIT candidates in a worst-first heap, reducing memory to O(K) and comparisons
    to O(N log K); the full stable sort remains the unbounded path.
    """
    if node.retained_limit is not None:
        yield from _top_rows(engine, node, node.retained_limit, context)
        return
    rows = list(engine._rows(node.child, context))
    for key in reversed(node.keys):
        rows.sort(
            key=lambda row, key=key: _sort_key(_sort_value(key, row, context)),
            reverse=key.descending,
        )
    yield from rows


@dataclass(slots=True)
class _TopCandidate:
    """One retained row whose heap order is the reverse of its query order.

    Python's heap exposes its smallest member.  Reversing ``__lt__`` therefore places the worst
    retained candidate at the root, where a better incoming row can replace it in O(log K).
    ``position`` is the final key and preserves the stable-sort promise for equal multi-key
    values, including a mix of ascending and descending keys.
    """

    keys: tuple[tuple[tuple[int, object], bool], ...]
    position: int
    row: _Row

    def precedes(self, other: _TopCandidate) -> bool:
        """Return whether this row appears before ``other`` in the requested stable order."""
        for (left, descending), (right, _other_descending) in zip(
            self.keys, other.keys
        ):
            if left == right:
                continue
            if left < right:
                return not descending
            if right < left:
                return descending
            # A shared ORDER BY key is expected to be total.  Treat an unordered pair as a tie
            # nonetheless, so a malformed/collaborator value cannot make heap order unstable.
        return self.position < other.position

    def __lt__(self, other: _TopCandidate) -> bool:
        """Put the later query row first in the min-heap."""
        return other.precedes(self)


def _top_heap_push(heap: list[_TopCandidate], candidate: _TopCandidate) -> None:
    """Push one candidate while preserving the local worst-first binary heap."""
    position = len(heap)
    heap.append(candidate)
    while position:
        parent = (position - 1) // 2
        incumbent = heap[parent]
        if not candidate < incumbent:
            break
        heap[position] = incumbent
        position = parent
    heap[position] = candidate


def _top_heap_replace(heap: list[_TopCandidate], candidate: _TopCandidate) -> None:
    """Replace the worst candidate and restore the binary heap in O(log K)."""
    heap[0] = candidate
    _top_heap_sift_down(heap, 0)


def _top_heap_pop(heap: list[_TopCandidate]) -> _TopCandidate:
    """Remove and return the worst candidate in O(log K)."""
    tail = heap.pop()
    if not heap:
        return tail
    result = heap[0]
    heap[0] = tail
    _top_heap_sift_down(heap, 0)
    return result


def _top_heap_sift_down(heap: list[_TopCandidate], position: int) -> None:
    """Move one rootward candidate down to its binary-heap position."""
    length = len(heap)
    candidate = heap[position]
    while True:
        left = position * 2 + 1
        if left >= length:
            break
        right = left + 1
        child = right if right < length and heap[right] < heap[left] else left
        if not heap[child] < candidate:
            break
        heap[position] = heap[child]
        position = child
    heap[position] = candidate


def _top_rows(
    engine: QueryEngine,
    node: SortRows,
    retained_limit: Expression,
    context: _Context,
) -> Iterator[_Row]:
    """Order a bounded result while retaining at most SKIP + LIMIT child rows."""
    limit = _window(retained_limit, context, "LIMIT")
    skipped = (
        _window(node.retained_skip, context, "SKIP")
        if node.retained_skip is not None
        else 0
    )
    retained = skipped + limit
    heap: list[_TopCandidate] = []
    for position, row in enumerate(engine._rows(node.child, context)):
        keys = tuple(
            (_sort_key(_sort_value(key, row, context)), key.descending)
            for key in node.keys
        )
        if retained == 0:
            # Consuming the child is observable for write pipelines and query budgets even when
            # LIMIT 0 means no row can survive this physical operator.  Evaluating the keys too
            # preserves ORDER BY refusals rather than turning LIMIT 0 into an expression bypass.
            continue
        candidate = _TopCandidate(
            keys=keys,
            position=position,
            row=row,
        )
        if len(heap) < retained:
            _top_heap_push(heap, candidate)
        elif candidate.precedes(heap[0]):
            _top_heap_replace(heap, candidate)

    # The heap yields worst first under its reversed comparator. Popping all K entries and
    # reversing them restores exact query order in O(K log K), within the O(N log K) bound.
    worst_first = [_top_heap_pop(heap).row for _ in range(len(heap))]
    yield from reversed(worst_first)


def _sort_value(key: SortItem, row: _Row, context: _Context) -> object:
    """Return the value one ORDER BY key reads from a row."""
    expression = key.expression
    columns = row.columns
    if (
        isinstance(expression, Variable)
        and columns is not None
        and expression.name in columns
    ):
        # An alias names a column of the projection this sort reads, and reading it from there
        # is not an optimisation: after DISTINCT or a group the row no longer carries what the
        # expression was computed from, so re-evaluating it would refuse.
        return columns[expression.name]
    return _evaluate(expression, row, context)


def _skip_rows(
    engine: QueryEngine, node: SkipRows, context: _Context
) -> Iterator[_Row]:
    """Drop the first rows the query asked to skip."""
    rows = engine._rows(node.child, context)
    dropped = 0
    wanted = _window(node.count, context, "SKIP")
    for row in rows:
        if dropped < wanted:
            dropped += 1
            continue
        yield row


def _limit_rows(
    engine: QueryEngine, node: LimitRows, context: _Context
) -> Iterator[_Row]:
    """Produce at most the number of rows the query asked for."""
    wanted = _window(node.count, context, "LIMIT")
    produced = 0
    for row in engine._rows(node.child, context):
        if produced >= wanted:
            return
        produced += 1
        yield row


def _window(expression: Expression, context: _Context, keyword: str) -> int:
    """Return a row window as a count, refusing anything that is not a whole number of rows."""
    value = _evaluate(expression, _Row(bindings={}), context)
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxPlanError(
            f"{keyword} takes a whole number of rows; got {type(value).__name__}.",
            field=keyword.lower(),
            value=type(value).__name__,
        )
    if value < 0:
        raise GrafxPlanError(
            f"{keyword} takes a count of rows that is zero or more; got {value}.",
            field=keyword.lower(),
            value=value,
        )
    return value


def _write_rows(
    engine: QueryEngine, node: PlanNode, context: _Context
) -> Iterator[_Row]:
    """Build every row a write operator would store, then refuse for the seam that is missing.

    The ORDER is the whole point and it is the shape the finished write path will keep. Every
    refusal a write can produce -- a property the table does not declare, a null in a column that
    forbids one, a value of the wrong type, a vector of the wrong dimension or one that names a
    retired space -- happens HERE, against rows that exist only in memory, before anything is
    handed to a transaction. Nothing becomes reachable in the heap, in an index or in a counter
    until every step that can still refuse has succeeded, which is the constraint C1 and C4 were
    each rejected for missing.

    When the two seams this refusal names arrive, the ``raise`` at the end is replaced by the
    staging call and NOTHING ELSE MOVES: the materialised rows are already validated, so the
    handover cannot be the step that discovers a problem.
    """
    for row in engine._rows(node.child, context):
        yield _write_one(engine, node, row, context)


def _write_one(
    engine: QueryEngine, node: PlanNode, row: _Row, context: _Context
) -> _Row:
    """Materialise everything one write operator stores for one row, then stage it.

    Two phases, in this order and never interleaved. Everything that can still refuse -- a
    property the table does not declare, a null in a column that forbids one, a value of the
    wrong type, a vector of the wrong dimension, an edge naming a row this snapshot cannot see --
    happens against values held in memory. Only when the whole set is built does anything reach
    the transaction. So a statement that refuses has staged nothing at all, rather than half of
    itself.
    """
    if isinstance(node, (CreateRelationships, MergePattern)):
        return _write_pattern(engine, node, row, context)
    if isinstance(node, SetProperties):
        return _write_assignments(engine, node, row, context)
    if isinstance(node, DeleteEntities):
        return _write_deletions(engine, node, row, context)
    raise GrafxUnsupportedOperation(
        f"The operator {node.label} cannot write in this build.",
        field="operator",
        value=node.label,
    )


def _write_assignments(
    engine: QueryEngine, node: SetProperties, row: _Row, context: _Context
) -> _Row:
    """Build the new version every SET assignment of one row produces, then hold it.

    Every assignment is checked and applied to a COPY first. Two reasons, and the second is the
    one that is easy to miss: a SET whose second assignment refuses must leave nothing behind,
    and two assignments to the same row must both land -- writing one version per assignment
    would make the last one win and silently drop the others.

    The row that leaves here still carries the binding it arrived with, so a RETURN above a SET
    projects the values the statement matched. That is openCypher's rule: the projection reads
    the row as the statement saw it, and the new version becomes visible at the commit number.
    """
    # Keyed by the stored ROW, not by the variable: `MATCH (n {id:1}), (m {id:1}) SET n.a = 1,
    # m.b = 2` names one row twice, and one version per variable made the last one win (C10
    # round-2 B4). The base is this transaction's latest staged version of the row, else the
    # snapshot's, so a second SET clause -- or a second statement -- builds on the first.
    updated: dict[object, tuple[RowBinding, list[Value]]] = {}
    for assignment in node.assignments:
        binding, position, value = _prepare_assignment(engine, assignment, row, context)
        row_key = (
            binding.ref if binding.ref is not None else ("pending", id(binding.version))
        )
        held = updated.get(row_key)
        if held is None:
            held = (binding, list(_current_values(context, binding)))
            updated[row_key] = held
        held[1][position] = value
    for binding, values in updated.values():
        settled = tuple(values)
        if binding.ref is None:
            # A row THIS statement created (MERGE ... SET, CREATE ... SET): there is no stored
            # version to replace, so the held insert itself is rewritten. Checking the new key
            # against the statement's own held copy of the row would refuse the row for being
            # itself, which is what happened (C10 round-2 B5, the MERGE-then-SET shape).
            _rewrite_held_insert(engine, context, binding, settled)
            context.count("properties_set", len(node.assignments))
            context.count("rows_updated")
            continue
        _require_unique_primary_key(
            engine, binding.table, settled, context, replacing=binding.ref
        )
        previous_keys = [_partition_key(binding.table, binding.version.values)]
        current = _current_values(context, binding)
        if current != binding.version.values:
            previous_keys.append(_partition_key(binding.table, current))
        context.hold_update(
            binding.table,
            binding.ref,
            settled,
            _partition_key(binding.table, settled),
            previous_keys=previous_keys,
        )
        context.count("properties_set", len(node.assignments))
        context.count("rows_updated")
    return row


def _rewrite_held_insert(
    engine: QueryEngine,
    context: _Context,
    binding: RowBinding,
    settled: tuple[Value, ...],
) -> None:
    """Replace the values of a row this statement holds as an insert, keeping it an insert.

    The held copy is taken out before the key check runs, so the check compares the new values
    against everything EXCEPT the row being rewritten, and is put back rewritten afterwards. The
    partition of the new key is declared beside the old one, for the same reason an update
    declares both.
    """
    position = _held_insert_position(context, binding)
    if position is not None:
        held = context.staged_rows[position]
        del context.staged_rows[position]
        try:
            _require_unique_primary_key(engine, binding.table, settled, context)
        except GrafxError:
            context.staged_rows.insert(position, held)
            raise
        context.staged_rows.insert(
            position,
            _HeldRow(
                _HELD_INSERT, held.table, settled, held.identity, None, held.token
            ),
        )
        new_key = _partition_key(binding.table, settled)
        if (binding.table.table_id, new_key) not in context.staged_partitions:
            context.staged_partitions.append((binding.table.table_id, new_key))
        return
    raise GrafxPlanError(
        f"SET names {binding.variable!r}, a row this statement created, but the statement no "
        f"longer holds it.",
        field="variable",
        value=binding.variable,
    )


def _held_insert_position(context: _Context, binding: RowBinding) -> int | None:
    """Return where this statement holds the insert a pending binding stands for, if it does.

    By TOKEN, never by values: two created rows may carry identical values (a table without a
    primary key), and the binding's version object is the one thing that names exactly one of
    them. A pending binding whose row was created by an EARLIER statement has no token here --
    that row is already on the transaction -- and the answer is None.
    """
    token = context.pending_tokens.get(id(binding.version))
    if token is None:
        return None
    for position, held in enumerate(context.staged_rows):
        if held.operation is _HELD_INSERT and held.token == token:
            return position
    return None


def _incident_edges(
    engine: QueryEngine, context: _Context, table: TableDef, endpoint_identity: object
) -> Iterator[tuple[TableDef, object, tuple[Value, ...]]]:
    """Yield every live relationship touching one node, in either direction.

    A stored relationship leads with its two endpoints (W5c), so ``values[0]`` is the source and
    ``values[1]`` the target. The match is POSITIONAL, and it has to be: record numbers are per
    table, so ``FROM Person TO Company`` can hold an edge whose Company is number 1 while the
    node being deleted is Person number 1. Asking only whether the number appears somewhere in
    the pair would end that edge, which belongs to a different node entirely.

    A self-loop is named on both sides of a table pointing at itself and is still yielded ONCE,
    because one visible version of one record is one row: ending it twice would write a second
    end at a number the version had already stopped at. Deduplicating here would be defending
    against a snapshot showing two live versions of the same record, which is corruption to
    report rather than a shape to absorb.
    """
    snapshot = context.snapshot
    catalog = context.schema()
    candidates: list[tuple[TableDef, bool, bool]] = []
    for candidate in catalog.tables():
        if candidate.kind != "rel":
            continue
        leaves = str(candidate.from_table) == table.name
        lands = str(candidate.to_table) == table.name
        if not (leaves or lands):
            continue
        candidates.append((candidate, leaves, lands))

        # An edge THIS statement is still holding has no logical identity the transaction can
        # be told to end: it is not on `row_intents` yet, so ending it would have to reach into
        # the statement's own held work while that work is still being built. Refuse before
        # anything is yielded, and therefore before any physical edge is held.
        for held in context.staged_rows:
            if held.operation is not _HELD_INSERT:
                continue
            if held.table.table_id != candidate.table_id:
                continue
            values = held.values or ()
            if len(values) < ENDPOINT_COLUMN_COUNT:
                continue
            if (leaves and values[0] == endpoint_identity) or (
                lands and values[1] == endpoint_identity
            ):
                raise GrafxUnsupportedOperation(
                    f"DETACH DELETE cannot include a {candidate.name!r} relationship this same "
                    "statement is creating. Create the relationship in an earlier statement, or "
                    "detach the node in one of its own.",
                    field="table",
                    value=candidate.name,
                    table_id=candidate.table_id,
                    operation="detach_delete",
                )

    for candidate, leaves, lands in candidates:
        # The edges earlier statements created come first, and ending one CANCELS it: the
        # transaction's own reducer folds an insert followed by a delete into no row at all, so
        # the node and everything hanging from it leave together with nothing to publish.
        _changed, inserted = _transaction_row_view(
            context, candidate, include_held=False
        )
        for reference, values in inserted:
            if not isinstance(reference, PendingRowRef):
                continue
            if not (
                (leaves and values[0] == endpoint_identity)
                or (lands and values[1] == endpoint_identity)
            ):
                continue
            yield (
                candidate,
                reference,
                tuple(values[:ENDPOINT_COLUMN_COUNT]),
            )
        for ref, endpoints in engine.heap.scan_relationship_endpoints(candidate, snapshot):
            incident = (leaves and endpoints[0] == endpoint_identity) or (
                lands and endpoints[1] == endpoint_identity
            )
            if not incident:
                continue
            yield candidate, ref, endpoints


def _write_deletions(
    engine: QueryEngine, node: DeleteEntities, row: _Row, context: _Context
) -> _Row:
    """End every row one DELETE names, under the same hold-until-complete discipline.

    A row named twice is ended once, whether the repeat comes from one statement naming it twice,
    from a Cartesian handing the same row to this write again, or from an earlier statement of
    the same transaction having already ended it. The second stamp would be an end written at a
    number the version had already stopped at, which is not a stronger statement of the same fact
    but a second fact that is not true.

    DETACH is what decides the fate of the relationships hanging from a node. With the keyword
    they end WITH it, physically, in the same statement. Without it only the node ends, and its
    edges stay on the pages as rows no traversal will follow: a landing whose snapshot cannot see
    the node is not reached, so the absence an ordinary DELETE promises is a logical one. Ending
    those edges anyway without being asked would turn a tombstone into a cascade.
    """
    for variable in node.variables:
        binding = row.bindings.get(variable)
        if not isinstance(binding, RowBinding):
            raise GrafxPlanError(
                f"DELETE names {variable!r}, which the rows reaching it do not carry.",
                field="variable",
                value=variable,
            )
        if binding.ref is None:
            # A row THIS statement created: deleting it is creating nothing. The held insert is
            # taken back. A refusal here would have been the honest alternative; what must not
            # happen is what did: the delete reached the transaction's door with no reference,
            # was refused THERE, after the statement's other rows were already staged -- and the
            # caller's commit made the rest of the statement durable (C10 round-3 B1).
            position = _held_insert_position(context, binding)
            if position is None:
                token = context.pending_tokens.get(id(binding.version))
                if token is not None and token in context.cancelled_insert_tokens:
                    continue
                raise GrafxUnsupportedOperation(
                    f"DELETE names {variable!r}, a row created by an earlier statement of this "
                    f"transaction; a row's identity is allocated by the commit (W5b), so it "
                    f"cannot be ended before then. Commit first, then delete it.",
                    field="variable",
                    value=variable,
                )
            token = context.pending_tokens.get(id(binding.version))
            if token is not None:
                context.cancelled_insert_tokens.add(token)
            del context.staged_rows[position]
            context.count("rows_deleted")
            continue
        if context.already_ended(binding.ref):
            continue
        context.note_ended(binding.ref)
        if node.detach and binding.table.kind != "rel":
            # Incident edges are settled BEFORE the node they hang from is held, so the whole
            # detach is one statement's worth of staged work: either every edge and the node
            # end together at the commit stamp, or a refusal leaves all of them standing.
            endpoint_identity = (
                binding.ref
                if isinstance(binding.ref, PendingRowRef)
                else binding.record_id
            )
            for edge_table, edge_ref, edge_endpoints in _incident_edges(
                engine, context, binding.table, endpoint_identity
            ):
                if context.already_ended(edge_ref):
                    continue
                context.note_ended(edge_ref)
                context.hold_delete(
                    edge_table,
                    edge_ref,
                    _partition_key(edge_table, edge_endpoints),
                )
                context.count("rows_deleted")
        context.hold_delete(
            binding.table,
            binding.ref,
            _partition_key(binding.table, binding.version.values),
        )
        context.count("rows_deleted")
    return row


def _write_pattern(
    engine: QueryEngine,
    node: CreateRelationships | MergePattern,
    row: _Row,
    context: _Context,
) -> _Row:
    """Materialise and stage the nodes and edges one written pattern names."""
    merging = isinstance(node, MergePattern)
    bindings = dict(row.bindings)
    staged: list[tuple[TableDef, tuple[Value, ...], int | None, int]] = []
    fresh: set[str] = set()
    for written in node.nodes:
        if written.variable is None:
            continue
        if written.table is None:
            continue
        values = materialise_row(
            engine, written.table, written.properties, row, context
        )
        if merging:
            existing = _matching_row(engine, written, values, context)
            if existing is not None:
                bindings[written.variable] = existing
                context.count("rows_matched")
                continue
        _require_unique_primary_key(
            engine, written.table, values, context, also=[held[1] for held in staged]
        )
        pending = _pending_binding(written.variable, written.table, values)
        staged.append((written.table, values, None, context.token_for(pending)))
        fresh.add(written.variable)
        bindings[written.variable] = pending
    edges: list[tuple[TableDef, tuple[Value, ...]]] = []
    for edge in node.relationships:
        materialised = _materialise_edge(engine, edge, bindings, fresh, row, context)
        if merging and _matching_edge(engine, edge, materialised, context):
            context.count("relationships_matched")
            continue
        edges.append(materialised)
    _require_write_transaction(context.txn)
    for table, values, identity, token in staged:
        context.hold(
            table, values, _partition_key(table, values), identity, token=token
        )
        context.count("rows_created")
    for table, values in edges:
        context.hold(table, values, _partition_key(table, values), None)
        context.count("relationships_created")
    return _Row(bindings=bindings, computed=row.computed, columns=row.columns)


def _endpoint_txn_memo(
    engine: QueryEngine, context: _Context
) -> _EndpointTxnMemo | None:
    """Return the memo owned by this exact transaction, snapshot and catalog picture."""
    txn_id = getattr(context.txn, "txn_id", None)
    if isinstance(txn_id, bool) or not isinstance(txn_id, int):
        return None
    snapshot = context.snapshot
    # Catalog refresh can read pages.  Establish the picture before entering the endpoint-only
    # registry guard; that guard never owns a heap or buffer-pool operation.
    schema = context.schema()
    closers: tuple[_EndpointIdentityLocator, ...] = ()
    try:
        with engine._endpoint_guard:
            memo = engine._endpoint_memo.get(txn_id)
            if memo is not None and (
                memo.txn is not context.txn
                or memo.snapshot is not snapshot
                or memo.schema is not schema
            ):
                engine._endpoint_memo.pop(txn_id, None)
                closers = memo.retire()
                memo = None
            if memo is None:
                try:
                    candidate = _EndpointTxnMemo(
                        txn=context.txn,
                        snapshot=snapshot,
                        schema=schema,
                        budget=engine._endpoint_budget,
                    )
                except _EndpointLocatorCapacity:
                    candidate = None
                if candidate is not None:
                    try:
                        engine._endpoint_memo[txn_id] = candidate
                    except BaseException:
                        candidate.retire()
                        raise
                    memo = candidate
    finally:
        for locator in closers:
            locator.close()
    return memo


def _canonical_identity_with_ref(
    engine: QueryEngine,
    context: _Context,
    table: TableDef,
    record_id: RecordId,
) -> tuple[RecordRef, HeapVersion] | None:
    """Use the unchanged canonical heap order for a non-accelerated identity proof."""
    return engine.heap._lookup_with_ref(table, record_id, context.snapshot)


def _visible_identity_with_ref(
    engine: QueryEngine,
    context: _Context,
    table: TableDef,
    record_id: RecordId,
) -> tuple[RecordRef, HeapVersion] | None:
    """Resolve one identity under this snapshot through a bounded, reusable prefix locator.

    This is intentionally independent of CREATE relationship.  It returns the generic
    ``RecordRef + HeapVersion`` pair P1.5 will need, while the transaction-owned lifecycle and
    memory policy remain in the executor.  Capacity and stale derived state are the only two
    reasons to fall back.  Stored-data mismatches and corruption propagate unchanged.
    """
    memo = _endpoint_txn_memo(engine, context)
    if memo is None:
        return _canonical_identity_with_ref(engine, context, table, record_id)
    build = False
    close_before_fallback: _EndpointIdentityLocator | None = None
    with engine._endpoint_guard:
        state, slot = memo.acquire(table.table_id)
        if state == "missing":
            try:
                slot = memo.claim(table.table_id)
            except (_EndpointLocatorCapacity, _EndpointLocatorStale):
                slot = None
            else:
                build = True
        elif state == "ready":
            assert slot is not None
            locator = slot.locator
            assert locator is not None
            if (
                locator.table is not table
                or locator.epoch != engine.heap._derived_read_epoch()
            ):
                close_before_fallback = memo.finish(
                    table.table_id, slot, disposition="evict"
                )
                locator = None
        else:
            # A paid disabled marker, a construction in progress and a concurrent active use all
            # choose the unchanged canonical door.  None allocates a second side structure.
            locator = None
    if close_before_fallback is not None:
        close_before_fallback.close()
    if slot is None or (not build and locator is None):
        return _canonical_identity_with_ref(engine, context, table, record_id)

    if build:
        try:
            candidate = _EndpointIdentityLocator(
                heap=engine.heap,
                table=table,
                snapshot=context.snapshot,
                budget=memo.budget,
                page_size=engine._pool.page_size,
            )
        except _EndpointLocatorCapacity:
            with engine._endpoint_guard:
                memo.abandon_build(table.table_id, slot, disabled=True)
            return _canonical_identity_with_ref(engine, context, table, record_id)
        except BaseException:
            with engine._endpoint_guard:
                memo.abandon_build(table.table_id, slot, disabled=False)
            raise
        with engine._endpoint_guard:
            installed = memo.install(table.table_id, slot, candidate)
        if not installed:
            candidate.close()
            return _canonical_identity_with_ref(engine, context, table, record_id)
        locator = candidate

    disposition = "keep"
    try:
        return locator.locate(record_id)
    except _EndpointLocatorCapacity:
        disposition = "disable"
        return _canonical_identity_with_ref(engine, context, table, record_id)
    except _EndpointLocatorStale:
        disposition = "evict"
        return _canonical_identity_with_ref(engine, context, table, record_id)
    except BaseException:
        # A stored-data refusal is the answer.  Drop the partial accelerator, but never turn the
        # same call into a fallback that could hide or reorder the failure.
        disposition = "evict"
        raise
    finally:
        with engine._endpoint_guard:
            closer = memo.finish(table.table_id, slot, disposition=disposition)
        if closer is not None:
            closer.close()


def _raise_missing_endpoint(
    edge_table: TableDef,
    endpoint_table: TableDef,
    identity: RecordId,
    end: str,
) -> None:
    """Raise the public endpoint absence used by HeapStore.require_endpoints."""
    raise GrafxConfigurationError(
        f"An edge in {edge_table.name!r} names row {identity} of "
        f"{endpoint_table.name!r} as its {end!r} endpoint, and this snapshot has no such row.",
        table=edge_table.name,
        table_id=edge_table.table_id,
        field=end,
        endpoint_table=endpoint_table.name,
        value=identity,
    )


def _require_physical_endpoint(
    engine: QueryEngine,
    context: _Context,
    edge_table: TableDef,
    endpoint_table: TableDef,
    identity: RecordId,
    expected_ref: RecordRef,
    end: str,
) -> HeapVersion:
    """Validate a binding's physical witness against the canonical snapshot-visible identity."""
    canonical = _visible_identity_with_ref(engine, context, endpoint_table, identity)
    if canonical is None:
        # A visible row outside the table's canonical page chain is corruption, not absence.
        disconnected = engine.heap._revalidate_visible_ref(
            endpoint_table, expected_ref, identity, context.snapshot
        )
        if disconnected is not None:
            raise GrafxCorruptionDetected(
                f"Endpoint reference {expected_ref.page}:{expected_ref.slot} names visible "
                f"record {identity} of {endpoint_table.name!r}, but that row is outside its "
                "canonical heap chain.",
                file=engine.heap.file,
                page=expected_ref.page,
                slot=expected_ref.slot,
                table=endpoint_table.name,
                table_id=endpoint_table.table_id,
                record_id=identity,
                field="record_ref",
                reason="outside_canonical_chain",
            )
        _raise_missing_endpoint(edge_table, endpoint_table, identity, end)
    canonical_ref, canonical_version = canonical
    if canonical_ref == expected_ref:
        return canonical_version

    # Decode the binding's witness before classifying it.  A malformed later duplicate remains
    # corruption in its own right and is never hidden behind a generic mismatch or a fallback.
    observed = engine.heap._revalidate_visible_ref(
        endpoint_table, expected_ref, identity, context.snapshot
    )
    if observed is not None:
        raise GrafxCorruptionDetected(
            f"Record {identity} of table {endpoint_table.name!r} has two snapshot-visible "
            f"physical references: canonical {canonical_ref.page}:{canonical_ref.slot} and "
            f"endpoint witness {expected_ref.page}:{expected_ref.slot}.",
            file=engine.heap.file,
            table=endpoint_table.name,
            table_id=endpoint_table.table_id,
            record_id=identity,
            field="record_id",
            canonical_ref=canonical_ref.encode(),
            observed_ref=expected_ref.encode(),
        )
    raise GrafxCorruptionDetected(
        f"Endpoint reference {expected_ref.page}:{expected_ref.slot} for record {identity} of "
        f"{endpoint_table.name!r} is not the canonical snapshot-visible physical reference.",
        file=engine.heap.file,
        page=expected_ref.page,
        slot=expected_ref.slot,
        table=endpoint_table.name,
        table_id=endpoint_table.table_id,
        record_id=identity,
        field="record_ref",
        canonical_ref=canonical_ref.encode(),
        observed_ref=expected_ref.encode(),
    )


def _materialise_edge(
    engine: QueryEngine,
    edge: CreatedRelationship,
    bindings: dict[str, object],
    fresh: set[str],
    row: _Row,
    context: _Context,
) -> tuple[TableDef, tuple[Value, ...]]:
    """Return the stored tuple of one edge, refusing endpoints that cannot be named yet.

    An endpoint is a RecordId, and the identity of a row created by THIS statement does not
    exist until the commit allocates it -- so an edge to a node the same statement creates is
    refused rather than staged against a number nobody has issued. It is a refusal with a
    remedy: match the endpoints first.

    The visibility of both endpoints is then the heap's own door, asked BEFORE anything is
    staged, so an edge naming a row this snapshot cannot see leaves nothing behind.
    """
    for end, variable in (("source", edge.source), ("target", edge.target)):
        if variable in fresh:
            raise GrafxUnsupportedOperation(
                f"The {end} of a {edge.table.name!r} edge is {variable!r}, which this statement "
                "is creating: its row identity is allocated by the commit and does not exist "
                "yet. Create the nodes first and match them, then create the edge.",
                field=end,
                value=variable,
            )
    catalog = context.schema()
    endpoint_specs = (
        ("source", edge.source, edge.table.from_table, ENDPOINT_COLUMNS[0]),
        ("target", edge.target, edge.table.to_table, ENDPOINT_COLUMNS[1]),
    )
    endpoints: list[object] = []
    guards: list[tuple[TableDef, bytes]] = []
    physical: list[
        tuple[str, TableDef, RecordId, RecordRef, RowBinding]
    ] = []
    for end, variable, table_name, endpoint_column in endpoint_specs:
        if table_name is None:
            raise GrafxConfigurationError(
                f"Relationship table {edge.table.name!r} does not say which table its "
                f"{endpoint_column!r} endpoint belongs to.",
                table=edge.table.name,
                field=endpoint_column,
            )
        endpoint_table = catalog.table(table_name)
        binding = bindings.get(variable)
        if not isinstance(binding, RowBinding):
            raise GrafxPlanError(
                f"The {end} of a {edge.table.name!r} edge is {variable!r}, which the rows "
                "reaching it do not carry.",
                field=end,
                value=variable,
            )
        if binding.table != endpoint_table or binding.version.table_id != endpoint_table.table_id:
            raise GrafxPlanError(
                f"The {end} of a {edge.table.name!r} edge must be a row of "
                f"{endpoint_table.name!r}, but {variable!r} is bound to "
                f"{binding.table.name!r}.",
                field=end,
                value=variable,
                table=edge.table.name,
                endpoint_table=endpoint_table.name,
                observed_table=binding.table.name,
            )
        guards.append(
            (binding.table, _partition_key(binding.table, binding.version.values))
        )
        if isinstance(binding.ref, PendingRowRef):
            # The node was staged by an EARLIER statement, so it has a private identity this
            # transaction owns and the commit path resolves before anything is written. The
            # endpoint carries that identity rather than a number nobody has issued.
            owns_pending = getattr(context.txn, "owns_pending_row_ref", None)
            if (
                binding.record_id != 0
                or binding.ref.table_id != endpoint_table.table_id
                or binding.ref.txn_id != getattr(context.txn, "txn_id", None)
                or not callable(owns_pending)
                or not owns_pending(binding.ref)
                or context.already_ended(binding.ref)
            ):
                raise GrafxTransactionStateError(
                    f"The {end} of a {edge.table.name!r} edge carries a pending row reference "
                    "that is not owned by this transaction and endpoint table.",
                    field="pending_row_reference",
                    table=edge.table.name,
                    endpoint_table=endpoint_table.name,
                    value=variable,
                )
            endpoints.append(binding.ref)
            continue
        if binding.record_id == 0:
            raise GrafxUnsupportedOperation(
                f"The {end} of a {edge.table.name!r} edge is {variable!r}, a staged node that "
                "carries no identity this transaction can resolve. Commit the node first, then "
                "create the relationship.",
                field=end,
                value=variable,
                table=edge.table.name,
                operation="relationship_endpoint",
            )
        if type(binding.ref) is not RecordRef:
            raise GrafxPlanError(
                f"The {end} of a {edge.table.name!r} edge has no physical row reference.",
                field=end,
                value=variable,
                table=edge.table.name,
                endpoint_table=endpoint_table.name,
            )
        if context.already_ended(binding.ref):
            _raise_missing_endpoint(
                edge.table, endpoint_table, binding.record_id, endpoint_column
            )
        endpoints.append(binding.record_id)
        physical.append(
            (
                endpoint_column,
                endpoint_table,
                binding.record_id,
                binding.ref,
                binding,
            )
        )
    properties = materialise_row(
        engine,
        edge.table,
        edge.properties,
        row,
        context,
        endpoints=(endpoints[0], endpoints[1]),
    )
    validated: set[tuple[int, int, int]] = set()
    for endpoint_column, endpoint_table, identity, ref, binding in physical:
        key = (endpoint_table.table_id, ref.encode(), identity)
        if key in validated:
            continue  # a physical self-loop needs one identical proof, not two heap reads
        version = _require_physical_endpoint(
            engine,
            context,
            edge.table,
            endpoint_table,
            identity,
            ref,
            endpoint_column,
        )
        if version.record_id != binding.record_id:
            raise GrafxCorruptionDetected(
                f"The validated {endpoint_column!r} endpoint changed identity while "
                f"materialising an edge in {edge.table.name!r}.",
                file=engine.heap.file,
                table=edge.table.name,
                table_id=edge.table.table_id,
                field="record_id",
                expected_record_id=binding.record_id,
                observed_record_id=version.record_id,
            )
        validated.add(key)
    # LAST, once nothing about this edge can still refuse. An edge is a statement ABOUT its two
    # endpoints: it is only correct while those rows are still there, and another transaction
    # deleting one of them makes it wrong. Without the declaration the two commits touch
    # disjoint partitions -- the edge table and the node table -- and neither refuses, so the
    # edge would be published against a node that no longer exists. Declaring the dependency is
    # also what makes that refusal arrive at the FIRST validation, before a row is written and
    # before a page is allocated for it.
    for guard in guards:
        context.hold_read(*guard)
    return edge.table, properties


def _pending_binding(
    variable: str,
    table: TableDef,
    values: tuple[Value, ...],
    *,
    reference: object = None,
    polymorphic: bool = False,
) -> RowBinding:
    """Return a binding for a row this statement staged but has not yet given an identity.

    The identity is zero because the commit allocates it, and zero is the value section 3
    reserves for "none" -- so a projection of a created row reads back the properties it was
    written with and never a number that looks like an identity and is not one. A row handed over
    by an earlier statement carries its private pending reference only in ``RowBinding.ref``;
    that lets SET and DELETE stage another logical intent without exposing the token as a value.
    """
    return RowBinding(
        variable=variable,
        table=table,
        ref=reference,
        polymorphic=polymorphic,
        version=HeapVersion(
            record_id=0,
            xmin=NO_CSN,
            xmax=NO_CSN,
            values=values,
            prev=None,
            schema_version=table.schema_version,
            deleted=False,
            table_id=table.table_id,
        ),
    )


def _transaction_row_view(
    context: _Context, table: TableDef, *, include_held: bool = True
) -> tuple[
    dict[object, tuple[Value, ...] | None],
    list[tuple[object, tuple[Value, ...]]],
]:
    """Return what this transaction has done to each stored row of a table, in order, settled.

    Two things come back. ``state`` maps the reference of every stored row this transaction
    touched to the values it now holds -- or to None when the transaction ended it. ``inserted``
    lists the reference and values of rows it created, including rows carrying a transaction-local
    pending reference but no stored row yet. Earlier statements' intents come
    first and this statement's held rows after, so the LAST thing said about a row is what the
    view reports: an update after an update builds on the first, a delete after an update ends
    the row, and an update after a delete does not resurrect it (the row cannot be matched once
    ended, see :func:`_node_scan`).

    Every question of the form "what exists, as far as this transaction is concerned" must be
    answered from this one view. Three separate readers used to answer it three different ways:
    MERGE matched a version this transaction had already replaced, the primary-key check refused
    a key the transaction had just freed by changing it, and a bulk SET resurrected a row the
    transaction had deleted (C10 round-2 B3, B4, B5).
    """
    state: dict[object, tuple[Value, ...] | None] = {}
    inserted: list[tuple[object, tuple[Value, ...]]] = []

    def note(operation: str, reference: object, values: object) -> None:
        """Fold one staged change into the view, in the order the transaction made it."""
        # Inserts can carry a PendingRowRef so later statements can identify them.  They are
        # nevertheless new logical rows, not heap-backed rows; classifying only by ``reference
        # is None`` would make matching code try to read the pending token from the heap.
        if operation == "INSERT" or reference is None:
            inserted.append((reference, tuple(values)))  # type: ignore[arg-type]
            return
        if operation == "DELETE":
            state[reference] = None
        elif reference in state and state[reference] is None:
            return  # ended already: a later update does not bring the row back
        elif values is not None:
            state[reference] = tuple(values)  # type: ignore[arg-type]

    logical_intents: list[RowIntent] = []
    for intent in getattr(context.txn, "row_intents", ()):
        intent_table = getattr(intent, "table", None)
        if getattr(intent_table, "table_id", None) != table.table_id:
            continue
        logical_intents.append(intent)
    if include_held:
        for held in context.staged_rows:
            if held.table.table_id != table.table_id:
                continue
            operation = {
                _HELD_INSERT: RowOperation.INSERT,
                _HELD_UPDATE: RowOperation.UPDATE,
                _HELD_DELETE: RowOperation.DELETE,
            }[held.operation]
            logical_intents.append(
                RowIntent(
                    table=held.table,
                    values=() if held.values is None else tuple(held.values),
                    record_id=held.identity,
                    operation=operation,
                    reference=held.reference,
                )
            )
    for intent in logical_intents:
        reference = intent.reference
        if isinstance(reference, PendingRowRef):
            owns = getattr(context.txn, "owns_pending_row_ref", None)
            table_id = getattr(intent.table, "table_id", None)
            if (
                reference.txn_id != getattr(context.txn, "txn_id", None)
                or reference.table_id != table_id
                or not callable(owns)
                or not owns(reference)
            ):
                raise GrafxTransactionStateError(
                    "A node overlay may read only a pending row identity issued by its owner "
                    "for this exact table.",
                    field="pending_row_reference",
                    value=repr(reference),
                    txn_id=getattr(context.txn, "txn_id", None),
                    table_id=table.table_id,
                )
    reduced = reduce_row_intents(logical_intents)
    for intent in reduced:
        if (
            isinstance(intent.reference, PendingRowRef)
            and intent.operation is not RowOperation.INSERT
        ):
            raise GrafxTransactionStateError(
                "A pending row identity must begin with an insert before it can be updated or "
                "deleted by its owner.",
                field="pending_row_reference",
                value=repr(intent.reference),
                txn_id=getattr(context.txn, "txn_id", None),
                table_id=table.table_id,
                operation=intent.operation.value,
            )
    for intent in reduced:
        note(intent.operation.name, intent.reference, intent.values)
    return state, inserted


def _require_unique_primary_key(
    engine: QueryEngine,
    table: TableDef,
    values: tuple[Value, ...],
    context: _Context,
    *,
    also: Sequence[tuple[Value, ...]] = (),
    replacing: object = None,
) -> None:
    """Refuse a row whose primary key another live row of the table already carries.

    The check is complete only together with optimistic validation, and the split is deliberate.
    This call compares against what THIS transaction can see: the rows in its snapshot, the rows
    it has staged itself (``also`` carries the ones of the pattern being built, which are not
    held yet), minus the rows it has ended. A row another transaction commits concurrently under
    the same key is invisible here -- and it does not need to be visible, because both commits
    write the partition that key hashes to (:func:`_partition_key`), so step 3.3 of the commit
    protocol refuses the second one with ``write_conflict``. The sequential case is this call;
    the concurrent case is the log. Neither alone is the rule.

    ``replacing`` names the stored row an update is writing a new version of, so a row that keeps
    its own key is not refused against itself. Kuzu refuses a duplicate key outright (D3), and
    until this call nothing did: two committed transactions each creating ``{id: 1}`` left two
    rows under one declared primary key, which is duplication an ordinary caller reaches.
    """
    if table.kind != "node" or table.primary_key is None:
        return
    position = table.column_index(table.primary_key)
    key = values[position]
    if key is None:
        return  # the schema refuses a null key elsewhere; this check is about collisions
    for other in also:
        if _equal(other[position], key):
            raise _duplicate_key(table, key)
    state, inserted = _transaction_row_view(context, table)
    for reference, pending in inserted:
        if replacing is not None and reference == replacing:
            continue
        if len(pending) > position and _equal(pending[position], key):
            raise _duplicate_key(table, key)
    for reference, latest in state.items():
        if latest is None or (replacing is not None and reference == replacing):
            continue  # ended, or this very row being updated again
        if len(latest) > position and _equal(latest[position], key):
            raise _duplicate_key(table, key)
    stored = _rows_carrying_key(engine, table, key, position, context)
    if stored is None:
        stored = engine.heap.scan(table, context.snapshot)
    for ref, version in stored:
        if ref in state or (replacing is not None and ref == replacing):
            continue  # replaced or ended by this transaction: the view above is its truth
        if _equal(version.values[position], key):
            raise _duplicate_key(table, key)


def _rows_carrying_key(
    engine: QueryEngine,
    table: TableDef,
    key: Value,
    position: int,
    context: _Context,
) -> tuple[tuple[object, object], ...] | None:
    """Return the stored rows the primary-key index offers, or None to fall back to a scan.

    The question this check asks -- "is there a live row of this table, visible to my snapshot,
    carrying this key?" -- is the question an EXACT index answers, and `IndexManager.lookup`
    already discharges the whole obligation of CONTRACT.md section 8.7 on the way: every candidate
    is read from the heap, checked for visibility under the caller's snapshot, checked to still
    carry the key it was filed under, and checked to belong to this table. So the rows this hands
    back are exactly the rows the scan would have kept, and the loop above is unchanged.

    Without it every insert scanned the whole table, which made a bulk load quadratic: measured
    at 16 ms a row over 200 rows and 89 ms a row over 3200, on a machine where a commit of ten
    rows costs about 75 ms in total.

    None means "ask the heap instead", and it is returned for three reasons that are all the same
    reason: there is no index framework, there is no index over this table, or the index is STALE.
    A stale index is a SUBSET of the heap, and a subset is precisely what this check must not
    consult -- a missing entry would let a duplicate key through, which is the duplication this
    function exists to refuse. Falling back to the scan is slower and right.
    """
    manager = getattr(engine, "_indexes", None)
    if manager is None:
        return None
    lookup = getattr(manager, "lookup", None)
    index_of = getattr(manager, "index", None)
    if not callable(lookup) or not callable(index_of):
        return None
    name = primary_key_index_name(table.name)
    try:
        index = index_of(name)
    except GrafxError:
        return None  # no index covers this table's key
    if (
        index.definition.table_id != table.table_id
        or index.definition.table_name != table.name
        or index.definition.positions != (position,)
    ):
        # A name collision rather than this table's index, and this guard is load-bearing rather
        # than defensive. An earlier version of this comment claimed "the catalog is what refuses
        # that"; measured, the catalog does NOT. It compares table names case-SENSITIVELY, so
        # `Person` and `person` are two legal tables, while an index name is case-FOLDED because
        # it becomes a file name -- so the two want one index. The second table goes without one
        # (see `_attach_primary_key_index`), and this check is what stops it reading the FIRST
        # table's index and answering "no row holds this key" for a key that table does hold.
        return None
    if getattr(index, "stale", False):
        return None
    template: list[Value] = [None] * len(table.columns)
    template[position] = key
    return tuple(
        (ref, engine.heap.read(ref))
        for ref in lookup(name, index_key(template, (position,)), context.snapshot)
    )


def _duplicate_key(table: TableDef, key: Value) -> GrafxQueryError:
    """Return the refusal for a primary key the table already holds."""
    return GrafxQueryError(
        f"Table {table.name!r} already holds a row whose primary key {table.primary_key!r} is "
        f"{key!r}, and a primary key names exactly one row.",
        field="primary_key",
        table=table.name,
        column=table.primary_key,
        value=repr(key),
        constraint="primary_key",
    )


def _uncommitted_rows_with_refs(
    context: _Context, table: TableDef
) -> Iterator[tuple[object, tuple[Value, ...]]]:
    """Yield this transaction's uncommitted rows of one table WITH the stored row each replaces.

    An insert carries its pending reference. An update carries the reference of the version it
    replaces, which is what lets a key check tell "another row under this key" from "an earlier
    statement of this transaction updating the very row being updated again" -- the second is
    the ordinary shape of two SETs on one row and must not refuse itself.
    """
    state, inserted = _transaction_row_view(context, table)
    yield from inserted
    for reference, latest in state.items():
        if latest is not None:
            yield reference, latest


def _uncommitted_rows(
    context: _Context, table: TableDef
) -> Iterator[tuple[Value, ...]]:
    """Yield the rows of one table this transaction has written but not yet committed.

    MERGE asks "does a row like this exist?", and a transaction that created one a moment ago
    must answer yes about its own work -- otherwise two MERGEs of one row in one transaction
    produce two rows, and one MERGE driven by a pipeline of N rows produces N. The heap cannot
    answer it: a staged row is deliberately not there yet, because its birth stamp is the commit
    number and the commit has not happened.

    Two places hold that work, and both are read, because they are different ages of the same
    thing: rows this STATEMENT has built and not yet handed over, and rows an EARLIER statement
    of the same transaction handed over already. Reading one and not the other would fix the
    cross-statement case and leave the within-statement case, or the reverse.

    An UPDATE is yielded as the new version, because that is the row this transaction now has. A
    DELETE yields nothing here and is answered by :func:`_ended_by_this_transaction` instead: it
    is the absence of a row rather than the presence of one, so it has to remove a heap answer
    rather than add a staged one.
    """
    for _reference, values in _uncommitted_rows_with_refs(context, table):
        yield values


def _current_values(context: _Context, binding: RowBinding) -> tuple[Value, ...]:
    """Return the values a SET must build on: this transaction's latest, else the snapshot's.

    A SET builds a whole new version, so it has to start from the row as this transaction has it,
    not as the snapshot has it. An earlier statement of the same transaction may already have
    staged an update -- the heap still shows the old values, because the new version is born at a
    commit number that does not exist yet -- and starting from the snapshot would silently undo
    that earlier statement while reporting success. Two statements each setting a different
    property of one row is an ordinary thing to write and the last one would win outright.

    This is the same blindness that made MERGE create instead of match, one operator along, and it
    reads the same two places for the same reason: what this statement has held, and what earlier
    statements of this transaction handed over.
    """
    if binding.ref is None:
        # A row THIS statement created: its latest values are the held insert's, which an
        # earlier SET clause may already have rewritten. Starting from the binding's original
        # values would make the second clause undo the first (C10 round-3 B2, case 8).
        position = _held_insert_position(context, binding)
        if position is not None:
            held = context.staged_rows[position].values
            if held is not None:
                return held
        return binding.version.values
    state, inserted = _transaction_row_view(context, binding.table)
    latest = state.get(binding.ref)
    if latest is not None:
        return latest
    for reference, pending in inserted:
        if reference == binding.ref:
            return pending
    return binding.version.values


def _overlay_identity(binding: RowBinding) -> object:
    """Return what names this row inside its owner's combined view.

    A committed row is named by its record id. A row this transaction staged has none yet -- the
    commit allocates it -- so it is named by the private reference the staging door issued. That
    is the SAME token a pending edge carries in its endpoint slot, which is what lets one map
    answer for both kinds of row without a second key space to keep in step.
    """
    reference = binding.ref
    if isinstance(reference, PendingRowRef):
        return reference
    return binding.record_id


def _owner_landing_txn_memo(
    engine: QueryEngine, context: _Context
) -> _OwnerLandingTxnMemo | None:
    """Return the lazy landing memo owned by this exact transaction and read picture."""
    txn_id = getattr(context.txn, "txn_id", None)
    if not isinstance(txn_id, int) or isinstance(txn_id, bool):
        return None
    snapshot = context.snapshot
    # Establishing the catalog picture may read pages in other compositions.  The private
    # derived-state guard never owns that collaborator work.
    schema = context.schema()
    closers: tuple[_OwnerLandingView, ...] = ()
    try:
        with engine._endpoint_guard:
            memo = engine._owner_memo.get(txn_id)
            if memo is not None and (
                memo.txn is not context.txn
                or memo.snapshot is not snapshot
                or memo.schema is not schema
            ):
                engine._owner_memo.pop(txn_id, None)
                closers = memo.retire()
                memo = None
            if memo is None:
                try:
                    candidate = _OwnerLandingTxnMemo(
                        txn=context.txn,
                        snapshot=snapshot,
                        schema=schema,
                        budget=engine._owner_budget,
                    )
                except _OwnerLandingCapacity:
                    candidate = None
                if candidate is not None:
                    try:
                        engine._owner_memo[txn_id] = candidate
                    except BaseException:
                        candidate.retire()
                        raise
                    memo = candidate
    finally:
        for view in closers:
            view.close()
    return memo


def _landing_fingerprint(context: _Context, table: TableDef) -> tuple:
    """Summarise everything this transaction has said about one table's rows, by content.

    The landing view of a table is a pure function of the snapshot, the schema, the row
    intents the transaction has staged for that table, and the rows this statement holds but
    has not yet handed over. Content, not counts: a savepoint rollback can truncate the intent
    list back to a length it already had, and a second update of one row replaces values
    without changing any length. Comparing the pieces themselves costs the size of this
    transaction's writes to the table -- the scan the memo avoids costs the size of the table.
    """
    table_id = table.table_id
    intents = tuple(
        (intent.operation, intent.reference, intent.record_id, intent.values)
        for intent in getattr(context.txn, "row_intents", ())
        if getattr(getattr(intent, "table", None), "table_id", None) == table_id
    )
    held = tuple(
        (row.operation, row.reference, row.identity, row.values, row.token)
        for row in context.staged_rows
        if row.table.table_id == table_id
    )
    return (table.schema_version, intents, held)


def _owner_landing_view(
    engine: QueryEngine,
    context: _Context,
    table: TableDef,
    ended: frozenset[object] | set[object],
) -> _OwnerLandingView:
    """Return a bounded, transaction-local, lazy identity view for one landing table.

    The former ST-6 map decoded and retained every visible row on the first landing.  This R1
    view asks the D-02 identity door only for IDs an edge actually names, while keeping the same
    ``ended``/``changed``/``inserted`` owner overlay.  Its decoded-result cache is admitted under
    explicit engine-wide bytes and entries ceilings before it grows and is discarded on quota.
    There is deliberately no R2 full scan: without evidence and a second complete admission
    design it would reintroduce both the O(table) cold cost and wide-table payload retention.

    Two deliberate exclusions, decided in ST-6 and not to be revisited casually:
    ``require_endpoints`` is NOT routed through here (the door checks visibility of a row the
    caller names; the memo filters ``ended`` -- same table, different question) and neither is
    ``_incident_edges`` (a DETACH DELETE invalidates its own view as it goes, so the memo
    would rebuild per statement and pay its bookkeeping for nothing).
    """
    fingerprint = _landing_fingerprint(context, table)
    memo = _owner_landing_txn_memo(engine, context)
    epoch = engine.heap._derived_read_epoch()
    slot: _OwnerLandingSlot | None = None
    install = False
    previous: _OwnerLandingView | None = None
    if memo is not None:
        with engine._endpoint_guard:
            slot = memo.tables.get(table.table_id)
            if slot is None:
                try:
                    slot = memo.claim(table.table_id)
                except _OwnerLandingCapacity:
                    slot = None
                else:
                    install = True
            elif slot.state == "ready":
                entry = slot.view
                assert entry is not None
                if entry.matches(
                    table=table,
                    snapshot=context.snapshot,
                    fingerprint=fingerprint,
                    epoch=epoch,
                ):
                    return entry
                previous = memo.begin_rebuild(table.table_id, slot)
                install = previous is not None
            else:
                # A concurrent builder already owns the one paid slot.  This statement uses an
                # unretained fallback rather than creating another side structure.
                slot = None
    if previous is not None:
        previous.close()

    try:
        changed, inserted = _transaction_row_view(context, table, include_held=False)
        owned_refs = set(changed)
        owned_refs.update(
            held.reference
            for held in context.staged_rows
            if held.table.table_id == table.table_id and held.reference is not None
        )
        table_ended = frozenset(
            reference for reference in owned_refs if reference in ended
        )
    except BaseException:
        # A claimed table slot is a reservation, not a disabled marker.  Even an allocation
        # failure while reducing the owner's overlay must not strand it until settlement.
        if memo is not None and slot is not None:
            with engine._endpoint_guard:
                memo.abandon_build(table.table_id, slot)
        raise

    budget = engine._owner_budget if install else None
    try:
        view = _OwnerLandingView(
            engine=engine,
            context=context,
            table=table,
            fingerprint=fingerprint,
            changed=changed,
            inserted=inserted,
            ended=table_ended,
            budget=budget,
            guard=engine._endpoint_guard,
        )
    except _OwnerLandingCapacity:
        # A table slot was admitted before its view.  Give that reservation back before making
        # the statement-local fallback; no failed-admission marker or payload survives here.
        if memo is not None and slot is not None:
            with engine._endpoint_guard:
                memo.abandon_build(table.table_id, slot)
        return _OwnerLandingView(
            engine=engine,
            context=context,
            table=table,
            fingerprint=fingerprint,
            changed=changed,
            inserted=inserted,
            ended=table_ended,
            budget=None,
            guard=engine._endpoint_guard,
        )
    except BaseException:
        if memo is not None and slot is not None:
            with engine._endpoint_guard:
                memo.abandon_build(table.table_id, slot)
        raise

    if not install or memo is None or slot is None:
        return view
    with engine._endpoint_guard:
        installed = memo.install(table.table_id, slot, view)
    if not installed:
        # Settlement or memo replacement won the publication race.  Preserve this statement's
        # already-computed overlay, but return every retention charge before exposing the view.
        view.forfeit_retention()
    return view


def _owner_edges(
    context: _Context, table: TableDef
) -> tuple[
    dict[object, tuple[Value, ...] | None],
    tuple[tuple[object, HeapVersion], ...],
]:
    """Return this transaction's changes to stored edges and the edges it has created."""
    changed, inserted = _transaction_row_view(context, table, include_held=False)
    pending = tuple(
        (
            reference,
            HeapVersion(
                record_id=0,
                xmin=NO_CSN,
                xmax=NO_CSN,
                values=values,
                prev=None,
                schema_version=table.schema_version,
                deleted=False,
                table_id=table.table_id,
            ),
        )
        for reference, values in inserted
        if isinstance(reference, PendingRowRef)
    )
    return changed, pending


def _ended_by_this_transaction(context: _Context) -> set[object]:
    """Return the stored rows this transaction has already staged the end of.

    A MERGE after a DELETE in the same transaction must not match the row the transaction just
    ended. The heap still holds it -- the end stamp is written at the commit number, which does
    not exist yet -- so the scan will find it and would answer "this row already exists" about a
    row the caller has just said it wants gone.
    """
    ended: set[object] = set()
    tables: dict[int, TableDef] = {}
    for held in context.staged_rows:
        tables[held.table.table_id] = held.table
    for intent in getattr(context.txn, "row_intents", ()):
        intent_table = getattr(intent, "table", None)
        if getattr(intent_table, "table_id", None) is not None:
            tables[intent_table.table_id] = intent_table  # type: ignore[union-attr]
    for table in tables.values():
        state, _inserted = _transaction_row_view(context, table)
        ended.update(reference for reference, latest in state.items() if latest is None)
    return ended


def _named_positions(table: TableDef, written: CreatedNode) -> tuple[int, ...] | None:
    """Return the column positions a MERGE pattern named, or None when it named none."""
    if written.properties is None or not written.properties.entries:
        return None
    return tuple(table.column_index(entry.key) for entry in written.properties.entries)


def _matching_row(
    engine: QueryEngine,
    written: CreatedNode,
    values: tuple[Value, ...],
    context: _Context,
) -> RowBinding | None:
    """Return the row a MERGE pattern already matches, or None when it must be created.

    The comparison is over the properties the pattern NAMED, not the whole tuple: MERGE means
    "one row like this", and a row that also carries values the pattern said nothing about is
    still that row.

    This transaction's own uncommitted rows are consulted BEFORE the heap, and that order is not
    an optimisation: they are the newer answer, and they are the only answer for a row the heap
    has never seen.
    """
    table = written.table
    if table is None or written.variable is None:
        return None
    positions = _named_positions(table, written)
    if positions is None:
        return None
    state, inserted = _transaction_row_view(context, table)
    for reference, pending in inserted:
        if len(pending) == len(values) and all(
            _equal(pending[at], values[at]) for at in positions
        ):
            return _pending_binding(
                written.variable, table, pending, reference=reference
            )
    for reference, latest in state.items():
        if latest is None or len(latest) != len(values):
            continue
        if all(_equal(latest[at], values[at]) for at in positions):
            # A stored row this transaction has already updated: it HAS a reference, and the
            # binding carries it with the transaction's latest values, so a SET, a DELETE or an
            # edge built on the match works the way it does for any stored row. Handing back an
            # identity-less pending binding here made every later clause refuse or -- for a
            # DELETE -- reach the transaction's door with no reference (C10 round-3 B1).
            stored = engine.heap.read(reference)
            return RowBinding(
                variable=written.variable,
                table=table,
                ref=reference,
                version=replace(stored, values=latest),
            )
    for ref, version in engine.heap.scan(table, context.snapshot):
        if ref in state:
            continue  # replaced or ended by this transaction; _uncommitted_rows spoke for it
        if all(_equal(version.values[at], values[at]) for at in positions):
            return RowBinding(
                variable=written.variable, table=table, ref=ref, version=version
            )
    return None


def _matching_edge(
    engine: QueryEngine,
    edge: CreatedRelationship,
    materialised: tuple[TableDef, tuple[Value, ...]],
    context: _Context,
) -> bool:
    """Return True when the edge a MERGE names already exists.

    An edge is identified by the pair it connects, which leads its stored tuple, plus whatever
    properties the pattern named -- so ``MERGE (a)-[:KNOWS]->(b)`` matches any KNOWS edge between
    the two, and ``MERGE (a)-[:KNOWS {since: 7}]->(b)`` matches only one that also carries that
    year. The same two sources are consulted as for a node and in the same order, for the same
    reason: without this a relationship MERGE is a CREATE wearing another keyword.
    """
    table, values = materialised
    positions = [0, 1]
    if edge.properties is not None:
        positions.extend(
            table.column_index(entry.key) for entry in edge.properties.entries
        )
    wanted = tuple(positions)
    for pending in _uncommitted_rows(context, table):
        if len(pending) == len(values) and all(
            _equal(pending[at], values[at]) for at in wanted
        ):
            return True
    for _ref, version in engine.heap.scan(table, context.snapshot):
        if all(_equal(version.values[at], values[at]) for at in wanted):
            return True
    return False


def _require_write_transaction(txn: object) -> object:
    """Return the transaction, refusing one that cannot accept a staged row."""
    stage = getattr(txn, "stage_row_insert", None)
    if stage is None or not callable(stage):
        raise GrafxTransactionStateError(
            "A statement that writes rows needs a write transaction to stage them on; the "
            f"object supplied is a {type(txn).__name__}.",
            field="transaction",
            value=type(txn).__name__,
        )
    return txn


def _partition_key(table: TableDef, values: tuple[Value, ...]) -> bytes:
    """Return the bytes that decide which partition one written row belongs to.

    The key is the row's own identity where the schema gives it one -- a primary key for a node,
    the pair of endpoints for an edge -- so two transactions writing the SAME row conflict and
    two writing different rows of one table do not. A table with no key of its own falls back to
    the table itself, which over-approximates: it can only produce a conflict that did not have
    to happen, never miss one that did.
    """
    if table.kind == "rel":
        endpoints = tuple(values[position] for position in range(ENDPOINT_COLUMN_COUNT))
        if any(isinstance(endpoint, PendingRowRef) for endpoint in endpoints):
            # An endpoint this transaction has not resolved yet has no encoding, and inventing
            # one would put the edge in a partition its committed self will not land in. The
            # table itself is the honest key here: it over-approximates, and an over-approximate
            # interest set can only produce a conflict that did not have to happen -- never miss
            # one that did.
            return b""
        return b"".join(encode_value(endpoint) for endpoint in endpoints)
    if table.primary_key is not None:
        return encode_value(values[table.column_index(table.primary_key)])
    return b""


def _prepare_assignment(
    engine: QueryEngine, assignment: PropertyAssignment, row: _Row, context: _Context
) -> tuple[RowBinding, int, Value]:
    """Check one SET assignment against the column it writes, and return what it writes.

    Nothing is stored here. The caller collects every assignment of the statement first, so a
    SET whose second assignment refuses leaves the transaction exactly as it found it.
    """
    target = assignment.target
    if not isinstance(target.subject, Variable):
        raise GrafxPlanError(
            f"SET assigns to a property of a matched variable; got {target.describe()}.",
            field="target",
            value=target.describe(),
        )
    binding = row.bindings.get(target.subject.name)
    if not isinstance(binding, RowBinding):
        raise GrafxPlanError(
            f"SET names {target.subject.name!r}, which the rows reaching it do not carry.",
            field="variable",
            value=target.subject.name,
        )
    if binding.table.kind == "rel" and target.key in ENDPOINT_COLUMNS:
        raise GrafxPlanError(
            f"{target.key!r} is the layout's own endpoint column of a relationship table, so "
            "SET may not write it; the arrow decides it.",
            field="column",
            value=target.key,
            table=binding.table.name,
        )
    column = _column_named(binding.table, target.key)
    value = _evaluate(assignment.value, row, context)
    _check_stored_value(engine, binding.table, column, value, context)
    return binding, binding.table.column_index(target.key), value


def materialise_row(
    engine: QueryEngine,
    table: TableDef,
    properties: MapExpression | None,
    row: _Row,
    context: _Context,
    *,
    endpoints: tuple[int, int] | None = None,
) -> tuple[Value, ...]:
    """Return the positional values one written row stores, refusing anything it cannot store.

    Columns the pattern did not mention take a null, which the schema then refuses for a column
    that forbids one -- so a missing primary key is caught here rather than becoming a row nobody
    can find. The final check is the domain model's own ``encode_tuple``, because arity,
    nullability and column type already have exactly one definition and a second one here would
    be free to drift from it (amendment A24). Its bytes are discarded: this call is the
    validation, and the heap encodes again when it actually stores the row.
    """
    values: list[Value] = [None] * table.arity
    if endpoints is not None:
        values[0], values[1] = endpoints
    if properties is not None:
        for entry in properties.entries:
            if entry.key in ENDPOINT_COLUMNS:
                raise GrafxPlanError(
                    f"{entry.key!r} is the layout's own endpoint column of a relationship "
                    "table, so a pattern may not write it; the arrow decides it.",
                    field="column",
                    value=entry.key,
                    table=table.name,
                )
            column = _column_named(table, entry.key)
            position = table.column_index(entry.key)
            stored = _stored_value(
                engine, table, column, _evaluate(entry.value, row, context), context
            )
            values[position] = stored
    materialised = tuple(values)
    # An endpoint an earlier statement promised has no stored encoding yet, so the shape is
    # validated against the width it will occupy. Nothing about the promise is waved through:
    # the commit path resolves it and re-encodes the resolved row before anything is written,
    # and every column the caller actually wrote is checked here exactly as before.
    encode_tuple(table, _validatable_row(table, materialised))
    return materialised


def _validatable_row(table: TableDef, values: tuple[Value, ...]) -> tuple[Value, ...]:
    """Return the row with each unresolved endpoint standing in for the id it will become.

    Only the two endpoint columns of a relationship may hold a promise, and the substitution is
    positional for that reason. Anywhere else a promise is a value that will never become one, so
    it is refused HERE rather than widened into the encoder -- the staging door draws the same
    line, and two doors drawing it differently is how a token eventually walks through one.
    """
    endpoints = ENDPOINT_COLUMN_COUNT if table.kind == "rel" else 0
    substituted: list[Value] = []
    for position, value in enumerate(values):
        if not isinstance(value, PendingRowRef):
            substituted.append(value)
            continue
        if position >= endpoints:
            raise GrafxUnsupportedOperation(
                "Only the two endpoint columns of a relationship may name a row this "
                "transaction has not committed yet.",
                field="column",
                value=table.columns[position].name
                if position < table.arity
                else position,
                table=table.name,
                operation="pending_value",
            )
        substituted.append(SIZING_ENDPOINT)
    return tuple(substituted)


def _column_named(table: TableDef, key: str) -> ColumnDef:
    """Return one column of a table, refusing a name it does not declare."""
    for column in table.columns:
        if column.name == key:
            return column
    declared = ", ".join(column.name for column in table.columns)
    raise GrafxPlanError(
        f"Table {table.name!r} has no column named {key!r}; it declares {declared}.",
        field="column",
        value=key,
        table=table.name,
    )


def _stored_value(
    engine: QueryEngine,
    table: TableDef,
    column: ColumnDef,
    value: object,
    context: _Context,
) -> Value:
    """Return the value as the column stores it, converting an embedding to its stored shape.

    A caller writes a vector as an ordinary list of numbers, and what the heap stores is a
    VectorValue that knows its own space. The conversion needs the space, and the CHECK -- the
    dimension, the finiteness of every component, whether the space still accepts writes -- is
    the vector subsystem's own door, so this asks it rather than repeating the rule (VEC BR-5).
    """
    if not column.is_vector or value is None:
        return value  # type: ignore[return-value]
    if isinstance(value, VectorValue):
        return value
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise GrafxPlanError(
            f"The column {table.name}.{column.name} stores an embedding, which is written as a "
            f"sequence of numbers; got {type(value).__name__}.",
            field="column",
            value=column.name,
        )
    space = context.schema().space(str(column.vector_space))
    vectors = engine.require_vectors()
    validated = vectors.validate_vector(space, tuple(value))  # type: ignore[attr-defined]
    return VectorValue(
        values=tuple(validated), space_ref=space.space_id, dtype=space.storage_dtype
    )


def _check_stored_value(
    engine: QueryEngine,
    table: TableDef,
    column: ColumnDef,
    value: object,
    context: _Context,
) -> Value:
    """Return one value checked against the column it is about to be written to."""
    stored = _stored_value(engine, table, column, value, context)
    if stored is None:
        if not column.nullable:
            raise GrafxPlanError(
                f"The column {table.name}.{column.name} does not accept a null.",
                field="column",
                value=column.name,
            )
        return stored
    observed = value_type_of(stored)
    if observed is not column.type:
        raise GrafxPlanError(
            f"The column {table.name}.{column.name} stores {column.type.name} and the value "
            f"written to it is {observed.name}.",
            field="column",
            value=column.name,
        )
    return stored


_Handler = Callable[[QueryEngine, PlanNode, _Context], Iterator[_Row]]

_HANDLERS: dict[type, _Handler] = {
    SingleRow: _single_row,  # type: ignore[dict-item]
    UnwindRows: _unwind_rows,  # type: ignore[dict-item]
    WithRows: _with_rows,  # type: ignore[dict-item]
    AllNodesScan: _all_nodes_scan,  # type: ignore[dict-item]
    NodeScan: _node_scan,  # type: ignore[dict-item]
    IndexSeek: _index_seek,  # type: ignore[dict-item]
    TraverseAnyRelationship: _traverse_any,  # type: ignore[dict-item]
    TraverseRelationship: _traverse,  # type: ignore[dict-item]
    RelationshipScan: _relationship_scan,  # type: ignore[dict-item]
    FilterRows: _filter_rows,  # type: ignore[dict-item]
    VectorSearch: _vector_search,  # type: ignore[dict-item]
    AggregateRows: _aggregate_rows,  # type: ignore[dict-item]
    ProjectRows: _project_rows,  # type: ignore[dict-item]
    DistinctRows: _distinct_rows,  # type: ignore[dict-item]
    UnionRows: _union_rows,  # type: ignore[dict-item]
    OptionalRows: _optional_rows,  # type: ignore[dict-item]
    EagerRows: _eager_rows,  # type: ignore[dict-item]
    SortRows: _sort_rows,  # type: ignore[dict-item]
    SkipRows: _skip_rows,  # type: ignore[dict-item]
    LimitRows: _limit_rows,  # type: ignore[dict-item]
    CreateRelationships: _write_rows,  # type: ignore[dict-item]
    MergePattern: _write_rows,  # type: ignore[dict-item]
    SetProperties: _write_rows,  # type: ignore[dict-item]
    DeleteEntities: _write_rows,  # type: ignore[dict-item]
}
"""Which function answers which operator, looked up by type rather than by a chain of checks."""


# --- expressions ------------------------------------------------------------------------------


def _evaluate(expression: Expression, row: _Row, context: _Context) -> object:
    """Return what one expression means for one row.

    Nulls follow the three-valued logic of the language and not Python's truthiness: a comparison
    with a null is unknown rather than false, ``AND`` is false as soon as either side is, and
    ``OR`` is true as soon as either side is. A filter keeps a row only when its predicate is
    exactly true, so unknown drops the row without ever claiming it was false.
    """
    computed = row.computed
    if computed is not None and expression in computed:
        return computed[expression]
    if isinstance(expression, Literal):
        return expression.value
    if isinstance(expression, Parameter):
        if expression.name not in context.parameters:
            raise GrafxPlanError(
                f"The query needs the parameter ${expression.name}, which was not supplied.",
                field="parameter",
                value=expression.name,
            )
        return context.parameters[expression.name]
    if isinstance(expression, Variable):
        return _read_variable(expression, row, computed)
    if isinstance(expression, Property):
        subject = _evaluate(expression.subject, row, context)
        if subject is None:
            return None
        if isinstance(subject, Mapping):
            return _map_property_value(subject, expression)
        if not isinstance(subject, RowBinding):
            raise GrafxPlanError(
                f"A property is read from a matched row; {expression.describe()} reads a "
                f"{type(subject).__name__}.",
                field="property",
                value=expression.key,
            )
        return subject.value(expression.key)
    if isinstance(expression, NullCheck):
        value = _evaluate(expression.operand, row, context)
        return (value is not None) if expression.negated else (value is None)
    if isinstance(expression, UnaryOperation):
        return _unary(expression, row, context)
    if isinstance(expression, BinaryOperation):
        return _binary(expression, row, context)
    if isinstance(expression, ListExpression):
        return tuple(
            _evaluate(element, row, context) for element in expression.elements
        )
    if isinstance(expression, MapExpression):
        return {
            entry.key: _evaluate(entry.value, row, context)
            for entry in expression.entries
        }
    if isinstance(expression, CaseExpression):
        return _case(expression, row, context)
    if isinstance(expression, Subscript):
        return _subscript(expression, row, context)
    if isinstance(expression, FunctionCall):
        return _call(expression, row, context)
    raise GrafxPlanError(
        f"An expression of type {type(expression).__name__} cannot be evaluated.",
        field="expression",
        value=type(expression).__name__,
    )


def _case(expression: CaseExpression, row: _Row, context: _Context) -> object:
    """Evaluate every CASE expression eagerly, then select its first matching arm."""
    operand = (
        _evaluate(expression.operand, row, context)
        if expression.operand is not None
        else None
    )
    alternatives: list[tuple[object, object]] = []
    for alternative in expression.alternatives:
        condition = _evaluate(alternative.condition, row, context)
        result = _evaluate(alternative.result, row, context)
        alternatives.append((condition, result))
    fallback = (
        _evaluate(expression.fallback, row, context)
        if expression.fallback is not None
        else None
    )

    selected = fallback
    if expression.operand is None:
        for condition, result in alternatives:
            if condition is not None and not isinstance(condition, bool):
                message = (
                    f"A searched CASE tests booleans or nulls; got "
                    f"{type(condition).__name__}."
                )
                raise GrafxPlanError(
                    message,
                    field="case",
                    value=expression.describe(),
                )
            if condition is True:
                selected = result
                break
    else:
        for condition, result in alternatives:
            equal = (
                operand is None
                and condition is None
                or operand is not None
                and condition is not None
                and _equal(operand, condition)
            )
            if equal:
                selected = result
                break

    result_type = context.case_types.get(id(expression))
    if result_type is ValueType.DOUBLE and selected is not None:
        return float(selected)
    return selected


def _subscript(expression: Subscript, row: _Row, context: _Context) -> object:
    """Extract one element with Ladybug's one-based positive and negative positions."""
    subject = _evaluate(expression.subject, row, context)
    index = _evaluate(expression.index, row, context)
    return _subscript_value(expression, subject, index)


def _subscript_value(expression: Subscript, subject: object, index: object) -> object:
    """Extract one already evaluated list element under the public subscript contract."""
    if subject is None or index is None:
        return None
    if not isinstance(subject, (list, tuple)):
        message = f"A subscript extracts from a list; got {type(subject).__name__}."
        raise GrafxPlanError(
            message,
            field="subscript",
            value=expression.describe(),
        )
    if isinstance(index, bool) or not isinstance(index, int):
        message = (
            f"A list subscript is a whole-number position; got {type(index).__name__}."
        )
        raise GrafxPlanError(
            message,
            field="subscript",
            value=expression.describe(),
        )
    if index == 0:
        message = "A list subscript uses one-based positions; zero is not a position."
        raise GrafxPlanError(
            message,
            field="subscript",
            value=index,
        )
    offset = index - 1 if index > 0 else index
    if not -len(subject) <= offset < len(subject):
        message = (
            f"The list subscript {index} is out of range for {len(subject)} elements."
        )
        raise GrafxPlanError(
            message,
            field="subscript",
            value=index,
        )
    return subject[offset]


def _map_property_value(
    subject: Mapping[object, object], expression: Property
) -> object:
    """Read one case-insensitive map key, refusing an ambiguous parameter map."""
    folded = expression.key.lower()
    matches = tuple(
        (key, value)
        for key, value in subject.items()
        if isinstance(key, str) and key.lower() == folded
    )
    if len(matches) > 1:
        keys = ", ".join(repr(key) for key, _value in sorted(matches))
        message = f"The map in {expression.describe()} has colliding keys {keys}."
        raise GrafxPlanError(
            message,
            field="property",
            value=expression.key,
        )
    if matches:
        return matches[0][1]
    message = f"The map in {expression.describe()} has no key {expression.key!r}."
    raise GrafxPlanError(
        message,
        field="property",
        value=expression.key,
    )


def _read_variable(
    expression: Variable, row: _Row, computed: dict[Expression, object] | None
) -> object:
    """Return what a bare variable denotes, refusing one the row no longer carries."""
    if expression.name in row.bindings:
        return row.bindings[expression.name]
    if row.columns is not None and expression.name in row.columns:
        return row.columns[expression.name]
    if computed is not None:
        raise GrafxPlanError(
            f"The variable {expression.name!r} is not available after a group is formed; only "
            "the grouping keys and the aggregates are.",
            field="variable",
            value=expression.name,
        )
    raise GrafxPlanError(
        f"The variable {expression.name!r} is bound by no pattern of this query.",
        field="variable",
        value=expression.name,
    )


def _unary(expression: UnaryOperation, row: _Row, context: _Context) -> object:
    """Return the value of a prefix operation."""
    value = _evaluate(expression.operand, row, context)
    if expression.operator == "NOT":
        truth = _truth(value)
        return None if truth is None else (not truth)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GrafxPlanError(
            f"A sign applies to a number; got {type(value).__name__}.",
            field="operator",
            value=expression.operator,
        )
    return -value if expression.operator == "-" else value


def _binary(expression: BinaryOperation, row: _Row, context: _Context) -> object:
    """Return the value of an infix operation, short-circuiting the logical ones."""
    operator = expression.operator
    if operator in ("AND", "OR"):
        return _logical(operator, expression, row, context)
    left = _evaluate(expression.left, row, context)
    right = _evaluate(expression.right, row, context)
    if operator == "XOR":
        left_truth, right_truth = _truth(left), _truth(right)
        if left_truth is None or right_truth is None:
            return None
        return left_truth != right_truth
    if operator == "=":
        return None if left is None or right is None else _equal(left, right)
    if operator == "<>":
        return None if left is None or right is None else not _equal(left, right)
    if operator in ("<", "<=", ">", ">="):
        return _ordered(operator, left, right)
    if operator == "IN":
        return _membership(left, right)
    if operator in ("STARTS WITH", "ENDS WITH", "CONTAINS"):
        return _text(operator, left, right)
    return _arithmetic(operator, left, right)


def _logical(
    operator: str, expression: BinaryOperation, row: _Row, context: _Context
) -> object:
    """Return the value of AND or OR, evaluating the right side only when it can matter."""
    left = _truth(_evaluate(expression.left, row, context))
    if operator == "AND" and left is False:
        return False
    if operator == "OR" and left is True:
        return True
    right = _truth(_evaluate(expression.right, row, context))
    if operator == "AND":
        if right is False:
            return False
        return None if left is None or right is None else (left and right)
    if right is True:
        return True
    return None if left is None or right is None else (left or right)


def _ordered(operator: str, left: object, right: object) -> object:
    """Return an ordering comparison, which is unknown across kinds that have no order."""
    if left is None or right is None or not _comparable(left, right):
        return None
    if operator == "<":
        return left < right  # type: ignore[operator]
    if operator == "<=":
        return left <= right  # type: ignore[operator]
    if operator == ">":
        return left > right  # type: ignore[operator]
    return left >= right  # type: ignore[operator]


def _membership(left: object, right: object) -> object:
    """Return whether a value appears in a list, which is unknown when either side is null."""
    if right is None:
        return None
    if isinstance(right, (str, bytes, bytearray)) or not isinstance(right, Sequence):
        raise GrafxPlanError(
            f"IN looks inside a list; got {type(right).__name__}.",
            field="operator",
            value="IN",
        )
    if left is None:
        return None
    found = False
    unknown = False
    for element in right:
        if element is None:
            unknown = True
            continue
        if _equal(left, element):
            found = True
    if found:
        return True
    return None if unknown else False


def _text(operator: str, left: object, right: object) -> object:
    """Return a string comparison, which is unknown unless both sides are strings."""
    if left is None or right is None:
        return None
    if not isinstance(left, str) or not isinstance(right, str):
        return None
    if operator == "STARTS WITH":
        return left.startswith(right)
    if operator == "ENDS WITH":
        return left.endswith(right)
    return right in left


def _arithmetic(operator: str, left: object, right: object) -> object:
    """Return the value of an arithmetic operation, refusing one that has no meaning."""
    if left is None or right is None:
        return None
    if operator == "+" and isinstance(left, str) and isinstance(right, str):
        return left + right
    if not _numbers(left, right):
        raise GrafxPlanError(
            f"The operator {operator!r} applies to numbers; got {type(left).__name__} and "
            f"{type(right).__name__}.",
            field="operator",
            value=operator,
        )
    if operator == "+":
        return left + right
    if operator == "-":
        return left - right
    if operator == "*":
        return left * right
    if operator in ("/", "%"):
        if right == 0:
            raise GrafxPlanError(
                f"The operator {operator!r} divides by zero in this query.",
                field="operator",
                value=operator,
            )
        if isinstance(left, int) and isinstance(right, int):
            # Integer division truncates towards zero in this dialect, which is what the
            # reference engine does and is NOT what Python's floor division does for a negative
            # operand: -7 // 2 is -4 there and -3 here.
            magnitude = abs(left) // abs(right)
            signed = -magnitude if (left < 0) != (right < 0) else magnitude
            return signed if operator == "/" else left - signed * right
        return left / right if operator == "/" else left % right
    if operator == "^":
        return float(left) ** float(right)
    raise GrafxPlanError(
        f"The operator {operator!r} has no meaning in this dialect.",
        field="operator",
        value=operator,
    )


def _coalesce_value_type(expression: FunctionCall, value: object) -> ValueType:
    """Return one runtime scalar type with COALESCE's public error taxonomy."""
    try:
        return value_type_of(value)  # type: ignore[arg-type]
    except GrafxError as failure:
        message = (
            f"{expression.name} accepts nulls, strings, booleans and numbers; got "
            f"{type(value).__name__}."
        )
        raise GrafxPlanError(
            message,
            field="function",
            value=expression.name,
        ) from failure


def _validate_parameter_maps(parameters: Mapping[str, object]) -> None:
    """Refuse case-insensitive map-key collisions deterministically during binding."""
    for name in sorted(parameters):
        _validate_parameter_map_value(parameters[name], parameter=name, seen=set())


def _validate_parameter_map_value(
    value: object, *, parameter: str, seen: set[int]
) -> None:
    """Validate every nested map carried by one referenced parameter."""
    if not isinstance(value, (Mapping, list, tuple)):
        return
    marker = id(value)
    if marker in seen:
        return
    seen.add(marker)
    if isinstance(value, Mapping):
        by_folded: dict[str, list[str]] = {}
        for key in value:
            if isinstance(key, str):
                by_folded.setdefault(key.lower(), []).append(key)
        collisions = tuple(
            sorted(
                (folded, tuple(sorted(keys)))
                for folded, keys in by_folded.items()
                if len(keys) > 1
            )
        )
        if collisions:
            keys = ", ".join(repr(key) for key in collisions[0][1])
            message = (
                f"Parameter ${parameter} contains map keys {keys} that collide "
                "case-insensitively."
            )
            raise GrafxPlanError(
                message,
                field="parameter",
                value=parameter,
            )
        ordered = sorted(
            value.items(), key=lambda item: (type(item[0]).__name__, repr(item[0]))
        )
        for _key, item in ordered:
            _validate_parameter_map_value(item, parameter=parameter, seen=seen)
        return
    for item in value:
        _validate_parameter_map_value(item, parameter=parameter, seen=seen)


def _bound_value_type(
    expression: Expression, value: object, *, owner: str
) -> ValueType:
    """Return a parameter-derived value type with the query binder's error taxonomy."""
    if isinstance(value, Mapping):
        return ValueType.MAP
    try:
        return value_type_of(value)  # type: ignore[arg-type]
    except GrafxError as failure:
        message = f"{owner} cannot use a value of type {type(value).__name__}."
        parameter = next(
            (node.name for node in walk(expression) if isinstance(node, Parameter)),
            expression.describe(),
        )
        raise GrafxPlanError(
            message,
            field="parameter",
            value=parameter,
        ) from failure


def _binder_resolvable(expression: Expression) -> bool:
    """Whether ``_bound_postfix_value`` can evaluate this expression during binding.

    Deliberately narrower than "contains no Variable": a call such as
    ``string_split('a,b', ',')[1]`` is row-independent yet outside the binder's postfix
    vocabulary, so asking it to evaluate one would turn a valid query into a bind-time
    refusal.  This predicate lists exactly the shapes that function handles.
    """

    if isinstance(expression, (Literal, Parameter)):
        return True
    if isinstance(expression, ListExpression):
        return all(_binder_resolvable(element) for element in expression.elements)
    if isinstance(expression, MapExpression):
        return all(_binder_resolvable(entry.value) for entry in expression.entries)
    if isinstance(expression, Property):
        return _binder_resolvable(expression.subject)
    if isinstance(expression, Subscript):
        return _binder_resolvable(expression.subject) and _binder_resolvable(
            expression.index
        )
    return False


def _static_postfix_target_expression(expression: Expression) -> Expression | None:
    """Resolve the expression selected by an exact written map/list postfix chain.

    This is a type proof, not evaluation. Selecting ``{v: $p + 1}.v`` only needs to type the
    chosen binary expression; materialising the whole map would unnecessarily require the small
    bind-time postfix evaluator to execute every scalar expression in it. Following one selected
    path also preserves sharing in a WITH-derived typing DAG.
    """
    if isinstance(expression, Property):
        subject = _static_postfix_target_expression(expression.subject)
        if not isinstance(subject, MapExpression):
            return None
        return subject.entry(expression.key)
    if isinstance(expression, Subscript):
        subject = _static_postfix_target_expression(expression.subject)
        if not isinstance(subject, ListExpression):
            return None
        index = expression.index
        if not isinstance(index, Literal):
            return None
        value = index.value
        if isinstance(value, bool) or not isinstance(value, int) or value == 0:
            return None
        offset = value - 1 if value > 0 else value
        if not -len(subject.elements) <= offset < len(subject.elements):
            return None
        return subject.elements[offset]
    return expression


def _bound_postfix_value(
    expression: Expression, parameters: Mapping[str, object], *, owner: str
) -> object:
    """Evaluate a row-independent parameter/list/map postfix chain during binding."""
    if isinstance(expression, Literal):
        return expression.value
    if isinstance(expression, Parameter):
        return parameters[expression.name]
    if isinstance(expression, ListExpression):
        return tuple(
            _bound_postfix_value(element, parameters, owner=owner)
            for element in expression.elements
        )
    if isinstance(expression, MapExpression):
        return {
            entry.key: _bound_postfix_value(entry.value, parameters, owner=owner)
            for entry in expression.entries
        }
    if isinstance(expression, Property):
        subject = _bound_postfix_value(expression.subject, parameters, owner=owner)
        if subject is None:
            return None
        if isinstance(subject, Mapping):
            return _map_property_value(subject, expression)
    elif isinstance(expression, Subscript):
        subject = _bound_postfix_value(expression.subject, parameters, owner=owner)
        index = _bound_postfix_value(expression.index, parameters, owner=owner)
        return _subscript_value(expression, subject, index)
    message = (
        f"The binder cannot evaluate the row-independent postfix value "
        f"{expression.describe()} in {owner}."
    )
    raise GrafxPlanError(
        message,
        field="expression",
        value=expression.describe(),
    )


def _bound_pulse_expression_type(
    expression: Expression,
    static_types: Mapping[int, ValueType | None],
    parameters: Mapping[str, object],
    *,
    owner: str,
    _resolved: dict[int, ValueType | None] | None = None,
) -> ValueType | None:
    """Resolve one expression once per type proof, preserving shared typing DAGs."""
    resolved = {} if _resolved is None else _resolved
    marker = id(expression)
    if marker in resolved:
        return resolved[marker]
    value_type = _infer_bound_pulse_expression_type(
        expression,
        static_types,
        parameters,
        owner=owner,
        resolved=resolved,
    )
    resolved[marker] = value_type
    return value_type


def _infer_bound_pulse_expression_type(
    expression: Expression,
    static_types: Mapping[int, ValueType | None],
    parameters: Mapping[str, object],
    *,
    owner: str,
    resolved: dict[int, ValueType | None],
) -> ValueType | None:
    """Resolve and validate one CASE/subscript expression before rows are produced."""
    static_type = static_types.get(id(expression))
    if isinstance(expression, Literal):
        return value_type_of(expression.value)
    if isinstance(expression, Parameter):
        return _bound_value_type(expression, parameters[expression.name], owner=owner)
    if isinstance(expression, Property):
        selected = _static_postfix_target_expression(expression)
        if selected is not None and selected is not expression:
            return _bound_pulse_expression_type(
                selected,
                static_types,
                parameters,
                owner=owner,
                _resolved=resolved,
            )
        if any(isinstance(node, Variable) for node in walk(expression.subject)):
            return static_type
        value = _bound_postfix_value(expression, parameters, owner=owner)
        return _bound_value_type(expression, value, owner=owner)
    if isinstance(expression, NullCheck):
        _bound_pulse_expression_type(
            expression.operand,
            static_types,
            parameters,
            owner=owner,
            _resolved=resolved,
        )
        return ValueType.BOOL
    if isinstance(expression, UnaryOperation):
        operand_type = _bound_pulse_expression_type(
            expression.operand,
            static_types,
            parameters,
            owner=owner,
            _resolved=resolved,
        )
        if expression.operator == "NOT":
            return ValueType.BOOL
        if operand_type in (ValueType.NULL, ValueType.INT64, ValueType.DOUBLE):
            return operand_type
        if operand_type is None:
            return static_type
        message = f"A sign applies to a number; got {operand_type.name}."
        raise GrafxPlanError(
            message,
            field="operator",
            value=expression.operator,
        )
    if isinstance(expression, BinaryOperation):
        left = _bound_pulse_expression_type(
            expression.left,
            static_types,
            parameters,
            owner=owner,
            _resolved=resolved,
        )
        right = _bound_pulse_expression_type(
            expression.right,
            static_types,
            parameters,
            owner=owner,
            _resolved=resolved,
        )
        if expression.operator in (
            "AND",
            "OR",
            "XOR",
            "=",
            "<>",
            "<",
            "<=",
            ">",
            ">=",
            "STARTS WITH",
            "ENDS WITH",
            "CONTAINS",
        ):
            return ValueType.BOOL
        if expression.operator == "IN":
            if right not in (None, ValueType.NULL, ValueType.LIST):
                message = f"IN looks inside a list; got {right.name}."
                raise GrafxPlanError(
                    message,
                    field="operator",
                    value=expression.operator,
                )
            return ValueType.BOOL
        if left is None or right is None:
            return static_type
        if ValueType.NULL in (left, right):
            return ValueType.NULL
        if (
            expression.operator == "+"
            and left is ValueType.STRING
            and right is ValueType.STRING
        ):
            return ValueType.STRING
        if left in (ValueType.INT64, ValueType.DOUBLE) and right in (
            ValueType.INT64,
            ValueType.DOUBLE,
        ):
            if expression.operator == "^" or ValueType.DOUBLE in (left, right):
                return ValueType.DOUBLE
            return ValueType.INT64
        message = (
            f"The operator {expression.operator!r} applies to numbers; got "
            f"{left.name} and {right.name}."
        )
        raise GrafxPlanError(
            message,
            field="operator",
            value=expression.operator,
        )
    if isinstance(expression, FunctionCall):
        name = expression.name.upper()
        argument_types = tuple(
            _bound_pulse_expression_type(
                argument,
                static_types,
                parameters,
                owner=owner,
                _resolved=resolved,
            )
            for argument in expression.arguments
        )
        if name == COALESCE_FUNCTION:
            result_type = coalesce_result_type(expression.name, argument_types)
            if result_type is None and all(
                value_type is ValueType.NULL for value_type in argument_types
            ):
                return ValueType.NULL
            return result_type
        if name == STRING_SPLIT_FUNCTION:
            wrong = tuple(
                value_type
                for value_type in argument_types
                if value_type not in (None, ValueType.NULL, ValueType.STRING)
            )
            if wrong:
                names = ", ".join(sorted({value_type.name for value_type in wrong}))
                message = f"{expression.name} takes strings; got {names}."
                raise GrafxPlanError(
                    message,
                    field="function",
                    value=expression.name,
                )
            return ValueType.LIST
        if name == TIMESTAMP_FUNCTION:
            argument_type = argument_types[0]
            if argument_type not in (
                None,
                ValueType.NULL,
                ValueType.STRING,
                ValueType.TIMESTAMP,
            ):
                message = (
                    f"{expression.name} reads an ISO-8601 string or a timestamp; got "
                    f"{argument_type.name}."
                )
                raise GrafxPlanError(
                    message,
                    field="function",
                    value=expression.name,
                )
            return ValueType.TIMESTAMP
        if name == LABEL_FUNCTION:
            argument_type = argument_types[0]
            if argument_type not in (None, ValueType.NULL):
                # A parameter or literal reached the binder with a scalar value, and a scalar
                # never came from a matched row.  Refusing here keeps the answer independent
                # of whether the pattern went on to match anything.
                message = (
                    f"{expression.name} reads the table of a matched node or relationship; "
                    f"got {argument_type.name}."
                )
                raise GrafxPlanError(
                    message,
                    field="function",
                    value=expression.name,
                )
            return ValueType.STRING
        if name == SIZE_FUNCTION:
            argument_type = argument_types[0]
            if argument_type not in (
                None,
                ValueType.NULL,
                ValueType.STRING,
                ValueType.LIST,
            ):
                message = (
                    f"{expression.name} measures a string or list; got "
                    f"{argument_type.name}."
                )
                raise GrafxPlanError(
                    message,
                    field="function",
                    value=expression.name,
                )
            return ValueType.INT64
        if name == "COUNT":
            return ValueType.INT64
        if name in ("AVG", "SUM"):
            return ValueType.DOUBLE
        if name in ("MIN", "MAX"):
            return argument_types[0]
        if name == "COLLECT":
            return ValueType.LIST
        return static_type
    if isinstance(expression, ListExpression):
        for element in expression.elements:
            _bound_pulse_expression_type(
                element,
                static_types,
                parameters,
                owner=owner,
                _resolved=resolved,
            )
        return ValueType.LIST
    if isinstance(expression, MapExpression):
        for entry in expression.entries:
            _bound_pulse_expression_type(
                entry.value,
                static_types,
                parameters,
                owner=owner,
                _resolved=resolved,
            )
        return ValueType.MAP
    if isinstance(expression, Subscript):
        subject_type = _bound_pulse_expression_type(
            expression.subject,
            static_types,
            parameters,
            owner=owner,
            _resolved=resolved,
        )
        index_type = _bound_pulse_expression_type(
            expression.index,
            static_types,
            parameters,
            owner=owner,
            _resolved=resolved,
        )
        subscript_argument_types(expression, subject_type, index_type)
        # Only shapes the binder can actually evaluate are materialized here.  Absence of a
        # Variable is not the same question: string_split('a,b', ',')[1] has none and is still
        # outside the postfix vocabulary, so the old guard sent it to _bound_postfix_value and
        # a valid query was refused at bind time whenever the plan carried no rows.
        if _binder_resolvable(expression):
            value = _bound_postfix_value(expression, parameters, owner=owner)
            return _bound_value_type(expression, value, owner=owner)
        if static_type is not None:
            return static_type
        return _bound_list_element_type(
            expression.subject,
            static_types,
            parameters,
            owner=owner,
            resolved=resolved,
        )
    if isinstance(expression, CaseExpression):
        compared = (
            tuple(alternative.condition for alternative in expression.alternatives)
            if expression.operand is None
            else (
                expression.operand,
                *(alternative.condition for alternative in expression.alternatives),
            )
        )
        comparison_types = tuple(
            _bound_pulse_expression_type(
                item,
                static_types,
                parameters,
                owner=expression.describe(),
                _resolved=resolved,
            )
            for item in compared
        )
        case_comparison_type(expression, comparison_types)
        result_types = tuple(
            _bound_pulse_expression_type(
                item,
                static_types,
                parameters,
                owner=expression.describe(),
                _resolved=resolved,
            )
            for item in expression.result_expressions()
        )
        result_type = case_result_type(expression, result_types)
        if result_type is None and all(
            value_type is ValueType.NULL for value_type in result_types
        ):
            return ValueType.NULL
        return result_type
    return static_type


def _bound_list_element_type(
    subject: Expression,
    static_types: Mapping[int, ValueType | None],
    parameters: Mapping[str, object],
    *,
    owner: str,
    resolved: dict[int, ValueType | None],
) -> ValueType | None:
    """Return the common element type of a bound written/parameter list, if knowable."""
    element_types: tuple[ValueType | None, ...]
    if isinstance(subject, ListExpression):
        element_types = tuple(
            _bound_pulse_expression_type(
                element,
                static_types,
                parameters,
                owner=owner,
                _resolved=resolved,
            )
            for element in subject.elements
        )
    elif isinstance(subject, (Literal, Parameter)):
        value = (
            subject.value if isinstance(subject, Literal) else parameters[subject.name]
        )
        if not isinstance(value, (list, tuple)):
            return None
        element_types = tuple(
            _bound_value_type(subject, element, owner=owner) for element in value
        )
    else:
        return None
    if not element_types or any(value_type is None for value_type in element_types):
        return None
    common = element_types[0]
    assert common is not None  # narrowed by the guard above
    for value_type in element_types[1:]:
        assert value_type is not None  # narrowed by the guard above
        common, _normalise = union_common_type(0, common, value_type)
    return common


def _required_bound_expression_type(
    expression: Expression,
    static_types: Mapping[int, ValueType | None],
    parameters: Mapping[str, object],
    *,
    owner: str,
) -> ValueType:
    """Return a fully bound scalar type, refusing metadata that still depends on a row."""
    value_type = _bound_pulse_expression_type(
        expression, static_types, parameters, owner=owner
    )
    if value_type is not None:
        return value_type
    message = (
        f"The plan left the type of {expression.describe()} in {owner} unresolved."
    )
    raise GrafxPlanError(
        message,
        field="expression",
        value=expression.describe(),
    )


def _bound_union_columns(
    plan: PlannedQuery, parameters: Mapping[str, object]
) -> tuple[bool, ...]:
    """Prove the two branches agree about every column, and say which ones must widen.

    Run once, before the first row, because a pair that disagrees about a column disagrees
    whether or not anything matched: discovering it mid-stream would mean a caller had already
    read rows from a result that was never coherent.

    The compatibility rule lives in :func:`union_common_type`, so planning and bound parameters
    cannot drift: an identical pair keeps its type, NULL takes the other side's, and INT64 beside
    DOUBLE widens to DOUBLE. Anything else is refused -- notably BOOL beside INT64, which some
    dialects treat as one family and this one does not.
    """
    static_types = {
        id(expression): value_type
        for expression, value_type in plan.pulse_expression_types
    }
    if plan.union_columns and len(plan.union_unwind_sources) != 2:
        raise GrafxPlanError(
            "A UNION plan carries the UNWIND source metadata of exactly two branches.",
            field="plan",
            value="union_unwind_sources",
        )
    unwind_carriers = tuple(
        None
        if unwind is None
        else _bound_unwind_carrier(
            unwind[1],
            parameters,
            owner="a UNION branch",
        )
        for unwind in plan.union_unwind_sources
    )
    coercions: list[bool] = []
    for position, left, _left_type, right, _right_type in plan.union_columns:
        owner = f"column {position + 1} of a UNION"
        # The same door the rest of the engine uses to turn a planned type into a bound one,
        # and the same refusal when the bind cannot finish it. Asking it here is what makes the
        # parameter case work without this function knowing anything about parameters.
        resolved: list[ValueType] = []
        for expression, unwind, carrier in zip(
            (left, right),
            plan.union_unwind_sources,
            unwind_carriers,
            strict=True,
        ):
            resolved.append(
                _required_bound_union_branch_type(
                    expression,
                    static_types,
                    parameters,
                    unwind=unwind,
                    carrier=carrier,
                    owner=owner,
                )
            )
        first, second = resolved
        _common, normalise_double = union_common_type(position, first, second)
        coercions.append(normalise_double)
    return tuple(coercions)


def _bound_unwind_carrier(
    source: Expression,
    parameters: Mapping[str, object],
    *,
    owner: str,
) -> tuple[object, ...] | None:
    """Materialise the finite binder vocabulary for one UNWIND source, validating its shape."""
    if not _binder_resolvable(source):
        # A row-independent literal/function shape may already have a static type in the plan.
        # This helper materialises only the binder's deliberately finite postfix vocabulary.
        return None
    carrier = _bound_postfix_value(source, parameters, owner=owner)
    if not isinstance(carrier, (list, tuple)):
        named = "null" if carrier is None else type(carrier).__name__
        raise GrafxPlanError(
            f"UNWIND reads a list or tuple; got {named}.",
            field="unwind",
            value=named,
        )
    return tuple(carrier)


def _required_bound_union_branch_type(
    expression: Expression,
    static_types: Mapping[int, ValueType | None],
    parameters: Mapping[str, object],
    *,
    unwind: tuple[str, Expression] | None,
    carrier: tuple[object, ...] | None,
    owner: str,
) -> ValueType:
    """Resolve one branch column, using each UNWIND element only when the column reads it."""
    if unwind is None or carrier is None:
        return _required_bound_expression_type(
            expression,
            static_types,
            parameters,
            owner=owner,
        )
    alias, _source = unwind
    if not _union_output_depends_on_alias(expression, alias, static_types):
        return _required_bound_expression_type(
            expression,
            static_types,
            parameters,
            owner=owner,
        )
    if not carrier:
        # An invariant outer type such as ``x IS NULL`` remains provable without a row.  A bare
        # ``x`` stays unresolved and therefore fail-closed, since an empty carrier supplies no
        # evidence about the column's type.
        return _required_bound_expression_type(
            expression,
            static_types,
            parameters,
            owner=owner,
        )

    output_types: list[ValueType] = []
    for element in carrier:
        typed_expression = _replace_bound_unwind_alias(
            expression,
            alias=alias,
            element=element,
        )
        output_types.append(
            _required_bound_expression_type(
                typed_expression,
                static_types,
                parameters,
                owner=owner,
            )
        )
    common = output_types[0]
    for value_type in output_types[1:]:
        common, _normalise = union_common_type(0, common, value_type)
    return common


def _union_output_depends_on_alias(
    expression: Expression,
    alias: str,
    static_types: Mapping[int, ValueType | None],
) -> bool:
    """Whether this column's ValueType still needs the runtime UNWIND element."""
    static_type = static_types.get(id(expression))
    if isinstance(expression, Variable):
        return expression.name == alias and static_type is None
    if isinstance(expression, (Property, Subscript)):
        return static_type is None and any(
            isinstance(node, Variable) and node.name == alias
            for node in walk(expression.subject)
        )
    if isinstance(expression, NullCheck):
        return False
    if isinstance(expression, UnaryOperation):
        return expression.operator != "NOT" and _union_output_depends_on_alias(
            expression.operand,
            alias,
            static_types,
        )
    if isinstance(expression, BinaryOperation):
        invariant = expression.operator in (
            "AND",
            "OR",
            "XOR",
            "=",
            "<>",
            "<",
            "<=",
            ">",
            ">=",
            "IN",
            "STARTS WITH",
            "ENDS WITH",
            "CONTAINS",
        )
        return not invariant and (
            _union_output_depends_on_alias(expression.left, alias, static_types)
            or _union_output_depends_on_alias(expression.right, alias, static_types)
        )
    if isinstance(expression, (ListExpression, MapExpression)):
        return False
    if isinstance(expression, CaseExpression):
        return any(
            _union_output_depends_on_alias(result, alias, static_types)
            for result in expression.result_expressions()
        )
    if isinstance(expression, FunctionCall):
        fixed = expression.name.upper() in (
            STRING_SPLIT_FUNCTION,
            LABEL_FUNCTION,
            TIMESTAMP_FUNCTION,
            SIZE_FUNCTION,
            SIMILARITY_FUNCTION,
            SIMILARITY_SCORE_FUNCTION,
            "COUNT",
            "AVG",
            "SUM",
            "COLLECT",
        )
        return not fixed and any(
            _union_output_depends_on_alias(child, alias, static_types)
            for child in expression.children()
        )
    return False


def _replace_bound_unwind_alias(
    expression: Expression,
    *,
    alias: str,
    element: object,
) -> Expression:
    """Substitute one UNWIND element into a non-executable branch typing expression."""
    if isinstance(expression, Variable):
        return Literal(value=element) if expression.name == alias else expression

    def substituted(child: Expression) -> Expression:
        """Substitute the bound UNWIND alias recursively in one child."""
        return _replace_bound_unwind_alias(child, alias=alias, element=element)

    if isinstance(expression, Property):
        subject = substituted(expression.subject)
        return (
            expression
            if subject is expression.subject
            else replace(expression, subject=subject)
        )
    if isinstance(expression, UnaryOperation):
        operand = substituted(expression.operand)
        return (
            expression
            if operand is expression.operand
            else replace(expression, operand=operand)
        )
    if isinstance(expression, BinaryOperation):
        left = substituted(expression.left)
        right = substituted(expression.right)
        if left is expression.left and right is expression.right:
            return expression
        return replace(expression, left=left, right=right)
    if isinstance(expression, NullCheck):
        operand = substituted(expression.operand)
        return (
            expression
            if operand is expression.operand
            else replace(expression, operand=operand)
        )
    if isinstance(expression, Subscript):
        subject = substituted(expression.subject)
        index = substituted(expression.index)
        if subject is expression.subject and index is expression.index:
            return expression
        return replace(expression, subject=subject, index=index)
    if isinstance(expression, FunctionCall):
        arguments = tuple(substituted(argument) for argument in expression.arguments)
        named_arguments = tuple(
            argument
            if (value := substituted(argument.value)) is argument.value
            else replace(argument, value=value)
            for argument in expression.named_arguments
        )
        if all(
            new is old for new, old in zip(arguments, expression.arguments, strict=True)
        ) and all(
            new is old
            for new, old in zip(
                named_arguments, expression.named_arguments, strict=True
            )
        ):
            return expression
        return replace(
            expression,
            arguments=arguments,
            named_arguments=named_arguments,
        )
    if isinstance(expression, ListExpression):
        elements = tuple(substituted(item) for item in expression.elements)
        if all(
            new is old for new, old in zip(elements, expression.elements, strict=True)
        ):
            return expression
        return replace(expression, elements=elements)
    if isinstance(expression, MapExpression):
        entries = tuple(
            entry
            if (value := substituted(entry.value)) is entry.value
            else replace(entry, value=value)
            for entry in expression.entries
        )
        if all(
            new is old for new, old in zip(entries, expression.entries, strict=True)
        ):
            return expression
        return replace(expression, entries=entries)
    if isinstance(expression, CaseExpression):
        operand = (
            None if expression.operand is None else substituted(expression.operand)
        )
        alternatives = []
        for alternative in expression.alternatives:
            condition = substituted(alternative.condition)
            result = substituted(alternative.result)
            alternatives.append(
                alternative
                if condition is alternative.condition and result is alternative.result
                else replace(alternative, condition=condition, result=result)
            )
        fallback = (
            None if expression.fallback is None else substituted(expression.fallback)
        )
        if (
            operand is expression.operand
            and fallback is expression.fallback
            and all(
                new is old
                for new, old in zip(alternatives, expression.alternatives, strict=True)
            )
        ):
            return expression
        return replace(
            expression,
            operand=operand,
            alternatives=tuple(alternatives),
            fallback=fallback,
        )
    return expression


def _bound_case_types(
    plan: PlannedQuery, parameters: Mapping[str, object]
) -> dict[int, ValueType | None]:
    """Resolve CASE comparands and result arms before the first row is read."""
    comparisons = {
        id(expression): planned_types
        for expression, planned_types in plan.case_comparison_types
    }
    static_types = {
        id(expression): value_type
        for expression, value_type in plan.pulse_expression_types
    }
    resolved: dict[int, ValueType | None] = {}
    for expression, planned_results in plan.case_result_types:
        compared = (
            tuple(alternative.condition for alternative in expression.alternatives)
            if expression.operand is None
            else (
                expression.operand,
                *(alternative.condition for alternative in expression.alternatives),
            )
        )
        marker = id(expression)
        planned_comparisons = comparisons.get(marker)
        if planned_comparisons is None or len(planned_comparisons) != len(compared):
            message = "A CASE plan must carry every comparison type."
            raise GrafxPlanError(
                message,
                field="plan",
                value=expression.describe(),
            )
        comparison_types = tuple(
            _required_bound_expression_type(
                item,
                static_types,
                parameters,
                owner=expression.describe(),
            )
            for item in compared
        )
        case_comparison_type(expression, comparison_types)
        results = expression.result_expressions()
        if len(planned_results) != len(results):
            message = "A CASE plan must carry every result-arm type."
            raise GrafxPlanError(
                message,
                field="plan",
                value=expression.describe(),
            )
        result_types = tuple(
            _required_bound_expression_type(
                item,
                static_types,
                parameters,
                owner=expression.describe(),
            )
            for item in results
        )
        resolved[marker] = case_result_type(expression, result_types)
    if set(comparisons) != set(resolved):
        message = (
            "A CASE plan carries comparison metadata without matching result metadata."
        )
        raise GrafxPlanError(
            message,
            field="plan",
            value="case_types",
        )
    return resolved


def _validate_bound_subscript_types(
    plan: PlannedQuery, parameters: Mapping[str, object]
) -> None:
    """Complete list and index parameter types before the first row is read."""
    static_types = {
        id(expression): value_type
        for expression, value_type in plan.pulse_expression_types
    }
    for expression, planned_types in plan.subscript_types:
        subject_type = _required_bound_expression_type(
            expression.subject,
            static_types,
            parameters,
            owner=expression.describe(),
        )
        index_type = _required_bound_expression_type(
            expression.index,
            static_types,
            parameters,
            owner=expression.describe(),
        )
        subscript_argument_types(expression, subject_type, index_type)
        if _binder_resolvable(expression):
            # Evaluating it here is what turns "zero rows" back into a refusal: the position
            # and the list are both bound, so an out-of-range subscript is already wrong
            # whether or not the pattern matches anything.  The value is discarded; only the
            # refusal inside _subscript_value matters.
            _bound_postfix_value(expression, parameters, owner=expression.describe())


_TIMESTAMP_EPOCH_ORDINAL = 719_163
_TIMESTAMP_MAX_ORDINAL = 3_652_059
_TIMESTAMP_MICROS_PER_SECOND = 1_000_000
_TIMESTAMP_MICROS_PER_MINUTE = 60 * _TIMESTAMP_MICROS_PER_SECOND
_TIMESTAMP_MICROS_PER_HOUR = 60 * _TIMESTAMP_MICROS_PER_MINUTE
_TIMESTAMP_MICROS_PER_DAY = 24 * _TIMESTAMP_MICROS_PER_HOUR
_TIMESTAMP_DAYS_BEFORE_MONTH = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
_TIMESTAMP_FRACTION_SCALES = (1_000_000, 100_000, 10_000, 1_000, 100, 10, 1)


def _timestamp_ascii_integer(text: str, start: int, length: int) -> int | None:
    """Read one fixed-width ASCII integer without accepting Unicode lookalikes."""

    end = start + length
    if end > len(text):
        return None
    answer = 0
    for index in range(start, end):
        digit = ord(text[index]) - ord("0")
        if digit < 0 or digit > 9:
            return None
        answer = answer * 10 + digit
    return answer


def _timestamp_is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _timestamp_calendar_ordinal(year: int, month: int, day: int) -> int | None:
    """Return the proleptic-Gregorian ordinal for one valid calendar date."""

    if year < 1 or year > 9_999 or month < 1 or month > 12 or day < 1:
        return None
    month_days = (
        29
        if month == 2 and _timestamp_is_leap_year(year)
        else (28 if month == 2 else (30 if month in (4, 6, 9, 11) else 31))
    )
    if day > month_days:
        return None
    years = year - 1
    ordinal = (
        years * 365
        + years // 4
        - years // 100
        + years // 400
        + _TIMESTAMP_DAYS_BEFORE_MONTH[month - 1]
        + day
    )
    if month > 2 and _timestamp_is_leap_year(year):
        ordinal += 1
    return ordinal


def _timestamp_iso_week_ordinal(year: int, week: int, weekday: int) -> int | None:
    """Return the calendar ordinal for one valid ISO week date."""

    if year < 1 or year > 9_999 or week < 1 or weekday < 1 or weekday > 7:
        return None
    january_first = _timestamp_calendar_ordinal(year, 1, 1)
    january_fourth = _timestamp_calendar_ordinal(year, 1, 4)
    if january_first is None or january_fourth is None:
        return None
    january_first_weekday = (january_first - 1) % 7
    weeks = (
        53
        if january_first_weekday == 3
        or (january_first_weekday == 2 and _timestamp_is_leap_year(year))
        else 52
    )
    if week > weeks:
        return None
    week_one_monday = january_fourth - (january_fourth - 1) % 7
    ordinal = week_one_monday + (week - 1) * 7 + weekday - 1
    if ordinal < 1 or ordinal > _TIMESTAMP_MAX_ORDINAL:
        return None
    return ordinal


def _timestamp_date_ordinal(text: str) -> int | None:
    """Read the finite calendar-date and ISO-week forms accepted by Grafx."""

    year = _timestamp_ascii_integer(text, 0, 4)
    if year is None:
        return None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        month = _timestamp_ascii_integer(text, 5, 2)
        day = _timestamp_ascii_integer(text, 8, 2)
        if month is None or day is None:
            return None
        return _timestamp_calendar_ordinal(year, month, day)
    if len(text) == 8 and text[4] not in ("W", "-"):
        month = _timestamp_ascii_integer(text, 4, 2)
        day = _timestamp_ascii_integer(text, 6, 2)
        if month is None or day is None:
            return None
        return _timestamp_calendar_ordinal(year, month, day)

    week: int | None = None
    weekday = 1
    if len(text) in (7, 8) and text[4] == "W":
        week = _timestamp_ascii_integer(text, 5, 2)
        if len(text) == 8:
            parsed_weekday = _timestamp_ascii_integer(text, 7, 1)
            if parsed_weekday is None:
                return None
            weekday = parsed_weekday
    elif len(text) == 8 and text[4:6] == "-W":
        week = _timestamp_ascii_integer(text, 6, 2)
    elif len(text) == 10 and text[4:6] == "-W" and text[8] == "-":
        week = _timestamp_ascii_integer(text, 6, 2)
        parsed_weekday = _timestamp_ascii_integer(text, 9, 1)
        if parsed_weekday is None:
            return None
        weekday = parsed_weekday
    if week is None:
        return None
    return _timestamp_iso_week_ordinal(year, week, weekday)


def _timestamp_fraction_micros(text: str, start: int) -> int | None:
    """Read a non-empty decimal fraction, truncating deterministically to microseconds."""

    if start == len(text):
        return None
    micros = 0
    used = 0
    for index in range(start, len(text)):
        digit = ord(text[index]) - ord("0")
        if digit < 0 or digit > 9:
            return None
        if used < 6:
            micros = micros * 10 + digit
            used += 1
    return micros * _TIMESTAMP_FRACTION_SCALES[used]


def _timestamp_clock_parts(
    text: str, *, allow_empty_fraction: bool = False
) -> tuple[int, int, int, int] | None:
    """Read the shared local-time/offset clock grammar."""

    fraction_at: int | None = None
    for index, character in enumerate(text):
        if character in (".", ","):
            fraction_at = index
            break
    base = text if fraction_at is None else text[:fraction_at]
    if fraction_at is None:
        fraction = 0
    elif allow_empty_fraction and fraction_at + 1 == len(text):
        fraction = 0
    else:
        fraction = _timestamp_fraction_micros(text, fraction_at + 1)
    if fraction is None:
        return None

    hour = _timestamp_ascii_integer(base, 0, 2)
    minute: int | None = 0
    second: int | None = 0
    if len(base) == 2:
        pass
    elif len(base) == 4:
        minute = _timestamp_ascii_integer(base, 2, 2)
    elif len(base) == 5 and base[2] == ":":
        minute = _timestamp_ascii_integer(base, 3, 2)
    elif len(base) == 6:
        minute = _timestamp_ascii_integer(base, 2, 2)
        second = _timestamp_ascii_integer(base, 4, 2)
    elif len(base) == 8 and base[2] == ":" and base[5] == ":":
        minute = _timestamp_ascii_integer(base, 3, 2)
        second = _timestamp_ascii_integer(base, 6, 2)
    else:
        return None
    if hour is None or minute is None or second is None:
        return None
    return hour, minute, second, fraction


def _timestamp_time_micros(text: str) -> int | None:
    """Read one time and optional zone as UTC-relative microseconds within its date."""

    zone_at: int | None = None
    for index, character in enumerate(text):
        if character in ("Z", "+", "-"):
            zone_at = index
            break
    local_text = text if zone_at is None else text[:zone_at]
    local = _timestamp_clock_parts(local_text, allow_empty_fraction=zone_at is not None)
    if local is None:
        return None
    hour, minute, second, fraction = local
    if hour > 23 or minute > 59 or second > 59:
        return None
    local_micros = (
        hour * _TIMESTAMP_MICROS_PER_HOUR
        + minute * _TIMESTAMP_MICROS_PER_MINUTE
        + second * _TIMESTAMP_MICROS_PER_SECOND
        + fraction
    )
    if zone_at is None:
        return local_micros

    zone_mark = text[zone_at]
    zone_text = text[zone_at + 1 :]
    if zone_mark == "Z":
        return local_micros if not zone_text else None
    offset = _timestamp_clock_parts(zone_text)
    if offset is None:
        return None
    offset_hour, offset_minute, offset_second, offset_fraction = offset
    integer_offset_seconds = offset_hour * 3_600 + offset_minute * 60 + offset_second
    # Keep CPython's effective zero-offset rule: a fractional remainder is discarded when
    # every whole offset component is zero (for example +00:00:00.5 is UTC).
    if integer_offset_seconds == 0:
        offset_fraction = 0
    offset_micros = (
        integer_offset_seconds * _TIMESTAMP_MICROS_PER_SECOND + offset_fraction
    )
    if offset_micros >= _TIMESTAMP_MICROS_PER_DAY:
        return None
    return (
        local_micros - offset_micros
        if zone_mark == "+"
        else local_micros + offset_micros
    )


def _timestamp_text_micros(text: str) -> int | None:
    """Read Grafx's deterministic ISO/Gregorian subset without mechanism imports."""

    # The old reader let this one CPython form through before its ten-character separator
    # guard: compact ISO week + one arbitrary separator + hours. Keep that effective surface
    # so restoring the pure boundary is not also a parsing break.
    if len(text) == 10:
        ordinal = _timestamp_date_ordinal(text[:7])
        time_micros = _timestamp_time_micros(text[8:])
        if ordinal is not None and time_micros is not None:
            return (
                ordinal - _TIMESTAMP_EPOCH_ORDINAL
            ) * _TIMESTAMP_MICROS_PER_DAY + time_micros
    if len(text) <= 10:
        ordinal = _timestamp_date_ordinal(text)
        return (
            None
            if ordinal is None
            else (ordinal - _TIMESTAMP_EPOCH_ORDINAL) * _TIMESTAMP_MICROS_PER_DAY
        )
    if text[10] not in ("T", " "):
        return None
    ordinal = _timestamp_date_ordinal(text[:10])
    time_micros = _timestamp_time_micros(text[11:])
    if ordinal is None or time_micros is None:
        return None
    return (
        ordinal - _TIMESTAMP_EPOCH_ORDINAL
    ) * _TIMESTAMP_MICROS_PER_DAY + time_micros


def _timestamp_of(function_name: str, value: object) -> Timestamp | None:
    """Read one ISO-8601 instant, or refuse it.

    The single place the conversion lives, so the binder and the evaluator cannot answer
    differently for the same text. The parser uses only integer Gregorian/ISO-week
    arithmetic: it neither depends on machine time nor crosses the pure-core boundary.
    """

    if value is None:
        return None
    if isinstance(value, Timestamp):
        return value
    if not isinstance(value, str):
        # A number is refused rather than read as an epoch: there would be no way to tell a
        # count of microseconds from a count of seconds, and guessing wrong is silent.
        message = (
            f"{function_name} reads an ISO-8601 string or a timestamp; got "
            f"{type(value).__name__}."
        )
        raise GrafxPlanError(
            message,
            field="function",
            value=function_name,
        )
    micros = _timestamp_text_micros(value)
    if micros is None:
        message = f"{function_name} could not read {value!r} as an ISO-8601 instant."
        raise GrafxPlanError(
            message,
            field="function",
            value=function_name,
        )
    return Timestamp(micros=micros)


def _timestamp_bindable(expression: Expression) -> bool:
    """Whether a scalar can be evaluated before rows without stealing aggregate work.

    Merely lacking a ``Variable`` is not enough: aggregates such as ``min($value)`` read the
    grouped values carried by ``_Row.computed``.  This vocabulary is intentionally local to
    timestamp binding and contains only scalar shapes whose complete value is already known.
    """

    if _binder_resolvable(expression):
        return True
    if isinstance(expression, NullCheck):
        return _timestamp_bindable(expression.operand)
    if isinstance(expression, UnaryOperation):
        return _timestamp_bindable(expression.operand)
    if isinstance(expression, BinaryOperation):
        return _timestamp_bindable(expression.left) and _timestamp_bindable(
            expression.right
        )
    if isinstance(expression, ListExpression):
        return all(_timestamp_bindable(item) for item in expression.elements)
    if isinstance(expression, MapExpression):
        return all(_timestamp_bindable(entry.value) for entry in expression.entries)
    if isinstance(expression, Property):
        return _timestamp_bindable(expression.subject)
    if isinstance(expression, Subscript):
        return _timestamp_bindable(expression.subject) and _timestamp_bindable(
            expression.index
        )
    if isinstance(expression, FunctionCall):
        return expression.name.upper() in (
            COALESCE_FUNCTION,
            STRING_SPLIT_FUNCTION,
        ) and all(_timestamp_bindable(argument) for argument in expression.arguments)
    if isinstance(expression, CaseExpression):
        compared = (
            tuple(alternative.condition for alternative in expression.alternatives)
            if expression.operand is None
            else (
                expression.operand,
                *(alternative.condition for alternative in expression.alternatives),
            )
        )
        results = expression.result_expressions()
        return all(_timestamp_bindable(item) for item in (*compared, *results))
    return False


def _bind_timestamp_values(plan: PlannedQuery, context: _Context) -> None:
    """Read every timestamp() argument the call has made knowable, before the stream.

    An unreadable instant is wrong whether or not the pattern matches, and a write must not
    stage anything on its way to finding out, so the conversion happens here and its refusal
    with it.  Keeping the result means a row does not re-parse text that cannot have changed.
    """

    empty = _Row(bindings={})
    for call in plan.timestamp_calls:
        argument = call.arguments[0]
        if not _timestamp_bindable(argument):
            continue
        context.timestamp_values[call] = _timestamp_of(
            call.name, _evaluate(argument, empty, context)
        )


def _validate_bound_label_arguments(
    plan: PlannedQuery, parameters: Mapping[str, object]
) -> None:
    """Finish the label() arguments the planner had to leave to the call.

    A parameter can never carry a matched row, so once its value is known the answer is
    either null or a refusal.  Deciding it here means an empty match cannot swallow the
    refusal, which is the rule the subscript range check already follows.
    """

    for call in plan.label_calls:
        argument = call.arguments[0]
        if not _binder_resolvable(argument):
            continue
        value = _bound_postfix_value(argument, parameters, owner=call.name)
        if value is None:
            continue
        message = (
            f"{call.name} reads the table of a matched node or relationship; got "
            f"{type(value).__name__}."
        )
        raise GrafxPlanError(
            message,
            field="function",
            value=call.name,
        )


def _bound_coalesce_types(
    plan: PlannedQuery, parameters: Mapping[str, object]
) -> dict[int, ValueType | None]:
    """Resolve parameter types and refuse incompatible COALESCE calls before row production."""
    resolved: dict[int, ValueType | None] = {}
    for expression, planned_types in plan.coalesce_argument_types:
        argument_types: list[ValueType | None] = []
        for argument, planned_type in zip(
            expression.arguments, planned_types, strict=True
        ):
            if planned_type is not None:
                argument_types.append(planned_type)
                continue
            if not isinstance(argument, Parameter):
                message = f"{expression.name} could not resolve the type of {argument.describe()}."
                raise GrafxPlanError(
                    message,
                    field="function",
                    value=expression.name,
                )
            argument_types.append(
                _coalesce_value_type(expression, parameters[argument.name])
            )
        resolved[id(expression)] = coalesce_result_type(expression.name, argument_types)
    return resolved


def _coalesce(expression: FunctionCall, row: _Row, context: _Context) -> object:
    """Return the first non-null scalar after eagerly evaluating every argument."""
    values = tuple(
        _evaluate(argument, row, context) for argument in expression.arguments
    )
    selected = next((value for value in values if value is not None), None)
    if selected is None:
        return None
    result_type = context.coalesce_types.get(id(expression))
    if result_type is None:
        runtime_types = tuple(
            _coalesce_value_type(expression, value) for value in values
        )
        result_type = coalesce_result_type(expression.name, runtime_types)
    if result_type is ValueType.DOUBLE:
        return float(selected)
    return selected


def _call(expression: FunctionCall, row: _Row, context: _Context) -> object:
    """Return the value of a function call: the score, or an aggregate already computed."""
    name = expression.name.upper()
    if name == COALESCE_FUNCTION:
        return _coalesce(expression, row, context)
    if name == STRING_SPLIT_FUNCTION:
        text = _evaluate(expression.arguments[0], row, context)
        separator = _evaluate(expression.arguments[1], row, context)
        if text is None or separator is None:
            return None
        if not isinstance(text, str) or not isinstance(separator, str):
            message = (
                f"{expression.name} takes a string and a string separator; got "
                f"{type(text).__name__} and {type(separator).__name__}."
            )
            raise GrafxPlanError(
                message,
                field="function",
                value=expression.name,
            )
        if not separator:
            if not text:
                message = f"{expression.name} cannot split an empty string with an empty separator."
                raise GrafxPlanError(
                    message,
                    field="function",
                    value=expression.name,
                )
            return tuple(text)
        pieces = str.split(text, separator)
        if len(pieces) == 1:
            return tuple(pieces)
        trailing_empty = pieces[-1] == ""
        result = [piece for piece in pieces if piece]
        if trailing_empty:
            result.append("")
        return tuple(result)
    if name == TIMESTAMP_FUNCTION:
        if expression in context.timestamp_values:
            return context.timestamp_values[expression]
        return _timestamp_of(
            expression.name, _evaluate(expression.arguments[0], row, context)
        )
    if name == LABEL_FUNCTION:
        subject = _evaluate(expression.arguments[0], row, context)
        if subject is None:
            return None
        if not isinstance(subject, RowBinding):
            # The planner refuses everything it can see, so reaching here means the shape was
            # only knowable once the row arrived.  Refusing beats returning a plausible string.
            message = (
                f"{expression.name} reads the table of a matched node or relationship; got "
                f"{type(subject).__name__}."
            )
            raise GrafxPlanError(
                message,
                field="function",
                value=expression.name,
            )
        return subject.table.name
    if name == SIZE_FUNCTION:
        value = _evaluate(expression.arguments[0], row, context)
        if value is None:
            return None
        if not isinstance(value, (str, list, tuple)):
            message = (
                f"{expression.name} measures a string or list; got "
                f"{type(value).__name__}."
            )
            raise GrafxPlanError(
                message,
                field="function",
                value=expression.name,
            )
        try:
            return len(value)
        except TypeError as exc:
            message = (
                f"{expression.name} could not measure this {type(value).__name__}."
            )
            raise GrafxPlanError(
                message,
                field="function",
                value=expression.name,
            ) from exc
    if name in (SIMILARITY_FUNCTION, SIMILARITY_SCORE_FUNCTION):
        score = row.bindings.get(SCORE_COLUMN)
        if score is None:
            raise GrafxPlanError(
                "The similarity score is read by a row the similarity operator did not "
                "produce.",
                field="function",
                value=expression.name,
            )
        return score
    if name in AGGREGATE_FUNCTIONS:
        raise GrafxPlanError(
            f"The aggregate {expression.name} is computed when the group is formed, and this "
            "row carries no group.",
            field="function",
            value=expression.name,
        )
    raise GrafxPlanError(
        f"There is no function named {expression.name!r} in this dialect.",
        field="function",
        value=expression.name,
    )


# --- value helpers ------------------------------------------------------------------------------


def _projected(row: _Row, columns: tuple[str, ...]) -> tuple[Value, ...]:
    """Return the projected values of one row, in column order."""
    values = row.columns if row.columns is not None else {}
    return tuple(_as_value(values.get(name)) for name in columns)


def _binding_identity(binding: RowBinding) -> tuple[object, ...]:
    """Return one owner-private logical identity for equality, dedupe and ordering."""
    reference = binding.ref
    if isinstance(reference, PendingRowRef):
        return ("pending", reference.txn_id, reference.table_id, reference.token)
    if reference is None and binding.record_id == 0:
        return ("held", binding.table.table_id, id(binding.version))
    return ("stored", binding.table.table_id, binding.record_id)


def _path_identity(context: _Context, binding: RowBinding) -> _PathIdentity:
    """Return a public-shaped opaque identity without exposing a pending reference.

    Durable identities already fit the query value domain in ordinary databases and remain
    stable across executions. A pending row has only a transaction-private negative token, and
    a theoretical durable u64 above the signed query domain cannot be published directly. Both
    receive a collision-free negative identity allocated from this execution's context. The
    allocation depends only on encounter order; it neither copies nor transforms the private
    token.
    """
    if (
        not isinstance(binding.ref, PendingRowRef)
        and 1 <= binding.record_id <= INT64_MAX
    ):
        offset = binding.record_id
    else:
        logical = _binding_identity(binding)
        offset = context.path_identities.get(logical)
        if offset is None:
            context.path_identities_issued += 1
            offset = INT64_MIN + context.path_identities_issued - 1
            context.path_identities[logical] = offset
    return _PathIdentity(offset=offset, table=binding.table.table_id)


def _path_properties(binding: RowBinding) -> tuple[tuple[str, Value], ...]:
    """Return every user property in schema order, padding an old short version with nulls."""
    first = ENDPOINT_COLUMN_COUNT if binding.table.kind == "rel" else 0
    values = binding.version.values
    return tuple(
        (
            column.name,
            values[position] if position < len(values) else None,
        )
        for position, column in enumerate(binding.table.columns)
        if position >= first
    )


def _one_hop_path_value(
    context: _Context,
    source: RowBinding,
    relationship: RowBinding,
    target: RowBinding,
) -> _PathValue:
    """Detach the exact visible hop into a nominal, capability-free result marker."""
    if source.table.kind != "node" or target.table.kind != "node":
        raise GrafxPlanError(
            "A projected path begins and ends at node tables.",
            field="path",
            value="non_node_endpoint",
        )
    if relationship.table.kind != "rel":
        raise GrafxPlanError(
            "A projected path carries a relationship table between its nodes.",
            field="path",
            value="non_relationship_hop",
        )
    if (
        relationship.table.from_table != source.table.name
        or relationship.table.to_table != target.table.name
    ):
        raise GrafxPlanError(
            "A projected outgoing path must preserve its relationship table's endpoints.",
            field="path",
            value=relationship.table.name,
        )

    source_identity = _path_identity(context, source)
    target_identity = _path_identity(context, target)
    relationship_identity = _path_identity(context, relationship)
    return _PathValue(
        nodes=(
            _PathNodeValue(
                identity=source_identity,
                label=source.table.name,
                properties=_path_properties(source),
            ),
            _PathNodeValue(
                identity=target_identity,
                label=target.table.name,
                properties=_path_properties(target),
            ),
        ),
        relationships=(
            _PathRelationshipValue(
                source=source_identity,
                target=target_identity,
                label=relationship.table.name,
                identity=relationship_identity,
                properties=_path_properties(relationship),
            ),
        ),
    )


def _as_value(value: object) -> Value:
    """Detach bindings recursively so no private pending reference reaches a result value.

    A node matched WITHOUT a label detaches as a map of its label and its properties. It has to
    carry the label: the caller asked across tables and the row alone would not say which one it
    came from. It carries no identity at all -- no record id, no reference, no version, no table
    id -- because those are owner-private and a caller who received one could do nothing correct
    with it. A node matched under a label keeps answering the identity it always answered; this
    path is additive rather than a change to what was already published.
    """
    if isinstance(value, RowBinding):
        if value.polymorphic:
            return {
                "label": value.table.name,
                "properties": {
                    column.name: _as_value(value.value(column.name))
                    for column in value.table.columns
                },
            }
        return value.record_id
    if type(value) is _PathValue:
        # The public result snapshot recognizes this exact nominal marker and rebuilds it into
        # ordinary maps and tuples after page access has ended.
        return value  # type: ignore[return-value]
    if isinstance(value, (list, tuple)):
        return tuple(_as_value(item) for item in value)
    if isinstance(value, dict):
        return {_as_value(key): _as_value(item) for key, item in value.items()}
    return value  # type: ignore[return-value]


def _freeze(value: object) -> object:
    """Return a hashable stand-in for one value, keyed on its KIND and its contents.

    The kind tag is what makes this usable for equality as well as for duplicates: ``1`` and
    ``"1"`` carry different tags and can never collide, while a dictionary and any other mapping
    of the same pairs carry the same tag and do. The scalar tag keeps the Python class name, so
    an integer and a double stay distinguishable for DISTINCT even though ``=`` treats them as
    one number -- those are two different questions and the numeric branch of :func:`_equal`
    answers the second one before it ever reaches here.
    """
    if isinstance(value, RowBinding):
        return ("binding", _binding_identity(value))
    if isinstance(value, (bytes, bytearray)):
        return ("bytes", bytes(value))
    if isinstance(value, (list, tuple)):
        return ("list", tuple(_freeze(item) for item in value))
    if isinstance(value, dict):
        return (
            "map",
            tuple(sorted((str(key), _freeze(item)) for key, item in value.items())),
        )
    return ("scalar", type(value).__name__, value)


def _require_optional_positive_limit(field: str, value: int | None) -> int | None:
    """Return an exact optional positive limit for direct engine composition."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GrafxConfigurationError(
            f"{field} must be a positive integer or None; got {value!r}.",
            field=field,
            value=repr(value),
        )
    return int(value)


def _sort_key(value: object) -> tuple[int, object]:
    """Return a total ordering key, so a sort over mixed kinds never raises and never varies.

    Ordering ACROSS kinds is decided by the rank alone, which is what makes the comparison total:
    two values of different kinds are never handed to an operator that has no meaning for them.
    A NaN sorts after every other number rather than inheriting Python's unordered comparison;
    equal NaNs remain stable, like every other equal key.
    Null sorts last ascending, which is the reference dialect's rule and puts it first when the
    direction is reversed.
    """
    if value is None:
        return (5, 0)
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        if isinstance(value, float) and isnan(value):
            return (1, (1, 0.0))
        return (1, (0, value))
    if isinstance(value, str):
        return (2, value)
    if isinstance(value, (bytes, bytearray)):
        return (3, bytes(value))
    if isinstance(value, RowBinding):
        return (4, _binding_identity(value))
    return (6, repr(value))


def _truth(value: object) -> bool | None:
    """Return the three-valued truth of one value: true, false, or unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return None


def _numbers(left: object, right: object) -> bool:
    """Return True when both operands are numbers the arithmetic operators accept.

    A boolean is not one. Python says ``True + 1`` is two and ``True < 2`` is true, and carrying
    that into the language would make ``true`` quietly arithmetic; here it is a condition and
    nothing else, so arithmetic over it is refused and ordering against a number is unknown.
    The exclusion is asked for by three callers and only one of them -- equality -- has a second
    guard of its own, so it is pinned through arithmetic and ordering rather than through the
    path that masks it (amendment A34).
    """
    return (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    )


def _comparable(left: object, right: object) -> bool:
    """Return True when two values have an ordering the language defines."""
    if _numbers(left, right):
        return True
    if isinstance(left, str) and isinstance(right, str):
        return True
    if isinstance(left, (bytes, bytearray)) and isinstance(right, (bytes, bytearray)):
        return True
    return isinstance(left, bool) and isinstance(right, bool)


def _equal(left: object, right: object) -> bool:
    """Return whether two values are the same VALUE of the language, not the same Python object.

    The distinction is load-bearing in both directions. A boolean is never a number here, and two
    numbers of different widths are the same number -- so ``true = 1`` is false and ``1 = 1.0``
    is true. Everything else is compared through :func:`_freeze`, which keys a value on the KIND
    the value system gives it rather than on the Python class carrying it, so a map supplied as
    an ordinary dictionary and one supplied as any other mapping are the same map, and a list
    literal and a list parameter are the same list. Comparing Python classes instead answered
    ``false`` for two spellings of one value, which is a wrong result a caller can reach with a
    parameter.

    Reusing ``_freeze`` is also what keeps ``=`` and ``DISTINCT`` from drifting: two rows that
    compare equal here are exactly the two rows DISTINCT collapses.
    """
    # There is deliberately no separate "a boolean is not a number" line here. Two mechanisms
    # already answer every pair it would have caught, and a third that no input can reach is a
    # guard nothing can test: with both of the others in place the A93 matrix showed this one
    # never decides an answer, and with both of them removed "true = 1" becomes true -- which is
    # what the exclusion in _numbers is pinned for, through arithmetic and ordering, where it is
    # the ONLY answer (amendments A34 and A67, LESSONS L1). A boolean against a number is
    # refused by that exclusion; a boolean against anything else carries a different kind tag in
    # _freeze. Proved over every pair of a spread of values: removing this line changed no
    # answer in 576 comparisons.
    if _numbers(left, right):
        return float(left) == float(right)
    if isinstance(left, RowBinding) or isinstance(right, RowBinding):
        return (
            isinstance(left, RowBinding)
            and isinstance(right, RowBinding)
            and _binding_identity(left) == _binding_identity(right)
        )
    return _freeze(left) == _freeze(right)
