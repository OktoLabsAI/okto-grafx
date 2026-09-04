"""TXN-4 groups MVCC header stamps once instead of scanning every row per page."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from types import SimpleNamespace

import pytest

from okto_grafx.domain.ids import NO_CSN, NO_PAGE, PROVISIONAL_CSN, RecordRef
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RecordHeader
from okto_grafx.domain.page import Page, PageType
from okto_grafx.domain.txn.records import WalRecordType, decode_page_write
from okto_grafx.engine.txn_manager import (
    TransactionManager,
    _MaterializedAttempt,
    _RowWrite,
)
from txn_support import Stack, build_stack

HEAP = "heap.dat"


def _header(record_id: int) -> bytes:
    return RecordHeader(
        record_id=record_id,
        xmin=PROVISIONAL_CSN,
        xmax=NO_CSN,
        prev_version=RecordRef(NO_PAGE, 0).encode(),
        payload_len=3,
        schema_version=1,
        flags=0,
        reserved=0,
    ).encode()


def _page(page_index: int, *, slots: int = 4) -> Page:
    page = Page(int(PageType.HEAP), page_size=512, page_index=page_index)
    for slot in range(slots):
        page.insert_slot(_header(page_index * 10 + slot) + b"row")
    return page


def _rows() -> tuple[_RowWrite, ...]:
    alpha = SimpleNamespace(table_id=1, name="Alpha")
    beta = SimpleNamespace(table_id=2, name="Beta")
    return (
        _RowWrite(born=RecordRef(3, 0), ended=None, table=alpha),
        _RowWrite(born=None, ended=RecordRef(4, 1), table=beta),
        _RowWrite(
            born=RecordRef(5, 0),
            ended=RecordRef(3, 1),
            table=alpha,
        ),
        _RowWrite(
            born=RecordRef(4, 2),
            ended=RecordRef(5, 2),
            table=beta,
        ),
        _RowWrite(
            born=RecordRef(3, 2),
            ended=RecordRef(3, 3),
            table=beta,
        ),
    )


def _canonical_stamp(
    stack: Stack,
    page: Page,
    file: str,
    page_index: int,
    csn: int,
    rows: Sequence[_RowWrite],
) -> None:
    """Literal pre-TXN-4 loop, kept independent from the grouping implementation."""
    if page.page_lsn < csn:
        page.page_lsn = csn
    if file == HEAP:
        for item in rows:
            if item.born is not None and item.born.page == page_index:
                stack.manager._restamp_page(page, item.born, xmin=csn)
            if item.ended is not None and item.ended.page == page_index:
                stack.manager._restamp_page(page, item.ended, xmax=csn)


def _stage_pages(stack: Stack, pages: Sequence[Page]) -> object:
    transaction = stack.manager.begin("write")
    for page in pages:
        transaction.owner._stage_page_image(
            transaction,
            HEAP,
            page.page_index,
            stack.codec.encode_page(page),
        )
    return transaction


def test_grouped_stamps_preserve_complete_wal_and_page_bytes_across_pages_and_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    optimized = build_stack(db_label="stamp-optimized")
    canonical = build_stack(db_label="stamp-canonical")
    pages = tuple(_page(page_index) for page_index in (3, 4, 5))
    optimized_txn = _stage_pages(optimized, pages)
    canonical_txn = _stage_pages(canonical, pages)
    optimized_rows = _rows()
    canonical_rows = _rows()

    try:
        grouping_calls: list[tuple[int, ...]] = []
        original_group = TransactionManager._group_page_stamps

        def count_grouping(
            manager: TransactionManager, rows: Sequence[_RowWrite]
        ) -> object:
            if manager is optimized.manager:
                grouping_calls.append(tuple(id(row) for row in rows))
            return original_group(manager, rows)

        with monkeypatch.context() as counted_grouping:
            counted_grouping.setattr(
                TransactionManager,
                "_group_page_stamps",
                count_grouping,
            )
            optimized_records, optimized_images, optimized_csn = (
                optimized.manager._build_records(optimized_txn, 7, optimized_rows)
            )
        assert grouping_calls == [tuple(id(row) for row in optimized_rows)]
        original_stamp = TransactionManager._stamp_page

        def stamp_with_canonical_loop(
            manager: TransactionManager,
            page: Page,
            file: str,
            page_index: int,
            csn: int,
            stamps: object,
        ) -> None:
            if manager is canonical.manager:
                _canonical_stamp(
                    canonical,
                    page,
                    file,
                    page_index,
                    csn,
                    canonical_rows,
                )
                return
            original_stamp(
                manager,
                page,
                file,
                page_index,
                csn,
                stamps,  # type: ignore[arg-type]
            )

        with monkeypatch.context() as canonical_path:
            canonical_path.setattr(
                TransactionManager,
                "_stamp_page",
                stamp_with_canonical_loop,
            )
            canonical_records, canonical_images, canonical_csn = (
                canonical.manager._build_records(canonical_txn, 7, canonical_rows)
            )

        assert optimized_csn == canonical_csn
        assert optimized_images == canonical_images
        assert optimized_records == canonical_records
        attempt = optimized.manager._materialized
        assert attempt is not None and attempt.page_stamps is not None
        assert [
            (stamp.reference, stamp.is_birth) for stamp in attempt.page_stamps[3]
        ] == [
            (RecordRef(3, 0), True),
            (RecordRef(3, 1), False),
            (RecordRef(3, 2), True),
            (RecordRef(3, 3), False),
        ]
        row_images = [
            location
            for location in optimized_images
            if location[0] == HEAP and location[1] in {3, 4, 5}
        ]
        assert [location[:2] for location in row_images] == [
            (HEAP, 3),
            (HEAP, 4),
            (HEAP, 5),
        ]

        for _file, page_index, image in row_images:
            stamped = optimized.codec.decode_page(image, verify=True)
            assert stamped.page_lsn == optimized_csn
            if page_index == 3:
                assert (
                    RecordHeader.decode(stamped.read_slot(0)[:RECORD_HEADER_SIZE]).xmin
                    == optimized_csn
                )
                assert (
                    RecordHeader.decode(stamped.read_slot(1)[:RECORD_HEADER_SIZE]).xmax
                    == optimized_csn
                )
                assert (
                    RecordHeader.decode(stamped.read_slot(2)[:RECORD_HEADER_SIZE]).xmin
                    == optimized_csn
                )
                assert (
                    RecordHeader.decode(stamped.read_slot(3)[:RECORD_HEADER_SIZE]).xmax
                    == optimized_csn
                )
            elif page_index == 4:
                assert (
                    RecordHeader.decode(stamped.read_slot(1)[:RECORD_HEADER_SIZE]).xmax
                    == optimized_csn
                )
                assert (
                    RecordHeader.decode(stamped.read_slot(2)[:RECORD_HEADER_SIZE]).xmin
                    == optimized_csn
                )
            else:
                assert (
                    RecordHeader.decode(stamped.read_slot(0)[:RECORD_HEADER_SIZE]).xmin
                    == optimized_csn
                )
                assert (
                    RecordHeader.decode(stamped.read_slot(2)[:RECORD_HEADER_SIZE]).xmax
                    == optimized_csn
                )

        # Page indexes are file-local.  Even a colliding catalog page receives only page_lsn;
        # heap-header actions must never be interpreted against its payload.
        catalog_page = Page(
            int(PageType.CATALOG), page_size=512, page_index=3, page_lsn=2
        )
        catalog_page.insert_slot(b"catalog payload")
        canonical_catalog = catalog_page.copy()
        optimized_catalog = catalog_page.copy()
        _canonical_stamp(
            optimized,
            canonical_catalog,
            "catalog.dat",
            3,
            optimized_csn,
            optimized_rows,
        )
        optimized.manager._stamp_page(
            optimized_catalog,
            "catalog.dat",
            3,
            optimized_csn,
            attempt.page_stamps[3],
        )
        assert optimized.codec.encode_page(optimized_catalog) == (
            optimized.codec.encode_page(canonical_catalog)
        )
        assert optimized_catalog.read_slot(0) == b"catalog payload"

        retargeted_records, retargeted_images = (
            optimized.manager._retarget_commit_batch(
                optimized_txn,
                optimized_records,
                optimized_images,
                optimized_rows,
                old_csn=optimized_csn,
                new_csn=optimized_csn + 7,
                epoch=7,
            )
        )
        with monkeypatch.context() as canonical_retarget:
            canonical_retarget.setattr(
                TransactionManager,
                "_stamp_page",
                stamp_with_canonical_loop,
            )
            canonical_retargeted_records, canonical_retargeted_images = (
                canonical.manager._retarget_commit_batch(
                    canonical_txn,
                    canonical_records,
                    canonical_images,
                    canonical_rows,
                    old_csn=canonical_csn,
                    new_csn=canonical_csn + 7,
                    epoch=7,
                )
            )
        assert retargeted_images == canonical_retargeted_images
        assert retargeted_records == canonical_retargeted_records
    finally:
        optimized.manager._materialized = None
        canonical.manager._materialized = None
        optimized.manager.rollback(optimized_txn)
        canonical.manager.rollback(canonical_txn)


class _CountingRows:
    """Sequence-like row source that records every effect yielded to classification."""

    def __init__(self, rows: Sequence[_RowWrite]) -> None:
        self.rows = tuple(rows)
        self.visited: list[int] = []

    def __iter__(self) -> Iterator[_RowWrite]:
        for row in self.rows:
            self.visited.append(id(row))
            yield row


@pytest.mark.parametrize("page_count", [1, 8, 32, 128])
def test_stamp_work_curve_classifies_each_row_once_and_visits_only_local_effects(
    page_count: int,
) -> None:
    stack = build_stack(db_label=f"stamp-curve-{page_count}")
    table = SimpleNamespace(table_id=1, name="Rows")
    rows = tuple(
        _RowWrite(
            born=RecordRef(page_index, 0),
            ended=None,
            table=table,
        )
        for page_index in range(1, page_count + 1)
    )
    counted = _CountingRows(rows)
    canonical = _CountingRows(rows)

    plan = stack.manager._group_page_stamps(counted)  # type: ignore[arg-type]
    stamp_visits = 0
    for page_index in range(1, page_count + 1):
        _canonical_stamp(
            stack,
            _page(page_index, slots=1),
            HEAP,
            page_index,
            11,
            canonical,  # type: ignore[arg-type]
        )
        stamps = plan.get(page_index, ())
        stamp_visits += len(stamps)
        stack.manager._stamp_page(
            _page(page_index, slots=1),
            HEAP,
            page_index,
            11,
            stamps,
        )

    assert counted.visited == [id(row) for row in rows]
    assert stamp_visits == page_count
    assert sum(len(stamps) for stamps in plan.values()) == page_count
    assert len(canonical.visited) == page_count * len(rows)
    assert len(counted.visited) + stamp_visits == 2 * page_count


def test_retarget_reuses_the_attempt_stamp_plan_without_reclassifying_rows(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = stack.manager.begin("write")
    rows = (_RowWrite(born=RecordRef(6, 0), ended=None),)
    plan = stack.manager._group_page_stamps(rows)
    page = _page(6, slots=1)
    stack.manager._stamp_page(page, HEAP, 6, 10, plan[6])
    image = stack.codec.encode_page(_page(6, slots=1))

    class _Commit:
        record_type = int(WalRecordType.COMMIT)
        epoch = 1

    stack.manager._materialized = _MaterializedAttempt(
        txn_id=int(transaction.txn_id),
        csn=10,
        pages={(HEAP, 6): page},
        page_stamps=plan,
    )

    def refuse_reclassification(
        _manager: TransactionManager, _rows: Sequence[_RowWrite]
    ) -> object:
        raise AssertionError("retarget classified the transaction rows again")

    monkeypatch.setattr(
        TransactionManager,
        "_group_page_stamps",
        refuse_reclassification,
    )
    records, corrected = stack.manager._retarget_commit_batch(
        transaction,
        [object(), _Commit()],
        [(HEAP, 6, image)],
        rows,
        old_csn=10,
        new_csn=12,
        epoch=1,
    )

    assert stack.manager._materialized is None
    assert len(records) == 2
    assert corrected[0][:2] == (HEAP, 6)
    stamped = stack.codec.decode_page(corrected[0][2], verify=True)
    assert stamped.page_lsn == 12
    assert RecordHeader.decode(stamped.read_slot(0)[:RECORD_HEADER_SIZE]).xmin == 12
    logged = decode_page_write(records[0].payload).image  # type: ignore[attr-defined]
    assert logged == corrected[0][2]
    stack.manager.rollback(transaction)
