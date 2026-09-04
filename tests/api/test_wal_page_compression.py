"""Explicit, downgrade-safe activation of WAL-v2 compressed page images."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import (
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.model.catalog import WAL_RECORD_V2_CAPABILITY
from okto_grafx.domain.txn.records import decode_page_write, encode_page_write
from okto_grafx.domain.wal import (
    WAL_FORMAT_VERSION,
    WAL_LEGACY_FORMAT_VERSION,
    WAL_V2_FLAG_PAGE_IMAGE_ZLIB1,
    WAL_V2_FLAG_REQUIRED,
    WalRecordType,
)
from okto_grafx.engine.txn_manager import TransactionManager

PAGE_SIZE = 512


def _create_schema(database: object) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")


def _insert(database: object, identity: int, name: str) -> None:
    with database.begin("write") as txn:
        txn.execute(f"CREATE (:Person {{id: {identity}, name: '{name}'}})")


def test_activation_is_a_v1_fence_then_later_commits_may_compress(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        _create_schema(database)

        before_refusal = database._wal.last_lsn
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            database.enable_wal_page_compression()
        assert raised.value.details["remedy"] == "maintenance.ensure_identity_indexes"
        assert database._wal.last_lsn == before_refusal

        database.ensure_identity_indexes()
        before_activation = database._wal.last_lsn
        database.enable_wal_page_compression()
        activation = tuple(database._wal.read_from(before_activation + 1))

        assert activation
        assert all(
            record.format_version == WAL_LEGACY_FORMAT_VERSION for record in activation
        )
        assert database._catalog.catalog.requires_capability(WAL_RECORD_V2_CAPABILITY)

        after_activation = database._wal.last_lsn
        database.enable_wal_page_compression()
        assert database._wal.last_lsn == after_activation

        _insert(database, 1, "compressible")
        committed = tuple(database._wal.read_from(after_activation + 1))
        page_records = tuple(
            record
            for record in committed
            if record.record_type == int(WalRecordType.WRITE_PAGE)
        )
        compressed = tuple(
            record
            for record in page_records
            if record.format_version == WAL_FORMAT_VERSION
        )

        assert compressed
        assert all(
            record.flags == WAL_V2_FLAG_REQUIRED | WAL_V2_FLAG_PAGE_IMAGE_ZLIB1
            for record in compressed
        )
        assert all(
            len(
                decode_page_write(
                    record.payload,
                    format_version=record.format_version,
                    flags=record.flags,
                ).image
            )
            == PAGE_SIZE
            for record in compressed
        )
        assert committed[-1].record_type == int(WalRecordType.COMMIT)
        assert committed[-1].format_version == WAL_LEGACY_FORMAT_VERSION
        database.checkpoint()
        assert database.execute("MATCH (p:Person {id: 1}) RETURN p.name").rows == (
            ("compressible",),
        )

    with connect(root, page_size=PAGE_SIZE) as reopened:
        assert reopened.execute("MATCH (p:Person {id: 1}) RETURN p.name").rows == (
            ("compressible",),
        )
        assert reopened._catalog.catalog.requires_capability(WAL_RECORD_V2_CAPABILITY)
        before = reopened._wal.last_lsn
        _insert(reopened, 2, "first after reopen")
        assert any(
            record.record_type == int(WalRecordType.WRITE_PAGE)
            and record.format_version == WAL_FORMAT_VERSION
            for record in reopened._wal.read_from(before + 1)
        )
        reopened.checkpoint()

    with connect(root, page_size=PAGE_SIZE, read_only=True) as reader:
        assert reader.execute("MATCH (p:Person) RETURN p.name").rows == (
            ("compressible",),
            ("first after reopen",),
        )


def test_a_raw_batch_that_rolls_keeps_legacy_page_images(tmp_path: Path) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE, wal_segment_bytes=PAGE_SIZE) as database:
        _create_schema(database)
        database.ensure_identity_indexes()
        database.enable_wal_page_compression()
        before = database._wal.last_lsn

        _insert(database, 1, "roll")
        committed = tuple(database._wal.read_from(before + 1))
        page_records = tuple(
            record
            for record in committed
            if record.record_type == int(WalRecordType.WRITE_PAGE)
        )

        assert page_records
        assert all(
            record.format_version == WAL_LEGACY_FORMAT_VERSION and record.flags == 0
            for record in page_records
        )
        assert committed[-1].lsn == database._transactions.published_lsn()


def test_a_warm_participant_adopts_the_fence_before_its_next_commit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    first = connect(root, page_size=PAGE_SIZE)
    second = connect(root, page_size=PAGE_SIZE)
    try:
        _create_schema(first)
        first.ensure_identity_indexes()
        first.enable_wal_page_compression()
        before = first._wal.last_lsn

        _insert(second, 2, "warm participant")
        committed = tuple(second._wal.read_from(before + 1))
        page_records = tuple(
            record
            for record in committed
            if record.record_type == int(WalRecordType.WRITE_PAGE)
        )

        assert page_records
        assert any(
            record.format_version == WAL_FORMAT_VERSION for record in page_records
        )
        assert second._catalog.catalog.requires_capability(WAL_RECORD_V2_CAPABILITY)
        assert first.execute("MATCH (p:Person {id: 2}) RETURN p.name").rows == (
            ("warm participant",),
        )
    finally:
        second.close()
        first.close()


def test_wal_batch_quota_measures_the_final_compressed_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with connect(tmp_path / "db", page_size=PAGE_SIZE) as database:
        _create_schema(database)
        database.ensure_identity_indexes()
        database.enable_wal_page_compression()
        original = TransactionManager._validate_wal_batch_budget
        calls: list[tuple[int, int]] = []
        refuse = False

        def validate(
            manager: TransactionManager,
            txn: object,
            records: object,
        ) -> None:
            batch = tuple(records)
            encoded = sum(record.encoded_length() for record in batch)
            raw = encoded
            for record in batch:
                if record.record_type != int(WalRecordType.WRITE_PAGE):
                    continue
                write = decode_page_write(
                    record.payload,
                    format_version=record.format_version,
                    flags=record.flags,
                )
                raw += len(encode_page_write(write.file, write.page_index, write.image))
                raw -= len(record.payload)
            calls.append((encoded, raw))
            manager._max_wal_batch_bytes = encoded - int(refuse)
            original(manager, txn, batch)

        monkeypatch.setattr(TransactionManager, "_validate_wal_batch_budget", validate)

        _insert(database, 1, "quota")
        assert len(calls) == 1
        assert calls[0][0] < calls[0][1]

        before = database._wal.last_lsn
        refuse = True
        with pytest.raises(GrafxTransactionBudgetExceeded) as raised:
            _insert(database, 2, "one byte below")
        assert raised.value.details["limit"] == raised.value.details["observed"] - 1
        assert database._wal.last_lsn == before


def test_cold_recovery_completes_a_compressed_commit_after_the_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "db"
    database = connect(root, page_size=PAGE_SIZE)
    try:
        _create_schema(database)
        database.ensure_identity_indexes()
        database.enable_wal_page_compression()
        database.checkpoint()
        before = database._wal.last_lsn
        transaction = database.begin("write")
        transaction.execute("CREATE (:Person {id: 9, name: 'recover me'})")

        def fail_after_barrier(
            manager: TransactionManager,
            _images: object,
        ) -> None:
            assert manager is database._transactions
            raise RuntimeError("injected compressed post-barrier apply failure")

        with monkeypatch.context() as injected:
            injected.setattr(TransactionManager, "_apply_images", fail_after_barrier)
            with pytest.raises(GrafxTransactionStateError) as raised:
                transaction.commit()

        assert raised.value.details["committed"] is True
        assert database.transactions.recovery_required is True
        durable_gap = tuple(database._wal.read_from(before + 1))
        assert any(
            record.record_type == int(WalRecordType.WRITE_PAGE)
            and record.format_version == WAL_FORMAT_VERSION
            for record in durable_gap
        )
    finally:
        database.close()

    with connect(root, page_size=PAGE_SIZE) as reopened:
        assert reopened.execute("MATCH (p:Person {id: 9}) RETURN p.name").rows == (
            ("recover me",),
        )
        assert reopened.verify("all").findings == ()
