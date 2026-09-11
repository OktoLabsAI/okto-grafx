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

from okto_grafx.domain.query.scalars import NATIVE_SCALARS, scalar_arity

from collections.abc import Iterator
from dataclasses import dataclass, replace

from okto_grafx.domain.errors import GrafxParseError, GrafxPlanError
from okto_grafx.domain.query.ast import (
    ListIteration,
    CaseExpression,
    CreateClause,
    CreateIndexStatement,
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
    NodePattern,
    Parameter,
    PatternPath,
    Property,
    Query,
    RelationshipPattern,
    ReturnClause,
    UnionQuery,
    SubqueryClause,
    ProcedureCall,
    ReturnItem,
    SetClause,
    Statement,
    UnwindClause,
    Variable,
    WithClause,
    free_variables,
    walk,
)
from okto_grafx.domain.query.lexer import tokenize
from okto_grafx.domain.query.limits import (
    MAX_CLAUSES,
    MAX_PARAMETERS,
    MAX_PROJECTION_ITEMS,
    MAX_TRAVERSAL_HOPS,
)
from okto_grafx.domain.query.tokens import (
    TokenKind,
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
    "ENTITY_PATH",
    "NAMED_PATH_SHAPE",
    "POLYMORPHIC_NODE_SHAPE",
    "ENTITY_RELATIONSHIP",
    "ENTITY_PROJECTED",
    "ENTITY_UNWOUND",
    "Aggregation",
    "Binding",
    "QueryAnalysis",
    "SimilarityUse",
    "analyze",
    "contains_aggregate",
    "is_aggregate",
    "named_path",
    "named_path_refusal",
    "exact_path_projection",
    "polymorphic_node",
    "polymorphic_node_refusal",
]

ENTITY_NODE: str = "node"
ENTITY_PATH: str = "path"
"""The one exact named path this subset projects as a result value."""
ENTITY_RELATIONSHIP: str = "relationship"
ENTITY_UNWOUND: str = "unwound value"
"""What UNWIND binds: one element of a list, which is a value and not a matched row."""
ENTITY_PROJECTED: str = "expression alias"
"""What a WITH item with AS binds: a value the projection computed for this row."""


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


POLYMORPHIC_NODE_SHAPE: str = (
    "composed read-only standalone node patterns, including inline maps, UNWIND and OPTIONAL MATCH"
)
"""Current admission boundary for standalone label-free node reads."""


def polymorphic_node(query: Query) -> NodePattern | None:
    """Return the node this query matches without naming a label, or None when it matches none.

    Only a node that stands ALONE counts, because only that one has to be looked for
    everywhere. A node written without a label anywhere in a path takes its table from the
    relationship beside it -- both ends of a hop are named by the relationship's own schema --
    and a second mention of a name an earlier pattern bound takes the table that pattern gave
    it. Neither reads every table, so neither is this, and a path that names no label on an end
    keeps the refusal it already had rather than being re-explained as a shape rule.
    """
    bound: set[str] = set()
    for clause in query.ordered_clauses():
        if isinstance(clause, WithClause):
            bound = (bound if clause.include_existing else set()) | {item.name for item in clause.items}
            continue
        if isinstance(clause, UnwindClause):
            bound.add(clause.alias)
            continue
        if isinstance(clause, ProcedureCall):
            bound.update(item.name for item in clause.yields)
            continue
        if isinstance(clause, SubqueryClause):
            branches = clause.query.branches() if isinstance(clause.query, UnionQuery) else (clause.query,)
            if branches[0].return_clause is not None:
                bound.update(branches[0].return_clause.column_names())
            continue
        patterns = ((clause.pattern,) if isinstance(clause, MergeClause) else
                    clause.patterns if isinstance(clause, (MatchClause, CreateClause)) else ())
        for pattern in patterns:
            first = pattern.nodes[0]
            if (
                isinstance(clause, MatchClause) and not pattern.relationships
                and not first.labels
                and (first.variable is None or first.variable not in bound)
            ):
                return first
            for node in pattern.nodes:
                if node.variable is not None:
                    bound.add(node.variable)
            for relationship in pattern.relationships:
                if relationship.variable is not None:
                    bound.add(relationship.variable)
    return None


def polymorphic_node_refusal(query: Query) -> tuple[str, str] | None:
    """Return a remaining polymorphic write restriction, or None.

    One function, asked by the analysis and asked again by the planner. Both need the answer --
    a tree that never passed the parser reaches the first, and a caller's own analysis walks
    past it to the second -- and two implementations of a shape rule are two chances to admit
    different things.
    """
    driver = polymorphic_node(query)
    if driver is None:
        return None
    reason = _polymorphic_shape_reason(query, driver)
    if reason is None:
        return None
    return (
        "A node that names no label matches every node table; supported reads use "
        f"{POLYMORPHIC_NODE_SHAPE}. {reason}",
        driver.describe(),
    )


def _polymorphic_shape_reason(query: Query, driver: NodePattern) -> str | None:
    """Keep dynamic write-target admission separate from expanded read composition."""
    if query.updating_clauses:
        return "This query writes, and a write needs one table to write into."
    if query.return_clause is None:
        return "This query has no RETURN, so it would read every table for nothing."
    return None


NAMED_PATH_SHAPE: str = (
    "one MATCH of one named path over one named outgoing hop of one type, both ends named and "
    "carrying exactly one label, no inline map and no written range, and a RETURN"
)
"""The only shape a named path is admitted in."""


def hop_range_refusal(query: Query) -> tuple[str, str] | None:
    """Return the refusal a hop range earns when it is not what the parser writes, or None.

    The parser can only produce whole counts inside the bounds, so for parsed text this asks a
    question that is already answered. It is asked anyway, at both doors, because a tree built
    by hand reaches ``analyze`` and a caller's own analysis reaches ``build_plan`` -- and the
    field this guards decides how far a traversal walks. A forged ``max_hops`` is not a wrong
    answer; it is unbounded work, which is the one thing the bound exists to prevent.

    Nothing here is interpolated into the message. A hostile object in these fields would be
    asked to render itself while the refusal was being built, and a refusal that raises reports
    nothing at all.
    """
    for relationship in _relationships_of(query):
        if type(relationship.hop_range_written) is not bool:
            return (
                "A relationship records whether a hop range was written as a boolean.",
                "hop_range_written",
            )
        if type(relationship.min_hops) is not int:
            return (
                "A hop range counts whole hops; its lower bound is an integer.",
                "min_hops",
            )
        if type(relationship.max_hops) is not int:
            return (
                "A hop range counts whole hops; its upper bound is an integer.",
                "max_hops",
            )
        if (
            not 1
            <= relationship.min_hops
            <= relationship.max_hops
            <= MAX_TRAVERSAL_HOPS
        ):
            return (
                "A hop range runs from at least one hop to at most "
                f"{MAX_TRAVERSAL_HOPS}, and starts at or below where it ends.",
                "hops",
            )
        if not relationship.hop_range_written and (
            relationship.min_hops != 1 or relationship.max_hops != 1
        ):
            # The counts say "many hops" and the flag says no range was written. One of the
            # two is a lie, and the pair is how a forged tree would walk a range while looking
            # like the single hop nothing checks.
            return (
                "A relationship that records no written hop range spans exactly one hop.",
                "hop_range_written",
            )
    return None


def _relationships_of(query: Query) -> Iterator[RelationshipPattern]:
    """Yield every relationship pattern the statement carries, matched or written."""
    for clause in query.match_clauses:
        for pattern in clause.patterns:
            yield from pattern.relationships
    for clause in query.updating_clauses:
        written: tuple[PatternPath, ...] = ()
        if isinstance(clause, CreateClause):
            written = clause.patterns
        elif isinstance(clause, MergeClause):
            written = (clause.pattern,)
        for pattern in written:
            yield from pattern.relationships


def union_refusal(statement: UnionQuery) -> tuple[str, str] | None:
    """Validate the bounded tree before traversing potentially forged ASTs."""
    pending: list[object] = [statement]
    seen: set[int] = set()
    columns: tuple[str, ...] | None = None
    leaves = 0
    while pending:
        branch = pending.pop()
        if id(branch) in seen:
            return ("A UNION tree cannot contain cycles or shared branch occurrences.", "branch")
        seen.add(id(branch))
        if type(branch) is UnionQuery:
            if type(branch.all) is not bool:
                return ("UNION ALL selection must be a boolean.", "all")
            if len(seen) > MAX_CLAUSES * 2:
                return ("Too many UNION branches.", "branch")
            pending.extend((branch.right, branch.left))
            continue
        if type(branch) is not Query:
            return ("Each UNION branch must be a query.", "branch")
        leaves += 1
        if leaves > MAX_CLAUSES:
            return ("Too many UNION branches.", "branch")
        if branch.writes:
            return ("UNION write branches are not yet supported.", "branch")
        if branch.return_clause is None:
            return ("Each UNION branch must end with RETURN.", "branch")
        names = branch.return_clause.column_names()
        if columns is None:
            columns = names
        elif names != columns:
            return ("UNION branches must return the same column names in the same order.", "columns")
    return None


def _union_parameters(statement: UnionQuery) -> tuple[str, ...]:
    """Return every parameter either branch reads, left to right, each named once.

    One list, because there is one call and one binding: a parameter both branches read is
    supplied once and means the same value in both, which is what makes the pair a single
    statement rather than two that happen to run together.
    """
    seen: list[str] = []
    for branch in statement.branches():
        for name in _Analyzer(branch).run().parameters:
            if name not in seen:
                if len(seen) >= MAX_PARAMETERS:
                    raise GrafxPlanError(
                        f"A query may reference at most {MAX_PARAMETERS} parameters.",
                        field="parameters",
                        value=MAX_PARAMETERS,
                    )
                seen.append(name)
    return tuple(seen)


def analyze_union(statement: UnionQuery, bindings: tuple[Binding, ...] = (), depth: int = 0) -> QueryAnalysis:
    """Return what a union means, refusing one this engine cannot answer.

    Each branch is analysed on its own, because each is a whole query: its own bindings, its own
    grouping, its own similarity. What the UNION contributes is what survives it -- the column
    names of the left branch and the parameters of both -- and nothing else does. No variable
    crosses the boundary, so the combined analysis binds none: a name that meant a matched row
    inside a branch has no meaning above the union, and carrying it up would say otherwise.
    """
    refusal = union_refusal(statement)
    if refusal is not None:
        message, value = refusal
        raise GrafxPlanError(message, field="union", value=value)
    branches = statement.branches()
    analyses = tuple(_Analyzer(branch, bindings, depth).run() for branch in branches)
    parameters = tuple(dict.fromkeys(name for item in analyses for name in item.parameters))
    if len(parameters) > MAX_PARAMETERS:
        raise GrafxPlanError("Too many parameters across UNION branches.", field="parameters")
    return QueryAnalysis(
        statement=statement,
        parameters=parameters,
        output_columns=analyses[0].output_columns,
    )


_UNTYPED_HOP_SOURCE = "a"
_UNTYPED_HOP_RELATIONSHIP = "r"
_UNTYPED_HOP_TARGET = "b"
_UNTYPED_HOP_LABEL = "Decision"
_UNTYPED_HOP_PROPERTY = "id"
"""The literal statement an untyped hop is admitted as, spelled out rather than generalised.

Naming the pieces is what makes the narrowness reviewable: a reader can see that this admits one
statement and can count the ways it could have admitted more.
"""


def untyped_one_hop_source(query: Query) -> NodePattern | None:
    """Return the source node when this statement is the one untyped hop this engine reads.

    A relationship without a type names no table, and a table is what a traversal walks. Exactly
    one statement is admitted, and it is admitted LITERALLY -- ``MATCH (a:Decision)-[r]->(b)
    RETURN a.id``, those names and that label -- rather than as a family that resembles it. The
    difference is not pedantry: a rule written for "any label, any names" admits a whole class of
    statements this milestone never measured, and a milestone that admits what it did not measure
    is one that shipped a guess. The tables the hop could live in are the relationship tables
    leaving ``Decision``, and the answer is their union in a fixed order.

    Everything else stays refused where it was refused before. This function does not REFUSE
    anything: it recognises, and a statement it does not recognise reaches the same message it
    reached before this milestone -- which is what keeps every other untyped shape, and the text
    a caller already sees for it, exactly as it was.

    Every field is checked for its exact type, because a tree built by hand can carry anything
    in any of them and this decides whether a traversal fans out across tables.
    """
    # Every container and flag below is checked for its EXACT type, not merely for a length or
    # for truthiness. A tree built by hand can carry a list where a tuple belongs and ``0`` where
    # a bool belongs, and both slip past ``len(...)`` and ``or flag`` while meaning something
    # this milestone never measured. The narrowness of this form is the whole reason it is
    # admitted, so the shape it claims has to be the shape it has.
    if type(query) is not Query:
        return None
    if query.unwind_clause is not None:
        return None
    for container in (query.match_clauses, query.with_clauses, query.updating_clauses):
        if type(container) is not tuple:
            return None
    if query.with_clauses or query.updating_clauses:
        return None
    if len(query.match_clauses) != 1:
        return None
    clause = query.match_clauses[0]
    if type(clause) is not MatchClause:
        return None
    if type(clause.optional) is not bool or clause.optional:
        return None
    if clause.predicate is not None:
        return None
    if type(clause.patterns) is not tuple or len(clause.patterns) != 1:
        return None
    pattern = clause.patterns[0]
    if type(pattern) is not PatternPath or pattern.variable is not None:
        return None
    if type(pattern.nodes) is not tuple or len(pattern.nodes) != 2:
        return None
    if type(pattern.relationships) is not tuple or len(pattern.relationships) != 1:
        return None
    source, target = pattern.nodes
    hop = pattern.relationships[0]
    if type(source) is not NodePattern or type(target) is not NodePattern:
        return None
    if type(hop) is not RelationshipPattern:
        return None
    if type(hop.types) is not tuple or type(source.labels) is not tuple:
        return None
    if type(target.labels) is not tuple:
        return None
    if hop.direction is not Direction.OUTGOING:
        return None
    if hop.types or hop.properties is not None:
        return None
    if type(hop.hop_range_written) is not bool or hop.hop_range_written:
        return None
    if type(hop.min_hops) is not int or type(hop.max_hops) is not int:
        return None
    if hop.min_hops != 1 or hop.max_hops != 1:
        return None
    if type(hop.variable) is not str or not hop.variable:
        return None
    if hop.variable != _UNTYPED_HOP_RELATIONSHIP:
        return None
    if type(source.variable) is not str or source.variable != _UNTYPED_HOP_SOURCE:
        return None
    if len(source.labels) != 1 or type(source.labels[0]) is not str:
        return None
    if source.labels[0] != _UNTYPED_HOP_LABEL:
        return None
    if source.properties is not None:
        return None
    if type(target.variable) is not str or target.variable != _UNTYPED_HOP_TARGET:
        return None
    if target.labels or target.properties is not None:
        return None
    returned = query.return_clause
    if type(returned) is not ReturnClause:
        return None
    if type(returned.distinct) is not bool or returned.distinct:
        return None
    if type(returned.sort_items) is not tuple or returned.sort_items:
        return None
    if returned.skip is not None or returned.limit is not None:
        return None
    if type(returned.items) is not tuple or len(returned.items) != 1:
        return None
    item = returned.items[0]
    if type(item) is not ReturnItem or item.alias is not None:
        return None
    if type(item.expression) is not Property:
        return None
    subject = item.expression.subject
    if type(subject) is not Variable or type(subject.name) is not str:
        return None
    if subject.name != _UNTYPED_HOP_SOURCE:
        return None
    if type(item.expression.key) is not str:
        return None
    if item.expression.key != _UNTYPED_HOP_PROPERTY:
        return None
    return source


def _is_literal_row_count(value: object) -> bool:
    """True for a row count written as a literal non-negative integer, else False.

    A literal LIMIT bounds this closed projection for clients that append a
    ceiling to every graph read. Schema identifiers and aliases may vary; the
    remaining clause shape stays fixed.

    Everything else stays out. A parameter or an arithmetic expression is not a
    number until something evaluates it, and a bound this recogniser cannot read
    is a bound it cannot claim to have checked. ``bool`` is refused explicitly
    because it is an ``int`` subclass in Python and ``LIMIT TRUE`` is not a row
    count anybody wrote on purpose. A negative count is refused rather than
    clamped, so a caller learns what it asked for.
    """
    if type(value) is not Literal:
        return False
    count = value.value
    return type(count) is int and count >= 0


def exact_path_projection(query: Query) -> PatternPath | None:
    """Return the path when ``query`` is a typed one-hop path projection, else None.

    The admitted shape is ``MATCH path = (a:Label)-[r:Type]->``
    ``(b:Label) RETURN expression`` at the AST boundary, with at least one expression
    using the path. Aliases and native path functions are permitted, optionally followed
    by ``LIMIT`` and a literal non-negative integer. Lexical trivia the parser discards --
    whitespace, keyword case and a trailing semicolon -- is deliberately not reconstructed.
    Every semantic field, class, container, flag and identifier is checked exactly so a tree a
    caller built cannot widen it into ranges or arbitrary joins.
    Names and schema identifiers are caller-defined; catalog binding proves both endpoints.

    This recognises and never refuses.  A miss reaches the pre-existing named-path refusal, which
    preserves the error surface for every other reading of a path name.
    """
    if type(query) is not Query or query.unwind_clause is not None:
        return None
    for container in (query.match_clauses, query.with_clauses, query.updating_clauses):
        if type(container) is not tuple:
            return None
    if query.with_clauses or query.updating_clauses:
        return None
    if len(query.match_clauses) != 1:
        return None
    clause = query.match_clauses[0]
    if type(clause) is not MatchClause:
        return None
    if type(clause.optional) is not bool or clause.optional:
        return None
    if clause.predicate is not None:
        return None
    if type(clause.patterns) is not tuple or len(clause.patterns) != 1:
        return None
    pattern = clause.patterns[0]
    if type(pattern) is not PatternPath:
        return None
    if not _is_written_path_name(pattern.variable):
        return None
    if type(pattern.nodes) is not tuple or len(pattern.nodes) != 2:
        return None
    if type(pattern.relationships) is not tuple or len(pattern.relationships) != 1:
        return None
    source, target = pattern.nodes
    hop = pattern.relationships[0]
    if type(source) is not NodePattern or type(target) is not NodePattern:
        return None
    if type(hop) is not RelationshipPattern:
        return None
    for node in (source, target):
        if not _is_written_path_name(node.variable):
            return None
        if type(node.labels) is not tuple or len(node.labels) != 1:
            return None
        if not _is_written_path_name(node.labels[0]):
            return None
        if node.properties is not None:
            return None
    if not _is_written_path_name(hop.variable):
        return None
    if type(hop.types) is not tuple or len(hop.types) != 1:
        return None
    if not _is_written_path_name(hop.types[0]):
        return None
    if len({pattern.variable, source.variable, target.variable, hop.variable}) != 4:
        return None
    if hop.direction is not Direction.OUTGOING or hop.properties is not None:
        return None
    if type(hop.hop_range_written) is not bool or hop.hop_range_written:
        return None
    if type(hop.min_hops) is not int or type(hop.max_hops) is not int:
        return None
    if hop.min_hops != 1 or hop.max_hops != 1:
        return None
    returned = query.return_clause
    if type(returned) is not ReturnClause:
        return None
    if type(returned.distinct) is not bool or returned.distinct:
        return None
    if type(returned.sort_items) is not tuple or returned.sort_items:
        return None
    if returned.skip is not None:
        return None
    if returned.limit is not None and not _is_literal_row_count(returned.limit):
        return None
    if type(returned.items) is not tuple or not returned.items:
        return None
    if any(type(item) is not ReturnItem for item in returned.items):
        return None
    for item in returned.items:
        for expression in walk(item.expression):
            if (isinstance(expression, Property) and isinstance(expression.subject, Variable)
                    and expression.subject.name == pattern.variable):
                return None
            if (isinstance(expression, FunctionCall) and expression.name.upper() == SIZE_FUNCTION
                    and any(isinstance(arg, Variable) and arg.name == pattern.variable
                            for arg in expression.arguments)):
                return None
    if any(isinstance(node, Variable) and (type(node) is not Variable or type(node.name) is not str)
           for item in returned.items for node in walk(item.expression)):
        return None
    if not any(pattern.variable in free_variables(item.expression) for item in returned.items):
        return None
    return pattern


def correlated_optional_hop(query: Query) -> PatternPath | None:
    """Recognize a labelled node followed by a correlated, single optional hop.

    Names, labels, direction, projection and row windows are caller-defined.
    Wider optional joins remain refused until they have execution semantics.
    """
    if (len(query.match_clauses) != 2 or query.unwind_clause is not None
            or query.with_clauses or query.updating_clauses or query.return_clause is None):
        return None
    root, optional = query.match_clauses
    if root.optional is not False or optional.optional is not True:
        return None
    if len(root.patterns) != 1 or len(optional.patterns) != 1:
        return None
    base, hop = root.patterns[0], optional.patterns[0]
    if (base.variable is not None or base.relationships or len(base.nodes) != 1
            or hop.variable is not None or len(hop.nodes) != 2 or len(hop.relationships) != 1):
        return None
    anchor = base.nodes[0]
    source, target = hop.nodes
    edge = hop.relationships[0]
    if (not anchor.variable or len(anchor.labels) != 1
            or source.variable != anchor.variable
            or source.labels not in ((), anchor.labels)
            or source.properties is not None or target.properties is not None
            or len(target.labels) > 1 or len(edge.types) > 1
            or edge.properties is not None or edge.hop_range_written
            or edge.min_hops != 1 or edge.max_hops != 1
            or type(edge.direction) is not Direction):
        return None
    names = [name for name in (anchor.variable, target.variable, edge.variable) if name]
    if len(names) != len(set(names)):
        return None
    return hop


def optional_match_refusal(query: Query) -> tuple[str, str] | None:
    """Refuse OPTIONAL MATCH outside the root-node and correlated single-hop forms.

    Root-node optional queries extend an empty scan once. Correlated optional hops extend each
    unmatched anchor separately, after the optional predicate. Wider joins remain refused.

    Asked at both doors, and for EVERY query rather than only the ones the parser marked. A tree
    built by hand reaches ``analyze`` and a caller's own analysis reaches ``build_plan``, and the
    flag this guards decides whether a query with no matches answers nothing or answers a row of
    nulls. A forged ``optional`` is not a wrong answer; it is a row the caller never asked for.

    Nothing here is interpolated into the message. A hostile object in these fields would be
    asked to render itself while the refusal was being built, and a refusal that raises reports
    nothing at all.
    """
    pipeline = query.clause_pipeline
    if type(pipeline) is not tuple:
        return "A query pipeline is an immutable tuple of clauses.", "pipeline"
    if pipeline:
        accepted = (MatchClause, WithClause, UnwindClause, SubqueryClause, ProcedureCall,
                    CreateClause, MergeClause, SetClause, DeleteClause)
        if any(type(clause) not in accepted for clause in pipeline):
            return "A query pipeline contains an unknown clause.", "pipeline"
        if (tuple(c for c in pipeline if isinstance(c, MatchClause)) != query.match_clauses
                or tuple(c for c in pipeline if isinstance(c, WithClause)) != query.with_clauses
                or tuple(c for c in pipeline if isinstance(c, (CreateClause, MergeClause, SetClause, DeleteClause))) != query.updating_clauses
                or next((c for c in pipeline if isinstance(c, UnwindClause)), None) != query.unwind_clause):
            return "A query pipeline must agree with its clause inventory and write classification.", "pipeline"
    order = query.read_clause_order
    if type(order) is not tuple or any(type(kind) is not str or kind not in ("match", "with") for kind in order):
        return "A reading pipeline records only MATCH and WITH clause kinds.", "clause"
    if order and (order.count("match") != len(query.match_clauses)
                  or order.count("with") != len(query.with_clauses)):
        return "A reading pipeline must include each MATCH and WITH exactly once.", "clause"
    for clause in query.match_clauses:
        if type(clause.optional) is not bool:
            return (
                "A MATCH clause records whether it was written OPTIONAL as a boolean.",
                "optional",
            )
    return None


def correlated_optional_pipeline(query: Query) -> bool:
    """Admit bounded incident hops with explicit projection barriers, not arbitrary joins.

    Each hop expands the original labelled anchor. WITH may aggregate each hop
    before the next expansion, avoiding accidental degree Cartesian products.
    Ordinary binding analysis separately enforces projection scope and aliases.
    """
    if (len(query.match_clauses) < 2 or query.unwind_clause is not None
            or query.updating_clauses or query.return_clause is None):
        return False
    root = query.match_clauses[0]
    for optional in query.match_clauses[1:]:
        probe = Query(match_clauses=(root, optional), return_clause=query.return_clause)
        if correlated_optional_hop(probe) is None:
            return False
    # A root scan cannot be postponed until after a projection. Ordinary MATCH
    # after WITH is deliberately still outside this bounded extension.
    return not query.read_clause_order or query.read_clause_order[0] == "match"


def named_path(query: Query) -> PatternPath | None:
    """Return the path this query gives a name to, or None when it names none."""
    for clause in query.match_clauses:
        for pattern in clause.patterns:
            if pattern.variable is not None:
                return pattern
    for clause in query.updating_clauses:
        written: tuple[PatternPath, ...] = ()
        if isinstance(clause, CreateClause):
            written = clause.patterns
        elif isinstance(clause, MergeClause):
            written = (clause.pattern,)
        for pattern in written:
            if pattern.variable is not None:
                return pattern
    return None


def _is_written_path_name(value: object) -> bool:
    """Whether the parser could have produced this as the name of a path.

    Asked of the LEXER rather than described again here: what a name may contain, and the
    ceiling on how long it may be, already live there, and a second copy would eventually be a
    second answer.

    A BARE identifier, deliberately, and not the back-quoted spelling the parser also accepts
    elsewhere. ``describe()`` writes a path name back without quotes, so a name that needed
    them would be put back as text nothing can read; the round trip is what makes the name
    decorative rather than merely ignored, and this subset keeps it by refusing the spelling
    that would break it.

    A tree built by hand can carry anything in this field. A field the parser could not have
    written is not a name to ignore; it is a tree to refuse.

    The type is checked EXACTLY, not by isinstance: a subclass of str is not what the parser
    produces, and one whose ``__getitem__`` raises would carry that exception out through the
    tokenizer as something other than a refusal. What cannot be judged is refused.
    """
    if type(value) is not str or not value:
        return False
    try:
        tokens = tokenize(value)
    except GrafxParseError:
        return False
    written = tuple(token for token in tokens if token.kind is not TokenKind.END)
    return (
        len(written) == 1
        and written[0].kind is TokenKind.NAME
        and not written[0].quoted
        and written[0].text == value
    )


def _named_path_detail(named: PatternPath) -> str:
    """Return a refusal detail that is safe on a tree already known to be malformed.

    The pattern is NOT written back here. By the time this is asked the shape has already been
    judged wrong, and a wrong shape is exactly the one ``describe()`` cannot walk: a path with
    one node and one relationship indexes past its own nodes. A refusal that raises while
    building its own message is worse than the mistake it was reporting.
    """
    variable = named.variable
    return variable if _is_written_path_name(variable) else "a named path"


def named_path_refusal(query: Query) -> tuple[str, str] | None:
    """Return the refusal a named path earns outside its one shape, or None.

    One function, asked by the analysis and asked again by the planner: a tree that never
    passed the parser reaches the first, and a caller's own analysis walks past it to the
    second. The name is decorative, so admitting it in one written form costs nothing at
    runtime -- and admitting it anywhere else would mean deciding what READING one means.
    """
    named = named_path(query)
    if named is None:
        return None
    reason = _named_path_shape_reason(query, named)
    if reason is None:
        return None
    return (
        "A named path is written and never read in this subset, and it is admitted in exactly "
        f"one shape: {NAMED_PATH_SHAPE}. {reason}",
        _named_path_detail(named),
    )


def _named_path_shape_reason(query: Query, named: PatternPath) -> str | None:
    """Return what this statement does that the one shape does not allow, or None."""
    if not _is_written_path_name(named.variable):
        return "A path is named with a name the parser could have written."
    for node in named.nodes:
        if node.variable is not None and type(node.variable) is not str:
            return "A node of a named path is named with a name the parser could have written."
        if node.variable == named.variable:
            return "The name of a path is not the name of a node in it."
    for relationship in named.relationships:
        if relationship.variable is not None and type(relationship.variable) is not str:
            return (
                "A relationship of a named path is named with a name the parser could have "
                "written."
            )
        if relationship.variable == named.variable:
            return "The name of a path is not the name of a relationship in it."
    if query.unwind_clause is not None:
        return "This one follows an UNWIND."
    if query.with_clauses:
        return "This query carries a WITH."
    if query.updating_clauses:
        return "This query writes, and a pattern being written is not one to refer back to."
    if query.return_clause is None:
        return "This query has no RETURN."
    if len(query.match_clauses) != 1:
        return f"This query has {len(query.match_clauses)} MATCH clauses."
    patterns = query.match_clauses[0].patterns
    if len(patterns) != 1:
        return f"This MATCH carries {len(patterns)} patterns."
    if patterns[0] is not named:
        return "The named path is not the pattern this MATCH reads."
    if len(named.relationships) != 1 or len(named.nodes) != 2:
        return "A named path here spans exactly one hop between two nodes."
    relationship = named.relationships[0]
    if relationship.variable is None or len(relationship.types) != 1:
        return "The hop of a named path is named and carries exactly one type."
    if relationship.direction is not Direction.OUTGOING:
        return "A named path points one way, from its first node to its second."
    if relationship.variable_length or relationship.hop_range_written:
        return "A named path spans one hop, so it carries no range."
    if relationship.properties is not None:
        return "The hop of a named path carries no inline property map."
    for node in named.nodes:
        if node.variable is None:
            return "Both ends of a named path are named."
        if len(node.labels) != 1:
            return "Both ends of a named path name exactly one label."
        if node.properties is not None:
            return "Neither end of a named path carries an inline property map."
    return None


def is_aggregate(expression: Expression) -> bool:
    """Return True when this expression is itself a call to one of the six aggregates."""
    return (
        isinstance(expression, FunctionCall)
        and expression.name.upper() in AGGREGATE_FUNCTIONS
    )


def contains_aggregate(expression: Expression) -> bool:
    """Return True when an aggregate appears anywhere inside this expression."""
    return any(is_aggregate(node) for node in walk(expression))


def analyze(statement: Statement, *, bindings: tuple[Binding, ...] = (), depth: int = 0) -> QueryAnalysis:
    """Return what a statement means, refusing one that parses but cannot be answered."""
    if depth > 16:
        raise GrafxPlanError("Subqueries may nest at most 16 levels.", field="subquery_depth")
    if isinstance(
        statement,
        (
            CreateIndexStatement,
            CreateNodeTableStatement,
            CreateRelTableStatement,
            CreateVectorSpaceStatement,
        ),
    ):
        return QueryAnalysis(
            statement=statement, parameters=_parameters_of_schema(statement)
        )
    if type(statement) is UnionQuery:
        return analyze_union(statement, bindings, depth)
    if not isinstance(statement, Query):
        raise GrafxPlanError(
            f"A statement of type {type(statement).__name__} cannot be planned.",
            field="statement",
            value=type(statement).__name__,
        )
    return _Analyzer(statement, bindings, depth).run()


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

    __slots__ = (
        "_query",
        "_bindings",
        "_discarded",
        "_projected_path",
        "_path_names",
        "_parameters",
        "_similarity",
        "_scores",
        "_depth",
    )

    def __init__(self, query: Query, bindings: tuple[Binding, ...] = (), depth: int = 0) -> None:
        self._query = query
        self._bindings: list[Binding] = list(bindings)
        self._depth = depth
        # The names a WITH stopped carrying, kept only so that reading one below it
        # is refused for what it is rather than as a variable nothing ever bound.
        self._discarded: set[str] = set()
        self._projected_path = exact_path_projection(query)
        # Every other path name is recorded so a collision is a refusal and a read earns the
        # established path-specific error rather than looking like an ordinary unbound name.
        self._path_names: set[str] = set()
        self._parameters: list[str] = []
        self._similarity: SimilarityUse | None = None
        self._scores = False

    def run(self) -> QueryAnalysis:
        """Analyse the whole query and return what the planner needs."""
        refusal = hop_range_refusal(self._query)
        if refusal is not None:
            message, value = refusal
            raise self._refuse(message, field="hops", value=value)
        refusal = named_path_refusal(self._query)
        if refusal is not None:
            message, value = refusal
            raise self._refuse(message, field="pattern", value=value)
        refusal = polymorphic_node_refusal(self._query)
        if refusal is not None:
            message, value = refusal
            raise self._refuse(message, field="pattern", value=value)
        refusal = optional_match_refusal(self._query)
        if refusal is not None:
            message, value = refusal
            raise self._refuse(message, field="clause", value=value)
        for clause in self._query.ordered_clauses():
            if isinstance(clause, UnwindClause):
                self._unwind_clause(clause)
                continue
            if isinstance(clause, MatchClause):
                self._match_clause(clause)
            elif isinstance(clause, WithClause):
                self._with_clause(clause)
            elif isinstance(clause, SubqueryClause):
                self._subquery_clause(clause)
            elif isinstance(clause, ProcedureCall):
                for argument in clause.arguments:
                    self._check_expression(argument, where="a procedure argument")
                    self._refuse_aggregate(argument, "CALL")
                for item in clause.yields:
                    if not isinstance(item.expression, Variable) or self._binding(item.name) is not None:
                        raise self._refuse("YIELD must bind a new name to a declared output column.", field="yield")
                    self._bind(item.name, ENTITY_PROJECTED, (), created=False)
                if clause.predicate is not None:
                    self._check_expression(clause.predicate, where="YIELD WHERE")
                    self._refuse_aggregate(clause.predicate, "YIELD WHERE")
            else:
                self._updating_clause(clause)
        if self._query.return_clause is None and not self._query.writes:
            raise self._refuse("A read query must end in RETURN.", field="clause", value="RETURN")
        grouping, aggregations = self._return_clause()
        if (self._similarity is not None and self._query.with_clauses
                and any(clause.optional for clause in self._query.match_clauses)):
            raise self._refuse(
                "Vector search cannot cross interleaved optional projection barriers.",
                field="function", value="similarity",
            )
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
            self._name_path(pattern)
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

    def _subquery_clause(self, clause: SubqueryClause) -> None:
        """Check imported scope independently and publish only explicit RETURN columns."""
        if (type(clause.imports) is not tuple or len(set(clause.imports)) != len(clause.imports)
                or any(type(name) is not str for name in clause.imports)):
            raise self._refuse("Subquery imports must be unique variable names.", field="imports")
        outer = clause.outer_names or clause.imports
        if len(outer) != len(clause.imports):
            raise self._refuse("Subquery import mapping has wrong arity.", field="imports")
        imported = []
        for source, target in zip(outer, clause.imports, strict=True):
            self._require_bound(source, "a subquery import")
            binding = self._binding(source)
            assert binding is not None
            imported.append(replace(binding, name=target))
        inner = analyze(clause.query, bindings=tuple(imported), depth=self._depth + 1)
        branches = clause.query.branches() if isinstance(clause.query, UnionQuery) else (clause.query,)
        if any(branch.writes or branch.return_clause is None for branch in branches):
            raise self._refuse("A returning subquery must be read-only.", field="subquery")
        exported = clause.output_aliases or inner.output_columns
        if len(exported) != len(inner.output_columns):
            raise self._refuse("Subquery output mapping has wrong arity.", field="subquery")
        for position, name in enumerate(exported):
            if self._binding(name) is not None:
                raise self._refuse("A subquery cannot overwrite an outer variable.", field="variable", value=name)
            carried = None
            if isinstance(clause.query, Query) and clause.query.return_clause is not None:
                expression = clause.query.return_clause.items[position].expression
                if isinstance(expression, Variable):
                    carried = inner.binding(expression.name)
            if carried is not None:
                self._bindings.append(replace(carried, name=name))
            else:
                self._bind(name, ENTITY_PROJECTED, (), created=False)
        for name in inner.parameters:
            if name not in self._parameters:
                self._parameters.append(name)
        if len(self._parameters) > MAX_PARAMETERS:
            raise self._refuse("Too many parameters across subqueries.", field="parameters")

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
        projected_variables = {item.expression.name for item in clause.items if isinstance(item.expression, Variable)}
        for key in clause.sort_items:
            if isinstance(key.expression, Variable) and key.expression.name in aliases:
                # An alias names a column of this clause's own result, so it is resolved before
                # the key is treated as an expression over bound variables -- otherwise every
                # ORDER BY over an alias would read as a variable no pattern bound.
                continue
            self._check_expression(key.expression, where="an ORDER BY key")
            if key.expression in projected:
                continue
            if (isinstance(key.expression, Property) and isinstance(key.expression.subject, Variable)
                    and key.expression.subject.name in projected_variables):
                # Returning the whole entity retains its properties after dedupe;
                # this does not admit a property of a discarded input variable.
                continue
            if contains_aggregate(key.expression):
                raise self._refuse(
                    "An ORDER BY key that aggregates must be projected first and sorted by its "
                    f"name; {key.expression.describe()} is not one of the returned items.",
                    field="sort_item",
                    value=key.expression.describe(),
                    reason="invalid_aggregation_context", query_phase="planning",
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

    def _name_path(self, pattern: PatternPath) -> None:
        """Record the name a path was given, refusing one something else already answers to."""
        name = pattern.variable
        if name is None:
            return
        if pattern is self._projected_path:
            # The exact recogniser proved both the whole statement and that this name cannot be
            # read anywhere except its sole RETURN item.  Giving it a real binding keeps the
            # published analysis honest without making any decorative named path readable.
            self._bind(name, ENTITY_PATH, (), created=False)
            return
        # A name shared with a node or a relationship is refused by the shape gate, over the
        # STATEMENT and before any of this runs, so that a caller supplying its own analysis
        # meets the same rule. Recording the name here is what makes reading it refusable.
        self._path_names.add(name)

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

    def _with_clause(self, clause: WithClause) -> None:
        """Replace the scope with what this stage projects, then check the WHERE it carries.

        Every item is checked against the scope the stage RECEIVED, and only when all of them
        are checked does the projected scope take over. That order is what makes the two rules
        of a stage true at once: an item cannot read an alias its own WITH is creating, because
        those names arrive together; and a variable this stage did not carry is unreadable
        below it, because it is no longer in the scope the clauses under it are checked in.

        The predicate is checked AFTER the substitution, because it belongs to this stage: it
        reads the aliases just created and it runs once the projection has produced them.
        """

        incoming = tuple(self._bindings)
        if type(clause.include_existing) is not bool:
            raise self._refuse("WITH include_existing must be boolean.", field="include_existing")
        created = {item.alias for item in clause.items if item.alias is not None}
        projected: list[Binding] = list(incoming) if clause.include_existing else []
        if len(projected) + len(clause.items) > MAX_PROJECTION_ITEMS:
            raise self._refuse(f"A WITH clause may project at most {MAX_PROJECTION_ITEMS} items after star expansion.",
                               field="items", value=MAX_PROJECTION_ITEMS)
        for item in clause.items:
            binding = self._projected_binding(item, created)
            if any(carried.name == binding.name for carried in projected):
                raise self._refuse(
                    f"WITH projects the name {binding.name!r} twice, and a row carries each "
                    "name once.",
                    field="item",
                    value=binding.name,
                )
            projected.append(binding)
        self._bindings = projected
        names = {binding.name for binding in projected}
        self._discarded.update(
            binding.name for binding in incoming if binding.name not in names
        )
        self._discarded.difference_update(names)
        for key in clause.sort_items:
            # No aggregate is valid in this normalized, non-grouped sort context.
            # Prove that before resolving arguments against the projected scope;
            # otherwise a dropped argument masks the real invalid aggregation.
            self._refuse_aggregate(key.expression, "the ORDER BY of a WITH")
        for key in clause.sort_items:
            self._check_expression(key.expression, where="the ORDER BY of a WITH")
        self._check_row_window(clause.skip, "SKIP")
        self._check_row_window(clause.limit, "LIMIT")
        if clause.predicate is not None:
            self._check_expression(clause.predicate, where="the WHERE of a WITH")
            self._refuse_aggregate(clause.predicate, "the WHERE of a WITH")

    def _projected_binding(self, item: ReturnItem, created: set[str]) -> Binding:
        """Check one WITH item against the incoming scope and return what it leaves bound."""
        self._refuse_same_stage_alias(item, created)
        expression = item.expression
        if isinstance(expression, Variable):
            self._require_bound(expression.name, "a WITH item")
            carried = self._binding(expression.name)
            assert carried is not None  # _require_bound refused when nothing bound it
            if item.alias is None or item.alias == expression.name:
                return carried
            return Binding(
                name=item.alias, entity=carried.entity, labels=carried.labels, created=carried.created
            )
        if item.alias is None:
            raise self._refuse(
                "WITH gives every item it computes a name; "
                f"{expression.describe()} needs AS.",
                field="item",
                value=expression.describe(),
            )
        self._check_expression(expression, where="a WITH item")
        # Aggregate calls are validated by _check_expression, exactly as in RETURN.
        # The planner inserts the same budgeted grouping operator before WITH.
        return Binding(
            name=item.alias, entity=ENTITY_PROJECTED, labels=(), created=False
        )

    def _refuse_same_stage_alias(self, item: ReturnItem, created: set[str]) -> None:
        """Refuse an item that reads a name its own WITH is creating."""
        for name in free_variables(item.expression):
            if name not in created or self._binding(name) is not None:
                continue
            raise self._refuse(
                f"The alias {name!r} is created by this WITH, so the items beside it cannot "
                "read it; a WITH below this one can.",
                field="variable",
                value=name,
                reason="undefined_variable", query_phase="planning",
            )


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
        for name in free_variables(expression):
            self._require_bound(name, where)
        for node in walk(expression):
            if isinstance(node, ListIteration):
                if node.mode not in ("map", "all", "any", "none", "single", "reduce"):
                    raise self._refuse("Unknown list iteration mode.", field="iteration")
                if (node.mode == "reduce") != (node.accumulator is not None and node.initial is not None):
                    raise self._refuse("A reduction needs an accumulator and initial value.", field="iteration")
                if node.mode != "reduce" and (node.accumulator is not None or node.initial is not None):
                    raise self._refuse("Only a reduction declares an accumulator and initial value.", field="iteration")
                if node.accumulator == node.variable or (node.mode != "map" and node.predicate is not None):
                    raise self._refuse("Invalid list iteration bindings.", field="iteration")
                for body in (node.predicate, node.body):
                    if body is not None and contains_aggregate(body):
                        raise self._refuse("List-local expressions cannot contain row aggregates.", field="iteration")
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
        if name in ("LENGTH", "NODES", "RELATIONSHIPS"):
            self._check_positional_call(call, arguments=1)
            return
        if name in NATIVE_SCALARS:
            scalar_arity(name, len(call.arguments))
            self._check_positional_call(call, arguments=len(call.arguments))
            return
        if name == "UDF":
            if call.star or call.distinct or call.named_arguments or not 1 <= len(call.arguments) <= 33:
                raise self._refuse("udf(name, ...) needs 1..33 positional arguments.", field="function", value=call.name)
            if not isinstance(call.arguments[0], Literal) or type(call.arguments[0].value) is not str:
                raise self._refuse("udf needs a literal registered name.", field="function", value=call.name)
            return
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
        if name in (STRING_SPLIT_FUNCTION, "SPLIT"):
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
            f"There is no function named {call.name!r} in the native query catalog. "
            "Native functions include coalesce, lower, split, abs and similarity. "
            "See QUERY_LANGUAGE.md for supported functions and COMPOSABLE_QUERIES.md "
            "for separately registered tabular CALL/YIELD procedures.",
            field="function",
            value=call.name,
            where=where,
        )

    def _check_positional_call(self, call: FunctionCall, *, arguments: int) -> None:
        """Require one scalar function's exact positional-only signature."""
        if call.distinct or call.star:
            message = f"{call.name} is a scalar function, so it takes neither DISTINCT nor a star."
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
        if not isinstance(subject, Property) or not isinstance(
            subject.subject, Variable
        ):
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
                reason="invalid_aggregation_context", query_phase="planning",
            )

    # --- variables ---------------------------------------------------------------------------

    def _aggregates_of(self, expression: Expression) -> tuple[FunctionCall, ...]:
        """Return every aggregate call inside one projected expression, in written order."""
        return tuple(node for node in walk(expression) if is_aggregate(node))

    def _binding(self, name: str) -> Binding | None:
        """Return the binding of one variable, or None."""
        if type(name) is not str:
            # A parser-produced variable name is always a builtin ``str``.  Check that
            # boundary before equality: a hand-built subclass may override ``__eq__`` and a
            # malformed tree must earn a typed refusal rather than execute that callback.
            raise self._refuse(
                "A variable is named with a name the parser could have written.",
                field="variable",
                value="a variable name",
            )
        for candidate in self._bindings:
            if candidate.name == name:
                return candidate
        return None

    def _require_bound(self, name: str, where: str) -> None:
        """Refuse a variable no pattern bound."""
        if self._binding(name) is not None:
            return
        if name in self._path_names:
            raise self._refuse(
                f"The path {name!r} is written and never read in this subset, so {where} "
                "cannot ask what it contains.",
                field="variable",
                value=name,
            )
        if name in self._discarded:
            raise self._refuse(
                f"The variable {name!r} used in {where} was dropped by a WITH "
                "clause; only the names a WITH projects stay in scope below it.",
                field="variable",
                value=name,
                reason="undefined_variable", query_phase="planning",
            )
        known = ", ".join(binding.name for binding in self._bindings) or "none"
        raise self._refuse(
            f"The variable {name!r} used in {where} is bound by no pattern; the variables this "
            f"query binds are {known}.",
            field="variable",
            value=name,
            reason="undefined_variable", query_phase="planning",
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
