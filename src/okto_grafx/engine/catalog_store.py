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

from collections.abc import Callable

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxTransactionStateError,
    GrafxQueryBudgetExceeded,
)
from okto_grafx.domain.ids import NO_PAGE, PROVISIONAL_CSN, PageIndex
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
from okto_grafx.engine.buffer_pool import (
    BufferPool,
    apply_page_image,
    build_chain_images,
    read_chain,
    refuse_endless_chain,
    visited_pages,
    write_chain,
)

__all__ = ["CATALOG_FILE", "MINIMUM_FRAMES", "CatalogStore"]

CATALOG_FILE: str = "catalog.dat"
"""The default name of the catalog file inside a database directory."""

MINIMUM_FRAMES: int = 2
"""Frames the catalog needs at once: one chain page being written and the header page."""


def read_catalog_page_images(
    images: tuple[tuple[int, bytes], ...], *, page_size: int, sequence: int,
) -> Catalog:
    """Decode a complete staged catalog, never consulting or adopting physical pages.

    CatalogStore.stage always includes header, complete chain and released pages.
    This checks that contract from WAL after-images even across partial apply.
    It returns data, not physical authority or permission to replay the images.
    """
    def refuse(field: str) -> GrafxCorruptionDetected:
        """Construct a located corruption error for an invalid catalog image set."""
        return GrafxCorruptionDetected("Invalid complete catalog page-image set.", file=CATALOG_FILE, field=field)

    if type(sequence) is not int or not 0 < sequence < PROVISIONAL_CSN:
        raise refuse("commit_sequence")
    if type(images) is not tuple or len(images) < 2:
        raise refuse("catalog_images")
    pages: dict[int, Page] = {}
    for index, raw in images:
        if type(index) is not int or not 0 <= index < NO_PAGE or index in pages:
            raise refuse("catalog_image_address")
        if type(raw) is not bytes or len(raw) != page_size:
            raise refuse("catalog_image_size")
        page = Page.from_bytes(raw, page_index=index)
        if page.page_lsn != sequence or page.seq % 2 or page.flags or page.header().reserved:
            raise refuse("catalog_image_stamp")
        pages[index] = page
    header_page = pages.get(HEADER_PAGE_INDEX)
    if header_page is None or header_page.page_type != PageType.META or header_page.slot_count != 1 or header_page.next_page != NO_PAGE:
        raise refuse("catalog_image_header")
    header = FileHeaderPage.read(header_page)
    if header.kind != FileKind.CATALOG or header.page_size != page_size:
        raise refuse("catalog_image_header")
    if not 0 < header.payload_length <= (len(pages) - 1) * chunk_capacity(page_size):
        raise refuse("catalog_image_coverage")
    chunks: list[bytes] = []
    visited: set[int] = {HEADER_PAGE_INDEX}
    index = header.root_page
    while index != NO_PAGE:
        if index in visited or index not in pages or len(visited) >= len(pages):
            raise refuse("catalog_image_chain")
        visited.add(index)
        page = pages[index]
        if page.page_type != PageType.CATALOG or page.slot_count != 1 or page.is_slot_free(0):
            raise refuse("catalog_image_chunk")
        chunks.append(page.read_slot(0))
        index = page.next_page
    if sum(map(len, chunks)) != header.payload_length:
        raise refuse("catalog_image_coverage")
    for index, page in pages.items():
        if index not in visited and (page.page_type != PageType.FREE or page.slot_count or page.next_page != NO_PAGE):
            raise refuse("catalog_image_unreferenced")
    return Catalog.deserialize(b"".join(chunks))


def read_published_catalog(read: Callable[[str, int], bytes], *, page_size: int,
                           max_bytes: int) -> tuple[Catalog, int]:
    """Capture current metadata without adopting it into an older participant's schema view.

    The caller supplies fresh reads and owns publication/stamp qualification.
    Only the named catalog chain is read; no retained history is scanned and no
    shared authority cache is created. Native complete-image grammar is reused.
    """
    raw = read(CATALOG_FILE, 0)
    page = Page.from_bytes(raw, page_index=0)
    header = FileHeaderPage.read(page)
    if header.kind != FileKind.CATALOG or header.page_size != page_size:
        raise GrafxCorruptionDetected("Invalid temporal metadata header.", field="catalog_header")
    if header.payload_length > max_bytes:
        raise GrafxQueryBudgetExceeded("Temporal metadata capture exceeds byte budget.", resource="history_scan")
    images = [(0, raw)]
    seen = {0}
    number = header.root_page
    while number != NO_PAGE:
        if number in seen:
            raise GrafxCorruptionDetected("Cyclic temporal metadata chain.", field="catalog_chain")
        if (len(images) + 1) * page_size > max_bytes:
            raise GrafxQueryBudgetExceeded("Temporal metadata capture exceeds byte budget.", resource="history_scan")
        seen.add(number)
        chunk = read(CATALOG_FILE, number)
        images.append((number, chunk))
        number = Page.from_bytes(chunk, page_index=number).next_page
    return read_catalog_page_images(tuple(images), page_size=page_size, sequence=page.page_lsn), len(images) * page_size


class CatalogStore:
    """Load, save and repair the catalog of one database through its own paged file."""

    __slots__ = (
        "_pool",
        "_file",
        "_catalog",
        "_loaded_epoch",
        "_persisted_image",
        "_bootstrapped_epoch",
    )

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
        # The catalog as the PAGES last held it, kept as the IMAGE rather than the object: a
        # reference would be the very object _catalog goes on mutating, so the two would always
        # compare equal and every unsaved change would look saved. Anything _catalog holds beyond
        # this image is a change of this caller's, and that difference is what tells a refresh
        # whether re-reading would destroy something (D7).
        self._persisted_image: bytes = Catalog().serialize()
        # The epoch this store's in-memory catalog belongs to. It is the same discipline the
        # heap applies to its tail: every holder of a derived answer reads the epoch before it
        # trusts what it derived. It is set here rather than left empty until the first load so
        # that the invariant is total -- a store that never read is still a store whose empty
        # catalog would overwrite whatever a replay put under it.
        self._loaded_epoch: int = self._pool.structure_epoch(self._file)
        # This memo belongs only to internal require doors. ``is_bootstrapped`` remains an
        # authoritative physical probe every time a public caller asks it.
        self._bootstrapped_epoch: int | None = None

    def has_unsaved_changes(self) -> bool:
        """Return True when this store holds a catalog the pages do not.

        Asked only when the pages have moved, so the serialisation it costs is paid on a
        boundary and never on a read that finds nothing has happened.
        """
        return self._catalog.serialize() != self._persisted_image

    def persisted_image(self) -> bytes:
        """Return the immutable catalog image last adopted from the current page view.

        This does not refresh or serialize the mutable catalog object.  Transaction boundaries
        use the value on both sides of :meth:`refresh` to distinguish a cache/view invalidation
        from a real catalog-authority change without rescanning every index artifact.
        """

        return self._persisted_image

    def rebase_unsaved_view_if_persisted_unchanged(self) -> bool:
        """Rebase a dirty live catalog only when durable authority is unchanged.

        Establishing a first or unproved read view may discard clean catalog frames even when
        no catalog page changed.  That cache event moves :meth:`_view_epoch`, and the ordinary
        :meth:`refresh` door must then refuse a live catalog with unsaved changes because
        replacing it would lose the caller's work.

        Transaction authority synchronization has one safe narrower answer: read the newly
        attached pages without adopting them and compare their complete canonical image with
        the image this live catalog was derived from.  Equality proves that only the local view
        changed, so advancing the view epoch preserves the unsaved delta.  A different image,
        malformed pages, or any read failure is never converted into permission; the caller
        proceeds through the established refusing refresh path (or propagates corruption).

        This door deliberately does nothing for a clean catalog.  Clean state belongs to
        :meth:`refresh`, which must adopt the newly read object rather than merely move a token.
        """

        current = self._view_epoch()
        if current == self._loaded_epoch or not self.has_unsaved_changes():
            return False
        observed = self.read_from_pages().serialize()
        if observed != self._persisted_image:
            return False
        self._loaded_epoch = current
        return True

    def refresh(self) -> bool:
        """Re-read the catalog when the pages under it may have moved, and say whether it did.

        The catalog in memory is a DERIVED answer, exactly like the heap's remembered tail, and
        the rule this component states for every derived answer applies to it: read the epoch
        before trusting what you derived. It was the one holder that never did -- a foreign
        CREATE TABLE stayed invisible to a live participant even after the pool dropped every
        frame it had, because dropping frames says nothing to an object that is not holding one.

        Re-reading is refused rather than performed when this store has unsaved changes of its
        own, because performing it would discard them -- the loss D7 exists to prevent. The way
        out is the same one save() names: read_from_pages(), merge, adopt().
        """
        current = self._view_epoch()
        if current == self._loaded_epoch:
            return False
        if self.has_unsaved_changes():
            raise GrafxTransactionStateError(
                f"The catalog of {self._file!r} has unsaved changes and its pages have moved "
                f"since it was read, so re-reading it would discard them; read_from_pages(), "
                f"merge what you still want, then adopt() it.",
                file=self._file,
                field="structure_epoch",
                loaded_epoch=self._loaded_epoch,
                current_epoch=current,
            )
        self.adopt(self.read_from_pages())
        return True

    def _view_epoch(self) -> int:
        """Return the reading a re-read is owed against.

        Both halves, deliberately. A relink of these pages changes what a walk over them finds,
        and a cache drop is how a participant says it may no longer be looking at the same file
        at all -- which is precisely the foreign-commit case. save() reads only the structural
        half because a drop moves no link; a READ has to distrust both.
        """
        return self._pool.structure_epoch(self._file) + self._pool.cache_drop_epoch(self._file)

    @property
    def catalog(self) -> Catalog:
        """Return the catalog this store holds, re-reading it when the pages have moved.

        The check is here rather than at a boundary door a caller has to remember, for the same
        reason the heap's tail cache checks itself: a participant that forgets gets a plausible
        wrong answer, and every door that reads a table name comes through this one.
        """
        self.refresh()
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
        """Return True when page 0 of the catalog file really is its reserved header page.

        The question is not "does the file have any pages". Amendment A22 lets recovery grow the
        file before anything has reserved page 0, so a catalog file can exist, have pages, and
        still have no header. A page 0 that is pristine has never been written and is reservable;
        anything else that is not a catalog header page is damage, and damage is raised rather
        than reported as "not bootstrapped", because bootstrap() must never overwrite a header
        page that merely failed to be understood (G6).
        """
        storage = self._pool.storage
        if not storage.exists(self._file) or storage.page_count(self._file) == 0:
            self._bootstrapped_epoch = None
            return False
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
            if page.is_pristine():
                self._bootstrapped_epoch = None
                return False
            self._require_header_page(page)
        self._bootstrapped_epoch = self._pool.derived_epoch(self._file)
        return True

    def bootstrap(self) -> Catalog:
        """Create the catalog file, reserve its header page, and return the catalog it holds.

        Calling it on a database that already has a catalog loads that catalog instead of
        replacing it, so bootstrapping is safe to run on every open. A file that a redo grew
        before anything reserved page 0 is repaired here rather than refused for good.
        """
        storage = self._pool.storage
        if not storage.exists(self._file):
            storage.create(self._file)
        if self.is_bootstrapped():
            return self.load()
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
                self._reserve_header_page(page)
            finally:
                self._pool.unpin(self._file, page.page_index, dirty=True)
        else:
            # The file already reaches past page 0 without anyone having reserved it, which is
            # what a redo of a later page leaves behind. The page is free, so it is reserved in
            # place. Whatever the redo wrote further on is NOT adopted: with no header there is
            # no root to trust, and guessing which page starts a chain is reconstruction, which
            # belongs to the verifier of C6. Those pages stay in the file, unreferenced.
            with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
                self._reserve_header_page(page)
        self._catalog = Catalog()
        # Bootstrap is the one door that decides what the file holds rather than reading it. It
        # has just reserved page 0 itself, and it deliberately does not adopt whatever a redo
        # left further on, so the empty catalog it is about to write belongs to the structure as
        # it stands right here.
        self._loaded_epoch = self._view_epoch()
        self.save()
        return self._catalog

    def _reserve_header_page(self, page: Page) -> None:
        """Turn page 0 into the reserved header page of this catalog file."""
        FileHeaderPage.initialize(
            page, FileHeader(kind=FileKind.CATALOG, page_size=self._pool.page_size)
        )

    def _require_header_page(self, page: Page) -> FileHeader:
        """Return the file header of page 0, refusing a page that is not one."""
        if page.page_type != int(PageType.META):
            raise GrafxCorruptionDetected(
                f"Page {HEADER_PAGE_INDEX} of {self._file!r} is of type {page.page_type} and "
                f"is not the reserved header page.",
                file=self._file,
                page=HEADER_PAGE_INDEX,
                page_type=page.page_type,
                expected_page_type=int(PageType.META),
            )
        header = FileHeaderPage.read(page)
        if header.kind is not FileKind.CATALOG:
            raise GrafxCorruptionDetected(
                f"Page {HEADER_PAGE_INDEX} of {self._file!r} carries the header of a "
                f"{header.kind.name.lower()} file, not of a catalog.",
                file=self._file,
                page=HEADER_PAGE_INDEX,
                kind=header.kind.name,
            )
        if header.page_size != self._pool.page_size:
            raise GrafxCorruptionDetected(
                f"The catalog file {self._file!r} was written with pages of "
                f"{header.page_size} bytes, and this database uses {self._pool.page_size}.",
                file=self._file,
                page=HEADER_PAGE_INDEX,
                page_size=header.page_size,
            )
        return header

    def read_from_pages(self) -> Catalog:
        """Return the catalog the file currently holds, WITHOUT replacing the one in memory.

        The non-destructive half of load(). A caller that has been refused a save because the
        pages moved under it needs to see what they moved to before it decides what to keep, and
        the only door that existed for that was load(), which throws its tables away to answer
        the question (D7, round 8). This one answers it and changes nothing.
        """
        header = self._read_file_header()
        if header.root_page == NO_PAGE or header.payload_length == 0:
            return Catalog()
        self._require_root_page(header.root_page)
        raw = read_chain(
            self._pool, self._file, header.root_page, page_type=int(PageType.CATALOG)
        )
        if len(raw) < header.payload_length:
            raise GrafxCorruptionDetected(
                f"The catalog of {self._file!r} declares {header.payload_length} bytes but its "
                f"chain carries {len(raw)}.",
                file=self._file,
                field="payload_length",
                declared=header.payload_length,
                observed=len(raw),
            )
        return Catalog.deserialize(raw[: header.payload_length])

    def adopt(self, catalog: Catalog) -> Catalog:
        """Take this catalog as the one derived from the pages as they stand right now.

        The other half of the non-destructive remedy: read_from_pages(), merge whatever the
        caller still wants, adopt the result, save. Adopting is the caller SAYING that what it
        holds describes the current pages, which is exactly the claim save() refuses to assume.
        """
        self._catalog = catalog
        self._persisted_image = catalog.serialize()
        self._loaded_epoch = self._view_epoch()
        return self._catalog

    def load(self) -> Catalog:
        """Read the catalog from its file, replacing what this store holds in memory.

        DESTRUCTIVE by definition: whatever this store held is gone afterwards. It is the right
        door when nothing in memory is worth keeping, and the wrong one as the remedy for a
        refused save -- read_from_pages() and adopt() are that.
        """
        return self.adopt(self.read_from_pages())

    def save(self) -> tuple[PageIndex, ...]:
        """Write the in-memory catalog into its file and return the pages of its chain.

        The pages go through the buffer pool. They are on the device once the pool is flushed
        and on the platter once a checkpoint barriers it; a save does neither on its own,
        because the log is the authority on durability and the redo of these pages is idempotent
        (CONTRACT.md section 8.5 step 6). BufferPool.checkpoint is the path that makes them
        durable, and it is the one that times the barrier as target data (A25).

        This is the door for a change that is ALREADY SETTLED -- bootstrap creating an empty
        catalog, recovery adopting what it replayed. It is the wrong door for a change that can
        still be refused, twice over: the pages are reachable from the moment it returns, and the
        pages it returns are the CHAIN only, so a caller that logs exactly what it is handed
        never logs page 0, and the file header reaches the device outside every log record.
        :meth:`stage` is the transactional door and answers with both.
        """
        # The check comes first on purpose: writing the chain would allocate page 0 of a file
        # that has no header page, and page 0 is reserved in every paged file (amendment A2).
        self._require_bootstrapped()
        self._require_derived_from_current_pages()
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
        self._persisted_image = payload
        self._loaded_epoch = self._view_epoch()
        return pages

    def stage(self, catalog: Catalog) -> tuple[tuple[PageIndex, bytes], ...]:
        """Return the page images this catalog needs, WITHOUT installing any of them.

        THE PROBLEM THIS EXISTS FOR. ``save()`` writes through the buffer pool the moment it is
        called, so a schema statement that is still allowed to be REFUSED had already made its
        pages reachable: the frames sat in the pool, and the next flush of the catalog file
        carried them to the device whether or not the transaction that asked for them ever
        committed. Three participants each creating a table left the file header describing one
        catalog and the chain carrying another -- *"declares 125 bytes but its chain carries
        63"* -- and the harm is not confined to a refusal, because ``save()`` returns the CHAIN
        pages only. A caller that staged exactly what it was handed staged everything except the
        one page that says where the chain starts and how much of it is meaningful, so even a
        commit that succeeded put its header on the device through a pool flush, covered by no
        log record and unreachable to redo.

        Both halves are closed here by answering with a VALUE. Nothing is written, nothing is
        allocated, this store is not modified in any way, and a transaction that is refused
        discards the tuple and has changed nothing anywhere. It is the shape a row write already
        has: staged when the caller asks, applied at the commit number, absent if the commit
        never happens.

        The tuple carries every page the change touches, in a fixed order:

        1. the pages of the new chain, in chain order;
        2. the pages a shrinking chain surrenders, emptied and marked free;
        3. **page 0, always**, carrying the root and the payload length of the new chain.

        Page 0 is last because it is what makes the chain reachable, and it is unconditional
        because the write set of a catalog change has to be total. ``TransactionContext``
        declares interest in every page image staged on it (defect E1), so two participants
        changing the schema at once always intersect on page 0 and one of them is refused --
        which does not depend on their chain pages happening to overlap, and would not hold if
        this door reported only the pages whose bytes it noticed changing.

        The images are already encoded and are handed straight to the transaction::

            catalog = store.read_from_pages()      # the copy: what the pages hold right now
            catalog.add_table(...)                 # the change, on the copy
            for page_index, image in store.stage(catalog):
                txn.stage_page_image(store.file, page_index, image)

        Nothing here refreshes what this store holds, and that is deliberate rather than
        forgotten: a marker saying "the pages hold this" must never be set from a catalog that a
        refusal can still throw away (LESSONS L21). The store finds out the ordinary way -- the
        commit applies these images through :func:`apply_page_image`, which announces that the
        structure of this file moved, and the ``catalog`` property re-reads on that signal.
        """
        self._require_stageable(catalog)
        self._require_bootstrapped()
        payload = catalog.serialize()
        existing = self.chain_pages()
        images = list(
            build_chain_images(
                self._pool,
                self._file,
                payload,
                page_type=int(PageType.CATALOG),
                reuse=existing,
            )
        )
        chain = tuple(index for index, _image in images)
        images.extend(self._released_images(existing[len(chain) :]))
        images.append(self._header_image(chain[0], len(payload)))
        return tuple(images)

    def _require_stageable(self, catalog: Catalog) -> None:
        """Refuse to stage anything but a catalog, and refuse this store's own live catalog.

        The identity check is the structural half of LESSONS L21. Staging exists so that a
        change is built on a COPY and the live store learns about it only if the commit happens;
        handing back the very object ``_catalog`` names defeats that silently. The images would
        be correct, the commit would succeed, and this store would then be holding a catalog its
        ``_persisted_image`` no longer describes -- so ``has_unsaved_changes()`` would say True
        forever and the refresh the commit triggers would refuse rather than re-read, leaving a
        wedged store behind a transaction that worked.

        Two things that must be independent must not be allowed to become the same object, and
        the only place that can be enforced is the door they arrive through.
        """
        if not isinstance(catalog, Catalog):
            raise GrafxConfigurationError(
                f"A catalog change is staged from a Catalog; got {type(catalog).__name__}.",
                field="catalog",
                value=type(catalog).__name__,
            )
        if catalog is self._catalog:
            raise GrafxTransactionStateError(
                f"The catalog of {self._file!r} cannot stage the very object this store holds, "
                f"because a refused change would still be in it and a committed one would leave "
                f"the store unable to re-read; build the change on read_from_pages() instead.",
                file=self._file,
                field="catalog",
            )

    def _released_images(
        self, pages: tuple[PageIndex, ...]
    ) -> list[tuple[PageIndex, bytes]]:
        """Return the images that empty the pages a shrinking chain no longer needs.

        The staged twin of :meth:`_release`, and it refuses page 0 for the same reason that one
        does. Reaching it needs a chain walk that returned the reserved page, which
        :meth:`chain_pages` closes at both ends -- so this is a guard behind a guard, kept
        because what it prevents is a staged image that would clear the file header at commit
        time and take every table with it, and when a component can destroy, the tie goes to
        preservation (LESSONS L17).
        """
        images: list[tuple[PageIndex, bytes]] = []
        for index in pages:
            if index == HEADER_PAGE_INDEX:
                raise GrafxCorruptionDetected(
                    f"Page {HEADER_PAGE_INDEX} of {self._file!r} is the reserved header page "
                    f"and is never released.",
                    file=self._file,
                    page=index,
                )
            page = Page(
                int(PageType.FREE), page_size=self._pool.page_size, page_index=index
            )
            images.append((index, self._pool.codec.encode_page(page)))
        return images

    def _header_image(
        self, root_page: PageIndex, payload_length: int
    ) -> tuple[PageIndex, bytes]:
        """Return the image page 0 would hold once this chain became the stored catalog.

        The resident page is read and never touched. The bytes it currently holds are decoded
        into a page of this function's own, and the new header is written into THAT -- so the
        frame keeps whatever it had, stays clean, and a caller that discards the staged tuple
        has left the file header exactly as it found it.
        """
        header = self._read_file_header()
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
            resident = self._pool.codec.encode_page(page)
        image = self._pool.codec.decode_page(resident, verify=True)
        # The codec port carries no page index, so a decoded page reports 0 whatever it came
        # from -- which is the right number here, and stamped rather than inherited so that the
        # field is true by statement and not by luck (the same reason apply_page_image stamps).
        image.page_index = HEADER_PAGE_INDEX
        FileHeaderPage.write(
            image,
            FileHeader(
                kind=FileKind.CATALOG,
                page_size=header.page_size,
                format_version=header.format_version,
                root_page=root_page,
                payload_length=payload_length,
            ),
        )
        return HEADER_PAGE_INDEX, self._pool.codec.encode_page(image)

    def chain_pages(self) -> tuple[PageIndex, ...]:
        """Return the pages the stored catalog occupies, in chain order."""
        if not self.is_bootstrapped():
            return ()
        header = self._read_file_header()
        if header.root_page == NO_PAGE:
            return ()
        self._require_root_page(header.root_page)
        pages: list[PageIndex] = []
        seen: set[PageIndex] = visited_pages()
        limit = self._pool.storage.page_count(self._file) + 1
        index = header.root_page
        while index != NO_PAGE:
            refuse_endless_chain(self._file, len(pages) + 1, limit)
            if index in seen:
                raise GrafxCorruptionDetected(
                    f"The catalog chain of {self._file!r} returns to page {index}.",
                    file=self._file,
                    page=index,
                    field="cycle",
                )
            seen.add(index)
            with self._pool.pinned(self._file, index) as page:
                if page.page_type != int(PageType.CATALOG):
                    raise GrafxCorruptionDetected(
                        f"Page {index} of {self._file!r} is of type {page.page_type} and is "
                        f"not part of the catalog chain.",
                        file=self._file,
                        page=index,
                        page_type=page.page_type,
                    )
                following = page.next_page
            pages.append(index)
            if following == HEADER_PAGE_INDEX:
                raise GrafxCorruptionDetected(
                    f"Page {index} of {self._file!r} chains to the reserved header page.",
                    file=self._file,
                    page=index,
                    field="next_page",
                )
            index = following
        return tuple(pages)

    def apply_page_image(self, page_index: PageIndex, image: bytes) -> bool:
        """Install a page image if it is newer than the page, and say whether it was applied.

        This is the redo rule of CONTRACT.md section 8.5 step 6 and amendment A22, applied to the
        catalog file. The in-memory catalog is deliberately not refreshed here: recovery replays
        many pages and then calls load() once, and reparsing a chain that is still half replayed
        would only produce a catalog nobody asked for.
        """
        return apply_page_image(self._pool, self._file, page_index, image)

    # --- internals ---------------------------------------------------------------------------

    def _require_derived_from_current_pages(self) -> None:
        """Refuse to write a catalog that was read before the pages under it changed.

        A redo replays catalog pages through apply_page_image, which announces that the structure
        of this file has moved. A store still holding the catalog it parsed before that replay
        would write it straight back over the replayed one, and everything the replay restored --
        a table, and with it every row of that table -- would be gone with no error raised.

        Reloading instead of refusing would be worse: it would silently discard whatever the
        caller had added in memory. So the caller is told -- and told something it can act on
        without losing anything: read_from_pages() shows what the pages now hold, the caller
        merges what it still wants, adopt() states that the result describes those pages, and
        save() then writes it. Naming load() as the remedy made the documented cure perform the
        very loss this guard exists to prevent (D7, round 8).

        The code is transaction_state and deliberately not stale_epoch. GrafxStaleEpoch is the
        writer lease of C3, and what a caller does about it is stop writing and hand over; what a
        caller does about this one is load and try again. Two recoveries that different must not
        arrive under one code (A11-revised).

        It reads structure_epoch and not derived_epoch on purpose. Dropping the cache moves no
        link on the device, so a plain invalidate() must not refuse a save; folding the two
        readings together made it do exactly that, with a remedy that would have destroyed the
        caller's tables (D7, round 8).

        Proven by test_saving_after_a_replay_does_not_overwrite_what_was_replayed,
        test_the_refusal_after_a_replay_names_the_non_destructive_remedy,
        test_dropping_the_cache_does_not_refuse_a_save and
        test_two_saves_in_a_row_from_one_store_are_allowed -- the last two being what keep this
        guard from being satisfied by refusing everything (A85).
        """
        current = self._view_epoch()
        if self._loaded_epoch != current:
            raise GrafxTransactionStateError(
                f"The catalog of {self._file!r} was read before its pages changed underneath, so "
                f"saving it now would write over what changed them; load it again first.",
                file=self._file,
                field="structure_epoch",
                loaded_epoch=self._loaded_epoch,
                current_epoch=current,
            )

    def _require_root_page(self, root_page: PageIndex) -> PageIndex:
        """Refuse a chain root that names the reserved header page.

        A root of 0 is four damaged bytes, not an empty chain. Reading through it would parse the
        file header as a catalog chunk, and writing through it would put the reserved page into
        the reuse list of the next save, which would clear the file header and stamp the page
        CATALOG: a detectable corruption turned into permanent loss by this engine (A2, G6).
        Every door that trusts the root asks this first, so all of them refuse for the same
        reason and none of them reaches a pin before doing it.
        """
        if root_page == HEADER_PAGE_INDEX:
            raise GrafxCorruptionDetected(
                f"The catalog of {self._file!r} names page {HEADER_PAGE_INDEX} as the root of "
                f"its chain, and that page is the reserved header page.",
                file=self._file,
                page=root_page,
                field="root_page",
            )
        return root_page

    def _release(self, pages: tuple[PageIndex, ...]) -> None:
        """Empty the pages a shrinking catalog no longer needs, so no orphan chain survives.

        The pages stay in the file, because no sanctioned operation shrinks a data file (G6);
        they are simply emptied and marked free, and the next save takes them back.
        """
        # These pages were part of the chain until this call empties them, and nothing here
        # announces that -- deliberately. _release is reached only from save(), immediately after
        # write_chain has rewritten the very pages it was given for reuse, and that call already
        # says the structure of this file changed. A second announcement for the same change is
        # not defence in depth: break either and the other answers, so neither can be shown to
        # work (A67). The counterfactual lives on the write_chain bump.
        for index in pages:
            if index == HEADER_PAGE_INDEX:
                raise GrafxCorruptionDetected(
                    f"Page {HEADER_PAGE_INDEX} of {self._file!r} is the reserved header page "
                    f"and is never released.",
                    file=self._file,
                    page=index,
                )
            with self._pool.pinned(self._file, index) as page:
                page.clear()
                page.page_type = int(PageType.FREE)
                page.next_page = NO_PAGE

    def _read_file_header(self) -> FileHeader:
        """Return the file header of the catalog file."""
        self._require_bootstrapped()
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
            return self._require_header_page(page)

    def _write_file_header(self, header: FileHeader) -> None:
        """Replace the file header of the catalog file."""
        self._require_bootstrapped()
        with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
            FileHeaderPage.write(page, header)

    def _require_bootstrapped(self) -> None:
        """Refuse to work against a catalog file that has not been created yet."""
        if self._bootstrapped_epoch == self._pool.derived_epoch(self._file):
            # Reuse only the device-existence/page-count proof.  A resident header can still be
            # modified without moving the pool's derived epoch, so keep the cheap hot-frame pin
            # and structural validation that prevents an in-memory corruption from being hidden
            # behind this optimization.
            with self._pool.pinned(self._file, HEADER_PAGE_INDEX) as page:
                self._require_header_page(page)
            return
        if not self.is_bootstrapped():
            raise GrafxCorruptionDetected(
                f"The catalog file {self._file!r} has no header page; call bootstrap() first.",
                file=self._file,
            )

    def __repr__(self) -> str:
        return f"CatalogStore(file={self._file!r}, catalog={self._catalog!r})"
