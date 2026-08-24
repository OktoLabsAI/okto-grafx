"""Every bound the query surface enforces, declared once (CONTRACT.md section 13 item 3).

A query engine reads text a caller wrote, and in an embedded database that caller is often one
layer away from a network. Two failures are therefore not merely bugs here, they are denial of
service: work that does not terminate, and work whose size is chosen by the input. Every rule in
this module exists to make one of those impossible, and each is a hard refusal rather than a
truncation, because silently reading half a query would be the wrong-result failure instead.

The numbers are deliberately generous for real queries and deliberately far below what the
interpreter can survive. ``MAX_EXPRESSION_DEPTH`` is the one that matters most: the parser is
recursive, and CPython answers an unbounded descent with ``RecursionError``, which is not a
``Grafx*`` type and would leave a public door in violation of CONTRACT.md section 11 item 5. The
guard is a counter checked BEFORE each descent, so the refusal happens while the stack is still
shallow; there is deliberately no ``except RecursionError`` anywhere in this component, because a
second mechanism answering the same question makes both untestable (amendment A67).

Each constant is pinned by a test asserting its literal value, never a value derived from the
constant itself (amendments A56 and A68).
"""

from __future__ import annotations

__all__ = [
    "MAX_CLAUSES",
    "MAX_COLUMN_DEFINITIONS",
    "MAX_EXPRESSION_DEPTH",
    "MAX_LIST_ELEMENTS",
    "MAX_MAP_ENTRIES",
    "MAX_NAME_CHARACTERS",
    "MAX_NUMBER_CHARACTERS",
    "MAX_PARAMETERS",
    "MAX_PATTERN_ELEMENTS",
    "MAX_PATTERNS_PER_CLAUSE",
    "MAX_PROJECTION_ITEMS",
    "MAX_QUERY_CHARACTERS",
    "MAX_RENDERED_QUERY_CHARACTERS",
    "MAX_SORT_KEYS",
    "MAX_STRING_CHARACTERS",
    "MAX_TOKENS",
    "MAX_TRAVERSAL_HOPS",
]

MAX_QUERY_CHARACTERS: int = 65536
"""Characters one query may carry. Anything longer is refused before a single token is cut."""

MAX_RENDERED_QUERY_CHARACTERS: int = 1_048_576
"""Characters one query-derived display value may carry in a public plan or result.

Rendering is not bounded by the source length: ``repr`` expands one non-printable non-BMP code
point to ten ASCII characters, and operator punctuation adds a little more.  Sixteen times the
source-text ceiling therefore admits every valid query without leaving collaborator-forged
columns unbounded.  This bound applies only to derived display names; identifiers and stored
string values retain their substantially smaller limits.
"""

MAX_TOKENS: int = 8192
"""Tokens one query may produce, so a pathological but short text cannot expand without bound."""

MAX_EXPRESSION_DEPTH: int = 48
"""How deeply expressions, patterns and parentheses may nest.

The parser descends once per nesting level and CPython's own limit is about a thousand frames,
so this leaves two orders of magnitude of headroom. Real Cypher rarely passes five.
"""

MAX_CLAUSES: int = 64
"""Clauses one query may chain, counting every MATCH, CREATE, MERGE, SET, DELETE and RETURN."""

MAX_PATTERNS_PER_CLAUSE: int = 32
"""Comma-separated patterns one MATCH, CREATE or MERGE clause may carry."""

MAX_PATTERN_ELEMENTS: int = 64
"""Nodes and relationships one pattern may chain, so a path cannot be arbitrarily long."""

MAX_TRAVERSAL_HOPS: int = 30
"""The largest upper bound a variable-length relationship may declare.

The dialect requires an explicit upper bound: ``-[:Knows*1..3]->`` is expressible and a bare
``*`` is not, because an unbounded traversal over a cyclic graph is exactly the shape that does
not terminate. Thirty is the same ceiling the reference dialect applies when a query omits one.
"""

MAX_PROJECTION_ITEMS: int = 256
"""Items one RETURN clause may project."""

MAX_SORT_KEYS: int = 32
"""Keys one ORDER BY clause may carry."""

MAX_LIST_ELEMENTS: int = 1024
"""Elements one list literal may hold."""

MAX_MAP_ENTRIES: int = 256
"""Entries one map literal may hold."""

MAX_PARAMETERS: int = 256
"""Distinct parameters one query may reference."""

MAX_COLUMN_DEFINITIONS: int = 512
"""Columns one CREATE NODE TABLE or CREATE REL TABLE statement may declare."""

MAX_NAME_CHARACTERS: int = 128
"""Characters one identifier may carry, matching the schema identifier rule of the domain model."""

MAX_NUMBER_CHARACTERS: int = 40
"""Characters one numeric literal may carry.

The bound is about the conversion, not about taste. CPython refuses to build an integer from a
very long digit string and raises ``ValueError`` while doing it, and a caller can ask for that in
a few kilobytes of text; forty characters is more than any real literal and far below the point
where the conversion becomes expensive at all.
"""

MAX_STRING_CHARACTERS: int = 16384
"""Characters one string literal may carry after its escapes are resolved."""
