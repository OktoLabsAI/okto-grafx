"""NumPy-backed version-1 page codec.

The adapter changes computation, never storage: every encoded byte is identical to
``PageCodecV1`` and both adapters read the same page format. NumPy is imported only in this
optional adapter, keeping the default package and the domain free of third-party dependencies.

Small directories deliberately use the pure codec. Creating NumPy arrays costs more than the
short Python loop at that size; the vector path starts only where its bounded operations win.
With native CRC-32C installed, the Windows/Python 3.13 calibration measured encode at 17 slots
as 16.1 -> 12.2 us and decode at 97 slots as 67.6 -> 50.4 us. Invalid images return to the pure
decoder so public exception type, text and details remain owned by one correctness oracle.
"""

from __future__ import annotations

import struct

import numpy

from okto_grafx.adapters.codec_v1 import PAGE_CODEC_FORMAT_VERSION, PageCodecV1
from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import PageIndex
from okto_grafx.domain.page import Page, crc32c, validate_page_size
from okto_grafx.domain.page.layout import (
    CHECKSUM_SIZE,
    PAGE_HEADER_SIZE,
    SLOT_ENTRY_SIZE,
    PageHeader,
)

__all__ = [
    "NUMPY_DECODE_MIN_SLOTS",
    "NUMPY_ENCODE_MIN_SLOTS",
    "NumpyPageCodecV1",
]

NUMPY_ENCODE_MIN_SLOTS: int = 16
"""First directory size for which vectorised packing is selected."""

NUMPY_DECODE_MIN_SLOTS: int = 96
"""First directory size for which vectorised validation is selected."""

_SLOT_DTYPE = numpy.dtype("<u2")
_SLOT_COUNT = struct.Struct("<H")


class NumpyPageCodecV1:
    """Encode dense slot directories with NumPy while preserving the v1 byte contract."""

    __slots__ = ("_page_size", "_pure")

    def __init__(self, page_size: int) -> None:
        self._page_size = validate_page_size(page_size)
        self._pure = PageCodecV1(self._page_size)

    @property
    def format_version(self) -> int:
        """Return the supported on-disk page codec version."""
        return PAGE_CODEC_FORMAT_VERSION

    @property
    def page_size(self) -> int:
        """Return the configured size of one encoded page."""
        return self._page_size

    def checksum(self, payload: bytes) -> int:
        """Return the same process-selected CRC-32C used by the pure codec."""

        return crc32c(bytes(payload))

    def encode_page(self, page: Page) -> bytes:
        """Encode one page, using vectorized directory packing only when worthwhile."""

        if not isinstance(page, Page):
            return self._pure.encode_page(page)
        if type(page) is not Page:
            return self._pure.encode_page(page)
        if page.page_size != self._page_size:
            return self._pure.encode_page(page)
        if page.slot_count < NUMPY_ENCODE_MIN_SLOTS:
            return self._pure.encode_page(page)

        image = bytearray(self._page_size)
        image[PAGE_HEADER_SIZE : page._free_start] = page._data[
            PAGE_HEADER_SIZE : page._free_start
        ]
        try:
            directory = numpy.asarray(page._slots, dtype=numpy.int64)
            if directory.shape != (page.slot_count, 2):
                return self._pure.encode_page(page)
            if bool(numpy.any(directory < 0)) or bool(numpy.any(directory > 0xFFFF)):
                return self._pure.encode_page(page)
            directory_start = self._page_size - page.slot_count * SLOT_ENTRY_SIZE
            image[directory_start:] = directory.astype(_SLOT_DTYPE)[::-1].tobytes(
                order="C"
            )
        except (OverflowError, TypeError, ValueError):
            # A privately corrupted Page must still receive the oracle's classified refusal.
            return self._pure.encode_page(page)

        image[0:PAGE_HEADER_SIZE] = page.header().encode()
        checksum = crc32c(bytes(image[CHECKSUM_SIZE : self._page_size]))
        image[0:CHECKSUM_SIZE] = checksum.to_bytes(CHECKSUM_SIZE, "little")
        encoded = bytes(image)
        if (
            len(encoded) != self._page_size
        ):  # pragma: no cover - bytearray fixes the length
            raise GrafxCorruptionDetected(
                f"A page image must be exactly {self._page_size} bytes; the encoder produced "
                f"{len(encoded)}.",
                field="page_image",
                value=len(encoded),
                page=page.page_index,
            )
        return encoded

    def decode_page(
        self,
        raw: bytes,
        *,
        verify: bool = True,
        page_index: PageIndex | None = None,
    ) -> Page:
        """Decode one page; invalid and small directories retain the pure oracle path."""

        raw_bytes = bytes(raw)
        if len(raw_bytes) != self._page_size:
            return self._pure.decode_page(
                raw_bytes,
                verify=verify,
                page_index=page_index,
            )
        (slot_count,) = _SLOT_COUNT.unpack_from(raw_bytes, 20)
        if slot_count < NUMPY_DECODE_MIN_SLOTS:
            return self._pure.decode_page(
                raw_bytes,
                verify=verify,
                page_index=page_index,
            )
        header = PageHeader.decode(raw_bytes)
        if verify:
            expected = crc32c(bytes(raw_bytes[CHECKSUM_SIZE : self._page_size]))
            if expected != header.checksum:
                return self._pure.decode_page(
                    raw_bytes,
                    verify=True,
                    page_index=page_index,
                )
        directory_bytes = header.slot_count * SLOT_ENTRY_SIZE
        if (
            PAGE_HEADER_SIZE + directory_bytes > self._page_size
            or header.free_end != self._page_size - directory_bytes
            or not PAGE_HEADER_SIZE <= header.free_start <= header.free_end
        ):
            return self._pure.decode_page(
                raw_bytes,
                verify=False,
                page_index=page_index,
            )

        try:
            directory = numpy.frombuffer(
                raw_bytes,
                dtype=_SLOT_DTYPE,
                count=header.slot_count * 2,
                offset=header.free_end,
            ).reshape((-1, 2))[::-1]
            offsets = directory[:, 0]
            lengths = directory[:, 1]
            live = (offsets != 0) | (lengths != 0)
            ends = offsets.astype(numpy.uint32) + lengths
            outside = live & ((offsets < PAGE_HEADER_SIZE) | (ends > header.free_start))
            if bool(numpy.any(outside)):
                return self._pure.decode_page(
                    raw_bytes,
                    verify=False,
                    page_index=page_index,
                )
            occupied = directory[live].astype(numpy.uint32, copy=False)
            if occupied.shape[0] > 1:
                occupied_ends = occupied[:, 0] + occupied[:, 1]
                order = numpy.lexsort((occupied_ends, occupied[:, 0]))
                occupied = occupied[order]
                if bool(
                    numpy.any(occupied[:-1, 0] + occupied[:-1, 1] > occupied[1:, 0])
                ):
                    return self._pure.decode_page(
                        raw_bytes,
                        verify=False,
                        page_index=page_index,
                    )
            entries = [tuple(pair) for pair in directory.tolist()]
        except (OverflowError, TypeError, ValueError):
            return self._pure.decode_page(
                raw_bytes,
                verify=False,
                page_index=page_index,
            )

        return Page._from_decoded(
            header,
            entries,
            raw_bytes,
            page_size=self._page_size,
            page_index=page_index,
        )
