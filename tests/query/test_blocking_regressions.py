"""The three blocking defects, each proved on the device after a cold reopen.

Every test here failed against the build that was rejected. They are grouped by the two roots the
defects share rather than by symptom, because that is what the fixes were:

* **what already exists includes what this transaction has not committed** -- a MERGE that only
  asked the heap was blind to rows the same transaction, and even the same statement, had staged;
* **a write is not a stream a window may truncate** -- every operator is a lazy generator, so a
  LIMIT above a write stopped the write too.

Two properties of the method matter as much as the assertions. The database is on a real
directory and every claim about what was written is read through a **cold reopen**, because
reading back through the connection that did the writing can be answered from a buffer pool that
was never asked to prove anything. And nothing here uses a double: the transactions are real, the
commits are real, and the rows are counted on the device.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxPlanError

SCHEMA: tuple[str, ...] = (
    "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))",
    "CREATE NODE TABLE Copy(id INT64, PRIMARY KEY(id))",
    "CREATE REL TABLE Knows(FROM Person TO Person, since INT64)",
)

EDGE_PATTERN: str = (
    "MATCH (a:Person), (b:Person) WHERE a.id = 1 AND b.id = 2 MERGE (a)-[:Knows]->(b)"
)


def build(directory: object) -> object:
    """Return a database on a real directory with the schema installed and committed."""
    handle = okto_grafx.connect(str(directory))
    with handle.begin("write") as txn:
        for statement in SCHEMA:
            txn.execute(statement)
    return handle


def reopened(directory: object, table: str) -> list[tuple[object, ...]]:
    """Open the database fresh from its directory and return every row of one table."""
    handle = okto_grafx.connect(str(directory))
    try:
        definition = handle.catalog.catalog.table(table)
        return [version.values for _ref, version in handle.heap.scan_all(definition)]
    finally:
        handle.close()


def two_people(handle: object) -> None:
    """Commit the two rows an edge test connects."""
    with handle.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'A'})")
        txn.execute("CREATE (:Person {id: 2, name: 'B'})")


@pytest.fixture
def database(tmp_path: object) -> Iterator[object]:
    """Return an open database on a real directory, and the directory it lives in."""
    handle = build(tmp_path / "db")
    try:
        yield handle
    finally:
        handle.close()


# --- root one: uncommitted work is part of what exists -----------------------------------------


def test_two_merges_of_one_row_in_one_transaction_leave_one_row(tmp_path: object) -> None:
    handle = build(tmp_path / "db")
    try:
        with handle.begin("write") as txn:
            txn.execute("MERGE (p:Person {id: 1, name: 'Ada'})")
            txn.execute("MERGE (p:Person {id: 1, name: 'Ada'})")
    finally:
        handle.close()
    assert reopened(tmp_path / "db", "Person") == [(1, "Ada")]


def test_a_merge_driven_by_many_rows_creates_exactly_once(tmp_path: object) -> None:
    handle = build(tmp_path / "db")
    try:
        with handle.begin("write") as txn:
            for identity in (1, 2, 3):
                txn.execute(
                    "CREATE (:Person {id: $id, name: 'p'})", {"id": identity}
                )
        with handle.begin("write") as txn:
            report = txn.execute("MATCH (p:Person) MERGE (g:Copy {id: 9})")
        assert report.statistics["rows_created"] == 1
        assert report.statistics["rows_matched"] == 2
    finally:
        handle.close()
    assert reopened(tmp_path / "db", "Copy") == [(9,)]


def test_a_merge_matches_a_row_an_earlier_statement_of_the_same_transaction_created(
    tmp_path: object,
) -> None:
    handle = build(tmp_path / "db")
    try:
        with handle.begin("write") as txn:
            txn.execute("CREATE (:Person {id: 1, name: 'Ada'})")
            report = txn.execute("MERGE (p:Person {id: 1, name: 'Ada'})")
        assert "rows_created" not in report.statistics
    finally:
        handle.close()
    assert reopened(tmp_path / "db", "Person") == [(1, "Ada")]


def test_a_relationship_merge_run_three_times_leaves_one_edge(tmp_path: object) -> None:
    handle = build(tmp_path / "db")
    reports = []
    try:
        two_people(handle)
        for _ in range(3):
            with handle.begin("write") as txn:
                reports.append(txn.execute(EDGE_PATTERN).statistics)
    finally:
        handle.close()
    assert reports[0]["relationships_created"] == 1
    assert reports[1]["relationships_matched"] == 1
    assert reports[2]["relationships_matched"] == 1
    assert reopened(tmp_path / "db", "Knows") == [(1, 2, None)]


def test_two_relationship_merges_in_one_transaction_leave_one_edge(tmp_path: object) -> None:
    handle = build(tmp_path / "db")
    try:
        two_people(handle)
        with handle.begin("write") as txn:
            txn.execute(EDGE_PATTERN)
            txn.execute(EDGE_PATTERN)
    finally:
        handle.close()
    assert len(reopened(tmp_path / "db", "Knows")) == 1


def test_a_relationship_merge_reports_the_outcome_it_reached(database: object) -> None:
    # A MERGE is contractually required to say which way it went, and there was no
    # relationships_matched statistic at all before this.
    two_people(database)
    with database.begin("write") as txn:
        first = txn.execute(EDGE_PATTERN).statistics
    with database.begin("write") as txn:
        second = txn.execute(EDGE_PATTERN).statistics
    assert first["relationships_created"] == 1
    assert "relationships_matched" not in first
    assert second["relationships_matched"] == 1
    assert "relationships_created" not in second


def test_a_relationship_merge_that_names_a_property_matches_only_on_it(
    tmp_path: object,
) -> None:
    # The other side of the rule: matching is over what the pattern NAMED, so an edge differing
    # in a named property is a different edge and is created.
    handle = build(tmp_path / "db")
    prefix = "MATCH (a:Person), (b:Person) WHERE a.id = 1 AND b.id = 2 MERGE (a)-[:Knows "
    try:
        two_people(handle)
        with handle.begin("write") as txn:
            txn.execute(prefix + "{since: 7}]->(b)")
            txn.execute(prefix + "{since: 8}]->(b)")
            txn.execute(prefix + "{since: 7}]->(b)")
    finally:
        handle.close()
    assert sorted(reopened(tmp_path / "db", "Knows")) == [(1, 2, 7), (1, 2, 8)]


def test_an_edge_between_a_different_pair_is_a_different_edge(tmp_path: object) -> None:
    handle = build(tmp_path / "db")
    try:
        two_people(handle)
        with handle.begin("write") as txn:
            txn.execute(EDGE_PATTERN)
            txn.execute(
                "MATCH (a:Person), (b:Person) WHERE a.id = 2 AND b.id = 1 "
                "MERGE (a)-[:Knows]->(b)"
            )
    finally:
        handle.close()
    assert sorted(reopened(tmp_path / "db", "Knows")) == [(1, 2, None), (2, 1, None)]


# --- root two: a window never decides how much was written -------------------------------------


WINDOWS: tuple[str, ...] = (
    "",
    "LIMIT 0",
    "LIMIT 1",
    "LIMIT 2",
    "SKIP 1 LIMIT 1",
    "ORDER BY p.id LIMIT 1",
    "SKIP 4",
)


@pytest.mark.parametrize("window", WINDOWS)
def test_a_window_never_decides_how_many_rows_a_statement_writes(
    tmp_path: object, window: str
) -> None:
    directory = tmp_path / "db"
    handle = build(directory)
    try:
        with handle.begin("write") as txn:
            for identity in range(1, 6):
                txn.execute("CREATE (:Person {id: $id, name: 'p'})", {"id": identity})
        with handle.begin("write") as txn:
            statement = "MATCH (p:Person) CREATE (:Copy {id: p.id}) RETURN p.id"
            txn.execute((statement + " " + window).strip())
    finally:
        handle.close()
    assert len(reopened(directory, "Copy")) == 5


def test_a_window_still_truncates_what_the_caller_receives(tmp_path: object) -> None:
    # The barrier must not have turned LIMIT into a no-op: five written, two returned.
    directory = tmp_path / "db"
    handle = build(directory)
    try:
        with handle.begin("write") as txn:
            for identity in range(1, 6):
                txn.execute("CREATE (:Person {id: $id, name: 'p'})", {"id": identity})
        with handle.begin("write") as txn:
            found = txn.execute(
                "MATCH (p:Person) CREATE (:Copy {id: p.id}) RETURN p.id ORDER BY p.id LIMIT 2"
            )
        assert found.rows == ((1,), (2,))
    finally:
        handle.close()
    assert len(reopened(directory, "Copy")) == 5


def test_the_barrier_is_an_operator_rather_than_a_habit_of_the_executor(
    database: object,
) -> None:
    plan = database.queries.explain("MATCH (p:Person) CREATE (:Copy {id: p.id}) RETURN p.id")
    labels = [node.label for node in plan.walk()]
    assert "EagerRows" in labels
    assert labels.index("EagerRows") < labels.index("CreateRelationships")


def test_a_query_that_only_reads_carries_no_barrier(database: object) -> None:
    plan = database.queries.explain("MATCH (p:Person) RETURN p.id LIMIT 1")
    assert "EagerRows" not in [node.label for node in plan.walk()]


# --- the window guard the planner cannot reach -------------------------------------------------


@pytest.mark.parametrize("keyword", ["SKIP", "LIMIT"])
def test_a_negative_window_supplied_as_a_parameter_is_refused(
    database: object, keyword: str
) -> None:
    # The planner's check reads LITERALS, so a parameter reaches the executor unexamined. Without
    # the executor's own refusal, LIMIT -1 silently returns nothing and SKIP -1 silently returns
    # everything: two different wrong answers from one missing guard.
    with pytest.raises(GrafxPlanError) as failure:
        database.execute(f"MATCH (p:Person) RETURN p.id {keyword} $n", {"n": -1})
    assert failure.value.details["field"] == keyword.lower()


@pytest.mark.parametrize("keyword", ["SKIP", "LIMIT"])
def test_a_window_parameter_that_is_not_a_count_is_refused(
    database: object, keyword: str
) -> None:
    with pytest.raises(GrafxPlanError) as failure:
        database.execute(f"MATCH (p:Person) RETURN p.id {keyword} $n", {"n": "two"})
    assert failure.value.details["field"] == keyword.lower()


@pytest.mark.parametrize(("keyword", "expected"), [("SKIP", 2), ("LIMIT", 1)])
def test_a_window_supplied_as_a_parameter_still_works(
    database: object, keyword: str, expected: int
) -> None:
    # The refusal must not have made every parameter window fail.
    with database.begin("write") as txn:
        for identity in (1, 2, 3):
            txn.execute("CREATE (:Person {id: $id, name: 'p'})", {"id": identity})
    found = database.execute(
        f"MATCH (p:Person) RETURN p.id ORDER BY p.id {keyword} $n", {"n": 1}
    )
    assert len(found.rows) == expected
