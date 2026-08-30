"""One entry of a secondary index, and the bytes it occupies in a slot (SPEC-M1 FR-12, SD-3).

The dual visibility rule of SD-3 lives in this file before it lives anywhere else, because it is
a property of the RECORD and not only of the code that reads it:

* an **exact** entry is UNVERSIONED. It carries no birth stamp at all, so nothing can decide from
  an entry alone whether a snapshot may see the row it points at. That is not an omission to be
  repaired later: it is the structural reason an exact hit is a candidate that MUST be validated
  against the heap.
* a **proximity** entry is VERSIONED. It carries the commit number that created it, so the same
  predicate the heap answers with can be answered by the entry, and a proximity lookup needs no
  heap read at all.

Both kinds carry a ``dead_csn``. For a proximity entry that field IS the tombstone SD-3 names.
For an exact entry it is only a reclamation stamp -- it says when the row stopped carrying this
key, so a maintenance pass can tell when no live snapshot could still want the entry. It is never
consulted by an exact lookup, because an exact lookup consults nothing.

Layout inside the slot, little-endian::

    flags u8 | key_length u16 | ref u64 | born_csn u64 | dead_csn u64 | key bytes
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.ids import (
    NO_CSN,
    NO_PAGE,
    PROVISIONAL_CSN,
    Csn,
    PageIndex,
    RecordRef,
    SlotId,
)

__all__ = [
    "ENTRY_FLAG_VERSIONED",
    "INDEX_ENTRY_HEADER_SIZE",
    "MAX_INDEX_KEY_BYTES",
    "IndexEntry",
]

ENTRY_FLAG_VERSIONED: int = 0x01
"""Bit 0 of the entry flags: this entry carries a birth stamp and decides its own visibility."""

_ENTRY_STRUCT: struct.Struct = struct.Struct("<BHQQQ")

INDEX_ENTRY_HEADER_SIZE: int = _ENTRY_STRUCT.size
"""Bytes of fixed header that precede the key inside an entry."""

MAX_INDEX_KEY_BYTES: int = 0xFFFF
"""Longest key the entry format can carry: the length prefix is 16 bits.

A page is far smaller than this, so a real key is refused by the page it does not fit long before
it reaches this bound. The constant is the FORMAT's limit and the encoder checks it, so a key
that could never be read back is refused where it is written rather than where it is read.
"""

_MAX_U64: int = 0xFFFFFFFFFFFFFFFF


def _validated_image(
    raw: bytes,
) -> tuple[memoryview, int, int, int, bool]:
    """Validate an entry image once and return its zero-copy view and decoded header fields."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise GrafxCorruptionDetected(
            f"An index entry image must be bytes; got {type(raw).__name__}.",
            field="entry",
            value=type(raw).__name__,
        )
    if isinstance(raw, memoryview):
        image = raw.cast("B") if raw.c_contiguous else memoryview(bytes(raw))
    else:
        image = memoryview(raw)
    if len(image) < INDEX_ENTRY_HEADER_SIZE:
        raise GrafxCorruptionDetected(
            f"An index entry needs {INDEX_ENTRY_HEADER_SIZE} bytes of header; this one has "
            f"{len(image)}.",
            field="entry",
            value=len(image),
        )
    flags, key_length, ref, born_csn, dead_csn = _ENTRY_STRUCT.unpack_from(image, 0)
    expected = INDEX_ENTRY_HEADER_SIZE + key_length
    if len(image) != expected:
        raise GrafxCorruptionDetected(
            f"An index entry declaring a key of {key_length} bytes occupies {expected} "
            f"bytes; this slot holds {len(image)}.",
            field="key_length",
            value=key_length,
            length=len(image),
        )
    if flags & ~ENTRY_FLAG_VERSIONED:
        raise GrafxCorruptionDetected(
            f"An index entry carries the unknown flag bits {flags:#04x}.",
            field="flags",
            value=flags,
        )
    versioned = bool(flags & ENTRY_FLAG_VERSIONED)
    if born_csn == PROVISIONAL_CSN or dead_csn == PROVISIONAL_CSN:
        field = "born_csn" if born_csn == PROVISIONAL_CSN else "dead_csn"
        raise GrafxCorruptionDetected(
            "A persisted index entry carries the stamp reserved for provisional heap "
            f"versions in {field}.",
            field=field,
            value=PROVISIONAL_CSN,
        )
    if not versioned and born_csn != NO_CSN:
        raise GrafxCorruptionDetected(
            "An unversioned index entry carries a birth stamp, so it is not an image this "
            f"encoder produced: born_csn={born_csn}.",
            field="born_csn",
            value=born_csn,
        )
    if versioned and born_csn == NO_CSN:
        raise GrafxCorruptionDetected(
            "A versioned index entry carries no birth stamp, so no snapshot could decide it.",
            field="born_csn",
            value=born_csn,
        )
    return image, ref, born_csn, dead_csn, versioned


def _require_commit_number(field: str, value: object) -> int:
    """Return a commit number that fits its field, refusing anything that is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxIndexError(
            f"An index entry needs an integer {field}; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if not 0 <= value <= _MAX_U64:
        raise GrafxIndexError(
            f"An index entry needs a {field} between 0 and {_MAX_U64}; got {value}.",
            field=field,
            value=value,
        )
    return value


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One key pointing at one heap version, plus where the entry itself is stored.

    ``page`` and ``slot`` are the location of the ENTRY, not of the row: a walk fills them in so
    that a verification finding can name the exact slot it disagreed with (SPEC-M1 FR-11). They
    are not part of the encoded form, because a record does not store its own address.
    """

    key: bytes
    ref: RecordRef
    versioned: bool
    born_csn: Csn = NO_CSN
    dead_csn: Csn = NO_CSN
    page: PageIndex = NO_PAGE
    slot: SlotId = 0

    def __post_init__(self) -> None:
        """Refuse an entry that could not be stored, or that mixes the two visibility rules.

        The last two checks are the ones this class exists for. An unversioned entry carrying a
        birth stamp would be an exact entry that LOOKS answerable by a snapshot, and the first
        caller to believe it would filter an exact index by a rule that index does not maintain
        -- which is wrong results, quietly. A versioned entry without one is the mirror: a
        proximity entry no snapshot could ever admit, so the row disappears. Neither shape is
        legal in any state, so both are refused at construction rather than guarded at readers.
        """
        if not isinstance(self.key, (bytes, bytearray, memoryview)):
            raise GrafxIndexError(
                f"An index key must be bytes; got {type(self.key).__name__}.",
                field="key",
                value=type(self.key).__name__,
            )
        if not isinstance(self.key, bytes):
            object.__setattr__(self, "key", bytes(self.key))
        if len(self.key) > MAX_INDEX_KEY_BYTES:
            raise GrafxIndexError(
                f"An index key holds at most {MAX_INDEX_KEY_BYTES} bytes; got {len(self.key)}.",
                field="key",
                value=len(self.key),
            )
        if not isinstance(self.ref, RecordRef):
            raise GrafxIndexError(
                f"An index entry must point at a RecordRef; got {type(self.ref).__name__}.",
                field="ref",
                value=type(self.ref).__name__,
            )
        if not isinstance(self.versioned, bool):
            raise GrafxIndexError(
                f"An index entry needs a boolean versioned flag; got {self.versioned!r}.",
                field="versioned",
                value=repr(self.versioned),
            )
        _require_commit_number("born_csn", self.born_csn)
        _require_commit_number("dead_csn", self.dead_csn)
        if self.born_csn == PROVISIONAL_CSN or self.dead_csn == PROVISIONAL_CSN:
            field = "born_csn" if self.born_csn == PROVISIONAL_CSN else "dead_csn"
            raise GrafxIndexError(
                "The provisional heap stamp is not a commit and cannot be stored in an index "
                f"entry's {field}.",
                field=field,
                value=PROVISIONAL_CSN,
            )
        if not self.versioned and self.born_csn != NO_CSN:
            raise GrafxIndexError(
                "An unversioned index entry carries no birth stamp, so it cannot declare one; "
                f"got born_csn={self.born_csn}.",
                field="born_csn",
                value=self.born_csn,
                versioned=False,
            )
        if self.versioned and self.born_csn == NO_CSN:
            raise GrafxIndexError(
                "A versioned index entry must carry the commit number that created it.",
                field="born_csn",
                value=self.born_csn,
                versioned=True,
            )

    @property
    def live(self) -> bool:
        """Return True when nothing has ended this entry yet."""
        return self.dead_csn == NO_CSN

    def matches(self, key: bytes, ref: RecordRef) -> bool:
        """Return True when this entry is the one that key and heap location name."""
        return self.key == key and self.ref == ref

    def located_at(self, page: PageIndex, slot: SlotId) -> IndexEntry:
        """Return the same entry, tagged with the page and slot it was read from."""
        return replace(self, page=page, slot=slot)

    def ended_at(self, csn: Csn) -> IndexEntry:
        """Return the same entry carrying the commit number that ended it."""
        return replace(self, dead_csn=_require_commit_number("dead_csn", csn))

    def encode(self) -> bytes:
        """Return the bytes this entry occupies in a slot.

        Every field is range-checked before ``struct.pack`` sees it (amendment A41), so a value
        that does not fit its width leaves this door as a typed refusal naming the field rather
        than as a ``struct.error`` from the standard library.
        """
        flags = ENTRY_FLAG_VERSIONED if self.versioned else 0
        head = _ENTRY_STRUCT.pack(
            flags,
            len(self.key),
            self.ref.encode(),
            self.born_csn,
            self.dead_csn,
        )
        return head + self.key

    @classmethod
    def decode(cls, raw: bytes) -> IndexEntry:
        """Return the entry stored in these bytes, refusing an image that is not one."""
        image, ref, born_csn, dead_csn, versioned = _validated_image(raw)
        return cls(
            key=bytes(image[INDEX_ENTRY_HEADER_SIZE:]),
            ref=RecordRef.decode(ref),
            versioned=versioned,
            born_csn=born_csn,
            dead_csn=dead_csn,
        )

    @classmethod
    def decode_if_matches(
        cls, raw: bytes, key: bytes, ref: RecordRef | None = None
    ) -> IndexEntry | None:
        """Validate every image but materialize only one matching the requested key/reference."""
        image, encoded_ref, born_csn, dead_csn, versioned = _validated_image(raw)
        if image[INDEX_ENTRY_HEADER_SIZE:] != key:
            return None
        if ref is not None and encoded_ref != ref.encode():
            return None
        return cls(
            key=bytes(image[INDEX_ENTRY_HEADER_SIZE:]),
            ref=RecordRef.decode(encoded_ref),
            versioned=versioned,
            born_csn=born_csn,
            dead_csn=dead_csn,
        )
