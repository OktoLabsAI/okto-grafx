"""The first open of a database must make the identity it hands out durable (P0.3).

``connect()`` on a fresh path creates ``grafx.meta`` (the identity of section 6.2), bootstraps
the catalog and the heap, and returns. ``MetaStore.create`` ends in a FLUSH and the bootstrap in
``pool.flush()``: no barrier covers the identity, the catalog header or the heap header when the
caller is handed a database and its UUID. A power loss then takes all three back, the log
survives (its segment IS barriered), and the next open silently creates ANOTHER database with
another UUID under the same path (EVOLUTION_PLAN_CODEX.md P0.3). Identity is not reconstructible
from the log, which is why this is a durability defect and not a recovery one.

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

from typing import Any

import pytest

from okto_grafx import connect
from okto_grafx.adapters.storage_fault import SimulatedCrash
from okto_grafx.domain.errors import GrafxError
from okto_grafx.engine.database import META_FILE
from okto_grafx.runtime.bootstrap import release_ports

from power_loss_support import (
    CONTROL_PREFIX,
    SEEDS,
    bench_registry,
    file_bytes,
    power_loss,
)

pytestmark = pytest.mark.timeout(300, method="thread")


def _volatile_data_files(bench: Any) -> tuple[str, ...]:
    """Return the data files with a write no barrier has pinned (the control plane excluded)."""
    return tuple(
        name for name in bench.volatile_files() if not name.startswith(CONTROL_PREFIX)
    )


def test_the_first_open_returns_only_after_its_identity_and_headers_are_durable() -> (
    None
):
    registry, bench, inner = bench_registry(1)
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


@pytest.mark.xfail(
    strict=True,
    reason="P0.3 (pre-fix at 8c88e9d): after a power loss the next open silently created another UUID. Pending M0C.",
)
def test_an_identity_handed_out_by_the_first_open_survives_a_power_loss() -> None:
    for seed in SEEDS:
        registry, bench, inner = bench_registry(seed)
        bench.start_reordering()
        database = connect(":memory:", registry=registry)
        uuid = database.identity.database_uuid
        database.close()
        power_loss(bench)
        if file_bytes(inner, META_FILE) is None:
            break  # the identity page never reached the platter: the state under test (A72)
        release_ports(registry)
    else:
        pytest.fail("no seed lost the identity page at the power loss")

    try:
        reopened = connect(":memory:", registry=registry)
    except GrafxError as refused:
        outcome: tuple[str, object] = ("refused", refused.code)
    else:
        with reopened:
            outcome = ("opened", reopened.identity.database_uuid)
    release_ports(registry)
    assert outcome[0] == "refused" or outcome[1] == uuid, (
        f"the first open handed out {uuid}; after a power loss the next open silently "
        f"produced {outcome}"
    )


def _open_and_close(registry: Any) -> None:
    connect(":memory:", registry=registry).close()


@pytest.mark.xfail(
    strict=True,
    reason="P0.3 (pre-fix at 8c88e9d): a crash at create/allocate heap.dat or write_page catalog.dat leaves a database that opens with one verify() finding. Pending M0C.",
)
def test_a_crash_at_every_write_point_of_the_first_open_leaves_a_path_that_opens_or_refuses() -> (
    None
):
    registry, bench, inner = bench_registry(1)
    points = bench.enumerate_write_points(lambda device: _open_and_close(registry))
    release_ports(registry)
    assert len(points) >= 3, points

    outcomes: dict[str, str] = {}
    crashed = 0
    for point in points:
        registry, bench, inner = bench_registry(1)
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
