"""Scalar admission skips dynamic container checks, never nested map validation."""

from collections.abc import Mapping

import pytest

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.engine.query_engine import _validate_parameter_maps


def test_large_scalar_page_and_nested_collision():
    ids = tuple(f"id-{i}" for i in range(500))
    _validate_parameter_maps({"ids": ids, "values": [None, True, 1, 1.5]})
    with pytest.raises(GrafxPlanError):
        _validate_parameter_maps({"rows": [ids, {"nested": [{"id": 1, "ID": 2}]}]})


def test_scalar_subclass_mapping_keeps_recursive_validation():
    class StringMap(str, Mapping):
        def __iter__(self):
            return iter(("id", "ID"))

        def __len__(self):
            return 2

        def __getitem__(self, key):
            return 1

    with pytest.raises(GrafxPlanError):
        _validate_parameter_maps({"value": StringMap("custom")})


def test_mutation_is_checked_again_on_each_bind():
    row = {"id": 1}
    _validate_parameter_maps({"rows": [row]})
    row["ID"] = 2
    with pytest.raises(GrafxPlanError):
        _validate_parameter_maps({"rows": [row]})
