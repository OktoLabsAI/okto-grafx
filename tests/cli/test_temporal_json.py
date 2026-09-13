"""Scalar temporal CLI output shares the lossless entity-value tags."""

import json

from okto_grafx.cli.output import jsonable, render_json
from okto_grafx.domain.query.entity_values import _json_value
from tests.api.test_temporal_transfer import VALUES


def test_temporal_scalar_and_nested_json_matches_entity_observations():
    for value in VALUES:
        expected = _json_value(value)
        assert jsonable(value) == expected
        assert json.loads(render_json({"rows": [[value, {"nested": [value]}]]})) == {
            "rows": [[expected, {"nested": [expected]}]]}
        assert isinstance(expected["type"], str)
    assert jsonable(VALUES[0])["epoch_day"] == str(VALUES[0].epoch_day)
    assert jsonable(VALUES[5])["months"] == str(2**63 - 1)


def test_cli_query_returns_temporal_tags_instead_of_descriptions(empty_database_path, cli):
    run = cli("query", empty_database_path,
              "RETURN date('2024-02-29') AS day, {d:duration('P1M')} AS nested, "
              "datetime('1969-12-31T23:59:59.999999999Z') AS instant", "--json")
    assert run.code == 0, run.err
    day, nested, instant = run.document["rows"][0]
    assert day == {"type": "date", "epoch_day": "19782"}
    assert nested["d"] == {"type": "duration", "months": "1", "days": "0", "seconds": "0", "nanoseconds": 0}
    assert instant["type"] == "datetime" and instant["epoch_seconds"] == "-1"
    assert instant["nanosecond"] == 999999999


def test_cli_unknown_label_is_empty_not_a_planning_failure(empty_database_path, cli):
    run = cli("query", empty_database_path, "MATCH(n:NoSuchTable) RETURN n", "--json")
    assert run.code == 0 and run.document["rows"] == []
