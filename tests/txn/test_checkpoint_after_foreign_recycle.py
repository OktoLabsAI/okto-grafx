"""checkpoint() while another process recycled WAL this handle had cached (issue #11).

Two real processes share one directory. Process A (the test) commits into its first WAL segments
and keeps descriptors cached for them. Process B commits about twenty segments of its own and
checkpoints; once A's standing reader pin has moved forward, B's second checkpoint recycles
A's segments. A then opens a read transaction, which makes its own checkpoint take the monolithic
path that flushes every cached descriptor with ``durable_barrier(None)``. That barrier used to
raise GrafxDurabilityBarrierFailed ("File 'wal/000000000001.wal' does not exist on this device").

Nothing here sleeps: B announces each phase on its standard output and waits for a line on its
standard input, and the pin of A advances at the ``begin`` that follows B's first checkpoint
because the reader stall threshold A opens with (STALL_SECONDS) makes every refresh due.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from okto_grafx import connect

SEGMENT_BYTES: int = 64 * 1024
A_COMMITS: int = 10
B_COMMITS: int = 100
STALL_SECONDS: float = 1e-6
"""A's reader stall threshold. Its refresh interval is a fraction of it, so every ``begin`` finds
the standing pin due and moves it, with no wall-clock wait in the test."""
PAD: str = "x" * 1000

CHILD: str = r'''
import sys
from okto_grafx import connect

root, segment_bytes, commits, pad = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
with connect(root, wal_segment_bytes=segment_bytes) as db:
    for k in range(commits):
        with db.begin("write") as tx:
            tx.execute("CREATE (:B {id: $i, pad: $p})", {"i": k, "p": pad})
    db.checkpoint()
    print("FIRST", flush=True)
    sys.stdin.readline()
    db.checkpoint()
    print("SECOND", flush=True)
'''


def _segments(root: Path) -> list[str]:
    return sorted(path.name for path in (root / "wal").iterdir() if path.suffix == ".wal")


def test_checkpoint_with_open_read_survives_segments_recycled_by_another_process(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, wal_segment_bytes=SEGMENT_BYTES, reader_stall_threshold_seconds=STALL_SECONDS) as a:
        with a.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, pad STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id INT64, pad STRING, PRIMARY KEY(id))")
        for k in range(A_COMMITS):
            with a.begin("write") as tx:
                tx.execute("CREATE (:A {id: $i, pad: $p})", {"i": k, "p": PAD})
        a.checkpoint()
        first_segments = _segments(root)

        holding = a.begin("read")
        assert holding.execute("MATCH (n:A) RETURN count(*)").rows == ((A_COMMITS,),)
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(root), str(SEGMENT_BYTES), str(B_COMMITS), PAD],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None and child.stdin is not None
            assert child.stdout.readline().strip() == "FIRST"
            holding.rollback()
            # The standing pin of A moves forward at this begin, so B may recycle up to it.
            advance = a.begin("read")
            assert advance.execute("MATCH (n:B) RETURN count(*)").rows == ((B_COMMITS,),)
            advance.rollback()
            assert len(_segments(root)) >= 15
            child.stdin.write("go\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "SECOND"
            assert child.wait(timeout=60) == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=30)
        remaining = _segments(root)
        assert first_segments[0] not in remaining, "the second checkpoint did not recycle"

        reading = a.begin("read")
        assert reading.execute("MATCH (n:B) RETURN count(*)").rows == ((B_COMMITS,),)
        report = a.checkpoint()
        assert report is not None
        assert a.maintenance.status().recovery_required is False
        reading.rollback()

    with connect(root, wal_segment_bytes=SEGMENT_BYTES) as third:
        assert third.verify("all").clean
        assert third.execute("MATCH (n:A) RETURN count(*)").rows == ((A_COMMITS,),)
        assert third.execute("MATCH (n:B) RETURN count(*)").rows == ((B_COMMITS,),)
