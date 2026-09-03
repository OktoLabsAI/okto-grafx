"""D-09 / P1.7: one encoding of the local WAL image, proved against the old generator.

Before this change a page this process materialised was logged as
``encode(frame) -> decode(verify) -> stamp -> encode``; a WAL segment roll repeated the decode
and encode on the logged bytes. Now the page is ``copy(frame) -> stamp -> encode``, the copy is
kept and a roll re-stamps it. These tests pin, in order: that the copy is deep and field
complete; that the new generator produces the same bytes as the old one for a corpus of pages;
that a real commit decodes a locally materialised page exactly once (the apply after the
barrier) and a pre-staged one exactly twice (verification plus apply); that a forced segment
roll adds no decode; and that a corrupt pre-staged image is still refused before the log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import NO_CSN, NO_PAGE, PROVISIONAL_CSN, RecordRef
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RecordHeader
from okto_grafx.domain.page import Page, PageType
from okto_grafx.domain.wal.record import WalRecordType
from okto_grafx.engine.txn_manager import _RowWrite
from okto_grafx.domain.txn.records import decode_page_write
from txn_support import Stack, make_page_image

HEAP = "heap.dat"


# --- corpus -----------------------------------------------------------------------------------


def _header(
    record_id: int, *, xmin: int, xmax: int = NO_CSN, payload_len: int = 4
) -> bytes:
    return RecordHeader(
        record_id=record_id,
        xmin=xmin,
        xmax=xmax,
        prev_version=RecordRef(NO_PAGE, 0).encode(),
        payload_len=payload_len,
        schema_version=1,
        flags=0,
        reserved=0,
    ).encode()


def _corpus(page_size: int) -> Iterator[tuple[str, Page]]:
    """Pages that exercise every header field and directory shape the copy must carry."""
    yield "fresh", Page(int(PageType.HEAP), page_size=page_size, page_index=3)

    filled = Page(
        int(PageType.HEAP), page_size=page_size, page_index=4, page_lsn=7, seq=9
    )
    for ordinal in range(5):
        filled.insert_slot(_header(ordinal, xmin=PROVISIONAL_CSN) + b"row%d" % ordinal)
    yield "heap_rows", filled

    freed = Page(int(PageType.HEAP), page_size=page_size, page_index=5, flags=0x5A5A)
    slots = [freed.insert_slot(b"x" * (10 + ordinal)) for ordinal in range(6)]
    freed.free_slot(slots[1])
    freed.free_slot(slots[4])
    yield "freed_slots", freed

    relocated = Page(
        int(PageType.HEAP), page_size=page_size, page_index=6, next_page=77
    )
    first = relocated.insert_slot(b"short")
    relocated.insert_slot(b"neighbour")
    relocated.update_slot(first, b"a much longer payload that must relocate")
    yield "relocated_slot", relocated

    compacted = Page(
        int(PageType.HEAP), page_size=page_size, page_index=8, page_lsn=123456789
    )
    kept = [compacted.insert_slot(b"k" * 20) for _ in range(4)]
    compacted.free_slot(kept[0])
    compacted.free_slot(kept[2])
    compacted.compact()
    yield "compacted", compacted

    reserved = Page(
        int(PageType.INDEX_HASH), page_size=page_size, page_index=9, seq=2, flags=1
    )
    reserved._reserved = 0x1234
    reserved.insert_slot(b"bucket-entry")
    yield "index_reserved", reserved

    overflow = Page(
        int(PageType.OVERFLOW), page_size=page_size, page_index=10, next_page=11
    )
    overflow.insert_slot(b"o" * (page_size // 2))
    yield "overflow", overflow

    full = Page(int(PageType.HEAP), page_size=page_size, page_index=12)
    while full.can_fit(24):
        full.insert_slot(b"f" * 24)
    yield "exactly_full", full


# --- deep copy --------------------------------------------------------------------------------


@pytest.mark.parametrize("page_size", [512, 8192])
def test_a_page_copy_carries_every_field_and_shares_nothing(page_size: int) -> None:
    for label, page in _corpus(page_size):
        before = page.to_bytes()
        was_dirty = page.dirty
        clone = page.copy()
        assert clone == page, label
        assert clone.page_index == page.page_index, label
        assert clone._reserved == page._reserved, label
        assert clone.free_start == page.free_start, label
        assert clone.to_bytes() == before, label
        assert clone._data is not page._data and clone._slots is not page._slots, label
        # Mutate the copy in every way the commit path or a later caller could.
        clone.page_lsn = 999_999
        clone.seq = page.seq + 1
        clone.flags = page.flags ^ 0xFFFF
        clone.next_page = 4242
        clone._reserved = page._reserved + 1
        if clone.can_fit(3):
            clone.insert_slot(b"new")
        if clone.live_slots():
            clone.free_slot(clone.live_slots()[0])
            clone.compact()
        assert page.to_bytes() == before, (
            f"{label}: the original changed under the copy"
        )
        assert page.dirty == was_dirty, label


# --- byte identity against the old generator -----------------------------------------------


def _old_generator(stack: Stack, page: Page, csn: int, rows: list[_RowWrite]) -> bytes:
    """encode -> decode(verify) -> stamp -> encode: exactly what _committed_image used to do."""
    image = stack.codec.encode_page(page)
    decoded = stack.codec.decode_page(image, verify=True)
    stack.manager._stamp_page(decoded, HEAP, page.page_index, csn, rows)
    return stack.codec.encode_page(decoded)


def _new_generator(stack: Stack, page: Page, csn: int, rows: list[_RowWrite]) -> bytes:
    """copy -> stamp -> encode: what _local_image does on the resident frame."""
    clone = page.copy()
    stack.manager._stamp_page(clone, HEAP, page.page_index, csn, rows)
    return stack.codec.encode_page(clone)


def test_the_single_encode_image_is_byte_identical_to_the_old_generator(
    make_stack: Callable[..., Stack],
) -> None:
    for page_size in (512, 8192):
        stack = make_stack(page_size=page_size, db_label=f"d09-{page_size}")
        for label, page in _corpus(page_size):
            rows: list[_RowWrite] = []
            if label == "heap_rows":
                rows = [
                    _RowWrite(born=RecordRef(page.page_index, 0), ended=None),
                    _RowWrite(born=None, ended=RecordRef(page.page_index, 2)),
                    _RowWrite(
                        born=RecordRef(page.page_index, 3),
                        ended=RecordRef(page.page_index, 4),
                    ),
                ]
            for csn in (1, 55, PROVISIONAL_CSN - 8):
                old = _old_generator(stack, page, csn, rows)
                new = _new_generator(stack, page, csn, rows)
                assert new == old, (label, page_size, csn)
                # A second stamp to a later number (the retarget) agrees as well.
                clone = page.copy()
                stack.manager._stamp_page(clone, HEAP, page.page_index, csn, rows)
                stack.manager._stamp_page(clone, HEAP, page.page_index, csn + 3, rows)
                assert stack.codec.encode_page(clone) == _old_generator(
                    stack, page, csn + 3, rows
                ), label
            assert page.page_lsn != 55, label
            if label == "heap_rows":
                stamped = stack.codec.decode_page(new, verify=True)
                born = RecordHeader.decode(stamped.read_slot(0)[:RECORD_HEADER_SIZE])
                ended = RecordHeader.decode(stamped.read_slot(2)[:RECORD_HEADER_SIZE])
                assert (
                    born.xmin == PROVISIONAL_CSN - 8
                    and ended.xmax == PROVISIONAL_CSN - 8
                )
                untouched = RecordHeader.decode(page.read_slot(0)[:RECORD_HEADER_SIZE])
                assert (
                    untouched.xmin == PROVISIONAL_CSN
                )  # the frame value was never stamped


# --- a real commit: decode counts ------------------------------------------------------------


class _DecodeCounter:
    """Attribute codec.decode_page calls to the three doors D-09 changes.

    Reads from the device decode pages too (catalog, heap, index buckets, page-0 certificates,
    index staging inside _build_records), so a whole-commit count says nothing about D-09. What
    is counted: decodes issued inside ``_local_image`` (must be zero: the frame is copied, not
    re-read), inside ``_committed_image`` (exactly one per pre-staged image: the verification),
    and decodes inside ``_retarget_commit_batch`` whose bytes are one of the images being
    retargeted (must be zero: the validated page values are re-stamped instead).
    """

    def __init__(self, codec: object, manager: object) -> None:
        self.codec_type = type(codec)
        self.manager_type = type(manager)
        self.local_calls = 0
        self.local_decodes = 0
        self.committed_calls = 0
        self.committed_decodes = 0
        self.retarget_calls = 0
        self.retarget_image_decodes = 0
        self._region: list[str] = []
        self._retarget_images: set[bytes] = set()
        self._decode = self.codec_type.decode_page
        self._local = self.manager_type._local_image  # type: ignore[attr-defined]
        self._committed = self.manager_type._committed_image  # type: ignore[attr-defined]
        self._retarget = self.manager_type._retarget_commit_batch  # type: ignore[attr-defined]

    def __enter__(self) -> "_DecodeCounter":
        counter = self
        decode, local, committed, retarget = (
            self._decode,
            self._local,
            self._committed,
            self._retarget,
        )

        def counted(
            codec_self: object, raw: bytes, *args: object, **kwargs: object
        ) -> object:
            if counter._region:
                region = counter._region[-1]
                if region == "local":
                    counter.local_decodes += 1
                elif region == "committed":
                    counter.committed_decodes += 1
                elif region == "retarget" and bytes(raw) in counter._retarget_images:
                    counter.retarget_image_decodes += 1
            return decode(codec_self, raw, *args, **kwargs)

        def in_local(manager_self: object, *args: object, **kwargs: object) -> object:
            counter.local_calls += 1
            counter._region.append("local")
            try:
                return local(manager_self, *args, **kwargs)
            finally:
                counter._region.pop()

        def in_committed(
            manager_self: object, *args: object, **kwargs: object
        ) -> object:
            counter.committed_calls += 1
            counter._region.append("committed")
            try:
                return committed(manager_self, *args, **kwargs)
            finally:
                counter._region.pop()

        def in_retarget(
            manager_self: object,
            txn: object,
            records: object,
            images: object,
            *args: object,
            **kwargs: object,
        ) -> object:
            counter.retarget_calls += 1
            counter._retarget_images = {bytes(image) for _file, _page, image in images}  # type: ignore[union-attr]
            counter._region.append("retarget")
            try:
                return retarget(manager_self, txn, records, images, *args, **kwargs)
            finally:
                counter._region.pop()

        setattr(self.codec_type, "decode_page", counted)
        setattr(self.manager_type, "_local_image", in_local)
        setattr(self.manager_type, "_committed_image", in_committed)
        setattr(self.manager_type, "_retarget_commit_batch", in_retarget)
        return self

    def __exit__(self, *exc: object) -> None:
        setattr(self.codec_type, "decode_page", self._decode)
        setattr(self.manager_type, "_local_image", self._local)
        setattr(self.manager_type, "_committed_image", self._committed)
        setattr(self.manager_type, "_retarget_commit_batch", self._retarget)


class _AppliedImages:
    """Capture the images handed to _apply_images: exactly the images the log carries."""

    def __init__(self, manager: object) -> None:
        self.manager_type = type(manager)
        self.groups: list[list[tuple[str, int, bytes]]] = []
        self._original = self.manager_type._apply_images  # type: ignore[attr-defined]

    @property
    def images(self) -> list[tuple[str, int, bytes]]:
        return [image for group in self.groups for image in group]

    @property
    def last_commit(self) -> list[tuple[str, int, bytes]]:
        """The images of the last commit applied (an identity-lease commit may precede it)."""
        return self.groups[-1] if self.groups else []

    def __enter__(self) -> "_AppliedImages":
        original = self._original
        capture = self

        def capturing(manager_self: object, images: object) -> object:
            capture.groups.append(list(images))  # type: ignore[arg-type]
            return original(manager_self, images)

        setattr(self.manager_type, "_apply_images", capturing)
        return self

    def __exit__(self, *exc: object) -> None:
        setattr(self.manager_type, "_apply_images", self._original)


def test_a_locally_materialised_page_is_decoded_only_by_the_apply_after_the_barrier(
    tmp_path: Path,
) -> None:
    with okto_grafx.connect(tmp_path / "db", page_size=512) as database:
        with database.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
        codec = database._pool.codec
        with (
            _AppliedImages(database._transactions) as applied,
            _DecodeCounter(codec, database._transactions) as counter,
        ):
            with database.begin("write") as writer:
                writer.execute(
                    "CREATE (:Person {id: 1, name: 'one'}), (:Person {id: 2, name: 'two'})"
                )
        writes = applied.images
        assert writes, "the commit logged no page image"
        # Every image came from a copied frame; building them decoded nothing.
        assert counter.local_calls == len(writes)
        assert counter.local_decodes == 0
        assert counter.committed_calls == 0
        csn = database.transactions.published_lsn()
        for _file, _page_index, image in applied.last_commit:
            assert codec.decode_page(image, verify=True).page_lsn == csn
        assert (
            database.execute("MATCH (p:Person) RETURN p.name").rows
            and not database.verify("all").findings
        )


def test_a_pre_staged_image_is_verified_once_and_applied_once(stack: Stack) -> None:
    txn = stack.manager.begin("write")
    txn.owner._stage_page_image(
        txn, HEAP, 4, make_page_image(stack.codec, [b"trusted"], page_index=4)
    )
    txn.note_write(stack.manager.partition_of(1, b"trusted"))
    with _DecodeCounter(stack.codec, stack.manager) as counter:
        report = stack.manager.commit(txn)
    assert report.durable is True
    assert (
        counter.committed_calls == 1 and counter.committed_decodes == 1
    )  # the verification
    assert counter.local_calls == 0
    writes = [
        r for r in stack.wal.records() if r.record_type == WalRecordType.WRITE_PAGE
    ]
    logged = decode_page_write(writes[-1].payload).image
    assert stack.codec.decode_page(logged, verify=True).page_lsn == report.csn


def test_a_corrupt_pre_staged_image_is_refused_before_the_log_is_touched(
    stack: Stack,
) -> None:
    txn = stack.manager.begin("write")
    image = bytearray(make_page_image(stack.codec, [b"trusted"], page_index=4))
    image[-1] ^= (
        0xFF  # damage the last payload/directory byte: the checksum no longer matches
    )
    txn.owner._stage_page_image(txn, HEAP, 4, bytes(image))
    txn.note_write(stack.manager.partition_of(1, b"trusted"))
    before = tuple(stack.wal.records())
    with pytest.raises(GrafxCorruptionDetected):
        stack.manager.commit(txn)
    assert tuple(stack.wal.records()) == before
    stack.manager.rollback(txn)


def test_a_forced_segment_roll_retargets_without_decoding_the_images_again(
    tmp_path: Path,
) -> None:
    with okto_grafx.connect(
        tmp_path / "db", page_size=512, wal_segment_bytes=4096
    ) as database:
        with database.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
        manager = database._transactions
        codec = database._pool.codec
        retargets = 0
        committed = 0
        try:
            for ordinal in range(40):
                with (
                    _AppliedImages(manager) as applied,
                    _DecodeCounter(codec, manager) as counter,
                ):
                    with database.begin("write") as writer:
                        writer.execute(
                            "CREATE (:Person {id: $id, name: $name})",
                            {"id": 100 + ordinal, "name": "n" * 40},
                        )
                committed += 1
                writes = applied.images
                assert writes, f"commit {ordinal} logged no page image"
                assert counter.local_decodes == 0, (
                    f"commit {ordinal} re-read a frame it was copying"
                )
                assert counter.retarget_image_decodes == 0, (
                    f"commit {ordinal} decoded a logged image again on retarget"
                )
                retargets += counter.retarget_calls
                csn = database.transactions.published_lsn()
                for _file, _page_index, image in applied.last_commit:
                    assert codec.decode_page(image, verify=True).page_lsn == csn
                if retargets:
                    break
        finally:
            pass
        assert retargets >= 1, (
            "no commit crossed a 4 KiB segment; the roll path was not exercised"
        )
        assert not database.verify("all").findings
    with okto_grafx.connect(
        tmp_path / "db", page_size=512, wal_segment_bytes=4096
    ) as reopened:
        assert not reopened.verify("all").findings
        assert len(reopened.execute("MATCH (p:Person) RETURN p.id").rows) == committed
