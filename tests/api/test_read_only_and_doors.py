"""What a read-only handle may do, and what may leave a public door (C11).

Two properties a blind critic broke in round 1, and the tests that would have stopped it.

*Read-only is a promise about BYTES, not about transactions.* The open sequence declines to run
recovery on a read-only database because recovery quarantines, truncates and replays -- and it
holds no writer lease while doing it. Every other door that can write owes the same refusal, and
the round-1 defect was that ``recover()`` and ``flush()`` did not.

*Only Grafx types leave a public door* (section 2, DoD item 5). A path is caller data, and the
file system hands back names that are not valid UTF-8 -- so the two escapes here are reached by an
ordinary ``os.listdir`` round trip, not by a hostile input.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.api.assembly import database_label
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxError,
    GrafxSchemaVersionMismatch,
    GrafxUnsupportedOperation,
)
from okto_grafx.engine.database import META_FILE

WRITING_DOORS = ("begin a write transaction", "run recovery", "flush pages")


def _tree(root: Path) -> dict[str, int]:
    """Return every DATA file under the root with its size, so a change of any kind is visible.

    The control plane is deliberately excluded, and the distinction is the point rather than a
    convenience. ``control/`` holds the writer lease, the reader registrations and the advisory
    lock files, and a READER is required to write there: AC-8 keeps a long-lived reader's log
    segments alive by having it publish the snapshot LSN it holds, so a read-only handle that
    registered nothing would have its segments recycled underneath it. What read-only promises is
    about the DATABASE -- the heap, the catalog, the indexes, the log, the ledger and quarantine
    -- which is also the exact set the round-1 defect destroyed.
    """
    return {
        str(path.relative_to(root)).replace("\\", "/"): path.stat().st_size
        for path in sorted(root.rglob("*"))
        if path.is_file() and not str(path.relative_to(root)).replace("\\", "/").startswith("control/")
    }


# --- read-only ---------------------------------------------------------------------------------


def test_a_read_only_database_refuses_every_door_that_can_write(tmp_path: Path) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    before = _tree(root)

    refused: list[str] = []
    with connect(root, page_size=512, read_only=True) as read_only:
        for call in (
            lambda: read_only.begin("write"),
            read_only.recover,
            read_only.flush,
        ):
            with pytest.raises(GrafxUnsupportedOperation) as raised:
                call()
            assert raised.value.details["field"] == "read_only"
            refused.append(str(raised.value.details["operation"]))
    assert refused == list(WRITING_DOORS)
    # The point of the refusal, asserted as bytes rather than as an exception type. The round-1
    # defect deleted a WAL segment, created a second one, created ledger/ledger.log and created a
    # quarantine directory -- from a handle the caller had asked to be read-only, while holding
    # no writer lease.
    assert _tree(root) == before
    assert any(name.startswith("wal/") for name in before)
    assert not any(name.startswith(("ledger/", "quarantine/")) for name in _tree(root))


def test_a_read_only_open_never_runs_recovery_and_says_so(tmp_path: Path) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=512):
        pass
    with connect(root, page_size=512, read_only=True) as read_only:
        assert read_only.recovery_report is None


def test_a_read_only_database_reads_the_schema_that_is_on_the_device(tmp_path: Path) -> None:
    """The read path's entire purpose, and nothing asserted it.

    A read-only open that skipped loading the catalog returned a database with ZERO tables and
    every existing test still passed, because they all asked what a read-only handle REFUSES and
    none asked what it ANSWERS.
    """
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
            txn.execute("CREATE NODE TABLE Doc(id INT64, title STRING, PRIMARY KEY(id))")
        expected = sorted(table.name for table in db.catalog.catalog.tables())
    assert expected == ["Doc", "Person"]

    with connect(root, page_size=512, read_only=True) as read_only:
        assert sorted(t.name for t in read_only.catalog.catalog.tables()) == expected
        result = read_only.execute("MATCH (p:Person) RETURN p.name")
        assert result.columns == ("p.name",)
        assert read_only.verify("all").findings == ()


def test_a_read_only_open_of_a_database_that_does_not_exist_creates_nothing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "absent"
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        connect(root, read_only=True)
    assert raised.value.details["field"] == "read_only"
    assert not (root / META_FILE).exists()


# --- only Grafx types leave a public door --------------------------------------------------------


def test_a_path_the_file_system_can_produce_but_utf_8_cannot_encode_opens(
    tmp_path: Path,
) -> None:
    """An unpaired surrogate is what ``os.fsdecode`` yields for a name UTF-8 cannot represent.

    On POSIX that is any non-UTF-8 filename, through ``surrogateescape``; on Windows it is an
    unpaired UTF-16 unit, through ``surrogatepass``. Either way it arrives from ``os.listdir`` or
    ``sys.argv``, so a caller reaches it by doing nothing unusual -- and ``str.encode("utf-8")``
    raises a bare ``UnicodeEncodeError`` for it.
    """
    root = tmp_path / "mydb-\udcff"
    with connect(root) as db:
        assert db.label.startswith("db")
        assert db.identity.page_size == 8192
    # The digest is still a digest: bounded, alphanumeric, and injective over paths.
    assert database_label(str(root)) == database_label(str(root))
    assert database_label(str(root)) != database_label(str(tmp_path / "mydb-other"))
    assert all(character.isalnum() or character in "-_." for character in db.label)


def test_a_path_no_platform_can_open_is_refused_inside_the_taxonomy(tmp_path: Path) -> None:
    # A NUL byte is rejected by the operating system itself, so the adapter meets a bare
    # ValueError from os.makedirs. The registry build converts it exactly as the assembly does.
    with pytest.raises(GrafxConfigurationError) as raised:
        connect(str(tmp_path / "bad\x00name"))
    assert raised.value.details["field"] == "ports"
    assert raised.value.details["cause"] == "ValueError"
    assert isinstance(raised.value.__cause__, ValueError)


@pytest.mark.parametrize(
    "path",
    ["mydb-\udcff", "bad\x00name", ""],
    ids=["surrogate", "nul", "empty"],
)
def test_no_path_makes_connect_raise_something_outside_the_taxonomy(
    tmp_path: Path, path: str
) -> None:
    target = str(tmp_path / path) if path else path
    try:
        database = connect(target)
    except GrafxError:
        return
    database.close()


# --- the identity file is exactly one page (named by _require_page_size_of_record) ---------------


def test_the_identity_file_is_exactly_one_page(tmp_path: Path) -> None:
    """The fact the page-size guard rests on, pinned rather than assumed (A85).

    ``_require_page_size_of_record`` compares the LENGTH of the identity file against the
    configured page size and calls a difference a schema mismatch. That is exact only while this
    file is exactly one page; if a later version chains a second page onto it, the guard must be
    rewritten rather than quietly become a length heuristic again.
    """
    for page_size in (512, 1024, 8192):
        root = tmp_path / f"db{page_size}"
        with connect(root, page_size=page_size) as db:
            assert db.storage.page_count(META_FILE) == 1
            assert db.storage.file_size(META_FILE) == page_size


@pytest.mark.parametrize("created", [512, 1024, 8192])
def test_reopening_at_any_other_page_size_is_a_schema_mismatch(
    tmp_path: Path, created: int
) -> None:
    """The whole domain, not the half a modulus covers.

    The first version of this guard compared ``size % page_size``, which passes 8192 reopened at
    4096 -- it divides evenly -- and the open then reported ``corruption_detected`` from the first
    page read, which is precisely the misclassification the guard exists to prevent. FR-8 and
    FR-10 route corruption to truncation, quarantine and forensic ledger entries, so a mistyped
    page size would manufacture an integrity incident on an intact database.
    """
    root = tmp_path / "db"
    with connect(root, page_size=created):
        pass
    for opened in (512, 1024, 2048, 4096, 8192, 16384):
        if opened == created:
            continue
        with pytest.raises(GrafxSchemaVersionMismatch) as raised:
            connect(root, page_size=opened)
        assert raised.value.details["stored"] == created
        assert raised.value.details["value"] == opened
        assert raised.value.details["file"] == META_FILE


# --- what an autocommit read leaves behind --------------------------------------------------------


def test_an_autocommit_read_leaves_no_transaction_open(tmp_path: Path) -> None:
    # Database.execute opens a transaction of its own. A door that opens one per call and keeps
    # it holds the recycling horizon down for the life of the database, and the caller is holding
    # nothing it could withdraw it with.
    with connect(tmp_path / "db", page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
        assert db.transactions.open_transactions == 0
        for _ in range(5):
            db.execute("MATCH (p:Person) RETURN p.name")
        assert db.transactions.open_transactions == 0
        assert db.coordinator.reader_horizon() is None


def test_an_autocommit_read_that_fails_leaves_no_transaction_open(tmp_path: Path) -> None:
    # The other arm. A failure is exactly when a caller cannot clean up after the door, because
    # it never received anything to clean up with.
    with connect(tmp_path / "db", page_size=512) as db:
        for _ in range(5):
            with pytest.raises(GrafxError):
                db.execute("THIS IS NOT A STATEMENT")
        assert db.transactions.open_transactions == 0
        assert db.coordinator.reader_horizon() is None


def test_an_interrupt_during_the_open_still_releases_the_device(tmp_path: Path) -> None:
    """KeyboardInterrupt is not a failure of the composition, and still may not leak a device.

    The guard has three arms -- Grafx, foreign, and everything else -- and the third exists only
    for this case: a BaseException passes through unconverted, but what was opened is released on
    the way past. Nothing else in the suite enters that arm.
    """
    from okto_grafx.adapters.storage_local import LocalStorageDevice

    opened: list[object] = []
    original_init = LocalStorageDevice.__init__
    original_exists = LocalStorageDevice.exists

    def recording(self: object, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]
        opened.append(self)

    def interrupt(self: object, file: str) -> bool:
        if opened and self is opened[0] and file == META_FILE:
            raise KeyboardInterrupt("the operator pressed control-C")
        return original_exists(self, file)  # type: ignore[arg-type]

    LocalStorageDevice.__init__ = recording  # type: ignore[method-assign]
    LocalStorageDevice.exists = interrupt  # type: ignore[method-assign]
    try:
        with pytest.raises(KeyboardInterrupt):
            connect(tmp_path / "db")
    finally:
        LocalStorageDevice.__init__ = original_init  # type: ignore[method-assign]
        LocalStorageDevice.exists = original_exists  # type: ignore[method-assign]
    assert len(opened) == 1
    with pytest.raises(GrafxUnsupportedOperation) as refusal:
        opened[0].exists("probe")  # type: ignore[attr-defined]
    assert refusal.value.details.get("reason") == "device_closed"


# --- the metric catalogue really is declared -------------------------------------------------------


def test_the_open_declares_the_whole_frozen_metric_catalogue(tmp_path: Path) -> None:
    """L12: every suite below this one uses a no-op sink, so nothing noticed the declaration.

    Registration is the authority (G7, A51): a recording sink refuses an emission under a name it
    was never given, which is how a coordinator that emits ``oktografx_lease_wait_seconds``
    without registering it broke every write commit under a real sink. The composition root
    declares the whole catalogue, and this is the test that can tell whether it did.
    """
    from okto_grafx.adapters.metrics_openmetrics import OpenMetricsSink
    from okto_grafx.engine.metrics_catalog import metric_names
    from okto_grafx.runtime.bootstrap import build_default_registry, release_ports
    from okto_grafx.runtime.config import DatabaseConfig

    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db"), page_size=512))
    sink = OpenMetricsSink()
    registry.bind("metrics", sink)
    declared_before = set(sink.snapshot())
    assert declared_before == set()
    try:
        database = connect(tmp_path / "db", page_size=512, registry=registry)
        try:
            declared = set(sink.snapshot())
        finally:
            database.close()
    finally:
        release_ports(registry)
    # Every name in the frozen catalogue of CONTRACT.md section 9, not merely the ones the
    # components that happened to run declared for themselves.
    assert metric_names() <= declared
    assert "oktografx_lease_wait_seconds" in declared


def test_a_write_commit_succeeds_under_a_recording_sink(tmp_path: Path) -> None:
    # The consequence L12 is about, seen from the public surface: without the declaration the
    # first write commit of any database configured for OpenMetrics or JSON refuses.
    with connect(tmp_path / "db", page_size=512, metrics="openmetrics") as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
        assert txn.report is not None
        assert txn.report.durable is True
        assert db.metrics.enabled is True


def test_the_json_sink_also_survives_a_write_commit(tmp_path: Path) -> None:
    destination = tmp_path / "metrics.jsonl"
    with connect(
        tmp_path / "db",
        page_size=512,
        metrics="json",
        metrics_destination=str(destination),
    ) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
        assert txn.report is not None and txn.report.durable is True
        db.metrics.publish()
    assert destination.exists()
    assert os.path.getsize(destination) > 0


def test_a_read_only_reopen_attaches_the_vector_index_without_writing(tmp_path: Path) -> None:
    """Attaching must adopt the durable index, not repair it, on a handle that may not write.

    ``IndexStore.create`` opens an existing file rather than replacing it (G6), so re-attaching a
    durable index is a read. This is the test that says so in bytes: a read-only reopen registers
    the index and leaves every data file of the database exactly as it found it.
    """
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE VECTOR SPACE minilm_v2 {dimension: 4, metric: 'cosine'}")
            txn.execute(
                "CREATE NODE TABLE Chunk("
                "id INT64, embedding VECTOR(minilm_v2), PRIMARY KEY(id))"
            )
        created = next(
            index.name for index in db.indexes.indexes() if index.name.startswith("vector_")
        )
    before = _tree(root)
    assert any("vector_Chunk_minilm_v2" in name for name in before)

    with connect(root, page_size=512, read_only=True) as read_only:
        assert read_only.attached_indexes == ("pk_Chunk", created)
        assert read_only.vectors.index("minilm_v2").name == created
        assert read_only.verify("all").findings == ()
    assert _tree(root) == before
