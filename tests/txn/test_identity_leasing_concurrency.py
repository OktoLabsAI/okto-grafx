"""Bounded cross-process evidence for durable row-identity leasing (CN-1).

The unit tests in ``test_identity_leasing.py`` pin the local planning rules.  These tests cover
the property an in-process fake cannot prove: two independent participants reserve through the
same durable page-zero floor without sharing their process-local lease cache.
"""

from __future__ import annotations

import multiprocessing
import queue as queue_module
import sys
import time
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

HERE: str = str(Path(__file__).resolve().parent)
SOURCE: str = str(Path(__file__).resolve().parents[2] / "src")
IDENTITY_LEASE_SIZE: int = 4
CHILD_BUDGET_SECONDS: float = 90.0
TEST_TIMEOUT_SECONDS: int = 240


def _table(table_id: int = 1, name: str = "Person") -> Any:
    from okto_grafx.domain.model.schema import ColumnDef, TableDef
    from okto_grafx.domain.model.value import ValueType

    return TableDef(
        table_id=table_id,
        name=name,
        kind="node",
        columns=(ColumnDef(name="value", type=ValueType.STRING, nullable=False),),
        primary_key=None,
        from_table=None,
        to_table=None,
    )


def _register(stack: Any, *tables: Any) -> None:
    for table in tables:
        stack.catalog.catalog.add_table(table)
    stack.catalog.save()
    stack.pool.flush(stack.catalog.file)


def _insert(
    stack: Any,
    table: Any,
    value: str,
    *,
    key: bytes,
    record_id: int | None = None,
) -> Any:
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (value,), record_id=record_id)
    txn.note_write(stack.manager.partition_of(table.table_id, key))
    stack.manager.commit(txn)
    return txn


def _identities(stack: Any, table: Any) -> list[int]:
    return sorted(version.record_id for _reference, version in stack.heap.scan_all(table))


def _distinct_partition_keys(manager: Any, table_id: int) -> tuple[bytes, bytes]:
    first = b"identity-a"
    first_partition = manager.partition_of(table_id, first)
    for suffix in range(1, 256):
        candidate = f"identity-b-{suffix}".encode()
        if manager.partition_of(table_id, candidate) != first_partition:
            return first, candidate
    raise AssertionError("the configured partitioner produced no two distinct test buckets")


def _wait_for(markers: tuple[str, ...], budget_seconds: float) -> None:
    deadline = time.monotonic() + budget_seconds
    while time.monotonic() < deadline:
        if all(Path(marker).exists() for marker in markers):
            return
        time.sleep(0.002)
    raise TimeoutError("a peer never reported ready")


def _cached_reservation(stack: Any, table_id: int) -> tuple[int, int]:
    """Recover the full just-reserved interval from its unspent local suffix.

    Every child stages one implicit insert and the configured lease has four slots.  The cache
    therefore retains a suffix even if the user transaction later meets the legitimate physical
    page conflict and retries.  Reading this private component state is intentional: visible row
    ids prove uniqueness, while the suffix also proves the two durable reservations themselves
    were disjoint.
    """
    lease = stack.manager._identity_leases.get(table_id)
    if lease is None:
        raise AssertionError("the one-row refill retained no observable local lease suffix")
    return lease.stop - IDENTITY_LEASE_SIZE, lease.stop


def _child_run(
    *,
    source_path: str,
    helper_path: str,
    root: str,
    owner_id: str,
    table_id: int,
    table_name: str,
    value: str,
    key: bytes,
    ready_marker: str,
    peers: tuple[str, ...],
) -> dict[str, Any]:
    for path in (source_path, helper_path):
        if path not in sys.path:
            sys.path.insert(0, path)

    from okto_grafx.domain.errors import GrafxWriteConflict
    from txn_support import build_stack

    stack = build_stack(
        Path(root),
        owner_id=owner_id,
        identity_lease_size=IDENTITY_LEASE_SIZE,
        commit_lock_timeout=30.0,
    )
    table = _table(table_id, table_name)
    floor_before = stack.heap.next_record_id(table)
    txn = stack.manager.begin("write")
    txn.stage_row_insert(table, (value,))
    txn.note_write(stack.manager.partition_of(table_id, key))
    Path(ready_marker).write_text("ready", encoding="utf-8")
    _wait_for(peers, CHILD_BUDGET_SECONDS)

    conflicts = 0
    reservation: tuple[int, int] | None = None
    committed = txn
    try:
        report = stack.manager.commit(txn)
    except GrafxWriteConflict as conflict:
        conflicts = 1
        if not conflict.retryable:
            raise
        reservation = _cached_reservation(stack, table_id)
        committed = stack.manager.retry(txn)
        committed.stage_row_insert(table, (value,))
        committed.note_write(stack.manager.partition_of(table_id, key))
        report = stack.manager.commit(committed)

    if reservation is None:
        reservation = _cached_reservation(stack, table_id)
    used_ids = [stack.heap.read(reference).record_id for reference in committed.row_refs]
    outcome = {
        "owner_id": owner_id,
        "committed": True,
        "conflicts": conflicts,
        "csn": report.csn,
        "floor_before": floor_before,
        "floor_after": stack.heap.next_record_id(table),
        "reservation": reservation,
        "used_ids": used_ids,
    }
    stack.manager.close()
    return outcome


def _child_entry(queue: Any, keywords: dict[str, Any]) -> None:
    try:
        report = _child_run(**keywords)
    except BaseException as failure:  # noqa: BLE001 - the parent needs the child traceback
        report = {
            "owner_id": keywords.get("owner_id", "unknown"),
            "committed": False,
            "error": f"{type(failure).__name__}: {failure}",
            "traceback": traceback.format_exc(),
        }
    try:
        queue.put(report)
    except BaseException:  # noqa: BLE001 - there is nowhere else to report a broken queue
        return


def _crash_after_durable_reservation_entry(
    source_path: str,
    root: str,
    durable_reservation: Any,
    release: Any,
) -> None:
    """Stop after publishing a lease floor, before returning to the user-row commit.

    This function is a top-level ``spawn`` target so the test follows the same Windows process
    boundary as production.  The class patch exists only inside the disposable child process.
    The wrapped method returns only after the reservation WAL barrier, page apply and commit-state
    publication, while its caller cannot start writing the user row until this wrapper returns.
    """
    if source_path not in sys.path:
        sys.path.insert(0, source_path)

    from okto_grafx import connect
    from okto_grafx.engine.txn_manager import TransactionManager

    commit_floor = TransactionManager._commit_identity_floor_plan

    def pause_after_publish(manager: Any, *args: Any, **kwargs: Any) -> Any:
        result = commit_floor(manager, *args, **kwargs)
        durable_reservation.set()
        if not release.wait(CHILD_BUDGET_SECONDS):
            raise TimeoutError("the parent never terminated or released the crash participant")
        return result

    TransactionManager._commit_identity_floor_plan = pause_after_publish
    with connect(Path(root), identity_lease_size=IDENTITY_LEASE_SIZE) as database:
        with database.begin("write") as transaction:
            transaction.execute("CREATE (:Person {value: 'must-not-survive'})")


def _spawn(root: Path, plans: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    markers = tuple(str(root / f"{plan['owner_id']}.ready") for plan in plans)
    processes = []
    for plan, marker in zip(plans, markers, strict=True):
        keywords = dict(plan)
        keywords.update(
            source_path=SOURCE,
            helper_path=HERE,
            root=str(root),
            ready_marker=marker,
            peers=markers,
        )
        process = context.Process(target=_child_entry, args=(queue, keywords))
        process.start()
        processes.append(process)

    reports: list[dict[str, Any]] = []
    deadline = time.monotonic() + CHILD_BUDGET_SECONDS
    while len(reports) < len(processes) and time.monotonic() < deadline:
        try:
            reports.append(queue.get(timeout=0.5))
        except queue_module.Empty:
            dead = [
                (process.name, process.exitcode)
                for process in processes
                if process.exitcode not in (None, 0)
            ]
            assert not dead, f"a participant died before reporting: {dead}"

    for process in processes:
        process.join(timeout=CHILD_BUDGET_SECONDS)
    still_running = [process.name for process in processes if process.is_alive()]
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)
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
    return sorted(reports, key=lambda report: report["owner_id"])


@pytest.mark.multiprocess
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS, method="thread")
def test_two_processes_refill_one_table_with_disjoint_ranges_and_monotone_floor(
    tmp_path: Path,
) -> None:
    from txn_support import build_stack

    root = tmp_path / "same-table"
    root.mkdir()
    setup = build_stack(root, owner_id="setup", identity_lease_size=IDENTITY_LEASE_SIZE)
    table = _table()
    _register(setup, table)
    _insert(setup, table, "seed", key=b"seed")
    assert setup.heap.next_record_id(table) == 2
    first_key, second_key = _distinct_partition_keys(setup.manager, table.table_id)
    setup.manager.close()

    reports = _spawn(
        root,
        [
            {
                "owner_id": "writer-a",
                "table_id": table.table_id,
                "table_name": table.name,
                "value": "from-a",
                "key": first_key,
            },
            {
                "owner_id": "writer-b",
                "table_id": table.table_id,
                "table_name": table.name,
                "value": "from-b",
                "key": second_key,
            },
        ],
    )

    assert all(report["committed"] for report in reports)
    assert all(report["floor_before"] == 2 for report in reports)
    ranges = sorted(tuple(report["reservation"]) for report in reports)
    assert ranges == [(2, 6), (6, 10)]
    assert ranges[0][1] <= ranges[1][0]
    used = [identity for report in reports for identity in report["used_ids"]]
    assert len(used) == len(set(used)) == 2
    for report in reports:
        start, stop = report["reservation"]
        assert all(start <= identity < stop for identity in report["used_ids"])

    reopened = build_stack(root, owner_id="observer", identity_lease_size=IDENTITY_LEASE_SIZE)
    assert reopened.heap.next_record_id(table) == 10
    assert reopened.heap.next_record_id(table) >= max(report["floor_after"] for report in reports)
    assert _identities(reopened, table) == sorted([1, *used])
    reopened.manager.close()


@pytest.mark.multiprocess
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS, method="thread")
def test_two_processes_refilling_distinct_tables_do_not_false_conflict_on_page_zero(
    tmp_path: Path,
) -> None:
    from txn_support import build_stack

    root = tmp_path / "distinct-tables"
    root.mkdir()
    setup = build_stack(root, owner_id="setup", identity_lease_size=IDENTITY_LEASE_SIZE)
    left = _table(1, "Left")
    right = _table(2, "Right")
    _register(setup, left, right)
    _insert(setup, left, "left-seed", key=b"left-seed")
    _insert(setup, right, "right-seed", key=b"right-seed")
    assert setup.heap.next_record_id(left) == setup.heap.next_record_id(right) == 2
    setup.manager.close()

    reports = _spawn(
        root,
        [
            {
                "owner_id": "writer-left",
                "table_id": left.table_id,
                "table_name": left.name,
                "value": "left-next",
                "key": b"left-next",
            },
            {
                "owner_id": "writer-right",
                "table_id": right.table_id,
                "table_name": right.name,
                "value": "right-next",
                "key": b"right-next",
            },
        ],
    )

    assert all(report["committed"] for report in reports)
    assert sum(report["conflicts"] for report in reports) == 0
    assert all(tuple(report["reservation"]) == (2, 6) for report in reports)
    assert all(report["used_ids"] == [2] for report in reports)

    reopened = build_stack(root, owner_id="observer", identity_lease_size=IDENTITY_LEASE_SIZE)
    assert reopened.heap.next_record_id(left) == 6
    assert reopened.heap.next_record_id(right) == 6
    assert _identities(reopened, left) == [1, 2]
    assert _identities(reopened, right) == [1, 2]
    reopened.manager.close()


@pytest.mark.multiprocess
@pytest.mark.timeout(TEST_TIMEOUT_SECONDS, method="thread")
def test_crash_after_durable_reservation_burns_range_across_recovery(
    tmp_path: Path,
) -> None:
    from okto_grafx import connect

    root = tmp_path / "crash-after-reservation"
    with connect(root, identity_lease_size=IDENTITY_LEASE_SIZE) as setup:
        with setup.begin("write") as schema:
            schema.execute("CREATE NODE TABLE Person(value STRING)")
        with setup.begin("write") as seed:
            seed.execute("CREATE (:Person {value: 'seed'})")
        table = setup.catalog.catalog.table("Person")
        assert setup._heap.next_record_id(table) == 2
        assert sorted(
            version.record_id for _reference, version in setup._heap.scan_all(table)
        ) == [1]

    context = multiprocessing.get_context("spawn")
    durable_reservation = context.Event()
    release = context.Event()
    process = context.Process(
        target=_crash_after_durable_reservation_entry,
        args=(SOURCE, str(root), durable_reservation, release),
    )
    process.start()
    try:
        assert durable_reservation.wait(CHILD_BUDGET_SECONDS), (
            "the child never reached the durable reservation boundary"
        )
        assert process.is_alive(), "the child left the reservation boundary before the crash"
        process.kill()
        process.join(timeout=CHILD_BUDGET_SECONDS)
        assert not process.is_alive(), "the crash participant did not terminate"
        assert process.exitcode not in (None, 0), (
            f"the crash participant exited cleanly with {process.exitcode}"
        )
    finally:
        if process.is_alive():
            release.set()
            process.terminate()
            process.join(timeout=5.0)

    with connect(root, identity_lease_size=IDENTITY_LEASE_SIZE) as recovered:
        table = recovered.catalog.catalog.table("Person")
        assert recovered.recovery_report.records_replayed > 0
        assert recovered._heap.next_record_id(table) == 6
        assert sorted(
            version.record_id for _reference, version in recovered._heap.scan_all(table)
        ) == [1]

        with recovered.begin("write") as survivor:
            survivor.execute("CREATE (:Person {value: 'after-crash'})")

        assert recovered._heap.next_record_id(table) == 10
        assert sorted(
            version.record_id for _reference, version in recovered._heap.scan_all(table)
        ) == [1, 6]
        assert recovered.execute("MATCH (p:Person) RETURN p.value").rows == (
            ("seed",),
            ("after-crash",),
        )


def test_an_invisible_id_below_a_burned_durable_floor_is_still_refused(
    tmp_path: Path,
) -> None:
    from okto_grafx.domain.errors import GrafxTransactionStateError
    from txn_support import build_stack

    root = tmp_path / "invisible-gap"
    root.mkdir()
    first = build_stack(root, owner_id="first", identity_lease_size=IDENTITY_LEASE_SIZE)
    table = _table()
    _register(first, table)
    _insert(first, table, "seed", key=b"seed")
    _insert(first, table, "leased", key=b"leased")
    assert first.heap.next_record_id(table) == 6
    assert _identities(first, table) == [1, 2]
    first.manager.close()

    reopened = build_stack(root, owner_id="reopened", identity_lease_size=IDENTITY_LEASE_SIZE)
    assert 4 not in _identities(reopened, table)
    refused = reopened.manager.begin("write")
    refused.stage_row_insert(table, ("must-not-reuse-burned-id",), record_id=4)
    refused.note_write(reopened.manager.partition_of(table.table_id, b"explicit-four"))
    with pytest.raises(GrafxTransactionStateError) as raised:
        reopened.manager.commit(refused)

    assert raised.value.details["field"] == "record_id"
    assert raised.value.details["durable_floor"] == 6
    assert _identities(reopened, table) == [1, 2]
    reopened.manager.rollback(refused)
    reopened.manager.close()
