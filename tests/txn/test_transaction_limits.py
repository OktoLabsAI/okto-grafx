"""Blocking acceptance tests for the four transaction resource limits."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxTransactionBudgetExceeded,
)
from okto_grafx.domain.model.schema import ColumnDef, TableDef, encode_tuple
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.txn.context import TransactionContext, TransactionMode
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.engine.txn_manager import TransactionManager
from txn_support import Stack

HEAP = "heap.dat"


def _table() -> TableDef:
    """Return the smallest schema that distinguishes payload sizes by one byte."""
    return TableDef(
        table_id=1,
        name="person",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="label", type=ValueType.STRING),
        ),
        primary_key="id",
        from_table=None,
        to_table=None,
    )


def _registered(stack: Stack) -> TableDef:
    """Install and flush the test table without involving the WAL under test."""
    table = _table()
    stack.catalog.catalog.add_table(table)
    stack.catalog.save()
    stack.pool.flush(stack.catalog.file)
    stack.pool.flush(stack.heap.file)
    return table


def _manager(stack: Stack, **limits: int) -> TransactionManager:
    """Compose a real manager over the fixture's real stores with selected limits."""
    return TransactionManager(
        stack.wal,
        stack.pool,
        stack.heap,
        stack.catalog,
        stack.coordinator,
        stack.clock,
        stack.metrics,
        None,
        partitions_per_table=8,
        commit_lock_timeout=5.0,
        descriptor="hash-v1;partitions_per_table=8",
        **limits,
    )


def _context(
    *, txn_id: int = 1, max_transaction_bytes: int | None = None
) -> tuple[TransactionContext, object]:
    """Open a real isolated context and return its private page-staging capability."""
    capability = object()
    return (
        TransactionContext(
            txn_id=txn_id,
            mode=TransactionMode.WRITE,
            snapshot=Snapshot(0),
            epoch=0,
            owner=object(),
            page_staging_capability=capability,
            max_transaction_bytes=max_transaction_bytes,
        ),
        capability,
    )


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "64", None])
def test_direct_manager_refuses_an_invalid_identity_lease_size(
    stack: Stack, value: object
) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        _manager(stack, identity_lease_size=value)  # type: ignore[arg-type]

    assert raised.value.details["field"] == "identity_lease_size"


def _assert_budget(
    failure: GrafxTransactionBudgetExceeded,
    *,
    field: str,
    limit: int,
    observed: int,
    txn_id: int,
) -> None:
    """Assert the stable machine-readable refusal contract."""
    assert failure.details == {
        "field": field,
        "limit": limit,
        "observed": observed,
        "txn_id": txn_id,
    }


def test_statement_write_limit_accepts_exactly_the_limit_and_refuses_one_more(
    tmp_path: Path,
) -> None:
    """The refused statement leaves the previous complete statement untouched."""
    limit = 2
    with connect(tmp_path / "statement-budget", max_statement_writes=limit) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
        for identity in (1, 2, 3):
            with database.begin("write") as seed:
                seed.execute(f"CREATE (:Person {{id: {identity}}})")

        transaction = database.begin("write")
        transaction.execute(
            "MATCH (p:Person) WHERE p.id < 3 CREATE (:Person {id: p.id + 100})"
        )
        accepted = tuple(transaction._context.row_intents)
        assert len(accepted) == limit

        with pytest.raises(GrafxTransactionBudgetExceeded) as raised:
            transaction.execute("MATCH (p:Person) CREATE (:Person {id: p.id + 200})")

        _assert_budget(
            raised.value,
            field="max_statement_writes",
            limit=limit,
            observed=limit + 1,
            txn_id=transaction.txn_id,
        )
        assert tuple(transaction._context.row_intents) == accepted
        assert transaction.commit().wrote is True

        result = database.execute(
            "MATCH (p:Person) WHERE p.id >= 100 RETURN p.id ORDER BY p.id"
        )
        assert result.rows == ((101,), (102,))


def test_transaction_row_limit_is_cumulative_and_prior_rows_still_commit(
    stack: Stack,
) -> None:
    """Crossing the transaction-wide row ceiling retains no part of the refused write."""
    limit = 2
    table = _registered(stack)
    manager = _manager(stack, max_transaction_rows=limit)
    transaction = manager.begin("write")
    transaction.stage_row_insert(table, (1, "Ada"))
    transaction.stage_row_insert(table, (2, "Grace"))

    with pytest.raises(GrafxTransactionBudgetExceeded) as raised:
        transaction.stage_row_insert(table, (3, "Linus"))

    _assert_budget(
        raised.value,
        field="max_transaction_rows",
        limit=limit,
        observed=limit + 1,
        txn_id=transaction.txn_id,
    )
    assert [intent.values for intent in transaction.row_intents] == [
        (1, "Ada"),
        (2, "Grace"),
    ]
    transaction.note_write(manager.partition_of(table.table_id, b"accepted"))
    assert manager.commit(transaction).wrote is True

    reader = manager.begin("read")
    assert [
        version.values for _ref, version in stack.heap.scan(table, reader.snapshot)
    ] == [
        (1, "Ada"),
        (2, "Grace"),
    ]
    manager.rollback(reader)


def test_transaction_byte_limit_uses_encoded_payload_and_refuses_before_retaining() -> (
    None
):
    """One extra encoded byte is visible in details and leaves the context empty."""
    table = _table()
    accepted_values = (1, "a")
    refused_values = (2, "ab")
    limit = len(encode_tuple(table, accepted_values))
    assert len(encode_tuple(table, refused_values)) == limit + 1

    accepted, _accepted_capability = _context(max_transaction_bytes=limit)
    accepted.stage_row_insert(table, accepted_values)
    assert [intent.values for intent in accepted.row_intents] == [accepted_values]
    assert accepted._staged_payload_bytes == limit

    refused, _refused_capability = _context(txn_id=2, max_transaction_bytes=limit)
    with pytest.raises(GrafxTransactionBudgetExceeded) as raised:
        refused.stage_row_insert(table, refused_values)

    _assert_budget(
        raised.value,
        field="max_transaction_bytes",
        limit=limit,
        observed=limit + 1,
        txn_id=refused.txn_id,
    )
    assert refused.row_intents == []
    assert refused._staged_payload_bytes == 0


def test_discard_restores_replaced_and_new_page_images_exactly() -> None:
    """Unwind restores replacements and additions without inferring keys from a count."""
    transaction, capability = _context(max_transaction_bytes=64)
    location = (HEAP, 7)
    transaction._stage_page_image(*location, b"before", capability=capability)
    expected_images = dict(transaction.page_images)
    expected_proofs = dict(transaction._page_image_proofs)
    expected_bytes = transaction._staged_payload_bytes

    mark = transaction.staging_mark()
    transaction._stage_page_image(*location, b"replacement", capability=capability)
    transaction.discard_since(mark)

    assert transaction.page_images == expected_images
    assert transaction._page_image_proofs == expected_proofs
    assert transaction._staged_payload_bytes == expected_bytes
    # A settlement failure must leave its mark available to the statement failure path.
    # Otherwise the staged row survives the refusal and a later commit can publish it.
    transaction, _capability = _context(max_transaction_bytes=64)
    invalid_record = object()
    transaction.pending_records.append(invalid_record)  # type: ignore[arg-type]
    mark = transaction.staging_mark()
    transaction.stage_row_insert(_table(), (7, "Ada"))

    with pytest.raises(GrafxConfigurationError):
        transaction.settle_staging_mark(mark)

    assert transaction._staging_marks[-1][0] is mark
    transaction.discard_since(mark)
    assert transaction.row_intents == []
    assert transaction.pending_records == [invalid_record]
    assert transaction.write_partitions == set()
    assert transaction._staging_marks == []
    assert transaction.unproved_page_images() == ()

    transaction, capability = _context(max_transaction_bytes=64)
    transaction._stage_page_image(HEAP, 9, b"older", capability=capability)
    expected_images = dict(transaction.page_images)
    expected_proofs = dict(transaction._page_image_proofs)
    expected_bytes = transaction._staged_payload_bytes

    mark = transaction.staging_mark()
    transaction._stage_page_image(HEAP, 1, b"newer", capability=capability)
    assert sorted(transaction.page_images)[0] == (HEAP, 1)
    transaction.discard_since(mark)

    assert transaction.page_images == expected_images
    assert transaction._page_image_proofs == expected_proofs
    assert transaction._staged_payload_bytes == expected_bytes


def test_discard_restores_read_and_write_partitions_exactly() -> None:
    """A refused statement cannot retain conflict guards for work it discarded."""
    transaction, _capability = _context()
    transaction.note_read(11)
    transaction.note_write(12)

    mark = transaction.staging_mark()
    transaction.note_read(21)
    transaction.note_write(22)
    transaction.discard_since(mark)

    assert transaction.read_partitions == {11}
    assert transaction.write_partitions == {12}


def test_schema_byte_budget_refusal_restores_the_whole_statement(tmp_path: Path) -> None:
    """A refusal between catalog pages leaves no image, partition or phantom schema behind."""
    limit = 512
    with connect(
        tmp_path / "schema-budget",
        page_size=512,
        buffer_budget_bytes=64 * 1024,
        max_transaction_bytes=limit,
    ) as database:
        transaction = database.begin("write")
        with pytest.raises(GrafxTransactionBudgetExceeded) as raised:
            transaction.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")

        _assert_budget(
            raised.value,
            field="max_transaction_bytes",
            limit=limit,
            observed=2 * limit,
            txn_id=transaction.txn_id,
        )
        assert transaction._context.page_images == {}
        assert transaction._context._page_image_proofs == {}
        assert transaction._context.write_partitions == set()
        assert transaction._context._staging_marks == []
        assert transaction.commit().wrote is False
        assert tuple(database.catalog.catalog.tables()) == ()


def test_wal_batch_limit_refuses_before_append_or_barrier_and_publishes_nothing(
    stack: Stack,
) -> None:
    """An oversized final batch leaves both the log and every readable snapshot unchanged."""
    limit = 1
    table = _registered(stack)
    manager = _manager(stack, max_wal_batch_bytes=limit)
    transaction = manager.begin("write")
    transaction.stage_row_insert(table, (1, "Ada"))
    transaction.note_write(manager.partition_of(table.table_id, b"Ada"))
    records_before = stack.wal.records()
    appended_before = tuple(stack.wal.appended)
    barriers_before = stack.wal.barriers
    lsn_before = stack.wal.last_lsn

    with pytest.raises(GrafxTransactionBudgetExceeded) as raised:
        manager.commit(transaction)

    assert raised.value.details["field"] == "max_wal_batch_bytes"
    assert raised.value.details["limit"] == limit
    assert raised.value.details["observed"] > limit
    assert raised.value.details["txn_id"] == transaction.txn_id
    assert stack.wal.records() == records_before
    assert tuple(stack.wal.appended) == appended_before
    assert stack.wal.barriers == barriers_before
    assert stack.wal.last_lsn == lsn_before

    reader = manager.begin("read")
    assert list(stack.heap.scan(table, reader.snapshot)) == []
    manager.rollback(reader)
    manager.rollback(transaction)
