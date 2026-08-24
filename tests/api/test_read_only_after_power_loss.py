"""``read_only=True`` after a power loss that kept the log but not the pages (P0.2).

The commit protocol barriers the log and ``commit.state`` and only FLUSHES the heap and index
pages, because the log is the authority and recovery replays it (section 8.5 step 6). A power
loss can therefore leave a database whose published commit state is ahead of its pages. A
writable open runs recovery and the row comes back. A read-only open skips recovery (it may not
write) and builds its snapshot from the published state -- so it reads pages that are BEHIND the
snapshot it claims to represent and answers with fewer rows than the log holds, ``stale`` False,
``recovery_report`` None (EVOLUTION_PLAN_CODEX.md P0.2).

Two properties, through the public door: the reader answers EXACTLY what the log holds or
refuses with a typed error (a silent short answer is the defect); and whatever it answers, it
writes nothing. The scenario is built with the fault bench of FR-16 (see
``power_loss_support``): honest barriers pin the log and the commit state, and the crash keeps a
seeded prefix of the un-barriered page writes. The seed is chosen by trying, and the state is
asserted before either property is (A72).
"""

from __future__ import annotations

from typing import Any

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError
from okto_grafx.engine.heap_store import HEAP_FILE
from okto_grafx.runtime.bootstrap import release_ports

from power_loss_support import (
    SEEDS,
    bench_registry,
    data_digests,
    durable_names,
    file_bytes,
    power_loss,
)

pytestmark = pytest.mark.timeout(300, method="thread")


def _scenario(seed: int) -> tuple[Any, Any, Any, bool]:
    """Build: schema checkpointed, one row committed, power loss. Say whether the state appeared.

    The state this test needs is the one the audit reproduced: the heap page of the row is back
    to its pre-commit image, while the log segments and ``commit.state`` are exactly what the
    commit left. The abandoned database handle is NOT closed -- the process died -- because
    closing it would write the page back and undo the power loss.
    """
    registry, bench, inner = bench_registry(seed)
    database = connect(":memory:", registry=registry)
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
    database.checkpoint()  # the schema is on the platter: checkpoint_lsn == last committed
    bench.start_reordering()  # from here a crash keeps only a seeded part of un-barriered writes
    heap_before = file_bytes(inner, HEAP_FILE)
    with database.begin("write") as txn:
        txn.execute("CREATE (:Person {id: 1})")
    # Acknowledged: the log and the commit state carry the row; the heap page was only flushed.
    durable_after = {name: file_bytes(inner, name) for name in durable_names(inner)}
    assert "control/commit.state" in durable_after
    power_loss(bench)
    page_lost = file_bytes(inner, HEAP_FILE) == heap_before
    log_kept = all(
        file_bytes(inner, name) == content for name, content in durable_after.items()
    )
    return registry, bench, inner, page_lost and log_kept


def _after_power_loss() -> tuple[Any, Any, Any]:
    """Return a composition in the state under test, trying seeds until one produces it."""
    for seed in SEEDS:
        registry, bench, inner, produced = _scenario(seed)
        if produced:
            return registry, bench, inner
        release_ports(registry)
    pytest.fail("no seed produced the state: heap page lost, log and commit state kept")


def _read_only_outcome(registry: Any) -> tuple[str, object]:
    """Open read-only and say what it did: ("answered", rows) or ("refused", code)."""
    try:
        read_only = connect(":memory:", registry=registry, read_only=True)
    except GrafxError as refused:
        # Declining is the other acceptable answer: the reader met a database that needs
        # recovery and said so, in the taxonomy, rather than answering from behind.
        return ("refused", refused.code)
    with read_only:
        assert (
            read_only.recovery_report is None
        )  # a reader runs no recovery, by contract
        return ("answered", tuple(read_only.execute("MATCH (p:Person) RETURN p.id")))


@pytest.mark.xfail(
    strict=True,
    reason="P0.2 (pre-fix at 8c88e9d): read_only answered () for a row the log holds; the writable open recovers it. Pending M0C.",
)
def test_a_read_only_open_after_a_power_loss_answers_the_log_or_refuses() -> None:
    registry, bench, inner = _after_power_loss()
    outcome = _read_only_outcome(registry)
    # The truth the reader had to match: the log holds the row, and a writable open proves it.
    with connect(":memory:", registry=registry) as writable:
        assert tuple(writable.execute("MATCH (p:Person) RETURN p.id")) == ((1,),)
    release_ports(registry)
    if outcome[0] == "answered":
        assert outcome[1] == ((1,),), (
            f"read_only answered {outcome[1]} for a commit the log holds and the writable "
            "open recovered: a silent short answer, stale False, recovery_report None"
        )


@pytest.mark.xfail(
    strict=True,
    reason="P0.2/P1.1 (pre-fix at 8c88e9d): the read-only open rewrote index/pk_Person.idx. Pending M0C.",
)
def test_a_read_only_open_after_a_power_loss_writes_nothing() -> None:
    registry, bench, inner = _after_power_loss()
    before = data_digests(inner)
    _read_only_outcome(registry)
    after = data_digests(inner)
    release_ports(registry)
    changed = sorted(
        name for name in set(before) | set(after) if before.get(name) != after.get(name)
    )
    assert not changed, f"a read-only open changed these data files: {changed}"
