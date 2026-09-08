"""Immutable ordered-tree bulk build, verification and bounded reverse walk."""

from __future__ import annotations

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.ids import NO_PAGE, RecordRef
from okto_grafx.domain.index.entry import IndexEntry
from okto_grafx.domain.index.ordered_keys import (
    ordered_entry_identity,
    ordered_timestamp_string_key,
)
from okto_grafx.domain.index.ordered_tree import (
    OrderedChildPointer,
    build_ordered_tree,
    decode_ordered_internal,
    seek_ordered_exact,
    verify_ordered_tree,
    walk_ordered_desc,
)
from okto_grafx.domain.model.value import Timestamp
from okto_grafx.domain.page import Page, PageType


_PAGE_SIZE = 512


def _entry(
    number: int,
    *,
    timestamp: int | None = None,
    text: str | None = None,
    ref: RecordRef | None = None,
) -> IndexEntry:
    return IndexEntry(
        key=ordered_timestamp_string_key(
            Timestamp(number if timestamp is None else timestamp),
            f"id-{number:04d}" if text is None else text,
        ),
        ref=RecordRef(number + 20, number % 7) if ref is None else ref,
        versioned=False,
    )


def _clone_with_payloads(page: Page, payloads: list[bytes]) -> Page:
    clone = Page(
        page.page_type,
        page_size=page.page_size,
        page_index=page.page_index,
        page_lsn=page.page_lsn,
    )
    for payload in payloads:
        clone.insert_slot(payload)
    return clone


def test_bulk_build_round_trip_verifies_and_walks_in_descending_order() -> None:
    entries = tuple(_entry(number) for number in range(180))
    build = build_ordered_tree(reversed(entries), page_size=_PAGE_SIZE, page_lsn=91)
    round_tripped = {
        page.page_index: Page.from_bytes(
            page.to_bytes(),
            page_size=_PAGE_SIZE,
            page_index=page.page_index,
        )
        for page in build.pages
    }

    assert build.root_page >= 3
    assert build.height >= 3
    assert tuple(page.page_index for page in build.pages) == tuple(
        range(3, 3 + len(build.pages))
    )
    report = verify_ordered_tree(
        build.root_page,
        build.height,
        round_tripped,
        expected_entry_count=len(entries),
    )
    expected = tuple(
        sorted(
            entries,
            key=lambda entry: ordered_entry_identity(entry.key, entry.ref),
            reverse=True,
        )
    )

    assert report.entry_count == len(entries)
    assert report.page_count == len(build.pages)
    assert report.leaf_pages > 1
    assert report.internal_pages > 1
    walked = tuple(walk_ordered_desc(build.root_page, build.height, round_tripped))
    assert tuple(ordered_entry_identity(entry.key, entry.ref) for entry in walked) == tuple(
        ordered_entry_identity(entry.key, entry.ref) for entry in expected
    )


def test_exclusive_logical_bound_removes_every_physical_tie_and_limit_is_lazy() -> None:
    upper_key = ordered_timestamp_string_key(Timestamp(5), "cursor")
    entries = (
        _entry(1, timestamp=4, text="z"),
        _entry(2, timestamp=5, text="before"),
        _entry(3, timestamp=5, text="cursor", ref=RecordRef(100, 1)),
        _entry(4, timestamp=5, text="cursor", ref=RecordRef(100, 2)),
        _entry(5, timestamp=5, text="later"),
        _entry(6, timestamp=6, text="a"),
    )
    build = build_ordered_tree(entries, page_size=_PAGE_SIZE)
    expected = tuple(
        sorted(
            (entry for entry in entries if entry.key < upper_key),
            key=lambda entry: ordered_entry_identity(entry.key, entry.ref),
            reverse=True,
        )
    )

    bounded = tuple(
        walk_ordered_desc(
            build.root_page,
            build.height,
            build.page_map(),
            upper_key=upper_key,
        )
    )

    assert tuple(ordered_entry_identity(entry.key, entry.ref) for entry in bounded) == tuple(
        ordered_entry_identity(entry.key, entry.ref) for entry in expected
    )
    assert all(entry.key != upper_key for entry in bounded)
    limited = tuple(
        walk_ordered_desc(
            build.root_page,
            build.height,
            build.page_map(),
            upper_key=upper_key,
            limit=2,
        )
    )
    assert tuple(ordered_entry_identity(entry.key, entry.ref) for entry in limited) == tuple(
        ordered_entry_identity(entry.key, entry.ref) for entry in expected[:2]
    )


def test_exact_seek_crosses_only_adjacent_children_that_can_share_the_key() -> None:
    shared_key = ordered_timestamp_string_key(Timestamp(5), "shared")
    tied = tuple(
        _entry(number, timestamp=5, text="shared", ref=RecordRef(1000 + number, 0))
        for number in range(90)
    )
    entries = (
        *tuple(_entry(number, timestamp=4, text=f"lower-{number}") for number in range(20)),
        *tied,
        *tuple(_entry(number + 200, timestamp=6, text=f"upper-{number}") for number in range(20)),
    )
    build = build_ordered_tree(entries, page_size=_PAGE_SIZE)

    found = seek_ordered_exact(
        build.root_page, build.height, build.page_map(), shared_key
    )

    assert tuple(ordered_entry_identity(entry.key, entry.ref) for entry in found) == tuple(
        ordered_entry_identity(entry.key, entry.ref) for entry in tied
    )
    assert seek_ordered_exact(
        build.root_page,
        build.height,
        build.page_map(),
        ordered_timestamp_string_key(Timestamp(5), "missing"),
    ) == ()


def test_empty_tree_has_no_pages_and_a_zero_height_walk() -> None:
    build = build_ordered_tree((), page_size=_PAGE_SIZE)

    assert build.root_page == NO_PAGE
    assert build.height == 0
    assert build.pages == ()
    assert verify_ordered_tree(NO_PAGE, 0, {}, expected_entry_count=0).entry_count == 0
    assert tuple(walk_ordered_desc(NO_PAGE, 0, {})) == ()


def test_builder_refuses_duplicates_versioned_entries_and_oversized_records() -> None:
    duplicate = _entry(1)
    with pytest.raises(GrafxIndexError) as repeated:
        build_ordered_tree((duplicate, duplicate), page_size=_PAGE_SIZE)
    assert repeated.value.details["field"] == "entries"

    with pytest.raises(GrafxIndexError) as versioned:
        build_ordered_tree(
            (replace(duplicate, versioned=True, born_csn=7),),
            page_size=_PAGE_SIZE,
        )
    assert versioned.value.details["field"] == "versioned"

    huge = IndexEntry(key=b"x" * 500, ref=RecordRef(1, 1), versioned=False)
    with pytest.raises(GrafxIndexError) as too_large:
        build_ordered_tree((huge,), page_size=_PAGE_SIZE)
    assert too_large.value.details["field"] == "page_size"


def test_verifier_refuses_leaf_order_separator_drift_and_missing_children() -> None:
    build = build_ordered_tree(
        tuple(_entry(number) for number in range(180)),
        page_size=_PAGE_SIZE,
    )
    pages = dict(build.page_map())
    leaf = next(
        page for page in build.pages if page.page_type == int(PageType.INDEX_ORDERED_LEAF)
    )
    payloads = [payload for _slot, payload in leaf.iter_slots()]
    pages[leaf.page_index] = _clone_with_payloads(leaf, list(reversed(payloads)))
    with pytest.raises(GrafxCorruptionDetected) as unordered:
        verify_ordered_tree(build.root_page, build.height, pages)
    assert unordered.value.details["field"] == "entry_order"

    pages = dict(build.page_map())
    root = pages[build.root_page]
    pointers = list(decode_ordered_internal(root))
    pointers[-1] = replace(pointers[-1], high_key=pointers[-1].high_key + b"\xff")
    pages[root.page_index] = _clone_with_payloads(
        root, [pointer.encode() for pointer in pointers]
    )
    with pytest.raises(GrafxCorruptionDetected) as separator:
        verify_ordered_tree(build.root_page, build.height, pages)
    assert separator.value.details["field"] in {"entry_order", "high_identity"}

    pages = dict(build.page_map())
    missing = decode_ordered_internal(pages[build.root_page])[0].child_page
    del pages[missing]
    with pytest.raises(GrafxCorruptionDetected) as absent:
        verify_ordered_tree(build.root_page, build.height, pages)
    assert absent.value.details["field"] == "child_page"


def test_internal_codec_and_page_direction_are_fail_closed() -> None:
    pointer = OrderedChildPointer(
        high_key=ordered_timestamp_string_key(Timestamp(1), "a"),
        high_ref=RecordRef(9, 2),
        child_page=7,
    )
    assert OrderedChildPointer.decode(pointer.encode()) == pointer

    with pytest.raises(GrafxCorruptionDetected) as truncated:
        OrderedChildPointer.decode(pointer.encode()[:-1])
    assert truncated.value.details["field"] == "key_length"

    parent = Page(
        int(PageType.INDEX_ORDERED_INTERNAL),
        page_size=_PAGE_SIZE,
        page_index=7,
    )
    parent.insert_slot(replace(pointer, child_page=7).encode())
    with pytest.raises(GrafxCorruptionDetected) as forward:
        decode_ordered_internal(parent)
    assert forward.value.details["field"] == "child_page"


def test_entry_count_and_height_mismatches_are_refused() -> None:
    build = build_ordered_tree(tuple(_entry(number) for number in range(40)), page_size=_PAGE_SIZE)

    with pytest.raises(GrafxCorruptionDetected) as wrong_count:
        verify_ordered_tree(
            build.root_page,
            build.height,
            build.page_map(),
            expected_entry_count=41,
        )
    assert wrong_count.value.details["field"] == "entry_count"

    with pytest.raises(GrafxCorruptionDetected) as wrong_height:
        verify_ordered_tree(build.root_page, build.height + 1, build.page_map())
    assert wrong_height.value.details["field"] == "page_type"


@pytest.mark.parametrize("page_size", [512, 1024])
def test_bulk_tree_shapes_preserve_every_physical_identity(page_size: int) -> None:
    for count in (1, 2, 7, 17, 80, 257):
        entries = tuple(
            _entry(
                number,
                timestamp=number // 9,
                text=f"tie-{number % 4}",
                ref=RecordRef(1000 + number, number % 11),
            )
            for number in range(count)
        )
        build = build_ordered_tree(
            reversed(entries),
            page_size=page_size,
            start_page=11,
            page_lsn=73,
        )
        report = verify_ordered_tree(
            build.root_page,
            build.height,
            build.page_map(),
            expected_entry_count=count,
        )
        walked = tuple(
            ordered_entry_identity(entry.key, entry.ref)
            for entry in walk_ordered_desc(
                build.root_page, build.height, build.page_map()
            )
        )
        expected = tuple(
            sorted(
                (
                    ordered_entry_identity(entry.key, entry.ref)
                    for entry in entries
                ),
                reverse=True,
            )
        )

        assert report.entry_count == count
        assert walked == expected
