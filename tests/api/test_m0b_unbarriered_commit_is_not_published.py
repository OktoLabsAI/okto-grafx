"""M0B supplementary probe: a commit whose durability was never proven publishes nothing.

The commit protocol appends the whole batch (COMMIT record included) and then asks the device
for the barrier; only past that proof may pages be applied and ``commit.state`` published. These
probes cut the proof off in three ways -- the barrier refuses with a typed error, raises a
foreign ``RuntimeError``, or is interrupted by ``KeyboardInterrupt`` -- and assert, in bytes:

* BEFORE any reopen: heap, index and ``commit.state`` are exactly as they were before the
  attempt (the log may hold the unproven append; nothing else moved);
* the failure leaves the process by the same door it entered: typed stays typed, foreign stays
  foreign (a swallowed ``KeyboardInterrupt`` would be a commit that "succeeded" under Ctrl+C);
* AFTER a kill and reopen the database is CONSISTENT: the row is either fully absent or fully
  present (the log replayed into heap, index and state together), never half. Process death
  keeps unbarriered bytes, so the surviving complete append may legitimately be replayed.

The fourth probe is the power-loss variant: with the bench reordering, the un-barriered append
is taken back by the crash, and the reopen must show the pre-commit state exactly -- no row, no
advanced ``commit.state``, ``verify()`` clean.

What the base (8c88e9d + the M0B-prep probes) does with the first three, recorded here and
carried as the strict-xfail reason: the refused commit grows the heap by one all-zero page
(an allocation; no content reaches the device before the barrier), and on reopen recovery
replays the surviving un-barriered append INTO THE HEAP ONLY -- a live row at heap page 1
slot 1, no index entry, ``commit.state`` still at the checkpoint -- ``verify()`` reports
``index_entry_missing`` while ``MATCH`` hides the row by MVCC: the half-published limbo of
P0.1, reached from a commit the caller was TOLD had failed. M0B must flip these.
"""

from __future__ import annotations

from typing import Any

import pytest

from okto_grafx import connect
from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice
from okto_grafx.domain.errors import GrafxError
from okto_grafx.engine.heap_store import HEAP_FILE
from okto_grafx.runtime.bootstrap import build_default_registry, release_ports
from okto_grafx.runtime.config import DatabaseConfig

from m0b_probe_support import ids, insert, schema
from power_loss_support import (
    SEEDS,
    durable_names,
    file_bytes,
    power_loss,
)

pytestmark = pytest.mark.timeout(300, method="thread")


class InterruptingBench(FaultInjectingStorageDevice):
    """The fault bench, with a barrier that can raise a FOREIGN exception once when armed.

    The bench's own plan speaks the storage taxonomy (typed refusals, simulated crashes); a
    host's exception is outside it -- a signal handler's ``KeyboardInterrupt``, a bug's
    ``RuntimeError`` -- and the commit path must let it out without publishing anything.
    """

    def __init__(self, inner: Any, *, seed: int = 0) -> None:
        super().__init__(inner, seed=seed)
        self.interrupt_barrier_with: BaseException | None = None

    def durable_barrier(self, file: str | None = None) -> None:
        armed = self.interrupt_barrier_with
        if armed is not None:
            self.interrupt_barrier_with = None
            raise armed
        super().durable_barrier(file)


def _prepared(
    seed: int,
) -> tuple[Any, Any, InterruptingBench, Any, dict[str, bytes | None]]:
    """One open database with a schema and a checkpoint, plus the pre-attempt digests.

    Returns (database, registry, bench, inner, published_digests): the handle stays open --
    it is the process the probe will "kill" by abandoning it after the failed commit.
    """
    config = DatabaseConfig(path=":memory:")
    registry = build_default_registry(config)
    inner = registry.get("storage")
    bench = InterruptingBench(inner, seed=seed)
    registry.bind("storage", bench)
    database = connect(":memory:", registry=registry)
    schema(database)
    database.checkpoint()
    return database, registry, bench, inner, _published_snapshot(inner)


def _published_snapshot(inner: Any) -> dict[str, bytes | None]:
    """Heap, catalog, meta, indexes and commit.state, in full bytes.

    The log is excluded (the unproven append legitimately lives there) and so is the rest of
    the control plane (leases and registrations move on every commit by design).
    """
    return {
        name: file_bytes(inner, name)
        for name in inner.list_files()
        if not name.startswith("wal/")
        if not name.startswith("control/") or name == "control/commit.state"
    }


def _assert_nothing_published(
    before: dict[str, bytes | None], inner: Any, who: str
) -> None:
    """No pre-existing byte moved; a file may only have GROWN by zeros (an allocation).

    Growth by an all-zero page is what ``allocate`` does to the device before a row is even
    validated; CONTENT -- a row image, an index entry, a commit state -- is publication.
    """
    zero = 0
    after = _published_snapshot(inner)
    for name, old in before.items():
        new = after.get(name)
        assert new is not None, f"{who}: {name} disappeared"
        held = old or b""
        assert new[: len(held)] == held, f"{who}: {name} rewrote existing bytes"
        assert sum(new[len(held) :]) == zero, (
            f"{who}: {name} grew with content, not zeros"
        )
    for name in set(after) - set(before):
        assert sum(after[name] or b"") == zero, f"{who}: new file {name} holds content"


def _reopen_and_observe(registry: Any) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Reopen after the kill and return (row ids, verify findings)."""
    reopened = connect(":memory:", registry=registry)
    with reopened:
        found = ids(reopened)
        findings = reopened.verify("all").findings
    return found, findings


@pytest.mark.xfail(
    strict=True,
    reason="base 8c88e9d+probes: the refused commit's un-barriered append is replayed on reopen into the heap only -- a live row at heap page 1 slot 1, no index entry, commit.state still at the checkpoint; verify() = index_entry_missing while MATCH hides the row by MVCC (the half-published limbo of P0.1, reached from a commit the caller was told had failed). Pending M0B: replay must be whole (heap+index+state) or nothing.",
)
def test_a_commit_whose_barrier_refuses_publishes_nothing_and_replays_whole_or_not_at_all() -> (
    None
):
    database, registry, bench, inner, before = _prepared(seed=1)
    bench.clear_trail()
    bench.fill_device_on("durable_barrier", occurrence=1)  # the commit's log barrier
    with pytest.raises(GrafxError):
        insert(database, 1)
    bench.disarm()
    _assert_nothing_published(before, inner, "refused barrier")
    # The process dies here: the handle is abandoned, and a fresh composition takes over.
    found, findings = _reopen_and_observe(registry)
    assert findings == (), findings
    assert found in ((1,), ()), f"a half-published row: {found}"
    release_ports(registry)


@pytest.mark.xfail(
    strict=True,
    reason="base 8c88e9d+probes: the refused commit's un-barriered append is replayed on reopen into the heap only -- a live row at heap page 1 slot 1, no index entry, commit.state still at the checkpoint; verify() = index_entry_missing while MATCH hides the row by MVCC (the half-published limbo of P0.1, reached from a commit the caller was told had failed). Pending M0B: replay must be whole (heap+index+state) or nothing.",
)
@pytest.mark.parametrize("foreign", [RuntimeError, KeyboardInterrupt])
def test_a_foreign_exception_at_the_barrier_escapes_and_publishes_nothing(
    foreign: type[BaseException],
) -> None:
    database, registry, bench, inner, before = _prepared(seed=1)
    bench.interrupt_barrier_with = foreign("injected at the durability barrier")
    with pytest.raises(foreign):
        insert(database, 1)
    _assert_nothing_published(before, inner, foreign.__name__)
    found, findings = _reopen_and_observe(registry)
    assert findings == (), findings
    assert found in ((1,), ()), f"a half-published row: {found}"
    release_ports(registry)


def test_a_power_loss_after_an_unproven_commit_leaves_the_pre_commit_state_exactly() -> (
    None
):
    for seed in SEEDS:
        database, registry, bench, inner, before = _prepared(seed)
        bench.start_reordering()
        log_before = {
            name: file_bytes(inner, name)
            for name in durable_names(inner)
            if name.startswith("wal/")
        }
        bench.clear_trail()
        bench.fill_device_on("durable_barrier", occurrence=1)
        with pytest.raises(GrafxError):
            insert(database, 1)
        bench.disarm()
        power_loss(bench)
        lost = all(
            file_bytes(inner, name) == content for name, content in log_before.items()
        ) and all(
            name in log_before
            for name in durable_names(inner)
            if name.startswith("wal/")
        )
        if lost:
            break  # A72: the unproven append never reached the platter
        release_ports(registry)
    else:
        pytest.fail("no seed lost the unbarriered append at the power loss")

    _assert_nothing_published(before, inner, "power loss")
    found, findings = _reopen_and_observe(registry)
    assert findings == (), findings
    assert found == (), (
        f"a commit whose append never reached the platter produced rows: {found}"
    )
    assert file_bytes(inner, HEAP_FILE) is not None
    release_ports(registry)
