"""The checkpoint door, and the log reclamation it drives (BR-10, CF-11).

Until this door existed, ``WalManager.recycle`` and ``recyclable_horizon`` had no caller anywhere
in ``src`` and the published checkpoint never moved: the log grew without bound, and recovery
replayed it from the first record every time. The tests here drive the public door and read the
DEVICE -- the segment files in ``wal/`` -- rather than any component's own report of itself.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxUnsupportedOperation

SEGMENT_BYTES: int = 64 * 1024
"""Small enough that sixty one-row commits span many segments."""


def _segments(path: Path) -> list[str]:
    return sorted(os.path.basename(name) for name in glob.glob(str(path / "wal" / "*.wal")))


def _people(database: object, *, at_least: int = 0) -> int:
    rows = database.execute("MATCH (p:P) RETURN p.id").rows
    return sum(1 for (identity,) in rows if identity >= at_least)


def _schema(database: object) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")


def test_a_checkpoint_reclaims_the_segments_the_database_no_longer_needs(tmp_path: Path) -> None:
    root = tmp_path / "db"
    database = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        _schema(database)
        for identity in range(1, 61):
            with database.begin("write") as txn:
                txn.execute(f"CREATE (:P {{id: {identity}, name: 'n{identity}'}})")
        before = _segments(root)
        assert len(before) > 5, "the scenario needs a log that spans several segments"
        report = database.checkpoint()
        after = _segments(root)
        assert len(report.recycled) == len(before) - len(after)
        assert len(after) < len(before)
        assert set(after).isdisjoint(report.recycled)
        assert _people(database) == 60
    finally:
        database.close()
    reopened = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        assert _people(reopened) == 60
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()


def test_a_second_checkpoint_with_nothing_new_reclaims_nothing(tmp_path: Path) -> None:
    root = tmp_path / "db"
    database = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        _schema(database)
        for identity in range(1, 31):
            with database.begin("write") as txn:
                txn.execute(f"CREATE (:P {{id: {identity}, name: 'n'}})")
        database.checkpoint()
        held = _segments(root)
        second = database.checkpoint()
        assert second.recycled == ()
        assert _segments(root) == held
    finally:
        database.close()


def test_a_read_only_database_cannot_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "db"
    database = connect(str(root))
    _schema(database)
    database.close()
    reader = connect(str(root), read_only=True)
    try:
        with pytest.raises(GrafxUnsupportedOperation) as refusal:
            reader.checkpoint()
        assert refusal.value.details["field"] == "read_only"
    finally:
        reader.close()


_OTHER_PROCESS = r'''
import sys, time
sys.path.insert(0, sys.argv[2])
from okto_grafx import connect
db = connect(sys.argv[1], wal_segment_bytes=65536)
for identity in range(200, 230):
    with db.begin("write") as txn:
        txn.execute(f"CREATE (:P {{id: {identity}, name: 'b'}})")
print("COMMITTED", flush=True)
time.sleep(60)   # hold the pages in memory; the test kills this process before it flushes
'''


def test_a_checkpoint_never_reclaims_a_page_another_process_has_not_flushed(
    tmp_path: Path,
) -> None:
    """The reason the checkpoint redoes the log onto the device before it publishes.

    The commit protocol applies a commit's page images to the committing process's OWN pool and
    deliberately does not put them on the platter (section 8.5 step 6). So another participant's
    committed pages may exist only in the log and in that participant's memory. A checkpoint
    that flushed only its own pool and then recycled the segments would destroy the only durable
    copy of those pages; if that participant then crashed, its acknowledged commits would be
    gone -- loss an ordinary caller reaches, with every commit having said ``durable=True``.

    So: process B commits thirty rows and holds them unflushed; process A checkpoints and
    recycles; B is killed without ever flushing or closing; a cold reopen must still find all
    thirty. This test reads the outcome from a third process's view of the device.
    """
    root = tmp_path / "db"
    source = str(Path(connect.__code__.co_filename).resolve().parents[1])
    database = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    _schema(database)
    database.close()

    other = subprocess.Popen(
        [sys.executable, "-c", _OTHER_PROCESS, str(root), source],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert other.stdout is not None
        deadline = time.monotonic() + 120
        line = ""
        while time.monotonic() < deadline and not line:
            line = other.stdout.readline().strip()
        assert line == "COMMITTED", f"the other process did not commit: {line!r}"

        checkpointer = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
        try:
            assert _people(checkpointer, at_least=200) == 30
            report = checkpointer.checkpoint()
            assert report.recycled, "the scenario needs the checkpoint to actually reclaim"
        finally:
            checkpointer.close()
    finally:
        other.kill()
        other.wait(timeout=30)

    reopened = connect(str(root), wal_segment_bytes=SEGMENT_BYTES)
    try:
        assert _people(reopened, at_least=200) == 30
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()
