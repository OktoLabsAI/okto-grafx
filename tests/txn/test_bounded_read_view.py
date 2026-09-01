"""CE-3 classifies only complete, bounded committed WAL deltas."""

from __future__ import annotations

from types import SimpleNamespace

from okto_grafx.domain.index.records import (
    IndexChange,
    IndexOperation,
    wal_record_for,
)
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.txn.commit_state import CommitState
from okto_grafx.domain.txn.records import (
    WalRecord,
    WalRecordType,
    encode_page_write,
)
from okto_grafx.engine.txn_manager import _ReadViewToken
from txn_support import build_stack


class _BoundedWal:
    def __init__(self, records: tuple[WalRecord, ...]) -> None:
        self.records = records
        self.calls: list[tuple[int, int, int, int]] = []

    def read_bounded(
        self,
        first_lsn: int,
        through_lsn: int,
        *,
        max_records: int,
        max_bytes: int,
    ) -> tuple[WalRecord, ...]:
        self.calls.append((first_lsn, through_lsn, max_records, max_bytes))
        return self.records


class _IndexRegistry:
    def __init__(self, file: str, *other_files: str) -> None:
        self.file = file
        self.files = (file, *other_files)
        self.names: list[str] = []

    def index(self, name: str) -> object:
        self.names.append(name)
        return SimpleNamespace(file=self.file)

    def indexes(self) -> tuple[object, ...]:
        return tuple(SimpleNamespace(file=file) for file in self.files)


def test_committed_page_and_index_effects_become_exact_physical_targets() -> None:
    stack = build_stack()
    page = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=encode_page_write("heap.dat", 17, b"image"),
        lsn=1,
        epoch=7,
        txn_id=9,
    )
    logical = wal_record_for(
        IndexChange(
            index="by_name",
            operation=IndexOperation.INSERT,
            key=b"ada",
            ref=RecordRef(17, 0),
        ),
        epoch=7,
        txn_id=9,
    )
    index = WalRecord(
        record_type=logical.record_type,
        payload=logical.payload,
        lsn=2,
        epoch=7,
        txn_id=9,
    )
    commit = WalRecord(
        record_type=int(WalRecordType.COMMIT),
        lsn=3,
        epoch=7,
        txn_id=9,
    )
    wal = _BoundedWal((page, index, commit))
    registry = _IndexRegistry("indexes/by_name.idx")
    stack.manager._wal = wal
    stack.manager._index_manager = registry

    changes = stack.manager._read_view_changes(
        _ReadViewToken(last_committed_lsn=0, checkpoint_lsn=0),
        _ReadViewToken(last_committed_lsn=3, checkpoint_lsn=0),
    )

    assert changes is not None
    assert changes.pages == frozenset({("heap.dat", 17)})
    assert changes.files == frozenset({"indexes/by_name.idx"})
    assert registry.names == ["by_name"]
    assert wal.calls == [(1, 3, 512, 2 * 1024 * 1024)]


def test_heap_effect_targets_sparse_index_headers_without_an_index_record() -> None:
    stack = build_stack()
    wal = _BoundedWal(
        (
            WalRecord(
                record_type=int(WalRecordType.WRITE_PAGE),
                payload=encode_page_write("heap.dat", 17, b"image"),
                lsn=1,
                epoch=7,
                txn_id=9,
            ),
            WalRecord(
                record_type=int(WalRecordType.COMMIT),
                lsn=2,
                epoch=7,
                txn_id=9,
            ),
        )
    )
    stack.manager._wal = wal
    stack.manager._index_manager = _IndexRegistry(
        "indexes/by_name.idx", "index/vector_person_embedding.idx"
    )

    changes = stack.manager._read_view_changes(
        _ReadViewToken(last_committed_lsn=0, checkpoint_lsn=0),
        _ReadViewToken(last_committed_lsn=2, checkpoint_lsn=0),
    )

    assert changes is not None
    assert changes.pages == frozenset(
        {
            ("heap.dat", 17),
            ("indexes/by_name.idx", 0),
            ("index/vector_person_embedding.idx", 0),
        }
    )
    assert changes.files == frozenset()


def test_unknown_record_checkpoint_movement_and_unregistered_index_decline_ce3() -> (
    None
):
    stack = build_stack()
    unknown = _BoundedWal(
        (
            WalRecord(record_type=int(WalRecordType.BEGIN), lsn=1, epoch=1, txn_id=1),
            WalRecord(record_type=int(WalRecordType.COMMIT), lsn=2, epoch=1, txn_id=1),
        )
    )
    stack.manager._wal = unknown
    assert (
        stack.manager._read_view_changes(
            _ReadViewToken(last_committed_lsn=0, checkpoint_lsn=0),
            _ReadViewToken(last_committed_lsn=2, checkpoint_lsn=0),
        )
        is None
    )

    unknown.calls.clear()
    assert (
        stack.manager._read_view_changes(
            _ReadViewToken(last_committed_lsn=0, checkpoint_lsn=0),
            _ReadViewToken(last_committed_lsn=2, checkpoint_lsn=1),
        )
        is None
    )
    assert unknown.calls == []

    logical = wal_record_for(
        IndexChange(
            index="missing",
            operation=IndexOperation.INSERT,
            key=b"key",
            ref=RecordRef(1, 0),
        ),
        epoch=2,
        txn_id=2,
    )
    stack.manager._wal = _BoundedWal(
        (
            WalRecord(
                record_type=logical.record_type,
                payload=logical.payload,
                lsn=1,
                epoch=2,
                txn_id=2,
            ),
            WalRecord(record_type=int(WalRecordType.COMMIT), lsn=2, epoch=2, txn_id=2),
        )
    )
    stack.manager._index_manager = None
    assert (
        stack.manager._read_view_changes(
            _ReadViewToken(last_committed_lsn=0, checkpoint_lsn=0),
            _ReadViewToken(last_committed_lsn=2, checkpoint_lsn=0),
        )
        is None
    )


def test_malformed_bounded_wal_collaborator_declines_ce3() -> None:
    stack = build_stack()

    class _MalformedWal:
        def read_bounded(self, *args: object, **kwargs: object) -> object:
            return (object(),)

    stack.manager._wal = _MalformedWal()

    assert (
        stack.manager._read_view_changes(
            _ReadViewToken(last_committed_lsn=0, checkpoint_lsn=0),
            _ReadViewToken(last_committed_lsn=1, checkpoint_lsn=0),
        )
        is None
    )

    stack.manager._wal = _BoundedWal(
        (
            WalRecord(
                record_type=int(WalRecordType.WRITE_PAGE),
                payload=encode_page_write("bad\x00file", 0, b"image"),
                lsn=1,
                epoch=1,
                txn_id=1,
            ),
            WalRecord(record_type=int(WalRecordType.COMMIT), lsn=2, epoch=1, txn_id=1),
        )
    )
    assert (
        stack.manager._read_view_changes(
            _ReadViewToken(last_committed_lsn=0, checkpoint_lsn=0),
            _ReadViewToken(last_committed_lsn=2, checkpoint_lsn=0),
        )
        is None
    )

    class _ShapeIncompatibleWal:
        def read_bounded(self, _first_lsn: int) -> tuple[WalRecord, ...]:
            return ()

    stack.manager._wal = _ShapeIncompatibleWal()
    assert (
        stack.manager._read_view_changes(
            _ReadViewToken(last_committed_lsn=0, checkpoint_lsn=0),
            _ReadViewToken(last_committed_lsn=1, checkpoint_lsn=0),
        )
        is None
    )


def test_own_publication_never_scans_the_wal_delta() -> None:
    stack = build_stack()
    initial = CommitState(last_committed_lsn=4, last_csn=4, checkpoint_lsn=0)
    stack.manager._establish_read_view(initial, own=False)

    class _ForbiddenWal:
        def read_bounded(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("an own read view scanned WAL")

    stack.manager._wal = _ForbiddenWal()
    stack.manager._establish_read_view(
        CommitState(last_committed_lsn=5, last_csn=5, checkpoint_lsn=0),
        own=True,
    )

    assert stack.pool.read_view_token() == _ReadViewToken(
        last_committed_lsn=5,
        checkpoint_lsn=0,
    )
