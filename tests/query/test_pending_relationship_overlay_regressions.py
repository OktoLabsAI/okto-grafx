"""Public regressions the frozen M-PULSE-1C contract does not reach.

The frozen file fixes the overlay's behaviour. These fix three things about its EDGES, in the
sense of boundary rather than graph: a start node the transaction created while the relationship
table itself is untouched, the endpoint guard on the side the frozen conflict does not delete, and
what the guard is actually worth -- that the losing commit is refused before it writes at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.errors import GrafxWriteConflict


def _install_schema(database: object) -> None:
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
        )
        schema.execute(
            "CREATE REL TABLE Knows(FROM Person TO Person, since INT64, note STRING)"
        )


def _people(database: object, *identities: int) -> None:
    with database.begin("write") as transaction:
        for identity in identities:
            transaction.execute(
                "CREATE (:Person {id: $id, name: $name})",
                {"id": identity, "name": f"person-{identity}"},
            )


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    handle = okto_grafx.connect(tmp_path / "graph")
    _install_schema(handle)
    try:
        yield handle
    finally:
        handle.close()


def test_a_created_node_with_no_created_edges_traverses_to_nothing(
    database: object,
) -> None:
    """A start node this transaction created answers empty, and does not reach the index.

    The relationship table is CLEAN here, so the indexed regime is still the right one for every
    other start -- and that is exactly what makes this case its own. A node created in this
    transaction has no committed edges by definition, but its identity is a private token with no
    stored encoding, so building an index key out of it asks the encoder to store a promise. The
    answer is the empty set, not a refusal about a value that was never going to be stored.
    """
    _people(database, 1, 2)
    with database.begin("write") as seed:
        seed.execute(
            "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
            "CREATE (a)-[:Knows {since: 2019, note: 'committed'}]->(b)"
        )
    # The premise: the endpoint indexes exist and cover the committed edge, so a start that CAN
    # be looked up still is.
    assert len(database.inspect_index("ef_Knows")) == 1
    assert len(database.inspect_index("et_Knows")) == 1

    writer = database.begin("write")
    try:
        writer.execute("CREATE (:Person {id: 9, name: 'created-here'})")
        assert writer.execute(
            "MATCH (a:Person {id: 9})-[:Knows]->(b:Person) RETURN b.id"
        ).rows == ()
        assert writer.execute(
            "MATCH (a:Person {id: 9})<-[:Knows]-(b:Person) RETURN b.id"
        ).rows == ()
        # The committed start still answers through the same traversal.
        assert writer.execute(
            "MATCH (a:Person {id: 1})-[:Knows]->(b:Person) RETURN b.id"
        ).rows == ((2,),)
    finally:
        writer.rollback()


def test_a_deleted_target_endpoint_refuses_the_edge_that_named_it(
    database: object,
) -> None:
    """The guard covers the side the frozen conflict does not delete.

    An edge depends on BOTH of its endpoints, and a guard that only declared the source would let
    a commit publish an edge into a node another transaction had just removed. The two sides are
    symmetric and nothing in the code says so on its own, so the second one is asserted.
    """
    _people(database, 1, 2)
    loser = database.begin("write")
    loser.execute(
        "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
        "CREATE (a)-[:Knows {since: 2020, note: 'loser'}]->(b)"
    )

    with database.begin("write") as winner:
        winner.execute("MATCH (p:Person {id: 2}) DETACH DELETE p")

    with pytest.raises(GrafxWriteConflict):
        loser.commit()
    loser.rollback()

    assert database.execute("MATCH (p:Person) RETURN p.id ORDER BY p.id").rows == ((1,),)
    assert database.execute(
        "MATCH (a:Person)-[r:Knows]->(b:Person) RETURN a.id"
    ).rows == ()
    assert database.verify("all").findings == ()


def test_the_losing_commit_never_reaches_the_write(
    database: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the guard is worth: the refusal arrives at the FIRST validation, before the write.

    The witness is the write itself, counted. A page count is only secondary evidence here and
    cannot carry the claim on its own: a commit that DID reach the write can still leave the
    count unchanged, because the row may land on a page the table already has. Whether
    ``_write_rows`` ran is the thing that separates a refusal before the write from one after it.

    The baseline is taken after the WINNER commits, so growth that legitimately belongs to the
    winner's own detach is never charged to the loser.
    """
    _people(database, 1, 2)
    loser = database.begin("write")
    loser.execute(
        "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
        "CREATE (a)-[:Knows {since: 2020, note: 'loser'}]->(b)"
    )

    with database.begin("write") as winner:
        winner.execute("MATCH (p:Person {id: 1}) DETACH DELETE p")

    storage = database._pool.storage
    after_winner = storage.page_count("heap.dat")
    writes: list[object] = []
    original = TransactionManager._write_rows

    def counted(self: TransactionManager, txn: object) -> object:
        writes.append(txn)
        return original(self, txn)

    monkeypatch.setattr(TransactionManager, "_write_rows", counted)
    with pytest.raises(GrafxWriteConflict):
        loser.commit()
    loser.rollback()

    assert writes == []  # the losing commit was refused before it wrote anything
    assert storage.page_count("heap.dat") == after_winner
    assert database.verify("all").findings == ()
