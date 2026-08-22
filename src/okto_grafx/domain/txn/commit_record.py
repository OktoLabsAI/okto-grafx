"""What C5 adds around the COMMIT payload: the file numbers a page touch carries.

The payload itself is NOT defined here. ``CommitPayload``, ``PageTouch`` and their limits are the
frozen layout of CONTRACT.md section 6.5, which is a WAL record format, and C4 owns
``domain/wal/**``. Two encoders of one frozen layout is the integration failure amendment A24 was
written about, so this module imports that one and re-exports it under the name the transaction
layer reads it by. What is genuinely C5's is here: turning a FILE NAME into the 16-bit number the
payload stores, which the log format has no opinion about.
"""

from __future__ import annotations

from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.wal.commit import (
    MAX_FILE_ID,
    MAX_PAGE_TOUCHES_PER_COMMIT,
    MAX_PARTITIONS_PER_COMMIT,
    CommitPayload,
    PageTouch,
)

__all__ = [
    "CATALOG_FILE_ID",
    "FIRST_DERIVED_FILE_ID",
    "HEAP_FILE_ID",
    "MAX_FILE_ID",
    "MAX_PAGE_TOUCHES_PER_COMMIT",
    "MAX_PARTITIONS_PER_COMMIT",
    "CommitPayload",
    "FileIdMap",
    "PageTouch",
]

HEAP_FILE_ID: int = 1
"""The file number a page touch carries for the heap."""

CATALOG_FILE_ID: int = 2
"""The file number a page touch carries for the catalog."""

FIRST_DERIVED_FILE_ID: int = 3
"""The first number available to a file that is not one of the two well-known ones."""


@dataclass(frozen=True, slots=True)
class FileIdMap:
    """Turns a file name into the 16-bit number a page touch carries.

    The two paged files every database has get fixed numbers. Anything else -- an index file, and
    there can be many with names only the catalog knows -- gets a number derived from its name,
    so two processes reading the same log agree on what they are looking at without consulting a
    table that is itself being recovered.

    The touch list is a RECORD of what a commit reached, not the instruction that replays it:
    redo works from the WRITE_PAGE records, which name their file in full (TR-4). That is what
    makes a derived number acceptable here and unacceptable there.
    """

    heap_file: str = "heap.dat"
    catalog_file: str = "catalog.dat"

    def id_of(self, file: str) -> int:
        """Return the file number for this name."""
        if not isinstance(file, str) or not file:
            raise GrafxConfigurationError(
                "A page touch must name a non-empty file.", field="file", value=repr(file)
            )
        if file == self.heap_file:
            return HEAP_FILE_ID
        if file == self.catalog_file:
            return CATALOG_FILE_ID
        span = MAX_FILE_ID + 1 - FIRST_DERIVED_FILE_ID
        return FIRST_DERIVED_FILE_ID + crc32c(file.encode("utf-8")) % span
