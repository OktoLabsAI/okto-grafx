"""ST-6: one transaction resolves each landing table once, and its memo dies with it."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.engine.heap_store import HeapStore

TRAVERSE = "MATCH (a:A)-[:E]->(b:B) WHERE a.id = 1 RETURN b.id, b.tag"


@pytest.fixture()
def database(tmp_path: Path):
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture()
def scans(monkeypatch: pytest.MonkeyPatch) -> Counter:
    """Count every heap scan by table name, without changing what the scan answers."""
    counts: Counter = Counter()
    original = HeapStore.scan

    def counting(self, table, *args, **kwargs):
        counts[table.name] += 1
        return original(self, table, *args, **kwargs)

    monkeypatch.setattr(HeapStore, "scan", counting)
    return counts


def _graph(db) -> None:
    with db.begin("write") as txn:
        txn.execute("CREATE NODE TABLE A(id INT64, x INT64, PRIMARY KEY(id))")
        txn.execute("CREATE NODE TABLE B(id INT64, tag STRING, PRIMARY KEY(id))")
        txn.execute("CREATE REL TABLE E(FROM A TO B, w INT64)")
    with db.begin("write") as txn:
        for identity in range(1, 4):
            txn.execute(
                "CREATE (:A {id: $i, x: 0})",
                {"i": identity},
            )
            txn.execute(
                "CREATE (:B {id: $i, tag: $t})",
                {"i": identity, "t": f"t{identity}"},
            )
    with db.begin("write") as txn:
        for source, target, weight in ((1, 1, 1), (1, 2, 2), (2, 1, 1), (3, 3, 2)):
            txn.execute(
                "MATCH (a:A {id: $a}), (b:B {id: $b}) CREATE (a)-[:E {w: $w}]->(b)",
                {"a": source, "b": target, "w": weight},
            )


# --- the memo pays for itself: one landing scan per transaction, not per statement --------------


def test_a_second_traversal_in_the_same_transaction_reuses_the_landing_view(
    database, scans
) -> None:
    _graph(database)
    with database.begin("write") as txn:
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "t1"), (2, "t2")]
        scans.clear()
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "t1"), (2, "t2")]
        assert scans["B"] == 0


def test_a_read_transaction_reuses_the_landing_view_too(database, scans) -> None:
    _graph(database)
    with database.begin("read") as txn:
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "t1"), (2, "t2")]
        scans.clear()
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "t1"), (2, "t2")]
        assert scans["B"] == 0


# --- invalidation is by table, and it answers the transaction's own writes ----------------------


def test_a_write_to_the_landing_table_rebuilds_the_view_exactly_once(
    database, scans
) -> None:
    _graph(database)
    with database.begin("write") as txn:
        txn.execute(TRAVERSE)
        txn.execute("MATCH (b:B {id: 1}) SET b.tag = 'seen'")
        scans.clear()
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "seen"), (2, "t2")]
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "seen"), (2, "t2")]
        assert scans["B"] == 1


def test_a_write_to_another_table_leaves_the_landing_view_standing(
    database, scans
) -> None:
    _graph(database)
    with database.begin("write") as txn:
        txn.execute(TRAVERSE)
        txn.execute("MATCH (a:A {id: 3}) SET a.x = 9")
        scans.clear()
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "t1"), (2, "t2")]
        assert scans["B"] == 0


def test_update_then_delete_in_one_transaction_never_resurrects_the_landing(
    database,
) -> None:
    # The C10 B3/B4/B5 shape, aimed at the memo: a fingerprint that misses the second write
    # of a row -- or the delete after it -- would answer this traversal from a dead picture.
    _graph(database)
    with database.begin("write") as txn:
        txn.execute("MATCH (b:B {id: 1}) SET b.tag = 'marked'")
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "marked"), (2, "t2")]
        txn.execute("MATCH (a:A)-[r:E]->(b:B {id: 1}) DELETE r")
        txn.execute("MATCH (b:B {id: 1}) DELETE b")
        assert sorted(txn.execute(TRAVERSE).rows) == [(2, "t2")]


def test_a_landing_created_in_the_transaction_appears_and_the_view_settles(
    database, scans
) -> None:
    _graph(database)
    with database.begin("write") as txn:
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "t1"), (2, "t2")]
        txn.execute("CREATE (:B {id: 9, tag: 'new'})")
        txn.execute("MATCH (a:A {id: 1}), (b:B {id: 9}) CREATE (a)-[:E {w: 1}]->(b)")
        assert sorted(txn.execute(TRAVERSE).rows) == [
            (1, "t1"),
            (2, "t2"),
            (9, "new"),
        ]
        scans.clear()
        assert sorted(txn.execute(TRAVERSE).rows) == [
            (1, "t1"),
            (2, "t2"),
            (9, "new"),
        ]
        assert scans["B"] == 0


def test_a_landing_ended_by_this_transaction_is_not_matched(database) -> None:
    _graph(database)
    with database.begin("write") as txn:
        txn.execute("MATCH (a:A)-[r:E]->(b:B {id: 1}) DELETE r")
        txn.execute("MATCH (b:B {id: 1}) DELETE b")
        assert sorted(txn.execute(TRAVERSE).rows) == [(2, "t2")]


# --- the memo is transaction-private and unbudgeted ---------------------------------------------


def test_the_memo_never_outlives_its_transaction(database, scans) -> None:
    _graph(database)
    with database.begin("write") as txn:
        txn.execute(TRAVERSE)
        txn.execute("MATCH (b:B {id: 1}) SET b.tag = 'after'")
    scans.clear()
    with database.begin("write") as txn:
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "after"), (2, "t2")]
        assert scans["B"] == 1


def test_the_memo_is_not_admitted_against_transaction_budgets(tmp_path: Path) -> None:
    # The memo retains every visible row of the landing table for the life of the transaction.
    # That memory is engine working state, not staged work: the admission budgets (c1d57e8,
    # 69a02a9) meter what the transaction asks to WRITE -- intent payloads and staged page
    # images -- and a traversal that merely lands on a wide table must not spend them. The
    # graph is built unbudgeted, then reopened under a budget one small write fits and the
    # ~8 KB the memo retains would blow at once if it were admitted.
    builder = okto_grafx.connect(tmp_path / "budgeted", page_size=512)
    try:
        with builder.begin("write") as txn:
            txn.execute("CREATE NODE TABLE A(id INT64, x INT64, PRIMARY KEY(id))")
            txn.execute("CREATE NODE TABLE B(id INT64, tag STRING, PRIMARY KEY(id))")
            txn.execute("CREATE REL TABLE E(FROM A TO B, w INT64)")
        fat = "y" * 200
        with builder.begin("write") as txn:
            txn.execute("CREATE (:A {id: 1, x: 0})")
            for identity in range(1, 41):
                txn.execute("CREATE (:B {id: $i, tag: $t})", {"i": identity, "t": fat})
        with builder.begin("write") as txn:
            for identity in range(1, 41):
                txn.execute(
                    "MATCH (a:A {id: 1}), (b:B {id: $i}) CREATE (a)-[:E {w: 1}]->(b)",
                    {"i": identity},
                )
    finally:
        builder.close()
    handle = okto_grafx.connect(
        tmp_path / "budgeted", page_size=512, max_transaction_bytes=4096
    )
    try:
        with handle.begin("write") as txn:
            txn.execute("MATCH (a:A {id: 1}) SET a.x = 1")
            for _ in range(3):
                rows = txn.execute(TRAVERSE).rows
                assert (
                    len(rows) == 40
                )  # the memo holds ~8 KB of tags; the budget is 4 KB
    finally:
        handle.close()
