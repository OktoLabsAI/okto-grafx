"""The checkpoint photographs only the tables its replay can have moved -- and never guesses.

A checkpoint proves the committed watermark of every table an index covers by walking that
table's heap headers (``HeapStore.committed_high_water``). The redo preflight classifies every
replayed page image by the heap table that owns it, and ``IndexManager.table_watermark_photo``
walks only those tables when -- and only when -- that classification is a verified proof. These
tests pin the scope by COUNT of walks, never by a clock, attribute every walk to the photograph
that asked for it or to the rest of the checkpoint, and pin the doors back to the complete
photograph: a catalog change in the interval, a replay whose page scope cannot be proved, and a
manager that merely looks like the concrete one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.engine.commit_redo import CommitRedo
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import IndexManager

TABLES = ("A", "B", "C")


def _seed(database: object) -> None:
    """Three indexed tables, one committed row each, one complete photograph on record."""
    with database.begin("write") as txn:
        for name in TABLES:
            txn.execute(
                f"CREATE NODE TABLE {name}(id INT64, name STRING, PRIMARY KEY(id))"
            )
    for name in TABLES:
        with database.begin("write") as txn:
            txn.execute(f"CREATE (:{name} {{id: 1, name: 'before'}})")
    database.checkpoint()


def _table_id(database: object, name: str) -> int:
    return database.catalog.catalog.table(name).table_id


class _Spy:
    """Record every heap walk, attributed to the photograph that asked for it or not."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.inside_photo: list[str] = []
        self.outside_photo: list[str] = []
        self.photos: list[frozenset[int] | None] = []
        self._depth = 0
        real_walk = HeapStore.committed_high_water
        real_photo = IndexManager.table_watermark_photo
        spy = self

        def walk(self_: HeapStore, table: object):  # type: ignore[no-untyped-def]
            (spy.inside_photo if spy._depth else spy.outside_photo).append(table.name)
            return real_walk(self_, table)

        def photo(self_: IndexManager, *, refresh_table_ids=None):  # type: ignore[no-untyped-def]
            spy.photos.append(
                None if refresh_table_ids is None else frozenset(refresh_table_ids)
            )
            spy._depth += 1
            try:
                return real_photo(self_, refresh_table_ids=refresh_table_ids)
            finally:
                spy._depth -= 1

        monkeypatch.setattr(HeapStore, "committed_high_water", walk)
        monkeypatch.setattr(IndexManager, "table_watermark_photo", photo)

    @property
    def proved_scope(self) -> frozenset[int] | None:
        """The union of every proved scope, or None if any photograph was the complete one."""
        if any(scope is None for scope in self.photos):
            return None
        return frozenset().union(*self.photos)


def test_the_photograph_walks_only_the_table_the_replay_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One non-indexed property update on A; B and C stay active but untouched."""
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        with database.begin("write") as txn:
            txn.execute("MATCH (a:A {id: 1}) SET a.name = 'after'")
        spy = _Spy(monkeypatch)
        database.checkpoint()
        assert spy.photos, "the checkpoint must photograph before certifying"
        assert spy.proved_scope == {_table_id(database, "A")}, spy.photos
        assert set(spy.inside_photo) == {"A"}, spy.inside_photo
        assert database.execute("MATCH (a:A {id: 1}) RETURN a.name").rows == (
            ("after",),
        )
        assert database.verify("all").findings == ()
    finally:
        database.close()


def test_the_whole_checkpoint_walks_only_the_table_the_replay_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract for the checkpoint as a whole, including a foreign writer.

    Every ``committed_high_water`` walk on the checkpoint path -- including the ones
    ``IndexManager.open`` performs outside ``table_watermark_photo`` -- must stay on the table
    the replay touched. A walk of an untouched table anywhere on the path is the cost this
    change exists to remove.
    """
    root = str(tmp_path / "db")
    database = connect(root)
    try:
        _seed(database)
        writer = connect(root)
        try:
            with writer.begin("write") as txn:
                txn.execute("MATCH (a:A {id: 1}) SET a.name = 'after'")
        finally:
            writer.close()
        spy = _Spy(monkeypatch)
        database.checkpoint()
        assert set(spy.inside_photo) == {"A"}, spy.inside_photo
        assert set(spy.outside_photo) <= {"A"}, spy.outside_photo
        assert database.execute("MATCH (a:A {id: 1}) RETURN a.name").rows == (
            ("after",),
        )
    finally:
        database.close()


def test_a_scoped_photograph_still_reports_every_active_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narrowing the walk never narrows the answer: untouched tables keep their last watermark."""
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        manager = database._transactions._index_manager
        assert type(manager) is IndexManager
        ids = {name: _table_id(database, name) for name in TABLES}
        before = dict(manager._table_watermarks)
        assert set(before) >= set(ids.values())
        with database.begin("write") as txn:
            txn.execute("MATCH (b:B {id: 1}) SET b.name = 'after'")
        spy = _Spy(monkeypatch)
        database.checkpoint()
        after = dict(manager._table_watermarks)
        assert spy.proved_scope == {ids["B"]}, spy.photos
        assert set(spy.inside_photo) == {"B"}, spy.inside_photo
        assert set(after) == set(before)
        assert after[ids["B"]] > before[ids["B"]]
        assert after[ids["A"]] == before[ids["A"]]
        assert after[ids["C"]] == before[ids["C"]]
    finally:
        database.close()


def _assert_complete_photograph(spy: _Spy, *tables: str) -> None:
    """The first photograph of the checkpoint was the complete one and walked every table."""
    assert spy.photos and spy.photos[0] is None, spy.photos
    # A later pass with no page effects may legitimately ask to refresh nothing.
    assert all(scope is None or scope == frozenset() for scope in spy.photos), (
        spy.photos
    )
    assert set(spy.inside_photo) >= set(tables), spy.inside_photo


def test_a_catalog_change_in_the_interval_takes_the_complete_photograph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DDL touches the catalog: the shortcut is refused and every active table is walked."""
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE D(id INT64, name STRING, PRIMARY KEY(id))")
        with database.begin("write") as txn:
            txn.execute("CREATE (:D {id: 1, name: 'new'})")
        spy = _Spy(monkeypatch)
        database.checkpoint()
        _assert_complete_photograph(spy, "A", "B", "C", "D")
        assert database.verify("all").findings == ()
    finally:
        database.close()


def test_an_unproved_page_scope_takes_the_complete_photograph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single page whose owner cannot be proved invalidates the whole shortcut, not one table."""
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        with database.begin("write") as txn:
            txn.execute("MATCH (a:A {id: 1}) SET a.name = 'after'")
        monkeypatch.setattr(
            CommitRedo,
            "_watermark_scope_for_page",
            lambda self, file, page, **kwargs: (False, frozenset()),
        )
        spy = _Spy(monkeypatch)
        database.checkpoint()
        _assert_complete_photograph(spy, "A", "B", "C")
        assert database.execute("MATCH (a:A {id: 1}) RETURN a.name").rows == (
            ("after",),
        )
        assert database.verify("all").findings == ()
    finally:
        database.close()


def test_a_lookalike_index_manager_never_earns_the_shortcut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the concrete IndexManager may classify a page; a subclass keeps the complete scan."""
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        with database.begin("write") as txn:
            txn.execute("MATCH (c:C {id: 1}) SET c.name = 'after'")

        class _Lookalike(IndexManager):
            __slots__ = ()

        redo = database._transactions._commit_redo
        assert type(redo._index_manager) is IndexManager
        monkeypatch.setattr(redo._index_manager, "__class__", _Lookalike)
        spy = _Spy(monkeypatch)
        database.checkpoint()
        _assert_complete_photograph(spy, "A", "B", "C")
    finally:
        database.close()
