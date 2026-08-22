"""The version 1 page codec: page images in, pages out, CRC-32C in between (CONTRACT.md 4.5).

The codec is an adapter because the port says so, not because it needs a mechanism: it opens no
file and reads no clock. What it owns is the promise the port makes to the rest of the engine --
that encode_page returns exactly page_size bytes with a correct checksum, and that decode_page
either returns a page whose structure has been proved or raises GrafxCorruptionDetected saying
where the damage is.

The checksum is CRC-32C (Castagnoli), the polynomial the on-disk format froze in section 6.3. The
arithmetic itself lives in the domain, in okto_grafx.domain.page.checksum, so the page layer can
verify its own bytes; this adapter is where the port meets it. An accelerated codec added later
must produce the same bytes for the same page, which is what makes the known answer tests in
tests/storage_core/test_checksum.py the contract rather than the implementation.
"""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import PageIndex
from okto_grafx.domain.page import (
    DEFAULT_PAGE_SIZE,
    Page,
    crc32c,
    validate_page_size,
)

__all__ = ["PAGE_CODEC_FORMAT_VERSION", "PageCodecV1"]

PAGE_CODEC_FORMAT_VERSION: int = 1
"""The on-disk page format this codec writes. Every earlier version stays readable."""


class PageCodecV1:
    """Encode and decode page images of one fixed page size, checksummed with CRC-32C."""

    __slots__ = ("_page_size",)

    def __init__(self, page_size: int = DEFAULT_PAGE_SIZE) -> None:
        """Bind the codec to the page size of the device it will serve."""
        self._page_size: int = validate_page_size(page_size)

    @property
    def format_version(self) -> int:
        """Return the on-disk page format version this codec writes."""
        return PAGE_CODEC_FORMAT_VERSION

    @property
    def page_size(self) -> int:
        """Return the page size this codec encodes and expects to decode."""
        return self._page_size

    def checksum(self, payload: bytes) -> int:
        """Return the CRC-32C of the payload."""
        return crc32c(bytes(payload))

    def encode_page(self, page: Page) -> bytes:
        """Return the image of the page: exactly page_size bytes with a correct checksum."""
        if not isinstance(page, Page):
            raise GrafxCorruptionDetected(
                f"This codec encodes Page values; got {type(page).__name__}.",
                field="page",
                value=type(page).__name__,
            )
        if page.page_size != self._page_size:
            raise GrafxCorruptionDetected(
                f"This codec encodes pages of {self._page_size} bytes; the page declares "
                f"{page.page_size}.",
                field="page_size",
                value=page.page_size,
                page=page.page_index,
                codec_page_size=self._page_size,
            )
        image = page.to_bytes()
        if len(image) != self._page_size:
            raise GrafxCorruptionDetected(
                f"A page image must be exactly {self._page_size} bytes; the encoder produced "
                f"{len(image)}.",
                field="page_image",
                value=len(image),
                page=page.page_index,
            )
        return image

    def decode_page(
        self, raw: bytes, *, verify: bool = True, page_index: PageIndex | None = None
    ) -> Page:
        """Parse a page image into a page, proving its structure and, when asked, its checksum.

        The structural proof always runs. Skipping it would mean handing back a page whose slot
        directory points outside its own payload area, and a caller cannot tell the difference
        between that and a real record until it has already read the wrong bytes. Only the
        checksum comparison is optional, because a caller that is deliberately inspecting a
        damaged page still needs to see what is inside it.

        The page index is not part of the port: the port has no room for it, and only the buffer
        pool that issued the read knows it. It is accepted here as a keyword so a caller that
        does know the location can have it named in the failure. Without it the failure says
        nothing about which page it was, which is better than claiming a page it cannot know.

        That covers the FAILURE. A page that decodes successfully through the port carries index
        zero -- Page has to store some number and zero is the only one available -- so every
        refusal it raises afterwards names page zero, which is a real page and a wrong answer. A
        caller reaching this through the PageCodec port cannot pass the keyword at all, so it
        must stamp what it knows: `page.page_index = index` immediately after decoding. Both of
        C1's own decode sites do exactly that, and a caller that forgets is the defect C6 found.
        """
        return Page.from_bytes(
            bytes(raw), page_size=self._page_size, page_index=page_index, verify=verify
        )
