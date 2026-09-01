"""The storage port (CONTRACT.md section 4.1, SPEC-M1 TR-2).

Every byte the engine persists crosses this boundary. The port deliberately has no arbitrary
positional write primitive: pages are allocated before they are written and logs only ever grow
at the end, which makes the beyond-end-of-file zero fill signature of the reference engine
structurally impossible to reproduce.
"""

from __future__ import annotations

from typing import Literal, Protocol, TypeAlias, runtime_checkable

from okto_grafx.domain.ids import PageIndex

__all__ = [
    "DESCRIPTOR_REVALIDATION_MODES",
    "DescriptorRevalidationMode",
    "StorageDevice",
]


DescriptorRevalidationMode: TypeAlias = Literal["strict", "generation"]
"""How a local descriptor cache proves that a logical name still owns its handle."""

DESCRIPTOR_REVALIDATION_MODES: frozenset[str] = frozenset({"strict", "generation"})
"""The closed descriptor-revalidation vocabulary shared by config and the local adapter."""


@runtime_checkable
class StorageDevice(Protocol):
    """Every byte the engine persists goes through here. There is deliberately NO
    positional write primitive: pages must be allocated first, logs are append-only.
    This makes the NTFS beyond-EOF zero-fill signature structurally impossible."""

    @property
    def name(self) -> str:
        """Short, bounded label of this device, safe to use as a metric label value."""
        ...

    @property
    def page_size(self) -> int:
        """Size in bytes of every page this device reads and writes."""
        ...

    # --- namespace ---

    def exists(self, file: str) -> bool:
        """Return True when the named file exists in this device namespace."""
        ...

    def create(self, file: str, *, exclusive: bool = True) -> None:
        """Create the named file. With exclusive set, an already existing file is an error."""
        ...

    def remove(self, file: str) -> None:
        """Delete the named file. Removing a missing file is an error."""
        ...

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        """Return every file name starting with the prefix, in a stable order."""
        ...

    def file_size(self, file: str) -> int:
        """Return the size of the named file in bytes."""
        ...

    def atomic_replace(self, source: str, target: str) -> None:
        """Move source onto target so a reader observes either the old or the new content."""
        ...

    def recycle(self, file: str) -> bool:
        """Release a file. Returns True when the space is reclaimed immediately,
        False when the platform deferred it (Windows pending-delete). Never raises
        for a still-open handle."""
        ...

    # --- paged space ---

    def page_count(self, file: str) -> int:
        """Return how many pages the named file currently holds."""
        ...

    def allocate(self, file: str, count: int = 1) -> PageIndex:
        """Grow the file by count pages, zero-filled. Returns the first new index."""
        ...

    def read_page(self, file: str, page_index: PageIndex) -> bytes:
        """Return exactly page_size bytes from an already allocated page."""
        ...

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        """The data must be exactly page_size bytes and page_index must already be allocated,
        otherwise the device raises a typed failure such as GrafxCorruptionDetected."""
        ...

    # --- bounded byte reads and append-only log writes ---

    def append_log(self, file: str, payload: bytes) -> int:
        """Append and return the new total size. A partial append must raise GrafxDeviceFull."""
        ...

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Fill length bytes from any named file unless EOF is reached; never change its shape."""
        ...

    def log_size(self, file: str) -> int:
        """Return the current size in bytes of an append-only file."""
        ...

    def truncate_log(self, file: str, size: int) -> None:
        """Shrink only. Growing is a programming error and raises GrafxUnsupportedOperation."""
        ...

    # --- durability ---

    def durable_barrier(self, file: str | None = None) -> None:
        """Flush the file to stable storage (and its directory on POSIX). None means every open
        file. Failure raises GrafxDurabilityBarrierFailed."""
        ...
