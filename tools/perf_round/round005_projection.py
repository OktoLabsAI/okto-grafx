"""Bounded ordered-page evidence with real non-null 384d vectors and overflow payloads."""
import json
from pathlib import Path
import statistics
import tempfile
import time
from unittest.mock import patch

from okto_grafx import Timestamp, connect
from okto_grafx.engine import query_engine


def main():
    query = "MATCH (n) RETURN n.id,n.created_at,n.title ORDER BY n.created_at DESC,n.id DESC LIMIT 128"
    with tempfile.TemporaryDirectory(prefix="grafx-v005-projection-") as raw:
        with connect(Path(raw) / "db", descriptor_revalidation="generation") as db:
            with db.begin("write") as tx:
                tx.execute("CREATE VECTOR SPACE emb {dimension:384,metric:'cosine'}")
                for table in ("A", "B"):
                    tx.execute(f"CREATE NODE TABLE {table}(id STRING,created_at TIMESTAMP,title STRING,payload STRING,v VECTOR(emb),PRIMARY KEY(id))")
                    for i in range(128):
                        tx.execute(f"CREATE (:{table} {{id:$id,created_at:$ts,title:'title',payload:$payload,v:$vector}})",
                                   {"id": f"{table}{i:04}", "ts": Timestamp(i), "payload": "x" * 12000,
                                    "vector": [1.0] + [0.0] * 383})
            for table in ("A", "B"):
                db.create_index(f"ordered_{table}", table, ("created_at", "id"), layout="ordered")
            db.checkpoint()
            expected = db.execute(query).rows
            assert len(expected) == 128
            timings = {"projected": [], "full_oracle": []}
            for _ in range(4):
                for mode in timings:
                    proof = query_engine._closed_node_scan_projections if mode == "projected" else lambda _root: {}
                    with patch.object(query_engine, "_closed_node_scan_projections", proof):
                        started = time.perf_counter()
                        result = db.execute(query)
                        timings[mode].append((time.perf_counter() - started) * 1000)
                    assert result.rows == expected
            print(json.dumps({"rows": 256, "page_rows": 128, "vector_dimension": 384,
                              "payload_characters": 12000,
                              "median_ms": {name: round(statistics.median(values), 3) for name, values in timings.items()},
                              "samples_ms": timings}, indent=2))


if __name__ == "__main__":
    main()
