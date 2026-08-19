"""The page codec port (CONTRACT.md section 4.5).

Encoding a page and checksumming it are one contract, because the checksum is computed over the
encoded bytes and must be verified before anything trusts them. The concrete CRC-32C codec is an
adapter (C1); the domain only ever knows this shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # The page model is delivered by C1 in wave W1 and re-exported from its package.
    from okto_grafx.domain.page import Page

__all__ = ["PageCodec"]


@runtime_checkable
class PageCodec(Protocol):
    """Turn a page into exactly page_size bytes and back, with an integrity check on the way in."""

    @property
    def format_version(self) -> int:
        """On-disk format version this codec writes. Older versions stay readable."""
        ...

    def checksum(self, payload: bytes) -> int:
        """Return the CRC-32C (Castagnoli) checksum of the payload."""
        ...

    def encode_page(self, page: Page) -> bytes:
        """Serialise the page, returning exactly page_size bytes."""
        ...

    def decode_page(self, raw: bytes, *, verify: bool = True) -> Page:
        """Parse page bytes. With verify set, a checksum failure raises GrafxCorruptionDetected."""
        ...
