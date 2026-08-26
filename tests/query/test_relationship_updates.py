"""Updates of properties on already-committed relationship rows.

These regressions use the public database and transaction doors.  A relationship update must
replace exactly the matched edge, keep the layout-owned endpoints byte-for-byte, participate in
optimistic conflict detection, and be visible only in its owner's transaction until commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.errors import GrafxPlanError, GrafxWriteConflict


@pytest.fixture
def graph_path(tmp_path: Path) -> Path:
    """Create the durable schema shared by one relationship-update regression."""
    root = tmp_path / "graph"
    with okto_grafx.connect(root) as database:
        with database.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
            schema.execute(
                "CREATE REL TABLE Knows(FROM Person TO Person, since INT64, note STRING)"
            )
    return root


def _seed(database: object, *relationships: tuple[int, int, int, str]) -> None:
    """Commit three endpoints and the requested relationship rows."""
    with database.begin("write") as transaction:
        for identity, name in ((1, "Ada"), (2, "Grace"), (3, "Alan")):
            transaction.execute(
                f"CREATE (:Person {{id: {identity}, name: '{name}'}})"
            )
    with database.begin("write") as transaction:
        for source, target, since, note in relationships:
            transaction.execute(
                f"MATCH (a:Person {{id: {source}}}), (b:Person {{id: {target}}}) "
                f"CREATE (a)-[:Knows {{since: {since}, note: '{note}'}}]->(b)"
            )


def _relationships(database: object) -> tuple[tuple[object, ...], ...]:
    """Return the public relationship picture in a deterministic order."""
    return database.execute(
        "MATCH (a:Person)-[r:Knows]->(b:Person) "
        "RETURN a.id, b.id, r.since, r.note ORDER BY r.since"
    ).rows


def test_relationship_update_is_owner_visible_and_survives_cold_reopen(
    graph_path: Path,
) -> None:
    with okto_grafx.connect(graph_path) as database:
        _seed(database, (1, 2, 2020, "original"))

        writer = database.begin("write")
        changed = writer.execute(
            "MATCH (a:Person {id: 1})-[r:Knows]->(b:Person {id: 2}) "
            "SET r.since = 2024, r.note = 'owner'"
        )

        assert changed.statistics["rows_updated"] == 1
        assert changed.statistics["properties_set"] == 2
        assert writer.execute(
            "MATCH (a:Person)-[r:Knows]->(b:Person) "
            "RETURN a.id, b.id, r.since, r.note"
        ).rows == ((1, 2, 2024, "owner"),)
        # A separate transaction still reads the committed snapshot.
        assert _relationships(database) == ((1, 2, 2020, "original"),)
        writer.commit()

    with okto_grafx.connect(graph_path) as reopened:
        assert _relationships(reopened) == ((1, 2, 2024, "owner"),)
        assert reopened.verify("all").findings == ()


def test_parallel_relationships_update_only_the_matched_row(graph_path: Path) -> None:
    with okto_grafx.connect(graph_path) as database:
        _seed(
            database,
            (1, 2, 2020, "first"),
            (1, 2, 2021, "second"),
            (1, 3, 2022, "third"),
        )

        with database.begin("write") as writer:
            changed = writer.execute(
                "MATCH (a:Person)-[r:Knows]->(b:Person) "
                "WHERE a.id = 1 AND b.id = 2 AND r.since = 2020 "
                "SET r.since = 2030, r.note = 'changed'"
            )
            assert changed.statistics["rows_updated"] == 1
            assert _relationships(writer) == (
                (1, 2, 2021, "second"),
                (1, 3, 2022, "third"),
                (1, 2, 2030, "changed"),
            )

        assert _relationships(database) == (
            (1, 2, 2021, "second"),
            (1, 3, 2022, "third"),
            (1, 2, 2030, "changed"),
        )


def test_rolled_back_relationship_update_leaves_endpoints_and_properties_unchanged(
    graph_path: Path,
) -> None:
    with okto_grafx.connect(graph_path) as database:
        _seed(database, (1, 2, 2020, "original"))

        writer = database.begin("write")
        writer.execute(
            "MATCH (a:Person)-[r:Knows]->(b:Person) "
            "SET r.since = 2099, r.note = 'doomed'"
        )
        assert _relationships(writer) == ((1, 2, 2099, "doomed"),)
        writer.rollback()

        assert _relationships(database) == ((1, 2, 2020, "original"),)

def test_conflicting_relationship_update_publishes_only_the_winner(
    graph_path: Path,
) -> None:
    with okto_grafx.connect(graph_path) as database:
        _seed(database, (1, 2, 2020, "original"))

        loser = database.begin("write")
        winner = database.begin("write")
        loser.execute(
            "MATCH (a:Person)-[r:Knows]->(b:Person) "
            "SET r.since = 2030, r.note = 'loser'"
        )
        winner.execute(
            "MATCH (a:Person)-[r:Knows]->(b:Person) "
            "SET r.since = 2040, r.note = 'winner'"
        )
        winner.commit()

        with pytest.raises(GrafxWriteConflict):
            loser.commit()
        loser.rollback()

        assert _relationships(database) == ((1, 2, 2040, "winner"),)
        assert database.verify("all").findings == ()


@pytest.mark.parametrize("endpoint", ("_from", "_to"))
def test_set_refuses_layout_owned_relationship_endpoints(
    graph_path: Path, endpoint: str
) -> None:
    with okto_grafx.connect(graph_path) as database:
        _seed(database, (1, 2, 2020, "original"))

        writer = database.begin("write")
        committed = False
        try:
            with pytest.raises(GrafxPlanError) as raised:
                writer.execute(
                    "MATCH (a:Person)-[r:Knows]->(b:Person) "
                    f"SET r.{endpoint} = 99"
                )

            assert raised.value.details == {
                "field": "column",
                "value": endpoint,
                "table": "Knows",
            }
            assert _relationships(writer) == ((1, 2, 2020, "original"),)
            writer.commit()
            committed = True
        finally:
            if not committed:
                writer.rollback()

        assert _relationships(database) == ((1, 2, 2020, "original"),)
