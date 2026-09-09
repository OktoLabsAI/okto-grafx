"""Isolated installed-wheel consumer smoke for the eight-item 0.0.5 delivery."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import okto_grafx
from okto_grafx import CancellationToken, HybridSearchOptions, TextIndexOptions, VectorValue, connect
from okto_grafx.backup import create_backup, restore_backup
from okto_grafx.errors import GrafxQueryCancelled
from okto_grafx.migrations import SchemaMigration, migrate_schema


def main() -> None:
    """Exercise only public APIs in a private temporary database, never Pulse data."""
    origin = Path(okto_grafx.__file__).resolve()
    assert "site-packages" in origin.parts and okto_grafx.__version__ == "0.0.5"
    plan = (SchemaMigration(1, (
        "CREATE VECTOR SPACE s {dimension:3,metric:'cosine'}",
        "CREATE NODE TABLE D(id INT64, text STRING, vector VECTOR(s), PRIMARY KEY(id))",
        "CREATE REL TABLE R(FROM D TO D)",
    )),)
    with TemporaryDirectory(prefix="grafx-eight-wheel-") as temporary:
        root = Path(temporary)
        with connect(root / "db", page_size=512) as db:
            assert migrate_schema(db, plan, namespace="smoke", dry_run=True).pending == (1,)
            assert migrate_schema(db, plan, namespace="smoke").applied == (1,)
            assert migrate_schema(db, plan, namespace="smoke").previously_applied == (1,)
            db.ensure_identity_indexes()
            space = db.catalog.catalog.space("s").space_id
            with db.begin() as tx:
                for i in range(24):
                    tx.execute("CREATE (:D {id:$i,text:$text,vector:$v})", {
                        "i": i, "text": f"common word{i}",
                        "v": VectorValue(values=(1.0, float(i + 1), 0.0), space_ref=space),
                    })
                for i in range(23):
                    tx.execute("MATCH (a:D {id:$a}), (b:D {id:$b}) CREATE (a)-[:R]->(b)",
                               {"a": i, "b": i + 1})
            db.create_text_index("text", "D", ("text",),
                                 options=TextIndexOptions(statistics_mode="durable"))
            started = perf_counter()
            text = db.search_text(index="text", query="word0")
            text_ms = (perf_counter() - started) * 1000
            assert text.statistics_regime == "durable_summary" and text.corpus_documents == 24
            seed = text.hits[0].record_id
            with db.begin("read") as reader:
                started = perf_counter()
                hybrid = db.search_hybrid(reader, table="D", index="text", query="common",
                    space="s", vector=(1.0, 1.0, 0.0), k=5,
                    options=HybridSearchOptions(graph_seeds=(seed,), graph_relations=("R",),
                                                graph_hops=1, graph_direction="out", graph_weight=0.2))
                hybrid_ms = (perf_counter() - started) * 1000
                assert hybrid.graph_regime == "incident_index" and hybrid.graph_edges_visited == 1
                assert hybrid.memory_peak_bytes > 0 and hybrid.vector_memory_peak_bytes > 0
                token = CancellationToken()
                token.cancel()
                try:
                    db.search_vectors(reader, space="s", query=(1.0, 1.0, 0.0), k=3, cancellation=token)
                except GrafxQueryCancelled:
                    pass
                else:
                    raise AssertionError("cancelled vector search was not refused")
                assert reader.active
                assert db.search_vectors(reader, space="s", query=(1.0, 1.0, 0.0), k=3).hits
            distribution = db.index_distribution("text")
            assert distribution.entries > 0
            db.rehash_index("text", bucket_count=8192)
            assert db.index_distribution("text", max_pages=9000).bucket_count == 8192
            assert db.search_text(index="text", query="word0").hits == text.hits
            backup = create_backup(db, root / "backup", max_capture_seconds=30)
        restore_backup(root / "backup", root / "restored", confirm_original_offline=True)
        with connect(root / "restored", page_size=512) as db:
            assert db.search_text(index="text", query="word0").hits == text.hits
            assert migrate_schema(db, plan, namespace="smoke", dry_run=True).previously_applied == (1,)
            assert not db.verify("all").findings
        print(json.dumps({"result": "PASS", "origin": str(origin), "nodes": 24, "edges": 23,
            "cold_durable_text_ms": round(text_ms, 3), "hybrid_ms": round(hybrid_ms, 3),
            "text_postings_visited": text.postings_visited, "hybrid_incident_edges": hybrid.graph_edges_visited,
            "hybrid_logical_peak_bytes": hybrid.memory_peak_bytes, "backup_bytes": backup.bytes}))


if __name__ == "__main__":
    main()
