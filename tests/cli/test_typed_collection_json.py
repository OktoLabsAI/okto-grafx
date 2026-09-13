"""CLI exposes the full stored descriptor and native tagged nested values."""

from okto_grafx import connect, StoredType
from okto_grafx.domain.model.stored_types import stored_type_to_json
from okto_grafx.domain.model.decimal_interchange import decimal_json_value
from okto_grafx import DecimalValue


def test_cli_schema_keeps_array_struct_and_nested_decimal_coordinates(tmp_path, cli):
    path = tmp_path / "db"
    with connect(path) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,v STRUCT<a:ARRAY<DECIMAL(12,4),2> NOT NULL>,PRIMARY KEY(id))")
            tx.execute("CREATE(:N {id:1,v:{a:[decimal('1.25',3,2),null]}})")
        descriptor = db.catalog.catalog.table("N").columns[1].stored_type
        db.checkpoint()
    inventory = cli("schema", str(path), "--json")
    assert inventory.code == 0, inventory.err
    columns = inventory.document["tables"][0]["columns"]
    assert "stored_type" not in columns[0]
    assert columns[1]["type"] == "MAP"
    assert columns[1]["stored_type"] == stored_type_to_json(descriptor)
    assert descriptor == StoredType("STRUCT", fields=(("a", StoredType("ARRAY", nullable=False,
        element=StoredType("DECIMAL", precision=12, scale=4), length=2)),))
    query = cli("query", str(path), "MATCH(n:N) RETURN n.v", "--json")
    assert query.code == 0, query.err
    assert query.document["rows"] == [[{"a": [decimal_json_value(DecimalValue(12500, 12, 4)), None]}]]
