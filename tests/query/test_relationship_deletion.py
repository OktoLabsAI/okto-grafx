"""Destructive semantics for relationships: DELETE r, and what DETACH changes about DELETE.

Written against the real composition root, like ``test_lifecycle``, because the claim here is
about what SURVIVES a commit and a reopen -- a double that records intents cannot answer that.

THE ORACLE, and the two that were retired to reach it. ``MATCH (a:Person)-[r:Knows]->(b:Person)``
cannot see the difference this file is about: the join needs both endpoints, so an edge whose
source is gone drops out of the result whether it was ENDED or merely ABANDONED. ``verify()``
cannot see it either -- under MVCC the ended versions stay on the pages, and a control that
deletes a node with no relationships at all shows the primary-key index keeps its entry too, so
neither counter moves.

What does see it is the heap itself. ``HeapVersion.live`` is a committed birth with no committed
end, so walking ``scan_all`` and keeping the live versions of the relationship table answers
"is this edge still a fact on the pages", which is exactly the question the join cannot ask.
``_live_edges`` below is that walk, and it is deliberately NOT a query: it recomputes the fact
from storage instead of asking the planner and the executor whether they believe they removed
something.

That distinction is the contract, not an implementation detail. A plain DELETE of a node ends the
NODE and leaves its edges standing as rows no traversal will follow -- a landing whose snapshot
cannot see the node is not reached, so the absence it promises is a logical one. DETACH DELETE
promises the stronger thing: the edges end too, physically, in the same commit. Both are tested
here, and ``_live_edges`` is what tells them apart.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import okto_grafx
from okto_grafx.errors import GrafxWriteConflict

SCHEMA: tuple[str, ...] = (
    "CREATE NODE TABLE Person(id INT64, tag INT64, PRIMARY KEY(id))",
    "CREATE REL TABLE Knows(FROM Person TO Person, since INT64)",
)

# A relationship between two DIFFERENT node tables, which is the only shape in which the two
# sides of an edge can be told apart: with one table on both ends every wrong answer about
# which side a number belongs to happens to coincide with the right one.
WORKPLACE: tuple[str, ...] = (
    "CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))",
    "CREATE NODE TABLE Company(id INT64, PRIMARY KEY(id))",
    "CREATE REL TABLE WorksAt(FROM Person TO Company, since INT64)",
)


@pytest.fixture
def database() -> Iterator[object]:
    """Return an open in-memory database with the schema installed and committed."""
    handle = okto_grafx.connect(":memory:")
    try:
        with handle.begin("write") as schema:
            for statement in SCHEMA:
                schema.execute(statement)
        yield handle
    finally:
        handle.close()


@pytest.fixture
def workplace() -> Iterator[object]:
    """Two people and two companies numbered alike, and one edge from person 2 to company 1.

    Record numbers are handed out per table, so this arrangement puts person number 1 and
    company number 2 each in collision with the OPPOSITE end of the stored pair (2, 1).
    """
    handle = okto_grafx.connect(":memory:")
    try:
        with handle.begin("write") as schema:
            for statement in WORKPLACE:
                schema.execute(statement)
        with handle.begin("write") as transaction:
            for identity in (1, 2):
                transaction.execute(f"CREATE (:Person {{id: {identity}}})")
                transaction.execute(f"CREATE (:Company {{id: {identity}}})")
        with handle.begin("write") as transaction:
            transaction.execute(
                "MATCH (p:Person), (c:Company) WHERE p.id = 2 AND c.id = 1 "
                "CREATE (p)-[:WorksAt {since: 2020}]->(c)"
            )
        yield handle
    finally:
        handle.close()


def _people(database: object, *identities: int) -> None:
    """Commit one node per identity."""
    with database.begin("write") as transaction:
        for identity in identities:
            transaction.execute(f"CREATE (:Person {{id: {identity}, tag: 0}})")


def _knows(database: object, source: int, target: int, since: int = 2020) -> None:
    """Commit one edge between two existing people."""
    with database.begin("write") as transaction:
        transaction.execute(
            f"MATCH (a:Person), (b:Person) WHERE a.id = {source} AND b.id = {target} "
            f"CREATE (a)-[:Knows {{since: {since}}}]->(b)"
        )


def _live_edges(database: object) -> tuple[tuple[int, int], ...]:
    """Return the endpoints of every relationship version still LIVE on the pages, ordered.

    A stored relationship leads with its two endpoints (W5c), so ``values[0]`` and ``values[1]``
    are the RECORD NUMBERS of its source and target. They are mapped back to the ``id`` property
    through every stored Person version -- including ended ones, so an edge whose endpoint was
    deleted is still reported under a readable name instead of a bare record number.
    """
    catalog = database.catalog.catalog
    identity_of = {
        version.record_id: version.values[0]
        for _ref, version in database._heap.scan_all(catalog.table("Person"))
    }
    return tuple(
        sorted(
            (identity_of[version.values[0]], identity_of[version.values[1]])
            for _ref, version in database._heap.scan_all(catalog.table("Knows"))
            if version.live
        )
    )


def _live_employments(database: object) -> tuple[tuple[int, int], ...]:
    """Return the (person number, company number) pairs still live in WorksAt.

    Reported as raw record numbers on purpose: the claim this serves is about WHICH SIDE a
    number sits on, and translating them back to identities would hide exactly that.
    """
    definition = database.catalog.catalog.table("WorksAt")
    return tuple(
        sorted(
            (version.values[0], version.values[1])
            for _ref, version in database._heap.scan_all(definition)
            if version.live
        )
    )


def _nodes(database: object) -> tuple[int, ...]:
    """Return the surviving identities, ordered."""
    return tuple(
        sorted(row[0] for row in database.execute("MATCH (p:Person) RETURN p.id").rows)
    )


def _record_number(database: object, table: str, identity: int) -> int:
    """Return the record number the named table gave the row carrying that identity."""
    definition = database.catalog.catalog.table(table)
    numbers = [
        version.record_id
        for _ref, version in database._heap.scan_all(definition)
        if version.values[0] == identity
    ]
    assert len(numbers) == 1, numbers
    return numbers[0]


def test_a_matched_relationship_can_be_deleted_on_its_own(database: object) -> None:
    """DELETE r ends the edge and leaves both endpoints standing."""
    _people(database, 1, 2, 3)
    _knows(database, 1, 2)
    _knows(database, 2, 3)
    assert _live_edges(database) == ((1, 2), (2, 3))

    with database.begin("write") as transaction:
        statistics = transaction.execute(
            "MATCH (a:Person)-[r:Knows]->(b:Person) WHERE a.id = 1 DELETE r"
        ).statistics

    # One row ended, not "at least one": a second end on the same version would be a second fact.
    assert statistics["rows_deleted"] == 1
    assert _live_edges(database) == ((2, 3),)
    assert _nodes(database) == (1, 2, 3)


def test_a_plain_delete_ends_the_node_and_leaves_its_relationships_standing(
    database: object,
) -> None:
    """Without DETACH the absence is logical: the node goes, the edge stays on the pages.

    This is the semantics the engine already had and that DETACH exists to strengthen. It is
    asserted rather than assumed, because a cascade sneaking into the plain path would be
    invisible to every query -- the join drops the edge either way.
    """
    _people(database, 1, 2)
    _knows(database, 1, 2)

    with database.begin("write") as transaction:
        statistics = transaction.execute(
            "MATCH (p:Person) WHERE p.id = 1 DELETE p"
        ).statistics

    assert statistics["rows_deleted"] == 1
    assert _nodes(database) == (2,)
    # Unreachable through the traversal...
    assert database.execute("MATCH (a:Person)-[r:Knows]->(b:Person) RETURN a.id").rows == ()
    # ...but still a live row, which is precisely what DETACH would have changed.
    assert _live_edges(database) == ((1, 2),)
    assert database.verify("all").findings == ()


def test_detach_delete_ends_the_incident_edges_and_the_node(database: object) -> None:
    """DETACH DELETE removes the node and every edge touching it, in one commit."""
    _people(database, 1, 2, 3, 4)
    _knows(database, 1, 2)
    _knows(database, 3, 4)
    assert _live_edges(database) == ((1, 2), (3, 4))

    with database.begin("write") as transaction:
        statistics = transaction.execute(
            "MATCH (p:Person) WHERE p.id = 1 DETACH DELETE p"
        ).statistics

    assert statistics["rows_deleted"] == 2  # the edge and the node, nothing else
    assert _nodes(database) == (2, 3, 4)
    # The unrelated edge must survive: detach ends the incident ones, not every one.
    assert _live_edges(database) == ((3, 4),)


def test_detach_delete_covers_both_directions_and_multiple_edges(database: object) -> None:
    """Incoming, outgoing and several edges at once all end with the node."""
    _people(database, 1, 2, 3, 4)
    _knows(database, 1, 2)  # outgoing from the victim
    _knows(database, 3, 1)  # incoming to the victim
    _knows(database, 1, 4)  # a second outgoing
    assert _live_edges(database) == ((1, 2), (1, 4), (3, 1))

    with database.begin("write") as transaction:
        statistics = transaction.execute(
            "MATCH (p:Person) WHERE p.id = 1 DETACH DELETE p"
        ).statistics

    assert statistics["rows_deleted"] == 4  # three edges and the node
    assert _nodes(database) == (2, 3, 4)
    assert _live_edges(database) == ()


def test_detach_delete_ends_a_self_loop_exactly_once(database: object) -> None:
    """A self-loop is incident on both sides and must still end once.

    The count is the assertion: ending it twice would write a second end at a number the version
    had already stopped at, and would show up here as three rows instead of two.
    """
    _people(database, 1, 2)
    _knows(database, 1, 1)
    assert _live_edges(database) == ((1, 1),)

    with database.begin("write") as transaction:
        statistics = transaction.execute(
            "MATCH (p:Person) WHERE p.id = 1 DETACH DELETE p"
        ).statistics

    assert statistics["rows_deleted"] == 2
    assert _nodes(database) == (2,)
    assert _live_edges(database) == ()


def test_one_edge_between_two_detached_nodes_ends_once(database: object) -> None:
    """A statement that detaches both endpoints must not end their shared edge twice.

    Each variable finds the same edge incident on it, so the count is the whole assertion: three
    rows, not four. A second end would be written at a number the version had already stopped at,
    which is not a stronger statement of the same fact but a second fact that is not true.
    """
    _people(database, 1, 2)
    _knows(database, 1, 2)

    with database.begin("write") as transaction:
        statistics = transaction.execute(
            "MATCH (a:Person), (b:Person) WHERE a.id = 1 AND b.id = 2 DETACH DELETE a, b"
        ).statistics

    assert statistics["rows_deleted"] == 3  # the two nodes and the one edge between them
    assert _nodes(database) == ()
    assert _live_edges(database) == ()


def test_a_node_and_an_edge_that_share_a_record_number_both_end(database: object) -> None:
    """Record numbers are per table, so the same number names two different rows.

    The premise is asserted, not assumed: if a later change made these numbers differ the test
    would still pass while no longer testing anything, which is the failure mode it exists to
    prevent. Deduplicating a statement's ended rows by the bare number collapses these two into
    one and silently leaves the edge alive.
    """
    _people(database, 1, 2)
    _knows(database, 1, 2)
    assert _record_number(database, "Person", 1) == _record_number(database, "Knows", 1) == 1

    with database.begin("write") as transaction:
        statistics = transaction.execute(
            "MATCH (p:Person) WHERE p.id = 1 DETACH DELETE p"
        ).statistics

    assert statistics["rows_deleted"] == 2
    assert _live_edges(database) == ()


def test_an_edge_is_incident_only_on_the_side_its_own_table_names(workplace: object) -> None:
    """A number found on the wrong end of the pair is a different node, not this one.

    The stored edge is (person 2, company 1). Person number 1 and company number 2 each match
    the opposite half of that pair, so an incidence test that merely asks whether the number
    appears among the endpoints ends an edge belonging to someone else. The premises are
    asserted, because if the numbering ever stopped colliding this test would keep passing while
    no longer testing anything.
    """
    assert _live_employments(workplace) == ((2, 1),)
    assert _record_number(workplace, "Person", 1) == 1
    assert _record_number(workplace, "Company", 2) == 2

    with workplace.begin("write") as transaction:
        unemployed = transaction.execute(
            "MATCH (p:Person) WHERE p.id = 1 DETACH DELETE p"
        ).statistics
        empty = transaction.execute(
            "MATCH (c:Company) WHERE c.id = 2 DETACH DELETE c"
        ).statistics

    assert unemployed["rows_deleted"] == 1  # the person alone: nobody employs them
    assert empty["rows_deleted"] == 1  # the company alone: nobody works there
    assert _live_employments(workplace) == ((2, 1),)

    # The control: the node that IS on the edge still takes it down with them.
    with workplace.begin("write") as transaction:
        employed = transaction.execute(
            "MATCH (p:Person) WHERE p.id = 2 DETACH DELETE p"
        ).statistics
    assert employed["rows_deleted"] == 2
    assert _live_employments(workplace) == ()


def test_a_cartesian_that_names_the_victim_again_ends_each_row_once(database: object) -> None:
    """A statement's write runs once per ROW, and three rows naming one node are still one node.

    The scanned count asserts the premise -- the Cartesian really did hand the victim over three
    times -- so the test cannot quietly stop exercising the repeat. Counting the repeats also
    HOLDS them, which is why the budget is the other half of this same claim, below.
    """
    _people(database, 1, 2, 3)
    _knows(database, 1, 2)

    with database.begin("write") as transaction:
        statistics = transaction.execute(
            "MATCH (p:Person), (q:Person) WHERE p.id = 1 DETACH DELETE p"
        ).statistics

    assert statistics["rows_scanned"] == 3
    assert statistics["rows_deleted"] == 2  # one node and one edge, however often they were named
    assert _nodes(database) == (2, 3)
    assert _live_edges(database) == ()


def test_a_repeated_victim_does_not_spend_the_statement_budget_twice() -> None:
    """Two entities need two writes, and a budget of two must therefore accept the statement.

    A repeat that is counted is also a repeat that is HELD, so the statement above used to reach
    its third held write and be refused for exceeding a limit it never actually needed. A budget
    that refuses work nobody asked for is worse than no budget: it is a wrong answer.
    """
    handle = okto_grafx.connect(":memory:", max_statement_writes=2)
    try:
        with handle.begin("write") as schema:
            for statement in SCHEMA:
                schema.execute(statement)
        _people(handle, 1, 2, 3)
        _knows(handle, 1, 2)

        with handle.begin("write") as transaction:
            statistics = transaction.execute(
                "MATCH (p:Person), (q:Person) WHERE p.id = 1 DETACH DELETE p"
            ).statistics

        assert statistics["rows_deleted"] == 2
        assert _live_edges(handle) == ()
    finally:
        handle.close()


def test_an_edge_this_transaction_already_ended_is_not_ended_again(database: object) -> None:
    """Two statements of one transaction read the same snapshot, which sees neither one's work.

    So the detach finds an edge the DELETE before it has already ended, and without consulting
    what the transaction has staged it holds a second end at a number the version had already
    stopped at: three writes for two entities, and the budget charged twice for one of them.
    """
    _people(database, 1, 2)
    _knows(database, 1, 2)

    with database.begin("write") as transaction:
        unlinked = transaction.execute(
            "MATCH (a:Person)-[r:Knows]->(b:Person) WHERE a.id = 1 DELETE r"
        ).statistics
        detached = transaction.execute(
            "MATCH (p:Person) WHERE p.id = 1 DETACH DELETE p"
        ).statistics

    assert unlinked["rows_deleted"] == 1  # the edge
    assert detached["rows_deleted"] == 1  # the node ALONE; its edge was already ended
    assert _nodes(database) == (2,)
    assert _live_edges(database) == ()


def test_a_detach_delete_that_loses_a_conflict_leaves_the_whole_graph_standing(
    database: object,
) -> None:
    """The loser publishes all of its work or none of it, and here it must be none.

    The winner touches only the node, never the edge, so nothing it does can be mistaken for
    something the loser did. Both halves are asserted: a node that survived without its edge, or
    an edge ended without its node, would each be half a statement made durable.
    """
    _people(database, 1, 2)
    _knows(database, 1, 2)

    loser = database.begin("write")
    loser.execute("MATCH (p:Person) WHERE p.id = 1 DETACH DELETE p")

    with database.begin("write") as winner:
        winner.execute("MATCH (p:Person) WHERE p.id = 1 SET p.tag = 7")

    with pytest.raises(GrafxWriteConflict):
        loser.commit()

    assert _nodes(database) == (1, 2)
    assert _live_edges(database) == ((1, 2),)
    # And the winner's value is what stands, so the loser did not partly overwrite it either.
    assert database.execute("MATCH (p:Person) WHERE p.id = 1 RETURN p.tag").rows == ((7,),)
    assert database.verify("all").findings == ()


def test_a_delete_matching_nothing_changes_nothing(database: object) -> None:
    """Zero matches is a successful statement that ended zero rows."""
    _people(database, 1, 2)
    _knows(database, 1, 2)

    with database.begin("write") as transaction:
        detached = transaction.execute(
            "MATCH (p:Person) WHERE p.id = 99 DETACH DELETE p"
        ).statistics
        unlinked = transaction.execute(
            "MATCH (a:Person)-[r:Knows]->(b:Person) WHERE a.id = 99 DELETE r"
        ).statistics

    assert detached.get("rows_deleted", 0) == 0
    assert unlinked.get("rows_deleted", 0) == 0
    assert _nodes(database) == (1, 2)
    assert _live_edges(database) == ((1, 2),)


def test_a_rolled_back_detach_delete_leaves_the_graph_untouched(database: object) -> None:
    """Nothing the statement removed becomes durable when the transaction rolls back."""
    _people(database, 1, 2)
    _knows(database, 1, 2)

    transaction = database.begin("write")
    transaction.execute("MATCH (p:Person) WHERE p.id = 1 DETACH DELETE p")
    transaction.rollback()

    assert _nodes(database) == (1, 2)
    # Live on the pages, not merely visible to a query that could not have told the difference.
    assert _live_edges(database) == ((1, 2),)


def test_the_result_survives_reopen_and_verifies(tmp_path: object) -> None:
    """Close the loop outside the process that wrote it: reopen from disk and look again."""
    root = tmp_path / "graph"
    with okto_grafx.connect(root) as database:
        with database.begin("write") as schema:
            for statement in SCHEMA:
                schema.execute(statement)
        _people(database, 1, 2, 3)
        _knows(database, 1, 2)
        _knows(database, 2, 3)
        with database.begin("write") as transaction:
            transaction.execute("MATCH (p:Person) WHERE p.id = 1 DETACH DELETE p")

    with okto_grafx.connect(root) as reopened:
        assert _nodes(reopened) == (2, 3)
        assert _live_edges(reopened) == ((2, 3),)
        assert reopened.verify("all").findings == ()
