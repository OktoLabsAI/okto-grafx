"""Two real processes over one database (SPEC-M1 FR-3, AC-1, AC-2, AC-3; TS-1, TS-2, TS-3).

Everything else in this suite drives interleavings inside one interpreter. These tests answer the
question that technique cannot: does the protocol hold when the two writers share nothing but the
file system -- no memory, no locks, and above all no clock.

Each scenario runs several rounds, because a race that passes once proves little.
"""

from __future__ import annotations

import multiprocessing
import pathlib
import queue as queue_module
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from okto_grafx.domain.txn import CommitPayload, WalRecordType
from txn_support import LogWal, build_stack, read_page_payloads

import txn_child

HERE: str = str(Path(__file__).resolve().parent)
SOURCE: str = str(Path(__file__).resolve().parents[2] / "src")
ROUNDS: int = 4
CHILD_BUDGET: float = 90.0
TEST_TIMEOUT_SECONDS: int = 300
PARTITIONS: int = 8
"""Each test carries its own bound, as pyproject.toml prescribes for anything slower than the
project default. Spawning two interpreters and committing through a real device costs more than
sixty seconds on a loaded machine, and a test that relied on the caller appending an option would
die with no junit report at all (A75.2, A92)."""


def _spawn(root: Path, plans: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run every plan in its own interpreter and return what each one reported."""
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    markers = tuple(str(root / f"{plan['owner_id']}.ready") for plan in plans)
    processes = []
    for plan, marker in zip(plans, markers, strict=True):
        keywords = dict(plan)
        keywords.setdefault("partitions_per_table", PARTITIONS)
        keywords.update(
            source_path=SOURCE,
            helper_path=HERE,
            root=str(root),
            ready_marker=marker,
            peers=markers,
        )
        process = context.Process(target=txn_child.entry, args=(queue, keywords))
        process.start()
        processes.append(process)
    reports: list[dict[str, Any]] = []
    deadline = time.monotonic() + CHILD_BUDGET
    while len(reports) < len(processes) and time.monotonic() < deadline:
        try:
            reports.append(queue.get(timeout=0.5))
        except queue_module.Empty:
            # A child that cannot even import reports nothing, and waiting for it would spend
            # the whole test budget and take the session down with no junit (A88, A92). Its exit
            # code is the evidence, and it is available immediately.
            dead = [
                (process.name, process.exitcode)
                for process in processes
                if process.exitcode not in (None, 0)
            ]
            assert not dead, f"a participant died before reporting: {dead}"
    for process in processes:
        process.join(timeout=CHILD_BUDGET)
    still_running = [process.name for process in processes if process.is_alive()]
    for process in processes:
        if process.is_alive():
            process.terminate()
    assert not still_running, f"these participants never finished: {still_running}"
    assert len(reports) == len(processes), (
        f"only {len(reports)} of {len(processes)} participants reported"
    )
    for process in processes:
        assert process.exitcode == 0, f"a participant died with exit code {process.exitcode}"
    for report in reports:
        assert "error" not in report, (
            f"{report.get('owner_id')}: {report['error']}\n{report.get('traceback', '')}"
        )
    return reports


def _prepare(root: Path) -> None:
    """Create the database files a participant expects to find, then let go of them."""
    build_stack(root, partitions_per_table=PARTITIONS, owner_id="setup").manager.close()


@pytest.mark.multiprocess
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_two_processes_writing_disjoint_partitions_both_commit(
    tmp_path: Path, round_number: int
) -> None:
    """AC-1 and TS-1: both confirm, both effects survive a reopen, nothing is lost."""
    root = tmp_path / f"disjoint-{round_number}"
    root.mkdir(parents=True, exist_ok=True)
    _prepare(root)
    reports = _spawn(
        root,
        [
            {
                "owner_id": "writer-a",
                "page_index": 3,
                "table_id": 1,
                "key": b"alpha",
                "payload": b"from-a",
                "retry_on_conflict": False,
            },
            {
                "owner_id": "writer-b",
                "page_index": 4,
                "table_id": 2,
                "key": b"beta",
                "payload": b"from-b",
                "retry_on_conflict": False,
            },
        ],
    )
    assert [report["committed"] for report in reports] == [True, True]
    assert sum(report["conflicts"] for report in reports) == 0
    assert len({report["csn"] for report in reports}) == 2

    reopened = build_stack(root, partitions_per_table=PARTITIONS, owner_id="third")
    assert read_page_payloads(reopened.pool, "heap.dat", 3) == (b"from-a",)
    assert read_page_payloads(reopened.pool, "heap.dat", 4) == (b"from-b",)
    assert reopened.manager.published_lsn() == max(report["csn"] for report in reports)
    commits = [
        record
        for record in LogWal(reopened.storage).records()
        if record.record_type == WalRecordType.COMMIT
    ]
    assert len(commits) == 2
    assert len({record.lsn for record in commits}) == 2


@pytest.mark.multiprocess
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_two_processes_writing_one_partition_serialise_through_a_conflict(
    tmp_path: Path, round_number: int
) -> None:
    """AC-2 and TS-2: exactly one confirms, the loser is told retryable, and its retry confirms."""
    root = tmp_path / f"same-{round_number}"
    root.mkdir(parents=True, exist_ok=True)
    _prepare(root)
    reports = _spawn(
        root,
        [
            {
                "owner_id": "writer-a",
                "page_index": 3,
                "table_id": 1,
                "key": b"contended",
                "payload": b"from-a",
                "retry_on_conflict": True,
            },
            {
                "owner_id": "writer-b",
                "page_index": 4,
                "table_id": 1,
                "key": b"contended",
                "payload": b"from-b",
                "retry_on_conflict": True,
            },
        ],
    )
    assert [report["committed"] for report in reports] == [True, True]
    conflicts = sum(report["conflicts"] for report in reports)
    assert conflicts == 1, (
        "both writers opened before either committed, so exactly one must have been refused; "
        "zero would mean a lost update and two would mean neither made progress"
    )
    for report in reports:
        if report["conflicts"]:
            assert report["retryable"] is True
            assert report["details_retryable"] is True
            assert report["retried_snapshot"] > report["snapshot"]

    reopened = build_stack(root, partitions_per_table=PARTITIONS, owner_id="third")
    assert read_page_payloads(reopened.pool, "heap.dat", 3) == (b"from-a",)
    assert read_page_payloads(reopened.pool, "heap.dat", 4) == (b"from-b",)
    records = LogWal(reopened.storage).records()
    commits = [record for record in records if record.record_type == WalRecordType.COMMIT]
    assert len(commits) == 2
    ordered = sorted(commits, key=lambda record: record.lsn)
    later = CommitPayload.decode(ordered[1].payload)
    assert later.snapshot_lsn >= ordered[0].lsn, (
        "the second commit must have validated against a snapshot at or after the first"
    )


@pytest.mark.multiprocess
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_a_reader_in_a_third_process_never_sees_a_partial_batch(tmp_path: Path) -> None:
    """AC-3 and TS-3: a snapshot taken while two writers commit is complete or not there at all."""
    root = tmp_path / "reader"
    root.mkdir(parents=True, exist_ok=True)
    _prepare(root)
    observer = build_stack(root, partitions_per_table=PARTITIONS, owner_id="observer")
    before = observer.manager.begin("read")
    reports = _spawn(
        root,
        [
            {
                "owner_id": "writer-a",
                "page_index": 3,
                "table_id": 1,
                "key": b"alpha",
                "payload": b"from-a",
                "retry_on_conflict": False,
            },
            {
                "owner_id": "writer-b",
                "page_index": 4,
                "table_id": 2,
                "key": b"beta",
                "payload": b"from-b",
                "retry_on_conflict": False,
            },
        ],
    )
    for report in reports:
        assert before.snapshot.visible(report["csn"], 0) is False
    observer.manager.commit(before)
    after = observer.manager.begin("read")
    for report in reports:
        assert after.snapshot.visible(report["csn"], 0) is True
    assert read_page_payloads(observer.pool, "heap.dat", 3) == (b"from-a",)
    assert read_page_payloads(observer.pool, "heap.dat", 4) == (b"from-b",)
    observer.manager.close()


def test_the_child_helper_pins_its_own_import_path() -> None:
    """A94: a spawned interpreter must not resolve the package through a shared editable install."""
    source = Path(txn_child.__file__).read_text(encoding="utf-8")
    assert "sys.path.insert(0, path)" in source
    assert SOURCE in sys.path or str(Path(SOURCE)) in sys.path


def test_every_test_in_this_module_carries_its_own_timeout() -> None:
    """A structural guard, because the hazard is decided by collection order, not by the author.

    A test here spawns interpreters and commits through a real device, which costs more than the
    project's sixty-second default on a loaded machine. When that bound fires under the thread
    method it hard-exits and NO junit is written, so the whole package reads as UNMEASURED
    rather than as red or green (A75.2) -- which is how nine real failures stayed invisible once.

    Whichever slow test runs first pays that price, so a later addition without a bound would
    reintroduce the problem silently. This fails instead.

    The spawning tests are found by looking for a CALL to the spawn helper in the parsed tree,
    not for its name in the text: a guard that read the text would flag itself, which is how the
    first version of it failed.
    """
    import ast

    module = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    unbounded = []
    for node in module.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        spawns = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_spawn"
            for call in ast.walk(node)
        )
        if not spawns:
            continue
        bounded = any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "timeout"
            for decorator in node.decorator_list
        )
        if not bounded:
            unbounded.append(node.name)
    assert unbounded == [], (
        f"these tests spawn interpreters with no bound of their own: {unbounded}"
    )


@pytest.mark.multiprocess
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_a_participant_that_cannot_start_reports_why_rather_than_only_dying(
    tmp_path: Path,
) -> None:
    """The bench must name the reason a child failed, not just that one did.

    This is the lesson of nine failures that read as "a participant died before reporting". The
    child's imports sat above its guard, so while a sibling component was mid-write in ``src/``
    the children could not import, exited 1, and said nothing -- and the parent could only report
    an exit code, which is indistinguishable from a defect in this component (A94). Every failure
    in a child is now carried back with its type and its traceback.
    """
    root = tmp_path / "unstartable"
    root.mkdir(parents=True, exist_ok=True)
    _prepare(root)
    with pytest.raises(AssertionError) as raised:
        _spawn(
            root,
            [
                {
                    "owner_id": "writer-a",
                    "page_index": 3,
                    "table_id": 1,
                    "key": b"alpha",
                    "payload": b"from-a",
                    "retry_on_conflict": False,
                    # No partition count a hash can be taken modulo, so the participant refuses
                    # to build at all -- inside the guard, which is the whole point.
                    "partitions_per_table": 0,
                }
            ],
        )
    message = str(raised.value)
    assert "writer-a" in message
    assert "GrafxConfigurationError" in message
    assert "partitions_per_table" in message
    assert "Traceback" in message, "the reason must travel with the report, not only its name"
