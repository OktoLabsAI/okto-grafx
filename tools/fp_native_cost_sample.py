"""Fixed-size native FP-8 observations, no speedup claim or performance threshold."""

import argparse
from dataclasses import asdict
import importlib.util
from importlib.metadata import version
import json
from pathlib import Path
import platform
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reference", ROOT / "tools/qualify_parity_references.py")
reference = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reference)


def timed(operation):
    started = time.perf_counter()
    value = operation()
    return value, round((time.perf_counter() - started) * 1000, 6)


def write(db, query, parameters=None):
    with db.begin("write") as tx:
        result = tx.execute(query, parameters)
        rows = result.rows
    return rows


def sample(root, size):
    from okto_grafx import connect
    from okto_grafx.runtime.config import DatabaseConfig
    path = root / ("chain-" + str(size))
    assert not path.exists()
    result = {"nodes": size, "edges": size - 1, "topology": "directed chain, id 0..n-1, initial v=2*id",
              "config": asdict(DatabaseConfig(path=str(path))), "measurements": []}
    with connect(path) as db:
        db.ensure_identity_indexes()
        def schema():
            with db.begin("write") as tx:
                tx.execute("CREATE NODE TABLE N(id INT64,v INT64,PRIMARY KEY(id))")
                tx.execute("CREATE REL TABLE R(FROM N TO N)")
        _, result["schema_ms"] = timed(schema)
        query = "UNWIND range(0,$last) AS i CREATE(n:N {id:i,v:2*i}) RETURN count(n) AS total"
        rows, result["seed_nodes_ms"] = timed(lambda: write(db, query, {"last": size - 1}))
        assert rows == ((size,),)
        print("SEEDED_NODES", size, result["seed_nodes_ms"], flush=True)
        scalar_seed = ("UNWIND range(0,$last) AS i MATCH(a:N {id:i}),(b:N {id:i+1}) "
                       "CREATE(a)-[r:R]->(b) RETURN count(r) AS total")
        query = ("UNWIND $rows AS row MATCH(a:N {id:row.source}),(b:N {id:row.target}) "
                 "CREATE(a)-[r:R]->(b) RETURN count(r) AS total")
        result["seed_scalar_plan"] = [node.label for node in db.explain(scalar_seed).walk()]
        result["seed_batch_plan"] = [node.label for node in db.explain(query).walk()]
        assert result["seed_batch_plan"].count("IndexSeek") == 2
        result["seed_edges_query"] = query
        result["seed_edges_input"] = {"rows": [{"source": i, "target": i+1} for i in range(size-1)]}
        rows, result["seed_edges_ms"] = timed(lambda: write(db, query, result["seed_edges_input"]))
        assert rows == ((size - 1,),)
        print("SEEDED_EDGES", size - 1, result["seed_edges_ms"], flush=True)
        _, result["pre_read_checkpoint_ms"] = timed(db.checkpoint)
        queries = [
            ("indexed_lookup", "MATCH(n:N {id:$id}) RETURN n.v AS value", {"id": size - 1}, ((2*(size-1),),)),
            ("sorted_page_50", "MATCH(n:N) RETURN n.id AS id ORDER BY id LIMIT 50", {}, tuple((i,) for i in range(50))),
            ("full_aggregate", "MATCH(n:N) RETURN count(n) AS total,sum(n.v) AS sum", {}, ((size,size*(size-1)),)),
            ("named_trail_0_to_3", "MATCH p=(a:N {id:0})-[:R*0..3]->(b) RETURN length(p) AS hops,b.id AS id ORDER BY hops,id",
             {}, ((0,0),(1,1),(2,2),(3,3))),
        ]
        for name, query, parameters, expected in queries:
            times = []
            for _ in range(5):
                observed, elapsed = timed(lambda: db.execute(query, parameters, timeout_seconds=30).rows)
                assert observed == expected, (name, observed)
                times.append(elapsed)
            result["measurements"].append({"operation": name, "query": query, "parameters": parameters,
                                            "expected_rows": reference.tagged(expected), "samples_ms": times,
                                            "first_ms": times[0], "subsequent_median_ms": statistics.median(times[1:]),
                                            "boundary": "public autocommit read including result materialization; "
                                                        "first query call, then four calls on same live handle"})
        times = []
        query = "UNWIND range(0,31) AS i MATCH(n:N {id:i}) SET n.v=n.v+1 RETURN count(n) AS total"
        for _ in range(3):
            rows, elapsed = timed(lambda: write(db, query))
            assert rows == ((32,),)
            times.append(elapsed)
        result["measurements"].append({"operation": "durable_update_32", "query": query,
                                        "samples_ms": times, "median_ms": statistics.median(times),
                                        "boundary": "begin + execute 32 distinct-node updates + durable COMMIT; "
                                                    "three sequential transactions, no warmup write excluded"})
        # A cold read-only opener requires checkpoint-complete authority. Keep
        # this maintenance cost explicit and outside the measured commits.
        _, result["post_write_checkpoint_ms"] = timed(db.checkpoint)
    with connect(path, read_only=True) as db:
        assert db.execute("MATCH(n:N) RETURN n.id,n.v ORDER BY n.id").rows == tuple(
            (i, 2*i + (3 if i < 32 else 0)) for i in range(size))
        assert db.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((size-1,),)
        verification, elapsed = timed(lambda: db.verify("all"))
        assert not verification.findings, verification
        result["reopened_exact_values"] = True
        result["verify_ms"] = elapsed
        result["verify"] = str(verification)
    print("CASE_COMPLETE", size, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    import okto_grafx
    proof = reference.package_proof(okto_grafx, args.wheel.resolve())
    report = {"scope": "Two bounded synthetic sizes, not a large-production-graph or Pulse throughput benchmark; "
                       "recorded while a separate full regression runs, no machine-idle certification, no thresholds",
              "package": proof, "python": sys.version, "platform": platform.platform(),
              "numpy": version("numpy"), "crc32c": version("google-crc32c"), "status": "running", "cases": []}
    receipt = args.output / "report.json"
    receipt.write_text(json.dumps(report, indent=2), encoding="utf-8")
    try:
        for size in (64, 1024):
            report["cases"].append(sample(args.output, size))
            receipt.write_text(json.dumps(report, indent=2), encoding="utf-8")
        assert reference.package_proof(okto_grafx, args.wheel.resolve()) == proof
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        receipt.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
