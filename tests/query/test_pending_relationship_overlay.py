"""Frozen public regressions for the M-PULSE-1C relationship overlay.

Every mutation and observation crosses the public query/transaction API.  The contract starts
at statement boundaries: nodes or relationships staged by an earlier statement belong to the
owner's combined view, while a relationship whose endpoints are created by that same statement
remains explicitly unsupported in this milestone.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.errors import GrafxWriteConflict


def _install_schema(database: object) -> None:
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
        )
        schema.execute(
            "CREATE REL TABLE Knows("
            "FROM Person TO Person, since INT64, note STRING)"
        )


def _people(database: object, *identities: int) -> None:
    with database.begin("write") as transaction:
        for identity in identities:
            transaction.execute(
                "CREATE (:Person {id: $id, name: $name})",
                {"id": identity, "name": f"person-{identity}"},
            )


def _nodes(database: object) -> tuple[tuple[object, ...], ...]:
    return database.execute(
        "MATCH (p:Person) RETURN p.id ORDER BY p.id"
    ).rows


def _relationships(database: object) -> tuple[tuple[object, ...], ...]:
    return database.execute(
        "MATCH (a:Person)-[r:Knows]->(b:Person) "
        "RETURN a.id, b.id, r.since, r.note ORDER BY r.since"
    ).rows


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    handle = okto_grafx.connect(tmp_path / "graph")
    _install_schema(handle)
    try:
        yield handle
    finally:
        handle.close()


def test_prior_statement_pending_endpoints_are_owner_visible_and_durable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "owner-view"
    with okto_grafx.connect(root) as database:
        _install_schema(database)
        _people(database, 3)

        writer = database.begin("write")
        writer.execute("CREATE (:Person {id: 1, name: 'pending-source'})")
        writer.execute("CREATE (:Person {id: 2, name: 'pending-target'})")
        writer.execute(
            "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
            "CREATE (a)-[:Knows {since: 2020, note: 'pending/pending'}]->(b)"
        )
        writer.execute(
            "MATCH (a:Person {id: 3}), (b:Person {id: 2}) "
            "CREATE (a)-[:Knows {since: 2021, note: 'mixed'}]->(b)"
        )

        expected = (
            (1, 2, 2020, "pending/pending"),
            (3, 2, 2021, "mixed"),
        )
        assert _relationships(writer) == expected

        outsider = database.begin("read")
        try:
            assert _relationships(outsider) == ()
            writer.commit()
            assert _relationships(outsider) == ()
        finally:
            outsider.commit()

    with okto_grafx.connect(root) as reopened:
        assert _nodes(reopened) == ((1,), (2,), (3,))
        assert _relationships(reopened) == expected
        assert reopened.verify("all").findings == ()


def test_traversal_reads_the_owner_node_update_without_leaking_it(
    database: object,
) -> None:
    """Replace the old dirty-endpoint refusal with the positive combined-view contract."""
    _people(database, 1, 2)
    with database.begin("write") as seed:
        seed.execute(
            "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
            "CREATE (a)-[:Knows {since: 2019, note: 'committed'}]->(b)"
        )

    writer = database.begin("write")
    writer.execute("MATCH (p:Person {id: 2}) SET p.name = 'owner-update'")
    outsider = database.begin("read")
    try:
        assert writer.execute(
            "MATCH (a:Person {id: 1})-[:Knows]->(b:Person) RETURN b.name"
        ).rows == (("owner-update",),)
        assert outsider.execute(
            "MATCH (a:Person {id: 1})-[:Knows]->(b:Person) RETURN b.name"
        ).rows == (("person-2",),)
    finally:
        outsider.commit()
        writer.rollback()

    assert database.execute(
        "MATCH (a:Person {id: 1})-[:Knows]->(b:Person) RETURN b.name"
    ).rows == (("person-2",),)


def test_pending_parallel_edges_and_self_loop_keep_shape_and_properties(
    database: object,
) -> None:
    _people(database, 1, 2)
    writer = database.begin("write")
    try:
        for statement in (
            "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
            "CREATE (a)-[:Knows {since: 2020, note: 'first'}]->(b)",
            "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
            "CREATE (a)-[:Knows {since: 2021, note: 'parallel'}]->(b)",
            "MATCH (a:Person {id: 1}) "
            "CREATE (a)-[:Knows {since: 2022, note: 'loop'}]->(a)",
        ):
            writer.execute(statement)

        assert _relationships(writer) == (
            (1, 2, 2020, "first"),
            (1, 2, 2021, "parallel"),
            (1, 1, 2022, "loop"),
        )
        assert writer.execute(
            "MATCH (a:Person {id: 1})-[r:Knows]->(b:Person) "
            "RETURN b.id, r.since ORDER BY r.since"
        ).rows == ((2, 2020), (2, 2021), (1, 2022))
        assert writer.execute(
            "MATCH (b:Person {id: 2})<-[r:Knows]-(a:Person) "
            "RETURN a.id, r.since ORDER BY r.since"
        ).rows == ((1, 2020), (1, 2021))
    finally:
        writer.rollback()


def test_delete_of_prior_pending_edge_cancels_only_that_edge(
    database: object,
) -> None:
    _people(database, 1, 2, 3)
    with database.begin("write") as writer:
        writer.execute(
            "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
            "CREATE (a)-[:Knows {since: 2020, note: 'remove'}]->(b)"
        )
        writer.execute(
            "MATCH (a:Person {id: 2}), (b:Person {id: 3}) "
            "CREATE (a)-[:Knows {since: 2021, note: 'keep'}]->(b)"
        )

        deleted = writer.execute(
            "MATCH (a:Person)-[r:Knows]->(b:Person) "
            "WHERE r.since = 2020 DELETE r"
        )

        assert deleted.statistics["rows_deleted"] == 1
        assert _nodes(writer) == ((1,), (2,), (3,))
        assert _relationships(writer) == ((2, 3, 2021, "keep"),)

    assert _nodes(database) == ((1,), (2,), (3,))
    assert _relationships(database) == ((2, 3, 2021, "keep"),)


@pytest.mark.parametrize("pending_victim", (False, True), ids=("committed", "pending"))
def test_detach_cancels_all_incident_pending_edges_and_keeps_unrelated(
    database: object, pending_victim: bool
) -> None:
    _people(database, *(2, 3, 4) if pending_victim else (1, 2, 3, 4))

    with database.begin("write") as writer:
        if pending_victim:
            writer.execute("CREATE (:Person {id: 1, name: 'pending-victim'})")
        for statement in (
            "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
            "CREATE (a)-[:Knows {since: 2020, note: 'outgoing'}]->(b)",
            "MATCH (a:Person {id: 3}), (b:Person {id: 1}) "
            "CREATE (a)-[:Knows {since: 2021, note: 'incoming'}]->(b)",
            "MATCH (a:Person {id: 1}) "
            "CREATE (a)-[:Knows {since: 2022, note: 'loop'}]->(a)",
            "MATCH (a:Person {id: 3}), (b:Person {id: 4}) "
            "CREATE (a)-[:Knows {since: 2023, note: 'unrelated'}]->(b)",
        ):
            writer.execute(statement)

        writer.execute("MATCH (p:Person {id: 1}) DETACH DELETE p")

        assert _nodes(writer) == ((2,), (3,), (4,))
        assert _relationships(writer) == ((3, 4, 2023, "unrelated"),)

    assert _nodes(database) == ((2,), (3,), (4,))
    assert _relationships(database) == ((3, 4, 2023, "unrelated"),)
    assert database.verify("all").findings == ()


def test_pending_relationship_rollback_leaves_zero_effect(database: object) -> None:
    _people(database, 1, 2)
    writer = database.begin("write")
    writer.execute(
        "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
        "CREATE (a)-[:Knows {since: 2020, note: 'rollback'}]->(b)"
    )
    assert _relationships(writer) == ((1, 2, 2020, "rollback"),)
    writer.rollback()

    assert _nodes(database) == ((1,), (2,))
    assert _relationships(database) == ()
    assert database.verify("all").findings == ()


def test_endpoint_conflict_publishes_no_partial_pending_relationship(
    database: object,
) -> None:
    _people(database, 1, 2)
    loser = database.begin("write")
    loser.execute(
        "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
        "CREATE (a)-[:Knows {since: 2020, note: 'loser'}]->(b)"
    )

    with database.begin("write") as winner:
        winner.execute("MATCH (p:Person {id: 1}) DETACH DELETE p")

    with pytest.raises(GrafxWriteConflict):
        loser.commit()
    loser.rollback()

    assert _nodes(database) == ((2,),)
    assert _relationships(database) == ()
    assert database.verify("all").findings == ()


def test_dirty_relationship_view_matches_with_endpoint_indexes_and_scan(
    tmp_path: Path,
) -> None:
    long_name = "R" + "x" * 127
    with okto_grafx.connect(tmp_path / "indexed-and-scan") as database:
        with database.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
            schema.execute("CREATE REL TABLE E(FROM Person TO Person, w INT64)")
            schema.execute(
                f"CREATE REL TABLE {long_name}(FROM Person TO Person, w INT64)"
            )
        _people(database, 1, 2, 3)
        with database.begin("write") as seed:
            seed.execute(
                "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
                "CREATE (a)-[:E {w: 1}]->(b)"
            )
            seed.execute(
                f"MATCH (a:Person {{id: 1}}), (b:Person {{id: 2}}) "
                f"CREATE (a)-[:{long_name} {{w: 1}}]->(b)"
            )

        assert len(database.inspect_index("ef_E")) == 1
        assert len(database.inspect_index("et_E")) == 1
        assert long_name in database.queries.skipped_indexes

        writer = database.begin("write")
        try:
            writer.execute(
                "MATCH (a:Person {id: 1}), (b:Person {id: 3}) "
                "CREATE (a)-[:E {w: 2}]->(b)"
            )
            writer.execute(
                f"MATCH (a:Person {{id: 1}}), (b:Person {{id: 3}}) "
                f"CREATE (a)-[:{long_name} {{w: 2}}]->(b)"
            )

            indexed = writer.execute(
                "MATCH (a:Person {id: 1})-[r:E]->(b:Person) "
                "RETURN b.id, r.w ORDER BY r.w"
            ).rows
            scanned = writer.execute(
                f"MATCH (a:Person {{id: 1}})-[r:{long_name}]->(b:Person) "
                "RETURN b.id, r.w ORDER BY r.w"
            ).rows

            assert indexed == scanned == ((2, 1), (3, 2))
        finally:
            writer.rollback()
