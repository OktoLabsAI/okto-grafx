"""The Pulse batch source: a bounded UNWIND that stays atomic and index-driven."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxPlanError, GrafxQueryBudgetExceeded
from okto_grafx.domain.query.analysis import analyze
from okto_grafx.domain.query.parser import parse


I67 = (
    "UNWIND $rows AS r "
    "MATCH (n:Decision {id: r.id}) "
    "SET n.relevance_score = r.score, n.last_recomputed_at = r.now"
)
I68 = (
    "UNWIND $rows AS r "
    "MATCH (n:Decision {id: r.id}) "
    "SET n.relevance_score = r.score, "
    "n.pre_cancellation_relevance_score = r.base_score, "
    "n.last_recomputed_at = r.now"
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    """Return a database with the clean primary-key index the Pulse batch probes use."""
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    with handle.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Decision("
            "id STRING, relevance_score DOUBLE, "
            "pre_cancellation_relevance_score DOUBLE, "
            "last_recomputed_at STRING, PRIMARY KEY(id))"
        )
    with handle.begin("write") as seed:
        for identity in ("d1", "d2", "d3"):
            seed.execute(
                "CREATE (:Decision {id: $id, relevance_score: 0.0, "
                "pre_cancellation_relevance_score: 0.0, "
                "last_recomputed_at: 'before'})",
                {"id": identity},
            )
    try:
        yield handle
    finally:
        handle.close()


def _operators(root: object) -> tuple[str, ...]:
    """Return the labels in one public plan, parents before children."""
    return tuple(node.label for node in root.walk())


def _row(
    identity: str,
    score: object,
    *,
    base_score: object = 0.5,
    now: object = "2026-08-26T12:00:00+00:00",
) -> dict[str, object]:
    """Return one Pulse-shaped scoring row."""
    return {
        "id": identity,
        "score": score,
        "base_score": base_score,
        "now": now,
    }


def test_unwind_returns_scalars_nulls_and_detached_maps_in_written_order(
    database: object,
) -> None:
    nested = {"id": "d1", "payload": {"items": [1, 2]}}
    result = database.execute("UNWIND $rows AS r RETURN r", {"rows": [1, None, nested]})

    nested["payload"]["items"].append(3)
    assert result.rows == (
        (1,),
        (None,),
        ({"id": "d1", "payload": {"items": (1, 2)}},),
    )


def test_unwind_map_access_is_case_insensitive_and_empty_is_a_noop(
    database: object,
) -> None:
    assert database.execute(
        "UNWIND $rows AS r RETURN r.id", {"rows": [{"ID": "d1"}]}
    ).rows == (("d1",),)
    assert database.execute("UNWIND $rows AS r RETURN r", {"rows": []}).rows == ()


def test_unwind_source_uses_the_same_eager_type_resolution_as_other_clauses(
    database: object,
) -> None:
    assert database.execute(
        "UNWIND [CASE WHEN true THEN 1 ELSE 2.5 END] AS r RETURN r"
    ).rows == ((1.0,),)

    with pytest.raises(GrafxPlanError) as raised:
        database.execute("UNWIND CASE WHEN true THEN [1] ELSE [2] END AS r RETURN r")
    assert raised.value.details == {
        "field": "case",
        "value": "CASE WHEN true THEN [1] ELSE [2] END",
    }


@pytest.mark.parametrize(
    "carrier",
    [None, {"id": "d1"}, "d1", b"d1", 7],
)
def test_unwind_refuses_every_non_list_carrier(
    database: object, carrier: object
) -> None:
    with pytest.raises(GrafxPlanError) as raised:
        database.execute("UNWIND $rows AS r RETURN r", {"rows": carrier})

    assert raised.value.details == {"field": "unwind", "value": "r"}


def test_unwind_refuses_missing_and_colliding_map_keys(database: object) -> None:
    with pytest.raises(GrafxPlanError) as missing:
        database.execute("UNWIND $rows AS r RETURN r.id", {"rows": [{}]})
    assert missing.value.details == {"field": "property", "value": "id"}

    with pytest.raises(GrafxPlanError) as collision:
        database.execute(
            "UNWIND $rows AS r RETURN r.id",
            {"rows": [{"id": "d1", "nested": {"x": 1, "X": 2}}]},
        )
    assert collision.value.details == {"field": "parameter", "value": "rows"}


def test_unwind_alias_is_a_value_and_never_a_set_or_delete_target() -> None:
    statements = (
        "UNWIND [{x: 1}] AS r MATCH (n:T) SET r.x = 2",
        "UNWIND [1] AS r DELETE r",
    )
    for text in statements:
        with pytest.raises(GrafxPlanError) as raised:
            analyze(parse(text))
        assert raised.value.details == {"field": "variable", "value": "r"}


@pytest.mark.parametrize(
    "tail",
    [
        "CREATE (:Decision {id: 'new'})",
        "MERGE (:Decision {id: 'd1'})",
        "MATCH (n:Decision) DELETE n",
        "MATCH (n:Decision) RETURN n",
        "MATCH (n:Decision) SET n.relevance_score = 1.0 RETURN n",
        "MATCH (n:Decision), (m:Decision) SET n.relevance_score = 1.0",
        "MATCH (n:Decision) SET n.relevance_score = 1.0 SET n.relevance_score = 2.0",
    ],
)
def test_unwind_refuses_every_tail_outside_the_two_frozen_shapes(
    database: object, tail: str
) -> None:
    with pytest.raises(GrafxPlanError) as raised:
        database.explain(f"UNWIND [1] AS r {tail}")
    assert raised.value.details == {"field": "clause", "value": "UNWIND"}


def test_pulse_batches_plan_a_correlated_primary_key_seek(database: object) -> None:
    for statement in (I67, I68):
        operators = _operators(database.explain(statement))
        assert "UnwindRows" in operators
        assert "IndexSeek" in operators
        assert "NodeScan" not in operators


def test_i67_and_i68_apply_each_row_and_ignore_unknown_ids(database: object) -> None:
    with database.begin("write") as transaction:
        first = transaction.execute(
            I67,
            {
                "rows": [
                    _row("d1", 0.7, now="after-67"),
                    _row("unknown", 0.9, now="ignored"),
                ]
            },
        )
        second = transaction.execute(
            I68,
            {"rows": [_row("d2", 0.8, base_score=0.6, now="after-68")]},
        )

    assert first.statistics["rows_updated"] == 1
    assert second.statistics["rows_updated"] == 1
    assert database.execute(
        "MATCH (n:Decision) RETURN n.id, n.relevance_score, "
        "n.pre_cancellation_relevance_score, n.last_recomputed_at ORDER BY n.id"
    ).rows == (
        ("d1", 0.7, 0.0, "after-67"),
        ("d2", 0.8, 0.6, "after-68"),
        ("d3", 0.0, 0.0, "before"),
    )


def test_duplicate_ids_are_applied_in_order_and_the_last_value_wins(
    database: object,
) -> None:
    with database.begin("write") as transaction:
        transaction.execute(
            I67,
            {
                "rows": [
                    _row("d1", 0.2, now="first"),
                    _row("d1", 0.3, now="second"),
                ]
            },
        )

    assert database.execute(
        "MATCH (n:Decision {id: 'd1'}) RETURN n.relevance_score, n.last_recomputed_at"
    ).rows == ((0.3, "second"),)


def test_a_late_invalid_batch_row_releases_no_partial_write(database: object) -> None:
    transaction = database.begin("write")
    transaction.execute("MATCH (n:Decision {id: 'd3'}) SET n.relevance_score = 0.4")
    accepted = tuple(transaction._context.row_intents)

    with pytest.raises(GrafxPlanError):
        transaction.execute(
            I67,
            {
                "rows": [
                    _row("d1", 0.1, now="would-leak"),
                    _row("d2", "not-a-double", now="refuses"),
                ]
            },
        )

    assert tuple(transaction._context.row_intents) == accepted
    assert transaction.commit().wrote is True
    assert database.execute(
        "MATCH (n:Decision) RETURN n.id, n.relevance_score, "
        "n.last_recomputed_at ORDER BY n.id"
    ).rows == (
        ("d1", 0.0, "before"),
        ("d2", 0.0, "before"),
        ("d3", 0.4, "before"),
    )


def test_unwind_intermediate_budget_counts_each_element_once(tmp_path: Path) -> None:
    exact = okto_grafx.connect(tmp_path / "exact", max_intermediate_rows=3)
    refused = okto_grafx.connect(tmp_path / "refused", max_intermediate_rows=2)
    try:
        assert exact.execute(
            "UNWIND $rows AS r RETURN r", {"rows": [1, 2, 3]}
        ).rows == ((1,), (2,), (3,))

        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            refused.execute("UNWIND $rows AS r RETURN r", {"rows": [1, 2, 3]})
        assert raised.value.details == {
            "field": "max_intermediate_rows",
            "limit": 2,
            "observed": 3,
            "operator": "UnwindRows",
        }
    finally:
        exact.close()
        refused.close()


def test_unwind_budget_refusal_releases_no_batch_write(tmp_path: Path) -> None:
    handle = okto_grafx.connect(tmp_path / "write-budget", max_intermediate_rows=1)
    try:
        with handle.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Decision("
                "id STRING, relevance_score DOUBLE, last_recomputed_at STRING, "
                "PRIMARY KEY(id))"
            )
        for identity in ("d1", "d2"):
            with handle.begin("write") as seed:
                seed.execute(
                    "CREATE (:Decision {id: $id, relevance_score: 0.0, "
                    "last_recomputed_at: 'before'})",
                    {"id": identity},
                )

        transaction = handle.begin("write")
        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            transaction.execute(
                I67,
                {
                    "rows": [
                        _row("d1", 0.1, now="would-leak"),
                        _row("d2", 0.2, now="refuses"),
                    ]
                },
            )

        assert raised.value.details == {
            "field": "max_intermediate_rows",
            "limit": 1,
            "observed": 2,
            "operator": "UnwindRows",
        }
        assert tuple(transaction._context.row_intents) == ()
        assert transaction.commit().wrote is False
        for identity in ("d1", "d2"):
            assert handle.execute(
                "MATCH (n:Decision {id: $id}) "
                "RETURN n.relevance_score, n.last_recomputed_at",
                {"id": identity},
            ).rows == ((0.0, "before"),)
    finally:
        handle.close()
