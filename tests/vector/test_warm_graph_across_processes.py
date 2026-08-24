"""A warm HNSW graph, a commit of another process, and a commit of this one (P0.5, warm probe).

The handoff for P0.5 asked for the warm path to be probed rather than improvised on. This is
what the probe found. ``VectorHnswIndex.commit`` used to note its OWN staged changes into the
warm graph and then stamp the graph's mark with the header's ``built_through_lsn`` -- whatever
that header said. When another PROCESS had committed since the graph was built, the header was
already past the graph, the graph lacked that process's rows, and the stamp certified it as
current anyway: the next search in this process answered without those rows, ``stale`` False,
``verify()`` clean. The same silent short answer as LESSONS L22, one path over from where L22
was fixed (the cold path re-checks the header; the warm path did not).

The fixed protocol certifies a picture only when the commit verified, BEFORE the store moved,
that the picture was current, and retires it otherwise. Two interpreters are needed, because
inside one process the header can only be moved by the commit that is about to certify.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from okto_grafx import connect

CHILD = Path(__file__).resolve().parent / "warm_graph_child.py"
CHILD_BUDGET: float = 120.0


def _components(record_id: int) -> tuple[float, float, float, float]:
    return (1.0 - record_id * 0.05, record_id * 0.05, 0.25, 0.0)


def _insert(txn: Any, record_id: int) -> None:
    a, b, c, d = _components(record_id)
    txn.execute(
        "CREATE (:V {id: $i, e: [$a, $b, $c, $d]})",
        {"i": record_id, "a": a, "b": b, "c": c, "d": d},
    )


def _search(database: Any, k: int) -> tuple[int, str]:
    reader = database.begin("read")
    try:
        hits = database.vectors.search(
            space="s",
            k=k,
            query=[1.0, 0.0, 0.25, 0.0],
            snapshot=reader.context.snapshot,
        )
        return (hits.achieved_k, hits.regime)
    finally:
        reader.rollback()


def _other_process_inserts(root: Path, record_id: int) -> dict[str, Any]:
    """Run one insert in a fresh interpreter and return what it reported."""
    completed = subprocess.run(
        [sys.executable, str(CHILD), str(root), str(record_id)],
        capture_output=True,
        text=True,
        timeout=CHILD_BUDGET,
        check=False,
    )
    assert completed.stdout.strip(), (
        f"the child produced no report; stderr={completed.stderr[-800:]}"
    )
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["failure"] is None, report["failure"]
    assert report["committed"] is True
    return report


@pytest.mark.multiprocess
@pytest.mark.timeout(240, method="thread")
def test_a_commit_does_not_certify_a_warm_graph_that_another_process_left_behind(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    database = connect(root, vector_exact_scan_threshold=0)
    try:
        with database.begin("write") as txn:
            txn.execute("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
            txn.execute("CREATE NODE TABLE V(id INT64, e VECTOR(s), PRIMARY KEY(id))")
        with database.begin("write") as txn:
            for record_id in (1, 2, 3):
                _insert(txn, record_id)
        # Warm the graph: three rows, built and certified at this process's own commit.
        assert _search(database, k=5) == (3, "approximate")

        # Another process commits a fourth row and leaves. Nothing in this process has looked
        # at the store since, so the warm graph is now behind the header.
        _other_process_inserts(root, 4)

        # This process commits a fifth row. Pre-fix, the commit noted row 5 into the warm graph
        # and stamped it with the header -- which was already past row 4 -- so the graph was
        # certified current while missing row 4.
        with database.begin("write") as txn:
            _insert(txn, 5)

        assert _search(database, k=5) == (5, "approximate")
    finally:
        database.close()
