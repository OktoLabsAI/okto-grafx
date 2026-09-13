"""Source-faithful result headings, independent of normalized expression identity."""

import pytest

import okto_grafx
from okto_grafx.domain.query.parser import parse
from okto_grafx.domain.query.lexer import tokenize


@pytest.mark.parametrize("query, columns, rows", [
    ("WITH null AS l RETURN size(l), size(null)", ("size(l)", "size(null)"), ((None, None),)),
    ("WITH [123, {existing: 42, notMissing: null}] AS list "
     "RETURN (list[1]).missing, (list[1]).notMissing, (list[1]).existing",
     ("(list[1]).missing", "(list[1]).notMissing", "(list[1]).existing"), ((None, None, 42),)),
    ("WITH null AS m RETURN keys(m), keys(null)", ("keys(m)", "keys(null)"), ((None, None),)),
    ("RETURN 12 / 4 * 3 - 2 * 4", ("12 / 4 * 3 - 2 * 4",), ((1,),)),
    ("RETURN 12 / 4 * (3 - 2 * 4)", ("12 / 4 * (3 - 2 * 4)",), ((-15,),)),
    ("RETURN 'a\\n' /* trailing */ , 0XfF // tail", ("'a\\n'", "0XfF"), (("a\n", 255),)),
    ("RETURN 1 /* inside */ + 2;", ("1 /* inside */ + 2",), ((3,),)),
    ("WITH 7 AS `odd name` WITH `odd name` RETURN `odd name`", ("odd name",), ((7,),)),
    ("RETURN (1 + 2) AS `explicit name`", ("explicit name",), ((3,),)),
])
def test_original_headings_survive_execution_and_repeat(query, columns, rows):
    with okto_grafx.connect(":memory:") as db:
        for _ in range(2):
            result = db.execute(query)
            assert result.columns == columns
            assert result.rows == rows
        # Public plan cloning must support the additional presentation metadata.
        db.explain(query)


def test_normalized_ast_identity_and_describe_ignore_source_spelling():
    first = parse("RETURN 1+2")
    second = parse("RETURN 1 + 2")
    assert first == second
    assert hash(first) == hash(second)
    assert first.describe() == second.describe()
    assert first.return_clause.column_names() == ("1+2",)
    assert second.return_clause.column_names() == ("1 + 2",)


def test_cache_does_not_mix_headings_for_equivalent_expressions():
    with okto_grafx.connect(":memory:") as db:
        for text in ("1+2", "1 + 2", "(1+2)", "1+2"):
            result = db.execute("RETURN " + text)
            assert result.columns == (text,)
            assert result.rows == ((3,),)


def test_source_extents_include_escapes_but_not_trailing_comments():
    source = "RETURN $`a``b`, 'a\\u0062', `x``y`, 0o17 // ignored"
    tokens = tokenize(source)
    assert [source[t.offset:t.end_offset] for t in tokens] == [
        "RETURN", "$`a``b`", ",", "'a\\u0062'", ",", "`x``y`", ",", "0o17", "",
    ]


def test_long_expression_heading_is_not_limited_as_an_identifier():
    text = "'" + "a" * 300 + "'"
    with okto_grafx.connect(":memory:") as db:
        assert db.execute("RETURN " + text).columns == (text,)
        db.explain("RETURN " + text)


def test_union_and_subquery_preserve_unaliased_headings():
    with okto_grafx.connect(":memory:") as db:
        result = db.execute("RETURN 1+2 UNION ALL RETURN 1+2")
        assert result.columns == ("1+2",)
        assert result.rows == ((3,), (3,))
        result = db.execute("CALL { RETURN 1+2 } RETURN `1+2`")
        assert result.columns == ("1+2",)
        assert result.rows == ((3,),)
