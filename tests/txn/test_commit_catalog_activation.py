"""Internal activation fence and durable horizon; journal writes stay disabled."""

from __future__ import annotations

from pathlib import Path
from struct import pack_into

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import (
    GrafxConfigurationError, GrafxCorruptionDetected, GrafxSchemaVersionMismatch,
    GrafxTransactionStateError, GrafxUnsupportedOperation,
    GrafxTransactionBudgetExceeded,
)
from okto_grafx.domain.ids import PROVISIONAL_CSN
from okto_grafx.domain.model.catalog import Catalog, COMMIT_CATALOG_V1_CAPABILITY
from okto_grafx.domain.page.checksum import crc32c
from okto_grafx.domain.recovery.decision import CommittedReplay
from okto_grafx.domain.wal.record import WalRecordType
from okto_grafx.engine.database import Database
from okto_grafx.engine.commit_redo import CommitRedo
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.wal_manager import WalManager


def test_horizon_codec_clone_and_one_way_admission() -> None:
    catalog = Catalog().upgrade_index_catalog()
    before = catalog.serialize()
    activated = catalog.copy().enable_commit_catalog(90)
    assert catalog.serialize() == before
    assert activated.requires_capability(COMMIT_CATALOG_V1_CAPABILITY)
    assert activated.enable_commit_catalog(90).serialize() == activated.serialize()
    assert Catalog.deserialize(activated.serialize()).commit_catalog_activation == 90
    assert activated.copy().commit_catalog_activation == 90
    with pytest.raises(GrafxConfigurationError):
        activated.enable_commit_catalog(91)


@pytest.mark.parametrize("sequence", [0, -1, True, 1.0, PROVISIONAL_CSN])
def test_invalid_horizon_admission_does_not_mutate_catalog(sequence: object) -> None:
    catalog = Catalog().upgrade_index_catalog()
    before = catalog.serialize()
    with pytest.raises(GrafxConfigurationError):
        catalog.enable_commit_catalog(sequence)  # type: ignore[arg-type]
    assert catalog.serialize() == before


@pytest.mark.parametrize("sequence", [0, PROVISIONAL_CSN])
def test_crc_valid_invalid_persisted_horizon_is_corruption(sequence: int) -> None:
    raw = bytearray(Catalog().upgrade_index_catalog().enable_commit_catalog(90).serialize())
    pack_into("<Q", raw, 44, sequence)
    pack_into("<I", raw, len(raw) - 4, crc32c(bytes(raw[:-4])))
    with pytest.raises(GrafxCorruptionDetected) as failure:
        Catalog.deserialize(bytes(raw))
    assert failure.value.details["field"] == "commit_catalog_activation"


def activate(database: Database) -> int:
    with database.begin("write") as txn:
        assert database._transactions.prepare_commit_catalog_activation(txn._context)
    result = database._catalog.catalog.commit_catalog_activation
    assert result is not None
    return result


def test_activation_horizon_is_the_real_wal_commit_and_survives_reopen(tmp_path: Path) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=512) as database:
        database.ensure_identity_indexes()
        before = database._wal.last_lsn
        horizon = activate(database)
        records = tuple(database._wal.read_from(before + 1))
        assert records[-1].record_type == int(WalRecordType.COMMIT)
        assert records[-1].lsn == horizon
        assert all(record.format_version == 1 for record in records)
        assert not (root / "commits.dir").exists()
        assert not (root / "commits.dat").exists()
        with database.begin("write") as txn:
            assert not database._transactions.prepare_commit_catalog_activation(txn._context)
        assert database._wal.last_lsn == horizon
        assert database._transactions._commit_catalog_activation_plans == {}
    with connect(root, page_size=512) as database:
        assert database._catalog.catalog.commit_catalog_activation == horizon
        before = database._wal.last_lsn
        with pytest.raises(GrafxUnsupportedOperation) as failure:
            with database.begin("write") as txn:
                txn.execute("CREATE NODE TABLE Forbidden(id INT64, PRIMARY KEY(id))")
        assert failure.value.details["field"] == "commit_catalog_publication"
        assert database._wal.last_lsn == before


def test_rollback_discards_activation_without_file_or_wal_effect(tmp_path: Path) -> None:
    with connect(tmp_path / "db", page_size=512) as database:
        database.ensure_identity_indexes()
        before = database._wal.last_lsn
        txn = database.begin("write")
        assert database._transactions.prepare_commit_catalog_activation(txn._context)
        txn.rollback()
        assert database._catalog.catalog.commit_catalog_activation is None
        assert database._wal.last_lsn == before
        assert database._transactions._commit_catalog_activation_plans == {}


def test_terminal_context_abort_discards_activation_before_manager_clear(tmp_path: Path) -> None:
    with connect(tmp_path / "db", page_size=512) as database:
        database.ensure_identity_indexes()
        before = database._wal.last_lsn
        txn = database.begin("write")
        manager = database._transactions
        manager.prepare_commit_catalog_activation(txn._context)
        with manager._participant_section():
            assert manager._abort_for_close_in_section(txn._context) is None
        assert not txn.active
        assert manager._commit_catalog_activation_plans == {}
        assert database._wal.last_lsn == before
        assert database._catalog.catalog.commit_catalog_activation is None


@pytest.mark.parametrize("compressed", [False, True])
def test_segment_roll_retargets_activation_horizon_and_keeps_v1_fence(tmp_path: Path, compressed: bool) -> None:
    with connect(tmp_path / "db", page_size=512, wal_segment_bytes=512) as database:
        database.ensure_identity_indexes()
        if compressed:
            database.enable_wal_page_compression()
        before = database._wal.last_lsn
        horizon = activate(database)
        records = tuple(database._wal.read_from(before + 1))
        assert any(r.record_type == int(WalRecordType.SEGMENT_HEADER) for r in records)
        assert records[-1].lsn == horizon
        assert all(r.format_version == 1 and r.flags == 0 for r in records)


def test_non_rolling_compression_enabled_activation_is_still_legacy_wal(tmp_path: Path) -> None:
    with connect(tmp_path / "db", page_size=512) as database:
        database.ensure_identity_indexes()
        database.enable_wal_page_compression()
        before = database._wal.last_lsn
        activate(database)
        records = tuple(database._wal.read_from(before + 1))
        assert not any(r.record_type == int(WalRecordType.SEGMENT_HEADER) for r in records)
        assert all(r.format_version == 1 and r.flags == 0 for r in records)


def test_same_handle_and_already_open_foreign_writer_cannot_create_untracked_commit(tmp_path: Path) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=512) as first:
        with first.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Item(id INT64, PRIMARY KEY(id))")
        first.ensure_identity_indexes()
        with connect(root, page_size=512) as second:
            pending = second.begin("write")
            pending.execute("CREATE (:Item {id: 1})")
            activate(first)
            before = first._wal.last_lsn
            with pytest.raises(GrafxUnsupportedOperation) as failure:
                pending.commit()
            assert failure.value.details["field"] == "commit_catalog_publication"
            pending.rollback()
            with pytest.raises(GrafxUnsupportedOperation):
                with first.begin("write") as txn:
                    txn.execute("CREATE (:Item {id: 2})")
            assert first._wal.last_lsn == before
            with first.begin("read") as reader:
                assert reader.execute("MATCH (i:Item) RETURN i.id").rows == ()


def test_activation_cannot_silently_discard_work_added_after_preparation(tmp_path: Path) -> None:
    with connect(tmp_path / "db", page_size=512) as database:
        database.ensure_identity_indexes()
        before = database._wal.last_lsn
        txn = database.begin("write")
        database._transactions.prepare_commit_catalog_activation(txn._context)
        txn.execute("CREATE NODE TABLE Other(id INT64, PRIMARY KEY(id))")
        with pytest.raises(GrafxConfigurationError):
            txn.commit()
        txn.rollback()
        assert database._wal.last_lsn == before
        assert database._catalog.catalog.commit_catalog_activation is None


def test_old_required_capability_mask_refuses_before_new_horizon_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    import okto_grafx.domain.model.catalog as module

    raw = bytearray(Catalog().upgrade_index_catalog().enable_commit_catalog(90).serialize())
    # Bad new-format body plus valid checksum: the legacy required-capability
    # mask must refuse unknown semantics BEFORE interpreting the new body.
    pack_into("<Q", raw, 44, 0)
    pack_into("<I", raw, len(raw) - 4, crc32c(bytes(raw[:-4])))
    monkeypatch.setattr(module, "_KNOWN_CAPABILITY_BITS", 15)
    with pytest.raises(GrafxSchemaVersionMismatch):
        Catalog.deserialize(bytes(raw))


def test_post_barrier_apply_failure_preserves_one_activation_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "db"
    database = connect(root, page_size=512)
    database.ensure_identity_indexes()
    before = database._wal.last_lsn
    txn = database.begin("write")
    database._transactions.prepare_commit_catalog_activation(txn._context)

    def fail_apply(manager: TransactionManager, images: object) -> None:
        assert manager is database._transactions
        raise RuntimeError("injected post-barrier apply cut")

    with monkeypatch.context() as patch:
        patch.setattr(TransactionManager, "_apply_images", fail_apply)
        with pytest.raises(GrafxTransactionStateError) as failure:
            txn.commit()
        assert failure.value.details["committed"] is True
        assert failure.value.details["durable"] is True
        committed = txn._context.commit_csn
    database.close()
    with connect(root, page_size=512) as recovered:
        assert recovered._catalog.catalog.commit_catalog_activation == committed
        commits = [r for r in recovered._wal.read_from(before + 1) if r.record_type == int(WalRecordType.COMMIT)]
        assert [r.lsn for r in commits] == [committed]


def test_pre_append_failure_can_rebind_the_same_uncommitted_activation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with connect(tmp_path / "db", page_size=512) as database:
        database.ensure_identity_indexes()
        before = database._wal.last_lsn
        txn = database.begin("write")
        database._transactions.prepare_commit_catalog_activation(txn._context)

        def fail_plan(manager: WalManager, records: object) -> int:
            raise GrafxTransactionBudgetExceeded("Injected pre-append failure.", field="test")

        with monkeypatch.context() as patch:
            patch.setattr(WalManager, "planned_terminal_lsn", fail_plan)
            with pytest.raises(GrafxTransactionBudgetExceeded):
                txn.commit()
        assert txn.active
        assert database._wal.last_lsn == before
        txn.commit()
        assert database._catalog.catalog.commit_catalog_activation == txn._context.commit_csn
        assert database._transactions._commit_catalog_activation_plans == {}


def test_public_checkpoint_anchors_activation_and_reopens_at_new_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=512, wal_segment_bytes=512) as database:
        database.ensure_identity_indexes()
        database.checkpoint()
        floor = database._transactions._commit_state_store.read().checkpoint_lsn
        assert floor > 0
        horizon = activate(database)
        assert horizon > floor
        observed: list[tuple[object, int]] = []
        original = CommitRedo.preflight

        def observe(redo: CommitRedo, replay: CommittedReplay, **kwargs: object) -> object:
            observed.append((kwargs.get("_checkpoint_lsn"), len(replay.effects)))
            return original(redo, replay, **kwargs)  # type: ignore[arg-type]

        with monkeypatch.context() as patch:
            patch.setattr(CommitRedo, "preflight", observe)
            database.checkpoint()
        assert observed and observed[0][0] == floor
        assert any(count for _value, count in observed)
        assert all(value == floor for value, count in observed if count)
        assert all(value in {floor, horizon} for value, _count in observed)
        assert database._transactions._commit_state_store.read().checkpoint_lsn == horizon
    with connect(root, page_size=512, wal_segment_bytes=512) as database:
        assert database._catalog.catalog.commit_catalog_activation == horizon
        assert database._transactions._commit_state_store.read().checkpoint_lsn == horizon
