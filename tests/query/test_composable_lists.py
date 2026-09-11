"""Public execution of list slicing, heterogeneous values and independent UNWIND stages."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError


@pytest.mark.parametrize(("query", "expected"), (
    ("RETURN [1,2,3][-2..], [1,2,3][..2], [1,2][NULL..]", (((2, 3), (1, 2), None),)),
    ("RETURN [1,2][..], [1,2][10..20], [1,2][-99..99], [1,2][2..1]", (((1, 2), (), (1, 2), ()),)),
    ("RETURN NULL[..], [1,2][..NULL]", ((None, None),)),
    ("UNWIND [1,2] AS a UNWIND [3,4] AS b RETURN abs(a), abs(b)", ((1,3), (1,4), (2,3), (2,4))),
    ("UNWIND [{a:1}, {a:2}] AS x UNWIND [{b:3}] AS y RETURN abs(x.a), abs(y.b)", ((1,3), (2,3))),
    ("UNWIND NULL AS x RETURN x", ()),
    ("UNWIND [0,1] AS x RETURN [1,'a'][x]", ((1,), ('a',))),
    ("UNWIND [1,'a'] AS x RETURN CASE WHEN x = 1 THEN x ELSE NULL END", ((1,), (None,))),
    ("WITH [1,2,3][1..] AS xs UNWIND xs AS x RETURN x", ((2,), (3,))),
))
def test_composable_list_semantics(query, expected):
    with connect(":memory:") as db:
        assert db.execute(query).rows == expected


@pytest.mark.parametrize("expression", ("[1,2][$bad..]", "[1,2][..$bad]", "$bad[..]"))
def test_slice_bad_parameters_fail_even_with_no_input_rows(expression):
    with connect(":memory:") as db:
        with pytest.raises(GrafxPlanError):
            db.execute(f"UNWIND [] AS x RETURN {expression}", {"bad": True})


def test_slices_preserve_parameter_and_element_types():
    values = [1, "a", None, {"A": 2}]
    with connect(":memory:") as db:
        result = db.execute("RETURN $values[$start..$end]", {"values": values, "start": -3, "end": 99})
    assert result.rows == ((("a", None, {"A": 2}),),)
    assert values == [1, "a", None, {"A": 2}]
