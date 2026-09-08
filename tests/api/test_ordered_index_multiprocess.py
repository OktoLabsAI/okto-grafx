"""Real-process fences for ordered catalog generations (OIX-2B).

The crash matrix proves what survives a cut.  These cases pin the concurrency promises
against participants that share no Python state: two writers publish alternating roots
without losing a batch, a reader pinned before a publication keeps its snapshot while the
writer is never blocked, a root that is durable before commit state shows nothing new to
another process, and a statement already walking the former generation finishes on it while
the next statement adopts the rebuilt one.
"""

from __future__ import annotations

import multiprocessing
import os
import queue as queue_module
import threading
import traceback
from pathlib import Path
from typing import Any

import pytest

from okto_grafx import connect
from okto_grafx.domain.index import IndexGenerationState
from okto_grafx.domain.model.value import Timestamp
from okto_grafx.engine.ordered_index import OrderedIndex

PAGE_SIZE = 512
CHILD_TIMEOUT_SECONDS = 90
INDEX_NAME = "by_event_time"
PK_SEEK = (
    "MATCH (e:Event) WHERE e.created_at = $created_at AND e.id = $id RETURN e.payload"
)
"""Answered through the primary-key hash index; the planner does not select an ordered
definition at this base (OIX-3), so the tree is read through the index manager's validated
door below, exactly as that path will."""


def _seed(database: object) -> None:
    with database.begin("write") as schema:  # type: ignore[attr-defined]
        schema.execute(
            "CREATE NODE TABLE Event("
            "id STRING, created_at TIMESTAMP, payload STRING, PRIMARY KEY(id))"
        )
    for event_id, micros in (("a", 30), ("b", 10), ("c", 20)):
        with database.begin("write") as rows:  # type: ignore[attr-defined]
            rows.execute(
                "CREATE (:Event {id: $id, created_at: $created_at, payload: $payload})",
                {
                    "id": event_id,
                    "created_at": Timestamp(micros),
                    "payload": event_id.upper(),
                },
            )
    database.create_index(INDEX_NAME, "Event", ("created_at", "id"), layout="ordered")  # type: ignore[attr-defined]


def _root(database: object) -> Any:
    store = database._indexes.active_index(
        INDEX_NAME, catalog=database._catalog.catalog
    )  # type: ignore[attr-defined]
    store.open()
    return store.open_root()


def _tree_rows(
    database: object, event_id: str, micros: int, snapshot: Any = None
) -> tuple:
    """Payloads the ordered tree offers for one exact key, heap-validated under a snapshot."""
    store = database._indexes.active_index(
        INDEX_NAME, catalog=database._catalog.catalog
    )  # type: ignore[attr-defined]
    store.open()
    table = database._catalog.catalog.table("Event")  # type: ignore[attr-defined]
    position = table.column_index("payload")
    template: list[object] = [None] * len(table.columns)
    template[table.column_index("created_at")] = Timestamp(micros)
    template[table.column_index("id")] = event_id
    key = store.definition.key_for(template)
    if snapshot is not None:
        hits = database._indexes.validated_versions(store, key, snapshot)  # type: ignore[attr-defined]
        return tuple((str(version.values[position]),) for _ref, version in hits)
    with database.begin("read") as transaction:  # type: ignore[attr-defined]
        hits = database._indexes.validated_versions(store, key, transaction.snapshot)  # type: ignore[attr-defined]
        return tuple((str(version.values[position]),) for _ref, version in hits)


def _rows(database: object, event_id: str, micros: int) -> tuple[tuple, tuple]:
    """(tree answer, primary-key seek answer) for one exact key."""
    params = {"created_at": Timestamp(micros), "id": event_id}
    return (
        _tree_rows(database, event_id, micros),
        database.execute(PK_SEEK, params).rows,  # type: ignore[attr-defined]
    )


def _report_failure(reports: Any, failure: BaseException) -> None:
    reports.put(
        {
            "ok": False,
            "error": repr(failure),
            "traceback": traceback.format_exc(),
            "pid": os.getpid(),
        }
    )


def _committing_participant(
    root: str,
    descriptor_revalidation: str,
    ready: Any,
    go: Any,
    reports: Any,
    prefix: str,
    count: int,
) -> None:
    """Open, wait for the parent, then commit ``count`` rows one transaction each."""
    database = None
    try:
        database = connect(
            root, page_size=PAGE_SIZE, descriptor_revalidation=descriptor_revalidation
        )
        ready.set()
        if not go.wait(CHILD_TIMEOUT_SECONDS):
            raise TimeoutError("the parent never released the writer")
        for number in range(count):
            with database.begin("write") as rows:
                rows.execute(
                    "CREATE (:Event {id: $id, created_at: $t, payload: $p})",
                    {
                        "id": f"{prefix}{number}",
                        "t": Timestamp(1_000 + number),
                        "p": f"{prefix}{number}".upper(),
                    },
                )
        published = database.transactions.published_state().last_committed_lsn
        reports.put(
            {
                "ok": True,
                "pid": os.getpid(),
                "published_lsn": published,
                "generation": _root(database).generation,
            }
        )
    except BaseException as failure:
        ready.set()
        _report_failure(reports, failure)
    finally:
        if database is not None:
            database.close()


def _parked_publication_participant(
    root: str,
    descriptor_revalidation: str,
    root_published: Any,
    release: Any,
    reports: Any,
) -> None:
    """Commit 'd' but hold the process between the root barrier and commit-state publication."""
    database = None
    try:
        database = connect(
            root, page_size=PAGE_SIZE, descriptor_revalidation=descriptor_revalidation
        )
        original_publish = OrderedIndex.publish_committed_batch

        def parked_publish(store: OrderedIndex, changes: Any, **options: Any) -> Any:
            report = original_publish(store, changes, **options)
            if store.name.lower() == INDEX_NAME:
                root_published.set()
                if not release.wait(CHILD_TIMEOUT_SECONDS):
                    raise TimeoutError("the parent never released the publication")
            return report

        OrderedIndex.publish_committed_batch = parked_publish  # type: ignore[method-assign]
        with database.begin("write") as rows:
            rows.execute(
                "CREATE (:Event {id: 'd', created_at: $t, payload: 'D'})",
                {"t": Timestamp(40)},
            )
        reports.put(
            {
                "ok": True,
                "pid": os.getpid(),
                "published_lsn": database.transactions.published_state().last_committed_lsn,
                "generation": _root(database).generation,
            }
        )
    except BaseException as failure:
        root_published.set()
        _report_failure(reports, failure)
    finally:
        release.set()
        if database is not None:
            database.close()


def _rebuild_participant(
    root: str,
    descriptor_revalidation: str,
    ready: Any,
    traversal_started: Any,
    rebuild_finished: Any,
    reports: Any,
) -> None:
    database = None
    try:
        database = connect(
            root, page_size=PAGE_SIZE, descriptor_revalidation=descriptor_revalidation
        )
        ready.set()
        if not traversal_started.wait(CHILD_TIMEOUT_SECONDS):
            raise TimeoutError("the parent never entered the old ordered traversal")
        rebuilt = database.rebuild_index(INDEX_NAME)
        reports.put(
            {
                "ok": True,
                "pid": os.getpid(),
                "nonce": rebuilt.active_nonce,
                "file": rebuilt.file,
            }
        )
    except BaseException as failure:
        ready.set()
        _report_failure(reports, failure)
    finally:
        rebuild_finished.set()
        if database is not None:
            database.close()


def _receive(process: Any, reports: Any) -> dict[str, Any]:
    try:
        report = reports.get(timeout=CHILD_TIMEOUT_SECONDS)
    except queue_module.Empty as failure:
        raise AssertionError("the spawned participant produced no report") from failure
    finally:
        process.join(timeout=CHILD_TIMEOUT_SECONDS)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
    assert process.exitcode == 0
    assert report["ok"], report.get("traceback", report)
    assert report["pid"] != os.getpid()
    return report


@pytest.mark.multiprocess
@pytest.mark.timeout(180)
@pytest.mark.parametrize("descriptor_revalidation", ["strict", "generation"])
def test_two_real_writers_publish_alternating_roots_without_losing_a_batch(
    tmp_path: Path, descriptor_revalidation: str
) -> None:
    root = tmp_path / "database"
    database = connect(
        root, page_size=PAGE_SIZE, descriptor_revalidation=descriptor_revalidation
    )
    context = multiprocessing.get_context("spawn")
    go = context.Event()
    reports = context.Queue()
    processes = []
    try:
        _seed(database)
        before = _root(database)
        file = database.indexes.index(INDEX_NAME).file
        pages_before = database._storage.page_count(file)
        for prefix in ("w", "x"):
            ready = context.Event()
            process = context.Process(
                target=_committing_participant,
                args=(
                    str(root),
                    descriptor_revalidation,
                    ready,
                    go,
                    reports,
                    prefix,
                    6,
                ),
            )
            process.start()
            assert ready.wait(CHILD_TIMEOUT_SECONDS), (
                "a writer never opened the database"
            )
            processes.append(process)
        go.set()
        received = [_receive(process, reports) for process in processes]
        assert all(report["generation"] > before.generation for report in received)
        for prefix in ("w", "x"):
            for number in range(6):
                expected = ((f"{prefix}{number}".upper(),),)
                assert _rows(database, f"{prefix}{number}", 1_000 + number) == (
                    expected,
                    expected,
                )
        after = _root(database)
        assert after.entry_count == 3 + 12
        # One publication per committed transaction, none lost, none coalesced across
        # transactions or processes.
        assert after.generation == before.generation + 12
        assert after.applied_through_lsn == max(r["published_lsn"] for r in received)
        pages_after = database._storage.page_count(file)
        assert pages_before < pages_after <= pages_before + 12 * 4
        assert database.indexes.index(INDEX_NAME).file == file
        assert database.verify("all").findings == ()
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        database.close()
    with connect(root, page_size=PAGE_SIZE) as reopened:
        assert _root(reopened) == after
        assert reopened.verify("all").findings == ()


@pytest.mark.multiprocess
@pytest.mark.timeout(180)
@pytest.mark.parametrize("descriptor_revalidation", ["strict", "generation"])
def test_reader_pinned_before_a_publication_keeps_its_snapshot_and_never_blocks_the_writer(
    tmp_path: Path, descriptor_revalidation: str
) -> None:
    root = tmp_path / "database"
    database = connect(
        root, page_size=PAGE_SIZE, descriptor_revalidation=descriptor_revalidation
    )
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    go = context.Event()
    reports = context.Queue()
    process = None
    try:
        _seed(database)
        before = _root(database)
        pinned = database.begin("read")
        assert (
            pinned.execute(PK_SEEK, {"created_at": Timestamp(1_000), "id": "y0"}).rows
            == ()
        )
        assert _tree_rows(database, "y0", 1_000, pinned.snapshot) == ()
        process = context.Process(
            target=_committing_participant,
            args=(str(root), descriptor_revalidation, ready, go, reports, "y", 1),
        )
        process.start()
        assert ready.wait(CHILD_TIMEOUT_SECONDS)
        go.set()
        report = _receive(process, reports)
        assert report["generation"] == before.generation + 1
        # The writer published while the reader was pinned: the pinned snapshot still sees
        # neither the new row through the tree nor through the heap.
        assert (
            pinned.execute(PK_SEEK, {"created_at": Timestamp(1_000), "id": "y0"}).rows
            == ()
        )
        assert _tree_rows(database, "y0", 1_000, pinned.snapshot) == ()
        assert pinned.execute(
            PK_SEEK, {"created_at": Timestamp(20), "id": "c"}
        ).rows == (("C",),)
        assert _tree_rows(database, "c", 20, pinned.snapshot) == (("C",),)
        pinned.rollback()
        assert _rows(database, "y0", 1_000) == ((("Y0",),), (("Y0",),))
        assert _root(database).generation == before.generation + 1
        assert database.verify("all").findings == ()
    finally:
        if process is not None and process.is_alive():
            process.terminate()
        database.close()


@pytest.mark.multiprocess
@pytest.mark.timeout(180)
@pytest.mark.parametrize("descriptor_revalidation", ["strict", "generation"])
def test_a_root_durable_before_commit_state_shows_nothing_new_to_another_process(
    tmp_path: Path, descriptor_revalidation: str
) -> None:
    root = tmp_path / "database"
    database = connect(
        root, page_size=PAGE_SIZE, descriptor_revalidation=descriptor_revalidation
    )
    context = multiprocessing.get_context("spawn")
    root_published = context.Event()
    release = context.Event()
    reports = context.Queue()
    process = None
    try:
        _seed(database)
        before = _root(database)
        published_before = database.transactions.published_state().last_committed_lsn
        process = context.Process(
            target=_parked_publication_participant,
            args=(str(root), descriptor_revalidation, root_published, release, reports),
        )
        process.start()
        assert root_published.wait(CHILD_TIMEOUT_SECONDS), (
            "the spawned writer never published its ordered root"
        )
        # The window the design permits: the selected root is ahead of the published
        # database high-water, and nothing new is visible to a fresh snapshot.
        ahead = _root(database)
        assert ahead.generation == before.generation + 1
        assert ahead.entry_count == before.entry_count + 1
        # The child's first commit on its fresh handle may already have reserved the identity
        # floor (its own commit-state publication); the user commit itself is not published.
        published_now = database.transactions.published_state().last_committed_lsn
        assert published_before <= published_now < ahead.applied_through_lsn
        assert _rows(database, "d", 40) == ((), ())
        assert _rows(database, "c", 20) == ((("C",),), (("C",),))
        release.set()
        report = _receive(process, reports)
        assert report["published_lsn"] >= ahead.applied_through_lsn
        assert _rows(database, "d", 40) == ((("D",),), (("D",),))
        assert _root(database) == ahead
        assert database.verify("all").findings == ()
    finally:
        release.set()
        if process is not None and process.is_alive():
            process.terminate()
        database.close()


@pytest.mark.multiprocess
@pytest.mark.timeout(180)
@pytest.mark.parametrize("descriptor_revalidation", ["strict", "generation"])
def test_certified_old_ordered_reader_finishes_then_next_statement_adopts_the_rebuilt_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, descriptor_revalidation: str
) -> None:
    root = tmp_path / "database"
    database = connect(
        root, page_size=PAGE_SIZE, descriptor_revalidation=descriptor_revalidation
    )
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    traversal_started = context.Event()
    rebuild_finished = context.Event()
    reports = context.Queue()
    process = None
    try:
        _seed(database)
        former = database.indexes.index(INDEX_NAME)
        process = context.Process(
            target=_rebuild_participant,
            args=(
                str(root),
                descriptor_revalidation,
                ready,
                traversal_started,
                rebuild_finished,
                reports,
            ),
        )
        process.start()
        assert ready.wait(CHILD_TIMEOUT_SECONDS)
        # The index manager's validated door walks the tree through this helper inside its
        # own stable view; parking here parks the statement on the former generation's pages.
        original_candidates = OrderedIndex._candidates_unchecked
        traversed_files: list[str] = []
        parked = False

        def park_old_traversal(
            index: OrderedIndex, wanted: bytes
        ) -> tuple[object, ...]:
            nonlocal parked
            candidates = original_candidates(index, wanted)
            if index.name.lower() == INDEX_NAME:
                traversed_files.append(index.file)
                if index.file == former.file and not parked:
                    parked = True
                    traversal_started.set()
                    if not rebuild_finished.wait(CHILD_TIMEOUT_SECONDS):
                        raise TimeoutError("the rebuild participant never finished")
            return candidates

        monkeypatch.setattr(OrderedIndex, "_candidates_unchecked", park_old_traversal)
        outcome: dict[str, Any] = {}

        def old_statement() -> None:
            try:
                outcome["rows"] = _tree_rows(database, "c", 20)
            except BaseException as failure:  # pragma: no cover - reported below
                outcome["error"] = failure

        worker = threading.Thread(target=old_statement)
        worker.start()
        assert traversal_started.wait(CHILD_TIMEOUT_SECONDS), (
            "the parent never entered the old ordered traversal"
        )
        report = _receive(process, reports)
        worker.join(timeout=CHILD_TIMEOUT_SECONDS)
        assert not worker.is_alive()
        assert "error" not in outcome, repr(outcome.get("error"))
        assert outcome["rows"] == (("C",),)
        assert traversed_files and traversed_files[0] == former.file
        # The next statement adopts the rebuilt ACTIVE generation: a statement refreshes the
        # handle's catalog picture; the last-observed public view alone does not.
        assert database.execute(
            PK_SEEK, {"created_at": Timestamp(10), "id": "b"}
        ).rows == (("B",),)
        assert _tree_rows(database, "b", 10) == (("B",),)
        assert traversed_files[-1] == report["file"] != former.file
        current = database.indexes.index(INDEX_NAME)
        assert current.active_nonce == report["nonce"]
        logical = database._catalog.catalog.index_definition(INDEX_NAME)
        assert (
            logical.generation(former.active_nonce).state is IndexGenerationState.STALE
        )
        assert database._storage.exists(former.file)
        assert database.verify("all").findings == ()
    finally:
        rebuild_finished.set()
        if process is not None and process.is_alive():
            process.terminate()
        database.close()
