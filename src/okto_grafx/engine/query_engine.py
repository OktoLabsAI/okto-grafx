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

**How the similarity operator obtains candidates.** A filtered, traversed or correlated CHILD is
materialised and handed to the vector subsystem as a
:class:`~okto_grafx.domain.vector.filter.RecordIdFilter`, so its real membership and cardinality
remain authoritative (SPEC-VEC FR-4, BR-6, AC-7).  A bounded whole-table node scan may instead use
the vector index as an end-to-end access path, but only at a page-0-certified snapshot frontier;
then each returned ``VectorHit.ref`` is revalidated against the heap and only K row payloads are
materialised by the query layer.  The exact vector oracle retains its own exhaustive validation;
this path removes the redundant child scan around it.  Historical, filtered, stale or otherwise
ambiguous cases retain the canonical CHILD path; an owner-dirty table keeps its pre-existing
fail-closed RYOW refusal.

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

from _thread import LockType
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from heapq import heappop, heappush
from types import MappingProxyType
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from math import isnan
from threading import Lock
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
from okto_grafx.domain.ids import NO_CSN, Lsn, RecordId, RecordRef
from okto_grafx.domain.index.catalog import (
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
    IndexGenerationState,
    identity_index_name,
)
from okto_grafx.domain.index.definition import (
    COLUMN_KEY_DERIVATION,
    ORDERED_KEY_DERIVATION,
    RECORD_ID_KEY_DERIVATION,
    IndexDefinition,
    automatic_index_definitions,
    index_definition_matches_table,
)
from okto_grafx.domain.index.keys import (
    custom_index_sizing,
    identity_index_sizing,
    index_key,
    record_id_key,
)
from okto_grafx.domain.index.layout import IndexLayout
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.engine.index_manager import (
    HashIndex,
    IndexManager,
    edge_from_index_name,
    edge_to_index_name,
    primary_key_index,
    primary_key_index_name,
    relationship_endpoint_indexes,
)
from okto_grafx.engine.heap_store import FIRST_RECORD_ID
from okto_grafx.engine.vector_engine import VectorEngine
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.record import HeapVersion
from okto_grafx.domain.model.schema import (
    ENDPOINT_COLUMNS,
    ENDPOINT_COLUMN_COUNT,
    ColumnDef,
    EmbeddingSpaceDef,
    TableDef,
    _UNMATERIALIZED_COLUMN,
    _encode_tuple_with_proof,
    _is_unmaterialized_column,
    encode_tuple,
)
from okto_grafx.domain.model.value import (
    INT64_MAX,
    INT64_MIN,
    VECTOR_VALUE_TYPES,
    Timestamp,
    Value,
    ValueType,
    VectorValue,
    decode_value,
    encode_value,
    value_type_of,
)
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricsSink
from okto_grafx.domain.ports.query_spill import (
    QuerySpillFactory,
    QuerySpillSorter,
    QuerySpillWorkspace,
)
from okto_grafx.domain.query.memory import LogicalMemoryBudget
from okto_grafx.domain.txn.context import (
    SIZING_ENDPOINT,
    PendingRowRef,
    RowIntent,
    RowOperation,
)
from okto_grafx.domain.txn.intents import reduce_row_intents
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.domain.wal.commit import partition_key
from okto_grafx.domain.query.analysis import Aggregation, QueryAnalysis, analyze
from okto_grafx.domain.query.ast import (
    Direction,
    BinaryOperation,
    CaseExpression,
    CreateClause,
    CreateIndexStatement,
    DeleteClause,
    Expression,
    FunctionCall,
    ListExpression,
    Literal,
    MapExpression,
    MatchClause,
    MergeClause,
    NodePattern,
    NullCheck,
    Parameter,
    PatternPath,
    Property,
    Query,
    RelationshipPattern,
    SetClause,
    SortItem,
    Statement,
    Subscript,
    UnaryOperation,
    UnionQuery,
    Variable,
    free_variables,
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
    CreateIndex,
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
    NodeMultiKeySeek,
    NodeScan,
    OrderedNodeMerge,
    OptionalRows,
    PlanNode,
    ProduceResults,
    ProjectRows,
    PropertyAssignment,
    RelationshipIncidentSeek,
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
    RELATIONSHIP_LOOKUP_FRONTIER_LIMIT,
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
from okto_grafx.domain.vector.filter import CandidateFilter, RecordIdFilter
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.domain.model.catalog import (
    CATALOG_FORMAT_VERSION,
    CATALOG_LEGACY_FORMAT_VERSION,
    Catalog,
)
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

# Parsed statements and prepared plans are immutable value graphs. These deliberately modest,
# process-local bounds make repeated statements cheap without letting caller-controlled query
# text become an unbounded retention surface. Bump the version after a planner/key semantic
# change so an older shape can never survive inside a long-lived engine.
_PARSE_CACHE_MAX_ENTRIES: int = 256
# One statement-authority memo per retained parsed statement; the parse cache bounds the
# statements, and this bound keeps the memo from outliving that cache by more than its size.
_STATEMENT_AUTHORITY_MEMO_MAX_ENTRIES: int = _PARSE_CACHE_MAX_ENTRIES
# Elements every IN parameter list of one statement may contribute, in total, to hashed
# membership memos: four full facade lists (MAX_LIST_ELEMENTS), where the Pulse graph page
# carries two of at most 500.  Beyond the ceiling the linear comparison stays the answer: the
# memo is an accelerator over the same _freeze keys, never a second semantics.
_IN_LIST_MEMO_MAX_TOTAL_ELEMENTS: int = 4_096
_PLAN_CACHE_MAX_ENTRIES: int = 128
_PREPARED_PLAN_VERSION: int = 1
_COMPILED_PREDICATE_MAX_ENTRIES: int = 256
"""Ceiling of the engine-local compiled WHERE predicates (EXEC-CSE).

A compiled predicate is a tree of closures derived from one plan expression -- no row, no
value, no snapshot, nothing a later statement could reuse as authority -- kept by the identity
of the expression object of a cached plan, so a plan executed again never compiles again.  The
oldest entry leaves when the ceiling is reached; the plan cache itself holds 128 plans.
"""

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
_OWNER_LANDING_TUPLE_SLOT_BYTES: int = 8
_OWNER_LANDING_SCALAR_BYTES: int = 40
_OWNER_LANDING_STRING_BASE_BYTES: int = 80
_OWNER_LANDING_STRING_CHAR_BYTES: int = 4
_OWNER_LANDING_BYTES_BASE_BYTES: int = 40
_OWNER_LANDING_OBJECT_BYTES: int = 96
_OWNER_LANDING_VECTOR_BASE_BYTES: int = 128
_OWNER_LANDING_VECTOR_COMPONENT_BYTES: int = 32
_VECTOR_FREE_CANONICAL_READ = HeapStore.read
_VECTOR_FREE_CANONICAL_DECODE = HeapStore._decode_version
_VECTOR_FREE_CANONICAL_VALIDATED = IndexManager.validated_versions
"""Shape tariff of one retained landing (KGRUN-M4).

Each constant dominates the CPython footprint of the object it meters: a scalar cell is one
reference plus a 24-28 byte int/float; a str is 49 + 1 x len (ASCII) up to 76 + 4 x len (UCS-4);
a vector is a tuple of floats (40 + 32 x n) inside a small slotted object.  The charge is a pure
function of the decoded values (their types, string lengths and vector dimension), so it is the
same on every platform and run, and it stays above the measured footprint (15.8 KiB real against
18.5 KiB charged for a 45-column row with a 384-float vector) without the 16 x payload rule, which
overcharged that row 2.7 x and starved the cache at 777 landings.
"""

# Repeated relationship layouts in one Pulse page resolve the same endpoint keys under the
# same transaction snapshot.  Keep only that exact validated result frontier, bounded within
# the decoded-landing budget.  The value is deliberately not configurable: it changes cost,
# never answers, and making it a knob would create a second operational contract before the
# workload has justified one.
_PRIMARY_KEY_RESOLUTION_MAX_ENTRIES: int = 4_096
_PRIMARY_KEY_RESOLUTION_ENTRY_BYTES: int = 512
_NATIVE_VALIDATED_VERSIONS_MANY = IndexManager.validated_versions_many
_NATIVE_VALIDATED_VERSIONS_MANY_REUSING = (
    IndexManager.validated_versions_many_reusing
)

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
class _WholeVectorTableFilter:
    """An all-record predicate carrying a certified regime-safe cardinality.

    A direct whole-table vector access path has no semantic membership filter: the versioned
    vector index and the snapshot decide visibility.  For a non-null vector column this is the
    cardinality the materialised ``NodeScan`` would have supplied.  For a nullable column it is
    used only above the exact threshold, where every possible count of additional NULL heap rows
    chooses the same approximate regime.
    """

    cardinality: int

    @staticmethod
    def admits(_record_id: RecordId) -> bool:
        """Admit every index record; table and visibility were certified separately."""
        return True


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
            value = self.version.values[position]
            if _is_unmaterialized_column(value):
                raise GrafxPlanError(
                    "An internal projected row was read outside its closed column plan.",
                    field="projection",
                    value=key,
                    table=self.table.name,
                    variable=self.variable,
                    position=position,
                )
            return value
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


@dataclass(slots=True)
class _OwnedPlanDoor:
    """One result's not-yet-materialised public plan: a compiled, capability-free clone recipe.

    Only :func:`_owned_query_result` installs a door, and only after the public facade proved that
    the exact engine owns the prepared plan root and compiled that root's clone recipe.  The public
    constructor refuses a door, so a collaborator cannot smuggle a callable into the hostile path.
    The first read of :attr:`QueryResult.plan` runs the recipe once, stores the independent tree in
    this result's own slot and drops the door.  A result whose plan is never read never clones one;
    two results never share a node, because every read of a door builds its own tree.  The lock
    belongs to this one door, so concurrent readers of the same result wait for the single run
    instead of each building a tree and racing for the slot.
    """

    clone: Callable[[], PlanNode]
    _lock: LockType = field(default_factory=Lock, init=False, repr=False, compare=False)
    _materialised: PlanNode | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def materialise(self) -> PlanNode:
        """Build this result's tree once, including when readers arrive concurrently."""

        materialised = self._materialised
        if materialised is not None:
            return materialised
        with self._lock:
            materialised = self._materialised
            if materialised is None:
                materialised = self.clone()
                self._materialised = materialised
            return materialised


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
        if type(_QUERY_RESULT_PLAN_SLOT.__get__(self, QueryResult)) is _OwnedPlanDoor:
            # The door is engine-private: it may only enter through _owned_query_result, which
            # never runs this constructor.  Refusing it here keeps the hostile publication path
            # free of any callable a collaborator could hand in as a plan.
            raise GrafxPlanError(
                "Query result plans must be operator trees; a sealed plan door is engine-private.",
                field="plan",
                value="sealed_door",
            )
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


_QUERY_RESULT_PLAN_SLOT = QueryResult.__dict__["plan"]
"""The physical ``plan`` slot; the class attribute below becomes the lazy door over it."""


def _read_query_result_plan(result: QueryResult) -> PlanNode | None:
    """Return this result's plan, materialising a sealed door exactly once and only for it."""
    stored = _QUERY_RESULT_PLAN_SLOT.__get__(result, QueryResult)
    if type(stored) is not _OwnedPlanDoor:
        return stored
    materialised = stored.materialise()
    _QUERY_RESULT_PLAN_SLOT.__set__(result, materialised)
    return materialised


def _write_query_result_plan(result: QueryResult, value: object) -> None:
    """Store a plan (or a door) in the physical slot; the frozen dataclass still refuses users."""
    _QUERY_RESULT_PLAN_SLOT.__set__(result, value)


# ``plan`` stays a dataclass field for fields()/replace()/__eq__/__repr__; only its descriptor
# changes, so every read -- including the base-class descriptor read the public facade performs
# -- goes through the door, and the frozen __setattr__ keeps refusing user assignment.
setattr(  # noqa: B010 - the slot descriptor is deliberately replaced after class creation
    QueryResult, "plan", property(_read_query_result_plan, _write_query_result_plan)
)


def _owned_query_result(
    *,
    columns: tuple[str, ...] = (),
    rows: tuple[tuple[Value, ...], ...] = (),
    plan: PlanNode | _OwnedPlanDoor | None = None,
    statistics: dict[str, int] | None = None,
) -> QueryResult:
    """Build a result whose exact fields were already validated and privately materialised.

    This is not a second public constructor.  The query engine calls it only with the output of
    its validated immutable plan, and the public boundary calls it only after rebuilding every
    collaborator field -- or, for a plan root the exact engine proved it owns, with a sealed
    :class:`_OwnedPlanDoor` that materialises this result's own tree on first read.  All other
    callers keep :class:`QueryResult`'s hostile validation.
    """
    result = object.__new__(QueryResult)
    object.__setattr__(result, "columns", columns)
    object.__setattr__(result, "rows", rows)
    object.__setattr__(result, "plan", plan)
    object.__setattr__(result, "statistics", {} if statistics is None else statistics)
    return result


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
    CreateIndex,
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
    encoding_proof: object = None


class _RevisionList(list[object]):
    """A list that distinguishes suffix appends from edits requiring a memo rebuild."""

    __slots__ = ("revision", "rewrite_revision")

    def __init__(self, values: Sequence[object] = ()) -> None:
        super().__init__(values)
        self.revision = 0
        self.rewrite_revision = 0

    def _appended(self) -> None:
        self.revision += 1

    def _rewritten(self) -> None:
        self.revision += 1
        self.rewrite_revision += 1

    def append(self, value: object) -> None:
        super().append(value)
        self._appended()

    def extend(self, values: object) -> None:
        before = len(self)
        try:
            super().extend(values)  # type: ignore[arg-type]
        finally:
            if len(self) != before:
                self._appended()

    def insert(self, index: int, value: object) -> None:
        super().insert(index, value)
        self._rewritten()

    def __setitem__(self, index: object, value: object) -> None:
        super().__setitem__(index, value)  # type: ignore[index,assignment]
        self._rewritten()

    def __delitem__(self, index: object) -> None:
        super().__delitem__(index)  # type: ignore[arg-type]
        self._rewritten()

    def __iadd__(self, values: object) -> _RevisionList:
        self.extend(values)
        return self

    def __imul__(self, count: int) -> _RevisionList:
        super().__imul__(count)
        self._rewritten()
        return self

    def pop(self, index: int = -1) -> object:
        value = super().pop(index)
        self._rewritten()
        return value

    def remove(self, value: object) -> None:
        super().remove(value)
        self._rewritten()

    def clear(self) -> None:
        if self:
            super().clear()
            self._rewritten()

    def reverse(self) -> None:
        super().reverse()
        self._rewritten()

    def sort(self, *args: object, **kwargs: object) -> None:
        try:
            super().sort(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            self._rewritten()


@dataclass(frozen=True, slots=True)
class _PrimaryKeyOutcome:
    """Latest reduced operation and values for one referenced transaction row."""

    operation: RowOperation
    values: tuple[Value, ...]


@dataclass(frozen=True, slots=True)
class _PrimaryKeyLegacyOwner:
    """Identity of one independent legacy insert that carries no pending reference."""

    position: int


@dataclass(slots=True)
class _PrimaryKeyFoldState:
    """Incremental row reduction and key occupancy for one table view."""

    position: int
    cursor: int = 0
    rewrite_revision: int = 0
    generation: int = 0
    fold_count: int = 0
    outcomes: dict[object, _PrimaryKeyOutcome] = field(default_factory=dict)
    cancelled: set[object] = field(default_factory=set)
    key_owners: dict[object, dict[object, Value]] = field(default_factory=dict)
    mutable_key_owners: dict[object, Value] = field(default_factory=dict)

    def reset(self, rewrite_revision: int) -> None:
        self.cursor = 0
        self.rewrite_revision = rewrite_revision
        self.generation += 1
        self.outcomes.clear()
        self.cancelled.clear()
        self.key_owners.clear()
        self.mutable_key_owners.clear()


@dataclass(slots=True)
class _PrimaryKeyTxnMemo:
    """All primary-key folds owned by one exact transaction and intent list."""

    txn: object
    intents: _RevisionList
    tables: dict[tuple[object, ...], _PrimaryKeyFoldState] = field(default_factory=dict)
    dirty_cursor: int = 0
    dirty_rewrite_revision: int = 0
    dirty_table_ids: set[int] = field(default_factory=set)
    dirty_snapshot: frozenset[int] = frozenset()
    row_intents_by_table: dict[int, list[RowIntent]] = field(default_factory=dict)
    row_intent_index_complete: bool = True
    has_delete_intent: bool = False
    resolutions: dict[tuple[object, ...], _PrimaryKeyResolutionCache] = field(
        default_factory=dict
    )
    resolution_entries: int = 0


@dataclass(slots=True)
class _PrimaryKeyResolutionCache:
    """Bounded exact-index answers owned by one transaction/snapshot/store generation."""

    store: object
    snapshot: object
    registry_revision: int
    heap_epoch: int
    generation: object
    entries: OrderedDict[
        bytes,
        tuple[tuple[tuple[RecordRef, HeapVersion], ...], int],
    ] = field(default_factory=OrderedDict)
    used_bytes: int = 0


@dataclass(slots=True)
class _PrimaryKeyStatementMemo:
    """Incremental overlay of held rows over one transaction table fold."""

    base_generation: int
    state: _PrimaryKeyFoldState
    changed_refs: set[object] = field(default_factory=set)


@dataclass(slots=True, frozen=True)
class _InListMemo:
    """Hashed picture of one detached IN parameter list, built at most once per statement.

    Only an exact tuple whose elements are exact ``str``, exact ``bytes`` or ``None`` is
    memoised: for those kinds ``_equal`` is nothing but ``_freeze`` key equality, so a set of
    the same keys answers exactly what the linear walk answers.  Numbers stay on the walk
    because ``1 = 1.0`` crosses Python types, and every other kind stays there too.  ``values``
    is the detached parameter object itself: an evaluation that meets a different object proves
    the memo stale instead of trusting the name.
    """

    values: tuple[Value, ...]
    keys: frozenset[object]
    has_null: bool


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
    staged_rows: list[_HeldRow] = field(default_factory=_RevisionList)  # type: ignore[arg-type]
    staged_partitions: list[tuple[int, bytes]] = field(default_factory=list)
    staged_reads: list[tuple[int, bytes]] = field(default_factory=list)
    tokens_issued: int = 0
    pending_tokens: dict[int, int] = field(default_factory=dict)
    cancelled_insert_tokens: set[int] = field(default_factory=set)
    ends_held: set[object] = field(default_factory=set)
    _ends_staged: frozenset[object] | None = None
    path_identities: dict[tuple[object, ...], int] = field(default_factory=dict)
    path_identities_issued: int = 0
    # The identity door chooses its access path once per complete table identity.  ``None`` is
    # a deliberate, statement-stable canonical fallback; a store value is the exact ACTIVE
    # generation this statement selected and must never be replaced by a quiet fallback later.
    endpoint_identity_indexes: dict[tuple[int, str], object | None] = field(
        default_factory=dict
    )
    # One immutable statement-local selection of the process-local stores authorized by the
    # exact catalog picture used for planning. Runtime operators resolve names from this same
    # projection instead of asking the catalog and registry to prove authority again.
    index_authority: _IndexAuthorityProjection | None = None
    primary_key_memos: dict[tuple[object, ...], _PrimaryKeyStatementMemo] = field(
        default_factory=dict
    )
    # Exact one-hop traversals beneath a streaming LIMIT may use their endpoint index even when
    # their source is a NodeScan. The set is derived once from the immutable physical plan; every
    # blocking or semantically wider shape is absent and keeps the canonical grouped scan.
    short_circuit_traversals: frozenset[int] = frozenset()
    # Exact, statement-local column sets for a closed single-source node scan. The outer key is
    # the physical scan node identity and the inner key is table_id (one table for NodeScan,
    # potentially many for AllNodesScan). Absence means the canonical full-row decoder.
    node_scan_projections: dict[int, dict[int, frozenset[int]]] = field(
        default_factory=dict
    )
    # A closed one-hop read may validate landing vectors without allocating them.
    # This is a per-operator proof, never a change to an index's authority.
    vector_free_landings: frozenset[int] = frozenset()
    # Hashed IN parameter lists keyed by parameter name.  ``None`` records a declined build, so
    # the linear walk is chosen once for that parameter rather than re-examined on every row.
    in_list_memos: dict[str, _InListMemo | None] = field(default_factory=dict)
    in_list_memo_elements: int = 0

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
        encoding_proof: object = None,
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
            _HeldRow(
                _HELD_INSERT,
                table,
                values,
                identity,
                None,
                token,
                encoding_proof,
            )
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
                    proved_stage = getattr(
                        transaction, "_stage_row_insert_with_encoding_proof", None
                    )
                    if held.encoding_proof is not None and callable(proved_stage):
                        proved_stage(
                            held.table,
                            held.values or (),
                            record_id=held.identity,
                            encoding_proof=held.encoding_proof,
                        )
                    else:
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


def _intent_table_ids(engine: QueryEngine, txn: object) -> frozenset[int]:
    """Return the tables whose indexes do not yet describe their owner's row view.

    Row intents are private to the transaction passed to :meth:`QueryEngine.execute`, so this
    set is owner-only by construction. Even an insert later cancelled by a delete keeps the
    table dirty until commit: planning from the raw intents preserves transaction budgets and
    avoids making plan safety depend on a second, planner-local reduction rule. The engine may
    replace the transaction's plain intent list with its revision-tracking subtype; existing
    intents are immutable, structural rewrites force a full rebuild, and append-only growth is
    scanned once. Repeated calls may therefore return the same immutable snapshot object.
    """
    identified = _revisioned_txn_memo(engine, txn)
    if identified is None:
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

    _txn_id, memo = identified
    _refresh_revisioned_intent_index(memo)
    return memo.dirty_snapshot


def _refresh_revisioned_intent_index(memo: _PrimaryKeyTxnMemo) -> None:
    """Index one transaction's append-only intent suffix by table and operation.

    ``_transaction_row_view`` used to rescan every intent in a transaction merely to discover
    that none belonged to the endpoint table being queried. Pulse relationship ingestion makes
    that question twice per relationship while the transaction history grows. The same
    revision-tracking list that protects the primary-key fold lets all consumers share one suffix
    walk. Structural edits rebuild from zero; unfamiliar entries keep row-view consumers on the
    canonical scan instead of silently changing their duck-typed behaviour.
    """
    intents = memo.intents
    if (
        memo.dirty_rewrite_revision != intents.rewrite_revision
        or memo.dirty_cursor > len(intents)
    ):
        memo.dirty_table_ids.clear()
        memo.row_intents_by_table.clear()
        memo.row_intent_index_complete = True
        memo.has_delete_intent = False
        memo.dirty_cursor = 0
        memo.dirty_rewrite_revision = intents.rewrite_revision
        memo.dirty_snapshot = frozenset()
    table_ids = memo.dirty_table_ids
    start = memo.dirty_cursor
    try:
        _walk_revisioned_intent_suffix(memo, intents, start, table_ids)
    except BaseException:
        # A walk that stops halfway has already appended part of the suffix; make the next
        # refresh rebuild from zero instead of appending the same intents a second time.
        memo.dirty_rewrite_revision = -1
        raise
    if memo.dirty_snapshot != table_ids:
        memo.dirty_snapshot = frozenset(table_ids)


def _walk_revisioned_intent_suffix(
    memo: _PrimaryKeyTxnMemo,
    intents: _RevisionList,
    start: int,
    table_ids: set[int],
) -> None:
    """Apply one append-only suffix after the caller installed interruption recovery."""
    for position in range(start, len(intents)):
        intent = intents[position]
        table_id = getattr(getattr(intent, "table", None), "table_id", None)
        if (
            isinstance(table_id, int)
            and not isinstance(table_id, bool)
            and table_id > 0
        ):
            table_ids.add(table_id)
            if type(intent) is RowIntent:
                memo.row_intents_by_table.setdefault(table_id, []).append(intent)
            else:
                memo.row_intent_index_complete = False
        else:
            # Engine-built RowIntent objects always carry a validated positive integer id. A
            # forged id that merely compares equal (True, 1.0, numpy ints) must keep consumers
            # on the canonical duck-typed scan instead of silently disappearing from the view.
            memo.row_intent_index_complete = False
        if type(intent) is RowIntent and intent.operation is RowOperation.DELETE:
            memo.has_delete_intent = True
    memo.dirty_cursor = len(intents)


def _indexed_row_intents(
    context: _Context, table: TableDef
) -> Sequence[RowIntent] | None:
    """Return the table-local intent slice, or None when canonical filtering is required."""
    engine = getattr(context, "engine", None)
    if engine is None or not hasattr(engine, "_primary_key_memos"):
        return None
    identified = _revisioned_txn_memo(engine, context.txn)
    if identified is None:
        return None
    _txn_id, memo = identified
    _refresh_revisioned_intent_index(memo)
    if not memo.row_intent_index_complete:
        return None
    return memo.row_intents_by_table.get(table.table_id, ())


def _has_indexed_delete_intent(context: _Context) -> bool | None:
    """Return the memoized DELETE fact, or None for an untrackable transaction."""
    engine = getattr(context, "engine", None)
    if engine is None or not hasattr(engine, "_primary_key_memos"):
        return None
    identified = _revisioned_txn_memo(engine, context.txn)
    if identified is None:
        return None
    _txn_id, memo = identified
    _refresh_revisioned_intent_index(memo)
    if not memo.row_intent_index_complete:
        return None
    return memo.has_delete_intent


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


_SchemaEffect = (
    _IndexSchemaEffect | _IndexObservationEffect | _VectorMapEffect | _SkipEffect
)


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
        self._budget.release(bytes_=self._charged_bytes, entries=self._charged_entries)
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
        self,
        table_id: int,
        slot: _EndpointLocatorSlot,
        locator: _EndpointIdentityLocator,
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
    """Return a conservative charge, or decline optional retention without changing the row.

    The charge is the shape tariff of the decoded values: deterministic, computed from types,
    string lengths and vector dimensions only, and above the real footprint of every shape it
    knows.  A row holding a compound value (LIST or MAP) has no bounded shape and keeps the
    former rule, 16 x its stored payload length, re-encoding a synthetic version once when the
    heap did not authenticate a length.
    """
    if found is None:
        return _OWNER_LANDING_MISS_BYTES
    values = found[1].values
    charge = _OWNER_LANDING_RESULT_BASE_BYTES + (
        _OWNER_LANDING_SCALAR_BYTES + _OWNER_LANDING_TUPLE_SLOT_BYTES * len(values)
    )
    compound = False
    for column, value in zip(table.columns, values):
        if _is_unmaterialized_column(value):
            charge += _OWNER_LANDING_TUPLE_SLOT_BYTES
            continue
        if value is None:
            charge += _OWNER_LANDING_TUPLE_SLOT_BYTES
            continue
        kind = column.type
        if kind is ValueType.STRING:
            charge += (
                _OWNER_LANDING_STRING_BASE_BYTES
                + _OWNER_LANDING_STRING_CHAR_BYTES * len(cast(str, value))
            )
        elif kind in VECTOR_VALUE_TYPES:
            # Pending SET can carry a not-yet-encoded value of the wrong type.
            # Optional retention must not replace the canonical schema refusal
            # with an AttributeError while estimating the cache charge.
            if type(value) is not VectorValue:
                return None
            charge += (
                _OWNER_LANDING_VECTOR_BASE_BYTES
                + _OWNER_LANDING_VECTOR_COMPONENT_BYTES
                * len(value.values)
            )
        elif kind is ValueType.BYTES:
            charge += _OWNER_LANDING_BYTES_BASE_BYTES + len(cast(bytes, value))
        elif kind is ValueType.LIST or kind is ValueType.MAP:
            compound = True
            break
        elif kind is ValueType.TIMESTAMP or kind is ValueType.UUID:
            charge += _OWNER_LANDING_OBJECT_BYTES
        else:
            charge += _OWNER_LANDING_SCALAR_BYTES
    if not compound:
        return charge
    authenticated = found[1].stored_payload_bytes
    if authenticated is not None:
        return (
            _OWNER_LANDING_RESULT_BASE_BYTES
            + authenticated * _OWNER_LANDING_PAYLOAD_MULTIPLIER
        )
    try:
        stored_bytes = len(encode_tuple(table, values))
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
    fingerprint and heap epoch still vouch for them.  Admission precedes every cache mutation.
    The retained results of one view form a least-recently-used set under the shared bytes and
    entries ceiling: a result that would exceed the ceiling first evicts this view's own least
    recently used results, and when nothing of this view is left to evict it is simply not
    retained -- the cache never grows past the ceiling, never disables itself for the rest of
    the transaction, and the query is never refused for acceleration capacity (KGRUN-M4).  When both this result cache and the D-02 prefix locator exceed their independent
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
        self, identity: object, context: _Context, *, materialize_vectors: bool = True
    ) -> tuple[object, HeapVersion] | None:
        """Return one owner-visible identity, memoizing only after successful admission."""
        # Partial rows never answer a later full-entity/vector read in this transaction.
        # Physical identities are integers or PendingRowRef, never this private tuple key.
        cache_key = identity if materialize_vectors else (identity, "vector_free")
        with self._guard:
            if self._retired:
                raise GrafxTransactionStateError(
                    "A transaction-local landing view was retired before its query finished.",
                    field="owner_landing_view",
                    table=self._table.name,
                    table_id=self._table.table_id,
                )
            cached = self._cache.get(cache_key)
            if cached is not None:
                # A hit becomes the most recently used entry (dict order is the LRU order).
                del self._cache[cache_key]
                self._cache[cache_key] = cached
                return cached[0]
            self._active += 1
        lease_open = True
        try:
            # The identity door can walk and decode heap pages.  It is deliberately outside the
            # injected registry guard; only the immutable overlay references above are leased.
            found = (
                self._resolve(identity, context)
                if materialize_vectors
                else self._resolve(identity, context, materialize_vectors=False)
            )
            charge = _owner_landing_result_bytes(self._table, found)
            if charge is not None and not materialize_vectors:
                charge += 128  # conservative retention tariff for the private key tuple
            with self._guard:
                try:
                    if (
                        not self._retired
                        and self._cache_enabled
                        and charge is not None
                        and cache_key not in self._cache
                    ):
                        budget = self._budget
                        if budget is not None and self._reserve_evicting_locked(
                            budget, charge
                        ):
                            try:
                                self._cache[cache_key] = (found, charge)
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
        self, identity: object, context: _Context, *, materialize_vectors: bool = True
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

        physical = (
            _visible_identity_with_ref(
                self._engine, context, self._table, cast(RecordId, identity)
            )
            if materialize_vectors
            else _visible_identity_with_ref(
                self._engine, context, self._table, cast(RecordId, identity), landing=True
            )
        )
        if physical is None:
            return None
        ref, version = physical
        if not materialize_vectors:
            # The identity-only decoder's proof values must not become row values.
            # Convert every vector position to the projected decoder's guarded marker;
            # owner updates below replace this with their complete, validated tuple.
            version = replace(version, values=tuple(
                _UNMATERIALIZED_COLUMN if column.type in VECTOR_VALUE_TYPES else value
                for column, value in zip(self._table.columns, version.values)
            ))
        if ref in self._ended:
            return None
        if ref in self._changed:
            values = self._changed[ref]
            if values is None:
                return None
            version = replace(version, values=values)
        return ref, version

    def _reserve_evicting_locked(
        self, budget: _OwnerLandingBudget, charge: int
    ) -> bool:
        """Reserve one result charge, evicting this view's LRU results while that helps.

        Only results of this view are evicted, oldest first, so a saturated shared budget makes a
        view trade its own stale results for fresh ones and never touches another view's.  When
        this view holds nothing more to release and the reservation still fails, the result is
        not retained and the cache stays enabled for the next one.  Caller holds the guard.
        """
        while True:
            try:
                budget.reserve(bytes_=charge, entries=1)
            except _OwnerLandingCapacity:
                if not self._cache:
                    return False
                oldest = next(iter(self._cache))
                _found, released = self._cache.pop(oldest)
                budget.release(bytes_=released, entries=1)
                self._cache_bytes -= released
                self._cache_entries -= 1
            else:
                return True

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


def _catalog_active_indexes(manager: object, catalog: Catalog) -> tuple[object, ...]:
    """Return the registry objects authorized by this exact catalog picture.

    Production's ``IndexManager`` owns the mapping from catalog definitions to resident stores.
    The legacy ``indexes`` fallback exists only for narrow query-engine doubles which implement
    the pre-v2 collaborator surface; it must not become a second production authority.
    """

    active = getattr(manager, "active_indexes", None)
    if callable(active):
        return tuple(active(catalog=catalog))
    listing = getattr(manager, "indexes", None)
    return tuple(listing()) if callable(listing) else ()


@dataclass(slots=True)
class _StatementAuthorityMemo:
    """One proved statement projection and the identities it was proved under.

    Every field but the projection is a fence: the entry answers only for the same statement
    object, the same catalog picture object and the same index manager, at the same registry
    revision.  The entry holds the statement, so the identity it is keyed by cannot be reused
    by another object while the entry lives.
    """

    statement: Statement
    catalog: Catalog
    manager: IndexManager
    registry_revision: int
    projection: _IndexAuthorityProjection


def _closed_statement_tables(
    statement: Statement, catalog: Catalog
) -> tuple[TableDef, ...] | None:
    """Return every table one statically closed query can reach, else ``None``.

    A named node label identifies one table. A named relationship type identifies its own table
    and both endpoint tables, including label-free endpoint variables. A standalone,
    label-free node can be scoped only when an earlier pattern already bound its variable.
    Anything less precise keeps the established global authority projection.

    The returned set may conservatively contain an endpoint that the direction later excludes;
    it must never omit a reachable table. That one-sided rule makes this an optimisation only:
    planning and execution still receive the exact catalog-authorised stores for every table
    they can use.
    """
    queries: tuple[Query, ...]
    if type(statement) is Query:
        queries = (statement,)
    elif (
        type(statement) is UnionQuery
        and type(statement.left) is Query
        and type(statement.right) is Query
    ):
        queries = (statement.left, statement.right)
    else:
        return None

    selected: dict[tuple[int, str], TableDef] = {}
    for query in queries:
        if (
            type(query.match_clauses) is not tuple
            or type(query.updating_clauses) is not tuple
            or type(query.with_clauses) is not tuple
        ):
            return None
        bound: dict[str, set[str]] = {}

        def add_table(name: str) -> TableDef | None:
            if type(name) is not str:
                return None
            try:
                table = catalog.table(name)
            except GrafxConfigurationError:
                return None
            selected[(table.table_id, table.name)] = table
            return table

        def visit(pattern: PatternPath) -> bool:
            if (
                type(pattern) is not PatternPath
                or type(pattern.nodes) is not tuple
                or type(pattern.relationships) is not tuple
                or not pattern.nodes
                or any(type(node) is not NodePattern for node in pattern.nodes)
                or any(
                    type(relationship) is not RelationshipPattern
                    for relationship in pattern.relationships
                )
            ):
                return False
            inferred: list[set[str]] = [set() for _ in pattern.nodes]
            if len(pattern.relationships) != len(pattern.nodes) - 1:
                return False
            for position, relationship in enumerate(pattern.relationships):
                if (
                    type(relationship.types) is not tuple
                    or len(relationship.types) != 1
                    or type(relationship.types[0]) is not str
                    or type(relationship.variable) not in {str, type(None)}
                    or type(relationship.direction) is not Direction
                ):
                    return False
                relation = add_table(relationship.types[0])
                if (
                    relation is None
                    or relation.kind != "rel"
                    or relation.from_table is None
                    or relation.to_table is None
                ):
                    return False
                source = add_table(relation.from_table)
                target = add_table(relation.to_table)
                if source is None or target is None:
                    return False
                if relationship.direction is Direction.OUTGOING:
                    inferred[position].add(source.name)
                    inferred[position + 1].add(target.name)
                elif relationship.direction is Direction.INCOMING:
                    inferred[position].add(target.name)
                    inferred[position + 1].add(source.name)
                else:
                    inferred[position].update((source.name, target.name))
                    inferred[position + 1].update((source.name, target.name))
                if relationship.variable is not None:
                    previous = bound.get(relationship.variable)
                    relation_names = {relation.name}
                    bound[relationship.variable] = (
                        relation_names
                        if previous is None
                        else previous | relation_names
                    )

            for position, node in enumerate(pattern.nodes):
                if (
                    type(node.labels) is not tuple
                    or any(type(label) is not str for label in node.labels)
                    or type(node.variable) not in {str, type(None)}
                ):
                    return False
                candidates: set[str]
                if node.labels:
                    if len(node.labels) != 1:
                        return False
                    candidates = {node.labels[0]}
                elif node.variable is not None and node.variable in bound:
                    candidates = set(bound[node.variable])
                else:
                    candidates = inferred[position]
                if not candidates:
                    return False
                for name in candidates:
                    if add_table(name) is None:
                        return False
                if node.variable is not None:
                    previous = bound.get(node.variable)
                    bound[node.variable] = (
                        set(candidates) if previous is None else previous | candidates
                    )
            return True

        for clause in query.match_clauses:
            if type(clause) is not MatchClause or type(clause.patterns) is not tuple:
                return None
            for pattern in clause.patterns:
                if not visit(pattern):
                    return None
        for clause in query.updating_clauses:
            if type(clause) is CreateClause:
                if type(clause.patterns) is not tuple:
                    return None
                for pattern in clause.patterns:
                    if not visit(pattern):
                        return None
            elif type(clause) is MergeClause:
                if not visit(clause.pattern):
                    return None
            elif type(clause) is SetClause:
                if type(clause.items) is not tuple or any(
                    type(item.target) is not Property
                    or type(item.target.subject) is not Variable
                    or item.target.subject.name not in bound
                    for item in clause.items
                ):
                    return None
            elif type(clause) is DeleteClause:
                if (
                    type(clause.targets) is not tuple
                    or type(clause.detach) is not bool
                    or any(
                        type(target) is not Variable or target.name not in bound
                        for target in clause.targets
                    )
                ):
                    return None
                if clause.detach:
                    detached_from = {
                        name for target in clause.targets for name in bound[target.name]
                    }
                    for table in catalog.tables():
                        if table.kind == "rel" and (
                            table.from_table in detached_from
                            or table.to_table in detached_from
                        ):
                            selected[(table.table_id, table.name)] = table
            else:
                return None
    return tuple(selected[key] for key in sorted(selected))


@dataclass(slots=True, frozen=True)
class _IndexAuthorityProjection:
    """The exact index stores selected once for one statement's catalog picture."""

    planning_indexes: tuple[object, ...]
    indexes: tuple[object, ...]
    by_name: dict[str, tuple[object, ...]]

    @classmethod
    def build(
        cls,
        indexes: tuple[object, ...],
        *,
        planning_indexes: tuple[object, ...] | None = None,
    ) -> _IndexAuthorityProjection:
        grouped: dict[str, list[object]] = {}
        for index in indexes:
            definition = getattr(index, "definition", None)
            key = getattr(definition, "registry_key", None)
            if not isinstance(key, str):
                name = getattr(index, "name", None)
                if not isinstance(name, str):
                    continue
                key = name.lower()
            grouped.setdefault(key, []).append(index)
        return cls(
            planning_indexes=indexes if planning_indexes is None else planning_indexes,
            indexes=indexes,
            by_name={key: tuple(values) for key, values in grouped.items()},
        )

    def named(self, name: str) -> object | None:
        """Resolve one name while retaining the previous duplicate-authority refusal."""
        matches = self.by_name.get(name.lower(), ())
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise GrafxIndexError(
                f"Statement catalog authority resolved {len(matches)} stores for index "
                f"{name!r}.",
                field="index_authority",
                index=name,
                count=len(matches),
            )
        return None


@dataclass(slots=True, frozen=True)
class _PreparedPlanKey:
    """A collaborator-free proof that one immutable plan still describes this statement."""

    text: str
    catalog_image: bytes
    index_picture: tuple[tuple[IndexDefinition, bool], ...]
    dirty_tables: frozenset[int]
    version: int = _PREPARED_PLAN_VERSION


def _dirty_primary_key_seek_definition(definition: object, catalog: Catalog) -> bool:
    """Say whether one dirty-table index has the exact overlay implemented by IndexSeek.

    This intentionally recognizes only Grafx's automatic exact primary-key artifact.  A custom
    exact index over the same column may share its bytes, but it does not share the frozen name
    and ownership contract used by the transaction-local primary-key fold.  Keeping the proof
    this narrow makes an unfamiliar or speculative artifact take the established scan fallback.
    """
    if not isinstance(definition, IndexDefinition):
        return False
    if definition.visibility is not IndexVisibility.EXACT:
        return False
    if not catalog.has_table(definition.table_name):
        return False
    table = catalog.table(definition.table_name)
    if (
        table.kind != "node"
        or table.table_id != definition.table_id
        or table.primary_key is None
        or definition.name != primary_key_index_name(table.name)
    ):
        return False
    return definition.positions == (table.column_index(table.primary_key),)


def _catalog_active_index(
    manager: object,
    name: str,
    catalog: Catalog,
    *,
    txn: object | None = None,
    projection: _IndexAuthorityProjection | None = None,
) -> object | None:
    """Return one catalog-authorized registered index, with a legacy-double fallback."""

    if projection is not None:
        selected = projection.named(name)
        if selected is not None:
            return selected
        if callable(getattr(manager, "active_index", None)):
            raise GrafxIndexError(
                f"No catalog-selected ACTIVE index named {name!r} is present in this "
                "statement's authority projection.",
                field="index_authority",
                value=name,
                index=name,
                registered=False,
            )
        return None

    scoped = getattr(manager, "active_indexes_for", None)
    if txn is not None and callable(scoped):
        logical = (
            catalog.index_definition(name)
            if catalog.format_version == CATALOG_FORMAT_VERSION
            and catalog.has_index_definition(name)
            else None
        )
        active_generation = None if logical is None else logical.active_generation()
        expected = (
            None
            if logical is None or active_generation is None
            else logical.runtime_definition(active_generation)
        )
        if expected is not None:
            table = catalog.table_by_id(expected.table_id)
            matches = tuple(
                index
                for index in scoped(
                    expected.table_id,
                    table_name=expected.table_name,
                    table=table,
                    txn=txn,
                    catalog=catalog,
                )
                if getattr(index, "definition", None) == expected
            )
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise GrafxIndexError(
                    f"Transaction-scoped catalog authority resolved {len(matches)} stores "
                    f"for index {name!r}.",
                    field="index_authority",
                    index=name,
                    count=len(matches),
                )
    active = getattr(manager, "active_index", None)
    if callable(active):
        return active(name, catalog=catalog)
    lookup = getattr(manager, "index", None)
    return lookup(name) if callable(lookup) else None


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
        "_primary_key_memos",
        "_txn_effects",
        "_skip_claims",
        "_durable_skips",
        "_schema_artifact_section",
        "_custom_index_preparer",
        "_page_stager",
        "_max_statement_writes",
        "_max_result_rows",
        "_max_intermediate_rows",
        "_query_memory_budget_bytes",
        "_query_spill",
        "_max_traversal_expansions",
        "_max_traversal_paths",
        "_max_index_build_entries",
        "_automatic_index_expected_cardinality",
        "_automatic_index_bucket_count",
        "_prepared_guard",
        "_parse_cache",
        "_plan_cache",
        "_owned_prepared_plans",
        "_statement_authority_memo",
        "_compiled_predicates",
        "_compiled_predicates_lock",
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
        custom_index_preparer: Callable[..., CatalogIndexDefinition] | None = None,
        endpoint_locator_guard: AbstractContextManager[object] | None = None,
        max_statement_writes: int | None = None,
        max_result_rows: int | None = None,
        max_intermediate_rows: int | None = None,
        query_memory_budget_bytes: int | None = None,
        query_spill: QuerySpillFactory | None = None,
        max_traversal_expansions: int | None = None,
        max_traversal_paths: int | None = None,
        max_index_build_entries: int | None = None,
        automatic_index_expected_cardinality: int | None = None,
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
        self._compiled_predicates: dict[int, _CompiledPredicate] = {}
        # Guards only the lookup, publication and eviction of compiled predicates: never held
        # while compiling, evaluating a row or doing I/O.
        self._compiled_predicates_lock = Lock()
        self._endpoint_memo: dict[int, _EndpointTxnMemo] = {}
        self._endpoint_budget = _EndpointLocatorBudget(
            max_bytes=_ENDPOINT_LOCATOR_MAX_BYTES,
            max_entries=_ENDPOINT_LOCATOR_MAX_ENTRIES,
            guard=self._endpoint_guard,
        )
        self._primary_key_memos: dict[int, _PrimaryKeyTxnMemo] = {}
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
        self._custom_index_preparer = custom_index_preparer
        self._max_statement_writes = _require_optional_positive_limit(
            "max_statement_writes", max_statement_writes
        )
        self._max_result_rows = _require_optional_positive_limit(
            "max_result_rows", max_result_rows
        )
        self._max_intermediate_rows = _require_optional_positive_limit(
            "max_intermediate_rows", max_intermediate_rows
        )
        self._query_memory_budget_bytes = _require_optional_positive_limit(
            "query_memory_budget_bytes", query_memory_budget_bytes
        )
        if self._query_memory_budget_bytes is not None and not isinstance(
            query_spill, QuerySpillFactory
        ):
            raise GrafxConfigurationError(
                "query_memory_budget_bytes needs a QuerySpillFactory supplied by the "
                "composition root.",
                field="query_spill",
                value=type(query_spill).__name__,
            )
        self._query_spill = query_spill
        self._max_traversal_expansions = _require_optional_positive_limit(
            "max_traversal_expansions", max_traversal_expansions
        )
        self._max_traversal_paths = _require_optional_positive_limit(
            "max_traversal_paths", max_traversal_paths
        )
        self._max_index_build_entries = _require_optional_positive_limit(
            "max_index_build_entries", max_index_build_entries
        )
        (
            self._automatic_index_bucket_count,
            self._automatic_index_expected_cardinality,
        ) = custom_index_sizing(
            expected_cardinality=automatic_index_expected_cardinality
        )
        # The composition root supplies one re-entrant process-local guard. Reusing that port
        # keeps the pure engine free of threading mechanism while protecting both bounded memo
        # families under the same deliberately short, non-I/O critical sections.
        self._prepared_guard = self._endpoint_guard
        self._parse_cache: OrderedDict[str, Statement] = OrderedDict()
        self._plan_cache: OrderedDict[_PreparedPlanKey, PlannedQuery] = OrderedDict()
        # Public result detachment may take its fast path only for a root retained here by this
        # exact engine. Counts handle one immutable plan admitted under more than one key.
        self._owned_prepared_plans: dict[int, tuple[PlanNode, int]] = {}
        # Statement-authority memo: keyed by the identity of a retained parsed statement and
        # fenced by the identities of the catalog picture and index manager plus the registry
        # revision.  An acceleration only -- every hit re-proves those fences.
        self._statement_authority_memo: OrderedDict[int, _StatementAuthorityMemo] = (
            OrderedDict()
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
        with self._prepared_guard:
            cached = self._parse_cache.get(text)
            if cached is not None:
                self._parse_cache.move_to_end(text)
        if cached is not None:
            self._observe(PHASE_PARSE, started)
            return cached
        try:
            statement = parse_text(text)
        except GrafxError as failure:
            self._count_error(failure)
            raise
        with self._prepared_guard:
            existing = self._parse_cache.get(text)
            if existing is None:
                self._parse_cache[text] = statement
                if len(self._parse_cache) > _PARSE_CACHE_MAX_ENTRIES:
                    _text, evicted = self._parse_cache.popitem(last=False)
                    # The memo is keyed by the identity of a retained statement; a statement
                    # the parse cache no longer retains leaves the memo with it.
                    self._statement_authority_memo.pop(id(evicted), None)
            else:
                statement = existing
                self._parse_cache.move_to_end(text)
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
        statement = self.parse(text)
        catalog = self._catalog.catalog
        authority = self._statement_index_authority(catalog, statement=statement)
        return self._planned_for(
            statement,
            None,
            None,
            authority=authority,
            cache_text=text,
        ).root

    def _compiled_predicate(
        self, expression: Expression, context: _Context
    ) -> _CompiledPredicate:
        """Return the closures of one WHERE predicate, compiling it at most once per plan.

        Keyed by the identity of the expression object, which the cached plan keeps alive; the
        entry is reused only while it is for that exact object and the planner's coalesce types
        it was compiled with still hold.  Bounded by ``_COMPILED_PREDICATE_MAX_ENTRIES``; the
        oldest entry leaves first.  It holds no row, value or snapshot: nothing here is
        authority beyond the life of the statement that runs it.

        Readers of one engine may run on several threads: the dictionary is only read, published
        or trimmed under ``_compiled_predicates_lock``, which is never held while compiling (so
        the concurrent compile of one predicate is at most repeated, never serialized with row
        evaluation) and the first published entry wins so every thread runs the same closures.
        """
        cache = self._compiled_predicates
        key = id(expression)
        with self._compiled_predicates_lock:
            entry = cache.get(key)
            if entry is not None and entry.serves(expression, context):
                return entry
        compiled = _compile_predicate(expression, context)
        with self._compiled_predicates_lock:
            entry = cache.get(key)
            if entry is not None and entry.serves(expression, context):
                return entry
            cache.pop(key, None)
            while len(cache) >= _COMPILED_PREDICATE_MAX_ENTRIES:
                cache.pop(next(iter(cache)))
            cache[key] = compiled
        return compiled

    def _planned_for(
        self,
        statement: Statement,
        txn: object,
        working: Catalog | None,
        *,
        authority: _IndexAuthorityProjection | None = None,
        cache_text: str | None = None,
    ) -> PlannedQuery:
        """Return the plan a statement RUNS with, seeing this transaction's own schema.

        The public ``planned``/``explain`` doors describe the database as committed, which is
        what an outside caller asks about. A statement running INSIDE a transaction that has
        already declared tables must be planned against that transaction's working catalog, or
        the second statement of the quick start's schema block fails to plan the table the first
        one declared. A table this transaction has already changed also withholds its indexes:
        only a scan can be safely combined with pending inserts and changed primary keys.
        """
        dirty_tables = _intent_table_ids(self, txn)
        catalog = working if working is not None else self._catalog.catalog
        if authority is None:
            authority = self._statement_index_authority(
                catalog, txn=txn, statement=statement
            )
        started = self._reading()
        try:
            key = self._prepared_plan_key(
                text=cache_text,
                statement=statement,
                catalog=catalog,
                committed_catalog=working is None,
                authority=authority,
                dirty_tables=dirty_tables,
            )
            plan = None if key is None else self._cached_prepared_plan(key)
            if plan is None:
                analysis = analyze(statement)
                plan = build_plan(
                    statement,
                    catalog=catalog,
                    indexes=self._index_definitions(
                        catalog=catalog,
                        without_indexes_for=dirty_tables,
                        authority=authority,
                    ),
                    analysis=analysis,
                )
                if key is not None:
                    plan = self._remember_prepared_plan(key, plan)
        except GrafxError as failure:
            self._count_error(failure)
            raise
        self._observe(PHASE_PLAN, started)
        return plan

    def planned(self, statement: Statement) -> PlannedQuery:
        """Return the plan together with the analysis it was built from."""
        started = self._reading()
        try:
            catalog = self._catalog.catalog
            authority = self._statement_index_authority(catalog, statement=statement)
            analysis = analyze(statement)
            plan = build_plan(
                statement,
                catalog=catalog,
                indexes=self._index_definitions(catalog=catalog, authority=authority),
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
        return self._execute_parsed(self.parse(text), txn, parameters, cache_text=text)

    def create_index(
        self,
        *,
        name: str,
        table: str,
        columns: tuple[str, ...],
        bucket_count: int | None,
        expected_cardinality: int | None,
        layout: str | None,
        txn: object,
    ) -> QueryResult:
        """Run the Python index door through the same analysis and plan as textual DDL."""
        return self._execute_parsed(
            CreateIndexStatement(
                name=name,
                variable="n",
                table=table,
                columns=columns,
                bucket_count=bucket_count,
                expected_cardinality=expected_cardinality,
                layout=layout,
            ),
            txn,
        )

    def _execute_parsed(
        self,
        statement: Statement,
        txn: object,
        parameters: Mapping[str, object] | None = None,
        *,
        cache_text: str | None = None,
    ) -> QueryResult:
        """Run a parsed statement while still planning against current transaction state.

        This internal door lets a bounded facade reuse syntax work for ``executemany``. Planning
        is intentionally repeated: earlier items can dirty tables, and their indexes must then be
        withheld so later items retain read-your-own-writes correctness.
        """
        working = self._working.get(getattr(txn, "txn_id", None))
        if working is not None and not self._txn_stages_catalog(txn):
            working = None
        catalog = working if working is not None else self._catalog.catalog
        authority = self._statement_index_authority(
            catalog, txn=txn, statement=statement
        )
        plan = self._planned_for(
            statement,
            txn,
            working,
            authority=authority,
            cache_text=cache_text,
        )
        started = self._reading()
        try:
            result = self._run(
                plan,
                txn,
                self._bind_parameters(plan, parameters),
                catalog=working,
                index_authority=authority,
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
        catalog = working if working is not None else self._catalog.catalog
        authority = self._statement_index_authority(
            catalog, txn=txn, statement=statement
        )
        plan = self._planned_for(
            statement,
            txn,
            working,
            authority=authority,
            cache_text=text,
        )
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
                index_authority=authority,
                node_scan_projections=_closed_node_scan_projections(root.child),
                vector_free_landings=_closed_vector_free_landings(root.child),
            )
            _bind_timestamp_values(plan, context)
            _validate_bound_subscript_types(plan, bound)
            _validate_bound_label_arguments(plan, bound)
            rows = self._rows(root.child, context)
            elapsed = (
                self._clock.monotonic() - started if self._metrics.enabled else 0.0
            )
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
        authority: _IndexAuthorityProjection | None = None,
    ) -> tuple[object, ...]:
        """Return usable index definitions, retaining safe owner-overlay primary seeks.

        A dirty table used to lose every index for the rest of its transaction.  That is safe,
        but turns the common ``create nodes, then connect them`` ingestion shape into one full
        node scan per endpoint.  The automatic primary-key index is different from a general
        secondary index: :func:`_index_seek` can overlay the transaction's incrementally folded
        primary-key ownership map on its durable candidates.  Keep only that exact access path
        for a dirty table; every other index still falls back to the canonical scan.
        """
        if self._indexes is None:
            return ()
        # A STALE index is withheld from the planner, and that is a correctness rule rather than
        # a policy. A stale index is a SUBSET of what the heap holds -- entries it never received
        # -- and being a subset is exactly what the EXACT contract cannot repair: validating a
        # candidate against the heap removes hits that should not be there and cannot invent ones
        # that are missing. So a plan built on one answers a keyed read with fewer rows than
        # exist. Withholding it plans the scan the engine planned before any index existed, which
        # is slower and right. The index says so itself through `stale`, and `Database.
        # stale_indexes` is where an operator sees which ones need rebuilding.
        indexes = (
            authority.planning_indexes
            if authority is not None
            else _catalog_active_indexes(self._indexes, catalog)
        )
        if type(catalog) is Catalog and type(self._indexes) is IndexManager:
            usable: list[object] = []
            for index in indexes:
                definition = index.definition
                if getattr(index, "stale", False) or (
                    definition.table_id in without_indexes_for
                    and not _dirty_primary_key_seek_definition(definition, catalog)
                ):
                    continue
                if not catalog.has_table(definition.table_name):
                    continue
                table = catalog.table(definition.table_name)
                if (
                    table.table_id == definition.table_id
                    and index_definition_matches_table(definition, table)
                ):
                    usable.append(definition)
            return tuple(usable)

        # Catalog subclasses and compatibility collaborators keep their observable whole-schema
        # projection. The concrete built-in path above uses the O(1) id directory for only the
        # already scoped indexes selected by this statement.
        tables = {(table.table_id, table.name): table for table in catalog.tables()}
        return tuple(
            index.definition
            for index in indexes
            if not getattr(index, "stale", False)
            and (
                index.definition.table_id not in without_indexes_for
                or _dirty_primary_key_seek_definition(index.definition, catalog)
            )
            and (
                (
                    table := tables.get(
                        (index.definition.table_id, index.definition.table_name)
                    )
                )
                is not None
                and index_definition_matches_table(index.definition, table)
            )
        )

    def _statement_index_authority(
        self,
        catalog: Catalog,
        *,
        txn: object | None = None,
        statement: Statement | None = None,
    ) -> _IndexAuthorityProjection:
        """Project catalog-selected stores once for planning and execution of one statement."""
        manager = self._indexes
        if manager is None:
            return _IndexAuthorityProjection.build(())
        txn_id = getattr(txn, "txn_id", None)
        scoped_txn = (
            txn
            if isinstance(txn_id, int) and not isinstance(txn_id, bool) and txn_id >= 0
            else None
        )
        fence = (
            self._statement_authority_fence(manager, scoped_txn)
            if statement is not None and type(manager) is IndexManager
            else None
        )
        if fence is not None:
            memoized = self._memoized_statement_authority(
                statement, catalog, manager, fence
            )
            if memoized is not None:
                return memoized
        closed_tables = (
            None if statement is None else _closed_statement_tables(statement, catalog)
        )
        scoped_indexes = getattr(manager, "_statement_indexes_for_tables", None)
        if (
            type(manager) is IndexManager
            and closed_tables is not None
            and callable(scoped_indexes)
        ):
            planning, runtime = scoped_indexes(
                closed_tables, txn=scoped_txn, catalog=catalog
            )
            projection = _IndexAuthorityProjection.build(
                tuple(runtime), planning_indexes=tuple(planning)
            )
            if fence is not None:
                self._remember_statement_authority(
                    statement, catalog, manager, fence, projection
                )
            return projection
        statement_indexes = getattr(manager, "_statement_indexes", None)
        if callable(statement_indexes):
            planning, runtime = statement_indexes(txn=scoped_txn, catalog=catalog)
            return _IndexAuthorityProjection.build(
                tuple(runtime), planning_indexes=tuple(planning)
            )
        if scoped_txn is not None:
            transaction_indexes = getattr(manager, "_transaction_indexes", None)
        else:
            transaction_indexes = None
        if callable(transaction_indexes):
            indexes = tuple(transaction_indexes(scoped_txn, catalog=catalog))
        else:
            indexes = _catalog_active_indexes(manager, catalog)
        return _IndexAuthorityProjection.build(indexes)

    @staticmethod
    def _statement_authority_fence(
        manager: IndexManager, scoped_txn: object | None
    ) -> int | None:
        """Return the registry revision a memoized projection may be keyed under, or None.

        The scoped projection of a statement depends on the parsed statement, the catalog
        picture, the registered index inventory and -- only when this transaction staged
        speculative DDL -- on what that transaction observed.  The first three are fenced by
        identity and by the registry revision; the last makes the answer transaction-specific,
        so such a statement is never memoized.  A manager whose revision is not the exact
        integer this door was written against declines as well.
        """
        revision = getattr(manager, "_registry_revision", None)
        if type(revision) is not int:
            return None
        if scoped_txn is not None:
            observed = getattr(manager, "_schema_observed", None)
            if type(observed) is not dict:
                return None
            if observed.get(getattr(scoped_txn, "txn_id", None)):
                return None
        return revision

    def _memoized_statement_authority(
        self,
        statement: Statement,
        catalog: Catalog,
        manager: IndexManager,
        fence: int,
    ) -> _IndexAuthorityProjection | None:
        """Return the projection memoized for exactly this statement under exactly these fences."""
        key = id(statement)
        with self._prepared_guard:
            entry = self._statement_authority_memo.get(key)
            if entry is None:
                return None
            if (
                entry.statement is not statement
                or entry.catalog is not catalog
                or entry.manager is not manager
                or entry.registry_revision != fence
            ):
                # Never an authority: a drifted fence drops the entry so the next proof
                # replaces it instead of shadowing it.
                del self._statement_authority_memo[key]
                return None
            self._statement_authority_memo.move_to_end(key)
            return entry.projection

    def _remember_statement_authority(
        self,
        statement: Statement,
        catalog: Catalog,
        manager: IndexManager,
        fence: int,
        projection: _IndexAuthorityProjection,
    ) -> None:
        """Retain one freshly proved projection for exactly this statement, bounded."""
        key = id(statement)
        with self._prepared_guard:
            self._statement_authority_memo[key] = _StatementAuthorityMemo(
                statement=statement,
                catalog=catalog,
                manager=manager,
                registry_revision=fence,
                projection=projection,
            )
            self._statement_authority_memo.move_to_end(key)
            while len(self._statement_authority_memo) > (
                _STATEMENT_AUTHORITY_MEMO_MAX_ENTRIES
            ):
                self._statement_authority_memo.popitem(last=False)

    def _prepared_plan_key(
        self,
        *,
        text: str | None,
        statement: Statement,
        catalog: Catalog,
        committed_catalog: bool,
        authority: _IndexAuthorityProjection,
        dirty_tables: frozenset[int],
    ) -> _PreparedPlanKey | None:
        """Return a pure cache key, or decline when exact text provenance is unavailable."""
        if text is None:
            return None
        with self._prepared_guard:
            # Hand-built statements and a private caller supplying mismatched text must never
            # acquire the authority of an unrelated cached parse.
            if self._parse_cache.get(text) is not statement:
                return None

        picture: list[tuple[IndexDefinition, bool]] = []
        for index in authority.planning_indexes:
            definition = getattr(index, "definition", None)
            stale = getattr(index, "stale", None)
            if not isinstance(definition, IndexDefinition) or type(stale) is not bool:
                # A foreign index collaborator remains usable through the canonical planning
                # path, but its dynamic protocol is not stable cache-key material.
                return None
            picture.append((definition, stale))

        # The committed store already retains the exact immutable image it adopted from pages;
        # using it is O(1). A transaction's working catalog is intentionally serialized because
        # its uncommitted mutations have no durable image and must still split cache entries.
        catalog_image = (
            self._catalog.persisted_image()
            if committed_catalog
            else catalog.serialize()
        )
        return _PreparedPlanKey(
            text=text,
            catalog_image=catalog_image,
            index_picture=tuple(picture),
            dirty_tables=dirty_tables,
        )

    def _cached_prepared_plan(self, key: _PreparedPlanKey) -> PlannedQuery | None:
        """Read and promote one LRU entry without exposing its runtime authority projection."""
        with self._prepared_guard:
            cached = self._plan_cache.get(key)
            if cached is not None:
                self._plan_cache.move_to_end(key)
            return cached

    def _remember_prepared_plan(
        self, key: _PreparedPlanKey, plan: PlannedQuery
    ) -> PlannedQuery:
        """Publish one immutable prepared plan, preserving a concurrent winner."""
        with self._prepared_guard:
            existing = self._plan_cache.get(key)
            if existing is not None:
                self._plan_cache.move_to_end(key)
                return existing
            self._plan_cache[key] = plan
            marker = id(plan.root)
            owned = self._owned_prepared_plans.get(marker)
            if owned is None or owned[0] is not plan.root:
                self._owned_prepared_plans[marker] = (plan.root, 1)
            else:
                self._owned_prepared_plans[marker] = (owned[0], owned[1] + 1)
            if len(self._plan_cache) > _PLAN_CACHE_MAX_ENTRIES:
                _old_key, old_plan = self._plan_cache.popitem(last=False)
                old_marker = id(old_plan.root)
                old_owned = self._owned_prepared_plans.get(old_marker)
                if old_owned is not None and old_owned[0] is old_plan.root:
                    if old_owned[1] == 1:
                        self._owned_prepared_plans.pop(old_marker, None)
                    else:
                        self._owned_prepared_plans[old_marker] = (
                            old_owned[0],
                            old_owned[1] - 1,
                        )
            return plan

    def _owns_prepared_plan(self, plan: object) -> bool:
        """Prove an exact root is retained by this engine's bounded prepared-plan cache."""
        with self._prepared_guard:
            owned = self._owned_prepared_plans.get(id(plan))
            return owned is not None and owned[0] is plan

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
        index_authority: _IndexAuthorityProjection | None = None,
    ) -> QueryResult:
        """Walk the plan and produce the result."""
        root = plan.root
        statistics: dict[str, int] = {}
        if isinstance(
            root, (CreateIndex, CreateNodeTable, CreateRelTable, CreateVectorSpace)
        ):
            self._schema(root, txn, statistics)
            return _owned_query_result(plan=root, statistics=dict(statistics))
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
            index_authority=index_authority,
            short_circuit_traversals=_short_circuit_traversals(root.child),
            node_scan_projections=_closed_node_scan_projections(root.child),
            vector_free_landings=_closed_vector_free_landings(root.child),
        )
        _bind_timestamp_values(plan, context)
        _validate_bound_subscript_types(plan, parameters)
        _validate_bound_label_arguments(plan, parameters)
        stream = self._rows(root.child, context)
        stream_failure: BaseException | None = None
        try:
            rows = self._collect_result_rows(stream) if root.columns else tuple(stream)
        except BaseException as caught:
            stream_failure = caught
            raise
        finally:
            _close_iterator(stream, stream_failure)
        context.release()
        produced: tuple[tuple[Value, ...], ...] = ()
        if root.columns:
            produced = tuple(_projected(row, root.columns) for row in rows)
        return _owned_query_result(
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

    def _spill_workspace(
        self, operator: str
    ) -> tuple[QuerySpillWorkspace, LogicalMemoryBudget]:
        """Open the configured adapter workspace for one blocking physical operator."""
        limit = self._query_memory_budget_bytes
        factory = self._query_spill
        if limit is None or factory is None:
            raise GrafxConfigurationError(
                "A bounded query spill was requested without its configured adapter.",
                field="query_spill",
                value="missing",
            )
        budget = LogicalMemoryBudget(limit, operator=operator)
        try:
            workspace = factory.open(budget)
        except GrafxError:
            raise
        except Exception as failure:
            raise GrafxConfigurationError(
                "The query spill adapter could not open a workspace.",
                field="query_spill",
                value=type(failure).__name__,
            ) from failure
        if not isinstance(workspace, QuerySpillWorkspace):
            closer = getattr(workspace, "close", None)
            if callable(closer):
                closer()
            raise GrafxConfigurationError(
                "The query spill adapter returned a malformed workspace.",
                field="query_spill",
                value=type(workspace).__name__,
            )
        return workspace, budget

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
        if isinstance(node, CreateIndex):
            prepare = self._custom_index_preparer
            if not callable(prepare):
                raise GrafxUnsupportedOperation(
                    "CREATE INDEX needs the transaction manager's detached exact-index "
                    "preparation port.",
                    field="indexes",
                    value="custom_index_preparer",
                )
            staged_pages = getattr(txn, "staged_pages", None)
            before = set(staged_pages()) if callable(staged_pages) else set()
            prepare(
                txn,
                name=node.name,
                table_name=node.table.name,
                positions=node.positions,
                bucket_count=node.bucket_count,
                expected_cardinality=node.expected_cardinality,
                layout=node.layout,
                key_derivation=node.key_derivation,
            )
            after = set(staged_pages()) if callable(staged_pages) else set()
            statistics["indexes_created"] = statistics.get("indexes_created", 0) + 1
            statistics["pages_staged"] = statistics.get("pages_staged", 0) + len(
                after - before
            )
            return
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
        # A structural copy: the immutable definitions are shared and only the dictionaries
        # are new, so the clone is still adopted whole or discarded whole.  The serialized
        # round trip this replaces validated nothing the installers had not, at O(tables x
        # columns) per statement.
        catalog = base.copy()
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
        if (
            self._automatic_index_expected_cardinality is not None
            and catalog.format_version == CATALOG_LEGACY_FORMAT_VERSION
            and isinstance(node, (CreateNodeTable, CreateRelTable))
        ):
            raise GrafxUnsupportedOperation(
                "Automatic-index sizing requires catalog v2 for table DDL on a non-empty "
                "legacy catalog; run maintenance.ensure_identity_indexes() first.",
                operation="create table",
                field="format_version",
                value=catalog.format_version,
                required=CATALOG_FORMAT_VERSION,
                remedy="maintenance.ensure_identity_indexes",
            )
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
            self._attach_primary_key_index(
                installed,
                statistics,
                undo,
                txn,
                catalog=catalog,
            )
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
            self._attach_endpoint_indexes(
                installed,
                statistics,
                undo,
                txn,
                catalog=catalog,
            )
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
            self._schema_artifact_section(sync_if=self._needs_committed_artifact_sync)
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
                        settle = getattr(self._vectors, "_settle_attachment", None)
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
            catalog = self._catalog.catalog
            tables = tuple(catalog.tables())
        except (AttributeError, GrafxError):
            return False
        missing_for = getattr(
            self._indexes, "unregistered_persistent_indexes_for", None
        )
        if callable(missing_for) and missing_for(tables, catalog=catalog):
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
            catalog = self._catalog.catalog
            registered = _catalog_active_indexes(self._indexes, catalog)
            tables = tuple(catalog.tables())
        except (AttributeError, GrafxError):
            return
        project = getattr(catalog, "active_index_definitions", None)
        projected = (
            tuple(project())
            if callable(project)
            else tuple(
                definition
                for table in tables
                for definition in automatic_index_definitions(table)
            )
        )
        durable: set[str] = set()
        for table in tables:
            try:
                definitions = (
                    tuple(
                        definition
                        for definition in automatic_index_definitions(table)
                        if definition.visibility is IndexVisibility.EXACT
                    )
                    if catalog.format_version == CATALOG_LEGACY_FORMAT_VERSION
                    else tuple(
                        definition
                        for definition in projected
                        if definition.visibility is IndexVisibility.EXACT
                        and definition.table_id == table.table_id
                        and definition.table_name == table.name
                    )
                )
            except GrafxError:
                # An automatic scalar index name outside the identifier budget is precisely the
                # supported decline this diagnostic describes.
                durable.add(table.name)
                continue
            if definitions and any(
                not any(index.definition == expected for index in registered)
                for expected in definitions
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
                    settle_index = getattr(self._indexes, "settle_speculative", None)
                    if callable(settle_index):
                        settle_index(effect.artifact, committed=True)
                    continue
                if isinstance(effect, _VectorMapEffect):
                    settle = getattr(self._vectors, "_settle_attachment", None)
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
            self._settle_primary_key_memo(txn_id)
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
        self._settle_primary_key_memo(txn_id)
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

    def _settle_primary_key_memo(self, txn_id: int) -> None:
        """Release every metered exact-key result on every terminal transaction path."""
        with self._endpoint_guard:
            memo = self._primary_key_memos.pop(txn_id, None)
            if memo is not None:
                _discard_primary_key_resolution_caches(self, memo)

    @property
    def skipped_indexes(self) -> tuple[str, ...]:
        """Return the tables whose primary-key index could not be created, in name order."""
        return tuple(sorted(self._skipped_indexes))

    def _stage_schema_index(
        self,
        candidate: object,
        txn: object,
        undo: list[_SchemaEffect],
        *,
        detached: bool = False,
    ) -> object:
        """Register/adopt one index and journal its object, nonce and empty observation."""
        register = getattr(
            self._indexes,
            "register_detached_speculative" if detached else "register_speculative",
            None,
        )
        if not callable(register):
            if detached:
                raise GrafxUnsupportedOperation(
                    "Detached catalog generations need transaction-scoped index authority.",
                    field="indexes",
                    value=type(self._indexes).__name__,
                )
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

    def _allocate_catalog_generation_nonce(self, catalog: Catalog) -> int:
        """Allocate one v2 generation identity outside every known ownership domain.

        The provider's storage probe excludes unreachable nonced files, while this inventory
        also excludes catalog-owned generations and the durable identities of process-local
        registrations.  Adding each planned definition to the working catalog before asking for
        the next nonce makes a multi-index DDL one collision-free allocation sequence.
        """
        manager = self.require_indexes()
        allocate = getattr(manager, "_allocate_detached_generation_nonce", None)
        if not callable(allocate):
            raise GrafxUnsupportedOperation(
                "Catalog-v2 DDL needs the index manager's bounded artifact-nonce provider.",
                field="artifact_nonce",
                value=type(manager).__name__,
            )
        occupied = {
            generation.artifact_nonce
            for definition in catalog.index_definitions()
            for generation in definition.generations
        }
        projected = getattr(manager, "_registered_artifact_nonces", None)
        if callable(projected):
            occupied.update(projected())
        else:
            # Compatibility for a narrow/alternative registry without the optimized v2
            # projection. Its registered objects retain the original validating header route.
            listing = getattr(manager, "indexes", None)
            if callable(listing):
                for index in listing():
                    header = index.open()
                    artifact_nonce = getattr(header, "artifact_nonce", 0)
                    if (
                        isinstance(artifact_nonce, int)
                        and not isinstance(artifact_nonce, bool)
                        and artifact_nonce > 0
                    ):
                        occupied.add(artifact_nonce)
        return int(allocate(occupied))

    def _automatic_index_namespace_available(
        self,
        definition: IndexDefinition,
        catalog: Catalog,
    ) -> bool:
        """Preserve the supported scan-only result of an automatic-name collision."""
        if catalog.has_index_definition(definition.name):
            return False
        manager = self.require_indexes()
        listing = getattr(manager, "indexes", None)
        if not callable(listing):
            return True
        for current in listing():
            current_definition = getattr(current, "definition", None)
            if (
                getattr(current_definition, "registry_key", None)
                != definition.registry_key
            ):
                continue
            return (
                getattr(current_definition, "table_id", None) == definition.table_id
                and getattr(current_definition, "table_name", None)
                == definition.table_name
            )
        return True

    def _sized_automatic_exact_definition(
        self, definition: IndexDefinition
    ) -> IndexDefinition:
        """Apply this connection's hint only to a new automatic exact generation."""

        if self._automatic_index_expected_cardinality is None:
            return definition
        return replace(
            definition,
            bucket_count=self._automatic_index_bucket_count,
        )

    def _plan_catalog_exact_generation(
        self,
        definition: IndexDefinition,
        catalog: Catalog,
        *,
        expected_cardinality: int | None = None,
        previous: CatalogIndexDefinition | None = None,
    ) -> IndexDefinition:
        """Add one ACTIVE nonced generation to the transaction's v2 catalog clone."""
        nonce = self._allocate_catalog_generation_nonce(catalog)
        generation = IndexGenerationDescriptor(
            artifact_nonce=nonce,
            bucket_count=definition.bucket_count,
            state=IndexGenerationState.ACTIVE,
        )
        if previous is None:
            logical = CatalogIndexDefinition(
                name=definition.name,
                table_id=definition.table_id,
                table_name=definition.table_name,
                positions=definition.positions,
                visibility=definition.visibility,
                key_derivation=definition.key_derivation,
                layout=definition.layout,
                automatic=True,
                expected_cardinality=expected_cardinality,
                generations=(generation,),
            )
            catalog.add_index_definition(logical)
        else:
            logical = replace(
                previous,
                expected_cardinality=(
                    previous.expected_cardinality
                    if previous.expected_cardinality is not None
                    else expected_cardinality
                ),
                generations=tuple(
                    sorted(
                        (
                            *(item.mark_stale() for item in previous.generations),
                            generation,
                        ),
                        key=lambda item: item.artifact_nonce,
                    )
                ),
            )
            catalog.replace_index_definition(logical)
        return logical.runtime_definition(generation)

    def _materialize_catalog_exact_generation(
        self,
        definition: IndexDefinition,
        txn: object,
        undo: list[_SchemaEffect],
        *,
        committed_table: TableDef | None = None,
    ) -> None:
        """Create and observe one v2 generation without granting committed authority early."""
        if committed_table is None:
            if definition.layout is IndexLayout.ORDERED:
                from okto_grafx.engine.ordered_index import OrderedIndex

                candidate = OrderedIndex(
                    definition, self._pool, self._metrics.sink
                )
            else:
                candidate = HashIndex(
                    definition, self._pool, self._metrics.sink
                )
        else:
            # `_schema` already owns COMMIT_SECTION through schema_artifact_section.  Grafx
            # materialises heap rows only inside that same section, so the durable heap cannot
            # move during this scan even though user staging continues outside it.  A writer
            # lease is intentionally unnecessary here: the generation is an exclusive-created,
            # unreachable orphan until the later ordinary commit acquires its lease, re-runs
            # OCC over every table partition declared above, and publishes catalog authority.
            build = getattr(
                self.require_indexes(), "_build_detached_exact_generation", None
            )
            if not callable(build):
                raise GrafxUnsupportedOperation(
                    "An identity index over an existing endpoint needs detached exact-index "
                    "construction.",
                    field="indexes",
                    value=type(self.require_indexes()).__name__,
                    table=committed_table.name,
                )
            candidate = build(definition, self._published_lsn_for_new_index())
        registered = self._stage_schema_index(
            candidate,
            txn,
            undo,
            detached=committed_table is not None,
        )
        if committed_table is None:
            # A new table's empty nonced generation has no logical WAL record from which its
            # header and bucket pages could be reconstructed.  Put that physical foundation on
            # stable storage before the later catalog commit is allowed to name it.  Detached
            # generations over committed tables already cross this barrier in their builder.
            self._pool.checkpoint(registered.file)

    def _validate_index_build_entry_budget(
        self,
        generations: Sequence[tuple[IndexDefinition, TableDef | None]],
        through_lsn: Lsn,
        txn: object,
    ) -> None:
        """Refuse an oversized v2 DDL batch before its first generation file exists."""

        limit = self._max_index_build_entries
        if limit is None:
            return
        observed = 0
        for definition, committed_table in generations:
            # A table declared by this statement has no durable heap versions yet.  Later row
            # staging is governed by the ordinary statement/transaction budgets and does not
            # belong to this detached shadow-build admission decision.
            if committed_table is None:
                continue
            count = getattr(
                self.require_indexes(),
                "_count_detached_exact_generation_entries",
                None,
            )
            if not callable(count):
                raise GrafxUnsupportedOperation(
                    "Index-build admission needs exact detached-generation accounting.",
                    operation="create catalog-v2 indexes",
                    field="indexes",
                    value=type(self.require_indexes()).__name__,
                )
            observed += count(
                definition,
                through_lsn,
                remaining=limit - observed,
            )
            if observed > limit:
                txn_id = getattr(txn, "txn_id", None)
                raise GrafxTransactionBudgetExceeded(
                    f"Index shadow-build batch for transaction {txn_id} would exceed "
                    f"max_index_build_entries: limit {limit}, observed {observed}.",
                    field="max_index_build_entries",
                    limit=limit,
                    observed=observed,
                    txn_id=txn_id,
                )

    def _committed_table_matching(self, table: TableDef) -> TableDef | None:
        """Return the exact committed table, excluding a same-id speculative lookalike."""
        try:
            committed = self._catalog.catalog.table_by_id(table.table_id)
        except GrafxError:
            return None
        return committed if committed == table else None

    @staticmethod
    def _declare_complete_table_read(txn: object, table: TableDef) -> None:
        """Fence every OCC partition before deriving an identity generation from the heap."""
        owner = getattr(txn, "owner", None)
        partitions = getattr(owner, "partitions_per_table", None)
        note_read = getattr(txn, "note_read", None)
        if (
            isinstance(partitions, bool)
            or not isinstance(partitions, int)
            or partitions < 1
            or not callable(note_read)
        ):
            raise GrafxTransactionStateError(
                "Catalog-v2 identity construction needs a transaction that can fence every "
                "table partition.",
                field="transaction",
                value=type(txn).__name__,
                table=table.name,
            )
        for partition in range(partitions):
            note_read(partition_key(table.table_id, partition))

    def _plan_endpoint_identity_generations(
        self,
        relation: TableDef,
        catalog: Catalog,
        txn: object,
    ) -> tuple[tuple[IndexDefinition, TableDef | None], ...]:
        """Plan missing ACTIVE RecordId generations for a new relation's endpoint tables."""
        planned: list[tuple[IndexDefinition, TableDef | None]] = []
        seen: set[int] = set()
        published = self._published_lsn_for_new_index()
        for endpoint_name in (relation.from_table, relation.to_table):
            endpoint = catalog.table(str(endpoint_name))
            if endpoint.table_id in seen:
                continue
            seen.add(endpoint.table_id)
            name = identity_index_name(endpoint.table_id)
            previous = (
                catalog.index_definition(name)
                if catalog.has_index_definition(name)
                else None
            )
            committed = self._committed_table_matching(endpoint)
            active_generation = (
                None if previous is None else previous.active_generation()
            )
            if active_generation is not None:
                manager = self.require_indexes()
                scoped = getattr(manager, "active_indexes_for", None)
                if not callable(scoped):
                    raise GrafxUnsupportedOperation(
                        "Catalog-v2 DDL needs transaction-scoped index authority.",
                        field="indexes",
                        value=type(manager).__name__,
                        index=name,
                    )
                expected = previous.runtime_definition(active_generation)
                matches = tuple(
                    index
                    for index in scoped(
                        endpoint.table_id,
                        table_name=endpoint.name,
                        table=endpoint,
                        txn=txn,
                        catalog=catalog,
                    )
                    if getattr(index, "definition", None) == expected
                )
                if len(matches) != 1:
                    raise GrafxIndexError(
                        f"ACTIVE identity index {name!r} has {len(matches)} transaction-scoped "
                        "physical stores; exactly one is required.",
                        field="index_authority",
                        index=name,
                        table=endpoint.name,
                        count=len(matches),
                    )
                active_index = matches[0]
                required = (
                    published
                    if committed is None
                    else self._heap.committed_high_water(committed)
                )
                stale = bool(
                    active_index.check_freshness(
                        published,
                        required_lsn=required,
                        persist=False,
                    )
                )
                if not stale:
                    continue
                if committed is None:
                    raise GrafxIndexError(
                        f"Speculative ACTIVE identity index {name!r} became stale before "
                        "relationship DDL could use it.",
                        field="index_authority",
                        index=name,
                        table=endpoint.name,
                        retryable=True,
                    )

            if committed is None:
                visible_rows = 0
            else:
                # The sizing scan and detached full-history build rely on the same committed
                # table.  Fence all of it before either read; the statement mark restores this
                # interest set if any later definition or artifact refuses.
                self._declare_complete_table_read(txn, committed)
                visible_rows = sum(
                    1
                    for _ref, _version in self._heap.scan(
                        committed, Snapshot(published)
                    )
                )
            expected, sized_bucket_count = identity_index_sizing(
                visible_rows,
                expected_cardinality=self._automatic_index_expected_cardinality,
            )
            bucket_count = (
                active_generation.bucket_count
                if active_generation is not None
                else max(item.bucket_count for item in previous.generations)
                if previous is not None and previous.generations
                else sized_bucket_count
            )
            provisional = IndexDefinition(
                name=name,
                table_id=endpoint.table_id,
                table_name=endpoint.name,
                positions=(),
                visibility=IndexVisibility.EXACT,
                bucket_count=bucket_count,
                key_derivation=RECORD_ID_KEY_DERIVATION,
            )
            runtime = self._plan_catalog_exact_generation(
                provisional,
                catalog,
                expected_cardinality=expected,
                previous=previous,
            )
            planned.append((runtime, committed))
        return tuple(planned)

    def _record_skipped_index(self, table_name: str, undo: list[_SchemaEffect]) -> None:
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
        *,
        catalog: Catalog | None = None,
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
        if table.primary_key is None:
            return
        if catalog is not None and catalog.format_version == CATALOG_FORMAT_VERSION:
            definitions = tuple(
                self._sized_automatic_exact_definition(definition)
                for definition in automatic_index_definitions(table)
                if definition.visibility is IndexVisibility.EXACT
            )
            accepted = tuple(
                definition
                for definition in definitions
                if self._automatic_index_namespace_available(definition, catalog)
            )
            if len(accepted) != 1:
                statistics["indexes_skipped"] = statistics.get("indexes_skipped", 0) + 1
                self._record_skipped_index(table.name, undo)
            if not accepted:
                return
            planned = tuple(
                self._plan_catalog_exact_generation(
                    definition,
                    catalog,
                    expected_cardinality=self._automatic_index_expected_cardinality,
                )
                for definition in accepted
            )
            # Prove the complete catalog value before the first physical artifact is created.
            # A later failure can leave a uniquely named orphan, but never a process-visible
            # partial authority or a semantic error discovered only after file creation.
            catalog.serialize()
            self._validate_index_build_entry_budget(
                tuple((definition, None) for definition in planned),
                self._published_lsn_for_new_index(),
                txn,
            )
            for definition in planned:
                self._materialize_catalog_exact_generation(
                    definition,
                    txn,
                    undo,
                )
            statistics["indexes_created"] = statistics.get("indexes_created", 0) + len(
                planned
            )
            return
        if self._indexes is None:
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
        *,
        catalog: Catalog | None = None,
    ) -> None:
        """Create the two indexes covering the endpoints of a relationship table just declared.

        Same moment, same rules as the primary key's index: the pair exists when the table is
        declared, the table is EMPTY so the indexes cover everything there is to cover and are
        told so, and the accelerator DECLINES rather than failing the statement or a later open
        -- a table whose endpoint index cannot be created traverses by the scan it always did,
        and the name is reported through :attr:`skipped_indexes`.
        """
        if catalog is not None and catalog.format_version == CATALOG_FORMAT_VERSION:
            endpoint_definitions = tuple(
                self._sized_automatic_exact_definition(definition)
                for definition in automatic_index_definitions(table)
                if definition.visibility is IndexVisibility.EXACT
            )
            accepted_endpoint_definitions = tuple(
                definition
                for definition in endpoint_definitions
                if self._automatic_index_namespace_available(definition, catalog)
            )
            endpoint_generations = tuple(
                self._plan_catalog_exact_generation(
                    definition,
                    catalog,
                    expected_cardinality=self._automatic_index_expected_cardinality,
                )
                for definition in accepted_endpoint_definitions
            )
            identity_generations = self._plan_endpoint_identity_generations(
                table,
                catalog,
                txn,
            )
            # This catches missing endpoint identity and every logical namespace collision
            # before any generation file is created.  Physical failures after this point remain
            # orphan-safe by nonce and statement unwind removes all process-local eligibility.
            catalog.serialize()
            self._validate_index_build_entry_budget(
                tuple((definition, None) for definition in endpoint_generations)
                + identity_generations,
                self._published_lsn_for_new_index(),
                txn,
            )
            for definition in endpoint_generations:
                self._materialize_catalog_exact_generation(
                    definition,
                    txn,
                    undo,
                )
            for definition, committed_table in identity_generations:
                self._materialize_catalog_exact_generation(
                    definition,
                    txn,
                    undo,
                    committed_table=committed_table,
                )
            created = len(endpoint_generations) + len(identity_generations)
            if created:
                statistics["indexes_created"] = (
                    statistics.get("indexes_created", 0) + created
                )
            if len(accepted_endpoint_definitions) < 2:
                statistics["indexes_skipped"] = statistics.get("indexes_skipped", 0) + 1
                self._record_skipped_index(table.name, undo)
            return
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


def _scanned_node_versions(
    engine: QueryEngine,
    table: TableDef,
    snapshot: object,
    materialized_positions: frozenset[int] | None,
) -> Iterator[tuple[RecordRef, HeapVersion]]:
    """Choose the closed projected scan only for the exact built-in heap."""
    if materialized_positions is not None and type(engine.heap) is HeapStore:
        return engine.heap.scan_projected(
            table,
            snapshot,  # type: ignore[arg-type]
            materialized_positions,
        )
    return engine.heap.scan(table, snapshot)  # type: ignore[arg-type]


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
    projections = getattr(context, "node_scan_projections", {}).get(id(node), {})
    for row in engine._rows(node.child, context):
        for table, changed, inserted in views:
            for ref, version in _scanned_node_versions(
                engine,
                table,
                snapshot,
                projections.get(table.table_id),
            ):
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
    projection = (
        getattr(context, "node_scan_projections", {})
        .get(id(node), {})
        .get(node.table.table_id)
    )
    for row in engine._rows(node.child, context):
        yield from _logical_node_rows_for_input(
            engine,
            table=node.table,
            variable=node.variable,
            input_row=row,
            context=context,
            snapshot=snapshot,
            changed=changed,
            inserted=inserted,
            single_source=single_source,
            materialized_positions=projection,
        )


def _logical_node_rows_for_input(
    engine: QueryEngine,
    *,
    table: TableDef,
    variable: str,
    input_row: _Row,
    context: _Context,
    snapshot: object,
    changed: Mapping[object, tuple[Value, ...] | None],
    inserted: Sequence[tuple[object, tuple[Value, ...]]],
    single_source: bool,
    materialized_positions: frozenset[int] | None = None,
) -> Iterator[_Row]:
    """Yield one table's scan-equivalent rows for an already-produced input row."""

    for ref, version in _scanned_node_versions(
        engine, table, snapshot, materialized_positions
    ):
        if ref in changed:
            latest = changed[ref]
            if latest is None:
                continue
            version = replace(version, values=latest)
        bindings = {} if single_source else dict(input_row.bindings)
        bindings[variable] = RowBinding(
            variable=variable,
            table=table,
            ref=ref,
            version=version,
        )
        context.count("rows_scanned")
        yield _Row(bindings=bindings)
    for reference, values in inserted:
        bindings = {} if single_source else dict(input_row.bindings)
        bindings[variable] = _pending_binding(
            variable,
            table,
            values,
            reference=reference,
        )
        context.count("rows_scanned")
        yield _Row(bindings=bindings)


_ENCODING_COMPLETE_EXACT_TYPES: frozenset[ValueType] = frozenset(
    {
        ValueType.BOOL,
        ValueType.INT64,
        ValueType.STRING,
        ValueType.BYTES,
        ValueType.TIMESTAMP,
        ValueType.UUID,
    }
)
"""Scalar kinds whose stored bytes are complete for the language's equality relation."""


def _string_probe_frontier(
    table: TableDef, position: int, values: tuple[object, ...]
) -> int | None:
    """Return the distinct encoded-key count of exact ASCII ``str`` probes of a STRING column.

    ``index_key`` encodes a STRING value as its tag, length and UTF-8 bytes, an injective map:
    two ``str`` probes are the same key exactly when they are equal, so the distinct count of
    the encoding route is known without encoding -- provided the encoding route would have
    accepted every probe.  Only ASCII text is provably encodable without encoding it (a lone
    surrogate is an exact ``str`` that UTF-8 refuses), so any non-ASCII probe returns ``None``.
    ``None`` is not a count: it means some probe is not an exact ASCII ``str`` (a subclass, a
    number, a compound value, non-ASCII text), and the caller must take the encoding route,
    which stays the single oracle for refusals and cross-representation probes, raising exactly
    what it raised before and at the same point.  A ``None`` probe is skipped exactly as the
    encoder skips it.
    """
    if table.columns[position].type is not ValueType.STRING:
        return None
    distinct: set[str] = set()
    for value in values:
        if value is None:
            continue
        if type(value) is not str or not value.isascii():
            return None
        distinct.add(value)
    return len(distinct)


def _exact_probe_is_encoding_complete(
    table: TableDef,
    positions: Sequence[int],
    values: Sequence[Value],
) -> bool:
    """Say whether one encoded key can represent every row equal to these probe values.

    The query language deliberately compares INT64 and DOUBLE as numbers, and treats ``-0.0``
    and ``0.0`` as equal.  Durable index keys retain the stored type tag and IEEE sign bit, so a
    single encoded probe is incomplete for those cross-representation cases.  Nested values can
    contain the same numeric cases, and MAP equality is independent of insertion order while its
    storage encoding is not.  Those probes use the canonical scan before consuming any index
    result; exact scalar probes retain the O(1)-directory access path.
    """

    for position, value in zip(positions, values):
        if value is None:
            return False
        observed = value_type_of(value)
        declared = table.columns[position].type
        if observed is not declared:
            return False
        if observed in _ENCODING_COMPLETE_EXACT_TYPES:
            continue
        if observed is ValueType.DOUBLE:
            number = float(value)  # type: ignore[arg-type]
            if not isnan(number) and number != 0.0:
                continue
        return False
    return True


def _index_lookup_versions(
    engine: QueryEngine,
    manager: object,
    name: str,
    key: bytes,
    snapshot: object,
    *,
    reuse_validated_version: bool,
    ended: Collection[object],
    selected_index: object | None = None,
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
    if selected_index is not None:
        visibility = getattr(selected_index, "visibility", None)
        validated_versions = getattr(manager, "validated_versions", None)
        if (
            reuse_validated_version
            and visibility is IndexVisibility.EXACT
            and callable(validated_versions)
        ):
            yield from validated_versions(selected_index, key, snapshot)
            return
        validated = getattr(manager, "validated", None)
        if visibility is IndexVisibility.EXACT:
            refs = (
                validated(selected_index, key, snapshot)
                if callable(validated)
                else getattr(manager, "lookup")(name, key, snapshot)
            )
        else:
            index_lookup = getattr(selected_index, "lookup", None)
            if not callable(index_lookup):
                refs = getattr(manager, "lookup")(name, key, snapshot)
            else:
                refs = index_lookup(key, snapshot)
    elif reuse_validated_version and callable(lookup_versions):
        yield from lookup_versions(name, key, snapshot)
        return
    else:
        refs = getattr(manager, "lookup")(name, key, snapshot)
    for ref in refs:
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
    logical_overlay: (
        tuple[
            dict[object, tuple[Value, ...] | None],
            list[tuple[object, tuple[Value, ...]]],
        ]
        | None
    ) = None

    def scan_overlay() -> tuple[
        dict[object, tuple[Value, ...] | None],
        list[tuple[object, tuple[Value, ...]]],
    ]:
        nonlocal logical_overlay
        if logical_overlay is None:
            logical_overlay = _transaction_row_view(
                context, node.table, include_held=False
            )
        return logical_overlay

    primary_state = (
        _transaction_primary_key_state(engine, context, node.table, primary_position)
        if reuse_validated_version and primary_position is not None
        else None
    )
    single_source = isinstance(node.child, SingleRow)
    selected_index: object | None = None
    index_resolved = False
    for row in engine._rows(node.child, context):
        values = tuple(
            _as_value(_evaluate(expression, row, context))
            for expression in node.key_values
        )
        # ``x = NULL`` is UNKNOWN for every x, including NULL.  The durable key format can
        # encode NULL, but probing it would turn that unknown predicate into matching rows.
        if any(value is None for value in values):
            continue
        if not _exact_probe_is_encoding_complete(node.table, positions, values):
            # Decide the scan fallback before resolving or consuming the active store.  The
            # index bytes distinguish representations which query equality intentionally joins
            # (INT64/DOUBLE, signed zero and nested numeric values), so a single hash probe could
            # otherwise omit true rows.  Filter here because the planner correctly consumed the
            # equality terms when it chose IndexSeek.
            changed, inserted = scan_overlay()
            for candidate in _logical_node_rows_for_input(
                engine,
                table=node.table,
                variable=node.variable,
                input_row=row,
                context=context,
                snapshot=snapshot,
                changed=changed,
                inserted=inserted,
                single_source=single_source,
            ):
                binding = candidate.bindings[node.variable]
                if all(
                    _equal(binding.version.values[position], value)
                    for position, value in zip(positions, values)
                ):
                    yield candidate
            continue
        if not index_resolved:
            selected_index = _catalog_active_index(
                manager,
                node.index,
                context.schema(),
                txn=getattr(context, "txn", None),
                projection=getattr(context, "index_authority", None),
            )
            index_resolved = True
        template: list[Value] = [None] * arity
        for position, value in zip(positions, values):
            template[position] = value
        key = index_key(template, positions)
        statement_primary: _PrimaryKeyStatementMemo | None = None
        overlay_candidates: tuple[tuple[object, _PrimaryKeyOutcome], ...] = ()
        if primary_state is not None and primary_position is not None:
            # Refresh the held-row suffix per driving row.  An UNWIND/SET statement can change
            # this same table while the seek pipeline is still producing input rows.
            statement_primary = _statement_primary_key_state(
                context, node.table, primary_position, primary_state
            )
            selected = _primary_key_seek_candidates(
                primary_state, statement_primary, values[0]
            )
            if selected is None:
                # A legacy insert without a pending identity cannot be reconstructed from the
                # incremental ownership fold.  Preserve its established duck-typed semantics by
                # using the complete logical scan for this one probe.
                changed, inserted = scan_overlay()
                for candidate in _logical_node_rows_for_input(
                    engine,
                    table=node.table,
                    variable=node.variable,
                    input_row=row,
                    context=context,
                    snapshot=snapshot,
                    changed=changed,
                    inserted=inserted,
                    single_source=single_source,
                ):
                    binding = candidate.bindings[node.variable]
                    if _equal(binding.version.values[primary_position], values[0]):
                        yield candidate
                continue
            overlay_candidates = selected
        yielded: set[object] = set()
        for ref, version in _index_lookup_versions(
            engine,
            manager,
            node.index,
            key,
            snapshot,
            reuse_validated_version=reuse_validated_version,
            ended=ended,
            selected_index=selected_index,
        ):
            if ref in ended:
                continue  # ended by this transaction: the same rule the scan applies
            if primary_state is not None and statement_primary is not None:
                replaced, outcome = _primary_key_current_outcome(
                    primary_state, statement_primary, ref
                )
                if replaced:
                    if outcome is None or outcome.operation is RowOperation.DELETE:
                        continue
                    version = replace(version, values=outcome.values)
            if not all(
                _equal(version.values[position], value)
                for position, value in zip(positions, values)
            ):
                # Hash hits remain candidates.  The manager proves that the heap still derives
                # this key; this second, cheap check proves that key-byte equality also means
                # query-language equality before the planner's consumed predicate disappears.
                continue
            bindings = {} if single_source else dict(row.bindings)
            bindings[node.variable] = RowBinding(
                variable=node.variable, table=node.table, ref=ref, version=version
            )
            context.count("rows_seeked")
            yielded.add(ref)
            yield _Row(bindings=bindings)
        for reference, outcome in overlay_candidates:
            if reference in yielded or outcome.operation is RowOperation.DELETE:
                continue
            if isinstance(reference, PendingRowRef):
                binding = _pending_binding(
                    node.variable,
                    node.table,
                    outcome.values,
                    reference=reference,
                )
            else:
                version = replace(engine.heap.read(reference), values=outcome.values)
                binding = RowBinding(
                    variable=node.variable,
                    table=node.table,
                    ref=reference,
                    version=version,
                )
            bindings = {} if single_source else dict(row.bindings)
            bindings[node.variable] = binding
            context.count("rows_seeked")
            yield _Row(bindings=bindings)


_EDGE_LOOKUP_FAN_LIMIT: int = RELATIONSHIP_LOOKUP_FRONTIER_LIMIT
"""Distinct starts from an unknown/bounded producer served before a grouped scan wins.

Chosen from the shape of the two costs, not tuned to a machine: a lookup costs a few bucket-page
reads however large the edge table is, and the grouped scan costs the whole edge table once.
NodeScan and AllNodesScan frontiers bypass this limit and scan immediately because their plan
already promises a whole-table walk. Unknown producers retain the conservative hybrid fallback."""


def _closed_vector_free_landings(root: PlanNode) -> frozenset[int]:
    """Prove one typed, single-hop read cannot inspect any landing vector.

    Keep the same traversal, row order, frontier, physical checks and admission.
    Only scalar projections/filters/aggregates over a single node source qualify.
    WITH, UNION, optional/range/path traversal, writes and unknown operators keep
    full rows. A bare target or a target vector anywhere in the pipeline declines.
    """
    expressions: list[Expression] = []
    planned = root
    while True:
        kind = type(planned)
        if kind is FilterRows:
            expressions.append(planned.predicate)
        elif kind is ProjectRows:
            expressions.extend(item.expression for item in planned.items)
        elif kind is AggregateRows:
            if planned.preserve_group_bindings:
                return frozenset()
            expressions.extend(item.expression for item in planned.grouping)
            expressions.extend(item.call for item in planned.aggregations)
        elif kind is SortRows:
            expressions.extend(item.expression for item in planned.keys)
            expressions.extend(value for value in (
                planned.retained_limit, planned.retained_skip,
            ) if value is not None)
        elif kind in (LimitRows, SkipRows):
            expressions.append(planned.count)
        elif kind is not DistinctRows:
            break
        planned = planned.child
    if type(planned) is not TraverseRelationship:
        return frozenset()
    hop = planned
    if (
        hop.min_hops != 1 or hop.max_hops != 1 or hop.path_variable is not None
        or hop.target_bound or hop.target_table is None or hop.target == hop.source
        or hop.direction is Direction.UNDIRECTED
    ):
        return frozenset()
    vector_names = {
        column.name for column in hop.target_table.columns
        if column.type in VECTOR_VALUE_TYPES
    }
    if not vector_names:
        return frozenset()
    source = hop.child
    while type(source) is FilterRows:
        expressions.append(source.predicate)
        source = source.child
    if (
        type(source) not in (NodeScan, IndexSeek)
        or type(source.child) is not SingleRow or source.variable != hop.source
    ):
        return frozenset()
    if type(source) is IndexSeek:
        expressions.extend(source.key_values)
    pending = list(expressions)
    while pending:
        expression = pending.pop()
        if (
            type(expression) is FunctionCall and expression.name.upper() == LABEL_FUNCTION
            and len(expression.arguments) == 1
            and type(expression.arguments[0]) is Variable
            and expression.arguments[0].name == hop.target
            and not expression.named_arguments and not expression.distinct and not expression.star
        ):
            continue
        if type(expression) is Property and type(expression.subject) is Variable:
            if expression.subject.name == hop.target and expression.key in vector_names:
                return frozenset()
            continue
        if type(expression) is Variable and expression.name == hop.target:
            return frozenset()
        pending.extend(expression.children())
    return frozenset((id(hop),))


def _closed_node_scan_projections(
    root: PlanNode,
) -> dict[int, dict[int, frozenset[int]]]:
    """Plan projections for one closed, read-only, single-source node pipeline.

    Projection is declined unless the entire physical path is a linear combination of filters,
    result projection, ordering, DISTINCT and a window over exactly one NodeScan/AllNodesScan
    driven by SingleRow. Every expression is walked: direct properties of the scan variable are
    retained, while a bare occurrence of that variable declines the optimization because it may
    expose or inspect the complete entity. Expressions over other computed aliases do not need
    stored columns. This makes the proof intentionally narrower than the query language.
    """
    expressions: list[Expression] = []
    planned = root
    while True:
        kind = type(planned)
        if kind is FilterRows:
            expressions.append(planned.predicate)  # type: ignore[attr-defined]
        elif kind is ProjectRows:
            expressions.extend(
                item.expression
                for item in planned.items  # type: ignore[attr-defined]
            )
        elif kind is SortRows:
            expressions.extend(
                item.expression
                for item in planned.keys  # type: ignore[attr-defined]
            )
            if planned.retained_limit is not None:  # type: ignore[attr-defined]
                expressions.append(planned.retained_limit)  # type: ignore[attr-defined]
            if planned.retained_skip is not None:  # type: ignore[attr-defined]
                expressions.append(planned.retained_skip)  # type: ignore[attr-defined]
        elif kind in (LimitRows, SkipRows):
            expressions.append(planned.count)  # type: ignore[attr-defined]
        elif kind is not DistinctRows:
            break
        planned = planned.child  # type: ignore[attr-defined]

    if type(planned) is NodeScan:
        if type(planned.child) is not SingleRow:
            return {}
        variable = planned.variable
        tables = (planned.table,)
    elif type(planned) is AllNodesScan:
        if type(planned.child) is not SingleRow:
            return {}
        variable = planned.variable
        tables = planned.tables
    else:
        return {}

    property_names: set[str] = set()
    pending = list(expressions)
    while pending:
        expression = pending.pop()
        if (
            type(expression) is FunctionCall
            and expression.name.upper() == LABEL_FUNCTION
            and len(expression.arguments) == 1
            and type(expression.arguments[0]) is Variable
            and expression.arguments[0].name == variable
            and not expression.named_arguments
            and not expression.distinct
            and not expression.star
        ):
            # label(n) reads the binding's table identity, not its stored values.
            continue
        if type(expression) is Property:
            subject = expression.subject
            if type(subject) is Variable:
                if subject.name == variable:
                    property_names.add(expression.key)
                continue
            pending.append(subject)
            continue
        if type(expression) is Variable:
            if expression.name == variable:
                return {}
            continue
        pending.extend(expression.children())

    by_table: dict[int, frozenset[int]] = {}
    for table in tables:
        positions = frozenset(
            position
            for name in property_names
            if (position := table.column_positions.get(name)) is not None
        )
        if len(positions) < len(table.columns):
            by_table[table.table_id] = positions
    return {id(planned): by_table} if by_table else {}


def _short_circuit_traversals(root: PlanNode) -> frozenset[int]:
    """Identify exact one-hop traversals whose consumer can stop them at LIMIT.

    This deliberately recognizes only the narrow linear pipeline whose operators are row-local:
    LIMIT, optional SKIP, projection, and filters. Sort, aggregation, DISTINCT, OPTIONAL, UNION,
    writes, ranges, and every other shape keep the grouped relationship scan. The identity is
    statement-local because plan nodes are immutable and the resulting set never escapes the
    execution context.
    """
    selected: set[int] = set()
    for planned in root.walk():
        if not isinstance(planned, LimitRows):
            continue
        child = planned.child
        while isinstance(child, (SkipRows, ProjectRows, FilterRows)):
            child = child.child
        if (
            isinstance(child, TraverseRelationship)
            and child.min_hops == 1
            and child.max_hops == 1
        ):
            selected.add(id(child))
    return frozenset(selected)


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
    catalog = context.schema()

    def usable(name: str) -> object | None:
        """Return the store when that index is present, this table's own, and fresh."""
        try:
            index = _catalog_active_index(  # type: ignore[arg-type]
                manager,
                name,
                catalog,
                txn=getattr(context, "txn", None),
                projection=getattr(context, "index_authority", None),
            )
        except GrafxError:
            return None
        if index is None:
            return None
        if (
            index.definition.table_id != relationship.table_id
            or index.definition.table_name != relationship.name
        ):
            return None  # a name collision, not this table's index
        if getattr(index, "stale", False):
            return None
        return index

    from_index = (
        usable(edge_from_index_name(relationship.name))
        if callable(lookup) and outgoing
        else None
    )
    to_index = (
        usable(edge_to_index_name(relationship.name))
        if callable(lookup) and incoming
        else None
    )
    indexed = ((not outgoing) or from_index) and ((not incoming) or to_index)
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
                cast(str, getattr(from_index, "name", None)),
                index_key((cast(Value, record_id), None), (0,)),
                snapshot,
                reuse_validated_version=False,
                ended=ended,
                selected_index=from_index,
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
                cast(str, getattr(to_index, "name", None)),
                index_key((None, cast(Value, record_id)), (1,)),
                snapshot,
                reuse_validated_version=False,
                ended=ended,
                selected_index=to_index,
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
    dirty_tables = _intent_table_ids(engine, context.txn)
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
    vector_free = (
        id(node) in context.vector_free_landings
        and type(engine.heap) is HeapStore
        and type(engine._indexes) is IndexManager
        and getattr(engine.heap.read, "__func__", None) is _VECTOR_FREE_CANONICAL_READ
        and getattr(engine.heap._decode_version, "__func__", None) is _VECTOR_FREE_CANONICAL_DECODE
        and getattr(engine._indexes.validated_versions, "__func__", None) is _VECTOR_FREE_CANONICAL_VALIDATED
    )

    def node_at(table: TableDef, identity: object) -> tuple[object, HeapVersion] | None:
        """Return one owner-visible node through a single lazy view per landing table."""
        view = landing_views.get(table.table_id)
        if view is None:
            view = _owner_landing_view(engine, context, table, ended)
            landing_views[table.table_id] = view
        return (
            view.get(identity, context, materialize_vectors=False)
            if vector_free else view.get(identity, context)
        )

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
        bounded_frontier=(
            _frontier_is_bounded(node.child, node.source)
            or id(node) in context.short_circuit_traversals
        ),
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
    dirty_tables = _intent_table_ids(engine, context.txn)
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
                    node.direction in (Direction.OUTGOING, Direction.UNDIRECTED)
                    and (node.source_table is None or table.from_table == node.source_table),
                    node.direction in (Direction.INCOMING, Direction.UNDIRECTED)
                    and (node.source_table is None or table.to_table == node.source_table),
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
        matched = False
        for table, steps in walkers:
            for ref, version, next_table, next_id in steps(identity):
                if charge_expansions:
                    context.admit_traversal_expansion()
                if node.target_table is not None and next_table.name != node.target_table:
                    continue
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
                    polymorphic=node.optional and node.target_table is None,
                )
                bindings[node.relationship] = RowBinding(
                    variable=node.relationship,
                    table=table,
                    ref=ref,
                    version=version,
                    polymorphic=node.relationship_polymorphic,
                )
                context.count("rows_scanned")
                candidate = _Row(
                    bindings=bindings, computed=row.computed, columns=row.columns
                )
                if node.predicate is not None and not _predicate_admits(node.predicate, candidate, context):
                    continue
                matched = True
                yield candidate
        if node.optional and not matched:
            bindings = dict(row.bindings)
            bindings[node.target] = None
            bindings[node.relationship] = None
            yield _Row(bindings=bindings, computed=row.computed, columns=row.columns)


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
    dirty_tables = _intent_table_ids(engine, context.txn)
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


def _primary_key_resolution_charge(
    table: TableDef,
    key: bytes,
    group: tuple[tuple[RecordRef, HeapVersion], ...],
) -> int | None:
    """Conservatively meter one retained exact-key answer without affecting its semantics."""
    charge = _PRIMARY_KEY_RESOLUTION_ENTRY_BYTES + len(key) * 2
    if not group:
        miss = _owner_landing_result_bytes(table, None)
        return None if miss is None else charge + miss
    for found in group:
        retained = _owner_landing_result_bytes(table, found)
        if retained is None:
            return None
        charge += retained
    return charge


def _discard_primary_key_resolution_state(
    engine: QueryEngine,
    memo: _PrimaryKeyTxnMemo,
    identity: tuple[object, ...],
    state: _PrimaryKeyResolutionCache,
) -> None:
    """Remove one exact cache and release its complete shared-budget reservation."""
    if memo.resolutions.get(identity) is state:
        memo.resolutions.pop(identity)
    count = len(state.entries)
    if count:
        engine._owner_budget.release(bytes_=state.used_bytes, entries=count)
        memo.resolution_entries -= count
    state.entries.clear()
    state.used_bytes = 0


def _discard_primary_key_resolution_caches(
    engine: QueryEngine, memo: _PrimaryKeyTxnMemo
) -> None:
    """Release all transaction-owned exact-key results; caller holds the endpoint guard."""
    for identity, state in tuple(memo.resolutions.items()):
        _discard_primary_key_resolution_state(engine, memo, identity, state)
    memo.resolution_entries = 0


def _evict_primary_key_resolution_lru(
    engine: QueryEngine,
    memo: _PrimaryKeyTxnMemo,
    state: _PrimaryKeyResolutionCache,
) -> bool:
    """Evict this store's oldest answer, returning whether one existed."""
    if not state.entries:
        return False
    _key, (_group, charge) = state.entries.popitem(last=False)
    state.used_bytes -= charge
    memo.resolution_entries -= 1
    engine._owner_budget.release(bytes_=charge, entries=1)
    return True


def _admit_primary_key_resolution(
    engine: QueryEngine,
    memo: _PrimaryKeyTxnMemo,
    state: _PrimaryKeyResolutionCache,
    key: bytes,
    group: tuple[tuple[RecordRef, HeapVersion], ...],
    charge: int,
) -> bool:
    """Reserve before retaining, evicting only this exact store's own LRU results."""
    while True:
        if memo.resolution_entries >= _PRIMARY_KEY_RESOLUTION_MAX_ENTRIES:
            if not _evict_primary_key_resolution_lru(engine, memo, state):
                return False
            continue
        try:
            engine._owner_budget.reserve(bytes_=charge, entries=1)
        except _OwnerLandingCapacity:
            if not _evict_primary_key_resolution_lru(engine, memo, state):
                return False
        else:
            try:
                state.entries[key] = (group, charge)
            except BaseException:
                engine._owner_budget.release(bytes_=charge, entries=1)
                raise
            state.used_bytes += charge
            memo.resolution_entries += 1
            return True


def _memoized_primary_key_groups(
    engine: QueryEngine,
    context: _Context,
    manager: object,
    store: object,
    keys: tuple[bytes, ...],
    table: TableDef,
    position: int,
    canonical_many: Callable[..., object],
) -> tuple[tuple[tuple[RecordRef, HeapVersion], ...], ...]:
    """Reuse exact PK answers only under all transaction, registry and storage fences.

    Custom managers and monkey-patched implementations keep the canonical multi-key door.  A
    native cache hit still opens a fresh page-0 certificate and companion heap view; only the
    already-proved bucket traversal and heap payload decode are skipped.  Any generation change
    revalidates the complete request inside IndexManager, while a local heap/registry transition
    during a cached read triggers one uncached read before the answer is returned.
    """
    reuse_many = getattr(manager, "validated_versions_many_reusing", None)
    if (
        type(manager) is not IndexManager
        or not callable(reuse_many)
        or getattr(reuse_many, "__func__", None)
        is not _NATIVE_VALIDATED_VERSIONS_MANY_REUSING
        or getattr(canonical_many, "__func__", None)
        is not _NATIVE_VALIDATED_VERSIONS_MANY
    ):
        return tuple(canonical_many(store, keys, context.snapshot))

    revision = getattr(manager, "_registry_revision", None)
    if type(revision) is not int:
        return tuple(canonical_many(store, keys, context.snapshot))
    identified = _revisioned_txn_memo(engine, context.txn)
    if identified is None:
        return tuple(canonical_many(store, keys, context.snapshot))
    txn_id, memo = identified
    snapshot = context.snapshot
    heap_epoch = engine.heap._derived_read_epoch()
    identity = _primary_key_table_identity(table, position)
    observed: _PrimaryKeyResolutionCache | None = None
    generation: object | None = None
    cached: dict[bytes, tuple[tuple[RecordRef, HeapVersion], ...]] = {}
    with engine._endpoint_guard:
        if engine._primary_key_memos.get(txn_id) is memo:
            candidate = memo.resolutions.get(identity)
            if candidate is not None and (
                candidate.store is not store
                or candidate.snapshot is not snapshot
                or candidate.registry_revision != revision
                or candidate.heap_epoch != heap_epoch
            ):
                _discard_primary_key_resolution_state(
                    engine, memo, identity, candidate
                )
                candidate = None
            if candidate is not None:
                observed = candidate
                generation = candidate.generation
                cached = {
                    key: candidate.entries[key][0]
                    for key in dict.fromkeys(keys)
                    if key in candidate.entries
                }

    next_generation, reused, raw_groups = reuse_many(
        store,
        keys,
        snapshot,
        generation=generation,
        cached=cached,
    )
    groups = tuple(raw_groups)
    after_revision = getattr(manager, "_registry_revision", None)
    after_epoch = engine.heap._derived_read_epoch()
    publish_revision = revision
    publish_epoch = heap_epoch
    publishable = after_revision == revision and after_epoch == heap_epoch
    if reused and not publishable:
        # The cached values were correctly bracketed by their index certificate, but a local
        # heap/registry fence moved while the statement was in flight.  Re-read every key rather
        # than asking callers to reason about which unrelated epoch transition occurred.
        publish_revision = after_revision
        publish_epoch = after_epoch
        next_generation, reused, raw_groups = reuse_many(
            store,
            keys,
            snapshot,
            generation=None,
            cached={},
        )
        groups = tuple(raw_groups)
        publishable = (
            type(publish_revision) is int
            and getattr(manager, "_registry_revision", None) == publish_revision
            and engine.heap._derived_read_epoch() == publish_epoch
        )

    if not publishable or type(publish_revision) is not int:
        return groups

    # Preserve the caller's established structured refusal for a hostile implementation that
    # violates the aligned-batch contract; it validates the length immediately after return.
    if len(groups) != len(keys):
        return groups
    unique_groups = dict(zip(keys, groups, strict=True))
    with engine._endpoint_guard:
        if engine._primary_key_memos.get(txn_id) is not memo:
            return groups
        current = memo.resolutions.get(identity)
        if current is not observed:
            # A concurrent resolver or settlement replaced the exact state observed above.  This
            # answer remains valid under its own certificate, but must not overwrite the newer
            # cache authority.
            return groups
        if current is None:
            current = _PrimaryKeyResolutionCache(
                store=store,
                snapshot=snapshot,
                registry_revision=publish_revision,
                heap_epoch=publish_epoch,
                generation=next_generation,
            )
            memo.resolutions[identity] = current
        elif not reused or current.generation != next_generation:
            _discard_primary_key_resolution_state(engine, memo, identity, current)
            current = _PrimaryKeyResolutionCache(
                store=store,
                snapshot=snapshot,
                registry_revision=publish_revision,
                heap_epoch=publish_epoch,
                generation=next_generation,
            )
            memo.resolutions[identity] = current
        else:
            current.registry_revision = publish_revision
            current.heap_epoch = publish_epoch

        for key, group in unique_groups.items():
            if key in current.entries:
                current.entries.move_to_end(key)
                continue
            charge = _primary_key_resolution_charge(table, key, group)
            if charge is not None:
                _admit_primary_key_resolution(
                    engine, memo, current, key, group, charge
                )
        if not current.entries:
            memo.resolutions.pop(identity, None)
    return groups


def _node_multi_key_seek(
    engine: QueryEngine, node: NodeMultiKeySeek, context: _Context
) -> Iterator[_Row]:
    """Read a closed primary-key list of one node table through its exact multi-key index.

    NODE-IN-SEEK.  Capability selection happens before the first lookup: a missing/stale store,
    an engine without the multi-key door, a table this transaction has written, a probe that is
    not a list or a probe the durable key cannot represent completely all execute the retained
    canonical scan, which sits under the very same predicate as this operator, so the scan
    judges, refuses and counts exactly what it judged before.  The durable allocation frontier
    then selects the scan for a small table before any index certificate is opened, by the
    rule the incident seek measured: every probe is a validated bucket read and a landing,
    every scanned row one decode, so a table whose ids ever allocated are at most half the
    distinct probes is cheaper to walk (measured on a 182-row table: 40 probes seek in 21-28 ms
    against a 33-69 ms scan, 500 probes seek in 103-181 ms against a 63-105 ms scan).  Every
    later refusal propagates: falling back after a partial certified read could hide a
    generation replacement or a corrupt heap candidate.  Hits are validated against the heap
    under the snapshot and yielded in heap order, the order the scan yields them, so LIMIT and
    every operator above observe the same sequence either way.
    """

    manager = engine._indexes
    many = getattr(manager, "validated_versions_many", None)
    authority = context.index_authority
    if manager is None or not callable(many) or authority is None:
        yield from engine._rows(node.fallback, context)
        return

    position = node.key_position
    store = authority.named(node.index)
    definition = getattr(store, "definition", None)
    if (
        store is None
        or not isinstance(definition, IndexDefinition)
        or definition.name != node.index
        or definition.table_id != node.table.table_id
        or definition.table_name != node.table.name
        or definition.positions != (position,)
        or definition.key_derivation != COLUMN_KEY_DERIVATION
        or definition.visibility is not IndexVisibility.EXACT
        or getattr(store, "stale", True) is not False
    ):
        yield from engine._rows(node.fallback, context)
        return

    if node.table.table_id in _intent_table_ids(engine, context.txn):
        # An exact index describes only durable heap versions: the owner's pending rows and
        # pending keys are visible only to the scan's logical overlay.
        yield from engine._rows(node.fallback, context)
        return

    raw = _evaluate(node.keys, _Row(bindings={}), context)
    if raw is None:
        # ``x IN NULL`` is UNKNOWN for every row, exactly as the incident seek treats it.
        return
    if type(raw) not in (list, tuple):
        # Preserve the canonical IN refusal (and custom Sequence behaviour) in the fallback.
        yield from engine._rows(node.fallback, context)
        return

    # Freeze a caller-owned list before encoding so one execution never observes a moving
    # parameter frontier while it is opening durable index certificates.
    values = tuple(raw)

    # next_record_id is the durable O(1) upper bound strictly beyond every identity ever
    # allocated for this table; deletions can only overestimate its live cardinality, which
    # conservatively favours the seek.  A table without an extent never allocated a page and
    # holds no stored row (its pending rows were excluded above), so it answers empty without
    # a scan or a certificate; a table whose pages exist keeps the canonical scan, which walks
    # and validates that physical chain.  The comparison uses integers so the boundary is
    # exact and deterministic on every platform.
    extent = engine.heap.extent_of(node.table)
    allocated_upper = 0 if extent is None else extent.next_record_id - FIRST_RECORD_ID

    def small_table() -> Iterator[_Row]:
        if extent is not None:
            yield from engine._rows(node.fallback, context)

    # KGRUN-M3: exact ASCII strings count their distinct keys without encoding; any other
    # probe shape takes the encoding route below, which remains the single oracle.
    exact_frontier = _string_probe_frontier(node.table, position, values)
    if exact_frontier is not None and allocated_upper * 2 <= exact_frontier:
        yield from small_table()
        return

    unique: dict[bytes, Value] = {}
    for value in values:
        if value is None:
            continue
        if not _exact_probe_is_encoding_complete(node.table, (position,), (value,)):
            yield from engine._rows(node.fallback, context)
            return
        template: list[Value] = [None] * node.table.arity
        template[position] = cast(Value, value)
        try:
            key = index_key(template, (position,))
        except (GrafxIndexError, SchemaMismatchError):
            # The encoder refuses only the probe's own shape (a lone surrogate, a form the key
            # cannot carry); the scan compares that probe by value and raises nothing, so the
            # scan keeps the answer.  No certificate has been opened yet.
            yield from engine._rows(node.fallback, context)
            return
        unique.setdefault(key, cast(Value, value))

    if allocated_upper * 2 <= len(unique):
        yield from small_table()
        return

    keys = tuple(unique)
    groups = _memoized_primary_key_groups(
        engine, context, manager, store, keys, node.table, position, many
    )
    if len(groups) != len(keys):
        raise GrafxIndexError(
            f"Multi-key validation for index {node.index!r} returned {len(groups)} result "
            f"groups for {len(keys)} keys.",
            field="index_batch",
            index=node.index,
            expected=len(keys),
            observed=len(groups),
        )
    resolved: dict[RecordId, tuple[object, HeapVersion]] = {}
    for (_key, value), hits in zip(unique.items(), groups, strict=True):
        for ref, version in hits:
            if not _equal(version.values[position], value):
                continue
            previous = resolved.setdefault(version.record_id, (ref, version))
            if previous[0] != ref:
                raise GrafxCorruptionDetected(
                    f"Primary-key index {node.index!r} resolved one snapshot-visible key to "
                    f"multiple rows of {node.table.name!r}.",
                    table=node.table.name,
                    table_id=node.table.table_id,
                    field="primary_key",
                    index=node.index,
                )
    ordered = sorted(
        resolved.values(), key=lambda item: cast(RecordRef, item[0]).encode()
    )
    for ref, version in ordered:
        context.count("rows_seeked")
        yield _Row(
            bindings={
                node.variable: RowBinding(
                    variable=node.variable,
                    table=node.table,
                    ref=ref,
                    version=version,
                )
            }
        )


@dataclass(slots=True)
class _OrderedMergeCandidate:
    """One current table head ordered best-first for Python's minimum heap."""

    key: bytes
    ordinal: int
    ref: RecordRef
    version: HeapVersion

    def __lt__(self, other: _OrderedMergeCandidate) -> bool:
        """Expose larger ordered keys first and retain catalog table order for exact ties."""
        if self.key != other.key:
            return self.key > other.key
        if self.ordinal != other.ordinal:
            return self.ordinal < other.ordinal
        return self.ref.encode() < other.ref.encode()


def _ordered_node_merge(
    engine: QueryEngine, node: OrderedNodeMerge, context: _Context
) -> Iterator[_Row]:
    """Merge the certified descending head of each node table in ``O(K log T)``.

    Every capability and bound is validated before the first ordered iterator is advanced. A
    failure there retains the complete canonical scan. Once a certificate has been opened no
    fallback is legal: all selected rows remain private until every iterator has closed under
    the same certificate, and drift, corruption or an incomplete generation propagates.
    """

    authority = context.index_authority
    if authority is None or type(engine.heap) is not HeapStore:
        yield from engine._rows(node.fallback, context)
        return
    if len(node.tables) != len(node.indexes):
        yield from engine._rows(node.fallback, context)
        return
    if {table.table_id for table in node.tables} & _intent_table_ids(
        engine, context.txn
    ):
        # Ordered artifacts describe durable heap versions only. The owner-visible overlay is
        # implemented by the fallback scan and must remain the single authority for dirty rows.
        yield from engine._rows(node.fallback, context)
        return

    stores: list[object] = []
    definitions: list[IndexDefinition] = []
    for table, name in zip(node.tables, node.indexes, strict=True):
        store = authority.named(name)
        definition = getattr(store, "definition", None)
        timestamp_position = table.column_positions.get(node.timestamp_column)
        string_position = table.column_positions.get(node.string_column)
        if (
            store is None
            or not isinstance(definition, IndexDefinition)
            or timestamp_position is None
            or string_position is None
            or definition.name != name
            or definition.table_id != table.table_id
            or definition.table_name != table.name
            or definition.positions != (timestamp_position, string_position)
            or definition.layout is not IndexLayout.ORDERED
            or definition.key_derivation != ORDERED_KEY_DERIVATION
            or definition.visibility is not IndexVisibility.EXACT
            or getattr(store, "stale", True) is not False
            or not callable(getattr(store, "iter_visible_desc", None))
        ):
            yield from engine._rows(node.fallback, context)
            return
        stores.append(store)
        definitions.append(definition)

    wanted = _window(node.limit, context, "LIMIT")
    if wanted == 0:
        # The canonical path is the authority for eager expression validation at a zero window.
        yield from engine._rows(node.fallback, context)
        return

    upper_keys: list[bytes | None] = [None] * len(stores)
    if node.upper_timestamp is not None and node.upper_string is not None:
        empty = _Row(bindings={})
        upper_timestamp = _evaluate(node.upper_timestamp, empty, context)
        upper_string = _evaluate(node.upper_string, empty, context)
        if not isinstance(upper_timestamp, Timestamp) or type(upper_string) is not str:
            yield from engine._rows(node.fallback, context)
            return
        for ordinal, (table, definition) in enumerate(
            zip(node.tables, definitions, strict=True)
        ):
            values: list[Value] = [None] * table.arity
            values[table.column_index(node.timestamp_column)] = upper_timestamp
            values[table.column_index(node.string_column)] = upper_string
            try:
                upper_keys[ordinal] = definition.key_for(values)
            except (GrafxIndexError, SchemaMismatchError):
                yield from engine._rows(node.fallback, context)
                return

    admits = (
        None
        if node.predicate is None
        else _predicate_admitter(node.predicate, context)
    )
    iterators: list[Iterator[tuple[bytes, RecordRef, HeapVersion]]] = []
    selected: list[_Row] = []
    failure: BaseException | None = None

    def next_admitted(ordinal: int) -> _OrderedMergeCandidate | None:
        iterator = iterators[ordinal]
        table = node.tables[ordinal]
        for key, ref, version in iterator:
            row = _Row(
                bindings={
                    node.variable: RowBinding(
                        variable=node.variable,
                        table=table,
                        ref=ref,
                        version=version,
                        polymorphic=True,
                    )
                }
            )
            context.count("ordered_candidates_examined")
            context.count("rows_scanned")
            if admits is None or admits(row):
                return _OrderedMergeCandidate(key, ordinal, ref, version)
        return None

    try:
        for table, store, upper_key in zip(
            node.tables, stores, upper_keys, strict=True
        ):
            iterator = store.iter_visible_desc(  # type: ignore[attr-defined]
                engine.heap,
                table,
                context.snapshot,
                upper_key=upper_key,
            )
            iterators.append(iter(iterator))

        queue: list[_OrderedMergeCandidate] = []
        for ordinal in range(len(iterators)):
            candidate = next_admitted(ordinal)
            if candidate is not None:
                heappush(queue, candidate)

        while queue and len(selected) < wanted:
            candidate = heappop(queue)
            table = node.tables[candidate.ordinal]
            selected.append(
                _Row(
                    bindings={
                        node.variable: RowBinding(
                            variable=node.variable,
                            table=table,
                            ref=candidate.ref,
                            version=candidate.version,
                            polymorphic=True,
                        )
                    }
                )
            )
            if len(selected) == wanted:
                break
            following = next_admitted(candidate.ordinal)
            if following is not None:
                heappush(queue, following)
    except BaseException as caught:
        failure = caught
        raise
    finally:
        cleanup_failure: BaseException | None = failure
        for iterator in iterators:
            try:
                _close_iterator(iterator)
            except BaseException as close_failure:
                if cleanup_failure is None:
                    cleanup_failure = close_failure
                else:
                    cleanup_failure.add_note(
                        "An ordered table iterator also failed to close with "
                        f"{type(close_failure).__name__}: {close_failure}"
                    )
        if failure is None and cleanup_failure is not None:
            raise cleanup_failure

    context.count("ordered_merge_tables", len(stores))
    context.count("ordered_merge_rows", len(selected))
    yield from selected


def _relationship_incident_seek(
    engine: QueryEngine, node: RelationshipIncidentSeek, context: _Context
) -> Iterator[_Row]:
    """Read a closed endpoint-key union through four exact multi-key indexes.

    Capability selection happens before the first lookup. A missing/stale store or an engine
    without the multi-key door executes the retained canonical plan. Once all four exact stores
    are adopted, the durable allocation frontier selects an edge-first scan for small tables
    before any index certificate is opened.  Every later refusal propagates: falling back after
    a partial certified read could hide a generation replacement or corrupt heap candidate.
    """

    manager = engine._indexes
    many = getattr(manager, "validated_versions_many", None)
    authority = context.index_authority
    if manager is None or not callable(many) or authority is None:
        yield from engine._rows(node.fallback, context)
        return

    def exact_store(
        name: str,
        table: TableDef,
        positions: tuple[int, ...],
    ) -> object | None:
        store = authority.named(name)
        definition = getattr(store, "definition", None)
        if (
            store is None
            or not isinstance(definition, IndexDefinition)
            or definition.name != name
            or definition.table_id != table.table_id
            or definition.table_name != table.name
            or definition.positions != positions
            or definition.key_derivation != COLUMN_KEY_DERIVATION
            or definition.visibility is not IndexVisibility.EXACT
            or getattr(store, "stale", True) is not False
        ):
            return None
        return store

    from_node_index = exact_store(
        node.from_index, node.from_table, (node.from_key_position,)
    )
    to_node_index = exact_store(node.to_index, node.to_table, (node.to_key_position,))
    from_edge_index = exact_store(node.relationship_from_index, node.table, (0,))
    to_edge_index = exact_store(node.relationship_to_index, node.table, (1,))
    if any(
        store is None
        for store in (
            from_node_index,
            to_node_index,
            from_edge_index,
            to_edge_index,
        )
    ):
        yield from engine._rows(node.fallback, context)
        return

    dirty = _intent_table_ids(engine, context.txn)
    if dirty & {
        node.table.table_id,
        node.from_table.table_id,
        node.to_table.table_id,
    }:
        yield from engine._rows(node.fallback, context)
        return

    empty = _Row(bindings={})

    def probe_values(expression: Expression) -> tuple[object, ...] | None:
        """Evaluate one probe list exactly once; None keeps the canonical fallback."""
        raw = _evaluate(expression, empty, context)
        if raw is None:
            return ()
        if type(raw) not in (list, tuple):
            # Preserve the canonical IN refusal (and custom Sequence behaviour) in the fallback.
            return None
        # Freeze a caller-owned list before encoding so one execution never observes a moving
        # parameter frontier while it is opening durable index certificates.
        return tuple(raw)

    def encoded_keys(
        values: tuple[object, ...],
        table: TableDef,
        position: int,
    ) -> tuple[tuple[bytes, Value], ...] | None:
        unique: dict[bytes, Value] = {}
        for value in values:
            if value is None:
                continue
            if not _exact_probe_is_encoding_complete(table, (position,), (value,)):
                return None
            template: list[Value] = [None] * table.arity
            template[position] = cast(Value, value)
            key = index_key(template, (position,))
            unique.setdefault(key, cast(Value, value))
        return tuple(unique.items())

    from_values = probe_values(node.from_keys)
    to_values = probe_values(node.to_keys)
    if from_values is None or to_values is None:
        yield from engine._rows(node.fallback, context)
        return

    # next_record_id is the durable O(1) upper bound that is strictly beyond every identity ever
    # allocated for this relationship table.  Deletions and reservation gaps can only
    # overestimate its live cardinality, which conservatively favours the seek.  page_count is
    # deliberately excluded: it is a repairable chain hint and may lag an interrupted append.
    #
    # Measured crossover: scan when allocated_upper <= 0.5 * distinct endpoint probes.  Use
    # integer arithmetic so the boundary is exact and deterministic on every platform.  This
    # decision happens before validated_versions_many opens the first durable index certificate.
    extent = engine.heap.extent_of(node.table)
    allocated_upper = 0 if extent is None else extent.next_record_id - FIRST_RECORD_ID

    def proved_empty() -> bool:
        """Answer the union of a table that has no page at all without reading anything.

        BATCH-REL-1.  A table without an extent never allocated a page, so it holds no stored
        relationship; the pending ones of this transaction were excluded above (a dirty table
        keeps the canonical plan).  The proof is exclusively the missing extent: a table whose
        pages exist while ``next_record_id`` still sits at the first id keeps the canonical
        scan, which walks and validates that physical chain and may reveal its corruption.

        The scan the former rule chose for a pageless table cost 8-19 ms a layout on the
        production board (its fixed setup: the probe memo, the operator chain) against
        0.6-1.5 ms for the seek, and about forty of its sixty-six layouts are pageless: the old
        selector answered the fan-out in 1.27-1.47 s where every seek took 0.77-0.89 s and the
        per-layout oracle 0.69-0.72 s.  Answering empty here opens no index and reads no page,
        so nothing becomes observable that the scan of nothing left unobserved, and every
        refusal that precedes this point still fires: in the exact-ASCII branch nothing was
        encoded and nothing needs to be; any other probe shape does not enter that branch, so
        the encoder runs first and an unencodable string or a mixed frontier is refused exactly
        as before.  The edge-first branch is what answered, so its statistic is kept.
        """
        if extent is not None:
            return False
        context.count("edge_scans")
        return True
    # KGRUN-M3: the edge-first scan is chosen BEFORE up to 2 x 500 probes are encoded, when the
    # distinct frontier is known exactly without encoding.  A STRING key column encodes an exact
    # ``str`` probe injectively (tag, length, UTF-8 bytes), so distinct strings are distinct keys
    # and equal strings share one key: the count below is the count the encoding route would
    # produce, and the branch chosen is therefore the same.  Every other probe shape (subclass,
    # numeric, mixed) keeps the encoding route, which remains the single oracle for refusals and
    # cross-representation probes; the scan branch never reads a key it did not encode.
    exact_from = _string_probe_frontier(
        node.from_table, node.from_key_position, from_values
    )
    exact_to = _string_probe_frontier(node.to_table, node.to_key_position, to_values)
    if (
        exact_from is not None
        and exact_to is not None
        and allocated_upper * 2 <= exact_from + exact_to
    ):
        if not proved_empty():
            yield from engine._rows(node.fallback, context)
        return

    from_keys = encoded_keys(from_values, node.from_table, node.from_key_position)
    to_keys = encoded_keys(to_values, node.to_table, node.to_key_position)
    if from_keys is None or to_keys is None:
        yield from engine._rows(node.fallback, context)
        return
    probe_frontier = len(from_keys) + len(to_keys)
    if allocated_upper * 2 <= probe_frontier:
        if not proved_empty():
            yield from engine._rows(node.fallback, context)
        return

    def aligned_many(
        store: object,
        keys: tuple[bytes, ...],
    ) -> tuple[tuple[tuple[object, HeapVersion], ...], ...]:
        batches = tuple(many(store, keys, context.snapshot))
        if len(batches) != len(keys):
            raise GrafxIndexError(
                f"Multi-key validation for index {getattr(store, 'name', None)!r} returned "
                f"{len(batches)} result groups for {len(keys)} keys.",
                field="index_batch",
                index=getattr(store, "name", None),
                expected=len(keys),
                observed=len(batches),
            )
        return batches

    def resolve_nodes(
        store: object,
        keyed: tuple[tuple[bytes, Value], ...],
        table: TableDef,
        position: int,
    ) -> dict[RecordId, tuple[object, HeapVersion]]:
        keys = tuple(key for key, _value in keyed)
        groups = _memoized_primary_key_groups(
            engine,
            context,
            manager,
            store,
            keys,
            table,
            position,
            many,
        )
        if len(groups) != len(keys):
            raise GrafxIndexError(
                f"Multi-key validation for index {getattr(store, 'name', None)!r} returned "
                f"{len(groups)} result groups for {len(keys)} keys.",
                field="index_batch",
                index=getattr(store, "name", None),
                expected=len(keys),
                observed=len(groups),
            )
        resolved: dict[RecordId, tuple[object, HeapVersion]] = {}
        for (_key, value), hits in zip(keyed, groups, strict=True):
            for ref, version in hits:
                if not _equal(version.values[position], value):
                    continue
                previous = resolved.setdefault(version.record_id, (ref, version))
                if previous[0] != ref:
                    raise GrafxCorruptionDetected(
                        f"Primary-key index {getattr(store, 'name', None)!r} resolved one "
                        f"snapshot-visible key to multiple rows of {table.name!r}.",
                        table=table.name,
                        table_id=table.table_id,
                        field="primary_key",
                        index=getattr(store, "name", None),
                    )
        return resolved

    from_nodes = resolve_nodes(
        from_node_index,
        from_keys,
        node.from_table,
        node.from_key_position,
    )
    if (
        to_node_index is from_node_index
        and node.to_table.table_id == node.from_table.table_id
        and node.to_key_position == node.from_key_position
        and to_keys == from_keys
    ):
        to_nodes = from_nodes
    else:
        to_nodes = resolve_nodes(
            to_node_index,
            to_keys,
            node.to_table,
            node.to_key_position,
        )

    def edge_keys(record_ids: Collection[RecordId], position: int) -> tuple[bytes, ...]:
        return tuple(
            index_key(
                (record_id, None) if position == 0 else (None, record_id),
                (position,),
            )
            for record_id in record_ids
        )

    candidates: dict[object, HeapVersion] = {}
    for store, keys in (
        (from_edge_index, edge_keys(from_nodes, 0)),
        (to_edge_index, edge_keys(to_nodes, 1)),
    ):
        for hits in aligned_many(store, keys):
            for ref, version in hits:
                existing = candidates.setdefault(ref, version)
                if existing != version:
                    raise GrafxCorruptionDetected(
                        f"Exact endpoint indexes disagree about relationship row {ref!r} of "
                        f"{node.table.name!r}.",
                        table=node.table.name,
                        table_id=node.table.table_id,
                        field="index_candidate",
                    )

    ordered = sorted(
        candidates.items(), key=lambda item: cast(RecordRef, item[0]).encode()
    )
    endpoint_rows: list[tuple[object, HeapVersion, RecordId, RecordId]] = []
    source_ids: set[RecordId] = set()
    target_ids: set[RecordId] = set()
    for ref, version in ordered:
        source_id = node.table.source_of(version.values)
        target_id = node.table.target_of(version.values)
        if source_id not in from_nodes and target_id not in to_nodes:
            raise GrafxCorruptionDetected(
                f"An endpoint index returned relationship row {ref!r} under a key the row no "
                "longer carries after exact validation.",
                table=node.table.name,
                table_id=node.table.table_id,
                field="index_key",
            )
        endpoint_rows.append((ref, version, source_id, target_id))
        source_ids.add(source_id)
        target_ids.add(target_id)

    def batch_landings(
        table: TableDef,
        cached: dict[RecordId, tuple[object, HeapVersion]],
        record_ids: Collection[RecordId],
    ) -> tuple[dict[RecordId, tuple[object, HeapVersion]], bool]:
        """Resolve the opposite landings through one identity-index certificate when present."""

        resolved = dict(cached)
        missing = tuple(
            record_id for record_id in record_ids if record_id not in resolved
        )
        if not missing:
            return resolved, True
        identity_index = _endpoint_identity_index(engine, context, table)
        if identity_index is None:
            return resolved, False
        groups = aligned_many(
            identity_index,
            tuple(record_id_key(record_id) for record_id in missing),
        )
        for record_id, hits in zip(missing, groups, strict=True):
            if len(hits) > 1:
                raise GrafxCorruptionDetected(
                    f"Identity index {identity_index.name!r} resolved record {record_id} of "
                    f"table {table.name!r} to {len(hits)} snapshot-visible versions.",
                    file=identity_index.file,
                    table=table.name,
                    table_id=table.table_id,
                    record_id=record_id,
                    field="record_id",
                    index=identity_index.name,
                    count=len(hits),
                )
            if hits:
                resolved[record_id] = hits[0]
        return resolved, True

    if node.from_table.table_id == node.to_table.table_id:
        shared = dict(from_nodes)
        for record_id, hit in to_nodes.items():
            previous = shared.setdefault(record_id, hit)
            if previous[0] != hit[0]:
                raise GrafxCorruptionDetected(
                    f"Endpoint indexes disagree about record {record_id} of self-relationship "
                    f"table {node.table.name!r}.",
                    table=node.table.name,
                    table_id=node.table.table_id,
                    record_id=record_id,
                    field="record_id",
                )
        shared, shared_definitive = batch_landings(
            node.from_table,
            shared,
            source_ids | target_ids,
        )
        from_landings = to_landings = shared
        from_definitive = to_definitive = shared_definitive
    else:
        from_landings, from_definitive = batch_landings(
            node.from_table,
            from_nodes,
            source_ids,
        )
        to_landings, to_definitive = batch_landings(
            node.to_table,
            to_nodes,
            target_ids,
        )

    def landing(
        table: TableDef,
        cached: dict[RecordId, tuple[object, HeapVersion]],
        definitive: bool,
        record_id: RecordId,
    ) -> tuple[object, HeapVersion] | None:
        found = cached.get(record_id)
        if found is not None or definitive:
            return found
        return _visible_identity_with_ref(engine, context, table, record_id)

    charge_expansions = engine._max_traversal_expansions is not None
    charge_paths = engine._max_traversal_paths is not None
    for ref, version, source_id, target_id in endpoint_rows:
        if charge_expansions:
            context.admit_traversal_expansion()
        source = landing(
            node.from_table,
            from_landings,
            from_definitive,
            source_id,
        )
        target = landing(
            node.to_table,
            to_landings,
            to_definitive,
            target_id,
        )
        if source is None or target is None:
            continue
        if charge_paths:
            context.admit_traversal_path()
        bindings = {
            node.from_variable: RowBinding(
                variable=node.from_variable,
                table=node.from_table,
                ref=source[0],
                version=source[1],
            ),
            node.to_variable: RowBinding(
                variable=node.to_variable,
                table=node.to_table,
                ref=target[0],
                version=target[1],
            ),
        }
        if node.relationship is not None:
            bindings[node.relationship] = RowBinding(
                variable=node.relationship,
                table=node.table,
                ref=ref,
                version=version,
            )
        context.count("edge_multi_key_lookups")
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
    admits = _predicate_admitter(node.predicate, context)
    for row in engine._rows(node.child, context):
        if admits(row):
            yield row


_COMPILED_MISS: object = object()
"""The per-row memo slot value of a shared subexpression not yet reached on this row."""


class _CompiledPredicate:
    """One WHERE predicate compiled to closures over the executor's own leaf functions.

    EXEC-CSE (with KGRUN-M1 inside): the recursive walk of :func:`_evaluate` decides the kind of
    every node on every row; here each node is resolved once, at compile time, to a closure that
    calls the very same leaf functions (``_truth``, ``_equal``, ``_ordered``, ``_read_variable``,
    ``_evaluate_parameter``, ``RowBinding.value``, the coalesce body) in the same order, with
    the same short-circuit and the same refusals.  Every node kind without a closure of its own
    (``IN``, text operators, arithmetic, CASE, lists, maps, subscripts, other functions) is a
    closure that calls :func:`_evaluate` on that subtree, so the canonical walk remains the single
    oracle there.  ``coalesce`` resolves its name at compile time and shares the executor's
    selection body.  Structurally identical subtrees (the seven ``coalesce(n.revocation_reason,
    '')`` of the Pulse page) share one per-row memo slot filled the first time the path reaches
    it -- never earlier -- which preserves short-circuit and the order of errors.  A row carrying
    ``computed`` (aggregation) never takes the compiled form: ``_evaluate`` consults that memo at
    every node and the compiled form consults it at none.
    """

    __slots__ = ("expression", "function", "slots", "calls")

    def __init__(
        self,
        expression: Expression,
        function: Callable[[_Row, _Context, list[object] | None], object],
        slots: int,
        calls: tuple[tuple[int, ValueType | None], ...],
    ) -> None:
        self.expression = expression
        self.function = function
        self.slots = slots
        self.calls = calls

    def serves(self, expression: Expression, context: _Context) -> bool:
        """Say whether this compilation is for this exact expression under this context."""
        if self.expression is not expression:
            return False
        types = context.coalesce_types
        for call_id, resolved in self.calls:
            if types.get(call_id) is not resolved:
                return False
        return True


def _compile_predicate(expression: Expression, context: _Context) -> _CompiledPredicate:
    """Compile one expression tree to closures; see :class:`_CompiledPredicate`."""
    keys: dict[int, tuple] = {}
    counts: dict[tuple, int] = {}
    calls: list[tuple[int, ValueType | None]] = []
    coalesce_types = context.coalesce_types

    def key_of(node: Expression) -> tuple:
        known = keys.get(id(node))
        if known is not None:
            return known
        kind = type(node)
        if kind is Literal:
            # A closed key, built without calling into the value: exact safe types by value
            # (a float by its bits, so 0.0 and -0.0 stay apart), anything else by identity, so
            # two distinct objects never share a slot.
            value = node.value
            value_kind = type(value)
            if (
                value is None
                or value_kind is bool
                or value_kind is int
                or value_kind is str
                or value_kind is bytes
            ):
                key: tuple = ("L", value_kind.__name__, value)
            elif value_kind is float:
                key = ("L", "float", value.hex())
            else:
                key = ("L", "id", id(value))
        elif kind is Parameter:
            key = ("P", node.name)
        elif kind is Variable:
            key = ("V", node.name)
        elif kind is Property:
            key = ("R", node.key, key_of(node.subject))
        elif kind is NullCheck:
            key = ("N", bool(node.negated), key_of(node.operand))
        elif kind is UnaryOperation:
            key = ("U", node.operator, key_of(node.operand))
        elif kind is BinaryOperation:
            key = ("B", node.operator, key_of(node.left), key_of(node.right))
        elif kind is FunctionCall:
            # Two calls share a slot only when the planner resolved the same result type for
            # both; the type is part of the key and is re-checked on every reuse (serves()).
            resolved = coalesce_types.get(id(node))
            calls.append((id(node), resolved))
            key = (
                "F",
                node.name.upper(),
                bool(node.distinct),
                bool(node.star),
                tuple(key_of(argument) for argument in node.arguments),
                tuple(
                    (argument.name, key_of(argument.value))
                    for argument in node.named_arguments
                ),
                resolved,
            )
        else:
            # Anything else runs through the canonical walk as a whole and is never shared, so
            # nothing below it is keyed or even visited here.
            key = ("X", id(node))
        keys[id(node)] = key
        counts[key] = counts.get(key, 0) + 1
        return key

    key_of(expression)
    slot_of: dict[tuple, int] = {}
    fallbacks = 0
    instrumented: dict[int, bool] = {}

    def shared(
        node: Expression, function: Callable[..., object]
    ) -> Callable[..., object]:
        kind = type(node)
        if kind is Literal or kind is Parameter or kind is Variable:
            return function
        key = keys[id(node)]
        # A subtree that reaches the canonical walk anywhere is never memoized: the walk does
        # not count the mapping reads it performs, so a repeated fallback could turn two
        # observable reads into one.
        if counts.get(key, 0) < 2 or not instrumented[id(node)]:
            return function
        slot = slot_of.get(key)
        if slot is None:
            slot = slot_of[key] = len(slot_of)

        def memoized(row: _Row, ctx: _Context, memo: list[object]) -> object:
            value = memo[slot]
            if value is _COMPILED_MISS:
                reads_before = memo[-1]
                value = function(row, ctx, memo)
                # A subtree that read a property of anything but a matched row (a mapping
                # parameter, a mapping variable) is never memoized: such a read may be observable
                # or mutable, and the canonical walk performs it once per occurrence.
                if memo[-1] == reads_before:
                    memo[slot] = value
            return value

        return memoized

    def compile_node(node: Expression) -> Callable[..., object]:
        fallbacks_before = fallbacks
        function = compile_kind(node)
        # Instrumented only when neither this node nor anything compiled below it fell back.
        instrumented[id(node)] = fallbacks == fallbacks_before
        return function

    def compile_kind(node: Expression) -> Callable[..., object]:
        kind = type(node)
        if kind is Literal:
            constant = node.value

            def literal(row: _Row, ctx: _Context, memo: object) -> object:
                return constant

            return literal
        if kind is Parameter:

            def parameter(row: _Row, ctx: _Context, memo: object) -> object:
                return _evaluate_parameter(node, row, ctx, None)

            return parameter
        if kind is Variable:

            def variable(row: _Row, ctx: _Context, memo: object) -> object:
                return _read_variable(node, row, None)

            return variable
        if kind is Property:
            subject = shared(node.subject, compile_node(node.subject))
            key = node.key

            def prop(row: _Row, ctx: _Context, memo: object) -> object:
                value = subject(row, ctx, memo)
                if value is None:
                    return None
                if type(value) is RowBinding:
                    return value.value(key)
                # A mapping subject or a refusal, decided from the value already obtained (the
                # subject is never evaluated twice); the read is counted so no memo slot keeps it.
                if memo is not None:
                    memo[-1] += 1  # type: ignore[index]
                return _property_of(value, node)

            return prop
        if kind is NullCheck:
            operand = shared(node.operand, compile_node(node.operand))
            negated = bool(node.negated)

            def null_check(row: _Row, ctx: _Context, memo: object) -> object:
                value = operand(row, ctx, memo)
                return (value is not None) if negated else (value is None)

            return null_check
        if kind is UnaryOperation and node.operator == "NOT":
            operand = shared(node.operand, compile_node(node.operand))

            def negation(row: _Row, ctx: _Context, memo: object) -> object:
                truth = _truth(operand(row, ctx, memo))
                return None if truth is None else (not truth)

            return negation
        if kind is BinaryOperation:
            operator = node.operator
            if operator in ("AND", "OR", "XOR", "=", "<>", "<", "<=", ">", ">="):
                left = shared(node.left, compile_node(node.left))
                right = shared(node.right, compile_node(node.right))
                if operator == "AND":

                    def conjunction(row: _Row, ctx: _Context, memo: object) -> object:
                        left_truth = _truth(left(row, ctx, memo))
                        if left_truth is False:
                            return False
                        right_truth = _truth(right(row, ctx, memo))
                        if right_truth is False:
                            return False
                        if left_truth is None or right_truth is None:
                            return None
                        return left_truth and right_truth

                    return conjunction
                if operator == "OR":

                    def disjunction(row: _Row, ctx: _Context, memo: object) -> object:
                        left_truth = _truth(left(row, ctx, memo))
                        if left_truth is True:
                            return True
                        right_truth = _truth(right(row, ctx, memo))
                        if right_truth is True:
                            return True
                        if left_truth is None or right_truth is None:
                            return None
                        return left_truth or right_truth

                    return disjunction
                if operator == "XOR":

                    def exclusive(row: _Row, ctx: _Context, memo: object) -> object:
                        left_value = left(row, ctx, memo)
                        right_value = right(row, ctx, memo)
                        left_truth, right_truth = (
                            _truth(left_value),
                            _truth(right_value),
                        )
                        if left_truth is None or right_truth is None:
                            return None
                        return left_truth != right_truth

                    return exclusive
                if operator == "=":

                    def equality(row: _Row, ctx: _Context, memo: object) -> object:
                        left_value = left(row, ctx, memo)
                        right_value = right(row, ctx, memo)
                        if left_value is None or right_value is None:
                            return None
                        return _equal(left_value, right_value)

                    return equality
                if operator == "<>":

                    def inequality(row: _Row, ctx: _Context, memo: object) -> object:
                        left_value = left(row, ctx, memo)
                        right_value = right(row, ctx, memo)
                        if left_value is None or right_value is None:
                            return None
                        return not _equal(left_value, right_value)

                    return inequality

                def ordering(row: _Row, ctx: _Context, memo: object) -> object:
                    return _ordered(
                        operator, left(row, ctx, memo), right(row, ctx, memo)
                    )

                return ordering
        if (
            kind is FunctionCall
            and node.name.upper() == COALESCE_FUNCTION
            and not node.named_arguments
            and not node.distinct
            and not node.star
        ):
            arguments = tuple(
                shared(argument, compile_node(argument)) for argument in node.arguments
            )

            def coalesce(row: _Row, ctx: _Context, memo: object) -> object:
                values = tuple(argument(row, ctx, memo) for argument in arguments)
                return _coalesce_selected(node, values, ctx)

            return coalesce

        nonlocal fallbacks
        fallbacks += 1

        def canonical(row: _Row, ctx: _Context, memo: object) -> object:
            return _evaluate(node, row, ctx)

        return canonical

    function = shared(expression, compile_node(expression))
    return _CompiledPredicate(expression, function, len(slot_of), tuple(calls))


def _predicate_admitter(
    expression: Expression, context: _Context
) -> Callable[[_Row], bool]:
    """Return the exact three-valued WHERE door of one node execution.

    The compiled closures are resolved once per execution (the planner bound every coalesce
    type before the first row, so nothing they were compiled against changes mid-stream) and
    applied to each already-bound row; a row carrying an aggregation memo is judged by the
    canonical walk, which consults that memo at every node.
    """
    engine = context.engine
    compiled: _CompiledPredicate | None = None

    def admits(row: _Row) -> bool:
        nonlocal compiled
        if row.computed is None:
            if compiled is None:
                # Nothing is compiled, cached or even keyed before the first row that needs it:
                # a child that yields no row leaves the predicate untouched, as the walk did.
                compiled = engine._compiled_predicate(expression, context)
            slots = compiled.slots
            if slots:
                memo: list[object] | None = [_COMPILED_MISS] * slots
                memo.append(0)  # the count of non-row property reads on this row
            else:
                memo = None
            value = compiled.function(row, context, memo)
        else:
            value = _evaluate(expression, row, context)
        if value is None:
            return False
        if not isinstance(value, bool):
            raise GrafxPlanError(
                "A WHERE predicate is a condition, not a value; "
                f"{expression.describe()} produced {type(value).__name__}.",
                field="predicate",
                value=type(value).__name__,
            )
        return value

    return admits


def _predicate_admits(expression: Expression, row: _Row, context: _Context) -> bool:
    """Apply the executor's exact three-valued WHERE rule to one already-bound row."""
    return _predicate_admitter(expression, context)(row)


def _vector_search(
    engine: QueryEngine, node: VectorSearch, context: _Context
) -> Iterator[_Row]:
    """Score one candidate set, using hit-driven heap reads only when the shape proves safe.

    A filtered/traversed/correlated child remains the authority and is materialised exactly as
    before: its record identifiers are both the membership predicate and the regime estimate.
    A bounded, unfiltered ``NodeScan(SingleRow)`` has a narrower option.  When the vector index
    certifies that its live count belongs to this snapshot's exact durable frontier, the index
    already represents the complete candidate vector set, so its ``VectorHit.ref`` may drive K
    point reads instead of first decoding N heap rows.  Every uncertain case returns to the
    canonical child path; none guesses eligibility.
    """
    table = _planned_table_for_variable(node.child, node.variable)
    if table is not None and table.table_id in _intent_table_ids(engine, context.txn):
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
    direct = _direct_vector_search(engine, vectors, node, context)
    if direct is not None:
        yield from direct
        return
    filtered = _filtered_vector_search(engine, vectors, node, context)
    if filtered is not None:
        yield from filtered
        return
    yield from _materialised_vector_search(engine, vectors, node, context)


def _direct_vector_search(
    engine: QueryEngine, vectors: object, node: VectorSearch, context: _Context
) -> tuple[_Row, ...] | None:
    """Return hit-driven rows, or ``None`` when the canonical child must remain authoritative.

    The gate is deliberately closed rather than cost-model driven.  Filters, traversal,
    correlation, a structural/custom snapshot, an unbounded search, row-dependent arguments and
    an enabled intermediate-row budget all preserve the old physical path.  Requiring the exact
    engine-owned immutable ``Snapshot`` makes the cardinality proof imply its standard visibility
    rule; the latter gate keeps an existing admission contract from changing merely because this
    acceleration was installed.
    """
    scan = node.child
    if (
        type(scan) is not NodeScan
        or type(scan.child) is not SingleRow
        or scan.variable != node.variable
        or node.k is None
        or type(context.snapshot) is not Snapshot
        or engine._max_intermediate_rows is not None
    ):
        return None
    expressions = (node.space, node.query_vector, node.k)
    if node.threshold is not None:
        expressions += (node.threshold,)
    if any(free_variables(expression) for expression in expressions):
        return None
    position = scan.table.column_positions.get(node.property_key)
    if position is None:
        return None
    column = scan.table.columns[position]
    if (
        not column.is_vector
        or column.vector_space != node.column_space
        or scan.table.kind != "node"
    ):
        return None
    count_at_frontier = getattr(vectors, "snapshot_frontier_live_count", None)
    threshold = getattr(vectors, "exact_scan_threshold", None)
    if not callable(count_at_frontier) or type(threshold) is not int:
        return None

    if not node.column_space:
        return None
    # Ask through the column's already-planned space, not the runtime space expression.  An
    # empty child historically returns before evaluating query arguments or touching the vector
    # index.  A zero count or a failed optional proof therefore falls back and preserves that
    # short circuit; only a proven non-empty vector set evaluates the user's arguments early.
    try:
        candidate_count = count_at_frontier(
            node.column_space,
            context.snapshot,
            table_id=scan.table.table_id,
            position=position,
        )
    except Exception:  # noqa: BLE001 - an optional cost proof never replaces canonical errors
        # A custom vector subsystem may expose an incompatible method or any implementation may
        # be unable to prove this optional shortcut.  Running the child is the pre-optimisation
        # semantics; its ordinary vector search remains responsible for the operation's error.
        return None
    if type(candidate_count) is not int or candidate_count <= 0:
        # Zero vector entries does not prove an empty nullable heap table.  The canonical path
        # preserves its empty-candidate short circuit and its missing-index diagnostic.
        return None

    requested = _requested_neighbour_count(node, (), context)
    if column.nullable and (
        candidate_count <= threshold or requested > candidate_count
    ):
        # With a sparse column, a small vector count does not reveal whether NULL heap rows push
        # the materialised filter across the exact/approximate boundary.  Likewise k greater
        # than the vector count can widen HNSW work when NULL rows made the old heap cardinality
        # larger.  Both retain the canonical path.  Above the threshold with k <= vectors, both
        # possible heap cardinalities select the same approximate regime.
        return None

    space = _space_name(node, (), context)
    query_vector = _query_vector(node, (), context)
    wanted = max(min(requested, candidate_count), 1)
    result = vectors.search(  # type: ignore[attr-defined]
        space=space,
        query=query_vector,
        k=wanted,
        snapshot=context.snapshot,
        candidate_filter=_WholeVectorTableFilter(candidate_count),
    )
    _record_vector_search_statistics(context, result, candidate_count)
    threshold_value = _threshold_value(node, (), context)

    materialised: list[tuple[RecordRef, int, float, HeapVersion]] = []
    for hit in result.hits:
        ref = getattr(hit, "ref", None)
        record_id = getattr(hit, "record_id", None)
        score = getattr(hit, "score")
        if type(ref) is not RecordRef or type(record_id) is not int or record_id < 1:
            raise GrafxIndexError(
                "A vector hit must name one positive record id and one exact heap reference.",
                field="vector_hit_ref",
                space=space,
                value=repr(ref),
                record_id=repr(record_id),
            )
        version = engine.heap._revalidate_visible_ref(
            scan.table, ref, record_id, context.snapshot
        )
        if version is None:
            raise GrafxIndexError(
                f"Vector hit {record_id} at page {ref.page} slot {ref.slot} is not visible to "
                "the snapshot its index search certified.",
                field="vector_hit_visibility",
                space=space,
                table=scan.table.name,
                table_id=scan.table.table_id,
                record_id=record_id,
                page=ref.page,
                slot=ref.slot,
            )
        # The hit belongs to an adapter boundary and need not be an immutable VectorHit.  Keep
        # exactly the values whose physical witness was checked: re-reading ``hit.ref`` while
        # emitting the RowBinding would permit a stateful object to substitute a different row
        # after revalidation (a classic check/use split in a DELETE or SET pipeline).
        materialised.append((ref, record_id, score, version))

    context.count("vector_direct_accesses")
    context.count("vector_rows_materialized", len(materialised))
    rows: list[_Row] = []
    for ref, _record_id, score, version in materialised:
        if threshold_value is not None and not _passes(
            node.threshold_operator, score, threshold_value
        ):
            continue
        rows.append(
            _Row(
                bindings={
                    node.variable: RowBinding(
                        variable=node.variable,
                        table=scan.table,
                        ref=ref,
                        version=version,
                    ),
                    node.score_column: score,
                }
            )
        )
    return tuple(rows)


def _and_terms(expression: Expression) -> tuple[Expression, ...]:
    """Flatten only conjunctions, preserving the predicate's left-to-right term order."""
    if type(expression) is BinaryOperation and expression.operator.upper() == "AND":
        return (*_and_terms(expression.left), *_and_terms(expression.right))
    return (expression,)


def _row_filter_property(
    expression: Expression, variable: str, table: TableDef
) -> ColumnDef | None:
    """Return an exact scalar property of this one row binding, or ``None``."""
    if (
        type(expression) is not Property
        or type(expression.subject) is not Variable
        or expression.subject.name != variable
    ):
        return None
    position = table.column_positions.get(expression.key)
    if position is None:
        return None
    return table.columns[position]


def _safe_filtered_vector_scalar(
    expression: Expression, variable: str, table: TableDef
) -> bool:
    """Recognise the total scalar leaves used by Pulse eligibility predicates."""
    if type(expression) in (Literal, Parameter):
        return True
    column = _row_filter_property(expression, variable, table)
    if column is not None:
        return not column.is_vector
    if type(expression) is not FunctionCall:
        return False
    return (
        expression.name.lower() == "coalesce"
        and not expression.named_arguments
        and not expression.distinct
        and not expression.star
        and bool(expression.arguments)
        and all(
            _safe_filtered_vector_scalar(argument, variable, table)
            for argument in expression.arguments
        )
    )


def _safe_filtered_vector_condition(
    expression: Expression, variable: str, table: TableDef
) -> bool:
    """Recognise a deliberately small, total boolean grammar with no host callbacks."""
    if type(expression) is NullCheck:
        return _row_filter_property(expression.operand, variable, table) is not None
    if type(expression) is not BinaryOperation:
        return False
    operator = expression.operator.upper()
    if operator in ("AND", "OR"):
        return _safe_filtered_vector_condition(
            expression.left, variable, table
        ) and _safe_filtered_vector_condition(expression.right, variable, table)
    if operator not in ("=", "<>", "!="):
        return False
    return _safe_filtered_vector_scalar(
        expression.left, variable, table
    ) and _safe_filtered_vector_scalar(expression.right, variable, table)


def _is_safe_filtered_vector_predicate(
    expression: Expression,
    *,
    variable: str,
    table: TableDef,
    vector_property: str,
) -> bool:
    """Prove the narrow Pulse predicate and its unconditional non-null vector guard.

    Merely finding ``embedding IS NOT NULL`` anywhere would be unsound when it sits below ``OR``.
    It must be a top-level conjunct.  The remaining terms accept only equality/inequality, null
    checks and ``coalesce`` over literals, parameters and scalar properties of this exact binding.
    That excludes CASE, arithmetic, similarity, correlated variables and arbitrary functions.
    """
    terms = _and_terms(expression)
    guarded = any(
        type(term) is NullCheck
        and term.negated
        and type(term.operand) is Property
        and type(term.operand.subject) is Variable
        and term.operand.subject.name == variable
        and term.operand.key == vector_property
        for term in terms
    )
    return guarded and all(
        _safe_filtered_vector_condition(term, variable, table) for term in terms
    )


def _filtered_vector_search(
    engine: QueryEngine, vectors: object, node: VectorSearch, context: _Context
) -> tuple[_Row, ...] | None:
    """Accelerate the proven Pulse filter without first materialising its whole NodeScan.

    The vector index scans at most until ``exact_threshold + 1`` admitted rows.  Exhaustion below
    that bound produces the exact result from the already validated rows; crossing it runs the
    ordinary filter-aware HNSW with lazy identity-index point reads.  Every unavailable proof
    returns ``None`` before query arguments are evaluated, retaining the canonical child path.
    """
    if type(vectors) is not VectorEngine:
        return None
    filtered = node.child
    if type(filtered) is not FilterRows:
        return None
    scan = filtered.child
    if (
        type(scan) is not NodeScan
        or type(scan.child) is not SingleRow
        or scan.variable != node.variable
        or node.k is None
        or type(context.snapshot) is not Snapshot
        or engine._max_intermediate_rows is not None
        or scan.table.kind != "node"
    ):
        return None
    expressions = (node.space, node.query_vector, node.k)
    if node.threshold is not None:
        expressions += (node.threshold,)
    if any(free_variables(expression) for expression in expressions):
        return None
    if type(node.k) not in (Literal, Parameter):
        # A fallback after the bounded proof may need to defer evaluation to the canonical
        # child-materialising path.  Restricting k to the side-effect-free Pulse forms keeps
        # evaluation ordering identical when that happens.
        return None
    position = scan.table.column_positions.get(node.property_key)
    if position is None:
        return None
    column = scan.table.columns[position]
    if (
        not column.is_vector
        or column.vector_space != node.column_space
        or not node.column_space
        or not _is_safe_filtered_vector_predicate(
            filtered.predicate,
            variable=node.variable,
            table=scan.table,
            vector_property=node.property_key,
        )
    ):
        return None
    # The planned space must be fixed before discovery.  A dynamic expression historically runs
    # only after child materialisation and therefore cannot safely select an index here.
    if type(node.space) is not Literal or node.space.value != node.column_space:
        return None
    try:
        frontier_count = vectors.snapshot_frontier_live_count(
            node.column_space,
            context.snapshot,
            table_id=scan.table.table_id,
            position=position,
        )
    except Exception:  # noqa: BLE001 - an unavailable optional proof keeps canonical semantics
        return None
    if type(frontier_count) is not int or frontier_count <= 0:
        return None

    def row_of(ref: RecordRef, version: HeapVersion) -> _Row:
        """Bind one vector-index heap witness exactly as the omitted NodeScan would."""
        return _Row(
            bindings={
                node.variable: RowBinding(
                    variable=node.variable,
                    table=scan.table,
                    ref=ref,
                    version=version,
                )
            }
        )

    admits = _predicate_admitter(filtered.predicate, context)

    def row_admits(ref: RecordRef, version: HeapVersion) -> bool:
        """Evaluate only the structurally proved, row-local Pulse predicate."""
        return admits(row_of(ref, version))

    proof = vectors._prepare_filtered_candidates(
        space=node.column_space,
        snapshot=context.snapshot,
        table_id=scan.table.table_id,
        position=position,
        row_admits=row_admits,
    )
    if proof is None:
        return None
    candidate_count = vectors._prepared_filtered_candidate_count(proof)
    if candidate_count == 0:
        # Match ``_materialised_vector_search``: an empty filtered child does not evaluate the
        # vector, k or threshold arguments and does not touch search metrics.
        return ()

    requested = _requested_neighbour_count(node, (), context)
    if candidate_count is None and not vectors._supports_bounded_filtered_k(requested):
        # Let the canonical child discover its exact filtered size and clamp k.  Widening HNSW
        # to an unbounded caller value here would turn this optimization back into O(N) work.
        return None
    identity_index = _endpoint_identity_index(engine, context, scan.table)
    if identity_index is None:
        return None

    space = _space_name(node, (), context)
    query_vector = _query_vector(node, (), context)
    wanted = (
        requested
        if candidate_count is None
        else max(min(requested, candidate_count), 1)
    )

    def resolved_identity(
        record_id: RecordId, expected_ref: RecordRef | None = None
    ) -> tuple[RecordRef, HeapVersion]:
        """Resolve and, when supplied, authenticate one exact HNSW physical witness."""
        resolved = _visible_identity_with_ref(engine, context, scan.table, record_id)
        if resolved is None:
            raise GrafxIndexError(
                f"Identity index {identity_index.name!r} cannot resolve vector candidate "
                f"{record_id} in table {scan.table.name!r}.",
                field="vector_filter_identity",
                table=scan.table.name,
                table_id=scan.table.table_id,
                record_id=record_id,
                index=identity_index.name,
            )
        if expected_ref is not None and resolved[0] != expected_ref:
            raise GrafxCorruptionDetected(
                f"Vector entry {record_id} at page {expected_ref.page} slot "
                f"{expected_ref.slot} does not match identity index {identity_index.name!r}.",
                field="vector_filter_identity",
                table=scan.table.name,
                table_id=scan.table.table_id,
                record_id=record_id,
                index=identity_index.name,
                page=expected_ref.page,
                slot=expected_ref.slot,
                identity_page=resolved[0].page,
                identity_slot=resolved[0].slot,
            )
        return resolved

    def record_admits(record_id: RecordId) -> bool:
        """Resolve an exact-path record through the certified identity index."""
        return row_admits(*resolved_identity(record_id))

    def entry_admits(record_id: RecordId, ref: RecordRef) -> bool:
        """Authenticate the HNSW ref before its score can enter the result set."""
        return row_admits(*resolved_identity(record_id, ref))

    result = vectors._search_prepared_filtered_candidates(
        proof,
        query=query_vector,
        k=wanted,
        snapshot=context.snapshot,
        record_admits=record_admits,
        entry_admits=entry_admits,
    )
    _record_vector_search_statistics(context, result, candidate_count)
    threshold_value = _threshold_value(node, (), context)

    materialised: list[tuple[RecordRef, int, float, HeapVersion]] = []
    for hit in result.hits:
        ref = getattr(hit, "ref", None)
        record_id = getattr(hit, "record_id", None)
        score = getattr(hit, "score")
        if type(ref) is not RecordRef or type(record_id) is not int or record_id < 1:
            raise GrafxIndexError(
                "A vector hit must name one positive record id and one exact heap reference.",
                field="vector_hit_ref",
                space=space,
                value=repr(ref),
                record_id=repr(record_id),
            )
        version = engine.heap._revalidate_visible_ref(
            scan.table, ref, record_id, context.snapshot
        )
        if version is None:
            raise GrafxIndexError(
                f"Vector hit {record_id} at page {ref.page} slot {ref.slot} is not visible to "
                "the snapshot its filtered index search certified.",
                field="vector_hit_visibility",
                space=space,
                table=scan.table.name,
                table_id=scan.table.table_id,
                record_id=record_id,
                page=ref.page,
                slot=ref.slot,
            )
        identity = _visible_identity_with_ref(engine, context, scan.table, record_id)
        if identity is None or identity[0] != ref:
            raise GrafxCorruptionDetected(
                f"Vector hit {record_id} does not match the certified identity index of "
                f"table {scan.table.name!r}.",
                field="vector_filter_identity",
                space=space,
                table=scan.table.name,
                table_id=scan.table.table_id,
                record_id=record_id,
                page=ref.page,
                slot=ref.slot,
                index=identity_index.name,
            )
        if not row_admits(ref, version):
            raise GrafxIndexError(
                f"Vector hit {record_id} no longer satisfies its snapshot-stable row filter.",
                field="vector_hit_filter",
                space=space,
                table=scan.table.name,
                record_id=record_id,
            )
        materialised.append((ref, record_id, score, version))

    context.count("vector_filtered_accesses")
    context.count("vector_rows_materialized", len(materialised))
    rows: list[_Row] = []
    for ref, _record_id, score, version in materialised:
        if threshold_value is not None and not _passes(
            node.threshold_operator, score, threshold_value
        ):
            continue
        bindings = dict(row_of(ref, version).bindings)
        bindings[node.score_column] = score
        rows.append(_Row(bindings=bindings))
    return tuple(rows)


def _materialised_vector_search(
    engine: QueryEngine, vectors: object, node: VectorSearch, context: _Context
) -> Iterator[_Row]:
    """Execute a child-materialising vector search, optionally sealing its physical refs.

    The immutable ``RowBinding`` objects are the only internal source allowed to carry point-read
    witnesses. A foreign vector subsystem or any ambiguous table/column/ref shape keeps the
    public ``RecordIdFilter`` and therefore the canonical exact scan.
    """
    candidates = tuple(engine._rows(node.child, context))
    by_record: dict[int, list[_Row]] = {}
    physical_pair: tuple[int, int | None] | None = None
    physical_pair_ambiguous = False
    for row in candidates:
        binding = row.bindings.get(node.variable)
        if not isinstance(binding, RowBinding):
            raise GrafxPlanError(
                f"The similarity search reads {node.variable}, which the rows reaching it do "
                "not carry.",
                field="variable",
                value=node.variable,
            )
        current_pair = (
            binding.table.table_id,
            binding.table.column_positions.get(node.property_key),
        )
        if physical_pair is None:
            physical_pair = current_pair
        elif current_pair != physical_pair:
            physical_pair_ambiguous = True
        by_record.setdefault(binding.record_id, []).append(row)
    if not by_record:
        return
    space = _space_name(node, candidates, context)
    query_vector = _query_vector(node, candidates, context)
    wanted = _neighbour_count(node, candidates, context, len(by_record))
    candidate_filter: CandidateFilter = RecordIdFilter(frozenset(by_record))

    def materialized_witnesses() -> Iterator[tuple[int, object]]:
        """Yield refs lazily only if the vector engine's cheap pre-gate accepts this filter."""
        for row in candidates:
            binding = row.bindings.get(node.variable)
            if not isinstance(binding, RowBinding):
                return
            yield binding.record_id, binding.ref

    try:
        sealer = getattr(vectors, "_seal_materialized_candidates", None)
    except Exception:  # noqa: BLE001 - an optional private capability may only decline
        sealer = None
    if callable(sealer) and physical_pair is not None and not physical_pair_ambiguous:
        table_id, position = physical_pair
        try:
            sealed = sealer(
                materialized_witnesses(),
                space=space,
                table_id=table_id,
                position=position,
                snapshot=context.snapshot,
                candidate_count=len(by_record),
            )
        except Exception:  # noqa: BLE001 - retain the pre-existing canonical integration path
            sealed = None
        if sealed is not None:
            candidate_filter = sealed
    result = vectors.search(  # type: ignore[attr-defined]
        space=space,
        query=query_vector,
        k=wanted,
        snapshot=context.snapshot,
        candidate_filter=candidate_filter,
    )
    _record_vector_search_statistics(context, result, len(by_record))
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


def _record_vector_search_statistics(
    context: _Context, result: object, candidate_count: int | None
) -> None:
    """Record the common vector outcome for both physical candidate paths."""
    regime = getattr(result, "regime")
    hits = getattr(result, "hits")
    context.statistics["vector_regime_exact"] = context.statistics.get(
        "vector_regime_exact", 0
    ) + (1 if regime == "exact" else 0)
    context.count("vector_hits", len(hits))
    if candidate_count is not None and candidate_count > 0 and not hits:
        # Silence here is the wrong answer to give a caller: the candidate source admitted rows
        # and the search came back with nothing.  Inventing rows would be worse, so keep the
        # empty answer observable whichever physical path supplied the cardinality.
        context.count("vector_empty_over_candidates")
        context.statistics["vector_candidates_offered"] = candidate_count


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
    return max(min(_requested_neighbour_count(node, rows, context), candidates), 1)


def _requested_neighbour_count(
    node: VectorSearch, rows: Sequence[_Row], context: _Context
) -> int:
    """Evaluate and validate one fused neighbour bound without a candidate-set clamp."""
    if node.k is None:
        raise GrafxPlanError(
            "An unbounded similarity search has no requested neighbour count.",
            field="k",
            value=None,
        )
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
    return max(wanted, 1)


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


_SPILL_KEY_HEADER = b"OGQK\x01"
_SPILL_PAYLOAD_HEADER = b"OGQP\x01"
_SPILL_BIG_INTEGER = b"OGQI\x01"
_SPILL_NAN_IDENTITY = b"OGQN\x01"
_SPILL_VECTOR_IDENTITY = b"OGQW\x01"
_SPILL_VALUE_SCALAR = b"OGQV\x01"
_SPILL_VALUE_SEQUENCE = b"OGQL\x01"
_SPILL_VALUE_MAP = b"OGQM\x01"
_SPILL_VALUE_PATH = b"OGQH\x01"
_SPILL_VALUE_BINDING = b"OGQB\x01"
_SPILL_VALUE_PENDING = b"OGQR\x01"
_SPILL_VALUE_UNMATERIALIZED = b"OGQU\x01"
_AGGREGATE_GROUP_OVERHEAD = 64
_AGGREGATE_SLOT_OVERHEAD = 64
_AGGREGATE_VALUE_OVERHEAD = 16
_NAN_IDENTITY_OVERHEAD = 64
_HELD_BINDING_OVERHEAD = 128


class _NaNIdentityRegistry:
    """Keep NaN identities live and bounded for legacy grouping/distinct semantics.

    Python's existing frozen query signatures treat a repeated NaN object as the same value,
    but independently created NaNs as different values. An integer ``id`` alone cannot preserve
    that rule once streaming releases an object because the allocator may reuse its address. The
    registry therefore authenticates every lookup with ``is`` and holds a strong reference until
    the blocking aggregate closes. Each retained identity has one documented fixed logical
    charge, so this otherwise unspillable bit of interpreter identity remains bounded.
    """

    __slots__ = ("_workspace", "_entries", "_next_token", "_closed")

    def __init__(self, workspace: QuerySpillWorkspace) -> None:
        self._workspace = workspace
        self._entries: dict[int, tuple[float, int]] = {}
        self._next_token = 0
        self._closed = False

    def token(self, value: float) -> int:
        """Return one stable token per live object, never merely per reused address."""
        if self._closed:
            raise RuntimeError("A closed NaN identity registry cannot be reused.")
        identity = id(value)
        existing = self._entries.get(identity)
        if existing is not None:
            if (
                existing[0] is not value
            ):  # pragma: no cover - a strong ref forbids reuse
                raise RuntimeError("A live NaN identity was reused by the interpreter.")
            return existing[1]
        self._workspace.reserve(
            _NAN_IDENTITY_OVERHEAD,
            reason="aggregate_nan_identity",
        )
        token = self._next_token
        try:
            self._entries[identity] = (value, token)
        except BaseException:
            self._workspace.release(_NAN_IDENTITY_OVERHEAD)
            raise
        self._next_token += 1
        return token

    def close(self) -> None:
        """Release every fixed identity slot; idempotent."""
        if self._closed:
            return
        retained = len(self._entries) * _NAN_IDENTITY_OVERHEAD
        self._entries.clear()
        if retained:
            self._workspace.release(retained)
        self._closed = True


def _spill_encode(value: Value, *, key: bool) -> bytes:
    """Encode one temporary query value behind a purpose/version header."""
    return (_SPILL_KEY_HEADER if key else _SPILL_PAYLOAD_HEADER) + encode_value(value)


def _spill_decode(payload: bytes, *, key: bool) -> Value:
    """Decode one complete temporary value, refusing cross-purpose or trailing bytes."""
    if type(payload) is not bytes:
        raise GrafxCorruptionDetected(
            "A temporary query spill record is not immutable bytes.",
            field="query_spill.record",
            value=type(payload).__name__,
        )
    header = _SPILL_KEY_HEADER if key else _SPILL_PAYLOAD_HEADER
    if not payload.startswith(header):
        raise GrafxCorruptionDetected(
            "A temporary query spill record has an invalid purpose or version header.",
            field="query_spill.header",
            value="invalid",
        )
    value, following = decode_value(payload, len(header))
    if following != len(payload):
        raise GrafxCorruptionDetected(
            "A temporary query spill record carries trailing bytes.",
            field="query_spill.record",
            value="trailing_bytes",
            offset=following,
            available=len(payload),
        )
    return value


def _spill_pack_internal(
    value: object,
    *,
    nan_identities: _NaNIdentityRegistry | None = None,
) -> Value:
    """Map engine-private comparison/signature shapes onto the safe Value codec."""
    if type(value) is int and not INT64_MIN <= value <= INT64_MAX:
        return (_SPILL_BIG_INTEGER, str(value))
    if isinstance(value, float):
        if isnan(value) and nan_identities is not None:
            return (_SPILL_NAN_IDENTITY, str(nan_identities.token(value)))
        # Python equality intentionally treats both signed zeros as one frozen key. Their IEEE
        # encodings differ, so byte-keyed grouping must canonicalise the sign to stay equivalent
        # to the in-memory set/dict path.
        if value == 0.0:
            return 0.0
    if isinstance(value, VectorValue):
        # The durable Value codec rounds VECTOR_F32 components. A query parameter may still carry
        # two unequal Python floats which round to the same durable bytes, while signed zeros are
        # equal in the existing in-memory semantics. Encode an explicit identity shape with f64
        # component values so spill is both injective for unequal values and canonical for zeros.
        return (
            _SPILL_VECTOR_IDENTITY,
            value.dtype,
            value.space_ref,
            tuple(
                _spill_pack_internal(component, nan_identities=nan_identities)
                for component in value.values
            ),
        )
    if isinstance(value, tuple):
        return tuple(
            _spill_pack_internal(item, nan_identities=nan_identities) for item in value
        )
    if isinstance(value, list):
        return tuple(
            _spill_pack_internal(item, nan_identities=nan_identities) for item in value
        )
    if isinstance(value, dict):
        return {
            _spill_pack_internal(
                item, nan_identities=nan_identities
            ): _spill_pack_internal(nested, nan_identities=nan_identities)
            for item, nested in value.items()
        }
    return cast(Value, value)


def _spill_unpack_internal(value: Value) -> object:
    """Reverse :func:`_spill_pack_internal` for trusted comparison records."""
    if isinstance(value, tuple):
        if len(value) == 2 and value[0] == _SPILL_BIG_INTEGER:
            encoded = value[1]
            if not isinstance(encoded, str):
                raise GrafxCorruptionDetected(
                    "A temporary query spill integer marker is malformed.",
                    field="query_spill.key",
                    value="invalid_integer",
                )
            try:
                return int(encoded)
            except ValueError as failure:
                raise GrafxCorruptionDetected(
                    "A temporary query spill integer marker is malformed.",
                    field="query_spill.key",
                    value="invalid_integer",
                ) from failure
        if len(value) == 2 and value[0] == _SPILL_NAN_IDENTITY:
            identity = value[1]
            if (
                not isinstance(identity, str)
                or not identity.isascii()
                or not identity.isdigit()
            ):
                raise GrafxCorruptionDetected(
                    "A temporary query spill NaN marker is malformed.",
                    field="query_spill.key",
                    value="invalid_nan_identity",
                )
            return ("nan_identity", int(identity))
        return tuple(_spill_unpack_internal(item) for item in value)
    if isinstance(value, dict):
        return {
            _spill_unpack_internal(item): _spill_unpack_internal(nested)
            for item, nested in value.items()
        }
    return value


def _spill_signature(
    value: object,
    nan_identities: _NaNIdentityRegistry,
) -> bytes:
    """Return a safe exact byte identity for one existing frozen query signature."""
    return _spill_encode(
        _spill_pack_internal(_freeze(value), nan_identities=nan_identities),
        key=True,
    )


def _spill_path_value(value: _PathValue) -> Value:
    """Encode one capability-free path DTO without collapsing its nominal validation."""

    def identity(item: _PathIdentity) -> Value:
        return (item.offset, item.table)

    nodes: tuple[Value, ...] = tuple(
        (
            identity(node.identity),
            node.label,
            tuple((name, _spill_detach_value(item)) for name, item in node.properties),
        )
        for node in value.nodes
    )
    relationships: tuple[Value, ...] = tuple(
        (
            identity(relationship.source),
            identity(relationship.target),
            relationship.label,
            identity(relationship.identity),
            tuple(
                (name, _spill_detach_value(item))
                for name, item in relationship.properties
            ),
        )
        for relationship in value.relationships
    )
    return (_SPILL_VALUE_PATH, nodes, relationships)


def _spill_detach_value(value: object) -> Value:
    """Remove row/page capabilities before a value crosses into the spill adapter."""
    if type(value) is _PathValue:
        return _spill_path_value(value)
    if isinstance(value, RowBinding):
        return _spill_detach_value(_as_value(value))
    if isinstance(value, (list, tuple)):
        return (
            _SPILL_VALUE_SEQUENCE,
            tuple(_spill_detach_value(item) for item in value),
        )
    if isinstance(value, dict):
        return (
            _SPILL_VALUE_MAP,
            tuple(
                (_spill_detach_value(item), _spill_detach_value(nested))
                for item, nested in value.items()
            ),
        )
    return (_SPILL_VALUE_SCALAR, cast(Value, value))


def _spill_restore_identity(value: Value, *, field: str) -> _PathIdentity:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or type(value[0]) is not int
        or type(value[1]) is not int
    ):
        raise GrafxCorruptionDetected(
            "A temporary projected-path identity is malformed.",
            field=field,
            value="path_identity",
        )
    return _PathIdentity(offset=value[0], table=value[1])


def _spill_restore_properties(
    value: Value, *, field: str
) -> tuple[tuple[str, Value], ...]:
    if not isinstance(value, tuple):
        raise GrafxCorruptionDetected(
            "Temporary projected-path properties are malformed.",
            field=field,
            value="path_properties",
        )
    restored: list[tuple[str, Value]] = []
    for position, pair in enumerate(value):
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], str)
        ):
            raise GrafxCorruptionDetected(
                "A temporary projected-path property is malformed.",
                field=field,
                value=position,
            )
        restored.append((pair[0], _spill_restore_value(pair[1])))
    return tuple(restored)


def _spill_restore_path(value: tuple[Value, ...]) -> _PathValue:
    if (
        len(value) != 3
        or not isinstance(value[1], tuple)
        or not isinstance(value[2], tuple)
    ):
        raise GrafxCorruptionDetected(
            "A temporary projected path is malformed.",
            field="query_spill.path",
            value="path",
        )
    nodes: list[_PathNodeValue] = []
    for position, item in enumerate(value[1]):
        if (
            not isinstance(item, tuple)
            or len(item) != 3
            or not isinstance(item[1], str)
        ):
            raise GrafxCorruptionDetected(
                "A temporary projected path node is malformed.",
                field="query_spill.path",
                value=position,
            )
        nodes.append(
            _PathNodeValue(
                identity=_spill_restore_identity(
                    item[0], field="query_spill.path.node_identity"
                ),
                label=item[1],
                properties=_spill_restore_properties(
                    item[2], field="query_spill.path.node_properties"
                ),
            )
        )
    relationships: list[_PathRelationshipValue] = []
    for position, item in enumerate(value[2]):
        if (
            not isinstance(item, tuple)
            or len(item) != 5
            or not isinstance(item[2], str)
        ):
            raise GrafxCorruptionDetected(
                "A temporary projected path relationship is malformed.",
                field="query_spill.path",
                value=position,
            )
        relationships.append(
            _PathRelationshipValue(
                source=_spill_restore_identity(
                    item[0], field="query_spill.path.relationship_source"
                ),
                target=_spill_restore_identity(
                    item[1], field="query_spill.path.relationship_target"
                ),
                label=item[2],
                identity=_spill_restore_identity(
                    item[3], field="query_spill.path.relationship_identity"
                ),
                properties=_spill_restore_properties(
                    item[4], field="query_spill.path.relationship_properties"
                ),
            )
        )
    return _PathValue(nodes=tuple(nodes), relationships=tuple(relationships))  # type: ignore[arg-type]


def _spill_restore_value(value: Value) -> Value:
    """Restore exactly one tagged detached query value from a spill payload."""
    if not isinstance(value, tuple) or not value or type(value[0]) is not bytes:
        raise GrafxCorruptionDetected(
            "A temporary detached query value is malformed.",
            field="query_spill.value",
            value="untagged",
        )
    tag = value[0]
    if tag == _SPILL_VALUE_SCALAR and len(value) == 2:
        if value_type_of(value[1]) not in (ValueType.LIST, ValueType.MAP):
            return value[1]
    if tag == _SPILL_VALUE_SEQUENCE and len(value) == 2 and isinstance(value[1], tuple):
        return tuple(_spill_restore_value(item) for item in value[1])
    if tag == _SPILL_VALUE_MAP and len(value) == 2 and isinstance(value[1], tuple):
        restored: dict[Value, Value] = {}
        for position, pair in enumerate(value[1]):
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise GrafxCorruptionDetected(
                    "A temporary detached query map is malformed.",
                    field="query_spill.value",
                    value=position,
                )
            key = _spill_restore_value(pair[0])
            try:
                restored[key] = _spill_restore_value(pair[1])
            except TypeError as failure:
                raise GrafxCorruptionDetected(
                    "A temporary detached query map has an unhashable key.",
                    field="query_spill.value",
                    value=position,
                ) from failure
        return restored
    if tag == _SPILL_VALUE_PATH:
        return cast(Value, _spill_restore_path(value))
    raise GrafxCorruptionDetected(
        "A temporary detached query value has an unknown tag or shape.",
        field="query_spill.value",
        value="invalid_tag",
    )


def _spill_private_int(value: Value, *, field: str) -> int:
    """Decode one engine-private integer represented through the safe Value codec."""
    unpacked = _spill_unpack_internal(value)
    if type(unpacked) is not int:
        raise GrafxCorruptionDetected(
            "A temporary query row carries a malformed private integer.",
            field=field,
            value=type(unpacked).__name__,
        )
    return unpacked


class _SpillRowCodec:
    """Safely detach and restore a projected row without retaining one capability per row."""

    __slots__ = (
        "_context",
        "_workspace",
        "_expressions",
        "_expression_tokens",
        "_held_by_identity",
        "_held_by_token",
        "_held_bytes",
        "_closed",
    )

    def __init__(self, context: _Context, workspace: QuerySpillWorkspace) -> None:
        self._context = context
        self._workspace = workspace
        self._expressions: list[Expression] = []
        self._expression_tokens: dict[Expression, int] = {}
        self._held_by_identity: dict[int, tuple[HeapVersion, int, int]] = {}
        self._held_by_token: dict[int, HeapVersion] = {}
        self._held_bytes = 0
        self._closed = False

    def encode(self, row: _Row) -> bytes:
        """Encode bindings, computed expressions and columns behind one versioned envelope."""
        if self._closed:
            raise RuntimeError("A closed spill row codec cannot be reused.")
        bindings: tuple[Value, ...] = tuple(
            (name, self._detach(value)) for name, value in row.bindings.items()
        )
        computed: Value = (
            None
            if row.computed is None
            else tuple(
                (self._expression_token(expression), self._detach(value))
                for expression, value in row.computed.items()
            )
        )
        columns: Value = (
            None
            if row.columns is None
            else tuple(
                (name, self._detach(value)) for name, value in row.columns.items()
            )
        )
        return _spill_encode(
            ("operator-row", bindings, computed, columns),
            key=False,
        )

    def decode(self, payload: bytes) -> _Row:
        """Restore one row using only its safe bytes and plan/catalog objects already retained."""
        value = _spill_decode(payload, key=False)
        if (
            not isinstance(value, tuple)
            or len(value) != 4
            or value[0] != "operator-row"
            or not isinstance(value[1], tuple)
            or (value[2] is not None and not isinstance(value[2], tuple))
            or (value[3] is not None and not isinstance(value[3], tuple))
        ):
            raise GrafxCorruptionDetected(
                "A temporary query operator row is malformed.",
                field="query_spill.record",
                value="operator_row",
            )
        bindings = self._named_values(value[1], field="query_spill.row.bindings")
        computed: dict[Expression, object] | None = None
        if isinstance(value[2], tuple):
            computed = {}
            for position, pair in enumerate(value[2]):
                if (
                    not isinstance(pair, tuple)
                    or len(pair) != 2
                    or type(pair[0]) is not int
                    or not 0 <= pair[0] < len(self._expressions)
                ):
                    raise GrafxCorruptionDetected(
                        "A temporary query row carries a malformed computed expression.",
                        field="query_spill.row.computed",
                        value=position,
                    )
                expression = self._expressions[pair[0]]
                if expression in computed:
                    raise GrafxCorruptionDetected(
                        "A temporary query row repeats a computed expression.",
                        field="query_spill.row.computed",
                        value=position,
                    )
                computed[expression] = self._restore(pair[1])
        columns = (
            None
            if value[3] is None
            else self._named_values(value[3], field="query_spill.row.columns")
        )
        return _Row(bindings=bindings, computed=computed, columns=columns)

    def close(self) -> None:
        """Release the bounded identities that cannot be reconstructed from value bytes."""
        if self._closed:
            return
        self._held_by_identity.clear()
        self._held_by_token.clear()
        retained = self._held_bytes
        self._held_bytes = 0
        if retained:
            self._workspace.release(retained)
        self._closed = True

    def _expression_token(self, expression: Expression) -> int:
        token = self._expression_tokens.get(expression)
        if token is not None:
            return token
        token = len(self._expressions)
        self._expressions.append(expression)
        self._expression_tokens[expression] = token
        return token

    def _held_token(self, version: HeapVersion) -> int:
        identity = id(version)
        existing = self._held_by_identity.get(identity)
        if existing is not None:
            if (
                existing[0] is not version
            ):  # pragma: no cover - a strong ref forbids reuse
                raise RuntimeError(
                    "A live held-row identity was reused by the interpreter."
                )
            return existing[1]
        token = len(self._held_by_token)
        detached_values = self._detach(version.values)
        charge = _HELD_BINDING_OVERHEAD + len(_spill_encode(detached_values, key=False))
        self._workspace.reserve(charge, reason="distinct_held_binding_identity")
        try:
            self._held_by_identity[identity] = (version, token, charge)
            self._held_by_token[token] = version
        except BaseException:
            self._held_by_identity.pop(identity, None)
            self._held_by_token.pop(token, None)
            self._workspace.release(charge)
            raise
        self._held_bytes += charge
        return token

    def _detach(self, value: object) -> Value:
        if _is_unmaterialized_column(value):
            # A projected binding can cross a blocking operator's temporary spill. Preserve its
            # closed-plan proof as an engine-private tag; never coerce the sentinel into a public
            # stored value and never materialise a column the scan deliberately omitted.
            return (_SPILL_VALUE_UNMATERIALIZED,)
        if isinstance(value, RowBinding):
            reference: Value
            if isinstance(value.ref, PendingRowRef):
                reference = (
                    "pending",
                    _spill_pack_internal(value.ref.txn_id),
                    _spill_pack_internal(value.ref.table_id),
                    _spill_pack_internal(value.ref.token),
                )
            elif value.ref is None and value.record_id == 0:
                reference = ("held", self._held_token(value.version))
            else:
                reference = ("stored",)
            return (
                _SPILL_VALUE_BINDING,
                value.variable,
                value.table.table_id,
                reference,
                _spill_pack_internal(value.record_id),
                self._detach(value.version.values),
                value.polymorphic,
            )
        if isinstance(value, PendingRowRef):
            return (
                _SPILL_VALUE_PENDING,
                _spill_pack_internal(value.txn_id),
                _spill_pack_internal(value.table_id),
                _spill_pack_internal(value.token),
            )
        if type(value) is _PathValue:
            return _spill_path_value(value)
        if isinstance(value, (list, tuple)):
            return (
                _SPILL_VALUE_SEQUENCE,
                tuple(self._detach(item) for item in value),
            )
        if isinstance(value, dict):
            return (
                _SPILL_VALUE_MAP,
                tuple(
                    (self._detach(item), self._detach(nested))
                    for item, nested in value.items()
                ),
            )
        return (_SPILL_VALUE_SCALAR, cast(Value, value))

    def _restore(self, value: Value) -> object:
        if not isinstance(value, tuple) or not value or type(value[0]) is not bytes:
            raise GrafxCorruptionDetected(
                "A temporary detached operator value is malformed.",
                field="query_spill.value",
                value="untagged_operator_value",
            )
        tag = value[0]
        if tag == _SPILL_VALUE_BINDING:
            return self._restore_binding(value)
        if tag == _SPILL_VALUE_PENDING:
            return self._restore_pending(value)
        if tag == _SPILL_VALUE_UNMATERIALIZED and len(value) == 1:
            return _UNMATERIALIZED_COLUMN
        if tag == _SPILL_VALUE_SCALAR and len(value) == 2:
            kind = value_type_of(value[1])
            if kind not in (ValueType.LIST, ValueType.MAP):
                return value[1]
        if (
            tag == _SPILL_VALUE_SEQUENCE
            and len(value) == 2
            and isinstance(value[1], tuple)
        ):
            return tuple(self._restore(item) for item in value[1])
        if tag == _SPILL_VALUE_MAP and len(value) == 2 and isinstance(value[1], tuple):
            restored: dict[object, object] = {}
            for position, pair in enumerate(value[1]):
                if not isinstance(pair, tuple) or len(pair) != 2:
                    raise GrafxCorruptionDetected(
                        "A temporary detached operator map is malformed.",
                        field="query_spill.value",
                        value=position,
                    )
                key = self._restore(pair[0])
                try:
                    restored[key] = self._restore(pair[1])
                except TypeError as failure:
                    raise GrafxCorruptionDetected(
                        "A temporary detached operator map has an unhashable key.",
                        field="query_spill.value",
                        value=position,
                    ) from failure
            return restored
        if tag == _SPILL_VALUE_PATH:
            return _spill_restore_path(value)
        raise GrafxCorruptionDetected(
            "A temporary detached operator value has an unknown tag or shape.",
            field="query_spill.value",
            value="invalid_operator_tag",
        )

    def _restore_pending(self, value: tuple[Value, ...]) -> PendingRowRef:
        if len(value) != 4:
            raise GrafxCorruptionDetected(
                "A temporary pending-row identity is malformed.",
                field="query_spill.row.binding",
                value="pending_shape",
            )
        txn_id = _spill_private_int(value[1], field="query_spill.row.binding")
        table_id = _spill_private_int(value[2], field="query_spill.row.binding")
        token = _spill_private_int(value[3], field="query_spill.row.binding")
        if txn_id < 1 or table_id < 1 or token >= 0:
            raise GrafxCorruptionDetected(
                "A temporary pending-row identity is outside its private domain.",
                field="query_spill.row.binding",
                value="pending_domain",
            )
        return PendingRowRef(txn_id=txn_id, table_id=table_id, token=token)

    def _restore_binding(self, value: tuple[Value, ...]) -> RowBinding:
        if (
            len(value) != 7
            or not isinstance(value[1], str)
            or type(value[2]) is not int
            or not isinstance(value[3], tuple)
            or type(value[6]) is not bool
        ):
            raise GrafxCorruptionDetected(
                "A temporary row binding is malformed.",
                field="query_spill.row.binding",
                value="binding_shape",
            )
        table = self._context.schema().table_by_id(value[2])
        record_id = _spill_private_int(value[4], field="query_spill.row.binding")
        restored_values = self._restore(value[5])
        if not isinstance(restored_values, tuple) or len(restored_values) > table.arity:
            raise GrafxCorruptionDetected(
                "A temporary row binding carries malformed values.",
                field="query_spill.row.binding",
                value="binding_values",
            )
        reference = value[3]
        version: HeapVersion
        ref: object
        if len(reference) == 2 and reference[0] == "held" and type(reference[1]) is int:
            try:
                version = self._held_by_token[reference[1]]
            except KeyError as failure:
                raise GrafxCorruptionDetected(
                    "A temporary row binding names an unknown held identity.",
                    field="query_spill.row.binding",
                    value="unknown_held_identity",
                ) from failure
            ref = None
        else:
            version = HeapVersion(
                record_id=record_id,
                xmin=0,
                xmax=0,
                values=cast(tuple[Value, ...], restored_values),
                prev=None,
                schema_version=table.schema_version,
                deleted=False,
                table_id=table.table_id,
            )
            if len(reference) == 1 and reference[0] == "stored":
                if record_id < 1:
                    raise GrafxCorruptionDetected(
                        "A temporary stored binding has no durable record identity.",
                        field="query_spill.row.binding",
                        value=record_id,
                    )
                ref = None
            elif len(reference) == 4 and reference[0] == "pending":
                ref = self._restore_pending(
                    cast(tuple[Value, ...], (_SPILL_VALUE_PENDING, *reference[1:]))
                )
                if ref.table_id != table.table_id:
                    raise GrafxCorruptionDetected(
                        "A temporary pending binding disagrees with its table.",
                        field="query_spill.row.binding",
                        value=ref.table_id,
                    )
            else:
                raise GrafxCorruptionDetected(
                    "A temporary row binding carries an unknown reference kind.",
                    field="query_spill.row.binding",
                    value="binding_reference",
                )
        return RowBinding(
            variable=value[1],
            table=table,
            ref=ref,
            version=version,
            polymorphic=value[6],
        )

    def _named_values(
        self, value: tuple[Value, ...], *, field: str
    ) -> dict[str, object]:
        restored: dict[str, object] = {}
        for position, pair in enumerate(value):
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or pair[0] in restored
            ):
                raise GrafxCorruptionDetected(
                    "A temporary query row carries malformed named values.",
                    field=field,
                    value=position,
                )
            restored[pair[0]] = self._restore(pair[1])
        return restored


def _spill_compare(left: object, right: object) -> int:
    """Compare two already-normalised internal values without truthy shortcuts."""
    if left == right:
        return 0
    if left < right:  # type: ignore[operator]
        return -1
    if right < left:  # type: ignore[operator]
        return 1
    return 0


def _spill_pair_key(payload: bytes, *, kind: str) -> tuple[bytes, int]:
    """Decode a ``(bytes, ordinal)`` key used by grouping passes."""
    value = _spill_decode(payload, key=True)
    if (
        not isinstance(value, tuple)
        or len(value) != 3
        or value[0] != kind
        or type(value[1]) is not bytes
        or type(value[2]) is not int
        or value[2] < 0
    ):
        raise GrafxCorruptionDetected(
            "A temporary query spill ordering key is malformed.",
            field="query_spill.key",
            value=kind,
        )
    return value[1], value[2]


def _compare_group_spill_keys(left: bytes, right: bytes) -> int:
    """Order grouping input by signature, retaining source order inside a group."""
    left_signature, left_ordinal = _spill_pair_key(left, kind="group")
    right_signature, right_ordinal = _spill_pair_key(right, kind="group")
    return _spill_compare(left_signature, right_signature) or _spill_compare(
        left_ordinal, right_ordinal
    )


def _compare_distinct_row_spill_keys(left: bytes, right: bytes) -> int:
    """Order projected rows by frozen identity and then original encounter order."""
    left_signature, left_ordinal = _spill_pair_key(left, kind="distinct-row")
    right_signature, right_ordinal = _spill_pair_key(right, kind="distinct-row")
    return _spill_compare(left_signature, right_signature) or _spill_compare(
        left_ordinal, right_ordinal
    )


def _compare_ordinal_spill_keys(left: bytes, right: bytes) -> int:
    """Restore first-encounter order for materialised aggregate groups."""
    left_value = _spill_decode(left, key=True)
    right_value = _spill_decode(right, key=True)
    if type(left_value) is not int or type(right_value) is not int:
        raise GrafxCorruptionDetected(
            "A temporary query spill ordinal is malformed.",
            field="query_spill.key",
            value="ordinal",
        )
    return _spill_compare(left_value, right_value)


def _distinct_spill_key(payload: bytes) -> tuple[int, bytes, int]:
    value = _spill_decode(payload, key=True)
    if (
        not isinstance(value, tuple)
        or len(value) != 4
        or value[0] != "distinct"
        or type(value[1]) is not int
        or value[1] < 0
        or type(value[2]) is not bytes
        or type(value[3]) is not int
        or value[3] < 0
    ):
        raise GrafxCorruptionDetected(
            "A temporary distinct-aggregate key is malformed.",
            field="query_spill.key",
            value="distinct",
        )
    return value[1], value[2], value[3]


def _compare_distinct_spill_keys(left: bytes, right: bytes) -> int:
    left_index, left_signature, left_ordinal = _distinct_spill_key(left)
    right_index, right_signature, right_ordinal = _distinct_spill_key(right)
    return (
        _spill_compare(left_index, right_index)
        or _spill_compare(left_signature, right_signature)
        or _spill_compare(left_ordinal, right_ordinal)
    )


def _accepted_spill_key(payload: bytes) -> tuple[int, int]:
    value = _spill_decode(payload, key=True)
    if (
        not isinstance(value, tuple)
        or len(value) != 3
        or value[0] != "accepted"
        or type(value[1]) is not int
        or value[1] < 0
        or type(value[2]) is not int
        or value[2] < 0
    ):
        raise GrafxCorruptionDetected(
            "A temporary accepted-aggregate key is malformed.",
            field="query_spill.key",
            value="accepted",
        )
    return value[1], value[2]


def _compare_accepted_spill_keys(left: bytes, right: bytes) -> int:
    left_index, left_ordinal = _accepted_spill_key(left)
    right_index, right_ordinal = _accepted_spill_key(right)
    return _spill_compare(left_index, right_index) or _spill_compare(
        left_ordinal, right_ordinal
    )


def _sort_spill_key(payload: bytes) -> tuple[tuple[tuple[int, object, bool], ...], int]:
    value = _spill_decode(payload, key=True)
    if (
        not isinstance(value, tuple)
        or len(value) != 3
        or value[0] != "sort"
        or not isinstance(value[1], tuple)
        or type(value[2]) is not int
        or value[2] < 0
    ):
        raise GrafxCorruptionDetected(
            "A temporary sorted-row key is malformed.",
            field="query_spill.key",
            value="sort",
        )
    components: list[tuple[int, object, bool]] = []
    for component in value[1]:
        if (
            not isinstance(component, tuple)
            or len(component) != 3
            or type(component[0]) is not int
            or type(component[2]) is not bool
        ):
            raise GrafxCorruptionDetected(
                "A temporary sorted-row component is malformed.",
                field="query_spill.key",
                value="sort_component",
            )
        components.append(
            (component[0], _spill_unpack_internal(component[1]), component[2])
        )
    return tuple(components), value[2]


def _compare_sort_spill_keys(left: bytes, right: bytes) -> int:
    left_components, left_ordinal = _sort_spill_key(left)
    right_components, right_ordinal = _sort_spill_key(right)
    if len(left_components) != len(right_components):
        raise GrafxCorruptionDetected(
            "Temporary sorted rows disagree on their key arity.",
            field="query_spill.key",
            value="sort_arity",
        )
    for (left_rank, left_value, descending), (
        right_rank,
        right_value,
        right_descending,
    ) in zip(left_components, right_components):
        if descending != right_descending:
            raise GrafxCorruptionDetected(
                "Temporary sorted rows disagree on a key direction.",
                field="query_spill.key",
                value="sort_direction",
            )
        compared = _spill_compare(left_rank, right_rank)
        if not compared:
            compared = _spill_compare(left_value, right_value)
        if compared:
            return -compared if descending else compared
    return _spill_compare(left_ordinal, right_ordinal)


def _aggregate_rows(
    engine: QueryEngine, node: AggregateRows, context: _Context
) -> Iterator[_Row]:
    """Produce one row per group, carrying the grouping keys and the aggregates."""
    if getattr(engine, "_query_memory_budget_bytes", None) is not None:
        yield from _spilled_aggregate_rows(engine, node, context)
        return
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
        self._values: list[object] | None = [] if self._function == "COLLECT" else None
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
        self.add_value(value)

    def add_value(
        self,
        value: object,
        *,
        sort_key: tuple[int, object] | None = None,
        apply_distinct: bool = True,
    ) -> None:
        """Fold one pre-evaluated value, optionally already externally deduplicated."""
        if value is None:
            return
        call = self._aggregation.call
        if apply_distinct and call.distinct:
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
            key = _sort_key(value) if sort_key is None else sort_key
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


def _aggregate_input_payload(
    node: AggregateRows,
    row: _Row,
    context: _Context,
    nan_identities: _NaNIdentityRegistry,
    group_codec: _SpillRowCodec | None = None,
) -> tuple[bytes, bytes, tuple[Value, ...]]:
    """Evaluate and safely encode one aggregate input row before releasing it."""
    raw_keys = tuple(_evaluate(item.expression, row, context) for item in node.grouping)
    # A grouping key can be a carried entity for a subsequent WITH/traversal.
    # Public value detachment loses RowBinding identity/capability; use the same
    # budgeted private codec as other blocking operators, never a heap reread.
    keys = tuple((group_codec._detach(value) if group_codec is not None
                  else _spill_detach_value(value)) for value in raw_keys)
    signature = _spill_encode(
        _spill_pack_internal(
            tuple(_freeze(value) for value in raw_keys),
            nan_identities=nan_identities,
        ),
        key=True,
    )
    entries: list[Value] = []
    for aggregation in node.aggregations:
        call = aggregation.call
        if call.star:
            entries.append(("star",))
            continue
        raw = _evaluate(call.arguments[0], row, context)
        if raw is None:
            entries.append(("null",))
            continue
        entries.append(
            (
                "value",
                _spill_detach_value(raw),
                _spill_signature(raw, nan_identities) if call.distinct else b"",
                _spill_pack_internal(_sort_key(raw)),
            )
        )
    payload = _spill_encode(
        ("aggregate-input", keys, tuple(entries)),
        key=False,
    )
    return signature, payload, keys


def _decode_aggregate_input(
    payload: bytes,
    *, encoded_group_keys: bool = False,
) -> tuple[tuple[Value, ...], tuple[Value, ...]]:
    value = _spill_decode(payload, key=False)
    if (
        not isinstance(value, tuple)
        or len(value) != 3
        or value[0] != "aggregate-input"
        or not isinstance(value[1], tuple)
        or not isinstance(value[2], tuple)
    ):
        raise GrafxCorruptionDetected(
            "A temporary aggregate input record is malformed.",
            field="query_spill.record",
            value="aggregate_input",
        )
    keys = value[1] if encoded_group_keys else tuple(_spill_restore_value(item) for item in value[1])
    return keys, value[2]


def _decode_aggregate_entry(
    entry: Value,
) -> tuple[str, Value | None, bytes | None, tuple[int, object] | None]:
    if not isinstance(entry, tuple) or not entry or not isinstance(entry[0], str):
        raise GrafxCorruptionDetected(
            "A temporary aggregate value record is malformed.",
            field="query_spill.record",
            value="aggregate_value",
        )
    kind = entry[0]
    if kind in ("star", "null") and len(entry) == 1:
        return kind, None, None, None
    if kind != "value" or len(entry) != 4 or type(entry[2]) is not bytes:
        raise GrafxCorruptionDetected(
            "A temporary aggregate value record is malformed.",
            field="query_spill.record",
            value="aggregate_value",
        )
    unpacked = _spill_unpack_internal(entry[3])
    if (
        not isinstance(unpacked, tuple)
        or len(unpacked) != 2
        or type(unpacked[0]) is not int
    ):
        raise GrafxCorruptionDetected(
            "A temporary aggregate comparison key is malformed.",
            field="query_spill.key",
            value="aggregate_sort_key",
        )
    return kind, _spill_restore_value(entry[1]), entry[2], (unpacked[0], unpacked[1])


def _aggregate_distinct_payload(
    index: int,
    ordinal: int,
    value: Value,
    sort_key: tuple[int, object],
) -> bytes:
    return _spill_encode(
        (
            "aggregate-distinct-value",
            index,
            ordinal,
            _spill_detach_value(value),
            _spill_pack_internal(sort_key),
        ),
        key=False,
    )


def _decode_aggregate_distinct_payload(
    payload: bytes,
) -> tuple[int, int, Value, tuple[int, object]]:
    value = _spill_decode(payload, key=False)
    if (
        not isinstance(value, tuple)
        or len(value) != 5
        or value[0] != "aggregate-distinct-value"
        or type(value[1]) is not int
        or value[1] < 0
        or type(value[2]) is not int
        or value[2] < 0
    ):
        raise GrafxCorruptionDetected(
            "A temporary distinct aggregate payload is malformed.",
            field="query_spill.record",
            value="aggregate_distinct_value",
        )
    unpacked = _spill_unpack_internal(value[4])
    if (
        not isinstance(unpacked, tuple)
        or len(unpacked) != 2
        or type(unpacked[0]) is not int
    ):
        raise GrafxCorruptionDetected(
            "A temporary distinct aggregate comparison key is malformed.",
            field="query_spill.key",
            value="aggregate_distinct_sort_key",
        )
    return (
        value[1],
        value[2],
        _spill_restore_value(value[3]),
        (unpacked[0], unpacked[1]),
    )


def _close_iterator(iterator: object, primary: BaseException | None = None) -> None:
    """Close a spill iterator, attaching cleanup evidence to an existing failure."""
    close = getattr(iterator, "close", None)
    if not callable(close):
        return
    try:
        close()
    except BaseException as failure:
        if primary is None:
            raise
        primary.add_note(
            "A temporary query spill iterator also failed to close with "
            f"{type(failure).__name__}: {failure}"
        )


class _SpilledAggregateState:
    """Bounded state for one group, with DISTINCT values delegated to external sorters."""

    def __init__(
        self,
        node: AggregateRows,
        keys: tuple[Value, ...],
        first_ordinal: int,
        workspace: QuerySpillWorkspace,
    ) -> None:
        self._node = node
        self.keys = keys
        self.first_ordinal = first_ordinal
        self._workspace = workspace
        self._accumulators = [_Accumulator(item) for item in node.aggregations]
        self._extra = [0 for _item in node.aggregations]
        self._base = (
            _AGGREGATE_GROUP_OVERHEAD
            + len(_spill_encode(_spill_detach_value(keys), key=False))
            + len(node.aggregations) * _AGGREGATE_SLOT_OVERHEAD
        )
        workspace.reserve(self._base, reason="aggregate_group_state")
        self._distinct: QuerySpillSorter | None = None
        self._closed = False

    def add(self, entries: tuple[Value, ...], ordinal: int) -> None:
        if len(entries) != len(self._node.aggregations):
            raise GrafxCorruptionDetected(
                "A temporary aggregate row disagrees with the plan arity.",
                field="query_spill.record",
                value=len(entries),
                expected=len(self._node.aggregations),
            )
        for index, (aggregation, entry) in enumerate(
            zip(self._node.aggregations, entries)
        ):
            kind, value, signature, sort_key = _decode_aggregate_entry(entry)
            if kind == "star":
                self._accumulators[index]._count += 1
                continue
            if kind == "null":
                continue
            assert value is not None and signature is not None and sort_key is not None
            if aggregation.call.distinct:
                if self._distinct is None:
                    self._distinct = self._workspace.sorter(
                        _compare_distinct_spill_keys
                    )
                self._distinct.append(
                    _spill_encode(("distinct", index, signature, ordinal), key=True),
                    _aggregate_distinct_payload(index, ordinal, value, sort_key),
                )
                continue
            self._fold(index, value, sort_key)

    def finish(self) -> bytes:
        """Finish external DISTINCT passes and encode one aggregate output row."""
        if self._distinct is not None:
            accepted = self._workspace.sorter(_compare_accepted_spill_keys)
            records = self._distinct.records()
            previous: tuple[int, bytes] | None = None
            failure: BaseException | None = None
            try:
                for key, payload in records:
                    index, signature, ordinal = _distinct_spill_key(key)
                    identity = (index, signature)
                    if identity == previous:
                        continue
                    previous = identity
                    accepted.append(
                        _spill_encode(("accepted", index, ordinal), key=True),
                        payload,
                    )
            except BaseException as caught:
                failure = caught
                raise
            finally:
                cleanup_failure: BaseException | None = failure
                for resource in (records, self._distinct):
                    try:
                        _close_iterator(resource)
                    except BaseException as close_failure:
                        if cleanup_failure is None:
                            cleanup_failure = close_failure
                        else:
                            cleanup_failure.add_note(
                                "Distinct aggregate cleanup also failed with "
                                f"{type(close_failure).__name__}: {close_failure}"
                            )
                if failure is None and cleanup_failure is not None:
                    raise cleanup_failure

            ordered = accepted.records()
            failure = None
            try:
                for _key, payload in ordered:
                    index, _ordinal, value, sort_key = (
                        _decode_aggregate_distinct_payload(payload)
                    )
                    if not 0 <= index < len(self._accumulators):
                        raise GrafxCorruptionDetected(
                            "A temporary distinct aggregate index is outside the plan.",
                            field="query_spill.record",
                            value=index,
                        )
                    self._fold(index, value, sort_key)
            except BaseException as caught:
                failure = caught
                raise
            finally:
                cleanup_failure = failure
                for resource in (ordered, accepted):
                    try:
                        _close_iterator(resource)
                    except BaseException as close_failure:
                        if cleanup_failure is None:
                            cleanup_failure = close_failure
                        else:
                            cleanup_failure.add_note(
                                "Accepted aggregate cleanup also failed with "
                                f"{type(close_failure).__name__}: {close_failure}"
                            )
                if failure is None and cleanup_failure is not None:
                    raise cleanup_failure

        results = tuple(accumulator.result() for accumulator in self._accumulators)
        payload = _spill_encode(
            (
                "aggregate-output",
                _spill_detach_value(self.keys),
                _spill_detach_value(results),
            ),
            key=False,
        )
        self.close()
        return payload

    def close(self) -> None:
        if self._closed:
            return
        for amount in self._extra:
            if amount:
                self._workspace.release(amount)
        self._workspace.release(self._base)
        self._closed = True

    def _fold(self, index: int, value: Value, sort_key: tuple[int, object]) -> None:
        accumulator = self._accumulators[index]
        name = accumulator._function
        old_charge = self._extra[index]
        new_charge = old_charge
        if name == "COLLECT":
            new_charge += _AGGREGATE_VALUE_OVERHEAD + len(
                _spill_encode(_spill_detach_value(value), key=False)
            )
        elif name in ("MIN", "MAX"):
            extreme_key = accumulator._extreme_key
            replaces = extreme_key is None or (
                sort_key < extreme_key if name == "MIN" else not sort_key < extreme_key
            )
            if replaces:
                new_charge = _AGGREGATE_VALUE_OVERHEAD + len(
                    _spill_encode(_spill_detach_value(value), key=False)
                )
        difference = new_charge - old_charge
        if difference > 0:
            self._workspace.reserve(difference, reason="aggregate_value_state")
        elif difference < 0:
            self._workspace.release(-difference)
        self._extra[index] = new_charge
        accumulator.add_value(value, sort_key=sort_key, apply_distinct=False)


def _decode_aggregate_output(payload: bytes, node: AggregateRows,
                             group_codec: _SpillRowCodec | None = None) -> _Row:
    value = _spill_decode(payload, key=False)
    if (
        not isinstance(value, tuple)
        or len(value) != 3
        or value[0] != "aggregate-output"
        or not isinstance(value[1], tuple)
        or not isinstance(value[2], tuple)
    ):
        raise GrafxCorruptionDetected(
            "A temporary aggregate output record is malformed.",
            field="query_spill.record",
            value="aggregate_output",
        )
    keys = _spill_restore_value(value[1])
    results = _spill_restore_value(value[2])
    if (
        not isinstance(keys, tuple)
        or len(keys) != len(node.grouping)
        or not isinstance(results, tuple)
        or len(results) != len(node.aggregations)
    ):
        raise GrafxCorruptionDetected(
            "A temporary aggregate result value is malformed.",
            field="query_spill.record",
            value="aggregate_results",
        )
    computed: dict[Expression, object] = {
        item.expression: (group_codec._restore(item_value) if group_codec is not None else item_value)
        for item, item_value in zip(node.grouping, keys)
    }
    computed.update(
        {
            aggregation.call: item_value
            for aggregation, item_value in zip(node.aggregations, results)
        }
    )
    return _Row(bindings={}, computed=computed)


def _spilled_aggregate_rows(
    engine: QueryEngine, node: AggregateRows, context: _Context
) -> Iterator[_Row]:
    """Group through bounded external passes, retaining only one aggregate state in core."""
    workspace, _budget = engine._spill_workspace(node.label)
    nan_identities = _NaNIdentityRegistry(workspace)
    group_codec = _SpillRowCodec(context, workspace) if node.preserve_group_bindings else None
    source = workspace.sorter(_compare_group_spill_keys)
    output = workspace.sorter(_compare_ordinal_spill_keys)
    current: _SpilledAggregateState | None = None
    source_records: Iterator[tuple[bytes, bytes]] | None = None
    output_records: Iterator[tuple[bytes, bytes]] | None = None
    failure: BaseException | None = None
    saw_rows = False
    try:
        for ordinal, row in enumerate(engine._rows(node.child, context)):
            if ordinal > INT64_MAX:
                raise GrafxQueryBudgetExceeded(
                    "Aggregate input cardinality exceeds the spill ordinal domain.",
                    field="query_memory_budget_bytes",
                    operator=node.label,
                    limit=INT64_MAX,
                    observed=ordinal,
                )
            signature, payload, _keys = _aggregate_input_payload(
                node, row, context, nan_identities, group_codec
            )
            source.append(
                _spill_encode(("group", signature, ordinal), key=True), payload
            )
            saw_rows = True

        if not saw_rows and not node.grouping:
            computed: dict[Expression, object] = {
                aggregation.call: _Accumulator(aggregation).result()
                for aggregation in node.aggregations
            }
            yield _Row(bindings={}, computed=computed)
            return

        source_records = source.records()
        current_signature: bytes | None = None
        for key, payload in source_records:
            signature, ordinal = _spill_pair_key(key, kind="group")
            keys, entries = _decode_aggregate_input(payload, encoded_group_keys=group_codec is not None)
            if current_signature != signature:
                if current is not None:
                    output.append(
                        _spill_encode(current.first_ordinal, key=True), current.finish()
                    )
                current = _SpilledAggregateState(node, keys, ordinal, workspace)
                current_signature = signature
            assert current is not None
            current.add(entries, ordinal)
        if current is not None:
            output.append(
                _spill_encode(current.first_ordinal, key=True), current.finish()
            )
            current = None

        output_records = output.records()
        for _key, payload in output_records:
            yield _decode_aggregate_output(payload, node, group_codec)
    except BaseException as caught:
        failure = caught
        raise
    finally:
        cleanup_failure: BaseException | None = failure
        if current is not None:
            try:
                current.close()
            except BaseException as close_failure:
                if cleanup_failure is None:
                    cleanup_failure = close_failure
                else:
                    cleanup_failure.add_note(
                        "Aggregate state cleanup also failed with "
                        f"{type(close_failure).__name__}: {close_failure}"
                    )
        for iterator in (source_records, output_records):
            try:
                _close_iterator(iterator)
            except BaseException as close_failure:
                if cleanup_failure is None:
                    cleanup_failure = close_failure
                else:
                    cleanup_failure.add_note(
                        "A query spill iterator also failed to close with "
                        f"{type(close_failure).__name__}: {close_failure}"
                    )
        try:
            if group_codec is not None:
                group_codec.close()
        except BaseException as close_failure:
            if cleanup_failure is None:
                cleanup_failure = close_failure
            else:
                cleanup_failure.add_note(f"Aggregate group codec cleanup also failed: {close_failure}")
        try:
            nan_identities.close()
        except BaseException as close_failure:
            if cleanup_failure is None:
                cleanup_failure = close_failure
            else:
                cleanup_failure.add_note(
                    "NaN identity cleanup also failed with "
                    f"{type(close_failure).__name__}: {close_failure}"
                )
        try:
            workspace.close()
        except BaseException as close_failure:
            if cleanup_failure is None:
                cleanup_failure = close_failure
            else:
                cleanup_failure.add_note(
                    "Query spill workspace cleanup also failed with "
                    f"{type(close_failure).__name__}: {close_failure}"
                )
        if failure is None and cleanup_failure is not None:
            raise cleanup_failure


def _project_rows(
    engine: QueryEngine, node: ProjectRows, context: _Context
) -> Iterator[_Row]:
    """Compute the projected columns of each row, keeping what it was projected from."""
    names: tuple[str, ...] | None = None
    for row in engine._rows(node.child, context):
        if names is None:
            # A projected name is a property of the plan, not of the row: ``ReturnItem.name``
            # renders ``describe()`` on every access, so it is rendered once, on the first row,
            # and a child that yields no row renders none -- exactly as before.
            names = tuple(item.name for item in node.items)
        columns = {
            name: _evaluate(item.expression, row, context)
            for name, item in zip(names, node.items)
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
    if getattr(engine, "_query_memory_budget_bytes", None) is not None:
        yield from _spilled_distinct_rows(engine, node, context)
        return
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


def _spilled_distinct_rows(
    engine: QueryEngine, node: DistinctRows, context: _Context
) -> Iterator[_Row]:
    """Deduplicate externally, then restore each first occurrence's input order."""
    workspace, _budget = engine._spill_workspace(node.label)
    nan_identities = _NaNIdentityRegistry(workspace)
    row_codec = _SpillRowCodec(context, workspace)
    source = workspace.sorter(_compare_distinct_row_spill_keys)
    output = workspace.sorter(_compare_ordinal_spill_keys)
    source_records: Iterator[tuple[bytes, bytes]] | None = None
    output_records: Iterator[tuple[bytes, bytes]] | None = None
    failure: BaseException | None = None
    try:
        for ordinal, row in enumerate(engine._rows(node.child, context)):
            if ordinal > INT64_MAX:
                raise GrafxQueryBudgetExceeded(
                    "Distinct input cardinality exceeds the spill ordinal domain.",
                    field="query_memory_budget_bytes",
                    operator=node.label,
                    limit=INT64_MAX,
                    observed=ordinal,
                )
            columns = row.columns if row.columns is not None else {}
            signature = _spill_encode(
                _spill_pack_internal(
                    tuple(
                        (name, _freeze(value))
                        for name, value in sorted(columns.items())
                    ),
                    nan_identities=nan_identities,
                ),
                key=True,
            )
            source.append(
                _spill_encode(("distinct-row", signature, ordinal), key=True),
                row_codec.encode(row),
            )

        source_records = source.records()
        previous: bytes | None = None
        for key, payload in source_records:
            signature, ordinal = _spill_pair_key(key, kind="distinct-row")
            if signature == previous:
                continue
            previous = signature
            output.append(_spill_encode(ordinal, key=True), payload)

        output_records = output.records()
        for _key, payload in output_records:
            yield row_codec.decode(payload)
    except BaseException as caught:
        failure = caught
        raise
    finally:
        cleanup_failure: BaseException | None = failure
        for iterator in (source_records, output_records):
            try:
                _close_iterator(iterator)
            except BaseException as close_failure:
                if cleanup_failure is None:
                    cleanup_failure = close_failure
                else:
                    cleanup_failure.add_note(
                        "A distinct spill iterator also failed to close with "
                        f"{type(close_failure).__name__}: {close_failure}"
                    )
        for resource, description in (
            (row_codec, "Distinct row-codec cleanup"),
            (nan_identities, "Distinct NaN-identity cleanup"),
            (workspace, "Distinct spill workspace cleanup"),
        ):
            try:
                resource.close()
            except BaseException as close_failure:
                if cleanup_failure is None:
                    cleanup_failure = close_failure
                else:
                    cleanup_failure.add_note(
                        f"{description} also failed with "
                        f"{type(close_failure).__name__}: {close_failure}"
                    )
        if failure is None and cleanup_failure is not None:
            raise cleanup_failure


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
    if getattr(engine, "_query_memory_budget_bytes", None) is not None:
        yield from _spilled_sort_rows(engine, node, context)
        return
    if node.retained_limit is not None:
        if type(node.child) is ProjectRows:
            yield from _top_projected_rows(
                engine, node, node.child, node.retained_limit, context
            )
            return
        yield from _top_rows(engine, node, node.retained_limit, context)
        return
    rows = list(engine._rows(node.child, context))
    for key in reversed(node.keys):
        rows.sort(
            key=lambda row, key=key: _sort_key(_sort_value(key, row, context)),
            reverse=key.descending,
        )
    yield from rows


def _sort_row_payload(row: _Row) -> bytes:
    """Encode one projected row after detaching every page-backed capability."""
    if row.columns is None:
        raise GrafxPlanError(
            "A spillable SortRows must consume projected columns.",
            field="query_spill.sort",
            value="unprojected_row",
        )
    columns: tuple[Value, ...] = tuple(
        (name, _spill_detach_value(value)) for name, value in row.columns.items()
    )
    return _spill_encode(("sorted-row", columns), key=False)


def _decode_sort_row(payload: bytes) -> _Row:
    """Rebuild a capability-free projected row from one complete spill payload."""
    value = _spill_decode(payload, key=False)
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or value[0] != "sorted-row"
        or not isinstance(value[1], tuple)
    ):
        raise GrafxCorruptionDetected(
            "A temporary sorted row is malformed.",
            field="query_spill.record",
            value="sorted_row",
        )
    columns: dict[str, object] = {}
    for position, pair in enumerate(value[1]):
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not pair[0]
            or pair[0] in columns
        ):
            raise GrafxCorruptionDetected(
                "A temporary sorted row carries malformed columns.",
                field="query_spill.record",
                value=position,
            )
        columns[pair[0]] = _spill_restore_value(pair[1])
    return _Row(bindings={}, columns=columns)


def _spilled_sort_rows(
    engine: QueryEngine, node: SortRows, context: _Context
) -> Iterator[_Row]:
    """Order projected rows through a bounded adapter-owned external merge."""
    workspace, _budget = engine._spill_workspace(node.label)
    sorter = workspace.sorter(_compare_sort_spill_keys)
    records: Iterator[tuple[bytes, bytes]] | None = None
    failure: BaseException | None = None
    try:
        for ordinal, row in enumerate(engine._rows(node.child, context)):
            if ordinal > INT64_MAX:
                raise GrafxQueryBudgetExceeded(
                    "Sorted input cardinality exceeds the spill ordinal domain.",
                    field="query_memory_budget_bytes",
                    operator=node.label,
                    limit=INT64_MAX,
                    observed=ordinal,
                )
            components: tuple[Value, ...] = tuple(
                (
                    rank,
                    _spill_pack_internal(comparable),
                    key.descending,
                )
                for key in node.keys
                for rank, comparable in ((_sort_key(_sort_value(key, row, context))),)
            )
            sorter.append(
                _spill_encode(("sort", components, ordinal), key=True),
                _sort_row_payload(row),
            )
        records = sorter.records()
        for _key, payload in records:
            yield _decode_sort_row(payload)
    except BaseException as caught:
        failure = caught
        raise
    finally:
        cleanup_failure: BaseException | None = failure
        try:
            _close_iterator(records)
        except BaseException as close_failure:
            if cleanup_failure is None:
                cleanup_failure = close_failure
            else:
                cleanup_failure.add_note(
                    "A query spill iterator also failed to close with "
                    f"{type(close_failure).__name__}: {close_failure}"
                )
        try:
            workspace.close()
        except BaseException as close_failure:
            if cleanup_failure is None:
                cleanup_failure = close_failure
            else:
                cleanup_failure.add_note(
                    "Query spill workspace cleanup also failed with "
                    f"{type(close_failure).__name__}: {close_failure}"
                )
        if failure is None and cleanup_failure is not None:
            raise cleanup_failure


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
    yield from _top_rows_from(
        node, engine._rows(node.child, context), retained, context
    )


def _top_rows_from(
    node: SortRows, rows: Iterator[_Row], retained: int, context: _Context
) -> Iterator[_Row]:
    """Retain the best ``retained`` rows of a stream in the order the query asked for."""
    heap: list[_TopCandidate] = []
    for position, row in enumerate(rows):
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


@dataclass(slots=True)
class _TopProjectedCandidate(_TopCandidate):
    """A retained row whose projection may still be partial (KGRUN-3).

    ``row`` carries the columns evaluated so far; ``source`` is the row they were projected
    from, and ``complete`` says whether every item was already evaluated on it.
    """

    source: _Row
    complete: bool


_MISSING_BINDING = object()


class _DeferredProjection:
    """The plan of one bounded projection: which items may wait for the retained rows.

    KGRUN-3.  Between a projection and the bounded sort above it, most rows are discarded, yet
    every one of them was projected.  An item is deferred only when its evaluation is TOTAL on
    the row -- it can neither raise nor observe anything -- so evaluating it after the retention
    instead of before is not observable: a literal; a parameter (bound and refused before the
    first row); a property of a variable bound to a matched row that declares the column (or
    to a polymorphic match, where an undeclared column is null) or to null; ``label`` of such a
    variable; a positional ``coalesce`` of those whose result type the planner resolved to
    something other than DOUBLE (so no coercion can fail).  Everything else -- an operator, a
    function, a variable the row does not bind, a map subject, an aggregation memo -- is
    evaluated for every row exactly where the canonical
    projection evaluated it, in the same order, so every refusal happens at the same row with
    the same message.  An item a sort key names as an alias is evaluated for every row as well,
    because the key reads it from the projected columns.
    """

    __slots__ = (
        "eager",
        "items",
        "names",
        "proven_tables",
        "requirements",
        "deferred_any",
    )

    def __init__(self, project: ProjectRows, node: SortRows, context: _Context) -> None:
        items = project.items
        self.items = items
        self.names: tuple[str, ...] | None = None
        aliased = _sort_key_variables(node.keys)
        requirements: dict[str, set[str]] = {}
        eager: list[int] = []
        for position, item in enumerate(items):
            if item.name in aliased or not _deferrable(
                item.expression, requirements, context
            ):
                eager.append(position)
        self.eager = tuple(eager)
        self.requirements = tuple(
            (variable, tuple(keys)) for variable, keys in requirements.items()
        )
        # Missing properties are total only for a polymorphic binding.  The same table can
        # legitimately reach an internal producer in both modes, so the proof cache must retain
        # the mode as well as the immutable table identity.
        self.proven_tables: dict[str, tuple[TableDef, bool]] = {}
        self.deferred_any = len(eager) < len(items)

    def proven(self, row: _Row) -> bool:
        """Say whether every deferred item is total on this row."""
        if row.computed is not None:
            return False
        bindings = row.bindings
        proven_tables = self.proven_tables
        for variable, keys in self.requirements:
            binding = bindings.get(variable, _MISSING_BINDING)
            if binding is None:
                continue
            if type(binding) is not RowBinding:
                return False
            table = binding.table
            cached = proven_tables.get(variable)
            if (
                cached is not None
                and cached[0] is table
                and cached[1] is binding.polymorphic
            ):
                continue
            if not binding.polymorphic:
                positions = table.column_positions
                for key in keys:
                    if key not in positions:
                        return False
            proven_tables[variable] = (table, binding.polymorphic)
        return True


def _sort_key_variables(keys: tuple[SortItem, ...]) -> frozenset[str]:
    """Return every variable name a sort key mentions anywhere in its expression."""
    names: set[str] = set()
    pending: list[Expression] = [key.expression for key in keys]
    while pending:
        expression = pending.pop()
        if type(expression) is Variable:
            names.add(expression.name)
        pending.extend(expression.children())
    return frozenset(names)


def _deferrable(
    expression: Expression, requirements: dict[str, set[str]], context: _Context
) -> bool:
    """Say whether an item is total on every row its proof admits, recording the proof."""
    kind = type(expression)
    if kind is Literal or kind is Parameter:
        return True
    if kind is Property:
        subject = expression.subject
        if type(subject) is not Variable:
            return False
        requirements.setdefault(subject.name, set()).add(expression.key)
        return True
    if kind is FunctionCall:
        if expression.named_arguments or expression.distinct or expression.star:
            return False
        name = expression.name.upper()
        if name == LABEL_FUNCTION:
            if len(expression.arguments) != 1:
                return False
            subject = expression.arguments[0]
            if type(subject) is not Variable:
                return False
            requirements.setdefault(subject.name, set())
            return True
        if name == COALESCE_FUNCTION:
            resolved = context.coalesce_types.get(id(expression))
            if resolved is None or resolved is ValueType.DOUBLE:
                return False
            return bool(expression.arguments) and all(
                type(argument) is Literal
                or type(argument) is Parameter
                or (
                    type(argument) is Property
                    and type(argument.subject) is Variable
                    and _deferrable(argument, requirements, context)
                )
                for argument in expression.arguments
            )
    return False


def _top_projected_rows(
    engine: QueryEngine,
    node: SortRows,
    project: ProjectRows,
    retained_limit: Expression,
    context: _Context,
) -> Iterator[_Row]:
    """Order a bounded, projected result while projecting the discarded rows only partially.

    The canonical shape is ``SortRows(retained) -> ProjectRows -> child``.  This door consumes
    the child's rows itself, evaluates on every row exactly the items the canonical projection
    could refuse on (and the ones a sort key reads by alias), retains the best rows with the
    same heap, and evaluates the remaining, provably total items on the retained rows only.
    Every row is still admitted to the projection's intermediate-row budget in the same
    position, the names are rendered once on the first row, and a LIMIT of zero keeps the
    canonical path, so the child, its budgets and every refusal stay observable.
    """
    limit = _window(retained_limit, context, "LIMIT")
    skipped = (
        _window(node.retained_skip, context, "SKIP")
        if node.retained_skip is not None
        else 0
    )
    retained = skipped + limit
    if retained == 0:
        yield from _top_rows_from(
            node, engine._rows(project, context), retained, context
        )
        return
    plan = _DeferredProjection(project, node, context)
    if not plan.deferred_any:
        yield from _top_rows_from(
            node, engine._rows(project, context), retained, context
        )
        return
    items = plan.items
    eager = plan.eager
    names: tuple[str, ...] | None = None
    admit = context.admit_intermediate
    heap: list[_TopCandidate] = []
    for position, source in enumerate(engine._rows(project.child, context)):
        if names is None:
            names = tuple(item.name for item in items)
        if plan.proven(source):
            complete = False
            columns = {
                names[index]: _evaluate(items[index].expression, source, context)
                for index in eager
            }
        else:
            complete = True
            columns = {
                name: _evaluate(item.expression, source, context)
                for name, item in zip(names, items)
            }
        admit(project)
        row = _Row(bindings=source.bindings, computed=source.computed, columns=columns)
        keys = tuple(
            (_sort_key(_sort_value(key, row, context)), key.descending)
            for key in node.keys
        )
        candidate = _TopProjectedCandidate(
            keys=keys,
            position=position,
            row=row,
            source=source,
            complete=complete,
        )
        if len(heap) < retained:
            _top_heap_push(heap, candidate)
        elif candidate.precedes(heap[0]):
            _top_heap_replace(heap, candidate)
    worst_first = [_top_heap_pop(heap) for _ in range(len(heap))]
    for candidate in reversed(worst_first):
        row = candidate.row
        if candidate.complete:
            yield row
            continue
        partial = row.columns
        source = candidate.source
        columns = {
            name: (
                partial[name]  # type: ignore[index]
                if index in eager
                else _evaluate(item.expression, source, context)
            )
            for index, (name, item) in enumerate(zip(names, items))  # type: ignore[arg-type]
        }
        yield _Row(bindings=source.bindings, computed=source.computed, columns=columns)


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
                _HELD_INSERT,
                held.table,
                settled,
                held.identity,
                None,
                held.token,
                None,
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
        for ref, endpoints in engine.heap.scan_relationship_endpoints(
            candidate, snapshot
        ):
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
    staged: list[tuple[TableDef, tuple[Value, ...], int | None, int, object]] = []
    fresh: set[str] = set()
    for written in node.nodes:
        if written.variable is None:
            continue
        if written.table is None:
            continue
        values, encoding_proof = _materialise_row_with_proof(
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
        staged.append(
            (
                written.table,
                values,
                None,
                context.token_for(pending),
                encoding_proof,
            )
        )
        fresh.add(written.variable)
        bindings[written.variable] = pending
    edges: list[tuple[TableDef, tuple[Value, ...], object]] = []
    for edge in node.relationships:
        table, values, encoding_proof = _materialise_edge(
            engine, edge, bindings, fresh, row, context
        )
        if merging and _matching_edge(engine, edge, (table, values), context):
            context.count("relationships_matched")
            continue
        edges.append((table, values, encoding_proof))
    _require_write_transaction(context.txn)
    for table, values, identity, token, encoding_proof in staged:
        context.hold(
            table,
            values,
            _partition_key(table, values),
            identity,
            token=token,
            encoding_proof=encoding_proof,
        )
        context.count("rows_created")
    for table, values, encoding_proof in edges:
        context.hold(
            table,
            values,
            _partition_key(table, values),
            None,
            encoding_proof=encoding_proof,
        )
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


def _endpoint_identity_index(
    engine: QueryEngine,
    context: _Context,
    table: TableDef,
) -> object | None:
    """Choose this statement's identity access path once for one complete table identity.

    Catalog v1 and a v2 table without an ACTIVE identity generation retain the canonical heap
    path.  The same is true when the selected physical store is already known stale before this
    statement adopts it.  Each verdict is cached: publication or repair during the statement
    cannot make one table alternate between two authorities.

    Once an ACTIVE, fresh generation is selected, every later failure belongs to that access
    path and must propagate.  In particular, this function never re-resolves a cached store and
    never converts a read refusal into a heap scan.  The catalog projection is table-local, so
    the decision costs O(indexes for this table), never O(all database indexes).
    """
    identity = (table.table_id, table.name)
    cache = getattr(context, "endpoint_identity_indexes", None)
    if cache is None:
        # Narrow unit collaborators predating the concrete slotted _Context may not expose the
        # cache yet.  Give mutable ones the same statement-stable behaviour without weakening
        # the production type.
        cache = {}
        try:
            setattr(context, "endpoint_identity_indexes", cache)
        except (AttributeError, TypeError):
            pass
    if identity in cache:
        return cache[identity]

    catalog = context.schema()
    if catalog.format_version == CATALOG_LEGACY_FORMAT_VERSION:
        cache[identity] = None
        return None

    projected = catalog.active_index_definitions_for(
        table.table_id,
        table_name=table.name,
    )
    candidates = tuple(
        definition
        for definition in projected
        if definition.key_derivation == RECORD_ID_KEY_DERIVATION
    )
    if not candidates:
        cache[identity] = None
        return None
    if len(candidates) != 1:
        raise GrafxCorruptionDetected(
            f"Table {table.name!r} projects {len(candidates)} ACTIVE identity indexes; exactly "
            "one generation may own a table's RecordId access path.",
            table=table.name,
            table_id=table.table_id,
            field="identity_index",
            count=len(candidates),
        )

    definition = candidates[0]
    expected_name = identity_index_name(table.table_id)
    if (
        definition.name != expected_name
        or definition.table_id != table.table_id
        or definition.table_name != table.name
        or definition.positions
        or definition.visibility is not IndexVisibility.EXACT
    ):
        raise GrafxCorruptionDetected(
            f"Catalog identity index {definition.name!r} does not describe the complete "
            f"identity access path of table {table.name!r}.",
            table=table.name,
            table_id=table.table_id,
            field="identity_index",
            index=definition.name,
        )

    manager = engine.require_indexes()
    index = _catalog_active_index(
        manager,
        definition.name,
        catalog,
        txn=getattr(context, "txn", None),
        projection=getattr(context, "index_authority", None),
    )
    if index is None or getattr(index, "definition", None) != definition:
        raise GrafxIndexError(
            f"Catalog-selected identity index {definition.name!r} has no registered store "
            "with its exact ACTIVE physical definition.",
            table=table.name,
            table_id=table.table_id,
            field="index_authority",
            index=definition.name,
            registered=index is not None,
        )
    if getattr(index, "stale", False):
        cache[identity] = None
        return None
    if not callable(getattr(manager, "validated_versions", None)):
        raise GrafxUnsupportedOperation(
            f"Identity index {definition.name!r} is ACTIVE, but this index framework cannot "
            "return its heap-validated versions.",
            table=table.name,
            table_id=table.table_id,
            field="component",
            value="validated_versions",
            index=definition.name,
        )

    cache[identity] = index
    return index


def _visible_identity_with_ref(
    engine: QueryEngine,
    context: _Context,
    table: TableDef,
    record_id: RecordId,
    *,
    landing: bool = False,
) -> tuple[RecordRef, HeapVersion] | None:
    """Resolve one identity through the statement's fixed index or canonical heap access path.

    A catalog-v2 ACTIVE identity generation is definitive: a validated miss is absence, and two
    visible versions are corruption.  No heap scan follows either result.  Tables without that
    access path retain the bounded reusable prefix locator below, including all of its existing
    capacity, lifecycle and fail-closed stored-data behaviour.

    ``landing=True`` (RELSEEK-M4) is for a caller that consumes only the header fields of the
    version it proves -- the physical endpoint witness of an edge -- and never a column: on the
    identity-index path the heap then validates vector bodies without building the vector
    objects.  The version it returns carries the decoder's sentinel in vector positions, which
    ``value_type_of`` refuses, so it can never be encoded or published as a row.  The two other
    access paths below have no such door and decode completely, which is only slower.
    """
    identity_index = _endpoint_identity_index(engine, context, table)
    if identity_index is not None:
        manager = engine.require_indexes()
        # Selection already proved this capability.  Do not catch AttributeError or any other
        # read failure here: after adoption, fallback would hide a generation change or damage.
        # The landing form is an optional capability asked for by name: a collaborator that
        # offers validated_identity_landings answers without vector objects, and one that only
        # implements the ordinary validated_versions(index, key, snapshot) contract answers
        # through it, with the same rows and the same refusals.  No signature introspection.
        validate = (
            getattr(manager, "validated_identity_landings", None) if landing else None
        )
        if validate is None:
            validate = manager.validated_versions
        found = tuple(
            validate(
                identity_index,
                record_id_key(record_id),
                context.snapshot,
            )
        )
        if not found:
            return None
        if len(found) > 1:
            raise GrafxCorruptionDetected(
                f"Identity index {identity_index.name!r} resolved record {record_id} of "
                f"table {table.name!r} to {len(found)} snapshot-visible versions.",
                file=identity_index.file,
                table=table.name,
                table_id=table.table_id,
                record_id=record_id,
                field="record_id",
                index=identity_index.name,
                count=len(found),
            )
        return found[0]

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
    """Validate a binding's physical witness against the canonical snapshot-visible identity.

    Only the header fields of the canonical version are consumed here and by the caller (its
    physical reference and its record id), so the landing form of the identity door is used:
    vector bodies are validated without building the vector objects (RELSEEK-M4).
    """
    canonical = _visible_identity_with_ref(
        engine, context, endpoint_table, identity, landing=True
    )
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
) -> tuple[TableDef, tuple[Value, ...], object]:
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
    physical: list[tuple[str, TableDef, RecordId, RecordRef, RowBinding]] = []
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
        if (
            binding.table != endpoint_table
            or binding.version.table_id != endpoint_table.table_id
        ):
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
    properties, encoding_proof = _materialise_row_with_proof(
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
    return edge.table, properties, encoding_proof


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

    indexed = _indexed_row_intents(context, table)
    if indexed is None:
        logical_intents: list[RowIntent] = []
        for intent in getattr(context.txn, "row_intents", ()):
            intent_table = getattr(intent, "table", None)
            if getattr(intent_table, "table_id", None) != table.table_id:
                continue
            logical_intents.append(intent)
    else:
        logical_intents = list(indexed)
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


def _primary_key_identity(value: Value) -> object | None:
    """Return a hashable identity with exactly the equality used by primary-key checks."""
    if isinstance(value, (bytearray, list, dict)) or (
        isinstance(value, tuple)
        and any(_primary_key_identity(item) is None for item in value)
    ):
        # Public values may retain caller-owned mutable containers. Keep those few keys on a
        # linear defensive side path so an in-place parameter mutation cannot stale the hash.
        return None
    if _numbers(value, value):
        numeric = float(value)  # _equal intentionally joins integer and double values
        if isnan(numeric):
            return ("nan", id(value))  # _equal still rejects even the same NaN object
        return ("number", numeric)
    return ("value", _freeze(value))


def _primary_key_table_identity(table: TableDef, position: int) -> tuple[object, ...]:
    """Name the complete table/key shape so speculative schemas cannot share a memo."""
    return (
        table.table_id,
        table.name,
        table.schema_version,
        table.primary_key,
        position,
    )


def _primary_key_remove_owner(
    state: _PrimaryKeyFoldState, owner: object, values: tuple[Value, ...]
) -> None:
    state.mutable_key_owners.pop(owner, None)
    if len(values) <= state.position:
        return
    identity = _primary_key_identity(values[state.position])
    if identity is None:
        return
    bucket = state.key_owners.get(identity)
    if bucket is None:
        return
    bucket.pop(owner, None)
    if not bucket:
        state.key_owners.pop(identity, None)


def _primary_key_add_owner(
    state: _PrimaryKeyFoldState, owner: object, values: tuple[Value, ...]
) -> None:
    if len(values) <= state.position:
        return
    key = values[state.position]
    identity = _primary_key_identity(key)
    if identity is None:
        state.mutable_key_owners[owner] = key
        return
    state.key_owners.setdefault(identity, {})[owner] = key


def _validate_primary_key_pending_ref(
    context: _Context, table: TableDef, intent: RowIntent
) -> None:
    reference = intent.reference
    if not isinstance(reference, PendingRowRef):
        return
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


def _primary_key_set_outcome(
    state: _PrimaryKeyFoldState,
    reference: object,
    outcome: _PrimaryKeyOutcome | None,
) -> None:
    previous = state.outcomes.get(reference)
    if previous is not None and previous.operation is not RowOperation.DELETE:
        _primary_key_remove_owner(state, reference, previous.values)
    if outcome is None:
        state.outcomes.pop(reference, None)
        return
    state.outcomes[reference] = outcome
    if outcome.operation is not RowOperation.DELETE:
        _primary_key_add_owner(state, reference, outcome.values)


def _fold_primary_key_intent(
    state: _PrimaryKeyFoldState,
    intent: RowIntent,
    intent_position: int,
    context: _Context,
    table: TableDef,
    *,
    base: _PrimaryKeyFoldState | None = None,
    changed_refs: set[object] | None = None,
) -> None:
    """Fold one suffix intent with the same last-intent-wins rules as the canonical reducer."""
    _validate_primary_key_pending_ref(context, table, intent)
    state.fold_count += 1
    reference = intent.reference
    values = tuple(intent.values)  # type: ignore[arg-type]
    if reference is None:
        owner = _PrimaryKeyLegacyOwner(intent_position)
        _primary_key_add_owner(state, owner, values)
        return

    if changed_refs is not None and reference not in changed_refs:
        changed_refs.add(reference)
        if base is not None:
            inherited = base.outcomes.get(reference)
            if inherited is not None:
                _primary_key_set_outcome(state, reference, inherited)
            if reference in base.cancelled:
                state.cancelled.add(reference)
    if reference in state.cancelled:
        return

    previous = state.outcomes.get(reference)
    if previous is None:
        if (
            isinstance(reference, PendingRowRef)
            and intent.operation is not RowOperation.INSERT
        ):
            raise GrafxTransactionStateError(
                "A pending row identity must begin with an insert before it can be updated or "
                "deleted by its owner.",
                field="pending_row_reference",
                value=repr(reference),
                txn_id=getattr(context.txn, "txn_id", None),
                table_id=table.table_id,
                operation=intent.operation.value,
            )
        _primary_key_set_outcome(
            state, reference, _PrimaryKeyOutcome(intent.operation, values)
        )
        return
    if previous.operation is RowOperation.DELETE:
        return
    if previous.operation is RowOperation.INSERT:
        if intent.operation is RowOperation.UPDATE:
            _primary_key_set_outcome(
                state, reference, _PrimaryKeyOutcome(RowOperation.INSERT, values)
            )
            return
        if intent.operation is RowOperation.DELETE:
            _primary_key_set_outcome(state, reference, None)
            state.cancelled.add(reference)
            return
        raise GrafxTransactionStateError(
            "One pending row reference cannot name two inserts in the same transaction.",
            field="reference",
            value=repr(reference),
        )
    if intent.operation is RowOperation.INSERT:
        raise GrafxTransactionStateError(
            "A stored row reference cannot become a new insert inside the same transaction.",
            field="reference",
            value=repr(reference),
        )
    _primary_key_set_outcome(
        state, reference, _PrimaryKeyOutcome(intent.operation, values)
    )


def _revisioned_txn_memo(
    engine: QueryEngine, txn: object
) -> tuple[int, _PrimaryKeyTxnMemo] | None:
    """Return revisioned owner state for one exact mutable transaction intent list."""
    txn_id = getattr(txn, "txn_id", None)
    if isinstance(txn_id, bool) or not isinstance(txn_id, int):
        return None
    raw = getattr(txn, "row_intents", None)
    memo = engine._primary_key_memos.get(txn_id)
    if memo is not None and memo.txn is txn and memo.intents is raw:
        return txn_id, memo
    if memo is not None:
        # A defensive transaction-id reuse/replacement must not strand decoded exact-key
        # results in the shared owner budget.  Ordinary calls never enter this guarded path.
        if memo.resolutions:
            with engine._endpoint_guard:
                if engine._primary_key_memos.get(txn_id) is memo:
                    engine._primary_key_memos.pop(txn_id, None)
                    _discard_primary_key_resolution_caches(engine, memo)
        elif engine._primary_key_memos.get(txn_id) is memo:
            engine._primary_key_memos.pop(txn_id, None)
    if isinstance(raw, _RevisionList):
        tracked = raw
    elif isinstance(raw, list):
        tracked = _RevisionList(raw)
        try:
            setattr(txn, "row_intents", tracked)
        except (AttributeError, TypeError):
            return None
        if getattr(txn, "row_intents", None) is not tracked:
            return None
    else:
        return None
    memo = _PrimaryKeyTxnMemo(txn, tracked)
    engine._primary_key_memos[txn_id] = memo
    return txn_id, memo


def _revisioned_row_intents(
    engine: QueryEngine, context: _Context
) -> tuple[int, _PrimaryKeyTxnMemo] | None:
    return _revisioned_txn_memo(engine, context.txn)


def _canonical_primary_key_rebuild(
    state: _PrimaryKeyFoldState,
    intents: Sequence[object],
    context: _Context,
    table: TableDef,
    rewrite_revision: int,
) -> None:
    logical = tuple(
        intent
        for intent in intents
        if isinstance(intent, RowIntent)
        and getattr(intent.table, "table_id", None) == table.table_id
    )
    for intent in logical:
        _validate_primary_key_pending_ref(context, table, intent)
    reduce_row_intents(logical)
    state.reset(rewrite_revision)
    for intent_position, raw in enumerate(intents):
        if not isinstance(raw, RowIntent):
            continue
        if getattr(raw.table, "table_id", None) != table.table_id:
            continue
        _fold_primary_key_intent(state, raw, intent_position, context, table)
    state.cursor = len(intents)


def _transaction_primary_key_state(
    engine: QueryEngine, context: _Context, table: TableDef, position: int
) -> _PrimaryKeyFoldState:
    identified = _revisioned_row_intents(engine, context)
    if identified is None:
        raw = tuple(getattr(context.txn, "row_intents", ()))
        state = _PrimaryKeyFoldState(position=position)
        _canonical_primary_key_rebuild(state, raw, context, table, 0)
        return state
    _txn_id, memo = identified
    key = _primary_key_table_identity(table, position)
    state = memo.tables.get(key)
    if state is None:
        state = _PrimaryKeyFoldState(position=position)
        memo.tables[key] = state
        _canonical_primary_key_rebuild(
            state, memo.intents, context, table, memo.intents.rewrite_revision
        )
        return state
    if state.rewrite_revision != memo.intents.rewrite_revision or state.cursor > len(
        memo.intents
    ):
        _canonical_primary_key_rebuild(
            state, memo.intents, context, table, memo.intents.rewrite_revision
        )
        return state
    changed = False
    for intent_position in range(state.cursor, len(memo.intents)):
        raw = memo.intents[intent_position]
        if not isinstance(raw, RowIntent):
            continue
        if getattr(raw.table, "table_id", None) != table.table_id:
            continue
        _fold_primary_key_intent(state, raw, intent_position, context, table)
        changed = True
    state.cursor = len(memo.intents)
    if changed:
        state.generation += 1
    return state


def _statement_primary_key_state(
    context: _Context,
    table: TableDef,
    position: int,
    base: _PrimaryKeyFoldState,
) -> _PrimaryKeyStatementMemo:
    key = _primary_key_table_identity(table, position)
    held = context.staged_rows
    if not isinstance(held, _RevisionList):
        held = _RevisionList(held)
        context.staged_rows = held  # type: ignore[assignment]
    memo = context.primary_key_memos.get(key)
    rewrite_revision = held.rewrite_revision
    if memo is None:
        memo = _PrimaryKeyStatementMemo(
            base_generation=base.generation,
            state=_PrimaryKeyFoldState(
                position=position, rewrite_revision=rewrite_revision
            ),
        )
        context.primary_key_memos[key] = memo
    if (
        memo.base_generation != base.generation
        or memo.state.rewrite_revision != rewrite_revision
        or memo.state.cursor > len(held)
    ):
        memo.base_generation = base.generation
        memo.changed_refs.clear()
        memo.state.reset(rewrite_revision)
    for held_position in range(memo.state.cursor, len(held)):
        raw = held[held_position]
        if not isinstance(raw, _HeldRow) or raw.table.table_id != table.table_id:
            continue
        operation = {
            _HELD_INSERT: RowOperation.INSERT,
            _HELD_UPDATE: RowOperation.UPDATE,
            _HELD_DELETE: RowOperation.DELETE,
        }[raw.operation]
        intent = RowIntent(
            table=raw.table,
            values=() if raw.values is None else tuple(raw.values),
            record_id=raw.identity,
            operation=operation,
            reference=raw.reference,
        )
        _fold_primary_key_intent(
            memo.state,
            intent,
            held_position,
            context,
            table,
            base=base,
            changed_refs=memo.changed_refs,
        )
    memo.state.cursor = len(held)
    return memo


def _primary_key_current_outcome(
    base: _PrimaryKeyFoldState,
    statement: _PrimaryKeyStatementMemo,
    reference: object,
) -> tuple[bool, _PrimaryKeyOutcome | None]:
    """Return the owner-visible replacement for one durable index hit, when any."""
    if reference in statement.changed_refs:
        return True, statement.state.outcomes.get(reference)
    if reference in base.outcomes:
        return True, base.outcomes.get(reference)
    return False, None


def _primary_key_state_owners(
    state: _PrimaryKeyFoldState, key: Value
) -> Iterator[tuple[object, Value]]:
    """Yield owners filed under one equality key without walking unrelated transaction rows."""
    identity = _primary_key_identity(key)
    if identity is None:
        for owner, observed in state.mutable_key_owners.items():
            if _equal(observed, key):
                yield owner, observed
        return
    for owner, observed in state.key_owners.get(identity, {}).items():
        if _equal(observed, key):
            yield owner, observed


def _primary_key_seek_candidates(
    base: _PrimaryKeyFoldState,
    statement: _PrimaryKeyStatementMemo,
    key: Value,
) -> tuple[tuple[object, _PrimaryKeyOutcome], ...] | None:
    """Return transaction-local rows under ``key``, or decline an unidentifiable legacy row.

    The base fold is incremental across statements and the statement fold is incremental across
    held rows.  Looking up their key buckets is therefore proportional to the number of matching
    transaction-local owners (one for a valid primary key), rather than to all nodes created so
    far.  A current-statement replacement shadows the corresponding base owner exactly as the
    canonical row reducer does.
    """
    selected: list[tuple[object, _PrimaryKeyOutcome]] = []

    def add(state: _PrimaryKeyFoldState, *, exclude: set[object]) -> bool:
        for owner, _observed in _primary_key_state_owners(state, key):
            if owner in exclude:
                continue
            if isinstance(owner, _PrimaryKeyLegacyOwner):
                return False
            outcome = state.outcomes.get(owner)
            if outcome is None:
                # Only legacy reference-less inserts lack an outcome.  Keep the fallback closed
                # for foreign transaction collaborators that reproduce that shape differently.
                return False
            if outcome.operation is not RowOperation.DELETE:
                selected.append((owner, outcome))
        return True

    if not add(base, exclude=statement.changed_refs):
        return None
    if not add(statement.state, exclude=set()):
        return None
    return tuple(selected)


def _primary_key_conflicts(
    state: _PrimaryKeyFoldState,
    key: Value,
    *,
    replacing: object,
    excluded: set[object] | None = None,
) -> bool:
    identity = _primary_key_identity(key)
    for owner, observed in state.mutable_key_owners.items():
        if replacing is not None and owner == replacing:
            continue
        if excluded is not None and owner in excluded:
            continue
        if _equal(observed, key):
            return True
    lookup_identity = ("value", _freeze(key)) if identity is None else identity
    for owner, observed in state.key_owners.get(lookup_identity, {}).items():
        if replacing is not None and owner == replacing:
            continue
        if excluded is not None and owner in excluded:
            continue
        if _equal(observed, key):
            return True
    return False


def _primary_key_replaces(
    base: _PrimaryKeyFoldState,
    statement: _PrimaryKeyStatementMemo,
    reference: object,
) -> bool:
    if reference in statement.changed_refs:
        outcome = statement.state.outcomes.get(reference)
    else:
        outcome = base.outcomes.get(reference)
    return outcome is not None and outcome.operation is not RowOperation.INSERT


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
    base = _transaction_primary_key_state(engine, context, table, position)
    statement = _statement_primary_key_state(context, table, position, base)
    if _primary_key_conflicts(
        statement.state, key, replacing=replacing
    ) or _primary_key_conflicts(
        base,
        key,
        replacing=replacing,
        excluded=statement.changed_refs,
    ):
        raise _duplicate_key(table, key)
    stored = _rows_carrying_key(engine, table, key, position, context)
    if stored is None:
        stored = engine.heap.scan(table, context.snapshot)
    for ref, version in stored:
        if _primary_key_replaces(base, statement, ref) or (
            replacing is not None and ref == replacing
        ):
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
    active_index = getattr(manager, "active_index", None)
    legacy_index = getattr(manager, "index", None)
    if not callable(lookup) or not (callable(active_index) or callable(legacy_index)):
        return None
    name = primary_key_index_name(table.name)
    try:
        index = _catalog_active_index(
            manager,
            name,
            context.schema(),
            txn=getattr(context, "txn", None),
            projection=getattr(context, "index_authority", None),
        )
    except GrafxError:
        return None  # no index covers this table's key
    if index is None:
        return None
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
    encoded_key = index_key(template, (position,))
    validated_versions = getattr(manager, "validated_versions", None)
    validated = getattr(manager, "validated", None)
    uses_canonical_validation = (
        callable(validated)
        and callable(validated_versions)
        and getattr(validated, "__func__", validated) is IndexManager.validated
        and getattr(validated_versions, "__func__", validated_versions)
        is IndexManager.validated_versions
    )
    if uses_canonical_validation:
        # Exact validation has already decoded each accepted heap version while the index's
        # pre/post certificate is stable.  Keep that immutable proof instead of reading the
        # identical slot a second time after the certified callback has ended.  This is one-call
        # reuse, not a cache: no result survives this uniqueness check or becomes authority for
        # another statement/process.
        return tuple(validated_versions(index, encoded_key, context.snapshot))
    return tuple(
        (ref, engine.heap.read(ref))
        for ref in (
            validated(
                index,
                encoded_key,
                context.snapshot,
            )
            if callable(validated)
            else lookup(name, encoded_key, context.snapshot)
        )
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
    row_intents = getattr(context.txn, "row_intents", ())
    has_held_delete = any(
        held.operation == _HELD_DELETE for held in context.staged_rows
    )
    if has_held_delete:
        has_delete = True
    else:
        has_delete = _has_indexed_delete_intent(context)
        if has_delete is None:
            has_delete = any(
                getattr(intent, "operation", None) is RowOperation.DELETE
                for intent in row_intents
            )
    if not has_delete:
        # DELETE is the only input that can make _transaction_row_view report an ended row.
        # Most relationship-ingestion transactions only append INSERT intents; avoid rebuilding
        # every dirty table's complete row view for each endpoint seek in that overwhelmingly
        # common case.  The seek's own table view still performs its canonical pending-reference
        # validation immediately after this precheck.
        return set()

    ended: set[object] = set()
    tables: dict[int, TableDef] = {}
    for held in context.staged_rows:
        tables[held.table.table_id] = held.table
    for intent in row_intents:
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
    be free to drift from it (amendment A24). Internal write paths may retain its bytes behind a
    sealed, exact-object proof; this public helper exposes only the values and therefore keeps
    the established return contract.
    """
    materialised, _encoding_proof = _materialise_row_with_proof(
        engine,
        table,
        properties,
        row,
        context,
        endpoints=endpoints,
    )
    return materialised


def _materialise_row_with_proof(
    engine: QueryEngine,
    table: TableDef,
    properties: MapExpression | None,
    row: _Row,
    context: _Context,
    *,
    endpoints: tuple[int, int] | None = None,
) -> tuple[tuple[Value, ...], object]:
    """Materialise one row and retain the exact bytes its validation produced.

    A relationship that still carries a pending endpoint is validated through a fixed-width
    stand-in and therefore receives no reusable proof: the commit resolves and encodes its real
    endpoint values before the heap can see them.  Every ordinary row retains both the exact
    values object and its sealed schema proof, never a bare object id.
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
    validatable = _validatable_row(table, materialised)
    if validatable is materialised:
        _payload, encoding_proof = _encode_tuple_with_proof(table, materialised)
        return materialised, encoding_proof
    encode_tuple(table, validatable)
    return materialised, None


def _validatable_row(table: TableDef, values: tuple[Value, ...]) -> tuple[Value, ...]:
    """Return the row with each unresolved endpoint standing in for the id it will become.

    Only the two endpoint columns of a relationship may hold a promise, and the substitution is
    positional for that reason. Anywhere else a promise is a value that will never become one, so
    it is refused HERE rather than widened into the encoder -- the staging door draws the same
    line, and two doors drawing it differently is how a token eventually walks through one.
    """
    endpoints = ENDPOINT_COLUMN_COUNT if table.kind == "rel" else 0
    substituted: list[Value] | None = None
    for position, value in enumerate(values):
        if not isinstance(value, PendingRowRef):
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
        if substituted is None:
            substituted = list(values)
        substituted[position] = SIZING_ENDPOINT
    return values if substituted is None else tuple(substituted)


def _column_named(table: TableDef, key: str) -> ColumnDef:
    """Return one column of a table, refusing a name it does not declare."""
    position = table.column_positions.get(key)
    if position is not None:
        return table.columns[position]
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
    NodeMultiKeySeek: _node_multi_key_seek,  # type: ignore[dict-item]
    OrderedNodeMerge: _ordered_node_merge,  # type: ignore[dict-item]
    IndexSeek: _index_seek,  # type: ignore[dict-item]
    TraverseAnyRelationship: _traverse_any,  # type: ignore[dict-item]
    TraverseRelationship: _traverse,  # type: ignore[dict-item]
    RelationshipScan: _relationship_scan,  # type: ignore[dict-item]
    RelationshipIncidentSeek: _relationship_incident_seek,  # type: ignore[dict-item]
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
    kind = type(expression)
    if kind is Literal:
        # The most frequent leaf, and the one node for which one identity test beats both the
        # table lookup and the first question of the walk; the answer is the walk's.
        return expression.value
    evaluator = _EXACT_EVALUATORS.get(kind)
    if evaluator is not None:
        return evaluator(expression, row, context, computed)
    return _evaluate_by_kind(expression, row, context, computed)


def _evaluate_by_kind(
    expression: Expression,
    row: _Row,
    context: _Context,
    computed: Mapping[Expression, object] | None,
) -> object:
    """Walk the node kinds in their fixed order; every subclass and unknown node comes here.

    The exact-type table in :func:`_evaluate` only shortcuts this walk for a node whose class
    is exactly one of the kinds below, and it dispatches to the very same function the walk
    would reach, so the two doors cannot answer differently.
    """
    if isinstance(expression, Literal):
        return _evaluate_literal(expression, row, context, computed)
    if isinstance(expression, Parameter):
        return _evaluate_parameter(expression, row, context, computed)
    if isinstance(expression, Variable):
        return _evaluate_variable(expression, row, context, computed)
    if isinstance(expression, Property):
        return _evaluate_property(expression, row, context, computed)
    if isinstance(expression, NullCheck):
        return _evaluate_null_check(expression, row, context, computed)
    if isinstance(expression, UnaryOperation):
        return _unary(expression, row, context)
    if isinstance(expression, BinaryOperation):
        return _binary(expression, row, context)
    if isinstance(expression, ListExpression):
        return _evaluate_list(expression, row, context, computed)
    if isinstance(expression, MapExpression):
        return _evaluate_map(expression, row, context, computed)
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


def _evaluate_literal(
    expression: Literal,
    row: _Row,
    context: _Context,
    computed: Mapping[Expression, object] | None,
) -> object:
    return expression.value


def _evaluate_parameter(
    expression: Parameter,
    row: _Row,
    context: _Context,
    computed: Mapping[Expression, object] | None,
) -> object:
    if expression.name not in context.parameters:
        raise GrafxPlanError(
            f"The query needs the parameter ${expression.name}, which was not supplied.",
            field="parameter",
            value=expression.name,
        )
    return context.parameters[expression.name]


def _evaluate_variable(
    expression: Variable,
    row: _Row,
    context: _Context,
    computed: Mapping[Expression, object] | None,
) -> object:
    return _read_variable(expression, row, computed)


def _evaluate_property(
    expression: Property,
    row: _Row,
    context: _Context,
    computed: Mapping[Expression, object] | None,
) -> object:
    subject = _evaluate(expression.subject, row, context)
    if subject is None:
        return None
    if type(subject) is RowBinding:
        # KGRUN-M1: the matched row is the common case; the abstract Mapping test below walks
        # the ABC registry and is only reached by a map subject or a refusal.
        return subject.value(expression.key)
    return _property_of(subject, expression)


def _property_of(subject: object, expression: Property) -> object:
    """Read one property of an already evaluated, non-null subject."""
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


def _evaluate_null_check(
    expression: NullCheck,
    row: _Row,
    context: _Context,
    computed: Mapping[Expression, object] | None,
) -> object:
    value = _evaluate(expression.operand, row, context)
    return (value is not None) if expression.negated else (value is None)


def _evaluate_list(
    expression: ListExpression,
    row: _Row,
    context: _Context,
    computed: Mapping[Expression, object] | None,
) -> object:
    return tuple(_evaluate(element, row, context) for element in expression.elements)


def _evaluate_map(
    expression: MapExpression,
    row: _Row,
    context: _Context,
    computed: Mapping[Expression, object] | None,
) -> object:
    return {
        entry.key: _evaluate(entry.value, row, context) for entry in expression.entries
    }


def _evaluate_unary(
    expression: UnaryOperation,
    row: _Row,
    context: _Context,
    computed: Mapping[Expression, object] | None,
) -> object:
    return _unary(expression, row, context)


def _evaluate_binary(
    expression: BinaryOperation,
    row: _Row,
    context: _Context,
    computed: Mapping[Expression, object] | None,
) -> object:
    return _binary(expression, row, context)


def _evaluate_case(
    expression: CaseExpression,
    row: _Row,
    context: _Context,
    computed: Mapping[Expression, object] | None,
) -> object:
    return _case(expression, row, context)


def _evaluate_subscript(
    expression: Subscript,
    row: _Row,
    context: _Context,
    computed: Mapping[Expression, object] | None,
) -> object:
    return _subscript(expression, row, context)


def _evaluate_call(
    expression: FunctionCall,
    row: _Row,
    context: _Context,
    computed: Mapping[Expression, object] | None,
) -> object:
    return _call(expression, row, context)


# One entry per EXACT node class, pointing at the function the isinstance walk reaches for that
# kind.  A subclass of any node is absent here on purpose and takes the walk, so the table can
# only shorten the dispatch, never change which function answers.  The proxy keeps the table
# read-only: the grammar of the evaluator is written once at import.
_EXACT_EVALUATORS: Mapping[type, Callable[..., object]] = MappingProxyType(
    {
        Literal: _evaluate_literal,
        Parameter: _evaluate_parameter,
        Variable: _evaluate_variable,
        Property: _evaluate_property,
        NullCheck: _evaluate_null_check,
        UnaryOperation: _evaluate_unary,
        BinaryOperation: _evaluate_binary,
        ListExpression: _evaluate_list,
        MapExpression: _evaluate_map,
        CaseExpression: _evaluate_case,
        Subscript: _evaluate_subscript,
        FunctionCall: _evaluate_call,
    }
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
        if isinstance(expression.right, Parameter):
            memo = _in_list_memo(expression.right.name, right, context)
            if memo is not None:
                return _memo_membership(left, memo)
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
    if isinstance(left, Timestamp) and isinstance(right, Timestamp):
        left = left.micros
        right = right.micros
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
    unknown = False
    for element in right:
        if element is None:
            unknown = True
            continue
        if _equal(left, element):
            # A match decides the answer whatever follows: a later null only matters when
            # nothing matched, so the walk ends here rather than comparing the rest.
            return True
    return None if unknown else False


def _in_list_memo(name: str, value: object, context: _Context) -> _InListMemo | None:
    """Return the statement's hashed picture of one IN parameter, building it at most once."""
    memos = context.in_list_memos
    if name in memos:
        memo = memos[name]
        if memo is None or memo.values is not value:
            return None
        return memo
    memo = _build_in_list_memo(value, context)
    memos[name] = memo
    return memo


def _build_in_list_memo(value: object, context: _Context) -> _InListMemo | None:
    """Hash one detached list of strings, bytes and nulls, or decline to the linear walk.

    Declining is silent and final for the statement: an exact list (not the detached tuple the
    facade hands over), any element of another kind, or a list that would carry the statement
    past its memo ceiling all keep the linear comparison, which is the same answer.
    """
    if type(value) is not tuple:
        return None
    if len(value) > _IN_LIST_MEMO_MAX_TOTAL_ELEMENTS - context.in_list_memo_elements:
        return None
    keys: set[object] = set()
    has_null = False
    for element in value:
        kind = type(element)
        if element is None:
            has_null = True
        elif kind is str or kind is bytes:
            keys.add(_freeze(element))
        else:
            return None
    context.in_list_memo_elements += len(value)
    return _InListMemo(values=value, keys=frozenset(keys), has_null=has_null)


def _memo_membership(left: object, memo: _InListMemo) -> object:
    """Answer IN over a hashed list exactly as the linear walk over the same list would.

    Only an exact ``str`` or ``bytes`` on the left consults the set: against a list of those
    kinds ``_equal`` is ``_freeze`` key equality and nothing else.  Every other left value --
    a binding, a list, a map, a number, a boolean -- takes the walk over the same detached
    list, so the memo never decides a comparison it did not build for.
    """
    if left is None:
        return None
    kind = type(left)
    if kind is not str and kind is not bytes:
        return _membership(left, memo.values)
    if _freeze(left) in memo.keys:
        return True
    return None if memo.has_null else False


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
    return _coalesce_selected(expression, values, context)


def _coalesce_selected(
    expression: FunctionCall, values: tuple[object, ...], context: _Context
) -> object:
    """Select and coerce the coalesce answer from already evaluated arguments."""
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
        return (6, 0)
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
    if isinstance(value, Timestamp):
        return (4, value.micros)
    if isinstance(value, RowBinding):
        return (5, _binding_identity(value))
    return (7, repr(value))


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
    if isinstance(left, Timestamp) and isinstance(right, Timestamp):
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
    kind = type(left)
    if kind is type(right) and (kind is str or kind is bytes):
        # One exact built-in kind on both sides: _freeze would tag both identically and compare
        # the payloads, so the payload comparison is the same answer without two tuples.
        return left == right
    if _numbers(left, right):
        return float(left) == float(right)
    if isinstance(left, RowBinding) or isinstance(right, RowBinding):
        return (
            isinstance(left, RowBinding)
            and isinstance(right, RowBinding)
            and _binding_identity(left) == _binding_identity(right)
        )
    return _freeze(left) == _freeze(right)
