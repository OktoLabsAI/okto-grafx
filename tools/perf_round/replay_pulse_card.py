"""Measure one real Pulse card consolidation on a disposable, authenticated data-home clone.

The operation is a reprocess on the mature post-drain graph, not an exact reconstruction of the
card's original historical predecessor state.  The driver stages (or validates) one historical
queue row, pauses every other pending row in one ``BEGIN IMMEDIATE`` transaction, invokes the
public ``ConsolidationProcessor.process_batch`` lifecycle, and proves that exactly the target was
ACKed and audited.  The supported runner always constructs a fresh per-run clone and the child
refuses the implicit default home plus direct invocation outside that parent process.

Use this only through ``baseline_runs.py --data-home-policy declared-copy-clone``.  RAW runs
install no hooks.  Instrumented runs use bounded, reversible hooks and must be profiled from the
outside with ``py-spy record`` without ``--locals``.  No payload, card text, query text, row value,
record reference, key, plan or commit number is written to the JSON artifact.

The parent-PID marker and self-hashed manifests prevent accidental misuse; they are deliberately
not an authority boundary against a malicious local operator.  A signed authority bundle remains
a separately governed future feature.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.perf_round.pulse_card_instrumentation import (  # noqa: E402
    PulseCardInstrumentation,
    null_instrumentation_report,
)
from tools.perf_round.receipt import (  # noqa: E402
    DATA_HOME_ENV,
    LiveBoardRefused,
    canonical_json,
    guard_not_data_home,
    require_declared_copy,
    sha256_text,
)

SCHEMA = "okto-grafx.perf-round-0.0.2.pulse-card.v1"
WORKLOAD_SEMANTICS = "reprocess_on_post_drain_snapshot"
SUPPORTED_PULSE_BUFFER_BUDGET_BYTES = 64 * 1024 * 1024
WARM_READ_CHUNK_BYTES = 1024 * 1024
_SNAPSHOT_HMAC_KEY = os.urandom(32)
SOURCE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_IMPORT_ROOT = SOURCE_ROOT / "src"

QUEUE_COLUMNS = (
    "id",
    "board_id",
    "artifact_type",
    "artifact_id",
    "work_kind",
    "generation",
    "payload",
    "delete_event_id",
    "priority",
    "source",
    "status",
    "triggered_at",
    "triggered_by_event",
    "claimed_by_session_id",
    "claim_token",
    "claimed_at",
    "last_error",
    "worker_id",
    "claim_timeout_at",
    "attempts",
    "next_retry_at",
)

AUDIT_COLUMNS = (
    "session_id",
    "board_id",
    "artifact_id",
    "artifact_type",
    "agent_id",
    "started_at",
    "committed_at",
    "nodes_added",
    "nodes_updated",
    "nodes_superseded",
    "edges_added",
    "summary_text",
    "content_hash",
    "undo_status",
    "undone_at",
    "error_details",
)

QUEUE_SELECT = (
    "SELECT " + ", ".join(QUEUE_COLUMNS) + " FROM consolidation_queue ORDER BY id"
)
AUDIT_SELECT = (
    "SELECT "
    + ", ".join(AUDIT_COLUMNS)
    + " FROM consolidation_audit ORDER BY session_id"
)
PAUSE_NON_TARGET_SQL = """
    UPDATE consolidation_queue SET status = 'paused'
    WHERE id <> :target_queue_id AND status = 'pending'
    RETURNING id, status
"""
PENDING_QUEUE_SQL = (
    "SELECT id FROM consolidation_queue WHERE status = 'pending' ORDER BY id"
)


class DriverRefused(RuntimeError):
    """The requested run does not satisfy the fail-closed evidence contract."""


@dataclass(frozen=True)
class RowSnapshot:
    rows: dict[str, dict[str, Any]]
    row_hmac_sha256: dict[str, str]
    hmac_sha256: str

    @property
    def count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class QueuePreparation:
    target_queue_id: str
    target_staged: bool
    before: RowSnapshot
    expected_after_isolation: RowSnapshot
    audit_before: RowSnapshot
    paused_count: int


def _uuid(value: str, *, field: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as failure:
        raise DriverRefused(f"--{field.replace('_', '-')} must be a UUID") from failure
    if str(parsed) != value.lower():
        raise DriverRefused(
            f"--{field.replace('_', '-')} must use canonical lowercase UUID form"
        )
    return str(parsed)


def _json_value(value: Any) -> Any:
    """Canonicalise a DB value for an in-memory digest; callers never emit this value."""
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise DriverRefused(f"unsupported relational value type {type(value).__name__}")


def snapshot_rows(rows: Sequence[Mapping[str, Any]], *, key: str) -> RowSnapshot:
    """Build a privacy-preserving equality oracle for queue/audit rows."""
    canonical: dict[str, dict[str, Any]] = {}
    row_hashes: dict[str, str] = {}
    for source in rows:
        if key not in source or type(source[key]) is not str or not source[key]:
            raise DriverRefused(f"relational snapshot has no non-empty {key}")
        identity = source[key]
        if identity in canonical:
            raise DriverRefused(f"relational snapshot repeats {key}")
        row = {str(name): _json_value(value) for name, value in source.items()}
        row_text = json.dumps(
            row,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        canonical[identity] = row
        row_hashes[identity] = hmac.new(
            _SNAPSHOT_HMAC_KEY, row_text.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    listing = "".join(
        f"{identity}\0{row_hashes[identity]}\n" for identity in sorted(row_hashes)
    )
    snapshot_hmac = hmac.new(
        _SNAPSHOT_HMAC_KEY, listing.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return RowSnapshot(canonical, row_hashes, snapshot_hmac)


def public_snapshot(snapshot: RowSnapshot) -> dict[str, Any]:
    return {
        "count": snapshot.count,
        "hmac_sha256": snapshot.hmac_sha256,
        "digest_scope": "ephemeral_process_key_no_values_emitted",
    }


def assert_only_target_consumed(
    expected: RowSnapshot,
    observed: RowSnapshot,
    *,
    target_queue_id: str,
) -> None:
    """Refuse any queue effect other than deletion of the isolated target."""
    expected_hashes = dict(expected.row_hmac_sha256)
    if target_queue_id not in expected_hashes:
        raise DriverRefused("isolated queue snapshot lost its target before execution")
    expected_hashes.pop(target_queue_id)
    if target_queue_id in observed.row_hmac_sha256:
        raise DriverRefused("target queue row was not ACKed and deleted")
    if observed.row_hmac_sha256 != expected_hashes:
        raise DriverRefused("a non-target queue row was added, removed, or changed")


def assert_one_target_audit(
    before: RowSnapshot,
    after: RowSnapshot,
    *,
    board_id: str,
    card_id: str,
) -> None:
    """Prove old audits are immutable and exactly one committed target audit appeared."""
    for identity, digest in before.row_hmac_sha256.items():
        if after.row_hmac_sha256.get(identity) != digest:
            raise DriverRefused(
                "a pre-existing consolidation audit changed or disappeared"
            )
    additions = sorted(set(after.rows) - set(before.rows))
    if len(additions) != 1:
        raise DriverRefused(
            "the workload did not append exactly one consolidation audit"
        )
    audit = after.rows[additions[0]]
    if (
        audit.get("board_id") != board_id
        or audit.get("artifact_id") != card_id
        or audit.get("artifact_type") != "card"
        or audit.get("committed_at") is None
    ):
        raise DriverRefused(
            "the new consolidation audit is not the committed target card"
        )
    for name in ("nodes_added", "nodes_updated", "nodes_superseded", "edges_added"):
        value = audit.get(name)
        if type(value) is not int or value < 0:
            raise DriverRefused("the new consolidation audit has an invalid counter")


def _manifest_subtree(
    manifest: Mapping[str, Any], *, root: Path, subtree: Path
) -> dict[str, Any]:
    """Derive a graph inventory from the already authenticated clone manifest, without rereads."""
    try:
        prefix_path = subtree.resolve().relative_to(root.resolve())
    except ValueError as failure:
        raise DriverRefused(
            "the active graph path escapes the disposable clone"
        ) from failure
    prefix = prefix_path.as_posix().rstrip("/") + "/"
    files = []
    for entry in manifest["copy"]["files"]:
        path = entry["path"]
        if path.startswith(prefix):
            files.append(
                {
                    "path": path[len(prefix) :],
                    "size": int(entry["size"]),
                    "sha256": entry["sha256"],
                }
            )
    files.sort(key=lambda item: item["path"])
    if not files:
        raise DriverRefused(
            "the authenticated clone manifest contains no active graph files"
        )
    listing = "".join(
        f"{item['path']}\0{item['size']}\0{item['sha256']}\n" for item in files
    )
    return {
        "file_count": len(files),
        "total_bytes": sum(item["size"] for item in files),
        "sha256": sha256_text(listing),
        "source": "authenticated_clone_manifest",
    }


def _tree_size(root: Path) -> dict[str, int]:
    """Count tree files/bytes without reading content or following an alias."""
    if not root.exists():
        return {"file_count": 0, "total_bytes": 0}
    file_count = 0
    total_bytes = 0
    for directory, subdirectories, names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in [*subdirectories, *names]:
            candidate = base / name
            info = os.lstat(candidate)
            if getattr(info, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
            ):
                raise DriverRefused("the graph tree contains a filesystem alias")
        for name in names:
            info = os.lstat(base / name)
            if not stat.S_ISREG(info.st_mode):
                raise DriverRefused("the graph tree contains a non-regular file")
            file_count += 1
            total_bytes += int(info.st_size)
    return {"file_count": file_count, "total_bytes": total_bytes}


def _warm_graph_files(root: Path) -> dict[str, Any]:
    """Populate the OS file cache by sequentially reading the active graph tree."""
    if not root.is_dir():
        raise DriverRefused("the active graph root is absent")
    before = _tree_size(root)
    if before["file_count"] == 0:
        raise DriverRefused("the active graph root is empty")
    files_read = 0
    bytes_read = 0
    started_ns = time.perf_counter_ns()
    for directory, _subdirectories, names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in sorted(names):
            candidate = base / name
            resolved = candidate.resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError as failure:
                raise DriverRefused(
                    "the graph warmup path escapes its root"
                ) from failure
            info = os.lstat(candidate)
            if getattr(info, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
            ) or not stat.S_ISREG(info.st_mode):
                raise DriverRefused("the graph warmup tree contains an alias")
            observed = 0
            with candidate.open("rb", buffering=0) as handle:
                while chunk := handle.read(WARM_READ_CHUNK_BYTES):
                    observed += len(chunk)
            if observed != int(info.st_size):
                raise DriverRefused("a graph file changed while warming the cache")
            files_read += 1
            bytes_read += observed
    after = _tree_size(root)
    if (
        after != before
        or files_read != before["file_count"]
        or bytes_read != before["total_bytes"]
    ):
        raise DriverRefused("the graph tree changed while warming the cache")
    return {
        "strategy": "sequential_read_active_graph_before_open",
        "files_read": files_read,
        "bytes_read": bytes_read,
        "wall_seconds": (time.perf_counter_ns() - started_ns) / 1_000_000_000,
    }


def _apply_thermal_protocol(thermal: str, graph_root: Path) -> dict[str, Any]:
    if thermal == "warm":
        return _warm_graph_files(graph_root)
    if thermal == "mixed":
        return {
            "strategy": "uncontrolled_os_cache",
            "files_read": 0,
            "bytes_read": 0,
            "wall_seconds": 0.0,
        }
    raise DriverRefused(
        "--thermal cold is unsupported: this driver cannot prove OS cache eviction"
    )


def _validate_supported_run_config(
    *, checksum: str, buffer_budget_bytes: int, thermal: str
) -> None:
    if checksum != "auto":
        raise DriverRefused("the pinned Pulse adapter supports only --checksum auto")
    if buffer_budget_bytes != SUPPORTED_PULSE_BUFFER_BUDGET_BYTES:
        raise DriverRefused(
            "the pinned Pulse adapter supports only a 67108864-byte Grafx buffer budget"
        )
    if thermal == "cold":
        raise DriverRefused(
            "--thermal cold is unsupported: this driver cannot prove OS cache eviction"
        )
    if thermal not in ("warm", "mixed"):
        raise DriverRefused("--thermal must be warm or mixed")


def _validate_runner_parent() -> None:
    raw = os.environ.get("OKTO_GRAFX_PERF_RUNNER_PID")
    try:
        expected_parent = int(raw) if raw is not None else -1
    except ValueError as failure:
        raise DriverRefused(
            "the performance runner parent marker is invalid"
        ) from failure
    if expected_parent <= 0 or expected_parent != os.getppid():
        raise DriverRefused(
            "this child must be launched directly by tools/perf_round/baseline_runs.py"
        )


def _module_file(module: Any, *, root_env: str, package: str) -> str:
    root_value = os.environ.get(root_env)
    if not root_value:
        raise DriverRefused(f"{root_env} is required")
    root = Path(root_value).resolve()
    expected = root / "src" / Path(*package.split(".")) / "__init__.py"
    actual_value = getattr(module, "__file__", None)
    if (
        type(actual_value) is not str
        or Path(actual_value).resolve() != expected.resolve()
    ):
        raise DriverRefused(f"{package} did not import from its pinned checkout")
    return str(expected.resolve())


def _validate_runtime_imports() -> dict[str, str]:
    import okto_grafx
    import okto_pulse.community
    import okto_pulse.core

    return {
        "okto_grafx_file": _module_file(
            okto_grafx, root_env="OKTO_GRAFX_EXPECTED_ROOT", package="okto_grafx"
        ),
        "okto_pulse_community_file": _module_file(
            okto_pulse.community,
            root_env="OKTO_PULSE_EXPECTED_ROOT",
            package="okto_pulse.community",
        ),
        "okto_pulse_core_file": _module_file(
            okto_pulse.core,
            root_env="OKTO_PULSE_CORE_EXPECTED_ROOT",
            package="okto_pulse.core",
        ),
    }


def _configure_environment(copy_root: Path, *, page_size: int, mode: str) -> str:
    for name in DATA_HOME_ENV:
        value = os.environ.get(name)
        if not value or Path(value).expanduser().resolve() != copy_root:
            raise DriverRefused(
                "every data-home environment variable must identify the disposable clone"
            )
    database_path = copy_root / "data" / "pulse.db"
    if not database_path.is_file():
        raise DriverRefused("the disposable data-home clone has no data/pulse.db")
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    values = {
        "DATABASE_URL": database_url,
        "UPLOAD_DIR": str(copy_root / "uploads"),
        "METRICS_DIR": str(copy_root / "metrics"),
        "KG_GRAPH_BACKEND": "grafx",
        "KG_GLOBAL_GRAPH_BACKEND": "grafx",
        "KG_GRAFX_PAGE_SIZE": str(page_size),
        "KG_GRAFX_DESCRIPTOR_REVALIDATION": mode,
    }
    os.environ.update(values)
    return database_url


async def _mappings(
    session: Any, statement: str, parameters: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    from sqlalchemy import text

    result = await session.execute(text(statement), dict(parameters or {}))
    return [dict(row) for row in result.mappings().all()]


async def _snapshot_queue(session: Any) -> RowSnapshot:
    return snapshot_rows(await _mappings(session, QUEUE_SELECT), key="id")


async def _snapshot_audit(session: Any) -> RowSnapshot:
    return snapshot_rows(await _mappings(session, AUDIT_SELECT), key="session_id")


def _validate_target_row(
    row: Mapping[str, Any], *, board_id: str, card_id: str
) -> None:
    expected = {
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
    }
    if any(row.get(name) != value for name, value in expected.items()):
        raise DriverRefused("the target queue row is not an eligible historical card")
    for name in (
        "claimed_by_session_id",
        "claim_token",
        "claimed_at",
        "worker_id",
        "claim_timeout_at",
    ):
        if row.get(name) is not None:
            raise DriverRefused("the target queue row carries claim ownership")
    attempts = row.get("attempts")
    if type(attempts) is not int or attempts < 0:
        raise DriverRefused("the target queue row has invalid attempts")


async def _prepare_queue(
    session_factory: Any,
    *,
    board_id: str,
    card_id: str,
    seed: int,
    run: int,
) -> QueuePreparation:
    from sqlalchemy import text

    from okto_pulse.core.ports.kg_governance import (
        HistoricalQueueInsert,
        get_kg_governance_store,
    )

    target_query = """
        SELECT q.*, c.id AS source_card_id, c.board_id AS source_board_id,
               CASE WHEN q.next_retry_at IS NULL OR q.next_retry_at <= CURRENT_TIMESTAMP
                    THEN 1 ELSE 0 END AS retry_ready
        FROM consolidation_queue AS q
        LEFT JOIN cards AS c ON c.id = q.artifact_id
        WHERE q.board_id = :board_id AND q.artifact_type = 'card'
          AND q.artifact_id = :card_id AND q.work_kind = 'consolidate'
    """
    params = {"board_id": board_id, "card_id": card_id}
    async with session_factory() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        claimed = await _mappings(
            session,
            "SELECT COUNT(*) AS count FROM consolidation_queue WHERE status = 'claimed'",
        )
        if claimed != [{"count": 0}]:
            raise DriverRefused("the disposable clone has a claimed queue row")
        audit_before = await _snapshot_audit(session)
        existing = await _mappings(session, target_query, params)
        if len(existing) > 1:
            raise DriverRefused("the target has more than one consolidate queue row")
        staged = not existing
        if staged:
            target_queue_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"okto-grafx-perf:{board_id}:{card_id}:{seed}:{run}",
                )
            )
            await get_kg_governance_store().add_queue_entries(
                session,
                (
                    HistoricalQueueInsert(
                        id=target_queue_id,
                        board_id=board_id,
                        artifact_type="card",
                        artifact_id=card_id,
                    ),
                ),
            )
            await session.flush()
            existing = await _mappings(session, target_query, params)
        if len(existing) != 1:
            raise DriverRefused("the target card does not exist or could not be staged")
        target = existing[0]
        _validate_target_row(target, board_id=board_id, card_id=card_id)
        target_queue_id = str(target["id"])
        before = await _snapshot_queue(session)
        pending_other_ids = {
            identity
            for identity, row in before.rows.items()
            if identity != target_queue_id and row.get("status") == "pending"
        }
        updated = await _mappings(
            session,
            PAUSE_NON_TARGET_SQL,
            {"target_queue_id": target_queue_id},
        )
        if {str(row["id"]) for row in updated} != pending_other_ids or any(
            row.get("status") != "paused" for row in updated
        ):
            raise DriverRefused("the queue isolation UPDATE did not match its snapshot")
        pending = await _mappings(
            session,
            PENDING_QUEUE_SQL,
        )
        if pending != [{"id": target_queue_id}]:
            raise DriverRefused("the target is not the sole pending queue row")
        expected = await _snapshot_queue(session)
        await session.commit()
    return QueuePreparation(
        target_queue_id=target_queue_id,
        target_staged=staged,
        before=before,
        expected_after_isolation=expected,
        audit_before=audit_before,
        paused_count=len(pending_other_ids),
    )


async def _verify_relational_after(
    session_factory: Any,
    preparation: QueuePreparation,
    *,
    board_id: str,
    card_id: str,
) -> tuple[RowSnapshot, RowSnapshot]:
    async with session_factory() as session:
        queue_after = await _snapshot_queue(session)
        audit_after = await _snapshot_audit(session)
    assert_only_target_consumed(
        preparation.expected_after_isolation,
        queue_after,
        target_queue_id=preparation.target_queue_id,
    )
    assert_one_target_audit(
        preparation.audit_before,
        audit_after,
        board_id=board_id,
        card_id=card_id,
    )
    return queue_after, audit_after


def _route_summary(route: Any, *, root: Path) -> dict[str, Any]:
    active_path = Path(route.active_path).resolve()
    try:
        relative = active_path.relative_to(root).as_posix()
    except ValueError as failure:
        raise DriverRefused(
            "the authenticated graph route escapes the clone"
        ) from failure
    return {
        "scope": str(route.scope),
        "scope_id": str(route.scope_id),
        "backend": str(route.backend),
        "generation": str(route.generation),
        "page_size": route.page_size,
        "active_path_relative": relative,
        "binding_sha256": str(route.binding_sha256),
        "route_sha256": str(route.route_sha256),
        "active_manifest_sha256": route.active_manifest_sha256,
    }


def _engine_observation(database: Any) -> dict[str, Any]:
    from okto_grafx.domain.page import crc32c_implementation

    maintenance = database.maintenance.status()
    wal = database.wal
    pool = database.pool
    storage = database.storage
    vectors = database.vectors
    return {
        "page_size": int(database.identity.page_size),
        "buffer_budget_bytes": int(pool.budget_bytes),
        "buffer_used_bytes": int(pool.used_bytes()),
        "descriptor_revalidation": str(database.descriptor_revalidation),
        "checksum_implementation": str(crc32c_implementation()),
        "stale_indexes": len(database.stale_indexes),
        "attached_indexes": len(database.attached_indexes),
        "vector_spaces": len(vectors.spaces()),
        "vector_indexes": len(vectors.indexes()),
        "wal": {
            "last_lsn": int(wal.last_lsn),
            "segments": len(wal.segments()),
            "bytes": int(maintenance.wal_bytes),
            "checkpoint_lag_lsn": maintenance.checkpoint_lag_lsn,
            "recovery_required": bool(maintenance.recovery_required),
        },
        "storage_bytes": sum(int(item.size_bytes) for item in storage.files),
    }


def _require_engine_config(
    observation: Mapping[str, Any],
    *,
    page_size: int,
    buffer_budget_bytes: int,
    mode: str,
) -> None:
    expected = {
        "page_size": page_size,
        "buffer_budget_bytes": buffer_budget_bytes,
        "descriptor_revalidation": mode,
    }
    if any(observation.get(name) != value for name, value in expected.items()):
        raise DriverRefused(
            "the effective Grafx handle does not match the requested config"
        )


async def _close_runtime(*, graph_configured: bool, database_configured: bool) -> None:
    failure: BaseException | None = None
    if graph_configured:
        try:
            from okto_pulse.community.adapters.kg_shutdown import (
                close_all_graphs_on_shutdown,
            )

            await asyncio.to_thread(close_all_graphs_on_shutdown)
        except BaseException as exc:
            failure = exc
    if database_configured:
        try:
            from okto_pulse.community.adapters.sqlalchemy_database import close_db

            await close_db()
        except BaseException as exc:
            if failure is None:
                failure = exc
            else:
                failure.add_note(f"relational close also failed: {type(exc).__name__}")
    if failure is not None:
        raise failure


async def _run(
    args: argparse.Namespace, settings: Any, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    from okto_pulse.community.adapters.composition import (
        community_storage_provider,
        configure_community_kg_registry,
        require_community_routed_graph_composition,
    )
    from okto_pulse.community.adapters.coordination import (
        register_community_coordination_providers,
    )
    from okto_pulse.community.adapters.graph_backend_binding import admit_grafx_database
    from okto_pulse.community.adapters.relational_effects import (
        register_community_relational_effects,
    )
    from okto_pulse.community.adapters.relational_schema_lifecycle import (
        register_community_relational_schema_lifecycle,
    )
    from okto_pulse.community.adapters.sqlalchemy_database import (
        configure_community_database,
        get_session_factory,
        init_db,
    )
    from okto_pulse.community.adapters.worker_runners import (
        TrackedBlockingExecution,
        UtcWorkerClock,
    )
    from okto_pulse.community.auth import LocalAuthProvider
    from okto_pulse.core import configure_auth, configure_settings, configure_storage
    from okto_pulse.core.application.processors.consolidation import (
        ConsolidationProcessor,
    )
    from okto_pulse.core.services.application_kg import get_current_provider_registry

    database_configured = False
    graph_configured = False
    try:
        configure_settings(settings)
        configure_auth(LocalAuthProvider())
        configure_storage(community_storage_provider(settings.upload_dir))
        register_community_relational_schema_lifecycle()
        configure_community_database(settings.database_url, echo=False)
        database_configured = True
        register_community_relational_effects(
            settings=settings,
            api_base_url=f"http://127.0.0.1:{settings.port}",
        )
        await init_db()
        session_factory = get_session_factory()
        configure_community_kg_registry(session_factory, settings=settings)
        graph_configured = True
        register_community_coordination_providers()

        preparation = await _prepare_queue(
            session_factory,
            board_id=args.board_id,
            card_id=args.card_id,
            seed=args.seed,
            run=args.run,
        )
        registry = get_current_provider_registry()
        bundle = require_community_routed_graph_composition(registry)
        route_before = bundle.resolver.inspect_board_route(args.board_id)
        if (
            route_before.scope != "board"
            or route_before.scope_id != args.board_id
            or route_before.backend != "grafx"
            or route_before.page_size != args.page_size
        ):
            raise DriverRefused(
                "the target board does not have the requested Grafx route"
            )
        route_before_public = _route_summary(route_before, root=args.copy)
        clone_manifest_graph_inventory = _manifest_subtree(
            manifest,
            root=args.copy,
            subtree=Path(route_before.active_path),
        )
        graph_tree_before_open = _tree_size(Path(route_before.active_path))
        thermal_protocol = _apply_thermal_protocol(
            args.thermal, Path(route_before.active_path)
        )
        wal_tree_before = _tree_size(Path(route_before.active_path) / "wal")

        instrumentation = (
            PulseCardInstrumentation() if args.kind == "instrumented" else None
        )

        with bundle.grafx_pool.acquire(
            route_before.active_path,
            page_size=route_before.page_size,
        ) as lease:
            database = lease.database
            admit_grafx_database(
                database,
                expected_page_size=route_before.page_size,
                expected_path=route_before.active_path,
                expected_descriptor_revalidation=args.mode,
                operation="perf_round_pulse_card",
            )
            engine_before = _engine_observation(database)
            _require_engine_config(
                engine_before,
                page_size=args.page_size,
                buffer_budget_bytes=args.buffer_budget_bytes,
                mode=args.mode,
            )
            if instrumentation is not None:
                instrumentation.observe_database(database)

            blocking = TrackedBlockingExecution()
            processor = ConsolidationProcessor(
                session_factory,
                batch_size=1,
                clock=UtcWorkerClock(),
                blocking_execution=blocking,
            )
            if instrumentation is not None:
                instrumentation.install()
            started_ns = time.perf_counter_ns()
            try:
                processed = int(await processor.process_batch())
                wall_ns = time.perf_counter_ns() - started_ns
                pending_native = await blocking.join(30.0)
            finally:
                if instrumentation is not None:
                    instrumentation.close()
            if pending_native:
                raise DriverRefused(
                    "a native blocking operation remained after the card run"
                )
            if processed != 1 or processor.last_attempted_count != 1:
                raise DriverRefused(
                    "the public processor did not attempt and ACK exactly one row"
                )
            instrument_report = (
                instrumentation.report()
                if instrumentation is not None
                else null_instrumentation_report()
            )
            if instrument_report.get("instrumentation_complete") is not True:
                raise DriverRefused("the instrumentation sample is incomplete")
            if (
                instrumentation is not None
                and instrument_report.get("baseline_handle_total") != 1
            ):
                raise DriverRefused(
                    "the instrument did not observe exactly one pre-existing pooled handle"
                )
            route_after = bundle.resolver.revalidate_snapshot(
                route_before,
                require_physical=True,
            )
            if route_after != route_before:
                raise DriverRefused("the graph route changed during the workload")
            engine_after = _engine_observation(database)
            _require_engine_config(
                engine_after,
                page_size=args.page_size,
                buffer_budget_bytes=args.buffer_budget_bytes,
                mode=args.mode,
            )
            endpoint_lookup_locality = (
                instrumentation.endpoint_hit_locality(database)
                if instrumentation is not None
                else {
                    "semantics": "not_collected_in_raw_run",
                    "exact": False,
                    "extra_reads_outside_timed_workload": False,
                    "total": 0,
                    "distance_pages": None,
                    "p1_4_last_10_percent_threshold_met": False,
                    "p1_4_threshold_evaluable": False,
                }
            )

        queue_after, audit_after = await _verify_relational_after(
            session_factory,
            preparation,
            board_id=args.board_id,
            card_id=args.card_id,
        )
        wal_tree_after = _tree_size(Path(route_before.active_path) / "wal")
        graph_tree_after_workload = _tree_size(Path(route_before.active_path))
        wall_seconds = wall_ns / 1_000_000_000
        wal_net = engine_after["wal"]["bytes"] - engine_before["wal"]["bytes"]
        storage_net = engine_after["storage_bytes"] - engine_before["storage_bytes"]
        return {
            "schema": SCHEMA,
            "workload_semantics": WORKLOAD_SEMANTICS,
            "target": {"board_id": args.board_id, "card_id": args.card_id},
            "summary": {
                "wall_seconds": wall_seconds,
                "cards_per_second": 1.0 / wall_seconds if wall_seconds > 0 else 0.0,
                "processed": processed,
                "attempted": processor.last_attempted_count,
                "wal_bytes_net_delta": wal_net,
                "wal_bytes_growth": max(0, wal_net),
                "storage_bytes_net_delta": storage_net,
                "storage_bytes_growth": max(0, storage_net),
            },
            "queue": {
                "target_staged": preparation.target_staged,
                "paused_non_target": preparation.paused_count,
                "before_isolation": public_snapshot(preparation.before),
                "after_isolation": public_snapshot(
                    preparation.expected_after_isolation
                ),
                "after_workload": public_snapshot(queue_after),
                "target_acked": True,
                "non_target_unchanged_after_isolation": True,
            },
            "audit": {
                "before": public_snapshot(preparation.audit_before),
                "after": public_snapshot(audit_after),
                "new_committed_target_rows": 1,
            },
            "route": {
                "before": route_before_public,
                "after": _route_summary(route_after, root=args.copy),
                "unchanged": True,
            },
            "clone_manifest_graph_inventory": clone_manifest_graph_inventory,
            "graph_tree": {
                "before_open": graph_tree_before_open,
                "after_workload": graph_tree_after_workload,
            },
            "thermal_protocol": thermal_protocol,
            "engine": {"before": engine_before, "after": engine_after},
            "wal_tree": {"before": wal_tree_before, "after": wal_tree_after},
            "instrumentation": instrument_report,
            "endpoint_lookup_locality": endpoint_lookup_locality,
        }
    finally:
        await _close_runtime(
            graph_configured=graph_configured,
            database_configured=database_configured,
        )


def run(args: argparse.Namespace) -> dict[str, Any]:
    args.copy = Path(args.copy).resolve()
    args.out = guard_not_data_home(args.out)
    if args.out.exists():
        raise DriverRefused("the output already exists")
    if (
        args.out == args.copy
        or args.copy in args.out.parents
        or args.out in args.copy.parents
    ):
        raise DriverRefused("the output and disposable clone must be disjoint")
    _validate_supported_run_config(
        checksum=args.checksum,
        buffer_budget_bytes=args.buffer_budget_bytes,
        thermal=args.thermal,
    )
    _validate_runner_parent()
    manifest = require_declared_copy(args.copy, allow_effective_data_home=True)
    source = manifest.get("source")
    if (
        not isinstance(source, dict)
        or type(source.get("cloned_from_declared_copy")) is not str
    ):
        raise DriverRefused("the data home is a declared copy, but not a per-run clone")
    if not (args.copy / "boards" / args.board_id).is_dir():
        raise DriverRefused("the target board is absent from the disposable clone")
    database_url = _configure_environment(
        args.copy,
        page_size=args.page_size,
        mode=args.mode,
    )
    runtime_files = _validate_runtime_imports()

    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.community.serve_lock import acquire_serve_lock

    settings = CommunitySettings(
        _env_file=None,
        data_dir=str(args.copy),
        database_url=database_url,
        upload_dir=str(args.copy / "uploads"),
        metrics_dir=str(args.copy / "metrics"),
        kg_base_dir=str(args.copy),
        kg_graph_backend="grafx",
        kg_global_graph_backend="grafx",
        kg_grafx_page_size=args.page_size,
        kg_grafx_descriptor_revalidation=args.mode,
    )
    if (
        Path(settings.data_dir).resolve() != args.copy
        or Path(settings.kg_base_dir).resolve() != args.copy
        or settings.database_url != database_url
        or settings.kg_graph_backend != "grafx"
        or settings.kg_global_graph_backend != "grafx"
        or settings.kg_grafx_page_size != args.page_size
        or settings.kg_grafx_descriptor_revalidation != args.mode
    ):
        raise DriverRefused(
            "the effective Pulse settings do not match the requested clone/config"
        )

    with acquire_serve_lock(settings) as owned_lock:
        if owned_lock is None:
            raise DriverRefused(
                "the disposable clone serve lock was not acquired exclusively"
            )
        document = asyncio.run(_run(args, settings, manifest))
        from okto_pulse.community.adapters.kg_shutdown import (
            close_all_graphs_on_shutdown,
        )

        close_all_graphs_on_shutdown()

    document["_perf_round"] = {
        "python_executable": str(Path(sys.executable).resolve()),
        **runtime_files,
        "mode": args.mode,
        "seed": args.seed,
        "kind": args.kind,
        "page_size": args.page_size,
        "buffer_budget_bytes": args.buffer_budget_bytes,
        "checksum": args.checksum,
        "thermal": args.thermal,
        "run": args.run,
        "effective_data_dir": str(args.copy),
    }
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--copy", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--board-id", required=True)
    parser.add_argument("--card-id", required=True)
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--mode", choices=("strict", "generation"), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--page-size", type=int, required=True)
    parser.add_argument("--buffer-budget-bytes", type=int, required=True)
    parser.add_argument("--checksum", required=True)
    parser.add_argument("--kind", choices=("raw", "instrumented"), required=True)
    parser.add_argument("--thermal", choices=("cold", "warm", "mixed"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        args.board_id = _uuid(args.board_id, field="board_id")
        args.card_id = _uuid(args.card_id, field="card_id")
        if args.run < 0:
            raise DriverRefused("--run must be non-negative")
        if args.page_size <= 0 or args.buffer_budget_bytes <= 0:
            raise DriverRefused("page size and buffer budget must be positive")
        document = run(args)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(document))
    except (DriverRefused, LiveBoardRefused) as refusal:
        print(f"REFUSED [{type(refusal).__name__}]: {refusal}", file=sys.stderr)
        return 2
    except Exception as failure:
        print(
            f"FAILED [{type(failure).__name__}]: pulse_card_driver_failed",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
