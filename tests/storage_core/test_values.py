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

import okto_grafx.domain.model.value as value_module

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxVectorValidationError,
)
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.value import (
    FLOAT32_OVERFLOW_THRESHOLD,
    INT64_MAX,
    INT64_MIN,
    MAX_FLOAT32,
    MAX_VALUE_DEPTH,
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
    ("unicode string", "graphs \u2192 vectors"),
    ("long string", "x" * 70000),
    ("empty bytes", b""),
    ("bytes", bytes(range(256))),
    ("empty list", ()),
    ("list", (1, "two", 3.0, None, True)),
    ("nested list", ((1, 2), (3, (4, 5)))),
    ("empty map", {}),
    ("map", {"a": 1, "b": "two"}),
    ("nested map", {"outer": {"inner": (1, 2, 3)}, "n": None}),
    ("map with non string keys", {1: "one", (2, 3): "pair", False: "no"}),
    ("timestamp", Timestamp(1_700_000_000_000_000)),
    ("timestamp zero", Timestamp(0)),
    ("timestamp negative", Timestamp(-1)),
    ("uuid", Uuid(bytes(range(16)))),
)


def test_decode_uses_the_closed_tag_table_without_constructing_an_enum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = encode_value(42)

    class NoEnumConstruction:
        def __getattr__(self, name: str) -> object:
            return getattr(ValueType, name)

        def __call__(self, tag: int) -> ValueType:
            raise AssertionError(f"ValueType({tag}) entered the decode hot path")

    monkeypatch.setattr(value_module, "ValueType", NoEnumConstruction())

    assert decode_value(raw) == (42, len(raw))
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
    assert [int(member) for member in ValueType] == list(range(19))
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


@pytest.mark.parametrize(
    ("kind", "dtype", "width"),
    (
        (ValueType.VECTOR_F32, "float32", 4),
        (ValueType.VECTOR_F64, "float64", 8),
    ),
)
def test_expected_vector_body_has_exact_generic_decoder_parity(
    kind: ValueType, dtype: str, width: int
) -> None:
    """Known-tag vector decoding preserves every result and corruption detail."""
    valid = encode_value(VectorValue((1.25, -2.5), space_ref=7, dtype=dtype))
    zero_dimension = bytes([int(kind)]) + (0).to_bytes(4, "little") + (7).to_bytes(
        4, "little"
    )
    oversized_dimension = (
        bytes([int(kind)])
        + (0xFFFFFFFF).to_bytes(4, "little")
        + (7).to_bytes(4, "little")
    )
    zero_space = valid[:5] + (0).to_bytes(4, "little") + valid[9:]
    largest_space = valid[:5] + (0xFFFFFFFF).to_bytes(4, "little") + valid[9:]
    payloads = (
        valid,
        zero_dimension,
        oversized_dimension,
        zero_space,
        largest_space,
        *(valid[:length] for length in range(1, 9)),
        *(valid[:length] for length in range(9, 9 + 2 * width)),
    )

    def outcome(call: object) -> tuple[object, ...]:
        try:
            value, following = call()  # type: ignore[operator]
        except BaseException as failure:
            cause = failure.__cause__
            return (
                "error",
                type(failure),
                getattr(failure, "message", str(failure)),
                dict(getattr(failure, "details", {})),
                None if cause is None else (type(cause), str(cause)),
            )
        return ("value", value, following)

    for payload in payloads:
        assert outcome(
            lambda payload=payload: value_module._decode_expected_value_body(
                payload, 1, kind
            )
        ) == outcome(lambda payload=payload: decode_value(payload))


@pytest.mark.parametrize(
    ("kind", "dtype"),
    (
        (ValueType.VECTOR_F32, "float32"),
        (ValueType.VECTOR_F64, "float64"),
    ),
)
def test_expected_vector_body_skips_redundant_generic_tag_dispatch(
    kind: ValueType, dtype: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded = encode_value(VectorValue((1.25, -2.5), space_ref=7, dtype=dtype))

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a proven vector tag must not be decoded again")

    monkeypatch.setattr(value_module, "decode_value", forbidden)

    restored, following = value_module._decode_expected_value_body(encoded, 1, kind)
    assert restored == VectorValue((1.25, -2.5), space_ref=7, dtype=dtype)
    assert following == len(encoded)


def test_decoding_past_the_end_of_a_buffer_is_refused() -> None:
    raw = encode_value("abcdef")
    for cut in range(1, len(raw)):
        with pytest.raises(GrafxCorruptionDetected):
            decode_value(raw[:cut])


def test_an_unknown_tag_is_refused() -> None:
    with pytest.raises(GrafxCorruptionDetected) as raised:
        decode_value(bytes([200]))
    assert raised.value.details["field"] == "tag"
    assert raised.value.details["value"] == 200
    assert raised.value.details["offset"] == 0


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


def test_a_string_with_no_utf8_encoding_is_refused_as_a_grafx_error() -> None:
    # A lone surrogate arrives from a lenient decoder, never from a keyboard: json.loads of an
    # escaped surrogate, or bytes.decode with errors set to surrogateescape. It must not leave
    # the write path as a raw UnicodeEncodeError (CONTRACT.md section 11 item 5).
    import json

    for text in (
        "\ud800",
        json.loads('"\ud800"'),
        b"caf\xe9".decode("utf-8", errors="surrogateescape"),
        "prefix\udfffsuffix",
    ):
        with pytest.raises(SchemaMismatchError) as raised:
            encode_value(text)
        assert raised.value.code == "schema_mismatch"
        assert "position" in raised.value.details


def test_a_surrogate_inside_a_container_is_refused_the_same_way() -> None:
    with pytest.raises(SchemaMismatchError):
        encode_value(("ok", "\ud800"))
    with pytest.raises(SchemaMismatchError):
        encode_value({"key": "\ud800"})
    with pytest.raises(SchemaMismatchError):
        encode_value({"\ud800": "value"})


def test_every_other_string_still_encodes() -> None:
    for text in ("", "ascii", "graphs and vectors", "\u00e9\u4e2d\U0001f600"):
        decoded, _offset = decode_value(encode_value(text))
        assert decoded == text


# --- a component the target dtype cannot hold ---------------------------------------------------


def test_a_component_too_large_for_a_float32_space_is_refused() -> None:
    # A float32 space is the default dtype (SPEC-VEC TR-4), and a finite double far above the
    # float32 range passes every other guard: it is not NaN, not an infinity and not out of
    # dimension. It would become an infinity on the page, which is exactly what BR-5 forbids,
    # and packing it raises a bare OverflowError from struct if nothing checks first.
    for component in (1e300, -1e39, 1e39, 3.5e38):
        with pytest.raises(GrafxVectorValidationError) as raised:
            encode_value(VectorValue((1.0, component), space_ref=1))
        assert raised.value.details["position"] == 1
        assert raised.value.details["dtype"] == "float32"
        assert raised.value.retryable is False


def test_the_same_component_is_fine_in_a_float64_space() -> None:
    vector = VectorValue((1e300, -1e39), space_ref=1, dtype="float64")
    decoded, _offset = decode_value(encode_value(vector))
    assert decoded.values == (1e300, -1e39)


def test_the_float32_boundary_is_exactly_where_struct_puts_it() -> None:
    # The limit is derived rather than guessed, so it is checked against the packer itself.
    import struct

    for candidate, packs in (
        (MAX_FLOAT32, True),
        (math.nextafter(FLOAT32_OVERFLOW_THRESHOLD, 0.0), True),
        (FLOAT32_OVERFLOW_THRESHOLD, False),
        (math.nextafter(FLOAT32_OVERFLOW_THRESHOLD, math.inf), False),
    ):
        for value in (candidate, -candidate):
            packed = True
            try:
                struct.pack("<f", value)
            except OverflowError:
                packed = False
            assert packed is packs, value
            accepted = True
            try:
                encode_value(VectorValue((value,), space_ref=1))
            except GrafxVectorValidationError:
                accepted = False
            assert accepted is packs, value


def test_a_vector_at_the_float32_maximum_still_round_trips() -> None:
    decoded, _offset = decode_value(encode_value(VectorValue((MAX_FLOAT32,), space_ref=1)))
    assert decoded.values == (MAX_FLOAT32,)


def test_a_row_with_an_unstorable_vector_component_never_reaches_a_page() -> None:
    from okto_grafx.domain.model.schema import ColumnDef, TableDef, encode_tuple

    table = TableDef(
        table_id=1,
        name="Chunk",
        kind="node",
        columns=(ColumnDef(name="embedding", type=ValueType.VECTOR_F32, vector_space="small"),),
    )
    with pytest.raises(GrafxVectorValidationError):
        encode_tuple(table, (VectorValue((1e300,), space_ref=1),))


# --- what this module does NOT check, stated so C9 can rely on it -------------------------------


def test_the_declared_dimension_of_a_space_is_not_checked_here() -> None:
    """A vector value carries no knowledge of the space it claims to belong to.

    The space really is declared here, with a dimension of four, and a table column really does
    point at it: that is the whole point, because the mismatch has to be built out of the real
    objects to show that nothing on this path can see it. VEC FR-1 assigns the check to the
    vector engine of C9. The test exists so the boundary is a decision on record rather than an
    oversight, and so a docstring here can never quietly claim it.
    """
    from okto_grafx.domain.model.catalog import Catalog
    from okto_grafx.domain.model.schema import (
        ColumnDef,
        EmbeddingSpaceDef,
        TableDef,
        decode_tuple,
        encode_tuple,
    )
    from okto_grafx.domain.ports.vectormath import DistanceMetric

    catalog = Catalog()
    space = catalog.add_space(
        EmbeddingSpaceDef(
            space_id=1,
            name="four_dimensional",
            dimension=4,
            metric=DistanceMetric.COSINE,
            normalized=False,
        )
    )
    table = catalog.add_table(
        TableDef(
            table_id=1,
            name="Chunk",
            kind="node",
            columns=(
                ColumnDef(
                    name="embedding",
                    type=ValueType.VECTOR_F32,
                    vector_space="four_dimensional",
                ),
            ),
        )
    )
    assert space.dimension == 4
    assert catalog.space(str(table.column("embedding").vector_space)).dimension == 4

    wrong_width = VectorValue((1.0, 2.0, 3.0), space_ref=space.space_id)
    payload = encode_tuple(table, (wrong_width,))
    restored = decode_tuple(table, payload)[0]
    assert restored.dimension == 3, "three components stored against a four-dimensional space"
    assert restored.space_ref == space.space_id


def test_the_module_never_claims_to_check_the_space() -> None:
    from okto_grafx.domain.model import value as module

    for text in (module.__doc__ or "", encode_value.__doc__ or ""):
        lowered = " ".join(text.lower().split())
        assert "validated here and nowhere else" not in lowered
        assert "cannot" in lowered or "not here" in lowered


@pytest.mark.parametrize(
    "claim",
    [
        "the dimension is at least one",
        "MAX_VECTOR_DIMENSION",
        "space reference",
        "finite",
        "storage dtype",
    ],
)
def test_the_module_says_exactly_what_it_does_check(claim: str) -> None:
    from okto_grafx.domain.model import value as module

    assert claim.lower() in " ".join((module.__doc__ or "").lower().split())


# --- the nesting budget --------------------------------------------------------------------------


def test_a_payload_of_nothing_but_container_tags_is_corruption_not_a_crash() -> None:
    """A few kilobytes of list tags unwind into thousands of nested calls.

    The interpreter answers that with a RecursionError, which is not a GrafxError, carries no
    location, and would leave C6 unable to classify the payload as a corruption incident at all
    (CONTRACT.md section 11 item 5).
    """
    blob = (bytes([int(ValueType.LIST)]) + (1).to_bytes(4, "little")) * 2000 + b"\x00"
    with pytest.raises(GrafxCorruptionDetected) as raised:
        decode_value(blob)
    assert raised.value.details["limit"] == MAX_VALUE_DEPTH
    assert "offset" in raised.value.details
    assert isinstance(raised.value, GrafxError)


def test_a_map_nested_past_the_budget_is_corruption_too() -> None:
    tag = bytes([int(ValueType.MAP)]) + (1).to_bytes(4, "little")
    blob = tag * 2000 + b"\x00"
    with pytest.raises(GrafxCorruptionDetected):
        decode_value(blob)


def test_nesting_up_to_the_budget_still_round_trips() -> None:
    value: object = 7
    for _ in range(MAX_VALUE_DEPTH - 1):
        value = (value,)
    decoded, _offset = decode_value(encode_value(value))
    assert decoded == value


def test_encoding_past_the_budget_is_refused_before_the_interpreter_gives_out() -> None:
    value: object = 7
    for _ in range(1500):
        value = (value,)
    with pytest.raises(SchemaMismatchError) as raised:
        encode_value(value)
    assert raised.value.details["limit"] == MAX_VALUE_DEPTH
    assert isinstance(raised.value, GrafxError)


def test_a_deeply_nested_map_key_is_refused_on_the_encode_side() -> None:
    key: object = 7
    for _ in range(1500):
        key = (key,)
    with pytest.raises(SchemaMismatchError):
        encode_value({key: "value"})


def test_a_corrupt_record_payload_surfaces_as_corruption_through_the_heap() -> None:
    # The read door of the heap, not just the value decoder: only a Grafx error may leave it.
    from okto_grafx.domain.model.schema import ColumnDef, TableDef

    from .conftest import MemoryDevice, RecordingMetrics, make_pool
    from okto_grafx.engine.catalog_store import CatalogStore
    from okto_grafx.engine.heap_store import HeapStore

    device = MemoryDevice()
    pool = make_pool(device, RecordingMetrics())
    catalog = CatalogStore(pool)
    catalog.bootstrap()
    table = TableDef(
        table_id=1,
        name="Blob",
        kind="node",
        columns=(ColumnDef(name="body", type=ValueType.LIST),),
    )
    catalog.catalog.add_table(table)
    catalog.save()
    heap = HeapStore(pool, catalog)
    heap.bootstrap()
    ref = heap.insert(table, 1, ((1, 2, 3),), xmin=5)

    nested = (bytes([int(ValueType.LIST)]) + (1).to_bytes(4, "little")) * 70 + b"\x00"
    with pool.pinned(heap.file, ref.page) as page:
        content = bytearray(page.read_slot(ref.slot))
        header = content[:40]
        header[4:8] = len(nested).to_bytes(4, "little")
        page.update_slot(ref.slot, bytes(header) + nested)
    with pytest.raises(GrafxCorruptionDetected):
        heap.read(ref)


@pytest.mark.parametrize("component", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_component_is_refused_in_a_double_precision_space(
    component: float,
) -> None:
    # The float32 range check happens to reject non-finite values as a side effect, so the
    # finiteness rule was only ever exercised through single precision. A float64 space has no
    # range to fall back on: without the explicit check a NaN is packed and persisted, which is
    # exactly what SPEC-VEC BR-5 forbids.
    vector = VectorValue((1.0, component, 3.0), space_ref=1, dtype="float64")
    with pytest.raises(GrafxVectorValidationError) as raised:
        encode_value(vector)
    assert raised.value.details["position"] == 1
    assert raised.value.details["field"] == "values"


def test_a_double_precision_vector_of_finite_components_still_encodes() -> None:
    vector = VectorValue((1e300, -1e300, 0.0), space_ref=1, dtype="float64")
    decoded, _offset = decode_value(encode_value(vector))
    assert decoded.values == (1e300, -1e300, 0.0)


# --- boundaries, not distant approximations -------------------------------------------------------


def nested(depth: int) -> object:
    """Return a list nested exactly that many levels deep."""
    value: object = 7
    for _ in range(depth):
        value = (value,)
    return value


def test_the_nesting_budget_accepts_exactly_the_depth_it_allows() -> None:
    # A test that stops well short of the boundary cannot tell 64 from 63 or from 6400, so it
    # cannot see the budget move. These two sit on either side of it.
    value = nested(MAX_VALUE_DEPTH)
    decoded, _offset = decode_value(encode_value(value))
    assert decoded == value


def test_the_nesting_budget_refuses_exactly_one_level_past_it() -> None:
    with pytest.raises(SchemaMismatchError) as raised:
        encode_value(nested(MAX_VALUE_DEPTH + 1))
    assert raised.value.details["limit"] == MAX_VALUE_DEPTH


def test_the_decoder_accepts_the_deepest_payload_an_encoder_could_write() -> None:
    payload = encode_value(nested(MAX_VALUE_DEPTH))
    decoded, offset = decode_value(payload)
    assert offset == len(payload)
    assert decoded == nested(MAX_VALUE_DEPTH)


def test_the_decoder_refuses_a_payload_one_level_deeper_than_that() -> None:
    # Built by hand, because the encoder will not produce it: one more list tag than the budget.
    payload = (
        (bytes([int(ValueType.LIST)]) + (1).to_bytes(4, "little")) * (MAX_VALUE_DEPTH + 2)
        + bytes([int(ValueType.INT64)])
        + (7).to_bytes(8, "little", signed=True)
    )
    with pytest.raises(GrafxCorruptionDetected) as raised:
        decode_value(payload)
    assert raised.value.details["limit"] == MAX_VALUE_DEPTH


def test_a_negative_offset_never_decodes_from_the_end_of_the_buffer() -> None:
    # Without the lower bound a negative offset indexes backwards from the end and decodes
    # whatever happens to be there, which is a wrong answer rather than an error.
    payload = encode_value("abcdef")
    with pytest.raises(GrafxCorruptionDetected) as raised:
        decode_value(payload, -len(payload))
    assert raised.value.details["offset"] == -len(payload)
    for offset in (-1, -100):
        with pytest.raises(GrafxCorruptionDetected):
            decode_value(payload, offset)
