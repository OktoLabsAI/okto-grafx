"""WRITE-1: one exact row encoding may cross validation and heap staging once.

The proof is an acceleration, never authority: it retains the exact table and values objects,
is revoked with the transaction, and every changed/copied/custom path falls back to the same
canonical encoder before a page can be touched.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import okto_grafx
import okto_grafx.domain.model.schema as schema_module
import okto_grafx.domain.txn.context as context_module
import okto_grafx.engine.heap_store as heap_store_module
import okto_grafx.engine.query_engine as query_engine_module
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.schema import (
    ColumnDef,
    TableDef,
    ValueType,
    _encode_tuple_with_proof,
    _proved_tuple_payload,
)
from okto_grafx.engine.heap_store import HeapStore


SCHEMA = "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"


def _database(tmp_path: object):
    database = okto_grafx.connect(str(tmp_path))
    with database.begin("write") as transaction:
        transaction.execute(SCHEMA)
    return database


def _count_encodes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count every module-local route to the canonical tuple encoder."""
    original = schema_module.encode_tuple
    calls = [0]

    def counted(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(schema_module, "encode_tuple", counted)
    monkeypatch.setattr(context_module, "encode_tuple", counted)
    monkeypatch.setattr(heap_store_module, "encode_tuple", counted)
    monkeypatch.setattr(query_engine_module, "encode_tuple", counted)
    return calls


def test_create_and_update_encode_each_final_row_once(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    try:
        calls = _count_encodes(monkeypatch)
        with database.begin("write") as transaction:
            transaction.execute("CREATE (:Person {id: 1, name: 'Ada'})")
        assert calls == [1]

        calls[0] = 0
        with database.begin("write") as transaction:
            transaction.execute("MATCH (p:Person {id: 1}) SET p.name = 'Grace'")
        assert calls == [1]
        assert database.execute("MATCH (p:Person) RETURN p.id, p.name").rows == (
            (1, "Grace"),
        )
    finally:
        database.close()


def test_a_replaced_equal_tuple_cannot_reuse_an_old_proof(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    try:
        transaction = database.begin("write")
        transaction.execute("CREATE (:Person {id: 1, name: 'Ada'})")
        intent = transaction._context.row_intents[0]
        copied = tuple(list(intent.values))
        assert copied == intent.values and copied is not intent.values
        transaction._context.row_intents[0] = replace(intent, values=copied)

        calls = _count_encodes(monkeypatch)
        transaction.commit()
        assert calls == [1]
        assert _proved_tuple_payload(
            intent.table, intent.values, intent._encoding_proof
        ) is None
    finally:
        database.close()


def test_nested_mutable_values_never_receive_an_identity_only_proof() -> None:
    table = TableDef(
        table_id=71,
        name="MutableValues",
        kind="node",
        columns=(
            ColumnDef(name="items", type=ValueType.LIST),
            ColumnDef(name="mapping", type=ValueType.MAP),
        ),
    )
    items = [1]
    mapping = {"before": 2}
    values = (items, mapping)

    before, proof = _encode_tuple_with_proof(table, values)
    assert proof is None
    items.append(3)
    mapping["after"] = 4
    after, another_proof = _encode_tuple_with_proof(table, values)

    assert another_proof is None
    assert after != before


def test_transaction_local_proof_budget_falls_back_to_canonical_encoding(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    try:
        monkeypatch.setattr(context_module, "_MAX_RETAINED_TUPLE_ENCODING_BYTES", 0)
        calls = _count_encodes(monkeypatch)
        transaction = database.begin("write")
        transaction.execute("CREATE (:Person {id: 1, name: 'Ada'})")
        assert transaction._context.row_intents[0]._encoding_proof is None
        transaction.commit()

        # Validation still occurs before staging, and commit seals a fresh canonical encoding
        # for the heap.  Exhausting the optimization budget therefore costs work, not safety.
        assert calls == [2]
    finally:
        database.close()


def test_a_changed_tuple_with_an_old_proof_is_refused_before_heap_mutation(
    tmp_path: object
) -> None:
    database = _database(tmp_path)
    try:
        transaction = database.begin("write")
        transaction.execute("CREATE (:Person {id: 1, name: 'Ada'})")
        intent = transaction._context.row_intents[0]
        transaction._context.row_intents[0] = replace(
            intent, values=("not-an-int", "Ada")
        )
        with pytest.raises(SchemaMismatchError):
            transaction.commit()
        transaction.rollback()
        assert database.execute("MATCH (p:Person) RETURN p.id").rows == ()
    finally:
        database.close()


def test_an_overridden_heap_door_receives_the_legacy_call_and_reencodes(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path)
    try:
        calls = _count_encodes(monkeypatch)
        original = HeapStore.insert_initial_reserved
        observed: list[dict[str, object]] = []

        def legacy_override(
            store,
            table,
            record_id,
            values,
            xmin,
            *,
            next_record_id,
        ):
            observed.append({"next_record_id": next_record_id})
            return original(
                store,
                table,
                record_id,
                values,
                xmin,
                next_record_id=next_record_id,
            )

        monkeypatch.setattr(HeapStore, "insert_initial_reserved", legacy_override)
        with database.begin("write") as transaction:
            transaction.execute("CREATE (:Person {id: 1, name: 'Ada'})")
        # Materialisation encoded once; the deliberately legacy heap override received no new
        # keyword and took its canonical encode fallback once.
        assert observed and calls == [2]
    finally:
        database.close()
