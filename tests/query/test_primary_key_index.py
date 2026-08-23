"""The index covering a declared PRIMARY KEY: that it exists, that it is used, and that a stale
one is never used.

WHY THIS FILE EXISTS. The engine shipped a complete index framework and no index. ``HashIndex``,
the EXACT contract of CONTRACT.md section 8.7, ``IndexSeek`` in the planner, ``_index_definitions``
feeding the planner -- all built, all tested, and ``CREATE NODE TABLE Person(id INT64, PRIMARY
KEY(id))`` registered nothing, so ``_index_for`` searched an empty list and every ``WHERE id = $k``
planned a full heap scan. Measured before this existed, a point read cost 5.97 ms over 200 rows,
25.4 ms over 800 and 57.4 ms over 3200 -- linear in the table, against a D5 ceiling of five times a
reference engine's ~0.9 ms. The uniqueness check on every insert scanned the same way, which made a
bulk load quadratic, and a two-pattern MATCH multiplied two scans: 1.95 s to create one edge in a
1600-row table.

The tests here assert the BEHAVIOUR, not the timings -- a test that asserts a duration fails on a
loaded machine and proves nothing on a fast one (D5 measurements live in COMPONENTS.md). What they
hold is that the index is created, that it is the plan the engine actually chooses, that it comes
back on the next open, and the correctness rule that makes all of it safe: a STALE index is never
planned, because a stale index is a subset of the heap and a subset is what the EXACT contract
cannot repair.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxQueryError
from okto_grafx.domain.query.plan import IndexSeek, NodeScan
from okto_grafx.engine.index_manager import primary_key_index_name


@pytest.fixture()
def database(tmp_path: Path):
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    try:
        yield handle
    finally:
        handle.close()


def _plan_operators(handle, statement: str, parameters=None) -> set[str]:
    """Return the operator types the planner chose for this statement.

    Through ``explain``, which is the door AC-7 gives a caller for exactly this and which builds
    the plan the query would actually run.
    """
    seen: set[str] = set()
    stack = [handle.queries.explain(statement)]
    while stack:
        node = stack.pop()
        seen.add(type(node).__name__)
        children = getattr(node, "children", None)
        stack.extend(children() if callable(children) else (children or ()))
    return seen


# --- it exists ----------------------------------------------------------------------------------


def test_declaring_a_primary_key_creates_the_index_that_covers_it(database) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")

    names = [index.name for index in database.indexes.indexes()]
    assert names == [primary_key_index_name("Person")]

    definition = database.indexes.index("pk_Person").definition
    table = database.catalog.catalog.table("Person")
    assert definition.table_id == table.table_id
    assert definition.positions == (table.column_index("id"),)
    assert definition.visibility.value == "exact"
    assert database.stale_indexes == ()


def test_a_relationship_table_declares_no_primary_key_and_gets_no_index(database) -> None:
    """A rel table has endpoints, not a key. Inventing an index for it would index nothing."""
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
        txn.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        txn.execute("CREATE REL TABLE E(FROM A TO B, w INT64)")

    assert [index.name for index in database.indexes.indexes()] == ["pk_A", "pk_B"]


# --- it is what the planner chooses ---------------------------------------------------------------


def test_a_keyed_read_is_planned_as_a_seek_and_an_unkeyed_one_as_a_scan(database) -> None:
    """The assertion is on the PLAN, because a timing assertion proves nothing on either machine.

    Both halves matter. Without the seek the engine is doing what it did before any index existed;
    without the scan on the unkeyed predicate the test would pass for an engine that planned a
    seek it could not answer.
    """
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    with database.begin("write") as txn:
        for identity in range(1, 21):
            txn.execute(
                "CREATE (:Person {id: $i, name: $n})", {"i": identity, "n": f"p{identity}"}
            )

    keyed = _plan_operators(database, "MATCH (p:Person) WHERE p.id = $k RETURN p.name", {"k": 3})
    assert IndexSeek.__name__ in keyed
    assert NodeScan.__name__ not in keyed

    unkeyed = _plan_operators(database, "MATCH (p:Person) WHERE p.name = 'p3' RETURN p.id")
    assert NodeScan.__name__ in unkeyed
    assert IndexSeek.__name__ not in unkeyed


def test_a_seek_and_a_scan_return_the_same_rows(database) -> None:
    """The index is an accelerator, so the two plans must agree on every row -- including one
    the same transaction has just written, which is in no index at all."""
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    with database.begin("write") as txn:
        for identity in range(1, 51):
            txn.execute(
                "CREATE (:Person {id: $i, name: $n})", {"i": identity, "n": f"p{identity}"}
            )

    for identity in (1, 17, 50):
        seek = database.execute(
            "MATCH (p:Person) WHERE p.id = $k RETURN p.name", {"k": identity}
        ).rows
        scan = database.execute(
            "MATCH (p:Person) WHERE p.name = $n RETURN p.name", {"n": f"p{identity}"}
        ).rows
        assert seek == scan == ((f"p{identity}",),)

    assert database.execute(
        "MATCH (p:Person) WHERE p.id = 9999 RETURN p.name"
    ).rows == ()


def test_a_deleted_row_is_not_returned_by_a_seek(database) -> None:
    """An EXACT index is a SUPERSET: the entry outlives the row, and the heap is what settles it.

    This is section 8.7's whole point, and the reason a primary key can be indexed at all without
    the index having to understand visibility.
    """
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'ada'})")
        txn.execute("CREATE (:Person {id: 2, name: 'grace'})")
    with database.begin("write") as txn:
        txn.execute("MATCH (p:Person) WHERE p.id = 1 DELETE p")

    assert database.execute("MATCH (p:Person) WHERE p.id = 1 RETURN p.name").rows == ()
    assert database.execute("MATCH (p:Person) WHERE p.id = 2 RETURN p.name").rows == (
        ("grace",),
    )


def test_an_updated_row_is_found_under_its_new_key_and_not_its_old_one(database) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'ada'})")
    with database.begin("write") as txn:
        txn.execute("MATCH (p:Person) WHERE p.id = 1 SET p.id = 7")

    assert database.execute("MATCH (p:Person) WHERE p.id = 1 RETURN p.name").rows == ()
    assert database.execute("MATCH (p:Person) WHERE p.id = 7 RETURN p.name").rows == (("ada",),)


# --- the uniqueness check reads it too ------------------------------------------------------------


def test_the_duplicate_key_refusal_survives_the_index_answering_it(database) -> None:
    """The check that refuses a duplicate now asks the index instead of scanning the table.

    It is the same question -- "is there a live row of this table, visible to me, under this key?"
    -- so the refusal must be unchanged. A test that only measured the speed-up would pass for an
    implementation that stopped refusing.
    """
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'ada'})")

    with pytest.raises(GrafxQueryError) as refused:
        with database.begin("write") as txn:
            txn.execute("CREATE (:Person {id: 1, name: 'other'})")
    assert refused.value.details.get("field") == "primary_key"

    # Two rows under one key inside ONE transaction, which the index cannot see because nothing
    # is committed yet: the transaction's own staged view is what refuses this one.
    with pytest.raises(GrafxQueryError):
        with database.begin("write") as txn:
            txn.execute("CREATE (:Person {id: 5, name: 'a'})")
            txn.execute("CREATE (:Person {id: 5, name: 'b'})")

    # And a key freed by a delete is available again.
    with database.begin("write") as txn:
        txn.execute("MATCH (p:Person) WHERE p.id = 1 DELETE p")
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'reused'})")
    assert database.execute("MATCH (p:Person) WHERE p.id = 1 RETURN p.name").rows == (
        ("reused",),
    )


# --- it survives the process --------------------------------------------------------------------


def test_the_index_comes_back_on_the_next_open_and_is_still_fresh(tmp_path: Path) -> None:
    """Created by the DDL, re-adopted by the composition, and fresh across both.

    The freshness half is not decoration. An index went stale the moment any commit happened
    after it was created and before it received its first entry, and `_advance` refuses to move a
    stale index -- so "create the schema in one session, load the data in the next" left an index
    that no amount of loading could ever lift, and the planner (correctly) refused to use it for
    ever. Reproduced on the vector index before a primary key was ever indexed.
    """
    root = tmp_path / "db"
    with okto_grafx.connect(root, page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
        assert db.stale_indexes == ()

    with okto_grafx.connect(root, page_size=512) as reopened:
        assert reopened.attached_indexes == ("pk_Person",)
        assert reopened.stale_indexes == (), "an index of a table nobody wrote went stale"
        with reopened.begin("write") as txn:
            txn.execute("CREATE (:Person {id: 1, name: 'ada'})")
        assert reopened.stale_indexes == ()

    with okto_grafx.connect(root, page_size=512) as third:
        assert third.stale_indexes == ()
        assert IndexSeek.__name__ in _plan_operators(
            third, "MATCH (p:Person) WHERE p.id = $k RETURN p.name", {"k": 1}
        )
        assert third.execute(
            "MATCH (p:Person) WHERE p.id = 1 RETURN p.name"
        ).rows == (("ada",),)
        assert third.verify("all").findings == ()


# --- the correctness rule that makes all of it safe -----------------------------------------------


def test_a_stale_index_is_never_planned_and_the_answer_stays_right(database) -> None:
    """A STALE index is withheld from the planner, and the query falls back to the scan.

    This is a correctness rule, not a policy. A stale index is a SUBSET of what the heap holds --
    entries it never received -- and being a subset is exactly what the EXACT contract cannot
    repair: validating a candidate against the heap removes hits that should not be there and
    cannot invent ones that are missing. A plan built on one answers a keyed read with fewer rows
    than exist, which is the wrong-result outcome section 14.1 names.
    """
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    with database.begin("write") as txn:
        for identity in range(1, 21):
            txn.execute(
                "CREATE (:Person {id: $i, name: $n})", {"i": identity, "n": f"p{identity}"}
            )
    assert IndexSeek.__name__ in _plan_operators(
        database, "MATCH (p:Person) WHERE p.id = $k RETURN p.name", {"k": 3}
    )

    index = database.indexes.index("pk_Person")
    index.mark_stale("forced by a test")
    # The index's own flag, not `Database.stale_indexes`: that attribute is the verdict taken at
    # open and does not move afterwards, so asserting it here would assert nothing about now.
    assert index.stale

    operators = _plan_operators(
        database, "MATCH (p:Person) WHERE p.id = $k RETURN p.name", {"k": 3}
    )
    assert IndexSeek.__name__ not in operators, "a stale index was planned"
    assert NodeScan.__name__ in operators

    # And the answer is still right, which is the whole reason the fallback exists.
    for identity in (1, 11, 20):
        assert database.execute(
            "MATCH (p:Person) WHERE p.id = $k RETURN p.name", {"k": identity}
        ).rows == ((f"p{identity}",),)
    assert database.execute("MATCH (p:Person) WHERE p.id = 99 RETURN p.name").rows == ()


def test_a_stale_index_does_not_let_a_duplicate_key_through(database) -> None:
    """The uniqueness check falls back for the same reason, and this is the harm if it did not.

    A missing entry would make the check answer "no row holds this key" for a key a row does hold,
    and the duplicate would be accepted -- data an ordinary caller reaches, under a declared
    primary key.
    """
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'ada'})")

    database.indexes.index("pk_Person").mark_stale("forced by a test")

    with pytest.raises(GrafxQueryError) as refused:
        with database.begin("write") as txn:
            txn.execute("CREATE (:Person {id: 1, name: 'other'})")
    assert refused.value.details.get("field") == "primary_key"
    assert database.execute("MATCH (p:Person) RETURN count(*)").rows == ((1,),)
