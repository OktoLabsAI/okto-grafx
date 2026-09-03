"""The indexes over a relationship table's endpoints, and the traversal that reads them.

WHY THIS FILE EXISTS. Traversal was a scan: "the edges leaving this node" was answered by
filtering EVERY edge of the table, per frontier node, and resolving each landing by scanning the
landing table. Measured on a 2500-node knowledge graph, a reverse hop into a well-referenced
entity read all 3600 edges and cost 1.46 s -- the slowest thing left in a real graph after the
primary keys were indexed. A stored relationship row leads with its endpoints (W5c), so the
question is exactly what an EXACT index over stored positions 0 and 1 answers.

What these tests hold, in order: the indexes exist and cover the right positions; the commit
populates them (through the same staging seam every index uses); the traversal answers the SAME
rows through the index and through the grouped-scan fallback; a plan-known whole-table frontier
never spends speculative endpoint probes; a stale endpoint index is never consulted; and the
accelerator declines rather than failing a statement, the same rule the primary key's index follows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import IndexManager, edge_from_index_name, edge_to_index_name


@pytest.fixture()
def database(tmp_path: Path):
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    try:
        yield handle
    finally:
        handle.close()


def _small_graph(db) -> None:
    with db.begin("write") as txn:
        txn.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
        txn.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        txn.execute("CREATE REL TABLE E(FROM A TO B, w INT64)")
    with db.begin("write") as txn:
        for identity in range(1, 6):
            txn.execute("CREATE (:A {id: $i})", {"i": identity})
        for identity in range(1, 4):
            txn.execute("CREATE (:B {id: $i})", {"i": identity})
    with db.begin("write") as txn:
        for source, target in ((1, 1), (1, 2), (2, 1), (3, 3), (4, 1), (5, 2)):
            txn.execute(
                "MATCH (a:A {id: $a}), (b:B {id: $b}) CREATE (a)-[:E {w: 1}]->(b)",
                {"a": source, "b": target},
            )


def test_the_commit_populates_both_endpoint_indexes(database) -> None:
    """One entry per edge in each, through the same staging seam every index uses.

    Walked directly rather than through a query, so a traversal that silently fell back to the
    scan could not make an EMPTY index look populated (the E3 shape, one component over).
    """
    _small_graph(database)
    assert len(database.inspect_index(edge_from_index_name("E"))) == 6
    assert len(database.inspect_index(edge_to_index_name("E"))) == 6


def test_traversal_answers_the_same_rows_by_index_and_by_scan(database) -> None:
    """The index is an accelerator: same rows forward, reverse, and undirected, both regimes."""
    _small_graph(database)
    shapes = [
        ("MATCH (a:A {id: 1})-[:E]->(b:B) RETURN b.id", None),
        ("MATCH (a:A)-[:E]->(b:B {id: 1}) RETURN a.id", None),
        ("MATCH (a:A)-[:E]->(b:B) RETURN a.id, b.id", None),
        ("MATCH (b:B {id: 1})<-[:E]-(a:A) RETURN a.id", None),
    ]
    indexed = [sorted(database.execute(text).rows) for text, _p in shapes]
    database._indexes.index(edge_from_index_name("E")).mark_stale("forced by this test")
    database._indexes.index(edge_to_index_name("E")).mark_stale("forced by this test")
    scanned = [sorted(database.execute(text).rows) for text, _p in shapes]
    assert indexed == scanned
    assert indexed[0] == [(1,), (2,)]
    assert indexed[1] == [(1,), (2,), (4,)]


def test_seek_frontier_uses_endpoint_lookup_but_scan_frontier_does_not(database) -> None:
    """The plan shape, not the number of rows observed so far, selects the traversal regime."""
    _small_graph(database)

    seek = database.execute("MATCH (a:A {id: 1})-[:E]->(b:B) RETURN b.id")
    scan = database.execute("MATCH (a:A)-[:E]->(b:B) RETURN a.id, b.id")

    assert sorted(seek.rows) == [(1,), (2,)]
    assert seek.statistics.get("edge_lookups") == 1
    assert seek.statistics.get("edge_scans", 0) == 0
    assert len(scan.rows) == 6
    assert scan.statistics.get("edge_lookups", 0) == 0
    assert scan.statistics.get("edge_scans") == 1


def test_endpoint_acceleration_crosses_the_central_exact_view_fence(
    database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _small_graph(database)
    crossed: list[str] = []
    original = IndexManager.validated

    def recording(self, index, key, snapshot):
        crossed.append(index.name)
        return original(self, index, key, snapshot)

    monkeypatch.setattr(IndexManager, "validated", recording)
    assert sorted(
        database.execute("MATCH (a:A {id: 1})-[:E]->(b:B) RETURN b.id").rows
    ) == [(1,), (2,)]
    assert edge_from_index_name("E") in crossed


def test_endpoint_seek_does_not_retain_every_exact_version_for_a_hub(
    database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Endpoint hits stay lazy instead of retaining ``degree * payload`` versions."""
    _small_graph(database)
    reads: list[object] = []
    original = HeapStore.read
    original_versions = IndexManager.lookup_versions

    def recording(self, ref):
        reads.append(ref)
        return original(self, ref)

    def guarded_versions(self, name, key, snapshot):
        if name in {edge_from_index_name("E"), edge_to_index_name("E")}:
            pytest.fail("endpoint indexes must not retain every validated heap version")
        return original_versions(self, name, key, snapshot)

    monkeypatch.setattr(HeapStore, "read", recording)
    monkeypatch.setattr(IndexManager, "lookup_versions", guarded_versions)
    result = database.execute("MATCH (a:A {id: 1})-[:E]->(b:B) RETURN b.id")

    assert sorted(result.rows) == [(1,), (2,)]
    assert len(reads) == 5
    assert len(set(reads)) == 3


def test_a_whole_table_frontier_scans_without_spending_the_fan_limit_first(
    database,
) -> None:
    """A whole-table frontier performs one grouped scan from its first emitted node.

    More distinct start nodes than the former limit, and the total is asserted against arithmetic
    (every A of the table has exactly one edge), so a scan that loses or duplicates rows cannot
    pass. The statistics make the absence of speculative endpoint probes observable.
    """
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
        txn.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        txn.execute("CREATE REL TABLE E(FROM A TO B, w INT64)")
    with database.begin("write") as txn:
        for identity in range(1, 101):
            txn.execute("CREATE (:A {id: $i})", {"i": identity})
        for identity in range(1, 4):
            txn.execute("CREATE (:B {id: $i})", {"i": identity})
    with database.begin("write") as txn:
        for identity in range(1, 101):
            txn.execute(
                "MATCH (a:A {id: $a}), (b:B {id: $b}) CREATE (a)-[:E {w: 1}]->(b)",
                {"a": identity, "b": identity % 3 + 1},
            )
    result = database.execute("MATCH (a:A)-[:E]->(b:B) RETURN a.id, b.id")
    assert len(result.rows) == 100
    assert len(set(result.rows)) == 100
    assert result.statistics.get("edge_lookups", 0) == 0
    assert result.statistics.get("edge_scans") == 1


def test_a_deleted_landing_node_is_not_reached_through_its_edges(database) -> None:
    """An edge is followed only when the snapshot sees the node it lands on -- index or scan."""
    _small_graph(database)
    with database.begin("write") as txn:
        txn.execute("MATCH (b:B) WHERE b.id = 1 DELETE b")
    assert database.execute("MATCH (a:A {id: 2})-[:E]->(b:B) RETURN b.id").rows == ()
    assert sorted(database.execute("MATCH (a:A {id: 1})-[:E]->(b:B) RETURN b.id").rows) == [
        (2,)
    ]


def test_endpoint_indexes_come_back_fresh_on_reopen(tmp_path: Path) -> None:
    root = tmp_path / "db"
    with okto_grafx.connect(root, page_size=512) as db:
        _small_graph(db)
    with okto_grafx.connect(root, page_size=512) as reopened:
        assert "ef_E" in reopened.attached_indexes
        assert "et_E" in reopened.attached_indexes
        assert reopened.stale_indexes == ()
        assert sorted(
            reopened.execute("MATCH (a:A)-[:E]->(b:B {id: 1}) RETURN a.id").rows
        ) == [(1,), (2,), (4,)]
        assert reopened.verify("all").findings == ()


def test_a_rel_table_whose_name_leaves_no_room_for_an_index_name_still_works(
    database,
) -> None:
    """The accelerator declines; the statement and the traversal do not fail.

    Same rule, same guard, same reason as the primary key's index: an index may not fail a
    statement, and a table whose endpoint indexes could not be created traverses by the scan it
    always did.
    """
    long_name = "R" + "x" * 127
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
        txn.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        txn.execute(f"CREATE REL TABLE {long_name}(FROM A TO B, w INT64)")
    assert long_name in database.queries.skipped_indexes
    with database.begin("write") as txn:
        txn.execute("CREATE (:A {id: 1})")
        txn.execute("CREATE (:B {id: 1})")
    with database.begin("write") as txn:
        txn.execute(
            f"MATCH (a:A {{id: 1}}), (b:B {{id: 1}}) CREATE (a)-[:{long_name} {{w: 1}}]->(b)"
        )
    assert database.execute(
        f"MATCH (a:A)-[:{long_name}]->(b:B) RETURN a.id, b.id"
    ).rows == ((1, 1),)
