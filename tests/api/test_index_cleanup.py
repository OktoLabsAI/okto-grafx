"""Proof-before-delete index cleanup, recovery retention, and interruption convergence."""

from __future__ import annotations

import pytest
import subprocess
import sys

from okto_grafx import connect
from okto_grafx.errors import (
    GrafxConfigurationError,
    GrafxError,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.index.records import IndexChange, IndexOperation
from okto_grafx.domain.txn.records import encode_page_write
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.domain.page.layout import PageType


ORPHAN = "index/g_fffffffffffffffe.idx"
DISPLACED = "index_orphan/" + "a" * 32 + ".idx"


@pytest.fixture
def database(tmp_path):
    with connect(str(tmp_path / "db"), page_size=512, wal_segment_bytes=512) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id: 1})")
        db.checkpoint()
        yield db


def _orphan(db, name=ORPHAN):
    db._storage.create(name)
    db._storage.allocate(name)
    return name


def test_dry_run_then_delete_and_reopen(database):
    db = database
    names = (_orphan(db), _orphan(db, DISPLACED))
    unknown = _orphan(db, "index/operator_notes.idx")
    before = db._storage.list_files()
    preview = db.maintenance.cleanup_indexes(confirm_quiescent=True)
    assert preview.dry_run and not preview.removed
    assert db._storage.list_files() == before
    assert {item.file for item in preview.files if item.reason == "orphan"} == set(
        names
    )
    assert preview.candidate_bytes == 1024
    report = db.maintenance.cleanup_indexes(confirm_quiescent=True, dry_run=False)
    assert set(report.removed) == set(names)
    assert not report.deferred
    assert db._storage.exists(unknown)
    assert db.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)
    assert not db.maintenance.cleanup_indexes(
        confirm_quiescent=True, dry_run=False
    ).removed
    path = db._path
    db.close()
    with connect(path, page_size=512) as reopened:
        assert reopened.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)
        assert reopened.verify("all").clean


def test_all_catalog_generations_remain_owned_after_rehash(database):
    database.create_index("by_id", "N", ("id",), bucket_count=8)
    database.rehash_index("by_id", bucket_count=16)
    expected = {
        generation.file
        for definition in database._catalog.catalog.index_definitions()
        for generation in definition.generations
    }
    assert expected
    database.checkpoint()
    report = database.maintenance.cleanup_indexes(confirm_quiescent=True, dry_run=False)
    assert expected <= {
        item.file for item in report.files if item.reason == "catalog_or_registered"
    }
    assert all(database._storage.exists(file) for file in expected)


@pytest.mark.parametrize("dry", [True, False])
def test_quiescence_and_open_transaction_refusal(database, dry):
    _orphan(database)
    with pytest.raises(GrafxUnsupportedOperation):
        database.maintenance.cleanup_indexes(dry_run=dry)
    with database.begin("read"):
        with pytest.raises(GrafxTransactionStateError):
            database.maintenance.cleanup_indexes(confirm_quiescent=True, dry_run=dry)
    assert database._storage.exists(ORPHAN)


def test_readonly_handle_refuses(database):
    path = database._path
    database.close()
    with connect(path, read_only=True, page_size=512) as reader:
        with pytest.raises(GrafxUnsupportedOperation):
            reader.maintenance.cleanup_indexes(confirm_quiescent=True)


@pytest.mark.parametrize(
    "options",
    [{"dry_run": 1}, {"max_files": True}, {"max_files": 0}, {"max_wal_records": -1}],
)
def test_argument_validation(database, options):
    with pytest.raises(GrafxConfigurationError):
        database.maintenance.cleanup_indexes(confirm_quiescent=True, **options)


def test_file_budget_refuses_before_first_delete(database):
    _orphan(database)
    with pytest.raises(GrafxUnsupportedOperation):
        database.maintenance.cleanup_indexes(
            confirm_quiescent=True, dry_run=False, max_files=1
        )
    assert database._storage.exists(ORPHAN)


def test_wal_page_reference_is_retained_even_before_checkpoint(database, monkeypatch):
    _orphan(database)
    record = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=encode_page_write(ORPHAN, 0, bytes(512)),
    )
    original = type(database._wal).read_from
    monkeypatch.setattr(
        type(database._wal),
        "read_from",
        lambda self, lsn: iter((record,)) if lsn == 1 else original(self, lsn),
    )
    report = database.maintenance.cleanup_indexes(confirm_quiescent=True, dry_run=False)
    assert (
        next(item for item in report.files if item.file == ORPHAN).reason
        == "retained_wal"
    )
    assert database._storage.exists(ORPHAN)


def test_unmapped_logical_wal_keeps_generation(database, monkeypatch):
    _orphan(database)
    change = IndexChange("unknown_legacy", IndexOperation.INSERT, key=b"a")
    record = WalRecord(
        record_type=int(WalRecordType.INDEX_WRITE), payload=change.encode()
    )
    original = type(database._wal).read_from
    monkeypatch.setattr(
        type(database._wal),
        "read_from",
        lambda self, lsn: iter((record,)) if lsn == 1 else original(self, lsn),
    )
    report = database.maintenance.cleanup_indexes(confirm_quiescent=True, dry_run=False)
    assert (
        next(item for item in report.files if item.file == ORPHAN).reason
        == "unresolved_logical_wal"
    )


@pytest.mark.parametrize("mode", ["malformed", "future", "budget", "scan_error"])
def test_full_wal_proof_precedes_every_delete(database, monkeypatch, mode):
    _orphan(database)
    _orphan(database, DISPLACED)
    original = type(database._wal).read_from

    def records(self, lsn):
        if lsn != 1:
            yield from original(self, lsn)
            return
        yield WalRecord(record_type=int(WalRecordType.BEGIN))
        if mode == "scan_error":
            raise GrafxUnsupportedOperation("synthetic damaged scan")
        yield WalRecord(
            record_type=int(WalRecordType.WRITE_PAGE)
            if mode == "malformed"
            else 65000
            if mode == "future"
            else int(WalRecordType.BEGIN),
            payload=b"bad",
        )

    monkeypatch.setattr(type(database._wal), "read_from", records)
    with pytest.raises(GrafxError):
        database.maintenance.cleanup_indexes(
            confirm_quiescent=True,
            dry_run=False,
            max_wal_records=1 if mode == "budget" else 100,
        )
    assert database._storage.exists(ORPHAN) and database._storage.exists(DISPLACED)


def test_interrupted_deletion_is_safe_to_repeat(database, monkeypatch):
    _orphan(database)
    _orphan(database, DISPLACED)
    cls = type(database._storage)
    original = cls.recycle
    calls = []

    def recycle(self, file):
        calls.append(file)
        if len(calls) == 2:
            raise KeyboardInterrupt()
        return original(self, file)

    with monkeypatch.context() as patch:
        patch.setattr(cls, "recycle", recycle)
        with pytest.raises(KeyboardInterrupt):
            database.maintenance.cleanup_indexes(confirm_quiescent=True, dry_run=False)
    report = database.maintenance.cleanup_indexes(confirm_quiescent=True, dry_run=False)
    assert len(report.removed) == 1
    assert database.execute("MATCH (n:N) RETURN count(n)").rows == ((1,),)


def test_deferred_delete_is_not_reported_as_reclaimed(database, monkeypatch):
    _orphan(database)
    monkeypatch.setattr(type(database._storage), "recycle", lambda self, file: False)
    report = database.maintenance.cleanup_indexes(confirm_quiescent=True, dry_run=False)
    assert report.deferred == (ORPHAN,) and not report.removed


def test_retained_historical_catalog_keeps_detached_generations(database, monkeypatch):
    _orphan(database)
    _orphan(database, DISPLACED)
    record = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=encode_page_write("catalog.dat", 0, bytes(512)),
    )
    original = type(database._wal).read_from
    monkeypatch.setattr(
        type(database._wal),
        "read_from",
        lambda self, lsn: iter((record,)) if lsn == 1 else original(self, lsn),
    )
    report = database.maintenance.cleanup_indexes(confirm_quiescent=True, dry_run=False)
    assert (
        next(item for item in report.files if item.file == ORPHAN).reason
        == "retained_catalog_wal"
    )
    assert report.removed == (DISPLACED,)


def test_dirty_orphan_frames_cannot_recreate_deleted_file(database):
    database._storage.create(ORPHAN)
    page = database._pool.allocate(ORPHAN, PageType.FREE)
    database._pool.unpin(ORPHAN, page.page_index, dirty=True)
    assert database._pool.has_dirty_pages(ORPHAN)
    database.maintenance.cleanup_indexes(confirm_quiescent=True, dry_run=False)
    database.checkpoint()
    assert not database._storage.exists(ORPHAN)


def test_pinned_orphan_refuses_before_any_delete(database):
    database._storage.create(ORPHAN)
    page = database._pool.allocate(ORPHAN, PageType.FREE)
    _orphan(database, DISPLACED)
    try:
        with pytest.raises(GrafxUnsupportedOperation):
            database.maintenance.cleanup_indexes(confirm_quiescent=True, dry_run=False)
        assert database._storage.exists(DISPLACED)
    finally:
        database._pool.unpin(ORPHAN, page.page_index, page=page)


def test_process_death_after_one_delete_preserves_live_store(database):
    _orphan(database)
    _orphan(database, DISPLACED)
    path = database._path
    database.close()
    program = """
import os, sys
from okto_grafx import connect
from okto_grafx.adapters.storage_local import LocalStorageDevice
original = LocalStorageDevice.recycle
def recycle(self, file):
    result = original(self, file)
    if file.startswith('index/'):
        os._exit(73)
    return result
with connect(sys.argv[1], page_size=512, wal_segment_bytes=512) as db:
    LocalStorageDevice.recycle = recycle
    db.maintenance.cleanup_indexes(confirm_quiescent=True, dry_run=False)
"""
    child = subprocess.run(
        [sys.executable, "-c", program, path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 73, child.stderr
    with connect(path, page_size=512, wal_segment_bytes=512) as db:
        assert not db._storage.exists(ORPHAN)
        assert db._storage.exists(DISPLACED)
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)
        assert db.verify("all").clean
        assert db.maintenance.cleanup_indexes(
            confirm_quiescent=True, dry_run=False
        ).removed == (DISPLACED,)
