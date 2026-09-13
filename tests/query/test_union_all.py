"""Two-branch bag semantics share type checks and budgets with UNION."""

import pytest
from okto_grafx import connect
from okto_grafx.errors import GrafxError
from okto_grafx.domain.query.parser import parse


def test_duplicates_nulls_exact_types_and_parameters():
    with connect(":memory:") as db:
        values = db.execute("RETURN 1 AS x UNION ALL RETURN 1.0 AS x").rows
        assert values == (
            (1,),
            (1.0,),
        )
        assert tuple(type(row[0]) for row in values) == (int, float)
        assert db.execute("RETURN null AS x UNION ALL RETURN null AS x").rows == (
            (None,),
            (None,),
        )
        result = db.execute(
            "UNWIND $v AS n RETURN n AS a UNION ALL RETURN $x AS a",
            {"v": [1, 1], "x": 2},
        )
        assert result.rows == ((1,), (1,), (2,))
        assert result.columns == ("a",)
        assert parse("RETURN 1 UNION ALL RETURN 1").all is True


@pytest.mark.parametrize(
    "text",
    [
        "RETURN 1 UNION ALL RETURN 'x'",
        "RETURN 1 UNION ALL RETURN 1,2",
        "RETURN 1 UNION ALL RETURN 2 UNION RETURN 3",
        "RETURN 1 UNION ALL CREATE (:N {id:1})",
        "RETURN 1 UNION ALL",
    ],
)
def test_refused_shapes(text):
    with connect(":memory:") as db, pytest.raises(GrafxError):
        db.execute(text)


def test_branch_windows_and_snapshot(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            for i in range(3):
                tx.execute("CREATE (:N {id:$id})", {"id": i})
        query = "MATCH (n:N) RETURN n.id AS x ORDER BY x LIMIT 1 UNION ALL MATCH (m:N) RETURN m.id AS x ORDER BY x DESC LIMIT 1"
        with db.begin("read") as old:
            with connect(tmp_path / "db") as other, other.begin() as tx:
                tx.execute("CREATE (:N {id:3})")
            assert old.execute(query).rows == ((0,), (2,))
        assert db.execute(query).rows == ((0,), (3,))


@pytest.mark.parametrize(
    "options", [{"max_result_rows": 1}, {"max_intermediate_rows": 1}]
)
def test_shared_budgets(options):
    from okto_grafx.errors import GrafxQueryBudgetExceeded

    with connect(":memory:", **options) as db, pytest.raises(GrafxQueryBudgetExceeded):
        db.execute("RETURN 1 AS x UNION ALL RETURN 1 AS x")
