"""Native scalar semantics, static planning, bind-time/runtime and composition."""

import pytest
from okto_grafx import connect
from okto_grafx.errors import GrafxError


@pytest.mark.parametrize("text,expected", [
    ("9223372036854775807", 9223372036854775807),
    ("-9223372036854775808", -9223372036854775808),
    ("9223372036854775807.1", None), ("-9223372036854775808.1", None),
    ("9007199254740993.9", 9007199254740993),
    ("+000.123e2", 12), ("-.9", 0), ("1.", 1),
    (".1E+1", 1), ("0e" + "9" * 5000, 0),
    ("1e" + "9" * 5000, None), ("1e-" + "9" * 5000, 0),
    ("0" * 5000 + "12", 12), ("1" + "0" * 5000 + "e-5000", 1),
    ("١", None), ("1_000", None), ("1e", None), ("1e1e1", None),
    ("--1", None), ("+", None), (".", None), ("NaN", None),
])
def test_decimal_integer_conversion_has_exact_bounded_pure_semantics(text, expected):
    from okto_grafx.domain.query.scalars import scalar_value

    assert scalar_value("TOINTEGER", text) == expected


def test_decimal_conversion_matches_independent_exact_oracle():
    from decimal import Decimal
    from okto_grafx.domain.query.scalars import scalar_value

    for coefficient in ("0", "0001", "-12.75", ".999", "9223372036854775807",
                        "-9223372036854775808", "9007199254740993.25"):
        for exponent in range(-25, 26):
            text = f"{coefficient}e{exponent}"
            exact = Decimal(text)
            expected = int(exact) if -(2 ** 63) <= exact <= 2 ** 63 - 1 else None
            assert scalar_value("TOINTEGER", text) == expected, text


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("lower('ÁBC')", "ábc"),
        ("upper('straße')", "STRASSE"),
        ("trim('  a b  ')", "a b"),
        ("lower(null)", None),
        ("abs(null)", None),
        ("abs(-17)", 17),
        ("abs(-2.5)", 2.5),
        ("upper(trim(' a '))", "A"),
        ("coalesce(lower(null), upper('z'))", "Z"),
    ],
)
def test_scalar_values(expression, expected):
    with connect(":memory:") as db:
        assert db.execute(f"RETURN {expression} AS value").rows == ((expected,),)


@pytest.mark.parametrize(
    "expression",
    [
        "lower()",
        "upper(1,2)",
        "trim(*)",
        "abs(true)",
        "lower(4)",
        "abs('x')",
        "lower(DISTINCT 'x')",
        "abs(-9223372036854775808)",
    ],
)
def test_refusals(expression):
    with connect(":memory:") as db, pytest.raises(GrafxError):
        db.execute(f"RETURN {expression}")


def test_parameters_schema_and_empty_table():
    with connect(":memory:") as db:
        assert db.execute("RETURN lower($x)", {"x": "ABC"}).rows == (("abc",),)
        assert db.execute("RETURN coalesce(abs($x), 0)", {"x": -3}).rows == ((3,),)
        with pytest.raises(GrafxError):
            db.execute("RETURN abs($x)", {"x": True})
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, body STRING, PRIMARY KEY(id))")
        with pytest.raises(GrafxError):
            db.explain("MATCH (n:N) RETURN lower(n.id)")
        with pytest.raises(GrafxError):
            db.execute("MATCH (n:N) RETURN lower(n.id)")
        with pytest.raises(GrafxError):
            db.execute("MATCH (n:N) RETURN lower($x)", {"x": 1})
        with db.begin() as tx:
            tx.execute("CREATE (:N {id:1,body:' AbC '})")
        assert db.execute(
            "MATCH (n:N) WHERE lower(trim(n.body)) = 'abc' RETURN upper(n.body)"
        ).rows == ((" ABC ",),)


def test_union_scalar_types():
    with connect(":memory:") as db:
        assert db.execute("RETURN abs(-1) AS n UNION RETURN 1.0 AS n").rows == ((1.0,),)
        assert db.execute("RETURN lower('X') AS n UNION RETURN 1 AS n").rows == (("x",), (1,))


def test_native_scalars_in_writes():
    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, body STRING, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1,body:' ABC '})")
        with db.begin() as tx:
            tx.execute("MATCH (n:N) SET n.body=lower(trim(n.body))")
        assert db.execute("MATCH (n:N) RETURN n.body").rows == (("abc",),)
