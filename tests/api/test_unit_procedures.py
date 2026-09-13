"""Unit CALL preserves input cardinality but exposes no tabular output or authority."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxPlanError
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure


def unit_registry(callback, *, required=frozenset(), granted=frozenset()):
    """Register a unit callback using an explicitly empty output schema."""
    return ExtensionRegistry(
        trusted=True,
        procedures=(TabularProcedure("app.observe", ("INT64",), (), callback,
                                     required_permissions=required),),
        procedure_permissions=granted,
    )


def test_unit_standalone_pipeline_empty_input_and_null():
    seen = []
    with connect(":memory:", extensions=unit_registry(seen.append)) as db:
        result = db.execute("CALL app.observe(1)")
        assert result.rows == ()
        assert result.columns == ()
        assert seen == [1]
        assert db.execute("UNWIND [2, 3, null] AS x CALL app.observe(x) RETURN x").rows == (
            (2,), (3,), (None,),
        )
        assert seen == [1, 2, 3, None]
        assert db.execute("UNWIND [] AS x CALL app.observe(4) RETURN x").rows == ()
        assert seen == [1, 2, 3, None]
        assert db.transactions.open_transactions == 0


@pytest.mark.parametrize("query", (
    "CALL app.observe(1) YIELD value RETURN value",
    "CALL app.observe('bad')",
    "CALL app.observe()",
    "UNWIND [] AS x CALL app.observe($bad) RETURN x",
))
def test_unit_refusals_precede_callbacks(query):
    seen = []
    with connect(":memory:", extensions=unit_registry(seen.append)) as db:
        with pytest.raises(GrafxPlanError):
            db.execute(query, {"bad": "bad"} if "$bad" in query else None)
        assert seen == []
        assert db.transactions.open_transactions == 0


def test_unit_permissions_remain_explicit_and_handle_local():
    seen = []
    permission = frozenset({"observe"})
    with connect(":memory:", extensions=unit_registry(seen.append, required=permission)) as db:
        with pytest.raises(GrafxPlanError, match="permissions"):
            db.execute("CALL app.observe(1)")
    assert seen == []
    with connect(":memory:", extensions=unit_registry(
        seen.append, required=permission, granted=permission,
    )) as db:
        assert db.execute("CALL app.observe(2)").rows == ()
    assert seen == [2]


@pytest.mark.parametrize("bad_result", ((), ((),), [], 1, False))
def test_unit_does_not_silently_discard_a_callback_result(bad_result):
    with connect(":memory:", extensions=unit_registry(lambda value: bad_result)) as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute("CALL app.observe(1)")
        assert failure.value.details["field"] == "procedure_result"
        assert db.transactions.open_transactions == 0


@pytest.mark.parametrize("codec", ("pure", "numpy"))
@pytest.mark.parametrize("failure_kind", ("callback", "result"))
def test_unit_late_failure_rolls_back_only_current_statement(tmp_path, codec, failure_kind):
    seen = []

    def observe(value):
        seen.append(value)
        if value == 3:
            if failure_kind == "callback":
                raise RuntimeError("late unit callback failure")
            return 1
        return None

    path = tmp_path / "unit"
    with connect(path, codec=codec, extensions=unit_registry(observe)) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id: 1})")
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id: 2})")
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute("UNWIND [2, 3] AS x CREATE (:T {id: x + 10}) "
                           "WITH x CALL app.observe(x) RETURN x")
            assert failure.value.details["field"] == (
                "procedure_callback" if failure_kind == "callback" else "procedure_result"
            )
            tx.execute("CREATE (:T {id: 4})")
        assert seen == [2, 3]  # Host effects are not transactional and must not be retried.
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (2,), (4,))
        db.verify()
    with connect(path, codec=codec) as reopened:
        assert reopened.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (2,), (4,))
        reopened.verify()


def test_unit_cursor_close_does_not_invoke_remaining_inputs():
    seen = []
    with connect(":memory:", extensions=unit_registry(seen.append)) as db:
        with db.query("UNWIND [1, 2, 3] AS x CALL app.observe(x) RETURN x").cursor(batch_size=1) as cursor:
            assert next(cursor) == (1,)
        assert seen == [1]
        assert db.transactions.open_transactions == 0


def test_unit_may_terminate_a_pipeline_and_compose_in_returning_subqueries():
    seen = []
    with connect(":memory:", extensions=unit_registry(seen.append)) as db:
        result = db.execute("UNWIND [1, 2] AS x CALL app.observe(x)")
        assert result.columns == () and result.rows == ()
        assert seen == [1, 2]
        assert db.execute("UNWIND [3, 4] AS x CALL (x) { CALL app.observe(x) RETURN x AS y } "
                          "RETURN y").rows == ((3,), (4,))
        assert seen == [1, 2, 3, 4]


def test_nonunit_in_query_procedure_requires_explicit_output_projection():
    seen = []
    procedure = TabularProcedure("app.rows", (), (("value", "INT64"),),
                                 lambda: seen.append(True))
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(procedure,))) as db:
        with pytest.raises(GrafxPlanError) as failure:
            db.execute("WITH 1 AS x CALL app.rows() RETURN x")
        assert failure.value.details["field"] == "yield"
        assert seen == []


def test_unit_direct_invocation_validates_and_exposes_no_rows():
    seen = []
    procedure = unit_registry(seen.append).procedures[0]
    assert tuple(procedure.invoke((1,))) == ()
    assert seen == [1]
    with pytest.raises(GrafxPlanError):
        tuple(procedure.invoke((True,)))
    assert seen == [1]


def test_unit_explain_is_callback_free_and_explicit_about_access():
    seen = []
    with connect(":memory:", extensions=unit_registry(seen.append)) as db:
        plan = db.explain("CALL app.observe(1)")
        nodes = [node for node in plan.walk() if node.label == "ProcedureRows"]
        assert len(nodes) == 1
        assert nodes[0].details()["access"] == "pure_unit"
        assert nodes[0].details()["columns"] == ()
        assert seen == []
