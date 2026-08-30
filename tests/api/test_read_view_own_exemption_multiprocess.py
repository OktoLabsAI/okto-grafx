"""A real foreign participant is never hidden by the own-commit read-view exemption (F4)."""

from __future__ import annotations

import multiprocessing
import os
import queue as queue_module
import traceback
from pathlib import Path
from typing import Any

import pytest

from okto_grafx import connect

CHILD_TIMEOUT_SECONDS = 90


def _run_child(root: str, statements: tuple[str, ...], checkpoint: bool, reports: Any) -> None:
    """Open the shared database, run the statements, optionally checkpoint, and close."""
    database = None
    try:
        database = connect(root, page_size=512)
        for statement in statements:
            if statement.startswith(("MATCH", "CREATE (")):
                with database.begin("write") as txn:
                    txn.execute(statement)
            else:
                with database.begin("write") as txn:
                    txn.execute(statement)
        if checkpoint:
            database.checkpoint()
        reports.put({"ok": True, "pid": os.getpid()})
    except BaseException as failure:
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


def _spawn(root: Path, statements: tuple[str, ...], *, checkpoint: bool = False) -> None:
    context = multiprocessing.get_context("spawn")
    reports = context.Queue()
    process = context.Process(
        target=_run_child, args=(str(root), statements, checkpoint, reports)
    )
    process.start()
    try:
        report = reports.get(timeout=CHILD_TIMEOUT_SECONDS)
    except queue_module.Empty as failure:
        raise AssertionError("the spawned participant produced no report") from failure
    finally:
        process.join(timeout=CHILD_TIMEOUT_SECONDS)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
    assert report["ok"], report.get("traceback", report)
    assert report["pid"] != os.getpid()
    assert process.exitcode == 0


@pytest.mark.multiprocess
@pytest.mark.timeout(180)
def test_a_foreign_vector_space_lands_between_an_own_commit_and_the_next_view(
    tmp_path: Path,
) -> None:
    # create_space writes catalog.dat WITHOUT moving the commit token, so the token after A's
    # own commit stays A's own and the next view takes the exempted branch. The unfenced
    # catalog drop inside that branch is what lets A adopt the foreign space; the mutant that
    # exempts catalog.dat with the rest never sees it.
    root = tmp_path / "db"
    database = connect(root, page_size=512)
    try:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
        with database.begin("write") as txn:
            txn.execute("CREATE (:P {id: 1})")  # A's own commit: the next view is own
        assert not database.catalog.catalog.has_space("f4_space")

        _spawn(root, ("CREATE VECTOR SPACE f4_space {dimension: 4, metric: 'cosine'}",))

        with database.begin("read") as txn:  # the own view: catalog.dat still dropped
            assert txn is not None
        assert database.catalog.catalog.has_space("f4_space")
    finally:
        database.close()


@pytest.mark.multiprocess
@pytest.mark.timeout(180)
def test_a_foreign_checkpoint_with_redo_is_seen_by_a_long_lived_participant(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    database = connect(root, page_size=512)
    try:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
        with database.begin("write") as txn:
            txn.execute("CREATE (:P {id: 1})")
        assert database.execute("MATCH (p:P) RETURN p.id").rows == ((1,),)

        _spawn(root, ("CREATE (:P {id: 2})",), checkpoint=True)

        # The child's commit moved the token; the child's checkpoint redid and recycled. A's
        # next view is foreign and drops everything; the row must be there.
        rows = {row[0] for row in database.execute("MATCH (p:P) RETURN p.id").rows}
        assert rows == {1, 2}
    finally:
        database.close()


@pytest.mark.multiprocess
@pytest.mark.timeout(180)
def test_a_foreign_reopen_with_recovery_leaves_a_long_lived_participant_correct(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    database = connect(root, page_size=512)
    try:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
        with database.begin("write") as txn:
            txn.execute("CREATE (:P {id: 1})")

        # The child opens the shared database (startup recovery runs), commits and closes,
        # settling whatever it can. A long-lived A keeps answering correctly afterwards.
        _spawn(root, ("CREATE (:P {id: 3})",))

        rows = {row[0] for row in database.execute("MATCH (p:P) RETURN p.id").rows}
        assert rows == {1, 3}
        with database.begin("write") as txn:  # and A can still commit over it
            txn.execute("CREATE (:P {id: 4})")
        rows = {row[0] for row in database.execute("MATCH (p:P) RETURN p.id").rows}
        assert rows == {1, 3, 4}
    finally:
        database.close()
