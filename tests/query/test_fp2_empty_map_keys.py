"""Empty string map keys are values, not permission for empty schema names."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.query.parser import parse
from okto_grafx.errors import GrafxParseError, GrafxPlanError


@pytest.mark.parametrize("query,expected", [
    ("RETURN {``: 7} AS m", {"": 7}),
    ("RETURN {``: 7}[''] AS value", 7),
    ("RETURN {``: 7}.`` AS value", 7),
    ("RETURN {``: {``: [3]}}.``.``[0] AS value", 3),
    ("RETURN {}.`` AS value", None),
    ("RETURN coalesce(null, {``: 9}).`` AS value", 9),
    ("WITH {``: 8} AS m RETURN m[''] AS value", 8),
    ("RETURN keys({``: 2}) AS value", ("",)),
])
def test_empty_key_execution_and_explain(query, expected):
    with connect(":memory:") as db:
        db.explain(query)
        assert db.execute(query).rows == ((expected,),)
        assert db.execute(query).rows == ((expected,),)  # prepared plan reuse


def test_parameter_key_and_map_stay_owned_by_the_caller():
    payload = {"": [4]}
    with connect(":memory:") as db:
        assert db.execute("RETURN $m.``[0], $m[$key][0]", {"m": payload, "key": ""}).rows == ((4, 4),)
        assert payload == {"": [4]}


def test_empty_key_description_roundtrip_and_original_heading():
    query = "RETURN {``: 4}.``"
    assert parse(parse(query).describe()) == parse(query)
    with connect(":memory:") as db:
        result = db.execute(query)
        assert result.columns == ("{``: 4}.``",)
        assert result.dictionaries() == ({"{``: 4}.``": 4},)


@pytest.mark.parametrize("query", [
    "RETURN {``: 1, ``: 2}", "RETURN ``", "RETURN 1 AS ``",
    "CREATE NODE TABLE ``(id INT64)", "CREATE NODE TABLE N(`` INT64)",
    "MATCH (``:N) RETURN 1", "CALL ``() YIELD x RETURN x",
    "MATCH ()-[``:R]->() RETURN 1", "MATCH `` = ()-->() RETURN 1",
    "RETURN [`` IN [1] | 1]", "RETURN f(`` => 1)",
])
def test_empty_symbol_names_and_duplicate_empty_keys_still_refuse(query):
    with pytest.raises(GrafxParseError):
        parse(query)


def test_empty_map_key_reaches_native_boolean_type_validation():
    with connect(":memory:") as db:
        with pytest.raises(GrafxPlanError) as caught:
            db.execute("RETURN NOT {``: false}")
        assert caught.value.details["reason"] == "boolean_operand_type"
        assert caught.value.details["query_phase"] == "planning"
