"""The first open of a database must make the identity it hands out durable (P0.3).

``connect()`` on a fresh path publishes ``grafx.meta`` (the identity of section 6.2), an empty
catalog and an empty heap. They are one first-open unit: an intent fixes the UUID, each paged file
is checkpointed under an unpublished name, complete files replace the final names atomically,
and the intent disappears only after a barrier covers all three. Identity is not reconstructible
from the log, so this protocol is the authority rather than recovery guessing what a partial
bootstrap meant.

Three properties, through the public door and the fault bench of FR-16:

* when ``connect()`` returns, no data file has a write that an honest barrier has not pinned;
* an identity handed out by ``connect()`` survives a power loss -- the next open returns the
  same one or refuses with a typed error, never a silently different one;
* a crash (process death, writes durable) at ANY write point of the first open leaves a path
  the next ``connect()`` either opens clean or refuses typed. This one is a matrix over the
  write points the bench enumerates, so nobody chooses which windows are interesting; its
  outcomes are collected per point and asserted as a set.
"""

from __future__ import annotations

import os
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread, current_thread
from typing import Any

import pytest
from power_loss_support import (
    CONTROL_PREFIX,
    bench_registry,
    file_bytes,
    power_loss,
)

from okto_grafx import connect
from okto_grafx.adapters.storage_fault import (
    FaultInjectingStorageDevice,
    SimulatedCrash,
)
from okto_grafx.api import assembly
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxUnsupportedOperation,
)
from okto_grafx.engine.catalog_store import CATALOG_FILE
from okto_grafx.engine.database import META_FILE
from okto_grafx.engine.heap_store import HEAP_FILE
from okto_grafx.runtime.bootstrap import build_default_registry, release_ports
from okto_grafx.runtime.config import DatabaseConfig

pytestmark = pytest.mark.timeout(300, method="thread")


class _BlockingPublicationStorage(FaultInjectingStorageDevice):
    """Pause the first global barrier after all three first-open replacements are visible."""

    def __init__(self, inner: Any) -> None:
        super().__init__(inner, seed=1)
        self.publication_visible = Event()
        self.allow_publication_barrier = Event()
        self._published: set[str] = set()
        self._blocked = False

    def atomic_replace(self, source: str, target: str) -> None:
        super().atomic_replace(source, target)
        if target in (META_FILE, CATALOG_FILE, HEAP_FILE):
            self._published.add(target)

    def durable_barrier(self, file: str | None = None) -> None:
        finals = {META_FILE, CATALOG_FILE, HEAP_FILE}
        if file is None and self._published == finals and not self._blocked:
            self._blocked = True
            self.publication_visible.set()
            if not self.allow_publication_barrier.wait(30.0):
                raise AssertionError("the test never released the first-open publication barrier")
        super().durable_barrier(file)


class _CreatorDied(BaseException):
    """Process death without a power loss; kernel-visible dirty bytes remain visible."""


class _DieBeforePublicationBarrierStorage(_BlockingPublicationStorage):
    """Abandon the creator after all finals are visible but before their global barrier."""

    def durable_barrier(self, file: str | None = None) -> None:
        finals = {META_FILE, CATALOG_FILE, HEAP_FILE}
        if file is None and self._published == finals and not self._blocked:
            self._blocked = True
            self.publication_visible.set()
            raise _CreatorDied("the first-open creator process died")
        FaultInjectingStorageDevice.durable_barrier(self, file)


class _DieAfterIntentStagingStorage(FaultInjectingStorageDevice):
    """Leave the exact process-crash debris of create or append for a writable retry."""

    def __init__(self, inner: Any, operation: str) -> None:
        super().__init__(inner, seed=1)
        self._operation = operation
        self._died = False

    def create(self, file: str, *, exclusive: bool = True) -> None:
        super().create(file, exclusive=exclusive)
        if (
            not self._died
            and self._operation == "create"
            and file == assembly._FIRST_OPEN_INTENT_STAGING
        ):
            self._died = True
            raise _CreatorDied("creator died after creating intent staging")

    def append_log(self, file: str, payload: bytes) -> int:
        terminal = super().append_log(file, payload)
        if (
            not self._died
            and self._operation == "append_log"
            and file == assembly._FIRST_OPEN_INTENT_STAGING
        ):
            self._died = True
            raise _CreatorDied("creator died after appending intent staging")
        return terminal


class _DieAfterPendingRemovalStorage(FaultInjectingStorageDevice):
    """Lose process-local delete debt after COMPLETE is durable but before its barrier."""

    def __init__(self, inner: Any) -> None:
        super().__init__(inner, seed=1)
        self._died = False

    def remove(self, file: str) -> None:
        super().remove(file)
        if (
            not self._died
            and file == assembly._FIRST_OPEN_INTENT
            and self.inner.exists(assembly._FIRST_OPEN_COMPLETE)
        ):
            self._died = True
            raise _CreatorDied(
                "creator died after removing pending intent and before its global barrier"
            )


def _volatile_data_files(bench: Any) -> tuple[str, ...]:
    """Return the data files with a write no barrier has pinned (the control plane excluded)."""
    return tuple(
        name for name in bench.volatile_files() if not name.startswith(CONTROL_PREFIX)
    )


def test_the_first_open_returns_only_after_its_identity_and_headers_are_durable() -> (
    None
):
    registry, bench, _inner = bench_registry(1)
    bench.start_reordering()  # track every write from the first byte of the database
    database = connect(":memory:", registry=registry)
    try:
        uuid = database.identity.database_uuid
        at_return = _volatile_data_files(bench)
    finally:
        database.close()
    after_close = _volatile_data_files(bench)
    release_ports(registry)
    assert at_return == (), (
        f"connect() handed out {uuid} while these files had no barrier: {at_return}; "
        f"still unpinned after close(): {after_close}"
    )


def test_an_identity_handed_out_by_the_first_open_survives_a_power_loss() -> None:
    registry, bench, inner = bench_registry(1)
    bench.start_reordering()
    database = connect(":memory:", registry=registry)
    uuid = database.identity.database_uuid
    durable_meta = file_bytes(inner, META_FILE)
    assert durable_meta is not None
    assert META_FILE not in _volatile_data_files(bench)

    # The handle is deliberately abandoned: the process died. Closing it would exercise a
    # graceful shutdown instead of proving what connect() had already made durable at return.
    power_loss(bench)
    assert file_bytes(inner, META_FILE) == durable_meta
    with connect(":memory:", registry=registry) as reopened:
        outcome = ("opened", reopened.identity.database_uuid)
    release_ports(registry)
    assert outcome == ("opened", uuid)


def test_successful_first_open_keeps_a_checksummed_positive_completion_authority() -> None:
    registry, bench, inner = bench_registry(1)
    with connect(":memory:", registry=registry) as database:
        identity = database.identity

    assert assembly._read_first_open_complete(bench) == identity
    assert file_bytes(inner, assembly._FIRST_OPEN_COMPLETE) == (
        assembly._encode_first_open_intent(
            identity, magic=assembly._FIRST_OPEN_COMPLETE_MAGIC
        )
    )
    assert not inner.exists(assembly._FIRST_OPEN_INTENT)
    assert not inner.exists(assembly._FIRST_OPEN_COMPLETE_STAGING)
    release_ports(registry)


def test_read_only_waits_until_visible_first_open_files_are_durable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DatabaseConfig(path=":memory:")
    registry = build_default_registry(config)
    inner = registry.get("storage")
    storage = _BlockingPublicationStorage(inner)
    storage.start_reordering()
    registry.bind("storage", storage)
    coordinator = registry.get("coordinator")

    reader_name = "first-open-read-only"
    reader_waiting = Event()
    reader_done = Event()
    creator_done = Event()
    original_exclusive = type(coordinator).exclusive

    def observed_exclusive(self: Any, name: str, *, timeout: float) -> Any:
        section = original_exclusive(self, name, timeout=timeout)
        if name != assembly._FIRST_OPEN_SECTION or current_thread().name != reader_name:
            return section

        @contextmanager
        def wait_for_creator() -> Any:
            reader_waiting.set()
            with section:
                if not creator_done.wait(30.0):
                    raise AssertionError("the creator did not finish after releasing first-open")
                yield

        return wait_for_creator()

    monkeypatch.setattr(type(coordinator), "exclusive", observed_exclusive)
    creator_result: dict[str, object] = {}
    reader_result: dict[str, object] = {}
    failures: list[BaseException] = []

    def create_database() -> None:
        try:
            database = connect(":memory:", registry=registry)
            creator_result["uuid"] = database.identity.database_uuid
            database.close()
        except BaseException as failure:  # noqa: BLE001 - carried back to the test thread
            failures.append(failure)
        finally:
            creator_done.set()

    def open_read_only() -> None:
        try:
            with connect(":memory:", registry=registry, read_only=True) as database:
                reader_result["uuid"] = database.identity.database_uuid
                reader_result["findings"] = database.verify("all").findings
        except BaseException as failure:  # noqa: BLE001 - carried back to the test thread
            failures.append(failure)
        finally:
            reader_done.set()

    creator = Thread(target=create_database, name="first-open-creator", daemon=True)
    creator.start()
    assert storage.publication_visible.wait(30.0)
    finals = (META_FILE, CATALOG_FILE, HEAP_FILE)
    visible_before_barrier = {name: file_bytes(inner, name) for name in finals}
    assert all(visible_before_barrier.values())
    assert set(finals).issubset(storage.volatile_files())

    reader = Thread(target=open_read_only, name=reader_name, daemon=True)
    reader.start()
    assert reader_waiting.wait(30.0)
    assert not reader_done.wait(0.1), "read_only escaped before the publication barrier"

    storage.allow_publication_barrier.set()
    creator.join(30.0)
    reader.join(30.0)
    assert not creator.is_alive()
    assert not reader.is_alive()
    assert not failures
    assert reader_result == {"uuid": creator_result["uuid"], "findings": ()}
    assert {name: file_bytes(inner, name) for name in finals} == visible_before_barrier
    assert not set(finals).intersection(storage.volatile_files())
    release_ports(registry)


def test_read_only_refuses_a_dead_creator_before_reading_visible_finals() -> None:
    """A valid intent outranks three visible-but-unbarriered final names.

    Process death releases FIRST_OPEN_SECTION without rolling kernel caches back.  The next
    read-only participant therefore can see all three complete final payloads, but must inspect
    the still-durable intent first and refuse without changing any byte.  A writable retry is
    the only participant allowed to finish this recoverable publication.
    """
    config = DatabaseConfig(path=":memory:")
    registry = build_default_registry(config)
    inner = registry.get("storage")
    storage = _DieBeforePublicationBarrierStorage(inner)
    storage.start_reordering()
    registry.bind("storage", storage)

    with pytest.raises(_CreatorDied):
        connect(":memory:", registry=registry)
    finals = (META_FILE, CATALOG_FILE, HEAP_FILE)
    assert all(inner.exists(name) for name in finals)
    assert set(finals).issubset(storage.volatile_files())
    before = {
        name: file_bytes(inner, name)
        for name in (*finals, assembly._FIRST_OPEN_INTENT)
    }
    storage.clear_trail()

    with pytest.raises(GrafxUnsupportedOperation) as raised:
        connect(":memory:", registry=registry, read_only=True)
    after = {
        name: file_bytes(inner, name)
        for name in (*finals, assembly._FIRST_OPEN_INTENT)
    }
    assert raised.value.details["field"] == "first_open_intent"
    assert raised.value.details["repairable"] is True
    assert after == before
    assert not any(
        record.file == META_FILE and record.method in {"page_count", "read_page"}
        for record in storage.trail()
    ), "read-only opened the visible identity page before classifying the intent"

    with connect(":memory:", registry=registry) as recovered:
        assert recovered.verify("all").findings == ()
    assert not inner.exists(assembly._FIRST_OPEN_INTENT)
    release_ports(registry)


def test_read_only_validates_damaged_intent_before_visible_meta() -> None:
    """A damaged intent is corruption even when every final name is already visible."""
    config = DatabaseConfig(path=":memory:")
    registry = build_default_registry(config)
    inner = registry.get("storage")
    storage = _DieBeforePublicationBarrierStorage(inner)
    registry.bind("storage", storage)
    with pytest.raises(_CreatorDied):
        connect(":memory:", registry=registry)

    intent = bytearray(file_bytes(inner, assembly._FIRST_OPEN_INTENT) or b"")
    intent[-1] ^= 0x01
    inner.truncate_log(assembly._FIRST_OPEN_INTENT, 0)
    inner.append_log(assembly._FIRST_OPEN_INTENT, bytes(intent))
    inner.durable_barrier(assembly._FIRST_OPEN_INTENT)
    finals = (META_FILE, CATALOG_FILE, HEAP_FILE)
    before = {
        name: file_bytes(inner, name)
        for name in (*finals, assembly._FIRST_OPEN_INTENT)
    }
    storage.clear_trail()

    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(":memory:", registry=registry, read_only=True)
    after = {
        name: file_bytes(inner, name)
        for name in (*finals, assembly._FIRST_OPEN_INTENT)
    }
    assert raised.value.details["field"] == "crc32c"
    assert after == before
    assert not any(
        record.file == META_FILE and record.method in {"page_count", "read_page"}
        for record in storage.trail()
    )
    release_ports(registry)


@pytest.mark.parametrize("operation", ["create", "append_log"])
def test_writable_retry_discards_only_unpublished_partial_intent_staging(
    operation: str,
) -> None:
    """Process death before intent publication cannot strand an otherwise empty path."""
    config = DatabaseConfig(path=":memory:")
    registry = build_default_registry(config)
    inner = registry.get("storage")
    storage = _DieAfterIntentStagingStorage(inner, operation)
    registry.bind("storage", storage)

    with pytest.raises(_CreatorDied):
        connect(":memory:", registry=registry)
    assert inner.exists(assembly._FIRST_OPEN_INTENT_STAGING)
    assert not any(inner.exists(name) for name in (META_FILE, CATALOG_FILE, HEAP_FILE))

    with connect(":memory:", registry=registry) as recovered:
        assert recovered.verify("all").findings == ()
    assert not inner.exists(assembly._FIRST_OPEN_INTENT_STAGING)
    assert not inner.exists(assembly._FIRST_OPEN_INTENT)
    release_ports(registry)


def test_read_only_preserves_and_refuses_partial_intent_staging() -> None:
    """Read-only never performs the cleanup writable retry is authorised to perform."""
    config = DatabaseConfig(path=":memory:")
    registry = build_default_registry(config)
    inner = registry.get("storage")
    inner.create(assembly._FIRST_OPEN_INTENT_STAGING)
    inner.append_log(assembly._FIRST_OPEN_INTENT_STAGING, b"partial")
    inner.durable_barrier(assembly._FIRST_OPEN_INTENT_STAGING)
    before = file_bytes(inner, assembly._FIRST_OPEN_INTENT_STAGING)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(":memory:", registry=registry, read_only=True)
    assert raised.value.details["field"] == "length"
    assert file_bytes(inner, assembly._FIRST_OPEN_INTENT_STAGING) == before
    release_ports(registry)


def test_unknown_bootstrap_orphan_is_not_misclassified_as_an_empty_path() -> None:
    """Only protocol-owned unpublished debris is eligible for automatic retirement."""
    registry, _bench, inner = bench_registry(1)
    orphan = "bootstrap/operator-restore.evidence"
    inner.create(orphan)
    inner.append_log(orphan, b"possibly authoritative")
    inner.durable_barrier(orphan)
    before = file_bytes(inner, orphan)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(":memory:", registry=registry)
    assert raised.value.details["field"] == "bootstrap_orphan"
    assert raised.value.details["state"] == "unknown_staging"
    assert file_bytes(inner, orphan) == before
    assert not inner.exists(assembly._FIRST_OPEN_INTENT)
    assert not any(inner.exists(name) for name in (META_FILE, CATALOG_FILE, HEAP_FILE))
    release_ports(registry)


def test_public_connect_never_follows_bootstrap_redirect_into_a_victim(
    tmp_path: Path,
) -> None:
    root = tmp_path / "database"
    victim = tmp_path / "victim"
    root.mkdir()
    victim.mkdir()
    protected = victim / "first-open.intent"
    protected.write_bytes(b"victim authority")
    redirected = root / "bootstrap"
    try:
        os.symlink(victim, redirected, target_is_directory=True)
    except (NotImplementedError, OSError) as failure:
        pytest.skip(f"directory symlink/reparse creation is unavailable: {failure}")
    before = {entry.name: entry.read_bytes() for entry in victim.iterdir()}
    try:
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            connect(str(root))
        assert raised.value.details["reason"] == "redirected_path"
        assert {entry.name: entry.read_bytes() for entry in victim.iterdir()} == before
        assert tuple(entry.name for entry in root.iterdir()) == ("bootstrap",)
    finally:
        redirected.unlink()


@pytest.mark.platform_specific
@pytest.mark.skipif(os.name == "nt", reason="A FIFO namespace probe requires POSIX mkfifo.")
def test_public_connect_refuses_fifo_evidence_without_opening_or_hiding_it(
    tmp_path: Path,
) -> None:
    """A special node is preserved evidence, never an absent file or an empty database."""
    root = tmp_path / "database"
    root.mkdir()
    fifo = root / "operator.evidence"
    os.mkfifo(fifo)
    before = os.lstat(fifo)
    try:
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            connect(root)
        after = os.lstat(fifo)
        assert raised.value.details["reason"] == "unsupported_entry_type"
        assert raised.value.details["file"] == "operator.evidence"
        assert (after.st_mode, after.st_ino, after.st_size) == (
            before.st_mode,
            before.st_ino,
            before.st_size,
        )
        assert tuple(entry.name for entry in root.iterdir()) == ("operator.evidence",)
    finally:
        fifo.unlink()


@pytest.mark.platform_specific
@pytest.mark.skipif(
    os.name != "nt", reason="An NTFS junction namespace probe requires Windows."
)
def test_public_connect_refuses_windows_junction_without_following_or_hiding_it(
    tmp_path: Path,
) -> None:
    """A Windows reparse point remains evidence; its victim is never used as storage."""
    root = tmp_path / "database"
    victim = tmp_path / "victim"
    root.mkdir()
    victim.mkdir()
    protected = victim / "first-open.intent"
    protected.write_bytes(b"victim authority")
    redirected = root / "bootstrap"
    try:
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(redirected), str(victim)],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as failure:
        pytest.skip(f"NTFS junction creation is unavailable: {failure}")
    if created.returncode != 0:
        pytest.skip(
            "NTFS junction creation is unavailable: "
            f"{created.stderr.strip() or created.stdout.strip()}"
        )

    try:
        before_redirect = os.lstat(redirected)
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        assert reparse_attribute
        assert before_redirect.st_file_attributes & reparse_attribute
        before_victim = {entry.name: entry.read_bytes() for entry in victim.iterdir()}
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            connect(root)
        after_redirect = os.lstat(redirected)
        assert raised.value.details["reason"] == "redirected_path"
        assert raised.value.details["file"] == "bootstrap"
        assert after_redirect.st_file_attributes & reparse_attribute
        assert {
            entry.name: entry.read_bytes() for entry in victim.iterdir()
        } == before_victim
        assert tuple(entry.name for entry in root.iterdir()) == ("bootstrap",)
    finally:
        redirected.rmdir()


def test_bootstrap_orphan_beside_published_database_is_never_retired() -> None:
    """A final identity removes the proof that any bootstrap payload is expendable."""
    registry, _bench, inner = bench_registry(1)
    with connect(":memory:", registry=registry) as database:
        identity = database.identity
    payload = assembly._encode_first_open_intent(identity)
    inner.create(assembly._FIRST_OPEN_INTENT_STAGING)
    inner.append_log(assembly._FIRST_OPEN_INTENT_STAGING, payload)
    inner.durable_barrier(assembly._FIRST_OPEN_INTENT_STAGING)
    before = {
        name: file_bytes(inner, name)
        for name in (
            META_FILE,
            CATALOG_FILE,
            HEAP_FILE,
            assembly._FIRST_OPEN_INTENT_STAGING,
        )
    }

    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(":memory:", registry=registry)
    after = {
        name: file_bytes(inner, name)
        for name in (
            META_FILE,
            CATALOG_FILE,
            HEAP_FILE,
            assembly._FIRST_OPEN_INTENT_STAGING,
        )
    }
    assert raised.value.details["field"] == "bootstrap_orphan"
    assert raised.value.details["state"] == "unexpected_after_complete"
    assert after == before
    release_ports(registry)


def test_a_resurrected_matching_intent_beside_history_is_completed_not_resumed() -> None:
    registry, bench, inner = bench_registry(1)
    database = connect(":memory:", registry=registry)
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Kept(id INT64, PRIMARY KEY(id))")
    identity = database.identity
    database.close()

    assembly._write_first_open_intent(bench, identity)
    before = {
        name: file_bytes(inner, name)
        for name in (META_FILE, CATALOG_FILE, HEAP_FILE)
    }
    with connect(":memory:", registry=registry) as reopened:
        assert reopened.identity == identity
        assert reopened.verify("all").findings == ()
    after = {
        name: file_bytes(inner, name)
        for name in (META_FILE, CATALOG_FILE, HEAP_FILE)
    }
    assert after == before
    assert not inner.exists(assembly._FIRST_OPEN_INTENT)
    release_ports(registry)


def test_read_only_validates_but_never_retires_matching_completed_pending_state() -> None:
    registry, bench, inner = bench_registry(1)
    with connect(":memory:", registry=registry) as database:
        identity = database.identity
    assembly._write_first_open_intent(bench, identity)
    pending = file_bytes(inner, assembly._FIRST_OPEN_INTENT)

    with connect(":memory:", registry=registry, read_only=True) as reader:
        assert reader.identity == identity
        assert reader.verify("all").findings == ()
    assert file_bytes(inner, assembly._FIRST_OPEN_INTENT) == pending

    with connect(":memory:", registry=registry) as writer:
        assert writer.identity == identity
    assert not inner.exists(assembly._FIRST_OPEN_INTENT)
    release_ports(registry)


def test_complete_marker_must_match_meta_without_changing_evidence() -> None:
    registry, _bench, inner = bench_registry(1)
    with connect(":memory:", registry=registry) as database:
        identity = database.identity
    foreign = replace(identity, database_uuid=bytes(reversed(identity.database_uuid)))
    payload = assembly._encode_first_open_intent(
        foreign, magic=assembly._FIRST_OPEN_COMPLETE_MAGIC
    )
    inner.truncate_log(assembly._FIRST_OPEN_COMPLETE, 0)
    inner.append_log(assembly._FIRST_OPEN_COMPLETE, payload)
    inner.durable_barrier(assembly._FIRST_OPEN_COMPLETE)
    names = inner.list_files()
    before = {name: file_bytes(inner, name) for name in names}

    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(":memory:", registry=registry)
    assert raised.value.details["state"] == "complete_meta_mismatch"
    assert inner.list_files() == names
    assert {name: file_bytes(inner, name) for name in names} == before
    release_ports(registry)


def test_complete_and_pending_identity_mismatch_is_preserved_and_refused() -> None:
    registry, bench, inner = bench_registry(1)
    with connect(":memory:", registry=registry) as database:
        identity = database.identity
    foreign = replace(identity, database_uuid=bytes(reversed(identity.database_uuid)))
    assembly._write_first_open_intent(bench, foreign)
    names = inner.list_files()
    before = {name: file_bytes(inner, name) for name in names}

    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(":memory:", registry=registry)
    assert raised.value.details["state"] == "pending_complete_mismatch"
    assert inner.list_files() == names
    assert {name: file_bytes(inner, name) for name in names} == before
    release_ports(registry)


def test_a_damaged_complete_marker_is_not_hidden_by_valid_finals() -> None:
    registry, bench, inner = bench_registry(1)
    with connect(":memory:", registry=registry):
        pass
    payload = bytearray(file_bytes(inner, assembly._FIRST_OPEN_COMPLETE) or b"")
    payload[-1] ^= 0x01
    inner.truncate_log(assembly._FIRST_OPEN_COMPLETE, 0)
    inner.append_log(assembly._FIRST_OPEN_COMPLETE, bytes(payload))
    inner.durable_barrier(assembly._FIRST_OPEN_COMPLETE)
    finals = (META_FILE, CATALOG_FILE, HEAP_FILE)
    before = {name: file_bytes(inner, name) for name in (*finals, assembly._FIRST_OPEN_COMPLETE)}
    bench.clear_trail()

    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(":memory:", registry=registry)
    after = {name: file_bytes(inner, name) for name in (*finals, assembly._FIRST_OPEN_COMPLETE)}
    assert raised.value.details["field"] == "crc32c"
    assert after == before
    assert not any(
        record.file == META_FILE and record.method in {"page_count", "read_page"}
        for record in bench.trail()
    )
    release_ports(registry)


def test_new_adapter_reinforces_complete_before_history_and_survived_pending_returns() -> None:
    """Absence of PENDING never authorises history; durable COMPLETE does.

    The creator dies after removing PENDING but before the global barrier that would pin that
    absence.  A wholly new fault adapter has no deletion debt to inherit.  It must first
    reinforce the positive COMPLETE marker, may then commit history, and a later power loss of
    the dead creator may resurrect matching PENDING (and the atomic-rename source staging).
    The next writer classifies those bytes as an already completed first open, preserves the
    transaction history, and durably retires only the matching duplicates.
    """
    config = DatabaseConfig(path=":memory:")
    registry = build_default_registry(config)
    inner = registry.get("storage")
    creator = _DieAfterPendingRemovalStorage(inner)
    creator.start_reordering()
    registry.bind("storage", creator)

    with pytest.raises(_CreatorDied):
        connect(":memory:", registry=registry)
    identity = assembly._read_first_open_complete(creator)
    assert identity is not None
    assert not inner.exists(assembly._FIRST_OPEN_INTENT)
    assert assembly._FIRST_OPEN_INTENT in creator.volatile_files()

    second_process = FaultInjectingStorageDevice(inner, seed=2)
    registry.bind("storage", second_process)
    with connect(":memory:", registry=registry) as writer:
        assert writer.identity == identity
        with writer.begin("write") as txn:
            txn.execute("CREATE NODE TABLE AfterComplete(id INT64, PRIMARY KEY(id))")
        assert writer.verify("all").findings == ()

    # The old process's process-local debt is deliberately the only thing that knows the
    # removal was not pinned.  A lying-barrier power loss deterministically rolls all of that
    # debt back, exactly as a new process cannot prevent.
    creator.lie_on_barrier()
    power_loss(creator)
    assert inner.exists(assembly._FIRST_OPEN_INTENT)
    assert inner.exists(assembly._FIRST_OPEN_COMPLETE_STAGING)

    third_process = FaultInjectingStorageDevice(inner, seed=3)
    registry.bind("storage", third_process)
    with connect(":memory:", registry=registry) as reopened:
        assert reopened.identity == identity
        assert reopened.verify("all").findings == ()
    assert not inner.exists(assembly._FIRST_OPEN_INTENT)
    assert not inner.exists(assembly._FIRST_OPEN_COMPLETE_STAGING)
    assert assembly._read_first_open_complete(third_process) == identity
    release_ports(registry)


def test_a_damaged_intent_is_never_interpreted_as_an_absent_intent() -> None:
    registry, bench, inner = bench_registry(1)
    identity = assembly._configured_identity(
        DatabaseConfig(path=":memory:"), registry.get("clock")
    )
    assembly._write_first_open_intent(bench, identity)
    payload = bytearray(file_bytes(inner, assembly._FIRST_OPEN_INTENT) or b"")
    assert payload
    payload[-1] ^= 0x01
    inner.truncate_log(assembly._FIRST_OPEN_INTENT, 0)
    inner.append_log(assembly._FIRST_OPEN_INTENT, bytes(payload))
    inner.durable_barrier(assembly._FIRST_OPEN_INTENT)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(":memory:", registry=registry)
    assert raised.value.details["field"] == "crc32c"
    assert not inner.exists(META_FILE)
    assert not inner.exists(CATALOG_FILE)
    assert not inner.exists(HEAP_FILE)
    release_ports(registry)


def test_valid_pending_intent_refuses_foreign_namespace_evidence_without_mutation() -> None:
    registry, bench, inner = bench_registry(1)
    identity = assembly._configured_identity(
        DatabaseConfig(path=":memory:"), registry.get("clock")
    )
    assembly._write_first_open_intent(bench, identity)
    foreign = "operator/restore.evidence"
    inner.create(foreign)
    inner.append_log(foreign, b"do not classify me as empty")
    inner.durable_barrier(foreign)
    names = inner.list_files()
    before = {name: file_bytes(inner, name) for name in names}

    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(":memory:", registry=registry)

    assert raised.value.details["field"] == "first_open_namespace"
    assert raised.value.details["state"] == "foreign_evidence"
    assert inner.list_files() == names
    assert {name: file_bytes(inner, name) for name in names} == before
    assert not any(inner.exists(name) for name in (META_FILE, CATALOG_FILE, HEAP_FILE))
    release_ports(registry)


def test_intent_publication_stages_complete_bytes_before_the_canonical_rename() -> None:
    """The canonical name is an atomic publication of a barriered complete payload."""
    registry, bench, inner = bench_registry(1)
    identity = assembly._configured_identity(
        DatabaseConfig(path=":memory:"), registry.get("clock")
    )
    payload = assembly._encode_first_open_intent(identity)
    bench.clear_trail()

    assembly._write_first_open_intent(bench, identity)

    assert file_bytes(inner, assembly._FIRST_OPEN_INTENT) == payload
    assert not inner.exists(assembly._FIRST_OPEN_INTENT_STAGING)
    assert [
        (record.method, record.file)
        for record in bench.trail()
    ] == [
        ("exists", assembly._FIRST_OPEN_INTENT),
        ("create", assembly._FIRST_OPEN_INTENT_STAGING),
        ("append_log", assembly._FIRST_OPEN_INTENT_STAGING),
        ("durable_barrier", assembly._FIRST_OPEN_INTENT_STAGING),
        ("exists", assembly._FIRST_OPEN_INTENT),
        ("atomic_replace", assembly._FIRST_OPEN_INTENT_STAGING),
        ("durable_barrier", assembly._FIRST_OPEN_INTENT),
    ]
    release_ports(registry)


def test_oversized_intent_is_refused_without_requesting_its_payload() -> None:
    """The on-disk length is never trusted as a read/allocation size."""
    registry, bench, inner = bench_registry(1)
    inner.create(assembly._FIRST_OPEN_INTENT)
    inner.append_log(
        assembly._FIRST_OPEN_INTENT,
        bytes(assembly._FIRST_OPEN_INTENT_MAX_BYTES + 1),
    )
    inner.durable_barrier(assembly._FIRST_OPEN_INTENT)
    bench.clear_trail()

    with pytest.raises(GrafxCorruptionDetected) as raised:
        assembly._read_first_open_intent(bench)
    assert raised.value.details["field"] == "length"
    assert not bench.calls_of("read_log")
    release_ports(registry)


def test_intent_declared_length_is_checked_after_only_the_fixed_header() -> None:
    """An extension cannot turn its file size into the decoder's second read request."""
    registry, bench, inner = bench_registry(1)
    header = assembly._FIRST_OPEN_INTENT_HEAD.pack(
        assembly._FIRST_OPEN_INTENT_MAGIC,
        assembly._FIRST_OPEN_INTENT_VERSION,
        1,
    )
    payload = header + bytes(64)
    inner.create(assembly._FIRST_OPEN_INTENT)
    inner.append_log(assembly._FIRST_OPEN_INTENT, payload)
    inner.durable_barrier(assembly._FIRST_OPEN_INTENT)
    bench.clear_trail()

    with pytest.raises(GrafxCorruptionDetected) as raised:
        assembly._read_first_open_intent(bench)
    reads = bench.calls_of("read_log")
    assert raised.value.details["field"] == "length"
    assert len(reads) == 1
    assert f"length={assembly._FIRST_OPEN_INTENT_HEAD.size}" in reads[0].args_summary
    release_ports(registry)


def test_a_missing_authoritative_store_is_damage_not_a_fresh_database() -> None:
    registry, bench, inner = bench_registry(1)
    database = connect(":memory:", registry=registry)
    database.close()
    meta_before = file_bytes(inner, META_FILE)
    catalog_before = file_bytes(inner, CATALOG_FILE)
    bench.remove(HEAP_FILE)
    bench.durable_barrier(None)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(":memory:", registry=registry)
    assert raised.value.details["file"] == HEAP_FILE
    assert not inner.exists(HEAP_FILE)
    assert file_bytes(inner, META_FILE) == meta_before
    assert file_bytes(inner, CATALOG_FILE) == catalog_before
    release_ports(registry)


def test_missing_identity_over_existing_stores_never_mints_a_new_uuid() -> None:
    registry, bench, inner = bench_registry(1)
    database = connect(":memory:", registry=registry)
    database.close()
    catalog_before = file_bytes(inner, CATALOG_FILE)
    heap_before = file_bytes(inner, HEAP_FILE)
    bench.remove(META_FILE)
    bench.durable_barrier(None)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(":memory:", registry=registry)
    assert raised.value.details["field"] == "first_open_complete"
    assert not inner.exists(META_FILE)
    assert file_bytes(inner, CATALOG_FILE) == catalog_before
    assert file_bytes(inner, HEAP_FILE) == heap_before
    assert not inner.exists(assembly._FIRST_OPEN_INTENT)
    release_ports(registry)


def _open_and_close(registry: Any) -> None:
    connect(":memory:", registry=registry).close()


def _crash_after_the_identity_file_is_published() -> tuple[Any, Any, Any, Any]:
    """Leave a valid intent with exactly the canonical identity final already published."""
    registry, bench, inner = bench_registry(1)
    points = bench.enumerate_write_points(lambda device: _open_and_close(registry))
    publication = next(
        point
        for point in points
        if point.method == "atomic_replace"
        and point.file == assembly._FIRST_OPEN_META_STAGING
    )
    release_ports(registry)

    registry, bench, inner = bench_registry(1)
    bench.clear_trail()
    bench.crash_at(publication.call_index, moment="after")
    with pytest.raises(SimulatedCrash):
        _open_and_close(registry)
    bench.disarm()
    intent = assembly._read_first_open_intent(bench)
    assert intent is not None
    assert inner.exists(META_FILE)
    assert not inner.exists(CATALOG_FILE)
    assert not inner.exists(HEAP_FILE)
    return registry, bench, inner, intent


def test_a_retry_accepts_an_identical_published_subset_and_completes_the_rest() -> None:
    registry, _bench, inner, intent = _crash_after_the_identity_file_is_published()
    meta_before = file_bytes(inner, META_FILE)
    with connect(":memory:", registry=registry) as reopened:
        assert reopened.identity == intent
        assert reopened.verify("all").findings == ()
    assert file_bytes(inner, META_FILE) == meta_before
    assert not inner.exists(assembly._FIRST_OPEN_INTENT)
    release_ports(registry)


def test_a_retry_refuses_one_divergent_byte_without_overwriting_any_final() -> None:
    registry, _bench, inner, _intent = _crash_after_the_identity_file_is_published()
    published = bytearray(inner.read_page(META_FILE, 0))
    published[-1] ^= 0x01
    inner.write_page(META_FILE, 0, bytes(published))
    divergent = file_bytes(inner, META_FILE)

    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(":memory:", registry=registry)
    assert raised.value.details["field"] == "published_bytes"
    assert file_bytes(inner, META_FILE) == divergent
    assert not inner.exists(CATALOG_FILE)
    assert not inner.exists(HEAP_FILE)
    assert inner.exists(assembly._FIRST_OPEN_INTENT)
    release_ports(registry)


def test_crash_before_and_after_every_completion_marker_write_converges() -> None:
    registry, bench, _inner = bench_registry(1)
    surveyed = bench.enumerate_write_points(lambda device: _open_and_close(registry))
    release_ports(registry)
    marker_files = {
        assembly._FIRST_OPEN_COMPLETE_STAGING,
        assembly._FIRST_OPEN_COMPLETE,
    }
    points = tuple(point for point in surveyed if point.file in marker_files)
    assert [(point.method, point.file) for point in points] == [
        ("create", assembly._FIRST_OPEN_COMPLETE_STAGING),
        ("append_log", assembly._FIRST_OPEN_COMPLETE_STAGING),
        ("durable_barrier", assembly._FIRST_OPEN_COMPLETE_STAGING),
        ("atomic_replace", assembly._FIRST_OPEN_COMPLETE_STAGING),
        ("durable_barrier", assembly._FIRST_OPEN_COMPLETE),
    ]

    outcomes: dict[str, str] = {}
    for point in points:
        for moment in ("before", "after"):
            registry, bench, inner = bench_registry(1)
            bench.clear_trail()
            bench.crash_at(point.call_index, moment=moment)
            with pytest.raises(SimulatedCrash):
                _open_and_close(registry)
            bench.disarm()
            key = f"{point.method}:{point.file}:{moment}"
            try:
                with connect(":memory:", registry=registry) as reopened:
                    findings = reopened.verify("all").findings
                    outcomes[key] = f"opened:verify={len(findings)}"
            except GrafxError as refused:
                outcomes[key] = f"refused:{type(refused).__name__}:{refused.code}"
            assert inner.exists(assembly._FIRST_OPEN_COMPLETE), key
            assert not inner.exists(assembly._FIRST_OPEN_INTENT), key
            assert not inner.exists(assembly._FIRST_OPEN_COMPLETE_STAGING), key
            release_ports(registry)
    assert set(outcomes.values()) == {"opened:verify=0"}, outcomes


def test_a_power_loss_at_every_first_open_write_point_makes_writable_progress() -> None:
    """Every protocol crash window converges; a generic typed refusal is not success."""
    registry, bench, _inner = bench_registry(1)
    surveyed = bench.enumerate_write_points(lambda device: _open_and_close(registry))
    release_ports(registry)
    intent_remove = next(
        point
        for point in surveyed
        if point.method == "remove" and point.file == assembly._FIRST_OPEN_INTENT
    )
    retirement = next(
        point
        for point in surveyed
        if point.call_index > intent_remove.call_index
        and point.method == "durable_barrier"
        and point.file is None
    )
    points = tuple(
        point for point in surveyed if point.call_index <= retirement.call_index
    )
    assert len(points) >= 3, points

    outcomes: dict[str, str] = {}
    crashed = 0
    for point in points:
        registry, bench, _inner = bench_registry(1)
        bench.clear_trail()  # the same call wears the same number in the survey and in the run
        bench.crash_at(point.call_index)
        try:
            _open_and_close(registry)
        except SimulatedCrash:
            crashed += 1
        except GrafxError as refused:
            outcomes[f"{point.call_index}:{point.method}:{point.file}"] = (
                f"first open refused before the crash point: {refused.code}"
            )
            release_ports(registry)
            continue
        bench.disarm()
        key = f"{point.call_index}:{point.method}:{point.file}"
        try:
            reopened = connect(":memory:", registry=registry)
        except GrafxError as refused:
            outcomes[key] = f"refused:{type(refused).__name__}:{refused.code}"
        except Exception as other:  # noqa: BLE001 - the taxonomy is the assertion
            outcomes[key] = f"died:{type(other).__name__}:{other}"
        else:
            with reopened:
                findings = reopened.verify("all").findings
                report = reopened.recovery_report
                outcomes[key] = (
                    f"opened:verify={len(findings)}:recovery="
                    f"{getattr(report, 'outcome', report)}"
                )
        release_ports(registry)
    assert crashed == len(points), outcomes  # every selected point was really exercised
    bad = {
        key: value
        for key, value in outcomes.items()
        if not value.startswith("opened") or ":verify=0:" not in value
    }
    assert not bad, {"bad": bad, "all": outcomes}
