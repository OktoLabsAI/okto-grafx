"""Native column/catalog fencing and strict typed row validation, including projections."""

from dataclasses import replace
import struct

import pytest

from okto_grafx import StoredType, DecimalValue
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected, GrafxSchemaVersionMismatch
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import ColumnDef, TableDef, encode_tuple, decode_tuple, _decode_tuple_projection
from okto_grafx.domain.model.stored_types import (
    TYPED_COLLECTIONS_CAPABILITY, TYPED_COLLECTIONS_CAPABILITY_BIT, stored_type_to_json, stored_type_from_json,
)
from okto_grafx.domain.model.value import ValueType, encode_values
from okto_grafx.domain.page.checksum import crc32c
from tests.storage_core.test_stored_types import RECORD


def table(descriptor=RECORD):
    return TableDef(1, "N", "node", (ColumnDef("id", ValueType.INT64, nullable=False),
                                    ColumnDef("v", descriptor.value_type, nullable=descriptor.nullable, stored_type=descriptor)), primary_key="id")


def test_column_owns_descriptor_and_parameter_changes_affect_schema_identity():
    column = table().columns[1]
    assert column.stored_type == RECORD and column.stored_type is not RECORD
    assert column.stored_type.fields[0][1] is not RECORD.fields[0][1]
    assert column != replace(column, stored_type=replace(RECORD, fields=RECORD.fields[:1]))
    with pytest.raises(GrafxConfigurationError):
        ColumnDef("v", ValueType.LIST, stored_type=RECORD)
    with pytest.raises(GrafxConfigurationError):
        ColumnDef("v", ValueType.MAP, nullable=False, stored_type=RECORD)
    with pytest.raises(GrafxConfigurationError):
        ColumnDef("v", ValueType.INT64, stored_type=StoredType("INT64"))
    with pytest.raises(GrafxConfigurationError):
        replace(table(), primary_key="v")


def test_catalog_roundtrip_adds_nested_capabilities_and_preserves_descriptor():
    catalog = Catalog()
    with pytest.raises(GrafxConfigurationError):
        catalog.add_table(table())
    assert not catalog.tables()
    catalog.upgrade_index_catalog(())
    catalog.add_table(table())
    assert catalog.requires_capability(TYPED_COLLECTIONS_CAPABILITY)
    assert catalog.requires_capability("decimal_values_v1")
    wire = catalog.serialize()
    assert Catalog.deserialize(wire).table("N") == table()
    assert catalog.copy().serialize() == wire
    assert Catalog.deserialize(wire).serialize() == wire


@pytest.mark.parametrize("kind,capability", [("ANY", "heterogeneous_properties_v1"), ("DATE", "temporal_values_v1")])
def test_nested_declarations_activate_all_required_capabilities(kind, capability):
    catalog = Catalog()
    catalog.upgrade_index_catalog(())
    catalog.add_table(table(StoredType("LIST", element=StoredType(kind))))
    assert catalog.requires_capability(TYPED_COLLECTIONS_CAPABILITY)
    assert catalog.requires_capability(capability)
    assert Catalog.deserialize(catalog.serialize()).requires_capability(capability)


def test_unknown_typed_capability_refuses_before_table_decoder(monkeypatch):
    import okto_grafx.domain.model.catalog as module
    catalog = Catalog()
    catalog.upgrade_index_catalog(())
    catalog.add_table(table())
    wire = catalog.serialize()
    monkeypatch.setattr(module, "_KNOWN_CAPABILITY_BITS", module._KNOWN_CAPABILITY_BITS & ~TYPED_COLLECTIONS_CAPABILITY_BIT)
    def forbidden(*args):
        raise AssertionError("Old reader must refuse before interpreting descriptor tables")
    monkeypatch.setattr(module, "_decode_table", forbidden)
    with pytest.raises(GrafxSchemaVersionMismatch):
        Catalog.deserialize(wire)


@pytest.mark.parametrize("missing", [TYPED_COLLECTIONS_CAPABILITY_BIT, 1 << 26])
def test_omitted_capability_with_valid_checksum_is_corruption(missing):
    catalog = Catalog()
    catalog.upgrade_index_catalog(())
    catalog.add_table(table())
    wire = bytearray(catalog.serialize())
    flags = struct.unpack_from("<Q", wire, 28)[0]
    struct.pack_into("<Q", wire, 28, flags & ~missing)
    struct.pack_into("<I", wire, len(wire)-4, crc32c(bytes(wire[:-4])))
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(bytes(wire))


@pytest.mark.parametrize("bad", [{"id": 1}, {"id": True, "amount": None, "samples": None, "tags": None},
    {"id": 1, "amount": DecimalValue(125, 3, 2), "samples": None, "tags": None},
    {"id": 1, "amount": None, "samples": [1.0], "tags": None},
    {"id": 1, "amount": None, "samples": None, "tags": {"k": [None]}}])
def test_malformed_stored_collection_is_rejected_even_when_projected_away(bad):
    raw = encode_values((1, bad))
    for decoder in (lambda: decode_tuple(table(), raw), lambda: _decode_tuple_projection(table(), raw, frozenset({0}))):
        with pytest.raises(GrafxCorruptionDetected):
            decoder()


def test_native_tuple_assignment_canonicalizes_before_bytes_and_read_preserves_it():
    raw = encode_tuple(table(), (1, {"id": 1, "amount": DecimalValue(125, 3, 2)}))
    expected = (1, {"id": 1, "amount": DecimalValue(12500, 12, 4), "samples": None, "tags": None})
    assert decode_tuple(table(), raw) == expected
    assert encode_tuple(table(), expected) == raw


def test_descriptor_json_is_exact_owned_and_bounded():
    data = stored_type_to_json(RECORD)
    assert stored_type_from_json(data) == RECORD
    data["fields"][0]["type"]["nullable"] = True
    assert stored_type_from_json(data) != RECORD
    with pytest.raises(GrafxConfigurationError):
        stored_type_from_json({**data, "ignored": 1})
    with pytest.raises(GrafxConfigurationError):
        stored_type_from_json({"kind": "ARRAY", "nullable": True, "element": {"kind": "INT64", "nullable": True}, "length": True})
    with pytest.raises(GrafxConfigurationError):
        stored_type_from_json({"kind": "INT64"})
    cyclic = {"kind": "LIST", "nullable": True}
    cyclic["element"] = cyclic
    with pytest.raises(GrafxConfigurationError):
        stored_type_from_json(cyclic)
