"""Reading the supported Cypher subset into a syntax tree (CONTRACT.md section 8.9, D3).

The parser is recursive descent with precedence climbing for expressions, and both halves of that
choice are about termination rather than elegance.

*Precedence climbing over one function per precedence level.* A chain of same-precedence operators
is consumed by a loop, not by a recursion, so ``a + b + c + ...`` costs constant stack whatever its
length. Only genuine NESTING recurses -- parentheses, a prefix operator, the right operand of a
right-associative operator -- and every one of those sites increments a depth counter that refuses
past :data:`~okto_grafx.domain.query.limits.MAX_EXPRESSION_DEPTH`. The counter is checked BEFORE
the descent, so the refusal arrives with the stack still shallow and as a ``GrafxParseError``
rather than as the ``RecursionError`` CPython would otherwise raise out of a public door.

*A token stream that always ends with an end marker.* Every lookahead is total: past the end the
cursor keeps answering with the end token instead of raising ``IndexError``. Truncated input is
the single most common hostile shape a parser meets, and the ordinary way a hand-written one
grows an ``IndexError`` is a bare ``tokens[position + 1]``.

Equality/ordering chains denote adjacent-pair conjunctions: ``a = b = c`` means
``a = b AND b = c``, not boolean-valued left association. Explicit grouping is
preserved. Expansion reuses operand trees and the ordinary bounded AND evaluator.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from okto_grafx.domain.errors import GrafxParseError
from okto_grafx.domain.model.value import INT64_MAX, INT64_MIN
from okto_grafx.domain.query.ast import (
    BinaryOperation,
    CaseAlternative,
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
    ListIteration,
    Literal,
    MapEntry,
    MapExpression,
    MatchClause,
    MergeClause,
    NamedArgument,
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
    SetItem,
    SortItem,
    Statement,
    Subscript,
    ListSlice,
    UnaryOperation,
    UnwindClause,
    UnionQuery,
    SubqueryClause,
    ProcedureCall,
    UpdatingClause,
    Variable,
    WithClause,
)
from okto_grafx.domain.query.lexer import tokenize
from okto_grafx.domain.query.scalars import NONDETERMINISTIC_SCALARS
from okto_grafx.domain.query.limits import (
    DEFAULT_TRAVERSAL_HOPS,
    MAX_CLAUSES,
    MAX_COLUMN_DEFINITIONS,
    MAX_EXPRESSION_DEPTH,
    MAX_LIST_ELEMENTS,
    MAX_MAP_ENTRIES,
    MAX_PATTERN_ELEMENTS,
    MAX_PATTERNS_PER_CLAUSE,
    MAX_PROJECTION_ITEMS,
    MAX_SORT_KEYS,
    MAX_TRAVERSAL_HOPS,
)
from okto_grafx.domain.query.tokens import AGGREGATE_FUNCTIONS, Token, TokenKind

__all__ = [
    "COLUMN_TYPE_NAMES",
    "PRECEDENCE_AND",
    "PRECEDENCE_COMPARISON",
    "PRECEDENCE_NOT",
    "PRECEDENCE_OR",
    "PRECEDENCE_POWER",
    "PRECEDENCE_PRODUCT",
    "PRECEDENCE_SUM",
    "PRECEDENCE_XOR",
    "VECTOR_TYPE_NAME",
    "parse",
]

PRECEDENCE_OR: int = 1
PRECEDENCE_XOR: int = 2
PRECEDENCE_AND: int = 3
PRECEDENCE_NOT: int = 4
PRECEDENCE_COMPARISON: int = 5
PRECEDENCE_NULL: int = 6
PRECEDENCE_SUM: int = 7
PRECEDENCE_PRODUCT: int = 8
PRECEDENCE_POWER: int = 9

VECTOR_TYPE_NAME: str = "VECTOR"
"""The column type that names an embedding space instead of a width.

The reference dialect writes a fixed list as ``DOUBLE[384]``, which carries the width and nothing
else. SPEC-VEC TR-2 makes the embedding space the carrier of the width, the metric and the
storage precision, and a column that repeated the width could contradict the space it points at
-- so the column names the space, and the width is read from the catalog. Recorded as a dialect
divergence rather than a silent one.
"""

COLUMN_TYPE_NAMES: frozenset[str] = frozenset(
    {
        "INT64",
        "STRING",
        "DOUBLE",
        "BOOL",
        "BOOLEAN",
        "BLOB",
        "UUID",
        "TIMESTAMP",
        VECTOR_TYPE_NAME,
    }
)
"""The column types the DDL accepts, folded to upper case."""

_COMPARISONS: frozenset[str] = frozenset({"=", "<>", "!=", "<", "<=", ">", ">="})
_SUM_SYMBOLS: frozenset[str] = frozenset({"+", "-"})
_PRODUCT_SYMBOLS: frozenset[str] = frozenset({"*", "/", "%"})


@dataclass(frozen=True, slots=True)
class _Operator:
    """One infix operator the expression parser recognised, with how to consume it."""

    text: str
    precedence: int
    tokens: int
    right_associative: bool = False


def parse(text: str) -> Statement:
    """Return the statement one query text denotes, refusing anything outside the subset.

    Only :class:`~okto_grafx.domain.errors.GrafxParseError` leaves this door. That is the whole
    contract of the function: a caller may hand it any string at all, including one built by an
    attacker, and the answer is either a statement or a located refusal.
    """
    tokens = tokenize(text)
    return _Parser(tokens, str.__str__(text)).parse_statement()


class _Parser:
    """A cursor over the tokens of one query, with a bounded recursion depth."""

    __slots__ = ("_tokens", "_position", "_depth", "_source")

    def __init__(self, tokens: tuple[Token, ...], source: str) -> None:
        self._tokens = tokens
        self._source = source
        self._position = 0
        self._depth = 0

    # --- cursor ------------------------------------------------------------------------------

    @property
    def _current(self) -> Token:
        """Return the token under the cursor, which is the end token past the end."""
        return self._tokens[min(self._position, len(self._tokens) - 1)]

    def _peek(self, ahead: int = 1) -> Token:
        """Return a token further along, answering with the end token past the end."""
        return self._tokens[min(self._position + ahead, len(self._tokens) - 1)]

    def _advance(self) -> Token:
        """Consume and return the token under the cursor."""
        token = self._current
        if self._position < len(self._tokens) - 1:
            self._position += 1
        return token

    def _at_symbol(self, symbol: str, ahead: int = 0) -> bool:
        """Return True when the token that far along is that punctuation symbol."""
        token = self._peek(ahead) if ahead else self._current
        return token.kind is TokenKind.SYMBOL and token.text == symbol

    def _at_keyword(self, keyword: str, ahead: int = 0) -> bool:
        """Return True when the token that far along is an unquoted word spelling that keyword."""
        token = self._peek(ahead) if ahead else self._current
        return token.reads_as(keyword)

    def _take_symbol(self, symbol: str) -> Token:
        """Consume one punctuation symbol, refusing anything else."""
        if not self._at_symbol(symbol):
            raise self._unexpected(f"{symbol!r}")
        return self._advance()

    def _take_keyword(self, keyword: str) -> Token:
        """Consume one keyword, refusing anything else."""
        if not self._at_keyword(keyword):
            raise self._unexpected(keyword)
        return self._advance()

    def _match_symbol(self, symbol: str) -> bool:
        """Consume one punctuation symbol when it is there, and say whether it was."""
        if self._at_symbol(symbol):
            self._advance()
            return True
        return False

    def _match_keyword(self, keyword: str) -> bool:
        """Consume one keyword when it is there, and say whether it was."""
        if self._at_keyword(keyword):
            self._advance()
            return True
        return False

    def _take_name(self, what: str, *, allow_empty: bool = False) -> str:
        """Consume one name token and return its text, refusing anything else."""
        token = self._current
        if token.kind is not TokenKind.NAME or (not token.text and not allow_empty):
            raise self._unexpected(what)
        self._advance()
        return token.text

    def _unexpected(self, expected: str) -> GrafxParseError:
        """Return the refusal for a token that is not what the grammar needs here."""
        token = self._current
        return GrafxParseError(
            f"Expected {expected} but found {token.describe()} "
            f"(line {token.line}, column {token.column}).",
            line=token.line,
            column=token.column,
            offset=token.offset,
            expected=expected,
            found=token.text,
            reason="unexpected_syntax",
            query_phase="planning",
        )

    def _refuse(self, message: str, **details: object) -> GrafxParseError:
        """Return a refusal about the token under the cursor."""
        token = self._current
        return GrafxParseError(
            f"{message} (line {token.line}, column {token.column}).",
            line=token.line,
            column=token.column,
            offset=token.offset,
            **details,
        )

    # --- depth -------------------------------------------------------------------------------

    def _descend(self) -> None:
        """Enter one level of nesting, refusing before the descent rather than after it."""
        if self._depth >= MAX_EXPRESSION_DEPTH:
            raise self._refuse(
                f"A query may nest at most {MAX_EXPRESSION_DEPTH} levels deep",
                field="depth",
                value=MAX_EXPRESSION_DEPTH,
            )
        self._depth += 1

    def _ascend(self) -> None:
        """Leave one level of nesting."""
        self._depth -= 1

    # --- statements --------------------------------------------------------------------------

    def parse_statement(self) -> Statement:
        """Parse the whole token stream as exactly one statement."""
        if self._current.kind is TokenKind.END:
            raise self._refuse("A query may not be empty", field="text")
        statement = self._statement_body()
        self._match_symbol(";")
        if self._current.kind is not TokenKind.END:
            raise self._unexpected("the end of the query")
        return statement

    def _statement_body(self) -> Statement:
        """Parse one statement, choosing between the schema statements and a query."""
        if self._at_keyword("CREATE"):
            if self._at_keyword("INDEX", 1):
                return self._create_index()
            if self._at_keyword("NODE", 1) and self._at_keyword("TABLE", 2):
                return self._create_node_table()
            if self._at_keyword("REL", 1) and self._at_keyword("TABLE", 2):
                return self._create_rel_table()
            if self._at_keyword("VECTOR", 1) and self._at_keyword("SPACE", 2):
                return self._create_vector_space()
        left = self._query()
        if not self._at_keyword("UNION"):
            return left
        return self._union(left)

    def _union(self, left: Query) -> UnionQuery:
        """Parse a bounded left-associated sequence with a per-operator duplicate policy."""
        combined: Query | UnionQuery = left
        branches = 1
        while self._match_keyword("UNION"):
            if branches >= MAX_CLAUSES:
                raise self._refuse("Too many UNION branches.", field="clauses", value=MAX_CLAUSES)
            keep_duplicates = self._match_keyword("ALL")
            right = self._query()
            combined = UnionQuery(left=combined, right=right, all=keep_duplicates)
            branches += 1
        assert isinstance(combined, UnionQuery)
        return combined

    def _create_index(self) -> CreateIndexStatement:
        """Parse the P2-ID custom exact-index declaration."""
        self._take_keyword("CREATE")
        self._take_keyword("INDEX")
        name = self._take_name("an index name")
        self._take_keyword("FOR")
        self._take_symbol("(")
        variable = self._take_name("a node variable")
        self._take_symbol(":")
        table = self._take_name("a node table name")
        self._take_symbol(")")
        self._take_keyword("ON")
        self._take_symbol("(")
        if self._at_symbol(")"):
            raise self._refuse(
                "An index needs at least one key column", field="columns"
            )
        columns: list[str] = []
        while True:
            key_variable = self._take_name("the indexed node variable")
            if key_variable != variable:
                raise self._refuse(
                    f"Every indexed column must belong to variable {variable!r}; got "
                    f"{key_variable!r}",
                    field="variable",
                    value=key_variable,
                )
            self._take_symbol(".")
            columns.append(self._take_name("an indexed column name"))
            if not self._match_symbol(","):
                break
        self._take_symbol(")")

        bucket_count: int | None = None
        expected_cardinality: int | None = None
        layout: str | None = None
        if self._match_keyword("OPTIONS"):
            option_token = self._current
            option = self._take_name("bucket_count, expected_cardinality or layout")
            folded = option.lower()
            if option_token.quoted or folded not in {
                "bucket_count",
                "expected_cardinality",
                "layout",
            }:
                raise self._refuse(
                    "An index option is bucket_count, expected_cardinality or layout; "
                    f"got {option!r}",
                    field="option",
                    value=option,
                )
            self._take_symbol("=")
            if folded == "layout":
                value_token = self._current
                if value_token.kind is not TokenKind.NAME or value_token.quoted:
                    raise self._refuse(
                        "An index layout is an unquoted layout name.",
                        field="layout",
                        value=value_token.text,
                    )
                value = self._take_name("the index layout")
                layout = value
            else:
                token = self._current
                if token.kind is not TokenKind.INTEGER:
                    raise self._unexpected("a positive integer sizing value")
                self._advance()
                value = int(token.value)
                if value <= 0:
                    raise self._refuse(
                        f"The {folded} option must be positive; got {value}",
                        field=folded,
                        value=value,
                    )
                if folded == "bucket_count":
                    bucket_count = value
                else:
                    expected_cardinality = value

        return CreateIndexStatement(
            name=name,
            variable=variable,
            table=table,
            columns=tuple(columns),
            bucket_count=bucket_count,
            expected_cardinality=expected_cardinality,
            layout=layout,
        )

    def _create_node_table(self) -> CreateNodeTableStatement:
        """Parse ``CREATE NODE TABLE name(columns, PRIMARY KEY(column))``."""
        self._take_keyword("CREATE")
        self._take_keyword("NODE")
        self._take_keyword("TABLE")
        name = self._take_name("a table name")
        self._take_symbol("(")
        columns: list[ColumnSpec] = []
        primary_key: str | None = None
        while True:
            if self._at_keyword("PRIMARY"):
                if primary_key is not None:
                    raise self._refuse(
                        "A table declares at most one PRIMARY KEY", field="primary_key"
                    )
                primary_key = self._primary_key()
            else:
                columns.append(self._column_spec(len(columns)))
            if not self._match_symbol(","):
                break
        self._take_symbol(")")
        if not columns:
            raise self._refuse("A table needs at least one column", field="columns")
        return CreateNodeTableStatement(
            name=name, columns=tuple(columns), primary_key=primary_key
        )

    def _create_rel_table(self) -> CreateRelTableStatement:
        """Parse ``CREATE REL TABLE name(FROM a TO b, columns)``."""
        self._take_keyword("CREATE")
        self._take_keyword("REL")
        self._take_keyword("TABLE")
        name = self._take_name("a table name")
        self._take_symbol("(")
        self._take_keyword("FROM")
        from_table = self._take_name("the table a relationship starts at")
        self._take_keyword("TO")
        to_table = self._take_name("the table a relationship ends at")
        columns: list[ColumnSpec] = []
        while self._match_symbol(","):
            columns.append(self._column_spec(len(columns)))
        self._take_symbol(")")
        return CreateRelTableStatement(
            name=name, from_table=from_table, to_table=to_table, columns=tuple(columns)
        )

    def _create_vector_space(self) -> CreateVectorSpaceStatement:
        """Parse ``CREATE VECTOR SPACE name {options}``."""
        self._take_keyword("CREATE")
        self._take_keyword("VECTOR")
        self._take_keyword("SPACE")
        name = self._space_name()
        if not self._at_symbol("{"):
            raise self._unexpected("a map of options, as in {dimension: 384}")
        options = self._map_literal()
        return CreateVectorSpaceStatement(name=name, options=options)

    def _space_name(self) -> str:
        """Return the name of an embedding space, written as a name or as a string."""
        token = self._current
        if token.kind is TokenKind.STRING:
            self._advance()
            return str(token.value)
        return self._take_name("an embedding space name")

    def _primary_key(self) -> str:
        """Parse ``PRIMARY KEY(column)`` and return the column it names."""
        self._take_keyword("PRIMARY")
        self._take_keyword("KEY")
        self._take_symbol("(")
        column = self._take_name("a column name")
        self._take_symbol(")")
        return column

    def _column_spec(self, declared: int) -> ColumnSpec:
        """Parse one ``name TYPE`` column declaration."""
        if declared >= MAX_COLUMN_DEFINITIONS:
            raise self._refuse(
                f"A table may declare at most {MAX_COLUMN_DEFINITIONS} columns",
                field="columns",
                value=MAX_COLUMN_DEFINITIONS,
            )
        name = self._take_name("a column name")
        type_token = self._current
        if type_token.kind is not TokenKind.NAME:
            raise self._unexpected("a column type")
        type_name = type_token.text.upper()
        if type_name not in COLUMN_TYPE_NAMES:
            raise self._refuse(
                f"Column {name!r} declares the unknown type {type_token.text!r}; the types this "
                f"dialect stores are {', '.join(sorted(COLUMN_TYPE_NAMES))}",
                field="type",
                value=type_token.text,
            )
        self._advance()
        if type_name != VECTOR_TYPE_NAME:
            return ColumnSpec(name=name, type_name=type_name)
        self._take_symbol("(")
        space = self._space_name()
        self._take_symbol(")")
        return ColumnSpec(name=name, type_name=type_name, vector_space=space)

    # --- queries -----------------------------------------------------------------------------

    def _query(self) -> Query:
        """Parse clauses in written order, keeping WITH as the write/read boundary."""
        pipeline: list[MatchClause | WithClause | UnwindClause | SubqueryClause | ProcedureCall | UpdatingClause] = []
        returned: ReturnClause | None = None
        writing = False
        while (self._current.kind is not TokenKind.END
               and not self._at_symbol(";") and not self._at_symbol("}")
               and not self._at_keyword("UNION")):
            if len(pipeline) >= MAX_CLAUSES:
                raise self._refuse(f"A query may chain at most {MAX_CLAUSES} clauses",
                                   field="clauses", value=MAX_CLAUSES)
            if returned is not None:
                raise self._refuse("RETURN ends a query, so no clause may follow it",
                                   field="clause", value=self._current.text)
            if self._at_keyword("RETURN"):
                returned = self._return_clause()
                continue
            if self._at_keyword("WITH"):
                pipeline.append(self._with_clause())
                writing = False
                continue
            if self._at_keyword("CALL"):
                if writing:
                    raise self._refuse("A WITH separates writes from a CALL subquery", field="clause")
                pipeline.append(self._call_subquery())
                continue
            if self._at_keyword("MATCH") or self._at_keyword("OPTIONAL") or self._at_keyword("UNWIND"):
                if writing:
                    raise self._refuse("A WITH separates writes from subsequent reads",
                                       field="clause", value=self._current.text)
                if self._at_keyword("UNWIND"):
                    pipeline.append(self._unwind_clause())
                else:
                    optional = self._match_keyword("OPTIONAL")
                    pipeline.append(self._match_clause(optional=optional))
                continue
            pipeline.append(self._updating_clause())
            writing = True
        updates = tuple(clause for clause in pipeline
                        if isinstance(clause, (CreateClause, MergeClause, SetClause, DeleteClause)))
        if returned is None and len(pipeline) == 1 and isinstance(pipeline[0], ProcedureCall):
            returned = ReturnClause(items=tuple(ReturnItem(expression=Variable(item.name))
                                                for item in pipeline[0].yields))
        if returned is None and not updates:
            raise self._refuse("A query that only reads must end with RETURN",
                               field="clause", value="RETURN")
        unwinds = tuple(clause for clause in pipeline if isinstance(clause, UnwindClause))
        matches = tuple(clause for clause in pipeline if isinstance(clause, MatchClause))
        projections = tuple(clause for clause in pipeline if isinstance(clause, WithClause))
        read_order = tuple("match" if isinstance(clause, MatchClause) else "with"
                           for clause in pipeline if isinstance(clause, (MatchClause, WithClause)))
        query = Query(
            unwind_clause=unwinds[0] if unwinds else None,
            match_clauses=matches, with_clauses=projections,
            updating_clauses=updates, return_clause=returned,
        )
        # Store order only when it conveys information absent from the grouped
        # fields. All queries still execute through ordered_clauses().
        if query.ordered_clauses() != tuple(pipeline):
            query = replace(query, read_clause_order=read_order, clause_pipeline=tuple(pipeline))
        return query

    def _call_subquery(self) -> SubqueryClause | ProcedureCall:
        """Parse explicit imports and a bounded nested query, without implicit outer scope."""
        self._take_keyword("CALL")
        if not self._at_symbol("(") and not self._at_symbol("{"):
            name = self._take_name("a registered procedure name")
            while self._match_symbol("."):
                name += "." + self._take_name("a procedure name component")
            self._take_symbol("(")
            arguments = []
            if not self._at_symbol(")"):
                while True:
                    if len(arguments) >= 32:
                        raise self._refuse("Too many procedure arguments", field="arguments")
                    arguments.append(self._expression())
                    if not self._match_symbol(","):
                        break
            self._take_symbol(")")
            self._take_keyword("YIELD")
            yielded = []
            while True:
                if len(yielded) >= MAX_PROJECTION_ITEMS:
                    raise self._refuse("Too many YIELD columns", field="yields")
                column = self._take_name("a procedure output column")
                alias = self._take_name("a YIELD alias") if self._match_keyword("AS") else None
                yielded.append(ReturnItem(expression=Variable(column), alias=alias))
                if not self._match_symbol(","):
                    break
            predicate = self._expression() if self._match_keyword("WHERE") else None
            return ProcedureCall(name, tuple(arguments), tuple(yielded), predicate)
        imports: list[str] = []
        if self._match_symbol("("):
            if not self._at_symbol(")"):
                while True:
                    if len(imports) >= MAX_PROJECTION_ITEMS:
                        raise self._refuse("Too many imported variables", field="imports")
                    imports.append(self._take_name("an imported variable"))
                    if not self._match_symbol(","):
                        break
            self._take_symbol(")")
        self._take_symbol("{")
        self._descend()
        try:
            query: Query | UnionQuery = self._query()
            if self._at_keyword("UNION"):
                query = self._union(query)
        finally:
            self._ascend()
        self._take_symbol("}")
        return SubqueryClause(query=query, imports=tuple(imports))

    def _unwind_clause(self) -> UnwindClause:
        """Parse ``UNWIND <expression> AS <alias>``."""
        self._take_keyword("UNWIND")
        expression = self._expression()
        self._take_keyword("AS")
        alias = self._take_name("an alias")
        return UnwindClause(expression=expression, alias=alias)

    def _with_clause(self) -> WithClause:
        """Parse a projection with DISTINCT, ordering, row window and a scoped WHERE."""
        self._take_keyword("WITH")
        distinct = self._match_keyword("DISTINCT")
        items: list[ReturnItem] = []
        while True:
            if len(items) >= MAX_PROJECTION_ITEMS:
                raise self._refuse(
                    f"A WITH clause may project at most {MAX_PROJECTION_ITEMS} items",
                    field="items",
                    value=MAX_PROJECTION_ITEMS,
                )
            items.append(self._return_item())
            if not self._match_symbol(","):
                break
        sort_items = self._order_by() if self._at_keyword("ORDER") else ()
        skip = self._expression() if self._match_keyword("SKIP") else None
        limit = self._expression() if self._match_keyword("LIMIT") else None
        predicate: Expression | None = None
        if self._match_keyword("WHERE"):
            predicate = self._expression()
        return WithClause(items=tuple(items), predicate=predicate, distinct=distinct,
                          sort_items=sort_items, skip=skip, limit=limit)

    def _updating_clause(self) -> UpdatingClause:
        """Parse one clause that writes."""
        if self._at_keyword("CREATE"):
            self._advance()
            return CreateClause(patterns=self._pattern_list())
        if self._at_keyword("MERGE"):
            self._advance()
            return MergeClause(pattern=self._pattern())
        if self._at_keyword("SET"):
            return self._set_clause()
        if self._at_keyword("DETACH") or self._at_keyword("DELETE"):
            return self._delete_clause()
        raise self._refuse(f"Unsupported query clause {self._current.text.upper()}",
                           field="clause", value=self._current.text.upper())

    def _match_clause(self, *, optional: bool = False) -> MatchClause:
        """Parse ``[OPTIONAL] MATCH patterns [WHERE predicate]``.

        The caller has already consumed ``OPTIONAL`` when there was one, because whether that
        word may appear at all is an ordering question and the clause loop is where ordering is
        decided.
        """
        self._take_keyword("MATCH")
        patterns = self._pattern_list(named=True)
        predicate: Expression | None = None
        if self._match_keyword("WHERE"):
            # The predicate belongs to the clause, optional or not. For an OPTIONAL MATCH that
            # is the whole difference between "matched nothing" and "matched and then filtered
            # everything away": both are no rows, and both take the null extension.
            predicate = self._expression()
        return MatchClause(patterns=patterns, predicate=predicate, optional=optional)

    def _set_clause(self) -> SetClause:
        """Parse ``SET n.property = expression [, ...]``."""
        self._take_keyword("SET")
        items: list[SetItem] = []
        while True:
            # The target is read at property precedence, not at expression precedence: reading
            # it as a full expression would swallow the "=" as a comparison and the assignment
            # would arrive here as a boolean test that had already consumed its own value.
            target = self._postfix()
            if not isinstance(target, Property):
                raise self._refuse(
                    "SET assigns to a property, as in SET n.age = 31",
                    field="target",
                    value=target.describe(),
                )
            self._take_symbol("=")
            items.append(SetItem(target=target, value=self._expression()))
            if not self._match_symbol(","):
                break
        return SetClause(items=tuple(items))

    def _delete_clause(self) -> DeleteClause:
        """Parse ``[DETACH] DELETE variable [, ...]``."""
        detach = self._match_keyword("DETACH")
        self._take_keyword("DELETE")
        targets: list[Variable] = []
        while True:
            name = self._take_name("a variable to delete")
            targets.append(Variable(name=name))
            if not self._match_symbol(","):
                break
        return DeleteClause(targets=tuple(targets), detach=detach)

    def _return_clause(self) -> ReturnClause:
        """Parse ``RETURN [DISTINCT] items [ORDER BY keys] [SKIP n] [LIMIT n]``."""
        self._take_keyword("RETURN")
        distinct = self._match_keyword("DISTINCT")
        items: list[ReturnItem] = []
        while True:
            if len(items) >= MAX_PROJECTION_ITEMS:
                raise self._refuse(
                    f"A RETURN clause may project at most {MAX_PROJECTION_ITEMS} items",
                    field="items",
                    value=MAX_PROJECTION_ITEMS,
                )
            items.append(self._return_item())
            if not self._match_symbol(","):
                break
        sort_items: tuple[SortItem, ...] = ()
        if self._at_keyword("ORDER"):
            sort_items = self._order_by()
        skip = self._expression() if self._match_keyword("SKIP") else None
        limit = self._expression() if self._match_keyword("LIMIT") else None
        return ReturnClause(
            items=tuple(items),
            distinct=distinct,
            sort_items=sort_items,
            skip=skip,
            limit=limit,
        )

    def _return_item(self) -> ReturnItem:
        """Parse one projected item and its optional alias."""
        start = self._current.offset
        expression = self._expression()
        end = self._tokens[self._position - 1].end_offset
        assert end is not None
        source_text = self._source[start:end]
        alias: str | None = None
        if self._match_keyword("AS"):
            alias = self._take_name("an alias")
        return ReturnItem(expression=expression, alias=alias, source_text=source_text)

    def _order_by(self) -> tuple[SortItem, ...]:
        """Parse ``ORDER BY key [ASC|DESC] [, ...]``."""
        self._take_keyword("ORDER")
        self._take_keyword("BY")
        keys: list[SortItem] = []
        while True:
            if len(keys) >= MAX_SORT_KEYS:
                raise self._refuse(
                    f"An ORDER BY clause may carry at most {MAX_SORT_KEYS} keys",
                    field="sort_items",
                    value=MAX_SORT_KEYS,
                )
            expression = self._expression()
            descending = False
            if self._match_keyword("DESC") or self._match_keyword("DESCENDING"):
                descending = True
            elif self._match_keyword("ASC") or self._match_keyword("ASCENDING"):
                descending = False
            keys.append(SortItem(expression=expression, descending=descending))
            if not self._match_symbol(","):
                break
        return tuple(keys)

    # --- patterns ----------------------------------------------------------------------------

    def _pattern_list(self, *, named: bool = False) -> tuple[PatternPath, ...]:
        """Parse one or more comma-separated patterns."""
        patterns: list[PatternPath] = []
        while True:
            if len(patterns) >= MAX_PATTERNS_PER_CLAUSE:
                raise self._refuse(
                    f"A clause may carry at most {MAX_PATTERNS_PER_CLAUSE} patterns",
                    field="patterns",
                    value=MAX_PATTERNS_PER_CLAUSE,
                )
            patterns.append(self._pattern(named=named))
            if not self._match_symbol(","):
                break
        return tuple(patterns)

    def _pattern(self, *, named: bool = False) -> PatternPath:
        """Parse one connected path of nodes and relationships.

        ``named`` is true only where a MATCH is being read. A path name is a way of REFERRING
        to what was matched, and nothing else in this subset reads one, so accepting it where
        a pattern is written rather than matched would invent a meaning for it.
        """
        variable: str | None = None
        if named and self._current.kind is TokenKind.NAME and self._at_symbol("=", 1):
            variable = self._take_name("a path variable")
            self._take_symbol("=")
        nodes = [self._node_pattern()]
        relationships: list[RelationshipPattern] = []
        while self._at_symbol("-") or self._at_symbol("<"):
            if len(nodes) + len(relationships) >= MAX_PATTERN_ELEMENTS:
                raise self._refuse(
                    f"A pattern may chain at most {MAX_PATTERN_ELEMENTS} elements",
                    field="pattern",
                    value=MAX_PATTERN_ELEMENTS,
                )
            relationships.append(self._relationship_pattern())
            nodes.append(self._node_pattern())
        return PatternPath(
            nodes=tuple(nodes),
            relationships=tuple(relationships),
            variable=variable,
        )

    def _node_pattern(self) -> NodePattern:
        """Parse ``(variable:Label {properties})`` with every part optional."""
        self._take_symbol("(")
        variable: str | None = None
        if self._current.kind is TokenKind.NAME:
            variable = self._take_name("a node variable")
        labels: list[str] = []
        while self._match_symbol(":"):
            labels.append(self._take_name("a node label"))
        properties = self._map_literal() if self._at_symbol("{") else None
        self._take_symbol(")")
        return NodePattern(
            variable=variable, labels=tuple(labels), properties=properties
        )

    def _relationship_pattern(self) -> RelationshipPattern:
        """Parse a relationship arrow with its optional detail and hop range."""
        incoming = self._match_symbol("<")
        self._take_symbol("-")
        variable: str | None = None
        types: tuple[str, ...] = ()
        min_hops = 1
        max_hops = 1
        properties: MapExpression | None = None
        starred = False
        if self._match_symbol("["):
            variable, types, min_hops, max_hops, properties, starred = (
                self._relationship_detail()
            )
            self._take_symbol("]")
        self._take_symbol("-")
        outgoing = self._match_symbol(">")
        if incoming and outgoing:
            raise self._refuse(
                "A relationship points one way; write it as -[]-> or <-[]- or -[]-",
                field="direction",
            )
        if incoming:
            direction = Direction.INCOMING
        elif outgoing:
            direction = Direction.OUTGOING
        else:
            direction = Direction.UNDIRECTED
        return RelationshipPattern(
            variable=variable,
            types=types,
            direction=direction,
            min_hops=min_hops,
            max_hops=max_hops,
            properties=properties,
            hop_range_written=starred,
        )

    def _relationship_detail(
        self,
    ) -> tuple[str | None, tuple[str, ...], int, int, MapExpression | None, bool]:
        """Parse the inside of ``[variable:TYPE|TYPE*1..3 {properties}]``."""
        variable: str | None = None
        if self._current.kind is TokenKind.NAME:
            variable = self._take_name("a relationship variable")
        types: list[str] = []
        if self._match_symbol(":"):
            types.append(self._take_name("a relationship type"))
            while self._match_symbol("|"):
                types.append(self._take_name("a relationship type"))
        starred = self._at_symbol("*")
        min_hops, max_hops = self._hop_range() if starred else (1, 1)
        properties = self._map_literal() if self._at_symbol("{") else None
        return variable, tuple(types), min_hops, max_hops, properties, starred

    def _hop_range(self) -> tuple[int, int]:
        """Parse ``*``, ``*n``, ``*n..``, ``*..`` or ``*n..m``, always ending with a bound.

        A traversal is never unbounded here: a bare ``*`` over a cyclic graph is the shape that
        does not terminate, and CONTRACT.md section 13 item 3 makes a hang a blocking defect. An
        OMITTED upper bound is not unbounded, though -- the public endpoint rewrites it to
        twenty hops before any engine sees it, so the omission already has one meaning, and this
        reads it the same way instead of refusing text the caller was told is legal.

        The lower bound written beside an omitted upper is kept: ``*3..`` means three to twenty,
        and if the lower is larger than the default the range is empty and refused below, which
        is the same answer ``*3..2`` gets.
        """
        self._take_symbol("*")
        lower: int | None = None
        if self._current.kind is TokenKind.INTEGER:
            lower = self._hop_count()
        if self._match_symbol(".."):
            if self._current.kind is TokenKind.INTEGER:
                upper = self._hop_count()
            else:
                upper = DEFAULT_TRAVERSAL_HOPS
            lower = 1 if lower is None else lower
        elif lower is not None:
            upper = lower
        else:
            lower, upper = 1, DEFAULT_TRAVERSAL_HOPS
        if lower > upper:
            raise self._refuse(
                f"A hop range starts at or below its upper bound; got {lower}..{upper}",
                field="min_hops",
                value=lower,
            )
        if lower == 0:
            # openCypher gives `*0..k` the start node itself as a zero-length path. This dialect's
            # subset starts at one hop (`*1..3`), and the executor walks from depth 1 -- so an
            # accepted zero was answered as `*1..k`, a third answer that is neither dialect's.
            # Refusing at the door is the honest one of the two acceptable outcomes.
            raise self._refuse(
                "A variable-length relationship starts at one hop in this dialect; zero-length "
                f"paths (*0..{upper}) are not supported",
                field="min_hops",
                value=0,
            )
        return lower, upper

    def _hop_count(self) -> int:
        """Consume one hop count, refusing one beyond the traversal ceiling."""
        token = self._current
        count = int(token.value) if isinstance(token.value, int) else 0
        if count > MAX_TRAVERSAL_HOPS:
            raise self._refuse(
                f"A variable-length relationship may span at most {MAX_TRAVERSAL_HOPS} hops; "
                f"got {count}",
                field="hops",
                value=count,
            )
        self._advance()
        return count

    # --- expressions -------------------------------------------------------------------------

    def _expression(self) -> Expression:
        """Parse one expression at the lowest precedence."""
        return self._binary(PRECEDENCE_OR)

    def _binary(self, minimum: int) -> Expression:
        """Parse an expression whose operators all bind at least as tightly as the minimum."""
        left = self._prefix()
        advanced_compared = False
        while True:
            if self._at_keyword("IS") and PRECEDENCE_NULL >= minimum:
                left = self._null_check(left)
                continue
            operator = self._peek_operator()
            if operator is None or operator.precedence < minimum:
                return left
            if operator.precedence == PRECEDENCE_COMPARISON:
                left = self._comparison_chain(left)
                continue
            if operator.text in ("IN", "CONTAINS", "STARTS WITH", "ENDS WITH"):
                if advanced_compared:
                    raise self._refuse(
                        "Only equality and ordering comparisons form an unparenthesized chain",
                        field="operator", value=operator.text,
                    )
                advanced_compared = True
            for _ in range(operator.tokens):
                self._advance()
            following = (
                operator.precedence
                if operator.right_associative
                else operator.precedence + 1
            )
            self._descend()
            right = self._binary(following)
            self._ascend()
            left = BinaryOperation(operator=operator.text, left=left, right=right)

    def _comparison_chain(self, first: Expression) -> Expression:
        """Lower adjacent comparisons to conjunctions, as specified by the reference.

        Reuse operand nodes, not evaluated values: ``a < b <= c`` has exactly the
        existing ``a < b AND b <= c`` contract, including three-valued AND.
        Explicit parentheses create a separate predicand and are never flattened.
        """
        previous = first
        combined: Expression | None = None
        while True:
            operator = self._peek_operator()
            if operator is None or operator.precedence != PRECEDENCE_COMPARISON:
                assert combined is not None
                return combined
            self._advance()
            self._descend()
            following = self._binary(PRECEDENCE_COMPARISON + 1)
            self._ascend()
            comparison = BinaryOperation(operator=operator.text, left=previous, right=following)
            combined = comparison if combined is None else BinaryOperation(
                operator="AND", left=combined, right=comparison,
            )
            previous = following

    def _null_check(self, operand: Expression) -> NullCheck:
        """Parse the ``IS NULL`` and ``IS NOT NULL`` tests that follow an expression."""
        self._take_keyword("IS")
        negated = self._match_keyword("NOT")
        self._take_keyword("NULL")
        return NullCheck(operand=operand, negated=negated)

    def _peek_operator(self) -> _Operator | None:
        """Return the infix operator under the cursor, or None when there is not one."""
        token = self._current
        if token.kind is TokenKind.SYMBOL:
            if token.text in _COMPARISONS:
                text = "<>" if token.text == "!=" else token.text
                return _Operator(text=text, precedence=PRECEDENCE_COMPARISON, tokens=1)
            if token.text in _SUM_SYMBOLS:
                return _Operator(text=token.text, precedence=PRECEDENCE_SUM, tokens=1)
            if token.text in _PRODUCT_SYMBOLS:
                return _Operator(
                    text=token.text, precedence=PRECEDENCE_PRODUCT, tokens=1
                )
            if token.text == "^":
                return _Operator(
                    text="^",
                    precedence=PRECEDENCE_POWER,
                    tokens=1,
                    right_associative=False,
                )
            return None
        if token.kind is not TokenKind.NAME or token.quoted:
            return None
        word = token.text.upper()
        if word == "OR":
            return _Operator(text="OR", precedence=PRECEDENCE_OR, tokens=1)
        if word == "XOR":
            return _Operator(text="XOR", precedence=PRECEDENCE_XOR, tokens=1)
        if word == "AND":
            return _Operator(text="AND", precedence=PRECEDENCE_AND, tokens=1)
        if word == "IN":
            return _Operator(text="IN", precedence=PRECEDENCE_NULL, tokens=1)
        if word == "CONTAINS":
            return _Operator(
                text="CONTAINS", precedence=PRECEDENCE_NULL, tokens=1
            )
        if word == "STARTS" and self._at_keyword("WITH", 1):
            return _Operator(
                text="STARTS WITH", precedence=PRECEDENCE_NULL, tokens=2
            )
        if word == "ENDS" and self._at_keyword("WITH", 1):
            return _Operator(
                text="ENDS WITH", precedence=PRECEDENCE_NULL, tokens=2
            )
        return None

    def _prefix(self) -> Expression:
        """Parse a prefix operator, or fall through to a postfix expression."""
        if self._at_keyword("NOT"):
            self._advance()
            self._descend()
            operand = self._binary(PRECEDENCE_NOT)
            self._ascend()
            return UnaryOperation(operator="NOT", operand=operand)
        if self._at_symbol("-") or self._at_symbol("+"):
            operator = self._advance().text
            folded = self._signed_literal(operator)
            if folded is not None:
                return folded
            self._descend()
            operand = self._binary(PRECEDENCE_POWER + 1)
            self._ascend()
            return UnaryOperation(operator=operator, operand=operand)
        return self._postfix()

    def _signed_literal(self, operator: str) -> Literal | None:
        """Return the literal a sign directly in front of a number denotes, or None.

        Folding here rather than after the operand is parsed is what makes the most negative
        64-bit integer writable at all: its magnitude is one past the positive range, so it
        exists only as a sign applied to a literal and the range check has to see the signed
        value. Signs bind more tightly than exponentiation under the fixed language
        reference, so ``-2^2`` denotes the square of minus two.
        """
        if self._current.kind not in (TokenKind.INTEGER, TokenKind.DOUBLE):
            return None
        token = self._advance()
        magnitude = token.value
        if not isinstance(magnitude, (int, float)) or isinstance(magnitude, bool):
            return None
        signed = -magnitude if operator == "-" else magnitude
        return Literal(value=self._require_storable_number(signed))

    def _require_storable_number(self, number: int | float) -> int | float:
        """Return the number when the value system can store it, else refuse it."""
        if isinstance(number, int) and not INT64_MIN <= number <= INT64_MAX:
            raise self._refuse(
                f"The literal {number} is outside the range a 64-bit integer can hold",
                field="integer",
                value=str(number),
            )
        return number

    def _postfix(self) -> Expression:
        """Parse an atom followed by any number of property accesses or list extracts."""
        expression = self._atom()
        while True:
            if self._at_symbol(".") and self._peek().kind is TokenKind.NAME:
                self._advance()
                key = self._advance().text
                expression = Property(subject=expression, key=key)
                continue
            if self._at_symbol("["):
                self._advance()
                self._descend()
                index = None if self._at_symbol("..") else self._expression()
                if self._at_symbol(".."):
                    self._advance()
                    end = None if self._at_symbol("]") else self._expression()
                    self._ascend()
                    self._take_symbol("]")
                    expression = ListSlice(subject=expression, start=index, end=end)
                    continue
                self._ascend()
                self._take_symbol("]")
                assert index is not None
                expression = Subscript(subject=expression, index=index)
                continue
            return expression

    def _atom(self) -> Expression:
        """Parse the smallest complete expression: a literal, a name, a call or a grouping."""
        token = self._current
        if token.kind is TokenKind.INTEGER:
            self._advance()
            return Literal(value=self._require_storable_number(int(token.value or 0)))
        if token.kind is TokenKind.DOUBLE:
            self._advance()
            return Literal(value=float(token.value or 0.0))
        if token.kind is TokenKind.STRING:
            self._advance()
            return Literal(value=str(token.value))
        if token.kind is TokenKind.PARAMETER:
            self._advance()
            return Parameter(name=str(token.value))
        if self._at_symbol("("):
            self._advance()
            self._descend()
            inner = self._expression()
            self._ascend()
            self._take_symbol(")")
            return inner
        if self._at_symbol("["):
            return self._list_literal()
        if self._at_symbol("{"):
            return self._map_literal()
        if self._at_keyword("CASE"):
            return self._case_expression()
        if token.kind is TokenKind.NAME:
            return self._name_expression(token)
        raise self._unexpected("an expression")

    def _case_expression(self) -> CaseExpression:
        """Parse searched and simple CASE, each with one or more WHEN alternatives."""
        self._take_keyword("CASE")
        operand: Expression | None = None
        if not self._at_keyword("WHEN"):
            self._descend()
            operand = self._expression()
            self._ascend()
        alternatives: list[CaseAlternative] = []
        while self._match_keyword("WHEN"):
            if len(alternatives) >= MAX_LIST_ELEMENTS:
                message = f"A CASE expression may carry at most {MAX_LIST_ELEMENTS} alternatives"
                raise self._refuse(
                    message,
                    field="alternatives",
                    value=MAX_LIST_ELEMENTS,
                )
            self._descend()
            condition = self._expression()
            self._ascend()
            self._take_keyword("THEN")
            self._descend()
            result = self._expression()
            self._ascend()
            alternatives.append(CaseAlternative(condition=condition, result=result))
        if not alternatives:
            wanted = "WHEN in a CASE expression"
            raise self._unexpected(wanted)
        fallback: Expression | None = None
        if self._match_keyword("ELSE"):
            self._descend()
            fallback = self._expression()
            self._ascend()
        self._take_keyword("END")
        return CaseExpression(
            operand=operand,
            alternatives=tuple(alternatives),
            fallback=fallback,
        )

    def _name_expression(self, token: Token) -> Expression:
        """Parse a name, which may be a constant, a function call or a variable."""
        if not token.text:
            raise self._unexpected("a non-empty variable or function name")
        if not token.quoted:
            word = token.text.upper()
            if word == "TRUE":
                self._advance()
                return Literal(value=True)
            if word == "FALSE":
                self._advance()
                return Literal(value=False)
            if word == "NULL":
                self._advance()
                return Literal(value=None)
        if self._at_symbol("(", 1):
            return self._function_call()
        self._advance()
        return Variable(name=token.text)

    def _function_call(self) -> Expression:
        """Parse ``name(arguments)``, including ``count(*)`` and ``count(DISTINCT x)``."""
        token = self._advance()
        name = token.text
        self._take_symbol("(")
        if name.upper() in ("ALL", "ANY", "NONE", "SINGLE", "REDUCE"):
            self._descend()
            accumulator = None
            initial = None
            if name.upper() == "REDUCE":
                accumulator = self._take_name("a reduction accumulator")
                self._take_symbol("=")
                initial = self._expression()
                self._take_symbol(",")
            variable = self._take_name("a list variable")
            self._take_keyword("IN")
            source = self._expression()
            if name.upper() == "REDUCE":
                self._take_symbol("|")
            else:
                self._take_keyword("WHERE")
            body = self._expression()
            self._take_symbol(")")
            self._ascend()
            return ListIteration(name.lower(), variable, source, body,
                                 accumulator=accumulator, initial=initial)
        if self._at_symbol("*"):
            self._advance()
            self._take_symbol(")")
            if name.upper() not in AGGREGATE_FUNCTIONS:
                raise self._refuse(
                    f"Only an aggregate is written with a star, and {name!r} is not one",
                    field="function",
                    value=name,
                )
            return FunctionCall(name=name, star=True)
        distinct = self._match_keyword("DISTINCT")
        if distinct and name.upper() not in AGGREGATE_FUNCTIONS:
            raise self._refuse(
                f"Only an aggregate takes DISTINCT, and {name!r} is not one",
                field="function",
                value=name,
            )
        arguments: list[Expression] = []
        named: list[NamedArgument] = []
        if not self._at_symbol(")"):
            while True:
                if len(arguments) + len(named) >= MAX_LIST_ELEMENTS:
                    raise self._refuse(
                        f"A call may take at most {MAX_LIST_ELEMENTS} arguments",
                        field="arguments",
                        value=MAX_LIST_ELEMENTS,
                    )
                if (
                    self._current.kind is TokenKind.NAME
                    and not self._current.quoted
                    and self._at_symbol("=>", 1)
                ):
                    argument_name = self._take_name("an argument name")
                    self._advance()
                    self._descend()
                    named.append(
                        NamedArgument(name=argument_name, value=self._expression())
                    )
                    self._ascend()
                elif named:
                    raise self._refuse(
                        "Every argument after a named one must be named too",
                        field="arguments",
                        value=self._current.text,
                    )
                else:
                    self._descend()
                    arguments.append(self._expression())
                    self._ascend()
                if not self._match_symbol(","):
                    break
        self._take_symbol(")")
        return FunctionCall(
            name=name,
            arguments=tuple(arguments),
            named_arguments=tuple(named),
            distinct=distinct,
            occurrence=token.offset if name.upper() in NONDETERMINISTIC_SCALARS else None,
        )

    def _list_literal(self) -> Expression:
        """Parse ``[element, ...]``."""
        self._take_symbol("[")
        if self._current.kind is TokenKind.NAME and self._at_keyword("IN", 1):
            self._descend()
            variable = self._take_name("a list variable")
            self._take_keyword("IN")
            source = self._expression()
            predicate = self._expression() if self._match_keyword("WHERE") else None
            body = self._expression() if self._match_symbol("|") else Variable(variable)
            self._take_symbol("]")
            self._ascend()
            return ListIteration("map", variable, source, body, predicate=predicate)
        elements: list[Expression] = []
        if not self._at_symbol("]"):
            while True:
                if len(elements) >= MAX_LIST_ELEMENTS:
                    raise self._refuse(
                        f"A list may hold at most {MAX_LIST_ELEMENTS} elements",
                        field="elements",
                        value=MAX_LIST_ELEMENTS,
                    )
                self._descend()
                elements.append(self._expression())
                self._ascend()
                if not self._match_symbol(","):
                    break
        self._take_symbol("]")
        return ListExpression(elements=tuple(elements))

    def _map_literal(self) -> MapExpression:
        """Parse ``{key: value, ...}``, refusing a key written twice."""
        self._take_symbol("{")
        entries: list[MapEntry] = []
        seen: set[str] = set()
        if not self._at_symbol("}"):
            while True:
                if len(entries) >= MAX_MAP_ENTRIES:
                    raise self._refuse(
                        f"A map may hold at most {MAX_MAP_ENTRIES} entries",
                        field="entries",
                        value=MAX_MAP_ENTRIES,
                    )
                key = self._take_name("a map key", allow_empty=True)
                if key in seen:
                    raise self._refuse(
                        f"The map key {key!r} is written more than once",
                        field="key",
                        value=key,
                    )
                seen.add(key)
                self._take_symbol(":")
                self._descend()
                entries.append(MapEntry(key=key, value=self._expression()))
                self._ascend()
                if not self._match_symbol(","):
                    break
        self._take_symbol("}")
        return MapExpression(entries=tuple(entries))
