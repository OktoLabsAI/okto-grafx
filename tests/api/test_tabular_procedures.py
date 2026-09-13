"""Typed CALL/YIELD composition, explicit host permissions, cleanup and atomic failure."""

from __future__ import annotations

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxConfigurationError, GrafxPlanError, GrafxQueryBudgetExceeded
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure


def registry(callback=None, *, granted=frozenset(), required=frozenset(), **budgets):
    """Build one explicitly trusted per-handle tabular registration."""
    callback = callback or (lambda text: ((word, len(word)) for word in text.split()))
    procedure = TabularProcedure("app.words", ("STRING",), (("word", "STRING"), ("length", "INT64")),
                                 callback, required_permissions=required, **budgets)
    return ExtensionRegistry(trusted=True, procedures=(procedure,), procedure_permissions=granted)


def test_standalone_correlated_filtered_nested_and_union_calls():
    with connect(":memory:", extensions=registry()) as db:
        assert db.execute("CALL app.words('a bb') YIELD word, length").rows == (("a", 1), ("bb", 2))
        assert db.execute("CALL app.words('a bb') YIELD word AS w, length AS n WHERE n > 1 "
                          "RETURN upper(w), n").rows == (("BB", 2),)
        assert db.execute("UNWIND ['a b', 'c'] AS text CALL app.words(text) YIELD word "
                          "RETURN word").rows == (("a",), ("b",), ("c",))
        assert db.execute("CALL { CALL app.words('a') YIELD word RETURN word } RETURN word").rows == (("a",),)
        assert db.execute("CALL app.words('a') YIELD word RETURN word UNION "
                          "CALL app.words('a b') YIELD word RETURN word").rows == (("a",), ("b",))


@pytest.mark.parametrize("query", (
    "CALL app.missing('a') YIELD word RETURN word",
    "CALL app.words(1) YIELD word RETURN word",
    "CALL app.words() YIELD word RETURN word",
    "CALL app.words('a') YIELD missing RETURN missing",
    "WITH 1 AS word CALL app.words('a') YIELD word RETURN word",
    "CALL app.words('a') YIELD word, word RETURN word",
))
def test_signature_refusal_precedes_callbacks(query):
    calls = []
    with connect(":memory:", extensions=registry(lambda value: calls.append(value))) as db:
        with pytest.raises(GrafxPlanError):
            db.execute(query)
    assert calls == []


def test_permission_grants_and_handle_isolation():
    required = frozenset({"tokenize"})
    with connect(":memory:", extensions=registry(required=required)) as denied:
        with pytest.raises(GrafxPlanError, match="permissions"):
            denied.execute("CALL app.words('a') YIELD word")
    with connect(":memory:", extensions=registry(required=required, granted=required)) as granted:
        assert granted.execute("CALL app.words('a') YIELD word").rows == (("a",),)
    with connect(":memory:") as absent:
        with pytest.raises(GrafxPlanError):
            absent.execute("CALL app.words('a') YIELD word")


@pytest.mark.parametrize("prefix, argument", (("UNWIND [] AS x", "$bad"),
                                             ("WITH $bad AS value UNWIND [] AS x", "value")))
def test_bound_procedure_argument_types_are_checked_without_input_rows(prefix, argument):
    calls = []
    with connect(":memory:", extensions=registry(lambda text: calls.append(text))) as db:
        query = f"{prefix} CALL app.words({argument}) YIELD word RETURN word"
        with pytest.raises(GrafxPlanError, match="type mismatch"):
            db.execute(query, {"bad": 3})
        with pytest.raises(GrafxPlanError, match="type mismatch"):
            with db.query(query, {"bad": 3}).cursor(batch_size=1) as cursor:
                next(cursor)
    assert calls == []


def test_early_cursor_close_closes_callback_and_releases_snapshot():
    closed = []
    def words(text):
        try:
            for word in text.split():
                yield word, len(word)
        finally:
            closed.append(True)
    with connect(":memory:", extensions=registry(words)) as db:
        with db.query("CALL app.words('a b c') YIELD word RETURN word").cursor(batch_size=1) as cursor:
            assert next(cursor) == ("a",)
        assert closed == [True]
        assert db.transactions.open_transactions == 0


@pytest.mark.parametrize("options", ({"max_rows": 1}, {"max_result_bytes": 1}, {"max_value_bytes": 4}))
def test_procedure_budgets(options):
    with connect(":memory:", extensions=registry(**options)) as db:
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.execute("CALL app.words('alpha beta') YIELD word RETURN word")


@pytest.mark.parametrize("callback", (lambda text: ((1, 2),), lambda text: (("a",),), lambda text: 1 / 0))
def test_callback_failures_cannot_publish_prior_statement_writes(tmp_path, callback):
    with connect(tmp_path / "db", extensions=registry(callback)) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE T(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            with pytest.raises(GrafxPlanError):
                tx.execute("CREATE (:T {id: 1}) WITH 1 AS keep "
                           "CALL app.words('a') YIELD word RETURN word")
        assert db.execute("MATCH (n:T) RETURN count(*)").rows == ((0,),)


def test_registration_rejects_mutable_or_ambiguous_schemas():
    with pytest.raises(GrafxConfigurationError):
        TabularProcedure("app.bad", (), (("n", "INT64"), ("n", "STRING")), lambda: ())
    with pytest.raises(GrafxConfigurationError):
        registry(required={"permission"})
    with pytest.raises(GrafxConfigurationError):
        registry(max_rows=0)


@pytest.mark.parametrize("bad_row", (False, True))
def test_callback_cleanup_failure_is_typed_and_does_not_mask_row_failure(bad_row):
    class Rows:
        def __init__(self):
            self.done = False
        def __iter__(self):
            return self
        def __next__(self):
            if self.done:
                raise StopIteration
            self.done = True
            return (1, 2) if bad_row else ("a", 1)
        def close(self):
            raise RuntimeError("close failed")
    with connect(":memory:", extensions=registry(lambda text: Rows())) as db:
        with pytest.raises(GrafxPlanError) as error:
            db.execute("CALL app.words('a') YIELD word")
        assert error.value.details["field"] == ("udf_type" if bad_row else "procedure_cleanup")
        if bad_row:
            assert any("cleanup also failed" in note for note in error.value.__notes__)
        assert db.transactions.open_transactions == 0


def test_public_result_failure_discards_only_current_statement(tmp_path, monkeypatch):
    import okto_grafx.engine.database as database
    with connect(tmp_path / "public-result") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE T(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id: 1})")
            with monkeypatch.context() as patch:
                def refuse(*args, **kwargs):
                    raise GrafxPlanError("forced public result refusal")
                patch.setattr(database, "_query_result_view", refuse)
                with pytest.raises(GrafxPlanError, match="forced public result"):
                    tx.execute("CREATE (n:T {id: 2}) RETURN n.id")
            tx.execute("CREATE (:T {id: 3})")
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))
