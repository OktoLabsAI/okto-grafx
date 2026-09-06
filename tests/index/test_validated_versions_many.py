"""Many exact keys validated under ONE durable page-0 certificate (KG-MK1).

``validated_versions`` costs one stable view per key: a page-0 pre-certificate, the bucket
traversal, the heap validation and a fresh page-0 post-read.  ``validated_versions_many`` answers
N keys of one EXACT index inside one such view, aligned one-to-one with the keys it was given.
These proofs pin what must not change: the answer per key is the scalar answer, every candidate
is validated the same way, keys are refused before a view opens, a page-0 transition repeats the
whole batch and never leaks a prefix, and a PROXIMITY or stale index is refused exactly as before.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import (
    INDEX_READ_RETRY_BUDGET,
    HashIndex,
    IndexManager,
    IndexStore,
)

from .conftest import Database, SnapshotDouble, TransactionDouble, build_database

BORN = 10
LATER = 20


def _insert(database: Database, record_id: int, name: str, csn: int) -> object:
    ref = database.insert(record_id, name, csn)
    txn = TransactionDouble(txn_id=record_id + 100)
    database.exact.stage_insert(txn, database.key(record_id, name), ref, 0)
    database.exact.commit(txn, csn)
    return ref


def _populated(count: int = 12) -> tuple[Database, dict[int, object]]:
    database = build_database()
    refs = {
        index: _insert(database, index, f"n{index}", BORN)
        for index in range(1, count + 1)
    }
    database.pool.flush(database.heap.file)
    return database, refs


def _keys(database: Database, count: int = 12) -> list[bytes]:
    return [database.key(index, f"n{index}") for index in range(1, count + 1)]


def _scalar(
    database: Database, keys: list[bytes], snapshot: SnapshotDouble
) -> tuple[tuple[tuple[object, object], ...], ...]:
    return tuple(
        database.manager.validated_versions(database.exact, key, snapshot)
        for key in keys
    )


def _refs(answer: tuple[tuple[object, object], ...]) -> tuple[object, ...]:
    return tuple(ref for ref, _version in answer)


class _ViewCounter:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.begins = 0
        self.finishes = 0
        self.probes = 0
        self.heap_views = 0
        original_begin = HashIndex.begin_exact_read
        original_finish = HashIndex.finish_exact_read
        original_probe = IndexStore._candidates_unchecked
        original_view = IndexManager._prepare_heap_view

        def begin(store: HashIndex, required_lsn: int) -> object:
            self.begins += 1
            return original_begin(store, required_lsn)

        def finish(store: HashIndex, before: object, required_lsn: int) -> bool:
            self.finishes += 1
            return original_finish(store, before, required_lsn)

        def probe(store: IndexStore, wanted: bytes) -> object:
            self.probes += 1
            return original_probe(store, wanted)

        def view(manager: IndexManager, file: str, certificate: object) -> None:
            self.heap_views += 1
            original_view(manager, file, certificate)

        monkeypatch.setattr(HashIndex, "begin_exact_read", begin)
        monkeypatch.setattr(HashIndex, "finish_exact_read", finish)
        monkeypatch.setattr(IndexStore, "_candidates_unchecked", probe)
        monkeypatch.setattr(IndexManager, "_prepare_heap_view", view)


def test_many_answers_every_position_exactly_as_the_scalar_read_does() -> None:
    database, refs = _populated()
    later = _insert(database, 13, "n13", LATER)
    database.pool.flush(database.heap.file)
    keys = _keys(database, 13) + [database.key(99, "absent")]

    for snapshot in (SnapshotDouble(BORN), SnapshotDouble(LATER)):
        many = database.manager.validated_versions_many(database.exact, keys, snapshot)
        assert type(many) is tuple
        assert len(many) == len(keys)
        assert many == _scalar(database, keys, snapshot)
        assert many[-1] == ()
    early = database.manager.validated_versions_many(
        database.exact, keys, SnapshotDouble(BORN)
    )
    assert early[12] == ()
    late = database.manager.validated_versions_many(
        database.exact, keys, SnapshotDouble(LATER)
    )
    assert _refs(late[12]) == (later,)
    assert _refs(late[0]) == (refs[1],)


def test_many_opens_one_certificate_and_one_heap_view_per_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _refs = _populated()
    keys = _keys(database)
    counter = _ViewCounter(monkeypatch)

    _scalar(database, keys, SnapshotDouble(BORN))
    assert (counter.begins, counter.finishes, counter.probes) == (12, 12, 12)
    scalar_views = counter.heap_views

    counter.begins = counter.finishes = counter.probes = counter.heap_views = 0
    database.manager.validated_versions_many(database.exact, keys, SnapshotDouble(BORN))

    assert (counter.begins, counter.finishes, counter.probes) == (1, 1, 12)
    assert counter.heap_views == 1 <= scalar_views


def test_a_page_zero_transition_repeats_the_whole_batch_and_matches_the_scalar_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _refs = _populated()
    keys = _keys(database)
    expected = _scalar(database, keys, SnapshotDouble(BORN))
    counter = _ViewCounter(monkeypatch)
    original_finish = HashIndex.finish_exact_read
    outcomes: list[bool] = []

    def changed_once(store: HashIndex, before: object, required_lsn: int) -> bool:
        stable = original_finish(store, before, required_lsn)
        if not outcomes:
            # A foreign page-0 transition landed while the batch was being validated: the
            # post-read must lose, and the ENTIRE batch must be re-proved from the device.
            outcomes.append(False)
            store._carried_certificate = None  # noqa: SLF001 - what a lost post-read does
            store._cache_certificate = None  # noqa: SLF001
            database.pool.discard_clean_file(store.file)
            return False
        outcomes.append(stable)
        return stable

    monkeypatch.setattr(HashIndex, "finish_exact_read", changed_once)

    many = database.manager.validated_versions_many(
        database.exact, keys, SnapshotDouble(BORN)
    )

    assert outcomes == [False, True]
    assert counter.probes == 2 * len(keys)
    assert many == expected


def test_a_transition_on_every_attempt_refuses_the_batch_without_a_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _refs = _populated()
    keys = _keys(database)
    counter = _ViewCounter(monkeypatch)

    monkeypatch.setattr(
        HashIndex, "finish_exact_read", lambda store, before, required_lsn: False
    )

    with pytest.raises(GrafxIndexError) as refused:
        database.manager.validated_versions_many(
            database.exact, keys, SnapshotDouble(BORN)
        )

    assert refused.value.details["field"] == "index_view_changed"
    assert refused.value.retryable is True
    assert counter.begins == INDEX_READ_RETRY_BUDGET + 1
    assert counter.probes == len(keys) * (INDEX_READ_RETRY_BUDGET + 1)


def test_a_proximity_index_is_refused_before_any_view_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _refs = _populated()
    counter = _ViewCounter(monkeypatch)

    with pytest.raises(GrafxIndexError) as refused:
        database.manager.validated_versions_many(
            database.proximity, [database.key(1, "n1")], SnapshotDouble(BORN)
        )

    assert refused.value.details["field"] == "visibility"
    assert counter.begins == 0


@pytest.mark.parametrize(
    "hostile",
    ("not-bytes", 7, None),
    ids=("str", "int", "none"),
)
def test_a_hostile_key_is_refused_before_any_view_opens(
    monkeypatch: pytest.MonkeyPatch, hostile: object
) -> None:
    database, _refs = _populated()
    counter = _ViewCounter(monkeypatch)

    with pytest.raises(GrafxIndexError) as refused:
        database.manager.validated_versions_many(
            database.exact,
            [database.key(1, "n1"), hostile],  # type: ignore[list-item]
            SnapshotDouble(BORN),
        )

    assert refused.value.details["field"] == "key"
    assert counter.begins == 0


def test_an_oversized_key_is_refused_before_any_view_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _refs = _populated()
    counter = _ViewCounter(monkeypatch)
    oversized = b"k" * (database.exact.max_key_bytes + 1)

    with pytest.raises(GrafxIndexError) as refused:
        database.manager.validated_versions_many(
            database.exact, [oversized], SnapshotDouble(BORN)
        )

    assert refused.value.details["field"] == "key"
    assert counter.begins == 0


def test_repeated_and_differently_typed_keys_are_probed_once_and_answered_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, refs = _populated()
    key = database.key(3, "n3")
    other = database.key(4, "n4")
    counter = _ViewCounter(monkeypatch)

    many = database.manager.validated_versions_many(
        database.exact,
        [key, bytearray(key), other, memoryview(key), key],
        SnapshotDouble(BORN),
    )

    assert len(many) == 5
    assert _refs(many[0]) == _refs(many[1]) == _refs(many[3]) == _refs(many[4])
    assert _refs(many[0]) == (refs[3],)
    assert _refs(many[2]) == (refs[4],)
    assert counter.probes == 2


def test_an_entry_whose_row_derives_another_key_is_skipped_as_the_scalar_read_skips_it() -> (
    None
):
    database, refs = _populated()
    foreign = database.key(5, "other")
    txn = TransactionDouble(txn_id=900)
    database.exact.stage_insert(txn, foreign, refs[5], 0)
    database.exact.commit(txn, BORN)
    keys = [foreign, database.key(5, "n5")]

    many = database.manager.validated_versions_many(
        database.exact, keys, SnapshotDouble(BORN)
    )

    assert many[0] == ()
    assert many == _scalar(database, keys, SnapshotDouble(BORN))
    assert _refs(many[1]) == (refs[5],)


def test_a_row_of_another_table_is_corruption_for_the_whole_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, refs = _populated()
    keys = _keys(database)
    original_read = HeapStore.read

    def foreign_table(store: HeapStore, ref: object) -> object:
        version = original_read(store, ref)
        if ref == refs[7]:
            return replace(version, table_id=version.table_id + 1)
        return version

    monkeypatch.setattr(HeapStore, "read", foreign_table)

    with pytest.raises(GrafxCorruptionDetected) as refused:
        database.manager.validated_versions_many(
            database.exact, keys, SnapshotDouble(BORN)
        )

    assert refused.value.details["field"] == "table_id"


def test_no_keys_open_no_view(monkeypatch: pytest.MonkeyPatch) -> None:
    database, _refs = _populated()
    counter = _ViewCounter(monkeypatch)

    assert (
        database.manager.validated_versions_many(
            database.exact, [], SnapshotDouble(BORN)
        )
        == ()
    )
    assert counter.begins == 0


def test_reusing_skips_only_cached_keys_inside_a_fresh_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, refs = _populated()
    keys = _keys(database, 4)
    counter = _ViewCounter(monkeypatch)

    generation, reused, first = database.manager.validated_versions_many_reusing(
        database.exact,
        keys[:3],
        SnapshotDouble(BORN),
        generation=None,
        cached={},
    )
    assert reused is False
    assert counter.probes == 3

    cached = dict(zip(keys[:3], first, strict=True))
    counter.begins = counter.finishes = counter.probes = counter.heap_views = 0
    next_generation, reused, second = (
        database.manager.validated_versions_many_reusing(
            database.exact,
            [keys[1], keys[3], keys[0]],
            SnapshotDouble(BORN),
            generation=generation,
            cached=cached,
        )
    )

    assert reused is True
    assert next_generation == generation
    assert counter.begins == counter.finishes == counter.heap_views == 1
    assert counter.probes == 1
    assert tuple(_refs(group) for group in second) == (
        (refs[2],),
        (refs[4],),
        (refs[1],),
    )


def test_reusing_a_changed_generation_revalidates_every_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _refs_by_id = _populated()
    keys = _keys(database, 3)
    generation, _reused, first = database.manager.validated_versions_many_reusing(
        database.exact,
        keys,
        SnapshotDouble(BORN),
        generation=None,
        cached={},
    )
    cached = dict(zip(keys, first, strict=True))
    _insert(database, 13, "n13", LATER)
    database.pool.flush(database.heap.file)
    counter = _ViewCounter(monkeypatch)

    changed, reused, answer = database.manager.validated_versions_many_reusing(
        database.exact,
        keys,
        SnapshotDouble(BORN),
        generation=generation,
        cached=cached,
    )

    assert changed != generation
    assert reused is False
    assert counter.probes == len(keys)
    assert answer == _scalar(database, keys, SnapshotDouble(BORN))


def test_reusing_retries_with_no_cached_prefix_after_a_mid_read_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _refs_by_id = _populated()
    keys = _keys(database, 4)
    generation, _reused, first = database.manager.validated_versions_many_reusing(
        database.exact,
        keys[:3],
        SnapshotDouble(BORN),
        generation=None,
        cached={},
    )
    cached = dict(zip(keys[:3], first, strict=True))
    original_finish = HashIndex.finish_exact_read
    outcomes: list[bool] = []
    counter = _ViewCounter(monkeypatch)

    def changed_once(store: HashIndex, before: object, required_lsn: int) -> bool:
        stable = original_finish(store, before, required_lsn)
        if not outcomes:
            outcomes.append(False)
            store._carried_certificate = None  # noqa: SLF001 - emulate a foreign publish
            store._cache_certificate = None  # noqa: SLF001
            database.pool.discard_clean_file(store.file)
            return False
        outcomes.append(stable)
        return stable

    monkeypatch.setattr(HashIndex, "finish_exact_read", changed_once)

    _new_generation, reused, answer = database.manager.validated_versions_many_reusing(
        database.exact,
        keys,
        SnapshotDouble(BORN),
        generation=generation,
        cached=cached,
    )

    assert outcomes == [False, True]
    assert reused is True
    # Both attempts saw the same durable certificate: three groups remained authorized and the
    # one absent key was reproved in each complete attempt.  Nothing from the losing attempt was
    # published to the caller.
    assert counter.probes == 2
    assert answer == _scalar(database, keys, SnapshotDouble(BORN))


def test_a_stale_index_is_refused_exactly_as_the_scalar_read_refuses_it() -> None:
    database, _refs = _populated()
    keys = _keys(database)
    database.exact.mark_stale("foreign generation")

    with pytest.raises(GrafxIndexError) as scalar:
        database.manager.validated_versions(
            database.exact, keys[0], SnapshotDouble(BORN)
        )
    with pytest.raises(GrafxIndexError) as many:
        database.manager.validated_versions_many(
            database.exact, keys, SnapshotDouble(BORN)
        )

    assert many.value.details["field"] == scalar.value.details["field"]
