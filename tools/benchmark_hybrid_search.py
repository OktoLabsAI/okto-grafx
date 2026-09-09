"""Fixed native hybrid conformance/latency sample; no timing threshold or model service."""

from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from okto_grafx import connect, HybridSearchOptions


def main() -> None:
    """Measure one cold plus 20 warm searches and compare independent native source ranks."""
    result = {"documents": 200, "dimensions": 2, "candidate_k": 100, "result_k": 20}
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "db"
        with connect(path, vector_exact_scan_threshold=0, vector_ef_search=256) as db:
            with db.begin() as tx:
                tx.execute("CREATE VECTOR SPACE semantic {dimension:2,metric:'cosine'}")
                tx.execute(
                    "CREATE NODE TABLE D(id INT64,body STRING,v VECTOR(semantic),PRIMARY KEY(id))"
                )
                for n in range(200):
                    tx.execute(
                        "CREATE (:D {id:$id,body:$body,v:$v})",
                        {
                            "id": n,
                            "body": f"graph durable group{n % 10}",
                            "v": [
                                math.cos(n / 200 * math.pi * 2),
                                math.sin(n / 200 * math.pi * 2),
                            ],
                        },
                    )
            db.create_text_index("text", "D", ("body",), bucket_count=64)
            samples = []
            for _ in range(21):
                start = perf_counter()
                answer = db.search_hybrid(
                    table="D",
                    index="text",
                    query="group3",
                    space="semantic",
                    vector=(1.0, 0.0),
                    k=20,
                )
                samples.append((perf_counter() - start) * 1000)
                assert len(answer.hits) == 20 and answer.regime == "complete"
            result.update(
                cold_ms=samples[0],
                warm_median_ms=median(samples[1:]),
                warm_max_ms=max(samples[1:]),
                warm_samples=20,
                vector_regime=answer.vector_regime,
                graph_edges=answer.graph_edges_visited,
            )
            with db.begin("read") as reader:
                lexical = db.search_text(reader, index="text", query="group3", k=100)
                vector = db.search_vectors(
                    reader, space="semantic", query=(1.0, 0.0), k=100
                )
                scores = {}
                for source in (lexical.hits, vector.hits):
                    for rank, hit in enumerate(source, 1):
                        scores[hit.record_id] = scores.get(hit.record_id, 0) + 1 / (
                            60 + rank
                        )
                expected = sorted(scores, key=lambda rid: (-scores[rid], rid))[:20]
                assert [h.record_id for h in answer.hits] == expected
                assert [h.score for h in answer.hits] == [
                    scores[rid] for rid in expected
                ]
                exact = db.search_hybrid(
                    reader,
                    table="D",
                    index="text",
                    query="group3",
                    space=None,
                    vector=(),
                    k=20,
                    options=HybridSearchOptions(vector_weight=0),
                )
                assert [h.record_id for h in exact.hits] == [
                    h.record_id for h in lexical.hits[:20]
                ]
            result["native_source_rrf_conformance"] = True
        with connect(path, vector_exact_scan_threshold=4096) as db:
            reference = db.search_hybrid(
                table="D",
                index="text",
                query="group3",
                space="semantic",
                vector=(1.0, 0.0),
                k=20,
            )
            result["fused_recall_at_20_against_exact_source"] = (
                len(
                    {h.record_id for h in answer.hits}
                    & {h.record_id for h in reference.hits}
                )
                / 20
            )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
