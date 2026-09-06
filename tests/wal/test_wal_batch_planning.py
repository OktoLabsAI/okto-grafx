"""WRITE-4/M1/M2: planning a batch neither rebuilds exact records nor plans the same objects twice."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from enum import IntEnum

import pytest

import okto_grafx.engine.wal_manager as wal_module
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxError, GrafxStaleEpoch
from okto_grafx.domain.wal import WalRecord, WalRecordType
from okto_grafx.engine.wal_manager import WalManager, _canonical_record

from .conftest import DESCRIPTOR, make_record


class _Kind(IntEnum):
    COMMIT = int(WalRecordType.COMMIT)


class _Text(str):
    """A str subclass: canonicalised, never returned as it is."""


def _legacy_canonical_record(record: WalRecord, position: int) -> WalRecord:
    """The canonicaliser of eca8e17, verbatim, as the oracle of every refusal and every field."""

    def value_of(field: str) -> object:
        try:
            return object.__getattribute__(record, field)
        except Exception as failure:
            cause = wal_module._builtin_type_name(failure)
            raise GrafxConfigurationError(
                f"Entry {position} of the WAL batch has no readable {field!r}; got {cause}.",
                field="records",
                value=position,
                record_field=field,
                cause=cause,
            ) from failure

    def integer(field: str) -> int:
        value = value_of(field)
        if type(value) is bool or not issubclass(type(value), int):
            observed = wal_module._builtin_type_name(value)
            raise GrafxConfigurationError(
                f"Entry {position} of the WAL batch has a non-integer {field!r}: {observed}.",
                field=field,
                value=observed,
                position=position,
            )
        return int.__int__(value)

    raw_payload = value_of("payload")
    if not isinstance(raw_payload, (bytes, bytearray, memoryview)):
        observed = wal_module._builtin_type_name(raw_payload)
        raise GrafxConfigurationError(
            f"Entry {position} of the WAL batch has a non-bytes payload: {observed}.",
            field="payload",
            value=observed,
            position=position,
        )
    try:
        payload = (
            raw_payload if type(raw_payload) is bytes else memoryview(raw_payload).tobytes()
        )
    except GrafxError:
        raise
    except Exception as failure:
        cause = wal_module._builtin_type_name(failure)
        raise GrafxConfigurationError(
            f"Entry {position} of the WAL batch has an unreadable payload; got {cause}.",
            field="payload",
            value=cause,
            position=position,
        ) from failure
    raw_descriptor = value_of("descriptor")
    if not issubclass(type(raw_descriptor), str):
        observed = wal_module._builtin_type_name(raw_descriptor)
        raise GrafxConfigurationError(
            f"Entry {position} of the WAL batch has a non-string descriptor: {observed}.",
            field="descriptor",
            value=observed,
            position=position,
        )
    return WalRecord(
        record_type=integer("record_type"),
        payload=payload,
        descriptor=str.__str__(raw_descriptor),
        lsn=integer("lsn"),
        epoch=integer("epoch"),
        txn_id=integer("txn_id"),
        flags=integer("flags"),
        format_version=integer("format_version"),
    )


def _legacy_stamped(record: WalRecord, position: int, descriptor: str) -> WalRecord:
    """What _validate_batch produced at eca8e17: canonical, then stamped when unstamped."""
    canonical = _legacy_canonical_record(record, position)
    return canonical if canonical.descriptor else canonical.with_descriptor(descriptor)


def _outcome(function: Callable[[], object]) -> tuple:
    try:
        value = function()
    except GrafxError as failure:
        return ("erro", type(failure).__name__, repr(sorted(failure.to_dict().items())))
    except Exception as failure:  # noqa: BLE001 - the shape of a Python error is the fixture
        return ("erro_py", type(failure).__name__, str(failure))
    assert isinstance(value, WalRecord)
    return (
        "registro",
        value.record_type,
        value.payload,
        value.descriptor,
        value.lsn,
        value.epoch,
        value.txn_id,
        value.flags,
        value.format_version,
        type(value.record_type).__name__,
        type(value.payload).__name__,
        type(value.descriptor).__name__,
    )


def _hostile_records() -> list[WalRecord]:
    """Exact and not-quite-exact records, plus records mutated behind the frozen door."""
    exact = make_record(1)
    corpus: list[WalRecord] = [
        exact,
        make_record(2, record_type=WalRecordType.COMMIT),
        replace(exact, descriptor=""),
        replace(exact, descriptor=DESCRIPTOR),
        replace(exact, record_type=_Kind.COMMIT),
        replace(exact, payload=bytearray(b"ba")),
        replace(exact, payload=memoryview(b"mv")),
        replace(exact, descriptor=_Text("hash-v1;partitions_per_table=64")),
        replace(exact, txn_id=7, flags=0, epoch=3),
    ]
    for field, value in (
        ("record_type", True),
        ("lsn", 2.0),
        ("epoch", "x"),
        ("payload", "text"),
        ("descriptor", b"bytes"),
        ("format_version", 70_000),
        ("txn_id", -1),
        ("payload", bytes(1)),
        ("descriptor", "\udc80"),
    ):
        broken = replace(exact)
        object.__setattr__(broken, field, value)
        corpus.append(broken)
    return corpus


def test_canonicalisation_answers_exactly_as_the_legacy_copy_for_every_hostile_record() -> None:
    for position, record in enumerate(_hostile_records()):
        expected = _outcome(lambda: _legacy_stamped(record, position, DESCRIPTOR))
        observed = _outcome(lambda: _canonical_record(record, position, DESCRIPTOR))
        assert observed == expected, (position, record)
        # And without a log descriptor, the plain canonical form.
        expected = _outcome(lambda: _legacy_canonical_record(record, position))
        observed = _outcome(lambda: _canonical_record(record, position))
        assert observed == expected, (position, record)


def test_an_exact_record_is_returned_as_the_object_it_is() -> None:
    exact = replace(
        make_record(1, descriptor=DESCRIPTOR), record_type=int(WalRecordType.WRITE_PAGE)
    )
    object.__setattr__(exact, "_decoded", ("proof",))
    assert _canonical_record(exact, 0, DESCRIPTOR) is exact
    assert exact._decoded == ("proof",)
    unstamped = replace(exact, descriptor="")
    stamped = _canonical_record(unstamped, 0, DESCRIPTOR)
    assert stamped is not unstamped
    assert stamped.descriptor == DESCRIPTOR
    assert stamped == unstamped.with_descriptor(DESCRIPTOR)


def test_a_record_that_is_not_exact_is_still_copied_into_exact_builtins() -> None:
    record = replace(make_record(1), record_type=_Kind.COMMIT, payload=bytearray(b"x"))
    canonical = _canonical_record(record, 0, DESCRIPTOR)
    assert canonical is not record
    assert type(canonical.record_type) is int
    assert type(canonical.payload) is bytes
    assert canonical.descriptor == DESCRIPTOR
    assert canonical == _legacy_stamped(record, 0, DESCRIPTOR)


def _exact(record: WalRecord) -> WalRecord:
    """The shape production builders hand the log: exact ints, exact bytes, exact str."""
    return replace(record, record_type=int(record.record_type))


def _count_canonicalisations(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    original = wal_module._canonical_record

    def counted(record, position, descriptor=""):  # type: ignore[no-untyped-def]
        calls[0] += 1
        return original(record, position, descriptor)

    monkeypatch.setattr(wal_module, "_canonical_record", counted)
    return calls


def test_the_append_reuses_the_canonical_form_the_preview_validated(
    wal: WalManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_canonicalisations(monkeypatch)
    records = (
        _exact(make_record(1)),
        replace(_exact(make_record(2)), descriptor=""),
        _exact(make_record(3, record_type=WalRecordType.COMMIT)),
    )
    planned = wal.planned_terminal_lsn(records)
    assert calls[0] == 3
    assert wal.append_many(records, expected_terminal_lsn=planned) == planned
    assert calls[0] == 3  # nothing canonicalised twice
    assert wal._planned_canonical == {} and wal._planned_records == ()

    # A batch whose objects the preview never saw is canonicalised again, in full.
    fresh = (_exact(make_record(4)), _exact(make_record(5, record_type=WalRecordType.COMMIT)))
    wal.append_many(fresh)
    assert calls[0] == 5

    # A record that is not exact (an IntEnum type) is never served from the memo: the append
    # canonicalises it again, so its copy is as fresh as its refusals would be.
    inexact = (make_record(6), _exact(make_record(7, record_type=WalRecordType.COMMIT)))
    planned = wal.planned_terminal_lsn(inexact)
    assert calls[0] == 7
    wal.append_many(inexact, expected_terminal_lsn=planned)
    assert calls[0] == 8


def test_the_memo_serves_only_the_objects_it_remembers(
    wal: WalManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_canonicalisations(monkeypatch)
    first = _exact(make_record(1))
    commit = _exact(make_record(2, record_type=WalRecordType.COMMIT))
    planned = wal.planned_terminal_lsn((first, commit))
    assert calls[0] == 2
    # The commit path may replace page records (compression) before it appends: only the
    # objects that survive are served from the memo.
    replacement = replace(first, payload=b"compressed")
    assert wal.append_many((replacement, commit), expected_terminal_lsn=planned) == planned
    assert calls[0] == 3


def test_a_tail_change_between_preview_and_append_still_refuses_the_epoch(
    wal: WalManager,
) -> None:
    """The memo keeps the record-intrinsic half only; the epoch is judged again at the append."""
    records = (make_record(1, epoch=1), make_record(2, record_type=WalRecordType.COMMIT, epoch=1))
    planned = wal.planned_terminal_lsn(records)
    wal.append(make_record(9, epoch=5))
    with pytest.raises(GrafxStaleEpoch):
        wal.append_many(records, expected_terminal_lsn=planned)
    assert wal._planned_canonical == {}


def test_a_second_preview_replaces_the_first(wal: WalManager, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _count_canonicalisations(monkeypatch)
    first = (_exact(make_record(1)), _exact(make_record(2, record_type=WalRecordType.COMMIT)))
    second = (_exact(make_record(3)), _exact(make_record(4, record_type=WalRecordType.COMMIT)))
    wal.planned_terminal_lsn(first)
    planned = wal.planned_terminal_lsn(second)
    assert calls[0] == 4
    wal.append_many(second, expected_terminal_lsn=planned)
    assert calls[0] == 4
    wal.append_many(first)
    assert calls[0] == 6  # the first preview was forgotten, so its objects are planned anew
