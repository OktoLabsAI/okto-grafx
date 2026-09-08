"""Opt-in consumer attribution using fresh stores and real Pulse read adapters.

Requires matching Community/Core source paths in OKTO_PULSE_{COMMUNITY,CORE}_REPO.
No live Pulse identity, workers, SQLite, credentials or reserved specs are loaded.
The HTTP fixture replaces authorization and service result caching, not graph IO.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
import gc
import hashlib
import json
import os
from pathlib import Path
import pstats
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import psutil

import okto_grafx
from okto_grafx import Timestamp, connect


def timed(operation):
    wall, cpu = time.perf_counter(), time.process_time()
    result = operation()
    return result, {"wall_ms": round((time.perf_counter() - wall) * 1000, 3),
                    "process_cpu_ms": round((time.process_time() - cpu) * 1000, 3)}


def profile_top(profile, limit=25):
    stats = pstats.Stats(profile)
    return [{"function": f"{Path(key[0]).name}:{key[1]}:{key[2]}",
             "calls": value[1], "self_ms": round(value[2] * 1000, 3),
             "cumulative_ms": round(value[3] * 1000, 3)}
            for key, value in sorted(stats.stats.items(), key=lambda item: item[1][3],
                                     reverse=True)[:limit]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=1100)
    parser.add_argument("--serve", action="store_true", help="Serve only the isolated GET route on 18105")
    parser.add_argument("--profile-open", action="store_true", help="Attribute open cost; timings include profiling overhead")
    args = parser.parse_args()
    assert 1 <= args.nodes <= 4096
    source = Path(__file__).resolve().parents[2]
    assert Path(okto_grafx.__file__).resolve().is_relative_to(source / "src")
    for name in ("CORE", "COMMUNITY"):
        repo = Path(os.environ[f"OKTO_PULSE_{name}_REPO"]).resolve()
        assert (repo / "src/okto_pulse").is_dir()
        sys.path.insert(0, str(repo / "src"))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from okto_pulse.core.kg import kg_service
    from okto_pulse.community.api import kg_routes
    from okto_pulse.community.adapters.grafx_cypher_executor import CommunityGrafxCypherExecutor
    from okto_pulse.community.adapters.routed_board_graph_facades import CommunityRoutedCypherExecutor
    from okto_pulse.community.adapters.grafx_schema_bootstrap import ensure_current_grafx_board_schema
    from okto_pulse.community.adapters.grafx_global_discovery import ensure_current_grafx_global_schema
    from okto_pulse.community.adapters.grafx_global_discovery_runtime import CommunityGrafxGlobalDiscoveryRuntime

    report = {"version": okto_grafx.__version__, "nodes": args.nodes,
              "scope": "synthetic real Pulse service/route/adapter; fixture auth; no live workers",
              "source_sha256": {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (Path(__file__).resolve(), source / "src/okto_grafx/engine/query_engine.py",
                                          source / "src/okto_grafx/api/assembly.py",
                                          source / "src/okto_grafx/engine/index_manager.py",
                                          source / "src/okto_grafx/engine/ordered_index.py",
                                          source / "src/okto_grafx/engine/heap_store.py")}}
    adapter_source = Path(os.environ["OKTO_PULSE_COMMUNITY_REPO"]) / (
        "src/okto_pulse/community/adapters/grafx_graph_transaction.py")
    report["source_sha256"][str(adapter_source)] = hashlib.sha256(adapter_source.read_bytes()).hexdigest()
    process = psutil.Process()
    report["rss_start_bytes"] = process.memory_info().rss
    with tempfile.TemporaryDirectory(prefix="grafx-v005-pulse-") as raw:
        root = Path(raw)
        with connect(root / "board") as db:
            _, report["schema"] = timed(lambda: ensure_current_grafx_board_schema(
                db, board_id="benchmark", bootstrapped_at=Timestamp(micros=1)))
            start = time.perf_counter()
            for start_id in range(0, args.nodes, 128):
                with db.begin("write") as tx:
                    for i in range(start_id, min(start_id + 128, args.nodes)):
                        tx.execute("CREATE (:Decision {id:$id,title:$title,created_at:$ts,"
                                   "source_confidence:1.0,relevance_score:1.0,graph_layer:'canonical'})",
                                   {"id": f"d{i:05}", "title": f"Decision {i}",
                                    "ts": Timestamp(micros=1_700_000_000_000_000 + i)})
            # A deliberately skewed hub, crossing both UI pages.
            with db.begin("write") as tx:
                for i in range(1, min(args.nodes, 129)):
                    tx.execute("MATCH (a:Decision {id:$a}),(b:Decision {id:$b}) "
                               "CREATE (a)-[:supersedes__Decision__Decision {confidence:0.9}]->(b)",
                               {"a": f"d{args.nodes - 1:05}", "b": f"d{i:05}"})
            report["seed_ms"] = round((time.perf_counter() - start) * 1000, 3)
            db.checkpoint()
        open_profile = cProfile.Profile()
        if args.profile_open:
            open_profile.enable()
        reader, report["cold_open"] = timed(lambda: connect(root / "board"))
        if args.profile_open:
            open_profile.disable()
            report["open_profile"] = profile_top(open_profile)
        with reader:
            executor = CommunityGrafxCypherExecutor(lambda _board: reader)
            routed = CommunityRoutedCypherExecutor(
                None, ladybug=None, grafx=executor, operation_window=lambda _board: nullcontext())
            service = kg_service.KGService(emit_hit_events=False)
            phases = []

            def phase(_board, _operation, name, started):
                now = time.perf_counter()
                phases.append({"phase": name, "ms": round((now - started) * 1000, 3)})
                return now

            async def allowed(**_kwargs):
                return SimpleNamespace(allowed=True)

            app = FastAPI()
            app.add_api_route("/kg/boards/{board_id}/graph", kg_routes.get_subgraph)
            app.dependency_overrides[kg_routes.require_kg_board_actor] = lambda: None
            app.dependency_overrides[kg_routes.get_unit_of_work] = lambda: None
            with (patch.object(routed, "_provider", lambda _board: executor),
                  patch.object(kg_service, "_get_cypher_executor", lambda: routed),
                  patch.object(service, "_cached_call", lambda _op, _board, _params, call: call()),
                  patch.object(kg_routes, "get_kg_service", lambda: service),
                  patch.object(kg_routes, "resolve_cypher_executor", lambda: routed),
                  patch.object(kg_routes, "_code_traceability_kg_read_access", allowed),
                  patch.object(kg_routes, "_record_read_phase", phase),
                  TestClient(app) as client):
                observations = []
                expected = [f"d{i:05}" for i in reversed(range(args.nodes))]
                for repeat in range(2):
                    cursor = ""
                    for page in range(2):
                        phases.clear()
                        response, timing = timed(lambda: client.get(
                            "/kg/boards/benchmark/graph", params={"limit": 500, "cursor": cursor}))
                        assert response.status_code == 200, response.text
                        body = response.json()
                        assert [row["id"] for row in body["nodes"]] == expected[page*500:(page+1)*500]
                        ids = {row["id"] for row in body["nodes"]}
                        expected_edges = {(f"d{args.nodes-1:05}", f"d{i:05}")
                                          for i in range(1, min(args.nodes, 129))
                                          if f"d{args.nodes-1:05}" in ids or f"d{i:05}" in ids}
                        actual_edges = {(e["source"], e["target"]) for e in body["edges"]}
                        assert actual_edges == expected_edges, {
                            "missing": sorted(expected_edges - actual_edges)[:5],
                            "unexpected": sorted(actual_edges - expected_edges)[:5],
                            "metadata": body["metadata"], "page": page,
                        }
                        assert body["metadata"]["edge_tables_failed"] == 0, body["metadata"]
                        observations.append({"repeat": repeat, "page": page, **timing,
                                             "phases": list(phases), "bytes": len(response.content),
                                             "rows": len(ids), "metadata": body["metadata"]})
                        cursor = body["next_cursor"]
                        if cursor is None:
                            break
                report["http"] = observations
                report["total_nodes"] = service.count_all_nodes("benchmark")
                assert report["total_nodes"] == args.nodes
                profile = cProfile.Profile()
                profile.enable()
                service.get_all_nodes("benchmark", max_rows=500)
                kg_routes._fetch_edges_for_nodes("benchmark", set(expected[:500]),
                                                node_types_by_id={n: "Decision" for n in expected[:500]})
                profile.disable()
                report["kg_profile"] = profile_top(profile)
                # Keep preparation attribution separate from nested execution time.
                # A public batch API is not justified merely by a fan-out count.
                report["kg_dispatch_profile"] = [
                    {"function": f"{Path(key[0]).name}:{key[1]}:{key[2]}",
                     "calls": value[1], "self_ms": round(value[2] * 1000, 3),
                     "cumulative_ms": round(value[3] * 1000, 3)}
                    for key, value in pstats.Stats(profile).stats.items()
                    if key[2] in {"_prepare", "_grafx_query_parameters", "_bind_parameters",
                                  "parse", "plan", "_prepared_plan_key", "_clone_bound_plan"}
                ]
                from okto_grafx.engine import query_engine
                with (patch.object(query_engine, "parse_text", wraps=query_engine.parse_text) as parse,
                      patch.object(query_engine, "build_plan", wraps=query_engine.build_plan) as plan):
                    service.get_all_nodes("benchmark", max_rows=500)
                    kg_routes._fetch_edges_for_nodes("benchmark", set(expected[:500]),
                                                    node_types_by_id={n: "Decision" for n in expected[:500]})
                report["warm_preparation"] = {"parse_builds": parse.call_count, "plan_builds": plan.call_count}
                assert parse.call_count == plan.call_count == 0
                report["plan_cache_entries"] = len(reader._queries._plan_cache)
                report["plan_cache_tariff_bytes"] = reader._queries._plan_cache_bytes
                from okto_pulse.core.kg import cypher_templates as tpl
                parameters = {"min_confidence": 0.0, "min_relevance": 0.0,
                              "graph_layer": "canonical", "include_code_traceability": True,
                              "max_rows": 500}
                report["native_page_statistics"] = dict(reader.execute(tpl.GET_ALL_NODES, parameters).statistics)
                report["concurrency"] = []
                for participants in (1, 2, 4):
                    barrier = threading.Barrier(participants)

                    def worker(slot):
                        from okto_grafx.errors import GrafxWriteConflict
                        with connect(root / "board") as db:
                            barrier.wait(timeout=20)
                            wall, cpu = time.perf_counter(), time.thread_time()
                            retries = 0
                            for step in range(2):
                                # Same actual page shape for every participant, independent handles.
                                result = db.execute(tpl.GET_ALL_NODES, parameters)
                                assert [row[0] for row in result.rows] == expected[:500]
                                if slot % 2 == 0:
                                    for attempt in range(8):
                                        try:
                                            with db.begin("write") as tx:
                                                tx.execute("MATCH (n:Decision {id:$id}) SET n.title=$title",
                                                           {"id": f"d{slot:05}", "title": f"writer-{slot}-{step}"})
                                            break
                                        except GrafxWriteConflict:
                                            retries += 1
                                            if attempt == 7:
                                                raise
                            elapsed = time.perf_counter() - wall
                            used_cpu = time.thread_time() - cpu
                            return {"slot": slot, "wall_ms": round(elapsed * 1000, 3),
                                    "thread_cpu_ms": round(used_cpu * 1000, 3),
                                    "non_cpu_ms": round(max(0, elapsed-used_cpu)*1000, 3),
                                    "conflict_retries": retries}

                    with ThreadPoolExecutor(max_workers=participants) as workers:
                        values = list(workers.map(worker, range(participants)))
                    report["concurrency"].append({"participants": participants, "workers": values})
                    for slot in range(0, participants, 2):
                        assert reader.execute("MATCH (n:Decision {id:$id}) RETURN n.title",
                                              {"id": f"d{slot:05}"}).rows == ((f"writer-{slot}-1",),)
                if args.serve:
                    import uvicorn
                    print("Isolated Grafx 0.0.5 GET fixture ready on 127.0.0.1:18105", flush=True)
                    uvicorn.run(app, host="127.0.0.1", port=18105, log_level="warning")
        with connect(root / "global") as global_db:
            from okto_pulse.core.application.processors.global_outbox import GlobalOutboxProcessor
            processor = GlobalOutboxProcessor(lambda: None)
            _, report["global_schema"] = timed(lambda: ensure_current_grafx_global_schema(global_db))
            fences = []
            runtime = CommunityGrafxGlobalDiscoveryRuntime(
                lambda: global_db, lambda: root / "global", lambda: None,
                lambda phase: fences.append((phase, time.perf_counter())))
            vector = [1.0, *([0.0] * 383)]
            _, report["global_board_write"] = timed(lambda: runtime.upsert_board_summary(
                board_id="benchmark", name="Benchmark", summary="Synthetic", summary_embedding=vector,
                decision_count=4, synced_at="2026-09-08T00:00:00Z"))
            writes = []
            for i in range(4):
                fences.clear()
                _, timing = timed(lambda: runtime.upsert_decision_digest(
                    digest_id=f"digest-{i}", board_id="benchmark", original_node_id=f"d{i:05}",
                    title="Synthetic", summary="No production source", node_type="Decision",
                    graph_layer="canonical", embedding=vector, created_at="2026-09-08T00:00:00Z"))
                _, link = timed(lambda: runtime.link_board_digest(board_id="benchmark", digest_id=f"digest-{i}"))
                writes.append({"digest": timing, "link": link,
                               "fence_intervals": [{"phase": b[0], "ms": round((b[1]-a[1])*1000, 3)}
                                                   for a, b in zip(fences, fences[1:])]})
            report["global_writes"] = writes
            assert global_db.execute("MATCH (:Board)-[r:CONTAINS_DECISION]->(:DecisionDigest) RETURN count(r)").rows == ((4,),)
            def inventory():
                digests, digest_time = timed(lambda: processor._read_global_digest_rows(runtime, "benchmark"))
                outgoing, outgoing_time = timed(lambda: processor._read_board_digest_edge_rows(runtime, "benchmark"))
                inbound, inbound_time = timed(lambda: processor._read_digest_inbound_edge_counts(runtime, "benchmark"))
                assert len(digests) == len(outgoing) == len(inbound) == 4
                assert all(n == 1 for n in inbound.values())
                return {"digests": digest_time, "outgoing": outgoing_time, "inbound": inbound_time}

            # Real Core dispatch, not the live relational/outbox loop. Loop startup included.
            inventories, dispatch = timed(lambda: asyncio.run(processor._run_graph_io(inventory)))
            report["global_inventory"] = inventories
            report["global_inventory_dispatch_including_loop_start"] = dispatch
            verification, report["global_verify"] = timed(lambda: global_db.verify("all"))
            assert verification.findings == ()
            _, report["global_checkpoint"] = timed(global_db.checkpoint)
        with connect(root / "global") as reopened:
            assert reopened.execute("MATCH (d:DecisionDigest) RETURN count(d)").rows == ((4,),)
        gc.collect()
        report["rss_end_bytes"] = process.memory_info().rss
        report["peak_working_set_bytes"] = getattr(process.memory_info(), "peak_wset", None)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
