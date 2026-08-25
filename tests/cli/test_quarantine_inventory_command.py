"""The complete quarantine inventory is observable without changing what it diagnoses."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from okto_grafx.adapters.storage_local import LocalStorageDevice, PENDING_DELETE_MARKER
from okto_grafx.cli import commands
from okto_grafx.cli.exits import (
    DAMAGED,
    FINDINGS,
    INCONCLUSIVE,
    INTERNAL,
    OK,
    REFUSED,
    RETRY,
    USAGE,
)
from okto_grafx.cli.parser import Invocation, parse
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxQuarantineError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.recovery.manifest import QuarantineManifest
from okto_grafx.engine.catalog_store import CATALOG_FILE
from okto_grafx.engine.database import META_FILE
from okto_grafx.engine.heap_store import HEAP_FILE
from okto_grafx.engine.quarantine import (
    QuarantineInventoryItem,
    QuarantineInventoryState,
    QuarantineStore,
)

from tests.cli.conftest import CliRunner


def _files_snapshot(root: str) -> tuple[tuple[str, bytes, int, int], ...]:
    """Capture bytes, size and mtime for every file below one database directory."""
    directory = Path(root)
    captured: list[tuple[str, bytes, int, int]] = []
    for path in sorted(
        candidate for candidate in directory.rglob("*") if candidate.is_file()
    ):
        stat = path.stat()
        captured.append(
            (
                path.relative_to(directory).as_posix(),
                path.read_bytes(),
                stat.st_size,
                stat.st_mtime_ns,
            )
        )
    return tuple(captured)


def test_inventory_forces_read_only_and_preserves_every_database_byte(
    database_path: str,
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_calls: list[tuple[str, dict[str, object]]] = []

    def forbidden_full_connect(path: str, **options: object) -> object:
        connect_calls.append((path, dict(options)))
        raise AssertionError(
            "inventory must not compose a coordinator or database engine"
        )

    monkeypatch.setattr(commands, "connect", forbidden_full_connect)
    before = _files_snapshot(database_path)

    run = cli("quarantine", "inventory", database_path, "--json")

    assert run.code == OK
    assert run.document["conclusive"] is True
    assert run.document["items"] == []
    assert run.document["count"] == 0
    assert connect_calls == []
    assert _files_snapshot(database_path) == before


def test_inventory_close_does_not_reap_pending_delete_evidence(
    database_path: str, cli: CliRunner
) -> None:
    pending = Path(database_path) / f"forensic{PENDING_DELETE_MARKER}17"
    pending.write_bytes(b"leave lifecycle evidence untouched")
    before = _files_snapshot(database_path)

    run = cli("quarantine", "inventory", database_path, "--json")

    assert run.code == OK
    assert _files_snapshot(database_path) == before


def test_detached_findings_win_when_the_real_storage_close_refuses(
    database_path: str,
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan = Path(database_path) / "quarantine" / "orphan" / "evidence.bin"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"preserved before close")
    before = _files_snapshot(database_path)
    close_calls: list[int] = []
    original = LocalStorageDevice.close_read_only

    def close_then_refuse(storage: LocalStorageDevice) -> None:
        close_calls.append(1)
        original(storage)
        raise GrafxUnsupportedOperation("secondary close refusal")

    monkeypatch.setattr(LocalStorageDevice, "close_read_only", close_then_refuse)

    run = cli("quarantine", "inventory", database_path, "--json")

    assert run.code == FINDINGS
    assert run.document["result"] == "findings"
    assert run.document["items"][0]["state"] == "incomplete"
    assert run.document["close_problem"] == "secondary close refusal"
    assert "error" not in run.document
    assert run.err == ""
    text_run = cli("quarantine", "inventory", database_path)
    assert text_run.code == FINDINGS
    assert "refused" not in text_run.err
    assert "could not close" in text_run.err
    assert close_calls == [1, 1, 1, 1]
    assert _files_snapshot(database_path) == before


def test_inventory_refuses_a_foreign_meta_instead_of_certifying_empty(
    tmp_path: Path, cli: CliRunner
) -> None:
    root = tmp_path / "foreign"
    root.mkdir()
    foreign = (b"SQLite format 3\x00foreign identity" * 512)[:8192]
    assert len(foreign) == 8192
    (root / META_FILE).write_bytes(foreign)
    before = _files_snapshot(str(root))

    run = cli("quarantine", "inventory", str(root), "--json")

    assert run.code == DAMAGED
    assert run.document["result"] == "damaged"
    assert "conclusive" not in run.document
    assert _files_snapshot(str(root)) == before


def test_inventory_refuses_a_valid_meta_without_the_rest_of_its_database(
    database_path: str, tmp_path: Path, cli: CliRunner
) -> None:
    root = tmp_path / "copied-identity-only"
    root.mkdir()
    (root / META_FILE).write_bytes((Path(database_path) / META_FILE).read_bytes())
    before = _files_snapshot(str(root))

    run = cli("quarantine", "inventory", str(root), "--json")

    assert run.code == DAMAGED
    assert run.document["result"] == "damaged"
    assert run.document["error"]["details"]["state"] == "final_missing"
    assert _files_snapshot(str(root)) == before


def test_inventory_refuses_aligned_foreign_final_store_pages(
    database_path: str, tmp_path: Path, cli: CliRunner
) -> None:
    root = tmp_path / "foreign-finals"
    root.mkdir()
    meta = (Path(database_path) / META_FILE).read_bytes()
    (root / META_FILE).write_bytes(meta)
    (root / CATALOG_FILE).write_bytes(bytes(len(meta)))
    (root / HEAP_FILE).write_bytes(bytes(len(meta)))
    before = _files_snapshot(str(root))

    run = cli("quarantine", "inventory", str(root), "--json")

    assert run.code == DAMAGED
    assert run.document["result"] == "damaged"
    assert run.document["error"]["details"]["file"] == CATALOG_FILE
    assert _files_snapshot(str(root)) == before


def test_inventory_refuses_options_that_disagree_with_the_stored_identity(
    database_path: str, cli: CliRunner
) -> None:
    before = _files_snapshot(database_path)

    run = cli(
        "quarantine",
        "inventory",
        database_path,
        "--page-size",
        "4096",
        "--json",
    )

    assert run.code == REFUSED
    assert run.document["error"]["code"] == "schema_version_mismatch"
    assert _files_snapshot(database_path) == before


def test_inventory_keeps_partitions_per_table_as_reopen_calibration(
    database_path: str, cli: CliRunner
) -> None:
    before = _files_snapshot(database_path)

    run = cli(
        "quarantine",
        "inventory",
        database_path,
        "--partitions-per-table",
        "32",
        "--json",
    )

    assert run.code == OK
    assert run.document["conclusive"] is True
    assert run.document["items"] == []
    assert _files_snapshot(database_path) == before


def test_inventory_refuses_a_missing_final_beside_first_open_completion(
    database_path: str, cli: CliRunner
) -> None:
    (Path(database_path) / CATALOG_FILE).unlink()
    before = _files_snapshot(database_path)

    run = cli("quarantine", "inventory", database_path, "--json")

    assert run.code == DAMAGED
    assert run.document["error"]["details"]["state"] == "final_missing"
    assert run.document["error"]["details"]["file"] == CATALOG_FILE
    assert _files_snapshot(database_path) == before


def test_inventory_reader_exposes_no_writable_storage_capability(
    database_path: str,
) -> None:
    before = _files_snapshot(database_path)
    reader = commands._open_quarantine_inventory(_inventory_invocation(database_path))
    try:
        assert not hasattr(reader, "_storage")
        assert not hasattr(reader, "_QuarantineInventoryReader__closer")
        close_outcome = getattr(
            reader, "_QuarantineInventoryReader__close_outcome", None
        )
        assert close_outcome is None
        assert not hasattr(reader, "create")
        assert not hasattr(reader, "append_log")
        assert not hasattr(reader, "write_page")
        assert not hasattr(reader.quarantine, "capture")
        assert not hasattr(reader.quarantine, "restore")
        assert reader.quarantine.inventory() == ()
    finally:
        reader.close()
    assert _files_snapshot(database_path) == before


def test_inventory_reader_detaches_a_close_failure_from_storage_and_traceback(
    database_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_calls: list[int] = []
    original = LocalStorageDevice.close_read_only

    def close_then_refuse(storage: LocalStorageDevice) -> None:
        close_calls.append(1)
        original(storage)
        raise GrafxUnsupportedOperation("detached close refusal")

    monkeypatch.setattr(LocalStorageDevice, "close_read_only", close_then_refuse)

    reader = commands._open_quarantine_inventory(_inventory_invocation(database_path))
    outcome = getattr(reader, "_QuarantineInventoryReader__close_outcome")

    assert type(outcome) is tuple
    assert all(type(value) in {str, bool} for value in outcome)
    assert not any(isinstance(value, BaseException) for value in outcome)
    assert close_calls == [1, 1]
    with pytest.raises(GrafxError) as raised:
        reader.close()
    assert raised.value.message == "detached close refusal"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    traceback = raised.value.__traceback__
    while traceback is not None:
        assert not any(
            isinstance(value, LocalStorageDevice)
            for value in traceback.tb_frame.f_locals.values()
        )
        traceback = traceback.tb_next


def test_detached_corruption_close_keeps_damage_precedence(
    database_path: str,
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = LocalStorageDevice.close_read_only

    def close_then_report_damage(storage: LocalStorageDevice) -> None:
        original(storage)
        raise GrafxCorruptionDetected("damage discovered while closing")

    monkeypatch.setattr(
        LocalStorageDevice, "close_read_only", close_then_report_damage
    )

    run = cli("quarantine", "inventory", database_path, "--json")

    assert run.code == DAMAGED
    assert run.document["result"] == "damaged"
    assert run.document["error"]["type"] == "GrafxCorruptionDetected"
    assert run.document["error"]["code"] == "corruption_detected"


def test_detaching_a_foreign_close_failure_never_dispatches_its_renderer(
    database_path: str,
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden_write = Path(database_path) / "render-side-effect.bin"
    original = LocalStorageDevice.close_read_only

    class SideEffect(Exception):
        def __str__(self) -> str:
            hidden_write.write_bytes(b"renderer obtained a write capability")
            return "hostile close failure"

    def close_then_raise_hostile(storage: LocalStorageDevice) -> None:
        original(storage)
        raise SideEffect()

    monkeypatch.setattr(
        LocalStorageDevice, "close_read_only", close_then_raise_hostile
    )

    run = cli("quarantine", "inventory", database_path, "--json")

    assert run.code == INTERNAL
    assert not hidden_write.exists()
    assert "hostile close failure" not in run.out


def test_malformed_declared_close_is_detached_and_cleanup_is_retried(
    database_path: str,
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = GrafxUnsupportedOperation("message removed after construction")
    del broken.message
    close_calls: list[int] = []
    original = LocalStorageDevice.close_read_only

    def refuse_once_then_close(storage: LocalStorageDevice) -> None:
        close_calls.append(1)
        if len(close_calls) == 1:
            raise broken
        original(storage)

    monkeypatch.setattr(LocalStorageDevice, "close_read_only", refuse_once_then_close)

    run = cli("quarantine", "inventory", database_path, "--json")

    assert run.code == REFUSED
    assert run.document["error"]["type"] == "GrafxUnsupportedOperation"
    assert run.document["error"]["message"] == (
        "A Grafx close failure had no safe text message."
    )
    assert close_calls == [1, 1]


def test_inventory_reports_incomplete_physical_evidence_without_repairing_it(
    database_path: str, cli: CliRunner
) -> None:
    orphan = Path(database_path) / "quarantine" / "orphan" / "unexpected.bin"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"preserve this incomplete evidence")
    before = _files_snapshot(database_path)

    run = cli("quarantine", "inventory", database_path, "--json")

    assert run.code == FINDINGS
    document = run.document
    assert document["conclusive"] is True
    assert document["count"] == 1
    assert document["items"] == [
        {
            "name": "orphan",
            "state": "incomplete",
            "files": ["quarantine/orphan/unexpected.bin"],
            "manifest_file": "quarantine/orphan/manifest.json",
            "payload_file": None,
            "manifest": None,
            "detail": "The entry directory contains evidence but no canonical manifest.",
        }
    ]
    assert _files_snapshot(database_path) == before


def test_inconclusive_inventory_uses_exit_six_and_an_error_envelope(
    database_path: str,
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = GrafxQuarantineError(
        "The namespace listing could not certify what exists.",
        directory="quarantine",
        conclusive=False,
        inconclusive=True,
    )

    def refuse(_store: QuarantineStore) -> tuple[QuarantineInventoryItem, ...]:
        raise primary

    monkeypatch.setattr(QuarantineStore, "inventory", refuse)
    before = _files_snapshot(database_path)

    run = cli("quarantine", "inventory", database_path, "--json")

    assert run.code == INCONCLUSIVE
    assert run.document["result"] == "inconclusive"
    assert run.document["error"] == {
        "type": "GrafxQuarantineError",
        "code": "quarantine_error",
        "message": primary.message,
        "retryable": False,
        "inconclusive": True,
        "details": {
            "directory": "quarantine",
            "conclusive": False,
            "inconclusive": True,
        },
    }
    assert _files_snapshot(database_path) == before


def test_inventory_rejects_create_before_connect_is_reached(
    database_path: str,
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden_connect(path: str, **_options: object) -> object:
        calls.append(path)
        raise AssertionError("parsing must reject --create before connect")

    monkeypatch.setattr(commands, "connect", forbidden_connect)

    run = cli("quarantine", "inventory", database_path, "--create")

    assert run.code == USAGE
    assert "always read-only" in run.err
    assert calls == []


def _inventory_invocation(path: str = "fake-database") -> Invocation:
    outcome = parse(["quarantine", "inventory", path])
    assert outcome.invocation is not None
    return outcome.invocation


class _FakeDatabase:
    def __init__(
        self, quarantine: object, *, close_failure: BaseException | None = None
    ) -> None:
        self.path = "fake-database"
        self.quarantine = quarantine
        self.close_calls = 0
        self.close_failure = close_failure

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failure is not None:
            raise self.close_failure


class _MissingQuarantineDatabase:
    path = "fake-database"

    def __init__(self) -> None:
        self.close_calls = 0

    @property
    def quarantine(self) -> object:
        raise GrafxUnsupportedOperation("Quarantine was not composed.")

    def close(self) -> None:
        self.close_calls += 1


def test_inventory_without_a_composed_component_is_refused_with_exit_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _MissingQuarantineDatabase()
    monkeypatch.setattr(
        commands, "_open_quarantine_inventory", lambda _invocation: database
    )

    report = commands.run(_inventory_invocation())

    assert report.exit_code == REFUSED
    assert report.payload["error"]["code"] == "unsupported_operation"  # type: ignore[index]
    assert database.close_calls == 1


def _complete_item() -> QuarantineInventoryItem:
    name = "00000000000000000003-wal_000000000001.wal-1-2"
    directory = f"quarantine/{name}"
    payload_file = f"{directory}/000000000001.wal"
    manifest = QuarantineManifest(
        origin="wal/000000000001.wal",
        offset=1,
        length=2,
        reason="checksum_failure",
        detail="bad checksum",
        captured_at_wall=3.5,
        digest="0" * 64,
        payload_file=payload_file,
        entry_name=name,
        expected_lsn=4,
    )
    return QuarantineInventoryItem(
        name=name,
        state="complete",
        files=(f"{directory}/manifest.json", payload_file),
        manifest_file=f"{directory}/manifest.json",
        payload_file=payload_file,
        manifest=manifest,
    )


def test_inventory_serializes_every_state_from_one_snapshot_without_calling_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states: tuple[QuarantineInventoryState, ...] = (
        "complete",
        "incomplete",
        "corrupt_manifest",
        "unreadable_manifest",
        "missing_payload",
        "manifest_mismatch",
        "unexpected_layout",
    )
    items = (
        _complete_item(),
        *(
            QuarantineInventoryItem(
                name=f"item-{state}",
                state=state,
                files=(),
                detail=f"detail for {state}",
            )
            for state in states[1:]
        ),
    )
    inventory_calls: list[int] = []

    class Store:
        directory = "quarantine"

        def inventory(self) -> tuple[QuarantineInventoryItem, ...]:
            inventory_calls.append(1)
            return items

        def list(self) -> Any:
            raise AssertionError("inventory must not call the legacy list")

    database = _FakeDatabase(Store())
    monkeypatch.setattr(
        commands, "_open_quarantine_inventory", lambda _invocation: database
    )

    report = commands.run(_inventory_invocation())

    assert report.exit_code == FINDINGS
    assert report.payload["conclusive"] is True
    serialized = report.payload["items"]
    assert [item["state"] for item in serialized] == list(states)  # type: ignore[index]
    complete = serialized[0]  # type: ignore[index]
    assert complete["files"] == list(items[0].files)
    assert complete["manifest"]["entry_name"] == items[0].name
    assert complete["manifest"]["schema"] == items[0].manifest.schema
    assert inventory_calls == [1]
    assert database.close_calls == 1


@pytest.mark.parametrize(
    ("primary", "expected"),
    (
        (GrafxCorruptionDetected("damaged evidence"), DAMAGED),
        (
            GrafxQuarantineError(
                "temporarily inconclusive",
                retryable=True,
                inconclusive=True,
            ),
            RETRY,
        ),
        (
            GrafxQuarantineError("inconclusive", inconclusive=True),
            INCONCLUSIVE,
        ),
        (GrafxUnsupportedOperation("refused"), REFUSED),
    ),
)
def test_inventory_primary_failure_wins_over_every_close_refusal(
    monkeypatch: pytest.MonkeyPatch,
    primary: GrafxError,
    expected: int,
) -> None:
    class Store:
        def inventory(self) -> tuple[()]:
            raise primary

    secondary = GrafxUnsupportedOperation("secondary close refusal")
    database = _FakeDatabase(Store(), close_failure=secondary)
    monkeypatch.setattr(
        commands, "_open_quarantine_inventory", lambda _invocation: database
    )

    report = commands.run(_inventory_invocation())

    assert report.exit_code == expected
    assert report.payload["error"]["code"] == primary.code  # type: ignore[index]
    assert report.payload["close_problem"]
    assert database.close_calls == 1


def test_inventory_process_control_primary_keeps_its_identity_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt()

    class Store:
        def inventory(self) -> tuple[()]:
            raise primary

    database = _FakeDatabase(
        Store(), close_failure=GrafxUnsupportedOperation("secondary close refusal")
    )
    monkeypatch.setattr(
        commands, "_open_quarantine_inventory", lambda _invocation: database
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        commands.run(_inventory_invocation())

    assert raised.value is primary
    assert database.close_calls == 1


def test_inventory_primary_survives_an_unprintable_process_control_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = SystemExit(17)

    class ToxicArgument:
        def __str__(self) -> str:
            raise KeyboardInterrupt()

    class Store:
        def inventory(self) -> tuple[()]:
            raise primary

    database = _FakeDatabase(Store(), close_failure=SystemExit(ToxicArgument()))
    monkeypatch.setattr(
        commands, "_open_quarantine_inventory", lambda _invocation: database
    )

    with pytest.raises(SystemExit) as raised:
        commands.run(_inventory_invocation())

    assert raised.value is primary
    assert database.close_calls == 1


def test_inventory_findings_win_over_a_close_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = QuarantineInventoryItem(
        name="orphan",
        state="incomplete",
        files=("quarantine/orphan/evidence.bin",),
        detail="preserved evidence",
    )

    class Store:
        directory = "quarantine"

        def inventory(self) -> tuple[QuarantineInventoryItem, ...]:
            return (item,)

    database = _FakeDatabase(
        Store(), close_failure=GrafxUnsupportedOperation("secondary close refusal")
    )
    monkeypatch.setattr(
        commands, "_open_quarantine_inventory", lambda _invocation: database
    )

    report = commands.run(_inventory_invocation())

    assert report.exit_code == FINDINGS
    assert report.payload["items"][0]["state"] == "incomplete"  # type: ignore[index]
    assert report.payload["close_problem"] == "secondary close refusal"
    assert "error" not in report.payload
    assert all("refused" not in problem for problem in report.problems)
    assert any("could not close" in problem for problem in report.problems)
    assert database.close_calls == 1


def test_public_capture_ignores_a_tuple_subclass_process_control_override(
    database_path: str,
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HostileTuple(tuple[QuarantineInventoryItem, ...]):
        def __iter__(self) -> Any:
            raise SystemExit(23)

    def inventory(_store: QuarantineStore) -> tuple[QuarantineInventoryItem, ...]:
        return HostileTuple()

    monkeypatch.setattr(QuarantineStore, "inventory", inventory)

    run = cli("quarantine", "inventory", database_path, "--json")

    assert run.code == OK
    assert run.document["conclusive"] is True
    assert run.document["items"] == []


def test_public_capture_copies_an_item_without_dispatching_its_subclass(
    database_path: str,
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HostileItem(QuarantineInventoryItem):
        def __getattribute__(self, name: str) -> object:
            if name in {
                "name",
                "state",
                "files",
                "manifest_file",
                "payload_file",
                "manifest",
                "detail",
            }:
                raise KeyboardInterrupt()
            return object.__getattribute__(self, name)

    hostile = HostileItem(
        name="orphan",
        state="incomplete",
        files=("quarantine/orphan/evidence.bin",),
        detail="preserved evidence",
    )

    def inventory(_store: QuarantineStore) -> tuple[QuarantineInventoryItem, ...]:
        return (hostile,)

    monkeypatch.setattr(QuarantineStore, "inventory", inventory)

    run = cli("quarantine", "inventory", database_path, "--json")

    assert run.code == FINDINGS
    assert run.document["items"][0] == {
        "name": "orphan",
        "state": "incomplete",
        "files": ["quarantine/orphan/evidence.bin"],
        "manifest_file": None,
        "payload_file": None,
        "manifest": None,
        "detail": "preserved evidence",
    }
