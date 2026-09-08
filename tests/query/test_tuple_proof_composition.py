"""Per-participant encoding proofs retain identity, weak lifetime and bounded locking."""

from __future__ import annotations

import copy
import gc
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Lock
from weakref import WeakKeyDictionary, ref

import pytest

from okto_grafx.domain.model import schema
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.runtime.tuple_encoding_proofs import new_tuple_encoding_proofs

# Proof factory/registry inspection is intentional; none of this is a public mutation API.
# ruff: noqa: SLF001


def _table() -> TableDef:
    return TableDef(
        table_id=81, name="ProofRows", kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64), ColumnDef(name="name", type=ValueType.STRING)),
    )


def test_exact_identity_and_unregistered_copies_cannot_forge_proofs() -> None:
    protocol = new_tuple_encoding_proofs()
    table = _table()
    values = (1, "Ada")
    payload, proof = protocol.encode(table, values)
    assert proof is not None
    assert protocol.payload(table, values, proof) is payload
    assert protocol.payload(replace(table), values, proof) is None
    assert protocol.payload(table, tuple(list(values)), proof) is None
    for forged in (object(), copy.copy(proof), copy.deepcopy(proof), object.__new__(type(proof))):
        assert protocol.payload(table, values, forged) is None
        protocol.forget(forged)
    assert protocol.payload(table, values, proof) is payload
    protocol.forget(proof)
    assert protocol.payload(table, values, proof) is None
    protocol.forget(proof)


def test_another_participant_neither_accepts_nor_revokes_the_original_proof() -> None:
    first = new_tuple_encoding_proofs()
    second = new_tuple_encoding_proofs()
    table = _table()
    values = (1, "Ada")
    payload, proof = first.encode(table, values)
    assert second.payload(table, values, proof) is None
    second.forget(proof)
    assert first.payload(table, values, proof) is payload


def test_registry_retention_ends_with_proof_lifetime() -> None:
    entries = WeakKeyDictionary()
    protocol = schema._tuple_encoding_proof_protocol(entries, Lock())
    table = _table()
    values = (1, "Ada")
    payload, proof = protocol.encode(table, values)
    weak_proof = ref(proof)
    assert len(entries) == 1
    assert entries[proof][0] is table
    assert entries[proof][1] is values
    assert entries[proof][2] is payload
    del proof
    gc.collect()
    assert weak_proof() is None
    assert len(entries) == 0


def test_encoding_error_publishes_no_partial_proof() -> None:
    entries = WeakKeyDictionary()
    protocol = schema._tuple_encoding_proof_protocol(entries, Lock())
    with pytest.raises(SchemaMismatchError):
        protocol.encode(_table(), ("wrong integer", "Ada"))
    assert len(entries) == 0


def test_canonical_encoding_runs_outside_the_registry_guard(monkeypatch) -> None:
    guard = Lock()
    entries = WeakKeyDictionary()
    protocol = schema._tuple_encoding_proof_protocol(entries, guard)
    original = schema.encode_tuple
    calls = []

    def observed(table, values):
        assert guard.acquire(blocking=False), "encoding was serialized under the proof guard"
        guard.release()
        calls.append(values)
        return original(table, values)

    monkeypatch.setattr(schema, "encode_tuple", observed)
    table = _table()
    values = (1, "Ada")
    payload, proof = protocol.encode(table, values)
    assert protocol.payload(table, values, proof) is payload
    assert calls == [values]


def test_registry_publication_lookup_and_forget_are_guarded() -> None:
    guard = Lock()
    observed = []

    class GuardedEntries(WeakKeyDictionary):
        def __setitem__(self, key, value):
            assert guard.locked()
            observed.append("set")
            return super().__setitem__(key, value)

        def get(self, key, default=None):
            assert guard.locked()
            observed.append("get")
            return super().get(key, default)

        def pop(self, key, default=None):
            assert guard.locked()
            observed.append("pop")
            return super().pop(key, default)

    protocol = schema._tuple_encoding_proof_protocol(GuardedEntries(), guard)
    table = _table()
    values = (1, "Ada")
    payload, proof = protocol.encode(table, values)
    assert protocol.payload(table, values, proof) is payload
    protocol.forget(proof)
    assert observed == ["set", "get", "pop"]
    assert not guard.locked()


def test_concurrent_encoders_cannot_cross_wire_rows_or_proofs() -> None:
    protocol = new_tuple_encoding_proofs()
    table = _table()
    barrier = Barrier(4, timeout=5)

    def run(index):
        values = (index, f"row {index}")
        payload, proof = protocol.encode(table, values)
        barrier.wait()
        assert protocol.payload(table, values, proof) is payload
        barrier.wait()
        return values, payload, proof

    with ThreadPoolExecutor(max_workers=4) as executor:
        outputs = tuple(executor.map(run, range(4)))
    for values, payload, proof in outputs:
        assert protocol.payload(table, values, proof) is payload
        for other_values, _, other_proof in outputs:
            if other_values is not values:
                assert protocol.payload(table, values, other_proof) is None
        protocol.forget(proof)


def test_omitted_protocol_is_canonical_only_not_an_implicit_global_registry() -> None:
    table = _table()
    values = (1, "Ada")
    payload, proof = schema._encode_tuple_with_proof(table, values)
    assert payload == schema.encode_tuple(table, values)
    assert proof is None
    protocol = new_tuple_encoding_proofs()
    _, live = protocol.encode(table, values)
    assert schema._proved_tuple_payload(table, values, live) is None
    schema._forget_tuple_encoding_proof(live)
    assert protocol.payload(table, values, live) is not None


def test_nested_mutable_values_are_not_proved_even_with_native_registry() -> None:
    table = TableDef(
        table_id=82, name="Lists", kind="node",
        columns=(ColumnDef(name="items", type=ValueType.LIST),),
    )
    protocol = new_tuple_encoding_proofs()
    mutable = [1]
    values = (mutable,)
    before, proof = protocol.encode(table, values)
    assert proof is None
    mutable.append(2)
    after, proof = protocol.encode(table, values)
    assert proof is None and after != before
