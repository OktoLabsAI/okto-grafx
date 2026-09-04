"""Focused equivalence and complexity checks for empty index builds (INDEX-1)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index import IndexChange, IndexOperation, bucket_of
from okto_grafx.engine.index_manager import IndexStore, _EmptyIndexBuild

from .conftest import Database, TransactionDouble

BUILD_LSN = 100


def _colliding_keys(database: Database, count: int) -> list[bytes]:
    """Return deterministic, differently-sized keys that all belong to bucket zero."""
    keys: list[bytes] = []
    candidate = 0
    while len(keys) < count:
        width = 3 + candidate % 29
        key = f"{candidate:08d}".encode().ljust(width, b"x")
        candidate += 1
        if bucket_of(key, database.exact.definition.bucket_count) == 0:
            keys.append(key)
    return keys


def _commit_empty_rebuild(
    database: Database,
    keys: Sequence[bytes],
    *,
    tombstone_every: int = 0,
    repeat_changes: bool = False,
) -> None:
    """Reset and rebuild the exact index through the public staging/commit path."""
    index = database.exact
    token = index._claim_rebuild("INDEX-1 focused empty-build test")
    txn = TransactionDouble(txn_id=991)
    index.stage_reset(txn, BUILD_LSN, rebuild_token=token)
    for number, key in enumerate(keys, start=1):
        ref = RecordRef(number, number % 7)
        index.stage_insert(txn, key, ref, 0)
        if tombstone_every and number % tombstone_every == 0:
            index.stage_delete(txn, key, ref, BUILD_LSN - 1)
            if repeat_changes:
                index.stage_delete(txn, key, ref, BUILD_LSN - 1)
        if repeat_changes and number % 11 == 0:
            index.stage_insert(txn, key, ref, 0)
    index.commit(txn, BUILD_LSN)


def _page_images(database: Database) -> tuple[bytes, ...]:
    """Return every durable page image of the exact index in physical order."""
    file = database.exact.file
    database.pool.flush(file)
    return tuple(
        database.device.raw_page(file, page)
        for page in range(database.device.page_count(file))
    )


def test_empty_build_matches_canonical_entries_lookups_and_page_images(
    monkeypatch: Any,
) -> None:
    """The ephemeral directory preserves first-fit order, duplicates and tombstones exactly."""
    accelerated = Database(budget_pages=256)
    keys = _colliding_keys(accelerated, 90)
    _commit_empty_rebuild(
        accelerated, keys, tombstone_every=5, repeat_changes=True
    )

    canonical = Database(budget_pages=256)
    with monkeypatch.context() as comparison:
        comparison.setattr(
            IndexStore,
            "_apply_empty_build_change",
            lambda *_args, **_kwargs: None,
        )
        _commit_empty_rebuild(
            canonical, keys, tombstone_every=5, repeat_changes=True
        )

    accelerated_entries = tuple(
        (entry.key, entry.ref, entry.born_csn, entry.dead_csn)
        for entry in accelerated.exact.walk()
    )
    canonical_entries = tuple(
        (entry.key, entry.ref, entry.born_csn, entry.dead_csn)
        for entry in canonical.exact.walk()
    )
    assert accelerated_entries == canonical_entries
    for key in (*keys, b"missing"):
        assert accelerated.exact.candidates(key) == canonical.exact.candidates(key)
    assert accelerated.exact.missing_targets == canonical.exact.missing_targets
    assert _page_images(accelerated) == _page_images(canonical)


def test_empty_build_bucket_walks_are_constant_instead_of_per_insert(
    monkeypatch: Any,
) -> None:
    """After RESET, one bucket directory replaces the quadratic chain walk for every row."""
    original_pages = IndexStore._bucket_pages
    original_matches = IndexStore._matching_entries_on

    def measured(count: int, *, canonical: bool) -> tuple[int, int]:
        database = Database(budget_pages=256)
        keys = _colliding_keys(database, count)
        calls = {"pages": 0, "matches": 0}

        def counted_pages(store: IndexStore, bucket: int) -> tuple[int, ...]:
            if store is database.exact and bucket == 0:
                calls["pages"] += 1
            return original_pages(store, bucket)

        def counted_matches(
            store: IndexStore, page: int, key: bytes, ref: RecordRef | None = None
        ) -> tuple[object, ...]:
            if store is database.exact:
                calls["matches"] += 1
            return original_matches(store, page, key, ref)

        with monkeypatch.context() as measurement:
            measurement.setattr(IndexStore, "_bucket_pages", counted_pages)
            measurement.setattr(IndexStore, "_matching_entries_on", counted_matches)
            if canonical:
                measurement.setattr(
                    IndexStore,
                    "_apply_empty_build_change",
                    lambda *_args, **_kwargs: None,
                )
            _commit_empty_rebuild(database, keys)
        return calls["pages"], calls["matches"]

    fast_curve = [measured(count, canonical=False) for count in (32, 64, 128)]
    canonical = measured(128, canonical=True)

    assert fast_curve == [(2, 0), (2, 0), (2, 0)]
    assert canonical[0] == 129
    assert canonical[1] > 500


def test_empty_build_declines_to_canonical_path_if_a_bucket_is_not_empty() -> None:
    """The accelerator is never trusted when its empty-source proof does not hold."""
    database = Database(budget_pages=32)
    key = _colliding_keys(database, 2)
    first = IndexChange(
        index=database.exact.name,
        operation=IndexOperation.INSERT,
        key=key[0],
        ref=RecordRef(1, 1),
    )
    second = IndexChange(
        index=database.exact.name,
        operation=IndexOperation.INSERT,
        key=key[1],
        ref=RecordRef(2, 1),
    )
    database.exact._apply_change(first, 1)
    build = _EmptyIndexBuild()

    assert database.exact._apply_empty_build_change(build, second, 2) is None
    assert not build.valid
    assert database.exact._apply_change(second, 2)
    assert {entry.ref for entry in database.exact.candidates(key[0])} == {
        first.ref
    }
    assert {entry.ref for entry in database.exact.candidates(key[1])} == {
        second.ref
    }
