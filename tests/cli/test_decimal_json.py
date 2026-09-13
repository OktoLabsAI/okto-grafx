"""CLI scalar/entity JSON and schema inventory preserve exact DECIMAL metadata."""

import json

from okto_grafx import connect, DecimalValue
from okto_grafx.cli.output import jsonable, render_json
from okto_grafx.domain.model.decimal_interchange import decimal_json_value
from okto_grafx.domain.query.entity_values import _json_value


def test_decimal_scalar_and_nested_json_share_entity_tags():
    value = DecimalValue(-(10**38 - 1), 38, 19)
    tagged = decimal_json_value(value)
    assert jsonable(value) == _json_value(value) == tagged
    assert json.loads(render_json({"rows": [[value, {"nested": [value]}]]})) == {
        "rows": [[tagged, {"nested": [tagged]}]]}


def test_cli_expression_returns_native_tags_and_does_not_activate_storage(empty_database_path, cli):
    run = cli("query", empty_database_path,
              "RETURN decimal('123.4500',38,4) AS d, {nested:[decimal('-0.01',4,2)]} AS m", "--json")
    assert run.code == 0, run.err
    assert run.document["rows"] == [[decimal_json_value(DecimalValue(1234500, 38, 4)),
                                     {"nested": [decimal_json_value(DecimalValue(-1, 4, 2))]}]]
    with connect(empty_database_path, read_only=True) as db:
        assert not db._catalog.catalog.requires_capability("decimal_values_v1")


def test_cli_read_node_edge_properties_and_schema_parameters(tmp_path, cli):
    path = tmp_path / "db"
    value = DecimalValue(12300, 12, 4)
    with connect(path) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,v DECIMAL(12,4),PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N,v DECIMAL(12,4))")
            tx.execute("CREATE(n:N {id:1,v:$v})-[:R {v:$v}]->(n)", {"v": value})
        db.checkpoint()
    run = cli("query", str(path), "MATCH(n:N)-[r:R]->() RETURN n.v,n,r", "--json")
    assert run.code == 0, run.err
    scalar, node, edge = run.document["rows"][0]
    assert scalar == node["properties"]["v"] == edge["properties"]["v"] == decimal_json_value(value)
    inventory = cli("schema", str(path), "--json")
    assert inventory.code == 0, inventory.err
    for table in inventory.document["tables"]:
        for column in table["columns"]:
            if column["type"] == "DECIMAL":
                assert (column["decimal_precision"], column["decimal_scale"]) == (12, 4)
            else:
                assert "decimal_precision" not in column and "decimal_scale" not in column
