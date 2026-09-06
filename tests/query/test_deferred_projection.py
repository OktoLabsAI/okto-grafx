"""KGRUN-3: a bounded ORDER BY / LIMIT projects the discarded rows only where it must."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
import okto_grafx.engine.query_engine as query_engine_module
from okto_grafx.domain.errors import GrafxError
from okto_grafx.engine.query_engine import RowBinding, _top_rows_from

PAGE = (
    "MATCH (n) "
    "RETURN n.id, label(n) AS node_type, n.title, n.body, n.created_at, n.score, "
    "coalesce(n.layer, 'legacy_unknown') AS layer, n.maturity, n.kind_of "
    "ORDER BY n.created_at DESC, n.id DESC LIMIT $max_rows"
)
STATEMENTS: tuple[tuple[str, dict[str, object]], ...] = (
    (PAGE, {"max_rows": 5}),
    (PAGE, {"max_rows": 0}),
    (PAGE, {"max_rows": 1000}),
    (
        "MATCH (n) RETURN n.id AS ident, n.created_at AS t, n.title "
        "ORDER BY t DESC, ident ASC SKIP 3 LIMIT 4",
        {},
    ),
    (
        "MATCH (n) RETURN n.id, n.created_at AS t ORDER BY t + 1 ASC, n.id LIMIT 4",
        {},
    ),
    ("MATCH (n:Doc) RETURN n.id, n.score, $tag AS tag ORDER BY n.score DESC LIMIT 3", {"tag": 7}),
    ("MATCH (n) RETURN DISTINCT n.layer ORDER BY n.layer LIMIT 2", {}),
    ("MATCH (n) RETURN n.id, n.title ORDER BY n.created_at", {}),
    ("MATCH (n:Doc) RETURN n.id, n.title ORDER BY n.created_at DESC LIMIT 3", {}),
    (
        "MATCH (n:Doc) RETURN label(n) AS kind, count(n) AS total "
        "ORDER BY total DESC LIMIT 1",
        {},
    ),
    ("MATCH (n:Doc) RETURN n.id, 10.0 / n.score AS inverse ORDER BY n.created_at DESC LIMIT 3", {}),
    ("MATCH (n:Doc) RETURN n.id, n.nope ORDER BY n.created_at LIMIT 2", {}),
    ("MATCH (n) RETURN n.id, n.nope ORDER BY n.created_at LIMIT 2", {}),
    (
        "UNWIND $maps AS m RETURN m.k AS k, m.o AS o ORDER BY m.o ASC LIMIT 1",
        {"maps": [{"k": 1, "o": 1}, {"o": 2}]},
    ),
    (
        "UNWIND $maps AS m RETURN m.k AS k, m.o AS o ORDER BY m.o ASC LIMIT 5",
        {"maps": [{"k": 1, "o": 1}, {"k": 2, "o": 2}]},
    ),
    (
        "MATCH (n:Doc) RETURN n.id, coalesce(n.score, 1) AS coerced "
        "ORDER BY n.created_at DESC LIMIT 2",
        {},
    ),
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    handle = okto_grafx.connect(tmp_path / "db", page_size=4096)
    with handle.begin("write") as transaction:
        transaction.execute(
            "CREATE NODE TABLE Doc(id STRING, title STRING, body STRING, created_at INT64, "
            "score DOUBLE, layer STRING, maturity STRING, kind_of STRING, PRIMARY KEY(id))"
        )
        transaction.execute(
            "CREATE NODE TABLE Note(id STRING, title STRING, created_at INT64, "
            "PRIMARY KEY(id))"
        )
    with handle.begin("write") as transaction:
        for index in range(24):
            transaction.execute(
                "CREATE (:Doc {id: $id, title: $title, body: $body, created_at: $at, "
                "score: $score, layer: $layer, maturity: $maturity, kind_of: $kind})",
                {
                    "id": f"d-{index:02d}",
                    "title": None if index % 7 == 3 else f"doc {index}",
                    "body": "x" * (index % 5),
                    "at": index // 3,  # ties on the first sort key
                    "score": 0.0 if index == 0 else float(index) / 4,
                    "layer": None if index % 4 == 1 else "canonical",
                    "maturity": "final" if index % 2 else None,
                    "kind": "code_evidence" if index % 9 == 0 else None,
                },
            )
        for index in range(12):
            transaction.execute(
                "CREATE (:Note {id: $id, title: $title, created_at: $at})",
                {"id": f"n-{index:02d}", "title": f"note {index}", "at": 2 + index // 2},
            )
    try:
        yield handle
    finally:
        handle.close()


def _outcome(database: object, text: str, parameters: dict[str, object]) -> tuple:
    try:
        result = database.execute(text, parameters)  # type: ignore[attr-defined]
    except GrafxError as failure:
        return ("erro", json.dumps(failure.to_dict(), sort_keys=True, default=repr))
    except Exception as failure:  # noqa: BLE001 - the shape of a Python error is the fixture
        return ("erro_py", type(failure).__name__, str(failure))
    return ("linhas", tuple(result.columns), tuple(tuple(row) for row in result.rows))


def _canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every bounded sort through the canonical projection-then-retain path."""

    def canonical(engine, node, project, retained_limit, context):  # type: ignore[no-untyped-def]
        limit = query_engine_module._window(retained_limit, context, "LIMIT")
        skipped = (
            query_engine_module._window(node.retained_skip, context, "SKIP")
            if node.retained_skip is not None
            else 0
        )
        return _top_rows_from(
            node, engine._rows(project, context), skipped + limit, context
        )

    monkeypatch.setattr(query_engine_module, "_top_projected_rows", canonical)


def test_every_shape_answers_exactly_as_the_canonical_projection(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows, columns, errors and Python errors: the same, statement by statement."""
    deferred = [_outcome(database, text, parameters) for text, parameters in STATEMENTS]
    with monkeypatch.context() as scoped:
        _canonical(scoped)
        canonical = [_outcome(database, text, parameters) for text, parameters in STATEMENTS]
    for (text, parameters), left, right in zip(STATEMENTS, deferred, canonical):
        assert left == right, (text, parameters)
    # The corpus is not trivially green: it carries real rows and real refusals.
    kinds = {outcome[0] for outcome in deferred}
    assert kinds == {"linhas", "erro"}
    assert any(outcome[0] == "linhas" and outcome[2] for outcome in deferred)


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    reads: dict[str, int] = {}
    original = RowBinding.value

    def counted(self: RowBinding, key: str) -> object:
        reads[key] = reads.get(key, 0) + 1
        return original(self, key)

    monkeypatch.setattr(RowBinding, "value", counted)
    return reads


def test_deferred_items_are_read_only_on_the_retained_rows(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """36 rows pass the scan; 5 are retained; the deferred properties are read 5 times."""
    reads = _count_reads(monkeypatch)
    rows = database.execute(PAGE, {"max_rows": 5}).rows  # type: ignore[attr-defined]
    assert len(rows) == 5
    assert reads["title"] == 5
    assert reads["body"] == 5
    assert reads["score"] == 5
    assert reads["layer"] == 5
    assert reads["maturity"] == 5
    assert reads["kind_of"] == 5
    # The sort keys are read on every row; the items that repeat them are read on the five.
    assert reads["created_at"] == 36 + 5
    assert reads["id"] == 36 + 5
    with monkeypatch.context() as scoped:
        _canonical(scoped)
        canonical_reads = _count_reads(scoped)
        database.execute(PAGE, {"max_rows": 5})  # type: ignore[attr-defined]
    assert canonical_reads["title"] == 36
    assert canonical_reads["created_at"] == 36 * 2


def test_an_item_that_can_refuse_is_still_evaluated_on_the_row_it_refuses(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A division by zero sits on the oldest row, which LIMIT 3 discards: it still refuses."""
    text = "MATCH (n:Doc) RETURN n.id, 10.0 / n.score AS inverse ORDER BY n.created_at DESC LIMIT 3"
    deferred = _outcome(database, text, {})
    with monkeypatch.context() as scoped:
        _canonical(scoped)
        canonical = _outcome(database, text, {})
    assert deferred == canonical
    assert deferred[0] == "erro"


def test_a_map_subject_and_a_missing_column_keep_their_refusals_on_discarded_rows(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof fails for a map binding and for an undeclared column of a labelled match."""
    unwind = "UNWIND $maps AS m RETURN m.k AS k, m.o AS o ORDER BY m.o ASC LIMIT 1"
    maps = {"maps": [{"k": 1, "o": 1}, {"o": 2}]}
    labelled = "MATCH (n:Doc) RETURN n.id, n.nope ORDER BY n.created_at LIMIT 2"
    for text, parameters in ((unwind, maps), (labelled, {})):
        deferred = _outcome(database, text, parameters)
        with monkeypatch.context() as scoped:
            _canonical(scoped)
            canonical = _outcome(database, text, parameters)
        assert deferred == canonical, text
        assert deferred[0] == "erro", text
    # A polymorphic match answers null for the undeclared column, on both paths.
    polymorphic = "MATCH (n) RETURN n.id, n.nope ORDER BY n.created_at LIMIT 2"
    assert _outcome(database, polymorphic, {})[0] == "linhas"


def test_the_projection_is_admitted_to_the_row_budget_once_per_scanned_row(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The projection still delivers every scanned row to max_intermediate_rows accounting."""
    admitted: dict[str, int] = {}
    original = query_engine_module._Context.admit_intermediate

    def counted(self, node):  # type: ignore[no-untyped-def]
        admitted[node.label] = admitted.get(node.label, 0) + 1
        return original(self, node)

    monkeypatch.setattr(query_engine_module._Context, "admit_intermediate", counted)
    monkeypatch.setattr(database._queries, "_max_intermediate_rows", 1_000)  # type: ignore[attr-defined]
    rows = database.execute(PAGE, {"max_rows": 5}).rows  # type: ignore[attr-defined]
    assert len(rows) == 5
    deferred = dict(admitted)
    admitted.clear()
    with monkeypatch.context() as scoped:
        _canonical(scoped)
        database.execute(PAGE, {"max_rows": 5})  # type: ignore[attr-defined]
    assert deferred == admitted
    assert deferred["ProjectRows"] == 36


def test_an_alias_a_sort_key_reads_is_projected_on_every_row(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ORDER BY reads an alias from the projected columns, so that item is never deferred."""
    reads = _count_reads(monkeypatch)
    text = "MATCH (n) RETURN n.id AS ident, n.title, n.created_at AS t ORDER BY t DESC, ident LIMIT 4"
    rows = database.execute(text, {}).rows  # type: ignore[attr-defined]
    assert len(rows) == 4
    assert reads["created_at"] == 36
    assert reads["id"] == 36
    assert reads["title"] == 4


def test_the_plan_is_unchanged_and_the_shape_is_only_the_bounded_sort_over_a_projection(
    database: object,
) -> None:
    """No new operator: the planner's shape is what the executor recognises."""
    plan = database.explain(PAGE)  # type: ignore[attr-defined]
    text = str(plan)
    assert "SortRows" in text and "ProjectRows" in text
    assert "Deferred" not in text
