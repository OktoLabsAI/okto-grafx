"""Scalar sort dispatch keeps total-order keys and the general subclass door.

The Mapping-port counter fails on the pre-change implementation. The semantic
cases guard against widening exact-type dispatch or changing a rank/NaN key.
Native latency is measured separately; the counter is not a speedup estimate.
"""
from collections.abc import Mapping
from math import copysign

import pytest

from okto_grafx.engine import query_engine as qe


SCALAR_KEYS = (
    (None, (12, 0)),
    (False, (6, 0)),
    (True, (6, 1)),
    (-(1 << 63), (7, (0, -(1 << 63)))),
    (0, (7, (0, 0))),
    ((1 << 63) - 1, (7, (0, (1 << 63) - 1))),
    (-2.5, (7, (0, -2.5))),
    (-0.0, (7, (0, -0.0))),
    (float("-inf"), (7, (0, float("-inf")))),
    (float("inf"), (7, (0, float("inf")))),
    (float("nan"), (7, (1, 0.0))),
    ("", (5, "")),
    ("a", (5, "a")),
    ("\u00e1\U0001f30e", (5, "\u00e1\U0001f30e")),
)


@pytest.mark.parametrize("value,expected", SCALAR_KEYS)
def test_scalar_keys_keep_the_existing_rank_and_payload(value, expected):
    assert qe._sort_key(value) == expected


def test_exact_scalars_do_not_probe_the_abstract_mapping_port(monkeypatch):
    observed = []

    class CountMapping(type):
        def __instancecheck__(cls, value):
            observed.append(type(value))
            return isinstance(value, Mapping)

    class ObservedMapping(metaclass=CountMapping):
        pass

    monkeypatch.setattr(qe, "Mapping", ObservedMapping)
    for value, expected in SCALAR_KEYS:
        assert qe._sort_key(value) == expected
    assert observed == []


def test_numeric_ties_signed_zero_and_nan_keep_stable_order():
    values = [("nan-first", float("nan")), ("zero-negative", -0.0),
              ("integer", 1), ("nan-last", float("nan")),
              ("zero-positive", 0.0), ("floating", 1.0),
              ("negative-infinity", float("-inf")), ("positive-infinity", float("inf"))]
    assert [label for label, value in sorted(values, key=lambda row: qe._sort_key(row[1]))] == [
        "negative-infinity", "zero-negative", "zero-positive", "integer", "floating",
        "positive-infinity", "nan-first", "nan-last",
    ]
    assert copysign(1, qe._sort_key(-0.0)[1][1]) == -1
    assert [label for label, value in sorted(values, key=lambda row: qe._sort_key(row[1]),
                                             reverse=True)] == [
        "nan-first", "nan-last", "positive-infinity", "integer", "floating",
        "zero-negative", "zero-positive", "negative-infinity",
    ]


@pytest.mark.parametrize("scalar", [int, float, str])
def test_scalar_subclasses_that_are_mappings_keep_mapping_precedence(scalar):
    class ScalarMapping(scalar, Mapping):
        def __getitem__(self, key):
            if key != "payload":
                raise KeyError(key)
            return 9

        def __iter__(self):
            return iter(("payload",))

        def __len__(self):
            return 1

    value = ScalarMapping(7 if scalar is not str else "scalar")
    assert qe._sort_key(value) == (0, (("payload", (7, (0, 9))),))


@pytest.mark.parametrize("scalar,value,expected", [
    (int, 7, (7, (0, 7))), (float, 2.5, (7, (0, 2.5))), (str, "s", (5, "s")),
])
def test_ordinary_scalar_subclasses_keep_the_existing_fallback(scalar, value, expected):
    class ScalarSubclass(scalar):
        pass
    assert qe._sort_key(ScalarSubclass(value)) == expected


def test_recursive_collection_keys_keep_kind_ranks():
    # Lists/maps recurse through the same door; their children may use scalar
    # dispatch without moving the containing kind in the total order.
    assert qe._sort_key({"z": [True, "a"], "a": 2}) == (
        0, (("a", (7, (0, 2))), ("z", (3, ((6, 1), (5, "a"))))),
    )
    values = [None, 3.0, False, "a", [1], {"k": 1}]
    assert sorted(values, key=qe._sort_key) == [{"k": 1}, [1], "a", False, 3.0, None]
