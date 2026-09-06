"""An already validated authority is reused; the first refusing validation is never skipped.

Two hot paths rebuilt what they had already proved.  ``CatalogIndexDefinition.runtime_definition``
constructed -- and fully re-validated -- a fresh ``IndexDefinition`` on every call, thirteen times
per relationship insert and once per replayed index record.  ``change_of`` decoded the same frozen
``WalRecord`` up to five times on the redo path (preflight twice, redo dispatch, manager dispatch,
the store's own apply) and twice more while retargeting a commit batch.  Both now keep ONE private
slot on the object they belong to, keyed by the identity of what they were built from: the
generation descriptor object, or the immutable ``bytes`` payload object.  These tests count the
constructions and decodes that are avoided, and pin the doors that keep refusing: a foreign
generation, an equal-but-distinct descriptor, a stand-in record, a mutable payload, a corrupt
payload and a record whose declared type contradicts its payload.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxIndexError,
)
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index.catalog import (
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
    IndexGenerationState,
)
from okto_grafx.domain.index.definition import IndexDefinition
from okto_grafx.domain.index.records import IndexChange, IndexOperation, change_of
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.wal.record import WalRecord, WalRecordType


class _Counter:
    """Count constructions of one dataclass by wrapping its __post_init__."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, owner: type) -> None:
        self.calls = 0
        original = owner.__post_init__
        counter = self

        def counted(instance: object) -> None:
            counter.calls += 1
            original(instance)

        monkeypatch.setattr(owner, "__post_init__", counted)


class _DecodeCounter:
    """Count payload decodes: the sealed protocol and every other reader call IndexChange.decode."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        original = IndexChange.decode
        counter = self

        def counted(payload: bytes) -> IndexChange:
            counter.calls += 1
            return original(payload)

        monkeypatch.setattr(IndexChange, "decode", staticmethod(counted))


def _generation(nonce: int, state: IndexGenerationState) -> IndexGenerationDescriptor:
    return IndexGenerationDescriptor(artifact_nonce=nonce, bucket_count=64, state=state)


def _logical() -> CatalogIndexDefinition:
    return CatalogIndexDefinition(
        name="pk_a",
        table_id=7,
        table_name="A",
        positions=(0,),
        visibility=IndexVisibility.EXACT,
        generations=(_generation(0x11, IndexGenerationState.ACTIVE),),
    )


# --- runtime definitions -----------------------------------------------------------------------


def test_the_runtime_definition_is_built_once_per_generation_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logical = _logical()
    built = _Counter(monkeypatch, IndexDefinition)

    first = logical.runtime_definition()
    assert built.calls == 1
    for _ in range(12):
        assert logical.runtime_definition() is first
    active = logical.active_generation()
    assert active is not None
    assert logical.runtime_definition(active) is first
    assert built.calls == 1, "thirteen reads, one construction and one validation"
    assert first.artifact_nonce == 0x11
    assert first.bucket_count == 64


def test_an_equal_but_distinct_descriptor_rebuilds_and_a_foreign_one_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logical = _logical()
    built = _Counter(monkeypatch, IndexDefinition)
    first = logical.runtime_definition()
    assert built.calls == 1

    # Equal value, different object: not the proved identity, so it is built -- and validated --
    # again, and the answer is equal but private to that call.
    twin = replace(logical.generations[0])
    assert twin == logical.generations[0] and twin is not logical.generations[0]
    rebuilt = logical.runtime_definition(twin)
    assert built.calls == 2
    assert rebuilt == first and rebuilt is not first

    # The ownership refusal runs on every call, before and after a hit: a foreign descriptor
    # with a colliding nonce never receives a definition, memoised or not.
    foreign = _generation(0x11, IndexGenerationState.ACTIVE)
    foreign = replace(foreign, bucket_count=128)
    with pytest.raises(GrafxIndexError):
        logical.runtime_definition(foreign)
    assert (
        logical.runtime_definition() is first or logical.runtime_definition() == first
    )
    with pytest.raises(GrafxIndexError):
        logical.runtime_definition(foreign)


def test_a_generation_change_never_serves_the_old_runtime_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logical = _logical()
    old = logical.runtime_definition()
    shadow = _generation(0x22, IndexGenerationState.BUILDING)
    with_shadow = logical.with_generation(shadow)
    activated = with_shadow.activate_generation(0x22)
    built = _Counter(monkeypatch, IndexDefinition)

    fresh = activated.runtime_definition()
    assert built.calls == 1
    assert fresh.artifact_nonce == 0x22
    assert fresh != old
    assert activated.runtime_definition() is fresh
    # The original logical definition is untouched: same object, same memo, same answer.
    assert logical.runtime_definition() is old
    assert built.calls == 1


def test_the_memo_is_not_part_of_the_value() -> None:
    logical = _logical()
    other = _logical()
    assert logical == other and hash(logical) == hash(other)
    logical.runtime_definition()
    assert logical == other and hash(logical) == hash(other)
    assert "_runtime" not in repr(logical)
    copied = replace(logical, automatic=True)
    assert copied._runtime is None, "replace() starts from an empty slot"


# --- log records ---------------------------------------------------------------------------------


def _change(operation: IndexOperation = IndexOperation.INSERT) -> IndexChange:
    return IndexChange(
        index="pk_a", operation=operation, key=b"\x01key", ref=RecordRef(3, 1)
    )


def _record(change: IndexChange, *, record_type: int | None = None) -> WalRecord:
    return WalRecord(
        record_type=int(change.record_type) if record_type is None else record_type,
        payload=change.encode(),
        txn_id=9,
    )


def test_an_exact_record_over_immutable_bytes_is_decoded_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record(_change())
    decoded = _DecodeCounter(monkeypatch)

    first = change_of(record)
    assert decoded.calls == 1
    for _ in range(6):
        assert change_of(record) is first
    assert decoded.calls == 1, "seven readers, one decode and one validation"
    assert first == _change()

    # A retargeted copy is a new record over new bytes: it proves itself once, independently.
    moved = replace(record, payload=replace(first, ref=RecordRef(4, 2)).encode())
    assert moved._decoded is None
    assert change_of(moved).ref == RecordRef(4, 2)
    assert change_of(moved) is change_of(moved)
    assert decoded.calls == 2
    assert change_of(record) is first, "the original record keeps its own proof"


def test_a_stand_in_record_and_a_mutable_payload_decode_every_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _change()
    decoded = _DecodeCounter(monkeypatch)

    stand_in = SimpleNamespace(
        record_type=int(change.record_type), payload=change.encode(), lsn=0
    )
    for _ in range(3):
        assert change_of(stand_in) == change
    assert decoded.calls == 3, "a structural stand-in is never trusted with a memo"

    # The record seals a mutable payload into immutable bytes at construction, so even a record
    # built from a bytearray proves itself once, and the caller's buffer can no longer reach it.
    buffer = bytearray(change.encode())
    sealed = WalRecord(record_type=int(change.record_type), payload=buffer, txn_id=9)
    assert type(sealed.payload) is bytes
    assert change_of(sealed) == change
    assert change_of(sealed) is change_of(sealed)
    assert decoded.calls == 4
    buffer[:] = replace(change, ref=RecordRef(5, 0)).encode()
    assert change_of(sealed).ref == RecordRef(3, 1), (
        "the sealed record never sees the buffer"
    )
    assert decoded.calls == 4


def test_a_corrupt_payload_refuses_every_reader_and_leaves_the_slot_empty() -> None:
    corrupt = WalRecord(
        record_type=int(WalRecordType.INDEX_WRITE), payload=b"\xff\xfe\xfd", txn_id=9
    )
    for _ in range(3):
        with pytest.raises(GrafxError):
            change_of(corrupt)
    assert corrupt._decoded is None


def test_a_record_type_contradicting_its_payload_is_still_corruption_after_a_hit() -> (
    None
):
    change = _change()
    lying = _record(change, record_type=int(WalRecordType.INDEX_RECONCILE))
    with pytest.raises(GrafxCorruptionDetected):
        change_of(lying)
    # The payload itself decoded fine and was memoised; the contradiction is re-checked per read.
    assert lying._decoded is not None
    with pytest.raises(GrafxCorruptionDetected):
        change_of(lying)


# --- end to end ----------------------------------------------------------------------------------


def test_a_checkpoint_decodes_each_index_record_once_and_builds_few_definitions(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The redo path reads every index record several times; the record proves itself once."""
    database = connect(str(tmp_path / "db"))
    try:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE A(id INT64, name STRING, PRIMARY KEY(id))")
        with database.begin("write") as txn:
            for row in range(40):
                txn.execute(f"CREATE (:A {{id: {row}, name: 'r{row}'}})")
        decoded = _DecodeCounter(monkeypatch)
        built = _Counter(monkeypatch, IndexDefinition)
        database.checkpoint()
        # 40 inserts on one primary-key index: one logical record each, decoded exactly once by
        # the whole redo path (preflight, dispatch, store apply), never five times.
        assert decoded.calls <= 40, decoded.calls
        # Resolving the index by name for each of those records reuses one runtime definition
        # per (logical index, generation); only header decodes may add a bounded few.
        assert built.calls <= 40, built.calls
        assert database.verify("all").findings == ()
        assert database.execute("MATCH (a:A {id: 39}) RETURN a.name").rows == (
            ("r39",),
        )
    finally:
        database.close()


# --- retargeting carries the proof ---------------------------------------------------------------


def test_a_retargeted_record_carries_the_change_that_produced_its_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_grafx.domain.index.records import record_for_change

    original = _record(_change())
    decoded = _DecodeCounter(monkeypatch)
    moved_change = replace(change_of(original), ref=RecordRef(4, 2))
    assert decoded.calls == 1

    carried = record_for_change(original, moved_change)
    assert carried is not original
    assert carried.payload == moved_change.encode()
    assert carried.record_type == original.record_type
    assert carried.txn_id == original.txn_id
    # The readers that follow a retarget -- staging validation, the commit path -- reuse the
    # process's own proof: no decode of bytes this process produced a moment ago.
    assert change_of(carried) is moved_change
    assert change_of(carried) is moved_change
    assert decoded.calls == 1
    # The proof is byte-identical to what the codec would rebuild.
    assert IndexChange.decode(carried.payload) == moved_change
    assert decoded.calls == 2
    # The original record keeps its own, unrelated proof.
    assert change_of(original).ref == RecordRef(3, 1)
    assert decoded.calls == 2


def test_a_stand_in_record_cannot_carry_a_change() -> None:
    from okto_grafx.domain.index.records import record_for_change

    change = _change()
    stand_in = SimpleNamespace(record_type=int(change.record_type), payload=b"", lsn=0)
    with pytest.raises(GrafxIndexError):
        record_for_change(stand_in, change)  # type: ignore[arg-type]
    with pytest.raises(GrafxIndexError):
        record_for_change(_record(change), SimpleNamespace(encode=lambda: b""))  # type: ignore[arg-type]


def test_a_record_born_from_a_change_carries_that_change_as_its_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_grafx.domain.index.records import wal_record_for

    change = _change()
    decoded = _DecodeCounter(monkeypatch)
    born = wal_record_for(change, epoch=1, txn_id=9)
    assert born.payload == change.encode()
    # Every later reader of the record this process built -- staging validation, retarget,
    # the commit path -- reuses the change itself: not one decode of bytes we just produced.
    assert change_of(born) is change
    assert change_of(born) is change
    assert decoded.calls == 0
    assert IndexChange.decode(born.payload) == change, "and the codec would rebuild it"
    assert decoded.calls == 1

    # A record read back from the log is a different object over the same bytes: it proves
    # itself once, from bytes, like any record that did not originate in this process.
    read_back = WalRecord(
        record_type=born.record_type, payload=bytes(born.payload), epoch=1, txn_id=9
    )
    assert change_of(read_back) == change
    assert change_of(read_back) is not change
    assert decoded.calls == 2


def test_a_change_subclass_is_never_carried_as_a_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_grafx.domain.index.records import wal_record_for

    class _Lookalike(IndexChange):
        __slots__ = ()

    change = _change()
    lookalike = _Lookalike(
        index=change.index, operation=change.operation, key=change.key, ref=change.ref
    )
    decoded = _DecodeCounter(monkeypatch)
    born = wal_record_for(lookalike, epoch=1, txn_id=9)
    observed = change_of(born)
    assert decoded.calls == 1, "a subclass's bytes are decoded like any other record's"
    assert type(observed) is IndexChange
    assert observed == change


# --- the proof cannot be planted, and it never outlives its bytes ---------------------------------


def test_an_intact_record_cannot_be_poisoned_through_any_ordinary_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every door that writes the slot derives one side from the other; none takes both."""
    from okto_grafx.domain.index.records import record_for_change, wal_record_for

    genuine = _change()
    intact = _record(genuine)
    fake = replace(genuine, ref=RecordRef(9, 9))
    decoded = _DecodeCounter(monkeypatch)

    # record_for_change never touches the record it is given: the change travels only in a
    # new record whose payload IS that change's encoding.
    carried = record_for_change(intact, fake)
    assert carried is not intact
    assert intact.payload == genuine.encode()
    assert change_of(intact) == genuine
    assert decoded.calls == 1
    assert change_of(carried) is fake
    assert IndexChange.decode(carried.payload) == fake
    # wal_record_for builds a new record from the change; the intact one is not involved.
    assert change_of(wal_record_for(fake)) is fake
    assert change_of(intact) == genuine


def test_a_slot_entry_without_the_private_token_is_not_a_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a forger who bypasses the frozen dataclass cannot make change_of trust a value."""
    genuine = _change()
    record = _record(genuine)
    fake = replace(genuine, ref=RecordRef(9, 9))
    decoded = _DecodeCounter(monkeypatch)

    for planted in (
        (record.payload, fake, object()),  # right payload, a token of its own making
        (record.payload, fake),  # wrong shape
        (bytes(record.payload), fake, object()),  # equal bytes, different object
        fake,  # a bare value
    ):
        object.__setattr__(record, "_decoded", planted)
        assert change_of(record) == genuine, "the bytes decide, not the planted value"
    assert decoded.calls == 4
    # After the last read the record carries a genuine proof again, and it is reused.
    assert change_of(record) == genuine
    assert decoded.calls == 4


def test_a_proof_is_reused_by_a_shallow_copy_but_never_by_a_deep_copy_or_a_pickle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import copy
    import pickle

    genuine = _change()
    record = _record(genuine)
    first = change_of(record)
    decoded = _DecodeCounter(monkeypatch)

    shallow = copy.copy(record)
    assert change_of(shallow) is first, "same bytes object, same private token: genuine"
    assert decoded.calls == 0

    deep = copy.deepcopy(record)
    assert change_of(deep) == genuine
    assert change_of(deep) is not first, (
        "a copied token is not the token: decoded from bytes"
    )
    assert decoded.calls == 1

    revived = pickle.loads(pickle.dumps(record))
    assert change_of(revived) == genuine
    assert change_of(revived) is not first
    assert decoded.calls == 2
    assert revived == record and deep == record and shallow == record


def test_no_helper_of_the_records_module_can_plant_a_change_in_an_intact_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Try every callable the module exposes, private ones included, as a poisoning door."""
    import inspect

    from okto_grafx.domain.index import records as module

    genuine = _change()
    record = _record(genuine)
    fake = replace(genuine, ref=RecordRef(9, 9))
    truth = IndexChange.decode(record.payload)
    attempts = 0
    for name, door in vars(module).items():
        if not callable(door) or inspect.isclass(door) or name.startswith("__"):
            continue
        for args in (
            (record, record.payload, fake),
            (record, fake),
            (fake, record),
            (record, fake, record.payload),
            (fake,),
        ):
            attempts += 1
            try:
                door(*args)
            except BaseException:  # noqa: BLE001 - any refusal is fine; only the slot matters
                pass
            try:
                door(fake, template=record)
            except BaseException:  # noqa: BLE001
                pass
            assert change_of(record) == truth, f"{name} planted a change"
            assert change_of(record) is not fake, f"{name} planted the fake object"
            assert record.payload == genuine.encode()
    assert attempts >= 15, "the sweep must have exercised the module"
    # And the only way to a different change is a different record over different bytes.
    other = module.record_for_change(record, fake)
    assert other is not record and other.payload == fake.encode()
    assert change_of(other) == IndexChange.decode(other.payload)
