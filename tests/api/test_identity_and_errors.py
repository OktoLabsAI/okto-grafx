"""Identity at open, and errors that arrive as the class they started as (C11, FR-1, A47).

Two properties nobody below the composition root can check.

*Identity*: SPEC-M1 FR-1 says a database has a UUID and an epoch before it has content, that
reopening finds the same one, and that recovery runs before a transaction is accepted. The record
is the one CONTRACT.md section 6.2 lays out, and it shares page 0 of ``grafx.meta`` with the file
header C1 defines and C6 reads.

*Composition of the taxonomy*: a failure deep in the storage device must reach a caller through
``connect`` and through ``Database`` as the section 2 class the device chose, with
``details["retryable"]`` intact. A47 exists because a retry predicate that switches on the
exception class reports a transient antivirus touch as permanent -- and the composition root is
the last place that could flatten one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.api.assembly import _release, assemble_database, database_label
from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.storage_local import LocalStorageDevice, barrier_failure
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxError,
    GrafxSchemaVersionMismatch,
    GrafxStorageError,
    GrafxUnsupportedOperation,
)
from okto_grafx.engine.database import (
    IDENTITY_FORMAT_VERSION,
    IDENTITY_MAGIC,
    IDENTITY_SLOT,
    META_FILE,
    DatabaseIdentity,
    MetaStore,
)
from okto_grafx.engine.recovery_manager import META_FILE as RECOVERY_META_FILE
from okto_grafx.runtime.bootstrap import build_default_registry, release_ports
from okto_grafx.runtime.config import DatabaseConfig

# --- identity ---------------------------------------------------------------------------------


def test_the_identity_file_is_the_one_recovery_reads() -> None:
    # Two components must agree on this name or recovery reads a file nobody writes.
    assert META_FILE == RECOVERY_META_FILE == "grafx.meta"


def test_a_new_database_gets_an_identity_of_its_own(tmp_path: Path) -> None:
    with connect(tmp_path / "db", partitions_per_table=32) as db:
        identity = db.identity
    assert len(identity.database_uuid) == 16
    assert identity.page_size == 8192
    assert identity.partitions_per_table == 32
    assert identity.granularity_descriptor == "hash-v1;partitions_per_table=32"
    assert identity.format_version == IDENTITY_FORMAT_VERSION
    assert identity.created_at_wall > 0.0


def test_reopening_finds_the_identity_the_database_was_created_with(tmp_path: Path) -> None:
    root = tmp_path / "db"
    with connect(root) as db:
        first = db.identity
    with connect(root) as db:
        second = db.identity
    assert second == first


def test_two_databases_never_share_an_identity(tmp_path: Path) -> None:
    with connect(tmp_path / "a") as first, connect(tmp_path / "b") as second:
        assert first.identity.database_uuid != second.identity.database_uuid


def test_the_identity_record_round_trips_through_its_own_bytes() -> None:
    identity = DatabaseIdentity(
        database_uuid=bytes(range(16)),
        page_size=4096,
        partitions_per_table=64,
        created_at_wall=1234.5,
        granularity_descriptor="hash-v1;partitions_per_table=64",
    )
    assert DatabaseIdentity.decode(identity.encode()) == identity


def test_the_identity_record_starts_with_the_magic_of_the_format() -> None:
    identity = DatabaseIdentity(
        database_uuid=bytes(16),
        page_size=512,
        partitions_per_table=8,
        created_at_wall=1.0,
        granularity_descriptor="hash-v1;partitions_per_table=8",
    )
    assert identity.encode().startswith(IDENTITY_MAGIC)


def test_a_flipped_byte_in_the_identity_record_is_damage(tmp_path: Path) -> None:
    identity = DatabaseIdentity(
        database_uuid=bytes(16),
        page_size=512,
        partitions_per_table=8,
        created_at_wall=1.0,
        granularity_descriptor="hash-v1;partitions_per_table=8",
    )
    damaged = bytearray(identity.encode())
    damaged[20] ^= 0xFF
    with pytest.raises(GrafxCorruptionDetected) as raised:
        DatabaseIdentity.decode(bytes(damaged))
    assert raised.value.details["field"] == "crc32c"


def test_an_identity_written_by_a_newer_format_stops_the_open() -> None:
    identity = DatabaseIdentity(
        database_uuid=bytes(16),
        page_size=512,
        partitions_per_table=8,
        created_at_wall=1.0,
        granularity_descriptor="hash-v1;partitions_per_table=8",
        format_version=IDENTITY_FORMAT_VERSION + 1,
    )
    with pytest.raises(GrafxSchemaVersionMismatch) as raised:
        DatabaseIdentity.decode(identity.encode())
    assert raised.value.details["supported"] == IDENTITY_FORMAT_VERSION


def test_an_identity_page_with_no_identity_record_is_damage(tmp_path: Path) -> None:
    # The header page exists and its file header is intact, but the identity slot is gone. That
    # is damage, not a database being created, and the two must not be confused: a database being
    # created has no page at all.
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db")))
    try:
        db = connect(tmp_path / "db", registry=registry)
        pool = db._pool
        db.close()
        with pool.pinned(META_FILE, 0) as page:
            page.free_slot(IDENTITY_SLOT)
        pool.flush(META_FILE)
        store = MetaStore(pool)
        with pytest.raises(GrafxCorruptionDetected) as raised:
            store.read()
        assert raised.value.details["field"] == "identity_slot"
    finally:
        release_ports(registry)


def test_reopening_at_another_page_size_is_a_schema_mismatch_not_corruption(
    tmp_path: Path,
) -> None:
    # Every paged door divides a file by the page size the device was built with, so this used to
    # surface as corruption_detected -- and FR-8/FR-10 route "corruption" to truncation,
    # quarantine and forensic ledger entries. A mistyped page size must never manufacture an
    # integrity incident on an intact database (A11-revised).
    root = tmp_path / "db"
    with connect(root, page_size=512):
        pass
    for page_size in (1024, 2048, 4096):
        with pytest.raises(GrafxSchemaVersionMismatch) as raised:
            connect(root, page_size=page_size)
        assert raised.value.details["field"] == "page_size"
        assert raised.value.details["file"] == META_FILE


def test_a_budget_that_cannot_hold_the_stores_is_refused_by_the_field_the_caller_wrote(
    tmp_path: Path,
) -> None:
    # C1 refuses the same configuration at store construction, naming its own parameter
    # ``budget_bytes``. Only the composition root can name ``buffer_budget_bytes``, which is the
    # option the caller actually passed, so the detail is what tells the two refusals apart (A62).
    with pytest.raises(GrafxConfigurationError) as raised:
        connect(tmp_path / "db", buffer_budget_bytes=1024, page_size=8192)
    assert raised.value.details["field"] == "buffer_budget_bytes"
    assert raised.value.details["required_bytes"] == 2 * 8192


def test_changing_the_partition_count_needs_no_migration(tmp_path: Path) -> None:
    # FR-4 and TR-4: the granularity is self-describing, so a different partition count is a new
    # descriptor and never a format change. The identity keeps what it was created with.
    root = tmp_path / "db"
    with connect(root, partitions_per_table=8) as db:
        created = db.identity.partitions_per_table
    with connect(root, partitions_per_table=256) as db:
        assert db.identity.partitions_per_table == created
        assert db.transactions.partitions_per_table == 256


def test_recovery_runs_before_a_transaction_can_be_opened(tmp_path: Path) -> None:
    # FR-1: reopening executes the recovery of FR-8 before accepting transactions.
    root = tmp_path / "db"
    with connect(root):
        pass
    with connect(root) as db:
        report = db.recovery_report
        assert report is not None
        assert report.outcome == "clean"


def test_the_refuse_policy_refuses_only_a_damaged_log(tmp_path: Path) -> None:
    root = tmp_path / "db"
    with connect(root, recovery_policy="refuse") as db:
        assert db.recovery_report.outcome == "clean"


# --- error composition (A47) --------------------------------------------------------------------


def test_a_device_failure_reaches_the_caller_with_its_class_and_its_retryable_intact(
    tmp_path: Path,
) -> None:
    # The device refuses to make its root because the name is taken by a file. The class, the
    # retryable flag and the machine-readable view are the device's, and connect() is not
    # allowed to re-dress any of them.
    taken = tmp_path / "taken"
    taken.write_bytes(b"not a directory")
    with pytest.raises(GrafxError) as raised:
        connect(taken)
    failure = raised.value
    assert isinstance(failure, GrafxStorageError)
    assert failure.code == "storage_error"
    assert failure.details["operation"] == "open_root"
    # The device classifies this as permanent -- a path that is a file never becomes a directory
    # -- and the composition root passes the classification through rather than deciding it.
    assert failure.details["reason"] == "permanently_refused"
    assert failure.retryable is False
    assert failure.to_dict()["retryable"] is failure.retryable
    # G1: the product surface is en-US. The operating system's own text is diagnostic and lives
    # in details, never in the message a caller prints.
    assert failure.message.isascii()
    assert "platform_message" in failure.details


@pytest.mark.parametrize(
    "planted",
    [
        GrafxDeviceFull("The device is full.", free_bytes=0),
        GrafxCorruptionDetected("A page failed its checksum.", page=7),
        GrafxStorageError("A sharing violation was exhausted.", winerror=32, attempts=3),
    ],
    ids=["device_full", "corruption", "storage"],
)
def test_a_failure_raised_under_the_assembly_arrives_unchanged(
    tmp_path: Path, planted: GrafxError
) -> None:
    # The assembly runs inside one guard that releases what it opened. A guard that swallowed or
    # re-dressed the failure on its way out would hide both the class a caller switches on and
    # the retryable detail A47 makes every retry predicate read.
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db")))
    device = registry.get("storage")
    original = type(device).page_count

    def refuse(self: object, file: str) -> int:
        raise planted

    type(device).page_count = refuse  # type: ignore[method-assign]
    try:
        with pytest.raises(GrafxError) as raised:
            assemble_database(DatabaseConfig(path=str(tmp_path / "db")), registry)
    finally:
        type(device).page_count = original  # type: ignore[method-assign]
        release_ports(registry)
    assert raised.value is planted
    assert raised.value.details.get("retryable", raised.value.retryable) == planted.retryable


@pytest.mark.parametrize("cleanup_type", [RuntimeError, KeyboardInterrupt])
def test_assembly_unwind_exhausts_closers_without_replacing_the_primary_failure(
    cleanup_type: type[BaseException],
) -> None:
    """Cleanup failures are secondary while an assembly failure is already active."""
    primary = GrafxDeviceFull("The assembly's primary operation failed.", free_bytes=0)
    cleanup_failure = cleanup_type("a closer failed during unwind")
    ran: list[str] = []

    def outer() -> None:
        ran.append("outer")

    def inner() -> None:
        ran.append("inner")
        raise cleanup_failure

    with pytest.raises(GrafxDeviceFull) as raised:
        try:
            raise primary
        except BaseException:
            # Acquisition order is outer then inner; assembly releases in reverse and a bare
            # raise preserves the failure that caused this unwind.
            _release([outer, inner])
            raise

    assert raised.value is primary
    assert ran == ["inner", "outer"]


def test_a_retryable_device_failure_stays_retryable_through_connect(tmp_path: Path) -> None:
    """A47: the retryable classification is what a caller acts on, so it must survive the open.

    This test used to provoke the condition with a path that is a file, which the device then
    classified as retryable. The device now classifies that correctly as permanent, so the
    provoking condition is gone and the test would have quietly become a no-op with a
    reassuring name (A83.1). Re-armed against a condition that still exists: a transient device
    failure planted at the door ``connect`` opens first.
    """
    planted = GrafxStorageError(
        "A sharing violation was exhausted.",
        winerror=32,
        attempts=3,
        reason="access_failed",
    )
    assert planted.retryable is True, "the fixture must be the transient case (A72)"
    device_type = LocalStorageDevice
    original = device_type.exists

    def refuse(self: object, file: str) -> bool:
        raise planted

    device_type.exists = refuse  # type: ignore[method-assign]
    try:
        with pytest.raises(GrafxStorageError) as raised:
            connect(tmp_path / "db")
    finally:
        device_type.exists = original  # type: ignore[method-assign]
    assert raised.value is planted
    assert raised.value.retryable is True
    assert raised.value.to_dict()["retryable"] is True


def test_a_barrier_failure_reaches_a_committing_caller_with_its_detail_intact(
    tmp_path: Path,
) -> None:
    """A28 and A47: a barrier carries its access classification in ``details["retryable"]``.

    This is the exact case A47 was written about, seen from the top for the first time: the
    failure starts in the device, passes the log, the transaction manager and the public
    Transaction, and a caller deciding whether to retry reads the detail rather than the class.
    A composition root that flattened it would tell a caller that a transient antivirus touch is
    permanent.
    """
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db")))
    device = registry.get("storage")
    planted = barrier_failure(
        "A durability barrier could not be served.",
        reason="access_failed",
        retryable=True,
        file="wal/000000000001.wal",
        attempts=3,
    )
    device_type = type(device)
    original = device_type.durable_barrier

    def refuse(self: object, file: str | None = None) -> None:
        # A44: only the device this test built refuses; another test's device is untouched.
        if self is device:
            raise planted
        original(self, file)  # type: ignore[arg-type]

    try:
        db = connect(tmp_path / "db", registry=registry)
        txn = db.begin("write")
        txn._context.owner._stage_page_image(
            txn._context, "heap.dat", 0, db._codec.encode_page(_heap_page(db))
        )
        txn._context.note_write(db.transactions.partition_of(1, b"k"))
        device_type.durable_barrier = refuse  # type: ignore[method-assign]
        try:
            with pytest.raises(GrafxError) as raised:
                txn.commit()
        finally:
            device_type.durable_barrier = original  # type: ignore[method-assign]
        failure = raised.value
        assert isinstance(failure, GrafxDurabilityBarrierFailed)
        assert failure.details["retryable"] is True
        db.close()
    finally:
        release_ports(registry)


def _heap_page(db: object) -> object:
    """Return a valid, empty heap page image for page 0 of the heap file."""
    from okto_grafx.domain.page import Page, PageType

    return Page(
        int(PageType.HEAP),
        page_size=db.codec.page_size,  # type: ignore[attr-defined]
        page_index=0,
        page_lsn=0,
        seq=2,
    )


def test_nothing_that_is_not_a_grafx_error_escapes_the_public_door(tmp_path: Path) -> None:
    # DoD item 5: only Grafx types leave a public door.
    for call in (
        lambda: connect(object()),  # type: ignore[arg-type]
        lambda: connect(":memory:", nonsense=1),  # type: ignore[call-arg]
        lambda: connect(""),
    ):
        with pytest.raises(GrafxError):
            call()


def test_a_registry_missing_a_slot_refuses_the_open() -> None:
    from okto_grafx.domain.errors import GrafxPortNotConfigured
    from okto_grafx.runtime.registry import PortRegistry

    with pytest.raises(GrafxPortNotConfigured) as raised:
        connect(":memory:", registry=PortRegistry())
    assert set(raised.value.details["missing"]) == set(PortRegistry.REQUIRED)


def test_a_device_whose_page_size_disagrees_with_the_configuration_is_refused(
    tmp_path: Path,
) -> None:
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db"), page_size=1024))
    try:
        with pytest.raises(GrafxConfigurationError) as raised:
            assemble_database(
                DatabaseConfig(path=str(tmp_path / "db"), page_size=2048), registry
            )
        assert raised.value.details["device"] == 1024
    finally:
        release_ports(registry)


def test_the_database_label_is_stable_for_one_path() -> None:
    assert database_label("/some/place") == database_label("/some/place")
    assert database_label("/some/place") != database_label("/other/place")


def test_a_component_that_is_absent_is_named_rather_than_guessed(tmp_path: Path) -> None:
    with connect(tmp_path / "db") as db:
        object.__setattr__(db, "_queries", None)
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            db.queries
        assert raised.value.details["component"] == "queries"
        assert "C10" in str(raised.value)


def test_an_adapter_that_does_not_honour_its_port_is_refused_in_the_taxonomy(
    tmp_path: Path,
) -> None:
    """Assembly classifies a bad adapter result while preserving its original cause.

    The registry refuses a wrong SHAPE statically and deliberately runs no adapter code, so an
    adapter that answers with a value its Protocol does not describe can only be met while the
    engines are being built. This open-time conversion is not a universal runtime boundary for
    trusted custom adapters. It names the port layer and chains the original, so the defect stays
    diagnosable rather than hidden behind a friendly message.
    """
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db")))
    codec = registry.get("codec")

    class LyingCodec:
        """A codec whose decode answers with bytes instead of a Page."""

        format_version = 1

        def checksum(self, payload: bytes) -> int:
            return codec.checksum(payload)

        def encode_page(self, page: object) -> bytes:
            return codec.encode_page(page)

        def decode_page(self, raw: bytes, *, verify: bool = True) -> object:
            return raw

        @property
        def page_size(self) -> int:
            return codec.page_size

    registry.bind("codec", LyingCodec())
    try:
        with pytest.raises(GrafxConfigurationError) as raised:
            connect(tmp_path / "db", registry=registry)
        assert raised.value.details["field"] == "ports"
        assert raised.value.details["cause"] == "AttributeError"
        assert isinstance(raised.value.__cause__, AttributeError)
    finally:
        release_ports(registry)


def test_a_grafx_failure_under_the_assembly_is_never_converted(tmp_path: Path) -> None:
    # The other side of the guard above: a Grafx failure keeps its class, so a caller can still
    # switch on it. Without this the conversion would swallow the taxonomy A47 protects.
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db")))
    device = registry.get("storage")
    planted = GrafxDeviceFull("The device is full.", free_bytes=0)
    device_type = type(device)
    original = device_type.allocate

    def refuse(self: object, file: str, count: int = 1) -> int:
        if self is device:
            raise planted
        return original(self, file, count)  # type: ignore[arg-type]

    device_type.allocate = refuse  # type: ignore[method-assign]
    try:
        with pytest.raises(GrafxDeviceFull) as raised:
            connect(tmp_path / "db", registry=registry)
        assert raised.value is planted
    finally:
        device_type.allocate = original  # type: ignore[method-assign]
        release_ports(registry)


def test_an_identity_record_that_does_not_start_with_the_magic_is_damage() -> None:
    identity = DatabaseIdentity(
        database_uuid=bytes(16),
        page_size=512,
        partitions_per_table=8,
        created_at_wall=1.0,
        granularity_descriptor="hash-v1;partitions_per_table=8",
    )
    foreign = b"NOTGRAFX" + identity.encode()[8:]
    with pytest.raises(GrafxCorruptionDetected) as raised:
        DatabaseIdentity.decode(foreign)
    assert raised.value.details["field"] == "magic"


def test_a_descriptor_length_that_does_not_fit_its_record_is_damage() -> None:
    identity = DatabaseIdentity(
        database_uuid=bytes(16),
        page_size=512,
        partitions_per_table=8,
        created_at_wall=1.0,
        granularity_descriptor="hash-v1;partitions_per_table=8",
    )
    raw = bytearray(identity.encode())
    raw[40:42] = (0xFFFF).to_bytes(2, "little")
    with pytest.raises(GrafxCorruptionDetected) as raised:
        DatabaseIdentity.decode(bytes(raw))
    assert raised.value.details["field"] == "granularity_descriptor_len"


def test_the_identity_store_refuses_a_page_size_the_database_was_not_created_with(
    tmp_path: Path,
) -> None:
    # The composition root refuses this earlier, from the file length, because the device cannot
    # even divide the file into pages. This is the guard behind that one, asked directly: it is
    # the one that compares what the identity record SAYS against what the caller asked for, and
    # it is what protects a caller that reaches MetaStore without going through connect.
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db"), page_size=512))
    try:
        db = connect(tmp_path / "db", page_size=512, registry=registry)
        pool = db._pool
        db.close()
        store = MetaStore(pool)
        wrong = DatabaseIdentity(
            database_uuid=bytes(16),
            page_size=1024,
            partitions_per_table=8,
            created_at_wall=1.0,
            granularity_descriptor="hash-v1;partitions_per_table=8",
        )
        with pytest.raises(GrafxSchemaVersionMismatch) as raised:
            store.open(wrong)
        assert raised.value.details["stored"] == 512
    finally:
        release_ports(registry)


def test_a_connect_that_meets_a_foreign_failure_still_closes_the_device(
    tmp_path: Path,
) -> None:
    """The taxonomy conversion must not cost the release that runs beside it.

    The arm that brings a foreign exception into the taxonomy is also the arm that releases what
    the open had already acquired. Its sibling arms are covered by a caller-supplied registry,
    where there is nothing to release -- so only a ``connect`` that OWNS its ports can show this
    one doing its other half.
    """
    opened: list[object] = []
    original_init = LocalStorageDevice.__init__

    def recording(self: object, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]
        opened.append(self)

    original_decode = PageCodecV1.decode_page

    def lie(self: object, raw: bytes, *, verify: bool = True) -> object:
        return raw

    LocalStorageDevice.__init__ = recording  # type: ignore[method-assign]
    PageCodecV1.decode_page = lie  # type: ignore[method-assign]
    try:
        with pytest.raises(GrafxConfigurationError) as raised:
            connect(tmp_path / "db")
    finally:
        LocalStorageDevice.__init__ = original_init  # type: ignore[method-assign]
        PageCodecV1.decode_page = original_decode  # type: ignore[method-assign]
    assert raised.value.details["field"] == "ports"
    assert len(opened) == 1
    # A44: the device is the object this call created, never "a device named X".
    with pytest.raises(GrafxUnsupportedOperation) as refusal:
        opened[0].exists("probe")  # type: ignore[attr-defined]
    assert refusal.value.details.get("reason") == "device_closed"


def test_a_device_failure_under_a_read_reaches_the_caller_as_the_class_it_started_as(
    tmp_path: Path,
) -> None:
    """The deepest path A47 governs, walked for the first time.

    A read through the public surface goes ``Database.execute`` -> Transaction -> QueryEngine ->
    HeapStore -> BufferPool -> StorageDevice, which is six layers between the caller and the
    failure. Each of them has an ``except`` somewhere, and any one of them could flatten the
    class or drop the retryable detail. The assertion is IDENTITY -- the object the device raised
    is the object the caller catches -- because an equal-looking error rebuilt on the way up
    passes every ``isinstance`` and has already lost whatever detail it did not copy.
    """
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db"), page_size=512))
    device = registry.get("storage")
    device_type = type(device)
    original = device_type.read_page
    planted = GrafxCorruptionDetected(
        "A page failed its checksum on the way up.",
        file="heap.dat",
        page=0,
        reason="planted_for_the_composition_test",
    )
    database = connect(tmp_path / "db", page_size=512, registry=registry)
    try:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
        database._pool.invalidate()

        def refuse(self: object, file: str, page_index: int) -> bytes:
            # A44: only the device this test built refuses, and only for the heap.
            if self is device and file == "heap.dat":
                raise planted
            return original(self, file, page_index)  # type: ignore[arg-type]

        device_type.read_page = refuse  # type: ignore[method-assign]
        try:
            with pytest.raises(GrafxCorruptionDetected) as raised:
                database.execute("MATCH (p:Person) RETURN p.name")
        finally:
            device_type.read_page = original  # type: ignore[method-assign]
        assert raised.value is planted
        assert raised.value.code == "corruption_detected"
        assert raised.value.retryable is False
        assert raised.value.details["reason"] == "planted_for_the_composition_test"
    finally:
        database.close()
        release_ports(registry)


def test_a_retryable_device_failure_under_a_read_keeps_its_retryable_flag(
    tmp_path: Path,
) -> None:
    # The same path with the other classification. A47's whole point is that the RETRYABLE detail
    # is what a caller acts on: reporting a transient device condition as permanent forbids the
    # one action that would have worked.
    registry = build_default_registry(DatabaseConfig(path=str(tmp_path / "db"), page_size=512))
    device = registry.get("storage")
    device_type = type(device)
    original = device_type.read_page
    planted = GrafxStorageError(
        "A sharing violation was exhausted.", winerror=32, attempts=3, reason="access_failed"
    )
    database = connect(tmp_path / "db", page_size=512, registry=registry)
    try:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
        database._pool.invalidate()

        def refuse(self: object, file: str, page_index: int) -> bytes:
            if self is device and file == "heap.dat":
                raise planted
            return original(self, file, page_index)  # type: ignore[arg-type]

        device_type.read_page = refuse  # type: ignore[method-assign]
        try:
            with pytest.raises(GrafxStorageError) as raised:
                database.execute("MATCH (p:Person) RETURN p.name")
        finally:
            device_type.read_page = original  # type: ignore[method-assign]
        assert raised.value is planted
        assert raised.value.retryable is True
        assert raised.value.to_dict()["retryable"] is True
    finally:
        database.close()
        release_ports(registry)
