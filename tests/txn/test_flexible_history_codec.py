"""Independent history-model framing and fail-closed authority checks."""

from dataclasses import replace
import struct

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxConfigurationError
from okto_grafx.domain.model.catalog import Catalog, _encode_table
from okto_grafx.domain.model.relationship_type import RelationshipTypeDef
from okto_grafx.domain.model.schema import TableDef, ColumnDef, SchemaType, encode_tuple
from okto_grafx.engine.system_history_store import HistoryChange, _decode_change, validate_history_model


def node():
    return TableDef(1,"N","node",(ColumnDef("_properties",SchemaType.ANY,nullable=False),),
                    flexible_properties=True,unlabeled=True)


def edge():
    return TableDef(2,"Physical","rel",node().columns,from_table="N",to_table="N",flexible_properties=True)


@pytest.mark.parametrize("relationship", [False, True])
def test_exact_schema_model_trailer_and_value_roundtrip(relationship):
    table = edge() if relationship else node()
    values = (1,2,{"v":(1,"s")}) if relationship else ({"v":(1,"s")},)
    name = "R" if relationship else None
    change = HistoryChange(table, 4, 1, values, logical_type=name)
    schema = _encode_table(table) + b"\0\0" + b"GXHM01" + (b"\x01\x01\0R" if relationship else b"\x03\0\0")
    payload = encode_tuple(table, values)
    expected = struct.pack("<BQII",1,4,len(schema),len(payload)) + schema + payload
    assert change.encode() == expected
    assert _decode_change(expected) == change
    # The old grammar's exact schema-length condition rejects the new trailer.
    assert len(schema) != len(_encode_table(table))+2


@pytest.mark.parametrize("trailer", [b"BAD001\x03\0\0", b"GXHM01\x04\0\0", b"GXHM01\0\0\0", b"GXHM01\x03\x02\0x", b"GXHM01\x03\x01\0R"])
def test_bad_metadata_refuses_even_with_valid_change_lengths(trailer):
    table = node()
    schema = _encode_table(table)+b"\0\0"+trailer
    raw = struct.pack("<BQII",4,0,len(schema),0)+schema
    with pytest.raises(GrafxCorruptionDetected):
        _decode_change(raw)


def test_lost_or_wrong_history_model_is_not_reconstructed_from_current_catalog():
    catalog = Catalog()
    catalog.upgrade_index_catalog(())
    catalog.add_table(node())
    catalog.add_table(edge())
    catalog.add_relationship_type(RelationshipTypeDef("R",(2,)))
    for change in (
        HistoryChange(replace(node(), flexible_properties=False,unlabeled=False),0,4,()),
        HistoryChange(edge(),0,4,()), HistoryChange(edge(),0,4,(),logical_type="Other"),
    ):
        # These frames can be individually well-formed but lack catalog authority.
        decoded = _decode_change(change.encode())
        with pytest.raises(GrafxCorruptionDetected):
            validate_history_model(decoded,catalog)
    validate_history_model(HistoryChange(node(),0,4,()),catalog)
    validate_history_model(HistoryChange(edge(),0,4,(),logical_type="R"),catalog)


@pytest.mark.parametrize("name", ["", "bad name", "Physical", 1, "é", "x"*129])
def test_invalid_logical_type_never_encodes(name):
    with pytest.raises(GrafxConfigurationError):
        HistoryChange(edge(),0,4,(),logical_type=name).encode()
