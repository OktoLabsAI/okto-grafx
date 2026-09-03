"""The slotted page: the working representation of one page of any paged file.

A page is a header, a payload area that grows up from the header, and a slot directory that
grows down from the end of the page. A slot is a stable name for a payload inside the page: it
is the low half of a RecordRef, so it may never be renumbered and it may never be reused. That
single rule decides the rest of the design:

* freeing a slot marks its directory entry dead and zeroes its payload, but keeps its position;
* compacting closes the gaps and rewrites the offsets, but keeps every slot id where it was;
* a payload that does not fit raises PageFullError instead of silently moving to another page,
  because only the caller knows whether the answer is a new page or an overflow chain.

The page owns no bytes of any file. It is decoded from a page image, mutated in memory and
encoded back into a page image of exactly page_size bytes, with the CRC-32C of everything after
the checksum field in the first four bytes.
"""

from __future__ import annotations

from collections.abc import Iterator

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import NO_LSN, NO_PAGE, PageIndex, SlotId
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.page.errors import PageFullError
from okto_grafx.domain.page.layout import (
    CHECKSUM_SIZE,
    DEFAULT_PAGE_SIZE,
    MAX_U16,
    MAX_U32,
    MAX_U64,
    PAGE_HEADER_SIZE,
    SLOT_ENTRY_SIZE,
    PageHeader,
    PageType,
    decode_slot_entry,
    encode_slot_entry,
    validate_page_size,
)

__all__ = ["FREE_SLOT", "Page"]

FREE_SLOT: tuple[int, int] = (0, 0)
"""The directory entry of a slot that has been freed: offset zero, length zero."""


class Page:
    """One page of a paged file, mutable in memory and serialisable to exactly page_size bytes.

    The header fields the rest of the engine reads and writes are exposed as properties, so any
    change also marks the page dirty; the buffer pool uses that flag to decide what to write
    back. Slot ids are stable for the life of the page, which is what makes a RecordRef a
    durable address.
    """

    __slots__ = (
        "_page_size",
        "_page_index",
        "_page_type",
        "_flags",
        "_page_lsn",
        "_seq",
        "_next_page",
        "_reserved",
        "_slots",
        "_data",
        "_free_start",
        "_dirty",
    )

    def __init__(
        self,
        page_type: int = int(PageType.FREE),
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        page_index: PageIndex = 0,
        page_lsn: int = NO_LSN,
        seq: int = 0,
        flags: int = 0,
        next_page: PageIndex = NO_PAGE,
    ) -> None:
        """Build an empty page of the given type and size."""
        self._page_size: int = validate_page_size(page_size)
        self._page_index: PageIndex = _check_unsigned("page_index", page_index, MAX_U32)
        self._page_type: int = int(_check_unsigned("page_type", page_type, MAX_U16))
        self._flags: int = _check_unsigned("flags", flags, MAX_U16)
        self._page_lsn: int = _check_unsigned("page_lsn", page_lsn, MAX_U64)
        self._seq: int = _check_unsigned("seq", seq, MAX_U32)
        self._next_page: PageIndex = _check_unsigned("next_page", next_page, MAX_U32)
        self._reserved: int = 0
        self._slots: list[tuple[int, int]] = []
        self._data: bytearray = bytearray(self._page_size)
        self._free_start: int = PAGE_HEADER_SIZE
        self._dirty: bool = False

    # --- identity and header fields ------------------------------------------------------

    @property
    def page_size(self) -> int:
        """Return the fixed size in bytes of this page."""
        return self._page_size

    @property
    def page_index(self) -> PageIndex:
        """Return the position of this page inside its file.

        The codec port carries no index, so a page decoded from raw bytes learns its own
        position from the buffer pool that read it.
        """
        return self._page_index

    @page_index.setter
    def page_index(self, value: PageIndex) -> None:
        """Record the position this page occupies inside its file."""
        self._page_index = _check_unsigned("page_index", value, MAX_U32)

    @property
    def page_type(self) -> int:
        """Return the kind of content this page carries."""
        return self._page_type

    @page_type.setter
    def page_type(self, value: int) -> None:
        """Change the kind of content this page carries and mark it dirty."""
        self._page_type = int(_check_unsigned("page_type", value, MAX_U16))
        self._dirty = True

    @property
    def flags(self) -> int:
        """Return the page flag word."""
        return self._flags

    @flags.setter
    def flags(self, value: int) -> None:
        """Replace the page flag word and mark the page dirty."""
        self._flags = _check_unsigned("flags", value, MAX_U16)
        self._dirty = True

    @property
    def page_lsn(self) -> int:
        """Return the LSN of the last WAL record applied to this page."""
        return self._page_lsn

    @page_lsn.setter
    def page_lsn(self, value: int) -> None:
        """Record the LSN of the last WAL record applied to this page."""
        self._page_lsn = _check_unsigned("page_lsn", value, MAX_U64)
        self._dirty = True

    @property
    def seq(self) -> int:
        """Return the write sequence counter: even is stable, odd is being written."""
        return self._seq

    @seq.setter
    def seq(self, value: int) -> None:
        """Replace the write sequence counter, which the buffer pool advances on every write."""
        self._seq = _check_unsigned("seq", value, MAX_U32)
        self._dirty = True

    @property
    def next_page(self) -> PageIndex:
        """Return the next page of the chain this page belongs to, or NO_PAGE."""
        return self._next_page

    @next_page.setter
    def next_page(self, value: PageIndex) -> None:
        """Point this page at the next one of its chain, or at NO_PAGE to end it."""
        self._next_page = _check_unsigned("next_page", value, MAX_U32)
        self._dirty = True

    @property
    def dirty(self) -> bool:
        """Return True when this page holds a change that is not on the device yet."""
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        """Mark the page as holding an unwritten change, or as clean after a write."""
        self._dirty = bool(value)

    # --- geometry -------------------------------------------------------------------------

    @property
    def slot_count(self) -> int:
        """Return the number of directory entries, freed slots included."""
        return len(self._slots)

    @property
    def free_start(self) -> int:
        """Return the offset where the next payload would be placed."""
        return self._free_start

    @property
    def free_end(self) -> int:
        """Return the offset where the slot directory begins."""
        return self._page_size - len(self._slots) * SLOT_ENTRY_SIZE

    def free_space(self) -> int:
        """Return the contiguous bytes between the payload area and the slot directory."""
        return self.free_end - self._free_start

    def can_fit(self, payload_size: int) -> bool:
        """Return True when a new slot of that payload size would fit without compacting."""
        return payload_size + SLOT_ENTRY_SIZE <= self.free_space()

    def live_bytes(self) -> int:
        """Return the payload bytes that live slots actually occupy, gaps excluded."""
        return sum(length for offset, length in self._slots if (offset, length) != FREE_SLOT)

    def compactable_space(self) -> int:
        """Return the payload bytes that would be free after compacting, directory excluded."""
        return self.free_end - PAGE_HEADER_SIZE - self.live_bytes()

    def header(self) -> PageHeader:
        """Return the header that describes this page right now, with a zero checksum."""
        return PageHeader(
            page_type=self._page_type,
            flags=self._flags,
            page_lsn=self._page_lsn,
            seq=self._seq,
            slot_count=len(self._slots),
            free_start=self._free_start,
            free_end=self.free_end,
            reserved=self._reserved,
            next_page=self._next_page,
            checksum=0,
        )

    # --- slots ----------------------------------------------------------------------------

    def insert_slot(self, payload: bytes) -> SlotId:
        """Append the payload to the page and return the new slot id.

        The slot id is the next free position of the directory. It is never a recycled id: a
        RecordRef that named a freed slot must fail to resolve, not silently resolve to a
        different record.
        """
        content = _as_bytes("payload", payload)
        # There is deliberately no check that the payload or the slot count fits its 16-bit
        # field. The largest legal page is MAX_PAGE_SIZE, so a payload can never reach 65535
        # bytes and a directory can never reach 65535 entries: can_fit refuses first, in every
        # case, on every accepted page size. A branch no input can reach is not a guard, it is
        # dead code that no test could defend (A31).
        if not self.can_fit(len(content)):
            # The directory grows by one entry, so the payload area loses those bytes too.
            capacity = self.compactable_space() - SLOT_ENTRY_SIZE
            if len(content) > capacity:
                raise PageFullError(
                    f"A payload of {len(content)} bytes plus a directory entry does not fit in "
                    f"page {self._page_index}, which can free {max(capacity, 0)} bytes.",
                    requested=len(content) + SLOT_ENTRY_SIZE,
                    available=self.free_space(),
                    page=self._page_index,
                )
            self.compact()
        offset = self._free_start
        self._data[offset : offset + len(content)] = content
        self._free_start = offset + len(content)
        self._slots.append((offset, len(content)))
        self._dirty = True
        return len(self._slots) - 1

    def read_slot(self, slot: SlotId) -> bytes:
        """Return the payload stored in the slot."""
        offset, length = self._entry(slot)
        return bytes(self._data[offset : offset + length])

    def slot_view(self, slot: SlotId) -> memoryview:
        """Return a read-only view of a live slot without copying its payload.

        The view is a short-lived decode aid: callers must consume it while they still own the
        page pin and must not retain it across a page mutation.  Returning it read-only keeps a
        decoder from bypassing the page's dirty tracking and slot geometry.
        """
        offset, length = self._entry(slot)
        return memoryview(self._data)[offset : offset + length].toreadonly()

    def slot_length(self, slot: SlotId) -> int:
        """Return the payload length of the slot."""
        return self._entry(slot)[1]

    def is_slot_free(self, slot: SlotId) -> bool:
        """Return True when the slot exists but has been freed."""
        self._check_slot_range(slot)
        return self._slots[slot] == FREE_SLOT

    def update_slot(self, slot: SlotId, payload: bytes) -> None:
        """Replace the payload of a live slot, keeping its id.

        A payload that is not larger than the one it replaces is written in place. A larger one
        is relocated inside the page, compacting first if that is what makes it fit. When the
        page cannot hold it at all the call raises PageFullError and the page is left exactly as
        it was, so the caller can place the new version elsewhere.
        """
        offset, length = self._entry(slot)
        content = _as_bytes("payload", payload)
        if len(content) <= length:
            self._data[offset : offset + len(content)] = content
            # Zero the tail that the shorter payload no longer covers, so the encoded image
            # depends only on the live content of the page.
            self._data[offset + len(content) : offset + length] = bytes(length - len(content))
            self._slots[slot] = (offset, len(content))
            self._dirty = True
            return
        # Feasibility is decided before anything is touched: the space this slot gives back
        # counts, but only a page that could hold the new payload after compacting may proceed.
        capacity = self.compactable_space() + length
        if len(content) > capacity:
            raise PageFullError(
                f"A payload of {len(content)} bytes does not fit in slot {slot} of page "
                f"{self._page_index}, which can offer {max(capacity, 0)} bytes.",
                requested=len(content),
                available=max(capacity, 0),
                page=self._page_index,
                slot=slot,
            )
        self._data[offset : offset + length] = bytes(length)
        self._slots[slot] = FREE_SLOT
        if len(content) > self.free_space():
            self.compact()
        new_offset = self._free_start
        self._data[new_offset : new_offset + len(content)] = content
        self._free_start = new_offset + len(content)
        self._slots[slot] = (new_offset, len(content))
        self._dirty = True

    def free_slot(self, slot: SlotId) -> int:
        """Free the slot and return the payload bytes that compacting can now reclaim.

        The directory entry stays in place and reads of the slot fail from now on. The bytes are
        zeroed immediately so that the encoded image of a page never carries the residue of a
        payload that was logically removed.
        """
        offset, length = self._entry(slot)
        self._data[offset : offset + length] = bytes(length)
        self._slots[slot] = FREE_SLOT
        self._dirty = True
        return length

    def iter_slots(self) -> Iterator[tuple[SlotId, bytes]]:
        """Yield every live slot id with its payload, in slot order."""
        for slot, (offset, length) in enumerate(self._slots):
            if (offset, length) == FREE_SLOT:
                continue
            yield slot, bytes(self._data[offset : offset + length])

    def live_slots(self) -> tuple[SlotId, ...]:
        """Return the ids of the slots that still hold a payload, in slot order."""
        return tuple(
            slot for slot, entry in enumerate(self._slots) if entry != FREE_SLOT
        )

    def compact(self) -> int:
        """Close the gaps left by freed and relocated payloads and return the bytes reclaimed.

        Slot ids and their order are preserved; only the offsets change. A freed directory entry
        is kept, including a trailing one: dropping it would hand its id to the next insertion,
        and a RecordRef that named the freed slot would then resolve to a different record.
        """
        before = self._free_start
        packed = bytearray(self._page_size)
        cursor = PAGE_HEADER_SIZE
        rebuilt: list[tuple[int, int]] = []
        for offset, length in self._slots:
            if (offset, length) == FREE_SLOT:
                rebuilt.append(FREE_SLOT)
                continue
            packed[cursor : cursor + length] = self._data[offset : offset + length]
            rebuilt.append((cursor, length))
            cursor += length
        self._data = packed
        self._slots = rebuilt
        self._free_start = cursor
        reclaimed = before - cursor
        if reclaimed:
            self._dirty = True
        return reclaimed

    def is_pristine(self) -> bool:
        """Return True when nothing has ever been placed on this page.

        A page the device allocated and nobody wrote decodes to exactly this shape: free, no
        slots, the payload area untouched, no chain, no log position and no flags. Anything else
        that merely happens to have no live slot has been written to, and a caller that reserves
        or reinitialises a page must be able to tell the two apart, because one is absence and
        the other is damage. A page carrying a page_lsn has had a log record applied to it, which
        is the loudest possible statement that it is not untouched.

        The write counter is deliberately not part of the question. A page the pool allocated and
        flushed carries seq 2 while holding nothing at all, and refusing to reserve that page
        would wedge exactly the recovery-grown file this predicate exists to repair.
        """
        return (
            self._page_type == int(PageType.FREE)
            and not self._slots
            and self._free_start == PAGE_HEADER_SIZE
            and self._next_page == NO_PAGE
            and self._page_lsn == NO_LSN
            and self._flags == 0
        )

    def clear(self) -> None:
        """Drop every slot and every payload byte, keeping the header fields."""
        self._slots = []
        self._data = bytearray(self._page_size)
        self._free_start = PAGE_HEADER_SIZE
        self._dirty = True

    def replace_with(self, other: Page) -> None:
        """Adopt the whole content of another page of the same size, keeping this page identity.

        The redo path of recovery uses this to install a page image over a resident frame
        without the buffer pool having to drop and re-read it.
        """
        if other.page_size != self._page_size:
            raise GrafxCorruptionDetected(
                f"A page of {other.page_size} bytes cannot replace one of {self._page_size}.",
                field="page_size",
                value=other.page_size,
                page=self._page_index,
            )
        self._page_type = other._page_type
        self._flags = other._flags
        self._page_lsn = other._page_lsn
        self._seq = other._seq
        self._next_page = other._next_page
        self._reserved = other._reserved
        self._slots = list(other._slots)
        self._data = bytearray(other._data)
        self._free_start = other._free_start
        self._dirty = True

    def copy(self) -> Page:
        """Return an independent page value carrying every field this page holds.

        The commit path stamps the predicted commit number into a copy of the resident frame
        and encodes that copy once; the frame itself must stay provisional until the WAL
        barrier returns, so nothing may be shared: the slot directory and the payload buffer
        are duplicated, and the identity, header fields, reserved word and dirty flag are
        carried whole -- it is a copy, not a clean re-read. ``to_bytes`` of the copy is
        byte-identical to ``to_bytes`` of the original.
        """
        clone = Page.__new__(Page)
        clone._page_size = self._page_size
        clone._page_index = self._page_index
        clone._page_type = self._page_type
        clone._flags = self._flags
        clone._page_lsn = self._page_lsn
        clone._seq = self._seq
        clone._next_page = self._next_page
        clone._reserved = self._reserved
        clone._slots = list(self._slots)
        clone._data = bytearray(self._data)
        clone._free_start = self._free_start
        clone._dirty = self._dirty
        return clone

    # --- serialisation --------------------------------------------------------------------

    def to_bytes(self) -> bytes:
        """Return the page image: exactly page_size bytes with a correct CRC-32C."""
        image = bytearray(self._page_size)
        image[PAGE_HEADER_SIZE : self._free_start] = self._data[
            PAGE_HEADER_SIZE : self._free_start
        ]
        position = self._page_size
        for offset, length in self._slots:
            position -= SLOT_ENTRY_SIZE
            image[position : position + SLOT_ENTRY_SIZE] = encode_slot_entry(offset, length)
        image[0:PAGE_HEADER_SIZE] = self.header().encode()
        checksum = crc32c(bytes(image[CHECKSUM_SIZE : self._page_size]))
        image[0:CHECKSUM_SIZE] = checksum.to_bytes(CHECKSUM_SIZE, "little")
        return bytes(image)

    @classmethod
    def from_bytes(
        cls,
        raw: bytes,
        *,
        page_size: int | None = None,
        page_index: PageIndex | None = None,
        verify: bool = True,
    ) -> Page:
        """Parse a page image, checking its structure and, when asked, its checksum.

        The structural checks always run: a header whose counters contradict each other, or a
        directory whose entries leave the payload area or overlap one another, describes bytes
        that cannot be interpreted at all, and reading them as if they made sense is how a
        corruption becomes a wrong answer instead of an error.

        The page index is optional because the codec port carries none: only the buffer pool
        that issued the read knows where the bytes came from. Without it these failures say
        nothing about which page it was, rather than claiming page zero, and the caller that
        does know the location names it in the error it raises in turn.
        """
        located = _location(page_index)
        if page_size is None:
            # With no size declared, the image defines it, so there is nothing to compare it
            # against: validate_page_size is the whole check, and it refuses a length that is not
            # a legal page size at all.
            size = validate_page_size(len(raw))
        else:
            size = validate_page_size(page_size)
            if len(raw) != size:
                raise GrafxCorruptionDetected(
                    f"A page image must be exactly {size} bytes; got {len(raw)}.",
                    field="page_image",
                    value=len(raw),
                    **located,
                )
        header = PageHeader.decode(raw)
        if verify:
            expected = crc32c(bytes(raw[CHECKSUM_SIZE:size]))
            if expected != header.checksum:
                raise GrafxCorruptionDetected(
                    f"{_subject(page_index)} failed its checksum: stored "
                    f"0x{header.checksum:08x}, computed 0x{expected:08x}.",
                    field="checksum",
                    stored_checksum=header.checksum,
                    computed_checksum=expected,
                    **located,
                )
        page = cls(
            page_type=header.page_type,
            page_size=size,
            page_index=0 if page_index is None else page_index,
            page_lsn=header.page_lsn,
            seq=header.seq,
            flags=header.flags,
            next_page=header.next_page,
        )
        page._reserved = header.reserved
        page._slots = _decode_directory(raw, header, size, page_index)
        page._free_start = header.free_start
        page._data[PAGE_HEADER_SIZE : header.free_start] = raw[
            PAGE_HEADER_SIZE : header.free_start
        ]
        page._dirty = False
        return page

    # --- protocol -------------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Page):
            return NotImplemented
        return (
            self._page_size == other._page_size
            and self._page_type == other._page_type
            and self._flags == other._flags
            and self._page_lsn == other._page_lsn
            and self._seq == other._seq
            and self._next_page == other._next_page
            and self._slots == other._slots
            and self._data[PAGE_HEADER_SIZE : self._free_start]
            == other._data[PAGE_HEADER_SIZE : other._free_start]
        )

    # A page is mutable, so it is deliberately unhashable: a page used as a dictionary key would
    # keep answering for the content it had when it was inserted.
    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return (
            f"Page(page_index={self._page_index}, page_type={self._page_type}, "
            f"slot_count={len(self._slots)}, free_space={self.free_space()}, "
            f"page_lsn={self._page_lsn}, seq={self._seq}, next_page={self._next_page})"
        )

    # --- internals ------------------------------------------------------------------------

    def _check_slot_range(self, slot: SlotId) -> None:
        """Refuse a slot id this page cannot serve, without calling the page damaged.

        A slot id is an ARGUMENT. A page decoded from an image has exactly as many directory
        entries as the image carried, so no id out of that range can have come from the page --
        it came from the caller, out of a stale reference, an index entry, or a typo. The page
        cannot know which, so it classifies by what it does know: this is a refusal about the
        request, not a finding about the bytes (A11-revised).

        A caller that knows its id came off a page -- following a prev_version pointer, reading
        a structural slot it wrote itself -- knows the provenance the page does not, and turns
        this refusal into corruption_detected at its own boundary. HeapStore._require_data_page
        is the worked example.
        """
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise GrafxConfigurationError(
                f"A slot id must be an integer; got {type(slot).__name__}.",
                field="slot",
                value=repr(slot),
                page=self._page_index,
            )
        if not 0 <= slot < len(self._slots):
            raise GrafxUnsupportedOperation(
                f"Slot {slot} does not exist on page {self._page_index}, which has "
                f"{len(self._slots)} slots.",
                field="slot",
                page=self._page_index,
                slot=slot,
                slot_count=len(self._slots),
            )

    def _entry(self, slot: SlotId) -> tuple[int, int]:
        """Return the directory entry of a LIVE slot, refusing a freed one as an ordinary state.

        Freeing a slot is a sanctioned operation -- C7's index manager does it on purpose -- so a
        freed slot is an ordinary state of an ordinary page, and reading one must not be able to
        start a quarantine. corruption_detected is not a severity, it is a ROUTE: FR-8 and FR-10
        turn it into truncation, quarantine and a forensic ledger entry, and this door was
        putting a normal state onto that route. C6 could not trust the exception and had to guard
        every read with is_slot_free instead, which is what a wrong contract looks like from the
        outside.

        Still a refusal and not an absence: page 236's rule is that a reference naming a freed
        slot must FAIL to resolve rather than quietly resolve to a neighbour, and returning None
        would put the burden of remembering that on every caller. What changes is the route, and
        field="freed_slot" is what a caller routes on.
        """
        self._check_slot_range(slot)
        entry = self._slots[slot]
        if entry == FREE_SLOT:
            raise GrafxUnsupportedOperation(
                f"Slot {slot} of page {self._page_index} has been freed and holds no payload.",
                field="freed_slot",
                page=self._page_index,
                slot=slot,
            )
        return entry


def _location(page_index: PageIndex | None) -> dict[str, int]:
    """Return the page detail of an error, empty when the caller did not say which page."""
    return {} if page_index is None else {"page": page_index}


def _subject(page_index: PageIndex | None) -> str:
    """Return how an error should name the page it is about."""
    return "A page image" if page_index is None else f"Page {page_index}"


def _check_unsigned(field: str, value: int, maximum: int) -> int:
    """Return the value after checking that it is an unsigned integer of that width."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxCorruptionDetected(
            f"Page field {field!r} must be an integer; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if not 0 <= value <= maximum:
        raise GrafxCorruptionDetected(
            f"Page field {field!r} is outside its width: {value} exceeds {maximum}.",
            field=field,
            value=value,
        )
    return value


def _as_bytes(field: str, payload: bytes) -> bytes:
    """Return the payload as immutable bytes, refusing anything that is not a byte buffer.

    An argument of the wrong type, classified the way the slot id beside it is: a caller handing
    a str where bytes belong has said nothing about any byte on any page, and corruption_detected
    is the route to truncation and quarantine. Leaving this one as damage while its neighbour
    became a configuration error would have made the same mistake, spelled differently.
    """
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload)
    raise GrafxConfigurationError(
        f"A slot {field} must be a byte buffer; got {type(payload).__name__}.",
        field=field,
        value=type(payload).__name__,
    )


def _decode_directory(
    raw: bytes, header: PageHeader, size: int, page_index: PageIndex | None
) -> list[tuple[int, int]]:
    """Return the slot directory of a page image after proving that it is self-consistent."""
    located = _location(page_index)
    subject = _subject(page_index)
    directory_bytes = header.slot_count * SLOT_ENTRY_SIZE
    if PAGE_HEADER_SIZE + directory_bytes > size:
        raise GrafxCorruptionDetected(
            f"{subject} declares {header.slot_count} slots, which do not fit in {size} bytes.",
            field="slot_count",
            slot_count=header.slot_count,
            directory_bytes=directory_bytes,
            **located,
        )
    if header.free_end != size - directory_bytes:
        raise GrafxCorruptionDetected(
            f"{subject} declares free_end {header.free_end}, but {header.slot_count} "
            f"slots put the directory at {size - directory_bytes}.",
            field="free_end",
            free_end=header.free_end,
            slot_count=header.slot_count,
            **located,
        )
    if not PAGE_HEADER_SIZE <= header.free_start <= header.free_end:
        raise GrafxCorruptionDetected(
            f"{subject} declares free_start {header.free_start}, which is outside the "
            f"payload area that ends at {header.free_end}.",
            field="free_start",
            free_start=header.free_start,
            free_end=header.free_end,
            **located,
        )
    entries: list[tuple[int, int]] = []
    occupied: list[tuple[int, int]] = []
    for slot in range(header.slot_count):
        position = size - (slot + 1) * SLOT_ENTRY_SIZE
        offset, length = decode_slot_entry(raw, position)
        if (offset, length) == FREE_SLOT:
            entries.append(FREE_SLOT)
            continue
        if offset < PAGE_HEADER_SIZE or offset + length > header.free_start:
            raise GrafxCorruptionDetected(
                f"Slot {slot} spans bytes {offset} to {offset + length}, which leaves the "
                f"payload area that ends at {header.free_start}.",
                field="slot_entry",
                slot=slot,
                offset=offset,
                length=length,
                **located,
            )
        occupied.append((offset, offset + length))
        entries.append((offset, length))
    occupied.sort()
    for (start, end), (next_start, _) in zip(occupied, occupied[1:]):
        if end > next_start:
            raise GrafxCorruptionDetected(
                f"{subject} has overlapping payloads: one ends at {end} and the next "
                f"starts at {next_start}.",
                field="slot_entry",
                offset=start,
                overlap=end - next_start,
                **located,
            )
    return entries
