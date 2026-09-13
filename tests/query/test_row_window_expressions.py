"""Row-independent windows preserve evaluation count, rollback and error phases."""

import pytest

from okto_grafx import connect
from okto_grafx.api import assembly
from okto_grafx.errors import GrafxPlanError
from tools.tck_errors import native_error


@pytest.mark.parametrize("keyword", ["SKIP", "LIMIT"])
@pytest.mark.parametrize("value,reason", [("-1", "negative_window"), ("1.5", "window_argument_type"),
                                        ("true", "window_argument_type"), ("null", "window_argument_type"),
                                        ("n", "non_constant_window"), ("n.v", "non_constant_window")])
def test_window_static_refusals(keyword, value, reason):
    with connect(":memory:") as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute(f"UNWIND [1,2] AS n RETURN n {keyword} {value}")
        assert failure.value.details["reason"] == reason
        assert failure.value.details["query_phase"] == "planning"
        assert native_error(failure.value).phase == "compile time"


@pytest.mark.parametrize("keyword", ["SKIP", "LIMIT"])
@pytest.mark.parametrize("value,reason", [(-1, "negative_window"), (1.5, "window_argument_type"),
                                        (True, "window_argument_type"), (None, "window_argument_type")])
def test_window_parameter_refusals_restore_instruction(tmp_path, keyword, value, reason):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Earlier)")
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute(f"CREATE(:Discarded) RETURN 1 {keyword} $amount", {"amount":value})
            assert failure.value.details["reason"] == reason
            assert failure.value.details["query_phase"] == "execution"
            tx.execute("CREATE(:Later)")
        assert db.execute("MATCH(n) RETURN labels(n) ORDER BY labels(n)").rows == ((("Earlier",),), (("Later",),))
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("stage", ["RETURN n", "WITH n"])
@pytest.mark.parametrize("ordered", [False, True])
def test_random_window_is_evaluated_once_per_invocation(monkeypatch, stage, ordered):
    observed = []
    def draw():
        observed.append(1)
        return 0.5
    monkeypatch.setattr(assembly, "_new_query_random_source", lambda: draw)
    suffix = " RETURN n" if stage.startswith("WITH") else ""
    order = " ORDER BY n" if ordered else ""
    query = f"UNWIND [1,2,3,4,5] AS n {stage}{order} SKIP toInteger(rand()*2) LIMIT toInteger(rand()*4){suffix}"
    with connect(":memory:") as db:
        db.explain(query)
        assert observed == []
        for _ in range(2):
            assert db.execute(query).rows == ((2,), (3,))
        assert len(observed) == 4


def test_parameter_arithmetic_and_closed_local_list_bindings():
    with connect(":memory:") as db:
        assert db.execute("UNWIND [1,2,3,4] AS n RETURN n SKIP $x + 1 LIMIT size([x IN [1,2] | x])", {"x":0}).rows == ((2,), (3,))


def test_final_zero_expression_limit_does_not_suppress_writes(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            assert tx.execute("UNWIND [1,2,3] AS i CREATE(:N {id:i}) RETURN i LIMIT 1-1").rows == ()
        assert db.execute("MATCH(n:N) RETURN count(n)").rows == ((3,),)


def test_random_window_repeats_per_correlated_subquery_not_per_statement(monkeypatch):
    observed = []
    def draw():
        observed.append(1)
        return 0.5
    monkeypatch.setattr(assembly, "_new_query_random_source", lambda: draw)
    with connect(":memory:") as db:
        result = db.execute("UNWIND [1,2] AS x CALL (x) { UNWIND [1,2,3] AS n RETURN n ORDER BY n LIMIT toInteger(rand()*4) } RETURN x,n")
        assert result.rows == ((1,1), (1,2), (2,1), (2,2))
        assert len(observed) == 2
