"""A real spawned participant cannot move page 0 invisibly through an exact lookup."""

from __future__ import annotations

import multiprocessing
import os
import queue as queue_module
import traceback
from pathlib import Path
from typing import Any

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.engine.index_manager import HashIndex, primary_key_index_name

CHILD_TIMEOUT_SECONDS = 90


def _mark_stale_in_spawned_participant(
    root: str,
    traversal_started: Any,
    mark_published: Any,
    reports: Any,
) -> None:
    """Wait until the parent collected candidates, then durably mark its PK index stale."""
    database = None
    try:
        database = connect(root, page_size=512)
        if not traversal_started.wait(CHILD_TIMEOUT_SECONDS):
            raise TimeoutError("the parent never entered exact candidate traversal")
        database._indexes.index(primary_key_index_name("P")).mark_stale(
            "spawned participant changed the exact view"
        )
        mark_published.set()
        reports.put({"ok": True, "pid": os.getpid()})
    except BaseException as failure:
        mark_published.set()
        reports.put(
            {
                "ok": False,
                "error": repr(failure),
                "traceback": traceback.format_exc(),
                "pid": os.getpid(),
            }
        )
    finally:
        if database is not None:
            database.close()


@pytest.mark.multiprocess
@pytest.mark.timeout(180)
def test_spawned_stale_mark_between_traversal_and_validation_forces_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "db"
    database = connect(root, page_size=512)
    context = multiprocessing.get_context("spawn")
    traversal_started = context.Event()
    mark_published = context.Event()
    reports = context.Queue()
    process = None
    try:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
        with database.begin("write") as txn:
            txn.execute("CREATE (:P {id: 1})")
        assert database.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id").rows == ((1,),)

        original = HashIndex._candidates_unchecked
        parked = False

        def park_after_candidates(self: HashIndex, wanted: bytes):
            nonlocal parked
            candidates = original(self, wanted)
            if self.name == primary_key_index_name("P") and not parked:
                parked = True
                traversal_started.set()
                if not mark_published.wait(CHILD_TIMEOUT_SECONDS):
                    raise TimeoutError("the child never published its stale mark")
            return candidates

        monkeypatch.setattr(HashIndex, "_candidates_unchecked", park_after_candidates)
        process = context.Process(
            target=_mark_stale_in_spawned_participant,
            args=(str(root), traversal_started, mark_published, reports),
        )
        process.start()

        with pytest.raises(GrafxIndexError) as refused:
            database.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id")
        assert refused.value.details["field"] == "index_view_unavailable"
        assert parked

        try:
            report = reports.get(timeout=CHILD_TIMEOUT_SECONDS)
        except queue_module.Empty as failure:
            raise AssertionError("the spawned participant produced no report") from failure
        assert report["ok"], report.get("traceback", report)
        assert report["pid"] != os.getpid()
    finally:
        traversal_started.set()
        mark_published.set()
        if process is not None:
            process.join(timeout=CHILD_TIMEOUT_SECONDS)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
            assert process.exitcode == 0
        database.close()


@pytest.mark.multiprocess
@pytest.mark.timeout(180)
def test_spawned_stale_mark_between_two_lookups_is_caught_by_the_post_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F4 for the carried certificate (CQ-3/QW-2): a REAL spawned participant durably marks the
    # PK index stale between two exact lookups of this process. The second lookup may begin
    # from the certificate the first lookup's post-read carried, so the foreign mark is caught
    # by the fresh post-read and the retry refuses from the device. Never a short answer.
    root = tmp_path / "db"
    database = connect(root, page_size=512)
    context = multiprocessing.get_context("spawn")
    first_lookup_done = context.Event()
    mark_published = context.Event()
    reports = context.Queue()
    process = None
    try:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
        with database.begin("write") as txn:
            txn.execute("CREATE (:P {id: 1})")
        assert database.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id").rows == ((1,),)

        original = HashIndex.finish_exact_read
        outcomes: list[bool] = []

        def recording(self: HashIndex, before: object, required_lsn: int) -> bool:
            stable = original(self, before, required_lsn)
            if self.name == primary_key_index_name("P"):
                outcomes.append(stable)
            return stable

        monkeypatch.setattr(HashIndex, "finish_exact_read", recording)
        process = context.Process(
            target=_mark_stale_in_spawned_participant,
            args=(str(root), first_lookup_done, mark_published, reports),
        )
        process.start()
        first_lookup_done.set()
        if not mark_published.wait(CHILD_TIMEOUT_SECONDS):
            raise TimeoutError("the child never published its stale mark")
        try:
            report = reports.get(timeout=CHILD_TIMEOUT_SECONDS)
        except queue_module.Empty as failure:
            raise AssertionError("the spawned participant produced no report") from failure
        assert report["ok"], report.get("traceback", report)
        assert report["pid"] != os.getpid()

        with pytest.raises(GrafxIndexError) as refused:
            database.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id")
        assert refused.value.details["field"] == "index_view_unavailable"
        # The carried pre-certificate let one traversal run; its post-read saw the mark.
        assert outcomes == [False]
    finally:
        first_lookup_done.set()
        mark_published.set()
        if process is not None:
            process.join(timeout=CHILD_TIMEOUT_SECONDS)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
            assert process.exitcode == 0
        database.close()
