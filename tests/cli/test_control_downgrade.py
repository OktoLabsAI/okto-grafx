"""The offline CE-1 rollback command preserves every logical control payload."""

from __future__ import annotations

from pathlib import Path

from okto_grafx import connect
from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FILE
from okto_grafx.engine.database import META_FILE

from tests.cli.conftest import CliRunner


def test_v1_v2_v1_round_trip_is_byte_identical_for_every_control_record(
    tmp_path: Path, cli: CliRunner
) -> None:
    root = tmp_path / "db"
    with connect(root) as database:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE (:P {id: 1})")
        database.checkpoint()
        identity = database.identity
    assert identity.format_version == 2

    # Read the newest logical bytes through the production decoders before conversion.
    from okto_grafx.adapters.coordination_local import (
        LEASE_FILE_NAME,
        decode_lease_record,
    )
    from okto_grafx.adapters.storage_local import LocalStorageDevice
    from okto_grafx.domain.control_record import (
        ControlRecordKind,
        TwoSlotControlRecordStore,
    )

    lease_name = f"control/{LEASE_FILE_NAME}"
    with LocalStorageDevice(root) as storage:
        before_lease = TwoSlotControlRecordStore(
            storage,
            file=lease_name,
            record_kind=ControlRecordKind.LEASE,
            database_uuid=identity.database_uuid,
            file_nonce=0,
            temporary="control/test-read-lease.tmp",
        ).read()
        before_commit = TwoSlotControlRecordStore(
            storage,
            file=COMMIT_STATE_FILE,
            record_kind=ControlRecordKind.COMMIT_STATE,
            database_uuid=identity.database_uuid,
            file_nonce=0,
            temporary="control/test-read-commit.tmp",
        ).read()
    assert before_lease is not None and not decode_lease_record(
        before_lease.payload, file=lease_name
    ).held
    assert before_commit is not None

    run = cli("control", "downgrade", str(root), "--json")
    assert run.code == 0, run.err
    assert run.document["current_format"] == 1

    assert (root / lease_name).read_bytes() == before_lease.payload
    assert (root / COMMIT_STATE_FILE).read_bytes() == before_commit.payload
    with connect(root, read_only=True) as reader:
        assert reader.identity.format_version == 1
        assert reader.execute("MATCH (p:P) RETURN p.id").rows == ((1,),)
    assert (root / META_FILE).stat().st_size == identity.page_size


def test_control_downgrade_is_idempotent_for_an_already_v1_database(
    tmp_path: Path, cli: CliRunner
) -> None:
    root = tmp_path / "db"
    with connect(root):
        pass
    first = cli("control", "downgrade", str(root), "--json")
    second = cli("control", "downgrade", str(root), "--json")
    assert first.code == second.code == 0
    assert second.document["already_current"] is True
