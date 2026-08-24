"""Startup must prove a read-only view and repair pages before interpreting them."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.domain.errors import GrafxLeaseTimeout, GrafxUnsupportedOperation
from okto_grafx.domain.model import CATALOG_MAGIC
from okto_grafx.domain.txn.commit_state import COMMIT_STATE_FILE
from okto_grafx.engine.coordination import COMMIT_SECTION

PAGE_SIZE = 512
CATALOG_FILE = "catalog.dat"


def _all_bytes(root: Path) -> dict[str, bytes]:
    """Return every file byte in the database tree, including the control plane."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_read_only_refuses_a_wal_commit_hidden_by_restored_old_state_without_writing(
    tmp_path: Path,
) -> None:
    """The WAL, not a possibly restored control hint, proves an unrecovered commit exists."""
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        database.checkpoint()
    old_state = (root / COMMIT_STATE_FILE).read_bytes()

    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )

    # Simulate a restore that rolled back only the small publication record. The durable WAL
    # still carries the complete commit and must override this stale hint for startup safety.
    (root / COMMIT_STATE_FILE).write_bytes(old_state)
    before = _all_bytes(root)

    with pytest.raises(GrafxUnsupportedOperation) as refused:
        connect(root, page_size=PAGE_SIZE, read_only=True)

    assert refused.value.details["field"] == "read_only_consistency"
    assert refused.value.details["wal_committed_lsn"] > refused.value.details["checkpoint_lsn"]
    assert _all_bytes(root) == before


def test_a_clean_normal_reopen_loads_the_existing_catalog_after_recovery(tmp_path: Path) -> None:
    """Deferring interpretation until recovery must not leave an ordinary reopen empty."""
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )

    with connect(root, page_size=PAGE_SIZE) as reopened:
        assert reopened.catalog.catalog.table("Person").primary_key == "id"


def test_read_only_consistency_times_out_behind_commit_without_changing_bytes(
    tmp_path: Path,
) -> None:
    """State plus WAL are sampled under the writer's section, even for a read-only open."""
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as transaction:
            transaction.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
        database.checkpoint()

    holder_storage = LocalStorageDevice(str(root), page_size=PAGE_SIZE)
    holder = LocalProcessCoordinator(
        holder_storage,
        SystemClock(),
        owner_id="read-only-gate-holder",
        lock_directory=str(root / "control"),
        metrics=NoOpMetricsSink(),
    )
    entered = threading.Event()
    release = threading.Event()
    before = _all_bytes(root)

    def hold_commit_section() -> None:
        with holder.exclusive(COMMIT_SECTION, timeout=2.0):
            entered.set()
            assert release.wait(timeout=5.0)

    blocker = threading.Thread(target=hold_commit_section, daemon=True)
    blocker.start()
    assert entered.wait(timeout=2.0)
    try:
        with pytest.raises(GrafxLeaseTimeout) as timed_out:
            connect(
                root,
                page_size=PAGE_SIZE,
                read_only=True,
                commit_lock_timeout_seconds=0.05,
            )
        assert timed_out.value.retryable is True
        assert timed_out.value.details["section"] == COMMIT_SECTION
    finally:
        release.set()
        blocker.join(timeout=5.0)
        holder_storage.close()
    assert not blocker.is_alive()
    assert _all_bytes(root) == before


def test_recovery_replays_catalog_pages_before_deserializing_the_catalog(tmp_path: Path) -> None:
    """A checksum-valid old catalog page may be repaired by a newer committed WAL image."""
    root = tmp_path / "db"
    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
        catalog_root = database.catalog.chain_pages()[0]
        committed_lsn = database.transactions.published_lsn()

    device = LocalStorageDevice(str(root), page_size=PAGE_SIZE)
    codec = PageCodecV1(PAGE_SIZE)
    try:
        page = codec.decode_page(
            device.read_page(CATALOG_FILE, catalog_root),
            verify=True,
            page_index=catalog_root,
        )
        payload = page.read_slot(0)
        assert payload.startswith(CATALOG_MAGIC)
        page.update_slot(0, b"BROKEN!!" + payload[len(CATALOG_MAGIC) :])
        page.page_lsn = 0
        damaged = codec.encode_page(page)
        # Re-encoding proves the outer page checksum is valid; only the catalog payload is bad.
        assert codec.decode_page(damaged, verify=True).read_slot(0).startswith(b"BROKEN!!")
        device.write_page(CATALOG_FILE, catalog_root, damaged)
        device.durable_barrier(CATALOG_FILE)
    finally:
        device.close()

    with connect(root, page_size=PAGE_SIZE) as recovered:
        assert recovered.transactions.published_lsn() == committed_lsn
        assert recovered.catalog.catalog.table("Person").primary_key == "id"
        assert recovered.recovery_report is not None
        assert recovered.recovery_report.records_replayed > 0
