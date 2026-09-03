"""Focused contract tests for the narrow maintenance facade."""

from __future__ import annotations

from pathlib import Path
from typing import get_type_hints

import pytest

from okto_grafx import Database, connect
from okto_grafx.domain.errors import GrafxUnsupportedOperation
from okto_grafx.domain.recovery.report import RecoveryReport
from okto_grafx.domain.verify.findings import VerificationReport
from okto_grafx.domain.wal.replay import RecycleReport
from okto_grafx.engine.database import Maintenance
from okto_grafx.engine.public_views import IndexView, MaintenanceStatus, VectorIndexView


def test_maintenance_surface_and_annotations_are_exact() -> None:
    """The facade has only the frozen operations and names their concrete outputs."""
    public_methods = frozenset(
        name
        for name, member in vars(Maintenance).items()
        if not name.startswith("_") and callable(member)
    )
    assert public_methods == frozenset(
        {
            "status",
            "checkpoint",
            "verify",
            "recover",
            "publish_metrics",
            "rebuild_vector_index",
            "ensure_identity_indexes",
            "create_index",
        }
    )

    maintenance_getter = Database.maintenance.fget
    assert maintenance_getter is not None
    assert get_type_hints(maintenance_getter)["return"] is Maintenance
    assert get_type_hints(Maintenance.status)["return"] is MaintenanceStatus
    assert get_type_hints(Maintenance.checkpoint)["return"] is RecycleReport
    assert get_type_hints(Maintenance.verify)["return"] is VerificationReport
    assert get_type_hints(Maintenance.recover)["return"] is RecoveryReport
    assert get_type_hints(Maintenance.publish_metrics)["return"] is type(None)
    assert get_type_hints(Maintenance.rebuild_vector_index)["return"] is VectorIndexView
    assert get_type_hints(Maintenance.ensure_identity_indexes)["return"] is type(None)
    assert get_type_hints(Maintenance.create_index)["return"] is IndexView


def test_status_reports_only_last_observed_available_values() -> None:
    """Status derives lag from one published-state view and is honest about absent estimates."""
    with connect(":memory:") as database:
        transactions = database.transactions
        state = transactions.published_state()
        observed = database.maintenance.status()

        assert observed == MaintenanceStatus(
            wal_bytes=database.wal.total_bytes(),
            checkpoint_lag_lsn=state.last_committed_lsn - state.checkpoint_lsn,
            recovery_required=False,
            stale_indexes=database.stale_indexes,
            heap_bloat_bytes=None,
            oldest_reader_age=None,
        )


def test_status_withholds_lag_while_publication_is_recovery_blocked() -> None:
    """A recovery-required handle must not dress an unavailable publication state as lag zero."""
    with connect(":memory:") as database:
        database._transactions.require_recovery()
        observed = database.maintenance.status()

        assert observed.recovery_required is True
        assert observed.checkpoint_lag_lsn is None


def test_operational_methods_delegate_to_the_existing_database_doors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The facade adds grouping only; it owns no second maintenance policy or mechanism."""
    database = connect(":memory:")
    maintenance = database.maintenance
    calls: list[tuple[str, object]] = []
    checkpoint_result = object()
    verification_result = object()
    recovery_result = object()
    index_result = object()

    def checkpoint(_database: Database) -> object:
        calls.append(("checkpoint", None))
        return checkpoint_result

    def verify(_database: Database, scope: str = "all") -> object:
        calls.append(("verify", scope))
        return verification_result

    def recover(_database: Database) -> object:
        calls.append(("recover", None))
        return recovery_result

    def publish_metrics(_database: Database) -> None:
        calls.append(("publish_metrics", None))

    def ensure_identity_indexes(_database: Database) -> None:
        calls.append(("ensure_identity_indexes", None))

    def create_index(
        _database: Database,
        name: str,
        table: str,
        columns: object,
        *,
        bucket_count: int | None = None,
        expected_cardinality: int | None = None,
    ) -> object:
        calls.append(
            (
                "create_index",
                (name, table, columns, bucket_count, expected_cardinality),
            )
        )
        return index_result

    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(Database, "checkpoint", checkpoint)
            boundary.setattr(Database, "verify", verify)
            boundary.setattr(Database, "recover", recover)
            boundary.setattr(Database, "publish_metrics", publish_metrics)
            boundary.setattr(
                Database, "ensure_identity_indexes", ensure_identity_indexes
            )
            boundary.setattr(Database, "create_index", create_index)

            assert maintenance.checkpoint() is checkpoint_result
            assert maintenance.verify("indexes") is verification_result
            assert maintenance.recover() is recovery_result
            assert maintenance.publish_metrics() is None
            assert maintenance.ensure_identity_indexes() is None
            assert maintenance.create_index(
                "by_name",
                "Person",
                ("name",),
                expected_cardinality=1_000,
            ) is index_result
    finally:
        database.close()

    assert calls == [
        ("checkpoint", None),
        ("verify", "indexes"),
        ("recover", None),
        ("publish_metrics", None),
        ("ensure_identity_indexes", None),
        (
            "create_index",
            ("by_name", "Person", ("name",), None, 1_000),
        ),
    ]


def test_a_retained_maintenance_facade_obeys_database_lifecycle() -> None:
    """Holding the facade cannot keep any operator door usable after database close."""
    database = connect(":memory:")
    maintenance = database.maintenance
    database.close()

    calls = (
        maintenance.status,
        maintenance.checkpoint,
        maintenance.verify,
        maintenance.recover,
        maintenance.publish_metrics,
        maintenance.ensure_identity_indexes,
    )
    for call in calls:
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            call()
        assert "closed" in str(raised.value)

    with pytest.raises(GrafxUnsupportedOperation):
        maintenance.create_index("by_name", "Person", ("name",))

    with pytest.raises(GrafxUnsupportedOperation):
        _ = database.maintenance


def test_read_only_maintenance_refuses_only_its_writing_operations(
    tmp_path: Path,
) -> None:
    """Checkpoint and recovery inherit the database's existing read-only refusal."""
    root = tmp_path / "database"
    with connect(root) as writer:
        writer.checkpoint()

    with connect(root, read_only=True) as reader:
        maintenance = reader.maintenance
        assert maintenance.status().recovery_required is False
        assert maintenance.verify().findings == ()
        assert maintenance.publish_metrics() is None

        for call in (maintenance.checkpoint, maintenance.recover):
            with pytest.raises(GrafxUnsupportedOperation) as raised:
                call()
            assert raised.value.details["field"] == "read_only"
