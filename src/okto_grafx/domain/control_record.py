"""Crash-safe two-slot envelopes for small cross-process control records.

The logical payload remains owned by the lease, reader, or commit-state codec.  This module
only owns the physical publication envelope introduced by CE-1: one immutable header page and
two alternating slot pages.  A publication overwrites the older slot with one page write and
then barriers that file; a concurrent reader can always fall back to the other valid slot.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.page.layout import (
    CHECKSUM_SIZE,
    MAX_U64,
    NO_PAGE,
    PAGE_HEADER_SIZE,
    PageHeader,
    PageType,
)
from okto_grafx.domain.ports.storage import StorageDevice

__all__ = [
    "CONTROL_FILE_PAGES",
    "CONTROL_HEADER_PAGE",
    "CONTROL_SLOT_PAGES",
    "ControlRecordKind",
    "ControlRecordRead",
    "TwoSlotControlRecordStore",
]

CONTROL_FILE_PAGES: int = 3
CONTROL_HEADER_PAGE: int = 0
CONTROL_SLOT_PAGES: tuple[int, int] = (1, 2)
CONTROL_SLOT_FORMAT_VERSION: int = 1
CONTROL_HEADER_MAGIC: bytes = b"OKTOCTRL"
CONTROL_SLOT_MAGIC: bytes = b"OKTOSLOT"
CONTROL_READ_ATTEMPTS: int = 4

_HEADER_BODY = struct.Struct("<8sHHI16sQQ16s")
_HEADER_CRC = struct.Struct("<I")
_SLOT_HEAD = struct.Struct("<8sHHQII16sQI")
_SLOT_CRC = struct.Struct("<I")


class ControlRecordKind:
    """Closed integer identifiers stored in the physical control envelope."""

    LEASE: int = 1
    READER: int = 2
    COMMIT_STATE: int = 3


@dataclass(frozen=True, slots=True)
class ControlRecordRead:
    """One complete logical payload read from either the legacy or slot envelope."""

    payload: bytes
    format_version: int
    generation: int
    valid_payloads: tuple[bytes, ...] = ()


@dataclass(frozen=True, slots=True)
class _Header:
    database_uuid: bytes
    file_nonce: int
    record_kind: int


@dataclass(frozen=True, slots=True)
class _Slot:
    valid: bool
    generation: int = 0
    payload: bytes = b""


def _read_log_if_exists(
    storage: StorageDevice, file: str, offset: int, length: int
) -> bytes | None:
    """Use an explicitly declared fused read, or preserve the literal port fallback.

    Adapter-only capabilities are opt-in by concrete type. Looking in the type dictionary is
    deliberate: a generic wrapper that forwards unknown attributes through ``__getattr__`` has
    not proved that it preserves the fused operation's identity semantics. Such a wrapper keeps
    the ordinary ``exists`` then ``read_log`` sequence, including any missing-file refusal from
    the second door.
    """
    implementation = vars(type(storage)).get("read_log_if_exists")
    if callable(implementation):
        # Resolve the now-proved method through the instance so adapter-local instrumentation
        # wrappers remain effective. The type-dictionary gate above is what prevents an
        # unrelated ``__getattr__`` from advertising the capability.
        fused_read = getattr(storage, "read_log_if_exists")
        return fused_read(file, offset, length)
    if not storage.exists(file):
        return None
    return storage.read_log(file, offset, length)


def _require_database_uuid(value: bytes) -> bytes:
    """Return an exact database UUID suitable for binding a control page."""
    if type(value) is not bytes or len(value) != 16:
        raise GrafxConfigurationError(
            "A two-slot control record needs an exact 16-byte database UUID.",
            field="database_uuid",
            value=type(value).__name__,
            length=len(value) if isinstance(value, bytes) else None,
        )
    return value


def _require_kind(value: int) -> int:
    """Return a supported record kind without accepting bool or integer subclasses."""
    if type(value) is not int or value not in {
        ControlRecordKind.LEASE,
        ControlRecordKind.READER,
        ControlRecordKind.COMMIT_STATE,
    }:
        raise GrafxConfigurationError(
            "A two-slot control record needs a supported record kind.",
            field="record_kind",
            value=repr(value),
        )
    return value


def _page(*, page_type: PageType, seq: int, body: bytes, page_size: int) -> bytes:
    """Return one checksummed page carrying ``body`` immediately after its header."""
    if len(body) > page_size - PAGE_HEADER_SIZE:
        raise GrafxConfigurationError(
            "The control record body does not fit in one database page.",
            field="payload_length",
            value=len(body),
            maximum=page_size - PAGE_HEADER_SIZE,
        )
    image = bytearray(page_size)
    header = PageHeader(
        page_type=int(page_type),
        page_lsn=0,
        seq=seq,
        slot_count=0,
        free_start=PAGE_HEADER_SIZE,
        free_end=page_size,
        next_page=NO_PAGE,
    )
    image[:PAGE_HEADER_SIZE] = header.encode()
    image[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + len(body)] = body
    struct.pack_into("<I", image, 0, crc32c(bytes(image[CHECKSUM_SIZE:])))
    return bytes(image)


def _checked_page(
    raw: bytes,
    *,
    page_type: PageType,
    page_size: int,
    file: str,
    permit_checksum_mismatch: bool = False,
) -> bytes:
    """Return a page after validating its envelope, or raise precise corruption."""
    if len(raw) != page_size:
        raise GrafxCorruptionDetected(
            "A control page was not read in full.",
            file=file,
            field="page_length",
            value=len(raw),
            expected=page_size,
        )
    header = PageHeader.decode(raw)
    expected = crc32c(raw[CHECKSUM_SIZE:])
    if header.checksum != expected and not permit_checksum_mismatch:
        raise GrafxCorruptionDetected(
            "The checksum of a control page does not match its bytes.",
            file=file,
            field="checksum",
            value=header.checksum,
            observed=expected,
        )
    if (
        header.page_type != int(page_type)
        or header.flags != 0
        or header.page_lsn != 0
        or header.slot_count != 0
        or header.free_start != PAGE_HEADER_SIZE
        or header.free_end != page_size
        or header.reserved != 0
        or header.next_page != NO_PAGE
    ):
        raise GrafxCorruptionDetected(
            "A control page header is not canonical for its declared role.",
            file=file,
            field="page_header",
            page_type=header.page_type,
        )
    return raw[PAGE_HEADER_SIZE:]


def _encode_header(
    *, database_uuid: bytes, file_nonce: int, record_kind: int, page_size: int
) -> bytes:
    """Return the authenticated immutable body of page zero."""
    body = _HEADER_BODY.pack(
        CONTROL_HEADER_MAGIC,
        CONTROL_SLOT_FORMAT_VERSION,
        record_kind,
        page_size,
        database_uuid,
        file_nonce,
        1,
        bytes(16),
    )
    return body + _HEADER_CRC.pack(crc32c(body))


def _decode_header(
    raw: bytes,
    *,
    database_uuid: bytes,
    record_kind: int,
    page_size: int,
    file: str,
    permit_page_checksum_mismatch: bool = False,
    permit_inner_checksum_mismatch: bool = False,
) -> _Header:
    """Decode page zero and bind it to this database, file kind, and page size."""
    body = _checked_page(
        raw,
        page_type=PageType.CONTROL_HEADER,
        page_size=page_size,
        file=file,
        permit_checksum_mismatch=permit_page_checksum_mismatch,
    )
    expected_length = _HEADER_BODY.size + _HEADER_CRC.size
    if len(body) < expected_length:
        raise GrafxCorruptionDetected(
            "The control header body is truncated.", file=file, field="header_length"
        )
    fields = _HEADER_BODY.unpack_from(body, 0)
    magic, version, kind, stored_page_size, stored_uuid, nonce, created, reserved = (
        fields
    )
    (stored_crc,) = _HEADER_CRC.unpack_from(body, _HEADER_BODY.size)
    if magic != CONTROL_HEADER_MAGIC:
        raise GrafxCorruptionDetected(
            "The control header carries foreign magic.", file=file, field="magic"
        )
    if version > CONTROL_SLOT_FORMAT_VERSION:
        raise GrafxSchemaVersionMismatch(
            "The control header was written by a newer slot format.",
            file=file,
            field="format_version",
            value=version,
            supported=CONTROL_SLOT_FORMAT_VERSION,
        )
    if version != CONTROL_SLOT_FORMAT_VERSION:
        raise GrafxCorruptionDetected(
            "The control header carries an invalid slot format.",
            file=file,
            field="format_version",
            value=version,
        )
    if (
        stored_crc != crc32c(body[: _HEADER_BODY.size])
        and not permit_inner_checksum_mismatch
    ):
        raise GrafxCorruptionDetected(
            "The inner checksum of the control header does not match.",
            file=file,
            field="header_crc32c",
        )
    if (
        kind != record_kind
        or stored_page_size != page_size
        or stored_uuid != database_uuid
        or created != 1
        or reserved != bytes(16)
    ):
        raise GrafxCorruptionDetected(
            "The control header is not bound to this database and record kind.",
            file=file,
            field="control_binding",
        )
    return _Header(database_uuid=stored_uuid, file_nonce=nonce, record_kind=kind)


def _encode_slot(
    *, header: _Header, generation: int, payload: bytes, page_size: int
) -> bytes:
    """Return one canonical EMPTY or populated slot page."""
    if type(payload) is not bytes:
        raise GrafxConfigurationError(
            "A control payload must be exact bytes.",
            field="payload",
            value=type(payload).__name__,
        )
    maximum = page_size - PAGE_HEADER_SIZE - _SLOT_HEAD.size - _SLOT_CRC.size
    if len(payload) > maximum:
        raise GrafxConfigurationError(
            "The control payload does not fit in one slot page.",
            field="payload_length",
            value=len(payload),
            maximum=maximum,
        )
    if not 0 <= generation <= MAX_U64:
        raise GrafxCorruptionDetected(
            "The control generation is outside its unsigned width.",
            field="generation",
            value=generation,
        )
    head = _SLOT_HEAD.pack(
        CONTROL_SLOT_MAGIC,
        CONTROL_SLOT_FORMAT_VERSION,
        header.record_kind,
        generation,
        len(payload),
        0,
        header.database_uuid,
        header.file_nonce,
        0,
    )
    record = head + payload
    body = record + _SLOT_CRC.pack(crc32c(record))
    return _page(
        page_type=PageType.CONTROL_SLOT,
        seq=(generation * 2) & 0xFFFFFFFF,
        body=body,
        page_size=page_size,
    )


def _decode_slot(raw: bytes, *, header: _Header, page_size: int, file: str) -> _Slot:
    """Return a valid/empty slot, treating any torn slot as independently invalid."""
    try:
        body = _checked_page(
            raw, page_type=PageType.CONTROL_SLOT, page_size=page_size, file=file
        )
        if len(body) < _SLOT_HEAD.size + _SLOT_CRC.size:
            return _Slot(False)
        (
            magic,
            version,
            kind,
            generation,
            payload_length,
            reserved,
            stored_uuid,
            nonce,
            reserved2,
        ) = _SLOT_HEAD.unpack_from(body, 0)
        if version > CONTROL_SLOT_FORMAT_VERSION:
            raise GrafxSchemaVersionMismatch(
                "A control slot was written by a newer slot format.",
                file=file,
                field="format_version",
                value=version,
                supported=CONTROL_SLOT_FORMAT_VERSION,
            )
        maximum = page_size - PAGE_HEADER_SIZE - _SLOT_HEAD.size - _SLOT_CRC.size
        end = _SLOT_HEAD.size + payload_length
        if (
            magic != CONTROL_SLOT_MAGIC
            or version != CONTROL_SLOT_FORMAT_VERSION
            or kind != header.record_kind
            or reserved != 0
            or reserved2 != 0
            or stored_uuid != header.database_uuid
            or nonce != header.file_nonce
            or payload_length > maximum
        ):
            return _Slot(False)
        payload = body[_SLOT_HEAD.size : end]
        (stored_crc,) = _SLOT_CRC.unpack_from(body, end)
        if stored_crc != crc32c(body[:end]):
            return _Slot(False)
        if generation == 0 and payload:
            return _Slot(False)
        return _Slot(True, generation=generation, payload=bytes(payload))
    except GrafxSchemaVersionMismatch:
        raise
    except GrafxCorruptionDetected:
        return _Slot(False)


class TwoSlotControlRecordStore:
    """Read legacy records and publish format-2 records through alternating pages."""

    __slots__ = (
        "_database_uuid",
        "_file",
        "_file_nonce",
        "_kind",
        "_last_seen",
        "_storage",
        "_temporary",
    )

    def __init__(
        self,
        storage: StorageDevice,
        *,
        file: str,
        record_kind: int,
        database_uuid: bytes,
        file_nonce: int,
        temporary: str,
    ) -> None:
        """Bind one file and its bootstrap nonce without creating or reading it."""
        self._storage = storage
        self._file = file
        self._kind = _require_kind(record_kind)
        self._database_uuid = _require_database_uuid(database_uuid)
        if type(file_nonce) is not int or not 0 <= file_nonce <= MAX_U64:
            raise GrafxConfigurationError(
                "A control file nonce must be an unsigned 64-bit integer.",
                field="file_nonce",
                value=repr(file_nonce),
            )
        self._file_nonce = file_nonce
        self._temporary = temporary
        self._last_seen = 0

    def read(self) -> ControlRecordRead | None:
        """Return the newest whole payload, a legacy payload, or None when absent."""
        image = self._read_file_image()
        if image is None:
            return None
        expected_size = CONTROL_FILE_PAGES * self._storage.page_size
        if len(image) != expected_size:
            return ControlRecordRead(image, 1, 0, (image,))
        damage: GrafxCorruptionDetected | None = None
        for attempt in range(CONTROL_READ_ATTEMPTS):
            try:
                _header, slots = self._decode_slots(image)
                chosen, _target = self._select_slot(slots)
                if chosen is None:
                    raise GrafxCorruptionDetected(
                        "Both slots of the control record are invalid or empty.",
                        file=self._file,
                        field="slots",
                    )
                if chosen.generation < self._last_seen:
                    raise GrafxCorruptionDetected(
                        "The control record generation regressed.",
                        file=self._file,
                        field="generation",
                        value=chosen.generation,
                        observed=self._last_seen,
                    )
                self._last_seen = chosen.generation
                return ControlRecordRead(
                    chosen.payload,
                    2,
                    chosen.generation,
                    self._valid_payloads(slots),
                )
            except GrafxCorruptionDetected as failure:
                damage = failure
                if attempt + 1 < CONTROL_READ_ATTEMPTS:
                    image = self._read_exact_image()
        if damage is None:  # pragma: no cover - the loop always records its refusal
            raise AssertionError("unreachable control record read")
        raise damage

    def publish(self, payload: bytes) -> int:
        """Publish payload in one slot write+barrier, bootstrapping/migrating atomically once."""
        return self.publish_checked(payload, expected_current=None, publications=1)

    @staticmethod
    def damage_is_replaceable(failure: GrafxCorruptionDetected) -> bool:
        """Return whether WAL-authorized recovery may rebuild this physical damage.

        Binding, magic, version and canonical-role mismatches can describe a valid foreign file,
        not torn local bytes, and are deliberately excluded.
        """
        return failure.details.get("field") in {
            "checksum",
            "header_crc32c",
            "slots",
        }

    def valid_payloads_behind_replaceable_damage(
        self, failure: GrafxCorruptionDetected
    ) -> tuple[bytes, ...]:
        """Inspect slots without discarding a fence hidden by header-checksum damage.

        Recovery may replace a torn header only after its remaining independent checksum and
        every semantic binding still establish which database, nonce and record kind own the
        slots. The slot pages then validate themselves against that header. No other header
        failure is relaxed, and ambiguous equal generations remain a refusal.
        """
        field = failure.details.get("field")
        if field not in {"checksum", "header_crc32c"}:
            return ()
        image = self._read_exact_image()
        page_size = self._storage.page_size
        header_start = CONTROL_HEADER_PAGE * page_size
        header = _decode_header(
            image[header_start : header_start + page_size],
            database_uuid=self._database_uuid,
            record_kind=self._kind,
            page_size=page_size,
            file=self._file,
            permit_page_checksum_mismatch=field == "checksum",
            permit_inner_checksum_mismatch=field == "header_crc32c",
        )
        slots = tuple(
            _decode_slot(
                image[page * page_size : (page + 1) * page_size],
                header=header,
                page_size=page_size,
                file=self._file,
            )
            for page in CONTROL_SLOT_PAGES
        )
        self._select_slot(slots)
        return self._valid_payloads(slots)

    def publish_checked(
        self,
        payload: bytes,
        *,
        expected_current: tuple[bytes | None, ...] | None,
        publications: int,
        replace_damaged: bool = False,
    ) -> int:
        """Publish one payload repeatedly after checking the current logical bytes once.

        The commit-state feature fence uses two publications for its first version transition.
        Preflighting the complete generation budget before the first page write prevents a
        half-promoted record at the generation ceiling. Other control records continue to use
        :meth:`publish`, which is this operation with one publication and no compare guard.
        """
        if type(payload) is not bytes:
            raise GrafxConfigurationError(
                "A control payload must be exact bytes.",
                field="payload",
                value=type(payload).__name__,
            )
        if type(publications) is not int or publications < 1:
            raise GrafxConfigurationError(
                "A checked control publication needs a positive exact publication count.",
                field="publications",
                value=repr(publications),
            )
        if type(replace_damaged) is not bool:
            raise GrafxConfigurationError(
                "Damaged-control replacement authority must be an exact bool.",
                field="replace_damaged",
                value=type(replace_damaged).__name__,
            )
        if expected_current is not None and (
            type(expected_current) is not tuple
            or not expected_current
            or any(
                item is not None and type(item) is not bytes
                for item in expected_current
            )
        ):
            raise GrafxConfigurationError(
                "Expected control payloads must be a non-empty tuple of exact bytes or None.",
                field="expected_current",
                value=type(expected_current).__name__,
            )
        expected_size = CONTROL_FILE_PAGES * self._storage.page_size
        image = self._read_file_image(complete_legacy=False)
        if image is None:
            observed: bytes | None = None
            header = None
            chosen = None
            target = CONTROL_SLOT_PAGES[0]
        elif len(image) != expected_size:
            observed = image
            header = None
            chosen = None
            target = CONTROL_SLOT_PAGES[0]
        else:
            try:
                header, chosen, target = self._decode_complete_image(image)
            except GrafxCorruptionDetected as failure:
                if not replace_damaged or not self.damage_is_replaceable(failure):
                    raise
                self._bootstrap(payload)
                self._last_seen = 1
                publications -= 1
                if publications == 0:
                    return 1
                image = self._read_exact_image()
                header, chosen, target = self._decode_complete_image(image)
            if chosen is None and replace_damaged:
                self._bootstrap(payload)
                self._last_seen = 1
                publications -= 1
                if publications == 0:
                    return 1
                image = self._read_exact_image()
                header, chosen, target = self._decode_complete_image(image)
            observed = None if chosen is None else chosen.payload
        if expected_current is not None and observed not in expected_current:
            raise GrafxCorruptionDetected(
                "The control record changed after its publisher read the predecessor.",
                file=self._file,
                field="current_payload",
            )
        if image is None or len(image) != expected_size:
            self._bootstrap(payload)
            self._last_seen = 1
            publications -= 1
            if publications == 0:
                return 1
            image = self._read_exact_image()
            header, chosen, target = self._decode_complete_image(image)
        if header is None or chosen is None:
            raise GrafxCorruptionDetected(
                "Both slots of the control record are invalid or empty.",
                file=self._file,
                field="slots",
            )
        if chosen.generation > MAX_U64 - publications:
            raise GrafxCorruptionDetected(
                "The control record cannot advance the complete publication without wrapping.",
                file=self._file,
                field="generation",
                value=chosen.generation,
                required=publications,
            )
        generation = chosen.generation
        for _publication in range(publications):
            generation += 1
            slot_image = _encode_slot(
                header=header,
                generation=generation,
                payload=payload,
                page_size=self._storage.page_size,
            )
            self._storage.write_page(self._file, target, slot_image)
            self._storage.durable_barrier(self._file)
            target = (
                CONTROL_SLOT_PAGES[1]
                if target == CONTROL_SLOT_PAGES[0]
                else CONTROL_SLOT_PAGES[0]
            )
            self._last_seen = generation
        return generation

    @staticmethod
    def _valid_payloads(slots: tuple[_Slot, _Slot]) -> tuple[bytes, ...]:
        """Return populated, outer-valid payloads in descending generation order."""
        populated = (slot for slot in slots if slot.valid and slot.generation > 0)
        return tuple(
            slot.payload
            for slot in sorted(
                populated, key=lambda item: item.generation, reverse=True
            )
        )

    def _read_file_image(self, *, complete_legacy: bool = True) -> bytes | None:
        """Read one stable legacy payload or complete slot image through one proved descriptor.

        A two-slot record is exactly ``CONTROL_FILE_PAGES`` pages.  Reading that bounded byte
        range in one storage call performs one strict descriptor-identity proof and cannot mix
        bytes from different descriptors.  It bypasses the buffer pool just as the previous
        three ``read_page`` calls did.  Publication still uses its independent ``write_page``
        and ``durable_barrier`` descriptor proofs; only that completed sequence is acknowledged
        as durable by this store.

        ``read_log`` deliberately permits a short result.  A confirmed short file is the legacy
        v1 representation and remains migration-compatible.  A length disagreement with the
        current logical name fails closed rather than being misclassified as legacy.  Publication
        only needs the v1/v2 shape and may decline to materialise an oversized legacy payload that
        it will replace immediately.
        """
        storage = self._storage
        expected_size = CONTROL_FILE_PAGES * storage.page_size
        observed = _read_log_if_exists(storage, self._file, 0, expected_size + 1)
        if observed is None:
            return None
        image = bytes(observed)
        if len(image) == expected_size:
            return image
        observed_size = storage.file_size(self._file)
        if observed_size <= expected_size:
            if observed_size == len(image):
                return image
            raise GrafxCorruptionDetected(
                "A legacy control record changed while it was read.",
                file=self._file,
                field="length",
                expected=observed_size,
                value=len(image),
            )
        if len(image) != expected_size + 1:
            raise GrafxCorruptionDetected(
                "A legacy control record changed while it was read.",
                file=self._file,
                field="length",
                expected=observed_size,
                value=len(image),
            )
        if not complete_legacy:
            return image
        complete = bytes(storage.read_log(self._file, 0, observed_size))
        if len(complete) != observed_size:
            raise GrafxCorruptionDetected(
                "A legacy control record changed while it was read.",
                file=self._file,
                field="length",
                expected=observed_size,
                value=len(complete),
            )
        return complete

    def _read_exact_image(self) -> bytes:
        """Re-read a v2 image after a transient refusal, rejecting any length change."""
        expected_size = CONTROL_FILE_PAGES * self._storage.page_size
        image = bytes(self._storage.read_log(self._file, 0, expected_size + 1))
        if len(image) != expected_size:
            raise GrafxCorruptionDetected(
                "A two-slot control record changed length while it was read.",
                file=self._file,
                field="length",
                expected=expected_size,
                value=len(image),
            )
        return image

    def _decode_complete_image(self, image: bytes) -> tuple[_Header, _Slot | None, int]:
        """Decode one exact three-page image without taking another storage descriptor."""
        header, slots = self._decode_slots(image)
        chosen, target = self._select_slot(slots)
        return header, chosen, target

    def _decode_slots(self, image: bytes) -> tuple[_Header, tuple[_Slot, _Slot]]:
        """Decode one exact image into its bound header and both physical slots."""
        page_size = self._storage.page_size
        expected_size = CONTROL_FILE_PAGES * page_size
        if len(image) != expected_size:
            raise GrafxCorruptionDetected(
                "A two-slot control record image is not exactly three complete pages.",
                file=self._file,
                field="length",
                expected=expected_size,
                value=len(image),
            )
        header_start = CONTROL_HEADER_PAGE * page_size
        header = _decode_header(
            image[header_start : header_start + page_size],
            database_uuid=self._database_uuid,
            record_kind=self._kind,
            page_size=page_size,
            file=self._file,
        )
        slots = (
            _decode_slot(
                image[
                    CONTROL_SLOT_PAGES[0] * page_size : (CONTROL_SLOT_PAGES[0] + 1)
                    * page_size
                ],
                header=header,
                page_size=page_size,
                file=self._file,
            ),
            _decode_slot(
                image[
                    CONTROL_SLOT_PAGES[1] * page_size : (CONTROL_SLOT_PAGES[1] + 1)
                    * page_size
                ],
                header=header,
                page_size=page_size,
                file=self._file,
            ),
        )
        return header, slots

    def _read_header(self) -> _Header:
        """Read and decode the immutable page-zero binding."""
        return _decode_header(
            self._storage.read_page(self._file, CONTROL_HEADER_PAGE),
            database_uuid=self._database_uuid,
            record_kind=self._kind,
            page_size=self._storage.page_size,
            file=self._file,
        )

    def _read_slots(self, header: _Header) -> tuple[_Slot | None, int]:
        """Return the newest populated slot and the only safe page to overwrite."""
        slots = tuple(
            _decode_slot(
                self._storage.read_page(self._file, page),
                header=header,
                page_size=self._storage.page_size,
                file=self._file,
            )
            for page in CONTROL_SLOT_PAGES
        )
        return self._select_slot(slots)

    def _select_slot(self, slots: tuple[_Slot, _Slot]) -> tuple[_Slot | None, int]:
        """Select the newest valid slot and the other page as the overwrite target."""
        populated = [
            (page, slot)
            for page, slot in zip(CONTROL_SLOT_PAGES, slots, strict=True)
            if slot.valid and slot.generation > 0
        ]
        if (
            len(populated) == 2
            and populated[0][1].generation == populated[1][1].generation
        ):
            raise GrafxCorruptionDetected(
                "Both control slots claim the same non-empty generation.",
                file=self._file,
                field="generation",
                value=populated[0][1].generation,
            )
        chosen_pair = (
            max(populated, key=lambda item: item[1].generation) if populated else None
        )
        chosen = None if chosen_pair is None else chosen_pair[1]
        if chosen_pair is None:
            empty = next(
                (
                    page
                    for page, slot in zip(CONTROL_SLOT_PAGES, slots, strict=True)
                    if slot.valid and slot.generation == 0
                ),
                CONTROL_SLOT_PAGES[0],
            )
            return None, empty
        other = (
            CONTROL_SLOT_PAGES[1]
            if chosen_pair[0] == CONTROL_SLOT_PAGES[0]
            else CONTROL_SLOT_PAGES[0]
        )
        return chosen, other

    def _bootstrap(self, payload: bytes) -> None:
        """Atomically install a complete three-page file with generation one and canonical EMPTY."""
        storage = self._storage
        if storage.exists(self._temporary):
            storage.remove(self._temporary)
        storage.create(self._temporary, exclusive=True)
        first = storage.allocate(self._temporary, CONTROL_FILE_PAGES)
        if first != 0:
            raise GrafxCorruptionDetected(
                "A control slot bootstrap did not allocate from page zero.",
                file=self._temporary,
                field="first_page",
                value=first,
            )
        header = _Header(
            database_uuid=self._database_uuid,
            file_nonce=self._file_nonce,
            record_kind=self._kind,
        )
        header_body = _encode_header(
            database_uuid=self._database_uuid,
            file_nonce=self._file_nonce,
            record_kind=self._kind,
            page_size=storage.page_size,
        )
        storage.write_page(
            self._temporary,
            CONTROL_HEADER_PAGE,
            _page(
                page_type=PageType.CONTROL_HEADER,
                seq=0,
                body=header_body,
                page_size=storage.page_size,
            ),
        )
        storage.write_page(
            self._temporary,
            CONTROL_SLOT_PAGES[0],
            _encode_slot(
                header=header,
                generation=1,
                payload=payload,
                page_size=storage.page_size,
            ),
        )
        storage.write_page(
            self._temporary,
            CONTROL_SLOT_PAGES[1],
            _encode_slot(
                header=header, generation=0, payload=b"", page_size=storage.page_size
            ),
        )
        storage.durable_barrier(self._temporary)
        storage.atomic_replace(self._temporary, self._file)
        storage.durable_barrier(self._file)
