"""Numeric IN hashing follows the current float-normalized equality, not Python int equality."""

from types import SimpleNamespace

import pytest

from okto_grafx.engine import query_engine as qe
from tests.query.stack import build_query_stack


@pytest.mark.parametrize("values", [
    (1, 2.0, None, "3"), (0, -0.0), (2**53, 2**53 + 1),
    (float("nan"),), (float("inf"), -float("inf")), (),
    (-(2**63), 2**63 - 1, 2.5),
])
@pytest.mark.parametrize("left", [
    None, True, False, 0, -0.0, 1, 2.0, "3", b"3", 2**53 + 1,
    float("nan"), float("inf"), -(2**63), 2**63 - 1, [1],
])
def test_numeric_hash_matches_canonical_membership(left, values):
    context = SimpleNamespace(in_list_memos={}, in_list_memo_elements=0)
    memo = qe._build_in_list_memo(values, context)
    assert memo is not None
    assert qe._memo_membership(left, memo) is qe._membership(left, values)


def test_repeated_numeric_probes_do_not_repeat_the_linear_comparison(monkeypatch):
    context = SimpleNamespace(in_list_memos={}, in_list_memo_elements=0)
    values = tuple(range(500))
    memo = qe._build_in_list_memo(values, context)
    assert memo is not None

    def forbidden(*args):
        raise AssertionError("numeric probe fell back to O(list length) membership")

    monkeypatch.setattr(qe, "_membership", forbidden)
    for value in range(1000):
        assert qe._memo_membership(value, memo) is (value < 500)


def test_large_integers_and_custom_numeric_values_keep_canonical_admission():
    class CustomInt(int):
        def __float__(self):
            raise AssertionError("custom numeric conversion during cache admission")

    for values in ((2**10000,), (CustomInt(1),), (True, 1)):
        context = SimpleNamespace(in_list_memos={}, in_list_memo_elements=0)
        assert qe._build_in_list_memo(values, context) is None
        assert context.in_list_memo_elements == 0


def test_end_to_end_numeric_memo_preserves_parameter_changes_and_nulls():
    stack = build_query_stack()
    txn = stack.transaction()
    text = "RETURN $needle IN $values AS found"
    for values in ([1, 2.0], [3, None], [3], [True, 1], [2**53]):
        result = stack.engine.execute(text, txn, {"needle": 2, "values": values})
        assert result.rows == ((qe._membership(2, values),),)
