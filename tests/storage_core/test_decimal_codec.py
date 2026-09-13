"""Independent decimal frames, typed tuple validation and catalog reader fencing."""

import random
import struct

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxSchemaVersionMismatch
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.decimal_codec import (
    DECIMAL_VALUES_CAPABILITY, DECIMAL_VALUES_CAPABILITY_BIT,
    encode_decimal_value, decode_decimal_value,
)
from okto_grafx.domain.model.decimal_values import DecimalValue
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.schema import (
    ColumnDef, TableDef, SchemaType, encode_tuple, decode_tuple, _decode_tuple_projection,
)
from okto_grafx.domain.model.value import ValueType, encode_value, decode_value
from okto_grafx.domain.temporal_admission import native_value_capabilities


@pytest.mark.parametrize("coefficient,precision,scale", [(0, 1, 0), (-12345, 8, 3),
    (10**38 - 1, 38, 0), (1 - 10**38, 38, 38), (0, 38, 38)])
def test_golden_vectors_cursors_and_all_truncation_boundaries(coefficient, precision, scale):
    value = DecimalValue(coefficient, precision, scale)
    wire = bytes((18, precision, scale)) + coefficient.to_bytes(16, "little", signed=True)
    assert encode_decimal_value(value) == encode_value(value) == wire
    assert decode_decimal_value(wire) == decode_value(wire) == (value, 19)
    assert decode_decimal_value(b"before" + wire + b"after", 6) == (value, 25)
    for end in range(19):
        with pytest.raises(GrafxCorruptionDetected):
            decode_decimal_value(wire[:end])


@pytest.mark.parametrize("raw,offset", [(None, 0), (bytearray(19), 0), (memoryview(bytes(19)), 0),
    (bytes(19), True), (bytes(19), -1), (bytes(19), 1), (bytes(19), 0)])
def test_invalid_buffers_and_offsets_refuse(raw, offset):
    with pytest.raises(GrafxCorruptionDetected):
        decode_decimal_value(raw, offset)


@pytest.mark.parametrize("precision,scale,coefficient", [(0, 0, 0), (39, 0, 1), (5, 6, 1),
    (5, 0, 100000), (5, 0, -100000), (38, 0, (1 << 127) - 1), (38, 0, -(1 << 127))])
def test_invalid_components_refuse_even_when_projection_omits_value(precision, scale, coefficient):
    wire = bytes((18, precision, scale)) + coefficient.to_bytes(16, "little", signed=True)
    table = TableDef(1, "N", "node", (ColumnDef("val", SchemaType.ANY),))
    for decode in (decode_decimal_value, decode_value, lambda raw: decode_tuple(table, raw),
                   lambda raw: _decode_tuple_projection(table, raw, frozenset())):
        with pytest.raises(GrafxCorruptionDetected):
            decode(wire)


@pytest.mark.parametrize("offered", [DecimalValue(123, 5, 2), [DecimalValue(123, 5, 2)],
    {"nested": [DecimalValue(123, 5, 2)]}])
def test_any_nested_roundtrip_and_capability_discovery(offered):
    wire = encode_value(offered)
    expected = decode_value(wire)[0]
    table = TableDef(1, "N", "node", (ColumnDef("val", SchemaType.ANY),))
    assert decode_tuple(table, encode_tuple(table, (offered,))) == (expected,)
    assert native_value_capabilities((offered,)) == frozenset({DECIMAL_VALUES_CAPABILITY})


def test_forged_values_and_late_invalid_nested_members_refuse():
    value = DecimalValue(1, 5, 2)
    object.__setattr__(value, "coefficient", 10**50)
    for operation in (encode_decimal_value, encode_value, lambda v: native_value_capabilities((v,))):
        with pytest.raises(SchemaMismatchError):
            operation(value)
    with pytest.raises(SchemaMismatchError):
        native_value_capabilities(([DecimalValue(1, 5, 2), object()],))
    # Finiteness belongs to stored tuple admission, not spill-capable value encoding.
    with pytest.raises(SchemaMismatchError):
        encode_tuple(TableDef(1, "N", "node", (ColumnDef("val", SchemaType.ANY),)),
                     ([DecimalValue(1, 5, 2), float("nan")],))


def test_typed_assignment_exact_rescale_and_projected_metadata_validation():
    column = ColumnDef("val", ValueType.DECIMAL, decimal_precision=8, decimal_scale=3)
    table = TableDef(1, "N", "node", (column,))
    assert decode_tuple(table, encode_tuple(table, (DecimalValue(12, 2, 1),))) == (DecimalValue(1200, 8, 3),)
    for offered in (DecimalValue(12345, 5, 4), DecimalValue(10**10, 11, 0), 1, 1.0):
        with pytest.raises(SchemaMismatchError):
            encode_tuple(table, (offered,))
    # Numeric equality does not authorize a noncanonical declared type on disk.
    for wire in (encode_value(DecimalValue(12, 2, 1)), encode_value(3)):
        for decode in (lambda raw: decode_tuple(table, raw),
                       lambda raw: _decode_tuple_projection(table, raw, frozenset())):
            with pytest.raises(GrafxCorruptionDetected):
                decode(wire)


def test_catalog_roundtrip_preserves_parameters_and_requires_fence(monkeypatch):
    import okto_grafx.domain.model.catalog as module
    from okto_grafx.domain.page import crc32c
    table = TableDef(1, "N", "node", (ColumnDef("val", ValueType.DECIMAL,
                                               decimal_precision=38, decimal_scale=17),))
    catalog = Catalog().upgrade_index_catalog()
    catalog.add_table(table)
    wire = catalog.serialize()
    assert Catalog.deserialize(wire).table("N") == table
    assert catalog.requires_capability(DECIMAL_VALUES_CAPABILITY)
    # The capability word follows the existing v2 preamble (not a new layout).
    offset = module._PREAMBLE.size
    corrupt = bytearray(wire)
    bits = struct.unpack_from("<Q", corrupt, offset)[0]
    assert bits & DECIMAL_VALUES_CAPABILITY_BIT
    struct.pack_into("<Q", corrupt, offset, bits & ~DECIMAL_VALUES_CAPABILITY_BIT)
    struct.pack_into("<I", corrupt, len(corrupt) - 4, crc32c(bytes(corrupt[:-4])))
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(bytes(corrupt))
    monkeypatch.setattr(module, "_KNOWN_CAPABILITY_BITS", module._KNOWN_CAPABILITY_BITS & ~DECIMAL_VALUES_CAPABILITY_BIT)
    with pytest.raises(GrafxSchemaVersionMismatch):
        Catalog.deserialize(wire)


def test_random_full_width_roundtrip_and_mutations_are_canonical_or_refused():
    rng = random.Random(0xDEC1A1)
    for _ in range(1000):
        precision = rng.randrange(1, 39)
        value = DecimalValue(rng.randrange(1 - 10**precision, 10**precision), precision, rng.randrange(precision + 1))
        wire = encode_decimal_value(value)
        assert decode_decimal_value(wire) == (value, 19)
        mutated = bytearray(wire)
        mutated[rng.randrange(19)] ^= 1 << rng.randrange(8)
        try:
            decoded, following = decode_decimal_value(bytes(mutated))
        except GrafxCorruptionDetected:
            continue
        assert following == 19 and encode_decimal_value(decoded) == bytes(mutated)


@pytest.mark.parametrize("door", ["preflight", "apply"])
def test_reader_without_decimal_capability_refuses_before_any_recovery_apply(monkeypatch, door):
    import okto_grafx.domain.model.catalog as module
    from okto_grafx.domain.recovery.decision import committed_replay
    from okto_grafx.engine.commit_redo import CommitRedo
    from tests.storage_core.test_catalog_replay_images import records, staged
    value = Catalog().upgrade_index_catalog()._enable_native_value_capabilities(frozenset({DECIMAL_VALUES_CAPABILITY}))
    pool, store, images = staged(value, 1000)
    before, writes = store.read_from_pages().serialize(), list(pool.storage.write_calls)
    monkeypatch.setattr(module, "_KNOWN_CAPABILITY_BITS", module._KNOWN_CAPABILITY_BITS & ~DECIMAL_VALUES_CAPABILITY_BIT)
    def forbidden(*args, **kwargs):
        raise AssertionError("No data/catalog page apply before native capability preflight")
    monkeypatch.setattr("okto_grafx.engine.commit_redo.apply_page_image", forbidden)
    with pytest.raises(GrafxSchemaVersionMismatch):
        getattr(CommitRedo(pool), door)(committed_replay(records(images, 1000)))
    assert store.read_from_pages().serialize() == before
    assert pool.storage.write_calls == writes
