"""The ledger store: appending, listing, exporting, reapplying and purging (FR-9, AC-12, TR-5)."""

from __future__ import annotations

import itertools
import struct

import pytest
from .conftest import FrozenClock, RecordingMetricsSink, Stack

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxLedgerError,
    GrafxPortNotConfigured,
    GrafxStorageError,
)
from okto_grafx.domain.ledger.entry import (
    LedgerEntry,
    LedgerEntryType,
    LedgerOriginClass,
    LedgerReason,
)
from okto_grafx.domain.ledger.payload import LedgerPayload, encode_payload
from okto_grafx.domain.recovery.retry import is_retryable
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.engine.ledger_store import (
    APPEND_ATTEMPTS,
    LEDGER_DEPTH,
    LEDGER_FILE,
    LEDGER_OLDEST_ENTRY_AGE_SECONDS,
    PURGE_CONFIRM_TOKEN,
    LedgerStore,
)


def _payload(
    origin: str = "wal/000000000001.wal", body: bytes = b"bytes", offset: int = 16
) -> LedgerPayload:
    """Return one envelope naming a plausible origin."""
    return LedgerPayload(origin=origin, offset=offset, length=len(body), body=body)


_DISTINCT_OFFSETS = itertools.count(16, 128)


def _discard(store: LedgerStore, **overrides: object) -> int:
    """Append one forensic discard describing a range no earlier call described.

    Each call gets its own offset on purpose. ``record_discard`` is idempotent on the damage
    identity -- that is the whole of B1 -- so a helper that always described the same range would
    make every test after the first one silently assert on one entry. Tests about the dedupe pass
    the offset explicitly.
    """
    settings: dict[str, object] = {
        "origin_class": LedgerOriginClass.FORENSIC,
        "reason": LedgerReason.CHECKSUM_FAILURE,
        "payload": _payload(offset=next(_DISTINCT_OFFSETS)),
        "lsn_start": 5,
        "lsn_end": 5,
    }
    settings.update(overrides)
    return store.record_discard(**settings)  # type: ignore[arg-type]


def _record_entry(store: LedgerStore, lsn: int = 11) -> int:
    """Append one reapplicable discard carrying a real log record."""
    record = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=bytes(16),
        lsn=lsn,
        epoch=2,
        txn_id=4,
    )
    return store.record_discard(
        origin_class=LedgerOriginClass.REAPPLICABLE,
        reason=LedgerReason.TRUNCATED_TAIL,
        payload=LedgerPayload(origin="wal/000000000001.wal", body=record.encode()),
        lsn_start=lsn,
        lsn_end=lsn,
        epoch=2,
    )


# --- construction and ports --------------------------------------------------------------------


def test_a_port_missing_a_door_the_ledger_opens_is_refused_at_construction(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    class Half:
        """A device that answers only some of the storage port."""

        def exists(self, file: str) -> bool:
            """Answer the one door this stand-in implements."""
            return False

    with pytest.raises(GrafxPortNotConfigured) as caught:
        LedgerStore(Half(), clock, metrics)  # type: ignore[arg-type]
    assert caught.value.details["slot"] == "storage"


def test_a_ledger_file_that_could_escape_the_database_is_refused(
    memory_device: object, clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    for name in ("", "../ledger.log", "ledger\\ledger.log"):
        with pytest.raises(GrafxConfigurationError):
            LedgerStore(memory_device, clock, metrics, file=name)  # type: ignore[arg-type]


# --- appending and reading ---------------------------------------------------------------------


def test_an_entry_survives_a_reopen_of_the_store(stack: Stack) -> None:
    entry_id = _discard(stack.ledger)
    reopened = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    stored = reopened.inspect(entry_id)
    assert stored.reason is LedgerReason.CHECKSUM_FAILURE
    assert stored.origin_class is LedgerOriginClass.FORENSIC


def test_identifiers_are_handed_out_in_order_and_never_reused(stack: Stack) -> None:
    first, second, third = _discard(stack.ledger), _discard(stack.ledger), _discard(stack.ledger)
    assert (first, second, third) == (1, 2, 3)
    reopened = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    assert _discard(reopened) == 4


def test_an_identifier_already_stored_is_refused_rather_than_overwritten(stack: Stack) -> None:
    _discard(stack.ledger)
    with pytest.raises(GrafxLedgerError) as caught:
        stack.ledger.append(
            LedgerEntry(
                entry_id=1,
                origin_class=LedgerOriginClass.FORENSIC,
                reason=LedgerReason.TRUNCATED_TAIL,
            )
        )
    assert caught.value.details["field"] == "entry_id"


def test_an_entry_is_stamped_with_the_wall_clock_and_never_with_the_monotonic_one(
    stack: Stack,
) -> None:
    stack.clock.advance(500.0)
    entry = stack.ledger.inspect(_discard(stack.ledger))
    assert entry.captured_at_wall == stack.clock.wall()


def test_listing_filters_by_class_and_by_reason_and_pages(stack: Stack) -> None:
    _discard(stack.ledger)
    _discard(stack.ledger, reason=LedgerReason.TRUNCATED_TAIL)
    _record_entry(stack.ledger)
    assert len(stack.ledger.list()) == 3
    assert len(stack.ledger.list(origin_class="forensic")) == 2
    assert len(stack.ledger.list(origin_class=LedgerOriginClass.REAPPLICABLE)) == 1
    assert len(stack.ledger.list(reason="checksum_failure")) == 1
    assert len(stack.ledger.list(limit=2)) == 2
    assert stack.ledger.list(limit=1, offset=1)[0].entry_id == 2


def test_a_filter_that_names_nothing_real_is_a_caller_error(stack: Stack) -> None:
    with pytest.raises(GrafxConfigurationError):
        stack.ledger.list(origin_class="unclassifiable")
    with pytest.raises(GrafxConfigurationError):
        stack.ledger.list(reason="something_else")
    with pytest.raises(GrafxConfigurationError):
        stack.ledger.list(limit=-1)


def test_inspecting_an_identifier_the_ledger_does_not_hold_is_refused(stack: Stack) -> None:
    with pytest.raises(GrafxLedgerError):
        stack.ledger.inspect(99)
    with pytest.raises(GrafxConfigurationError):
        stack.ledger.inspect(0)


def test_depth_counts_only_lost_work_and_uses_the_metric_label_values(stack: Stack) -> None:
    _discard(stack.ledger)
    _record_entry(stack.ledger)
    assert stack.ledger.depth() == {"reapplicable": 1, "forensic": 1}
    assert stack.metrics.gauge(LEDGER_DEPTH, origin_class="forensic") == 1.0
    assert stack.metrics.gauge(LEDGER_DEPTH, origin_class="reapplicable") == 1.0


def test_the_oldest_entry_age_is_measured_from_the_wall_clock(stack: Stack) -> None:
    _discard(stack.ledger)
    stack.clock.advance(120.0)
    stack.ledger.open()
    assert stack.metrics.gauge(LEDGER_OLDEST_ENTRY_AGE_SECONDS, origin_class="forensic") == 120.0


# --- exporting -----------------------------------------------------------------------------------


def test_exporting_a_forensic_entry_returns_the_bytes_that_were_captured(stack: Stack) -> None:
    body = bytes(range(64))
    entry_id = _discard(stack.ledger, payload=_payload(body=body))
    assert stack.ledger.export(entry_id) == body


def test_exporting_a_reapplicable_entry_is_refused_because_it_carries_an_operation(
    stack: Stack,
) -> None:
    entry_id = _record_entry(stack.ledger)
    with pytest.raises(GrafxLedgerError) as caught:
        stack.ledger.export(entry_id)
    assert caught.value.details["field"] == "origin_class"


def test_an_export_refuses_bytes_that_were_damaged_after_they_were_written(
    stack: Stack,
) -> None:
    """A flipped byte is caught by the entry's own checksum, before any digest comparison."""
    entry_id = _discard(stack.ledger, payload=_payload(body=b"original"))
    size = stack.storage.log_size(LEDGER_FILE)  # type: ignore[attr-defined]
    raw = bytearray(stack.storage.read_log(LEDGER_FILE, 0, size))  # type: ignore[attr-defined]
    raw[-6] ^= 0xFF
    stack.storage.truncate_log(LEDGER_FILE, 0)  # type: ignore[attr-defined]
    stack.storage.append_log(LEDGER_FILE, bytes(raw))  # type: ignore[attr-defined]
    with pytest.raises(GrafxCorruptionDetected) as caught:
        stack.ledger.export(entry_id)
    assert caught.value.details["field"] == "crc32c"


def test_an_export_refuses_a_valid_entry_that_is_not_the_one_it_named(stack: Stack) -> None:
    """A62: this is the ONLY state the digest comparison can answer for.

    A damaged entry is refused by its own checksum a moment earlier, so a test that damages one
    proves nothing about this guard. What only this guard can catch is a SUBSTITUTION: an entry
    that is internally perfect, carries the identifier the caller asked for, and is not the entry
    whose bytes were recorded. Without it, ``export`` would hand back somebody else's bytes with
    no error at all -- wrong results, quietly.
    """
    entry_id = _discard(stack.ledger, payload=_payload(body=b"original"))
    original = stack.ledger.inspect(entry_id)
    substitute = LedgerEntry(
        entry_id=original.entry_id,
        origin_class=original.origin_class,
        reason=original.reason,
        payload=encode_payload(_payload(body=b"substituted")),
        entry_type=original.entry_type,
        lsn_start=original.lsn_start,
        lsn_end=original.lsn_end,
        epoch=original.epoch,
        captured_at_wall=original.captured_at_wall,
    )
    stack.storage.truncate_log(LEDGER_FILE, 0)  # type: ignore[attr-defined]
    stack.storage.append_log(LEDGER_FILE, substitute.encode())  # type: ignore[attr-defined]
    with pytest.raises(GrafxLedgerError) as caught:
        stack.ledger.export(entry_id)
    assert caught.value.details["field"] == "digest"


def test_the_provenance_of_an_entry_names_its_origin_and_offset(stack: Stack) -> None:
    entry_id = _discard(stack.ledger, payload=_payload(offset=4096))
    provenance = stack.ledger.provenance(entry_id)
    assert provenance.origin == "wal/000000000001.wal"
    assert provenance.offset == 4096


# --- reapplying ------------------------------------------------------------------------------------


def test_reapplying_a_reapplicable_entry_hands_the_decoded_record_to_the_applier(
    stack: Stack,
) -> None:
    entry_id = _record_entry(stack.ledger, lsn=11)
    seen: list[WalRecord] = []
    report = stack.ledger.reprocess(entry_id, seen.append)
    assert report.applied is True and report.already_applied is False
    assert [record.lsn for record in seen] == [11]
    assert seen[0].record_type == int(WalRecordType.WRITE_PAGE)


def test_reapplying_the_same_entry_again_is_a_no_operation(stack: Stack) -> None:
    entry_id = _record_entry(stack.ledger, lsn=11)
    calls: list[WalRecord] = []
    first = stack.ledger.reprocess(entry_id, calls.append)
    second = stack.ledger.reprocess(entry_id, calls.append)
    assert len(calls) == 1
    assert first.applied is True
    assert second.applied is False and second.already_applied is True
    assert second.receipt_id == first.receipt_id


def test_the_receipt_makes_reapplying_a_no_operation_across_a_reopen(stack: Stack) -> None:
    entry_id = _record_entry(stack.ledger, lsn=11)
    calls: list[WalRecord] = []
    stack.ledger.reprocess(entry_id, calls.append)
    reopened = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    again = reopened.reprocess(entry_id, calls.append)
    assert len(calls) == 1
    assert again.already_applied is True


def test_reapplying_a_forensic_entry_fails_with_a_typed_error(stack: Stack) -> None:
    entry_id = _discard(stack.ledger)
    with pytest.raises(GrafxLedgerError) as caught:
        stack.ledger.reprocess(entry_id, lambda record: None)
    assert caught.value.details["field"] == "origin_class"
    assert caught.value.code == "ledger_error"


def test_reapplying_a_receipt_is_refused_because_a_receipt_is_not_lost_work(stack: Stack) -> None:
    entry_id = _record_entry(stack.ledger, lsn=11)
    report = stack.ledger.reprocess(entry_id, lambda record: None)
    with pytest.raises(GrafxLedgerError) as caught:
        stack.ledger.reprocess(report.receipt_id, lambda record: None)
    assert caught.value.details["field"] == "entry_type"


def test_an_applier_that_fails_leaves_no_receipt_so_a_retry_is_possible(stack: Stack) -> None:
    entry_id = _record_entry(stack.ledger, lsn=11)
    attempts: list[int] = []

    def failing(record: WalRecord) -> None:
        """Fail the first time and succeed afterwards."""
        attempts.append(record.lsn)
        if len(attempts) == 1:
            raise GrafxDeviceFull("The device has no room for this page.")

    with pytest.raises(GrafxDeviceFull):
        stack.ledger.reprocess(entry_id, failing)
    assert stack.ledger.list(origin_class="reapplicable")[-1].entry_type is LedgerEntryType.DISCARD
    report = stack.ledger.reprocess(entry_id, failing)
    assert report.applied is True and attempts == [11, 11]


def test_an_applier_that_raises_a_foreign_exception_leaves_only_a_grafx_error(
    stack: Stack,
) -> None:
    entry_id = _record_entry(stack.ledger, lsn=11)

    def exploding(record: WalRecord) -> None:
        """Raise something that is not part of the taxonomy."""
        raise ValueError("this is not a Grafx error")

    with pytest.raises(GrafxLedgerError) as caught:
        stack.ledger.reprocess(entry_id, exploding)
    assert caught.value.details["applier_error"] == "ValueError"


def test_an_entry_with_no_origin_sequence_number_cannot_be_applied_exactly_once(
    stack: Stack,
) -> None:
    entry_id = stack.ledger.record_discard(
        origin_class=LedgerOriginClass.REAPPLICABLE,
        reason=LedgerReason.TRUNCATED_TAIL,
        payload=_payload(),
    )
    with pytest.raises(GrafxLedgerError) as caught:
        stack.ledger.reprocess(entry_id, lambda record: None)
    assert caught.value.details["field"] == "lsn_start"


def test_an_applier_that_is_not_callable_is_a_caller_error(stack: Stack) -> None:
    entry_id = _record_entry(stack.ledger)
    with pytest.raises(GrafxConfigurationError):
        stack.ledger.reprocess(entry_id, "not callable")  # type: ignore[arg-type]


# --- purging ---------------------------------------------------------------------------------------


def test_purging_without_the_exact_token_removes_nothing(stack: Stack) -> None:
    _discard(stack.ledger)
    with pytest.raises(GrafxLedgerError) as caught:
        stack.ledger.purge(confirm_token="yes")
    assert caught.value.details["field"] == "confirm_token"
    assert len(stack.ledger.list()) == 1


def test_purging_with_the_token_removes_the_entries_and_keeps_the_rest(stack: Stack) -> None:
    first = _discard(stack.ledger)
    _discard(stack.ledger)
    removed = stack.ledger.purge(confirm_token=PURGE_CONFIRM_TOKEN, entry_ids=[first])
    assert removed == 1
    remaining = stack.ledger.list()
    assert [entry.entry_id for entry in remaining] == [2]
    reopened = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    assert [entry.entry_id for entry in reopened.list()] == [2]


def test_a_receipt_is_never_purged_because_that_would_allow_a_second_application(
    stack: Stack,
) -> None:
    entry_id = _record_entry(stack.ledger, lsn=11)
    report = stack.ledger.reprocess(entry_id, lambda record: None)
    with pytest.raises(GrafxLedgerError) as caught:
        stack.ledger.purge(confirm_token=PURGE_CONFIRM_TOKEN, entry_ids=[report.receipt_id])
    assert caught.value.details["field"] == "entry_type"
    stack.ledger.purge(confirm_token=PURGE_CONFIRM_TOKEN)
    assert [entry.entry_id for entry in stack.ledger.list()] == [report.receipt_id]
    calls: list[WalRecord] = []
    reopened = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    with pytest.raises(GrafxLedgerError):
        reopened.reprocess(entry_id, calls.append)
    assert calls == []


def test_purging_with_identifiers_that_are_not_a_sequence_is_a_typed_refusal(
    stack: Stack,
) -> None:
    """B4: a FROZEN signature, on the one destructive operator door; no raw TypeError."""
    _discard(stack.ledger)
    for wrong in (17, -1, 3.5, True, object(), "12"):
        with pytest.raises(GrafxConfigurationError) as caught:
            stack.ledger.purge(confirm_token=PURGE_CONFIRM_TOKEN, entry_ids=wrong)  # type: ignore[arg-type]
        assert caught.value.details["field"] == "entry_ids"
    assert len(stack.ledger.list()) == 1


def test_purging_with_an_empty_sequence_removes_nothing(stack: Stack) -> None:
    _discard(stack.ledger)
    assert stack.ledger.purge(confirm_token=PURGE_CONFIRM_TOKEN, entry_ids=[]) == 0
    assert len(stack.ledger.list()) == 1


def test_purging_an_empty_ledger_is_a_no_operation(stack: Stack) -> None:
    assert stack.ledger.purge(confirm_token=PURGE_CONFIRM_TOKEN) == 0


# --- one entry per damage (B1) ---------------------------------------------------------------------


def test_recording_the_same_damage_twice_returns_the_entry_that_already_describes_it(
    stack: Stack,
) -> None:
    """CONTRACT.md section 8.6 step 4: EXACTLY one entry per discarded record (G8, BR-3)."""
    payload = _payload(offset=900)
    first = stack.ledger.record_discard(
        origin_class=LedgerOriginClass.FORENSIC,
        reason=LedgerReason.CHECKSUM_FAILURE,
        payload=payload,
        lsn_start=5,
        lsn_end=5,
    )
    second = stack.ledger.record_discard(
        origin_class=LedgerOriginClass.FORENSIC,
        reason=LedgerReason.CHECKSUM_FAILURE,
        payload=payload,
        lsn_start=5,
        lsn_end=5,
    )
    assert second == first
    assert len(stack.ledger.list()) == 1


def test_the_damage_identity_survives_a_reopen_so_a_later_run_recognises_it(
    stack: Stack,
) -> None:
    payload = _payload(offset=900)
    first = stack.ledger.record_discard(
        origin_class=LedgerOriginClass.FORENSIC,
        reason=LedgerReason.CHECKSUM_FAILURE,
        payload=payload,
        lsn_start=5,
        lsn_end=5,
    )
    reopened = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    again = reopened.record_discard(
        origin_class=LedgerOriginClass.FORENSIC,
        reason=LedgerReason.CHECKSUM_FAILURE,
        payload=payload,
        lsn_start=5,
        lsn_end=5,
    )
    assert again == first
    assert len(reopened.list()) == 1


def test_a_different_range_of_the_same_file_is_a_different_entry(stack: Stack) -> None:
    """The dedupe must not collapse two genuinely different discards into one."""
    first = _discard(stack.ledger, payload=_payload(offset=100))
    second = _discard(stack.ledger, payload=_payload(offset=200))
    third = _discard(stack.ledger, payload=_payload(origin="wal/000000000002.wal", offset=100))
    assert len({first, second, third}) == 3
    assert len(stack.ledger.list()) == 3


def test_a_retirement_and_a_discard_of_one_file_are_not_the_same_statement(
    stack: Stack,
) -> None:
    payload = _payload(origin="control/a.reader", offset=0)
    discard = _discard(stack.ledger, payload=payload)
    retirement = stack.ledger.record_retirement(payload=payload)
    assert discard != retirement
    assert stack.ledger.record_retirement(payload=payload) == retirement


# --- a damaged tail -------------------------------------------------------------------------------


def test_a_partial_entry_at_the_tail_is_reported_and_the_good_entries_survive(
    stack: Stack,
) -> None:
    _discard(stack.ledger)
    _discard(stack.ledger)
    size = stack.storage.log_size(LEDGER_FILE)  # type: ignore[attr-defined]
    stack.storage.append_log(LEDGER_FILE, b"\x00" * 20)  # type: ignore[attr-defined]
    reopened = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    assert len(reopened.list()) == 2
    damage = reopened.damage
    assert damage is not None and damage.offset == size and damage.length == 20


def test_appending_behind_a_damaged_tail_is_refused_and_names_the_remedy(stack: Stack) -> None:
    _discard(stack.ledger)
    stack.storage.append_log(LEDGER_FILE, b"\x00" * 20)  # type: ignore[attr-defined]
    reopened = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    with pytest.raises(GrafxLedgerError) as caught:
        _discard(reopened)
    assert caught.value.details["field"] == "damage"
    assert "discard the damaged tail" in caught.value.message


def test_discarding_the_damaged_tail_makes_the_ledger_writable_again(stack: Stack) -> None:
    _discard(stack.ledger)
    stack.storage.append_log(LEDGER_FILE, b"\x00" * 20)  # type: ignore[attr-defined]
    reopened = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    discard = reopened.discard_damaged_tail()
    assert discard.removed_bytes == 20
    assert reopened.damage is None
    assert _discard(reopened) == 2
    assert len(reopened.list()) == 2


def test_discarding_the_damaged_tail_preserves_it_in_quarantine_first(stack: Stack) -> None:
    _discard(stack.ledger)
    stack.storage.append_log(LEDGER_FILE, b"\xa5" * 20)  # type: ignore[attr-defined]
    reopened = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    discard = reopened.discard_damaged_tail()
    assert stack.quarantine.read(discard.quarantine) == b"\xa5" * 20


def test_a_damaged_first_entry_does_not_destroy_the_entries_behind_it(stack: Stack) -> None:
    """The whole unreadable range is preserved, including entries that were perfectly good."""
    _discard(stack.ledger)
    _discard(stack.ledger)
    size = stack.storage.log_size(LEDGER_FILE)  # type: ignore[attr-defined]
    raw = bytearray(stack.storage.read_log(LEDGER_FILE, 0, size))  # type: ignore[attr-defined]
    raw[100] ^= 0xFF
    stack.storage.truncate_log(LEDGER_FILE, 0)  # type: ignore[attr-defined]
    stack.storage.append_log(LEDGER_FILE, bytes(raw))  # type: ignore[attr-defined]
    reopened = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    damage = reopened.damage
    assert damage is not None and damage.offset == 0
    discard = reopened.discard_damaged_tail()
    assert reopened.list() == ()
    # Nothing was lost: every byte the cut removed is in quarantine, second entry included.
    assert stack.quarantine.read(discard.quarantine) == bytes(raw)


def test_discarding_a_tail_with_no_quarantine_to_copy_into_is_refused(stack: Stack) -> None:
    _discard(stack.ledger)
    stack.storage.append_log(LEDGER_FILE, b"\x00" * 20)  # type: ignore[attr-defined]
    before = stack.storage.log_size(LEDGER_FILE)  # type: ignore[attr-defined]
    bare = LedgerStore(stack.storage, stack.clock, stack.metrics)  # type: ignore[arg-type]
    with pytest.raises(GrafxLedgerError) as caught:
        bare.discard_damaged_tail()
    assert caught.value.details["field"] == "quarantine"
    assert stack.storage.log_size(LEDGER_FILE) == before  # type: ignore[attr-defined]


def test_a_quarantine_of_the_wrong_type_is_refused_at_construction(stack: Stack) -> None:
    with pytest.raises(GrafxConfigurationError) as caught:
        LedgerStore(
            stack.storage,  # type: ignore[arg-type]
            stack.clock,
            stack.metrics,
            quarantine="not a store",  # type: ignore[arg-type]
        )
    assert caught.value.details["field"] == "quarantine"


def test_discarding_a_tail_that_is_not_damaged_is_refused(stack: Stack) -> None:
    _discard(stack.ledger)
    with pytest.raises(GrafxLedgerError) as caught:
        stack.ledger.discard_damaged_tail()
    assert caught.value.details["field"] == "damage"


# --- the retry rule of amendment A47 -------------------------------------------------------------


def test_a_retry_decision_reads_the_details_and_not_the_class() -> None:
    lying = GrafxStorageError("A sharing violation was met.", retryable=False)
    lying.details["retryable"] = True
    assert is_retryable(lying) is True
    honest = GrafxDeviceFull("The device is full.")
    honest.details["retryable"] = False
    assert is_retryable(honest) is False
    assert is_retryable(ValueError("not a Grafx error")) is False


def test_an_append_rides_out_a_device_condition_its_details_call_retryable(
    stack: Stack,
) -> None:
    device = stack.storage
    attempts: list[int] = []
    original = device.append_log  # type: ignore[attr-defined]

    def flaky(file: str, payload: bytes) -> int:
        """Refuse the first append with a condition that declares itself retryable."""
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            failure = GrafxStorageError("An indexer is holding the ledger open.")
            failure.details["retryable"] = True
            raise failure
        return original(file, payload)

    device.append_log = flaky  # type: ignore[attr-defined]
    try:
        entry_id = _discard(stack.ledger)
    finally:
        device.append_log = original  # type: ignore[attr-defined]
    assert entry_id == 1 and len(attempts) == 2
    assert stack.ledger.inspect(entry_id).entry_id == 1


def test_an_append_does_not_ride_out_a_condition_its_details_call_permanent(
    stack: Stack,
) -> None:
    device = stack.storage
    attempts: list[int] = []
    original = device.append_log  # type: ignore[attr-defined]

    def refusing(file: str, payload: bytes) -> int:
        """Refuse with a condition that declares itself permanent."""
        attempts.append(len(attempts) + 1)
        failure = GrafxStorageError("The path is gone.", retryable=True)
        failure.details["retryable"] = False
        raise failure

    device.append_log = refusing  # type: ignore[attr-defined]
    try:
        with pytest.raises(GrafxStorageError):
            _discard(stack.ledger)
    finally:
        device.append_log = original  # type: ignore[attr-defined]
    assert len(attempts) == 1
    assert APPEND_ATTEMPTS > 1


def test_the_ledger_index_survives_a_reopen_after_every_kind_of_entry(stack: Stack) -> None:
    _discard(stack.ledger)
    reapplicable = _record_entry(stack.ledger, lsn=21)
    stack.ledger.reprocess(reapplicable, lambda record: None)
    stack.ledger.record_retirement(payload=_payload(origin="control/a.reader"))
    reopened = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    kinds = [entry.entry_type for entry in reopened.list()]
    assert kinds == [
        LedgerEntryType.DISCARD,
        LedgerEntryType.DISCARD,
        LedgerEntryType.REPROCESS_RECEIPT,
        LedgerEntryType.RETIREMENT,
    ]


def test_an_entry_of_a_newer_format_stops_the_read_at_that_entry(stack: Stack) -> None:
    _discard(stack.ledger)
    offset = stack.storage.log_size(LEDGER_FILE)  # type: ignore[attr-defined]
    future = bytearray(
        LedgerEntry(
            entry_id=2,
            origin_class=LedgerOriginClass.FORENSIC,
            reason=LedgerReason.TRUNCATED_TAIL,
            payload=encode_payload(_payload()),
        ).encode()
    )
    future[4:6] = struct.pack("<H", 99)
    stack.storage.append_log(LEDGER_FILE, bytes(future))  # type: ignore[attr-defined]
    reopened = LedgerStore(
        stack.storage, stack.clock, stack.metrics, quarantine=stack.quarantine
    )
    assert len(reopened.list()) == 1
    damage = reopened.damage
    assert damage is not None and damage.offset == offset


def test_appending_the_same_damage_twice_through_the_raw_door_is_refused(stack: Stack) -> None:
    """G8/BR-3 at the door that has no identifier to hand back.

    The typed doors short-circuit on the damage identity and RETURN the entry that already
    describes the range, which is what makes an interrupted recovery converge -- so a test that
    used one of them would be satisfied by ``_record_once`` and would say nothing about this
    guard (A62). ``append`` is the raw door: a caller that assembled an entry by hand and
    appended it twice is told, rather than quietly doubling the ledger.
    """
    payload = _payload(origin="wal/000000000009.wal", body=b"raw", offset=4096)

    def built() -> LedgerEntry:
        return LedgerEntry(
            entry_id=0,
            origin_class=LedgerOriginClass.FORENSIC,
            reason=LedgerReason.CHECKSUM_FAILURE,
            payload=encode_payload(payload),
        )

    first = stack.ledger.append(built())
    with pytest.raises(GrafxLedgerError) as caught:
        stack.ledger.append(built())
    assert caught.value.details["field"] == "identity"
    assert caught.value.details["entry_id"] == first
    assert caught.value.details["file"] == "wal/000000000009.wal"
    assert caught.value.details["offset"] == 4096
    assert len(stack.ledger.entries()) == 1


def test_a_raw_entry_describing_a_different_range_is_still_accepted(stack: Stack) -> None:
    """The other side of the rule, so the guard above cannot pass by refusing everything (A85)."""

    def built(offset: int) -> LedgerEntry:
        return LedgerEntry(
            entry_id=0,
            origin_class=LedgerOriginClass.FORENSIC,
            reason=LedgerReason.CHECKSUM_FAILURE,
            payload=encode_payload(_payload(body=b"raw", offset=offset)),
        )

    stack.ledger.append(built(8192))
    stack.ledger.append(built(8256))
    assert len(stack.ledger.entries()) == 2
