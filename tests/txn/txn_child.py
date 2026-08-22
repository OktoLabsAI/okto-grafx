"""The body a spawned participant runs in the multi-process tests.

Amendment A94: a spawned interpreter resolves ``okto_grafx`` from whatever its own ``sys.path``
says, which on a shared checkout may be an editable install pointing at a tree another component
is writing. Both paths are therefore passed in by the parent and inserted at the FRONT here,
before anything of the package is imported.
"""

from __future__ import annotations

import sys
import time
from typing import Any


def run(
    *,
    source_path: str,
    helper_path: str,
    root: str,
    owner_id: str,
    page_index: int,
    table_id: int,
    key: bytes,
    payload: bytes,
    ready_marker: str,
    peers: tuple[str, ...],
    partitions_per_table: int,
    retry_on_conflict: bool,
    budget_seconds: float = 30.0,
) -> dict[str, Any]:
    """Open a database, stage one page, wait for every peer, commit, and report what happened.

    EVERY import is inside the guard, including the ones that bring the package in. They used to
    sit above it, and the consequence was measured rather than imagined: while a sibling
    component was mid-write in ``src/``, these children failed to import, exited 1, and reported
    NOTHING -- so the parent could say only that a participant had died, and nine multi-process
    tests read as unattributable failures of this component instead of as the sibling churn they
    were (A94). A child that cannot import is now a child that says so.
    """
    for path in (source_path, helper_path):
        if path not in sys.path:
            sys.path.insert(0, path)

    outcome: dict[str, Any] = {"owner_id": owner_id, "committed": False, "conflicts": 0}
    try:
        import pathlib

        from okto_grafx.domain.errors import GrafxError, GrafxWriteConflict
        from txn_support import build_stack, make_page_image

        outcome["resolved_from"] = _package_location()
        stack = build_stack(
            pathlib.Path(root),
            owner_id=owner_id,
            partitions_per_table=partitions_per_table,
            commit_lock_timeout=30.0,
        )
        txn = stack.manager.begin("write")
        txn.stage_page_image(
            "heap.dat",
            page_index,
            make_page_image(stack.codec, [payload], page_index=page_index),
        )
        txn.note_write(stack.manager.partition_of(table_id, key))
        outcome["snapshot"] = txn.snapshot.read_lsn
        pathlib.Path(ready_marker).write_text("ready", encoding="utf-8")
        _wait_for(peers, budget_seconds)
        try:
            report = stack.manager.commit(txn)
            outcome["committed"] = True
            outcome["csn"] = report.csn
            outcome["durable"] = report.durable
        except GrafxWriteConflict as conflict:
            outcome["conflicts"] = 1
            outcome["retryable"] = conflict.retryable
            outcome["details_retryable"] = conflict.details.get("retryable", conflict.retryable)
            if retry_on_conflict:
                successor = stack.manager.retry(txn)
                successor.stage_page_image(
                    "heap.dat",
                    page_index,
                    make_page_image(stack.codec, [payload], page_index=page_index),
                )
                successor.note_write(stack.manager.partition_of(table_id, key))
                report = stack.manager.commit(successor)
                outcome["committed"] = True
                outcome["csn"] = report.csn
                outcome["retried_snapshot"] = successor.snapshot.read_lsn
        stack.manager.close()
    except GrafxError as failure:
        outcome["error"] = f"{type(failure).__name__}: {failure.message}"
        outcome["traceback"] = _trace()
    except BaseException as failure:  # noqa: BLE001 - the parent must see why a child died
        outcome["error"] = f"{type(failure).__name__}: {failure}"
        outcome["traceback"] = _trace()
    return outcome


def _trace() -> str:
    """Return the traceback of the failure being handled, so the reason travels with the name."""
    import traceback

    return traceback.format_exc()


def _wait_for(markers: tuple[str, ...], budget_seconds: float) -> None:
    """Wait until every peer has said it is ready, so the commits really do overlap."""
    import pathlib

    deadline = time.monotonic() + budget_seconds
    while time.monotonic() < deadline:
        if all(pathlib.Path(marker).exists() for marker in markers):
            return
        time.sleep(0.002)
    raise TimeoutError("a peer never reported ready")


def _package_location() -> str:
    """Return where this child resolved the package from, so a wrong tree is visible (A94)."""
    import okto_grafx

    return str(okto_grafx.__file__)


def entry(queue: Any, keywords: dict[str, Any]) -> None:
    """Run the body and put its report on the queue, so the parent always gets an answer.

    Nothing may escape this function. A child that raises here dies with its traceback on a
    stream the parent does not read, and the parent is left with an exit code and no reason --
    which is the difference between a defect it can name and one it can only guess at.
    """
    try:
        report = run(**keywords)
    except BaseException as failure:  # noqa: BLE001 - the parent must see why a child died
        import traceback

        report = {
            "owner_id": keywords.get("owner_id", "unknown"),
            "committed": False,
            "conflicts": 0,
            "error": f"{type(failure).__name__}: {failure}",
            "traceback": traceback.format_exc(),
        }
    try:
        queue.put(report)
    except BaseException:  # noqa: BLE001 - a queue that cannot carry the answer is the last word
        return
