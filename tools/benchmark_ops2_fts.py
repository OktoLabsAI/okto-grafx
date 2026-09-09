"""Bounded reproducible FTS/transfer sample; correctness assertions, no timing gate."""

from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
import json

from okto_grafx import connect
from okto_grafx.transfer import export_graph, import_graph


def main() -> None:
    """Measure a fixed 200-document local corpus, with durable native commits."""
    result = {}
    with TemporaryDirectory() as temp:
        root = Path(temp)
        with connect(root / "db") as db:
            with db.begin() as tx:
                tx.execute(
                    "CREATE NODE TABLE Doc(id INT64, body STRING, PRIMARY KEY(id))"
                )
                for number in range(200):
                    tx.execute(
                        "CREATE (:Doc {id:$id, body:$body})",
                        {
                            "id": number,
                            "body": f"graph durable wal group{number % 10} document{number}",
                        },
                    )
            started = perf_counter()
            db.create_text_index("text", "Doc", ("body",), bucket_count=64)
            result["build_ms"] = (perf_counter() - started) * 1000
            started = perf_counter()
            cold = db.search_text(index="text", query="group3", k=20)
            result["cold_search_ms"] = (perf_counter() - started) * 1000
            assert len(cold.hits) == 20 and cold.corpus_documents == 200
            warm = []
            for _ in range(20):
                started = perf_counter()
                found = db.search_text(index="text", query="group3", k=20)
                warm.append((perf_counter() - started) * 1000)
                assert found.hits == cold.hits
            result["warm_median_ms"] = median(warm)
            result["cold_postings"] = cold.postings_visited
            result["warm_postings"] = found.postings_visited
            started = perf_counter()
            with db.begin() as tx:
                tx.execute(
                    "CREATE (:Doc {id:200, body:'graph durable wal group3 document200'})"
                )
            result["one_document_transaction_ms"] = (perf_counter() - started) * 1000
            started = perf_counter()
            advanced = db.search_text(index="text", query="document200")
            result["post_write_search_ms"] = (perf_counter() - started) * 1000
            assert advanced.hits and advanced.statistics_regime == "wal_delta"
            result["post_write_postings"] = advanced.postings_visited
            result["statistics_wal_records"] = advanced.statistics_wal_records
            db._text_stats_cache.clear()
            census = db.search_text(index="text", query="document200")
            assert (
                census.hits == advanced.hits
                and census.corpus_documents == advanced.corpus_documents
            )
            result["reference_census_postings"] = census.postings_visited
            started = perf_counter()
            exported = export_graph(db, root / "artifact")
            result["export_ms"] = (perf_counter() - started) * 1000
            result["artifact_bytes"] = exported.bytes
            assert not db.verify("all").findings
        started = perf_counter()
        imported = import_graph(root / "artifact", root / "imported")
        result["import_verify_ms"] = (perf_counter() - started) * 1000
        assert imported.rows == 201
        with connect(root / "imported") as db:
            assert db.search_text(index="text", query="document200").hits
        result["documents_after_write"] = 201
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
