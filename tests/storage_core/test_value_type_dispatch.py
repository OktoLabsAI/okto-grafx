"""The exact-type table in value_type_of answers exactly what the isinstance walk answers."""

from __future__ import annotations

from collections import namedtuple
from enum import IntEnum
from types import MappingProxyType

import pytest

import okto_grafx.domain.model.value as value_module
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.value import (
    Timestamp,
    Uuid,
    ValueType,
    VectorValue,
    encode_value,
    value_type_of,
)


class Flag(IntEnum):
    ONE = 1


class Text(str):
    pass


class Blob(bytes):
    pass


class Real(float):
    pass


class Pairs(dict):
    pass


class Items(list):
    pass


Point = namedtuple("Point", ["x", "y"])


class Opaque:
    pass


EXACT = [
    True,
    False,
    0,
    -(2**63),
    2**63 - 1,
    1.5,
    float("nan"),
    "",
    "texto",
    b"",
    b"bytes",
    bytearray(b"ba"),
    Timestamp(0),
    Uuid(bytes(16)),
    (),
    (1, "a"),
    [],
    [1.0, None],
    {},
    {"k": 1},
    VectorValue((1.0,), space_ref=1),
    VectorValue((1.0, 2.0), space_ref=1, dtype="float64"),
    None,
]
SUBCLASSES = [
    Flag.ONE,
    Text("t"),
    Blob(b"b"),
    Real(2.5),
    Pairs(k=1),
    Items([1]),
    Point(1, 2),
]


def _walk(value: object) -> ValueType:
    """The answer of the isinstance walk alone, with the exact-type table emptied."""
    saved = value_module._EXACT_VALUE_TYPES
    value_module._EXACT_VALUE_TYPES = MappingProxyType({})
    try:
        return value_type_of(value)  # type: ignore[arg-type]
    finally:
        value_module._EXACT_VALUE_TYPES = saved


@pytest.mark.parametrize("value", EXACT + SUBCLASSES, ids=repr)
def test_table_and_walk_agree_on_type_and_bytes(value: object) -> None:
    assert value_type_of(value) is _walk(value)  # type: ignore[arg-type]
    expected = encode_value(value)  # type: ignore[arg-type]
    saved = value_module._EXACT_VALUE_TYPES
    value_module._EXACT_VALUE_TYPES = MappingProxyType({})
    try:
        assert encode_value(value) == expected  # type: ignore[arg-type]
    finally:
        value_module._EXACT_VALUE_TYPES = saved


def test_table_keys_are_exact_classes_the_walk_answers_unconditionally() -> None:
    table = value_module._EXACT_VALUE_TYPES
    assert isinstance(table, MappingProxyType)
    with pytest.raises(TypeError):
        table[Opaque] = ValueType.NULL  # type: ignore[index]
    assert set(table) == {
        bool,
        int,
        float,
        str,
        bytes,
        bytearray,
        Timestamp,
        Uuid,
        tuple,
        list,
        dict,
    }
    samples = {
        bool: True,
        int: 1,
        float: 1.0,
        str: "s",
        bytes: b"b",
        bytearray: bytearray(b"b"),
        Timestamp: Timestamp(0),
        Uuid: Uuid(bytes(16)),
        tuple: (),
        list: [],
        dict: {},
    }
    for kind, sample in samples.items():
        assert type(sample) is kind
        assert table[kind] is _walk(sample)


def test_bool_stays_bool_and_int_subclasses_stay_int64() -> None:
    assert value_type_of(True) is ValueType.BOOL
    assert value_type_of(1) is ValueType.INT64
    assert value_type_of(Flag.ONE) is ValueType.INT64
    assert encode_value(Flag.ONE) == encode_value(1)


def test_vectors_follow_their_dtype_not_the_table() -> None:
    assert VectorValue not in value_module._EXACT_VALUE_TYPES
    assert value_type_of(VectorValue((1.0,), space_ref=1)) is ValueType.VECTOR_F32
    assert (
        value_type_of(VectorValue((1.0,), space_ref=1, dtype="float64"))
        is ValueType.VECTOR_F64
    )


def test_unknown_objects_are_refused_with_the_walk_message() -> None:
    with pytest.raises(SchemaMismatchError, match="no stored value type for a Opaque"):
        value_type_of(Opaque())  # type: ignore[arg-type]
    with pytest.raises(SchemaMismatchError, match="no stored value type for a Opaque"):
        _walk(Opaque())
