"""The whole lifecycle through the real composition root, with no double anywhere.

Every other module here builds the engine over a fixture. This one calls ``connect`` and uses
what a caller uses: the real storage device, the real page codec, the real buffer pool, catalog,
heap, write-ahead log, coordinator, transaction manager, index framework and vector subsystem.

That is the point. A double weaker than the thing it stands in for certifies nothing (LESSONS
L12), and the write path in particular is a claim about what SURVIVES a commit -- which a double
that records intents cannot answer at all. Here the rows go through the log, the commit assigns
their birth stamps, and the read that follows is a different transaction at a later snapshot.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import okto_grafx
SCHEMA: tuple[str, ...] = (
    "CREATE NODE TABLE Person(id INT64, name STRING, age INT64, PRIMARY KEY(id))",
    "CREATE REL TABLE Knows(FROM Person TO Person, since INT64)",
)


@pytest.fixture
def database() -> Iterator[object]:
    """Return an open in-memory database with the schema installed and committed."""
    handle = okto_grafx.connect(":memory:")
    try:
        with handle.begin("write") as txn:
            for statement in SCHEMA:
                txn.execute(statement)
        yield handle
    finally:
        handle.close()


def test_a_row_created_in_one_transaction_is_read_by_the_next(database: object) -> None:
    with database.begin("write") as txn:
        report = txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
    assert report.statistics["rows_created"] == 1
    assert database.execute("MATCH (p:Person) RETURN p.name").rows == (("Ada",),)


def test_several_rows_of_one_transaction_all_survive_it(database: object) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
        txn.execute("CREATE (:Person {id: 2, name: 'Grace', age: 45})")
        txn.execute("CREATE (:Person {id: 3, name: 'Alan', age: 41})")
    found = database.execute("MATCH (p:Person) RETURN p.id, p.name ORDER BY p.id")
    assert found.rows == ((1, "Ada"), (2, "Grace"), (3, "Alan"))


def test_a_row_is_invisible_until_its_transaction_commits(database: object) -> None:
    # The birth stamp is the commit number, so a snapshot opened before the commit cannot see
    # the row and one opened after can. This is the property the whole staging design exists for.
    txn = database.begin("write")
    txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
    reader = database.begin("read")
    assert reader.execute("MATCH (p:Person) RETURN p.name").rows == ()
    reader.commit()
    txn.commit()
    assert database.execute("MATCH (p:Person) RETURN p.name").rows == (("Ada",),)


def test_a_rolled_back_transaction_leaves_nothing_behind(database: object) -> None:
    txn = database.begin("write")
    txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
    txn.rollback()
    assert database.execute("MATCH (p:Person) RETURN p.name").rows == ()


def test_a_statement_that_refuses_leaves_its_transaction_committable(
    database: object,
) -> None:
    # The refusal staged nothing, so the transaction is exactly as it was and the good statement
    # beside it still commits. A statement that staged half of itself would make this durable.
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
        with pytest.raises(Exception):  # noqa: PT011 - the schema names its own class
            txn.execute("CREATE (:Person {id: 2, nosuch: 1})")
    assert database.execute("MATCH (p:Person) RETURN p.id").rows == ((1,),)


def test_an_edge_between_committed_rows_stores_both_endpoints(database: object) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
        txn.execute("CREATE (:Person {id: 2, name: 'Grace', age: 45})")
    with database.begin("write") as txn:
        txn.execute(
            "MATCH (a:Person), (b:Person) WHERE a.id = 1 AND b.id = 2 "
            "CREATE (a)-[:Knows {since: 1994}]->(b)"
        )
    table = database.catalog.catalog.table("Knows")
    stored = [version.values for _ref, version in database._heap.scan_all(table)]
    # Source and target lead the tuple, ahead of the user's property, per the layout C1 fixed.
    assert stored == [(1, 2, 1994)]


def test_the_database_verifies_clean_after_a_write(database: object) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
    assert database.verify("all").findings == ()


def test_a_merge_creates_once_and_then_matches(database: object) -> None:
    with database.begin("write") as txn:
        txn.execute("MERGE (p:Person {id: 7, name: 'Once'})")
    with database.begin("write") as txn:
        second = txn.execute("MERGE (p:Person {id: 7, name: 'Once'})")
    assert "rows_created" not in second.statistics
    assert database.execute("MATCH (p:Person) RETURN p.id").rows == ((7,),)


def test_a_parameterised_write_and_read_agree(database: object) -> None:
    with database.begin("write") as txn:
        txn.execute(
            "CREATE (:Person {id: $id, name: $name, age: $age})",
            {"id": 5, "name": "Barbara", "age": 52},
        )
    found = database.execute(
        "MATCH (p:Person) WHERE p.id = $wanted RETURN p.name, p.age", {"wanted": 5}
    )
    assert found.rows == (("Barbara", 52),)


def test_an_aggregate_reads_what_the_writes_left(database: object) -> None:
    with database.begin("write") as txn:
        for identity, name, age in ((1, "Ada", 36), (2, "Grace", 45), (3, "Alan", 41)):
            txn.execute(
                "CREATE (:Person {id: $id, name: $name, age: $age})",
                {"id": identity, "name": name, "age": age},
            )
    found = database.execute("MATCH (p:Person) RETURN count(*) AS total, max(p.age) AS oldest")
    assert found.rows == ((3, 45),)


def test_explain_describes_the_tree_the_write_actually_walked(database: object) -> None:
    plan = database.explain("CREATE (:Person {id: 1, name: 'Ada'})")
    labels = [node.label for node in plan.walk()]
    # The eager barrier sits between the write and everything that shapes the result, so a
    # window above can never decide how many rows were written.
    assert labels == ["ProduceResults", "EagerRows", "CreateRelationships", "SingleRow"]


def test_a_set_replaces_the_row_through_the_public_api(database: object) -> None:
    """The seam C5 opened and this engine now consumes: a new version at the commit number."""
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
    with database.begin("write") as txn:
        txn.execute("MATCH (p:Person {id: 1}) SET p.age = 37")
    assert sorted(database.execute("MATCH (p:Person) RETURN p.id, p.age").rows) == [(1, 37)]


def test_a_set_of_two_properties_of_one_row_writes_one_version(database: object) -> None:
    """One version per ROW, not per assignment: a version each would make the last one win."""
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
    with database.begin("write") as txn:
        txn.execute("MATCH (p:Person {id: 1}) SET p.name = 'Ada L', p.age = 37")
    assert sorted(database.execute("MATCH (p:Person) RETURN p.name, p.age").rows) == [
        ("Ada L", 37)
    ]


def test_a_delete_ends_the_row_through_the_public_api(database: object) -> None:
    """A delete ends a version rather than removing bytes, and the row stops being returned."""
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
        txn.execute("CREATE (:Person {id: 2, name: 'Grace', age: 45})")
    with database.begin("write") as txn:
        txn.execute("MATCH (p:Person {id: 2}) DELETE p")
    assert sorted(database.execute("MATCH (p:Person) RETURN p.id").rows) == [(1,)]


def test_two_sets_of_one_row_in_one_transaction_both_survive(database: object) -> None:
    """A SET builds a whole version, so it must build on what THIS transaction already has.

    The heap still shows the old values -- the earlier statement's version is born at a commit
    number that does not exist yet -- so a SET that started from the snapshot would silently undo
    the statement before it and report success. Two statements each setting a different property
    of one row is an ordinary thing to write.
    """
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
    with database.begin("write") as txn:
        txn.execute("MATCH (p:Person {id: 1}) SET p.age = 50")
        txn.execute("MATCH (p:Person {id: 1}) SET p.name = 'Ada L'")
    assert sorted(database.execute("MATCH (p:Person) RETURN p.name, p.age").rows) == [
        ("Ada L", 50)
    ]


def test_a_row_updated_and_then_deleted_in_one_transaction_ends_once(
    database: object,
) -> None:
    """A version ends exactly once, so the transaction's answer about a row is one answer.

    Without settling the intents, the update ends the version at the commit number and the delete
    asks the heap to end the same version again. The heap refuses -- correctly -- and the refusal
    arrives INSIDE the commit section, where the only failures still meant to be possible are the
    device's.
    """
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
        txn.execute("CREATE (:Person {id: 2, name: 'Grace', age: 45})")
    with database.begin("write") as txn:
        txn.execute("MATCH (p:Person {id: 2}) SET p.age = 99")
        txn.execute("MATCH (p:Person {id: 2}) DELETE p")
    assert sorted(database.execute("MATCH (p:Person) RETURN p.id").rows) == [(1,)]


def test_a_merge_does_not_match_a_row_the_same_transaction_deleted(database: object) -> None:
    """The heap still holds it -- its end stamp is the commit number, which does not exist yet.

    A MERGE that scanned the heap without subtracting this transaction's own deletes would answer
    "this row already exists" about a row the caller has just said it wants gone, and the commit
    would then end the very row the MERGE decided not to create.
    """
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
    with database.begin("write") as txn:
        txn.execute("MATCH (p:Person {id: 1}) DELETE p")
        txn.execute("MERGE (p:Person {id: 1, name: 'Ada', age: 36})")
    assert sorted(database.execute("MATCH (p:Person) RETURN p.id").rows) == [(1,)]


# --- E1: a commit that stages durable state must declare interest in it -------------------------


def test_a_schema_statement_declares_the_interest_its_pages_represent(
    database: object,
) -> None:
    """A schema commit must not reach the log with an empty interest set.

    Optimistic validation short-circuits on an empty set -- a transaction that declares interest
    in nothing cannot conflict with anything -- so a schema commit that declared nothing was
    unrefusable, and three participants each acknowledged a durable commit while two of the three
    catalogs were silently replaced. The declaration is structural rather than remembered: it
    comes from staging the page, not from this component choosing to announce it.
    """
    txn = database.begin("write")
    try:
        txn.execute("CREATE NODE TABLE Later(id INT64, PRIMARY KEY(id))")
        assert txn._context.write_partitions != set()
    finally:
        txn.rollback()


def test_two_schema_commits_at_one_snapshot_do_not_both_succeed(database: object) -> None:
    """Exactly one of two concurrent schema changes commits; the loser is told to retry.

    This is E1 as a property. Both transactions write the same catalog page, so the second image
    would replace the first and destroy an acknowledged commit -- which is why the two must not
    both be acknowledged.
    """
    from okto_grafx.domain.errors import GrafxWriteConflict

    first = database.begin("write")
    second = database.begin("write")
    first.execute("CREATE NODE TABLE Alpha(id INT64, PRIMARY KEY(id))")
    second.execute("CREATE NODE TABLE Beta(id INT64, PRIMARY KEY(id))")
    assert first._context.write_partitions & second._context.write_partitions
    first.commit()
    with pytest.raises(GrafxWriteConflict) as refusal:
        second.commit()
    assert refusal.value.retryable is True


# --- the index seam: a commit is what puts a row into every index that covers it ---------------


def test_a_commit_puts_every_written_row_into_every_index_that_covers_it() -> None:
    """The seam that makes a secondary index real, driven through the public door.

    ``IndexManager``'s three staging doors had no caller anywhere in ``src``: a commit wrote rows
    to the heap, applied whatever had been staged into the indexes, and nothing between the two
    ever staged anything. Every index therefore stayed empty for ever -- a lookup answered
    nothing and a similarity search returned no hits for rows plainly visible in the heap, while
    reporting ``stale=False`` and a correct ``live_count``.

    It was not a vector defect. An ordinary hash index was equally empty, which is why this test
    drives one: the seam belongs to the commit section, not to any one kind of index.
    """
    from okto_grafx.domain.index.definition import IndexDefinition
    from okto_grafx.domain.index.visibility import IndexVisibility
    from okto_grafx.engine.index_manager import HashIndex

    handle = okto_grafx.connect(":memory:")
    try:
        with handle.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
        table = handle.catalog.catalog.table("P")
        index = handle._indexes.register(
            HashIndex(
                IndexDefinition(
                    name="hash_P_id",
                    table_id=table.table_id,
                    table_name="P",
                    positions=(0,),
                    visibility=IndexVisibility.EXACT,
                ),
                handle._pool,
                handle._metrics,
            )
        )
        def live() -> int:
            return sum(1 for entry in index.walk() if not entry.dead_csn)

        assert len(index.walk()) == 0

        with handle.begin("write") as txn:
            txn.execute("CREATE (:P {id: 1, name: 'Ada'})")
            txn.execute("CREATE (:P {id: 2, name: 'Grace'})")
        assert (len(index.walk()), live()) == (2, 2)

        # An update ends the entry the old version had and creates one for the new version --
        # both halves, whether or not the key changed, because the new version lives elsewhere.
        with handle.begin("write") as txn:
            txn.execute("MATCH (p:P {id: 2}) SET p.name = 'G Hopper'")
        assert (len(index.walk()), live()) == (3, 2)

        # An EXACT index returns a superset (SD-3), so a delete ENDS the entry it already holds
        # rather than adding one: the total does not move and the LIVE count falls.
        with handle.begin("write") as txn:
            txn.execute("MATCH (p:P {id: 1}) DELETE p")
        assert (len(index.walk()), live()) == (3, 1)
        assert handle.verify("all").findings == ()
    finally:
        handle.close()


def test_a_similarity_search_finds_the_rows_a_match_can_see() -> None:
    """The consequence at the public door, and the one a caller would report as a wrong result.

    An empty index is not visible as damage: the search reported ``regime='exact'``, where recall
    is 1.0 by construction, with ``achieved_k=0`` for three rows a ``MATCH`` returned.
    """
    handle = okto_grafx.connect(":memory:")
    try:
        with handle.begin("write") as txn:
            txn.execute("CREATE VECTOR SPACE minilm_v2 {dimension: 4, metric: 'cosine'}")
            txn.execute(
                "CREATE NODE TABLE Chunk("
                "id INT64, body STRING, embedding VECTOR(minilm_v2), PRIMARY KEY(id))"
            )
        with handle.begin("write") as txn:
            for identity, vector in enumerate(
                ([1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.9, 0.1, 0.0, 0.0]), 1
            ):
                txn.execute(
                    f"CREATE (:Chunk {{id: {identity}, body: 'b{identity}', "
                    f"embedding: {vector}}})"
                )
        assert len(handle.execute("MATCH (c:Chunk) RETURN c.id").rows) == 3

        reader = handle.begin("read")
        try:
            result = handle.search_vectors(
                reader,
                space="minilm_v2",
                k=3,
                query=[1.0, 0.0, 0.0, 0.0],
            )
        finally:
            reader.rollback()
        assert result.achieved_k == 3
        assert [hit.record_id for hit in result.hits] == [1, 3, 2]
        assert handle.verify("all").findings == ()
    finally:
        handle.close()


def test_verify_keys_a_row_the_way_the_index_it_checks_keys_it() -> None:
    """A vector index keys on a digest of the embedding, not on the column bytes.

    The coverage walk called ``index_key`` directly -- one derivation of several -- so for a
    vector index it computed a key no entry could ever match and reported every live row as
    missing an entry it in fact had. That is a false ``corruption`` verdict on a correct
    database, which is what A11-revised exists to prevent, and it stayed invisible only while
    nothing populated a vector index at all.
    """
    handle = okto_grafx.connect(":memory:")
    try:
        with handle.begin("write") as txn:
            txn.execute("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
            txn.execute("CREATE NODE TABLE C(id INT64, e VECTOR(s), PRIMARY KEY(id))")
        with handle.begin("write") as txn:
            txn.execute("CREATE (:C {id: 1, e: [1.0, 0.0, 0.0, 0.0]})")
        # The VECTOR index by name, not by position: the table also carries the index of its
        # primary key now, and that one keys on column bytes -- so "the first index" would have
        # made this assertion pass for the wrong index and prove nothing about the vector one.
        index = handle._indexes.index("vector_C_s")
        assert index.definition.key_derivation != "columns"
        assert len(index.walk()) == 1
        assert handle.verify("all").findings == ()
        assert handle.verify("indexes").findings == ()
    finally:
        handle.close()


# --- the primary key names exactly one row ----------------------------------------------------


def test_a_second_row_under_one_primary_key_is_refused_across_transactions(
    database: object,
) -> None:
    """Kuzu refuses a duplicate key outright (D3). Until this check nothing here did.

    Two committed transactions each creating ``{id: 1}`` left two rows under one declared
    primary key -- duplication an ordinary caller reaches, with ``verify()`` agreeing. The
    refusal is typed and names the constraint; it is not a plan error, because the statement
    is a valid one against a table that happens to hold the row already.
    """
    from okto_grafx.domain.errors import GrafxQueryError

    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
    with database.begin("write") as txn:
        with pytest.raises(GrafxQueryError) as refusal:
            txn.execute("CREATE (:Person {id: 1, name: 'Other', age: 1})")
    assert refusal.value.details["constraint"] == "primary_key"
    assert database.execute("MATCH (p:Person) RETURN p.id").rows == ((1,),)


def test_two_nodes_of_one_statement_cannot_share_a_primary_key(database: object) -> None:
    """The rows of the pattern being built are not held yet, so they are checked explicitly."""
    from okto_grafx.domain.errors import GrafxQueryError

    with database.begin("write") as txn:
        with pytest.raises(GrafxQueryError):
            txn.execute("CREATE (:Person {id: 9, name: 'a'}), (:Person {id: 9, name: 'b'})")
    assert database.execute("MATCH (p:Person) RETURN p.id").rows == ()


def test_a_set_cannot_move_a_row_onto_another_rows_primary_key(database: object) -> None:
    """An update writes a whole new version, so it is checked like an insert -- minus itself."""
    from okto_grafx.domain.errors import GrafxQueryError

    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
        txn.execute("CREATE (:Person {id: 2, name: 'Grace', age: 45})")
    with database.begin("write") as txn:
        with pytest.raises(GrafxQueryError):
            txn.execute("MATCH (p:Person {id: 2}) SET p.id = 1")
    # A row that keeps its own key is not refused against itself.
    with database.begin("write") as txn:
        txn.execute("MATCH (p:Person {id: 2}) SET p.id = 2, p.name = 'G'")
    assert sorted(database.execute("MATCH (p:Person) RETURN p.id, p.name").rows) == [
        (1, "Ada"),
        (2, "G"),
    ]


def test_a_key_freed_by_a_delete_in_the_same_transaction_may_be_reused(
    database: object,
) -> None:
    """The heap still shows the deleted row -- its end stamp is the commit number -- so the
    check subtracts this transaction's own deletes, exactly as MERGE does."""
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
    with database.begin("write") as txn:
        txn.execute("MATCH (p:Person {id: 1}) DELETE p")
        txn.execute("CREATE (:Person {id: 1, name: 'Again', age: 1})")
    assert database.execute("MATCH (p:Person) RETURN p.name").rows == (("Again",),)


# --- traversal: a relationship is a walk over the two endpoints its row leads with (W5c) ---------


@pytest.fixture
def graph() -> Iterator[object]:
    """A four-person database with a three-edge cycle 1 -> 2 -> 3 -> 1 and person 4 isolated.

    The edges are created in a LATER transaction than the people. A row's identity is allocated
    by the commit (W5b), so a MATCH inside the transaction that created a row cannot bind it
    yet; the rows must be committed before an edge between them can be written.
    """
    handle = okto_grafx.connect(":memory:")
    try:
        with handle.begin("write") as txn:
            for statement in SCHEMA:
                txn.execute(statement)
        with handle.begin("write") as txn:
            for identity, name in enumerate(("Ada", "Grace", "Alan", "Barbara"), 1):
                txn.execute(f"CREATE (:Person {{id: {identity}, name: '{name}', age: 1}})")
        with handle.begin("write") as txn:
            for source, target, since in ((1, 2, 1), (2, 3, 2), (3, 1, 3)):
                txn.execute(
                    f"MATCH (a:Person {{id: {source}}}), (b:Person {{id: {target}}}) "
                    f"CREATE (a)-[:Knows {{since: {since}}}]->(b)"
                )
        yield handle
    finally:
        handle.close()


def _rows(handle: object, text: str) -> list[tuple[object, ...]]:
    return sorted(handle.execute(text).rows)


def test_one_hop_follows_the_edge_the_way_it_points(graph: object) -> None:
    assert _rows(graph, "MATCH (a:Person)-[:Knows]->(b:Person) RETURN a.id, b.id") == [
        (1, 2),
        (2, 3),
        (3, 1),
    ]
    assert _rows(graph, "MATCH (a:Person)<-[:Knows]-(b:Person) RETURN a.id, b.id") == [
        (1, 3),
        (2, 1),
        (3, 2),
    ]


def test_an_undirected_pattern_follows_the_edge_both_ways(graph: object) -> None:
    assert _rows(graph, "MATCH (a:Person {id: 1})-[:Knows]-(b:Person) RETURN b.id") == [
        (2,),
        (3,),
    ]


def test_a_relationship_variable_binds_the_edge_and_its_properties(graph: object) -> None:
    assert _rows(
        graph, "MATCH (a:Person)-[k:Knows]->(b:Person) RETURN a.id, k.since, b.id"
    ) == [(1, 1, 2), (2, 2, 3), (3, 3, 1)]


def test_a_hop_range_reaches_every_depth_in_the_range(graph: object) -> None:
    assert _rows(graph, "MATCH (a:Person {id: 1})-[:Knows*2]->(b:Person) RETURN b.id") == [
        (3,)
    ]
    assert _rows(
        graph, "MATCH (a:Person {id: 1})-[:Knows*1..3]->(b:Person) RETURN b.id"
    ) == [(1,), (2,), (3,)]


def test_no_edge_is_walked_twice_on_one_path(graph: object) -> None:
    """openCypher's relationship isomorphism: the cycle has three edges, so four hops find nothing
    rather than going round again for ever."""
    assert _rows(graph, "MATCH (a:Person {id: 1})-[:Knows*3]->(b:Person) RETURN b.id") == [
        (1,)
    ]
    assert _rows(graph, "MATCH (a:Person {id: 1})-[:Knows*4]->(b:Person) RETURN b.id") == []


def test_a_target_bound_upstream_is_a_filter_not_a_new_binding(graph: object) -> None:
    assert _rows(
        graph,
        "MATCH (a:Person {id: 1}), (b:Person {id: 2}) MATCH (a)-[:Knows]->(b) RETURN a.id, b.id",
    ) == [(1, 2)]
    assert _rows(
        graph,
        "MATCH (a:Person {id: 1}), (b:Person {id: 3}) MATCH (a)-[:Knows]->(b) RETURN a.id, b.id",
    ) == []


def test_an_edge_to_a_node_the_snapshot_cannot_see_is_not_followed(graph: object) -> None:
    """The edge survives its endpoint's deletion (W5c carries RecordIds, not refs), and a walk
    under a snapshot that no longer sees the endpoint does not land on it."""
    with graph.begin("write") as txn:
        txn.execute("MATCH (p:Person {id: 3}) DELETE p")
    assert _rows(graph, "MATCH (a:Person {id: 2})-[:Knows]->(b:Person) RETURN b.id") == []
    assert _rows(graph, "MATCH (a:Person)-[:Knows]->(b:Person) RETURN a.id, b.id") == [(1, 2)]


def test_an_isolated_node_has_no_neighbours(graph: object) -> None:
    assert _rows(graph, "MATCH (a:Person {id: 4})-[:Knows]->(b:Person) RETURN b.id") == []
