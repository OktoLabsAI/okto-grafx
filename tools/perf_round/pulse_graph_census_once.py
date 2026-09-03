"""Run one untimed authenticated graph census on a disposable full Pulse-home clone.

This is deliberately not the P0.4 baseline series: a structural census needs one exact
post-drain observation, not three repeated walks.  The runner accepts only a previously declared
stable copy, creates one byte-identical per-run clone, pins clean Grafx, Pulse Community and Pulse
Core source trees, and launches :mod:`pulse_graph_census` as its direct child.  The child opens only
the authenticated Grafx binding, read-only and recovery-refusing.  Every pre-existing byte must
remain unchanged; the single Grafx handle may add only its exact empty participant lock.

The process-tree RSS/private figures and supervision wall time are operational diagnostics only.
They are not workload timings, thermal evidence or a performance baseline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.perf_round.baseline_runs import (  # noqa: E402
    RunRefused,
    _clone_copy,
    _spawn,
    _watch,
    validate_child_provenance,
)
from tools.perf_round.profile_pulse_card import (  # noqa: E402
    ProfileRefused,
    RuntimePins,
    _overlaps,
    _validate_clone_commits,
    _validate_source_pins,
)
from tools.perf_round.pulse_graph_census import (  # noqa: E402
    SCHEMA as CHILD_SCHEMA,
    WORKLOAD_SEMANTICS,
)
from tools.perf_round.receipt import (  # noqa: E402
    COPY_MANIFEST_NAME,
    DATA_HOME_ENV,
    LiveBoardRefused,
    build_receipt,
    guard_not_data_home,
    inventory,
    machine_sample,
    plain_input,
    sha256_file,
    sha256_text,
    write_receipt,
)
from tools.perf_round.replay_pulse_card import (  # noqa: E402
    SUPPORTED_PULSE_BUFFER_BUDGET_BYTES,
)

SCHEMA = "okto-grafx.perf-round-0.0.2.pulse-graph-census-once.v2"
SOURCE_ROOT = Path(__file__).resolve().parents[2]
CENSUS_SCRIPT = SOURCE_ROOT / "tools" / "perf_round" / "pulse_graph_census.py"
RUN_COPY_NAME = "graph_census_run_copy"
CHILD_OUTPUT_NAME = "graph_census.json"
RECEIPT_NAME = "graph_census_receipt.json"
_MANIFEST_FILES = (COPY_MANIFEST_NAME, COPY_MANIFEST_NAME + ".sha256")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PARTICIPANT_LOCK_FILE = re.compile(r"^txn-[0-9a-f]{8}\.lock$")
_WINDOWS_RESERVED_SEGMENT = re.compile(
    r"^(?:aux|con|nul|prn|com[1-9]|lpt[1-9])(?:\.|$)", re.IGNORECASE
)
_EMPTY_FILE_SHA256 = sha256_text("")
_BINDING_FORMAT = "okto-pulse-community-graph-binding/1"
_BINDING_FILE = "graph_backend_binding.json"
_BINDING_KEYS = {
    "binding_format",
    "scope",
    "scope_id",
    "backend",
    "generation",
    "physical_path",
    "page_size",
    "binding_sha256",
}


class CensusOnceRefused(RuntimeError):
    """The one-shot run cannot satisfy its provenance or isolation contract."""


def _canonical_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as failure:
        raise CensusOnceRefused("--board-id must be a UUID") from failure
    if str(parsed) != value.lower():
        raise CensusOnceRefused("--board-id must use canonical lowercase UUID form")
    return str(parsed)


def _validate_arguments(args: argparse.Namespace) -> None:
    args.board_id = _canonical_uuid(args.board_id)
    if args.mode not in ("strict", "generation"):
        raise CensusOnceRefused("--mode must be strict or generation")
    if isinstance(args.seed, bool) or not isinstance(args.seed, int):
        raise CensusOnceRefused("--seed must be an integer")
    if (
        isinstance(args.page_size, bool)
        or not isinstance(args.page_size, int)
        or args.page_size <= 0
    ):
        raise CensusOnceRefused("--page-size must be a positive integer")
    if args.buffer_budget_bytes != SUPPORTED_PULSE_BUFFER_BUDGET_BYTES:
        raise CensusOnceRefused(
            "this Pulse round requires the effective 67108864-byte Grafx buffer budget"
        )
    if args.checksum != "auto":
        raise CensusOnceRefused("this Pulse round supports only --checksum auto")
    for name in ("timeout_seconds", "poll_seconds"):
        value = getattr(args, name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise CensusOnceRefused(
                f"--{name.replace('_', '-')} must be finite and positive"
            )
    if not 0.02 <= args.poll_seconds <= 5.0:
        raise CensusOnceRefused("--poll-seconds must be between 0.02 and 5")


def _child_argv(
    args: argparse.Namespace, *, clone: Path, child_output: Path
) -> list[str]:
    return [
        str(Path(sys.executable).resolve()),
        str(CENSUS_SCRIPT.resolve()),
        "--copy",
        str(clone.resolve()),
        "--out",
        str(child_output.resolve()),
        "--board-id",
        args.board_id,
        "--run",
        "0",
        "--mode",
        args.mode,
        "--seed",
        str(args.seed),
        "--page-size",
        str(args.page_size),
        "--buffer-budget-bytes",
        str(args.buffer_budget_bytes),
        "--checksum",
        "auto",
        "--kind",
        "instrumented",
        "--thermal",
        "mixed",
    ]


def _expected_child(
    args: argparse.Namespace, *, clone: Path, pins: RuntimePins
) -> dict[str, Any]:
    return {
        "python_executable": str(Path(sys.executable).resolve()),
        "okto_grafx_file": str(pins.grafx_file),
        "okto_pulse_community_file": str(pins.pulse_file),
        "okto_pulse_core_file": str(pins.pulse_core_file),
        "mode": args.mode,
        "seed": args.seed,
        "kind": "instrumented",
        "page_size": args.page_size,
        "buffer_budget_bytes": args.buffer_budget_bytes,
        "checksum": "auto",
        "thermal": "mixed",
        "run": 0,
        "effective_data_dir": str(clone.resolve()),
    }


def _integer(value: Any, *, minimum: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CensusOnceRefused(f"the census child returned an invalid {field}")
    return value


def _inventory_file_map(
    document: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    files = document.get("files")
    if type(files) is not list:
        raise CensusOnceRefused("an inventory has no exact file population")
    mapped: dict[str, Mapping[str, Any]] = {}
    for entry in files:
        if type(entry) is not dict or set(entry) != {"path", "size", "sha256"}:
            raise CensusOnceRefused("an inventory contains a malformed file entry")
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
            raise CensusOnceRefused("an inventory contains a malformed file entry")
        mapped[path] = entry
    return mapped


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate binding key")
        result[key] = value
    return result


def _authenticated_graph_relative(
    copy_root: Path,
    *,
    board_id: str,
    page_size: int,
    expected_inventory: Mapping[str, Any],
) -> str:
    """Authenticate the persisted Community binding without opening the graph database.

    Keep this canonical JSON/path subset aligned with Community's
    ``graph_backend_binding.py``; the independent runner deliberately does not import or start
    Pulse while establishing the expected graph root.
    """
    binding_relative = f"boards/{board_id}/{_BINDING_FILE}"
    expected_files = _inventory_file_map(expected_inventory)
    expected_binding = expected_files.get(binding_relative)
    binding_path = copy_root.joinpath(*PurePosixPath(binding_relative).parts)
    try:
        encoded = binding_path.read_bytes()
    except OSError as failure:
        raise CensusOnceRefused("the board graph binding is unreadable") from failure
    if (
        len(encoded) > 16 * 1024
        or expected_binding is None
        or expected_binding.get("size") != len(encoded)
        or expected_binding.get("sha256") != hashlib.sha256(encoded).hexdigest()
    ):
        raise CensusOnceRefused("the board graph binding is outside the clone manifest")
    try:
        document = json.loads(
            encoded.decode("utf-8"), object_pairs_hook=_strict_json_object
        )
    except (UnicodeDecodeError, ValueError) as failure:
        raise CensusOnceRefused(
            "the board graph binding is not strict JSON"
        ) from failure
    if type(document) is not dict or set(document) != _BINDING_KEYS:
        raise CensusOnceRefused("the board graph binding has an invalid shape")
    supplied_digest = document["binding_sha256"]
    body = {key: value for key, value in document.items() if key != "binding_sha256"}
    canonical = json.dumps(
        body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    generation = document["generation"]
    if (
        type(supplied_digest) is not str
        or _SHA256.fullmatch(supplied_digest) is None
        or sha256_text(canonical) != supplied_digest
        or document["binding_format"] != _BINDING_FORMAT
        or document["scope"] != "board"
        or document["scope_id"] != board_id
        or document["backend"] != "grafx"
        or type(document["page_size"]) is not int
        or document["page_size"] != page_size
        or type(generation) is not str
        or not generation
        or len(generation) > 128
        or generation in {".", ".."}
        or any(character in generation for character in '\\/<>:"|?*')
        or generation[-1] in {".", " "}
        or _WINDOWS_RESERVED_SEGMENT.match(generation) is not None
        or any(ord(character) < 32 for character in generation)
    ):
        raise CensusOnceRefused("the board graph binding did not authenticate")
    graph_relative = PurePosixPath("boards", board_id, "grafx", generation)
    if document["physical_path"] != graph_relative.as_posix():
        raise CensusOnceRefused("the board graph binding path is not canonical")
    graph_path = copy_root.joinpath(*graph_relative.parts)
    if not graph_path.is_dir():
        raise CensusOnceRefused("the bound Grafx database directory is absent")
    return graph_relative.as_posix()


def _validate_independent_inventory_delta(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
    child_inventory: Mapping[str, Any],
    *,
    expected_graph_relative: str,
) -> None:
    """Reprove the child-declared opaque participant-lock delta from a full inventory."""
    expected_files = _inventory_file_map(expected)
    observed_files = _inventory_file_map(observed)
    expected_paths = set(expected_files)
    observed_paths = set(observed_files)
    if not expected_paths <= observed_paths or any(
        observed_files[path] != expected_files[path] for path in expected_paths
    ):
        raise CensusOnceRefused(
            "the one-shot runner found a removed or changed clone file"
        )

    added_paths = observed_paths - expected_paths
    added_count = child_inventory.get("expected_participant_lock_added_count")
    opaque_path = child_inventory.get("expected_participant_lock_path_sha256")
    if (
        isinstance(added_count, bool)
        or not isinstance(added_count, int)
        or added_count not in (0, 1)
        or type(opaque_path) is not str
        or _SHA256.fullmatch(opaque_path) is None
        or len(added_paths) != added_count
    ):
        raise CensusOnceRefused(
            "the one-shot runner could not reconcile the participant-lock count"
        )

    matching_paths = [
        path for path in observed_paths if sha256_text(path) == opaque_path
    ]
    if len(matching_paths) != 1:
        raise CensusOnceRefused(
            "the participant-lock opaque path proof does not match the clone"
        )
    participant_path = matching_paths[0]
    parsed = PurePosixPath(participant_path)
    if (
        parsed.parent != PurePosixPath(expected_graph_relative) / "control"
        or _PARTICIPANT_LOCK_FILE.fullmatch(parsed.name) is None
        or observed_files[participant_path]
        != {
            "path": participant_path,
            "size": 0,
            "sha256": _EMPTY_FILE_SHA256,
        }
        or (added_count == 1 and added_paths != {participant_path})
        or (added_count == 0 and participant_path not in expected_paths)
    ):
        raise CensusOnceRefused(
            "the participant-lock opaque path proof names no valid empty lock"
        )

    expected_count = _integer(
        expected.get("file_count"), minimum=0, field="expected file count"
    )
    observed_count = _integer(
        observed.get("file_count"), minimum=0, field="observed file count"
    )
    expected_bytes = _integer(
        expected.get("total_bytes"), minimum=0, field="expected byte count"
    )
    observed_bytes = _integer(
        observed.get("total_bytes"), minimum=0, field="observed byte count"
    )
    raw_unchanged = added_count == 0
    if (
        expected_count != len(expected_files)
        or observed_count != len(observed_files)
        or observed_count != expected_count + added_count
        or observed_bytes != expected_bytes
        or child_inventory.get("sha256_before") != expected.get("sha256")
        or child_inventory.get("sha256_after") != observed.get("sha256")
        or child_inventory.get("file_count_before") != expected_count
        or child_inventory.get("file_count_after") != observed_count
        or child_inventory.get("total_bytes_before") != expected_bytes
        or child_inventory.get("total_bytes_after") != observed_bytes
        or child_inventory.get("raw_unchanged") is not raw_unchanged
        or child_inventory.get("unchanged") is not raw_unchanged
        or ((observed.get("sha256") == expected.get("sha256")) is not raw_unchanged)
    ):
        raise CensusOnceRefused(
            "the one-shot runner independently found the census clone changed"
        )


def _validate_aggregate(document: Mapping[str, Any]) -> None:
    verification = document.get("verification")
    if not isinstance(verification, dict):
        raise CensusOnceRefused("the census child returned no verification evidence")
    checked: dict[str, int] = {}
    for scope in ("pages", "records"):
        report = verification.get(scope)
        if not isinstance(report, dict) or report.get("clean") is not True:
            raise CensusOnceRefused(
                f"the census child {scope} verification is not clean"
            )
        checked[scope] = _integer(
            report.get("checked_total"),
            minimum=1,
            field=f"{scope} verification population",
        )

    counts = document.get("counts")
    heap = document.get("heap")
    if not isinstance(counts, dict) or not isinstance(heap, dict):
        raise CensusOnceRefused("the census child returned no aggregate census")
    count_fields = (
        "tables_total",
        "heap_pages_total",
        "heap_record_slots_total",
        "vector_spaces_total",
        "vector_indexes_total",
        "stale_indexes_total",
        "stale_vector_indexes_total",
    )
    validated_counts = {
        field: _integer(counts.get(field), minimum=0, field=field)
        for field in count_fields
    }
    if validated_counts["tables_total"] < 1:
        raise CensusOnceRefused("the census child found no graph tables")
    if not 1 <= validated_counts["heap_pages_total"] <= checked["pages"]:
        raise CensusOnceRefused("the census child heap-page population is inconsistent")

    headers = heap.get("mvcc_headers")
    tails = heap.get("live_version_tail_distance_pages")
    if not isinstance(headers, dict) or not isinstance(tails, dict):
        raise CensusOnceRefused(
            "the census child returned an incomplete heap aggregate"
        )
    header_fields = (
        "versions_total",
        "live_committed_open",
        "dead_total",
        "dead_committed_ended",
        "dead_no_csn_birth",
        "dead_provisional_birth",
    )
    validated_headers = {
        field: _integer(headers.get(field), minimum=0, field=f"mvcc {field}")
        for field in header_fields
    }
    versions_total = validated_headers["versions_total"]
    dead_total = validated_headers["dead_total"]
    if not (
        versions_total
        == checked["records"]
        == validated_counts["heap_record_slots_total"]
        == validated_headers["live_committed_open"] + dead_total
    ):
        raise CensusOnceRefused("the census child MVCC populations do not reconcile")
    if dead_total != sum(
        validated_headers[field]
        for field in (
            "dead_committed_ended",
            "dead_no_csn_birth",
            "dead_provisional_birth",
        )
    ):
        raise CensusOnceRefused("the census child dead MVCC classes do not reconcile")

    tail_total = _integer(
        tails.get("total"), minimum=0, field="live-version tail population"
    )
    tail_last = _integer(
        tails.get("last_10_percent"),
        minimum=0,
        field="live-version last-10-percent population",
    )
    if tail_total != validated_headers["live_committed_open"] or tail_last > tail_total:
        raise CensusOnceRefused(
            "the census child live-version locality population does not reconcile"
        )
    ordered_fields = ("min", "p50", "p90", "p99", "max")
    if tail_total:
        ordered = [
            _integer(tails.get(field), minimum=0, field=f"tail-distance {field}")
            for field in ordered_fields
        ]
        if ordered != sorted(ordered):
            raise CensusOnceRefused(
                "the census child tail-distance distribution is inconsistent"
            )
        expected_ratio: float | None = tail_last / tail_total
    else:
        if any(tails.get(field) is not None for field in ordered_fields):
            raise CensusOnceRefused(
                "the empty census child tail-distance distribution is inconsistent"
            )
        expected_ratio = None
    if tails.get("last_10_percent_ratio") != expected_ratio:
        raise CensusOnceRefused(
            "the census child tail-distance ratio does not reconcile"
        )


def _validate_child_document(
    document: Any,
    *,
    expected_provenance: Mapping[str, Any],
    expected_clone_inventory: Mapping[str, Any],
) -> None:
    if (
        not isinstance(document, dict)
        or document.get("schema") != CHILD_SCHEMA
        or document.get("workload_semantics") != WORKLOAD_SEMANTICS
    ):
        raise CensusOnceRefused("the census child output has an unexpected schema")
    errors = validate_child_provenance(document, dict(expected_provenance))
    if errors:
        raise CensusOnceRefused(
            "the census child runtime/config provenance does not match"
        )
    provenance = document.get("_perf_round")
    if not isinstance(provenance, dict) or provenance.get("tool_sha256") != sha256_file(
        CENSUS_SCRIPT
    ):
        raise CensusOnceRefused("the census child tool hash does not match")
    semantics = document.get("measurement_semantics")
    if not isinstance(semantics, dict) or any(
        (
            semantics.get("timed") is not False,
            semantics.get("filesystem_cache_controlled") is not False,
            semantics.get("thermal_label") != "mixed",
            semantics.get("kind") != "instrumented",
            semantics.get("vacuum_safety_established") is not False,
            semantics.get("vector_activity_established") is not False,
        )
    ):
        raise CensusOnceRefused("the census child measurement semantics are incomplete")
    route = document.get("route")
    engine = document.get("engine")
    if (
        not isinstance(route, dict)
        or route.get("binding_authenticated") is not True
        or route.get("backend") != "grafx"
        or route.get("path_inside_clone") is not True
        or not isinstance(engine, dict)
        or engine.get("read_only") is not True
        or engine.get("recovery_ran") is not False
        or engine.get("recovery_required") is not False
    ):
        raise CensusOnceRefused("the census child did not attest the safe Grafx route")
    _validate_aggregate(document)
    clone_inventory = document.get("clone_inventory")
    expected_count = expected_clone_inventory.get("file_count")
    expected_bytes = expected_clone_inventory.get("total_bytes")
    if (
        not isinstance(clone_inventory, dict)
        or clone_inventory.get("serve_lock_artifacts_absent") is not True
        or clone_inventory.get("unchanged_except_expected_participant_lock") is not True
        or clone_inventory.get("sha256_before")
        != expected_clone_inventory.get("sha256")
        or type(clone_inventory.get("expected_participant_lock_path_sha256")) is not str
        or _SHA256.fullmatch(clone_inventory["expected_participant_lock_path_sha256"])
        is None
        or isinstance(
            clone_inventory.get("expected_participant_lock_added_count"), bool
        )
        or clone_inventory.get("expected_participant_lock_added_count") not in (0, 1)
        or isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 0
        or isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes < 0
    ):
        raise CensusOnceRefused(
            "the census child did not prove its bounded clone inventory delta"
        )
    added_count = clone_inventory["expected_participant_lock_added_count"]
    raw_unchanged = added_count == 0
    after_digest = clone_inventory.get("sha256_after")
    if (
        clone_inventory.get("unchanged") is not raw_unchanged
        or clone_inventory.get("raw_unchanged") is not raw_unchanged
        or (
            (after_digest == expected_clone_inventory.get("sha256"))
            is not raw_unchanged
        )
        or clone_inventory.get("file_count_before") != expected_count
        or clone_inventory.get("file_count_after") != expected_count + added_count
        or clone_inventory.get("file_count") != expected_count + added_count
        or clone_inventory.get("total_bytes_before") != expected_bytes
        or clone_inventory.get("total_bytes_after") != expected_bytes
        or clone_inventory.get("total_bytes") != expected_bytes
    ):
        raise CensusOnceRefused(
            "the census child returned inconsistent clone inventory summaries"
        )


def census_once(args: argparse.Namespace) -> dict[str, Any]:
    _validate_arguments(args)
    pins = _validate_source_pins(args)
    declared_copy = guard_not_data_home(args.declared_copy)
    out_dir = guard_not_data_home(args.out_dir)
    protected = (declared_copy, SOURCE_ROOT, pins.pulse_root, pins.pulse_core_root)
    if any(_overlaps(out_dir, path) for path in protected):
        raise CensusOnceRefused(
            "--out-dir must be disjoint from the declared copy and pinned source trees"
        )
    if out_dir.exists():
        raise CensusOnceRefused("--out-dir already exists")
    if not out_dir.parent.is_dir():
        raise CensusOnceRefused("--out-dir parent must already exist")
    out_dir.mkdir()

    clone = out_dir / RUN_COPY_NAME
    child_output = out_dir / CHILD_OUTPUT_NAME
    receipt_path = out_dir / RECEIPT_NAME
    clone_manifest = _clone_copy(declared_copy, clone)
    _validate_clone_commits(
        clone_manifest,
        grafx=args.grafx_sha,
        pulse=args.pulse_sha,
        pulse_core=args.pulse_core_sha,
    )
    expected_inventory = clone_manifest.get("copy")
    if not isinstance(expected_inventory, dict):
        raise CensusOnceRefused("the per-run clone has no inventory authority")
    expected_graph_relative = _authenticated_graph_relative(
        clone,
        board_id=args.board_id,
        page_size=args.page_size,
        expected_inventory=expected_inventory,
    )

    argv = _child_argv(args, clone=clone, child_output=child_output)
    machine_before = machine_sample(interval_seconds=1.0)
    started_wall = time.time()
    started = time.perf_counter()
    try:
        process = _spawn(argv, clone, pins.pulse_root, pins.pulse_core_root)
    except OSError as failure:
        raise CensusOnceRefused("the census child could not be started") from failure
    resources = _watch(process, args.poll_seconds, args.timeout_seconds)
    supervision_wall_seconds = time.perf_counter() - started
    if resources.get("timed_out") is True:
        raise CensusOnceRefused("the census child exceeded its finite timeout")
    if (
        resources.get("peak_rss_bytes_tree") is None
        or resources.get("peak_private_bytes_tree") is None
    ):
        raise CensusOnceRefused("process-tree RSS/private counters were unavailable")
    if process.returncode != 0:
        raise CensusOnceRefused("the census child did not complete successfully")
    if not child_output.is_file() or child_output.stat().st_mtime < started_wall - 1.0:
        raise CensusOnceRefused("the census child wrote no fresh JSON output")
    try:
        child_document = json.loads(child_output.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as failure:
        raise CensusOnceRefused(
            "the census child output is not valid JSON"
        ) from failure
    _validate_child_document(
        child_document,
        expected_provenance=_expected_child(args, clone=clone, pins=pins),
        expected_clone_inventory=expected_inventory,
    )
    observed_inventory = inventory(clone, exclude=_MANIFEST_FILES)
    child_clone_inventory = child_document["clone_inventory"]
    _validate_independent_inventory_delta(
        expected_inventory,
        observed_inventory,
        child_clone_inventory,
        expected_graph_relative=expected_graph_relative,
    )
    raw_unchanged = child_clone_inventory["raw_unchanged"]

    results = {
        "schema": SCHEMA,
        "complete": True,
        "one_shot": True,
        "measurement_semantics": child_document["measurement_semantics"],
        "census": {
            "path": str(child_output.resolve()),
            "sha256": sha256_file(child_output),
            "schema": child_document["schema"],
            "verification": child_document.get("verification"),
            "counts": child_document.get("counts"),
            "heap": child_document.get("heap"),
        },
        "copy": {
            "declared_copy": str(declared_copy),
            "run_copy": str(clone.resolve()),
            "sha256_before": expected_inventory["sha256"],
            "sha256_after": observed_inventory["sha256"],
            "unchanged": raw_unchanged,
            "raw_unchanged": raw_unchanged,
            "unchanged_except_expected_participant_lock": True,
            "expected_participant_lock_path_sha256": child_clone_inventory[
                "expected_participant_lock_path_sha256"
            ],
            "expected_participant_lock_added_count": child_clone_inventory[
                "expected_participant_lock_added_count"
            ],
        },
        "supervision": {
            "wall_seconds_not_a_benchmark": supervision_wall_seconds,
            "process_tree_resources_not_a_benchmark": resources,
        },
    }
    receipt = build_receipt(
        tool=__file__,
        seed=args.seed,
        parameters={
            "board_id": args.board_id,
            "one_shot": True,
            "timed_workload": False,
            "child_data_home_variables": list(DATA_HOME_ENV),
            "timeout_seconds": args.timeout_seconds,
            "poll_seconds": args.poll_seconds,
        },
        series={"mode": args.mode, "thermal": "mixed", "kind": "instrumented"},
        config={
            "descriptor_revalidation": args.mode,
            "page_size": args.page_size,
            "buffer_budget_bytes": args.buffer_budget_bytes,
            "checksum": "auto",
        },
        inputs=[
            {
                "role": "declared_copy",
                "path": str(declared_copy),
                "declared_copy": True,
                "inventory_sha256": clone_manifest["source"]["sha256_after_copy"],
            },
            plain_input(
                "python_executable",
                Path(sys.executable),
                inventory_digest=sha256_file(Path(sys.executable)),
            ),
            plain_input(
                "pulse_graph_census",
                CENSUS_SCRIPT,
                inventory_digest=sha256_file(CENSUS_SCRIPT),
            ),
        ],
        results=results,
        grafx_sha=args.grafx_sha,
        pulse_sha=args.pulse_sha,
        pulse_core_sha=args.pulse_core_sha,
        machine_idle_asserted=False,
        machine=machine_before,
        notes=[
            "this is one structural census, not the N>=3 P0.4 performance series",
            "thermal=mixed means the OS cache is uncontrolled and is not a timing claim",
            "supervision wall/RSS/private values are diagnostics, never benchmark results",
            "dead heap headers do not establish vacuum eligibility or reader-horizon safety",
            "static vector inventory does not establish vector search or build activity",
        ],
    )
    write_receipt(receipt_path, receipt)
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--declared-copy", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--board-id", required=True)
    parser.add_argument("--mode", choices=("strict", "generation"), required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--page-size", type=int, default=8192)
    parser.add_argument(
        "--buffer-budget-bytes",
        type=int,
        default=SUPPORTED_PULSE_BUFFER_BUDGET_BYTES,
    )
    parser.add_argument("--checksum", default="auto")
    parser.add_argument("--timeout-seconds", type=float, default=7200.0)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--grafx-sha", required=True)
    parser.add_argument("--pulse-root", type=Path, required=True)
    parser.add_argument("--pulse-sha", required=True)
    parser.add_argument("--pulse-core-root", type=Path, required=True)
    parser.add_argument("--pulse-core-sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        census_once(args)
    except (CensusOnceRefused, LiveBoardRefused, ProfileRefused, RunRefused) as refusal:
        print(f"REFUSED [{type(refusal).__name__}]: {refusal}", file=sys.stderr)
        return 2
    except Exception as failure:
        print(
            f"FAILED [{type(failure).__name__}]: pulse_graph_census_once_failed",
            file=sys.stderr,
        )
        return 1
    print(f"census receipt: {(args.out_dir / RECEIPT_NAME).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
