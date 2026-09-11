"""The six blocking defects of the C10 round-2 blind review, each pinned through the public door.

Every assertion reads the rows back on a COLD reopen or through a second connection, never
through the statement's own report (LESSONS L16, L23). The shapes are the critic's own.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxParseError, GrafxQueryError, GrafxWriteConflict

SCHEMA: tuple[str, ...] = (
    "CREATE NODE TABLE P(id INT64, a INT64, b INT64, c INT64, PRIMARY KEY(id))",
    "CREATE REL TABLE R(FROM P TO P, w INT64)",
)


@pytest.fixture
def path(tmp_path: Path) -> str:
    root = str(tmp_path / "db")
    handle = okto_grafx.connect(root)
    try:
        with handle.begin("write") as txn:
            for statement in SCHEMA:
                txn.execute(statement)
    finally:
        handle.close()
    return root


def _live(path: str) -> list[tuple[object, ...]]:
    """Return every live P row as a cold reopen sees it."""
    handle = okto_grafx.connect(path)
    try:
        return sorted(handle.execute("MATCH (p:P) RETURN p.id, p.a, p.b, p.c").rows)
    finally:
        handle.close()


def _write(path: str, *statements: str) -> None:
    handle = okto_grafx.connect(path)
    try:
        with handle.begin("write") as txn:
            for statement in statements:
                txn.execute(statement)
    finally:
        handle.close()


# --- B1: inline properties on the TARGET node of a traversal are a filter ------------------------


@pytest.fixture
def fan(path: str) -> str:
    _write(path, "CREATE (:P {id: 1})", "CREATE (:P {id: 2})", "CREATE (:P {id: 3})")
    _write(
        path,
        "MATCH (x:P {id: 1}), (y:P {id: 2}) CREATE (x)-[:R {w: 1}]->(y)",
        "MATCH (x:P {id: 1}), (y:P {id: 3}) CREATE (x)-[:R {w: 3}]->(y)",
    )
    return path


def test_a_target_node_property_map_selects_only_the_rows_it_names(fan: str) -> None:
    """The map on a target node used to be dropped: every neighbour of x came back."""
    handle = okto_grafx.connect(fan)
    try:
        both = handle.execute("MATCH (x:P {id: 1})-[r:R]->(y:P {id: 2}) RETURN y.id, r.w").rows
        none = handle.execute("MATCH (x:P {id: 1})-[r:R]->(y:P {id: 99}) RETURN y.id").rows
    finally:
        handle.close()
    assert sorted(both) == [(2, 1)]
    assert none == ()


def test_a_set_and_a_delete_through_a_target_map_touch_only_the_named_row(fan: str) -> None:
    """The consequence that made it data loss: SET/DELETE above the traversal hit every row."""
    _write(fan, "MATCH (x:P {id: 1})-[:R]->(y:P {id: 2}) SET y.a = 7")
    assert _live(fan) == [(1, None, None, None), (2, 7, None, None), (3, None, None, None)]
    _write(fan, "MATCH (x:P {id: 1})-[:R]->(y:P {id: 2}) DETACH DELETE y")
    assert [row[0] for row in _live(fan)] == [1, 3]


# --- B2: a key-changing SET declares the partition of the key it HAD ----------------------------


def test_a_key_change_conflicts_with_a_concurrent_write_of_the_same_row(path: str) -> None:
    """Two transactions at one snapshot; A moves row 1 to key 7, B sets a on every row.

    A declared only the NEW key's partition, so B passed optimistic validation and the heap
    refused it inside the commit section -- and the rows B's `_write_rows` had already written
    stayed behind as a committed-looking write. Now the two meet where step 3.3 can see them:
    B is refused with a retryable write conflict, and nothing of B survives.
    """
    _write(path, "CREATE (:P {id: 2, a: 0})", "CREATE (:P {id: 1, a: 0})")
    handle = okto_grafx.connect(path)
    try:
        a = handle.begin("write")
        b = handle.begin("write")
        a.execute("MATCH (n:P {id: 1}) SET n.id = 7")
        b.execute("MATCH (n:P) SET n.a = 3")
        a.commit()
        with pytest.raises(GrafxWriteConflict):
            b.commit()
        b.rollback()
        with handle.begin("write") as later:
            later.execute("CREATE (:P {id: 9})")
    finally:
        handle.close()
    assert _live(path) == [(2, 0, None, None), (7, 0, None, None), (9, None, None, None)]


# --- B3: a row this transaction deleted cannot be matched again ----------------------------------


def test_a_later_bulk_set_does_not_resurrect_a_row_this_transaction_deleted(path: str) -> None:
    _write(path, "CREATE (:P {id: 1, a: 0})", "CREATE (:P {id: 2, a: 0})")
    _write(path, "MATCH (n:P {id: 1}) DELETE n", "MATCH (n:P) SET n.a = 5")
    assert _live(path) == [(2, 5, None, None)]


# --- B4: two updates of one row in one statement both land ---------------------------------------


def test_two_set_clauses_in_one_statement_build_on_each_other(path: str) -> None:
    _write(path, "CREATE (:P {id: 1, a: 0, b: 0, c: 0})")
    _write(
        path,
        "MATCH (n:P {id: 1}) SET n.c = 9",
        "MATCH (n:P {id: 1}) SET n.a = 1 SET n.b = 2",
    )
    assert _live(path) == [(1, 1, 2, 9)]


def test_two_variables_bound_to_one_row_both_assign(path: str) -> None:
    _write(path, "CREATE (:P {id: 1, a: 0, b: 0, c: 0})")
    _write(path, "MATCH (n:P {id: 1}), (m:P {id: 1}) SET n.a = 1, m.b = 2")
    assert _live(path) == [(1, 1, 2, 0)]


# --- B5: what exists is what this transaction says exists, not the versions it replaced ----------


def test_merge_does_not_match_a_version_this_transaction_replaced(path: str) -> None:
    _write(path, "CREATE (:P {id: 1, a: 0})")
    _write(
        path,
        "MATCH (n:P {id: 1}) SET n.id = 2",
        "MERGE (m:P {id: 1}) SET m.a = 9",
    )
    assert _live(path) == [(1, 9, None, None), (2, 0, None, None)]


def test_merge_after_set_and_delete_of_a_key_creates_the_row_again(path: str) -> None:
    _write(path, "CREATE (:P {id: 1, a: 0})")
    _write(
        path,
        "MATCH (n:P {id: 1}) SET n.a = 5",
        "MATCH (n:P {id: 1}) DELETE n",
        "MERGE (n:P {id: 1})",
    )
    assert [row[0] for row in _live(path)] == [1]


def test_a_key_freed_by_a_key_change_may_be_created_in_the_same_transaction(path: str) -> None:
    _write(path, "CREATE (:P {id: 1, a: 0})")
    _write(path, "MATCH (n:P {id: 1}) SET n.id = 2", "CREATE (:P {id: 1, a: 9})")
    assert _live(path) == [(1, 9, None, None), (2, 0, None, None)]


def test_a_second_row_under_a_key_this_transaction_still_holds_is_refused(path: str) -> None:
    """The other direction, so the key check is not merely permissive: the key is NOT free."""
    _write(path, "CREATE (:P {id: 1, a: 0})")
    handle = okto_grafx.connect(path)
    try:
        with handle.begin("write") as txn:
            txn.execute("MATCH (n:P {id: 1}) SET n.a = 5")
            with pytest.raises(GrafxQueryError):
                txn.execute("CREATE (:P {id: 1, a: 9})")
    finally:
        handle.close()


# --- B6: zero-length paths are refused, not answered as one hop ---------------------------------


def test_a_zero_hop_pattern_is_refused_at_the_door(fan: str) -> None:
    handle = okto_grafx.connect(fan)
    try:
        with pytest.raises(GrafxParseError):
            handle.execute("MATCH (x:P {id: 1})-[:R*0..1]->(y:P) RETURN y.id")
    finally:
        handle.close()


# --- the ended-row rule holds on every read path, not only the scan -----------------------------


def test_a_row_this_transaction_deleted_is_invisible_through_a_traversal_too(fan: str) -> None:
    """The scan learned to skip rows this transaction ended; the traversal's landing and the
    index seek are the other two read paths, and a rule that holds on one of three is a rule a
    caller finds by accident. Delete node 2, then traverse from 1: 2 is not a landing any more."""
    handle = okto_grafx.connect(fan)
    try:
        with handle.begin("write") as txn:
            txn.execute("MATCH (y:P {id: 2}) DETACH DELETE y")
            rows = txn.execute("MATCH (x:P {id: 1})-[:R]->(y:P) RETURN y.id").rows
            assert sorted(rows) == [(3,)]
    finally:
        handle.close()
    assert [row[0] for row in _live(fan)] == [1, 3]


def test_a_match_after_a_delete_in_the_same_transaction_does_not_return_the_row(path: str) -> None:
    """The read-side half of the ended-row rule, pinned on its own.

    The write-side half (a later bulk SET must not resurrect the row) is also held by the
    transaction manager's settle, so a test of the outcome cannot tell the two lines apart. A
    RETURN inside the transaction has no second line: if the scan hands out the deleted row, the
    caller sees a row it has just asked to be rid of.
    """
    _write(path, "CREATE (:P {id: 1, a: 0})", "CREATE (:P {id: 2, a: 0})")
    handle = okto_grafx.connect(path)
    try:
        with handle.begin("write") as txn:
            txn.execute("MATCH (n:P {id: 1}) DELETE n")
            assert sorted(txn.execute("MATCH (n:P) RETURN n.id").rows) == [(2,)]
    finally:
        handle.close()
    assert [row[0] for row in _live(path)] == [2]


def test_a_set_that_moves_a_statement_created_row_onto_a_taken_key_is_refused(path: str) -> None:
    """The key check on the rewrite of a held insert, pinned on its own (round-3 survivor R01).

    MERGE creates row 1 in this statement; the SET in the same statement moves it onto key 5,
    which another row already holds. Without the check inside ``_rewrite_held_insert`` two live
    rows commit under primary key 5.
    """
    _write(path, "CREATE (:P {id: 5, a: 0})")
    handle = okto_grafx.connect(path)
    try:
        with handle.begin("write") as txn:
            with pytest.raises(GrafxQueryError) as refusal:
                txn.execute("MERGE (a:P {id: 1}) SET a.id = 5")
            assert refusal.value.details["constraint"] == "primary_key"
    finally:
        handle.close()
    assert [row[0] for row in _live(path)] == [5]


# --- round 3, B1: a refused statement leaves NOTHING staged, on every shape ---------------------


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("MERGE (n:P {id: 9}) DELETE n", [1, 2]),
        ("CREATE (n:P {id: 9}) DELETE n", [1, 2]),
        ("MATCH (m:P {id: 2}) MERGE (n:P {id: 9}) DELETE m, n", [1]),
    ],
    ids=["merge-delete", "create-delete", "match-merge-delete-both"],
)
def test_deleting_a_row_the_statement_created_creates_nothing_and_loses_nothing(
    path: str, statement: str, expected: list[int]
) -> None:
    """Creating a row and deleting it in one statement is creating nothing.

    The delete of a statement-created row used to reach the transaction's door with no
    reference and be refused THERE, after the statement's other rows were already staged. A
    commit after the refusal made the rest durable: a refused DELETE that deleted row 2, a refused
    MERGE that created row 9. Now the held insert is simply taken back, the statement succeeds,
    and what it says happened is what happened.
    """
    _write(path, "CREATE (:P {id: 1, a: 0})", "CREATE (:P {id: 2, a: 0})")
    _write(path, statement)
    assert [row[0] for row in _live(path)] == expected


def test_one_statement_deletes_a_stored_row_and_a_pending_row_atomically(
    path: str,
) -> None:
    """A pending row now has the private identity DELETE needs to take it back.

    The stored delete and the pending delete are still handed over under one staging mark. At
    commit the shared reducer keeps the former and cancels the earlier insert, so the statement
    publishes all of its outcome rather than preserving the historical unsupported-operation
    seam this milestone closes.
    """
    _write(path, "CREATE (:P {id: 1, a: 0})", "CREATE (:P {id: 2, a: 0})")
    handle = okto_grafx.connect(path)
    try:
        with handle.begin("write") as txn:
            txn.execute("CREATE (:P {id: 9, a: 0})")
            txn.execute("MATCH (m:P {id: 2}) MERGE (n:P {id: 9}) DELETE m, n")
    finally:
        handle.close()
    assert [row[0] for row in _live(path)] == [1]


def test_a_merge_matched_row_this_transaction_updated_can_be_set_and_deleted(path: str) -> None:
    """A row the transaction already updated comes back from MERGE WITH its reference."""
    _write(path, "CREATE (:P {id: 1, a: 0})")
    _write(path, "MATCH (n:P {id: 1}) SET n.a = 5", "MERGE (n:P {id: 1}) SET n.b = 7")
    assert _live(path) == [(1, 5, 7, None)]
    _write(path, "MATCH (n:P {id: 1}) SET n.a = 6", "MERGE (n:P {id: 1}) DELETE n")
    assert _live(path) == []


# --- round 3, B2: the held insert is identified by the binding, never by its values --------------


def test_two_created_rows_with_identical_values_are_set_independently(tmp_path: Path) -> None:
    """A node table without a primary key can hold two identical rows; SET must hit the right one."""
    root = str(tmp_path / "nopk")
    handle = okto_grafx.connect(root)
    try:
        with handle.begin("write") as txn:
            txn.execute("CREATE NODE TABLE T(x INT64, y INT64)")
        with handle.begin("write") as txn:
            txn.execute("CREATE (a:T {x: 1}), (b:T {x: 1}) SET a.x = 2 SET a.y = 5")
        rows = handle.execute("MATCH (t:T) RETURN t.x, t.y").rows
        assert Counter(rows) == Counter([(1, None), (2, 5)])
        with handle.begin("write") as txn:
            txn.execute("CREATE (a:T {x: 8}), (b:T {x: 8}) SET a.y = 5 SET a.y = 7")
        rows = handle.execute("MATCH (t:T) WHERE t.x = 8 RETURN t.x, t.y").rows
        assert Counter(rows) == Counter([(8, None), (8, 7)])
    finally:
        handle.close()
