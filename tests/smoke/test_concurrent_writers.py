"""Several OS processes appending to one table, through the public door, against a real directory.

WHY THIS FILE EXISTS. A suite of 7858 tests was green while three processes appending to one
table made the heap permanently unreadable in under a minute: ``MATCH (n:Item) RETURN count(*)``
in a fresh process, after every writer had exited cleanly and recovery had replayed, refused with
``corruption_detected``. Nothing had crashed. Every earlier concurrency test either drove the
engine below the public door, used a fresh short-lived process to read, or contended hard enough
that the writers serialised and the window closed -- so the regime that breaks was the one regime
nothing ran.

The cause is pinned by unit tests in ``tests/txn/test_chain_relink_regressions.py``, which fail
without the fix and pass with it. This file is the other half and a different kind of evidence:
it asserts the SYMPTOM is gone, in the shape a user meets it, with nothing stubbed. It is slow on
purpose. A cheaper version of it did not find the defect.

The assertions are the ones D1 exists for, and each is read from a process that did not do the
writing:

  * every commit that returned is findable afterwards -- no acknowledged write is lost;
  * no row is stored twice -- no acknowledged write is duplicated;
  * no writer dies, and no refusal escapes as anything but a retryable one;
  * the heap is readable and ``verify()`` is clean, live and again after a cold reopen.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from okto_grafx import connect

CHILD = Path(__file__).resolve().parent / "smoke_child.py"

WRITERS: int = 3
"""Processes appending at once. Two is enough to contend; three keeps a third participant
advancing the table's extent while two are contending, which is the interleaving that turned a
dangling link into an unreadable heap."""

ROUNDS: int = 16
PER_ROUND: int = 8
CHILD_BUDGET: float = 600.0


def _run_writers(root: Path) -> tuple[list[int], int]:
    """Start every writer at once, wait for all of them, and return what they acknowledged."""
    running = [
        subprocess.Popen(
            [sys.executable, str(CHILD), str(root), str(slot), str(ROUNDS), str(PER_ROUND)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for slot in range(1, WRITERS + 1)
    ]
    acknowledged: list[int] = []
    conflicts = 0
    failures: list[str] = []
    for child in running:
        out, err = child.communicate(timeout=CHILD_BUDGET)
        if not out.strip():
            failures.append(f"writer produced no report; stderr={err[-600:]}")
            continue
        report = json.loads(out.strip().splitlines()[-1])
        if report["failure"]:
            failures.append(f"writer {report['slot']} died:\n{report['failure']}")
        acknowledged.extend(report["acknowledged"])
        conflicts += report["conflicts"]
    assert not failures, "\n\n".join(failures)
    return acknowledged, conflicts


@pytest.mark.slow
@pytest.mark.multiprocess
@pytest.mark.timeout(900, method="thread")
def test_concurrent_writers_leave_a_readable_heap_and_lose_nothing(tmp_path: Path) -> None:
    root = tmp_path / "db"
    database = connect(str(root))
    with database.begin("write") as txn:
        txn.execute(
            "CREATE NODE TABLE Item(id INT64, owner INT64, body STRING, PRIMARY KEY(id))"
        )
    database.close()

    acknowledged, conflicts = _run_writers(root)
    assert len(acknowledged) == WRITERS * ROUNDS * PER_ROUND

    # A process that did none of the writing, reading what the device holds.
    reader = connect(str(root))
    stored = [row[0] for row in reader.execute("MATCH (i:Item) RETURN i.id").rows]
    live_findings = reader.verify("all").findings
    reader.close()

    reopened = connect(str(root))
    reopened_rows = [row[0] for row in reopened.execute("MATCH (i:Item) RETURN i.id").rows]
    reopened_findings = reopened.verify("all").findings
    reopened.close()

    missing = sorted(set(acknowledged) - set(stored))
    assert not missing, (
        f"{len(missing)} acknowledged rows are not in the database: {missing[:10]} "
        f"({conflicts} retryable conflicts along the way)"
    )
    assert len(stored) == len(set(stored)), "a row is stored more than once"
    assert sorted(stored) == sorted(reopened_rows), "the reopened database disagrees"
    assert [finding.kind for finding in live_findings] == []
    assert [finding.kind for finding in reopened_findings] == []
