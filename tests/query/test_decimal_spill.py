"""Decimal numeric identities/order/aggregates are identical in memory and spill."""

import pytest

from okto_grafx import DecimalValue
from okto_grafx.adapters.query_spill_local import LocalQuerySpillFactory
from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.engine.query_engine import _spill_pack_internal, _spill_unpack_internal, _spill_encode, _spill_decode
from okto_grafx.domain.query.decimal_numeric import DecimalOrderKey
from tests.query.stack import build_query_stack


@pytest.mark.parametrize("query", [
    "UNWIND $xs AS x RETURN DISTINCT x",
    "UNWIND $xs AS x RETURN x, count(*) AS n",
    "UNWIND $xs AS x RETURN count(DISTINCT x)",
    "UNWIND $xs AS x RETURN x ORDER BY x",
    "UNWIND $xs AS x RETURN x ORDER BY x DESC LIMIT 37",
    "UNWIND $xs AS x RETURN min(x),max(x)",
])
def test_numeric_grouping_and_sorting_use_real_spill_with_exact_keys(query, monkeypatch):
    values = [1, 1.0, DecimalValue(100, 3, 2), DecimalValue(1, 1, 0),
              DecimalValue(1, 1, 1), 0.1, DecimalValue(125, 3, 2), 1.25,
              DecimalValue(100000000000000000001, 21, 0), 1e20,
              DecimalValue(10**38 - 1, 38, 0),
              {"v": DecimalValue(100, 3, 2)}, {"v": 1.0},
              [DecimalValue(1, 1, 0), None], [1, None], True, False, None] * 12
    values += [f"unique-{index:03d}" for index in range(160)]
    ordinary = build_query_stack(query_memory_budget_bytes=None)
    expected = ordinary.engine.execute(query, ordinary.transaction(), {"xs": values}).rows
    opened = []
    original = LocalQuerySpillFactory.open
    def record(factory, budget):
        opened.append(True)
        return original(factory, budget)
    monkeypatch.setattr(LocalQuerySpillFactory, "open", record)
    spilled = build_query_stack(query_memory_budget_bytes=4096)
    actual = spilled.engine.execute(query, spilled.transaction(), {"xs": values}).rows
    assert opened, "The bounded engine did not exercise the temporary spill workspace"
    assert actual == expected


@pytest.mark.parametrize("distinct", [False, True])
def test_exact_decimal_aggregate_and_ordered_minmax_spill(distinct, monkeypatch):
    values = [DecimalValue(index, 6, 2) for index in range(-130, 131)] * 2
    values += [None, 0, 1, -1]
    clause = "DISTINCT " if distinct else ""
    query = f"UNWIND $xs AS x RETURN sum({clause}x),avg({clause}x),min(x),max(x),count({clause}x)"
    ordinary = build_query_stack(query_memory_budget_bytes=None)
    expected = ordinary.engine.execute(query, ordinary.transaction(), {"xs": values}).rows
    opened = []
    original = LocalQuerySpillFactory.open
    def record(factory, budget):
        opened.append(True)
        return original(factory, budget)
    monkeypatch.setattr(LocalQuerySpillFactory, "open", record)
    spilled = build_query_stack(query_memory_budget_bytes=4096)
    assert spilled.engine.execute(query, spilled.transaction(), {"xs": values}).rows == expected
    assert opened


def test_internal_decimal_order_marker_roundtrip_and_refusal():
    key = (7, (0, DecimalOrderKey(DecimalValue(125, 3, 2))))
    wire = _spill_encode(_spill_pack_internal(key), key=True)
    restored = _spill_unpack_internal(_spill_decode(wire, key=True))
    assert restored == key
    assert restored < (7, (0, 1.3))
    assert restored > (7, (0, 1))
    with pytest.raises(GrafxCorruptionDetected):
        _spill_unpack_internal((b"OGQD\x01", 1.25))
