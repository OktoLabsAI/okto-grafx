"""Real-process fences for foreground growth-only exact-index rehash.

The ordinary rehash tests prove the generation rotation and recovery outcomes.  These two
cases pin the remaining concurrency promises against a participant that shares no Python
state: a writer cannot publish through the shadow-build window, while a read already fenced
on the immutable former generation may finish and the next statement adopts the new ACTIVE.
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
from okto_grafx.domain.errors import GrafxError
from okto_grafx.engine.index_manager import HashIndex


PAGE_SIZE = 512
CHILD_TIMEOUT_SECONDS = 90


def _seed(database: object) -> None:
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Person(id INT64, email STRING, PRIMARY KEY(id))"
        )
    with database.begin("write") as rows:
        rows.execute("CREATE (:Person {id: 1, email: 'ada@example.test'})")
        rows.execute("CREATE (:Person {id: 2, email: 'grace@example.test'})")
    database.create_index("by_email", "Person", ("email",), bucket_count=8)


def _report_failure(reports: Any, failure: BaseException) -> None:
    reports.put(
        {
            "ok": False,
            "error": repr(failure),
            "traceback": traceback.format_exc(),
            "pid": os.getpid(),
        }
    )


def _parked_rehash_participant(
    root: str,
    descriptor_revalidation: str,
    build_entered: Any,
    release_build: Any,
    reports: Any,
) -> None:
    """Hold the new generation inside its fenced physical build until the parent releases it."""

    database = None
    try:
        database = connect(
            root,
            page_size=PAGE_SIZE,
            descriptor_revalidation=descriptor_revalidation,
        )
        manager_type = type(database._indexes)
        original_build = manager_type._build_detached_exact_generation

        def parked_build(
            manager: object,
            definition: object,
            through_lsn: int,
        ) -> object:
            if str(getattr(definition, "name", "")).lower() == "by_email":
                build_entered.set()
                if not release_build.wait(CHILD_TIMEOUT_SECONDS):
                    raise TimeoutError(
                        "the parent never released the rehash shadow build"
                    )
            return original_build(manager, definition, through_lsn)

        manager_type._build_detached_exact_generation = parked_build
        grown = database.rehash_index("by_email", bucket_count=16)
        reports.put(
            {
                "ok": True,
                "pid": os.getpid(),
                "nonce": grown.active_nonce,
                "file": grown.file,
                "bucket_count": grown.bucket_count,
                "published_lsn": database.transactions.published_state().last_committed_lsn,
            }
        )
    except BaseException as failure:
        build_entered.set()
        _report_failure(reports, failure)
    finally:
        release_build.set()
        if database is not None:
            database.close()


def _triggered_rehash_participant(
    root: str,
    descriptor_revalidation: str,
    ready: Any,
    traversal_started: Any,
    rehash_finished: Any,
    reports: Any,
) -> None:
    """Open first, then rotate the generation while the parent's old traversal is parked."""

    database = None
    try:
        database = connect(
            root,
            page_size=PAGE_SIZE,
            descriptor_revalidation=descriptor_revalidation,
        )
        ready.set()
        if not traversal_started.wait(CHILD_TIMEOUT_SECONDS):
            raise TimeoutError("the parent never entered the old exact-index traversal")
        grown = database.rehash_index("by_email", bucket_count=16)
        reports.put(
            {
                "ok": True,
                "pid": os.getpid(),
                "nonce": grown.active_nonce,
                "file": grown.file,
                "bucket_count": grown.bucket_count,
            }
        )
    except BaseException as failure:
        ready.set()
        _report_failure(reports, failure)
    finally:
        rehash_finished.set()
        if database is not None:
            database.close()


def _receive_child_report(process: Any, reports: Any) -> dict[str, Any]:
    try:
        report = reports.get(timeout=CHILD_TIMEOUT_SECONDS)
    except queue_module.Empty as failure:
        raise AssertionError(
            "the spawned rehash participant produced no report"
        ) from failure
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
def test_writer_cannot_publish_inside_rehash_build_and_catalog_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_revalidation: str,
) -> None:
    """RH-14: a writer arriving during the shadow build publishes only after the rehash."""

    root = tmp_path / "database"
    database = connect(
        root,
        page_size=PAGE_SIZE,
        descriptor_revalidation=descriptor_revalidation,
    )
    context = multiprocessing.get_context("spawn")
    build_entered = context.Event()
    release_build = context.Event()
    reports = context.Queue()
    process = None
    writer = None
    writer_thread = None
    try:
        _seed(database)
        former = database.indexes.index("by_email")
        process = context.Process(
            target=_parked_rehash_participant,
            args=(
                str(root),
                descriptor_revalidation,
                build_entered,
                release_build,
                reports,
            ),
        )
        process.start()
        assert build_entered.wait(CHILD_TIMEOUT_SECONDS), (
            "the spawned participant never reached the rehash shadow build"
        )

        # This transaction is intentionally staged against the pre-rehash generation.  Record
        # the exact instant it asks for the cross-process lease, then let the child finish.  If
        # the writer could interleave, its publication would receive the earlier durable LSN;
        # under the fence it either commits later or is refused as stale before any WAL byte.
        writer = database.begin("write")
        writer.execute("CREATE (:Person {id: 3, email: 'barbara@example.test'})")
        writer_at_lease = threading.Event()
        writer_done = threading.Event()
        writer_outcome: dict[str, object] = {}
        coordinator_type = type(database._transactions._coordinator)
        original_acquire = coordinator_type.acquire_writer_lease

        def recording_acquire(coordinator: object, *, timeout: float) -> object:
            if coordinator is database._transactions._coordinator:
                writer_at_lease.set()
            return original_acquire(coordinator, timeout=timeout)

        monkeypatch.setattr(
            coordinator_type,
            "acquire_writer_lease",
            recording_acquire,
        )

        def commit_writer() -> None:
            try:
                report = writer.commit()
                writer_outcome["report"] = report
            except BaseException as failure:  # transported to the assertion thread
                writer_outcome["failure"] = failure
            finally:
                writer_done.set()

        writer_thread = threading.Thread(target=commit_writer, daemon=True)
        writer_thread.start()
        assert writer_at_lease.wait(CHILD_TIMEOUT_SECONDS), (
            "the competing writer never requested its lease"
        )
        assert not writer_done.is_set(), (
            "the writer crossed a lease already held by the parked rehash"
        )
        release_build.set()
        child = _receive_child_report(process, reports)
        writer_thread.join(timeout=CHILD_TIMEOUT_SECONDS)
        assert writer_done.is_set(), "the competing writer did not settle after rehash"

        failure = writer_outcome.get("failure")
        if failure is not None:
            # A transaction staged on the former generation may correctly lose provenance.
            # Its refusal must remain pre-WAL/retryable; a fresh writer below proves progress.
            assert isinstance(failure, GrafxError), repr(failure)
            assert failure.retryable is True
            assert writer.active
            writer.rollback()
            with database.begin("write") as successor:
                successor.execute(
                    "CREATE (:Person {id: 3, email: 'barbara@example.test'})"
                )
            writer_lsn = database.transactions.published_state().last_committed_lsn
        else:
            report = writer_outcome["report"]
            writer_lsn = int(getattr(report, "csn"))

        assert writer_lsn > int(child["published_lsn"])
        assert child["bucket_count"] == 16
        assert child["file"] != former.file
        assert database.execute(
            "MATCH (p:Person) WHERE p.email = 'barbara@example.test' RETURN p.id"
        ).rows == ((3,),)
        active = database.indexes.index("by_email")
        assert active.file == child["file"]
        assert active.active_nonce == child["nonce"]
        assert database.verify("all").findings == ()
    finally:
        release_build.set()
        if writer_thread is not None:
            writer_thread.join(timeout=10)
        if writer is not None and writer.active:
            writer.rollback()
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=10)
        database.close()


@pytest.mark.multiprocess
@pytest.mark.timeout(180)
@pytest.mark.parametrize("descriptor_revalidation", ["strict", "generation"])
def test_certified_old_reader_finishes_then_next_statement_adopts_new_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_revalidation: str,
) -> None:
    """RH-15: immutable retired generations remain valid only for an in-flight statement."""

    root = tmp_path / "database"
    database = connect(
        root,
        page_size=PAGE_SIZE,
        descriptor_revalidation=descriptor_revalidation,
    )
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    traversal_started = context.Event()
    rehash_finished = context.Event()
    reports = context.Queue()
    process = None
    try:
        _seed(database)
        former = database.indexes.index("by_email")
        process = context.Process(
            target=_triggered_rehash_participant,
            args=(
                str(root),
                descriptor_revalidation,
                ready,
                traversal_started,
                rehash_finished,
                reports,
            ),
        )
        process.start()
        assert ready.wait(CHILD_TIMEOUT_SECONDS), (
            "the spawned rehash participant never opened the database"
        )

        original_candidates = HashIndex._candidates_unchecked
        traversed_files: list[str] = []
        parked = False

        def park_old_traversal(
            index: HashIndex,
            wanted: bytes,
        ) -> tuple[object, ...]:
            nonlocal parked
            candidates = original_candidates(index, wanted)
            if index.name.lower() == "by_email":
                traversed_files.append(index.file)
                if index.file == former.file and not parked:
                    parked = True
                    traversal_started.set()
                    if not rehash_finished.wait(CHILD_TIMEOUT_SECONDS):
                        raise TimeoutError(
                            "the child never published the replacement generation"
                        )
            return candidates

        monkeypatch.setattr(HashIndex, "_candidates_unchecked", park_old_traversal)

        # Candidate traversal is already attached to the former file's pre-certificate when
        # the child rotates catalog authority.  Because rehash never rewrites that file, the
        # post-certificate may finish this statement from the coherent old generation.
        first = database.execute(
            "MATCH (p:Person) WHERE p.email = 'grace@example.test' RETURN p.id"
        )
        assert first.rows == ((2,),)
        child = _receive_child_report(process, reports)
        assert traversed_files == [former.file]
        assert child["file"] != former.file

        # A statement boundary refreshes the foreign commit token, catalog and runtime
        # authority.  The same logical predicate must now traverse only the new ACTIVE file.
        second = database.execute(
            "MATCH (p:Person) WHERE p.email = 'ada@example.test' RETURN p.id"
        )
        assert second.rows == ((1,),)
        assert traversed_files == [former.file, child["file"]]
        active = database.indexes.index("by_email")
        assert active.file == child["file"]
        assert active.active_nonce == child["nonce"]
        assert database.storage.exists(former.file)
        assert database.verify("all").findings == ()
    finally:
        traversal_started.set()
        rehash_finished.set()
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=10)
        database.close()
