"""Immutable ordered-tree pages, bulk construction, verification and bounded reverse walk."""

from __future__ import annotations

import struct
from bisect import bisect_left
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.ids import (
    NO_LSN,
    NO_PAGE,
    PROVISIONAL_CSN,
    Lsn,
    PageIndex,
    RecordRef,
)
from okto_grafx.domain.index.entry import MAX_INDEX_KEY_BYTES, IndexEntry
from okto_grafx.domain.index.ordered_keys import ordered_entry_identity
from okto_grafx.domain.index.ordered_root import FIRST_ORDERED_TREE_PAGE
from okto_grafx.domain.page import DEFAULT_PAGE_SIZE, Page, PageType, validate_page_size

__all__ = [
    "ORDERED_INTERNAL_ENTRY_SIZE",
    "OrderedChildPointer",
    "OrderedTreeBuild",
    "OrderedTreeVerification",
    "build_ordered_tree",
    "decode_ordered_internal",
    "decode_ordered_leaf",
    "verify_ordered_tree",
    "walk_ordered_desc",
]

_INTERNAL = struct.Struct("<HIQ")
ORDERED_INTERNAL_ENTRY_SIZE: int = _INTERNAL.size


def _key_bytes(value: object, *, stored: bool) -> bytes:
    """Return a bounded, non-empty key under the caller/stored error taxonomy."""

    error_type = GrafxCorruptionDetected if stored else GrafxIndexError
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise error_type(
            "An ordered tree key must be bytes.",
            field="key",
            value=type(value).__name__,
        )
    key = bytes(value)
    if not key or len(key) > MAX_INDEX_KEY_BYTES:
        raise error_type(
            f"An ordered tree key must contain 1..{MAX_INDEX_KEY_BYTES} bytes; got "
            f"{len(key)}.",
            field="key",
            value=len(key),
        )
    return key


@dataclass(frozen=True, slots=True)
class OrderedChildPointer:
    """The inclusive high identity and page of one ordered internal child."""

    high_key: bytes
    high_ref: RecordRef
    child_page: PageIndex

    def __post_init__(self) -> None:
        """Refuse an unaddressable child or malformed high identity."""

        object.__setattr__(self, "high_key", _key_bytes(self.high_key, stored=False))
        if not isinstance(self.high_ref, RecordRef):
            raise GrafxIndexError(
                "An ordered child high identity requires a RecordRef.",
                field="high_ref",
                value=type(self.high_ref).__name__,
            )
        self.high_ref.encode()
        if (
            isinstance(self.child_page, bool)
            or not isinstance(self.child_page, int)
            or not FIRST_ORDERED_TREE_PAGE <= self.child_page < NO_PAGE
        ):
            raise GrafxIndexError(
                "An ordered child must point at an allocated tree page.",
                field="child_page",
                value=repr(self.child_page),
            )

    @property
    def high_identity(self) -> tuple[bytes, int]:
        """Return the child's inclusive high identity."""

        return ordered_entry_identity(self.high_key, self.high_ref)

    def encode(self) -> bytes:
        """Return the length-delimited internal-slot image."""

        return _INTERNAL.pack(
            len(self.high_key), self.child_page, self.high_ref.encode()
        ) + self.high_key

    @classmethod
    def decode(cls, raw: bytes) -> OrderedChildPointer:
        """Decode one stored child pointer without leaking constructor errors."""

        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise GrafxCorruptionDetected(
                "An ordered internal entry must be bytes.",
                field="ordered_internal_entry",
                value=type(raw).__name__,
            )
        image = bytes(raw)
        if len(image) < ORDERED_INTERNAL_ENTRY_SIZE:
            raise GrafxCorruptionDetected(
                "An ordered internal entry is shorter than its fixed header.",
                field="ordered_internal_entry",
                value=len(image),
            )
        key_length, child_page, encoded_ref = _INTERNAL.unpack_from(image, 0)
        if len(image) != ORDERED_INTERNAL_ENTRY_SIZE + key_length:
            raise GrafxCorruptionDetected(
                "An ordered internal entry length disagrees with its key length.",
                field="key_length",
                value=key_length,
                length=len(image),
            )
        key = _key_bytes(image[ORDERED_INTERNAL_ENTRY_SIZE:], stored=True)
        try:
            return cls(
                high_key=key,
                high_ref=RecordRef.decode(encoded_ref),
                child_page=child_page,
            )
        except (GrafxCorruptionDetected, GrafxIndexError) as failure:
            raise GrafxCorruptionDetected(
                f"A stored ordered child pointer is invalid: {failure.message}",
                field=str(failure.details.get("field", "ordered_internal_entry")),
                value=failure.details.get("value"),
            ) from failure


@dataclass(frozen=True, slots=True)
class OrderedTreeBuild:
    """The contiguous immutable pages produced by one bulk build."""

    root_page: PageIndex
    height: int
    entry_count: int
    pages: tuple[Page, ...]

    def page_map(self) -> Mapping[PageIndex, Page]:
        """Return the built pages keyed by their durable page index."""

        return {page.page_index: page for page in self.pages}


@dataclass(frozen=True, slots=True)
class OrderedTreeVerification:
    """Counts and boundary identities proved for one selected root."""

    entry_count: int
    page_count: int
    leaf_pages: int
    internal_pages: int
    minimum: tuple[bytes, int] | None
    maximum: tuple[bytes, int] | None


@dataclass(frozen=True, slots=True)
class _Subtree:
    page: PageIndex
    minimum: tuple[bytes, int]
    maximum: tuple[bytes, int]
    entry_count: int


def _validate_page_shape(page: Page, expected: PageType) -> None:
    """Validate immutable ordered-page header invariants shared by readers."""

    if not isinstance(page, Page):
        raise GrafxCorruptionDetected(
            "An ordered tree page loader returned the wrong value type.",
            field="page",
            value=type(page).__name__,
        )
    if page.page_type != int(expected):
        raise GrafxCorruptionDetected(
            f"Ordered page {page.page_index} has type {page.page_type}, expected "
            f"{int(expected)}.",
            field="page_type",
            value=page.page_type,
            page=page.page_index,
        )
    if page.flags != 0 or page.next_page != NO_PAGE or page.header().reserved != 0:
        raise GrafxCorruptionDetected(
            f"Ordered page {page.page_index} carries unsupported header metadata.",
            field="page_header",
            page=page.page_index,
            flags=page.flags,
            next_page=page.next_page,
            reserved=page.header().reserved,
        )
    if page.seq & 1:
        raise GrafxCorruptionDetected(
            f"Ordered page {page.page_index} carries an in-progress sequence.",
            field="seq",
            value=page.seq,
            page=page.page_index,
        )
    if page.slot_count == 0:
        raise GrafxCorruptionDetected(
            f"A reachable ordered page {page.page_index} is empty.",
            field="slot_count",
            value=0,
            page=page.page_index,
        )


def decode_ordered_leaf(page: Page) -> tuple[IndexEntry, ...]:
    """Decode and prove one strictly ordered exact leaf page."""

    _validate_page_shape(page, PageType.INDEX_ORDERED_LEAF)
    entries: list[IndexEntry] = []
    previous: tuple[bytes, int] | None = None
    for slot, raw in page.iter_slots():
        entry = IndexEntry.decode(raw).located_at(page.page_index, slot)
        if entry.versioned:
            raise GrafxCorruptionDetected(
                "An ordered exact leaf contains a versioned entry.",
                field="versioned",
                value=True,
                page=page.page_index,
                slot=slot,
            )
        _key_bytes(entry.key, stored=True)
        identity = ordered_entry_identity(entry.key, entry.ref)
        if previous is not None and identity <= previous:
            raise GrafxCorruptionDetected(
                "An ordered leaf is not strictly increasing by key and RecordRef.",
                field="entry_order",
                page=page.page_index,
                slot=slot,
            )
        previous = identity
        entries.append(entry)
    return tuple(entries)


def decode_ordered_internal(page: Page) -> tuple[OrderedChildPointer, ...]:
    """Decode and prove one strictly ordered internal page."""

    _validate_page_shape(page, PageType.INDEX_ORDERED_INTERNAL)
    pointers: list[OrderedChildPointer] = []
    previous: tuple[bytes, int] | None = None
    children: set[PageIndex] = set()
    for slot, raw in page.iter_slots():
        pointer = OrderedChildPointer.decode(raw)
        if pointer.child_page >= page.page_index:
            raise GrafxCorruptionDetected(
                "An append-only ordered parent must follow every child it references.",
                field="child_page",
                value=pointer.child_page,
                page=page.page_index,
                slot=slot,
            )
        identity = pointer.high_identity
        if previous is not None and identity <= previous:
            raise GrafxCorruptionDetected(
                "Ordered internal high identities are not strictly increasing.",
                field="entry_order",
                page=page.page_index,
                slot=slot,
            )
        if pointer.child_page in children:
            raise GrafxCorruptionDetected(
                "An ordered internal page references one child more than once.",
                field="child_page",
                value=pointer.child_page,
                page=page.page_index,
                slot=slot,
            )
        previous = identity
        children.add(pointer.child_page)
        pointers.append(pointer)
    return tuple(pointers)


def _new_page(page_type: PageType, page_size: int, page_index: int, page_lsn: int) -> Page:
    return Page(
        int(page_type),
        page_size=page_size,
        page_index=page_index,
        page_lsn=page_lsn,
    )


def _next_page_index(start_page: int, pages: list[Page]) -> int:
    page_index = start_page + len(pages)
    if page_index >= NO_PAGE:
        raise GrafxIndexError(
            "An ordered tree exhausted the encodable page-index space.",
            field="page_index",
            value=page_index,
        )
    return page_index


def _append_packed_pages(
    payloads: Iterable[tuple[bytes, tuple[bytes, int]]],
    *,
    page_type: PageType,
    page_size: int,
    start_page: int,
    page_lsn: int,
    pages: list[Page],
) -> list[_Subtree]:
    """Pack ordered payloads greedily and return one high-key summary per page."""

    summaries: list[_Subtree] = []
    current: Page | None = None
    first: tuple[bytes, int] | None = None
    last: tuple[bytes, int] | None = None
    count = 0

    def publish() -> None:
        nonlocal current, first, last, count
        if current is None or first is None or last is None:
            return
        summaries.append(
            _Subtree(current.page_index, first, last, count)
        )
        current = None
        first = None
        last = None
        count = 0

    for payload, identity in payloads:
        if current is None:
            current = _new_page(
                page_type,
                page_size,
                _next_page_index(start_page, pages),
                page_lsn,
            )
            pages.append(current)
        if not current.can_fit(len(payload)):
            if current.slot_count == 0:
                raise GrafxIndexError(
                    "One ordered-tree record does not fit in an empty page.",
                    field="page_size",
                    value=page_size,
                    record_size=len(payload),
                )
            publish()
            current = _new_page(
                page_type,
                page_size,
                _next_page_index(start_page, pages),
                page_lsn,
            )
            pages.append(current)
            if not current.can_fit(len(payload)):
                raise GrafxIndexError(
                    "One ordered-tree record does not fit in an empty page.",
                    field="page_size",
                    value=page_size,
                    record_size=len(payload),
                )
        current.insert_slot(payload)
        first = identity if first is None else first
        last = identity
        count += 1
    publish()
    return summaries


def build_ordered_tree(
    entries: Iterable[IndexEntry],
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    start_page: PageIndex = FIRST_ORDERED_TREE_PAGE,
    page_lsn: Lsn = NO_LSN,
) -> OrderedTreeBuild:
    """Bulk-build a contiguous append-only tree from exact entries."""

    validate_page_size(page_size)
    if (
        isinstance(page_lsn, bool)
        or not isinstance(page_lsn, int)
        or not NO_LSN <= page_lsn < PROVISIONAL_CSN
    ):
        raise GrafxIndexError(
            "An ordered tree page LSN must be a non-provisional unsigned position.",
            field="page_lsn",
            value=repr(page_lsn),
        )
    if (
        isinstance(start_page, bool)
        or not isinstance(start_page, int)
        or not FIRST_ORDERED_TREE_PAGE <= start_page < NO_PAGE
    ):
        raise GrafxIndexError(
            "An ordered bulk build must start in the tree-page range.",
            field="start_page",
            value=repr(start_page),
        )
    try:
        materialized = tuple(entries)
    except TypeError as failure:
        raise GrafxIndexError(
            "An ordered bulk build needs an iterable of exact index entries.",
            field="entries",
            value=type(entries).__name__,
        ) from failure
    for entry in materialized:
        if not isinstance(entry, IndexEntry):
            raise GrafxIndexError(
                "An ordered bulk build accepts only IndexEntry values.",
                field="entries",
                value=type(entry).__name__,
            )
        if entry.versioned:
            raise GrafxIndexError(
                "An ordered exact tree cannot contain a versioned entry.",
                field="versioned",
                value=True,
            )
        _key_bytes(entry.key, stored=False)
    ordered = tuple(sorted(materialized, key=lambda entry: ordered_entry_identity(entry.key, entry.ref)))
    for before, after in zip(ordered, ordered[1:], strict=False):
        if ordered_entry_identity(before.key, before.ref) == ordered_entry_identity(
            after.key, after.ref
        ):
            raise GrafxIndexError(
                "An ordered bulk build contains a duplicate key/reference identity.",
                field="entries",
                key=before.key.hex(),
                ref=before.ref.encode(),
            )
    if not ordered:
        return OrderedTreeBuild(NO_PAGE, 0, 0, ())

    pages: list[Page] = []
    level = _append_packed_pages(
        (
            (entry.encode(), ordered_entry_identity(entry.key, entry.ref))
            for entry in ordered
        ),
        page_type=PageType.INDEX_ORDERED_LEAF,
        page_size=page_size,
        start_page=start_page,
        page_lsn=page_lsn,
        pages=pages,
    )
    height = 1
    while len(level) > 1:
        previous_count = len(level)
        pointers = (
            OrderedChildPointer(
                high_key=summary.maximum[0],
                high_ref=RecordRef.decode(summary.maximum[1]),
                child_page=summary.page,
            )
            for summary in level
        )
        next_level = _append_packed_pages(
            ((pointer.encode(), pointer.high_identity) for pointer in pointers),
            page_type=PageType.INDEX_ORDERED_INTERNAL,
            page_size=page_size,
            start_page=start_page,
            page_lsn=page_lsn,
            pages=pages,
        )
        if len(next_level) >= previous_count:
            raise GrafxIndexError(
                "Ordered internal pages cannot reduce this level at the configured page size.",
                field="page_size",
                value=page_size,
            )
        # Internal summaries counted child pointers; their subtree count is the sum below.
        rebuilt: list[_Subtree] = []
        cursor = 0
        for parent in next_level:
            child_count = parent.entry_count
            children = level[cursor : cursor + child_count]
            rebuilt.append(
                _Subtree(
                    page=parent.page,
                    minimum=children[0].minimum,
                    maximum=children[-1].maximum,
                    entry_count=sum(child.entry_count for child in children),
                )
            )
            cursor += child_count
        level = rebuilt
        height += 1
    return OrderedTreeBuild(level[0].page, height, len(ordered), tuple(pages))


def verify_ordered_tree(
    root_page: PageIndex,
    height: int,
    pages: Mapping[PageIndex, Page],
    *,
    expected_entry_count: int | None = None,
) -> OrderedTreeVerification:
    """Verify every page reachable from one immutable ordered root."""

    if root_page == NO_PAGE:
        if height != 0 or expected_entry_count not in (None, 0):
            raise GrafxCorruptionDetected(
                "An empty ordered root disagrees with its height or entry count.",
                field="root_page",
                value=root_page,
                height=height,
                entry_count=expected_entry_count,
            )
        return OrderedTreeVerification(0, 0, 0, 0, None, None)
    if (
        isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
        or root_page < FIRST_ORDERED_TREE_PAGE
    ):
        raise GrafxCorruptionDetected(
            "A non-empty ordered root requires a positive height and tree page.",
            field="height",
            value=repr(height),
            root_page=root_page,
        )

    visiting: set[PageIndex] = set()
    visited: set[PageIndex] = set()
    leaf_pages = 0
    internal_pages = 0

    def visit(page_index: PageIndex, depth: int) -> _Subtree:
        nonlocal leaf_pages, internal_pages
        if page_index in visiting or page_index in visited:
            raise GrafxCorruptionDetected(
                "An ordered tree contains a cycle or shared child.",
                field="child_page",
                value=page_index,
            )
        page = pages.get(page_index)
        if page is None:
            raise GrafxCorruptionDetected(
                "An ordered tree references an absent page.",
                field="child_page",
                value=page_index,
            )
        if page.page_index != page_index:
            raise GrafxCorruptionDetected(
                "An ordered page is stored under the wrong page identity.",
                field="page_index",
                value=page.page_index,
                expected=page_index,
            )
        visiting.add(page_index)
        if depth == 1:
            entries = decode_ordered_leaf(page)
            identities = tuple(
                ordered_entry_identity(entry.key, entry.ref) for entry in entries
            )
            result = _Subtree(
                page_index, identities[0], identities[-1], len(identities)
            )
            leaf_pages += 1
        else:
            pointers = decode_ordered_internal(page)
            children: list[_Subtree] = []
            previous_maximum: tuple[bytes, int] | None = None
            for pointer in pointers:
                child = visit(pointer.child_page, depth - 1)
                if child.maximum != pointer.high_identity:
                    raise GrafxCorruptionDetected(
                        "An ordered child maximum disagrees with its parent separator.",
                        field="high_identity",
                        page=page_index,
                        child_page=pointer.child_page,
                    )
                if previous_maximum is not None and child.minimum <= previous_maximum:
                    raise GrafxCorruptionDetected(
                        "Adjacent ordered child ranges overlap or regress.",
                        field="child_range",
                        page=page_index,
                        child_page=pointer.child_page,
                    )
                previous_maximum = child.maximum
                children.append(child)
            result = _Subtree(
                page_index,
                children[0].minimum,
                children[-1].maximum,
                sum(child.entry_count for child in children),
            )
            internal_pages += 1
        visiting.remove(page_index)
        visited.add(page_index)
        return result

    root = visit(root_page, height)
    if expected_entry_count is not None and root.entry_count != expected_entry_count:
        raise GrafxCorruptionDetected(
            "The ordered root entry count disagrees with its reachable leaves.",
            field="entry_count",
            value=expected_entry_count,
            actual=root.entry_count,
        )
    return OrderedTreeVerification(
        entry_count=root.entry_count,
        page_count=len(visited),
        leaf_pages=leaf_pages,
        internal_pages=internal_pages,
        minimum=root.minimum,
        maximum=root.maximum,
    )


def walk_ordered_desc(
    root_page: PageIndex,
    height: int,
    pages: Mapping[PageIndex, Page],
    *,
    upper_key: bytes | None = None,
    limit: int | None = None,
) -> Iterator[IndexEntry]:
    """Yield entries below an optional logical key in strict descending tree order."""

    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
    ):
        raise GrafxIndexError(
            "An ordered walk limit must be a non-negative integer or None.",
            field="limit",
            value=repr(limit),
        )
    bound = _key_bytes(upper_key, stored=False) if upper_key is not None else None
    if root_page == NO_PAGE:
        if height != 0:
            raise GrafxCorruptionDetected(
                "An empty ordered root declares a non-zero height.",
                field="height",
                value=height,
            )
        return
    if height <= 0:
        raise GrafxCorruptionDetected(
            "A non-empty ordered walk requires a positive height.",
            field="height",
            value=height,
        )
    if limit == 0:
        return

    visiting: set[PageIndex] = set()
    emitted = 0

    def descend(page_index: PageIndex, depth: int, ceiling: bytes | None) -> Iterator[IndexEntry]:
        nonlocal emitted
        if page_index in visiting:
            raise GrafxCorruptionDetected(
                "An ordered walk encountered a child cycle.",
                field="child_page",
                value=page_index,
            )
        page = pages.get(page_index)
        if page is None or page.page_index != page_index:
            raise GrafxCorruptionDetected(
                "An ordered walk could not resolve the requested page identity.",
                field="page_index",
                value=page_index,
            )
        visiting.add(page_index)
        try:
            upper_identity = (ceiling, -1) if ceiling is not None else None
            if depth == 1:
                entries = decode_ordered_leaf(page)
                identities = [
                    ordered_entry_identity(entry.key, entry.ref) for entry in entries
                ]
                end = (
                    len(entries)
                    if upper_identity is None
                    else bisect_left(identities, upper_identity)
                )
                for position in range(end - 1, -1, -1):
                    if limit is not None and emitted >= limit:
                        return
                    emitted += 1
                    yield entries[position]
                return
            pointers = decode_ordered_internal(page)
            highs = [pointer.high_identity for pointer in pointers]
            start = (
                len(pointers) - 1
                if upper_identity is None
                else min(bisect_left(highs, upper_identity), len(pointers) - 1)
            )
            for position in range(start, -1, -1):
                if limit is not None and emitted >= limit:
                    return
                child_ceiling = ceiling if position == start else None
                yield from descend(
                    pointers[position].child_page, depth - 1, child_ceiling
                )
        finally:
            visiting.remove(page_index)

    yield from descend(root_page, height, bound)
