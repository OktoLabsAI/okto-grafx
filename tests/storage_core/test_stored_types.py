"""Recursive type foundation: canonical schema bytes and exact owned assignments."""

from dataclasses import replace
from decimal import localcontext, Inexact, Rounded
from types import MappingProxyType
import random

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.stored_types import (
    StoredType, MAX_STORED_TYPE_BYTES, encode_stored_type, decode_stored_type,
    normalize_typed_value, validate_typed_value,
)
from okto_grafx.domain.model.value import ValueType, Timestamp, Uuid, VectorValue, encode_value, decode_value
from okto_grafx.domain.model.decimal_values import DecimalValue
from okto_grafx.domain.model.temporal_values import DateValue

INTEGER = StoredType("INT64", nullable=False)
AMOUNT = StoredType("DECIMAL", precision=12, scale=4)
RECORD = StoredType("STRUCT", fields=(
    ("id", INTEGER), ("amount", AMOUNT),
    ("samples", StoredType("ARRAY", element=StoredType("DOUBLE"), length=2)),
    ("tags", StoredType("MAP", element=StoredType("LIST", element=StoredType("STRING", nullable=False)))),
))


@pytest.mark.parametrize("descriptor", [
    *(StoredType(name) for name in ("BOOL", "INT64", "DOUBLE", "STRING", "BYTES", "TIMESTAMP", "UUID",
                                  "DATE", "LOCALTIME", "TIME", "LOCALDATETIME", "DATETIME", "DURATION", "ANY")),
    INTEGER, AMOUNT, StoredType("LIST", element=INTEGER), StoredType("MAP", element=AMOUNT),
    StoredType("ARRAY", element=INTEGER, length=0), StoredType("ARRAY", element=INTEGER, length=2**32-1),
    StoredType("STRUCT"), RECORD,
])
def test_descriptor_canonical_codec_and_query_family(descriptor):
    frame = encode_stored_type(descriptor)
    actual = decode_stored_type(frame)
    assert actual == descriptor and actual is not descriptor
    assert encode_stored_type(actual) == frame
    expected = (None if descriptor.kind == "ANY" else ValueType.LIST if descriptor.kind == "ARRAY"
                else ValueType.MAP if descriptor.kind == "STRUCT" else ValueType[descriptor.kind])
    assert actual.value_type == expected


@pytest.mark.parametrize("options", [
    {"kind": "integer"}, {"kind": "VECTOR_F32"}, {"kind": "NULL"}, {"kind": []},
    {"kind": "INT64", "nullable": 1}, {"kind": "INT64", "element": INTEGER},
    {"kind": "INT64", "fields": (("x", INTEGER),)}, {"kind": "INT64", "length": 1},
    {"kind": "INT64", "precision": 12}, {"kind": "LIST"}, {"kind": "MAP", "element": "INT64"},
    {"kind": "ARRAY", "element": INTEGER}, {"kind": "ARRAY", "element": INTEGER, "length": True},
    {"kind": "ARRAY", "element": INTEGER, "length": -1}, {"kind": "ARRAY", "element": INTEGER, "length": 2**32},
    {"kind": "DECIMAL"}, {"kind": "DECIMAL", "precision": True, "scale": 0},
    {"kind": "DECIMAL", "precision": 39, "scale": 0}, {"kind": "DECIMAL", "precision": 12, "scale": 13},
    {"kind": "STRUCT", "fields": []}, {"kind": "STRUCT", "fields": (("x", "INT64"),)},
    {"kind": "STRUCT", "fields": (("x", INTEGER), ("x", INTEGER))},
    {"kind": "STRUCT", "fields": (("", INTEGER),)}, {"kind": "STRUCT", "fields": (("1x", INTEGER),)},
    {"kind": "STRUCT", "fields": (("á", INTEGER),)}, {"kind": "STRUCT", "fields": (("x"*129, INTEGER),)},
    {"kind": "STRUCT", "fields": (("x",),)}, {"kind": "STRUCT", "fields": tuple((f"f{i}", INTEGER) for i in range(257))},
])
def test_invalid_descriptor_refused_without_host_coercion(options):
    with pytest.raises(GrafxConfigurationError):
        StoredType(**options)


def test_nested_exact_assignment_and_strict_stored_validation():
    original = {"id": 1, "amount": DecimalValue(125, 3, 2), "samples": [1.5, None], "tags": {"x": ["a", "b"]}}
    with localcontext() as context:
        context.prec = 1
        context.traps[Inexact] = context.traps[Rounded] = True
        actual = normalize_typed_value(RECORD, original)
    assert actual == {"id": 1, "amount": DecimalValue(12500, 12, 4), "samples": (1.5, None), "tags": {"x": ("a", "b")}}
    validate_typed_value(RECORD, actual)
    raw = encode_value(actual)
    recovered, position = decode_value(raw)
    assert position == len(raw) and recovered == actual
    validate_typed_value(RECORD, recovered)
    original["samples"][0] = 3.5
    original["tags"]["x"].append("changed")
    assert actual["samples"] == (1.5, None) and actual["tags"]["x"] == ("a", "b")
    with pytest.raises(SchemaMismatchError, match="stored type"):
        validate_typed_value(RECORD, original)  # Stored coordinates must not be rescaled on read.


def test_nullable_struct_defaults_do_not_repair_missing_stored_fields():
    actual = normalize_typed_value(RECORD, {"id": 1})
    assert actual == {"id": 1, "amount": None, "samples": None, "tags": None}
    validate_typed_value(RECORD, actual)
    with pytest.raises(SchemaMismatchError) as failure:
        validate_typed_value(RECORD, {"id": 1})
    assert failure.value.details["reason"] == "missing_stored_struct_field"
    with pytest.raises(SchemaMismatchError):
        normalize_typed_value(RECORD, {})


@pytest.mark.parametrize("bad", [
    {"id": True}, {"id": 1, "extra": None}, {"id": 1, "samples": [1.0]},
    {"id": 1, "samples": [1, 2]}, {"id": 1, "samples": [float("nan"), 1.0]},
    {"id": 1, "amount": 1.25}, {"id": 1, "amount": DecimalValue(123456, 6, 5)},
    {"id": 1, "amount": DecimalValue(10**12, 13, 4)},
    {"id": 1, "tags": {1: ["x"]}}, {"id": 1, "tags": {"x": [None]}},
    {"id": 1, "tags": {"x": ["\ud800"]}}, {"id": 1, "tags": {"\ud800": []}},
])
def test_invalid_nested_leaf_shape_and_decimal_assignment(bad):
    with pytest.raises(SchemaMismatchError):
        normalize_typed_value(RECORD, bad)


@pytest.mark.parametrize("kind,value", [
    ("BOOL", True), ("INT64", -(2**63)), ("DOUBLE", 1.25), ("STRING", "wide 🌎"),
    ("BYTES", b"bytes"), ("TIMESTAMP", Timestamp(-1234)), ("UUID", Uuid(bytes(16))),
    ("DATE", DateValue(2024, 2, 29)), ("DECIMAL", DecimalValue(12500, 12, 4)),
])
def test_leaf_types_in_list_map_array_struct(kind, value):
    scalar = AMOUNT if kind == "DECIMAL" else StoredType(kind)
    for descriptor, original in (
        (StoredType("LIST", element=scalar), [value, None]),
        (StoredType("MAP", element=scalar), {"v": value, "null": None}),
        (StoredType("ARRAY", element=scalar, length=2), [None, value]),
        (StoredType("STRUCT", fields=(("v", scalar),)), {"v": value}),
    ):
        actual = normalize_typed_value(descriptor, original)
        validate_typed_value(descriptor, actual)
        recovered = decode_value(encode_value(actual))[0]
        validate_typed_value(decode_stored_type(encode_stored_type(descriptor)), recovered)
        assert recovered == actual


def test_any_nested_native_values_are_owned_per_occurrence():
    value = DecimalValue(125, 3, 2)
    shared = {"value": value, "date": DateValue(2024, 2, 29)}
    actual = normalize_typed_value(StoredType("ANY"), [shared, shared])
    assert actual[0] == actual[1] and actual[0] is not actual[1]
    assert actual[0]["value"] is not value and actual[0]["value"] is not actual[1]["value"]
    object.__setattr__(value, "coefficient", 1)
    shared["date"] = None
    assert actual[0]["value"].coefficient == 125
    assert actual[0]["date"] == DateValue(2024, 2, 29)
    validate_typed_value(StoredType("ANY"), actual)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), VectorValue((1.0,), 1), object()])
def test_any_cannot_bypass_nonfinite_or_vector_space_authority(bad):
    for wrapped in (bad, [bad], {"deep": [bad]}):
        with pytest.raises(SchemaMismatchError):
            normalize_typed_value(StoredType("ANY"), wrapped)
        with pytest.raises(SchemaMismatchError):
            validate_typed_value(StoredType("ANY"), wrapped)


def test_forged_container_subclasses_do_not_bypass_any_validation():
    class CustomList(list):
        pass
    with pytest.raises(SchemaMismatchError):
        normalize_typed_value(StoredType("ANY"), CustomList([float("nan")]))


def test_read_only_maps_are_owned_and_ordinary_dict_output_remains_independent():
    source = {"v": [1, 2]}
    descriptor = StoredType("MAP", element=StoredType("LIST", element=INTEGER))
    actual = normalize_typed_value(descriptor, MappingProxyType(source))
    source["v"].append(3)
    assert actual == {"v": (1, 2)}


def test_descriptor_cycles_depth_nodes_and_bytes_are_bounded():
    cyclic = StoredType("LIST", element=INTEGER)
    object.__setattr__(cyclic, "element", cyclic)
    for operation in (encode_stored_type, lambda d: normalize_typed_value(d, []), lambda d: validate_typed_value(d, [])):
        with pytest.raises(GrafxConfigurationError):
            operation(cyclic)
    depth = INTEGER
    for _ in range(64):
        depth = StoredType("LIST", element=depth)
    assert decode_stored_type(encode_stored_type(depth)) == depth
    with pytest.raises(GrafxConfigurationError):
        StoredType("LIST", element=depth)
    # Shared subtrees count at each occurrence, not only by unique Python identity.
    wide = StoredType("STRUCT", fields=tuple((f"f{i}", INTEGER) for i in range(256)))
    with pytest.raises(GrafxConfigurationError):
        StoredType("STRUCT", fields=tuple((f"f{i}", wide) for i in range(16)))
    long_names = StoredType("STRUCT", fields=tuple((f"f{i}_" + "x"*120, INTEGER) for i in range(256)))
    three = StoredType("STRUCT", fields=(("a", long_names), ("b", long_names), ("c", long_names)))
    with pytest.raises(GrafxConfigurationError) as failure:
        encode_stored_type(three)
    assert failure.value.details["reason"] == "descriptor_bytes"


def test_cyclic_and_excessively_nested_values_are_typed_refusals():
    value = []
    value.append(value)
    for operation in (normalize_typed_value, validate_typed_value):
        with pytest.raises(SchemaMismatchError) as failure:
            operation(StoredType("ANY"), value)
        assert failure.value.details["reason"] == "value_depth"


def test_malformed_descriptor_truncations_flags_tags_names_and_trailing_data():
    frame = encode_stored_type(RECORD)
    bad = [frame[:end] for end in range(len(frame))]
    bad += [frame + b"\x00", b"BAD!" + frame[4:], frame[:4] + b"\x00" + frame[5:],
            frame[:5] + b"\x02" + frame[6:], b"GXT1\xfd\x01\xff\xff",
            b"GXT1\xfd\x01\x01\x00\x01\xff\x02\x01", b"x"*(MAX_STORED_TYPE_BYTES+1)]
    decimal = encode_stored_type(AMOUNT)
    bad += [decimal[:-2]+b"\x00\x00", decimal[:-1]+b"\x0d"]
    # Two fields named x; valid individually but not as a stored struct.
    bad += [b"GXT1\xfd\x01\x02\x00" + b"\x01x\x02\x01"*2]
    for malformed in bad:
        with pytest.raises(GrafxCorruptionDetected):
            decode_stored_type(malformed)
    for malformed in (None, "GXT1", bytearray(frame)):
        with pytest.raises(GrafxCorruptionDetected):
            decode_stored_type(malformed)


def test_deterministic_descriptor_mutation_fuzz_never_escapes_typed_errors():
    rng = random.Random(61)
    frame = encode_stored_type(RECORD)
    for _ in range(300):
        raw = bytearray(frame)
        for _ in range(rng.randrange(1, 5)):
            raw[rng.randrange(len(raw))] = rng.randrange(256)
        try:
            decoded = decode_stored_type(bytes(raw))
        except GrafxCorruptionDetected:
            continue
        assert encode_stored_type(decoded) == bytes(raw)


def test_assignment_validates_forged_native_scalar_and_preserves_no_aliases():
    value = DecimalValue(1, 12, 4)
    normalized = normalize_typed_value(AMOUNT, value)
    assert normalized == value and normalized is not value
    object.__setattr__(value, "coefficient", 10**12)
    with pytest.raises(SchemaMismatchError):
        normalize_typed_value(AMOUNT, value)
    with pytest.raises(SchemaMismatchError):
        validate_typed_value(AMOUNT, value)
    with pytest.raises(GrafxConfigurationError):
        replace(AMOUNT, scale=13)
