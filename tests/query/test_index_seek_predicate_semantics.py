"""An exact seek is an access path, not permission to rewrite predicate semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query.plan import FilterRows, IndexSeek, NodeScan


@pytest.fixture()
def database(tmp_path: Path):
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    try:
        with handle.begin("write") as txn:
            txn.execute(
                "CREATE NODE TABLE WithPk(k INT64, name STRING, PRIMARY KEY(k))"
            )
            txn.execute("CREATE NODE TABLE WithoutIndex(k INT64, name STRING)")
        with handle.begin("write") as txn:
            for table in ("WithPk", "WithoutIndex"):
                txn.execute(f"CREATE (:{table} {{k: 1, name: 'one'}})")
                txn.execute(f"CREATE (:{table} {{k: 2, name: 'two'}})")
        yield handle
    finally:
        handle.close()


def _operators(handle, statement: str) -> tuple[object, ...]:
    return tuple(handle.explain(statement).walk())


def test_seek_keeps_the_original_conjunction_over_a_matching_hit(database) -> None:
    """A STRING is unknown inside AND, but is a refused standalone WHERE predicate."""

    keyed = "MATCH (p:WithPk) WHERE p.k = 1 AND p.name RETURN p.k"
    scanned = "MATCH (p:WithoutIndex) WHERE p.k = 1 AND p.name RETURN p.k"

    assert database.execute(scanned).rows == ()
    assert database.execute(keyed).rows == ()

    plan = _operators(database, keyed)
    assert any(isinstance(node, IndexSeek) for node in plan)
    filters = [node for node in plan if isinstance(node, FilterRows)]
    assert len(filters) == 1
    assert filters[0].predicate.describe() == "((p.k = 1) AND p.name)"


def test_seek_miss_keeps_the_equality_short_circuit(database) -> None:
    statement = "MATCH (p:WithPk) WHERE p.k = 9 AND p.name RETURN p.k"

    assert database.execute(statement).rows == ()
    assert any(isinstance(node, IndexSeek) for node in _operators(database, statement))


def test_a_term_before_the_indexed_equality_keeps_the_canonical_scan(database) -> None:
    """The seek must not hide an exception observed before its equality on a non-hit row."""

    keyed = "MATCH (p:WithPk) WHERE (1 / (p.k - 2)) = 0 AND p.k = 9 RETURN p.k"
    scanned = keyed.replace("WithPk", "WithoutIndex")

    plan = _operators(database, keyed)
    assert not any(isinstance(node, IndexSeek) for node in plan)
    assert any(isinstance(node, NodeScan) for node in plan)
    with pytest.raises(GrafxPlanError):
        database.execute(scanned)
    with pytest.raises(GrafxPlanError):
        database.execute(keyed)


def test_a_valid_leading_equality_still_uses_the_index(database) -> None:
    statement = "MATCH (p:WithPk) WHERE p.k = 2 AND p.name = 'two' RETURN p.k"

    result = database.execute(statement)

    assert result.rows == ((2,),)
    assert result.statistics["rows_seeked"] == 1
    assert any(isinstance(node, IndexSeek) for node in _operators(database, statement))
