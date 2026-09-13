"""Independent exact JSON oracles and hostile/malformed collection transport inputs."""

from dataclasses import replace
import json
from types import MappingProxyType
from typing import get_type_hints

import pytest

from okto_grafx import (StoredType, DecimalValue, DateValue, LocalTimeValue, TimeValue,
                       LocalDateTimeValue, DateTimeValue, DurationValue)
from okto_grafx.domain.model.value import Timestamp, Uuid
from okto_grafx.collection_json import collection_json_value, collection_from_json_value
from okto_grafx.errors import GrafxError, GrafxConfigurationError, GrafxQueryBudgetExceeded

SCALARS = (
    (StoredType("BOOL"), True, True),
    (StoredType("INT64"), -(2**63), "-9223372036854775808"),
    (StoredType("INT64"), 2**63 - 1, "9223372036854775807"),
    (StoredType("DOUBLE"), -0.0, -0.0),
    (StoredType("STRING"), "árvore\n🌳", "árvore\n🌳"),
    (StoredType("BYTES"), b"\xff\x00", "/wA="),
    (StoredType("TIMESTAMP"), Timestamp(2**63 - 1), "9223372036854775807"),
    (StoredType("UUID"), Uuid(bytes(16)), "00000000-0000-0000-0000-000000000000"),
    (StoredType("DECIMAL", precision=12, scale=4), DecimalValue(12500, 12, 4),
     {"type": "decimal", "coefficient": "12500", "precision": 12, "scale": 4}),
    (StoredType("DATE"), DateValue.from_epoch_day(20000), {"type": "date", "epoch_day": "20000"}),
    (StoredType("LOCALTIME"), LocalTimeValue(123), {"type": "localtime", "nanoseconds": "123"}),
    (StoredType("TIME"), TimeValue(LocalTimeValue(123), 3600), {"type": "time", "nanoseconds": "123", "offset_seconds": 3600}),
    (StoredType("LOCALDATETIME"), LocalDateTimeValue(DateValue.from_epoch_day(20000), LocalTimeValue(123)),
     {"type": "localdatetime", "epoch_day": "20000", "nanoseconds": "123"}),
    (StoredType("DATETIME"), DateTimeValue.from_epoch_parts(100, 123, offset_seconds=3600, zone=None),
     {"type": "datetime", "epoch_seconds": "100", "nanosecond": 123, "offset_seconds": 3600, "zone": None}),
    (StoredType("DURATION"), DurationValue(1, 2, 3, 4),
     {"type": "duration", "months": "1", "days": "2", "seconds": "3", "nanoseconds": 4}),
)


@pytest.mark.parametrize("leaf,value,wire", SCALARS)
@pytest.mark.parametrize("container", ("LIST", "MAP", "ARRAY", "STRUCT"))
def test_all_native_leaves_in_each_collection_have_independent_json_oracles(leaf, value, wire, container):
    if container in ("LIST", "ARRAY"):
        descriptor = StoredType(container, element=leaf, length=2 if container == "ARRAY" else None)
        native, encoded = (value, None), [wire, None]
    else:
        descriptor = StoredType(container, element=leaf if container == "MAP" else None,
                                fields=(("x", leaf), ("empty", leaf)) if container == "STRUCT" else ())
        native, encoded = {"x": value, "empty": None}, {"x": wire, "empty": None}
    assert collection_json_value(descriptor, native) == encoded
    assert collection_from_json_value(descriptor, json.loads(json.dumps(encoded))) == native
    assert collection_json_value(descriptor, collection_from_json_value(descriptor, encoded)) == encoded


def test_dynamic_values_do_not_confuse_maps_with_scalar_tags_and_preserve_key_families():
    descriptor = StoredType("MAP", element=StoredType("ANY"))
    native = {"literal": {"type": "decimal", "coefficient": "bad"}, "value": DecimalValue(125, 3, 2),
              "keys": {(1, 2): b"abc", None: "null key", 7: True}, "mixed": (1, 1.0, "1", False)}
    wire = collection_json_value(descriptor, native)
    assert wire["literal"]["type"] == "map"
    assert wire["value"] == {"type": "decimal", "coefficient": "125", "precision": 3, "scale": 2}
    assert wire["mixed"] == {"type": "list", "value": [
        {"type": "int64", "value": "1"}, {"type": "double", "value": 1.0},
        {"type": "string", "value": "1"}, {"type": "bool", "value": False}]}
    assert collection_from_json_value(descriptor, wire) == native
    # Neither direction retains caller-owned dictionaries/lists.
    restored = collection_from_json_value(descriptor, wire)
    restored["literal"]["type"] = "changed"
    assert wire["literal"]["entries"][0][1]["value"] == "decimal"
    native["literal"]["type"] = "changed"
    assert collection_from_json_value(descriptor, wire)["literal"]["type"] == "decimal"


@pytest.mark.parametrize("wire", [1, "1", {"type": "int64", "value": 1}, {"type": "int64", "value": "01"},
    {"type": "int64", "value": "-0"}, {"type": "int64", "value": "9223372036854775808"},
    {"type": "INT64", "value": "1"}, {"type": "double", "value": float("nan")},
    {"type": "map", "value": {}}, {"type": "decimal", "coefficient": "1", "precision": 1},
    {"type": "list", "value": [None], "extra": 1}, {"type": "bytes", "value": "YR=="},
    {"type": "map", "entries": [[{"type": "string", "value": "x"}, None], [{"type": "string", "value": "x"}, None]]},
    {"type": "map", "entries": [[{"type": "map", "entries": []}, None]]}])
def test_dynamic_invalid_and_ambiguous_cells_refuse(wire):
    with pytest.raises(GrafxError):
        collection_from_json_value(StoredType("LIST", element=StoredType("ANY")), [wire])


@pytest.mark.parametrize("value", [[1], [True], [None], ["+1"], ["-0"], ["01"], ["١"], ["1_0"]])
def test_typed_nonnull_integer_cells_require_canonical_text(value):
    with pytest.raises(GrafxError):
        collection_from_json_value(StoredType("LIST", element=StoredType("INT64", nullable=False)), value)


def test_exact_descriptor_and_native_schema_validation_are_not_assignment_normalization():
    descriptor = StoredType("STRUCT", nullable=False, fields=(("v", StoredType("DECIMAL", precision=12, scale=4)),))
    with pytest.raises(GrafxError):
        collection_json_value(descriptor, {"v": DecimalValue(125, 3, 2)})
    for value in ({}, {"v": None, "extra": None}, None,
                  {"v": {"type": "decimal", "coefficient": "125", "precision": 3, "scale": 2}}):
        with pytest.raises(GrafxError):
            collection_from_json_value(descriptor, value)
    assert collection_from_json_value(descriptor, {"v": None}) == {"v": None}
    assert collection_json_value(descriptor, MappingProxyType({"v": None})) == {"v": None}


def test_empty_null_depth_cycle_and_byte_bounds_are_explicit():
    for descriptor, native, wire in ((StoredType("STRUCT"), {}, {}),
                                     (StoredType("ARRAY", element=StoredType("INT64"), length=0), (), [])):
        assert collection_json_value(descriptor, native) == wire
        assert collection_from_json_value(descriptor, wire) == native
        assert collection_from_json_value(descriptor, None) is None
    descriptor = StoredType("LIST", element=StoredType("ANY"))
    cyclic = []
    cyclic.append(cyclic)
    for value in (cyclic, [[float("inf")]], [[object()]]):
        with pytest.raises(GrafxError):
            collection_json_value(descriptor, value)
    with pytest.raises(GrafxError):
        collection_from_json_value(descriptor, cyclic)
    for value in (["🌳" * 10], ["\x00" * 10]):
        with pytest.raises(GrafxQueryBudgetExceeded):
            collection_json_value(StoredType("LIST", element=StoredType("STRING")), value, max_bytes=20)
    wire = ["a", "b"]
    size = len(json.dumps(wire, separators=(",", ":")).encode())
    strings = StoredType("LIST", element=StoredType("STRING"))
    assert collection_json_value(strings, tuple(wire), max_bytes=size) == wire
    with pytest.raises(GrafxQueryBudgetExceeded):
        collection_from_json_value(strings, wire, max_bytes=size-1)
    for bound in (0, True, -1, 2**31+1):
        with pytest.raises(GrafxConfigurationError):
            collection_from_json_value(strings, wire, max_bytes=bound)


def test_descriptors_are_validated_without_host_coercion_and_annotations_resolve():
    descriptor = StoredType("LIST", element=StoredType("INT64"))
    with pytest.raises(GrafxConfigurationError):
        collection_json_value(StoredType("INT64"), 1)
    forged = replace(descriptor)
    object.__setattr__(forged, "element", forged)
    with pytest.raises(GrafxConfigurationError):
        collection_json_value(forged, [])
    assert get_type_hints(collection_from_json_value)["return"]
    class HostInt(int):
        pass
    with pytest.raises(GrafxError):
        collection_json_value(descriptor, [HostInt(1)])


def test_cumulative_binary_budget_refuses_before_next_base64_allocation(monkeypatch):
    import okto_grafx.collection_json as module
    calls = []
    original = module.base64.b64encode

    def observed(value):
        calls.append(len(value))
        return original(value)

    monkeypatch.setattr(module.base64, "b64encode", observed)
    with pytest.raises(GrafxQueryBudgetExceeded):
        collection_json_value(StoredType("LIST", element=StoredType("BYTES")), [b"x" * 6] * 100, max_bytes=16)
    assert calls == [6, 6]
