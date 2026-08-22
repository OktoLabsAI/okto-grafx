"""BR-2: the ledger receives involuntary loss and nothing else.

The semantic boundary of D7, stated by BR-2: a code path that can fail synchronously with a typed
exception DOES fail synchronously and writes nothing to the ledger. Only work lost involuntarily
-- a crash, damaged bytes, a stale epoch -- is recorded, and always classified as reapplicable or
forensic. Every refusal below is a path that could have taken the easy way out and written an
entry instead of raising.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxLedgerError,
    GrafxQuarantineError,
    GrafxRecoveryRefused,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.ledger.entry import LedgerEntryType, LedgerOriginClass
from okto_grafx.domain.recovery.report import POLICY_REFUSE

from .conftest import (
    HEAP_FILE,
    RefusingProbe,
    Stack,
    build_stack,
    commit_pages,
    make_page_image,
)


def _commit(stack: Stack) -> None:
    """Commit one heap page so the log holds an intact record."""
    commit_pages(
        stack,
        [(HEAP_FILE, 3, make_page_image(stack.codec, [b"kept"], page_index=3))],
        txn_id=1,
    )


def _damage(stack: Stack) -> None:
    """Append bytes that are not a record to the newest segment."""
    name = stack.wal.segments()[-1].name
    stack.storage.append_log(name, bytes(96))  # type: ignore[attr-defined]
    stack.wal.open()


def _reopened(stack: Stack) -> Stack:
    """Return a second stack over the same device."""
    return build_stack(stack.storage, clock=stack.clock, metrics=stack.metrics, bootstrap=False)


def test_a_refused_recovery_writes_nothing_to_the_ledger(stack: Stack) -> None:
    _commit(stack)
    _damage(stack)
    reopened = _reopened(stack)
    with pytest.raises(GrafxRecoveryRefused):
        reopened.recovery(policy=POLICY_REFUSE).run()
    assert reopened.ledger.list() == ()
    assert reopened.quarantine.list() == ()


def test_a_record_from_a_newer_build_writes_nothing_to_the_ledger(stack: Stack) -> None:
    import struct

    from okto_grafx.domain.wal.record import WalRecord, WalRecordType

    from .conftest import DESCRIPTOR

    _commit(stack)
    name = stack.wal.segments()[-1].name
    future = bytearray(
        WalRecord(
            record_type=int(WalRecordType.WRITE_PAGE),
            payload=bytes(16),
            descriptor=DESCRIPTOR,
            lsn=stack.wal.last_lsn + 1,
            epoch=1,
            txn_id=1,
        ).encode()
    )
    future[4:6] = struct.pack("<H", 250)
    stack.storage.append_log(name, bytes(future))  # type: ignore[attr-defined]
    reopened = _reopened(stack)
    with pytest.raises(GrafxSchemaVersionMismatch):
        reopened.recovery().run()
    assert reopened.ledger.list() == ()


def test_a_refused_retirement_writes_nothing_to_the_ledger(stack: Stack) -> None:
    device = stack.storage
    device.create("control/reader-a.reader", exclusive=False)  # type: ignore[attr-defined]
    device.append_log("control/reader-a.reader", b"damaged")  # type: ignore[attr-defined]
    probe = RefusingProbe(GrafxCorruptionDetected("Unreadable."))
    manager = stack.recovery(control_probe=probe)
    for name in (HEAP_FILE, "wal/000000000001.wal", "control/missing.reader"):
        with pytest.raises(GrafxRecoveryRefused):
            manager.retire_control_record(name)
    assert stack.ledger.list() == ()
    assert stack.quarantine.list() == ()


def test_a_refused_quarantine_capture_writes_nothing_to_the_ledger(stack: Stack) -> None:
    with pytest.raises(GrafxQuarantineError):
        stack.quarantine.capture(origin="wal/absent.wal", offset=0, length=8, reason="a")
    assert stack.ledger.list() == ()


def test_a_refused_ledger_call_writes_nothing_to_the_ledger(stack: Stack) -> None:
    with pytest.raises(GrafxLedgerError):
        stack.ledger.inspect(42)
    with pytest.raises(GrafxLedgerError):
        stack.ledger.purge(confirm_token="wrong")
    assert stack.ledger.list() == ()


def test_every_entry_a_recovery_writes_carries_one_of_the_two_classifications(
    stack: Stack,
) -> None:
    _commit(stack)
    _damage(stack)
    reopened = _reopened(stack)
    reopened.recovery().run()
    entries = reopened.ledger.list(limit=100)
    assert entries
    for entry in entries:
        assert entry.origin_class in (
            LedgerOriginClass.REAPPLICABLE,
            LedgerOriginClass.FORENSIC,
        )


def test_the_backlog_counts_lost_work_and_not_the_receipts_of_recovery_acts(
    stack: Stack,
) -> None:
    """A retirement and a reprocess receipt are records of deliberate acts, not of a backlog."""
    _commit(stack)
    _damage(stack)
    reopened = _reopened(stack)
    reopened.recovery().run()
    lost = sum(reopened.ledger.depth().values())

    device = stack.storage
    device.create("control/reader-a.reader", exclusive=False)  # type: ignore[attr-defined]
    device.append_log("control/reader-a.reader", b"damaged")  # type: ignore[attr-defined]
    probe = RefusingProbe(GrafxCorruptionDetected("Unreadable."))
    reopened.recovery(control_probe=probe).retire_control_record("control/reader-a.reader")

    assert sum(reopened.ledger.depth().values()) == lost
    kinds = {entry.entry_type for entry in reopened.ledger.list(limit=100)}
    assert LedgerEntryType.RETIREMENT in kinds
