"""Publication of query values: exact built-ins skip the canonical copy, names render once.

Three per-row redundancies were removed from the result path and each is pinned here by a
COUNT, never by a clock: the projected column name is rendered once per plan instead of once per
row; an exact ``int``/``float``/``str`` is published without the canonical copy that only a
subclass needs; and the field label of a published value is built without formatting two
integers per value. The refusals those paths can raise stay byte-identical, and ``bool`` and
every subclass keep taking the canonical path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_grafx.engine.public_views as public_views
from okto_grafx import connect
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.query import ast
from okto_grafx.engine.public_views import _query_result_snapshot, _query_value_snapshot
from okto_grafx.engine.query_engine import QueryResult


def _populate(database, rows: int) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE T(a INT64, b STRING, d DOUBLE, PRIMARY KEY(a))")
    with database.begin("write") as txn:
        txn.executemany(
            "CREATE (n:T {a: $a, b: $b, d: $d})",
            [{"a": i, "b": f"row-{i}", "d": i * 1.5} for i in range(rows)],
        )


def _count_name_renders(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    counter = [0]
    original = ast.ReturnItem.name

    def counting(self: ast.ReturnItem) -> str:
        counter[0] += 1
        return original.fget(self)  # type: ignore[misc]

    monkeypatch.setattr(ast.ReturnItem, "name", property(counting))
    return counter


def _count_canonical_copies(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    counter = {"int": 0, "text": 0}
    original_int = public_views._builtin_int
    original_text = public_views._builtin_text

    def counting_int(value: object, *, field: str = "integer") -> int:
        counter["int"] += 1
        return original_int(value, field=field)

    def counting_text(value: object, *, field: str, empty: bool = True) -> str:
        counter["text"] += 1
        return original_text(value, field=field, empty=empty)

    monkeypatch.setattr(public_views, "_builtin_int", counting_int)
    monkeypatch.setattr(public_views, "_builtin_text", counting_text)
    return counter


def _name_renders_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: int
) -> int:
    database = connect(str(tmp_path / f"db{rows}"))
    try:
        if rows:
            _populate(database, rows)
        else:
            with database.begin("write") as txn:
                txn.execute(
                    "CREATE NODE TABLE T(a INT64, b STRING, d DOUBLE, PRIMARY KEY(a))"
                )
        counter = _count_name_renders(monkeypatch)
        with database.begin("read") as txn:
            result = txn.execute("MATCH (n:T) RETURN n.a, n.b")
            assert len(result.rows) == rows
            assert result.columns == ("n.a", "n.b")
        return counter[0]
    finally:
        database.close()


def test_projected_names_render_once_per_plan_not_once_per_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other doors (plan, columns) also read ``ReturnItem.name``; the row projector adds
    exactly one render per item on the first row and none afterwards -- and none at all when
    the child yields no row. Before this change the projector added one render per row."""
    empty = _name_renders_for(tmp_path, monkeypatch, 0)
    small = _name_renders_for(tmp_path, monkeypatch, 300)
    large = _name_renders_for(tmp_path, monkeypatch, 3000)
    assert small - empty == 2, (empty, small)
    assert large == small, (small, large)


def test_exact_builtins_publish_without_the_canonical_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The number of canonical copies must not depend on the number of published values."""
    counts: dict[int, dict[str, int]] = {}
    for rows in (200, 2000):
        database = connect(str(tmp_path / f"db{rows}"))
        try:
            _populate(database, rows)
            counter = _count_canonical_copies(monkeypatch)
            with database.begin("read") as txn:
                result = txn.execute("MATCH (n:T) RETURN n.a, n.b, n.d")
                assert len(result.rows) == rows
                assert [type(v) for v in result.rows[0]] == [int, str, float]
            counts[rows] = dict(counter)
        finally:
            database.close()
    assert counts[200] == counts[2000], counts


@pytest.mark.parametrize(
    ("value", "expected_type", "canonical_copies"),
    (
        (True, bool, {"int": 0, "text": 0}),
        (7, int, {"int": 0, "text": 0}),
        (2.5, float, {"int": 0, "text": 0}),
        ("plain", str, {"int": 0, "text": 0}),
    ),
    ids=("bool", "exact-int", "exact-float", "exact-str"),
)
def test_exact_values_and_bool_take_the_fast_path(
    monkeypatch: pytest.MonkeyPatch,
    value: object,
    expected_type: type,
    canonical_copies: dict[str, int],
) -> None:
    counter = _count_canonical_copies(monkeypatch)
    published = _query_value_snapshot(value, field="f", depth=0, active=set())
    assert type(published) is expected_type
    assert published == value
    assert counter == canonical_copies


class _IntLike(int):
    pass


class _StrLike(str):
    pass


class _FloatLike(float):
    pass


@pytest.mark.parametrize(
    ("value", "expected_type", "canonical_copies"),
    (
        (_IntLike(7), int, {"int": 1, "text": 0}),
        (_StrLike("plain"), str, {"int": 0, "text": 1}),
        (_FloatLike(2.5), float, {"int": 0, "text": 0}),
    ),
    ids=("int-subclass", "str-subclass", "float-subclass"),
)
def test_subclasses_keep_the_canonical_copy(
    monkeypatch: pytest.MonkeyPatch,
    value: object,
    expected_type: type,
    canonical_copies: dict[str, int],
) -> None:
    counter = _count_canonical_copies(monkeypatch)
    published = _query_value_snapshot(value, field="f", depth=0, active=set())
    assert type(published) is expected_type  # never the subclass
    assert published == value
    assert counter == canonical_copies


def _refusal(callable_, *args, **kwargs) -> tuple[str, dict[str, object]]:
    with pytest.raises(GrafxConfigurationError) as caught:
        callable_(*args, **kwargs)
    return caught.value.message, dict(caught.value.details)


def test_int_range_refusal_is_byte_identical_on_both_paths() -> None:
    too_big = 2**63
    fast = _refusal(_query_value_snapshot, too_big, field="f", depth=0, active=set())
    canonical = _refusal(
        _query_value_snapshot, _IntLike(too_big), field="f", depth=0, active=set()
    )
    assert fast == canonical
    assert fast[0] == "A query integer must fit in 64 signed bits."
    assert fast[1]["field"] == "f" and fast[1]["value"] == too_big


def test_text_length_refusal_is_byte_identical_on_both_paths() -> None:
    long_text = "abcd"
    fast = _refusal(
        _query_value_snapshot,
        long_text,
        field="f",
        depth=0,
        active=set(),
        max_string_characters=3,
    )
    canonical = _refusal(
        _query_value_snapshot,
        _StrLike(long_text),
        field="f",
        depth=0,
        active=set(),
        max_string_characters=3,
    )
    assert fast == canonical
    assert fast[0] == "A query string may carry at most 3 characters."
    assert fast[1]["field"] == "f" and fast[1]["value"] == 4 and fast[1]["limit"] == 3


def test_published_value_field_label_names_row_and_column_exactly() -> None:
    source = QueryResult(
        columns=("x", "y"),
        rows=(("ok", "ok"), ("ok", "ok"), ("ok", "too-long")),
    )
    message, details = _refusal(_query_result_snapshot, source, max_string_characters=3)
    assert message == "A query string may carry at most 3 characters."
    assert details["field"] == "query.result.rows[2][1]"


def test_row_arity_refusal_names_the_row_exactly() -> None:
    source = QueryResult(columns=("x", "y"), rows=(("a", "b"),))
    object.__setattr__(source, "rows", (("a", "b"), ("only-one",)))
    message, details = _refusal(_query_result_snapshot, source)
    assert message == "Every query result row must have exactly one value per column."
    assert details["field"] == "query.result.rows[1]"
    assert details["value"] == 1 and details["expected"] == 2
