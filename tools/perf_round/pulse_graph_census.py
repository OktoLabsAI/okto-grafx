"""Run the untimed P0.3 heap census on one authenticated disposable Pulse clone.

This is a child instrument, not a general database inspector.  It accepts only a full per-run
clone made by :mod:`tools.perf_round.baseline_runs`, requires every Pulse data-home variable to
name that clone, authenticates the persisted Community board binding, and refuses the implicit
default home.  The child must also be a direct child of the performance runner named by
``OKTO_GRAFX_PERF_RUNNER_PID``.

Grafx is opened directly with ``read_only=True`` and ``recovery_policy="refuse"``.  A read-only
open therefore stops on inconsistent WAL instead of replaying, truncating, quarantining or
otherwise repairing it.  Page and record verification must both be clean before the private,
version-pinned ``HeapStore.pages_of``/``HeapStore._walk(copy_content=False)`` census is admitted.

The result deliberately contains only aggregate counts.  It never emits a board, table, record,
page or slot identity; a commit stamp; a value, key, query or physical graph path.  The clone is
inventoried again only after the Grafx handle and the exclusive Pulse serve lock have closed.  A
read-only Grafx participant may materialise its one empty advisory lock; that exact path is bound
to the authenticated graph internally and disclosed only as an opaque SHA-256.  A single JSON
document is then created with exclusive ``open("x")`` semantics outside the clone.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from okto_grafx.domain.model.record import RecordHeader  # noqa: E402
from tools.perf_round.heap_census import summarize_heap_census  # noqa: E402
from tools.perf_round.receipt import (  # noqa: E402
    COPY_MANIFEST_NAME,
    DATA_HOME_ENV,
    LiveBoardRefused,
    canonical_json,
    guard_not_data_home,
    inventory,
    require_declared_copy,
    sha256_text,
    tool_sha256,
)

SCHEMA = "okto-grafx.perf-round-0.0.2.pulse-graph-census.v2"
WORKLOAD_SEMANTICS = "untimed_read_only_post_drain_heap_census"
_MANIFEST_FILES = (COPY_MANIFEST_NAME, COPY_MANIFEST_NAME + ".sha256")
_SERVE_LOCK_FILES = (".okto-pulse-serve.lock", ".okto-pulse-serve.lock.acquire")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PARTICIPANT_SECTION = re.compile(r"^txn-[0-9a-f]{8}$")
_EMPTY_FILE_SHA256 = sha256_text("")


class GraphCensusRefused(RuntimeError):
    """The requested census cannot produce the fail-closed P0.3 evidence."""


def _uuid(value: str, *, field: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as failure:
        raise GraphCensusRefused(
            f"--{field.replace('_', '-')} must be a UUID"
        ) from failure
    if str(parsed) != value.lower():
        raise GraphCensusRefused(
            f"--{field.replace('_', '-')} must use canonical lowercase UUID form"
        )
    return str(parsed)


def _validate_runner_parent() -> None:
    raw = os.environ.get("OKTO_GRAFX_PERF_RUNNER_PID")
    try:
        expected_parent = int(raw) if raw is not None else -1
    except ValueError as failure:
        raise GraphCensusRefused(
            "the performance runner parent marker is invalid"
        ) from failure
    if expected_parent <= 0 or expected_parent != os.getppid():
        raise GraphCensusRefused(
            "this census must be launched directly by its performance runner"
        )


def _module_file(module: Any, *, root_env: str, package: str) -> str:
    root_value = os.environ.get(root_env)
    if not root_value:
        raise GraphCensusRefused(f"{root_env} is required")
    root = Path(root_value).resolve()
    expected = root / "src" / Path(*package.split(".")) / "__init__.py"
    actual_value = getattr(module, "__file__", None)
    if (
        type(actual_value) is not str
        or Path(actual_value).resolve() != expected.resolve()
    ):
        raise GraphCensusRefused(f"{package} did not import from its pinned checkout")
    return str(expected.resolve())


def _validate_runtime_imports() -> dict[str, str]:
    import okto_grafx
    import okto_pulse.community
    import okto_pulse.core

    return {
        "okto_grafx_file": _module_file(
            okto_grafx,
            root_env="OKTO_GRAFX_EXPECTED_ROOT",
            package="okto_grafx",
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


def _validate_effective_data_home(copy_root: Path) -> None:
    mismatches = []
    for name in DATA_HOME_ENV:
        value = os.environ.get(name)
        if not value or Path(value).expanduser().resolve() != copy_root:
            mismatches.append(name)
    if mismatches:
        raise GraphCensusRefused(
            "every data-home environment variable must identify the per-run clone"
        )


def _exclusive_clone_lock(copy_root: Path) -> Any:
    from okto_pulse.community.serve_lock import acquire_serve_lock

    return acquire_serve_lock(copy_root)


def _acquire_board_binding(copy_root: Path, board_id: str) -> Any:
    from okto_pulse.community.adapters.graph_backend_binding import (
        CommunityGraphBackendBindingStore,
    )

    return CommunityGraphBackendBindingStore(copy_root).acquire_board_binding(board_id)


def _require_authenticated_grafx_binding(
    binding: Any,
    *,
    clone_root: Path,
    board_id: str,
    page_size: int,
) -> Path:
    if (
        getattr(binding, "scope", None) != "board"
        or getattr(binding, "scope_id", None) != board_id
        or getattr(binding, "backend", None) != "grafx"
        or getattr(binding, "page_size", None) != page_size
    ):
        raise GraphCensusRefused(
            "the authenticated board binding is not the requested Grafx geometry"
        )
    try:
        graph_path = Path(binding.physical_path).resolve(strict=True)
        relative = graph_path.relative_to(clone_root)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as failure:
        raise GraphCensusRefused(
            "the authenticated graph path is not a descendant of the per-run clone"
        ) from failure
    if not relative.parts or not graph_path.is_dir():
        raise GraphCensusRefused(
            "the authenticated graph path is not a database directory inside the clone"
        )
    return graph_path


def _require_clean_verification(report: Any, *, scope: str) -> int:
    if getattr(report, "scope", None) != scope:
        raise GraphCensusRefused(
            "Grafx returned a verification report for another scope"
        )
    findings = getattr(report, "findings", None)
    if findings != () or getattr(report, "clean", None) is not True:
        raise GraphCensusRefused(f"the read-only {scope} verification is not clean")
    field = "pages_checked" if scope == "pages" else "records_checked"
    count = getattr(report, field, None)
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise GraphCensusRefused(
            f"the read-only {scope} verification did not check a positive population"
        )
    return count


def _header_observations(heap: Any, table: Any) -> Iterator[tuple[int, RecordHeader]]:
    for ref, header, content in heap._walk(table, copy_content=False):
        if content != b"":
            raise GraphCensusRefused(
                "the header-only heap walk unexpectedly copied record content"
            )
        if not isinstance(header, RecordHeader):
            raise GraphCensusRefused(
                "the header-only heap walk returned an invalid header"
            )
        page = getattr(ref, "page", None)
        if isinstance(page, bool) or not isinstance(page, int):
            raise GraphCensusRefused(
                "the header-only heap walk returned an invalid physical location"
            )
        yield page, header


def collect_heap_census(database: Any) -> dict[str, Any]:
    """Verify, walk and aggregate one already-admitted read-only Grafx handle."""
    pages_report = database.verify("pages")
    pages_checked = _require_clean_verification(pages_report, scope="pages")
    records_report = database.verify("records")
    records_checked = _require_clean_verification(records_report, scope="records")

    tables = tuple(database.catalog.catalog.tables())
    heap = getattr(database, "_heap", None)
    if heap is None:
        raise GraphCensusRefused(
            "the pinned Grafx handle exposes no heap for the census"
        )

    heap_pages_total = 0

    def populations() -> Iterator[
        tuple[tuple[int, ...], Iterator[tuple[int, RecordHeader]]]
    ]:
        nonlocal heap_pages_total
        for table in tables:
            pages = tuple(heap.pages_of(table))
            heap_pages_total += len(pages)
            yield pages, _header_observations(heap, table)

    heap_summary = summarize_heap_census(populations())
    heap_slots_total = heap_summary["mvcc_headers"]["versions_total"]
    if heap_slots_total != records_checked:
        raise GraphCensusRefused(
            "the header-only heap population does not reconcile with record verification"
        )

    vector_view = database.vectors
    vector_spaces = tuple(vector_view.spaces())
    vector_indexes = tuple(vector_view.indexes())
    stale_vector_indexes = sum(
        1 for index in vector_indexes if getattr(index, "stale", None) is True
    )
    stale_indexes = tuple(database.stale_indexes)

    return {
        "verification": {
            "pages": {"clean": True, "checked_total": pages_checked},
            "records": {"clean": True, "checked_total": records_checked},
        },
        "counts": {
            "tables_total": len(tables),
            "heap_pages_total": heap_pages_total,
            "heap_record_slots_total": heap_slots_total,
            "vector_spaces_total": len(vector_spaces),
            "vector_indexes_total": len(vector_indexes),
            "stale_indexes_total": len(stale_indexes),
            "stale_vector_indexes_total": stale_vector_indexes,
        },
        "heap": heap_summary,
    }


def _connect_read_only(graph_path: Path, args: argparse.Namespace) -> Any:
    from okto_grafx import connect

    return connect(
        graph_path,
        read_only=True,
        recovery_policy="refuse",
        page_size=args.page_size,
        buffer_budget_bytes=args.buffer_budget_bytes,
        checksum=args.checksum,
        descriptor_revalidation=args.mode,
    )


def _admit_database(database: Any, graph_path: Path, args: argparse.Namespace) -> None:
    from okto_pulse.community.adapters.graph_backend_binding import admit_grafx_database

    admit_grafx_database(
        database,
        expected_page_size=args.page_size,
        expected_path=graph_path,
        expected_descriptor_revalidation=args.mode,
        operation="perf_round_pulse_graph_census",
    )


def _open_and_collect(
    graph_path: Path, args: argparse.Namespace
) -> tuple[dict[str, Any], str]:
    database = _connect_read_only(graph_path, args)
    result: dict[str, Any] | None = None
    participant_section_name: str | None = None
    try:
        _admit_database(database, graph_path, args)
        status = database.maintenance.status()
        if (
            database.read_only is not True
            or database.recovery_report is not None
            or getattr(status, "recovery_required", None) is not False
        ):
            raise GraphCensusRefused(
                "the Grafx handle is not a recovery-free consistent read-only view"
            )
        if (
            database.identity.page_size != args.page_size
            or database.pool.budget_bytes != args.buffer_budget_bytes
            or database.descriptor_revalidation != args.mode
        ):
            raise GraphCensusRefused(
                "the effective Grafx handle does not match the requested configuration"
            )
        transactions = getattr(database, "_transactions", None)
        participant_section_name = getattr(
            transactions, "_participant_section_name", None
        )
        if (
            type(participant_section_name) is not str
            or _PARTICIPANT_SECTION.fullmatch(participant_section_name) is None
        ):
            raise GraphCensusRefused(
                "the Grafx handle exposed no valid participant section identity"
            )
        result = collect_heap_census(database)
    finally:
        database.close()
    if (
        getattr(database, "closed", None) is not True
        or getattr(database, "close_complete", None) is not True
    ):
        raise GraphCensusRefused("the read-only Grafx handle did not close completely")
    if result is None:
        raise GraphCensusRefused("the read-only Grafx census produced no aggregate")
    if participant_section_name is None:  # pragma: no cover - guarded before collection
        raise GraphCensusRefused("the Grafx participant section identity was lost")
    return result, participant_section_name


def _inventory_matches(
    expected: Mapping[str, Any], observed: Mapping[str, Any]
) -> bool:
    return all(
        expected.get(field) == observed.get(field)
        for field in ("sha256", "file_count", "total_bytes", "files")
    )


def _inventory_file_map(
    document: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]] | None:
    files = document.get("files")
    if type(files) is not list:
        return None
    mapped: dict[str, Mapping[str, Any]] = {}
    for entry in files:
        if type(entry) is not dict or set(entry) != {"path", "size", "sha256"}:
            return None
        path = entry.get("path")
        size = entry.get("size")
        digest = entry.get("sha256")
        if (
            type(path) is not str
            or not path
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or type(digest) is not str
            or _SHA256.fullmatch(digest) is None
            or path in mapped
        ):
            return None
        mapped[path] = entry
    return mapped


def _expected_participant_lock_relative(
    *, copy_root: Path, graph_path: Path, participant_section_name: str
) -> str:
    if _PARTICIPANT_SECTION.fullmatch(participant_section_name) is None:
        raise GraphCensusRefused(
            "the Grafx participant section name is outside the frozen lock grammar"
        )
    try:
        graph_relative = graph_path.resolve().relative_to(copy_root.resolve())
    except (OSError, RuntimeError, ValueError) as failure:
        raise GraphCensusRefused(
            "the participant lock is not inside the authenticated clone graph"
        ) from failure
    if not graph_relative.parts:
        raise GraphCensusRefused("the authenticated graph cannot be the clone root")
    return (graph_relative / "control" / f"{participant_section_name}.lock").as_posix()


def _inventory_delta_for_expected_participant_lock(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
    *,
    expected_relative_path: str,
) -> int | None:
    """Return 0/1 for strict equality or one exact empty participant-lock addition.

    Both trees have already gone through :func:`inventory`, so no path is hidden from its
    regular-file, reparse-point, hard-link and root-containment checks.  This comparison permits
    only the one path derived from the authenticated graph and captured participant section;
    every pre-existing entry remains part of the immutable proof.
    """
    expected_files = _inventory_file_map(expected)
    observed_files = _inventory_file_map(observed)
    if expected_files is None or observed_files is None:
        return None
    expected_lock = {
        "path": expected_relative_path,
        "size": 0,
        "sha256": _EMPTY_FILE_SHA256,
    }
    if _inventory_matches(expected, observed):
        return (
            0 if expected_files.get(expected_relative_path) == expected_lock else None
        )
    expected_paths = set(expected_files)
    observed_paths = set(observed_files)
    if not expected_paths <= observed_paths:
        return None
    if any(observed_files[path] != expected_files[path] for path in expected_paths):
        return None
    added = observed_paths - expected_paths
    if added != {expected_relative_path}:
        return None
    added_entry = observed_files[expected_relative_path]
    if added_entry != expected_lock:
        return None
    expected_count = expected.get("file_count")
    observed_count = observed.get("file_count")
    expected_bytes = expected.get("total_bytes")
    observed_bytes = observed.get("total_bytes")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (expected_count, observed_count, expected_bytes, observed_bytes)
    ):
        return None
    if (
        expected_count != len(expected_files)
        or observed_count != len(observed_files)
        or observed_count != expected_count + 1
        or observed_bytes != expected_bytes
        or observed.get("sha256") == expected.get("sha256")
    ):
        return None
    return 1


def _require_lock_artifacts_absent(copy_root: Path) -> None:
    if any(os.path.lexists(copy_root / name) for name in _SERVE_LOCK_FILES):
        raise GraphCensusRefused(
            "the exclusive Pulse serve-lock artifacts remained after lock release"
        )


def _require_per_run_clone_manifest(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    source = manifest.get("source")
    marker = (
        source.get("cloned_from_declared_copy") if isinstance(source, dict) else None
    )
    if type(marker) is not str or _SHA256.fullmatch(marker) is None:
        raise GraphCensusRefused(
            "the declared data home is not a proved full per-run clone"
        )
    expected_inventory = manifest.get("copy")
    if not isinstance(expected_inventory, dict):
        raise GraphCensusRefused(
            "the per-run clone manifest has no inventory authority"
        )
    return expected_inventory


def _validate_args(args: argparse.Namespace) -> None:
    args.board_id = _uuid(args.board_id, field="board_id")
    if isinstance(args.run, bool) or not isinstance(args.run, int) or args.run < 0:
        raise GraphCensusRefused("--run must be a non-negative integer")
    if args.mode not in ("strict", "generation"):
        raise GraphCensusRefused("--mode must be strict or generation")
    if isinstance(args.seed, bool) or not isinstance(args.seed, int):
        raise GraphCensusRefused("--seed must be an integer")
    if (
        isinstance(args.page_size, bool)
        or not isinstance(args.page_size, int)
        or args.page_size <= 0
        or isinstance(args.buffer_budget_bytes, bool)
        or not isinstance(args.buffer_budget_bytes, int)
        or args.buffer_budget_bytes <= 0
    ):
        raise GraphCensusRefused(
            "page size and buffer budget must be positive integers"
        )
    if args.checksum != "auto":
        raise GraphCensusRefused("this census supports only --checksum auto")
    if args.kind != "instrumented":
        raise GraphCensusRefused(
            "the private header-only census requires --kind instrumented"
        )
    if args.thermal != "mixed":
        raise GraphCensusRefused(
            "the untimed census requires --thermal mixed because it does not control the OS cache"
        )


def run(args: argparse.Namespace) -> dict[str, Any]:
    _validate_args(args)
    args.copy = Path(args.copy).resolve()
    args.out = guard_not_data_home(args.out)
    if args.out.exists():
        raise GraphCensusRefused("the output already exists")
    if not args.out.parent.is_dir():
        raise GraphCensusRefused("the output parent directory must already exist")
    if (
        args.out == args.copy
        or args.copy in args.out.parents
        or args.out in args.copy.parents
    ):
        raise GraphCensusRefused("the output and per-run clone must be disjoint")

    _validate_runner_parent()
    _validate_effective_data_home(args.copy)
    manifest = require_declared_copy(args.copy, allow_effective_data_home=True)
    expected_inventory = _require_per_run_clone_manifest(manifest)
    if not (args.copy / "boards" / args.board_id).is_dir():
        raise GraphCensusRefused("the requested board is absent from the per-run clone")

    runtime_files = _validate_runtime_imports()
    with _exclusive_clone_lock(args.copy) as owned_lock:
        if owned_lock is None:
            raise GraphCensusRefused(
                "the per-run clone lock was not acquired exclusively"
            )
        binding = _acquire_board_binding(args.copy, args.board_id)
        graph_path = _require_authenticated_grafx_binding(
            binding,
            clone_root=args.copy,
            board_id=args.board_id,
            page_size=args.page_size,
        )
        aggregate, participant_section_name = _open_and_collect(graph_path, args)

    _require_lock_artifacts_absent(args.copy)
    observed_inventory = inventory(args.copy, exclude=_MANIFEST_FILES)
    participant_lock_relative = _expected_participant_lock_relative(
        copy_root=args.copy,
        graph_path=graph_path,
        participant_section_name=participant_section_name,
    )
    participant_lock_added_count = _inventory_delta_for_expected_participant_lock(
        expected_inventory,
        observed_inventory,
        expected_relative_path=participant_lock_relative,
    )
    if participant_lock_added_count is None:
        raise GraphCensusRefused(
            "the disposable clone changed while the read-only census was running"
        )
    raw_unchanged = participant_lock_added_count == 0

    return {
        "schema": SCHEMA,
        "workload_semantics": WORKLOAD_SEMANTICS,
        "measurement_semantics": {
            "timed": False,
            "filesystem_cache_controlled": False,
            "thermal_label": "mixed",
            "thermal_meaning": "uncontrolled_and_not_applicable_to_an_untimed_census",
            "kind": "instrumented",
            "kind_meaning": "private_header_only_static_census_not_replay_hooks",
            "mvcc_classification": "heap_version_live_predicate",
            "dead_meaning": "not_heap_version_live_not_vacuum_safe",
            "vacuum_safety_established": False,
            "vector_inventory_meaning": "static_catalog_presence_only",
            "vector_activity_established": False,
        },
        "route": {
            "binding_authenticated": True,
            "backend": "grafx",
            "path_inside_clone": True,
            "page_size": args.page_size,
        },
        "engine": {
            "read_only": True,
            "recovery_ran": False,
            "recovery_required": False,
            "descriptor_revalidation": args.mode,
            "buffer_budget_bytes": args.buffer_budget_bytes,
        },
        **aggregate,
        "clone_inventory": {
            "unchanged": raw_unchanged,
            "raw_unchanged": raw_unchanged,
            "unchanged_except_expected_participant_lock": True,
            "serve_lock_artifacts_absent": True,
            "sha256_before": expected_inventory["sha256"],
            "sha256_after": observed_inventory["sha256"],
            "file_count_before": expected_inventory["file_count"],
            "file_count_after": observed_inventory["file_count"],
            "file_count": observed_inventory["file_count"],
            "total_bytes_before": expected_inventory["total_bytes"],
            "total_bytes_after": observed_inventory["total_bytes"],
            "total_bytes": observed_inventory["total_bytes"],
            "expected_participant_lock_path_sha256": sha256_text(
                participant_lock_relative
            ),
            "expected_participant_lock_added_count": participant_lock_added_count,
        },
        "_perf_round": {
            "python_executable": str(Path(sys.executable).resolve()),
            **runtime_files,
            "tool_sha256": tool_sha256(__file__),
            "mode": args.mode,
            "seed": args.seed,
            "kind": args.kind,
            "page_size": args.page_size,
            "buffer_budget_bytes": args.buffer_budget_bytes,
            "checksum": args.checksum,
            "thermal": args.thermal,
            "run": args.run,
            "effective_data_dir": str(args.copy),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--copy", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--board-id", required=True)
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
    args = _parser().parse_args(argv)
    try:
        document = run(args)
        with args.out.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(document))
    except (GraphCensusRefused, LiveBoardRefused) as refusal:
        print(f"REFUSED [{type(refusal).__name__}]: {refusal}", file=sys.stderr)
        return 2
    except Exception as failure:
        print(
            f"FAILED [{type(failure).__name__}]: pulse_graph_census_failed",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
