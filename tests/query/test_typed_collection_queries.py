"""Typed columns compose with generic query values without leaking staged ownership."""

import pytest

from okto_grafx import connect, DecimalValue
from tests.api.test_typed_collection_storage import seed, INPUT, CREATE, EXPECTED, READ


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_collection_parameters_results_and_cached_plans_are_owned(tmp_path, codec):
    with connect(tmp_path / "db", codec=codec) as db:
        seed(db, data=False)
        values = {**INPUT, "xs": [1, 2], "amounts": {"price": DecimalValue(125, 3, 2)}}
        with db.begin() as tx:
            first = tx.execute(CREATE, {"id": 1, **values})
            values["xs"].append(99)
            values["amounts"]["price"] = DecimalValue(999, 3, 0)
            first.rows[0][1]["price"] = DecimalValue(1, 1, 0)
            assert tx.execute(READ).rows == (EXPECTED,)
            tx.execute("MATCH(n:N {id:1}) SET n.xs=n.xs+[3] RETURN n.xs")
            assert tx.execute("MATCH(n:N {id:1}) RETURN n.xs").rows == (((1, 2, 3),),)
        for offered, expected in (([1, 2, 3], 1), ([1, 2], 0), ([True, 2, 3], 0)):
            assert db.execute("MATCH(n:N) WHERE n.xs=$xs RETURN count(n)", {"xs": offered}).rows == ((expected,),)
        assert not db.verify("all").findings


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_collection_projection_union_unwind_and_scalar_indexes(tmp_path, codec):
    with connect(tmp_path / "db", codec=codec) as db:
        seed(db)
        db.create_index("by_id", "N", ("id",), bucket_count=4)
        assert db.execute("MATCH(n:N {id:1}) RETURN n.xs AS v UNION MATCH(n:N {id:1}) RETURN n.xs AS v").rows == (((1, 2),),)
        assert db.execute("MATCH(n:N {id:1}) UNWIND n.xs AS x RETURN sum(x)").rows == ((3,),)
        assert db.execute("MATCH(n:N {id:1}) WITH n.info AS info RETURN info.meta.nested").rows == (((DecimalValue(1, 1, 0), True),),)
        assert db.execute("MATCH(n:N) WHERE n.id=1 RETURN n.amounts.price").rows == ((DecimalValue(12500, 12, 4),),)
        assert not db.verify("all").findings
