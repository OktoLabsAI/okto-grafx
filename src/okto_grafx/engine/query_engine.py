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

**How the two index contracts are honoured.** An index seek asks
:meth:`~okto_grafx.engine.index_manager.IndexManager.lookup`, which is C7's own door and the one
place the dual rule of CONTRACT.md section 8.7 is applied: an EXACT hit is a candidate that is
validated against the heap under this snapshot before it is returned, a PROXIMITY hit is already
decided by its birth stamp and its tombstone. This engine deliberately does NOT reimplement that
choice. It records in the plan which contract the seek was built for, so a plan can be reviewed
for having assumed the wrong one, and then asks the component that owns the rule.

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

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
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
from okto_grafx.domain.ids import NO_CSN
from okto_grafx.domain.index.keys import index_key
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
    Value,
    VectorValue,
    encode_value,
    value_type_of,
)
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricsSink
from okto_grafx.domain.txn.context import RowIntent, RowOperation
from okto_grafx.domain.txn.intents import reduce_row_intents
from okto_grafx.domain.query.analysis import Aggregation, QueryAnalysis, analyze
from okto_grafx.domain.query.ast import (
    Direction,
    BinaryOperation,
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
    UnaryOperation,
    Variable,
)
from okto_grafx.domain.query.limits import (
    MAX_NAME_CHARACTERS,
    MAX_PROJECTION_ITEMS,
    MAX_RENDERED_QUERY_CHARACTERS,
)
from okto_grafx.domain.query.parser import parse as parse_text
from okto_grafx.domain.query.plan import (
    AggregateRows,
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
    PlanNode,
    ProduceResults,
    ProjectRows,
    PropertyAssignment,
    SetProperties,
    SingleRow,
    SkipRows,
    SortRows,
    TraverseRelationship,
    VectorSearch,
    validate_plan,
)
from okto_grafx.domain.query.planner import SCORE_COLUMN, PlannedQuery, build_plan
from okto_grafx.domain.query.tokens import (
    AGGREGATE_FUNCTIONS,
    SIMILARITY_FUNCTION,
    SIMILARITY_SCORE_FUNCTION,
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

    @property
    def record_id(self) -> int:
        """Return the stable identity of the row this binding names."""
        return self.version.record_id

    def value(self, key: str) -> Value:
        """Return one property of this row, refusing a column its table does not declare."""
        for position, column in enumerate(self.table.columns):
            if column.name == key:
                if position >= len(self.version.values):
                    return None
                return self.version.values[position]
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
    # The catalog this statement was PLANNED against. Inside a transaction that has declared
    # schema of its own, that is the transaction's working copy, and execution must resolve
    # tables and spaces from the same picture the planner did -- a row materialised for a table
    # whose vector space exists only in the working copy cannot ask the live catalog for it.
    catalog: Catalog | None = None
    result_node: PlanNode | None = None
    intermediate_rows: dict[int, int] = field(default_factory=dict)
    staged_rows: list[_HeldRow] = field(default_factory=list)
    staged_partitions: list[tuple[int, bytes]] = field(default_factory=list)
    tokens_issued: int = 0
    pending_tokens: dict[int, int] = field(default_factory=dict)
    ends_held: set[object] = field(default_factory=set)
    _ends_staged: frozenset[object] | None = None

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
        self.staged_rows.append(_HeldRow(_HELD_INSERT, table, values, identity, None, token))
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
        if not self.staged_rows:
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
                    transaction.stage_row_update(held.table, held.reference, held.values or ())
                else:
                    transaction.stage_row_delete(held.table, held.reference)
            if note_write is not None and partition_of is not None:
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
        "_txn_effects",
        "_page_stager",
        "_max_statement_writes",
        "_max_result_rows",
        "_max_intermediate_rows",
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
        max_statement_writes: int | None = None,
        max_result_rows: int | None = None,
        max_intermediate_rows: int | None = None,
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
        # Every out-of-transaction effect each open schema transaction has made -- indexes
        # registered, spaces attached, skip-report entries, index files created -- in the order
        # it made them. The rollback undo. Pruning by table id against the live catalog was
        # tried first and fails exactly when it matters: a loser's table and the winner's table
        # both allocated their ids from the same committed pages, so the loser's registrations
        # looked alive and the documented retry was then refused by its own predecessor's
        # leftovers.
        self._txn_effects: dict[int, list[tuple[str, str]]] = {}
        self._indexes = indexes
        self._vectors = vectors
        self._page_stager = page_stager
        self._max_statement_writes = _require_optional_positive_limit(
            "max_statement_writes", max_statement_writes
        )
        self._max_result_rows = _require_optional_positive_limit(
            "max_result_rows", max_result_rows
        )
        self._max_intermediate_rows = _require_optional_positive_limit(
            "max_intermediate_rows", max_intermediate_rows
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
        one declared.
        """
        if working is None:
            return self.planned(statement)
        started = self._reading()
        try:
            analysis = analyze(statement)
            plan = build_plan(
                statement,
                catalog=working,
                indexes=self._index_definitions(),
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
                indexes=self._index_definitions(),
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
        statement = self.parse(text)
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

    def __repr__(self) -> str:
        """Return a short representation naming which engines this one was given."""
        present = [
            name
            for name, value in (("indexes", self._indexes), ("vectors", self._vectors))
            if value is not None
        ]
        return f"QueryEngine(with={present})"

    # --- planning support ---------------------------------------------------------------------

    def _index_definitions(self) -> tuple[object, ...]:
        """Return the definitions of every registered index, or none without a framework."""
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
        return tuple(
            index.definition for index in listing() if not getattr(index, "stale", False)
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
        context = _Context(
            engine=self,
            txn=txn,
            parameters=parameters,
            analysis=plan.analysis,
            statistics=statistics,
            catalog=catalog,
            result_node=root.child if root.columns else None,
        )
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
        undo: list[tuple[str, str]] = []
        take_mark = getattr(txn, "staging_mark", None)
        discard = getattr(txn, "discard_since", None)
        settle = getattr(txn, "settle_staging_mark", None)
        mark = take_mark() if callable(take_mark) else None
        try:
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
            self._unwind_schema_statement(undo)
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
        undo: list[tuple[str, str]],
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
            self._attach_primary_key_index(installed, statistics, undo)
            self._attach_vector_columns(installed, statistics, catalog, undo)
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
            self._attach_endpoint_indexes(installed, statistics, undo)
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

    def _unwind_schema_statement(self, undo: list[tuple[str, str]]) -> None:
        """Reverse one refused schema statement's out-of-transaction effects, newest first.

        Runs while the refusal is already unwinding, so nothing here may raise. Each entry names
        exactly one thing this statement did -- an index registered, a space attached, a table
        added to the skip report -- so another open transaction's attachments are untouched.
        """
        for kind, name in reversed(undo):
            try:
                if kind == "index" and self._indexes is not None:
                    drop = getattr(self._indexes, "unregister", None)
                    if callable(drop):
                        drop(name)
                elif kind == "space":
                    detach = getattr(self._vectors, "detach", None)
                    if callable(detach):
                        detach(name)
                elif kind == "skip":
                    self._skipped_indexes.discard(name)
                elif kind == "file":
                    # The register CREATED this file, and left behind it refuses the retry: the
                    # successor's definition digest differs (its ids re-derive from pages the
                    # winner has since changed), and the vector attach has no decline guard by
                    # design. Frames are dropped UNWRITTEN first, or the next flush would
                    # re-create what was just removed.
                    storage = self._pool.storage
                    if storage.exists(name):
                        for page_index in range(storage.page_count(name)):
                            self._pool.discard(name, page_index)
                        storage.remove(name)
            except GrafxError:
                continue

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
        working = self._working.pop(txn_id, None)
        effects = self._txn_effects.pop(txn_id, None)
        if committed or working is None:
            return
        # The transaction's own journal, replayed in reverse -- never a prune against a catalog.
        # A catalog prune was the first shape of this and it failed exactly when it mattered: a
        # loser's table and the winner's allocate their ids from the same committed pages, so by
        # table id the loser's registrations looked alive, and by ANY catalog another open
        # transaction's registrations look dead. The journal names precisely what this
        # transaction did, and nothing else.
        self._unwind_schema_statement(effects or [])

    @property
    def skipped_indexes(self) -> tuple[str, ...]:
        """Return the tables whose primary-key index could not be created, in name order."""
        return tuple(sorted(self._skipped_indexes))

    def _attach_primary_key_index(
        self, table: TableDef, statistics: dict[str, int], undo: list[tuple[str, str]]
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
            existed = self._pool.storage.exists(index.file)
            self._indexes.register(
                index, complete_through=self._published_lsn_for_new_index()
            )
            undo.append(("index", index.definition.name))
            if not existed:
                undo.append(("file", index.file))
        except (GrafxIndexError, GrafxUnsupportedOperation):
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
            if table.name not in self._skipped_indexes:
                self._skipped_indexes.add(table.name)
                undo.append(("skip", table.name))
            return
        statistics["indexes_created"] = statistics.get("indexes_created", 0) + 1

    def _attach_endpoint_indexes(
        self, table: TableDef, statistics: dict[str, int], undo: list[tuple[str, str]]
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
                existed = self._pool.storage.exists(index.file)
                self._indexes.register(
                    index, complete_through=self._published_lsn_for_new_index()
                )
                undo.append(("index", index.definition.name))
                if not existed:
                    undo.append(("file", index.file))
        except (GrafxIndexError, GrafxUnsupportedOperation):
            statistics["indexes_skipped"] = statistics.get("indexes_skipped", 0) + 1
            if table.name not in self._skipped_indexes:
                self._skipped_indexes.add(table.name)
                undo.append(("skip", table.name))
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
        if callable(published):          # a composition whose manager exposes it as a method
            return int(published())
        return 0

    def _attach_vector_columns(
        self,
        table: TableDef,
        statistics: dict[str, int],
        catalog: Catalog | None = None,
        undo: list[tuple[str, str]] | None = None,
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
            attached = attach(table, space, catalog)
            if undo is not None:
                undo.append(("index", attached.definition.name))
                undo.append(("space", space))
                undo.append(("file", attached.file))
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


def _node_scan(
    engine: QueryEngine, node: NodeScan, context: _Context
) -> Iterator[_Row]:
    """Produce one row per visible version of the table, per incoming row."""
    snapshot = context.snapshot
    ended = _ended_by_this_transaction(context)
    for row in engine._rows(node.child, context):
        for ref, version in engine.heap.scan(node.table, snapshot):
            if ref in ended:
                # The snapshot still shows it -- its end stamp is the commit number, which does
                # not exist yet -- but this transaction has already said it wants the row gone.
                # Matching it again let a later bulk SET stage an update that the settle then
                # let win over the delete: an acknowledged delete silently lost (C10 round-2 B3).
                continue
            bindings = dict(row.bindings)
            bindings[node.variable] = RowBinding(
                variable=node.variable, table=node.table, ref=ref, version=version
            )
            context.count("rows_scanned")
            yield _Row(bindings=bindings)


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
    ended = _ended_by_this_transaction(context)
    for row in engine._rows(node.child, context):
        template: list[Value] = [None] * arity
        for position, expression in zip(positions, node.key_values):
            template[position] = _as_value(_evaluate(expression, row, context))
        key = index_key(template, positions)
        for ref in manager.lookup(node.index, key, snapshot):  # type: ignore[attr-defined]
            if ref in ended:
                continue  # ended by this transaction: the same rule the scan applies
            version = engine.heap.read(ref)
            bindings = dict(row.bindings)
            bindings[node.variable] = RowBinding(
                variable=node.variable, table=node.table, ref=ref, version=version
            )
            context.count("rows_seeked")
            yield _Row(bindings=bindings)


_EDGE_LOOKUP_FAN_LIMIT: int = 64
"""Distinct traversal start nodes served by index lookups before one grouped edge scan wins.

Chosen from the shape of the two costs, not tuned to a machine: a lookup costs a few bucket-page
reads however large the edge table is, and the grouped scan costs the whole edge table once.
Sixty-four lookups are well under one scan of any edge table large enough for the difference to
matter, and a frontier that crosses sixty-four distinct nodes is a whole-table walk, which is
what the scan is for."""


def _edge_steps(
    engine: QueryEngine,
    context: _Context,
    relationship: TableDef,
    from_table: TableDef,
    to_table: TableDef,
    outgoing: bool,
    incoming: bool,
    ended: frozenset[object] | set[object],
) -> Callable[[object], Iterator[tuple[object, HeapVersion, TableDef, object]]]:
    """Return the function a traversal expands one frontier node with.

    Two regimes, chosen once per traversal and the same answer from both.

    **By index**, when every direction the pattern walks has its endpoint index present, owned
    by this table, and FRESH. A stored relationship row leads with its endpoints, so "the edges
    leaving this node" is exactly the question the ``ef_``/``et_`` indexes answer, and
    ``IndexManager.lookup`` discharges section 8.7 on the way: every candidate is validated
    against the heap under this snapshot, so the hits are the edges the scan would have kept.
    Before this existed, a reverse hop into a well-referenced node of a 2500-node graph read all
    3600 edges and cost 1.46 s.

    **By one scan**, otherwise -- no framework, no index, or a STALE one, which is a subset of
    the heap and the one thing validation cannot repair. The scan is taken ONCE and grouped by
    endpoint, so a frontier of F nodes costs O(E), not the O(F x E) the old per-node rescan
    paid; slower than the index, and right, which is the same fallback rule the planner and the
    uniqueness check follow.
    """
    manager = engine._indexes
    lookup = getattr(manager, "lookup", None) if manager is not None else None

    def usable(name: str) -> str | None:
        """Return the name when that index is present, this table's own, and fresh."""
        try:
            index = manager.index(name)  # type: ignore[union-attr]
        except GrafxError:
            return None
        if index.definition.table_id != relationship.table_id:
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
    snapshot = context.snapshot

    def by_index(
        record_id: object,
    ) -> Iterator[tuple[object, HeapVersion, TableDef, object]]:
        """Yield the node's edges from the endpoint indexes, validated against the heap."""
        if outgoing:
            for ref in lookup(from_name, index_key((record_id, None), (0,)), snapshot):
                if ref in ended:
                    continue
                version = engine.heap.read(ref)
                yield ref, version, to_table, version.values[1]
        if incoming:
            for ref in lookup(to_name, index_key((None, record_id), (1,)), snapshot):
                if ref in ended:
                    continue
                version = engine.heap.read(ref)
                yield ref, version, from_table, version.values[0]

    maps: list[tuple[dict, dict]] = []

    def grouped() -> tuple[dict, dict]:
        """Build the by-endpoint edge maps ONCE, on the first caller that needs them."""
        if not maps:
            by_source: dict[object, list[tuple[object, HeapVersion]]] = {}
            by_target: dict[object, list[tuple[object, HeapVersion]]] = {}
            for ref, version in engine.heap.scan(relationship, snapshot):
                if ref in ended:
                    continue
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

    if not indexed:
        return by_scan

    # Indexed, WITH a fan limit -- and the limit is a cost model, not a hedge. One lookup
    # answers one frontier node, so a bounded frontier (a seek, a bound endpoint, a short
    # range) pays a handful of bucket probes and never reads the edge table. A frontier that
    # keeps growing -- a scan traversing every node of a table -- pays one lookup per node,
    # and past a point that costs more than reading the edges ONCE and grouping them. The
    # switch is by DISTINCT start nodes seen, so it is deterministic for a given plan and
    # data, and both regimes return the same tuples because the lookup validates against the
    # same snapshot the scan reads under.
    seen_starts: set[object] = set()

    def hybrid(
        record_id: object,
    ) -> Iterator[tuple[object, HeapVersion, TableDef, object]]:
        """Serve by index up to the fan limit, then by the grouped scan for good."""
        if not maps:
            seen_starts.add(record_id)
            if len(seen_starts) <= _EDGE_LOOKUP_FAN_LIMIT:
                yield from by_index(record_id)
                return
        yield from by_scan(record_id)

    return hybrid


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
    snapshot = context.snapshot
    catalog = context.schema()
    relationship = node.table
    from_table = catalog.table(relationship.from_table)
    to_table = catalog.table(relationship.to_table)
    nodes_by_id: dict[int, dict[int, tuple[object, HeapVersion]]] = {}

    ended = _ended_by_this_transaction(context)

    def node_at(table: TableDef, record_id: object) -> tuple[object, HeapVersion] | None:
        """Return the visible version of one node by identity, indexing each table once."""
        found = nodes_by_id.get(table.table_id)
        if found is None:
            found = {
                version.record_id: (ref, version)
                for ref, version in engine.heap.scan(table, snapshot)
                if ref not in ended  # a node this transaction ended is not a landing
            }
            nodes_by_id[table.table_id] = found
        return found.get(record_id)  # type: ignore[arg-type]

    outgoing = node.direction in (Direction.OUTGOING, Direction.UNDIRECTED)
    incoming = node.direction in (Direction.INCOMING, Direction.UNDIRECTED)
    steps = _edge_steps(
        engine, context, relationship, from_table, to_table, outgoing, incoming, ended
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
            (start.record_id, start.table, ())
        ]
        for depth in range(1, node.max_hops + 1):
            reached: list[tuple[object, TableDef, tuple[RowBinding, ...]]] = []
            for record_id, _table, path in frontier:
                taken = {edge.ref for edge in path}
                for ref, version, next_table, next_id in steps(record_id):
                    if ref in taken:
                        continue
                    if (
                        isinstance(bound_target, RowBinding)
                        and bound_target.record_id == next_id
                        and bound_target.table.table_id == next_table.table_id
                    ):
                        # The landing IS the bound row, which arrived through operators that
                        # already validated its visibility -- so the full-table scan node_at
                        # would take to re-prove it is not paid.
                        landing = (bound_target.ref, bound_target.version)
                    else:
                        landing = node_at(next_table, next_id)
                    if landing is None:
                        continue
                    edge = RowBinding(
                        variable=node.relationship or "", table=relationship,
                        ref=ref, version=version,
                    )
                    extended = (*path, edge)
                    reached.append((next_id, next_table, extended))
                    if depth < node.min_hops:
                        continue
                    landing_ref, landing_version = landing
                    if isinstance(bound_target, RowBinding) and (
                        bound_target.record_id != next_id
                        or bound_target.table.table_id != next_table.table_id
                    ):
                        continue
                    bindings = dict(row.bindings)
                    bindings[node.target] = RowBinding(
                        variable=node.target, table=next_table,
                        ref=landing_ref, version=landing_version,
                    )
                    if node.relationship is not None:
                        bindings[node.relationship] = (
                            edge if node.max_hops == 1 and node.min_hops == 1 else extended
                        )
                    context.count("rows_scanned")
                    yield _Row(bindings=bindings, computed=row.computed, columns=row.columns)
            frontier = reached
            if not frontier:
                break


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
        if threshold is not None and not _passes(node.threshold_operator, hit.score, threshold):
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
                "A reference vector holds numbers; got "
                f"{type(component).__name__}.",
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
        computed = {
            item.expression: value for item, value in zip(node.grouping, keys)
        }
        for aggregation in node.aggregations:
            accumulator = accumulators.get(aggregation.call)
            computed[aggregation.call] = (
                accumulator.result() if accumulator is not None else None
            )
        yield _Row(bindings={}, computed=computed)


class _Accumulator:
    """The running state of one aggregate over one group."""

    __slots__ = ("_aggregation", "_seen", "_values", "_count", "_total")

    def __init__(self, aggregation: Aggregation) -> None:
        self._aggregation = aggregation
        self._seen: set[object] = set()
        self._values: list[object] = []
        self._count = 0
        self._total: float = 0.0

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
            if frozen in self._seen:
                return
            self._seen.add(frozen)
        self._count += 1
        self._values.append(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self._total += float(value)

    def result(self) -> object:
        """Return what this aggregate reports for its group."""
        name = self._aggregation.function
        if name == "COUNT":
            return self._count
        if name == "COLLECT":
            return tuple(self._values)
        if not self._values:
            return None
        if name == "SUM":
            return self._total
        if name == "AVG":
            return self._total / self._count if self._count else None
        ordered = sorted(self._values, key=_sort_key)
        return ordered[0] if name == "MIN" else ordered[-1]


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
    makes the same query answer in the same order on every run.
    """
    rows = list(engine._rows(node.child, context))
    for key in reversed(node.keys):
        rows.sort(
            key=lambda row, key=key: _sort_key(_sort_value(key, row, context)),
            reverse=key.descending,
        )
    yield from rows


def _sort_value(key: SortItem, row: _Row, context: _Context) -> object:
    """Return the value one ORDER BY key reads from a row."""
    expression = key.expression
    columns = row.columns
    if isinstance(expression, Variable) and columns is not None and expression.name in columns:
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
        row_key = binding.ref if binding.ref is not None else ("pending", id(binding.version))
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
    engine: QueryEngine, context: _Context, binding: RowBinding, settled: tuple[Value, ...]
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
            _HeldRow(_HELD_INSERT, held.table, settled, held.identity, None, held.token),
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
    engine: QueryEngine, context: _Context, table: TableDef, record_id: object
) -> Iterator[tuple[TableDef, object, HeapVersion]]:
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
    for candidate in catalog.tables():
        if candidate.kind != "rel":
            continue
        leaves = str(candidate.from_table) == table.name
        lands = str(candidate.to_table) == table.name
        if not (leaves or lands):
            continue
        for ref, version in engine.heap.scan(candidate, snapshot):
            incident = (leaves and version.values[0] == record_id) or (
                lands and version.values[1] == record_id
            )
            if not incident:
                continue
            yield candidate, ref, version


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
                raise GrafxUnsupportedOperation(
                    f"DELETE names {variable!r}, a row created by an earlier statement of this "
                    f"transaction; a row's identity is allocated by the commit (W5b), so it "
                    f"cannot be ended before then. Commit first, then delete it.",
                    field="variable",
                    value=variable,
                )
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
            for edge_table, edge_ref, edge_version in _incident_edges(
                engine, context, binding.table, binding.record_id
            ):
                if context.already_ended(edge_ref):
                    continue
                context.note_ended(edge_ref)
                context.hold_delete(
                    edge_table, edge_ref, _partition_key(edge_table, edge_version.values)
                )
                context.count("rows_deleted")
        context.hold_delete(
            binding.table, binding.ref, _partition_key(binding.table, binding.version.values)
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
        values = materialise_row(engine, written.table, written.properties, row, context)
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
        context.hold(table, values, _partition_key(table, values), identity, token=token)
        context.count("rows_created")
    for table, values in edges:
        context.hold(table, values, _partition_key(table, values), None)
        context.count("relationships_created")
    return _Row(bindings=bindings, computed=row.computed, columns=row.columns)


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
    endpoints: list[int] = []
    for end, variable in (("source", edge.source), ("target", edge.target)):
        binding = bindings.get(variable)
        if not isinstance(binding, RowBinding):
            raise GrafxPlanError(
                f"The {end} of a {edge.table.name!r} edge is {variable!r}, which the rows "
                "reaching it do not carry.",
                field=end,
                value=variable,
            )
        endpoints.append(binding.record_id)
    properties = materialise_row(
        engine, edge.table, edge.properties, row, context, endpoints=(endpoints[0], endpoints[1])
    )
    engine.heap.require_endpoints(edge.table, properties, context.snapshot)
    return edge.table, properties


def _pending_binding(variable: str, table: TableDef, values: tuple[Value, ...]) -> RowBinding:
    """Return a binding for a row this statement staged but has not yet given an identity.

    The identity is zero because the commit allocates it, and zero is the value section 3
    reserves for "none" -- so a projection of a created row reads back the properties it was
    written with and never a number that looks like an identity and is not one.
    """
    return RowBinding(
        variable=variable,
        table=table,
        ref=None,
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
    context: _Context, table: TableDef
) -> tuple[dict[object, tuple[Value, ...] | None], list[tuple[Value, ...]]]:
    """Return what this transaction has done to each stored row of a table, in order, settled.

    Two things come back. ``state`` maps the reference of every stored row this transaction
    touched to the values it now holds -- or to None when the transaction ended it. ``inserted``
    lists the rows it created, including rows carrying a transaction-local pending reference but
    no stored row yet. Earlier statements' intents come
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
    inserted: list[tuple[Value, ...]] = []

    def note(operation: str, reference: object, values: object) -> None:
        """Fold one staged change into the view, in the order the transaction made it."""
        # Inserts can carry a PendingRowRef so later statements can identify them.  They are
        # nevertheless new logical rows, not heap-backed rows; classifying only by ``reference
        # is None`` would make matching code try to read the pending token from the heap.
        if operation == "INSERT" or reference is None:
            if values:
                inserted.append(tuple(values))  # type: ignore[arg-type]
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
    for intent in reduce_row_intents(logical_intents):
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
    for pending in inserted:
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
    if index.definition.table_id != table.table_id or index.definition.positions != (position,):
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

    An insert carries no reference. An update carries the reference of the version it replaces,
    which is what lets a key check tell "another row under this key" from "an earlier statement
    of this transaction updating the very row being updated again" -- the second is the ordinary
    shape of two SETs on one row and must not refuse itself.
    """
    state, inserted = _transaction_row_view(context, table)
    for values in inserted:
        yield None, values
    for reference, latest in state.items():
        if latest is not None:
            yield reference, latest


def _uncommitted_rows(context: _Context, table: TableDef) -> Iterator[tuple[Value, ...]]:
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
    state, _inserted = _transaction_row_view(context, binding.table)
    latest = state.get(binding.ref)
    return binding.version.values if latest is None else latest


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
    engine: QueryEngine, written: CreatedNode, values: tuple[Value, ...], context: _Context
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
    for pending in inserted:
        if len(pending) == len(values) and all(
            _equal(pending[at], values[at]) for at in positions
        ):
            return _pending_binding(written.variable, table, pending)
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
        positions.extend(table.column_index(entry.key) for entry in edge.properties.entries)
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
        return b"".join(
            encode_value(values[position]) for position in range(ENDPOINT_COLUMN_COUNT)
        )
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
    encode_tuple(table, materialised)
    return materialised


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
    NodeScan: _node_scan,  # type: ignore[dict-item]
    IndexSeek: _index_seek,  # type: ignore[dict-item]
    TraverseRelationship: _traverse,  # type: ignore[dict-item]
    FilterRows: _filter_rows,  # type: ignore[dict-item]
    VectorSearch: _vector_search,  # type: ignore[dict-item]
    AggregateRows: _aggregate_rows,  # type: ignore[dict-item]
    ProjectRows: _project_rows,  # type: ignore[dict-item]
    DistinctRows: _distinct_rows,  # type: ignore[dict-item]
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
        return tuple(_evaluate(element, row, context) for element in expression.elements)
    if isinstance(expression, MapExpression):
        return {entry.key: _evaluate(entry.value, row, context) for entry in expression.entries}
    if isinstance(expression, FunctionCall):
        return _call(expression, row, context)
    raise GrafxPlanError(
        f"An expression of type {type(expression).__name__} cannot be evaluated.",
        field="expression",
        value=type(expression).__name__,
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


def _call(expression: FunctionCall, row: _Row, context: _Context) -> object:
    """Return the value of a function call: the score, or an aggregate already computed."""
    name = expression.name.upper()
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


def _as_value(value: object) -> Value:
    """Return a value in the shape the value system stores, resolving a bound row to its id."""
    if isinstance(value, RowBinding):
        return value.record_id
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
        return ("binding", value.table.name, value.record_id)
    if isinstance(value, (bytes, bytearray)):
        return ("bytes", bytes(value))
    if isinstance(value, (list, tuple)):
        return ("list", tuple(_freeze(item) for item in value))
    if isinstance(value, dict):
        return ("map", tuple(sorted((str(key), _freeze(item)) for key, item in value.items())))
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
    Null sorts last ascending, which is the reference dialect's rule and puts it first when the
    direction is reversed.
    """
    if value is None:
        return (5, 0)
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (1, value)
    if isinstance(value, str):
        return (2, value)
    if isinstance(value, (bytes, bytearray)):
        return (3, bytes(value))
    if isinstance(value, RowBinding):
        return (4, (value.table.name, value.record_id))
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
            and left.table.name == right.table.name
            and left.record_id == right.record_id
        )
    return _freeze(left) == _freeze(right)
