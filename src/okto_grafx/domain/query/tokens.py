"""The lexical vocabulary of the supported Cypher subset (CONTRACT.md section 8.9, D3).

A token is a value: what kind of thing it is, the exact text it was cut from, the decoded value
when there is one, and where in the query it started. The position is not decoration -- a parse
failure that cannot point at a character is a failure a caller has to bisect by hand, and this
engine is embedded in someone else's application where that is expensive.

Keywords are NOT a separate kind. The dialect is case-insensitive for keywords and
case-sensitive for labels and properties, and it lets a property be called ``count`` or ``order``
without ceremony; so the lexer emits every unquoted word as a :attr:`TokenKind.NAME` and the
parser asks whether a name reads as a particular keyword. A back-quoted name carries
``quoted=True`` and can never read as one, which is exactly what back quotes are for.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "AGGREGATE_FUNCTIONS",
    "COALESCE_FUNCTION",
    "KEYWORDS",
    "LABEL_FUNCTION",
    "SIZE_FUNCTION",
    "SIMILARITY_FUNCTION",
    "SIMILARITY_SCORE_FUNCTION",
    "STRING_SPLIT_FUNCTION",
    "SYMBOLS",
    "TIMESTAMP_FUNCTION",
    "Token",
    "TokenKind",
]


class TokenKind(str, Enum):
    """The kinds of token the lexer produces."""

    NAME = "name"
    INTEGER = "integer"
    DOUBLE = "double"
    STRING = "string"
    PARAMETER = "parameter"
    SYMBOL = "symbol"
    END = "end"


@dataclass(frozen=True, slots=True)
class Token:
    """One lexical unit, with where it came from and what it decodes to."""

    kind: TokenKind
    text: str
    offset: int
    line: int
    column: int
    value: object = None
    quoted: bool = False

    @property
    def upper(self) -> str:
        """Return the text folded to upper case, which is how a keyword is compared."""
        return self.text.upper()

    def reads_as(self, keyword: str) -> bool:
        """Return True when this token is an unquoted word spelling that keyword."""
        return (
            self.kind is TokenKind.NAME
            and not self.quoted
            and self.text.upper() == keyword
        )

    def describe(self) -> str:
        """Return a short en-US description of this token for a failure message."""
        if self.kind is TokenKind.END:
            return "the end of the query"
        return f"{self.kind.value} {self.text!r}"


SYMBOLS: tuple[str, ...] = (
    "=>",
    "<>",
    "!=",
    "<=",
    ">=",
    "..",
    "(",
    ")",
    "{",
    "}",
    "[",
    "]",
    ",",
    ".",
    ":",
    ";",
    "|",
    "=",
    "<",
    ">",
    "+",
    "-",
    "*",
    "/",
    "%",
    "^",
)
"""Every punctuation symbol of the dialect, longest first so the scanner is unambiguous.

Order is load-bearing: ``<=`` must be tried before ``<`` or a comparison would be cut into two
tokens and the query would parse as something else. A test asserts the tuple is sorted by
descending length for exactly that reason.
"""

KEYWORDS: frozenset[str] = frozenset(
    {
        "AND",
        "AS",
        "ASC",
        "ASCENDING",
        "BY",
        "CASE",
        "CONTAINS",
        "CREATE",
        "DELETE",
        "DESC",
        "DESCENDING",
        "DETACH",
        "DISTINCT",
        "ENDS",
        "ELSE",
        "END",
        "FALSE",
        "FROM",
        "IN",
        "IS",
        "KEY",
        "LIMIT",
        "MATCH",
        "MERGE",
        "NODE",
        "NOT",
        "NULL",
        "OR",
        "ORDER",
        "PRIMARY",
        "REL",
        "RETURN",
        "SET",
        "SKIP",
        "SPACE",
        "STARTS",
        "TABLE",
        "THEN",
        "TO",
        "TRUE",
        "VECTOR",
        "WHERE",
        "WHEN",
        "WITH",
        "XOR",
    }
)
"""Every word the grammar gives a meaning to.

The set is not a reservation list -- a property may still be called ``key`` -- it is what the
parser recognises, and it is pinned by a test so a keyword cannot be added or dropped without
one. ``WITH`` is present although no clause implements it: the subset CONTRACT.md section 8.9
freezes does not include it, and a query using it must be refused with a message that names it
rather than with a puzzling failure about an unexpected word.
"""

AGGREGATE_FUNCTIONS: frozenset[str] = frozenset(
    {"COUNT", "SUM", "AVG", "MIN", "MAX", "COLLECT"}
)
"""The six aggregates CONTRACT.md section 8.9 freezes, compared case-insensitively."""

COALESCE_FUNCTION: str = "COALESCE"
"""The null-selection function required by the Pulse query contract 1.0."""

LABEL_FUNCTION: str = "LABEL"
"""The table a matched node or relationship came from, as Pulse contract 1.0 asks."""

SIZE_FUNCTION: str = "SIZE"
"""The collection and string cardinality function required by the Pulse query contract 1.0."""

SIMILARITY_FUNCTION: str = "SIMILARITY"
"""The similarity extension of SPEC-VEC FR-4, which puts the search in the query language."""

SIMILARITY_SCORE_FUNCTION: str = "SIMILARITY_SCORE"
"""The projection of the score the similarity operator produced for the current row."""

STRING_SPLIT_FUNCTION: str = "STRING_SPLIT"
"""The text partitioning function required by the Pulse query contract 1.0."""

TIMESTAMP_FUNCTION: str = "TIMESTAMP"
"""An ISO-8601 reading normalized to UTC microseconds, as Pulse contract 1.0 asks."""
