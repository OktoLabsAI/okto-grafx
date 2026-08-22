"""A long-lived participant must keep seeing what other processes commit (D1).

Found by C9's round-3 blind critic and routed to C2: `LocalStorageDevice` cached the descriptor of
``control/commit.state``, and another process's ``atomic_replace`` moved the directory entry from
under it. The cached handle kept reading the REPLACED file -- so a process that had once read the
published state never learned of anyone else's commits, answered stale rows for ever, and had every
commit of its own refused as a conflict with a world it could not see. Every multi-process test before
this one used a FRESH process for the read, which is the safe regime (LESSONS L23, L24).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from okto_grafx import connect

_CHILD = r'''
import sys
sys.path.insert(0, sys.argv[2])
from okto_grafx import connect
db = connect(sys.argv[1])
for identity in range(2, 6):
    with db.begin("write") as txn:
        txn.execute(f"CREATE (:P {{id: {identity}}})")
print(db.transactions.published_lsn(), flush=True)
db.close()
'''


def test_a_long_lived_process_sees_what_another_process_commits(tmp_path: Path) -> None:
    root = str(tmp_path / "db")
    source = str(Path(connect.__code__.co_filename).resolve().parents[1])
    parent = connect(root)
    try:
        with parent.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE (:P {id: 1})")
        # The parent has READ the published state once; its descriptor is now cached.
        assert sorted(parent.execute("MATCH (p:P) RETURN p.id").rows) == [(1,)]
        before = parent.transactions.published_lsn()

        child = subprocess.run(
            [sys.executable, "-c", _CHILD, root, source],
            capture_output=True, text=True, timeout=180,
        )
        assert child.returncode == 0, child.stderr[-500:]
        published_by_child = int(child.stdout.strip())
        assert published_by_child > before

        # The same handle, without reconnecting: the new rows and the new number.
        assert sorted(parent.execute("MATCH (p:P) RETURN p.id").rows) == [
            (1,), (2,), (3,), (4,), (5,),
        ]
        assert parent.transactions.published_lsn() == published_by_child

        # And it can still commit: its snapshot is current, so nothing conflicts.
        with parent.begin("write") as txn:
            txn.execute("CREATE (:P {id: 100})")
        assert sorted(parent.execute("MATCH (p:P) RETURN p.id").rows) == [
            (1,), (2,), (3,), (4,), (5,), (100,),
        ]
    finally:
        parent.close()
    fresh = connect(root)
    try:
        assert len(fresh.execute("MATCH (p:P) RETURN p.id").rows) == 6
        assert fresh.verify("all").findings == ()
    finally:
        fresh.close()
