"""What a statement means, decided without a catalog (CONTRACT.md section 8.9, SPEC-VEC FR-4).

The parser answers "is this the language"; this pass answers "is this a query at all". It resolves
which pattern bound which variable, decides whether the RETURN clause aggregates and over what,
and finds the one similarity call the vector operator will be built from -- all from the tree
alone, with no table, no column and no embedding space in reach. That separation is what lets a
statement be analysed once and planned against a catalog that is still changing.

Every refusal here is a :class:`~okto_grafx.domain.errors.GrafxPlanError`, because the text WAS
the language and what failed is the meaning: an unbound variable, an aggregate where no group
exists, an ORDER BY key naming nothing that is returned. CONTRACT.md section 2 gives that class
exactly this job -- the statement parsed, and no valid plan exists for it.

The similarity rules deserve their own note, because SPEC-VEC BR-6 makes them a correctness
question rather than a convenience. A query carries AT MOST ONE ``similarity`` call. Two would
need two rankings in one pass and the caller would have no way to say which score
``similarity_score()`` meant; one refusal is better than a silent choice. The call must name its
embedding space, because VEC FR-2 puts the identity of the space in the query rather than in the
data, and inferring it from the column would be exactly the silent cross-space comparison BR-1
exists to forbid.
"""

from __future__ import annotations

from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query.ast import (
    CaseExpression,
    CreateClause,
    CreateNodeTableStatement,
    CreateRelTableStatement,
    CreateVectorSpaceStatement,
    DeleteClause,
    Direction,
    Expression,
    FunctionCall,
    Literal,
    MapExpression,
    MatchClause,
    MergeClause,
    Parameter,
    PatternPath,
    Property,
    Query,
    ReturnClause,
    SetClause,
    Statement,
    UnwindClause,
    Variable,
    walk,
)
from okto_grafx.domain.query.limits import MAX_PARAMETERS
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
    "ENTITY_NODE",
    "ENTITY_RELATIONSHIP",
    "ENTITY_UNWOUND",
    "Aggregation",
    "Binding",
    "QueryAnalysis",
    "SimilarityUse",
    "analyze",
    "contains_aggregate",
    "is_aggregate",
]

ENTITY_NODE: str = "node"
ENTITY_RELATIONSHIP: str = "relationship"
ENTITY_UNWOUND: str = "unwound value"
"""What UNWIND binds: one element of a list, which is a value and not a matched row."""


@dataclass(frozen=True, slots=True)
class Binding:
    """One variable a pattern bound, and what kind of thing it names."""

    name: str
    entity: str
    labels: tuple[str, ...]
    created: bool

    def describe(self) -> str:
        """Return the binding as a short en-US phrase."""
        labels = "".join(f":{label}" for label in self.labels)
        return f"{self.name}{labels} ({self.entity})"


@dataclass(frozen=True, slots=True)
class SimilarityUse:
    """The one similarity call of a query, taken apart into what the operator needs."""

    variable: str
    property_key: str
    space: Expression
    query_vector: Expression
    call: FunctionCall

    def describe(self) -> str:
        """Return the call as a short en-US phrase."""
        return (
            f"similarity({self.variable}.{self.property_key}, "
            f"{self.query_vector.describe()}, space => {self.space.describe()})"
        )


@dataclass(frozen=True, slots=True)
class Aggregation:
    """One aggregate a RETURN clause asks for, and the item it belongs to."""

    position: int
    call: FunctionCall

    @property
    def function(self) -> str:
        """Return the aggregate name folded to upper case."""
        return self.call.name.upper()

    def describe(self) -> str:
        """Return the aggregate as it was written."""
        return self.call.describe()


@dataclass(frozen=True, slots=True)
class QueryAnalysis:
    """Everything the planner needs that can be decided without a catalog."""

    statement: Statement
    bindings: tuple[Binding, ...] = ()
    parameters: tuple[str, ...] = ()
    output_columns: tuple[str, ...] = ()
    grouping_positions: tuple[int, ...] = ()
    aggregations: tuple[Aggregation, ...] = ()
    similarity: SimilarityUse | None = None
    scores_similarity: bool = False

    @property
    def aggregated(self) -> bool:
        """Return True when the RETURN clause groups rather than projects row by row."""
        return bool(self.aggregations)

    def binding(self, name: str) -> Binding | None:
        """Return the binding of one variable, or None when nothing bound it."""
        for candidate in self.bindings:
            if candidate.name == name:
                return candidate
        return None

    def variables(self, entity: str) -> tuple[str, ...]:
        """Return the variables bound to one kind of entity, in binding order."""
        return tuple(
            binding.name for binding in self.bindings if binding.entity == entity
        )


def is_aggregate(expression: Expression) -> bool:
    """Return True when this expression is itself a call to one of the six aggregates."""
    return (
        isinstance(expression, FunctionCall)
        and expression.name.upper() in AGGREGATE_FUNCTIONS
    )


def contains_aggregate(expression: Expression) -> bool:
    """Return True when an aggregate appears anywhere inside this expression."""
    return any(is_aggregate(node) for node in walk(expression))


def analyze(statement: Statement) -> QueryAnalysis:
    """Return what a statement means, refusing one that parses but cannot be answered."""
    if isinstance(
        statement,
        (CreateNodeTableStatement, CreateRelTableStatement, CreateVectorSpaceStatement),
    ):
        return QueryAnalysis(statement=statement, parameters=_parameters_of_schema(statement))
    if not isinstance(statement, Query):
        raise GrafxPlanError(
            f"A statement of type {type(statement).__name__} cannot be planned.",
            field="statement",
            value=type(statement).__name__,
        )
    return _Analyzer(statement).run()


def _parameters_of_schema(statement: Statement) -> tuple[str, ...]:
    """Return the parameters a schema statement references, which is none of them.

    A schema statement is deliberately parameter-free: the shape of a table is not a runtime
    value, and letting a parameter name a column would make the catalog depend on the arguments
    of the call that read it.
    """
    if isinstance(statement, CreateVectorSpaceStatement):
        for node in walk(statement.options):
            if isinstance(node, Parameter):
                raise GrafxPlanError(
                    "An embedding space is declared with constants, not with parameters; "
                    f"${node.name} cannot be part of a schema statement.",
                    field="parameter",
                    value=node.name,
                )
    return ()


class _Analyzer:
    """The single-use walker that decides what one query means."""

    __slots__ = ("_query", "_bindings", "_parameters", "_similarity", "_scores")

    def __init__(self, query: Query) -> None:
        self._query = query
        self._bindings: list[Binding] = []
        self._parameters: list[str] = []
        self._similarity: SimilarityUse | None = None
        self._scores = False

    def run(self) -> QueryAnalysis:
        """Analyse the whole query and return what the planner needs."""
        if self._query.unwind_clause is not None:
            self._unwind_clause(self._query.unwind_clause)
        for clause in self._query.match_clauses:
            self._match_clause(clause)
        for clause in self._query.updating_clauses:
            self._updating_clause(clause)
        grouping, aggregations = self._return_clause()
        return QueryAnalysis(
            statement=self._query,
            bindings=tuple(self._bindings),
            parameters=tuple(self._parameters),
            output_columns=(
                self._query.return_clause.column_names()
                if self._query.return_clause is not None
                else ()
            ),
            grouping_positions=grouping,
            aggregations=aggregations,
            similarity=self._similarity,
            scores_similarity=self._scores,
        )

    # --- refusals ----------------------------------------------------------------------------

    def _refuse(self, message: str, **details: object) -> GrafxPlanError:
        """Return the planning refusal for a meaning this engine cannot give the statement."""
        return GrafxPlanError(message, **details)

    # --- clauses -----------------------------------------------------------------------------

    def _match_clause(self, clause: MatchClause) -> None:
        """Bind the variables of a MATCH clause and check its predicate."""
        for pattern in clause.patterns:
            self._bind_pattern(pattern, created=False)
        if clause.predicate is not None:
            self._check_expression(clause.predicate, where="a WHERE predicate")
            if contains_aggregate(clause.predicate):
                raise self._refuse(
                    "An aggregate cannot appear in WHERE, because there is no group to "
                    "aggregate over until the rows are projected.",
                    field="predicate",
                    value=clause.predicate.describe(),
                )

    def _updating_clause(self, clause: object) -> None:
        """Bind and check one clause that writes."""
        if isinstance(clause, CreateClause):
            for pattern in clause.patterns:
                self._require_writable_pattern(pattern, "CREATE")
                self._bind_pattern(pattern, created=True)
            return
        if isinstance(clause, MergeClause):
            self._require_writable_pattern(clause.pattern, "MERGE")
            self._bind_pattern(clause.pattern, created=True)
            return
        if isinstance(clause, SetClause):
            for item in clause.items:
                self._require_bound_subject(item.target, "SET")
                self._check_expression(item.value, where="a SET value")
                self._refuse_aggregate(item.value, "SET")
            return
        if isinstance(clause, DeleteClause):
            for target in clause.targets:
                self._require_bound_entity(target.name, "DELETE")
            return
        raise self._refuse(
            f"A clause of type {type(clause).__name__} cannot be planned.",
            field="clause",
            value=type(clause).__name__,
        )

    def _return_clause(self) -> tuple[tuple[int, ...], tuple[Aggregation, ...]]:
        """Check the RETURN clause and return its grouping positions and its aggregates."""
        clause = self._query.return_clause
        if clause is None:
            return (), ()
        self._require_unique_output_columns(clause)
        grouping: list[int] = []
        aggregations: list[Aggregation] = []
        for position, item in enumerate(clause.items):
            self._check_expression(item.expression, where="a RETURN item")
            found = self._aggregates_of(item.expression)
            if not found:
                grouping.append(position)
                continue
            for call in found:
                aggregations.append(Aggregation(position=position, call=call))
        self._check_sort_keys(clause, bool(aggregations))
        self._check_row_window(clause.skip, "SKIP")
        self._check_row_window(clause.limit, "LIMIT")
        if self._scores and self._similarity is None:
            raise self._refuse(
                f"{SIMILARITY_SCORE_FUNCTION.lower()}() reports the score of a similarity "
                "search, and this query performs none.",
                field="function",
                value=SIMILARITY_SCORE_FUNCTION.lower(),
            )
        return tuple(grouping), tuple(aggregations)

    def _require_unique_output_columns(self, clause: ReturnClause) -> None:
        """Refuse any two projected items whose explicit or derived names collide."""
        seen: dict[str, bool] = {}
        for item in clause.items:
            name = item.name
            if name in seen:
                explicit_collision = seen[name] and item.alias is not None
                raise self._refuse(
                    f"Two projected items are both named {name!r}; a result column name "
                    "must identify exactly one item.",
                    field="alias" if explicit_collision else "column",
                    value=name,
                )
            seen[name] = item.alias is not None

    def _check_sort_keys(self, clause: ReturnClause, aggregated: bool) -> None:
        """Refuse an ORDER BY key that names nothing the clause returns.

        When the clause aggregates or removes duplicates, the rows that reach the sort are the
        projected rows and nothing else -- so a key that reads a variable the projection dropped
        cannot be evaluated at all, and Cypher engines that quietly sort by something else are
        the reason this is a refusal rather than a best effort.
        """
        aliases = {item.alias for item in clause.items if item.alias is not None}
        projected = {item.expression for item in clause.items}
        for key in clause.sort_items:
            if isinstance(key.expression, Variable) and key.expression.name in aliases:
                # An alias names a column of this clause's own result, so it is resolved before
                # the key is treated as an expression over bound variables -- otherwise every
                # ORDER BY over an alias would read as a variable no pattern bound.
                continue
            self._check_expression(key.expression, where="an ORDER BY key")
            if key.expression in projected:
                continue
            if contains_aggregate(key.expression):
                raise self._refuse(
                    "An ORDER BY key that aggregates must be projected first and sorted by its "
                    f"name; {key.expression.describe()} is not one of the returned items.",
                    field="sort_item",
                    value=key.expression.describe(),
                )
            if aggregated or clause.distinct:
                raise self._refuse(
                    f"ORDER BY {key.expression.describe()} reads something this clause does not "
                    "return, and after DISTINCT or an aggregate there is no row left to read "
                    "it from.",
                    field="sort_item",
                    value=key.expression.describe(),
                )

    def _check_row_window(self, expression: Expression | None, keyword: str) -> None:
        """Refuse a SKIP or LIMIT that is not a whole number of rows."""
        if expression is None:
            return
        if isinstance(expression, Parameter):
            self._note_parameter(expression.name)
            return
        if isinstance(expression, Literal) and isinstance(expression.value, int):
            if isinstance(expression.value, bool) or expression.value < 0:
                raise self._refuse(
                    f"{keyword} takes a count of rows that is zero or more; got "
                    f"{expression.describe()}.",
                    field=keyword.lower(),
                    value=expression.describe(),
                )
            return
        raise self._refuse(
            f"{keyword} takes a whole number of rows or a parameter; got "
            f"{expression.describe()}.",
            field=keyword.lower(),
            value=expression.describe(),
        )

    # --- patterns ----------------------------------------------------------------------------

    def _bind_pattern(self, pattern: PatternPath, *, created: bool) -> None:
        """Record every variable a pattern binds and check its inline property maps."""
        for node in pattern.nodes:
            self._bind(node.variable, ENTITY_NODE, node.labels, created=created)
            self._check_properties(node.properties)
        for relationship in pattern.relationships:
            self._bind(
                relationship.variable,
                ENTITY_RELATIONSHIP,
                relationship.types,
                created=created,
            )
            self._check_properties(relationship.properties)

    def _check_properties(self, properties: MapExpression | None) -> None:
        """Check the expressions of an inline property map."""
        if properties is None:
            return
        for entry in properties.entries:
            self._check_expression(entry.value, where="an inline property")
            self._refuse_aggregate(entry.value, "a pattern")

    def _unwind_clause(self, clause: UnwindClause) -> None:
        """Check the list UNWIND expands, then bind what it produced.

        The order matters: the expression may not read the alias it is about to define, so it
        is checked while that name is still unbound.
        """

        self._check_expression(clause.expression, where="an UNWIND list")
        # There is no group before the source, so an aggregate here has nothing to summarise.
        self._refuse_aggregate(clause.expression, "UNWIND")
        self._bind(clause.alias, ENTITY_UNWOUND, (), created=False)

    def _bind(
        self, name: str | None, entity: str, labels: tuple[str, ...], *, created: bool
    ) -> None:
        """Record one binding, refusing a name already bound to a different kind of thing."""
        if name is None:
            return
        existing = self._binding(name)
        if existing is None:
            self._bindings.append(
                Binding(name=name, entity=entity, labels=labels, created=created)
            )
            return
        if existing.entity != entity:
            raise self._refuse(
                f"The variable {name!r} is bound to a {existing.entity} and used again as a "
                f"{entity}.",
                field="variable",
                value=name,
            )
        if labels and existing.labels and labels != existing.labels:
            raise self._refuse(
                f"The variable {name!r} is bound with labels {list(existing.labels)} and used "
                f"again with {list(labels)}.",
                field="variable",
                value=name,
            )
        if labels and not existing.labels:
            self._bindings[self._bindings.index(existing)] = Binding(
                name=name, entity=entity, labels=labels, created=existing.created
            )

    def _require_writable_pattern(self, pattern: PatternPath, keyword: str) -> None:
        """Refuse a pattern that says what to look for but not what to write."""
        for node in pattern.nodes:
            if node.variable is not None and self._binding(node.variable) is not None:
                continue
            if len(node.labels) != 1:
                raise self._refuse(
                    f"{keyword} needs exactly one label on every new node, because a row is "
                    f"stored in exactly one table; got {node.describe()}.",
                    field="labels",
                    value=node.describe(),
                )
        for relationship in pattern.relationships:
            if len(relationship.types) != 1:
                raise self._refuse(
                    f"{keyword} needs exactly one relationship type on every new relationship; "
                    f"got {relationship.describe()}.",
                    field="types",
                    value=relationship.describe(),
                )
            if relationship.variable_length:
                raise self._refuse(
                    f"{keyword} writes one relationship at a time, so a hop range has no "
                    f"meaning here; got {relationship.describe()}.",
                    field="hops",
                    value=relationship.describe(),
                )
            if relationship.direction is Direction.UNDIRECTED:
                raise self._refuse(
                    f"{keyword} needs a direction on every new relationship; got "
                    f"{relationship.describe()}.",
                    field="direction",
                    value=relationship.describe(),
                )

    # --- expressions -------------------------------------------------------------------------

    def _check_expression(self, expression: Expression, *, where: str) -> None:
        """Check one expression: its variables are bound and its aggregates are not nested."""
        for node in walk(expression):
            if isinstance(node, Variable):
                self._require_bound(node.name, where)
            elif isinstance(node, Parameter):
                self._note_parameter(node.name)
            elif isinstance(node, FunctionCall):
                self._check_call(node, where=where)
            elif isinstance(node, CaseExpression) and not node.alternatives:
                message = "A CASE expression needs at least one WHEN alternative."
                raise self._refuse(
                    message,
                    field="case",
                    value=node.describe(),
                )

    def _check_call(self, call: FunctionCall, *, where: str) -> None:
        """Check one function call: aggregate nesting, the star form and the two extensions."""
        name = call.name.upper()
        if is_aggregate(call):
            if call.star and name != "COUNT":
                raise self._refuse(
                    f"Only count is written as count(*); got {call.describe()}.",
                    field="function",
                    value=call.name,
                )
            if not call.star and len(call.arguments) != 1:
                raise self._refuse(
                    f"The aggregate {call.name} takes exactly one argument; got "
                    f"{len(call.arguments)}.",
                    field="function",
                    value=call.name,
                )
            for argument in call.arguments:
                if contains_aggregate(argument):
                    raise self._refuse(
                        f"An aggregate cannot be given another aggregate to aggregate; got "
                        f"{call.describe()}.",
                        field="function",
                        value=call.name,
                    )
            return
        if name == SIMILARITY_SCORE_FUNCTION:
            if call.arguments or call.named_arguments:
                raise self._refuse(
                    f"{call.name}() takes no arguments; it reports the score the similarity "
                    "operator produced for the row being projected.",
                    field="function",
                    value=call.name,
                )
            self._scores = True
            return
        if name == COALESCE_FUNCTION:
            if call.named_arguments:
                message = (
                    f"{call.name} takes positional arguments only; got "
                    f"{len(call.named_arguments)} named."
                )
                raise self._refuse(
                    message,
                    field="function",
                    value=call.name,
                )
            if not call.arguments:
                message = f"{call.name} needs at least one argument."
                raise self._refuse(
                    message,
                    field="function",
                    value=call.name,
                )
            return
        if name == STRING_SPLIT_FUNCTION:
            self._check_positional_call(call, arguments=2)
            return
        if name == LABEL_FUNCTION:
            self._check_positional_call(call, arguments=1)
            return
        if name == SIZE_FUNCTION:
            self._check_positional_call(call, arguments=1)
            return
        if name == TIMESTAMP_FUNCTION:
            self._check_positional_call(call, arguments=1)
            return
        if name == SIMILARITY_FUNCTION:
            self._check_similarity_call(call)
            return
        raise self._refuse(
            f"There is no function named {call.name!r} in this dialect; it reads "
            f"{', '.join(sorted(function.lower() for function in AGGREGATE_FUNCTIONS))}, "
            f"{COALESCE_FUNCTION.lower()}, {LABEL_FUNCTION.lower()}, "
            f"{SIZE_FUNCTION.lower()}, "
            f"{SIMILARITY_FUNCTION.lower()}, {SIMILARITY_SCORE_FUNCTION.lower()} and "
            f"{STRING_SPLIT_FUNCTION.lower()} and {TIMESTAMP_FUNCTION.lower()}.",
            field="function",
            value=call.name,
            where=where,
        )

    def _check_positional_call(self, call: FunctionCall, *, arguments: int) -> None:
        """Require one scalar function's exact positional-only signature."""
        if call.distinct or call.star:
            message = (
                f"{call.name} is a scalar function, so it takes neither DISTINCT nor a star."
            )
            raise self._refuse(
                message,
                field="function",
                value=call.name,
            )
        if call.named_arguments:
            message = (
                f"{call.name} takes positional arguments only; got "
                f"{len(call.named_arguments)} named."
            )
            raise self._refuse(
                message,
                field="function",
                value=call.name,
            )
        if len(call.arguments) != arguments:
            message = (
                f"{call.name} takes exactly {arguments} positional "
                f"{'argument' if arguments == 1 else 'arguments'}; got "
                f"{len(call.arguments)}."
            )
            raise self._refuse(
                message,
                field="function",
                value=call.name,
            )

    def _check_similarity_call(self, call: FunctionCall) -> None:
        """Check the shape of the similarity extension and take it apart for the planner."""
        if call.distinct or call.star:
            raise self._refuse(
                f"{call.name} is not an aggregate, so it takes neither DISTINCT nor a star.",
                field="function",
                value=call.name,
            )
        if len(call.arguments) != 2:
            raise self._refuse(
                f"{call.name} compares a stored vector with a reference vector, so it takes "
                f"exactly two arguments and a space; got {len(call.arguments)}.",
                field="function",
                value=call.name,
            )
        subject = call.arguments[0]
        if not isinstance(subject, Property) or not isinstance(subject.subject, Variable):
            raise self._refuse(
                f"The first argument of {call.name} names the stored vector as a property of a "
                f"matched variable, as in n.embedding; got {subject.describe()}.",
                field="argument",
                value=subject.describe(),
            )
        self._require_bound(subject.subject.name, f"{call.name}")
        space = call.named_argument("space")
        if space is None:
            raise self._refuse(
                f"{call.name} must name the embedding space it searches, as in "
                f"space => 'minilm_v2'; comparing vectors of different spaces is refused rather "
                "than guessed.",
                field="space",
                value=call.describe(),
            )
        if not isinstance(space, (Literal, Parameter)):
            raise self._refuse(
                f"The embedding space of {call.name} is a name known before the query runs; got "
                f"{space.describe()}.",
                field="space",
                value=space.describe(),
            )
        for argument in call.named_arguments:
            if argument.name.lower() != "space":
                raise self._refuse(
                    f"{call.name} takes one named argument, space; got {argument.name!r}.",
                    field="argument",
                    value=argument.name,
                )
        reference = call.arguments[1]
        if contains_aggregate(reference) or contains_aggregate(subject):
            raise self._refuse(
                f"{call.name} runs before any grouping, so neither argument may aggregate.",
                field="function",
                value=call.name,
            )
        use = SimilarityUse(
            variable=subject.subject.name,
            property_key=subject.key,
            space=space,
            query_vector=reference,
            call=call,
        )
        if self._similarity is not None and self._similarity.call != call:
            raise self._refuse(
                "A query performs at most one similarity search, because one result carries one "
                f"score; found {self._similarity.describe()} and {use.describe()}.",
                field="function",
                value=call.name,
            )
        self._similarity = use

    def _refuse_aggregate(self, expression: Expression, keyword: str) -> None:
        """Refuse an aggregate in a place that has no group to aggregate over."""
        if contains_aggregate(expression):
            raise self._refuse(
                f"An aggregate cannot appear in {keyword}, because it summarises a group of "
                "result rows and nothing here has produced one.",
                field="expression",
                value=expression.describe(),
            )

    # --- variables ---------------------------------------------------------------------------

    def _aggregates_of(self, expression: Expression) -> tuple[FunctionCall, ...]:
        """Return every aggregate call inside one projected expression, in written order."""
        return tuple(node for node in walk(expression) if is_aggregate(node))

    def _binding(self, name: str) -> Binding | None:
        """Return the binding of one variable, or None."""
        for candidate in self._bindings:
            if candidate.name == name:
                return candidate
        return None

    def _require_bound(self, name: str, where: str) -> None:
        """Refuse a variable no pattern bound."""
        if self._binding(name) is not None:
            return
        known = ", ".join(binding.name for binding in self._bindings) or "none"
        raise self._refuse(
            f"The variable {name!r} used in {where} is bound by no pattern; the variables this "
            f"query binds are {known}.",
            field="variable",
            value=name,
        )

    def _require_bound_subject(self, target: Property, keyword: str) -> None:
        """Refuse a property assignment whose subject is not a bound variable."""
        if not isinstance(target.subject, Variable):
            raise self._refuse(
                f"{keyword} assigns to a property of a matched variable; got "
                f"{target.describe()}.",
                field="target",
                value=target.describe(),
            )
        self._require_bound_entity(target.subject.name, keyword)

    def _require_bound_entity(self, name: str, where: str) -> None:
        """Require a variable that names a matched node or relationship, not a row value."""
        self._require_bound(name, where)
        binding = self._binding(name)
        if binding is not None and binding.entity in (ENTITY_NODE, ENTITY_RELATIONSHIP):
            return
        raise self._refuse(
            f"{where} targets a matched node or relationship; {name!r} is an "
            f"{binding.entity if binding is not None else 'unbound value'}.",
            field="variable",
            value=name,
        )

    def _note_parameter(self, name: str) -> None:
        """Record a parameter reference, refusing a query that names too many."""
        if name in self._parameters:
            return
        if len(self._parameters) >= MAX_PARAMETERS:
            raise self._refuse(
                f"A query may reference at most {MAX_PARAMETERS} parameters.",
                field="parameters",
                value=MAX_PARAMETERS,
            )
        self._parameters.append(name)
