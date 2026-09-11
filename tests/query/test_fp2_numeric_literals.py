"""Native exact based-integer syntax at lexer/parser/execution/write boundaries."""

import pytest

import okto_grafx
from okto_grafx.errors import GrafxParseError
from okto_grafx.domain.query.lexer import tokenize
from okto_grafx.domain.query.limits import MAX_NUMBER_CHARACTERS


@pytest.mark.parametrize("literal, expected", [
    ("0x1", 1), ("0XfF", 255), ("0x162CD4F6", 372036854),
    ("0x7FFFFFFFFFFFFFFF", 9223372036854775807),
    ("-0x8000000000000000", -9223372036854775808),
    ("0o2613152366", 372036854), ("0o777777777777777777777", 9223372036854775807),
    ("-0o1000000000000000000000", -9223372036854775808),
    ("+0x0", 0), ("-0o0", 0), ("0x_1_F", 31), ("0o_1_7", 15),
])
def test_based_integers_execute_as_exact_int64(literal, expected):
    with okto_grafx.connect(":memory:") as db:
        value = db.execute(f"RETURN {literal} AS value").rows[0][0]
        assert type(value) is int
        assert value == expected


@pytest.mark.parametrize("literal", [
    "0x", "0o", "0x_", "0xG", "0x1Z", "0o8", "0o17j", "0x1_", "0o1__7",
    "0x8000000000000000", "+0x8000000000000000", "-0x8000000000000001",
    "0o1000000000000000000000", "-0o1000000000000000000001",
])
def test_invalid_or_out_of_range_literal_refuses_before_write_effects(literal):
    with okto_grafx.connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1})")
            with pytest.raises(GrafxParseError):
                tx.execute(f"CREATE (:N {{id:2}}) RETURN {literal} AS invalid")
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)


def test_numeric_token_text_offsets_and_query_composition():
    tokens = tokenize("RETURN\n 0XfF + 0o17")
    assert tokens[1].text == "0XfF"
    assert (tokens[1].value, tokens[1].line, tokens[1].column) == (255, 2, 2)
    with okto_grafx.connect(":memory:") as db:
        assert db.execute("UNWIND [0x1,0o2] AS n RETURN n + 0x10 ORDER BY n").rows == ((17,), (18,))


def test_based_literal_length_is_bounded_before_conversion():
    with pytest.raises(GrafxParseError, match="numeric literal may carry at most"):
        tokenize("RETURN 0x" + "0" * MAX_NUMBER_CHARACTERS)


def test_based_literals_round_trip_durable_storage(tmp_path):
    path = tmp_path / "db"
    with okto_grafx.connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:-0x8000000000000000}), (:N {id:0o777777777777777777777})")
    with okto_grafx.connect(path) as db:
        assert db.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == ((-9223372036854775808,), (9223372036854775807,))
