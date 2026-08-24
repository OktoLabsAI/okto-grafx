"""Pure committed-replay decisions shared by startup and explicit recovery."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxRecoveryRefused
from okto_grafx.domain.ids import NO_LSN
from okto_grafx.domain.recovery.decision import committed_replay, redo_order
from okto_grafx.domain.wal.record import WalRecord, WalRecordType


def _record(
    record_type: WalRecordType,
    lsn: int,
    *,
    epoch: int = 1,
    txn_id: int = 1,
) -> WalRecord:
    """Build one already-numbered record for the pure replay decision."""
    return WalRecord(
        record_type=int(record_type),
        payload=record_type.name.encode("ascii"),
        lsn=lsn,
        epoch=epoch,
        txn_id=txn_id,
    )


def test_committed_replay_keeps_all_required_effects_in_global_wal_order() -> None:
    records = (
        _record(WalRecordType.WRITE_PAGE, 1, txn_id=10),
        _record(WalRecordType.INDEX_WRITE, 2, txn_id=20),
        _record(WalRecordType.INDEX_RECONCILE, 3, txn_id=10),
        # Transaction 20 commits first, but its effect must not jump ahead of LSN 1.
        _record(WalRecordType.COMMIT, 4, txn_id=20),
        _record(WalRecordType.COMMIT, 5, txn_id=10),
    )

    replay = committed_replay(records)

    assert [record.lsn for record in replay.effects] == [1, 2, 3]
    assert replay.incomplete_effects == ()
    assert [record.record_type for record in replay.effects] == [
        int(WalRecordType.WRITE_PAGE),
        int(WalRecordType.INDEX_WRITE),
        int(WalRecordType.INDEX_RECONCILE),
    ]
    assert replay.last_committed_lsn == 5


def test_committed_replay_keys_transactions_by_epoch_and_transaction_id() -> None:
    records = (
        _record(WalRecordType.WRITE_PAGE, 1, epoch=7, txn_id=42),
        _record(WalRecordType.INDEX_WRITE, 2, epoch=8, txn_id=42),
        _record(WalRecordType.COMMIT, 3, epoch=7, txn_id=42),
    )

    replay = committed_replay(records)

    assert [record.lsn for record in replay.effects] == [1]
    assert [record.lsn for record in replay.incomplete_effects] == [2]
    assert replay.last_committed_lsn == 3


def test_committed_replay_drops_aborted_effects_but_reports_incomplete_tails() -> None:
    records = (
        _record(WalRecordType.WRITE_PAGE, 1, txn_id=10),
        _record(WalRecordType.ABORT, 2, txn_id=10),
        _record(WalRecordType.INDEX_RECONCILE, 3, txn_id=20),
    )

    replay = committed_replay(records)

    assert replay.effects == ()
    assert [record.lsn for record in replay.incomplete_effects] == [3]
    assert replay.last_committed_lsn == NO_LSN


def test_empty_commit_advances_watermark_and_legacy_redo_remains_page_only() -> None:
    records = (
        _record(WalRecordType.WRITE_PAGE, 1, txn_id=10),
        _record(WalRecordType.INDEX_WRITE, 2, txn_id=10),
        _record(WalRecordType.COMMIT, 3, txn_id=10),
        _record(WalRecordType.COMMIT, 4, txn_id=20),
    )

    replay = committed_replay(records)

    assert [record.lsn for record in replay.effects] == [1, 2]
    assert replay.incomplete_effects == ()
    assert replay.last_committed_lsn == 4
    assert [record.lsn for record in redo_order(records)] == [1]


def test_commit_after_abort_is_refused_as_an_ambiguous_legacy_outcome() -> None:
    records = (
        _record(WalRecordType.WRITE_PAGE, 1, txn_id=10),
        _record(WalRecordType.ABORT, 2, txn_id=10),
        _record(WalRecordType.COMMIT, 3, txn_id=10),
    )

    with pytest.raises(GrafxRecoveryRefused) as refused:
        committed_replay(records)

    assert refused.value.details["field"] == "wal_transaction"
    assert refused.value.details["terminal_type"] == "ABORT"
    assert refused.value.details["offending_type"] == "COMMIT"


def test_a_duplicate_terminal_outcome_is_refused() -> None:
    records = (
        _record(WalRecordType.COMMIT, 1, txn_id=10),
        _record(WalRecordType.COMMIT, 2, txn_id=10),
    )

    with pytest.raises(GrafxRecoveryRefused) as refused:
        committed_replay(records)

    assert refused.value.details["terminal_lsn"] == 1
    assert refused.value.details["offending_lsn"] == 2


def test_an_effect_after_a_terminal_outcome_is_refused() -> None:
    records = (
        _record(WalRecordType.COMMIT, 1, txn_id=10),
        _record(WalRecordType.WRITE_PAGE, 2, txn_id=10),
    )

    with pytest.raises(GrafxRecoveryRefused) as refused:
        committed_replay(records)

    assert refused.value.details["terminal_type"] == "COMMIT"
    assert refused.value.details["offending_type"] == "WRITE_PAGE"
