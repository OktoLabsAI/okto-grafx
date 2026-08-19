"""The value system: every type, its edges, and the vector contract (CONTRACT.md section 7.1).

The encoding is the durable part of the engine, so the tests are written against the bytes as
well as against the round trip: a tag that changes value silently makes every stored row of an
older database unreadable.

The vector rules come from SPEC-VEC BR-5 and are one-directional on purpose. A vector is judged
on the way in, where refusing it costs nothing and persists nothing; a read never judges it
again, because a value that was refused was never stored.
"""

from __future__ import annotations

import math

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxVectorValidationError
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.value import (
    INT64_MAX,
    INT64_MIN,
    MAX_VECTOR_DIMENSION,
    Timestamp,
    Uuid,
    ValueType,
    VectorValue,
    decode_value,
    decode_values,
    encode_value,
    encode_values,
    value_type_of,
)

ROUND_TRIP_CASES: tuple[tuple[str, object], ...] = (
    ("null", None),
    ("true", True),
    ("false", False),
    ("zero", 0),
    ("int64 max", INT64_MAX),
    ("int64 min", INT64_MIN),
    ("negative", -1),
    ("double", 3.5),
    ("double zero", 0.0),
    ("negative zero", -0.0),
    ("empty string", ""),
    ("string", "Ada Lovelace"),
    ("unicode string", "graphs → vectors"),
    ("long string", "x" * 70000),
    ("empty bytes", b""),
    ("bytes", bytes(range(256))),
    ("empty list", ()),
    ("list", (1, "two", 3.0, None, True)),
    ("nested list", ((1, 2), (3, (4, 5)))),
    ("empty map", {}),
    ("map", {"a": 1, "b": "two"}),
    ("nested map", {"outer": {"inner": (1, 2, 3)}, "n": None}),
    ("map with non string keys", {1: "one", (2, 3): "pair", True: "yes"}),
    ("timestamp", Timestamp(1_700_000_000_000_000)),
    ("timestamp zero", Timestamp(0)),
    ("timestamp negative", Timestamp(-1)),
    ("uuid", Uuid(bytes(range(16)))),
)
"""One case per shape the encoder must survive, edges included."""


@pytest.mark.parametrize(
    ("label", "value"), ROUND_TRIP_CASES, ids=[row[0] for row in ROUND_TRIP_CASES]
)
def test_every_value_round_trips(label: str, value: object) -> None:
    raw = encode_value(value)
    decoded, offset = decode_value(raw, 0)
    assert offset == len(raw), label
    assert decoded == value, label
    assert type(decoded) is type(value) or isinstance(value, (list, tuple)), label


def test_the_tag_of_every_type_is_the_one_the_contract_froze() -> None:
    assert [int(member) for member in ValueType] == list(range(12))
    assert ValueType.NULL == 0
    assert ValueType.BOOL == 1
    assert ValueType.INT64 == 2
    assert ValueType.DOUBLE == 3
    assert ValueType.STRING == 4
    assert ValueType.BYTES == 5
    assert ValueType.LIST == 6
    assert ValueType.MAP == 7
    assert ValueType.VECTOR_F32 == 8
    assert ValueType.VECTOR_F64 == 9
    assert ValueType.TIMESTAMP == 10
    assert ValueType.UUID == 11


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ValueType.NULL),
        (True, ValueType.BOOL),
        (7, ValueType.INT64),
        (7.0, ValueType.DOUBLE),
        ("x", ValueType.STRING),
        (b"x", ValueType.BYTES),
        ((), ValueType.LIST),
        ([], ValueType.LIST),
        ({}, ValueType.MAP),
        (Timestamp(0), ValueType.TIMESTAMP),
        (Uuid(bytes(16)), ValueType.UUID),
        (VectorValue((1.0,), space_ref=1), ValueType.VECTOR_F32),
        (VectorValue((1.0,), space_ref=1, dtype="float64"), ValueType.VECTOR_F64),
    ],
)
def test_the_value_type_of_a_python_object(value: object, expected: ValueType) -> None:
    assert value_type_of(value) is expected
    assert encode_value(value)[0] == int(expected)


def test_a_boolean_is_not_encoded_as_an_integer() -> None:
    assert encode_value(True)[0] == int(ValueType.BOOL)
    assert encode_value(1)[0] == int(ValueType.INT64)
    assert decode_value(encode_value(True))[0] is True
    assert decode_value(encode_value(1))[0] == 1


def test_a_list_decodes_to_a_tuple() -> None:
    decoded, _offset = decode_value(encode_value([1, 2, 3]))
    assert decoded == (1, 2, 3)
    assert isinstance(decoded, tuple)


def test_an_object_with_no_encoding_is_refused() -> None:
    with pytest.raises(SchemaMismatchError):
        encode_value(object())
    with pytest.raises(SchemaMismatchError):
        value_type_of({1, 2})


def test_an_integer_outside_sixty_four_bits_is_refused() -> None:
    with pytest.raises(SchemaMismatchError):
        encode_value(INT64_MAX + 1)
    with pytest.raises(SchemaMismatchError):
        encode_value(INT64_MIN - 1)


def test_a_double_may_be_a_special_value() -> None:
    # Only vectors forbid non-finite components; a DOUBLE column stores what it is given.
    decoded, _offset = decode_value(encode_value(float("inf")))
    assert decoded == float("inf")
    decoded, _offset = decode_value(encode_value(float("nan")))
    assert math.isnan(decoded)


def test_values_encode_and_decode_positionally() -> None:
    values = (1, "two", None, 4.0)
    raw = encode_values(values)
    decoded, offset = decode_values(raw, len(values))
    assert decoded == values
    assert offset == len(raw)


def test_decoding_past_the_end_of_a_buffer_is_refused() -> None:
    raw = encode_value("abcdef")
    for cut in range(1, len(raw)):
        with pytest.raises(GrafxCorruptionDetected):
            decode_value(raw[:cut])


def test_an_unknown_tag_is_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected) as raised:
        decode_value(bytes([200]))
    assert raised.value.details["field"] == "tag"


def test_a_boolean_body_outside_zero_and_one_is_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected):
        decode_value(bytes([int(ValueType.BOOL), 2]))


def test_a_string_that_is_not_utf8_is_refused() -> None:
    raw = bytearray(encode_value("abc"))
    raw[-1] = 0xFF
    raw[-2] = 0xC3
    raw[-3] = 0xFF
    with pytest.raises(GrafxCorruptionDetected):
        decode_value(bytes(raw))


def test_a_declared_length_larger_than_the_buffer_is_refused() -> None:
    raw = bytearray(encode_value(b"abc"))
    raw[1:5] = (1 << 20).to_bytes(4, "little")
    with pytest.raises(GrafxCorruptionDetected) as raised:
        decode_value(bytes(raw))
    assert raised.value.details["needed"] == 1 << 20


# --- vectors -----------------------------------------------------------------------------------


def test_a_vector_round_trips_with_its_space_and_dimension() -> None:
    vector = VectorValue((0.5, -1.5, 2.25), space_ref=7)
    raw = encode_value(vector)
    decoded, offset = decode_value(raw)
    assert offset == len(raw)
    assert decoded == vector
    assert decoded.space_ref == 7
    assert decoded.dimension == 3
    assert decoded.dtype == "float32"


def test_a_double_precision_vector_keeps_full_precision() -> None:
    vector = VectorValue((0.1, 0.2, 0.3), space_ref=2, dtype="float64")
    decoded, _offset = decode_value(encode_value(vector))
    assert decoded.values == (0.1, 0.2, 0.3)
    assert decoded.dtype == "float64"


def test_a_single_precision_vector_is_rounded_to_single_precision() -> None:
    decoded, _offset = decode_value(encode_value(VectorValue((0.1,), space_ref=2)))
    assert decoded.values[0] != 0.1
    assert abs(decoded.values[0] - 0.1) < 1e-7


def test_the_vector_body_carries_the_dimension_and_the_space_reference() -> None:
    raw = encode_value(VectorValue((1.0, 2.0), space_ref=9))
    assert raw[0] == int(ValueType.VECTOR_F32)
    assert int.from_bytes(raw[1:5], "little") == 2
    assert int.from_bytes(raw[5:9], "little") == 9
    assert len(raw) == 1 + 4 + 4 + 2 * 4


def test_a_vector_at_the_maximum_dimension_round_trips() -> None:
    values = tuple(float(index % 8) for index in range(MAX_VECTOR_DIMENSION))
    vector = VectorValue(values, space_ref=1)
    raw = encode_value(vector)
    assert len(raw) == 1 + 8 + MAX_VECTOR_DIMENSION * 4
    decoded, offset = decode_value(raw)
    assert offset == len(raw)
    assert decoded.dimension == MAX_VECTOR_DIMENSION
    assert decoded.values == values


def test_a_vector_beyond_the_maximum_dimension_is_refused() -> None:
    vector = VectorValue(tuple(0.0 for _ in range(MAX_VECTOR_DIMENSION + 1)), space_ref=1)
    with pytest.raises(GrafxVectorValidationError) as raised:
        encode_value(vector)
    assert raised.value.details["field"] == "dimension"


def test_an_empty_vector_is_refused() -> None:
    with pytest.raises(GrafxVectorValidationError):
        encode_value(VectorValue((), space_ref=1))


@pytest.mark.parametrize("component", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_component_is_refused_at_encode_time(component: float) -> None:
    vector = VectorValue((1.0, component, 3.0), space_ref=1)
    with pytest.raises(GrafxVectorValidationError) as raised:
        encode_value(vector)
    assert raised.value.details["position"] == 1
    assert raised.value.retryable is False


def test_a_vector_with_no_space_is_refused() -> None:
    with pytest.raises(GrafxVectorValidationError) as raised:
        encode_value(VectorValue((1.0,), space_ref=0))
    assert raised.value.details["field"] == "space_ref"


def test_a_space_reference_beyond_thirty_two_bits_is_refused() -> None:
    with pytest.raises(GrafxVectorValidationError):
        encode_value(VectorValue((1.0,), space_ref=1 << 32))


def test_an_unknown_storage_dtype_is_refused_at_construction() -> None:
    with pytest.raises(GrafxVectorValidationError):
        VectorValue((1.0,), space_ref=1, dtype="float16")


def test_a_vector_normalises_its_components_to_floats() -> None:
    vector = VectorValue([1, 2, 3], space_ref=1)
    assert vector.values == (1.0, 2.0, 3.0)
    assert all(isinstance(component, float) for component in vector.values)


def test_a_vector_of_something_that_is_not_numbers_is_refused() -> None:
    with pytest.raises(GrafxVectorValidationError):
        VectorValue("123", space_ref=1)  # type: ignore[arg-type]
    with pytest.raises(GrafxVectorValidationError):
        VectorValue(("a", "b"), space_ref=1)  # type: ignore[arg-type]


def test_a_stored_vector_is_never_judged_again_on_the_way_out() -> None:
    # A non-finite component cannot be stored, so the read path has nothing to defend against.
    # Building the same shape by hand proves the decode path does not re-run the write rules.
    import struct

    body = struct.pack("<BIIff", int(ValueType.VECTOR_F32), 2, 3, float("nan"), 1.0)
    decoded, offset = decode_value(body)
    assert offset == len(body)
    assert math.isnan(decoded.values[0])
    assert decoded.space_ref == 3


def test_a_truncated_vector_body_is_refused() -> None:
    raw = encode_value(VectorValue((1.0, 2.0, 3.0), space_ref=1))
    with pytest.raises(GrafxCorruptionDetected):
        decode_value(raw[:-1])


# --- timestamps and identifiers ------------------------------------------------------------------


def test_a_timestamp_converts_to_and_from_a_wall_reading() -> None:
    assert Timestamp.from_wall(1.5).micros == 1_500_000
    assert Timestamp(1_500_000).to_wall() == 1.5
    assert Timestamp.from_wall(0.0).micros == 0


def test_a_timestamp_outside_sixty_four_signed_bits_is_refused() -> None:
    with pytest.raises(SchemaMismatchError):
        Timestamp(1 << 63)
    with pytest.raises(SchemaMismatchError):
        Timestamp("now")  # type: ignore[arg-type]
    with pytest.raises(SchemaMismatchError):
        Timestamp.from_wall(float("inf"))


def test_an_identifier_is_sixteen_bytes_and_prints_canonically() -> None:
    identifier = Uuid.from_hex("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
    assert len(identifier.raw) == 16
    assert identifier.canonical() == "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
    assert identifier.hex == "6ba7b8109dad11d180b400c04fd430c8"
    assert Uuid.from_hex(identifier.hex) == identifier


def test_an_identifier_of_the_wrong_size_is_refused() -> None:
    with pytest.raises(SchemaMismatchError):
        Uuid(b"short")
    with pytest.raises(SchemaMismatchError):
        Uuid.from_hex("abcd")
    with pytest.raises(SchemaMismatchError):
        Uuid.from_hex("z" * 32)
    with pytest.raises(SchemaMismatchError):
        Uuid("not bytes")  # type: ignore[arg-type]


def test_an_identifier_survives_a_round_trip_through_bytes() -> None:
    identifier = Uuid(bytes(range(16)))
    decoded, _offset = decode_value(encode_value(identifier))
    assert decoded == identifier
    assert decoded.raw == identifier.raw
