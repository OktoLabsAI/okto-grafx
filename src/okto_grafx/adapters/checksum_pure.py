"""The reference CRC-32C adapter: the pure-Python implementation, as an installable adapter.

D2 keeps the core pure Python and puts native acceleration behind a port. This is the pure half,
and it is the one the engine runs when nothing else is installed -- the fallback and the
reference, exactly as :mod:`okto_grafx.adapters.vectormath_pure` is for vector arithmetic.

It computes nothing of its own. The algorithm lives in
:func:`okto_grafx.domain.page.checksum.crc32c_reference`, because the domain must be able to
verify its own bytes without asking an adapter for permission; this module is the shape that
makes the choice of implementation an explicit, named, reportable act of composition rather than
an import side effect.
"""

from __future__ import annotations

from okto_grafx.domain.page.checksum import (
    CRC32C_INITIAL,
    PURE_IMPLEMENTATION_NAME,
    crc32c_reference,
    install_crc32c,
)

__all__ = ["PURE_ADAPTER_NAME", "PureCrc32c"]

PURE_ADAPTER_NAME: str = PURE_IMPLEMENTATION_NAME
"""The bounded label this adapter reports, matching the ``pure`` selector of DatabaseConfig."""


class PureCrc32c:
    """The table-driven CRC-32C of the domain, offered as an adapter that can be installed."""

    __slots__ = ()

    @property
    def name(self) -> str:
        """Return the bounded label this adapter reports."""
        return PURE_ADAPTER_NAME

    def checksum(self, data: bytes, crc: int = CRC32C_INITIAL) -> int:
        """Return the CRC-32C of the data, continuing a previous result when one is given."""
        return crc32c_reference(data, crc)

    def install(self) -> str:
        """Make this the implementation :func:`crc32c` calls, and say which one it replaced.

        Installing the reference is how a composition goes BACK to pure after an accelerator was
        installed, and it is deliberately the same door with the same validation rather than a
        reset: a door that skips the check because "this one is obviously fine" is a door that
        stops being evidence of anything.
        """
        return install_crc32c(crc32c_reference, name=PURE_ADAPTER_NAME)

    def __repr__(self) -> str:
        return f"PureCrc32c(name={PURE_ADAPTER_NAME!r})"
