"""Native exact batches share bucket walks, not heap proofs or authority."""
from collections import Counter

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.index.keys import bucket_of
from okto_grafx.engine.index_manager import HashIndex, INDEX_READ_RETRY_BUDGET, IndexStore

from .conftest import SnapshotDouble, TEST_BUCKET_COUNT
from .test_validated_versions_many import _keys, _populated, _scalar, BORN


def _bucket_pin_counter(database, monkeypatch):
    pool_type = type(database.pool)
    original = pool_type.pinned
    pins = []

    def counted(pool, file, page, **kwargs):
        if pool is database.pool and file == database.exact.file and page != 0:
            pins.append(page)
        return original(pool, file, page, **kwargs)

    monkeypatch.setattr(pool_type, "pinned", counted)
    return pins


def test_colliding_keys_visit_each_chain_page_once_and_keep_scalar_entry_order(monkeypatch):
    database, _ = _populated(60)
    keys = _keys(database, 60)
    expected = {key: database.exact._candidates_unchecked(key) for key in keys}
    pages = {page for bucket in range(TEST_BUCKET_COUNT)
             for page in database.exact._bucket_pages(bucket)}
    pins = _bucket_pin_counter(database, monkeypatch)
    observed = dict(database.exact._candidate_groups_unchecked(keys))
    assert observed == expected
    assert Counter(pins) == Counter({page: 1 for page in pages})


@pytest.mark.parametrize("unstable", [False, True])
def test_native_grouped_read_retries_entire_view_without_partial_answers(monkeypatch, unstable):
    database, _ = _populated(24)
    keys = _keys(database, 24)
    expected = _scalar(database, keys, SnapshotDouble(BORN))
    finish = HashIndex.finish_exact_read
    attempts = []
    pins = _bucket_pin_counter(database, monkeypatch)

    def changed(store, certificate, lsn):
        stable = finish(store, certificate, lsn)
        attempts.append(stable)
        return False if unstable or len(attempts) == 1 else stable

    monkeypatch.setattr(HashIndex, "finish_exact_read", changed)
    if unstable:
        with pytest.raises(GrafxIndexError) as error:
            database.manager.validated_versions_many(database.exact, keys, SnapshotDouble(BORN))
        assert error.value.details["field"] == "index_view_changed"
        assert len(attempts) == INDEX_READ_RETRY_BUDGET + 1
    else:
        assert database.manager.validated_versions_many(
            database.exact, keys, SnapshotDouble(BORN)) == expected
        assert len(attempts) == 2
    assert pins
    assert set(Counter(pins).values()) == {len(attempts)}


@pytest.mark.parametrize("damage", ["entry", "cycle"])
def test_grouped_scan_refuses_unrequested_corrupt_entry_and_chain_cycle(damage):
    database, _ = _populated(60)
    entries = database.exact.walk()
    bucket = bucket_of(entries[0].key, TEST_BUCKET_COUNT)
    candidates = [entry for entry in entries if bucket_of(entry.key, TEST_BUCKET_COUNT) == bucket]
    wanted = [entry.key for entry in candidates[:2]]
    assert len(candidates) > 2
    if damage == "entry":
        damaged = candidates[-1]
        assert damaged.key not in wanted
        with database.pool.pinned(database.exact.file, damaged.page) as page:
            image = bytearray(page.read_slot(damaged.slot))
            image[0] |= 0x80
            page.update_slot(damaged.slot, image)
        field = "flags"
    else:
        chain = database.exact._bucket_pages(bucket)
        with database.pool.pinned(database.exact.file, chain[-1]) as page:
            page.next_page = chain[0]
        field = "cycle"
    with pytest.raises(GrafxCorruptionDetected) as error:
        database.manager.validated_versions_many(database.exact, wanted, SnapshotDouble(BORN))
    assert error.value.details["field"] == field


def test_specialized_scalar_hook_is_not_bypassed(monkeypatch):
    database, _ = _populated(24)
    keys = _keys(database, 24)
    calls = []
    original = IndexStore._candidates_unchecked

    def specialized(store, key):
        calls.append(key)
        return original(store, key)

    monkeypatch.setattr(IndexStore, "_candidates_unchecked", specialized)
    database.manager.validated_versions_many(database.exact, keys, SnapshotDouble(BORN))
    assert calls == keys
