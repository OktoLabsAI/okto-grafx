"""The statement-authority memo: identity-keyed, fence-checked, bounded, never an authority.

``_statement_index_authority`` projected the catalog-authorised stores of a statement on every
``execute``, before the plan cache could even answer.  The projection depends only on the parsed
statement, the catalog picture, the registered index inventory and -- when this transaction
staged speculative DDL -- on what that transaction observed.  The memo keys the projection by the
IDENTITY of the retained parsed statement and re-proves the other fences on every hit; anything
that could change the answer misses, and a statement inside a DDL transaction is never memoized.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.engine.index_manager import IndexManager
from okto_grafx.engine.query_engine import (
    _STATEMENT_AUTHORITY_MEMO_MAX_ENTRIES,
    QueryEngine,
)

SCHEMA = (
    "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))",
    "CREATE NODE TABLE City(id INT64, name STRING, PRIMARY KEY(id))",
    "CREATE REL TABLE Lives(FROM Person TO City)",
)
NODE = "CREATE (:Person {id: $id, name: $name})"
EDGE = "MATCH (a:Person {id: $a}), (b:City {id: $b}) CREATE (a)-[:Lives]->(b)"


class _Counter:
    """Count scoped projections and capture every projection the engine hands out."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.projections = 0
        self.handed: list[object] = []
        original_scoped = IndexManager._statement_indexes_for_tables
        original_authority = QueryEngine._statement_index_authority

        def counted_scoped(
            manager: IndexManager, *args: object, **kwargs: object
        ) -> object:
            self.projections += 1
            return original_scoped(manager, *args, **kwargs)

        def captured_authority(
            engine: QueryEngine, *args: object, **kwargs: object
        ) -> object:
            projection = original_authority(engine, *args, **kwargs)
            self.handed.append(projection)
            return projection

        monkeypatch.setattr(
            IndexManager, "_statement_indexes_for_tables", counted_scoped
        )
        monkeypatch.setattr(
            QueryEngine, "_statement_index_authority", captured_authority
        )


def _open(path: str | None = None) -> okto_grafx.Database:
    database = okto_grafx.connect(":memory:" if path is None else path)
    with database.begin("write") as txn:
        for statement in SCHEMA:
            txn.execute(statement)
    return database


def _engine(database: okto_grafx.Database) -> QueryEngine:
    engine = database._queries  # noqa: SLF001
    assert type(engine) is QueryEngine
    return engine


def _write_people(database: okto_grafx.Database, count: int, *, start: int = 0) -> None:
    with database.begin("write") as txn:
        for index in range(start, start + count):
            txn.execute(NODE, {"id": index, "name": f"p{index}"})


def test_a_retained_statement_projects_its_authority_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _open()
    try:
        counter = _Counter(monkeypatch)
        _write_people(database, 20)

        assert counter.projections == 1
        assert len(counter.handed) == 20
        assert all(projection is counter.handed[0] for projection in counter.handed)
        assert len(_engine(database)._statement_authority_memo) == 1  # noqa: SLF001

        with database.begin("write") as txn:
            txn.execute("CREATE (:City {id: 1, name: 'Lisboa'})")
            for index in range(10):
                txn.execute(EDGE, {"a": index, "b": 1})

        # One projection for the City insert and one for the edge statement.
        assert counter.projections == 3
        assert database.execute(
            "MATCH (p:Person)-[:Lives]->(c:City) RETURN count(*)"
        ).rows == ((10,),)
    finally:
        database.close()


def test_an_equal_but_distinct_statement_never_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _open()
    try:
        counter = _Counter(monkeypatch)
        engine = _engine(database)
        _write_people(database, 2)
        assert counter.projections == 1
        retained = engine._parse_cache[NODE]  # noqa: SLF001

        # Evicting the parsed statement makes the next execute parse an equal-valued but
        # distinct object.  Value equality of the frozen AST would let a hostile or merely
        # coincidental equal object answer for another; identity does not.
        with engine._prepared_guard:  # noqa: SLF001
            del engine._parse_cache[NODE]  # noqa: SLF001
        _write_people(database, 2, start=2)

        fresh = engine._parse_cache[NODE]  # noqa: SLF001
        assert fresh == retained
        assert fresh is not retained
        assert counter.projections == 2
        assert counter.handed[-1] is not counter.handed[0]
    finally:
        database.close()


def test_a_registry_change_forces_a_new_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _open()
    try:
        counter = _Counter(monkeypatch)
        _write_people(database, 3)
        assert counter.projections == 1
        before = counter.handed[-1]

        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Extra(id INT64, PRIMARY KEY(id))")
        _write_people(database, 3, start=3)

        assert counter.handed[-1] is not before
        # The schema statement takes the global projection path; the node insert after the
        # registry moved is the second scoped projection.
        assert counter.projections == 2
        assert database.execute("MATCH (p:Person) RETURN count(*)").rows == ((6,),)
    finally:
        database.close()


def test_a_foreign_catalog_picture_forces_a_new_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = str(tmp_path / "shared")
    first = _open(path)
    second = okto_grafx.connect(path)
    try:
        counter = _Counter(monkeypatch)
        _write_people(first, 3)
        projections_after_first = counter.projections
        before = counter.handed[-1]

        with second.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Foreign(id INT64, PRIMARY KEY(id))")
        _write_people(first, 3, start=3)

        assert counter.handed[-1] is not before
        assert counter.projections > projections_after_first
        assert first.execute("MATCH (p:Person) RETURN count(*)").rows == ((6,),)
    finally:
        second.close()
        first.close()


def test_a_statement_inside_a_ddl_transaction_is_never_memoized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _open()
    try:
        counter = _Counter(monkeypatch)
        engine = _engine(database)
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Late(id INT64, PRIMARY KEY(id))")
            projections_after_ddl = counter.projections
            for index in range(4):
                txn.execute("CREATE (:Late {id: $id})", {"id": index})
            # Every insert re-projects: the transaction's speculative index must stay visible
            # to its own statements, and that answer is transaction-specific.
            assert counter.projections == projections_after_ddl + 4
            late = engine._parse_cache["CREATE (:Late {id: $id})"]  # noqa: SLF001
            assert id(late) not in engine._statement_authority_memo  # noqa: SLF001
            names = {
                getattr(getattr(index, "definition", None), "registry_key", None)
                for index in counter.handed[-1].indexes
            }
            assert any(name is not None and "late" in name.lower() for name in names)
        assert database.execute("MATCH (n:Late) RETURN count(*)").rows == ((4,),)
    finally:
        database.close()


def test_parse_cache_eviction_drops_the_memo_entry_and_the_memo_stays_bounded() -> None:
    database = _open()
    try:
        engine = _engine(database)
        _write_people(database, 1)
        retained = engine._parse_cache[NODE]  # noqa: SLF001
        assert id(retained) in engine._statement_authority_memo  # noqa: SLF001

        for index in range(_STATEMENT_AUTHORITY_MEMO_MAX_ENTRIES + 8):
            database.execute(f"MATCH (p:Person) WHERE p.id = {index} RETURN p.id")

        assert NODE not in engine._parse_cache  # noqa: SLF001
        assert id(retained) not in engine._statement_authority_memo  # noqa: SLF001
        assert (
            len(engine._statement_authority_memo)  # noqa: SLF001
            <= _STATEMENT_AUTHORITY_MEMO_MAX_ENTRIES
        )
    finally:
        database.close()


def test_a_manager_without_an_exact_revision_never_memoizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _open()
    try:
        counter = _Counter(monkeypatch)
        engine = _engine(database)
        manager = engine._indexes  # noqa: SLF001
        assert type(manager) is IndexManager
        monkeypatch.setattr(manager, "_registry_revision", "not-a-revision")

        _write_people(database, 5)

        assert counter.projections == 5
        assert not engine._statement_authority_memo  # noqa: SLF001
    finally:
        database.close()


def test_memoized_and_canonical_executions_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(disable_memo: bool) -> tuple[tuple[object, ...], ...]:
        database = _open()
        try:
            if disable_memo:
                monkeypatch.setattr(
                    QueryEngine,
                    "_memoized_statement_authority",
                    lambda *args, **kwargs: None,
                )
            _write_people(database, 12)
            with database.begin("write") as txn:
                txn.execute("CREATE (:City {id: 1, name: 'Porto'})")
                for index in range(12):
                    txn.execute(EDGE, {"a": index, "b": 1})
            rows = database.execute(
                "MATCH (p:Person)-[:Lives]->(c:City) RETURN p.id, c.name ORDER BY p.id"
            ).rows
            lookups = tuple(
                database.execute(
                    "MATCH (p:Person {id: $id}) RETURN p.name", {"id": index}
                ).rows
                for index in range(12)
            )
            return rows, lookups
        finally:
            database.close()
            monkeypatch.undo()

    assert run(disable_memo=False) == run(disable_memo=True)


def test_a_registry_revision_bump_alone_forces_a_new_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The revision fence is proved on its own: nothing else about the picture moves here."""
    database = _open()
    try:
        counter = _Counter(monkeypatch)
        engine = _engine(database)
        manager = engine._indexes  # noqa: SLF001
        _write_people(database, 2)
        assert counter.projections == 1
        before = counter.handed[-1]

        manager._registry_revision += 1  # noqa: SLF001 - what register/unregister do
        _write_people(database, 2, start=2)

        assert counter.projections == 2
        assert counter.handed[-1] is not before
        assert engine._parse_cache[NODE] is engine._parse_cache[NODE]  # noqa: SLF001
    finally:
        database.close()


def test_a_new_catalog_picture_object_alone_forces_a_new_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalog fence is proved on its own: an equal picture under a new identity misses."""
    database = _open()
    try:
        counter = _Counter(monkeypatch)
        engine = _engine(database)
        store = engine._catalog  # noqa: SLF001
        _write_people(database, 2)
        assert counter.projections == 1
        before = counter.handed[-1]

        current = store.catalog
        replacement = current.copy()
        assert replacement == current and replacement is not current
        store.adopt(replacement)
        _write_people(database, 2, start=2)

        assert counter.projections == 2
        assert counter.handed[-1] is not before
    finally:
        database.close()


def test_parse_only_eviction_drops_the_memo_entry() -> None:
    """The parse cache door evicts the memo entry itself, before the memo's own bound could."""
    database = _open()
    try:
        engine = _engine(database)
        _write_people(database, 1)
        retained = engine._parse_cache[NODE]  # noqa: SLF001
        assert id(retained) in engine._statement_authority_memo  # noqa: SLF001

        # Parsing alone fills the parse cache without executing, so the memo keeps exactly
        # one entry and only the parse-cache eviction can remove it.
        for index in range(_STATEMENT_AUTHORITY_MEMO_MAX_ENTRIES + 4):
            engine.parse(f"MATCH (p:Person) WHERE p.id = {index} RETURN p.id")

        assert NODE not in engine._parse_cache  # noqa: SLF001
        assert id(retained) not in engine._statement_authority_memo  # noqa: SLF001
        assert len(engine._statement_authority_memo) == 0  # noqa: SLF001
    finally:
        database.close()


def test_a_planted_entry_for_another_statement_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry re-proves the statement identity it was stored under, not only the key."""
    database = _open()
    try:
        counter = _Counter(monkeypatch)
        engine = _engine(database)
        _write_people(database, 2)
        assert counter.projections == 1
        retained = engine._parse_cache[NODE]  # noqa: SLF001
        entry = engine._statement_authority_memo[id(retained)]  # noqa: SLF001
        other = engine.parse("MATCH (p:Person) RETURN p.id")
        entry.statement = other  # a forged slot: same key, another statement's identity

        _write_people(database, 2, start=2)

        assert counter.projections == 2
        assert engine._statement_authority_memo[id(retained)].statement is retained  # noqa: SLF001
    finally:
        database.close()
