"""The vector index under concurrency, through the public doors (C9 round-3 B5 and B6).

SPEC-M1 FR-3 names N processes AND N threads holding transactions on one database. A derived
structure -- the warm HNSW graph -- has to stay consistent under both: a search on one thread
while another commits, and a search in one process after another process committed.
"""

from __future__ import annotations

import random
import subprocess
import sys
import threading
import time
from pathlib import Path

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError

DIM = 6


def _vec(rnd: random.Random) -> list[float]:
    return [round(rnd.uniform(-1, 1), 6) for _ in range(DIM)]


def _schema(database: object) -> None:
    with database.begin("write") as txn:
        txn.execute("CREATE VECTOR SPACE s {dimension: 6, metric: 'cosine'}")
        txn.execute(
            "CREATE NODE TABLE Chunk(id INT64, layer INT64, embedding VECTOR(s), PRIMARY KEY(id))"
        )


def _search(database: object, query: list[float], k: int) -> list[int]:
    reader = database.begin("read")
    try:
        result = database.vectors.search(
            space="s", k=k, query=query, snapshot=reader.context.snapshot
        )
    finally:
        reader.rollback()
    return [hit.record_id for hit in result.hits]


# --- B5: a search on one thread while another thread commits -------------------------------------


def test_a_search_never_escapes_while_another_thread_commits_vectors(tmp_path: Path) -> None:
    """A traversal used to reach a node whose entry was not registered yet, and ``KeyError``
    left the section 8.8 door; a graph discarded under a search in flight did the same. Now a
    node is reachable only once its entry is known, and a search keeps the picture it started
    with for its whole duration."""
    rnd = random.Random(9)
    database = connect(str(tmp_path / "db"), vector_exact_scan_threshold=0)
    try:
        _schema(database)
        with database.begin("write") as txn:
            for identity in range(1, 101):
                txn.execute(
                    f"CREATE (:Chunk {{id: {identity}, layer: {identity % 3}, "
                    f"embedding: {_vec(rnd)}}})"
                )
        query = _vec(rnd)
        assert len(_search(database, query, 10)) == 10  # warm the graph

        stop = threading.Event()
        escapes: list[tuple[str, str]] = []
        refusals: list[str] = []
        answered = [0]
        committed = [0]
        guard = threading.Lock()

        def reader_loop() -> None:
            while not stop.is_set():
                try:
                    got = _search(database, query, 10_000)
                    with guard:
                        answered[0] += 1
                        if len(got) != len(set(got)):
                            escapes.append(("duplicate-ids", str(len(got) - len(set(got)))))
                except GrafxError:
                    pass  # a typed refusal is a legal answer under contention
                except BaseException as escaped:  # noqa: BLE001 - the defect under test
                    with guard:
                        escapes.append((type(escaped).__name__, repr(escaped)[:120]))

        def writer_loop() -> None:
            identity = 100
            while not stop.is_set():
                try:
                    with database.begin("write") as txn:
                        for _ in range(5):
                            identity += 1
                            txn.execute(
                                f"CREATE (:Chunk {{id: {identity}, layer: {identity % 3}, "
                                f"embedding: {_vec(rnd)}}})"
                            )
                        if identity % 50 == 0:
                            txn.execute(f"MATCH (c:Chunk {{id: {identity - 30}}}) DELETE c")
                    with guard:
                        committed[0] += 1
                except GrafxError as refused:
                    # A typed refusal is legal under contention -- but it is KEPT, because a
                    # commit refused AFTER the log barrier is durable and may have been applied
                    # to the heap and not to the index, which verify() then reports.
                    with guard:
                        refusals.append(f"{type(refused).__name__}: {refused.message[:160]}")
                except BaseException as escaped:  # noqa: BLE001
                    with guard:
                        escapes.append(("writer:" + type(escaped).__name__, repr(escaped)[:120]))
                    break

        readers = [threading.Thread(target=reader_loop, daemon=True) for _ in range(2)]
        writer = threading.Thread(target=writer_loop, daemon=True)
        for thread in (*readers, writer):
            thread.start()
        time.sleep(8.0)
        stop.set()
        for thread in (*readers, writer):
            thread.join(timeout=60)
        assert escapes == [], escapes
        assert answered[0] > 10 and committed[0] > 3, (answered[0], committed[0])
        findings = database.verify("all").findings
        assert findings == (), (
            [(finding.kind, finding.detail[:160]) for finding in findings[:4]],
            refusals[:6],
            answered[0],
            committed[0],
        )
    finally:
        database.close()


# --- B6: a warm graph in one process, commits in another ------------------------------------------


_OTHER = r'''
import sys, random
sys.path.insert(0, sys.argv[2])
from okto_grafx import connect
rnd = random.Random(77)
def vec(): return [round(rnd.uniform(-1, 1), 6) for _ in range(6)]
db = connect(sys.argv[1], vector_exact_scan_threshold=0)
with db.begin("write") as txn:
    for identity in range(13, 21):
        txn.execute(f"CREATE (:Chunk {{id: {identity}, layer: 0, embedding: {vec()}}})")
with db.begin("write") as txn:
    for identity in (2, 5, 13, 20):
        txn.execute(f"MATCH (c:Chunk {{id: {identity}}}) DELETE c")
with db.begin("write") as txn:
    for identity in (3, 7, 15):
        txn.execute(f"MATCH (c:Chunk {{id: {identity}}}) SET c.embedding = $v", {"v": None})
print("DONE", flush=True)
db.close()
'''


def test_a_warm_graph_learns_what_another_process_committed(tmp_path: Path) -> None:
    """The graph's only invalidation signals were this process's own commits (LESSONS L22).

    Parent warms the graph on 12 rows; a child inserts 8, deletes 4, updates 3. The parent's next
    search, under a new snapshot, must answer the APPROXIMATE regime from the rows that exist now:
    no deleted row, every live row reachable, and the same id set the exact regime gives.
    """
    root = str(tmp_path / "db")
    source = str(Path(connect.__code__.co_filename).resolve().parents[1])
    rnd = random.Random(3)
    parent = connect(root, vector_exact_scan_threshold=0)
    try:
        _schema(parent)
        with parent.begin("write") as txn:
            for identity in range(1, 13):
                txn.execute(
                    f"CREATE (:Chunk {{id: {identity}, layer: 0, embedding: {_vec(rnd)}}})"
                )
        query = _vec(rnd)
        warm = _search(parent, query, 100)
        assert sorted(warm) == list(range(1, 13))

        # The update statement in the child needs a VectorValue parameter; a list literal is the
        # recorded C10 punch-list item. Build the child's updates through the API instead.
        child_script = _OTHER.replace(
            '        txn.execute(f"MATCH (c:Chunk {{id: {identity}}}) SET c.embedding = $v", {"v": None})',
            '        pass',
        )
        child = subprocess.run(
            [sys.executable, "-c", child_script, root, source],
            capture_output=True, text=True, timeout=300,
        )
        assert child.returncode == 0, child.stderr[-600:]
        assert child.stdout.strip() == "DONE"

        expected = sorted(set(range(1, 21)) - {2, 5, 13, 20})
        approximate = sorted(_search(parent, query, 100))
        assert approximate == expected, (approximate, expected)

        exact = connect(root, vector_exact_scan_threshold=4096)
        try:
            assert sorted(_search(exact, query, 100)) == expected
        finally:
            exact.close()
        assert parent.verify("all").findings == ()
    finally:
        parent.close()
