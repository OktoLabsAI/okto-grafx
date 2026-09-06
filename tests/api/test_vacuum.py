"""Public contract for manual, process-quiescent MVCC vacuum v1."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import VectorValue, connect
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxIndexError,
    GrafxSnapshotReclaimed,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.model.catalog import HEAP_RECLAIM_V1_CAPABILITY
from okto_grafx.engine.public_views import VacuumReport
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.errors import GrafxSnapshotReclaimed as PublicSnapshotReclaimed


def prepare_churned_database(root: Path) -> None:
    """Create one indexed row with two obsolete inline versions."""

    with connect(root, page_size=512, partitions_per_table=8) as database:
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
        database.ensure_identity_indexes()
        with database.begin("write") as transaction:
            transaction.execute("CREATE (p:Person {id: 1, name: 'old'})")
        with database.begin("write") as transaction:
            transaction.execute("MATCH (p:Person {id: 1}) SET p.name = 'middle'")
        with database.begin("write") as transaction:
            transaction.execute("MATCH (p:Person {id: 1}) SET p.name = 'current'")


def seed_churn_on_open_database(database: object) -> None:
    """Create the same fixture without closing away resident committed frames."""

    with database.begin("write") as transaction:  # type: ignore[attr-defined]
        transaction.execute(
            "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
        )
    database.ensure_identity_indexes()  # type: ignore[attr-defined]
    for statement in (
        "CREATE (p:Person {id: 1, name: 'old'})",
        "MATCH (p:Person {id: 1}) SET p.name = 'middle'",
        "MATCH (p:Person {id: 1}) SET p.name = 'current'",
    ):
        with database.begin("write") as transaction:  # type: ignore[attr-defined]
            transaction.execute(statement)


def current_name(database: object) -> str:
    """Read the only current Person name through the public query surface."""

    result = database.execute("MATCH (p:Person) RETURN p.name AS name")  # type: ignore[attr-defined]
    assert len(result.rows) == 1
    return str(result.rows[0][0])


def test_public_vacuum_reclaims_history_and_cold_reopens_clean(tmp_path: Path) -> None:
    root = tmp_path / "db"
    prepare_churned_database(root)

    with connect(root, page_size=512, partitions_per_table=8) as database:
        report = database.maintenance.vacuum(confirm_quiescent=True)

        assert isinstance(report, VacuumReport)
        assert report.capability_activated is True
        assert report.wrote is True
        assert report.reclaim_floor_before == 0
        assert report.reclaim_floor_after == report.horizon_lsn
        assert report.reclaimed_versions == 2
        assert report.reclaimed_slot_bytes > 0
        assert report.relinked_versions == 1
        assert report.pages_rewritten >= 1
        assert report.index_entries_removed >= 2
        assert report.complete is True
        assert current_name(database) == "current"
        assert database.verify("all").findings == ()

        second = database.maintenance.vacuum(confirm_quiescent=True)
        assert second.capability_activated is False
        assert second.wrote is False
        assert second.reclaimed_versions == 0
        assert second.index_entries_removed == 0
        assert second.reclaim_floor_after == report.reclaim_floor_after

    with connect(root, page_size=512, partitions_per_table=8) as reopened:
        assert (
            HEAP_RECLAIM_V1_CAPABILITY
            in reopened._catalog.catalog.required_capabilities()
        )
        assert reopened._heap.reclaim_floor() == report.reclaim_floor_after
        assert current_name(reopened) == "current"
        assert reopened.verify("all").findings == ()


def test_vacuum_plans_from_current_commits_without_requiring_a_reopen(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "db", page_size=512, partitions_per_table=8) as database:
        seed_churn_on_open_database(database)

        report = database.maintenance.vacuum(confirm_quiescent=True)

        assert report.reclaimed_versions == 2
        assert current_name(database) == "current"
        assert database.verify("all").findings == ()


def test_vacuum_requires_exact_quiescence_confirmation_and_no_open_transaction(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    prepare_churned_database(root)
    with connect(root, page_size=512, partitions_per_table=8) as database:
        before = database.wal.last_lsn
        with pytest.raises(GrafxUnsupportedOperation) as missing:
            database.maintenance.vacuum()
        assert missing.value.details["field"] == "confirm_quiescent"
        assert database.wal.last_lsn == before

        transaction = database.begin("read")
        with pytest.raises(GrafxTransactionStateError) as active:
            database.maintenance.vacuum(confirm_quiescent=True)
        assert active.value.details["field"] == "open_transactions"
        transaction.rollback()
        assert database.wal.last_lsn == before


def test_vacuum_refuses_read_only_and_invalid_limits_without_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    prepare_churned_database(root)

    with connect(root, page_size=512, partitions_per_table=8) as database:
        before = database.wal.last_lsn
        for invalid in (0, -1, True):
            with pytest.raises(GrafxConfigurationError):
                database.maintenance.vacuum(
                    confirm_quiescent=True,
                    max_versions=invalid,
                )
            assert database.wal.last_lsn == before
        database.checkpoint()

    with connect(
        root,
        page_size=512,
        partitions_per_table=8,
        read_only=True,
    ) as reader:
        before = reader.wal.last_lsn
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            reader.maintenance.vacuum(confirm_quiescent=True)
        assert raised.value.details["field"] == "read_only"
        assert reader.wal.last_lsn == before


def test_vacuum_requires_explicit_catalog_v2_activation(tmp_path: Path) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=512, partitions_per_table=8) as database:
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
        before = database.wal.last_lsn

        with pytest.raises(GrafxUnsupportedOperation) as raised:
            database.maintenance.vacuum(confirm_quiescent=True)

        assert raised.value.details["field"] == "format_version"
        assert raised.value.details["remedy"] == "maintenance.ensure_identity_indexes"
        assert database.wal.last_lsn == before


def test_vacuum_max_versions_drains_history_in_deterministic_passes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    prepare_churned_database(root)
    with connect(root, page_size=512, partitions_per_table=8) as database:
        first = database.maintenance.vacuum(
            confirm_quiescent=True,
            max_versions=1,
        )
        assert first.reclaimed_versions == 1
        assert first.complete is False
        assert current_name(database) == "current"

        second = database.maintenance.vacuum(
            confirm_quiescent=True,
            max_versions=1,
        )
        assert second.reclaimed_versions == 1
        assert second.complete is True
        assert current_name(database) == "current"
        assert database.verify("all").findings == ()


def test_a_snapshot_below_the_durable_floor_is_retryably_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert PublicSnapshotReclaimed is GrafxSnapshotReclaimed
    with connect(tmp_path / "db", page_size=512, partitions_per_table=8) as database:
        database.ensure_identity_indexes()
        database.maintenance.vacuum(confirm_quiescent=True)
        snapshot = database.transactions.published_state().last_committed_lsn
        floor = snapshot + 1
        monkeypatch.setattr(HeapStore, "reclaim_floor", lambda _store: floor)

        with pytest.raises(GrafxSnapshotReclaimed) as raised:
            database.begin("read")

        assert raised.value.retryable is True
        assert raised.value.details == {
            "snapshot_lsn": snapshot,
            "reclaim_floor_lsn": floor,
            "file": "heap.dat",
            "remedy": "reopen",
        }
        assert database.transactions.open_transactions == 0


def test_vacuum_reconciles_vector_tombstones_before_the_next_search(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(
        root,
        page_size=512,
        partitions_per_table=8,
        vector_exact_scan_threshold=4096,
    ) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
            schema.execute(
                "CREATE NODE TABLE V(id INT64, e VECTOR(s), PRIMARY KEY(id))"
            )
        database.ensure_identity_indexes()
        with database.begin("write") as writer:
            writer.execute("CREATE (:V {id: 1, e: [1.0, 0.0]})")
        with database.begin("write") as writer:
            space = database.catalog.catalog.space("s")
            writer.execute(
                "MATCH (v:V {id: 1}) SET v.e = $value",
                {
                    "value": VectorValue(
                        (0.0, 1.0),
                        space.space_id,
                        space.storage_dtype,
                    )
                },
            )

        report = database.maintenance.vacuum(confirm_quiescent=True)

        assert report.reclaimed_versions == 1
        assert report.index_entries_removed >= 1
        with database.begin("read") as reader:
            result = database.search_vectors(
                reader,
                space="s",
                query=(0.0, 1.0),
                k=1,
            )
        assert tuple(hit.record_id for hit in result.hits) == (1,)
        assert database.verify("all").findings == ()

    with connect(
        root,
        page_size=512,
        partitions_per_table=8,
        vector_exact_scan_threshold=4096,
    ) as reopened:
        with reopened.begin("read") as reader:
            result = reopened.search_vectors(
                reader,
                space="s",
                query=(0.0, 1.0),
                k=1,
            )
        assert tuple(hit.record_id for hit in result.hits) == (1,)
        assert reopened.verify("all").findings == ()


def test_failed_rehash_artifact_is_never_reactivated_across_vacuum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry builds a fresh authority after vacuum, never a pre-vacuum shadow."""

    root = tmp_path / "db"
    with connect(root, page_size=512, partitions_per_table=8) as database:
        with database.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Person(id INT64, email STRING, PRIMARY KEY(id))"
            )
        with database.begin("write") as writer:
            writer.execute("CREATE (:Person {id: 1, email: 'old@example.test'})")
        original = database.create_index(
            "by_email",
            "Person",
            ("email",),
            bucket_count=8,
        )
        with database.begin("write") as writer:
            writer.execute(
                "MATCH (p:Person {id: 1}) SET p.email = 'current@example.test'"
            )

        manager_type = type(database._indexes)
        original_build = manager_type._build_detached_exact_generation
        failed: list[tuple[int, str]] = []

        def fail_after_complete_shadow(
            manager: object,
            definition: object,
            through_lsn: int,
        ) -> object:
            original_build(manager, definition, through_lsn)
            failed.append(
                (
                    int(getattr(definition, "artifact_nonce")),
                    str(getattr(definition, "file")),
                )
            )
            raise GrafxIndexError("injected failure after complete rehash shadow")

        monkeypatch.setattr(
            manager_type,
            "_build_detached_exact_generation",
            fail_after_complete_shadow,
        )
        with pytest.raises(GrafxIndexError, match="injected failure"):
            database.rehash_index("by_email", bucket_count=16)
        failed_nonce, failed_file = failed[0]
        assert database._storage.exists(failed_file)

        vacuum = database.maintenance.vacuum(confirm_quiescent=True)
        assert vacuum.reclaimed_versions == 1

        monkeypatch.setattr(
            manager_type,
            "_build_detached_exact_generation",
            original_build,
        )
        grown = database.rehash_index("by_email", bucket_count=16)

        assert grown.active_nonce not in {original.active_nonce, failed_nonce}
        assert grown.file != failed_file
        assert database.execute(
            "MATCH (p:Person) WHERE p.email = 'current@example.test' RETURN p.id"
        ).rows == ((1,),)
        assert database.verify("all").findings == ()
