"""Focused, short tests for the post-drain Pulse card driver and its hooks."""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.errors import GrafxConfigurationError
from okto_grafx.domain.model.record import RecordHeader
from okto_grafx.engine import heap_store, query_engine
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.database import Database, Transaction
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import IndexManager
from okto_grafx.engine.query_engine import QueryEngine
from okto_grafx.engine.vector_engine import VectorEngine
from tools.perf_round import baseline_runs, board_copy, receipt, replay_pulse_card
from tools.perf_round.pulse_card_instrumentation import (
    InstrumentationError,
    PulseCardInstrumentation,
    null_instrumentation_report,
)
from tools.perf_round.replay_pulse_card import (
    DriverRefused,
    PAUSE_NON_TARGET_SQL,
    PENDING_QUEUE_SQL,
    SUPPORTED_PULSE_BUFFER_BUDGET_BYTES,
    _apply_thermal_protocol,
    _manifest_subtree,
    _require_serve_lock_released,
    _validate_supported_run_config,
    _validate_runner_parent,
    _validate_target_row,
    assert_one_target_audit,
    assert_only_target_consumed,
    public_snapshot,
    snapshot_rows,
)


def _descriptors() -> dict[str, object]:
    return {
        "connect": inspect.getattr_static(okto_grafx, "connect"),
        "header_decode": inspect.getattr_static(RecordHeader, "decode"),
        "decode_tuple": inspect.getattr_static(heap_store, "decode_tuple"),
        "buffer_read": inspect.getattr_static(BufferPool, "_read_page"),
        "heap_walk": inspect.getattr_static(HeapStore, "_walk"),
        "heap_read": inspect.getattr_static(HeapStore, "read"),
        "heap_scan": inspect.getattr_static(HeapStore, "scan"),
        "heap_require_endpoints": inspect.getattr_static(
            HeapStore, "require_endpoints"
        ),
        "heap_lookup": inspect.getattr_static(HeapStore, "lookup"),
        "endpoint_identity": inspect.getattr_static(
            query_engine, "_visible_identity_with_ref"
        ),
        "materialise_edge": inspect.getattr_static(query_engine, "_materialise_edge"),
        "index_lookup": inspect.getattr_static(IndexManager, "lookup"),
        "vector_search": inspect.getattr_static(VectorEngine, "search"),
        "query_execute": inspect.getattr_static(QueryEngine, "execute"),
        "database_begin": inspect.getattr_static(Database, "begin"),
        "database_retry": inspect.getattr_static(Database, "retry"),
        "database_rebuild_vector": inspect.getattr_static(
            Database, "rebuild_vector_index"
        ),
        "transaction_commit": inspect.getattr_static(Transaction, "commit"),
        "transaction_rollback": inspect.getattr_static(Transaction, "rollback"),
    }


def test_raw_marker_installs_no_hooks() -> None:
    before = _descriptors()
    report = null_instrumentation_report()
    assert report["raw_uninstrumented"] is True
    assert report["instrumentation_complete"] is True
    assert _descriptors() == before


def test_instrumentation_restores_exact_descriptors_and_omits_query_text() -> None:
    before = _descriptors()
    probe = PulseCardInstrumentation()
    secret = "low-entropy-secret-literal"
    probe.install()
    try:
        with okto_grafx.connect(":memory:") as database:
            with database.begin("write") as transaction:
                transaction.execute(
                    "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
                )
                transaction.execute(f"CREATE (:Person {{id: 1, name: '{secret}'}})")
            assert database.execute(
                "MATCH (p:Person) WHERE p.id = 1 RETURN p.name"
            ).rows == ((secret,),)
    finally:
        probe.close()

    report = probe.report()
    assert _descriptors() == before
    assert report["instrumentation_complete"] is True
    assert report["statement_total"] == 3
    assert report["counters"]["transaction_commit_succeeded"] == 2
    assert report["database_open_total"] == 1
    assert report["baseline_handle_total"] == 0
    assert report["vector_activity"] == {
        "observed": True,
        "search_calls": 0,
        "search_failures": 0,
        "rebuild_calls": 0,
        "rebuild_failures": 0,
    }
    rendered = json.dumps(report, sort_keys=True)
    assert secret not in rendered
    assert "CREATE NODE TABLE" not in rendered


def test_nested_instrumentation_is_refused_and_first_instance_restores() -> None:
    before = _descriptors()
    first = PulseCardInstrumentation()
    second = PulseCardInstrumentation()
    first.install()
    try:
        with pytest.raises(InstrumentationError, match="another instrumentation"):
            second.install()
    finally:
        first.close()
    assert _descriptors() == before


def test_instrumentation_counts_actual_endpoint_lookups_without_serializing_refs() -> (
    None
):
    probe = PulseCardInstrumentation()
    secret = "endpoint-value-must-not-leak"
    with okto_grafx.connect(":memory:") as database:
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
            transaction.execute(
                "CREATE REL TABLE Knows(FROM Person TO Person, note STRING)"
            )
            transaction.execute(
                f"CREATE (:Person {{id: 1, name: '{secret}'}}), "
                "(:Person {id: 2, name: 'other'})"
            )

        probe.observe_database(database)
        probe.install()
        try:
            with database.begin("write") as transaction:
                transaction.execute(
                    "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
                    "CREATE (a)-[:Knows {note: 'observed'}]->(b)"
                )
        finally:
            probe.close()

        locality = probe.endpoint_hit_locality(database)

    report = probe.report()
    assert report["counters"]["endpoint_validation_calls"] == 1
    assert report["counters"]["endpoint_lookup_calls"] == 2
    assert report["counters"]["endpoint_lookup_hits"] == 2
    assert report["endpoint_hit_coordinates"] == {
        "total": 2,
        "retained": 2,
        "truncated": False,
        "serialized": False,
    }
    assert locality == {
        "semantics": "actual_endpoint_lookup_hits_to_post_workload_tail_pages",
        "exact": True,
        "extra_reads_outside_timed_workload": True,
        "total": 2,
        "distance_pages": {
            "total": 2,
            "min": 0,
            "p50": 0,
            "p90": 0,
            "p99": 0,
            "max": 0,
            "last_10_percent": 2,
            "last_10_percent_ratio": 1.0,
        },
        "p1_4_last_10_percent_threshold_met": True,
        "p1_4_threshold_evaluable": True,
    }
    rendered = json.dumps(report, sort_keys=True)
    assert secret not in rendered
    assert "table_id" not in rendered
    assert "page_id" not in rendered


def test_endpoint_locality_stays_exact_beyond_one_hundred_thousand_hits() -> None:
    """Synthetic: 100_001 hits on one page aggregate exactly with no truncation mode."""
    import tools.perf_round.pulse_card_instrumentation as instrumentation_module

    assert not hasattr(instrumentation_module, "MAX_ENDPOINT_HITS")
    probe = PulseCardInstrumentation()
    hits = 100_001
    with okto_grafx.connect(":memory:") as database:
        with database.begin("write") as transaction:
            transaction.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
            transaction.execute("CREATE (:Person {id: 1}), (:Person {id: 2})")
        probe.observe_database(database)
        table = next(t for t in database.catalog.catalog.tables() if t.name == "Person")
        page = int(database._heap.pages_of(table)[0])
        state = {"hit_page": page}
        for _ in range(hits):
            probe._bump("endpoint_lookup_hits")
            probe._record_endpoint_hit(database._heap, table, state)
        locality = probe.endpoint_hit_locality(database)

    report = probe.report()
    assert report["observation_failures"] == {}
    assert report["endpoint_hit_coordinates"] == {
        "total": hits,
        "retained": hits,
        "truncated": False,
        "serialized": False,
    }
    assert locality["exact"] is True
    assert locality["total"] == hits
    assert locality["distance_pages"]["total"] == hits
    assert locality["distance_pages"]["last_10_percent"] == hits
    assert locality["distance_pages"]["last_10_percent_ratio"] == 1.0
    assert locality["p1_4_threshold_evaluable"] is True


def test_failed_endpoint_validation_restores_context_and_preserves_error() -> None:
    probe = PulseCardInstrumentation()
    with okto_grafx.connect(":memory:") as database:
        with database.begin("write") as transaction:
            transaction.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
            transaction.execute("CREATE REL TABLE Knows(FROM Person TO Person)")
            transaction.execute("CREATE (:Person {id: 1})")
        person = database.catalog.catalog.table("Person")
        knows = database.catalog.catalog.table("Knows")
        with database.begin("read") as transaction:
            probe.observe_database(database)
            probe.install()
            try:
                with pytest.raises(GrafxConfigurationError) as raised:
                    database._heap.require_endpoints(
                        knows, (99, 100), transaction.snapshot
                    )
                assert raised.value.code == "configuration_error"
                assert getattr(probe._lookup_local, "endpoint_depth", 0) == 0
                assert (
                    database._heap.lookup(person, 1, transaction.snapshot) is not None
                )
            finally:
                probe.close()

    report = probe.report()
    assert report["instrumentation_complete"] is True
    assert report["counters"]["endpoint_validation_failed"] == 1
    assert report["counters"]["endpoint_lookup_calls"] == 1
    assert report["counters"]["endpoint_lookup_misses"] == 1
    assert report["counters"]["heap_lookup_calls"] == 2


def test_endpoint_hit_from_an_unbound_database_invalidates_the_sample() -> None:
    probe = PulseCardInstrumentation()
    with (
        okto_grafx.connect(":memory:") as target,
        okto_grafx.connect(":memory:") as foreign,
    ):
        for database in (target, foreign):
            with database.begin("write") as transaction:
                transaction.execute(
                    "CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))"
                )
                transaction.execute("CREATE REL TABLE Knows(FROM Person TO Person)")
                transaction.execute("CREATE (:Person {id: 1}), (:Person {id: 2})")

        probe.observe_database(target)
        probe.install()
        try:
            with foreign.begin("write") as transaction:
                transaction.execute(
                    "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
                    "CREATE (a)-[:Knows]->(b)"
                )
        finally:
            probe.close()

    report = probe.report()
    assert report["instrumentation_complete"] is False
    assert report["observation_failures"] == {"endpoint_hit_coordinate": 2}
    assert report["endpoint_hit_coordinates"]["total"] == 0


def test_preexisting_handle_is_not_reported_as_a_database_open() -> None:
    probe = PulseCardInstrumentation()
    with okto_grafx.connect(":memory:") as database:
        probe.observe_database(database)
    report = probe.report()
    assert report["baseline_handle_total"] == 1
    assert len(report["baseline_handles_observed"]) == 1
    assert report["database_open_total"] == 0
    assert report["database_opens"] == []


def test_observation_memory_failure_does_not_mask_database_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _descriptors()
    probe = PulseCardInstrumentation()
    probe.install()

    def fail_counter() -> dict[str, int]:
        raise MemoryError("simulated probe allocation failure")

    monkeypatch.setattr(probe, "_counter_shard", fail_counter)
    try:
        with okto_grafx.connect(":memory:") as database:
            with database.begin("write") as transaction:
                transaction.execute(
                    "CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))"
                )
                transaction.execute("CREATE (:Person {id: 1})")
            assert database.execute("MATCH (p:Person) RETURN p.id").rows == ((1,),)
    finally:
        probe.close()

    assert _descriptors() == before
    assert probe.report()["instrumentation_complete"] is False


def test_runtime_clone_exception_requires_every_data_home_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in receipt.DATA_HOME_ENV:
        monkeypatch.delenv(name, raising=False)
    live = tmp_path / "not-live"
    monkeypatch.setenv("DATA_DIR", str(live))
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").mkdir()
    (source / "data" / "pulse.db").write_bytes(b"sqlite")
    declared = tmp_path / "declared"
    board_copy.copy_board(
        source,
        declared,
        declare_copy=True,
        drained=False,
        grafx_sha="g",
        pulse_sha="p",
        pulse_core_sha="c",
    )
    clone = tmp_path / "clone"
    baseline_runs._clone_copy(declared, clone)
    for name in receipt.DATA_HOME_ENV:
        monkeypatch.setenv(name, str(clone))

    manifest = receipt.require_declared_copy(clone, allow_effective_data_home=True)
    assert manifest["source"]["cloned_from_declared_copy"]
    with pytest.raises(receipt.LiveBoardRefused, match="live data home"):
        receipt.require_declared_copy(clone)
    monkeypatch.setenv("KG_BASE_DIR", str(tmp_path / "different"))
    with pytest.raises(receipt.LiveBoardRefused, match="mismatches"):
        receipt.require_declared_copy(clone, allow_effective_data_home=True)


def test_queue_isolation_sql_leaves_only_target_pending_with_large_depth() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE consolidation_queue (id TEXT PRIMARY KEY, status TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO consolidation_queue(id, status) VALUES (?, 'pending')",
        [(f"row-{index:03d}",) for index in range(205)],
    )
    target = "row-117"
    updated = connection.execute(
        PAUSE_NON_TARGET_SQL, {"target_queue_id": target}
    ).fetchall()
    pending = connection.execute(PENDING_QUEUE_SQL).fetchall()
    assert len(updated) == 204
    assert {row["status"] for row in updated} == {"paused"}
    assert [row["id"] for row in pending] == [target]


def test_target_validation_rejects_an_orphan_queue_row() -> None:
    board_id = "00000000-0000-0000-0000-000000000001"
    card_id = "00000000-0000-0000-0000-000000000002"
    row = {
        "board_id": board_id,
        "artifact_type": "card",
        "artifact_id": card_id,
        "work_kind": "consolidate",
        "generation": 0,
        "delete_event_id": None,
        "priority": "low",
        "source": "historical_backfill",
        "status": "pending",
        "source_card_id": card_id,
        "source_board_id": board_id,
        "retry_ready": 1,
        "claimed_by_session_id": None,
        "claim_token": None,
        "claimed_at": None,
        "worker_id": None,
        "claim_timeout_at": None,
        "attempts": 0,
    }
    _validate_target_row(row, board_id=board_id, card_id=card_id)
    orphan = dict(row, source_card_id=None, source_board_id=None)
    with pytest.raises(DriverRefused, match="eligible historical card"):
        _validate_target_row(orphan, board_id=board_id, card_id=card_id)


def test_supported_run_config_and_thermal_protocol_are_honest(tmp_path: Path) -> None:
    _validate_supported_run_config(
        checksum="auto",
        buffer_budget_bytes=SUPPORTED_PULSE_BUFFER_BUDGET_BYTES,
        thermal="warm",
    )
    with pytest.raises(DriverRefused, match="67108864-byte"):
        _validate_supported_run_config(
            checksum="auto",
            buffer_budget_bytes=32 * 1024 * 1024,
            thermal="warm",
        )
    with pytest.raises(DriverRefused, match="cannot prove OS cache eviction"):
        _validate_supported_run_config(
            checksum="auto",
            buffer_budget_bytes=SUPPORTED_PULSE_BUFFER_BUDGET_BYTES,
            thermal="cold",
        )

    graph = tmp_path / "graph"
    graph.mkdir()
    (graph / "heap.bin").write_bytes(b"abc")
    (graph / "wal").mkdir()
    (graph / "wal" / "1.wal").write_bytes(b"defgh")
    warm = _apply_thermal_protocol("warm", graph)
    assert warm["strategy"] == "sequential_read_active_graph_before_open"
    assert warm["files_read"] == 2
    assert warm["bytes_read"] == 8
    assert warm["wall_seconds"] >= 0
    mixed = _apply_thermal_protocol("mixed", graph)
    assert mixed == {
        "strategy": "uncontrolled_os_cache",
        "files_read": 0,
        "bytes_read": 0,
        "wall_seconds": 0.0,
    }


def test_child_requires_its_direct_baseline_runner_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OKTO_GRAFX_PERF_RUNNER_PID", str(os.getppid()))
    _validate_runner_parent()
    monkeypatch.setenv("OKTO_GRAFX_PERF_RUNNER_PID", str(os.getppid() + 1))
    with pytest.raises(DriverRefused, match="launched directly"):
        _validate_runner_parent()


@pytest.mark.parametrize(
    "name", (".okto-pulse-serve.lock", ".okto-pulse-serve.lock.acquire")
)
def test_child_refuses_a_serve_lock_artifact_after_release(
    tmp_path: Path, name: str
) -> None:
    (tmp_path / name).write_text("left behind", encoding="utf-8")

    with pytest.raises(DriverRefused, match="remained after lock release"):
        _require_serve_lock_released(tmp_path)


def test_queue_and_audit_oracles_reject_every_undeclared_effect() -> None:
    expected = snapshot_rows(
        [
            {"id": "target", "status": "pending", "payload": {"a": 1}},
            {"id": "other", "status": "paused", "payload": {"b": 2}},
        ],
        key="id",
    )
    valid_after = snapshot_rows(
        [{"id": "other", "status": "paused", "payload": {"b": 2}}],
        key="id",
    )
    public = public_snapshot(expected)
    assert public["digest_scope"] == "ephemeral_process_key_no_values_emitted"
    assert "payload" not in json.dumps(public)
    assert_only_target_consumed(expected, valid_after, target_queue_id="target")
    for invalid in (
        [
            {"id": "target", "status": "pending"},
            {"id": "other", "status": "paused", "payload": {"b": 2}},
        ],
        [{"id": "other", "status": "failed", "payload": {"b": 2}}],
        [],
        [
            {"id": "other", "status": "paused", "payload": {"b": 2}},
            {"id": "new", "status": "paused"},
        ],
    ):
        with pytest.raises(DriverRefused):
            assert_only_target_consumed(
                expected,
                snapshot_rows(invalid, key="id"),
                target_queue_id="target",
            )

    before_audit = snapshot_rows(
        [{"session_id": "old", "board_id": "b", "artifact_id": "x"}],
        key="session_id",
    )
    after_audit = snapshot_rows(
        [
            {"session_id": "old", "board_id": "b", "artifact_id": "x"},
            {
                "session_id": "new",
                "board_id": "b",
                "artifact_id": "c",
                "artifact_type": "card",
                "committed_at": "2026-09-03T00:00:00Z",
                "nodes_added": 1,
                "nodes_updated": 0,
                "nodes_superseded": 0,
                "edges_added": 2,
            },
        ],
        key="session_id",
    )
    assert_one_target_audit(before_audit, after_audit, board_id="b", card_id="c")


def test_unexpected_failure_boundary_does_not_emit_exception_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "card-payload-must-not-reach-stderr"

    def fail(_args: object) -> dict[str, object]:
        raise RuntimeError(secret)

    monkeypatch.setattr(replay_pulse_card, "run", fail)
    exit_code = replay_pulse_card.main(
        [
            "--copy",
            str(tmp_path / "copy"),
            "--out",
            str(tmp_path / "result.json"),
            "--board-id",
            "00000000-0000-0000-0000-000000000001",
            "--card-id",
            "00000000-0000-0000-0000-000000000002",
            "--run",
            "0",
            "--mode",
            "strict",
            "--seed",
            "0",
            "--page-size",
            "8192",
            "--buffer-budget-bytes",
            str(SUPPORTED_PULSE_BUFFER_BUDGET_BYTES),
            "--checksum",
            "auto",
            "--kind",
            "raw",
            "--thermal",
            "mixed",
        ]
    )
    stderr = capsys.readouterr().err
    assert exit_code == 1
    assert "FAILED [RuntimeError]: pulse_card_driver_failed" in stderr
    assert secret not in stderr


def test_graph_inventory_is_derived_from_authenticated_manifest_without_values(
    tmp_path: Path,
) -> None:
    root = tmp_path / "clone"
    graph = root / "boards" / "b" / "grafx" / "g1"
    manifest = {
        "copy": {
            "files": [
                {"path": "data/pulse.db", "size": 10, "sha256": "a" * 64},
                {
                    "path": "boards/b/grafx/g1/heap.bin",
                    "size": 20,
                    "sha256": "b" * 64,
                },
                {
                    "path": "boards/b/grafx/g1/wal/1.wal",
                    "size": 30,
                    "sha256": "c" * 64,
                },
            ]
        }
    }
    derived = _manifest_subtree(manifest, root=root, subtree=graph)
    assert derived["file_count"] == 2
    assert derived["total_bytes"] == 50
    assert derived["source"] == "authenticated_clone_manifest"
