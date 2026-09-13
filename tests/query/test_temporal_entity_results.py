"""Native temporals survive entity/path materialization, not just scalar RETURN."""

import json
import pytest

from okto_grafx import connect, DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue
from okto_grafx.errors import GrafxError
from okto_grafx.domain.query.entity_values import NodeValue
from tests.query.test_fp3_entity_values import node


VALUES = [
    (DateValue(1970), {"type":"date", "epoch_day":"0"}),
    (LocalTimeValue(1), {"type":"localtime", "nanoseconds":"1"}),
    (TimeValue(LocalTimeValue(1),3600), {"type":"time", "nanoseconds":"1", "offset_seconds":3600}),
    (LocalDateTimeValue(DateValue(1970),LocalTimeValue(1)),
     {"type":"localdatetime", "epoch_day":"0", "nanoseconds":"1"}),
    (DateTimeValue.from_epoch_parts(0,1,offset_seconds=3600,zone="Europe/Paris"),
     {"type":"datetime", "epoch_seconds":"0", "nanosecond":1, "offset_seconds":3600, "zone":"Europe/Paris"}),
    (DurationValue(2**63-1,-1,-1,999999999),
     {"type":"duration", "months":"9223372036854775807", "days":"-1", "seconds":"-1", "nanoseconds":999999999}),
]


@pytest.mark.parametrize("value, encoded", VALUES)
def test_entity_temporal_properties_are_owned_and_losslessly_json_tagged(value, encoded):
    observed = node(properties={"v":value, "nested":[{"v":value}]})
    assert type(observed.properties["v"]) is type(value)
    assert observed.properties["v"] == value
    assert observed.properties["v"] is not value
    serialized = json.loads(json.dumps(observed.to_dict()))
    assert serialized["properties"]["v"] == encoded
    assert serialized["properties"]["nested"]["items"][0]["entries"]["v"] == encoded


@pytest.mark.parametrize("value, encoded", VALUES)
def test_temporal_node_relationship_path_and_scalar_outputs_agree(tmp_path, value, encoded):
    path = tmp_path / "db"
    with connect(path) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            pending = tx.execute("CREATE (a:A {v:$v})-[r:R {v:$v}]->(b:B {v:$v}) RETURN a", {"v":value}).rows[0][0]
            assert type(pending) is NodeValue and pending.properties["v"] == value
            assert pending.to_dict()["properties"]["v"] == encoded
    with connect(path) as db:
        row = db.execute("MATCH p=(a:A)-[r:R]->(b:B) RETURN a,r,p,a.v,properties(r)").rows[0]
        assert row[0].properties["v"] == row[1].properties["v"] == row[3] == row[4]["v"] == value
        assert row[2].nodes[0].properties["v"] == row[2].nodes[1].properties["v"] == value
        assert row[2].to_dict()["relationships"][0]["properties"]["v"] == encoded
        with db.query("MATCH (a:A) RETURN a").cursor(batch_size=1) as cursor:
            assert cursor.fetchone()[0].properties["v"] == value


def test_temporal_entity_copy_does_not_share_nested_frozen_objects():
    original = LocalDateTimeValue(DateValue(2024),LocalTimeValue(1))
    observed = node(properties={"v":original})
    object.__setattr__(original.date, "year", 2025)
    assert observed.properties["v"].date.year == 2024
    invalid = DateValue(2024)
    object.__setattr__(invalid, "month", 99)
    with pytest.raises(GrafxError):
        node(properties={"v":invalid})


def test_query_orders_and_returns_whole_entities_with_temporal_properties():
    with connect(":memory:") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE (:A {day:date('2024-01-02')}),(:B {day:date('2024-01-01')})")
        rows = db.execute("MATCH (a) WITH a, a.day AS day WITH a,day ORDER BY day LIMIT 2 RETURN a,day").rows
        assert [row[0].label for row in rows] == ["B","A"]
        assert all(row[0].properties["day"] == row[1] for row in rows)
