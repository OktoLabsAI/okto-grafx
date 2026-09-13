"""Schema-only ANY declaration: concrete wire tags, admission and catalog fences."""

import struct
from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected, GrafxSchemaVersionMismatch
from okto_grafx.domain.model import catalog as catalog_module
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.schema import (
    ColumnDef, TableDef, SchemaType, HETEROGENEOUS_PROPERTIES_CAPABILITY,
    encode_tuple, decode_tuple, _decode_tuple_projection, decode_tuple_landing, _is_unmaterialized_column,
)
from okto_grafx.domain.model.value import ValueType, Timestamp, VectorValue, encode_value, decode_value
from okto_grafx.domain.page import crc32c
from okto_grafx.domain.recovery.decision import committed_replay
from okto_grafx.engine.commit_redo import CommitRedo

from .test_catalog_replay_images import records, staged


def table(nullable=True):
    return TableDef(1, "Flex", "node", (ColumnDef("v", SchemaType.ANY, nullable=nullable),))


def schema():
    result = Catalog()
    result.upgrade_index_catalog(())
    result.add_table(table())
    return result


def checksum(body):
    frozen = bytes(body)
    return frozen + struct.pack("<I", crc32c(frozen))


@pytest.mark.parametrize("value", [None, True, False, 1, -(2**63), 2**63 - 1, 2.5, "text", b"bytes",
                                  (1, "a", False, None), {"a": (1, "text"), "b": {}}, Timestamp(123)])
def test_actual_tags_are_preserved_for_every_projection(value):
    definition = table()
    encoded = encode_tuple(definition, (value,))
    assert encoded == encode_value(value)
    assert decode_tuple(definition, encoded) == (value,)
    assert decode_tuple_landing(definition, encoded) == (value,)
    assert _is_unmaterialized_column(_decode_tuple_projection(definition, encoded, frozenset())[0])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"),
                                  [float("nan")], {"a": [float("inf")]}])
def test_nonfinite_admission_cannot_hide_in_skipped_properties(value):
    with pytest.raises(SchemaMismatchError):
        encode_tuple(table(), (value,))
    raw = encode_value(value)  # Expression codec intentionally admits these.
    for decode in (decode_tuple, decode_tuple_landing):
        with pytest.raises(SchemaMismatchError):
            decode(table(), raw)
    with pytest.raises(SchemaMismatchError):
        _decode_tuple_projection(table(), raw, frozenset())


@pytest.mark.parametrize("raw", [b"", b"\xff", b"\x04\x01\x00\x00\x00\xff", b"\x00\x00"])
def test_malformed_values_refuse_even_when_not_projected(raw):
    with pytest.raises(GrafxCorruptionDetected):
        _decode_tuple_projection(table(), raw, frozenset())


def test_any_is_not_a_new_value_type_or_wire_tag():
    assert [int(kind) for kind in ValueType] == list(range(19))
    with pytest.raises(GrafxCorruptionDetected):
        decode_value(b"\xff")
    with pytest.raises(SchemaMismatchError):
        encode_tuple(table(False), (None,))
    with pytest.raises(SchemaMismatchError):
        _decode_tuple_projection(table(False), b"\0", frozenset())
    with pytest.raises(GrafxConfigurationError):
        replace(table(), primary_key="v")


def test_no_embedding_space_bypass():
    vector = VectorValue((1.0, 2.0), space_ref=999, dtype="float32")
    for value in (vector, [vector], {"a": [vector]}):
        with pytest.raises(SchemaMismatchError):
            encode_tuple(table(), (value,))


def test_legacy_and_failed_install_leave_catalog_unchanged():
    legacy = Catalog()
    before = legacy.serialize()
    with pytest.raises(GrafxConfigurationError):
        legacy.add_table(table())
    assert legacy.serialize() is before
    normal = Catalog()
    normal.upgrade_index_catalog(())
    normal.add_table(TableDef(1, "Flex", "node", (ColumnDef("v", ValueType.STRING),)))
    before = normal.serialize()
    with pytest.raises(GrafxConfigurationError):
        normal.add_table(table())
    assert normal.serialize() is before
    assert not normal.requires_capability(HETEROGENEOUS_PROPERTIES_CAPABILITY)


def test_catalog_round_trip_and_old_reader_refusal(monkeypatch):
    original = schema()
    raw = original.serialize()
    assert original.requires_capability(HETEROGENEOUS_PROPERTIES_CAPABILITY)
    assert struct.unpack_from("<Q", raw, 28)[0] & (1 << 20)
    assert Catalog.deserialize(raw) == original
    assert Catalog.deserialize(raw).table("Flex").columns[0].type is SchemaType.ANY
    assert original.copy().serialize() == raw
    monkeypatch.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS",
                        catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 20))
    with pytest.raises(GrafxSchemaVersionMismatch):
        Catalog.deserialize(raw)


def test_checksum_valid_catalog_cannot_drop_required_capability():
    raw = bytearray(schema().serialize()[:-4])
    bits = struct.unpack_from("<Q", raw, 28)[0]
    struct.pack_into("<Q", raw, 28, bits & ~(1 << 20))
    with pytest.raises(GrafxCorruptionDetected, match="capability"):
        Catalog.deserialize(checksum(raw))


def test_appended_any_installs_both_capabilities_and_preserves_prior_layout():
    original = Catalog()
    original.upgrade_index_catalog(())
    original.add_table(TableDef(1, "Flex", "node", (ColumnDef("id", ValueType.INT64),)))
    result = original.add_nullable_column("Flex", ColumnDef("v", SchemaType.ANY))
    assert result.schema_layouts == ((1, 1),)
    assert original.requires_capability("nullable_columns_v1")
    assert original.requires_capability(HETEROGENEOUS_PROPERTIES_CAPABILITY)
    assert Catalog.deserialize(original.serialize()) == original


def test_committed_catalog_replay_is_idempotent_and_requires_commit():
    expected = schema()
    pool, store, images = staged(expected, 1000)
    before = store.read_from_pages()
    writes = list(pool.storage.write_calls)
    CommitRedo(pool).apply(committed_replay(records(images, 1000)[:-1]))
    assert store.read_from_pages() == before
    assert pool.storage.write_calls == writes
    replay = committed_replay(records(images, 1000))
    CommitRedo(pool).apply(replay)
    assert store.read_from_pages() == expected
    CommitRedo(pool).apply(replay)
    assert store.read_from_pages() == expected


def test_old_reader_refuses_committed_new_catalog_before_applying_any_page(monkeypatch):
    pool, store, images = staged(schema(), 1000)
    before = store.read_from_pages()
    writes = list(pool.storage.write_calls)
    monkeypatch.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS",
                        catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 20))
    with pytest.raises(GrafxSchemaVersionMismatch):
        CommitRedo(pool).apply(committed_replay(records(images, 1000)))
    assert pool.storage.write_calls == writes
    assert store.read_from_pages() == before
