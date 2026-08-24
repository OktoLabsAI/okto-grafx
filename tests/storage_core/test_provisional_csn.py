"""Heap lifetime semantics for frames persisted before their WAL append succeeded."""

from __future__ import annotations

from okto_grafx.domain.ids import PROVISIONAL_CSN
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.engine.heap_store import HeapStore

from .conftest import SnapshotDouble


def test_a_provisional_update_leaves_the_committed_version_writable(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    """A persisted pre-WAL xmax is an abandoned end, while its new birth is abandoned."""
    original = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    abandoned = heap_store.update(
        person_table, original, (1, "never committed"), xmin=PROVISIONAL_CSN
    )

    assert heap_store.read(original).live is True
    assert heap_store.read(abandoned).live is False
    replacement = heap_store.update(person_table, original, (1, "Grace"), xmin=20)

    assert heap_store.read(original).xmax == 20
    assert heap_store.read(replacement).live is True
    assert [
        version.values
        for _ref, version in heap_store.scan(person_table, SnapshotDouble(20))
    ] == [(1, "Grace")]


def test_a_provisional_delete_leaves_the_committed_version_deletable(
    heap_store: HeapStore, person_table: TableDef
) -> None:
    """A later real delete may replace the abandoned provisional end stamp."""
    original = heap_store.insert(person_table, 1, (1, "Ada"), xmin=10)
    heap_store.delete(person_table, original, xmax=PROVISIONAL_CSN)

    assert heap_store.read(original).live is True
    heap_store.delete(person_table, original, xmax=20)

    assert heap_store.read(original).live is False
    assert heap_store.lookup(person_table, 1, SnapshotDouble(20)) is None
