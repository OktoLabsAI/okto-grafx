"""M0B invariant 5: a checkpoint over an incomplete log lineage refuses without mutating.

A checkpoint redoes the log from the published checkpoint to the last commit onto the device,
flushes, publishes the new checkpoint and recycles what the horizon allows. With a segment
MISSING from that range the redo cannot be complete, and a checkpoint that published anyway
would let the next recycle destroy the only durable copy of commits it never reached. It must
refuse, typed, and leave the published state and the segments exactly as they were.
"""

from __future__ import annotations

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError
from okto_grafx.runtime.bootstrap import release_ports

from m0b_probe_support import insert, schema
from power_loss_support import bench_registry, file_bytes

pytestmark = pytest.mark.timeout(300, method="thread")

SEGMENT_BYTES = 64 * 1024
"""Small segments, so a dozen commits roll the log over several files."""


def test_a_checkpoint_over_a_missing_log_segment_refuses_without_mutating() -> None:
    registry, bench, inner = bench_registry(1, wal_segment_bytes=SEGMENT_BYTES)
    database = connect(":memory:", registry=registry, wal_segment_bytes=SEGMENT_BYTES)
    try:
        schema(database)
        database.checkpoint()
        for record_id in range(1, 13):
            insert(database, record_id)
        segments = sorted(
            name for name in inner.list_files() if name.startswith("wal/")
        )
        assert len(segments) >= 3, segments  # A72: the commits rolled the log over
        victim = segments[1]  # a middle segment, inside the checkpoint's redo range
        state_before = database.transactions.published_state()
        others = {
            name: file_bytes(inner, name)
            for name in inner.list_files()
            if name != victim
        }
        inner.remove(victim)
        try:
            database.checkpoint()
        except GrafxError as refused:
            outcome: tuple[str, object] = ("refused", refused.code)
        except Exception as other:  # noqa: BLE001 - the taxonomy is the assertion
            outcome = ("died", type(other).__name__)
        else:
            outcome = ("checkpointed", database.transactions.published_state())
        assert outcome[0] == "refused", outcome
        assert database.transactions.published_state() == state_before
        assert (
            file_bytes(inner, "control/commit.state") == others["control/commit.state"]
        )
        assert sorted(n for n in inner.list_files() if n.startswith("wal/")) == sorted(
            n for n in others if n.startswith("wal/")
        ), "the checkpoint recycled or created a segment over an incomplete lineage"
    finally:
        try:
            database.close()
        except GrafxError:
            pass
        release_ports(registry)
