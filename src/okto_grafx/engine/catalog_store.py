"""The catalog file: the schema of a database, persisted as ordinary pages.

The catalog is not kept in a file of its own format. It is serialised into a chain of ordinary
pages of catalog.dat, written through the buffer pool like any other page, so that the write
ahead log of C4 covers it with the same records and the redo of C6 replays it with the same
rule. That is the whole reason apply_page_image exists here: recovery hands the store a page
image and the store installs it if, and only if, the image is newer than what the page holds.

Page 0 is the reserved header page. It carries the file header, whose root_page names the first
page of the chain and whose payload_length says how many of the chained bytes are meaningful.
Saving rewrites the chain in place and only grows the file when the schema no longer fits.
"""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.ids import NO_PAGE, PageIndex
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.page import (
    HEADER_PAGE_INDEX,
    FileHeader,
    FileHeaderPage,
    FileKind,
    Page,
    PageType,
    chunk_capacity,
)
from okto_grafx.engine.buffer_pool import BufferPool, read_chain, write_chain

__all__ = ["CATALOG_FILE", "MINIMUM_FRAMES", "CatalogStore"]

CATALOG_FILE: str = "catalog.dat"
"""The default name of the catalog file inside a database directory."""

MINIMUM_FRAMES: int = 2
"""Frames the catalog needs at once: one chain page being written and the header page."""


class CatalogStore:
    """Load, save and repair the catalog of one database through its own paged file."""

    __slots__ = ("_pool", "_file", "_catalog")

    def __init__(self, pool: BufferPool, *, file: str = CATALOG_FILE) -> None:
        """Bind the store to a buffer pool and a file, starting from an empty catalog."""
        if pool.capacity_pages < MINIMUM_FRAMES:
            raise GrafxConfigurationError(
                f"The catalog needs a buffer budget of at least {MINIMUM_FRAMES} pages of "
                f"{pool.page_size} bytes; the budget of {pool.budget_bytes} bytes holds "
                f"{pool.capacity_pages}.",
                field="budget_bytes",
                value=pool.budget_bytes,
            )
        self._pool: BufferPool = pool
        self._file: str = file
        self._catalog: Catalog = Catalog()

    @property
    def catalog(self) -> Catalog:
        """Return the in-memory catalog this store persists."""
        return self._catalog

    @property
    def file(self) -> str:
        """Return the name of the catalog file this store reads and writes."""
        return self._file

    @property
    def chunk_capacity(self) -> int:
        """Return how many serialised bytes one page of the catalog chain carries."""
        return chunk_capacity(self._pool.page_size)

    def is_bootstrapped(self) -> bool:
        """Return True when the catalog file exists and carries its header page."""
        storage = self._pool.storage
        return storage.exists(self._file) and storage.page_count(self._file) > 0

    def bootstrap(self) -> Catalog:
        """Create the catalog file, its header page and an empty catalog, and return it.

        Calling it on a database that already has a catalog loads that catalog instead of
        replacing it, so bootstrapping is safe to run on every open.
        """
        storage = self._pool.storage
        if not storage.exists(self._file):
            storage.create(self._file)
        if storage.page_count(self._file) == 0:
            page = self._pool.allocate(self._file, int(PageType.META))
            try:
                if page.page_index != HEADER_PAGE_INDEX:
                    raise GrafxCorruptionDetected(
                        f"The header page of {self._file!r} must be page "
                        f"{HEADER_PAGE_INDEX}; the device handed out {page.page_index}.",
                        file=self._file,
                        page=page.page_index,
                    )
                FileHeaderPage.initialize(
                    page, FileHeader(kind=FileKind.CATALOG, page_size=self._pool.page_size)
                )
            finally:
                self._pool.unpin(self._file, page.page_index, dirty=True)
            self._catalog = Catalog()
            self.save()
            return self._catalog
        return self.load()

    def load(self) -> Catalog:
        """Read the catalog from its file, replacing what this store holds in memory."""
        header = self._read_file_header()
        if header.root_page == NO_PAGE or header.payload_length == 0:
            self._catalog = Catalog()
            return self._catalog
        raw = read_chain(
            self._pool, self._file, header.root_page, page_type=int(PageType.CATALOG)
        )
        if len(raw) < header.payload_length:
            raise GrafxCorruptionDetected(
                f"The catalog of {self._file!r} declares {header.payload_length} bytes but its "
                f"chain carries {len(raw)}.",
                file=self._file,
                declared=header.payload_length,
                observed=len(raw),
            )
        self._catalog = Catalog.deserialize(raw[: header.payload_length])
        return self._catalog

    def save(self) -> tuple[PageIndex, ...]:
        """Write the in-memory catalog into its file and return the pages of its chain.

        The pages are staged through the buffer pool and are durable once the pool is flushed
        and the log that covers them has passed its barrier; the store never forces a barrier of
        its own, because the log is the authority on durability.
        """
        # The check comes first on purpose: writing the chain would allocate page 0 of a file
        # that has no header page, and page 0 is reserved in every paged file (amendment A2).
        self._require_bootstrapped()
        payload = self._catalog.serialize()
        existing = self.chain_pages()
        pages = write_chain(
            self._pool,
            self._file,
            payload,
            page_type=int(PageType.CATALOG),
            reuse=existing,
        )
        self._release(existing[len(pages) :])
        header = self._read_file_header()
        self._write_file_header(
            FileHeader(
                kind=FileKind.CATALOG,
                page_size=header.page_size,
                format_version=header.format_version,
                root_page=pages[0],
                payload_length=len(payload),
            )
        )
        return pages

    def chain_pages(self) -> tuple[PageIndex, ...]:
        """Return the pages the stored catalog occupies, in chain order."""
        if not self.is_bootstrapped():
            return ()
        header = self._read_file_header()
        if header.root_page == NO_PAGE:
            return ()
        pages: list[PageIndex] = []
        seen: set[PageIndex] = set()
        index = header.root_page
        while index != NO_PAGE:
            if index in seen:
                raise GrafxCorruptionDetected(
                    f"The catalog chain of {self._file!r} returns to page {index}.",
                    file=self._file,
                    page=index,
                )
            seen.add(index)
            pages.append(index)
            with self._pool.pinned(self._file, index) as page:
                index = page.next_page
        return tuple(pages)

    def apply_page_image(self, page_index: PageIndex, image: bytes) -> bool:
        """Install a page image if it is newer than the page, and say whether it was applied.

        This is the redo rule of CONTRACT.md section 8.5 step 6, expressed for one page: a page
        whose page_lsn already covers the image is left alone, which is what makes replaying the
        same log twice produce the same file. A page the file does not have yet is grown into
        existence first, because a crash can happen between an allocation and the write that
        followed it.
        """
        decoded = self._pool.codec.decode_page(bytes(image), verify=True)
        if not isinstance(decoded, Page):
            raise GrafxCorruptionDetected(
                f"The codec returned a {type(decoded).__name__} instead of a page image.",
                file=self._file,
                page=page_index,
            )
        self._grow_to(page_index)
        with self._pool.pinned(self._file, page_index) as page:
            if page.page_type != int(PageType.FREE) and page.page_lsn >= decoded.page_lsn:
                return False
            page.replace_with(decoded)
            return True

    # --- internals ---------------------------------------------------------------------------

    def _release(self, pages: tuple[PageIndex, ...]) -> None:
        """Empty the pages a shrinking catalog no longer needs, so no orphan chain survives.

        The pages stay in the file, because no sanctioned operation shrinks a data file (G6);
        they are simply emptied and marked free, and the next save takes them back.
        """
        for index in pages:
            with self._pool.pinned(self._file, index) as page:
                page.clear()
                page.page_type = int(PageType.FREE)
                page.next_page = NO_PAGE

    def _grow_to(self, page_index: PageIndex) -> None:
        """Allocate pages until the file has the requested index."""
        storage = self._pool.storage
        if not storage.exists(self._file):
            storage.create(self._file)
        while storage.page_count(self._file) <= page_index:
            page = self._pool.allocate(self._file, int(PageType.FREE))
            self._pool.unpin(self._file, page.page_index, dirty=True)

    def _read_file_header(self) -> FileHeader:
        """Return the file header of the catalog file."""
        self._require_bootstrapped()
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
            return FileHeaderPage.read(page)

    def _write_file_header(self, header: FileHeader) -> None:
        """Replace the file header of the catalog file."""
        self._require_bootstrapped()
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
            FileHeaderPage.write(page, header)

    def _require_bootstrapped(self) -> None:
        """Refuse to work against a catalog file that has not been created yet."""
        if not self.is_bootstrapped():
            raise GrafxCorruptionDetected(
                f"The catalog file {self._file!r} has no header page; call bootstrap() first.",
                file=self._file,
            )

    def __repr__(self) -> str:
        return f"CatalogStore(file={self._file!r}, catalog={self._catalog!r})"
