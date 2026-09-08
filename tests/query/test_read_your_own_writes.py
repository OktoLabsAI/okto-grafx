"""Owner-only node reads over writes staged by an earlier statement."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxPlanError,
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.txn.context import (
    PendingRowRef,
    RowIntent,
    RowOperation,
    TransactionContext,
)
from okto_grafx.engine.query_engine import QueryResult


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    """Return a real database whose node table has an exact primary-key index."""
    handle = okto_grafx.connect(str(tmp_path / "db"))
    with handle.begin("write") as txn:
        txn.execute(
            "CREATE NODE TABLE Person("
            "id INT64, name STRING, age INT64, PRIMARY KEY(id))"
        )
    try:
        yield handle
    finally:
        handle.close()


def _labels(result: QueryResult) -> list[str]:
    """Return the labels of the exact operator tree this result executed."""
    assert result.plan is not None
    return [node.label for node in result.plan.walk()]


def _assert_public_rows_hold_no_pending_reference(result: QueryResult) -> None:
    """Pin the public boundary: transient row identities are never result values."""

    def contains_pending(value: object) -> bool:
        if isinstance(value, PendingRowRef):
            return True
        if isinstance(value, (list, tuple)):
            return any(contains_pending(item) for item in value)
        if isinstance(value, dict):
            return any(
                contains_pending(key) or contains_pending(item)
                for key, item in value.items()
            )
        return False

    assert not contains_pending(result.rows)


def test_created_node_is_visible_to_keyed_and_unkeyed_match(database: object) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")

        keyed = txn.execute("MATCH (p:Person {id: 1}) RETURN p.id, p.name, p.age")
        unkeyed = txn.execute(
            "MATCH (p:Person) RETURN p.id, p.name, p.age ORDER BY p.id"
        )
        merged = txn.execute("MERGE (:Person {id: 1, name: 'Ada', age: 36})")

        assert keyed.rows == ((1, "Ada", 36),)
        assert unkeyed.rows == ((1, "Ada", 36),)
        assert merged.statistics["rows_matched"] == 1
        assert "rows_created" not in merged.statistics
        _assert_public_rows_hold_no_pending_reference(keyed)
        _assert_public_rows_hold_no_pending_reference(unkeyed)


def test_created_node_can_be_set_then_read_and_committed(database: object) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
        changed = txn.execute(
            "MATCH (p:Person {id: 1}) SET p.name = 'Ada L' SET p.age = 37"
        )
        owned = txn.execute("MATCH (p:Person {id: 1}) RETURN p.name, p.age")

        assert changed.statistics["rows_updated"] == 2
        assert owned.rows == (("Ada L", 37),)
        _assert_public_rows_hold_no_pending_reference(owned)

    committed = database.execute("MATCH (p:Person {id: 1}) RETURN p.name, p.age")
    assert committed.rows == (("Ada L", 37),)


def test_coalesce_in_set_observes_nulls_and_rollback(database: object) -> None:
    with database.begin("write") as seed:
        seed.execute("CREATE (:Person {id: 1, name: 'Ada', age: null})")

    writer = database.begin("write")
    try:
        changed = writer.execute(
            "MATCH (p:Person {id: 1}) "
            "SET p.age = coalesce(p.age, 0) + $delta RETURN p.age",
            {"delta": 5},
        )

        assert changed.statistics["rows_updated"] == 1
        assert writer.execute("MATCH (p:Person {id: 1}) RETURN p.age").rows == ((5,),)
    finally:
        writer.rollback()

    assert database.execute("MATCH (p:Person {id: 1}) RETURN p.age").rows == ((None,),)


def test_invalid_coalesce_refuses_before_a_set_has_any_effect(database: object) -> None:
    with database.begin("write") as seed:
        seed.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")

    writer = database.begin("write")
    try:
        accepted = tuple(writer._context.row_intents)
        with pytest.raises(GrafxPlanError) as failure:
            writer.execute("MATCH (p:Person {id: 1}) SET p.age = coalesce()")

        assert failure.value.details["field"] == "function"
        assert tuple(writer._context.row_intents) == accepted
        assert writer.execute("MATCH (p:Person {id: 1}) RETURN p.age").rows == ((36,),)
    finally:
        writer.rollback()


def test_nested_pending_binding_is_detached_before_a_write_is_released(
    database: object,
) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
        changed = txn.execute(
            "MATCH (p:Person {id: 1}) SET p.name = 'after' RETURN [p] AS nested"
        )

        assert changed.rows == (((0,),),)
        _assert_public_rows_hold_no_pending_reference(changed)

    assert database.execute("MATCH (p:Person {id: 1}) RETURN p.name").rows == (
        ("after",),
    )


def test_created_node_can_be_deleted_then_stays_absent_after_commit(
    database: object,
) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
        deleted = txn.execute("MATCH (p:Person {id: 1}) DELETE p")
        owned = txn.execute("MATCH (p:Person) RETURN p.id")

        assert deleted.statistics["rows_deleted"] == 1
        assert owned.rows == ()

    assert database.execute("MATCH (p:Person) RETURN p.id").rows == ()


def test_primary_key_change_moves_the_owner_overlay(database: object) -> None:
    with database.begin("write") as seed:
        seed.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")

    with database.begin("write") as txn:
        txn.execute("MATCH (p:Person {id: 1}) SET p.id = 2")

        old_key = txn.execute("MATCH (p:Person {id: 1}) RETURN p.name")
        new_key = txn.execute("MATCH (p:Person {id: 2}) RETURN p.name")

        assert old_key.rows == ()
        assert new_key.rows == (("Ada",),)

    assert database.execute("MATCH (p:Person {id: 1}) RETURN p.name").rows == ()
    assert database.execute("MATCH (p:Person {id: 2}) RETURN p.name").rows == (
        ("Ada",),
    )


def test_pending_node_identity_does_not_alias_other_pending_nodes(
    database: object,
) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
        txn.execute("CREATE (:Person {id: 2, name: 'Grace', age: 37})")

        deleted = txn.execute(
            "MATCH (a:Person), (b:Person) WHERE a = b AND b.id = 1 DELETE a"
        )

        assert deleted.statistics["rows_deleted"] == 1
        assert txn.execute("MATCH (p:Person) RETURN p.id ORDER BY p.id").rows == ((2,),)

    assert database.execute("MATCH (p:Person) RETURN p.id").rows == ((2,),)


def test_cartesian_rows_delete_one_pending_reference_only_once(
    database: object,
) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
        txn.execute("CREATE (:Person {id: 2, name: 'Grace', age: 37})")

        deleted = txn.execute("MATCH (a:Person), (b:Person) WHERE a.id = 1 DELETE a")

        assert deleted.statistics["rows_deleted"] == 1
        assert txn.execute("MATCH (p:Person) RETURN p.id").rows == ((2,),)

    assert database.execute("MATCH (p:Person) RETURN p.id").rows == ((2,),)


def test_uncommitted_node_overlay_is_visible_only_to_its_owner(
    database: object,
) -> None:
    writer = database.begin("write")
    writer.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
    assert writer.execute("MATCH (p:Person) RETURN p.name").rows == (("Ada",),)

    outsider = database.begin("read")
    try:
        assert outsider.execute("MATCH (p:Person) RETURN p.name").rows == ()
        writer.commit()
        assert outsider.execute("MATCH (p:Person) RETURN p.name").rows == ()
    finally:
        outsider.commit()

    assert database.execute("MATCH (p:Person) RETURN p.name").rows == (("Ada",),)


def test_dirty_table_uses_overlayed_exact_index_before_and_after_commit(
    database: object,
) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
        dirty = txn.execute("MATCH (p:Person {id: 1}) RETURN p.name")

        assert dirty.rows == (("Ada",),)
        assert "IndexSeek" in _labels(dirty)
        assert "NodeScan" not in _labels(dirty)
        assert dirty.statistics["rows_seeked"] == 1

    committed = database.execute("MATCH (p:Person {id: 1}) RETURN p.name")
    assert committed.rows == (("Ada",),)
    assert "IndexSeek" in _labels(committed)
    assert "NodeScan" not in _labels(committed)


def test_dirty_primary_seek_overlays_key_update_value_update_and_delete(
    database: object,
) -> None:
    with database.begin("write") as seed:
        seed.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")

    with database.begin("write") as txn:
        txn.execute("MATCH (p:Person {id: 1}) SET p.id = 2 SET p.name = 'Ada L'")

        old = txn.execute("MATCH (p:Person {id: 1}) RETURN p.name")
        moved = txn.execute("MATCH (p:Person {id: 2}) RETURN p.name")
        txn.execute("MATCH (p:Person {id: 2}) DELETE p")
        deleted = txn.execute("MATCH (p:Person {id: 2}) RETURN p.name")

        assert old.rows == ()
        assert moved.rows == (("Ada L",),)
        assert deleted.rows == ()
        for result in (old, moved, deleted):
            assert "IndexSeek" in _labels(result)
            assert "NodeScan" not in _labels(result)

    assert database.execute("MATCH (p:Person) RETURN p.id").rows == ()


def test_two_dirty_primary_endpoint_seeks_do_not_scan_the_node_table(
    tmp_path: Path,
) -> None:
    handle = okto_grafx.connect(str(tmp_path / "edge-db"))
    try:
        with handle.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Entity(id STRING, name STRING, PRIMARY KEY(id))"
            )
            schema.execute("CREATE REL TABLE Links(FROM Entity TO Entity)")

        with handle.begin("write") as txn:
            for identity in range(100):
                txn.execute(
                    "CREATE (:Entity {id: $id, name: $name})",
                    {"id": f"n{identity}", "name": f"node-{identity}"},
                )
            linked = txn.execute(
                "MATCH (a:Entity {id: $source}), (b:Entity {id: $target}) "
                "CREATE (a)-[:Links]->(b) RETURN a.id, b.id",
                {"source": "n17", "target": "n83"},
            )

            assert linked.rows == (("n17", "n83"),)
            assert linked.statistics["rows_seeked"] == 2
            assert linked.statistics.get("rows_scanned", 0) == 0
            assert _labels(linked).count("IndexSeek") == 2
            assert "NodeScan" not in _labels(linked)
    finally:
        handle.close()


@pytest.mark.parametrize(
    "statement",
    (
        "MATCH (p:Person {id: 1}) SET p.name = 'changed'",
        "MATCH (p:Person {id: 1}) DELETE p",
    ),
)
def test_pending_update_or_delete_budget_refusal_restores_the_statement(
    tmp_path: Path, statement: str
) -> None:
    handle = okto_grafx.connect(str(tmp_path / "budget-db"), max_transaction_rows=1)
    try:
        with handle.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )

        writer = handle.begin("write")
        writer.execute("CREATE (:Person {id: 1, name: 'Ada'})")
        accepted = tuple(writer._context.row_intents)

        with pytest.raises(GrafxTransactionBudgetExceeded) as raised:
            writer.execute(statement)

        assert raised.value.details == {
            "field": "max_transaction_rows",
            "limit": 1,
            "observed": 2,
            "txn_id": writer.txn_id,
        }
        assert tuple(writer._context.row_intents) == accepted
        assert writer.execute("MATCH (p:Person {id: 1}) RETURN p.name").rows == (
            ("Ada",),
        )
        writer.commit()

        assert handle.execute("MATCH (p:Person {id: 1}) RETURN p.name").rows == (
            ("Ada",),
        )
    finally:
        handle.close()


def test_failed_relationship_handover_discards_endpoint_read_guards(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after endpoint guards are published leaves no ghost OCC dependency."""
    with database.begin("write") as schema:
        schema.execute("CREATE REL TABLE Knows(FROM Person TO Person)")
    with database.begin("write") as seed:
        seed.execute("CREATE (:Person {id: 1, name: 'Ada'})")
        seed.execute("CREATE (:Person {id: 2, name: 'Grace'})")

    writer = database.begin("write")
    writer._context.note_read(11)
    writer._context.note_write(12)
    expected_rows = tuple(writer._context.row_intents)
    expected_reads = set(writer._context.read_partitions)
    expected_writes = set(writer._context.write_partitions)

    def refuse_write(_context: TransactionContext, _partition: int) -> None:
        raise RuntimeError("injected failure after endpoint guards")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(TransactionContext, "note_write", refuse_write)
            with pytest.raises(
                GrafxConfigurationError, match="collaborator raised RuntimeError"
            ) as raised:
                writer.execute(
                    "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
                    "CREATE (a)-[:Knows]->(b)"
                )
            assert isinstance(raised.value.__cause__, RuntimeError)

        assert tuple(writer._context.row_intents) == expected_rows
        assert writer._context.read_partitions == expected_reads
        assert writer._context.write_partitions == expected_writes
        assert (
            writer.execute(
                "MATCH (a:Person)-[r:Knows]->(b:Person) RETURN a.id, b.id"
            ).rows
            == ()
        )
    finally:
        writer.rollback()


def test_budget_refusal_mid_handover_unwinds_stored_and_pending_deletes(
    tmp_path: Path,
) -> None:
    handle = okto_grafx.connect(str(tmp_path / "handover-db"), max_transaction_rows=2)
    try:
        with handle.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
        with handle.begin("write") as seed:
            seed.execute("CREATE (:Person {id: 1, name: 'Ada'})")
            seed.execute("CREATE (:Person {id: 2, name: 'Grace'})")

        writer = handle.begin("write")
        writer.execute("CREATE (:Person {id: 9, name: 'Pending'})")
        accepted = tuple(writer._context.row_intents)

        with pytest.raises(GrafxTransactionBudgetExceeded) as raised:
            writer.execute(
                "MATCH (m:Person {id: 2}) "
                "MERGE (n:Person {id: 9, name: 'Pending'}) DELETE m, n"
            )

        assert raised.value.details == {
            "field": "max_transaction_rows",
            "limit": 2,
            "observed": 3,
            "txn_id": writer.txn_id,
        }
        assert tuple(writer._context.row_intents) == accepted
        assert writer.execute("MATCH (p:Person) RETURN p.id ORDER BY p.id").rows == (
            (1,),
            (2,),
            (9,),
        )
        writer.commit()

        assert handle.execute("MATCH (p:Person) RETURN p.id ORDER BY p.id").rows == (
            (1,),
            (2,),
            (9,),
        )
    finally:
        handle.close()


def test_similarity_over_a_dirty_node_table_refuses_before_vector_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = okto_grafx.connect(str(tmp_path / "vector-db"))
    try:
        with handle.begin("write") as schema:
            schema.execute(
                "CREATE VECTOR SPACE minilm_v2 {dimension: 4, metric: 'cosine'}"
            )
            schema.execute(
                "CREATE NODE TABLE Chunk("
                "id INT64, embedding VECTOR(minilm_v2), PRIMARY KEY(id))"
            )

        writer = handle.begin("write")
        writer.execute("CREATE (:Chunk {id: 1, embedding: [1.0, 0.0, 0.0, 0.0]})")

        def search_must_not_run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("dirty-owner refusal must precede the vector engine")

        monkeypatch.setattr(type(handle._vectors), "search", search_must_not_run)
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            writer.execute(
                "MATCH (c:Chunk) "
                "WHERE similarity(c.embedding, $query, space => 'minilm_v2') > 0.0 "
                "RETURN c.id",
                {"query": [1.0, 0.0, 0.0, 0.0]},
            )

        assert raised.value.details == {
            "field": "table",
            "value": "Chunk",
            "table_id": handle.catalog.catalog.table("Chunk").table_id,
            "operation": "similarity",
        }
        assert writer.execute("MATCH (c:Chunk) RETURN c.id").rows == ((1,),)
        writer.rollback()
    finally:
        handle.close()


def test_pending_node_cannot_reach_the_heap_as_a_relationship_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = okto_grafx.connect(str(tmp_path / "pending-endpoint-db"))
    try:
        with handle.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
            schema.execute("CREATE REL TABLE Knows(FROM Person TO Person, since INT64)")

        def endpoint_check_must_not_run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(
                "a pending endpoint must not reach the physical heap door"
            )

        monkeypatch.setattr(
            type(handle._heap), "require_endpoints", endpoint_check_must_not_run
        )

        same_statement = handle.begin("write")
        with pytest.raises(GrafxUnsupportedOperation) as same_statement_raised:
            same_statement.execute(
                "CREATE (a:Person {id: 10, name: 'A'}) "
                "CREATE (b:Person {id: 11, name: 'B'}) "
                "CREATE (a)-[:Knows {since: 2020}]->(b)"
            )
        assert (
            same_statement_raised.value.details["operation"] == "relationship_endpoint"
        )
        assert same_statement_raised.value.details["field"] == "source"
        assert same_statement._context.row_intents == []
        same_statement.rollback()
    finally:
        handle.close()


def test_detach_delete_refuses_an_incident_relationship_held_by_same_statement(
    tmp_path: Path,
) -> None:
    """A refused compound statement hands neither its edge nor its detach to the txn."""
    handle = okto_grafx.connect(str(tmp_path / "held-detach-db"))
    try:
        with handle.begin("write") as schema:
            schema.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
            schema.execute("CREATE REL TABLE Knows(FROM Person TO Person, since INT64)")
        with handle.begin("write") as seed:
            seed.execute("CREATE (:Person {id: 1})")
            seed.execute("CREATE (:Person {id: 2})")

        with handle.begin("write") as writer:
            with pytest.raises(GrafxUnsupportedOperation) as raised:
                writer.execute(
                    "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
                    "CREATE (a)-[:Knows {since: 2020}]->(b) DETACH DELETE a"
                )
            assert raised.value.details["operation"] == "detach_delete"
            assert writer._context.row_intents == []

        assert handle.execute("MATCH (p:Person) RETURN p.id ORDER BY p.id").rows == (
            (1,),
            (2,),
        )
        assert (
            handle.execute("MATCH (a:Person)-[:Knows]->(b:Person) RETURN a.id").rows
            == ()
        )
    finally:
        handle.close()


def test_same_statement_pending_node_named_twice_by_delete_is_cancelled_once(
    database: object,
) -> None:
    """Repeated DELETE of one held insert is idempotent, just like a stored-row delete."""
    with database.begin("write") as writer:
        result = writer.execute(
            "CREATE (p:Person {id: 99, name: 'temporary', age: 1}) DELETE p, p"
        )
        assert result.statistics["rows_deleted"] == 1
        assert writer._context.row_intents == []

    assert database.execute("MATCH (p:Person {id: 99}) RETURN p.id").rows == ()


def test_malformed_pending_sequence_is_refused_before_heap_access(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = database.begin("write")
    try:
        writer.execute("CREATE (:Person {id: 1, name: 'Ada', age: 36})")
        inserted = writer._context.row_intents[0]
        assert isinstance(inserted.reference, PendingRowRef)
        writer._context.row_intents[:] = [
            RowIntent(
                table=inserted.table,
                values=(1, "malformed", 36),
                operation=RowOperation.UPDATE,
                reference=inserted.reference,
            )
        ]

        def heap_read_must_not_run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("a PendingRowRef must never reach heap.read")

        monkeypatch.setattr(type(database._heap), "read", heap_read_must_not_run)
        with pytest.raises(GrafxTransactionStateError) as raised:
            writer.execute("MERGE (:Person {id: 1, name: 'Ada', age: 36})")

        assert raised.value.details["field"] == "pending_row_reference"
        assert raised.value.details["operation"] == "update"
    finally:
        writer.rollback()
