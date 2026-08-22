"""A seeded fuzz corpus over the whole language surface (CONTRACT.md section 11 item 5).

The property under test is one sentence: **for any string at all, ``parse`` either returns a
statement or raises ``GrafxParseError``, and it always returns.** A parser is the single most
likely place in an embedded database for a raw ``IndexError``, ``RecursionError``, ``ValueError``
or ``struct.error`` to escape on hostile input, and the caller of this door is often one layer
away from a network.

Four generators feed it, and each one exists because it produces a different failure shape:

* **random characters** from an alphabet weighted towards the punctuation of the dialect, which
  finds unterminated literals and symbols the scanner has no rule for;
* **mutations of valid queries** -- delete, duplicate, replace, transpose -- which keep enough
  structure to reach deep into the grammar before going wrong;
* **every truncation of every valid query**, which is the shape that finds a lookahead past the
  end of the token stream;
* **structured attacks**: nesting, repetition, enormous numbers and long literals, which are the
  shapes that exhaust a resource rather than confuse a rule.

Randomness comes from the seeded generator the domain already owns, so a failing case is
reproducible from the seed printed in the assertion rather than from a stored corpus file
(G2b, amendment A5).
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxError, GrafxParseError
from okto_grafx.domain.query.analysis import analyze
from okto_grafx.domain.query.ast import Statement
from okto_grafx.domain.query.limits import MAX_QUERY_CHARACTERS
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.planner import build_plan
from okto_grafx.domain.rand import SplitMix64
from tests.query.conftest import build_catalog, build_indexes

ASCII_ALPHABET: str = (
    "()[]{}<>-=*+/%^,.:;|$'\"`_"
    + "\t\n "
    + "0123456789"
    + "abcdefghijklmnopqrstuvwxyz"
    + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    + "\\!?#@&~"
)
"""The punctuation, digits and letters of the dialect, where most of the grammar lives."""

BEYOND_ASCII: str = "".join(
    chr(code_point)
    for code_point in (
        0x00A0,  # a space that is not one, and a classic reason a scanner loops
        0x00E9,  # an accented letter, a name character under no ASCII rule
        0x00DF,  # a letter whose upper case is two characters, so folding changes length
        0x0301,  # a combining mark, a character with no width of its own
        0x200B,  # a zero width space
        0x200F,  # a right-to-left mark: invisible and directional
        0x2028,  # a line separator that str.splitlines honours and a scanner must not
        0x2029,  # a paragraph separator, the same shape
        0x3000,  # an ideographic space
        0x4E2D,  # a CJK ideograph, which isalpha() answers True for
        0xFEFF,  # a byte order mark in the middle of the text
        0xFF08,  # a full-width parenthesis, which looks like syntax and is not
        0x1F600,  # an astral-plane character: two UTF-16 units, one code point
        0x1D7CE,  # an astral-plane DIGIT, which isdigit() answers True for
        0x10FFFF,  # the last code point there is
        0x0000,  # the null character, which a C library treats as an end
        0xD800,  # a lone surrogate, which has no UTF-8 encoding at all
    )
)
"""Characters outside the ASCII plane, each chosen for a rule it can break.

A corpus that shares an alphabet with its assertions cannot find a defect that only appears
outside it. This dialect accepts any character inside a string literal and inside back quotes,
so an ASCII-only corpus would never reach the code that decides what to do with the rest --
and two of the entries here, the astral-plane digit and the lone surrogate, are exactly the
shapes that turn a plausible-looking isdigit() or encode() into a non-Grafx escape.
"""

ALPHABET: str = ASCII_ALPHABET + BEYOND_ASCII
"""Characters the random generator draws from: the dialect, plus what lies outside it."""

WORDS: tuple[str, ...] = (
    "MATCH",
    "RETURN",
    "WHERE",
    "CREATE",
    "MERGE",
    "SET",
    "DELETE",
    "DETACH",
    "ORDER",
    "BY",
    "SKIP",
    "LIMIT",
    "DISTINCT",
    "AS",
    "AND",
    "OR",
    "XOR",
    "NOT",
    "IS",
    "NULL",
    "TRUE",
    "FALSE",
    "IN",
    "STARTS",
    "ENDS",
    "WITH",
    "CONTAINS",
    "NODE",
    "REL",
    "TABLE",
    "VECTOR",
    "SPACE",
    "PRIMARY",
    "KEY",
    "FROM",
    "TO",
    "count",
    "sum",
    "avg",
    "collect",
    "similarity",
    "similarity_score",
    "Person",
    "Chunk",
    "Doc",
    "Knows",
    "BELONGS_TO",
    "minilm_v2",
    "p",
    "n",
    "id",
    "name",
    "age",
    "layer",
    "embedding",
    "$q",
    "$id",
    "1",
    "0.5",
    "'text'",
    "*1..3",
    "'" + chr(0x1F600) + "'",
    "`" + chr(0x4E2D) + "`",
    chr(0x00E9),
    chr(0xFEFF),
    "=>",
    "->",
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
    ",",
    ":",
    ".",
    "=",
    ">",
)
"""Fragments the token-level generator draws from, so a case can reach deep into the grammar."""

VALID: tuple[str, ...] = (
    "MATCH (p:Person) RETURN p.name",
    "MATCH (p:Person) WHERE p.id = $id RETURN p.name AS n ORDER BY n DESC SKIP 1 LIMIT 5",
    "MATCH (a:Person)-[:Knows*1..3]->(b:Person) WHERE a.age >= 18 RETURN DISTINCT b.name",
    "MATCH (n:Chunk)-[:BELONGS_TO]->(d:Doc) WHERE n.layer = $layer AND d.active = true "
    "AND similarity(n.embedding, $q, space => 'minilm_v2') > 0.7 "
    "RETURN n.id, similarity_score() AS score ORDER BY score DESC LIMIT 10",
    "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))",
    "CREATE REL TABLE Knows(FROM Person TO Person, since INT64)",
    "CREATE VECTOR SPACE minilm_v2 {dimension: 4, metric: 'cosine', normalized: true}",
    "CREATE (:Person {id: 1, name: 'Ada'})",
    "MATCH (p:Person) SET p.age = p.age + 1 RETURN p.age",
    "MATCH (p:Person) WHERE p.name IS NOT NULL DETACH DELETE p",
    "MERGE (p:Person {id: 7})",
    "MATCH (p:Person) RETURN count(*) AS total, p.city AS city ORDER BY total DESC",
    "RETURN 1 + 2 * 3 ^ 2 - -4 AS value",
    "MATCH (p:Person) WHERE p.name STARTS WITH 'A' OR p.age IN [1, 2, 3] RETURN p.id",
    "MATCH (p:Person) WHERE p.name = '"
    + chr(0x00E9)
    + chr(0x4E2D)
    + chr(0x1F600)
    + "' RETURN p.id",
    "MATCH (`" + chr(0x4E2D) + "`:Person) RETURN `" + chr(0x4E2D) + "`.id",
    "RETURN '" + chr(0x2028) + chr(0xFEFF) + chr(0x200B) + "' AS invisible",
)
"""Queries the mutation and truncation generators start from."""

SEEDS: tuple[int, ...] = (
    0x0C10_0000_0000_0001,
    0x0C10_0000_0000_0002,
    0x0C10_0000_0000_0003,
    0x0C10_0000_0000_0004,
)
"""The seeds the random generators run under, so every case is reproducible from its label."""

CASES_PER_SEED: int = 400


def _random_text(generator: SplitMix64, length: int) -> str:
    """Return a string of that many characters drawn from the alphabet."""
    return "".join(ALPHABET[generator.next_below(len(ALPHABET))] for _ in range(length))


def _random_words(generator: SplitMix64, count: int) -> str:
    """Return that many fragments joined by single spaces."""
    return " ".join(WORDS[generator.next_below(len(WORDS))] for _ in range(count))


def _mutate(generator: SplitMix64, text: str) -> str:
    """Return the text with one character deleted, duplicated, replaced or transposed."""
    if not text:
        return text
    position = generator.next_below(len(text))
    choice = generator.next_below(4)
    if choice == 0:
        return text[:position] + text[position + 1 :]
    if choice == 1:
        return text[:position] + text[position] + text[position:]
    if choice == 2:
        return text[:position] + ALPHABET[generator.next_below(len(ALPHABET))] + text[position + 1 :]
    if position + 1 >= len(text):
        return text
    return text[:position] + text[position + 1] + text[position] + text[position + 2 :]


def _check(text: str, label: str) -> None:
    """Parse one case and assert that nothing outside the taxonomy escaped."""
    try:
        statement = parse(text)
    except GrafxParseError:
        return
    except BaseException as failure:  # noqa: BLE001 - the whole point is to see everything
        raise AssertionError(
            f"{label} escaped the parser as {type(failure).__name__}: {failure!r}\n"
            f"text={text!r}"
        ) from failure
    assert isinstance(statement, Statement), f"{label} produced {type(statement).__name__}"


def _check_downstream(text: str, label: str) -> None:
    """Analyse and plan one case and assert that only the taxonomy escapes."""
    catalog = build_catalog()
    indexes = build_indexes()
    try:
        statement = parse(text)
    except GrafxParseError:
        return
    try:
        analysis = analyze(statement)
        build_plan(statement, catalog=catalog, indexes=indexes, analysis=analysis)
    except GrafxError:
        return
    except BaseException as failure:  # noqa: BLE001 - the whole point is to see everything
        raise AssertionError(
            f"{label} escaped the planner as {type(failure).__name__}: {failure!r}\n"
            f"text={text!r}"
        ) from failure


# --- generators -----------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_random_characters_never_escape_the_taxonomy(seed: int) -> None:
    generator = SplitMix64(seed)
    for case in range(CASES_PER_SEED):
        length = generator.next_below(120)
        text = _random_text(generator, length)
        _check(text, f"seed={seed:#x} case={case} kind=characters")


@pytest.mark.parametrize("seed", SEEDS)
def test_random_fragments_never_escape_the_taxonomy(seed: int) -> None:
    generator = SplitMix64(seed ^ 0xF00D)
    for case in range(CASES_PER_SEED):
        text = _random_words(generator, 1 + generator.next_below(24))
        _check(text, f"seed={seed:#x} case={case} kind=fragments")


@pytest.mark.parametrize("seed", SEEDS)
def test_mutated_queries_never_escape_the_taxonomy(seed: int) -> None:
    generator = SplitMix64(seed ^ 0xBEEF)
    for case in range(CASES_PER_SEED):
        text = VALID[generator.next_below(len(VALID))]
        for _ in range(1 + generator.next_below(4)):
            text = _mutate(generator, text)
        _check(text, f"seed={seed:#x} case={case} kind=mutation")


@pytest.mark.parametrize("seed", SEEDS)
def test_mutated_queries_never_escape_the_taxonomy_downstream(seed: int) -> None:
    generator = SplitMix64(seed ^ 0xCAFE)
    for case in range(CASES_PER_SEED):
        text = VALID[generator.next_below(len(VALID))]
        for _ in range(1 + generator.next_below(3)):
            text = _mutate(generator, text)
        _check_downstream(text, f"seed={seed:#x} case={case} kind=mutation-plan")


@pytest.mark.parametrize("text", VALID, ids=range(len(VALID)))
def test_every_truncation_of_a_valid_query_is_answered_in_the_taxonomy(text: str) -> None:
    for cut in range(len(text) + 1):
        _check(text[:cut], f"truncation at {cut}")


@pytest.mark.parametrize("text", VALID, ids=range(len(VALID)))
def test_every_truncation_survives_the_planner_too(text: str) -> None:
    for cut in range(len(text) + 1):
        _check_downstream(text[:cut], f"truncation at {cut}")


# --- structured attacks ---------------------------------------------------------------------


ATTACKS: tuple[tuple[str, str], ...] = (
    ("open parentheses", "RETURN " + "(" * 5000 + "1"),
    ("closed parentheses", "RETURN 1" + ")" * 5000),
    ("open brackets", "RETURN " + "[" * 5000),
    ("open braces", "RETURN " + "{" * 5000),
    ("nested negation", "RETURN " + "NOT " * 5000 + "true"),
    ("nested sign", "RETURN " + "-" * 5000 + "1"),
    ("right associative tower", "RETURN " + "2^" * 5000 + "2"),
    ("nested calls", "RETURN " + "count(" * 5000 + "1"),
    ("nested lists", "RETURN " + "[" * 3000 + "1" + "]" * 3000),
    ("nested maps", "RETURN " + "{a:" * 3000 + "1" + "}" * 3000),
    ("enormous integer", "RETURN " + "9" * 60000),
    ("enormous exponent", "RETURN 1e" + "9" * 60000),
    ("long string", "RETURN '" + "a" * 60000 + "'"),
    ("unterminated string", "RETURN '" + "a" * 60000),
    ("unterminated comment", "RETURN 1 /*" + "a" * 60000),
    ("long name", "RETURN " + "a" * 60000),
    ("long back quoted name", "RETURN `" + "a" * 60000 + "`"),
    ("many tokens", "RETURN " + "+" * 60000),
    ("many commas", "RETURN 1" + ",1" * 20000),
    ("many patterns", "MATCH " + ",".join(["(p:Person)"] * 5000) + " RETURN 1"),
    ("long pattern chain", "MATCH (a:Person)" + "-[:Knows]->(b:Person)" * 3000 + " RETURN 1"),
    ("deep property chain", "MATCH (p:Person) RETURN p" + ".a" * 20000),
    ("null bytes", "RETURN " + "\x00" * 1000),
    ("carriage returns", "RETURN 1" + "\r\n" * 20000),
    ("high code points", "RETURN " + "".join(chr(0x1F600 + index % 32) for index in range(2000))),
    ("surrogate escapes", "RETURN '" + r"\ud800" * 2000 + "'"),
    ("at the length ceiling", "RETURN " + "1 + " * ((MAX_QUERY_CHARACTERS - 8) // 4) + "1"),
    ("past the length ceiling", "x" * (MAX_QUERY_CHARACTERS + 1)),
    ("only whitespace", " \t\r\n" * 5000),
    ("only comments", "// nothing\n" * 5000),
    ("null bytes inside a string", "RETURN " + chr(39) + chr(0) * 5000 + chr(39)),
    ("a byte order mark first", chr(0xFEFF) + "RETURN 1"),
    ("an ideographic space as whitespace", "RETURN" + chr(0x3000) + "1"),
    ("full-width parentheses", "RETURN " + chr(0xFF08) + "1" + chr(0xFF09)),
    ("an astral digit as a literal", "RETURN " + chr(0x1D7CE) * 200),
    ("an astral character as a name", "RETURN " + chr(0x1F600) * 2000),
    ("an astral character inside a name", "RETURN abc" + chr(0x1F600)),
    ("a combining mark after a name", "RETURN abc" + chr(0x0301)),
    ("a right-to-left mark in a predicate", "RETURN 1 " + chr(0x200F) + "= 1"),
    ("a line separator in a string", "RETURN " + chr(39) + chr(0x2028) * 2000 + chr(39)),
    ("an astral back-quoted name", "RETURN `" + chr(0x1F600) * 200 + "`"),
    ("an unterminated astral string", "RETURN " + chr(39) + chr(0x1F600) * 20000),
    ("the last code point, repeated", "RETURN " + chr(0x10FFFF) * 2000),
    ("a lone surrogate outside a string", "RETURN " + chr(0xD800)),
)


@pytest.mark.parametrize("label,text", ATTACKS, ids=[label for label, _ in ATTACKS])
def test_a_structured_attack_is_answered_rather_than_survived(label: str, text: str) -> None:
    _check(text, f"attack {label}")


@pytest.mark.parametrize("label,text", ATTACKS, ids=[label for label, _ in ATTACKS])
def test_a_structured_attack_is_answered_by_the_planner_too(label: str, text: str) -> None:
    _check_downstream(text, f"attack {label}")


def test_the_corpus_is_large_enough_to_be_worth_something() -> None:
    # A fuzz suite that examines a handful of cases passes for the wrong reason, so the size of
    # the corpus is itself asserted.
    generated = len(SEEDS) * CASES_PER_SEED * 4
    truncations = sum(len(text) + 1 for text in VALID) * 2
    assert generated + truncations + len(ATTACKS) * 2 > 8000
