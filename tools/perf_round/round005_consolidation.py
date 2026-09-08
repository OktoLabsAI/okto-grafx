"""One synthetic Pulse consolidation, real Community SQLite/Grafx/outbox/ACK.

Run in a fresh subprocess with matching OKTO_PULSE_{CORE,COMMUNITY}_REPO paths.
Only the temporary identity is used. No live specs, external embeddings or workers.
"""
from __future__ import annotations

import asyncio
import cProfile
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

import okto_grafx


async def exercise(root: Path) -> dict:
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.core.infra.config import configure_settings
    settings = CommunitySettings(data_dir=str(root), kg_graph_backend="grafx", kg_global_graph_backend="grafx",
                                 kg_embedding_mode="stub", kg_cleanup_enabled=False)
    configure_settings(settings)
    from okto_pulse.community.adapters import sqlalchemy_database as relational
    from okto_pulse.community.adapters.relational_schema_lifecycle import register_community_relational_schema_lifecycle
    from okto_pulse.community.adapters.sqlalchemy_models import Board, Spec, KuzuNodeRef, GlobalUpdateOutbox
    from okto_pulse.community.adapters.composition import configure_community_kg_registry, require_community_routed_graph_composition
    from okto_pulse.community.adapters.relational_effects import register_community_relational_effects
    from okto_pulse.community.adapters.coordination import register_community_coordination_providers
    from okto_pulse.core.kg.interfaces.registry import get_kg_registry
    from okto_pulse.core.kg import primitives
    from okto_pulse.core.kg.schemas import (NodeCandidate, KGNodeType, EdgeCandidate, KGEdgeType,
        BeginConsolidationRequest, AddEdgeCandidateRequest, ProposeReconciliationRequest,
        CommitConsolidationRequest)
    from okto_pulse.core.application.processors.global_outbox import GlobalOutboxProcessor
    from sqlalchemy import select, func

    report = {"version": okto_grafx.__version__, "scope": "synthetic Community composition + real SQLite, Grafx, primitives and outbox ACK", "phases": {}}
    grafx_root = Path(__file__).resolve().parents[2]
    measured_sources = [Path(__file__).resolve(), Path(primitives.__file__),
                        grafx_root / "src/okto_grafx/api/assembly.py",
                        grafx_root / "src/okto_grafx/engine/query_engine.py",
                        grafx_root / "src/okto_grafx/engine/ordered_index.py"]
    report["source_sha256"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in measured_sources}
    report["consolidation_phases"] = []
    class PhaseCapture(logging.Handler):
        def emit(self, record):
            if getattr(record, "event", None) == "kg.consolidation.phase":
                report["consolidation_phases"].append({
                    "phase": record.phase, "outcome": record.outcome,
                    "elapsed_ms": round(record.elapsed_s * 1000, 3)})
    logging.getLogger("okto_pulse.core.kg.consolidation_timing").addHandler(PhaseCapture())
    async def phase(name, operation):
        started = time.perf_counter()
        try:
            return await operation()
        finally:
            elapsed = round((time.perf_counter() - started) * 1000, 3)
            report["phases"][name] = elapsed
            print(f"PHASE {name}: {elapsed} ms", file=sys.stderr, flush=True)

    relational.configure_community_database(f"sqlite+aiosqlite:///{root / 'pulse.db'}")
    register_community_relational_schema_lifecycle()
    await phase("sqlite_schema", relational.init_db)
    factory = relational.get_session_factory()
    register_community_relational_effects(settings=settings)
    register_community_coordination_providers()
    configure_community_kg_registry(factory, settings=settings)
    registry = get_kg_registry()
    if "--profile-reconcile" in sys.argv:
        from tools.perf_round.round005_pulse import profile_top
        find_matches = primitives._find_existing_graph_matches
        def profiled_matches(*args, **kwargs):
            profile = cProfile.Profile()
            try:
                return profile.runcall(find_matches, *args, **kwargs)
            finally:
                report["reconcile_profile"] = profile_top(profile)
        primitives._find_existing_graph_matches = profiled_matches
    board_id, spec_id = str(uuid.uuid4()), str(uuid.uuid4())
    agent = "system:layer1_worker"
    async with factory() as db:
        db.add(Board(id=board_id, name="Grafx isolated benchmark", owner_id="benchmark"))
        db.add(Spec(id=spec_id, board_id=board_id, title="Synthetic consolidation",
                    description="Temporary benchmark, never a production spec.",
                    created_by="benchmark", status="done"))
        await db.commit()
    await phase("board_schema", lambda: registry.graph_schema_manager.ensure_bootstrapped(board_id))
    def initialize_global():
        from okto_pulse.core.kg.global_discovery_writer import GlobalDiscoveryWriterLease
        lease = GlobalDiscoveryWriterLease.acquire(operation="synthetic_global_init")
        try:
            with lease.guard():
                require_community_routed_graph_composition().initialize_global_route()
        finally:
            lease.release()
    await phase("global_schema", lambda: asyncio.to_thread(initialize_global))
    nodes = [
        NodeCandidate(candidate_id="board", node_type=KGNodeType.ENTITY, title="Synthetic board",
                      content="Fixture board root", source_artifact_ref=f"board:{board_id}", source_confidence=1.0),
        NodeCandidate(candidate_id="root", node_type=KGNodeType.ENTITY, title="Synthetic spec",
                      content="Fixture root", source_artifact_ref=f"spec:{spec_id}", source_confidence=1.0),
        NodeCandidate(candidate_id="requirement", node_type=KGNodeType.REQUIREMENT, title="Preserve durability",
                      content="ACK only after durable graph effects", source_artifact_ref=f"spec:{spec_id}:fr:0", source_confidence=1.0),
        NodeCandidate(candidate_id="decision", node_type=KGNodeType.DECISION, title="Use durable outbox",
                      content="Mirror the decision and verify before ACK", source_artifact_ref=f"spec:{spec_id}:decision:0", source_confidence=1.0),
    ]
    begin = await phase("begin", lambda: primitives.begin_consolidation(
        BeginConsolidationRequest(board_id=board_id, artifact_type="spec", artifact_id=spec_id,
                                  raw_content="Synthetic benchmark", deterministic_candidates=nodes),
        agent_id=agent, db=None))
    for source, target, kind in (("root", "board", KGEdgeType.BELONGS_TO),
                                  ("requirement", "root", KGEdgeType.BELONGS_TO),
                                  ("decision", "root", KGEdgeType.BELONGS_TO),
                                  ("decision", "requirement", KGEdgeType.DERIVES_FROM)):
        await primitives.add_edge_candidate(AddEdgeCandidateRequest(session_id=begin.session_id,
            candidate=EdgeCandidate(candidate_id=f"{source}-{target}", edge_type=kind,
                                    from_candidate_id=source, to_candidate_id=target, confidence=1.0)), agent_id=agent)
    await phase("reconcile", lambda: primitives.propose_reconciliation(
        ProposeReconciliationRequest(session_id=begin.session_id), agent_id=agent, db=None))
    async with factory() as db:
        commit = await phase("commit_consolidation", lambda: primitives.commit_consolidation(
            CommitConsolidationRequest(session_id=begin.session_id, summary_text="Synthetic measured consolidation"),
            agent_id=agent, db=db, defer_session_finalization=True))
        await phase("sqlite_commit", db.commit)
    await phase("finalize", lambda: primitives.finalize_deferred_consolidation(begin.session_id, agent_id=agent))
    assert commit.nodes_added >= 4 and commit.edges_added >= 4, commit
    report["nodes_added"], report["edges_added"] = commit.nodes_added, commit.edges_added
    worker = GlobalOutboxProcessor(relational_scope_factory=factory, interval_seconds=5)
    apply_event = worker._apply_event
    verify = worker._verify_processed_batch
    async def measured_apply(*args, **kwargs):
        try:
            return await phase("outbox_apply", lambda: apply_event(*args, **kwargs))
        except Exception as exc:
            print(f"OUTBOX FAILURE {type(exc).__name__}: {getattr(exc, 'details', {})}", file=sys.stderr)
            raise
    def measured_verify(*args, **kwargs):
        started = time.perf_counter()
        try:
            return verify(*args, **kwargs)
        finally:
            report["phases"]["outbox_flush_reopen_verify"] = round((time.perf_counter() - started) * 1000, 3)
    worker._apply_event = measured_apply
    worker._verify_processed_batch = measured_verify
    processed = await phase("outbox_including_ack", worker.process_once)
    async with factory() as db:
        refs = (await db.execute(select(func.count()).select_from(KuzuNodeRef).where(KuzuNodeRef.board_id == board_id))).scalar_one()
        events = list((await db.execute(select(GlobalUpdateOutbox).where(GlobalUpdateOutbox.board_id == board_id))).scalars())
    assert processed >= 1, [(e.event_type, e.last_error, e.retry_count) for e in events]
    assert refs >= 3 and events and all(e.processed_at is not None for e in events), (refs, [(e.id, e.processed_at) for e in events])
    report["refs"], report["acked_events"] = refs, len(events)
    assert await phase("outbox_idempotent_empty", worker.process_once) == 0
    runtime = registry.require_global_discovery_runtime()
    digests = runtime.execute("MATCH (n:DecisionDigest) RETURN count(n)")
    report["global_digest_rows"] = digests.rows
    assert digests.rows[0][0] >= 1, digests
    return report


async def run_isolated(root: Path):
    try:
        return await exercise(root)
    finally:
        from okto_pulse.community.adapters import sqlalchemy_database as relational
        from okto_pulse.community.adapters.composition import require_community_routed_graph_composition
        try:
            bundle = require_community_routed_graph_composition()
        except RuntimeError:
            pass  # Startup may fail before it publishes the isolated composition.
        else:
            bundle.grafx_pool.close_all()
        if relational.is_database_runtime_configured():
            await relational.close_db()


def main():
    assert Path(okto_grafx.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2] / "src")
    for name in ("CORE", "COMMUNITY"):
        repo = Path(os.environ[f"OKTO_PULSE_{name}_REPO"]).resolve()
        assert (repo / "src/okto_pulse").is_dir()
        sys.path.insert(0, str(repo / "src"))
    logging.basicConfig(level=logging.INFO)
    with tempfile.TemporaryDirectory(prefix="grafx-v005-consolidation-") as raw:
        root = Path(raw)
        os.environ.update(DATA_DIR=raw, OKTO_PULSE_DATA_DIR=raw,
                          DATABASE_URL=f"sqlite+aiosqlite:///{root / 'pulse.db'}",
                          KG_BASE_DIR=str(root / "boards"), KG_GRAPH_BACKEND="grafx", KG_GLOBAL_GRAPH_BACKEND="grafx", KG_EMBEDDING_MODE="stub")
        print(json.dumps(asyncio.run(run_isolated(root)), indent=2))


if __name__ == "__main__":
    main()
