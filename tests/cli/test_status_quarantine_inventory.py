"""Status derives quarantine evidence from one complete public inventory snapshot."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from okto_grafx.cli import commands
from okto_grafx.cli.exits import FINDINGS, INCONCLUSIVE, INTERNAL, OK
from okto_grafx.cli.parser import Invocation, parse
from okto_grafx.domain.errors import (
    GrafxQuarantineError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.recovery.report import RecoveryReport
from okto_grafx.engine.quarantine import (
    QuarantineInventoryItem,
    QuarantineInventoryState,
)

from tests.cli.conftest import CliRunner

STATES: tuple[QuarantineInventoryState, ...] = (
    "complete",
    "incomplete",
    "corrupt_manifest",
    "unreadable_manifest",
    "missing_payload",
    "manifest_mismatch",
    "unexpected_layout",
)


class _Ledger:
    damage = None

    def depth(self) -> dict[str, int]:
        return {}


class _InventorySnapshot:
    """Observable public-snapshot shape whose legacy doors are fatal if status uses them."""

    def __init__(
        self,
        items: tuple[QuarantineInventoryItem, ...] = (),
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.items = items
        self.failure = failure
        self.inventory_calls = 0

    def inventory(self) -> tuple[QuarantineInventoryItem, ...]:
        self.inventory_calls += 1
        if self.failure is not None:
            raise self.failure
        return self.items

    def count(self) -> None:
        raise AssertionError("status must not call the legacy quarantine count")

    def list(self) -> None:
        raise AssertionError("status must not call the legacy quarantine list")


class _StatusDatabase:
    path = "status-database"
    read_only = False
    metrics_endpoint = None
    identity = SimpleNamespace(
        database_uuid=b"\x17" * 16,
        page_size=8192,
        partitions_per_table=64,
        created_at_wall=1.0,
        granularity_descriptor="hash-v1;partitions_per_table=64",
        format_version=1,
    )

    def __init__(
        self,
        quarantine: _InventorySnapshot,
        *,
        close_failure: BaseException | None = None,
    ) -> None:
        self._quarantine = quarantine
        self.close_failure = close_failure
        self.ledger = _Ledger()
        self.quarantine_accesses = 0
        self.recovery_accesses = 0
        self.close_calls = 0

    @property
    def recovery_report(self) -> RecoveryReport:
        self.recovery_accesses += 1
        return RecoveryReport(outcome="clean")

    @property
    def quarantine(self) -> _InventorySnapshot:
        self.quarantine_accesses += 1
        return self._quarantine

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failure is not None:
            raise self.close_failure


def _item(state: QuarantineInventoryState, position: int = 0) -> QuarantineInventoryItem:
    name = f"item-{position}-{state}"
    return QuarantineInventoryItem(
        name=name,
        state=state,
        files=(f"quarantine/{name}/evidence.bin",),
        detail=f"evidence in state {state}",
    )


def _install_database(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: _InventorySnapshot,
    *,
    close_failure: BaseException | None = None,
) -> list[_StatusDatabase]:
    opened: list[_StatusDatabase] = []

    def open_database(_invocation: object) -> _StatusDatabase:
        database = _StatusDatabase(snapshot, close_failure=close_failure)
        opened.append(database)
        return database

    monkeypatch.setattr(commands, "_open", open_database)
    return opened


def _status_invocation() -> Invocation:
    outcome = parse(["status", "status-database"])
    assert outcome.invocation is not None
    return outcome.invocation


def _forbid_second_inventory_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("status must not open the dedicated inventory reader")

    monkeypatch.setattr(commands, "open_quarantine_inventory", forbidden)


def test_incomplete_only_is_physical_evidence_but_not_a_legacy_complete_entry(
    monkeypatch: pytest.MonkeyPatch, cli: CliRunner
) -> None:
    snapshot = _InventorySnapshot((_item("incomplete"),))
    opened = _install_database(monkeypatch, snapshot)
    _forbid_second_inventory_reader(monkeypatch)

    machine = cli("status", "status-database", "--json")

    assert machine.code == FINDINGS
    quarantine = machine.document["quarantine"]
    assert quarantine["entries"] == 0
    assert quarantine["inventory"] == {
        "conclusive": True,
        "count": 1,
        "states": {state: int(state == "incomplete") for state in STATES},
    }
    assert "physical inventory item" in machine.document["reasons"][0]
    assert snapshot.inventory_calls == 1
    assert opened[0].quarantine_accesses == 1
    assert opened[0].recovery_accesses == 1


def test_status_counts_all_seven_states_and_complete_entries_from_the_same_snapshot(
    monkeypatch: pytest.MonkeyPatch, cli: CliRunner
) -> None:
    snapshot = _InventorySnapshot(
        tuple(_item(state, position) for position, state in enumerate(STATES))
    )
    opened = _install_database(monkeypatch, snapshot)

    run = cli("status", "status-database", "--json")

    assert run.code == FINDINGS
    quarantine = run.document["quarantine"]
    assert quarantine["entries"] == 1
    assert quarantine["inventory"] == {
        "conclusive": True,
        "count": 7,
        "states": {state: 1 for state in STATES},
    }
    assert snapshot.inventory_calls == 1
    assert opened[0].quarantine_accesses == 1


def test_status_uses_one_public_inventory_call_and_no_legacy_or_second_reader(
    monkeypatch: pytest.MonkeyPatch, cli: CliRunner
) -> None:
    snapshot = _InventorySnapshot((_item("complete"),))
    opened = _install_database(monkeypatch, snapshot)
    _forbid_second_inventory_reader(monkeypatch)

    run = cli("status", "status-database", "--json")

    assert run.code == FINDINGS
    assert snapshot.inventory_calls == 1
    assert opened[0].quarantine_accesses == 1
    assert opened[0].close_calls == 1


def test_empty_inventory_is_conclusive_clean_and_keeps_every_zero_state(
    monkeypatch: pytest.MonkeyPatch, cli: CliRunner
) -> None:
    snapshot = _InventorySnapshot()
    _install_database(monkeypatch, snapshot)

    machine = cli("status", "status-database", "--json")
    text = cli("status", "status-database")

    assert machine.code == text.code == OK
    assert machine.document["quarantine"] == {
        "entries": 0,
        "inventory": {
            "conclusive": True,
            "count": 0,
            "states": {state: 0 for state in STATES},
        },
    }
    assert "0 physical item(s)" in text.out
    for state in STATES:
        assert f"{state}=0" in text.out
    assert "this database is clean." in text.out
    assert snapshot.inventory_calls == 2


def test_typed_inventory_failure_propagates_through_the_existing_taxonomy(
    monkeypatch: pytest.MonkeyPatch, cli: CliRunner
) -> None:
    failure = GrafxQuarantineError(
        "The public snapshot cannot certify inventory.",
        inconclusive=True,
        conclusive=False,
    )
    snapshot = _InventorySnapshot(failure=failure)
    opened = _install_database(monkeypatch, snapshot)

    run = cli("status", "status-database", "--json")

    assert run.code == INCONCLUSIVE
    assert run.document["error"]["code"] == "quarantine_error"
    assert run.document["error"]["inconclusive"] is True
    assert "quarantine" not in run.document
    assert snapshot.inventory_calls == 1
    assert opened[0].quarantine_accesses == 1
    assert opened[0].close_calls == 1


def test_status_json_text_reason_and_exit_share_the_same_physical_total(
    monkeypatch: pytest.MonkeyPatch, cli: CliRunner
) -> None:
    snapshot = _InventorySnapshot(
        (_item("incomplete", 1), _item("missing_payload", 2))
    )
    _install_database(monkeypatch, snapshot)

    machine = cli("status", "status-database", "--json")
    text = cli("status", "status-database")

    assert machine.code == text.code == FINDINGS
    assert machine.document["exit_code"] == FINDINGS
    assert machine.document["result"] == "findings"
    assert machine.document["quarantine"]["entries"] == 0
    assert machine.document["quarantine"]["inventory"]["count"] == 2
    assert any("2 physical inventory item" in reason for reason in machine.document["reasons"])
    assert "2 physical item(s), 0 complete entry(ies)" in text.out
    assert "incomplete=1" in text.out
    assert "missing_payload=1" in text.out
    assert "this database is NOT clean" in text.out


def test_findings_keep_their_envelope_when_typed_close_refuses(
    monkeypatch: pytest.MonkeyPatch, cli: CliRunner
) -> None:
    snapshot = _InventorySnapshot((_item("incomplete"),))
    _install_database(
        monkeypatch,
        snapshot,
        close_failure=GrafxUnsupportedOperation("secondary close refusal"),
    )

    machine = cli("status", "status-database", "--json")
    text = cli("status", "status-database")

    assert machine.code == text.code == FINDINGS
    assert machine.document["result"] == "findings"
    assert machine.document["quarantine"]["inventory"]["count"] == 1
    assert machine.document["close_problem"] == "secondary close refusal"
    assert "error" not in machine.document
    assert "refused" not in text.err
    assert "could not be closed" in text.err


@pytest.mark.parametrize("secondary", [RuntimeError("close defect"), SystemExit(37)])
def test_findings_win_over_foreign_and_process_control_close(
    monkeypatch: pytest.MonkeyPatch,
    secondary: BaseException,
) -> None:
    snapshot = _InventorySnapshot((_item("missing_payload"),))
    opened = _install_database(monkeypatch, snapshot, close_failure=secondary)

    report = commands.run(_status_invocation())

    assert report.exit_code == FINDINGS
    assert report.payload["quarantine"]["inventory"]["count"] == 1  # type: ignore[index]
    assert "error" not in report.payload
    assert report.payload["close_problem"] == "a secondary close failure"
    assert opened[0].close_calls == 1


def test_typed_inventory_failure_wins_over_process_control_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = GrafxQuarantineError(
        "inventory was not conclusive", inconclusive=True, conclusive=False
    )
    snapshot = _InventorySnapshot(failure=primary)
    opened = _install_database(
        monkeypatch, snapshot, close_failure=SystemExit(29)
    )

    report = commands.run(_status_invocation())

    assert report.exit_code == INCONCLUSIVE
    assert report.payload["error"]["code"] == "quarantine_error"  # type: ignore[index]
    assert report.payload["close_problem"]
    assert opened[0].close_calls == 1


def test_process_control_primary_keeps_identity_when_close_raises_process_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt()
    secondary = SystemExit(31)
    snapshot = _InventorySnapshot(failure=primary)
    opened = _install_database(monkeypatch, snapshot, close_failure=secondary)

    with pytest.raises(KeyboardInterrupt) as raised:
        commands.run(_status_invocation())

    assert raised.value is primary
    assert opened[0].close_calls == 1


def test_clean_result_does_not_swallow_process_control_from_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secondary = SystemExit(41)
    opened = _install_database(
        monkeypatch, _InventorySnapshot(), close_failure=secondary
    )

    with pytest.raises(SystemExit) as raised:
        commands.run(_status_invocation())

    assert raised.value is secondary
    assert opened[0].close_calls == 1


def test_clean_close_does_not_trust_a_foreign_grafx_subclass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[str] = []

    class ForeignRefusal(GrafxUnsupportedOperation):
        def __getattribute__(self, name: str) -> object:
            if name == "retryable":
                probes.append(name)
                raise SystemExit(93)
            return super().__getattribute__(name)

    primary = ForeignRefusal("foreign close refusal")
    _install_database(
        monkeypatch, _InventorySnapshot(), close_failure=primary
    )

    report = commands.run(_status_invocation())

    assert report.exit_code == INTERNAL
    assert report.payload["error"]["type"] == "UndeclaredGrafxError"  # type: ignore[index]
    assert probes == []


def test_clean_close_copies_an_exact_declared_failure_before_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[str] = []

    class ToxicText(str):
        def __format__(self, format_spec: str) -> str:
            probes.append(format_spec)
            raise SystemExit(95)

    failure = GrafxUnsupportedOperation(ToxicText("unsafe text"))
    _install_database(
        monkeypatch, _InventorySnapshot(), close_failure=failure
    )

    report = commands.run(_status_invocation())

    assert report.exit_code != OK
    assert report.payload["error"]["code"] == "unsupported_operation"  # type: ignore[index]
    assert report.payload["error"]["message"] == (  # type: ignore[index]
        "A Grafx failure had no safe text message."
    )
    assert probes == []
