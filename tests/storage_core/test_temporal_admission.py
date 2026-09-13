"""The pending-row discovery contract is independent of enabled value codecs."""

import pytest

from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.temporal_values import (
    DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue, TemporalInstant,
)
from okto_grafx.domain.model.value import MAX_VALUE_DEPTH
from okto_grafx.domain.temporal_admission import temporal_storage_required

VALUES = (DateValue(2000), LocalTimeValue(0), TimeValue(LocalTimeValue(0), 0),
          LocalDateTimeValue(DateValue(2000), LocalTimeValue(0)),
          DateTimeValue.from_epoch_parts(0, zone="Future/Zone"), DurationValue(1, 2, 3, 4))


@pytest.mark.parametrize("value", VALUES)
def test_all_six_families_discovered_as_direct_nested_and_map_keys(value):
    assert temporal_storage_required((value,))
    assert temporal_storage_required((0, {"x": [None, {value: [value]}]}))


def test_empty_and_ordinary_values_do_not_require_capability():
    assert not temporal_storage_required(())
    assert not temporal_storage_required((None, True, 1, 1.25, "s", b"b", [1, {"x": 3}]))


@pytest.mark.parametrize("bad", [object(), TemporalInstant(0)])
def test_discovery_does_not_short_circuit_after_first_temporal(bad):
    with pytest.raises(SchemaMismatchError):
        temporal_storage_required((DateValue(2000), [bad]))


def test_depth_and_cycles_refuse_even_after_positive_match():
    for seed, expected in ((DateValue(2000), True), (None, False)):
        value = seed
        for _ in range(MAX_VALUE_DEPTH):
            value = [value]
        assert temporal_storage_required((value,)) is expected
        with pytest.raises(SchemaMismatchError):
            temporal_storage_required((DateValue(2000), [value]))
    cycle = []
    cycle.append(cycle)
    with pytest.raises(SchemaMismatchError):
        temporal_storage_required((cycle,))


def test_bad_native_components_and_host_callbacks_cannot_masquerade_as_temporal():
    class Host:
        def __index__(self):
            raise AssertionError("No host conversion")
    value = LocalTimeValue(0)
    object.__setattr__(value, "nanoseconds", Host())
    with pytest.raises(SchemaMismatchError):
        temporal_storage_required((value,))
    class Row(tuple):
        def __iter__(self):
            raise AssertionError("No row iterator callback")
    with pytest.raises(SchemaMismatchError):
        temporal_storage_required(Row((DateValue(2000),)))


def test_native_collection_subclass_iteration_is_not_invoked():
    class Items(dict):
        def items(self):
            raise AssertionError("No overridden items")
    class ItemsList(list):
        def __iter__(self):
            raise AssertionError("No overridden iterator")
    assert temporal_storage_required((Items(x=ItemsList([DateValue(2000)])),))
