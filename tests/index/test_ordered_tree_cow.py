"""Batch copy-on-write mutation of immutable ordered trees."""

from __future__ import annotations

import random

import pytest

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.ids import NO_PAGE, RecordRef
from okto_grafx.domain.index import (
    IndexChange,
    IndexEntry,
    IndexOperation,
    build_ordered_tree,
    mutate_ordered_tree,
    ordered_entry_identity,
    ordered_timestamp_string_key,
    verify_ordered_tree,
    walk_ordered_desc,
)
from okto_grafx.domain.model.value import Timestamp


PAGE_SIZE = 512


def _entry(number: int, *, ref_offset: int = 0) -> IndexEntry:
    return IndexEntry(
        key=ordered_timestamp_string_key(
            Timestamp(number // 4), f"id-{number:05d}"
        ),
        ref=RecordRef(100 + number + ref_offset, number % 9),
        versioned=False,
    )


def _change(operation: IndexOperation, entry: IndexEntry, *, csn: int = 70) -> IndexChange:
    return IndexChange(
        index="ordered_Event",
        operation=operation,
        key=entry.key,
        ref=entry.ref,
        csn=csn if operation is not IndexOperation.INSERT else 0,
    )


def _combined(build, mutation):  # type: ignore[no-untyped-def]
    pages = dict(build.page_map())
    pages.update(mutation.page_map())
    return pages


def test_cow_batch_changes_only_affected_paths_and_preserves_complete_order() -> None:
    original = tuple(_entry(number) for number in range(600))
    build = build_ordered_tree(original, page_size=PAGE_SIZE, page_lsn=40)
    inserted = _entry(900)
    tombstoned = original[305]
    removed = original[7]
    missing = _entry(1200)
    changes = (
        _change(IndexOperation.INSERT, inserted),
        _change(IndexOperation.TOMBSTONE, tombstoned),
        _change(IndexOperation.REMOVE, removed),
        _change(IndexOperation.REMOVE, missing),
    )

    mutation = mutate_ordered_tree(
        build.root_page,
        build.height,
        build.entry_count,
        build.page_map(),
        changes,
        page_size=PAGE_SIZE,
        start_page=3 + len(build.pages),
        page_lsn=80,
    )
    pages = _combined(build, mutation)
    report = verify_ordered_tree(
        mutation.root_page,
        mutation.height,
        pages,
        expected_entry_count=len(original),
    )
    walked = tuple(
        walk_ordered_desc(mutation.root_page, mutation.height, pages)
    )
    by_identity = {
        ordered_entry_identity(entry.key, entry.ref): entry for entry in original
    }
    del by_identity[ordered_entry_identity(removed.key, removed.ref)]
    by_identity[ordered_entry_identity(inserted.key, inserted.ref)] = inserted
    ended = tombstoned.ended_at(70)
    by_identity[ordered_entry_identity(ended.key, ended.ref)] = ended
    expected = tuple(by_identity[key] for key in sorted(by_identity, reverse=True))

    assert mutation.changed
    assert mutation.entry_count == len(original)
    assert mutation.missing_targets == 1
    assert report.entry_count == len(original)
    assert report.page_count > len(mutation.pages)
    assert len(mutation.pages) < len(build.pages)
    assert [entry.encode() for entry in walked] == [entry.encode() for entry in expected]
    assert all(page.page_lsn == 80 for page in mutation.pages)
    assert tuple(page.page_index for page in mutation.pages) == tuple(
        range(3 + len(build.pages), 3 + len(build.pages) + len(mutation.pages))
    )


def test_many_changes_in_one_leaf_copy_the_path_once_for_the_batch() -> None:
    original = tuple(_entry(number) for number in range(180))
    build = build_ordered_tree(original, page_size=PAGE_SIZE)
    additions = tuple(_entry(1000 + number) for number in range(20))

    mutation = mutate_ordered_tree(
        build.root_page,
        build.height,
        build.entry_count,
        build.page_map(),
        tuple(_change(IndexOperation.INSERT, entry) for entry in additions),
        page_size=PAGE_SIZE,
        start_page=3 + len(build.pages),
        page_lsn=90,
    )

    assert mutation.entry_count == len(original) + len(additions)
    assert len(mutation.pages) <= (build.height * 2) + 3
    verify_ordered_tree(
        mutation.root_page,
        mutation.height,
        _combined(build, mutation),
        expected_entry_count=mutation.entry_count,
    )


def test_idempotent_batch_allocates_no_page_and_empty_tree_obeys_sequential_effects() -> None:
    entry = _entry(4)
    build = build_ordered_tree((entry,), page_size=PAGE_SIZE)
    noop = mutate_ordered_tree(
        build.root_page,
        build.height,
        1,
        build.page_map(),
        (
            _change(IndexOperation.INSERT, entry),
            _change(IndexOperation.TOMBSTONE, _entry(99)),
        ),
        page_size=PAGE_SIZE,
        start_page=4,
        page_lsn=10,
    )
    assert not noop.changed
    assert noop.pages == ()
    assert noop.root_page == build.root_page
    assert noop.missing_targets == 1

    born = _entry(8)
    empty = mutate_ordered_tree(
        NO_PAGE,
        0,
        0,
        {},
        (
            _change(IndexOperation.INSERT, born),
            _change(IndexOperation.TOMBSTONE, born, csn=12),
        ),
        page_size=PAGE_SIZE,
        start_page=3,
        page_lsn=12,
    )
    walked = tuple(
        walk_ordered_desc(empty.root_page, empty.height, empty.page_map())
    )
    assert len(walked) == 1
    assert walked[0].dead_csn == 12

    canceled = mutate_ordered_tree(
        NO_PAGE,
        0,
        0,
        {},
        (
            _change(IndexOperation.INSERT, born),
            _change(IndexOperation.REMOVE, born),
        ),
        page_size=PAGE_SIZE,
        start_page=3,
        page_lsn=12,
    )
    assert canceled.root_page == NO_PAGE
    assert canceled.pages == ()
    assert not canceled.changed


def test_randomized_cow_batches_match_an_in_memory_identity_model() -> None:
    rng = random.Random(9404)
    original = tuple(_entry(number) for number in range(140))
    build = build_ordered_tree(original, page_size=PAGE_SIZE)
    model = {
        ordered_entry_identity(entry.key, entry.ref): entry for entry in original
    }
    changes: list[IndexChange] = []
    candidates = list(original) + [_entry(500 + number) for number in range(80)]
    for step in range(220):
        entry = rng.choice(candidates)
        operation = rng.choice(
            (IndexOperation.INSERT, IndexOperation.TOMBSTONE, IndexOperation.REMOVE)
        )
        change = _change(operation, entry, csn=100 + step)
        changes.append(change)
        identity = ordered_entry_identity(entry.key, entry.ref)
        existing = model.get(identity)
        if operation is IndexOperation.INSERT:
            if existing is None:
                model[identity] = entry
        elif operation is IndexOperation.TOMBSTONE:
            if existing is not None and existing.live:
                model[identity] = existing.ended_at(100 + step)
        elif existing is not None:
            del model[identity]

    mutation = mutate_ordered_tree(
        build.root_page,
        build.height,
        build.entry_count,
        build.page_map(),
        changes,
        page_size=PAGE_SIZE,
        start_page=3 + len(build.pages),
        page_lsn=400,
    )
    pages = _combined(build, mutation)
    walked = tuple(
        walk_ordered_desc(mutation.root_page, mutation.height, pages)
    )
    expected = tuple(model[key] for key in sorted(model, reverse=True))

    verify_ordered_tree(
        mutation.root_page,
        mutation.height,
        pages,
        expected_entry_count=len(expected),
    )
    assert [entry.encode() for entry in walked] == [entry.encode() for entry in expected]


def test_cow_refuses_reset_and_versioned_changes_before_allocating() -> None:
    entry = _entry(1)
    reset = IndexChange(index="ordered_Event", operation=IndexOperation.RESET)
    with pytest.raises(GrafxIndexError) as reset_error:
        mutate_ordered_tree(
            NO_PAGE,
            0,
            0,
            {},
            (reset,),
            page_size=PAGE_SIZE,
            start_page=3,
            page_lsn=1,
        )
    assert reset_error.value.details["field"] == "operation"

    versioned = IndexChange(
        index="ordered_Event",
        operation=IndexOperation.INSERT,
        key=entry.key,
        ref=entry.ref,
        csn=2,
        versioned=True,
    )
    with pytest.raises(GrafxIndexError) as versioned_error:
        mutate_ordered_tree(
            NO_PAGE,
            0,
            0,
            {},
            (versioned,),
            page_size=PAGE_SIZE,
            start_page=3,
            page_lsn=2,
        )
    assert versioned_error.value.details["field"] == "versioned"
