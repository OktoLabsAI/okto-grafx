"""Bounded 0.0.5 native attribution; only fresh temporary stores, never Pulse data.

Run with PYTHONPATH=src: python -m tools.perf_round.round005_native.
Timings are observations, not gates. Assert answers, reopen and concurrent writes.
The JSON intentionally does not label Python allocation peaks as process RSS.
"""

from __future__ import annotations

import json
import hashlib
import platform
import tempfile
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import okto_grafx
from okto_grafx import __version__, connect
from okto_grafx.errors import GrafxWriteConflict


def measured(operation):
    start = time.perf_counter()
    value = operation()
    return value, round((time.perf_counter() - start) * 1000, 3)


def populate(db, count):
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Item(id INT64, title STRING, PRIMARY KEY(id))")
    with db.begin("write") as tx:
        for number in range(count):
            tx.execute("CREATE (:Item {id:$id,title:$title})", {
                "id": number, "title": f"item {number}",
            })


def sample(root, count):
    db, open_ms = measured(lambda: connect(root))
    with db:
        _, setup_ms = measured(lambda: populate(db, count))
        queries = {
            "selective_page": (
                "MATCH (n:Item) WHERE n.id IN $ids RETURN n.id ORDER BY n.id LIMIT 500",
                {"ids": list(range(min(500, count)))},
            ),
            "next_selective_page": (
                "MATCH (n:Item) WHERE n.id IN $ids RETURN n.id ORDER BY n.id LIMIT 500",
                {"ids": list(range(500, min(1000, count)))},
            ),
            "unindexed_range_page": (
                "MATCH (n:Item) WHERE n.id >= $start RETURN n.id ORDER BY n.id LIMIT 500",
                {"start": 0},
            ),
        }
        observations = {}
        for name, (query, params) in queries.items():
            results = []
            for _ in range(2):
                result, elapsed = measured(lambda: db.execute(query, params))
                expected = params.get("ids", list(range(min(count, 500))))
                assert result.rows == tuple((value,) for value in expected)
                results.append({"ms": elapsed, "statistics": dict(result.statistics)})
            observations[name] = results
        tracemalloc.start()
        with db.begin("write") as tx:
            _, preflight_ms = measured(lambda: [
                tx.execute("MATCH (n:Item {id:0}) RETURN n.id,n.title") for _ in range(20)
            ])
            _, stage_ms = measured(lambda: [
                tx.execute("CREATE (:Item {id:$id,title:'new'})", {"id": count + number})
                for number in range(11)
            ])
            _, commit_ms = measured(tx.commit)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert db._queries._owner_budget._used_bytes == 0
        _, checkpoint_ms = measured(db.checkpoint)
    reopened, reopen_ms = measured(lambda: connect(root))
    with reopened:
        assert reopened.execute("MATCH (n:Item) RETURN count(n)").rows == ((count + 11,),)
    return {
        "nodes_before": count, "new_open_ms": open_ms, "setup_ms": setup_ms,
        "queries": observations, "preflight_20_reads_ms": preflight_ms,
        "stage_11_rows_ms": stage_ms, "commit_11_rows_ms": commit_ms,
        "write_python_traced_peak_bytes": peak, "checkpoint_ms": checkpoint_ms,
        "reopen_ms": reopen_ms,
    }


def concurrent(root, participants):
    with connect(root) as db:
        populate(db, 32)
    barrier = threading.Barrier(participants)

    def worker(slot):
        with connect(root) as db:
            barrier.wait(timeout=20)
            retries = 0
            started = time.perf_counter()
            for step in range(4):
                if slot % 2:
                    assert db.execute("MATCH (n:Item {id:0}) RETURN n.id").rows == ((0,),)
                    continue
                for attempt in range(8):
                    try:
                        with db.begin("write") as tx:
                            tx.execute("CREATE (:Item {id:$id,title:'concurrent'})",
                                       {"id": 100 + slot * 10 + step})
                        break
                    except GrafxWriteConflict:
                        retries += 1
                        if attempt == 7:
                            raise
            return {"slot": slot, "kind": "reader" if slot % 2 else "writer",
                    "ms": round((time.perf_counter() - started) * 1000, 3),
                    "conflict_retries": retries}

    with ThreadPoolExecutor(max_workers=participants) as workers:
        results = list(workers.map(worker, range(participants)))
    with connect(root) as db:
        expected = 32 + ((participants + 1) // 2) * 4
        assert db.execute("MATCH (n:Item) RETURN count(n)").rows == ((expected,),)
    return {"participants": participants, "workers": results}


def main():
    source_root = Path(__file__).resolve().parents[2]
    assert Path(okto_grafx.__file__).resolve().is_relative_to(source_root / "src")
    with tempfile.TemporaryDirectory(prefix="grafx-round005-native-") as raw:
        root = Path(raw)
        result = {
            "version": __version__, "python": platform.python_version(),
            "platform": platform.platform(), "scope": "native synthetic only; not Pulse/UI",
            "source_sha256": {
                str(path.relative_to(source_root)).replace("\\", "/"):
                hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    source_root / "src/okto_grafx/engine/query_engine.py",
                    Path(__file__).resolve(),
                )
            },
            "scale": [sample(root / f"scale-{count}", count) for count in (128, 512, 2048)],
            "concurrency": [concurrent(root / f"concurrent-{n}", n) for n in (1, 2, 4)],
        }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
