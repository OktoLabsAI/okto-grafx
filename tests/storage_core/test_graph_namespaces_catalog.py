"""Qualified physical names and logical graph-kind separation in catalog v2."""

import struct
from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected, GrafxSchemaVersionMismatch
from okto_grafx.domain.model import catalog as catalog_module
from okto_grafx.domain.model.catalog import Catalog, GRAPH_NAMESPACES_CAPABILITY
from okto_grafx.domain.model.relationship_type import RelationshipTypeDef
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.engine.public_views import CatalogView

from .test_relationship_type_catalog import schema, checksum


def overlapping():
    value = schema()
    value.add_table(TableDef(6, "AB", "node", (ColumnDef("value", ValueType.STRING),)))
    return value


@pytest.mark.parametrize("public", [False, True])
def test_explicit_kind_preserves_both_ids_and_unqualified_ambiguity(public):
    catalog = overlapping()
    view = CatalogView(catalog.tables(), catalog.spaces()) if public else catalog
    assert view.has_table("AB")
    assert view.has_table("AB", kind="node")
    assert view.has_table("AB", kind="rel")
    assert view.table("AB", kind="node").table_id == 6
    assert view.table("AB", kind="rel").table_id == 4
    assert view.table_by_id(4).kind == "rel"
    assert view.relationship_tables("AB")[0].table_id == 4
    with pytest.raises(GrafxConfigurationError) as caught:
        view.table("AB")
    assert caught.value.details["reason"] == "ambiguous_table_name"
    assert not view.has_table("A", kind="rel")
    with pytest.raises(GrafxConfigurationError):
        view.table("A", kind="rel")


@pytest.mark.parametrize("kind", ["NODE", "relationship", 1, False, [], {}])
@pytest.mark.parametrize("public", [False, True])
def test_invalid_kind_refuses_without_guessing(kind, public):
    catalog = schema()
    view = CatalogView(catalog.tables(), catalog.spaces()) if public else catalog
    with pytest.raises(GrafxConfigurationError):
        view.has_table("A", kind=kind)
    with pytest.raises(GrafxConfigurationError):
        view.table("A", kind=kind)


def test_exact_capability_roundtrip_and_copy():
    catalog = overlapping()
    assert GRAPH_NAMESPACES_CAPABILITY in catalog.required_capabilities()
    data = catalog.serialize()
    assert struct.unpack_from("<Q", data, 28)[0] & (1 << 24)
    restored = Catalog.deserialize(data)
    assert restored == catalog
    assert restored.serialize() == data
    copied = catalog.copy()
    copied.add_table(TableDef(7, "Extra", "node", (ColumnDef("v", ValueType.INT64),)))
    assert not catalog.has_table("Extra")
    assert copied.table("AB", kind="rel") == catalog.table("AB", kind="rel")


@pytest.mark.parametrize("group_first", [False, True])
def test_logical_group_and_node_can_have_one_name_in_either_order(group_first):
    catalog = schema()
    node = TableDef(6, "G", "node", (ColumnDef("v", ValueType.INT64),))
    if not group_first:
        catalog.add_table(node)
    catalog.add_relationship_type(RelationshipTypeDef("G", (4,5)))
    if group_first:
        catalog.add_table(node)
    assert catalog.table("G") is node
    assert tuple(t.table_id for t in catalog.relationship_tables("G")) == (4,5)
    assert Catalog.deserialize(catalog.serialize()) == catalog


@pytest.mark.parametrize("logical", [False, True])
def test_rechecksummed_overlap_without_capability_is_corruption(logical):
    catalog = schema()
    if logical:
        catalog.add_relationship_type(RelationshipTypeDef("A", (4,5)))
    else:
        catalog.add_table(TableDef(6, "AB", "node", (ColumnDef("v", ValueType.INT64),)))
    body = bytearray(catalog.serialize()[:-4])
    flags = struct.unpack_from("<Q", body, 28)[0]
    struct.pack_into("<Q", body, 28, flags & ~(1 << 24))
    with pytest.raises(GrafxCorruptionDetected) as caught:
        Catalog.deserialize(checksum(bytes(body)))
    assert caught.value.details["field"] == "required_capabilities"


def test_old_inventory_refuses_new_bit_before_decoding_body(monkeypatch):
    data = overlapping().serialize()
    monkeypatch.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS", catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 24))
    with pytest.raises(GrafxSchemaVersionMismatch):
        Catalog.deserialize(data)


def test_duplicates_and_invalid_groups_leave_capabilities_and_bytes_unchanged():
    catalog = schema()
    before = catalog.serialize()
    with pytest.raises(GrafxConfigurationError):
        catalog.add_table(replace(catalog.table("A"), table_id=6))
    with pytest.raises(GrafxConfigurationError):
        catalog.add_relationship_type(RelationshipTypeDef("A", (4,99)))
    assert catalog.serialize() is before
    assert not catalog.requires_capability(GRAPH_NAMESPACES_CAPABILITY)


def test_nonoverlapping_catalog_does_not_activate_capability():
    catalog = schema()
    assert not catalog.requires_capability(GRAPH_NAMESPACES_CAPABILITY)
    assert not struct.unpack_from("<Q", catalog.serialize(), 28)[0] & (1 << 24)
