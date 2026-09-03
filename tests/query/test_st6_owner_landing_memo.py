"""ST-6: one transaction resolves each landing table once, and its memo dies with it."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxWriteConflict
from okto_grafx.engine import heap_store as heap_store_module
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


@pytest.fixture()
def decodes(monkeypatch: pytest.MonkeyPatch) -> Counter:
    """Count payload tuple decodes by table without counting header-only locator work."""
    counts: Counter = Counter()
    original = heap_store_module.decode_tuple

    def counting(table, payload):
        counts[table.name] += 1
        return original(table, payload)

    monkeypatch.setattr(heap_store_module, "decode_tuple", counting)
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
    database, scans, decodes
) -> None:
    _graph(database)
    with database.begin("write") as txn:
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "t1"), (2, "t2")]
        scans.clear()
        decodes.clear()
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "t1"), (2, "t2")]
        assert scans["B"] == 0
        assert decodes["B"] == 0


def test_a_read_transaction_reuses_the_landing_view_too(
    database, scans, decodes
) -> None:
    _graph(database)
    with database.begin("read") as txn:
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "t1"), (2, "t2")]
        scans.clear()
        decodes.clear()
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "t1"), (2, "t2")]
        assert scans["B"] == 0
        assert decodes["B"] == 0


def test_a_wide_table_decodes_only_distinct_landings_and_deduplicates_repeats(
    database, scans, decodes
) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
        txn.execute("CREATE NODE TABLE B(id INT64, tag STRING, PRIMARY KEY(id))")
        txn.execute("CREATE REL TABLE E(FROM A TO B, w INT64)")
    with database.begin("write") as txn:
        txn.execute("CREATE (:A {id: 1})")
        for identity in range(1, 97):
            txn.execute(
                "CREATE (:B {id: $i, tag: $tag})",
                {"i": identity, "tag": f"landing-{identity}-{'x' * 48}"},
            )
    with database.begin("write") as txn:
        for weight, target in enumerate((*([1] * 8), 2), start=1):
            txn.execute(
                "MATCH (a:A {id: 1}), (b:B {id: $target}) "
                "CREATE (a)-[:E {w: $weight}]->(b)",
                {"target": target, "weight": weight},
            )

    query = "MATCH (a:A)-[:E]->(b:B) WHERE a.id = 1 RETURN b.id, b.tag"
    with database.begin("read") as txn:
        scans.clear()
        decodes.clear()
        rows = txn.execute(query).rows
        assert [row[0] for row in rows].count(1) == 8
        assert [row[0] for row in rows].count(2) == 1
        assert scans["B"] == 0
        assert decodes["B"] == 2

        decodes.clear()
        assert txn.execute(query).rows == rows
        assert decodes["B"] == 0


# --- invalidation is by table, and it answers the transaction's own writes ----------------------


def test_a_write_to_the_landing_table_rebuilds_the_view_exactly_once(
    database, scans, decodes
) -> None:
    _graph(database)
    with database.begin("write") as txn:
        txn.execute(TRAVERSE)
        txn.execute("MATCH (b:B {id: 1}) SET b.tag = 'seen'")
        scans.clear()
        decodes.clear()
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "seen"), (2, "t2")]
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "seen"), (2, "t2")]
        assert scans["B"] == 0
        assert decodes["B"] == 2


def test_a_write_to_another_table_leaves_the_landing_view_standing(
    database, scans, decodes
) -> None:
    _graph(database)
    with database.begin("write") as txn:
        txn.execute(TRAVERSE)
        txn.execute("MATCH (a:A {id: 3}) SET a.x = 9")
        scans.clear()
        decodes.clear()
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "t1"), (2, "t2")]
        assert scans["B"] == 0
        # The dirty A frontier visits its newly changed row too, so B3 is requested for the first
        # time.  B1/B2 remain cached: one new identity decode, never a three-row landing scan.
        assert decodes["B"] == 1


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
    database, scans, decodes
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
        decodes.clear()
        assert sorted(txn.execute(TRAVERSE).rows) == [
            (1, "t1"),
            (2, "t2"),
            (9, "new"),
        ]
        assert scans["B"] == 0
        assert decodes["B"] == 0


def test_a_landing_ended_by_this_transaction_is_not_matched(database) -> None:
    _graph(database)
    with database.begin("write") as txn:
        txn.execute("MATCH (a:A)-[r:E]->(b:B {id: 1}) DELETE r")
        txn.execute("MATCH (b:B {id: 1}) DELETE b")
        assert sorted(txn.execute(TRAVERSE).rows) == [(2, "t2")]


def test_a_refused_statement_savepoint_does_not_poison_the_cached_view(
    database, decodes
) -> None:
    _graph(database)
    with database.begin("write") as txn:
        expected = sorted(txn.execute(TRAVERSE).rows)
        accepted = tuple(txn._context.row_intents)
        with pytest.raises(GrafxConfigurationError):
            txn.execute(
                "MATCH (a:A {id: 1}), (b:B {id: 1}), (c:B {id: 2}) "
                "SET b.tag = 'poison' DELETE c CREATE (a)-[:E {w: 9}]->(c)"
            )
        assert tuple(txn._context.row_intents) == accepted
        decodes.clear()
        assert sorted(txn.execute(TRAVERSE).rows) == expected
        assert decodes["B"] == 0


# --- the memo is transaction-private, internally bounded, and settled --------------------------


def test_the_memo_never_outlives_its_transaction(database, scans, decodes) -> None:
    _graph(database)
    with database.begin("write") as txn:
        txn.execute(TRAVERSE)
        txn.execute("MATCH (b:B {id: 1}) SET b.tag = 'after'")
    scans.clear()
    decodes.clear()
    with database.begin("write") as txn:
        assert sorted(txn.execute(TRAVERSE).rows) == [(1, "after"), (2, "t2")]
        assert scans["B"] == 0
        assert decodes["B"] == 2


def test_retry_retires_the_predecessors_memo_before_the_successor_reads(database) -> None:
    _graph(database)
    winner = database.begin("write")
    loser = database.begin("write")
    try:
        assert sorted(loser.execute(TRAVERSE).rows) == [(1, "t1"), (2, "t2")]
        old_txn_id = loser.txn_id
        assert old_txn_id in database._queries._owner_memo

        winner.execute("MATCH (b:B {id: 1}) SET b.tag = 'winner'")
        winner.commit()
        loser.execute("MATCH (b:B {id: 1}) SET b.tag = 'loser'")
        with pytest.raises(GrafxWriteConflict):
            loser.commit()

        successor = database.retry(loser)
        assert old_txn_id not in database._queries._owner_memo
        assert sorted(successor.execute(TRAVERSE).rows) == [
            (1, "winner"),
            (2, "t2"),
        ]
        successor.rollback()
    finally:
        if winner.active:
            winner.rollback()
        if loser.active:
            loser.rollback()


def test_the_memo_is_not_admitted_against_transaction_budgets(tmp_path: Path) -> None:
    # Lazy landing retention has its own explicit engine budget.  The public transaction budget
    # still meters staged writes, not read acceleration: one small update fits even while the
    # traversal requests forty wide rows.  Unlike old ST-6, only those requested rows can be
    # retained, and their charges disappear at transaction settlement.
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
                assert len(rows) == 40
        assert handle._queries._owner_memo == {}
        assert handle._queries._owner_budget._used_bytes == 0
        assert handle._queries._owner_budget._used_entries == 0
    finally:
        handle.close()
