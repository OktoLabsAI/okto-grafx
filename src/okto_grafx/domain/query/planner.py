"""Turning a statement into an operator tree against a catalog (CONTRACT.md section 8.9).

This is where a query stops being text and becomes a decision about how to answer it, and three
of those decisions are correctness rather than performance.

**Which index may answer a predicate.** An exact index answers EQUALITY on the whole of its key
and nothing else. Using one for a range, or for a prefix of a composite key, would return a subset
of the rows the query asked for -- so the planner takes an index seek only when the predicate
constrains every key column of that index with an equality against a value known before the query
runs. The visibility class the index declares travels into the plan, because CONTRACT.md section
8.7 gives the two classes different guarantees: an exact hit is a CANDIDATE the framework
validates against the heap under the snapshot, a proximity hit is already decided.

**Where the similarity operator sits.** Everything the query filters -- node properties,
relationship properties, traversal -- is planned BELOW the similarity operator, so the rows that
reach it are exactly the candidate set and the two-regime planner of the vector subsystem chooses
its regime from the real cardinality. That is SPEC-VEC BR-6 and AC-7 as a plan shape rather than
as a promise, and :func:`~okto_grafx.domain.query.plan.validate_plan` refuses the shape that
breaks it.

**When a LIMIT may become a k.** Only when nothing above the similarity operator can drop a row:
no filter, no DISTINCT, no aggregate, and an ORDER BY that is exactly the score descending. Fusing
in any other case would ask the vector operator for k rows and then discard some, which is the
under-delivery BR-2 forbids. When the conditions do not hold the operator scores every candidate,
which is slower and always right.

The dialect diverges from the reference engine in three places, deliberately and reported rather
than hidden: a matched node needs exactly one label, a matched relationship needs exactly one
type, and MERGE covers a single node or a single relationship between two already-bound nodes.
Each is a refusal with a message that names the rule, never a silent partial answer.
"""

from __future__ import annotations

from okto_grafx.domain.query.scalars import NATIVE_SCALARS, scalar_type
from okto_grafx.domain.query.extensions import TabularProcedure

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from okto_grafx.domain.errors import GrafxEmbeddingSpaceMismatch, GrafxPlanError
from okto_grafx.domain.index.catalog import CatalogIndexDefinition
from okto_grafx.domain.index.definition import (
    COLUMN_KEY_DERIVATION,
    ORDERED_KEY_DERIVATION,
    IndexDefinition,
    automatic_index_definitions,
    index_definition_matches_table,
)
from okto_grafx.domain.index.keys import custom_index_sizing
from okto_grafx.domain.index.layout import IndexLayout
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import VECTOR_DTYPES, ValueType, value_type_of
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.query.analysis import (
    Binding,
    QueryAnalysis,
    Aggregation,
    SimilarityUse,
    analyze,
    exact_path_projection,
    hop_range_refusal,
    named_path,
    named_path_refusal,
    optional_match_refusal,
    correlated_optional_pipeline,
    is_aggregate,
    untyped_one_hop_source,
    polymorphic_node_refusal,
)
from okto_grafx.domain.query.ast import (
    BinaryOperation,
    CaseExpression,
    ColumnSpec,
    CreateClause,
    CreateIndexStatement,
    CreateNodeTableStatement,
    CreateRelTableStatement,
    CreateVectorSpaceStatement,
    DeleteClause,
    Direction,
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
    ReturnClause,
    ReturnItem,
    SetClause,
    SortItem,
    Statement,
    Subscript,
    ListSlice,
    ListIteration,
    UnaryOperation,
    UnwindClause,
    UnionQuery,
    SubqueryClause,
    ProcedureCall,
    UpdatingClause,
    Variable,
    WithClause,
    free_variables,
    walk,
)
from okto_grafx.domain.query.plan import (
    AggregateRows,
    AllNodesScan,
    ApplyRows,
    ArgumentRows,
    SubqueryRows,
    ProcedureRows,
    CreatedNode,
    CreatedRelationship,
    CreateIndex,
    CreateNodeTable,
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
    PlanNode,
    ProduceResults,
    ProjectRows,
    PropertyAssignment,
    RelationshipIncidentSeek,
    SetProperties,
    SingleRow,
    SkipRows,
    SortRows,
    TraverseAnyRelationship,
    RelationshipScan,
    TraverseRelationship,
    UnionRows,
    UnwindRows,
    VectorSearch,
    WithRows,
    validate_plan,
)
from okto_grafx.domain.query.limits import MAX_EXPRESSION_DEPTH
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

__all__ = [
    "ANONYMOUS_VARIABLE_PREFIX",
    "COLUMN_VALUE_TYPES",
    "RELATIONSHIP_LOOKUP_FRONTIER_LIMIT",
    "SCORE_COLUMN",
    "THRESHOLD_OPERATORS",
    "PlannedQuery",
    "build_plan",
    "case_comparison_type",
    "case_result_type",
    "coalesce_result_type",
    "conjuncts_of",
    "subscript_argument_types",
]

RELATIONSHIP_LOOKUP_FRONTIER_LIMIT: int = 64
"""Largest literal LIMIT that keeps a no-predicate typed hop on endpoint lookups.

Below this boundary a seek/lookup frontier can stop before reading the whole relationship table;
above it the edge-first scan wins by avoiding the source-node scan.  The executor imports this
same value for its hybrid lookup-to-grouped-scan transition, so the planner and runtime cannot
drift onto different cost boundaries.
"""

ANONYMOUS_VARIABLE_PREFIX: str = "anonymous pattern element "
"""The name an unnamed pattern element is bound under while a query runs.

It carries spaces on purpose. A user variable is an ASCII identifier and can never contain one,
so an anonymous binding can never be shadowed by, or shadow, something the caller wrote.
"""

_PATH_PROJECTION_NODE_KEYS = frozenset({"_ID", "_LABEL"})
_PATH_PROJECTION_RELATIONSHIP_KEYS = frozenset({"_SRC", "_DST", "_LABEL", "_ID"})
"""The catalog declaration required by the one projected path."""

SCORE_COLUMN: str = "similarity score"
"""The row key the similarity operator writes its score under, for the same reason."""

THRESHOLD_OPERATORS: frozenset[str] = frozenset({">", ">="})
"""The comparisons a similarity threshold may use.

Only the two that put a FLOOR under the score. The vector subsystem returns the best neighbours
first, so a floor can be applied inside the operator without changing which rows it would have
returned; a ceiling would ask it to skip its own best answers, which is a different search.
"""

COLUMN_VALUE_TYPES: dict[str, ValueType] = {
    "INT64": ValueType.INT64,
    "STRING": ValueType.STRING,
    "DOUBLE": ValueType.DOUBLE,
    "BOOL": ValueType.BOOL,
    "BOOLEAN": ValueType.BOOL,
    "BLOB": ValueType.BYTES,
    "UUID": ValueType.UUID,
    "TIMESTAMP": ValueType.TIMESTAMP,
}
"""The scalar column types of the dialect mapped to the stored value types of the domain model."""

_SPACE_OPTIONS: tuple[str, ...] = ("dimension", "metric", "normalized", "storage_dtype")

_COALESCE_FAMILY: dict[ValueType, str] = {
    ValueType.BOOL: "boolean",
    ValueType.INT64: "number",
    ValueType.DOUBLE: "number",
    ValueType.STRING: "string",
    ValueType.TIMESTAMP: "timestamp",
}


def coalesce_result_type(
    function_name: str, argument_types: Sequence[ValueType | None]
) -> ValueType | None:
    """Infer a shared type when provable; unlike storage columns values may be heterogeneous."""
    concrete = set(argument_types) - {ValueType.NULL}
    if not concrete:
        return ValueType.NULL
    return next(iter(concrete)) if len(concrete) == 1 else None


def case_result_type(
    expression: CaseExpression, argument_types: Sequence[ValueType | None]
) -> ValueType | None:
    """Infer without coercing the selected arm or restricting valid value families."""
    return coalesce_result_type("CASE", argument_types)


def case_comparison_type(
    expression: CaseExpression, argument_types: Sequence[ValueType | None]
) -> None:
    """Validate the conditions of a searched CASE or comparands of a simple CASE."""
    concrete = tuple(
        value_type
        for value_type in argument_types
        if value_type is not None and value_type is not ValueType.NULL
    )
    if expression.operand is None:
        wrong = tuple(
            value_type for value_type in concrete if value_type is not ValueType.BOOL
        )
        if wrong:
            names = ", ".join(sorted({value_type.name for value_type in wrong}))
            message = f"A searched CASE tests booleans or nulls; got {names}."
            raise GrafxPlanError(
                message,
                field="case",
                value=expression.describe(),
            )
        return
    # Simple CASE uses ordinary three-valued equality across all query value families.
    return


def boolean_argument_types(operator: str, *types: ValueType | None, phase: str = "planning") -> None:
    """Reject proven non-boolean operands, without evaluating unknown values."""
    if any(kind not in (None, ValueType.NULL, ValueType.BOOL) for kind in types):
        raise GrafxPlanError("Boolean operators require BOOL or NULL operands.",
                             field="operator", value=operator,
                             reason="boolean_operand_type", query_phase=phase)


def membership_argument_type(kind: ValueType | None, *, phase: str = "planning") -> None:
    """IN accepts arbitrary left values, but only LIST/NULL on the right."""
    if kind not in (None, ValueType.NULL, ValueType.LIST):
        raise GrafxPlanError("IN looks inside a list; its right operand must be LIST or NULL.",
                             field="operator", value="IN", reason="membership_operand_type", query_phase=phase)


def subscript_argument_types(
    expression: Subscript, subject_type: ValueType | None, index_type: ValueType | None,
    *, phase: str = "planning",
) -> None:
    """Validate the statically or runtime-resolved arguments of one list extraction."""
    if subject_type not in (None, ValueType.NULL, ValueType.LIST, ValueType.MAP):
        message = f"A subscript extracts from a list or map; got {subject_type.name}."
        raise GrafxPlanError(
            message,
            field="subscript",
            value=expression.describe(),
            reason="subscript_subject_type", query_phase=phase,
        )
    allowed = ((ValueType.STRING,) if subject_type is ValueType.MAP else
               (ValueType.INT64,) if subject_type is ValueType.LIST else
               (ValueType.STRING, ValueType.INT64))
    if index_type not in (None, ValueType.NULL, *allowed):
        message = f"A subscript needs an integer list position or string map key; got {index_type.name}."
        raise GrafxPlanError(
            message,
            field="subscript",
            value=expression.describe(),
            reason="map_key_type" if subject_type is ValueType.MAP else "list_index_type",
            query_phase=phase,
        )


@dataclass(frozen=True, slots=True)
class PlannedQuery:
    """A plan together with what a caller needs to run and read it."""

    root: PlanNode
    analysis: QueryAnalysis
    columns: tuple[str, ...] = ()
    writes: bool = False
    coalesce_argument_types: tuple[
        tuple[FunctionCall, tuple[ValueType | None, ...]], ...
    ] = ()
    case_comparison_types: tuple[
        tuple[CaseExpression, tuple[ValueType | None, ...]], ...
    ] = ()
    case_result_types: tuple[
        tuple[CaseExpression, tuple[ValueType | None, ...]], ...
    ] = ()
    subscript_types: tuple[
        tuple[Subscript, tuple[ValueType | None, ValueType | None]], ...
    ] = ()
    pulse_expression_types: tuple[tuple[Expression, ValueType | None], ...] = ()
    type_alias_sources: tuple[tuple[Expression, Expression], ...] = ()
    label_calls: tuple[FunctionCall, ...] = ()
    timestamp_calls: tuple[FunctionCall, ...] = ()

    def describe(self) -> str:
        """Return the operator tree as one block of indented lines."""
        return "\n".join(self.root.render())


def conjuncts_of(predicate: Expression | None) -> tuple[Expression, ...]:
    """Return the top-level AND terms of a predicate, in written order.

    Splitting is what lets one conjunct become an index seek, another a similarity threshold and
    the rest a filter, without any of them being evaluated twice. Only AND splits: an OR term is
    one condition, and taking it apart would drop rows.
    """
    if predicate is None:
        return ()
    pending = [predicate]
    found: list[Expression] = []
    while pending:
        node = pending.pop(0)
        if isinstance(node, BinaryOperation) and node.operator == "AND":
            pending.insert(0, node.right)
            pending.insert(0, node.left)
            continue
        found.append(node)
    return tuple(found)


def _conjoin(terms: Sequence[Expression]) -> Expression | None:
    """Return the conjunction of these terms, or None when there are none."""
    if not terms:
        return None
    combined = terms[0]
    for term in terms[1:]:
        combined = BinaryOperation(operator="AND", left=combined, right=term)
    return combined




def build_plan(
    statement: Statement,
    *,
    catalog: Catalog,
    indexes: Sequence[IndexDefinition] = (),
    analysis: QueryAnalysis | None = None,
    scalar_types: Mapping[str, ValueType] | None = None,
    procedures: Mapping[str, TabularProcedure] | None = None,
) -> PlannedQuery:
    """Return the operator tree that answers one statement against this catalog.

    Only :class:`~okto_grafx.domain.errors.GrafxPlanError` and the typed refusals of the domain
    model leave this door, and the plan it returns has already been checked against the shape
    rules of :func:`~okto_grafx.domain.query.plan.validate_plan`.
    """
    if not isinstance(catalog, Catalog):
        raise GrafxPlanError(
            f"Planning needs the catalog of the database; got {type(catalog).__name__}.",
            field="catalog",
            value=type(catalog).__name__,
        )
    resolved = analysis if analysis is not None else analyze(statement)
    planner = _Planner(catalog=catalog, indexes=tuple(indexes), analysis=resolved,
                       scalar_types={} if scalar_types is None else scalar_types,
                       procedures={} if procedures is None else procedures)
    return planner.run(statement)


@dataclass(slots=True)
class _Planner:
    """The single-use builder that turns one statement into one operator tree."""

    catalog: Catalog
    indexes: tuple[IndexDefinition, ...]
    analysis: QueryAnalysis
    scalar_types: Mapping[str, ValueType] = field(default_factory=dict)
    procedures: Mapping[str, TabularProcedure] = field(default_factory=dict)
    imported_bindings: tuple[Binding, ...] = ()
    binding_types: dict[str, ValueType | None] = field(default_factory=dict)
    list_locals: set[str] = field(default_factory=set)
    argument_slot: int | None = None
    tables: dict[str, TableDef] = field(default_factory=dict)
    multi_hop_variables: set[str] = field(default_factory=set)
    polymorphic_variables: set[str] = field(default_factory=set)
    polymorphic_tables: dict[str, tuple[TableDef, ...]] = field(default_factory=dict)
    typed_endpoint_form: bool = False
    path_projection: PatternPath | None = None
    untyped_one_hop_label: str | None = None
    unwind_alias: str | None = None
    unwind_source: Expression | None = None
    unwind_sources: dict[str, Expression] = field(default_factory=dict)
    alias_definitions: dict[str, Expression] = field(default_factory=dict)
    label_calls: list[FunctionCall] = field(default_factory=list)
    timestamp_calls: list[FunctionCall] = field(default_factory=list)
    coalesce_argument_types: dict[
        int, tuple[FunctionCall, tuple[ValueType | None, ...]]
    ] = field(default_factory=dict)
    case_comparison_types: dict[
        int, tuple[CaseExpression, tuple[ValueType | None, ...]]
    ] = field(default_factory=dict)
    case_result_types: dict[
        int, tuple[CaseExpression, tuple[ValueType | None, ...]]
    ] = field(default_factory=dict)
    subscript_types: dict[
        int,
        tuple[Subscript, tuple[ValueType | None, ValueType | None]],
    ] = field(default_factory=dict)
    union_type_aliases: dict[str, Expression] = field(default_factory=dict)
    union_type_alias_depths: dict[str, int] = field(default_factory=dict)
    pulse_expression_types: dict[int, tuple[Expression, ValueType | None]] = field(
        default_factory=dict
    )
    type_alias_sources: dict[int, tuple[Expression, Expression]] = field(default_factory=dict)
    seek_rechecks: list[Expression] = field(default_factory=list)
    prefer_full_relationship_scan: bool = False
    anonymous: int = 0
    apply_slot: int = 0
    union_entity_outputs: dict[str, tuple[TableDef, ...]] = field(default_factory=dict)

    # --- entry -------------------------------------------------------------------------------

    def run(self, statement: Statement) -> PlannedQuery:
        """Plan whichever kind of statement this is."""
        if isinstance(statement, CreateIndexStatement):
            return self._planned(self._index(statement), writes=True)
        if isinstance(statement, CreateNodeTableStatement):
            return self._planned(self._node_table(statement), writes=True)
        if isinstance(statement, CreateRelTableStatement):
            return self._planned(self._rel_table(statement), writes=True)
        if isinstance(statement, CreateVectorSpaceStatement):
            return self._planned(self._vector_space(statement), writes=True)
        if type(statement) is UnionQuery:
            return self._union(statement)
        if isinstance(statement, Query):
            return self._query(statement)
        raise GrafxPlanError(
            f"A statement of type {type(statement).__name__} cannot be planned.",
            field="statement",
            value=type(statement).__name__,
        )

    def _union(self, statement: UnionQuery) -> PlannedQuery:
        """Compose branches in one transaction, retaining each value's runtime type."""
        self.analysis = analyze(statement, bindings=self.imported_bindings)
        columns = self.analysis.output_columns
        pipelines: list[PlanNode] = []
        entity_outputs: list[dict[str, tuple[TableDef, ...]]] = []
        for branch in (statement.left, statement.right):
            sub = _Planner(scalar_types=self.scalar_types, catalog=self.catalog,
                           procedures=self.procedures,
                           indexes=self.indexes, analysis=analyze(branch, bindings=self.imported_bindings),
                           imported_bindings=self.imported_bindings, argument_slot=self.argument_slot,
                           binding_types=dict(self.binding_types),
                           polymorphic_variables=set(self.polymorphic_variables),
                           polymorphic_tables=dict(self.polymorphic_tables),
                           tables=dict(self.tables), alias_definitions=dict(self.alias_definitions),
                           apply_slot=self.apply_slot)
            planned = sub.run(branch)
            entity_outputs.append(sub._entity_output_tables())
            root = planned.root
            if not isinstance(root, ProduceResults):
                raise GrafxPlanError("A UNION branch must produce rows.", field="union", value="branch")
            pipelines.append(root.child)
            self._absorb(sub)
            self.apply_slot = max(self.apply_slot, sub.apply_slot)
        combined = UnionRows(left=pipelines[0], right=pipelines[1], columns=columns)
        for name in columns:
            if all(name in branch for branch in entity_outputs):
                tables = tuple({table.table_id: table for branch in entity_outputs for table in branch[name]}.values())
                if tables and len({table.kind for table in tables}) == 1:
                    self.union_entity_outputs[name] = tables
        return self._planned(
            ProduceResults(child=combined if statement.all else DistinctRows(child=combined),
                           columns=columns),
            columns=columns,
        )

    def _entity_output_tables(self) -> dict[str, tuple[TableDef, ...]]:
        """Retain entity-table alternatives through UNION and returning subqueries."""
        statement = self.analysis.statement
        if isinstance(statement, UnionQuery):
            return self.union_entity_outputs
        if not isinstance(statement, Query) or statement.return_clause is None:
            return {}
        result = {}
        for item in statement.return_clause.items:
            expression = item.expression
            if isinstance(expression, Variable) and expression.name in self.tables:
                result[item.name] = (self.tables[expression.name],)
            elif isinstance(expression, Variable) and expression.name in self.polymorphic_variables:
                result[item.name] = self.polymorphic_tables.get(expression.name, self._node_tables())
            elif isinstance(expression, Literal) and expression.value is None:
                result[item.name] = ()
        return result

    def _absorb(self, other: "_Planner") -> None:
        """Take over what a branch planner proved, so one bind covers the pair."""
        self.coalesce_argument_types.update(other.coalesce_argument_types)
        self.case_comparison_types.update(other.case_comparison_types)
        self.case_result_types.update(other.case_result_types)
        self.subscript_types.update(other.subscript_types)
        self.pulse_expression_types.update(other.pulse_expression_types)
        self.type_alias_sources.update(other.type_alias_sources)
        self.label_calls.extend(other.label_calls)
        self.timestamp_calls.extend(other.timestamp_calls)


    def _union_type_expression(
        self,
        expression: Expression,
        *,
        active_aliases: frozenset[str] = frozenset(),
        typing_depth: int = 0,
    ) -> Expression:
        """Return a non-executable expression that exposes WITH aliases to UNION typing.

        A branch still executes its original RETURN expression against the row projected by
        ``WITH``.  The pre-stream type proof has a different need: postfix access such as
        ``WITH $map AS m RETURN m.key`` must be seen as ``$map.key`` so the ordinary binder can
        inspect the supplied map before either branch is read.  Rebuilding only nodes whose
        children change preserves every schema-derived type attached to untouched AST nodes.
        """
        if typing_depth > MAX_EXPRESSION_DEPTH:
            raise GrafxPlanError(
                f"A UNION typing expression may nest at most {MAX_EXPRESSION_DEPTH} "
                "levels after WITH aliases are resolved.",
                field="depth",
                value=MAX_EXPRESSION_DEPTH,
            )
        if isinstance(expression, Variable):
            definition = self.alias_definitions.get(expression.name)
            if definition is None:
                return expression
            cached = self.union_type_aliases.get(expression.name)
            if cached is not None:
                if (
                    typing_depth + self.union_type_alias_depths[expression.name]
                    > MAX_EXPRESSION_DEPTH
                ):
                    raise GrafxPlanError(
                        f"A UNION typing expression may nest at most "
                        f"{MAX_EXPRESSION_DEPTH} levels after WITH aliases are resolved.",
                        field="depth",
                        value=MAX_EXPRESSION_DEPTH,
                    )
                return cached
            if expression.name in active_aliases:
                raise GrafxPlanError(
                    "A UNION branch contains a cyclic WITH alias definition.",
                    field="plan",
                    value=expression.name,
                )
            resolved = self._union_type_expression(
                definition,
                active_aliases=active_aliases | {expression.name},
                typing_depth=typing_depth,
            )
            # One immutable node may stand behind every occurrence of an alias.  Keeping that
            # sharing turns a chain such as ``aN = aN-1 + aN-1`` into a linear DAG instead of
            # materialising an exponentially large typing tree.
            self.union_type_aliases[expression.name] = resolved
            self.union_type_alias_depths[expression.name] = self._expression_depth(
                resolved
            )
            return resolved

        def expanded(child: Expression) -> Expression:
            """Expand one child while carrying the active alias and typing depth."""
            return self._union_type_expression(
                child,
                active_aliases=active_aliases,
                typing_depth=typing_depth + 1,
            )

        if isinstance(expression, Property):
            subject = expanded(expression.subject)
            return (
                expression
                if subject is expression.subject
                else replace(expression, subject=subject)
            )
        if isinstance(expression, UnaryOperation):
            operand = expanded(expression.operand)
            return (
                expression
                if operand is expression.operand
                else replace(expression, operand=operand)
            )
        if isinstance(expression, BinaryOperation):
            left = expanded(expression.left)
            right = expanded(expression.right)
            if left is expression.left and right is expression.right:
                return expression
            return replace(expression, left=left, right=right)
        if isinstance(expression, NullCheck):
            operand = expanded(expression.operand)
            return (
                expression
                if operand is expression.operand
                else replace(expression, operand=operand)
            )
        if isinstance(expression, Subscript):
            subject = expanded(expression.subject)
            index = expanded(expression.index)
            if subject is expression.subject and index is expression.index:
                return expression
            return replace(expression, subject=subject, index=index)
        if isinstance(expression, ListIteration):
            return replace(expression, source=expanded(expression.source), body=expanded(expression.body),
                           predicate=None if expression.predicate is None else expanded(expression.predicate),
                           initial=None if expression.initial is None else expanded(expression.initial))
        if isinstance(expression, ListSlice):
            subject = expanded(expression.subject)
            start = None if expression.start is None else expanded(expression.start)
            end = None if expression.end is None else expanded(expression.end)
            if subject is expression.subject and start is expression.start and end is expression.end:
                return expression
            return replace(expression, subject=subject, start=start, end=end)
        if isinstance(expression, FunctionCall):
            arguments = tuple(expanded(argument) for argument in expression.arguments)
            named_arguments = tuple(
                argument
                if (value := expanded(argument.value)) is argument.value
                else replace(argument, value=value)
                for argument in expression.named_arguments
            )
            if all(
                new is old
                for new, old in zip(arguments, expression.arguments, strict=True)
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
            elements = tuple(expanded(element) for element in expression.elements)
            if all(
                new is old
                for new, old in zip(elements, expression.elements, strict=True)
            ):
                return expression
            return replace(expression, elements=elements)
        if isinstance(expression, MapExpression):
            entries = tuple(
                entry
                if (value := expanded(entry.value)) is entry.value
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
                None if expression.operand is None else expanded(expression.operand)
            )
            alternatives_list = []
            for alternative in expression.alternatives:
                condition = expanded(alternative.condition)
                result = expanded(alternative.result)
                alternatives_list.append(
                    alternative
                    if condition is alternative.condition
                    and result is alternative.result
                    else replace(alternative, condition=condition, result=result)
                )
            alternatives = tuple(alternatives_list)
            fallback = (
                None if expression.fallback is None else expanded(expression.fallback)
            )
            if (
                operand is expression.operand
                and fallback is expression.fallback
                and all(
                    new is old
                    for new, old in zip(
                        alternatives, expression.alternatives, strict=True
                    )
                )
            ):
                return expression
            return replace(
                expression,
                operand=operand,
                alternatives=alternatives,
                fallback=fallback,
            )
        return expression

    @staticmethod
    def _expression_depth(expression: Expression) -> int:
        """Return the longest child path in one already-bounded immutable expression DAG."""
        depths: dict[int, int] = {}

        def depth(node: Expression) -> int:
            """Return and memoize the longest child path below ``node``."""
            marker = id(node)
            known = depths.get(marker)
            if known is not None:
                return known
            children = node.children()
            answer = 0 if not children else 1 + max(depth(child) for child in children)
            depths[marker] = answer
            return answer

        return depth(expression)

    def _planned(
        self, root: PlanNode, *, columns: tuple[str, ...] = (), writes: bool = False
    ) -> PlannedQuery:
        """Wrap a root in the value a caller receives, after checking the plan's shape."""
        return PlannedQuery(
            root=validate_plan(root),
            analysis=self.analysis,
            columns=columns,
            writes=writes,
            coalesce_argument_types=tuple(self.coalesce_argument_types.values()),
            case_comparison_types=tuple(self.case_comparison_types.values()),
            case_result_types=tuple(self.case_result_types.values()),
            subscript_types=tuple(self.subscript_types.values()),
            pulse_expression_types=tuple(self.pulse_expression_types.values()),
            type_alias_sources=tuple(self.type_alias_sources.values()),
            label_calls=tuple(self.label_calls),
            timestamp_calls=tuple(self.timestamp_calls),
        )

    # --- schema ------------------------------------------------------------------------------

    def _index(self, statement: CreateIndexStatement) -> PlanNode:
        """Resolve a custom exact index against one committed node table."""
        table = self._table_named(statement.table, "table")
        if table.kind != "node":
            raise GrafxPlanError(
                f"A custom index is declared on a node table; {table.name!r} is a "
                f"{table.kind} table.",
                field="table",
                value=table.name,
            )
        layout = IndexLayout.parse(statement.layout or IndexLayout.HASH.value)
        if layout is IndexLayout.ORDERED:
            if (
                statement.bucket_count is not None
                or statement.expected_cardinality is not None
            ):
                raise GrafxPlanError(
                    "An ordered index has no hash-directory sizing options.",
                    field="layout",
                    value=layout.value,
                )
            bucket_count = 1
            expected_cardinality = None
            key_derivation = ORDERED_KEY_DERIVATION
        else:
            bucket_count, expected_cardinality = custom_index_sizing(
                bucket_count=statement.bucket_count,
                expected_cardinality=statement.expected_cardinality,
            )
            key_derivation = COLUMN_KEY_DERIVATION
        definition = IndexDefinition.on(
            table,
            name=statement.name,
            columns=statement.columns,
            visibility=IndexVisibility.EXACT,
            bucket_count=bucket_count,
            key_derivation=key_derivation,
            layout=layout,
        )
        if layout is IndexLayout.ORDERED:
            first, second = (table.columns[position] for position in definition.positions)
            if first.type is not ValueType.TIMESTAMP or second.type is not ValueType.STRING:
                raise GrafxPlanError(
                    "An ordered index requires TIMESTAMP then STRING key columns; "
                    f"got {first.type.name} then {second.type.name}.",
                    field="columns",
                    value=statement.columns,
                    table=table.name,
                )
        logical = CatalogIndexDefinition(
            name=definition.name,
            table_id=definition.table_id,
            table_name=definition.table_name,
            positions=definition.positions,
            visibility=definition.visibility,
            key_derivation=definition.key_derivation,
            layout=definition.layout,
            expected_cardinality=expected_cardinality,
        )
        self._require_new_index_name(logical.name)
        return CreateIndex(
            name=logical.name,
            table=table,
            positions=logical.positions,
            bucket_count=definition.bucket_count,
            expected_cardinality=logical.expected_cardinality,
            layout=logical.layout,
            key_derivation=logical.key_derivation,
        )

    def _require_new_index_name(self, name: str) -> None:
        """Refuse a logical name already owned by persisted or schema-derived authority."""
        key = name.lower()
        collision = self.catalog.has_index_definition(name) or any(
            definition.registry_key == key for definition in self.indexes
        )
        if not collision:
            collision = any(
                definition.registry_key == key
                for table in self.catalog.tables()
                for definition in automatic_index_definitions(table)
            )
        if collision:
            raise GrafxPlanError(
                f"An index named {name!r} already exists without regard to case.",
                field="name",
                value=name,
            )

    def _node_table(self, statement: CreateNodeTableStatement) -> PlanNode:
        """Plan a CREATE NODE TABLE statement."""
        columns = self._columns(statement.columns, statement.primary_key)
        if statement.primary_key is not None and statement.primary_key not in {
            column.name for column in columns
        }:
            raise GrafxPlanError(
                f"Table {statement.name!r} names {statement.primary_key!r} as its primary key, "
                "which is not one of its columns.",
                field="primary_key",
                value=statement.primary_key,
            )
        return CreateNodeTable(
            name=statement.name, columns=columns, primary_key=statement.primary_key
        )

    def _rel_table(self, statement: CreateRelTableStatement) -> PlanNode:
        """Plan a CREATE REL TABLE statement, checking that both endpoints are node tables."""
        for role, name in (("from", statement.from_table), ("to", statement.to_table)):
            endpoint = self._table_named(name, role)
            if endpoint.kind != "node":
                raise GrafxPlanError(
                    f"A relationship connects node tables; {name!r} is a {endpoint.kind} table.",
                    field=role,
                    value=name,
                )
        return CreateRelTable(
            name=statement.name,
            from_table=statement.from_table,
            to_table=statement.to_table,
            columns=self._columns(statement.columns, None),
        )

    def _vector_space(self, statement: CreateVectorSpaceStatement) -> PlanNode:
        """Plan a CREATE VECTOR SPACE statement from its options map."""
        options = statement.options
        for key in options.keys():
            if key.lower() not in _SPACE_OPTIONS:
                raise GrafxPlanError(
                    f"An embedding space has no option named {key!r}; it takes "
                    f"{', '.join(_SPACE_OPTIONS)}.",
                    field="option",
                    value=key,
                )
        dimension = self._required_option(options, "dimension", int)
        metric_name = self._required_option(options, "metric", str)
        normalized = self._optional_option(options, "normalized", bool, False)
        storage_dtype = self._optional_option(options, "storage_dtype", str, "float32")
        metric = self._metric(metric_name)
        return CreateVectorSpace(
            name=statement.name,
            dimension=dimension,
            metric=metric,
            normalized=normalized,
            storage_dtype=storage_dtype,
        )

    def _metric(self, name: str) -> DistanceMetric:
        """Return the distance metric of that name, refusing one the port does not offer."""
        for candidate in DistanceMetric:
            if candidate.value == name:
                return candidate
        allowed = ", ".join(repr(candidate.value) for candidate in DistanceMetric)
        raise GrafxPlanError(
            f"An embedding space compares vectors with one of {allowed}; got {name!r}.",
            field="metric",
            value=name,
        )

    def _required_option(self, options: MapExpression, key: str, kind: type) -> object:
        """Return one option of an embedding space, refusing a missing or non-constant one."""
        value = options.entry(key)
        if value is None:
            raise GrafxPlanError(
                f"An embedding space must declare its {key}.",
                field=key,
                value=None,
            )
        return self._constant(value, key, kind)

    def _optional_option(
        self, options: MapExpression, key: str, kind: type, fallback: object
    ) -> object:
        """Return one option of an embedding space, or the default when it was not written."""
        value = options.entry(key)
        if value is None:
            return fallback
        return self._constant(value, key, kind)

    def _constant(self, expression: Expression, key: str, kind: type) -> object:
        """Return the constant an option carries, refusing anything decided at run time."""
        if not isinstance(expression, Literal):
            raise GrafxPlanError(
                f"The {key} of an embedding space is written as a constant; got "
                f"{expression.describe()}.",
                field=key,
                value=expression.describe(),
            )
        value = expression.value
        if kind is bool:
            if not isinstance(value, bool):
                raise self._wrong_option(key, "true or false", expression)
            return value
        if kind is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise self._wrong_option(key, "a whole number", expression)
            return value
        if not isinstance(value, str):
            raise self._wrong_option(key, "a name in quotes", expression)
        return value

    def _wrong_option(
        self, key: str, wanted: str, expression: Expression
    ) -> GrafxPlanError:
        """Return the refusal for an option written with the wrong kind of constant."""
        return GrafxPlanError(
            f"The {key} of an embedding space is {wanted}; got {expression.describe()}.",
            field=key,
            value=expression.describe(),
        )

    def _columns(
        self, specs: Sequence[ColumnSpec], primary_key: str | None
    ) -> tuple[ColumnDef, ...]:
        """Resolve the declared columns against the catalog, refusing a name written twice."""
        seen: set[str] = set()
        resolved: list[ColumnDef] = []
        for spec in specs:
            if spec.name in seen:
                raise GrafxPlanError(
                    f"The column {spec.name!r} is declared more than once.",
                    field="column",
                    value=spec.name,
                )
            seen.add(spec.name)
            resolved.append(self._column(spec, nullable=spec.name != primary_key))
        return tuple(resolved)

    def _column(self, spec: ColumnSpec, *, nullable: bool) -> ColumnDef:
        """Resolve one declared column, reading a vector column's precision from its space."""
        if spec.vector_space is None:
            value_type = COLUMN_VALUE_TYPES.get(spec.type_name)
            if value_type is None:
                raise GrafxPlanError(
                    f"The column {spec.name!r} declares the type {spec.type_name!r}, which this "
                    "engine does not store.",
                    field="type",
                    value=spec.type_name,
                )
            return ColumnDef(name=spec.name, type=value_type, nullable=nullable)
        space = self._space_named(spec.vector_space)
        return ColumnDef(
            name=spec.name,
            type=VECTOR_DTYPES[space.storage_dtype],
            nullable=nullable,
            vector_space=space.name,
        )

    # --- catalog -----------------------------------------------------------------------------

    def _table_named(self, name: str, role: str) -> TableDef:
        """Return one table by name, refusing a name the catalog does not know."""
        if not self.catalog.has_table(name):
            known = ", ".join(table.name for table in self.catalog.tables()) or "none"
            raise GrafxPlanError(
                f"No table named {name!r} exists; the tables of this database are {known}.",
                field=role,
                value=name,
            )
        return self.catalog.table(name)

    def _space_named(self, name: str) -> EmbeddingSpaceDef:
        """Return one embedding space by name, refusing one the catalog does not know."""
        if not self.catalog.has_space(name):
            known = ", ".join(space.name for space in self.catalog.spaces()) or "none"
            raise GrafxPlanError(
                f"No embedding space named {name!r} exists; the spaces of this database are "
                f"{known}.",
                field="space",
                value=name,
            )
        return self.catalog.space(name)

    def _column_of(self, table: TableDef, key: str) -> ColumnDef | None:
        """Return one column of a table by name, or None when the table has no such column."""
        for column in table.columns:
            if column.name == key:
                return column
        return None

    def _index_for(
        self, table: TableDef, constrained: Sequence[str]
    ) -> tuple[IndexDefinition, tuple[str, ...]] | None:
        """Return the widest index whose WHOLE key is constrained by these columns.

        The whole key and nothing less. A hash index stores the encoding of ALL its key columns
        as one key, so a seek that constrained only some of them would be asking for entries
        under a key that was never written -- an empty answer for a query that has rows, which
        is the wrong-result failure CONTRACT.md section 13 item 2 names.

        The key order comes from the index definition rather than from the order the conditions
        were written in, because the definition is what decided the bytes. Candidates are ranked
        by key width and then by name, so the choice does not depend on the order the caller
        happened to register its indexes in.
        """
        by_position: dict[int, str] = {}
        for name in constrained:
            position = table.column_index(name)
            by_position[position] = name
        candidates: list[tuple[int, str, IndexDefinition, tuple[str, ...]]] = []
        for definition in self.indexes:
            if not index_definition_matches_table(definition, table):
                continue
            if (
                definition.key_derivation != COLUMN_KEY_DERIVATION
                or not definition.positions
            ):
                # RecordId and value-derived indexes have dedicated access paths.  In
                # particular, the identity definition has no column positions, so treating
                # ``all([])`` as a generic equality match would select it for every predicate.
                continue
            if not all(position in by_position for position in definition.positions):
                continue
            columns = tuple(by_position[position] for position in definition.positions)
            candidates.append((-len(columns), definition.name, definition, columns))
        if not candidates:
            return None
        candidates.sort(key=lambda entry: (entry[0], entry[1]))
        chosen = candidates[0]
        return chosen[2], chosen[3]

    # --- queries -----------------------------------------------------------------------------

    def _query(self, statement: Query) -> PlannedQuery:
        """Plan a reading and updating query."""
        # A supplied summary is not authority for an ordered pipeline's parameters,
        # scopes or write classification. Compile once from its complete clauses.
        self.analysis = analyze(statement, bindings=self.imported_bindings)
        from okto_grafx.domain.query.scopes import lower_scopes
        statement = lower_scopes(statement, tuple(binding.name for binding in self.imported_bindings))
        self.analysis = analyze(statement, bindings=self.imported_bindings)
        for expression in self._query_expressions(statement):
            for local in walk(expression):
                if isinstance(local, ListIteration):
                    self.list_locals.add(local.variable)
                    self.binding_types[local.variable] = None
                    if local.accumulator is not None:
                        self.list_locals.add(local.accumulator)
                        self.binding_types[local.accumulator] = None
        projected_path = exact_path_projection(statement)
        refusal = hop_range_refusal(statement)
        if refusal is not None:
            message, value = refusal
            raise GrafxPlanError(message, field="hops", value=value)
        refusal = named_path_refusal(statement)
        if refusal is not None:
            message, value = refusal
            raise GrafxPlanError(message, field="pattern", value=value)
        self._refuse_named_path_reads(statement, projected_path=projected_path)
        refusal = polymorphic_node_refusal(statement)
        if refusal is not None:
            message, value = refusal
            raise GrafxPlanError(message, field="pattern", value=value)
        refusal = optional_match_refusal(statement)
        if refusal is not None:
            message, value = refusal
            raise GrafxPlanError(message, field="clause", value=value)
        self.typed_endpoint_form = self._is_typed_endpoint_form(statement)
        self.path_projection = projected_path
        untyped_source = untyped_one_hop_source(statement)
        self.untyped_one_hop_label = (
            None if untyped_source is None else untyped_source.labels[0]
        )
        if (untyped_source is not None or projected_path is not None
                or statement.with_clauses or correlated_optional_pipeline(statement)):
            # Each literal recogniser judged the STATEMENT, while the pipeline below also reads
            # the analysis -- which a caller may have supplied. A supplied summary that claims
            # an aggregation gets one: _result inserts AggregateRows over a statement that
            # aggregates nothing. Recompute from the statement the gate actually approved so
            # neither literal can be widened through its analysis argument.
            self.analysis = analyze(statement, bindings=self.imported_bindings)
        returned = statement.return_clause
        literal_limit = None if returned is None else returned.limit
        self.prefer_full_relationship_scan = bool(
            self.analysis.aggregated
            or returned is None
            or returned.limit is None
            or (
                isinstance(literal_limit, Literal)
                and type(literal_limit.value) is int
                and literal_limit.value > RELATIONSHIP_LOOKUP_FRONTIER_LIMIT
            )
        )
        pipeline: PlanNode = SingleRow() if self.argument_slot is None else ArgumentRows(slot=self.argument_slot)
        similarity_terms: list[Expression] = []
        similarity_placed = False
        correlated = correlated_optional_pipeline(statement)
        pending_write = False
        for clause in statement.ordered_clauses():
            if isinstance(clause, ProcedureCall):
                procedure = self.procedures.get(clause.name)
                if procedure is None:
                    raise GrafxPlanError("Procedure is not registered or its permissions were not granted.",
                                         field="procedure", value=clause.name)
                if len(clause.arguments) != len(procedure.argument_types):
                    raise GrafxPlanError("Procedure argument count mismatch.", field="procedure_arity")
                for argument, expected in zip(clause.arguments, procedure.argument_types, strict=True):
                    actual = self._pulse_expression_type(argument, owner="procedure argument")
                    self._require_resolvable_runtime_type(argument, actual, owner="procedure argument")
                    if actual not in (None, ValueType.NULL, ValueType[expected]):
                        raise GrafxPlanError("Procedure argument type mismatch.", field="procedure_type")
                columns = dict(procedure.columns)
                for item in clause.yields:
                    if item.expression.name not in columns:
                        raise GrafxPlanError("YIELD names an undeclared procedure column.", field="yield", value=item.expression.name)
                    self.binding_types[item.name] = ValueType[columns[item.expression.name]]
                pipeline = ProcedureRows(child=pipeline, name=procedure.name, columns=procedure.columns,
                                         max_rows=procedure.max_rows, max_result_bytes=procedure.max_result_bytes,
                                         arguments=clause.arguments, yields=clause.yields)
                if clause.predicate is not None:
                    pipeline = FilterRows(child=pipeline, predicate=clause.predicate)
                continue
            if isinstance(clause, SubqueryClause):
                self.apply_slot += 1
                slot = self.apply_slot
                outer = clause.outer_names or clause.imports
                imported = tuple(Binding(name=inner, entity="expression alias", labels=(), created=False)
                                 for inner in clause.imports)
                from dataclasses import replace
                imported = tuple(replace(binding, entity=("relationship" if self.tables[source].kind == "rel" else "node"),
                                         labels=(self.tables[source].name,))
                                 if source in self.tables else
                                 replace(binding, entity=("relationship" if self.polymorphic_tables.get(source)
                                         and self.polymorphic_tables[source][0].kind == "rel" else "node"))
                                 if source in self.polymorphic_variables else binding
                                 for source, binding in zip(outer, imported, strict=True))
                sub = _Planner(
                    catalog=self.catalog, indexes=self.indexes, scalar_types=self.scalar_types,
                    procedures=self.procedures,
                    analysis=analyze(clause.query, bindings=imported), imported_bindings=imported,
                    argument_slot=slot, apply_slot=slot,
                    tables={target: self.tables[source] for source, target in zip(outer, clause.imports)
                            if source in self.tables},
                    polymorphic_variables={target for source, target in zip(outer, clause.imports)
                                           if source in self.polymorphic_variables},
                    polymorphic_tables={target: self.polymorphic_tables.get(source, self._node_tables())
                                        for source, target in zip(outer, clause.imports)
                                        if source in self.polymorphic_variables},
                    binding_types={target: self._pulse_expression_type(Variable(source), owner="subquery import")
                                   for source, target in zip(outer, clause.imports)
                                   if not self._matched_row(source)},
                )
                planned = sub.run(clause.query)
                assert isinstance(planned.root, ProduceResults)
                self._absorb(sub)
                self.apply_slot = max(self.apply_slot, sub.apply_slot)
                outputs = clause.output_aliases or planned.columns
                lowered = sub.analysis.statement
                if isinstance(lowered, Query) and lowered.return_clause is not None:
                    for target, item in zip(outputs, lowered.return_clause.items, strict=True):
                        expression = item.expression
                        if isinstance(expression, Variable) and expression.name in sub.tables:
                            self.tables[target] = sub.tables[expression.name]
                        elif isinstance(expression, Variable) and expression.name in sub.polymorphic_variables:
                            self.polymorphic_variables.add(target)
                            self.polymorphic_tables[target] = sub.polymorphic_tables.get(expression.name, self._node_tables())
                        else:
                            self.binding_types[target] = sub._pulse_expression_type(expression, owner="subquery output")
                else:
                    entity_tables = sub._entity_output_tables()
                    for source, target in zip(planned.columns, outputs, strict=True):
                        alternatives = entity_tables.get(source)
                        if alternatives:
                            if len(alternatives) == 1:
                                self.tables[target] = alternatives[0]
                            else:
                                self.polymorphic_variables.add(target)
                                self.polymorphic_tables[target] = alternatives
                        else:
                            self.binding_types[target] = None
                pipeline = SubqueryRows(child=pipeline, inner=planned.root.child, slot=slot,
                                        imports=tuple(zip(outer, clause.imports, strict=True)),
                                        outputs=outputs)
                continue
            if isinstance(clause, UnwindClause):
                self.unwind_alias = clause.alias
                self.unwind_source = clause.expression
                self.unwind_sources[clause.alias] = clause.expression
                pipeline = UnwindRows(child=pipeline, alias=clause.alias, expression=clause.expression)
                continue
            if isinstance(clause, WithClause):
                if pending_write:
                    pipeline = EagerRows(child=pipeline, publish_read_phase=True)
                    pending_write = False
                pipeline = self._with_clause(pipeline, clause)
                continue
            if not isinstance(clause, MatchClause):
                pipeline = self._updating_clause(pipeline, clause)
                pending_write = True
                continue
            if clause.optional and correlated:
                pipeline = self._correlated_optional(pipeline, clause)
                continue
            if clause.optional:
                before = set(self.tables) | self.polymorphic_variables
                self.apply_slot += 1
                slot = self.apply_slot
                inner, deferred = self._match_clause(ArgumentRows(slot=slot), clause)
                if deferred or (self.analysis.similarity is not None
                                and self.analysis.similarity.variable not in before):
                    if not isinstance(pipeline, SingleRow) or len(statement.match_clauses) != 1:
                        raise GrafxPlanError("A correlated optional predicate cannot defer vector search.", field="clause")
                    # Root optional vector filtering must finish before null extension.
                    inner = self._similarity(inner, statement, deferred)
                    similarity_placed = True
                introduced = (set(self.tables) | self.polymorphic_variables) - before
                pipeline = ApplyRows(child=pipeline, inner=inner, slot=slot,
                                     null_variables=tuple(sorted(introduced)))
                continue
            pipeline, deferred = self._match_clause(pipeline, clause)
            similarity_terms.extend(deferred)
        if not similarity_placed:
            pipeline = self._similarity(pipeline, statement, similarity_terms)
        self._record_polymorphic_properties(statement)
        self._record_coalesce_types(statement)
        self._record_label_arguments(statement)
        self._record_case_and_subscript_types(statement)
        # CASE/subscript metadata has to exist before timestamp() asks for the type of a
        # composed argument such as timestamp(CASE ... END).  Recording the instant first
        # would mistake an otherwise fully knowable CASE for an unresolved expression.
        self._record_timestamp_arguments(statement)
        if statement.updating_clauses:
            # Everything that writes is drawn in full before anything above can stop early. A
            # LIMIT truncates what the caller RECEIVES; it must not decide how many rows got
            # written, and without this barrier it does -- silently, and differently depending on
            # whether an ORDER BY happens to sit beside it.
            pipeline = EagerRows(child=pipeline)
        columns: tuple[str, ...] = ()
        if statement.return_clause is not None:
            pipeline = self._result(pipeline, statement.return_clause)
            columns = statement.return_clause.column_names()
        return self._planned(
            ProduceResults(child=pipeline, columns=columns),
            columns=columns,
            writes=statement.writes,
        )

    def _correlated_optional(self, pipeline: PlanNode, clause: MatchClause) -> PlanNode:
        """Plan an optional incident expansion over snapshot-visible relationship tables."""
        pattern = clause.patterns[0]
        source, target = pattern.nodes
        edge = pattern.relationships[0]
        anchor = self.tables[source.variable]
        if edge.types:
            candidates = (self._relationship_table(edge),)
        else:
            candidates = tuple(t for t in self.catalog.tables() if t.kind == "rel")
        selected = []
        for table in sorted(candidates, key=lambda t: t.table_id):
            self._table_named(table.from_table, "from")
            self._table_named(table.to_table, "to")
            outgoing = edge.direction in (Direction.OUTGOING, Direction.UNDIRECTED)
            incoming = edge.direction in (Direction.INCOMING, Direction.UNDIRECTED)
            if ((outgoing and table.from_table == anchor.name)
                    or (incoming and table.to_table == anchor.name)):
                selected.append(table)
        target_name = target.variable or self._anonymous()
        if target.labels:
            target_definition = self._table_named(target.labels[0], "label")
            if target_definition.kind != "node":
                raise GrafxPlanError("An optional hop target must name a node table.")
            self.tables[target_name] = target_definition
        elif target.variable:
            self.polymorphic_variables.add(target.variable)
            landing_names = set()
            for table in selected:
                if edge.direction in (Direction.OUTGOING, Direction.UNDIRECTED) and table.from_table == anchor.name:
                    landing_names.add(table.to_table)
                if edge.direction in (Direction.INCOMING, Direction.UNDIRECTED) and table.to_table == anchor.name:
                    landing_names.add(table.from_table)
            self.polymorphic_tables[target.variable] = tuple(
                table for table in self._node_tables() if table.name in landing_names
            )
        if edge.variable and edge.types:
            self.tables[edge.variable] = candidates[0]
        elif edge.variable:
            self.polymorphic_variables.add(edge.variable)
            self.polymorphic_tables[edge.variable] = tuple(selected)
        if clause.predicate is not None and self._reads_similarity(clause.predicate):
            raise GrafxPlanError("An optional hop predicate cannot perform vector search.")
        return TraverseAnyRelationship(
            child=pipeline, source=source.variable, target=target_name,
            relationship=edge.variable or self._anonymous(), tables=tuple(selected),
            direction=edge.direction, optional=True, source_table=anchor.name,
            target_table=target.labels[0] if target.labels else None,
            relationship_polymorphic=not edge.types,
            predicate=clause.predicate,
        )

    def _with_clause(self, pipeline: PlanNode, clause: WithClause) -> PlanNode:
        """Plan one WITH stage: the projection, then the WHERE that belongs to it."""
        for item in clause.items:
            if item.alias is None or item.expression == Variable(name=item.alias):
                # A name carried under itself defines nothing new; recording it would make the
                # alias its own definition and the type of it would ask for itself.
                continue
            # What the alias stands for, so a type that is provable for the expression is
            # provable for every use of the name below it: parts[1] is a STRING because parts
            # is the string_split() this stage projected.
            self.alias_definitions[item.alias] = item.expression
            if isinstance(item.expression, Variable):
                original = item.expression.name
                if original in self.tables:
                    self.tables[item.alias] = self.tables[original]
                if original in self.polymorphic_variables:
                    self.polymorphic_variables.add(item.alias)
                    if original in self.polymorphic_tables:
                        self.polymorphic_tables[item.alias] = self.polymorphic_tables[original]
        aggregations = tuple(
            Aggregation(position=position, call=call)
            for position, item in enumerate(clause.items)
            for call in walk(item.expression) if is_aggregate(call)
        )
        if aggregations:
            grouped_positions = {aggregate.position for aggregate in aggregations}
            pipeline = AggregateRows(
                child=pipeline,
                grouping=tuple(item for position, item in enumerate(clause.items)
                               if position not in grouped_positions),
                aggregations=aggregations,
                preserve_group_bindings=True,
            )
        pipeline = WithRows(child=pipeline, items=clause.items)
        if clause.distinct:
            pipeline = DistinctRows(child=pipeline)
        if clause.sort_items:
            pipeline = SortRows(child=pipeline, keys=clause.sort_items,
                                retained_limit=clause.limit,
                                retained_skip=clause.skip if clause.limit is not None else None)
        if clause.skip is not None:
            pipeline = SkipRows(child=pipeline, count=clause.skip)
        if clause.limit is not None:
            pipeline = LimitRows(child=pipeline, count=clause.limit)
        if clause.predicate is not None:
            # The predicate belongs to THIS stage, so it filters what the projection produced
            # rather than what the projection read. It is not pushed down beside the pattern
            # filters for the same reason: the names it reads exist only above this operator.
            pipeline = FilterRows(child=pipeline, predicate=clause.predicate)
        return pipeline

    def _refuse_named_path_reads(
        self, statement: Query, *, projected_path: PatternPath | None
    ) -> None:
        """Refuse every path-name read except the one exact projected path.

        The analysis refuses each of these where it is written, and says which clause asked.
        This repeats the rule rather than the message, because ``build_plan`` accepts an
        ANALYSIS from its caller, and an analysis that never looked is an analysis that never
        refused. The shape gate above is not enough on its own: it decides what a query may
        LOOK like, and this decides what the name inside it may be used for.

        It also catches the collision case, and correctly: when the path shares a name with a
        node, a read of that name cannot be told from a read of the path, so neither is allowed
        to reach a row.
        """
        named = named_path(statement)
        if named is None or named.variable is None:
            return
        if named is projected_path:
            # The exact recogniser has checked every expression-bearing site and proved that
            # the sole occurrence is the one unaliased RETURN item. Nothing wider is skipped.
            return
        for expression in self._query_expressions(statement):
            for node in walk(expression):
                if not isinstance(node, Variable):
                    continue
                if type(node.name) is not str:
                    # ``build_plan`` accepts a caller-supplied analysis, so this AST boundary
                    # must be checked independently.  Do it before equality: a hand-built
                    # ``str`` subclass can make comparison execute arbitrary code.
                    raise GrafxPlanError(
                        "A variable is named with a name the parser could have written.",
                        field="variable",
                        value="a variable name",
                    )
                if node.name == named.variable:
                    raise GrafxPlanError(
                        f"The path {named.variable!r} is written and never read in this "
                        "subset, so nothing may project it or filter on it.",
                        field="variable",
                        value=named.variable,
                    )

    def _record_coalesce_types(self, statement: Query) -> None:
        """Resolve every COALESCE argument whose type the bound schema makes knowable."""
        for expression in self._query_expressions(statement):
            for node in walk(expression):
                if isinstance(node, Property) and isinstance(node.subject, Variable):
                    binding = self.analysis.binding(node.subject.name)
                    if binding is not None and binding.entity == "path":
                        raise GrafxPlanError("A path has no property map; use nodes() or relationships().", field="property", value=node.key)
                if not isinstance(node, FunctionCall):
                    continue
                if node.name.upper() == SIZE_FUNCTION and node.arguments and isinstance(node.arguments[0], Variable):
                    binding = self.analysis.binding(node.arguments[0].name)
                    if binding is not None and binding.entity == "path":
                        raise GrafxPlanError("Use length() for paths; size() accepts strings/lists.", field="function", value=node.name)
                if node.name.upper() in NATIVE_SCALARS | {"LENGTH", "NODES", "RELATIONSHIPS", "SUM", "AVG"}:
                    self._pulse_expression_type(node, owner=node.name)
                if node.name.upper() == "UDF":
                    if (not node.arguments or type(node.arguments[0]) is not Literal
                            or type(node.arguments[0].value) is not str
                            or node.arguments[0].value not in self.scalar_types):
                        raise GrafxPlanError("Scalar function is not registered on this handle.", field="udf_name")
                if node.name.upper() != COALESCE_FUNCTION:
                    continue
                types = tuple(
                    self._coalesce_argument_type(node, argument)
                    for argument in node.arguments
                )
                coalesce_result_type(node.name, types)
                # FunctionCall is a frozen dataclass, so two separately parsed calls with the
                # same text compare equal.  Their schema context need not be equal: UNION
                # branches may deliberately reuse ``n`` for different tables.  Metadata is
                # therefore owned by the AST occurrence, never by structural equality.
                self.coalesce_argument_types[id(node)] = (node, types)


    def _record_label_arguments(self, statement: Query) -> None:
        """Refuse a label() argument that is not one matched row, while still planning.

        The runtime can only read a table off a binding, so anything else is wrong before a
        row exists.  Leaving the check to evaluation would let an empty match answer with no
        rows for a query that could never have worked.
        """

        for expression in self._query_expressions(statement):
            for node in walk(expression):
                if not isinstance(node, FunctionCall):
                    continue
                if node.name.upper() != LABEL_FUNCTION:
                    continue
                argument = node.arguments[0]
                if isinstance(argument, Literal) and argument.value is None:
                    # A written null is the one non-binding the contract answers rather than
                    # refuses, and it answers null.
                    continue
                if isinstance(argument, Parameter):
                    # The value arrives with the call, so the binder decides: null answers
                    # null, anything else is refused there, still before the first row.
                    self.label_calls.append(node)
                    continue
                if not isinstance(argument, Variable):
                    message = (
                        f"{node.name} reads the table of a matched node or relationship; "
                        f"{argument.describe()} is not one."
                    )
                    raise GrafxPlanError(
                        message,
                        field="function",
                        value=node.name,
                    )
                if argument.name in self.multi_hop_variables:
                    message = (
                        f"{node.name} reads one matched row, and {argument.name!r} is a "
                        "variable-length relationship, which binds every hop it walked."
                    )
                    raise GrafxPlanError(
                        message,
                        field="function",
                        value=node.name,
                    )
                if not self._matched_row(argument.name):
                    message = (
                        f"{node.name} reads the table of a matched node or relationship; "
                        f"{argument.name!r} is bound to none."
                    )
                    raise GrafxPlanError(
                        message,
                        field="function",
                        value=node.name,
                    )

    def _record_timestamp_arguments(self, statement: Query) -> None:
        """Carry every timestamp() call to the binder, which is where the value arrives.

        The reading itself cannot be judged here: a literal could be checked, but a parameter
        is only known at the call, and both have to answer the same way.  Recording the call
        lets one converter decide both, before the first row.
        """

        for expression in self._query_expressions(statement):
            for node in walk(expression):
                if not isinstance(node, FunctionCall):
                    continue
                if node.name.upper() != TIMESTAMP_FUNCTION:
                    continue
                self._require_readable_instant(node)
                self.timestamp_calls.append(node)

    def _require_readable_instant(self, node: FunctionCall) -> None:
        """Refuse an argument whose family the schema already rules out.

        A bound parameter is left to the binder because only the call knows its value, but a
        column of the wrong type, or a matched row, is wrong the moment the tables resolve.
        Waiting for evaluation would let an empty match answer with no rows for a query that
        could never have produced an instant.
        """

        argument = node.arguments[0]
        if isinstance(argument, Variable):
            if argument.name in self.multi_hop_variables or self._matched_row(
                argument.name
            ):
                message = (
                    f"{node.name} reads an ISO-8601 string or a timestamp; "
                    f"{argument.name!r} is a matched row."
                )
                raise GrafxPlanError(
                    message,
                    field="function",
                    value=node.name,
                )
            return
        # Every remaining shape goes through the same question rather than a list of the
        # ones thought of here.  Enumerating shapes is how `1 + 1` slipped past: it is
        # neither binder-resolvable nor one of the three that were named, so an empty match
        # answered with no rows for an argument that could never be an instant.
        static = self._pulse_expression_type(argument, owner=node.name)
        if static in (None, ValueType.NULL, ValueType.STRING, ValueType.TIMESTAMP):
            return
        message = (
            f"{node.name} reads an ISO-8601 string or a timestamp; got {static.name}."
        )
        raise GrafxPlanError(
            message,
            field="function",
            value=node.name,
        )

    def _record_case_and_subscript_types(self, statement: Query) -> None:
        """Resolve CASE and list-subscript types after every pattern has bound a table."""
        for expression in self._query_expressions(statement):
            # Children are recorded before their parents so nested CASE expressions expose their
            # result type to an enclosing CASE and STRING_SPLIT(...)[n] exposes STRING.
            for node in reversed(tuple(walk(expression))):
                marker = id(node)
                if (isinstance(node, UnaryOperation) and node.operator == "NOT"
                        or isinstance(node, BinaryOperation) and node.operator in {"AND", "OR", "XOR", "IN"}):
                    self._pulse_expression_type(node, owner=node.describe())
                if isinstance(node, Property):
                    subject = node.subject
                    # Graph bindings have their schema/property validation path.
                    # Projected scalar aliases, unlike live entities, can already
                    # prove that this access could never yield a property.
                    if isinstance(subject, Variable):
                        subject_type = self.binding_types.get(subject.name)
                        if (subject.name in self.alias_definitions
                                and subject.name not in self.tables
                                and subject.name not in self.polymorphic_variables):
                            subject_type = self._pulse_expression_type(subject, owner=node.describe())
                    else:
                        subject_type = self._pulse_expression_type(subject, owner=node.describe())
                    if subject_type not in (None, ValueType.NULL, ValueType.MAP):
                        raise GrafxPlanError("Property access requires a map, graph entity or NULL.",
                                             field="property", value=node.key,
                                             reason="property_subject_type", query_phase="planning")
                if isinstance(node, (ListSlice, ListIteration)):
                    self._pulse_expression_type(node, owner=node.describe())
                    continue
                if isinstance(node, Subscript) and marker not in self.subscript_types:
                    subject_type = self._pulse_expression_type(
                        node.subject, owner=node.describe()
                    )
                    index_type = self._pulse_expression_type(
                        node.index, owner=node.describe()
                    )
                    self._require_resolvable_runtime_type(
                        node.subject, subject_type, owner=node.describe()
                    )
                    self._require_resolvable_runtime_type(
                        node.index, index_type, owner=node.describe()
                    )
                    subscript_argument_types(node, subject_type, index_type)
                    self.subscript_types[marker] = (
                        node,
                        (subject_type, index_type),
                    )
                    continue
                if (
                    isinstance(node, CaseExpression)
                    and marker not in self.case_result_types
                ):
                    compared = (
                        tuple(
                            alternative.condition for alternative in node.alternatives
                        )
                        if node.operand is None
                        else (
                            node.operand,
                            *(
                                alternative.condition
                                for alternative in node.alternatives
                            ),
                        )
                    )
                    comparison_types = tuple(
                        self._pulse_expression_type(item, owner=node.describe())
                        for item in compared
                    )
                    for item, value_type in zip(
                        compared, comparison_types, strict=True
                    ):
                        self._require_resolvable_runtime_type(
                            item, value_type, owner=node.describe()
                        )
                    case_comparison_type(node, comparison_types)
                    results = node.result_expressions()
                    result_types = tuple(
                        self._pulse_expression_type(item, owner=node.describe())
                        for item in results
                    )
                    for item, value_type in zip(results, result_types, strict=True):
                        self._require_resolvable_runtime_type(
                            item, value_type, owner=node.describe()
                        )
                    case_result_type(node, result_types)
                    self.case_comparison_types[marker] = (node, comparison_types)
                    self.case_result_types[marker] = (node, result_types)

    def _require_resolvable_runtime_type(
        self, expression: Expression, value_type: ValueType | None, *, owner: str
    ) -> None:
        """Retain alias proofs; row-dependent types are checked by the runtime operator."""
        resolved = self._union_type_expression(expression)
        if isinstance(resolved, Variable) and resolved.name in self.binding_types:
            return
        if value_type is not None or any(
            isinstance(node, Parameter) for node in walk(resolved)
        ):
            if value_type is None and resolved is not expression:
                self.type_alias_sources[id(expression)] = (expression, resolved)
            return
        # Heterogeneous lists, CASE arms and tabular outputs need not have one scalar type.
        # Static bad types were rejected by their expression typer; unknown is not invalid.

    def _pulse_expression_type(
        self, expression: Expression, *, owner: str
    ) -> ValueType | None:
        """Return and remember the provable type of a CASE/subscript subexpression."""
        marker = id(expression)
        if marker in self.pulse_expression_types:
            return self.pulse_expression_types[marker][1]
        value_type = self._infer_pulse_expression_type(expression, owner=owner)
        self.pulse_expression_types[marker] = (expression, value_type)
        return value_type

    def _infer_pulse_expression_type(
        self, expression: Expression, *, owner: str
    ) -> ValueType | None:
        """Return the provable type of one expression used by CASE or a list subscript."""
        if isinstance(expression, Parameter):
            return None
        if isinstance(expression, Literal):
            return value_type_of(expression.value)
        if isinstance(expression, Variable):
            if expression.name in self.binding_types:
                return self.binding_types[expression.name]
            definition = self.alias_definitions.get(expression.name)
            if definition is not None:
                # An alias is exactly as knowable as what it was projected from.
                return self._pulse_expression_type(definition, owner=owner)
            if expression.name in self.unwind_sources:
                return self._unwind_static_element_type(expression.name, owner=owner)
        if isinstance(expression, Property):
            target = self._static_postfix_target(expression)
            if target is not expression:
                return self._pulse_expression_type(target, owner=owner)
            if isinstance(expression.subject, Variable):
                if expression.subject.name in self.list_locals:
                    return None
                if expression.subject.name in self.unwind_sources:
                    return self._unwind_static_postfix_type(expression, owner=owner)
                if expression.subject.name in self.polymorphic_variables:
                    return self._polymorphic_property_type(expression.key, owner, expression.subject.name)
                table = self.tables.get(expression.subject.name)
                if table is None:
                    message = f"{owner} reads {expression.describe()}, whose variable has no table."
                    raise GrafxPlanError(
                        message,
                        field="property",
                        value=expression.key,
                    )
                column = self._column_of(table, expression.key)
                if column is None:
                    message = (
                        f"Table {table.name!r} has no column named {expression.key!r}."
                    )
                    raise GrafxPlanError(
                        message,
                        field="column",
                        value=expression.key,
                    )
                return column.type
            if (
                isinstance(expression.subject, Literal)
                and expression.subject.value is None
            ):
                return ValueType.NULL
            if isinstance(expression.subject, MapExpression):
                entry = expression.subject.entry(expression.key)
                if entry is None:
                    return ValueType.NULL
                return self._pulse_expression_type(entry, owner=owner)
            subject_type = self._pulse_expression_type(expression.subject, owner=owner)
            if subject_type not in (None, ValueType.NULL, ValueType.MAP):
                raise GrafxPlanError("Property access requires a map, graph entity or NULL.",
                                     field="property", value=expression.key,
                                     reason="property_subject_type", query_phase="planning")
            # Calls, CASE and extracts can produce maps without a statically known
            # field layout. Unknown field type is not invalid syntax. Do not execute
            # these expressions during planning to discover their values.
            return ValueType.NULL if subject_type is ValueType.NULL else None
        if isinstance(expression, NullCheck):
            return ValueType.BOOL
        if isinstance(expression, UnaryOperation):
            if expression.operator == "NOT":
                boolean_argument_types("NOT", self._pulse_expression_type(expression.operand, owner=owner))
                return ValueType.BOOL
            return self._pulse_expression_type(expression.operand, owner=owner)
        if isinstance(expression, BinaryOperation):
            if expression.operator == "IN":
                membership_argument_type(self._pulse_expression_type(expression.right, owner=owner))
                return ValueType.BOOL
            if expression.operator in ("AND", "OR", "XOR"):
                boolean_argument_types(expression.operator,
                                       self._pulse_expression_type(expression.left, owner=owner),
                                       self._pulse_expression_type(expression.right, owner=owner))
                return ValueType.BOOL
            if expression.operator in (
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
            left = self._pulse_expression_type(expression.left, owner=owner)
            right = self._pulse_expression_type(expression.right, owner=owner)
            if left is None or right is None:
                return None
            concrete = tuple(
                value_type
                for value_type in (left, right)
                if value_type is not None and value_type is not ValueType.NULL
            )
            if not concrete:
                return ValueType.NULL
            if expression.operator == "+" and ValueType.LIST in concrete:
                return ValueType.LIST
            if expression.operator == "+" and all(
                value_type is ValueType.STRING for value_type in concrete
            ):
                return ValueType.STRING
            if all(
                value_type in (ValueType.INT64, ValueType.DOUBLE)
                for value_type in concrete
            ):
                if expression.operator == "^" or ValueType.DOUBLE in concrete:
                    return ValueType.DOUBLE
                return ValueType.INT64
            message = f"The scalar type of {expression.describe()} in {owner} is incompatible."
            raise GrafxPlanError(
                message,
                field="expression",
                value=expression.describe(),
            )
        if isinstance(expression, FunctionCall):
            name = expression.name.upper()
            if name in ("LENGTH", "NODES", "RELATIONSHIPS"):
                argument = expression.arguments[0]
                if not isinstance(argument, Parameter):
                    binding = self.analysis.binding(argument.name) if isinstance(argument, Variable) else None
                    if not (isinstance(argument, Literal) and argument.value is None) and not (binding is not None and binding.entity == "path"):
                        raise GrafxPlanError("Path functions require a bound path or NULL.", field="function", value=name)
                return ValueType.INT64 if name == "LENGTH" else ValueType.LIST
            if name in NATIVE_SCALARS:
                return scalar_type(name, *(self._pulse_expression_type(argument, owner=owner)
                                           for argument in expression.arguments))
            if name == "UDF" and expression.arguments and isinstance(expression.arguments[0], Literal):
                result = self.scalar_types.get(expression.arguments[0].value)
                if result is not None:
                    return result
            if name == COALESCE_FUNCTION:
                types = tuple(
                    self._pulse_expression_type(argument, owner=owner)
                    for argument in expression.arguments
                )
                return coalesce_result_type(expression.name, types)
            if name in (STRING_SPLIT_FUNCTION, "SPLIT"):
                return ValueType.LIST
            if name == LABEL_FUNCTION:
                return ValueType.STRING
            if name == TIMESTAMP_FUNCTION:
                return ValueType.TIMESTAMP
            if name == SIZE_FUNCTION or name == "COUNT":
                return ValueType.INT64
            if name in ("SUM", "AVG"):
                argument_type = self._pulse_expression_type(expression.arguments[0], owner=owner)
                if argument_type not in (None, ValueType.NULL, ValueType.INT64, ValueType.DOUBLE):
                    raise GrafxPlanError("Numeric aggregates require numeric values or NULL.",
                                         field="function", value=name)
                return (ValueType.DOUBLE if name == "AVG" else
                        ValueType.INT64 if argument_type is ValueType.NULL else argument_type)
            if name in (SIMILARITY_FUNCTION, SIMILARITY_SCORE_FUNCTION):
                return ValueType.DOUBLE
            if name in ("MIN", "MAX"):
                return self._pulse_expression_type(expression.arguments[0], owner=owner)
            if name == "COLLECT":
                return ValueType.LIST
            if name in AGGREGATE_FUNCTIONS:
                message = f"The aggregate {expression.name!r} has no scalar CASE type."
                raise GrafxPlanError(
                    message,
                    field="function",
                    value=expression.name,
                )
        if isinstance(expression, ListIteration):
            source_type = self._pulse_expression_type(expression.source, owner=owner)
            if source_type not in (None, ValueType.NULL, ValueType.LIST):
                raise GrafxPlanError("List iteration requires a list.", field="iteration")
            predicate = expression.body if expression.mode in ("all", "any", "none", "single") else expression.predicate
            if predicate is not None and self._pulse_expression_type(predicate, owner=owner) not in (None, ValueType.NULL, ValueType.BOOL):
                raise GrafxPlanError("List predicates require booleans.", field="iteration")
            if expression.mode == "map":
                return ValueType.LIST
            return None if expression.mode == "reduce" else ValueType.BOOL
        if isinstance(expression, ListSlice):
            subject_type = self._pulse_expression_type(expression.subject, owner=owner)
            if subject_type not in (None, ValueType.NULL, ValueType.LIST):
                raise GrafxPlanError("A list slice requires a list.", field="slice")
            for bound in (expression.start, expression.end):
                if bound is not None and self._pulse_expression_type(bound, owner=owner) not in (None, ValueType.NULL, ValueType.INT64):
                    raise GrafxPlanError("List slice bounds require integers.", field="slice")
            return ValueType.LIST
        if isinstance(expression, ListExpression):
            return ValueType.LIST
        if isinstance(expression, MapExpression):
            return ValueType.MAP
        if isinstance(expression, Subscript):
            if self._unwind_postfix_rooted(expression):
                return self._unwind_static_postfix_type(expression, owner=owner)
            target = self._static_postfix_target(expression)
            if target is not expression:
                return self._pulse_expression_type(target, owner=owner)
            # A subscript of an alias is a subscript of what the alias was
            # projected from, which is where the element type is written.
            subject = self._alias_definition(expression.subject)
            if isinstance(subject, FunctionCall) and (
                subject.name.upper() in (STRING_SPLIT_FUNCTION, "SPLIT")
            ):
                return ValueType.STRING
            if isinstance(subject, ListExpression):
                element_types = tuple(
                    self._pulse_expression_type(element, owner=owner)
                    for element in subject.elements
                )
                concrete = tuple(
                    value_type
                    for value_type in element_types
                    if value_type is not None and value_type is not ValueType.NULL
                )
                if not concrete:
                    return None
                if all(value_type is concrete[0] for value_type in concrete):
                    return concrete[0]
                return None
            return None
        if isinstance(expression, CaseExpression):
            planned = self.case_result_types.get(id(expression))
            if planned is not None:
                return case_result_type(expression, planned[1])
            # UNION typing may carry a non-executable clone whose WITH aliases have been
            # expanded.  Its executable CASE was already validated when the branch plan was
            # built, but metadata is identity-keyed to that original node.  Infer the clone's
            # result arms directly so the separate pre-stream proof does not depend on an id
            # that deliberately changed.
            result_types = tuple(
                self._pulse_expression_type(result, owner=owner)
                for result in expression.result_expressions()
            )
            return case_result_type(expression, result_types)
        message = (
            f"{owner} needs a scalar expression whose type is known; got "
            f"{expression.describe()}."
        )
        raise GrafxPlanError(
            message,
            field="expression",
            value=expression.describe(),
        )

    def _unwind_static_element_type(self, alias: str, *, owner: str) -> ValueType | None:
        """Return the common type of every written UNWIND element, when knowable."""
        source = self._alias_definition(self.unwind_sources[alias])
        if isinstance(source, FunctionCall) and (
            source.name.upper() in (STRING_SPLIT_FUNCTION, "SPLIT")
        ):
            return ValueType.STRING
        if not isinstance(source, ListExpression) or not source.elements:
            return None
        element_types = tuple(
            self._pulse_expression_type(element, owner=owner)
            for element in source.elements
        )
        if any(value_type is None for value_type in element_types):
            return None
        common = element_types[0]
        assert common is not None  # narrowed by the guard above
        for value_type in element_types[1:]:
            assert value_type is not None  # narrowed by the guard above
            if common is not value_type:
                return None
        return common

    def _unwind_static_postfix_type(
        self,
        expression: Expression,
        *,
        owner: str,
    ) -> ValueType | None:
        """Type one postfix selection over every written element of an UNWIND list."""
        root = expression
        while isinstance(root, (Property, Subscript)):
            root = root.subject
        if not isinstance(root, Variable) or root.name not in self.unwind_sources:
            return None
        source = self._alias_definition(self.unwind_sources[root.name])
        if not isinstance(source, ListExpression) or not source.elements:
            return None
        selected_types = tuple(
            self._pulse_expression_type(
                self._replace_unwind_alias(expression, element, root.name),
                owner=owner,
            )
            for element in source.elements
        )
        if any(value_type is None for value_type in selected_types):
            return None
        common = selected_types[0]
        assert common is not None  # narrowed by the guard above
        for value_type in selected_types[1:]:
            assert value_type is not None  # narrowed by the guard above
            if common is not value_type:
                return None
        return common

    def _replace_unwind_alias(
        self,
        expression: Expression,
        replacement: Expression,
        alias: str,
    ) -> Expression:
        """Replace the root UNWIND alias in one typing-only postfix expression."""
        if isinstance(expression, Variable):
            return replacement if expression.name == alias else expression
        if isinstance(expression, Property):
            subject = self._replace_unwind_alias(expression.subject, replacement, alias)
            return (
                expression
                if subject is expression.subject
                else replace(expression, subject=subject)
            )
        if isinstance(expression, Subscript):
            subject = self._replace_unwind_alias(expression.subject, replacement, alias)
            return (
                expression
                if subject is expression.subject
                else replace(expression, subject=subject)
            )
        return expression

    def _unwind_postfix_rooted(self, expression: Expression) -> bool:
        """Whether one postfix chain starts at this query's UNWIND alias."""
        if isinstance(expression, Variable):
            return expression.name in self.unwind_sources
        if isinstance(expression, (Property, Subscript)):
            return self._unwind_postfix_rooted(expression.subject)
        return False

    def _alias_definition(self, expression: Expression) -> Expression:
        """Resolve a WITH alias to the expression it was projected from, or leave it alone."""
        if not isinstance(expression, Variable):
            return expression
        definition = self.alias_definitions.get(expression.name)
        return expression if definition is None else definition

    def _static_postfix_target(self, expression: Expression) -> Expression:
        """Resolve map-dot and literal-list postfixes when their target is written in the AST."""
        if isinstance(expression, Property):
            subject = self._static_postfix_target(expression.subject)
            if isinstance(subject, MapExpression):
                entry = subject.entry(expression.key)
                return entry if entry is not None else Literal(value=None)
            return expression
        if not isinstance(expression, Subscript):
            return expression
        subject = self._static_postfix_target(expression.subject)
        if isinstance(subject, MapExpression) and isinstance(expression.index, Literal):
            key = expression.index.value
            if isinstance(key, str):
                entry = subject.entry(key)
                return entry if entry is not None else Literal(value=None)
        if not isinstance(subject, ListExpression):
            return expression
        if not isinstance(expression.index, Literal):
            return expression
        index = expression.index.value
        if isinstance(index, bool) or not isinstance(index, int):
            return expression
        offset = index
        if not -len(subject.elements) <= offset < len(subject.elements):
            return Literal(value=None)
        return subject.elements[offset]

    def _coalesce_argument_type(
        self, call: FunctionCall, argument: Expression
    ) -> ValueType | None:
        """Use the shared expression typer, including list/map values and lexical aliases."""
        return self._pulse_expression_type(argument, owner=call.name)

    @staticmethod
    def _pattern_expressions(pattern: PatternPath) -> tuple[Expression, ...]:
        """Return every inline property map of one pattern."""
        roots: list[Expression] = []
        roots.extend(
            node.properties for node in pattern.nodes if node.properties is not None
        )
        roots.extend(
            relationship.properties
            for relationship in pattern.relationships
            if relationship.properties is not None
        )
        return tuple(roots)

    def _query_expressions(self, statement: Query) -> tuple[Expression, ...]:
        """Return every expression root the query may evaluate."""
        roots: list[Expression] = []
        for clause in statement.ordered_clauses():
            if isinstance(clause, UnwindClause):
                roots.append(clause.expression)
            if isinstance(clause, ProcedureCall):
                roots.extend(clause.arguments)
                if clause.predicate is not None:
                    roots.append(clause.predicate)
        for clause in statement.match_clauses:
            for pattern in clause.patterns:
                roots.extend(self._pattern_expressions(pattern))
            if clause.predicate is not None:
                roots.append(clause.predicate)
        for clause in statement.with_clauses:
            roots.extend(item.expression for item in clause.items)
            roots.extend(item.expression for item in clause.sort_items)
            if clause.skip is not None:
                roots.append(clause.skip)
            if clause.limit is not None:
                roots.append(clause.limit)
            if clause.predicate is not None:
                roots.append(clause.predicate)
        for clause in statement.updating_clauses:
            if isinstance(clause, CreateClause):
                for pattern in clause.patterns:
                    roots.extend(self._pattern_expressions(pattern))
            elif isinstance(clause, MergeClause):
                roots.extend(self._pattern_expressions(clause.pattern))
            elif isinstance(clause, SetClause):
                roots.extend(item.value for item in clause.items)
        returned = statement.return_clause
        if returned is not None:
            roots.extend(item.expression for item in returned.items)
            roots.extend(item.expression for item in returned.sort_items)
            if returned.skip is not None:
                roots.append(returned.skip)
            if returned.limit is not None:
                roots.append(returned.limit)
        return tuple(roots)

    def _match_clause(
        self, pipeline: PlanNode, clause: MatchClause
    ) -> tuple[PlanNode, list[Expression]]:
        """Plan the patterns of one MATCH clause and place its residual predicate."""
        original = list(conjuncts_of(clause.predicate))
        terms = list(original)
        self.seek_rechecks.clear()
        for pattern in clause.patterns:
            pipeline, terms = self._pattern(pipeline, pattern, terms)
        # A seek is only the access path that found a candidate.  When another term remains,
        # reconstruct the original conjunction around every equality the seek consumed.  A
        # residual such as ``p.name`` is UNKNOWN inside ``p.k = 1 AND p.name`` but is a refused
        # non-boolean predicate on its own; promoting it therefore changes the query.  Terms
        # introduced by inline property maps follow the written WHERE terms, matching the
        # ordering this planner already gives the scan path.
        retained = {id(term) for term in terms}
        if terms:
            retained.update(id(term) for term in self.seek_rechecks)
        ordered = list(original)
        known = {id(term) for term in ordered}
        for term in (*terms, *self.seek_rechecks):
            if id(term) not in known:
                ordered.append(term)
                known.add(id(term))
        active = [term for term in ordered if id(term) in retained]
        self.seek_rechecks.clear()
        residual = [term for term in active if not self._reads_similarity(term)]
        deferred = [term for term in active if self._reads_similarity(term)]
        predicate = _conjoin(residual)
        if predicate is not None:
            pipeline = FilterRows(child=pipeline, predicate=predicate)
        return pipeline, deferred

    def _reads_similarity(self, expression: Expression) -> bool:
        """Return True when this term needs the score the similarity operator produces."""
        return any(
            isinstance(node, FunctionCall)
            and node.name.upper() in (SIMILARITY_FUNCTION, SIMILARITY_SCORE_FUNCTION)
            for node in walk(expression)
        )

    # --- patterns ----------------------------------------------------------------------------

    def _pattern(
        self, pipeline: PlanNode, pattern: PatternPath, terms: list[Expression]
    ) -> tuple[PlanNode, list[Expression]]:
        """Plan one connected path, taking index seeks from the predicate where it can."""
        fast = self._single_hop_fast_path(pipeline, pattern, terms)
        if fast is not None:
            return fast
        first = pattern.nodes[0]
        inferred: TableDef | None = None
        if pattern.relationships and not first.labels:
            inferred = self._typed_endpoint_source(pattern)
        pipeline, terms, source = self._match_node(
            pipeline,
            first,
            terms,
            standalone=not pattern.relationships,
            inferred_table=inferred,
        )
        for position, relationship in enumerate(pattern.relationships):
            target_pattern = pattern.nodes[position + 1]
            pipeline, source = self._traverse(
                pipeline,
                source,
                relationship,
                target_pattern,
                path_variable=(
                    pattern.variable if pattern is self.path_projection else None
                ),
            )
            if target_pattern.properties is not None:
                # The inline map on a TARGET node is the same shorthand it is on the first node,
                # and it used to be dropped here: `(x)-[:R]->(y:P {id: 2})` matched every
                # neighbour of x, and a SET or DELETE above it touched all of them (C10 round-2
                # B1). The terms become a filter above the traversal, where the target is bound.
                terms = terms + list(
                    self._property_terms(source, target_pattern.properties)
                )
        return pipeline, terms

    def _match_node(
        self,
        pipeline: PlanNode,
        pattern: NodePattern,
        terms: list[Expression],
        *,
        standalone: bool = True,
        inferred_table: TableDef | None = None,
    ) -> tuple[PlanNode, list[Expression], str]:
        """Bind one node of a pattern, as a reference, an index seek or a scan.

        ``standalone`` is false when a relationship follows this node in its path. A node at
        the end of a hop is named by the relationship's own schema, so it is resolved the way
        it always was -- reading every table for it would answer a different query, and the
        refusal it already earns for naming no label is the one to keep.
        """
        variable = pattern.variable or self._anonymous()
        if pattern.variable is not None and pattern.variable in self.polymorphic_variables:
            # Re-matching an incoming entity is a test, not a new cross product.
            # Preserve its table-qualified binding and null-extension semantics.
            pipeline = FilterRows(child=pipeline, predicate=NullCheck(operand=Variable(variable), negated=True))
            if pattern.labels:
                table = self._node_table_of(pattern)
                pipeline = FilterRows(child=pipeline, predicate=BinaryOperation(
                    operator="=", left=FunctionCall(name=LABEL_FUNCTION, arguments=(Variable(variable),)),
                    right=Literal(table.name)))
            if pattern.properties is not None:
                terms = terms + list(self._property_terms(variable, pattern.properties))
            return pipeline, terms, variable
        if pattern.variable is not None and pattern.variable in self.tables:
            if pattern.labels:
                self._require_same_table(pattern.variable, pattern.labels)
            if pattern.properties is not None:
                terms = terms + list(self._property_terms(variable, pattern.properties))
            return FilterRows(child=pipeline, predicate=NullCheck(
                operand=Variable(name=variable), negated=True)), terms, variable
        if standalone and not pattern.labels:
            return self._match_every_node(pipeline, pattern, terms, variable)
        table = (
            inferred_table
            if inferred_table is not None
            else self._node_table_of(pattern)
        )
        self.tables[variable] = table
        if pattern.properties is not None:
            terms = terms + list(self._property_terms(variable, pattern.properties))
        seek, remaining = self._index_seek(pipeline, variable, table, terms)
        if seek is not None:
            return seek, remaining, variable
        if standalone:
            keyed = self._node_multi_key_seek(pipeline, variable, table, terms)
            if keyed is not None:
                return keyed, terms, variable
        return NodeScan(child=pipeline, variable=variable, table=table), terms, variable

    def _node_multi_key_seek(
        self,
        pipeline: PlanNode,
        variable: str,
        table: TableDef,
        terms: Sequence[Expression],
    ) -> PlanNode | None:
        """Return the multi-key seek for ``n.pk IN $keys`` leading a standalone labelled node.

        NODE-IN-SEEK.  The term must be the first local term of the conjunction: seeking on a
        later conjunct would evaluate it before the terms written ahead of it and could suppress
        a refusal the scan path raises on every row the seek eliminates.  Only a parameter list
        is admitted; a literal list, a non-primary-key column, a polymorphic node, a nested
        pattern and a table without its automatic exact primary-key index keep the scan.  No
        term is consumed: the whole conjunction stays above the seek exactly as it stays above
        the scan, so both paths judge, refuse and count the predicate the same way.
        """
        if type(pipeline) is not SingleRow or not terms or table.primary_key is None:
            return None
        term = terms[0]
        if (
            not isinstance(term, BinaryOperation)
            or term.operator != "IN"
            or not isinstance(term.left, Property)
            or not isinstance(term.left.subject, Variable)
            or term.left.subject.name != variable
            or term.left.key != table.primary_key
            or not isinstance(term.right, Parameter)
        ):
            return None
        position = table.column_index(table.primary_key)
        automatic = {
            definition.name
            for definition in automatic_index_definitions(table)
            if definition.positions == (position,)
            and definition.visibility is IndexVisibility.EXACT
            and definition.key_derivation == COLUMN_KEY_DERIVATION
        }
        chosen: IndexDefinition | None = None
        for definition in self.indexes:
            if (
                definition.name in automatic
                and index_definition_matches_table(definition, table)
                and definition.visibility is IndexVisibility.EXACT
                and definition.key_derivation == COLUMN_KEY_DERIVATION
                and definition.positions == (position,)
            ):
                chosen = definition
                break
        if chosen is None:
            return None
        return NodeMultiKeySeek(
            fallback=NodeScan(child=pipeline, variable=variable, table=table),
            variable=variable,
            table=table,
            keys=term.right,
            key_position=position,
            index=chosen.name,
        )

    def _match_every_node(
        self,
        pipeline: PlanNode,
        pattern: NodePattern,
        terms: list[Expression],
        variable: str,
    ) -> tuple[PlanNode, list[Expression], str]:
        """Bind one node that names no label, which is every node of every node table.

        The variable is deliberately NOT recorded in ``tables``: it is bound to a row, but not
        to one table, and every rule that reads a variable's table has to see the difference
        rather than a missing entry it can read as "unbound".
        """

        self.polymorphic_variables.add(variable)
        if pattern.properties is not None:
            terms = terms + list(self._property_terms(variable, pattern.properties))
            # Inline keys may have no explicit Property AST in the source query.
            for entry in pattern.properties.entries:
                self._polymorphic_property_type(entry.key, pattern.describe(), variable)
        return (
            AllNodesScan(child=pipeline, variable=variable, tables=self._node_tables()),
            terms,
            variable,
        )

    def _node_tables(self) -> tuple[TableDef, ...]:
        """Return every node table of the catalog, in the order a scan reads them."""
        return tuple(table for table in self.catalog.tables() if table.kind == "node")

    def _matched_row(self, name: str) -> bool:
        """Return True when this variable names a matched row, table or no table."""
        return name in self.tables or name in self.polymorphic_variables

    def _polymorphic_property_type(self, key: str, owner: str, variable: str) -> ValueType | None:
        """Return the one type a property has across the tables that declare it.

        A polymorphic match reads one name across many tables, so the property is typed only
        when the tables that declare it agree; integers and doubles still promote, which is the
        rule everywhere else here. Two tables that declare the same name with families that
        cannot both be right make the read wrong for a reason no row settles -- the answer would
        depend on which table the scan reached first -- so it is refused before the first row.

        A name NO table declares is not an error: the scan spans tables that were never obliged
        to carry it, and every row reads null.
        """

        declared = [
            (table.name, column.type)
            for table in self.polymorphic_tables.get(variable, self._node_tables())
            for column in (self._column_of(table, key),)
            if column is not None
        ]
        if not declared:
            return None
        types = {value_type for _, value_type in declared}
        if len(types) == 1:
            return next(iter(types))
        if types <= {ValueType.INT64, ValueType.DOUBLE}:
            return ValueType.DOUBLE
        listing = ", ".join(
            f"{name}.{key} is {value_type.name}" for name, value_type in declared
        )
        raise GrafxPlanError(
            f"{owner} reads {key!r} on a polymorphic binding, and the tables do not agree "
            f"on what it is: {listing}.",
            field="property",
            value=key,
        )

    def _record_polymorphic_properties(self, statement: Query) -> None:
        """Check every property a label-free node reads, before any row is produced."""
        for expression in self._query_expressions(statement):
            for node in walk(expression):
                if not isinstance(node, Property):
                    continue
                subject = node.subject
                if not isinstance(subject, Variable):
                    continue
                if subject.name not in self.polymorphic_variables:
                    continue
                self._polymorphic_property_type(node.key, node.describe(), subject.name)

    def _typed_endpoint_source(self, pattern: PatternPath) -> TableDef | None:
        """Return the table a label-free source reads, when the query is the one shape for it.

        The FAR end of a hop already takes its table from the relationship: ``(a:X)-[r:T]->(b)``
        binds b to T's TO table without b naming a label, because a relationship declares what
        sits at each of its ends. This is that same rule applied to the NEAR end, and
        deliberately nothing more -- it answers only for the exact frozen form, so no other
        pattern in the language starts resolving a name from a schema it did not resolve it
        from before.
        """
        if not self.typed_endpoint_form:
            return None
        relationship = pattern.relationships[0]
        # A name that is not a relationship is refused HERE, with the message the hop itself
        # would have given. Returning None instead would have let the source be refused first,
        # for naming no label -- an answer about the wrong half of the pattern, and one this
        # subset never gave before.
        table = self._relationship_table(relationship)
        return self._table_named(str(table.from_table), "from_table")

    def _relationship_table(self, relationship: RelationshipPattern) -> TableDef:
        """Return the table one relationship pattern reads, refusing a name that is not one."""
        table = self._table_named(relationship.types[0], "type")
        if table.kind != "rel":
            raise GrafxPlanError(
                f"The type {table.name!r} names a {table.kind} table, so it cannot match a "
                "relationship.",
                field="type",
                value=table.name,
            )
        return table

    @staticmethod
    def _is_typed_endpoint_form(statement: Query) -> bool:
        """Whether this is the one read-only shape that reads its endpoints from the type.

        Exact and closed on purpose. One MATCH of one pattern; one named outgoing hop of one
        type and no range; both ends named, and neither carrying a label nor an inline map; an
        optional WHERE; a RETURN. Anything else -- a clause that writes, a WITH, an UNWIND, an
        incoming or undirected hop, an anonymous end -- keeps the refusal it has today, for the
        reason it has it. The question is asked of the STATEMENT, so an analysis handed in by a
        caller cannot widen the answer.
        """
        if statement.unwind_clause is not None or statement.with_clauses:
            return False
        if statement.updating_clauses or statement.return_clause is None:
            return False
        if len(statement.match_clauses) != 1:
            return False
        patterns = statement.match_clauses[0].patterns
        if len(patterns) != 1:
            return False
        pattern = patterns[0]
        if pattern.variable is not None:
            # A named path is its own frozen form, with LABELLED ends. Reading the near end
            # from the relationship here would mix the two and admit a shape neither froze.
            return False
        if len(pattern.relationships) != 1 or len(pattern.nodes) != 2:
            return False
        relationship = pattern.relationships[0]
        if relationship.variable is None or len(relationship.types) != 1:
            return False
        if relationship.direction is not Direction.OUTGOING:
            return False
        if relationship.variable_length or relationship.hop_range_written:
            # `*1..1` means one hop, but it WRITES a range, and the frozen text has none.
            return False
        if relationship.properties is not None:
            return False
        return all(
            node.variable is not None and not node.labels and node.properties is None
            for node in pattern.nodes
        )

    def _node_table_of(self, pattern: NodePattern) -> TableDef:
        """Return the table a matched node reads, refusing a pattern that names none or many."""
        if len(pattern.labels) != 1:
            raise GrafxPlanError(
                "A matched node names exactly one label, because a row lives in exactly one "
                f"table; got {pattern.describe()}.",
                field="labels",
                value=pattern.describe(),
            )
        table = self._table_named(pattern.labels[0], "label")
        if table.kind != "node":
            raise GrafxPlanError(
                f"The label {table.name!r} names a {table.kind} table, so it cannot match a "
                "node.",
                field="label",
                value=table.name,
            )
        return table

    def _require_same_table(self, variable: str, labels: tuple[str, ...]) -> None:
        """Refuse a second mention of a variable under a different label."""
        bound = self.tables[variable]
        if len(labels) != 1 or labels[0] != bound.name:
            raise GrafxPlanError(
                f"The variable {variable!r} is already bound to {bound.name!r} and is mentioned "
                f"again as {list(labels)}.",
                field="variable",
                value=variable,
            )

    def _property_terms(
        self, variable: str, properties: MapExpression
    ) -> tuple[Expression, ...]:
        """Return the equality terms an inline property map is shorthand for."""
        return tuple(
            BinaryOperation(
                operator="=",
                left=Property(subject=Variable(name=variable), key=entry.key),
                right=entry.value,
            )
            for entry in properties.entries
        )

    def _index_seek(
        self,
        pipeline: PlanNode,
        variable: str,
        table: TableDef,
        terms: list[Expression],
    ) -> tuple[PlanNode | None, list[Expression]]:
        """Return an index seek for this variable when an index answers the predicate exactly."""
        constrained = self._leading_equality_constraints(variable, table, terms)
        if not constrained:
            return None, terms
        found = self._index_for(table, tuple(constrained))
        if found is None:
            return None, terms
        definition, columns = found
        used = [constrained[column][0] for column in columns]
        self.seek_rechecks.extend(used)
        remaining = [term for term in terms if term not in used]
        return (
            IndexSeek(
                child=pipeline,
                variable=variable,
                table=table,
                index=definition.name,
                visibility=definition.visibility,
                key_columns=columns,
                key_values=tuple(constrained[column][1] for column in columns),
            ),
            remaining,
        )

    def _leading_equality_constraints(
        self, variable: str, table: TableDef, terms: Sequence[Expression]
    ) -> dict[str, tuple[Expression, Expression]]:
        """Return the safe leading local equalities an index may use for this binding.

        Seeking on a later conjunct evaluates it before earlier terms and can suppress an
        observable refusal on every row the seek eliminates.  A leading run of local equality
        terms is total once its row is decoded, so using any index covered by that run preserves
        the predicate's written expression order.  The complete conjunction is still replayed
        over hits by ``_match_clause`` whenever a residual remains.
        """
        constrained: dict[str, tuple[Expression, Expression]] = {}
        for term in terms:
            binding = self._equality_on(term, variable, table)
            if binding is None and self._bound_local_equality(term, variable):
                # A prior scan has already bound this owner.  Its local equality is total and
                # remains in ``terms`` for the final filter, so it is safe to look past without
                # pretending this seek consumed it.  This preserves the nested P-scan -> Q-seek
                # shape for ``p.id = $p AND q.id = $q``.
                continue
            if binding is None:
                break
            column, value = binding
            if column not in constrained:
                constrained[column] = (term, value)
        return constrained

    def _bound_local_equality(self, term: Expression, variable: str) -> bool:
        """Whether ``term`` is a total equality on another already-bound table row."""
        for owner, owner_table in self.tables.items():
            if owner == variable:
                continue
            if self._equality_on(term, owner, owner_table) is not None:
                return True
        return False

    def _equality_on(
        self, term: Expression, variable: str, table: TableDef
    ) -> tuple[str, Expression] | None:
        """Return the column and value an equality on this variable constrains, or None."""
        if not isinstance(term, BinaryOperation) or term.operator != "=":
            return None
        for subject, value in ((term.left, term.right), (term.right, term.left)):
            if not isinstance(subject, Property) or not isinstance(
                subject.subject, Variable
            ):
                continue
            if subject.subject.name != variable:
                continue
            if not self._seekable_key(value):
                continue
            if self._column_of(table, subject.key) is None:
                raise GrafxPlanError(
                    f"Table {table.name!r} has no column named {subject.key!r}.",
                    field="column",
                    value=subject.key,
                )
            return subject.key, value
        return None

    def _seekable_key(self, value: Expression) -> bool:
        """Whether an index can be probed with this value once per driving row.

        A literal or a parameter is the same for every row, so the seek is trivially valid.
        The batch element is the one row-dependent value allowed: ``UnwindRows`` has already
        bound it by the time the seek runs, and probing the index once per element is the
        whole point of the shape -- the alternative is a scan of the table for every element.

        Nothing wider is admitted. Another matched variable would make the seek depend on a
        row this operator may not have produced yet, which is a different feature with a
        different correctness argument.
        """

        if isinstance(value, (Literal, Parameter)):
            return not free_variables(value)
        if self.unwind_alias is None:
            return False
        return (
            isinstance(value, Property)
            and isinstance(value.subject, Variable)
            and value.subject.name == self.unwind_alias
        )

    def _traverse_untyped(
        self,
        pipeline: PlanNode,
        source: str,
        relationship: RelationshipPattern,
        target_pattern: NodePattern,
    ) -> tuple[PlanNode, str]:
        """Plan the one untyped hop this engine reads, across every table it could live in.

        Reached only when the WHOLE statement is the admitted shape, which is decided once in
        :func:`~okto_grafx.domain.query.analysis.untyped_one_hop_source` and not re-derived here.
        Every other untyped relationship falls through to the refusal it has always earned.
        """
        label = self.untyped_one_hop_label
        tables = tuple(
            table
            for table in sorted(self.catalog.tables(), key=lambda item: item.table_id)
            if table.kind == "rel" and table.from_table == label
        )
        # A label with nothing leaving it is not a shape defect; it is a query whose answer is
        # no rows, and the operator below produces exactly that from an empty table list.
        # Refusing here would turn a fact about the schema into a fault in the query.
        for table in tables:
            # Every candidate's landing table is resolved BEFORE the operator is built, and by
            # the same door the typed route uses. A catalog can name an endpoint it does not
            # hold; admitting that plan would defer a schema defect until execution while the
            # typed route refuses it during planning. Refusing here keeps both routes fail-fast
            # at the same public boundary and with the same typed error.
            self._table_named(table.to_table, "to")
        named = target_pattern.variable
        target = named if named is not None else self._anonymous()
        # Deliberately NOT recorded in self.tables: the landing table differs per relationship
        # table, so there is no single answer to "which table is b". Nothing in the admitted
        # shape reads b, and a name bound to one table would be a claim this hop cannot make.
        return (
            TraverseAnyRelationship(
                child=pipeline,
                source=source,
                target=target,
                relationship=relationship.variable or "",
                tables=tables,
            ),
            target,
        )

    def _traverse(
        self,
        pipeline: PlanNode,
        source: str,
        relationship: RelationshipPattern,
        target_pattern: NodePattern,
        *,
        path_variable: str | None = None,
    ) -> tuple[PlanNode, str]:
        """Plan one relationship hop, or a bounded range of them."""
        if self.untyped_one_hop_label is not None and not relationship.types:
            return self._traverse_untyped(
                pipeline, source, relationship, target_pattern
            )
        if len(relationship.types) != 1:
            raise GrafxPlanError(
                "A matched relationship names exactly one type, because a relationship lives in "
                f"exactly one table; got {relationship.describe()}.",
                field="types",
                value=relationship.describe(),
            )
        table = self._relationship_table(relationship)
        if path_variable is not None:
            self._require_path_projection_schema(table)
        if relationship.properties is not None:
            raise GrafxPlanError(
                "A matched relationship carries no inline property map in this dialect; write "
                f"the condition in WHERE instead of {relationship.describe()}.",
                field="properties",
                value=relationship.describe(),
            )
        self._require_endpoint(source, table, relationship.direction)
        if relationship.variable is not None and relationship.variable_length:
            # `[r*1..3]` binds every hop it walked, so `r` is a tuple of bindings rather
            # than one row.  Recording that here is what lets label() refuse it while
            # planning instead of discovering the shape once a row arrives.
            self.multi_hop_variables.add(relationship.variable)
        if relationship.variable is not None:
            # A matched relationship variable names its table exactly as a written one does in
            # ``_written_pattern``. Without this the table map knew every node of the pattern
            # and none of its edges, so DELETE r refused a variable the traversal had already
            # bound at runtime -- the plan, not the execution, was what lacked the edge.
            self.tables[relationship.variable] = table
        named = target_pattern.variable
        already_bound = named is not None and named in self.tables
        target_variable = named if named is not None else self._anonymous()
        target_table = self._target_table(table, relationship.direction, target_pattern)
        if already_bound and named is not None:
            if target_pattern.labels:
                self._require_same_table(named, target_pattern.labels)
            target_table = self.tables[named]
        elif target_table is not None:
            self.tables[target_variable] = target_table
        if (
            relationship.min_hops == 1
            and relationship.max_hops == 1
            and target_table is not None
            and (target_pattern.labels or already_bound)
        ):
            # ST-1 (a): both ends of a single typed hop are declared by the relationship.
            # The source has always been validated above; a labelled or bound TARGET that
            # cannot be that table's landing used to plan fine and match nothing.
            self._require_landing(target_table, table, relationship.direction)
        return (
            TraverseRelationship(
                child=pipeline,
                source=source,
                target=target_variable,
                relationship=relationship.variable,
                table=table,
                direction=relationship.direction,
                min_hops=relationship.min_hops,
                max_hops=relationship.max_hops,
                target_table=target_table,
                target_bound=already_bound,
                path_variable=path_variable,
            ),
            target_variable,
        )

    def _require_landing(
        self, landing: TableDef, table: TableDef, direction: Direction
    ) -> None:
        """Refuse a single hop whose far end cannot be an endpoint of that relationship."""
        if direction is Direction.OUTGOING:
            allowed: tuple[str, ...] = (str(table.to_table),)
        elif direction is Direction.INCOMING:
            allowed = (str(table.from_table),)
        else:
            allowed = (str(table.from_table), str(table.to_table))
        if landing.name not in allowed:
            raise GrafxPlanError(
                f"A {table.name!r} relationship written this way lands at "
                f"{' or '.join(allowed)}, and the target is bound to {landing.name!r}, so "
                "this pattern can match nothing.",
                field="to_table",
                value=landing.name,
            )

    def _single_hop_fast_path(
        self, pipeline: PlanNode, pattern: PatternPath, terms: list[Expression]
    ) -> tuple[PlanNode, list[Expression]] | None:
        """Plan one directed typed single hop from its cheap side (ST-1), or return None.

        Two shapes, tried in order and only when today's plan would start with a scan: a
        seekable TARGET mirrors the hop so the seek drives it, and a hop whose residual
        predicate reads only the relationship becomes one RelationshipScan. Every guard
        below is an early-out to the unchanged path -- multi-hop, undirected, untyped,
        projected paths, bound ends, inline relationship maps and label-free sources all
        keep exactly the plan they had.
        """
        if len(pattern.relationships) != 1 or len(pattern.nodes) != 2:
            return None
        if pattern is self.path_projection:
            # A projected named path needs the traversal to construct its public path value.
            # A decorative name is deliberately allowed below: the query analysis has already
            # proved nobody can read it, so it must not change the unnamed pattern's plan.
            return None
        relationship = pattern.relationships[0]
        if len(relationship.types) != 1:
            return None
        if (
            relationship.min_hops != 1
            or relationship.max_hops != 1
            or relationship.hop_range_written
            or relationship.direction is Direction.UNDIRECTED
            or relationship.properties is not None
        ):
            return None
        first, target = pattern.nodes[0], pattern.nodes[1]
        if not first.labels:
            return None
        if first.variable is not None and first.variable in self.tables:
            return None
        if target.variable is not None and target.variable in self.tables:
            return None
        table = self._relationship_table(relationship)
        if relationship.direction is Direction.OUTGOING:
            near_name, far_name = str(table.from_table), str(table.to_table)
        else:
            near_name, far_name = str(table.to_table), str(table.from_table)
        first_table = self._node_table_of(first)
        target_table = (
            self._node_table_of(target)
            if target.labels
            else self._table_named(far_name, "to_table")
        )
        # ST-1 (a): both ends of a typed hop are declared by the relationship. Validate them
        # before choosing a side, so a wrong end refuses instead of matching nothing.
        if first_table.name != near_name:
            raise GrafxPlanError(
                f"A {table.name!r} relationship written this way starts at {near_name}, and "
                f"{first.variable or first.describe()!r} is bound to {first_table.name!r}, "
                "so this pattern can match nothing.",
                field="from_table",
                value=first_table.name,
            )
        mirrored_direction = (
            Direction.INCOMING
            if relationship.direction is Direction.OUTGOING
            else Direction.OUTGOING
        )
        self._require_landing(target_table, table, relationship.direction)
        if self._dry_seek(first, first_table, terms):
            return None  # today's shape already drives from the seekable source
        if target.variable is not None and self._dry_seek(target, target_table, terms):
            pipeline, terms, source = self._match_node(
                pipeline, target, terms, standalone=False, inferred_table=target_table
            )
            mirrored = replace(relationship, direction=mirrored_direction)
            pipeline, landing = self._traverse(pipeline, source, mirrored, first)
            if first.properties is not None:
                terms = terms + list(self._property_terms(landing, first.properties))
            return pipeline, terms
        return self._relationship_scan_shape(
            pipeline,
            terms,
            relationship,
            first,
            target,
            table,
            first_table,
            target_table,
        )

    def _dry_seek(
        self, node_pattern: NodePattern, table: TableDef, terms: list[Expression]
    ) -> bool:
        """Whether an index seek would answer this node's equalities, consuming nothing."""
        if node_pattern.variable is None:
            return False
        candidates = list(terms)
        if node_pattern.properties is not None:
            candidates += list(
                self._property_terms(node_pattern.variable, node_pattern.properties)
            )
        constrained = self._leading_equality_constraints(
            node_pattern.variable, table, candidates
        )
        if not constrained:
            return False
        return self._index_for(table, tuple(constrained)) is not None

    def _relationship_scan_shape(
        self,
        pipeline: PlanNode,
        terms: list[Expression],
        relationship: RelationshipPattern,
        first: NodePattern,
        target: NodePattern,
        table: TableDef,
        first_table: TableDef,
        target_table: TableDef,
    ) -> tuple[PlanNode, list[Expression]] | None:
        """Scan the relationship once when the residual predicate reads only it (ST-1 b)."""
        if first.properties is not None or target.properties is not None:
            return None
        first_variable = first.variable or self._anonymous()
        target_variable = target.variable or self._anonymous()
        r_variable = relationship.variable
        incident = self._relationship_incident_shape(
            pipeline,
            terms,
            relationship,
            first_variable,
            target_variable,
            table,
            first_table,
            target_table,
        )
        if incident is not None:
            return incident
        r_only: list[Expression] = []
        rest: list[Expression] = []
        for term in terms:
            names = {leaf.name for leaf in walk(term) if isinstance(leaf, Variable)}
            if names & {first_variable, target_variable}:
                return (
                    None  # an endpoint is part of the predicate: keep today's traversal
                )
            if r_variable is not None and names == {r_variable}:
                r_only.append(term)
            else:
                rest.append(term)
        if not r_only and (
            terms
            or relationship.variable is None
            or not self.prefer_full_relationship_scan
        ):
            # A relationship predicate earns the scan by rejecting rows before endpoint
            # resolution.  With no predicate, use it only when the result shape consumes the
            # full/large frontier; a small LIMIT keeps the indexed node-first path that can stop
            # early.  Anonymous relationships retain traversal order so adding a small LIMIT is
            # still the canonical prefix of the same query; Pulse binds `r`, and therefore takes
            # the edge-first path. Variable-free and endpoint predicates retain their existing
            # semantics and placement in this first bounded change.
            return None
        self.tables[first_variable] = first_table
        self.tables[target_variable] = target_table
        if r_variable is not None:
            self.tables[r_variable] = table
        if relationship.direction is Direction.OUTGOING:
            from_variable, to_variable = first_variable, target_variable
            from_table_def, to_table_def = first_table, target_table
        else:
            from_variable, to_variable = target_variable, first_variable
            from_table_def, to_table_def = target_table, first_table
        return (
            RelationshipScan(
                child=pipeline,
                from_variable=from_variable,
                to_variable=to_variable,
                relationship=r_variable,
                table=table,
                from_table=from_table_def,
                to_table=to_table_def,
                predicate=_conjoin(r_only),
            ),
            rest,
        )

    def _relationship_incident_shape(
        self,
        pipeline: PlanNode,
        terms: list[Expression],
        relationship: RelationshipPattern,
        first_variable: str,
        target_variable: str,
        table: TableDef,
        first_table: TableDef,
        target_table: TableDef,
    ) -> tuple[PlanNode, list[Expression]] | None:
        """Select the exact multi-key incident-edge shape, retaining its canonical fallback."""

        if (
            type(pipeline) is not SingleRow
            or not self.prefer_full_relationship_scan
            or relationship.variable is None
            or relationship.direction is Direction.UNDIRECTED
            or len(terms) != 1
            or table.from_table is None
            or table.to_table is None
        ):
            return None
        predicate = terms[0]
        keys = self._incident_key_expressions(
            predicate,
            first_variable,
            target_variable,
            first_table,
            target_table,
        )
        if keys is None:
            return None

        first_pk = first_table.primary_key
        target_pk = target_table.primary_key
        if first_pk is None or target_pk is None:
            return None
        first_index = self._index_for(first_table, (first_pk,))
        target_index = self._index_for(target_table, (target_pk,))
        if first_index is None or target_index is None:
            return None
        first_definition, first_columns = first_index
        target_definition, target_columns = target_index
        if (
            first_definition.visibility is not IndexVisibility.EXACT
            or target_definition.visibility is not IndexVisibility.EXACT
            or first_columns != (first_pk,)
            or target_columns != (target_pk,)
        ):
            return None

        relationship_indexes: dict[int, IndexDefinition] = {}
        for definition in self.indexes:
            if (
                index_definition_matches_table(definition, table)
                and definition.visibility is IndexVisibility.EXACT
                and definition.key_derivation == COLUMN_KEY_DERIVATION
                and definition.positions in {(0,), (1,)}
            ):
                relationship_indexes.setdefault(definition.positions[0], definition)
        if relationship_indexes.keys() != {0, 1}:
            return None

        self.tables[first_variable] = first_table
        self.tables[target_variable] = target_table
        self.tables[relationship.variable] = table
        if relationship.direction is Direction.OUTGOING:
            from_variable, to_variable = first_variable, target_variable
            from_table_def, to_table_def = first_table, target_table
            from_keys, to_keys = keys[first_variable], keys[target_variable]
            from_definition, to_definition = first_definition, target_definition
        else:
            from_variable, to_variable = target_variable, first_variable
            from_table_def, to_table_def = target_table, first_table
            from_keys, to_keys = keys[target_variable], keys[first_variable]
            from_definition, to_definition = target_definition, first_definition

        # Keep the cheap branch edge-first.  The incident predicate needs both endpoint
        # bindings, so RelationshipScan cannot consume it internally, but it can still avoid
        # walking the whole source-node table merely to discover a small relationship table.
        # This is also the fallback for a missing/stale acceleration capability: only the
        # physical access path changes, never the statement predicate or its snapshot.
        canonical = RelationshipScan(
            child=pipeline,
            from_variable=from_variable,
            to_variable=to_variable,
            relationship=relationship.variable,
            table=table,
            from_table=from_table_def,
            to_table=to_table_def,
            predicate=None,
        )
        fallback: PlanNode = FilterRows(child=canonical, predicate=predicate)

        return (
            RelationshipIncidentSeek(
                fallback=fallback,
                from_variable=from_variable,
                to_variable=to_variable,
                relationship=relationship.variable,
                table=table,
                from_table=from_table_def,
                to_table=to_table_def,
                from_keys=from_keys,
                to_keys=to_keys,
                from_key_position=from_table_def.column_index(
                    str(from_table_def.primary_key)
                ),
                to_key_position=to_table_def.column_index(str(to_table_def.primary_key)),
                from_index=from_definition.name,
                to_index=to_definition.name,
                relationship_from_index=relationship_indexes[0].name,
                relationship_to_index=relationship_indexes[1].name,
            ),
            [],
        )

    @staticmethod
    def _incident_key_expressions(
        predicate: Expression,
        first_variable: str,
        target_variable: str,
        first_table: TableDef,
        target_table: TableDef,
    ) -> dict[str, Expression] | None:
        """Recognise exactly ``first.pk IN keys OR target.pk IN keys`` in either arm order."""

        if not isinstance(predicate, BinaryOperation) or predicate.operator != "OR":
            return None
        expected = {
            first_variable: first_table.primary_key,
            target_variable: target_table.primary_key,
        }
        if any(value is None for value in expected.values()):
            return None
        found: dict[str, Expression] = {}
        for arm in (predicate.left, predicate.right):
            if not isinstance(arm, BinaryOperation) or arm.operator != "IN":
                return None
            subject = arm.left
            if (
                not isinstance(subject, Property)
                or not isinstance(subject.subject, Variable)
                or subject.subject.name not in expected
                or subject.key != expected[subject.subject.name]
                or not isinstance(arm.right, (Parameter, ListExpression))
                or free_variables(arm.right)
            ):
                return None
            name = subject.subject.name
            if name in found:
                return None
            found[name] = arm.right
        return found if found.keys() == expected.keys() else None

    def _require_path_projection_schema(self, table: TableDef) -> None:
        """Require the relationship declaration to match both written path endpoints.

        The ordinary traversal checks the table at its starting end. Its labelled target is
        otherwise resolved from the label the query wrote, without proving that label is the
        relationship's declared ``to_table``. That is insufficient for a path value: publishing
        a target label while the edge actually lands in another table would encode a path the
        query did not match. Close both ends before any row streams.
        """
        pattern = self.path_projection
        assert pattern is not None  # Only the exact typed shape can publish a path value.
        source_label, target_label = (node.labels[0] for node in pattern.nodes)
        if not (
            table.name == pattern.relationships[0].types[0]
            and table.from_table == source_label
            and table.to_table == target_label
        ):
            raise GrafxPlanError(
                "The projected path endpoint labels do not match the relationship's "
                "catalog declaration.",
                field="endpoint",
                value=table.name,
                from_table=table.from_table,
                to_table=table.to_table,
            )

        for label in (source_label, target_label):
            node_table = self._table_named(label, "label")
            self._require_path_property_keys(node_table, _PATH_PROJECTION_NODE_KEYS)
        self._require_path_property_keys(table, _PATH_PROJECTION_RELATIONSHIP_KEYS)

    @staticmethod
    def _require_path_property_keys(
        table: TableDef, reserved_keys: frozenset[str]
    ) -> None:
        """Refuse properties that would replace structural keys in the public path value."""
        for column in table.property_columns:
            if column.name in reserved_keys:
                raise GrafxPlanError(
                    f"Table {table.name!r} declares path-reserved property {column.name!r}; "
                    "the projected path reserves that key for structural metadata.",
                    field="column",
                    value=column.name,
                    table=table.name,
                )

    def _require_endpoint(
        self, source: str, table: TableDef, direction: Direction
    ) -> None:
        """Refuse a hop whose start cannot be an endpoint of that relationship table."""
        bound = self.tables.get(source)
        if bound is None:
            return
        allowed: tuple[str, ...]
        if direction is Direction.OUTGOING:
            allowed = (str(table.from_table),)
        elif direction is Direction.INCOMING:
            allowed = (str(table.to_table),)
        else:
            allowed = (str(table.from_table), str(table.to_table))
        if bound.name not in allowed:
            raise GrafxPlanError(
                f"A {table.name!r} relationship written this way starts at "
                f"{' or '.join(allowed)}, and {source!r} is bound to {bound.name!r}, so this "
                "pattern can match nothing.",
                field="from_table",
                value=bound.name,
            )

    def _target_table(
        self, table: TableDef, direction: Direction, target: NodePattern
    ) -> TableDef | None:
        """Return the table the far end of a hop lands in, when the pattern fixes one."""
        if target.labels:
            if len(target.labels) != 1:
                raise GrafxPlanError(
                    f"A matched node names exactly one label; got {target.describe()}.",
                    field="labels",
                    value=target.describe(),
                )
            return self._table_named(target.labels[0], "label")
        if direction is Direction.OUTGOING:
            return self._table_named(str(table.to_table), "to")
        if direction is Direction.INCOMING:
            return self._table_named(str(table.from_table), "from")
        if table.from_table == table.to_table:
            return self._table_named(str(table.to_table), "to")
        return None

    def _anonymous(self) -> str:
        """Return a fresh binding name for a pattern element the query did not name."""
        self.anonymous += 1
        return f"{ANONYMOUS_VARIABLE_PREFIX}{self.anonymous}"

    # --- similarity --------------------------------------------------------------------------

    def _similarity(
        self, pipeline: PlanNode, statement: Query, terms: Sequence[Expression]
    ) -> PlanNode:
        """Place the similarity operator above the whole candidate pipeline, if there is one."""
        use = self.analysis.similarity
        if use is None:
            if terms:
                raise GrafxPlanError(
                    "A predicate reads a similarity score, and this query performs no "
                    "similarity search.",
                    field="predicate",
                    value=terms[0].describe(),
                )
            return pipeline
        column_space = self._similarity_space(use)
        threshold_operator, threshold, residual = self._threshold(terms, use.call)
        bound = self._fusible_k(statement, residual)
        search = VectorSearch(
            child=pipeline,
            variable=use.variable,
            space=use.space,
            query_vector=use.query_vector,
            property_key=use.property_key,
            score_column=SCORE_COLUMN,
            column_space=column_space,
            k=bound,
            threshold=threshold,
            threshold_operator=threshold_operator,
        )
        predicate = _conjoin(residual)
        if predicate is None:
            return search
        return FilterRows(child=search, predicate=predicate)

    def _similarity_space(self, use: SimilarityUse) -> str:
        """Return the space the searched column belongs to, refusing a cross-space search.

        SPEC-VEC BR-1 makes a comparison between two embedding spaces a typed refusal rather
        than a meaningless ranking, and the earliest place that refusal can happen is here: a
        query that names a space the column does not belong to is wrong before a single vector
        is read. A space named by a PARAMETER cannot be judged now, so the column's own space
        travels into the plan and the executor makes the same comparison once the value is
        bound -- one rule, checked wherever the name becomes known.
        """
        column_space = self._require_vector_column(use.variable, use.property_key)
        named = use.space
        if not isinstance(named, Literal):
            return column_space
        if not isinstance(named.value, str):
            raise GrafxPlanError(
                f"An embedding space is named by text; got {named.describe()}.",
                field="space",
                value=named.describe(),
            )
        self._space_named(named.value)
        if named.value != column_space:
            raise GrafxEmbeddingSpaceMismatch(
                f"The column {use.variable}.{use.property_key} stores vectors of the embedding "
                f"space {column_space!r}, and this query searches {named.value!r}; comparing "
                "vectors across spaces is refused rather than ranked.",
                field="space",
                value=named.value,
                column_space=column_space,
            )
        return column_space

    def _require_vector_column(self, variable: str, key: str) -> str:
        """Return the space a searched column belongs to, refusing one that stores no vector."""
        table = self.tables.get(variable)
        if table is None:
            raise GrafxPlanError(
                f"The variable {variable!r} used by the similarity search is bound to no table.",
                field="variable",
                value=variable,
            )
        column = self._column_of(table, key)
        if column is None:
            raise GrafxPlanError(
                f"Table {table.name!r} has no column named {key!r}.",
                field="column",
                value=key,
            )
        if not column.is_vector or column.vector_space is None:
            raise GrafxPlanError(
                f"The column {table.name}.{key} stores {column.type.name} and not an embedding, "
                "so a similarity search over it has no meaning.",
                field="column",
                value=key,
            )
        return column.vector_space

    def _threshold(
        self, terms: Sequence[Expression], call: FunctionCall
    ) -> tuple[str | None, Expression | None, list[Expression]]:
        """Take one score floor out of the deferred terms, leaving the rest as a filter."""
        remaining = list(terms)
        for term in terms:
            if not isinstance(term, BinaryOperation):
                continue
            if term.operator not in THRESHOLD_OPERATORS:
                continue
            if term.left != call or self._reads_similarity(term.right):
                continue
            if free_variables(term.right):
                continue
            remaining.remove(term)
            return term.operator, term.right, remaining
        return None, None, remaining

    def _fusible_k(
        self, statement: Query, residual: Sequence[Expression]
    ) -> Expression | None:
        """Return the neighbour count a LIMIT may become, or None when fusing would drop rows.

        Every condition here is a way a row could be discarded after the vector operator ran. A
        residual filter, DISTINCT and an aggregate each drop rows; a sort by anything other than
        the score means the k best by score are not the k the query wants; a SKIP is fusible only
        by adding it to the limit, because the rows it drops still have to be produced.

        An updating clause is deliberately never fused.  Its following RETURN window controls
        only the rows delivered to the caller; every matched row must still reach DELETE or SET.
        """
        clause = statement.return_clause
        if (
            clause is None
            or statement.updating_clauses
            or statement.with_clauses
            or residual
            or clause.distinct
            or self.analysis.aggregated
        ):
            return None
        if clause.limit is None or len(clause.sort_items) != 1:
            return None
        key = clause.sort_items[0]
        if not key.descending or not self._sorts_by_score(clause, key):
            return None
        if clause.skip is None:
            return clause.limit
        return BinaryOperation(operator="+", left=clause.limit, right=clause.skip)

    def _sorts_by_score(self, clause: ReturnClause, key: SortItem) -> bool:
        """Return True when this ORDER BY key is the similarity score and nothing else."""
        expression = key.expression
        if isinstance(expression, Variable):
            for item in clause.items:
                if item.alias == expression.name:
                    expression = item.expression
                    break
        if not isinstance(expression, FunctionCall):
            return False
        return expression.name.upper() in (
            SIMILARITY_FUNCTION,
            SIMILARITY_SCORE_FUNCTION,
        )

    # --- writes ------------------------------------------------------------------------------

    def _updating_clause(self, pipeline: PlanNode, clause: UpdatingClause) -> PlanNode:
        """Plan one clause that writes."""
        if isinstance(clause, CreateClause):
            for pattern in clause.patterns:
                pipeline = self._create(pipeline, pattern)
            return pipeline
        if isinstance(clause, MergeClause):
            return self._merge(pipeline, clause.pattern)
        if isinstance(clause, SetClause):
            return SetProperties(
                child=pipeline,
                assignments=tuple(
                    PropertyAssignment(target=item.target, value=item.value)
                    for item in clause.items
                ),
            )
        if isinstance(clause, DeleteClause):
            for target in clause.targets:
                if target.name not in self.tables:
                    raise GrafxPlanError(
                        f"The variable {target.name!r} is bound to no table, so DELETE has "
                        "nothing to remove.",
                        field="variable",
                        value=target.name,
                    )
            return DeleteEntities(
                child=pipeline,
                variables=tuple(target.name for target in clause.targets),
                detach=clause.detach,
            )
        raise GrafxPlanError(
            f"A clause of type {type(clause).__name__} cannot be planned.",
            field="clause",
            value=type(clause).__name__,
        )

    def _create(self, pipeline: PlanNode, pattern: PatternPath) -> PlanNode:
        """Plan a CREATE pattern into the nodes and relationships it inserts."""
        nodes, relationships = self._written_pattern(pattern, "CREATE")
        return CreateRelationships(
            child=pipeline, nodes=nodes, relationships=relationships
        )

    def _merge(self, pipeline: PlanNode, pattern: PatternPath) -> PlanNode:
        """Plan a MERGE pattern, which this dialect supports in two shapes."""
        if len(pattern.relationships) > 1:
            raise GrafxPlanError(
                "MERGE covers one node or one relationship between two bound nodes in this "
                f"dialect; got {pattern.describe()}.",
                field="pattern",
                value=pattern.describe(),
            )
        if pattern.relationships:
            for endpoint in (pattern.nodes[0], pattern.nodes[1]):
                if endpoint.variable is None or endpoint.variable not in self.tables:
                    raise GrafxPlanError(
                        "MERGE of a relationship needs both of its nodes already matched; "
                        f"{endpoint.describe()} is not.",
                        field="pattern",
                        value=pattern.describe(),
                    )
        nodes, relationships = self._written_pattern(pattern, "MERGE")
        return MergePattern(child=pipeline, nodes=nodes, relationships=relationships)

    def _written_pattern(
        self, pattern: PatternPath, keyword: str
    ) -> tuple[tuple[CreatedNode, ...], tuple[CreatedRelationship, ...]]:
        """Resolve one written pattern into the nodes and relationships it names."""
        nodes: list[CreatedNode] = []
        names: list[str] = []
        for node in pattern.nodes:
            variable = node.variable or self._anonymous()
            if node.variable is not None and node.variable in self.tables:
                if node.properties is not None:
                    raise GrafxPlanError(
                        f"{keyword} cannot give properties to {node.variable!r}, which an "
                        "earlier clause already bound; use SET instead.",
                        field="properties",
                        value=node.describe(),
                    )
                nodes.append(
                    CreatedNode(variable=variable, table=None, properties=None)
                )
                names.append(variable)
                continue
            table = self._node_table_of(node)
            self.tables[variable] = table
            nodes.append(
                CreatedNode(variable=variable, table=table, properties=node.properties)
            )
            names.append(variable)
        relationships: list[CreatedRelationship] = []
        for position, relationship in enumerate(pattern.relationships):
            table = self._table_named(relationship.types[0], "type")
            if table.kind != "rel":
                raise GrafxPlanError(
                    f"The type {table.name!r} names a {table.kind} table, so it cannot be "
                    "written as a relationship.",
                    field="type",
                    value=table.name,
                )
            left, right = names[position], names[position + 1]
            if relationship.direction is Direction.INCOMING:
                left, right = right, left
            self._require_written_endpoint(left, str(table.from_table), table)
            self._require_written_endpoint(right, str(table.to_table), table)
            if relationship.variable is not None:
                self.tables[relationship.variable] = table
            relationships.append(
                CreatedRelationship(
                    variable=relationship.variable,
                    table=table,
                    source=left,
                    target=right,
                    direction=Direction.OUTGOING,
                    properties=relationship.properties,
                )
            )
        return tuple(nodes), tuple(relationships)

    def _require_written_endpoint(
        self, variable: str, expected: str, table: TableDef
    ) -> None:
        """Refuse a written relationship whose endpoint is not the table the schema declares."""
        bound = self.tables.get(variable)
        if bound is None or bound.name == expected:
            return
        raise GrafxPlanError(
            f"A {table.name!r} relationship connects {table.from_table} to {table.to_table}, "
            f"and {variable!r} is bound to {bound.name!r}.",
            field="endpoint",
            value=bound.name,
        )

    # --- results -----------------------------------------------------------------------------

    def _result(self, pipeline: PlanNode, clause: ReturnClause) -> PlanNode:
        """Shape the rows a query returns: group, project, deduplicate, order and window."""
        if self.analysis.aggregated:
            pipeline = AggregateRows(
                child=pipeline,
                grouping=tuple(
                    clause.items[position]
                    for position in self.analysis.grouping_positions
                ),
                aggregations=self.analysis.aggregations,
            )
        ordered = self._ordered_node_merge(pipeline, clause)
        if ordered is not None:
            pipeline = ordered
        pipeline = ProjectRows(child=pipeline, items=self._projected(clause))
        if clause.distinct:
            pipeline = DistinctRows(child=pipeline)
        if clause.sort_items:
            pipeline = SortRows(
                child=pipeline,
                keys=clause.sort_items,
                retained_limit=clause.limit,
                retained_skip=clause.skip if clause.limit is not None else None,
            )
        if clause.skip is not None:
            pipeline = SkipRows(child=pipeline, count=clause.skip)
        if clause.limit is not None:
            pipeline = LimitRows(child=pipeline, count=clause.limit)
        return pipeline

    def _ordered_node_merge(
        self, pipeline: PlanNode, clause: ReturnClause
    ) -> OrderedNodeMerge | None:
        """Select the closed Pulse keyset path or retain the complete canonical pipeline."""

        if (
            self.analysis.aggregated
            or clause.distinct
            or clause.skip is not None
            or clause.limit is None
            or len(clause.sort_items) != 2
        ):
            return None
        first, second = clause.sort_items
        if not first.descending or not second.descending:
            return None
        if not isinstance(first.expression, Property) or not isinstance(
            second.expression, Property
        ):
            return None
        first_subject = first.expression.subject
        second_subject = second.expression.subject
        if (
            not isinstance(first_subject, Variable)
            or not isinstance(second_subject, Variable)
            or first_subject.name != second_subject.name
        ):
            return None

        predicate: Expression | None = None
        scan = pipeline
        if isinstance(scan, FilterRows):
            predicate = scan.predicate
            scan = scan.child
        if (
            not isinstance(scan, AllNodesScan)
            or not isinstance(scan.child, SingleRow)
            or scan.variable != first_subject.name
            or not scan.tables
        ):
            return None
        if predicate is not None and not self._ordered_filter_is_total(
            predicate, scan.variable
        ):
            return None
        if any(
            not self._ordered_scalar_is_total(item.expression, scan.variable)
            for item in clause.items
        ):
            return None

        timestamp_column = first.expression.key
        string_column = second.expression.key
        ordered_tables: list[TableDef] = []
        index_names: list[str] = []
        for table in scan.tables:
            timestamp_definition = self._column_of(table, timestamp_column)
            string_definition = self._column_of(table, string_column)
            if timestamp_definition is None or string_definition is None:
                if predicate is not None and self._ordered_filter_excludes_table(
                    predicate, scan.variable, table
                ):
                    continue
                return None
            timestamp_position = table.column_index(timestamp_column)
            string_position = table.column_index(string_column)
            if (
                timestamp_definition.type is not ValueType.TIMESTAMP
                or string_definition.type is not ValueType.STRING
                or table.primary_key != string_column
            ):
                return None
            candidates = sorted(
                (
                    definition.name
                    for definition in self.indexes
                    if index_definition_matches_table(definition, table)
                    and definition.layout is IndexLayout.ORDERED
                    and definition.visibility is IndexVisibility.EXACT
                    and definition.key_derivation == ORDERED_KEY_DERIVATION
                    and definition.positions
                    == (timestamp_position, string_position)
                ),
                key=str.casefold,
            )
            if not candidates:
                return None
            ordered_tables.append(table)
            index_names.append(candidates[0])

        if not ordered_tables:
            return None

        upper_timestamp: Expression | None = None
        upper_string: Expression | None = None
        if predicate is not None:
            bound = self._ordered_keyset_bound(
                predicate,
                variable=scan.variable,
                timestamp_column=timestamp_column,
                string_column=string_column,
            )
            if bound is not None:
                upper_timestamp, upper_string = bound

        return OrderedNodeMerge(
            fallback=pipeline,
            variable=scan.variable,
            tables=tuple(ordered_tables),
            indexes=tuple(index_names),
            timestamp_column=timestamp_column,
            string_column=string_column,
            limit=clause.limit,
            predicate=predicate,
            upper_timestamp=upper_timestamp,
            upper_string=upper_string,
        )

    @classmethod
    def _ordered_filter_excludes_table(
        cls, expression: Expression, variable: str, table: TableDef
    ) -> bool:
        """Prove that a polymorphic table can never make the WHERE predicate true.

        A missing property reads as ``NULL``.  SQL/Cypher comparisons involving that value are
        therefore unknown and a filter rejects the row.  This deliberately small proof lets an
        internal metadata table that lacks the ordered columns stay out of a polymorphic merge,
        but only when the complete predicate proves that every one of its rows is rejected.
        """

        if isinstance(expression, BinaryOperation):
            if expression.operator == "AND":
                return cls._ordered_filter_excludes_table(
                    expression.left, variable, table
                ) or cls._ordered_filter_excludes_table(
                    expression.right, variable, table
                )
            if expression.operator == "OR":
                return cls._ordered_filter_excludes_table(
                    expression.left, variable, table
                ) and cls._ordered_filter_excludes_table(
                    expression.right, variable, table
                )
            if expression.operator in ("=", "<>", "<", "<=", ">", ">="):
                return cls._ordered_operand_is_missing_property(
                    expression.left, variable, table
                ) or cls._ordered_operand_is_missing_property(
                    expression.right, variable, table
                )
            return False
        if isinstance(expression, NullCheck) and expression.negated:
            return cls._ordered_operand_is_missing_property(
                expression.operand, variable, table
            )
        return False

    @staticmethod
    def _ordered_operand_is_missing_property(
        expression: Expression, variable: str, table: TableDef
    ) -> bool:
        return (
            isinstance(expression, Property)
            and isinstance(expression.subject, Variable)
            and expression.subject.name == variable
            and all(column.name != expression.key for column in table.columns)
        )

    @staticmethod
    def _ordered_filter_is_total(expression: Expression, variable: str) -> bool:
        """Admit only the scalar predicate subset that cannot hide a late refusal."""

        if isinstance(expression, NullCheck):
            return _Planner._ordered_filter_operand(expression.operand, variable)
        if isinstance(expression, UnaryOperation):
            return expression.operator == "NOT" and _Planner._ordered_filter_is_total(
                expression.operand, variable
            )
        if not isinstance(expression, BinaryOperation):
            return False
        if expression.operator in ("AND", "OR"):
            return _Planner._ordered_filter_is_total(
                expression.left, variable
            ) and _Planner._ordered_filter_is_total(expression.right, variable)
        if expression.operator == "IN":
            return _Planner._ordered_filter_operand(
                expression.left, variable
            ) and isinstance(expression.right, ListExpression) and all(
                _Planner._ordered_scalar_is_total(element, variable)
                for element in expression.right.elements
            )
        if expression.operator not in ("=", "<>", "<", "<=", ">", ">="):
            return False
        return _Planner._ordered_filter_operand(
            expression.left, variable
        ) and _Planner._ordered_filter_operand(expression.right, variable)

    @staticmethod
    def _ordered_filter_operand(expression: Expression, variable: str) -> bool:
        """Recognize total leaves used by the Pulse page filters and keyset predicate."""

        return _Planner._ordered_scalar_is_total(expression, variable)

    @staticmethod
    def _ordered_scalar_is_total(expression: Expression, variable: str) -> bool:
        """Recognize scalar expressions that cannot hide a refusal below the ordered K."""

        if isinstance(expression, (Literal, Parameter)):
            return True
        if isinstance(expression, Property):
            return isinstance(expression.subject, Variable) and (
                expression.subject.name == variable
            )
        if not isinstance(expression, FunctionCall):
            return False
        if expression.named_arguments or expression.distinct or expression.star:
            return False
        name = expression.name.upper()
        if name == LABEL_FUNCTION:
            return len(expression.arguments) == 1 and isinstance(
                expression.arguments[0], Variable
            ) and expression.arguments[0].name == variable
        if name == TIMESTAMP_FUNCTION:
            # Literal/parameter conversions are bound before the first row. A property conversion
            # can refuse only on a later row, which an early LIMIT must never conceal.
            return len(expression.arguments) == 1 and isinstance(
                expression.arguments[0], (Literal, Parameter)
            )
        if name == COALESCE_FUNCTION:
            return bool(expression.arguments) and all(
                _Planner._ordered_scalar_is_total(argument, variable)
                for argument in expression.arguments
            )
        return False

    @staticmethod
    def _ordered_keyset_bound(
        predicate: Expression,
        *,
        variable: str,
        timestamp_column: str,
        string_column: str,
    ) -> tuple[Expression, Expression] | None:
        """Extract only ``ts < bound OR (ts = bound AND id < bound)`` from conjunctions."""

        terms: list[Expression] = []
        pending = [predicate]
        while pending:
            current = pending.pop()
            if isinstance(current, BinaryOperation) and current.operator == "AND":
                pending.extend((current.right, current.left))
            else:
                terms.append(current)

        def property_is(expression: Expression, column: str) -> bool:
            """Recognize an exact property access on the selected query variable."""
            return (
                isinstance(expression, Property)
                and isinstance(expression.subject, Variable)
                and expression.subject.name == variable
                and expression.key == column
            )

        for term in terms:
            if not isinstance(term, BinaryOperation) or term.operator != "OR":
                continue
            earlier = term.left
            tied = term.right
            if (
                not isinstance(earlier, BinaryOperation)
                or earlier.operator != "<"
                or not property_is(earlier.left, timestamp_column)
                or not isinstance(tied, BinaryOperation)
                or tied.operator != "AND"
            ):
                continue
            same_time = tied.left
            earlier_id = tied.right
            if (
                not isinstance(same_time, BinaryOperation)
                or same_time.operator != "="
                or not property_is(same_time.left, timestamp_column)
                or same_time.right != earlier.right
                or not isinstance(earlier_id, BinaryOperation)
                or earlier_id.operator != "<"
                or not property_is(earlier_id.left, string_column)
            ):
                continue
            return earlier.right, earlier_id.right
        return None

    def _projected(self, clause: ReturnClause) -> tuple[ReturnItem, ...]:
        """Return the projected items, giving every one of them the name it is read under."""
        return tuple(
            ReturnItem(expression=item.expression, alias=item.name)
            for item in clause.items
        )
