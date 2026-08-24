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

from contextlib import contextmanager
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
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxError
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


def test_an_interrupted_intent_cannot_overwrite_a_database_with_history() -> None:
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
    with pytest.raises(GrafxCorruptionDetected) as raised:
        connect(":memory:", registry=registry)
    after = {
        name: file_bytes(inner, name)
        for name in (META_FILE, CATALOG_FILE, HEAP_FILE)
    }
    release_ports(registry)
    assert raised.value.details["field"] == "transaction_history"
    assert after == before


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
    assert raised.value.details["field"] == "identity_missing"
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


def test_a_crash_at_every_write_point_of_the_first_open_leaves_a_path_that_opens_or_refuses() -> (
    None
):
    registry, bench, _inner = bench_registry(1)
    points = bench.enumerate_write_points(lambda device: _open_and_close(registry))
    release_ports(registry)
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
    assert crashed >= 1, outcomes  # A72: the matrix really cut the first open off
    bad = {
        key: value
        for key, value in outcomes.items()
        if value.startswith("died")
        or (value.startswith("opened") and ":verify=0:" not in value)
    }
    assert not bad, {"bad": bad, "all": outcomes}
