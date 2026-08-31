"""Literal CE-3 two-process instrument over the frozen M-PULSE-7 pf5 workload.

This tool measures the question frozen in ``GRAFX_PERFORMANCE_NEXT_STEPS.md`` section 5b:
process A runs all twelve M-PULSE-7 families on a continuously open, warm Grafx handle while
process B either keeps an idle handle open or commits small writes in the same (``Decision``) or
an unrelated (``Assumption``) table at an aggregate target of 1/s or 10/s.

The RAW and instrumented passes always use different copies.  Only the instrumented A process
installs the versioned H8 hooks; RAW therefore contains no monkeypatch, profiler or phase timer.
Every official input and every copy is authenticated, every operation is checked through the
versioned harness postcondition, and every resulting database is cold-opened for ``verify(all)``.
No measured handle is reopened.  A refusal is evidence of a failed scenario, never a retry.

The tool intentionally implements an instrument, not CE-3 itself.  It changes no engine code and
does not decide whether WAL-directed invalidation should ship.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import multiprocessing
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "okto-grafx.ce3-m7-multiprocess.v1"
PINNED_HARNESS_HEAD = "0dfb5269dd8531fd4db2679fc80b649c64bd9b09"
PINNED_HARNESS_BLOB = "a02b86dce098ceec3fdbd10a820a4dd6f9e2a7b1"
EXPECTED_OPERATION_SET_SHA256 = (
    "c994255b0bf695040c972ce339cc5d580ec253d2146674664e7722cf6b5a7f81"
)
EXPECTED_FAMILIES = (
    "create_node",
    "create_edge",
    "update_node",
    "replace_node_payload",
    "mark_superseded",
    "increment_attestation",
    "replace_with_source_deleted_tombstone",
    "reconcile_spec_lineage_parent",
    "clear_spec_lineage_parent",
    "reconcile_projection_active_set",
    "delete_edges_by_session",
    "delete_nodes_by_session",
)
REQUIRED_HOOKS = (
    "BufferPool._read_page",
    "LocalStorageDevice._still_names",
    "BufferPool._invalidate",
)
OFFICIAL_PER_FAMILY = 5
OFFICIAL_RATE_TOLERANCE = 0.25
DEFAULT_CHILD_TIMEOUT_SECONDS = 1800.0
OFFICIAL_PYTHON_VERSION = "3.13.1"
OFFICIAL_NUMPY_VERSION = "2.5.1"
OFFICIAL_LADYBUG_VERSION = "0.16.0"
RAW_HOOK_GUARD_SHA256 = "a1f1e7f4cdb544a0d74a99168a04790dd32810906c9a695c7cbcb48d24a9e870"
TOOL_RELATIVE_PATH = "tools/measure_m7_ce3.py"


class MeasurementRefused(RuntimeError):
    """The requested run cannot produce valid CE-3 evidence."""


@dataclass(frozen=True, slots=True)
class Scenario:
    """One process-B workload paired with one process-A pf5 pass."""

    identifier: str
    relation: str
    table: str | None
    target_rate_per_second: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "relation": self.relation,
            "table": self.table,
            "target_rate_per_second": self.target_rate_per_second,
        }

    @property
    def meaning(self) -> str:
        if self.relation == "idle":
            return "idle handle; zero commits"
        if self.relation == "same":
            return "same=Decision: a small foreign create_node commit in A's hot table"
        return "unrelated=Assumption: a small foreign create_node commit outside A's hot table"


SCENARIOS = (
    Scenario("idle-0", "idle", None, 0.0),
    Scenario("same-1", "same", "Decision", 1.0),
    Scenario("same-10", "same", "Decision", 10.0),
    Scenario("unrelated-1", "unrelated", "Assumption", 1.0),
    Scenario("unrelated-10", "unrelated", "Assumption", 10.0),
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_digest(root: Path) -> dict[str, Any]:
    """Authenticate a tree by relative name, size and the SHA-256 of every file."""

    resolved = root.resolve()
    if not resolved.is_dir():
        raise MeasurementRefused(f"workspace is not a directory: {resolved}")
    outer = hashlib.sha256()
    files = 0
    total = 0
    for path in sorted(candidate for candidate in resolved.rglob("*") if candidate.is_file()):
        size = path.stat().st_size
        inner = _sha256_file(path)
        relative = path.relative_to(resolved).as_posix()
        outer.update(f"{relative}\0{size}\0{inner}\n".encode())
        files += 1
        total += size
    if files == 0:
        raise MeasurementRefused(f"workspace has no files: {resolved}")
    return {"sha256": outer.hexdigest(), "files": files, "bytes": total}


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise MeasurementRefused(
            f"git {' '.join(arguments)} failed for {repo}: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def checkout_evidence(
    name: str,
    repo: Path,
    *,
    expected_head: str | None = None,
) -> dict[str, Any]:
    """Pin a named tree to its real clean content, never merely to a claimed SHA."""

    resolved = repo.resolve()
    head = _git(resolved, "rev-parse", "HEAD")
    dirty = [line for line in _git(resolved, "status", "--porcelain").splitlines() if line]
    if dirty:
        raise MeasurementRefused(
            f"{name} checkout is dirty ({len(dirty)} entries; first={dirty[:4]}): {resolved}"
        )
    if expected_head is not None and head != expected_head:
        raise MeasurementRefused(
            f"{name} HEAD {head} != required {expected_head}: {resolved}"
        )
    return {
        "name": name,
        "path": str(resolved),
        "head": head,
        "expected_head": expected_head,
        "status": "clean",
    }


def raw_hook_guard_ast_evidence(path: Path) -> dict[str, Any]:
    """Pin the semantic guard that keeps Hooks.install unreachable in RAW."""

    source = path.resolve().read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_run_process_a_async"
        ),
        None,
    )
    if function is None:
        raise MeasurementRefused("_run_process_a_async is missing from the launcher AST")
    parents = {
        child: parent for parent in ast.walk(function) for child in ast.iter_child_nodes(parent)
    }
    installs = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "hooks"
        and node.func.attr == "install"
    ]
    if len(installs) != 1:
        raise MeasurementRefused(
            f"RAW hook guard needs exactly one hooks.install call; found {len(installs)}"
        )
    install = installs[0]
    ancestor = parents.get(install)
    guarded_by: ast.If | None = None
    while ancestor is not None:
        if isinstance(ancestor, ast.If):
            guarded_by = ancestor
            break
        ancestor = parents.get(ancestor)
    if guarded_by is None:
        raise MeasurementRefused("hooks.install is not nested under an if guard")
    branch_node: ast.AST = install
    while parents.get(branch_node) is not guarded_by:
        parent = parents.get(branch_node)
        if parent is None:
            raise MeasurementRefused("cannot bind hooks.install to its guarding branch")
        branch_node = parent
    if branch_node not in guarded_by.body:
        raise MeasurementRefused("hooks.install is not in the positive body of its guard")
    semantic = f"if-body\n{ast.unparse(guarded_by.test)}\n{ast.unparse(install)}"
    digest = hashlib.sha256(semantic.encode()).hexdigest()
    if (
        semantic != "if-body\nhooks is not None\nhooks.install()"
        or digest != RAW_HOOK_GUARD_SHA256
    ):
        raise MeasurementRefused(
            f"RAW hook guard semantic digest {digest} != pinned {RAW_HOOK_GUARD_SHA256}"
        )
    return {
        "status": "passed",
        "semantic": semantic,
        "sha256": digest,
        "expected_sha256": RAW_HOOK_GUARD_SHA256,
    }


def _identity_stable_payload(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        str(name): field
        for name, field in value.items()
        if name not in {"captured_at_utc", "fingerprint_sha256"}
    }


def _identity_fingerprint(value: object) -> str | None:
    stable = _identity_stable_payload(value)
    if stable is None:
        return None
    try:
        encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _capture_identity(
    *,
    grafx_repo: Path,
    harness_repo: Path,
    core_repo: Path,
    expected_grafx_head: str | None,
    expected_core_head: str | None,
    expected_tool_commit: str | None,
    expected_tool_blob: str | None,
) -> dict[str, Any]:
    """Capture commits, worktree cleanliness, launcher blob and executor bytes."""

    checkouts = {
        "grafx": checkout_evidence(
            "grafx", grafx_repo, expected_head=expected_grafx_head
        ),
        "community": checkout_evidence(
            "community/harness", harness_repo, expected_head=PINNED_HARNESS_HEAD
        ),
        "core": checkout_evidence("core", core_repo, expected_head=expected_core_head),
    }
    tool_path = (grafx_repo / TOOL_RELATIVE_PATH).resolve()
    if tool_path != Path(__file__).resolve():
        raise MeasurementRefused(
            f"launcher {Path(__file__).resolve()} is not pinned Grafx tool {tool_path}"
        )
    head = checkouts["grafx"]["head"]
    working_blob = _git(
        grafx_repo,
        "hash-object",
        f"--path={TOOL_RELATIVE_PATH}",
        str(tool_path),
    )
    committed_blob = _git(grafx_repo, "rev-parse", f"{head}:{TOOL_RELATIVE_PATH}")
    if working_blob != committed_blob:
        raise MeasurementRefused(
            f"launcher working blob {working_blob} != committed blob {committed_blob}"
        )
    if expected_tool_commit is not None and head != expected_tool_commit:
        raise MeasurementRefused(
            f"launcher commit {head} != expected tool commit {expected_tool_commit}"
        )
    if expected_tool_blob is not None and working_blob != expected_tool_blob:
        raise MeasurementRefused(
            f"launcher blob {working_blob} != expected tool blob {expected_tool_blob}"
        )
    profile = harness_repo / "tools" / "profile_m7_families.py"
    harness_blob = _git(harness_repo, "hash-object", str(profile))
    if harness_blob != PINNED_HARNESS_BLOB:
        raise MeasurementRefused(
            f"profile harness blob {harness_blob} != frozen {PINNED_HARNESS_BLOB}"
        )
    executor = Path(sys.executable).resolve()
    stable = {
        "checkouts": checkouts,
        "tool": {
            "path": str(tool_path),
            "commit": head,
            "expected_commit": expected_tool_commit,
            "blob": working_blob,
            "committed_blob": committed_blob,
            "expected_blob": expected_tool_blob,
            "sha256": _sha256_file(tool_path),
            "raw_hook_guard": raw_hook_guard_ast_evidence(tool_path),
        },
        "harness_profile_blob": harness_blob,
        "executor": {
            "path": str(executor),
            "sha256": _sha256_file(executor),
            "python_release": platform.python_version(),
        },
    }
    fingerprint = _identity_fingerprint(stable)
    if fingerprint is None:
        raise MeasurementRefused("launcher identity could not be fingerprinted")
    return {
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **stable,
        "fingerprint_sha256": fingerprint,
    }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _prepare_import_paths(config: Mapping[str, Any]) -> None:
    roots = (
        Path(str(config["grafx_repo"])).resolve() / "src",
        Path(str(config["core_repo"])).resolve() / "src",
        Path(str(config["harness_repo"])).resolve() / "src",
    )
    for root in reversed(roots):
        text = str(root)
        if text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)


def _require_module_origin(module_name: str, repo: Path) -> str:
    origin = Path(importlib.import_module(module_name).__file__).resolve()
    if not _is_within(origin, repo):
        raise MeasurementRefused(
            f"{module_name} imported from {origin}, outside pinned checkout {repo.resolve()}"
        )
    return str(origin)


def _load_harness(config: Mapping[str, Any]) -> ModuleType:
    """Load only the pinned, clean, versioned Pulse harness."""

    harness_repo = Path(str(config["harness_repo"])).resolve()
    profile = harness_repo / "tools" / "profile_m7_families.py"
    head = _git(harness_repo, "rev-parse", "HEAD")
    blob = _git(harness_repo, "hash-object", str(profile))
    if head != PINNED_HARNESS_HEAD:
        raise MeasurementRefused(
            f"harness HEAD {head} != frozen {PINNED_HARNESS_HEAD}"
        )
    if blob != PINNED_HARNESS_BLOB:
        raise MeasurementRefused(
            f"profile_m7_families.py blob {blob} != frozen {PINNED_HARNESS_BLOB}"
        )
    dirty = _git(harness_repo, "status", "--porcelain")
    if dirty:
        raise MeasurementRefused("the pinned Pulse harness checkout is dirty")
    module_name = "_okto_grafx_pinned_m7_profile"
    loaded = sys.modules.get(module_name)
    if loaded is None:
        spec = importlib.util.spec_from_file_location(module_name, profile)
        if spec is None or spec.loader is None:
            raise MeasurementRefused(f"cannot load pinned harness: {profile}")
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = loaded
        spec.loader.exec_module(loaded)
    loaded.COMMUNITY = harness_repo
    loaded.CORE = Path(str(config["core_repo"])).resolve()
    loaded.GRAFX = Path(str(config["grafx_repo"])).resolve()
    loaded.SCRATCH = Path(str(config["scratch"])).resolve()
    loaded.FORENSIC_WORKSPACE = Path(str(config["source_workspace"])).resolve()
    loaded.PROFILE_RUN_ID = str(config["profile_run_id"])
    return loaded


def _runtime(config: Mapping[str, Any]) -> tuple[Any, Any, Any, Any, dict[str, Any]]:
    """Rebuild the exact logical plan in each spawned process and authenticate it."""

    _prepare_import_paths(config)
    harness = _load_harness(config)
    runner, backends = harness._load_gate_modules()
    templates = harness._templates(runner)
    fixtures = harness.Fixtures(templates, int(config["per_family"]), "scope")
    families = list(harness.FAMILIES)
    if tuple(families) != EXPECTED_FAMILIES:
        raise MeasurementRefused(
            f"harness families changed: {tuple(families)!r} != {EXPECTED_FAMILIES!r}"
        )
    plan = {
        family: [fixtures.measured(family, index) for index in range(int(config["per_family"]))]
        for family in families
    }
    digest = harness._plan_digest(plan, families, "scope")
    if bool(config["official_requested"]) and digest != EXPECTED_OPERATION_SET_SHA256:
        raise MeasurementRefused(
            f"logical operation set {digest} != frozen pf5 {EXPECTED_OPERATION_SET_SHA256}"
        )
    return harness, runner, backends, fixtures, {"families": families, "plan": plan, "digest": digest}


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    temporary.replace(path)


def _failure_payload(role: str, failure: BaseException) -> dict[str, Any]:
    return {
        "role": role,
        "status": "failed",
        "failure": {
            "type": type(failure).__name__,
            "message": str(failure),
            "traceback": traceback.format_exc().splitlines()[-80:],
        },
    }


def _barrier_wait(barrier: Any, timeout_seconds: float) -> int:
    try:
        return int(barrier.wait(timeout=timeout_seconds))
    except BaseException as failure:
        raise MeasurementRefused(f"spawn barrier failed: {type(failure).__name__}: {failure}") from failure


async def _open_warm(
    harness: Any,
    runner: Any,
    backends: Any,
    workspace: Path,
) -> tuple[Any, Any, dict[str, Any]]:
    opened_at = time.perf_counter_ns()
    backend, context = await harness._open_backend(runner, backends, "grafx", workspace)
    await harness._read_rows(backend, "MATCH (m:BoardMeta) RETURN m.board_id", {})
    origins = {
        "okto_grafx": _require_module_origin(
            "okto_grafx", Path(harness.GRAFX)
        ),
        "okto_pulse.community": _require_module_origin(
            "okto_pulse.community", Path(harness.COMMUNITY)
        ),
        "okto_pulse.core": _require_module_origin(
            "okto_pulse.core", Path(harness.CORE)
        ),
    }
    from okto_grafx.domain.page.checksum import crc32c_implementation

    checksum = crc32c_implementation()
    if checksum != "native":
        raise MeasurementRefused(
            f"[accel] was requested but the opened database installed checksum={checksum!r}"
        )
    return backend, context, {
        "opened_at_ns": opened_at,
        "warm_completed_at_ns": time.perf_counter_ns(),
        "imports": origins,
        "checksum_implementation": checksum,
        "environment": _environment(),
    }


def _extract_hooks(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    calls = snapshot.get("calls", {})
    inclusive = snapshot.get("inclusive_ms", {})
    observed = [name for name in REQUIRED_HOOKS if int(calls.get(name, 0)) > 0]
    return {
        "capture_status": "captured_after_reset",
        "calls": {name: int(calls[name]) for name in observed},
        "inclusive_ms": {name: float(inclusive[name]) for name in observed},
        "observed_hooks": observed,
        "read_view_drops": dict(snapshot.get("read_view_drops", {})),
    }


def _installed_required_hooks(hooks: Any) -> list[str]:
    """Read back what Hooks.install actually replaced; do not claim the static target list."""

    installed: set[str] = set()
    for owner, attribute, _original in hooks._restore:
        owner_name = getattr(owner, "__name__", type(owner).__name__)
        candidate = f"{owner_name}.{attribute}"
        if candidate in REQUIRED_HOOKS:
            installed.add(candidate)
    return [hook for hook in REQUIRED_HOOKS if hook in installed]


async def _run_process_a_async(
    config: Mapping[str, Any],
    workspace: Path,
    barrier: Any,
    stop_event: Any,
    b_quiescent_event: Any,
    live_verify_done_event: Any,
    instrumented: bool,
) -> dict[str, Any]:
    harness, runner, backends, _fixtures, plan_info = _runtime(config)
    hooks = harness.Hooks("grafx") if instrumented else None
    backend: Any = None
    samples: list[dict[str, Any]] = []
    barrier_passed = False
    handle: dict[str, Any] = {}
    installed_hooks: list[str] = []
    try:
        if hooks is not None:
            hooks.install()
            installed_hooks = _installed_required_hooks(hooks)
            if set(installed_hooks) != set(REQUIRED_HOOKS):
                raise MeasurementRefused(
                    f"instrumented hook installation incomplete: {installed_hooks!r}"
                )
        backend, context, handle = await _open_warm(
            harness, runner, backends, workspace
        )
        barrier_token = _barrier_wait(barrier, float(config["barrier_timeout_seconds"]))
        barrier_passed = True
        active_start = time.perf_counter_ns()
        for family in plan_info["families"]:
            for index, (operation, postcondition) in enumerate(plan_info["plan"][family]):
                if postcondition.get("type") and postcondition.get("id"):
                    await harness._read_rows(
                        backend,
                        f"MATCH (n:{postcondition['type']}) WHERE n.id = $id RETURN n.id",
                        {"id": postcondition["id"]},
                    )
                before = await harness._pre(backend, postcondition)
                if hooks is not None:
                    hooks.reset()
                started = time.perf_counter_ns()
                await runner._execute_operation(backend, context, operation)
                ended = time.perf_counter_ns()
                hook_result = _extract_hooks(hooks.snapshot()) if hooks is not None else None
                if hook_result is not None:
                    hook_result["applicability"] = {
                        "BufferPool._read_page": "recorded_when_the_operation_reads_a_page",
                        "LocalStorageDevice._still_names": "recorded_when_the_operation_checks_names",
                        "BufferPool._invalidate": "event_driven; recorded_when_a_read_view_is_dropped",
                    }
                if postcondition.get("kind") == "structural_write_variant":
                    raise MeasurementRefused(
                        "the frozen plan unexpectedly contains a non-discriminating structural_write_variant"
                    )
                await harness._check(backend, postcondition, before)
                sample: dict[str, Any] = {
                    "family": family,
                    "index": index,
                    "operation_id": operation["operation_id"],
                    "started_at_ns": started,
                    "ended_at_ns": ended,
                    "wall_ms": (ended - started) / 1e6,
                    "postcondition": postcondition["kind"],
                    "postcondition_status": "passed",
                }
                if hook_result is not None:
                    sample["hooks"] = hook_result
                samples.append(sample)
        active_end = time.perf_counter_ns()
        stop_event.set()
        if not b_quiescent_event.wait(float(config["barrier_timeout_seconds"])):
            raise MeasurementRefused("B did not quiesce with its handle open before live verify(all)")
        live_started = time.perf_counter_ns()
        live_verification = dict(await backend._verify_all())
        live_verification["clean"] = True
        live_ended = time.perf_counter_ns()
        live_verify_done_event.set()
        await backend.close()
        backend = None
        handle["closed_at_ns"] = time.perf_counter_ns()
        return {
            "role": "A",
            "status": "passed",
            "pid": os.getpid(),
            "spawn_start_method": multiprocessing.get_start_method(),
            "barrier_passed": barrier_passed,
            "barrier_token": barrier_token,
            "handle": handle,
            "active_start_ns": active_start,
            "active_end_ns": active_end,
            "reopens_during_measured_window": 0,
            "live_verifier": {
                "status": "passed",
                "cold_open": False,
                "handle_was_open": True,
                "verify_scope": "all",
                "started_at_ns": live_started,
                "ended_at_ns": live_ended,
                "verification": live_verification,
            },
            "instrumentation": {
                "enabled": instrumented,
                "installed": installed_hooks,
                "raw_contaminated": False,
            },
            "operation_set_sha256": plan_info["digest"],
            "families": list(plan_info["families"]),
            "samples": samples,
            "postconditions_passed": len(samples),
            "refusals": [],
        }
    finally:
        stop_event.set()
        live_verify_done_event.set()
        if backend is not None:
            await backend.close()
        if hooks is not None:
            hooks.uninstall()


def _process_a_worker(
    config: dict[str, Any],
    workspace_text: str,
    result_text: str,
    barrier: Any,
    stop_event: Any,
    b_quiescent_event: Any,
    live_verify_done_event: Any,
    instrumented: bool,
) -> None:
    result = Path(result_text)
    try:
        payload = asyncio.run(
            _run_process_a_async(
                config,
                Path(workspace_text),
                barrier,
                stop_event,
                b_quiescent_event,
                live_verify_done_event,
                instrumented,
            )
        )
    except BaseException as failure:
        stop_event.set()
        try:
            barrier.abort()
        except BaseException:
            pass
        _atomic_json(result, _failure_payload("A", failure))
        raise
    _atomic_json(result, payload)


async def _run_process_b_async(
    config: Mapping[str, Any],
    workspace: Path,
    barrier: Any,
    stop_event: Any,
    b_quiescent_event: Any,
    live_verify_done_event: Any,
    scenario: Scenario,
) -> dict[str, Any]:
    harness, runner, backends, fixtures, plan_info = _runtime(config)
    backend: Any = None
    commits: list[dict[str, Any]] = []
    barrier_passed = False
    session = f"ce3-b-{scenario.identifier}-{str(config['copy_id'])[:12]}"
    handle: dict[str, Any] = {}
    try:
        backend, context, handle = await _open_warm(
            harness, runner, backends, workspace
        )
        barrier_token = _barrier_wait(barrier, float(config["barrier_timeout_seconds"]))
        barrier_passed = True
        active_start = time.perf_counter_ns()
        if scenario.target_rate_per_second == 0.0:
            while not stop_event.wait(0.05):
                pass
        else:
            interval_ns = int(1e9 / scenario.target_rate_per_second)
            due = active_start + interval_ns
            sequence = 0
            while not stop_event.is_set():
                remaining = (due - time.perf_counter_ns()) / 1e9
                if remaining > 0 and stop_event.wait(min(remaining, 0.05)):
                    break
                if time.perf_counter_ns() < due:
                    continue
                sequence += 1
                node_id = f"{session}-{sequence:08d}"
                payload = fixtures._node(
                    str(scenario.table), node_id, session, f"foreign-{sequence}"
                )
                operation = harness._op(
                    "create_node", payload, "scope", 1_000_000 + sequence
                )
                started = time.perf_counter_ns()
                await runner._execute_operation(backend, context, operation)
                ended = time.perf_counter_ns()
                commits.append(
                    {
                        "sequence": sequence,
                        "node_id": node_id,
                        "scheduled_at_ns": due,
                        "started_at_ns": started,
                        "ended_at_ns": ended,
                        "wall_ms": (ended - started) / 1e6,
                        "schedule_lag_ms": (started - due) / 1e6,
                    }
                )
                # Never burst to hide an unattainable target.  The effective rate is evidence.
                due = max(due + interval_ns, ended + 1)
        active_end = time.perf_counter_ns()
        if scenario.table is not None:
            rows = await harness._read_rows(
                backend,
                f"MATCH (n:{scenario.table}) WHERE n.source_session_id = $s RETURN n.id",
                {"s": session},
            )
            observed_ids = sorted(str(row[0]) for row in rows)
        else:
            observed_ids = []
        expected_ids = sorted(commit["node_id"] for commit in commits)
        if observed_ids != expected_ids:
            raise MeasurementRefused(
                f"B effects mismatch: observed {len(observed_ids)} != committed {len(expected_ids)}"
            )
        b_quiescent_event.set()
        if not live_verify_done_event.wait(float(config["barrier_timeout_seconds"])):
            raise MeasurementRefused("A did not complete live verify(all) while B remained open")
        await backend.close()
        backend = None
        handle["closed_at_ns"] = time.perf_counter_ns()
        return {
            "role": "B",
            "status": "passed",
            "pid": os.getpid(),
            "spawn_start_method": multiprocessing.get_start_method(),
            "barrier_passed": barrier_passed,
            "barrier_token": barrier_token,
            "handle": handle,
            "active_start_ns": active_start,
            "active_end_ns": active_end,
            "reopens_during_measured_window": 0,
            "operation_set_sha256": plan_info["digest"],
            "scenario": scenario.as_dict(),
            "session": session,
            "commits": commits,
            "effects": {
                "expected_nodes": len(expected_ids),
                "observed_nodes": len(observed_ids),
                "status": "passed",
            },
            "refusals": [],
        }
    finally:
        b_quiescent_event.set()
        if backend is not None:
            await backend.close()


def _process_b_worker(
    config: dict[str, Any],
    workspace_text: str,
    result_text: str,
    barrier: Any,
    stop_event: Any,
    b_quiescent_event: Any,
    live_verify_done_event: Any,
    scenario_payload: dict[str, Any],
) -> None:
    result = Path(result_text)
    scenario = Scenario(
        str(scenario_payload["identifier"]),
        str(scenario_payload["relation"]),
        scenario_payload.get("table"),
        float(scenario_payload["target_rate_per_second"]),
    )
    try:
        payload = asyncio.run(
            _run_process_b_async(
                config,
                Path(workspace_text),
                barrier,
                stop_event,
                b_quiescent_event,
                live_verify_done_event,
                scenario,
            )
        )
    except BaseException as failure:
        stop_event.set()
        try:
            barrier.abort()
        except BaseException:
            pass
        _atomic_json(result, _failure_payload("B", failure))
        raise
    _atomic_json(result, payload)


async def _verify_async(config: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    harness, runner, backends, _fixtures, plan_info = _runtime(config)
    backend: Any = None
    try:
        backend, _context, handle = await _open_warm(
            harness, runner, backends, workspace
        )
        verification = dict(await backend._verify_all())
        verification["clean"] = True
        fingerprints = backend.observe_fingerprints()
        await backend.close()
        backend = None
        handle["closed_at_ns"] = time.perf_counter_ns()
        return {
            "role": "verifier",
            "status": "passed",
            "pid": os.getpid(),
            "cold_open": True,
            "verify_scope": "all",
            "verification": verification,
            "fingerprints": fingerprints,
            "handle": handle,
            "operation_set_sha256": plan_info["digest"],
        }
    finally:
        if backend is not None:
            await backend.close()


def _verify_worker(config: dict[str, Any], workspace_text: str, result_text: str) -> None:
    result = Path(result_text)
    try:
        payload = asyncio.run(_verify_async(config, Path(workspace_text)))
    except BaseException as failure:
        _atomic_json(result, _failure_payload("verifier", failure))
        raise
    _atomic_json(result, payload)


def _read_child_result(path: Path, role: str) -> dict[str, Any]:
    if not path.is_file():
        return {
            "role": role,
            "status": "failed",
            "failure": {"type": "MissingResult", "message": str(path)},
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as failure:
        return {
            "role": role,
            "status": "failed",
            "failure": {"type": type(failure).__name__, "message": str(failure)},
        }
    if not isinstance(value, dict):
        return {
            "role": role,
            "status": "failed",
            "failure": {"type": "InvalidResult", "message": "child JSON is not an object"},
        }
    return value


def _terminate_exact(processes: Iterable[multiprocessing.Process]) -> None:
    for process in processes:
        if process.is_alive():
            process.terminate()
    for process in processes:
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join(timeout=10)


def _join_until(processes: Sequence[multiprocessing.Process], timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    for process in processes:
        process.join(timeout=max(0.0, deadline - time.monotonic()))
    if any(process.is_alive() for process in processes):
        _terminate_exact(processes)


def _rate_evidence(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    if a.get("status") != "passed" or b.get("status") != "passed":
        return {"status": "unavailable", "reason": "a_or_b_failed"}
    start = max(int(a["active_start_ns"]), int(b["active_start_ns"]))
    end = min(int(a["active_end_ns"]), int(b["active_end_ns"]))
    overlap_seconds = max(0.0, (end - start) / 1e9)
    commits = [
        commit
        for commit in b.get("commits", [])
        if start <= int(commit["ended_at_ns"]) <= end
    ]
    effective = len(commits) / overlap_seconds if overlap_seconds > 0 else 0.0
    target = float(b["scenario"]["target_rate_per_second"])
    ratio = effective / target if target else None
    return {
        "status": "measured",
        "definition": "B commit completions inside intersection(A.active, B.active) / intersection seconds",
        "intersection_start_ns": start,
        "intersection_end_ns": end,
        "intersection_seconds": overlap_seconds,
        "commits_completed_in_intersection": len(commits),
        "target_rate_per_second": target,
        "effective_rate_per_second": effective,
        "effective_to_target_ratio": ratio,
    }


def _annotate_foreign_commits(
    a: Mapping[str, Any], b: Mapping[str, Any]
) -> list[dict[str, Any]]:
    commits = list(b.get("commits", [])) if b.get("status") == "passed" else []
    annotated: list[dict[str, Any]] = []
    for sample in a.get("samples", []):
        started = int(sample["started_at_ns"])
        ended = int(sample["ended_at_ns"])
        value = dict(sample)
        value["foreign_commits_completed_during_operation"] = sum(
            started <= int(commit["ended_at_ns"]) <= ended for commit in commits
        )
        annotated.append(value)
    return annotated


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise MeasurementRefused("a latency summary needs at least one sample")
    ordered = sorted(float(value) for value in values)
    rank = max(1, int((len(ordered) * percentile) + 0.999999999))
    return ordered[min(rank, len(ordered)) - 1]


def _latency_summary(values: Sequence[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50_ms": statistics.median(values),
        "p90_ms": _nearest_rank(values, 0.90),
        "p99_ms": _nearest_rank(values, 0.99),
        "max_ms": max(values),
    }


def _summarize_a_samples(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_family: dict[str, Any] = {}
    for family in EXPECTED_FAMILIES:
        selected = [sample for sample in samples if sample.get("family") == family]
        if not selected:
            continue
        summary = _latency_summary([float(sample["wall_ms"]) for sample in selected])
        summary["foreign_commits_completed"] = sum(
            int(sample.get("foreign_commits_completed_during_operation", 0))
            for sample in selected
        )
        if any("hooks" in sample for sample in selected):
            hook_summary: dict[str, Any] = {}
            for hook in REQUIRED_HOOKS:
                observed = [
                    sample["hooks"]
                    for sample in selected
                    if hook in sample.get("hooks", {}).get("calls", {})
                ]
                hook_summary[hook] = {
                    "samples_observed": len(observed),
                    "samples_missing": len(selected) - len(observed),
                    "positive_calls_median": (
                        statistics.median(sample["calls"][hook] for sample in observed)
                        if observed
                        else None
                    ),
                    "inclusive_ms_median_when_observed": (
                        statistics.median(sample["inclusive_ms"][hook] for sample in observed)
                        if observed
                        else None
                    ),
                }
            summary["hooks"] = hook_summary
        by_family[family] = summary
    return {
        "by_family": by_family,
        "sum_of_family_wall_ms_medians": sum(
            float(summary["p50_ms"]) for summary in by_family.values()
        ),
        "all_operations": _latency_summary(
            [float(sample["wall_ms"]) for sample in samples]
        ),
    }


def _scenario_is_exact(value: object, canonical: Scenario) -> bool:
    if not isinstance(value, Mapping):
        return False
    expected = canonical.as_dict()
    if set(value) != set(expected):
        return False
    return all(
        type(value.get(name)) is type(expected_value)
        and value.get(name) == expected_value
        for name, expected_value in expected.items()
    )


def _official_environment_matches(value: object) -> bool:
    """Validate observed versions and availability, not self-asserted booleans alone."""

    if not isinstance(value, Mapping):
        return False
    expected_baseline = {
        "python": OFFICIAL_PYTHON_VERSION,
        "numpy": OFFICIAL_NUMPY_VERSION,
        "ladybug": OFFICIAL_LADYBUG_VERSION,
        "google_crc32c": "available",
    }
    expected_checks = {
        "python_3_13_1": True,
        "numpy_2_5_1": True,
        "ladybug_0_16_0": True,
        "google_crc32c_available": True,
    }
    modules = value.get("modules")
    if not isinstance(modules, Mapping):
        return False
    numpy = modules.get("numpy")
    ladybug = modules.get("ladybug")
    google_crc32c = modules.get("google_crc32c")
    if not all(isinstance(module, Mapping) for module in (numpy, ladybug, google_crc32c)):
        return False
    return bool(
        value.get("python_release") == OFFICIAL_PYTHON_VERSION
        and value.get("accel_ready") is True
        and value.get("official_baseline") == expected_baseline
        and value.get("official_baseline_checks") == expected_checks
        and value.get("official_baseline_matches") is True
        and numpy.get("version") == OFFICIAL_NUMPY_VERSION
        and isinstance(numpy.get("origin"), str)
        and numpy.get("origin")
        and "error" not in numpy
        and ladybug.get("version") == OFFICIAL_LADYBUG_VERSION
        and isinstance(ladybug.get("origin"), str)
        and ladybug.get("origin")
        and "error" not in ladybug
        and google_crc32c.get("version")
        and isinstance(google_crc32c.get("origin"), str)
        and google_crc32c.get("origin")
        and "error" not in google_crc32c
    )


def _environment_runtime_identity(value: object) -> dict[str, Any] | None:
    """Return the observed executor/module identity shared by launcher and children."""

    if not _official_environment_matches(value):
        return None
    assert isinstance(value, Mapping)
    modules = value["modules"]
    assert isinstance(modules, Mapping)
    return {
        "python_executable_sha256": value.get("python_executable_sha256"),
        "python_release": value.get("python_release"),
        "modules": {
            name: {
                "version": modules[name].get("version"),
                "origin": modules[name].get("origin"),
            }
            for name in ("numpy", "ladybug", "google_crc32c")
        },
    }


def _scenario_shortfalls(result: Mapping[str, Any]) -> list[str]:
    """Fail-closed criteria; tests mutate each evidence surface independently."""

    shortfalls: list[str] = []
    a = result.get("process_a", {})
    b = result.get("process_b", {})
    verifier = result.get("verifier", {})
    if a.get("status") != "passed":
        shortfalls.append("process_a_failed")
    if b.get("status") != "passed":
        shortfalls.append("process_b_failed")
    if verifier.get("status") != "passed":
        shortfalls.append("post_run_verifier_failed")
    copy_auth = result.get("copy_authentication", {})
    if (
        copy_auth.get("initial_matches_base") is not True
        or copy_auth.get("initial") != copy_auth.get("base")
    ):
        shortfalls.append("scenario_copy_not_authenticated")
    if result.get("barrier", {}).get("parent_participated") is not True:
        shortfalls.append("spawn_barrier_parent_not_proved")
    if a.get("barrier_passed") is not True or b.get("barrier_passed") is not True:
        shortfalls.append("spawn_barrier_not_proved")
    if a.get("pid") == b.get("pid") or not a.get("pid") or not b.get("pid"):
        shortfalls.append("distinct_real_processes_not_proved")
    if a.get("reopens_during_measured_window") != 0 or b.get("reopens_during_measured_window") != 0:
        shortfalls.append("measured_handle_reopened")
    per_family = result.get("per_family")
    expected_samples = (
        int(per_family) * len(EXPECTED_FAMILIES)
        if isinstance(per_family, int) and not isinstance(per_family, bool) and per_family > 0
        else OFFICIAL_PER_FAMILY * len(EXPECTED_FAMILIES)
    )
    if len(a.get("samples", [])) != expected_samples:
        shortfalls.append("a_did_not_run_exact_pf5")
    if tuple(a.get("families", ())) != EXPECTED_FAMILIES:
        shortfalls.append("a_family_set_or_order_changed")
    if a.get("postconditions_passed") != expected_samples:
        shortfalls.append("a_postconditions_incomplete")
    samples = list(a.get("samples", []))
    if any(sample.get("postcondition_status") != "passed" for sample in samples):
        shortfalls.append("a_postcondition_status_not_passed")
    family_counts = {
        family: sum(sample.get("family") == family for sample in samples)
        for family in EXPECTED_FAMILIES
    }
    expected_per_family = expected_samples // len(EXPECTED_FAMILIES)
    if any(count != expected_per_family for count in family_counts.values()):
        shortfalls.append("a_family_sample_counts_changed")
    if a.get("refusals") or b.get("refusals"):
        shortfalls.append("refusal_was_not_fail_closed")
    if any(
        result.get("process_exitcodes", {}).get(role) != 0
        for role in ("a", "b", "verifier")
    ):
        shortfalls.append("child_process_exit_nonzero")
    if any(
        participant.get("operation_set_sha256") != a.get("operation_set_sha256")
        for participant in (b, verifier)
    ):
        shortfalls.append("child_operation_set_digest_disagrees")
    if per_family == OFFICIAL_PER_FAMILY and a.get("operation_set_sha256") != EXPECTED_OPERATION_SET_SHA256:
        shortfalls.append("child_logical_pf5_digest_mismatch")
    if any(
        participant.get("handle", {}).get("checksum_implementation") != "native"
        for participant in (a, b, verifier)
    ):
        shortfalls.append("child_accel_checksum_not_native")
    child_environments = [
        participant.get("handle", {}).get("environment", {})
        for participant in (a, b, verifier)
    ]
    if any(not _official_environment_matches(environment) for environment in child_environments):
        shortfalls.append("child_official_environment_mismatch")
    child_runtime_identities = [
        _environment_runtime_identity(environment) for environment in child_environments
    ]
    if any(
        identity is None or identity != child_runtime_identities[0]
        for identity in child_runtime_identities
    ):
        shortfalls.append("child_environment_identity_disagrees")
    executor_hashes = {
        environment.get("python_executable_sha256") for environment in child_environments
    }
    if len(executor_hashes) != 1 or None in executor_hashes:
        shortfalls.append("child_executor_identity_disagrees")
    scenario = result.get("scenario", {})
    canonical = next(
        (candidate for candidate in SCENARIOS if candidate.identifier == scenario.get("id")),
        None,
    )
    if canonical is None:
        shortfalls.append("scenario_id_not_canonical")
    else:
        if not _scenario_is_exact(scenario, canonical):
            shortfalls.append("result_scenario_not_canonical")
        if not _scenario_is_exact(b.get("scenario", {}), canonical):
            shortfalls.append("process_b_scenario_not_canonical")
    effects = b.get("effects", {})
    if (
        effects.get("status") != "passed"
        or effects.get("expected_nodes") != effects.get("observed_nodes")
        or effects.get("expected_nodes") != len(b.get("commits", []))
    ):
        shortfalls.append("b_effects_not_proved")
    instrumentation = a.get("instrumentation", {})
    measured_pass = result.get("pass")
    if measured_pass == "raw":
        if instrumentation.get("enabled") is not False or instrumentation.get("installed"):
            shortfalls.append("raw_pass_contaminated_by_hooks")
        if any("hooks" in sample for sample in a.get("samples", [])):
            shortfalls.append("raw_samples_contain_hook_output")
    elif measured_pass == "instrumented":
        if instrumentation.get("enabled") is not True:
            shortfalls.append("instrumented_pass_missing_hooks")
        if tuple(instrumentation.get("installed", ())) != REQUIRED_HOOKS:
            shortfalls.append("instrumented_hook_installation_not_proved")
        per_operation_complete = True
        observed_totals = {hook: 0 for hook in REQUIRED_HOOKS}
        for sample in samples:
            captured = sample.get("hooks")
            if not isinstance(captured, Mapping) or captured.get("capture_status") != "captured_after_reset":
                per_operation_complete = False
                continue
            calls = captured.get("calls", {})
            inclusive = captured.get("inclusive_ms", {})
            observed = captured.get("observed_hooks")
            if (
                not isinstance(calls, Mapping)
                or not isinstance(inclusive, Mapping)
                or not isinstance(observed, list)
            ):
                per_operation_complete = False
                continue
            call_keys = list(calls)
            if observed != call_keys or set(inclusive) != set(calls) or not calls:
                per_operation_complete = False
            if any(
                hook not in REQUIRED_HOOKS
                or not isinstance(count, int)
                or isinstance(count, bool)
                or count <= 0
                for hook, count in calls.items()
            ):
                per_operation_complete = False
            if any(
                not isinstance(duration, (int, float))
                or isinstance(duration, bool)
                or float(duration) < 0.0
                for duration in inclusive.values()
            ):
                per_operation_complete = False
            for hook in REQUIRED_HOOKS:
                observed_totals[hook] += int(calls.get(hook, 0))
        if not per_operation_complete or len(samples) != expected_samples:
            shortfalls.append("instrumented_per_operation_hook_evidence_incomplete")
        if any(total <= 0 for total in observed_totals.values()):
            shortfalls.append("instrumented_required_hook_never_observed")
    else:
        shortfalls.append("unknown_pass")
    live_verifier = a.get("live_verifier", {})
    live_verification = live_verifier.get("verification", {})
    if (
        live_verifier.get("status") != "passed"
        or live_verifier.get("cold_open") is not False
        or live_verifier.get("handle_was_open") is not True
        or live_verifier.get("verify_scope") != "all"
        or live_verification.get("clean") is not True
        or live_verification.get("engine") != "okto-grafx"
        or not any(
            int(live_verification.get(name, 0)) > 0
            for name in ("pages_checked", "records_checked", "index_entries_checked")
        )
    ):
        shortfalls.append("live_verify_all_clean_coverage_not_proved")
    else:
        if not (
            int(a.get("active_end_ns", 2**63 - 1))
            <= int(live_verifier.get("started_at_ns", 0))
            <= int(live_verifier.get("ended_at_ns", 0))
            <= int(a.get("handle", {}).get("closed_at_ns", 0))
            and int(live_verifier.get("ended_at_ns", 2**63 - 1))
            <= int(b.get("handle", {}).get("closed_at_ns", 0))
        ):
            shortfalls.append("live_verify_not_inside_open_handle_window")
    verification = verifier.get("verification", {})
    if (
        verifier.get("verify_scope") != "all"
        or verifier.get("cold_open") is not True
        or verification.get("clean") is not True
        or verification.get("engine") != "okto-grafx"
        or not any(
            int(verification.get(name, 0)) > 0
            for name in ("pages_checked", "records_checked", "index_entries_checked")
        )
    ):
        shortfalls.append("cold_verify_all_clean_coverage_not_proved")
    rate = result.get("rate", {})
    recomputed_rate = _rate_evidence(a, b)
    comparable_rate_fields = (
        "status",
        "intersection_start_ns",
        "intersection_end_ns",
        "intersection_seconds",
        "commits_completed_in_intersection",
        "target_rate_per_second",
        "effective_rate_per_second",
        "effective_to_target_ratio",
    )
    if any(rate.get(name) != recomputed_rate.get(name) for name in comparable_rate_fields):
        shortfalls.append("effective_rate_not_reproducible_from_timestamps")
        rate = recomputed_rate
    target = float(scenario.get("target_rate_per_second", -1))
    effective = float(rate.get("effective_rate_per_second", -1))
    if rate.get("status") != "measured" or float(rate.get("intersection_seconds", 0)) <= 0:
        shortfalls.append("effective_rate_intersection_missing")
    elif target == 0:
        if int(rate.get("commits_completed_in_intersection", -1)) != 0 or effective != 0:
            shortfalls.append("idle_scenario_committed")
    else:
        ratio = rate.get("effective_to_target_ratio")
        if int(rate.get("commits_completed_in_intersection", 0)) <= 0:
            shortfalls.append("foreign_commit_not_observed")
        if ratio is None or not (1 - OFFICIAL_RATE_TOLERANCE <= float(ratio) <= 1 + OFFICIAL_RATE_TOLERANCE):
            shortfalls.append("effective_rate_outside_frozen_target_tolerance")
    if not (
        int(b.get("handle", {}).get("opened_at_ns", 2**63 - 1))
        <= int(a.get("active_start_ns", 0))
        and int(b.get("handle", {}).get("closed_at_ns", 0))
        >= int(a.get("active_end_ns", 2**63 - 1))
    ):
        shortfalls.append("b_handle_did_not_cover_a_window")
    return sorted(set(shortfalls))


def official_shortfalls(report: Mapping[str, Any]) -> list[str]:
    """Return every reason a report cannot be cited as the literal official CE-3 matrix."""

    shortfalls: list[str] = []
    inputs = report.get("inputs", {})
    provenance = report.get("provenance", {})
    if report.get("official_requested") is not True:
        shortfalls.append("official_not_requested")
    if report.get("check_only") is not False:
        shortfalls.append("check_only_has_no_measurements")
    if inputs.get("per_family") != OFFICIAL_PER_FAMILY:
        shortfalls.append("per_family_not_pf5")
    if provenance.get("operation_set_sha256") != EXPECTED_OPERATION_SET_SHA256:
        shortfalls.append("logical_pf5_digest_mismatch")
    identity = provenance.get("identity", {})
    identity_start = identity.get("start", {})
    identity_end = identity.get("end", {})
    identity_start_stable = _identity_stable_payload(identity_start)
    identity_end_stable = _identity_stable_payload(identity_end)
    if (
        identity.get("stable") is not True
        or identity_start_stable is None
        or identity_end_stable is None
        or identity_start_stable != identity_end_stable
        or not identity_start.get("fingerprint_sha256")
        or identity_start.get("fingerprint_sha256")
        != identity_end.get("fingerprint_sha256")
        or identity_start.get("fingerprint_sha256")
        != _identity_fingerprint(identity_start)
        or identity_end.get("fingerprint_sha256") != _identity_fingerprint(identity_end)
    ):
        shortfalls.append("tool_executor_or_checkout_identity_drift")
    expected_tool_commit = inputs.get("tool_commit")
    expected_tool_blob = inputs.get("tool_blob")
    for capture in (identity_start, identity_end):
        tool = capture.get("tool", {})
        guard = tool.get("raw_hook_guard", {})
        if (
            not expected_tool_commit
            or tool.get("commit") != expected_tool_commit
            or tool.get("expected_commit") != expected_tool_commit
            or not expected_tool_blob
            or tool.get("blob") != expected_tool_blob
            or tool.get("committed_blob") != expected_tool_blob
            or tool.get("expected_blob") != expected_tool_blob
            or guard.get("status") != "passed"
            or guard.get("sha256") != RAW_HOOK_GUARD_SHA256
            or guard.get("expected_sha256") != RAW_HOOK_GUARD_SHA256
        ):
            shortfalls.append("launcher_blob_commit_or_raw_guard_not_pinned")
        executor = capture.get("executor", {})
        if (
            not executor.get("sha256")
            or executor.get("python_release") != OFFICIAL_PYTHON_VERSION
        ):
            shortfalls.append("executor_identity_not_pinned")
    harness = provenance.get("harness", {})
    if harness.get("head") != PINNED_HARNESS_HEAD or harness.get("profile_blob") != PINNED_HARNESS_BLOB:
        shortfalls.append("harness_pin_mismatch")
    source = provenance.get("source_workspace", {})
    if not source.get("expected_sha256") or source.get("sha256") != source.get("expected_sha256"):
        shortfalls.append("source_workspace_not_authenticated")
    source_provenance = provenance.get("source_provenance", {})
    if (
        not source_provenance.get("expected_sha256")
        or source_provenance.get("sha256") != source_provenance.get("expected_sha256")
        or source_provenance.get("semantic_status") != "passed"
        or not all(source_provenance.get("semantic_checks", {}).values())
    ):
        shortfalls.append("source_provenance_not_authenticated")
    if report.get("source_unchanged_after_run") is not True:
        shortfalls.append("source_workspace_changed")
    if source.get("after") != {
        name: source.get(name) for name in ("sha256", "files", "bytes")
    }:
        shortfalls.append("source_after_digest_mismatch")
    if report.get("machine", {}).get("machine_idle_asserted") is not True:
        shortfalls.append("machine_idle_not_asserted")
    before_cpu = report.get("machine", {}).get("before", {}).get("cpu_percent")
    maximum_cpu = inputs.get("maximum_initial_cpu_percent")
    if before_cpu is None or maximum_cpu is None or float(before_cpu) > float(maximum_cpu):
        shortfalls.append("machine_idle_sample_failed")
    checkouts = provenance.get("checkouts", {})
    if checkouts != identity_start.get("checkouts"):
        shortfalls.append("checkout_identity_not_bound_to_launcher_capture")
    for name in ("grafx", "community", "core"):
        checkout = checkouts.get(name, {})
        if checkout.get("status") != "clean":
            shortfalls.append(f"{name}_checkout_not_clean")
        if not checkout.get("expected_head") or checkout.get("head") != checkout.get("expected_head"):
            shortfalls.append(f"{name}_checkout_pin_mismatch")
    environment = provenance.get("environment", {})
    if environment.get("accel_ready") is not True:
        shortfalls.append("accel_environment_not_proved")
    if not _official_environment_matches(environment):
        shortfalls.append("official_environment_baseline_mismatch")
    results = report.get("results", [])
    expected_keys = {
        (pass_name, scenario.identifier)
        for pass_name in ("raw", "instrumented")
        for scenario in SCENARIOS
    }
    actual_keys = {(result.get("pass"), result.get("scenario", {}).get("id")) for result in results}
    if actual_keys != expected_keys or len(results) != len(expected_keys):
        shortfalls.append("raw_instrumented_matrix_incomplete")
    copy_ids = [result.get("copy_id") for result in results]
    if len(set(copy_ids)) != len(copy_ids) or None in copy_ids:
        shortfalls.append("raw_and_instrumented_copies_not_distinct")
    launcher_executor_sha256 = identity_start.get("executor", {}).get("sha256")
    launcher_environment_identity = _environment_runtime_identity(environment)
    if (
        launcher_environment_identity is None
        or launcher_environment_identity.get("python_executable_sha256")
        != launcher_executor_sha256
    ):
        shortfalls.append("launcher_environment_not_bound_to_executor")
    if not launcher_executor_sha256 or any(
        participant.get("handle", {})
        .get("environment", {})
        .get("python_executable_sha256")
        != launcher_executor_sha256
        for result in results
        for participant in (
            result.get("process_a", {}),
            result.get("process_b", {}),
            result.get("verifier", {}),
        )
    ):
        shortfalls.append("child_executor_differs_from_launcher")
    if launcher_environment_identity is None or any(
        _environment_runtime_identity(
            participant.get("handle", {}).get("environment", {})
        )
        != launcher_environment_identity
        for result in results
        for participant in (
            result.get("process_a", {}),
            result.get("process_b", {}),
            result.get("verifier", {}),
        )
    ):
        shortfalls.append("child_environment_differs_from_launcher")
    fixture_base = provenance.get("fixture_base", {})
    expected_base_digest = {
        name: fixture_base.get(name) for name in ("sha256", "files", "bytes")
    }
    if not expected_base_digest.get("sha256"):
        shortfalls.append("fixture_base_not_authenticated")
    elif any(
        result.get("copy_authentication", {}).get("base") != expected_base_digest
        for result in results
    ):
        shortfalls.append("scenario_copy_base_disagrees_with_provenance")
    for result in results:
        shortfalls.extend(
            f"{result.get('pass')}:{result.get('scenario', {}).get('id')}:{failure}"
            for failure in _scenario_shortfalls(result)
        )
    return sorted(set(shortfalls))


def _scenario_payload(scenario: Scenario) -> dict[str, Any]:
    return {
        "identifier": scenario.identifier,
        "relation": scenario.relation,
        "table": scenario.table,
        "target_rate_per_second": scenario.target_rate_per_second,
    }


def _run_scenario(
    config: dict[str, Any],
    base_workspace: Path,
    base_digest: Mapping[str, Any],
    pass_name: str,
    scenario: Scenario,
) -> dict[str, Any]:
    scratch = Path(str(config["scratch"])).resolve()
    copy_id = f"{pass_name}-{scenario.identifier}-{hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:12]}"
    workspace = scratch / f"m7profile-ce3-{copy_id}" / "workspace"
    shutil.copytree(base_workspace, workspace)
    initial = content_digest(workspace)
    results_dir = scratch / "ce3-results" / copy_id
    a_result = results_dir / "a.json"
    b_result = results_dir / "b.json"
    verifier_result = results_dir / "verify.json"
    child_config = dict(config)
    child_config["copy_id"] = copy_id
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    stop_event = context.Event()
    b_quiescent_event = context.Event()
    live_verify_done_event = context.Event()
    process_a = context.Process(
        target=_process_a_worker,
        name=f"ce3-a-{copy_id}",
        args=(
            child_config,
            str(workspace),
            str(a_result),
            barrier,
            stop_event,
            b_quiescent_event,
            live_verify_done_event,
            pass_name == "instrumented",
        ),
    )
    process_b = context.Process(
        target=_process_b_worker,
        name=f"ce3-b-{copy_id}",
        args=(
            child_config,
            str(workspace),
            str(b_result),
            barrier,
            stop_event,
            b_quiescent_event,
            live_verify_done_event,
            _scenario_payload(scenario),
        ),
    )
    released_at: int | None = None
    process_a.start()
    process_b.start()
    try:
        _barrier_wait(barrier, float(config["barrier_timeout_seconds"]))
        released_at = time.perf_counter_ns()
    except BaseException:
        stop_event.set()
        try:
            barrier.abort()
        except BaseException:
            pass
    _join_until((process_a, process_b), float(config["child_timeout_seconds"]))
    a = _read_child_result(a_result, "A")
    b = _read_child_result(b_result, "B")
    if process_a.exitcode != 0 and a.get("status") == "passed":
        a = _failure_payload("A", MeasurementRefused(f"unexpected exit code {process_a.exitcode}"))
    if process_b.exitcode != 0 and b.get("status") == "passed":
        b = _failure_payload("B", MeasurementRefused(f"unexpected exit code {process_b.exitcode}"))
    verifier_process = context.Process(
        target=_verify_worker,
        name=f"ce3-verify-{copy_id}",
        args=(child_config, str(workspace), str(verifier_result)),
    )
    verifier_process.start()
    _join_until((verifier_process,), float(config["child_timeout_seconds"]))
    verifier = _read_child_result(verifier_result, "verifier")
    if verifier_process.exitcode != 0 and verifier.get("status") == "passed":
        verifier = _failure_payload(
            "verifier", MeasurementRefused(f"unexpected exit code {verifier_process.exitcode}")
        )
    if a.get("status") == "passed":
        a = dict(a)
        a["samples"] = _annotate_foreign_commits(a, b)
        a["summary"] = _summarize_a_samples(a["samples"])
    if b.get("status") == "passed" and b.get("commits"):
        b = dict(b)
        b["commit_latency"] = _latency_summary(
            [float(commit["wall_ms"]) for commit in b["commits"]]
        )
    final_digest = content_digest(workspace)
    result: dict[str, Any] = {
        "copy_id": copy_id,
        "per_family": int(config["per_family"]),
        "pass": pass_name,
        "scenario": scenario.as_dict(),
        "barrier": {
            "implementation": "multiprocessing.get_context('spawn').Barrier(3)",
            "parent_released_at_ns": released_at,
            "parent_participated": released_at is not None,
        },
        "copy_authentication": {
            "workspace": str(workspace),
            "base": dict(base_digest),
            "initial": initial,
            "initial_matches_base": initial == dict(base_digest),
            "post_run": final_digest,
        },
        "process_a": a,
        "process_b": b,
        "verifier": verifier,
        "rate": _rate_evidence(a, b),
        "process_exitcodes": {
            "a": process_a.exitcode,
            "b": process_b.exitcode,
            "verifier": verifier_process.exitcode,
        },
    }
    result["shortfalls"] = _scenario_shortfalls(result)
    result["status"] = "passed" if not result["shortfalls"] else "failed"
    scenario_root = workspace.parent
    if scenario_root.parent != scratch or not scenario_root.name.startswith("m7profile-ce3-"):
        raise MeasurementRefused(f"refusing to remove unexpected scenario root {scenario_root}")
    shutil.rmtree(scenario_root)
    return result


def _machine_state() -> dict[str, Any]:
    state: dict[str, Any] = {
        "sampled_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cpu_percent": None,
        "python_processes": None,
        "method": "unavailable",
    }
    try:
        import psutil

        state["cpu_percent"] = psutil.cpu_percent(interval=2.0)
        state["python_processes"] = sum(
            1
            for process in psutil.process_iter(["name"])
            if str(process.info.get("name") or "").lower().startswith("python")
        )
        state["method"] = "psutil"
    except BaseException as failure:
        state["error"] = f"{type(failure).__name__}: {failure}"
    return state


def _environment() -> dict[str, Any]:
    modules: dict[str, Any] = {}
    distributions = {
        "numpy": "numpy",
        "google_crc32c": "google-crc32c",
        "ladybug": "ladybug",
    }
    for name, distribution in distributions.items():
        try:
            module = importlib.import_module(name)
            modules[name] = {
                "version": importlib.metadata.version(distribution),
                "origin": str(Path(module.__file__).resolve()),
            }
        except BaseException as failure:
            modules[name] = {"error": f"{type(failure).__name__}: {failure}"}
    python_release = platform.python_version()
    baseline_checks = {
        "python_3_13_1": python_release == OFFICIAL_PYTHON_VERSION,
        "numpy_2_5_1": modules.get("numpy", {}).get("version") == OFFICIAL_NUMPY_VERSION,
        "ladybug_0_16_0": modules.get("ladybug", {}).get("version")
        == OFFICIAL_LADYBUG_VERSION,
        "google_crc32c_available": "error" not in modules.get("google_crc32c", {}),
    }
    return {
        "python": sys.executable,
        "python_executable_sha256": _sha256_file(Path(sys.executable).resolve()),
        "python_version": sys.version,
        "python_release": python_release,
        "platform": platform.platform(),
        "modules": modules,
        "accel_ready": all(
            "error" not in modules[name] for name in ("numpy", "google_crc32c")
        ),
        "official_baseline": {
            "python": OFFICIAL_PYTHON_VERSION,
            "numpy": OFFICIAL_NUMPY_VERSION,
            "ladybug": OFFICIAL_LADYBUG_VERSION,
            "google_crc32c": "available",
        },
        "official_baseline_checks": baseline_checks,
        "official_baseline_matches": all(baseline_checks.values()),
    }


def _source_binding(source_workspace: Path) -> dict[str, Any]:
    roots = [path for path in (source_workspace / ".mp7" / "g").iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise MeasurementRefused(
            f"expected one Grafx root under {source_workspace / '.mp7' / 'g'}, found {len(roots)}"
        )
    binding_path = roots[0] / "kg" / "boards" / "m-pulse-7-acceptance" / "graph_backend_binding.json"
    if not binding_path.is_file():
        raise MeasurementRefused(f"M-PULSE-7 Grafx binding missing: {binding_path}")
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    if binding.get("backend") != "grafx":
        raise MeasurementRefused(f"source binding backend is not grafx: {binding.get('backend')!r}")
    physical = roots[0] / "kg" / str(binding.get("physical_path", ""))
    if not physical.is_dir():
        raise MeasurementRefused(f"source binding physical_path is missing: {physical}")
    return {
        "root_relative": roots[0].relative_to(source_workspace).as_posix(),
        "binding_relative": binding_path.relative_to(source_workspace).as_posix(),
        "binding_sha256": _sha256_file(binding_path),
        "physical_relative": physical.relative_to(source_workspace).as_posix(),
        "backend": "grafx",
    }


def source_provenance_evidence(
    path: Path,
    source_workspace: Path,
    source_digest: Mapping[str, Any],
    *,
    expected_file_sha256: str | None,
) -> dict[str, Any]:
    """Prove that the named report actually certifies this source and the frozen pf5 plan."""

    resolved = path.resolve()
    file_sha256 = _sha256_file(resolved)
    if expected_file_sha256 and file_sha256 != expected_file_sha256:
        raise MeasurementRefused(
            f"source provenance digest {file_sha256} != expected {expected_file_sha256}"
        )
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as failure:
        raise MeasurementRefused(
            f"source provenance is not readable JSON: {type(failure).__name__}: {failure}"
        ) from failure
    if not isinstance(payload, dict):
        raise MeasurementRefused("source provenance JSON must be an object")
    forensic = payload.get("forensic")
    if not isinstance(forensic, dict):
        raise MeasurementRefused("source provenance has no forensic object")
    declared_workspace = Path(str(forensic.get("workspace", ""))).resolve()
    semantic_checks = {
        "backend_is_grafx": payload.get("backend") == "grafx",
        "continuous_mode": payload.get("mode") == "continuous",
        "scope_path": payload.get("method_path") == "scope",
        "per_family_is_five": payload.get("per_family") == OFFICIAL_PER_FAMILY,
        "logical_digest_is_pf5": payload.get("operation_set_sha256")
        == EXPECTED_OPERATION_SET_SHA256,
        "workspace_path_matches": declared_workspace == source_workspace.resolve(),
        "workspace_digest_matches": forensic.get("content_sha256")
        == source_digest.get("sha256"),
        "workspace_file_count_matches": forensic.get("files") == source_digest.get("files"),
        "workspace_bytes_match": forensic.get("bytes") == source_digest.get("bytes"),
    }
    if not all(semantic_checks.values()):
        failed = sorted(name for name, passed in semantic_checks.items() if not passed)
        raise MeasurementRefused(
            "source provenance does not certify this continuous pf5 board: "
            + ", ".join(failed)
        )
    return {
        "path": str(resolved),
        "sha256": file_sha256,
        "expected_sha256": expected_file_sha256,
        "semantic_status": "passed",
        "semantic_checks": semantic_checks,
        "producer_checkouts": payload.get("checkouts"),
    }


def _assert_output_outside_inputs(
    out: Path,
    work_root: Path,
    source: Path,
    checkouts: Sequence[Path],
) -> None:
    resolved_out = out.resolve()
    resolved_work = work_root.resolve()
    if _is_within(resolved_out, source):
        raise MeasurementRefused("report path must be outside the source workspace/database")
    for checkout in checkouts:
        if _is_within(resolved_out, checkout) or _is_within(resolved_work, checkout):
            raise MeasurementRefused(
                f"official report/work paths must be outside measured checkout {checkout.resolve()}"
            )
    if _is_within(resolved_work, source) or _is_within(source, resolved_work):
        raise MeasurementRefused("work root and source workspace must not contain each other")


def run(args: argparse.Namespace) -> dict[str, Any]:
    source = args.source_workspace.resolve()
    harness_repo = args.harness_repo.resolve()
    grafx_repo = args.grafx.resolve()
    core_repo = args.core.resolve()
    work_root = args.work_root.resolve()
    out = args.out.resolve()
    if args.official and args.scenario != "all":
        raise MeasurementRefused("official CE-3 evidence requires --scenario all")
    if args.official and args.per_family != OFFICIAL_PER_FAMILY:
        raise MeasurementRefused("official CE-3 evidence requires --per-family 5")
    if args.official:
        _assert_output_outside_inputs(
            out, work_root, source, (harness_repo, grafx_repo, core_repo)
        )
    identity_start = _capture_identity(
        grafx_repo=grafx_repo,
        harness_repo=harness_repo,
        core_repo=core_repo,
        expected_grafx_head=args.grafx_sha,
        expected_core_head=args.core_sha,
        expected_tool_commit=args.tool_commit,
        expected_tool_blob=args.tool_blob,
    )
    checkouts = identity_start["checkouts"]
    profile = harness_repo / "tools" / "profile_m7_families.py"
    profile_blob = _git(harness_repo, "hash-object", str(profile))
    if profile_blob != PINNED_HARNESS_BLOB:
        raise MeasurementRefused(
            f"profile harness blob {profile_blob} != frozen {PINNED_HARNESS_BLOB}"
        )
    source_before = content_digest(source)
    if args.source_workspace_sha256 and source_before["sha256"] != args.source_workspace_sha256:
        raise MeasurementRefused(
            f"source workspace digest {source_before['sha256']} != expected {args.source_workspace_sha256}"
        )
    binding = _source_binding(source)
    provenance_path = args.source_provenance.resolve()
    source_provenance = source_provenance_evidence(
        provenance_path,
        source,
        source_before,
        expected_file_sha256=args.source_provenance_sha256,
    )
    work_root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="grafx-ce3-", dir=work_root)).resolve()
    profile_run_id = f"m7-ce3-{hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:20]}"
    config: dict[str, Any] = {
        "harness_repo": str(harness_repo),
        "grafx_repo": str(grafx_repo),
        "core_repo": str(core_repo),
        "source_workspace": str(source),
        "scratch": str(scratch),
        "profile_run_id": profile_run_id,
        "per_family": args.per_family,
        "official_requested": bool(args.official),
        "barrier_timeout_seconds": args.barrier_timeout_seconds,
        "child_timeout_seconds": args.child_timeout_seconds,
    }
    _prepare_import_paths(config)
    environment = _environment()
    harness, runner, backends, fixtures, plan_info = _runtime(config)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "tool": {
            "path": str(Path(__file__).resolve()),
            "sha256": _sha256_file(Path(__file__).resolve()),
            "commit": identity_start["tool"]["commit"],
            "blob": identity_start["tool"]["blob"],
            "expected_commit": args.tool_commit,
            "expected_blob": args.tool_blob,
        },
        "official_requested": bool(args.official),
        "official": False,
        "inputs": {
            "per_family": args.per_family,
            "method_path": "scope",
            "scenario_selector": args.scenario,
            "machine_idle_asserted": bool(args.machine_idle_asserted),
            "maximum_initial_cpu_percent": args.maximum_initial_cpu_percent,
            "barrier_timeout_seconds": args.barrier_timeout_seconds,
            "child_timeout_seconds": args.child_timeout_seconds,
            "no_reopen_workaround": True,
            "tool_commit": args.tool_commit,
            "tool_blob": args.tool_blob,
        },
        "provenance": {
            "harness": {
                "repo": str(harness_repo),
                "head": checkouts["community"]["head"],
                "profile": str(profile),
                "profile_blob": profile_blob,
            },
            "operation_set_sha256": plan_info["digest"],
            "expected_pf5_operation_set_sha256": EXPECTED_OPERATION_SET_SHA256,
            "families": list(plan_info["families"]),
            "checkouts": checkouts,
            "environment": environment,
            "source_workspace": {
                "path": str(source),
                **source_before,
                "expected_sha256": args.source_workspace_sha256,
                "binding": binding,
                "opened_in_place": False,
            },
            "source_provenance": source_provenance,
            "profile_run_id": profile_run_id,
            "identity": {"start": identity_start, "end": None, "stable": None},
        },
        "machine": {
            "before": _machine_state(),
            "after": None,
            "machine_idle_asserted": bool(args.machine_idle_asserted),
        },
        "matrix": {
            "passes": ["raw", "instrumented"],
            "scenarios": [scenario.as_dict() for scenario in SCENARIOS],
            "scenario_meanings": {
                scenario.identifier: scenario.meaning for scenario in SCENARIOS
            },
            "same_table": "Decision",
            "unrelated_table": "Assumption",
            "raw_and_instrumented_use_distinct_copies": True,
        },
        "results": [],
        "check_only": bool(args.check_only),
        "notes": [
            "CE-3 is not implemented by this tool; it only measures the frozen two-process question.",
            "RAW has no hooks/cProfile/timers. Instrumented captures H8 on A only and is never subtracted from RAW.",
            "B uses one continuously open handle. No refusal is retried or relabelled.",
            "same=Decision and unrelated=Assumption are fixed measurement classifications.",
        ],
    }
    before_cpu = report["machine"]["before"].get("cpu_percent")
    if args.official and environment.get("official_baseline_matches") is not True:
        _atomic_json(out, report)
        shutil.rmtree(scratch)
        raise MeasurementRefused(
            "official environment differs from Python 3.13.1 / numpy 2.5.1 / ladybug 0.16.0 "
            "with google-crc32c available"
        )
    if args.official and (
        before_cpu is None or float(before_cpu) > args.maximum_initial_cpu_percent
    ):
        _atomic_json(out, report)
        shutil.rmtree(scratch)
        raise MeasurementRefused(
            "initial machine-idle sample is unavailable or above "
            f"{args.maximum_initial_cpu_percent}% (observed {before_cpu!r})"
        )
    if args.check_only:
        source_after = content_digest(source)
        report["source_unchanged_after_run"] = source_after == source_before
        report["provenance"]["source_workspace"]["after"] = source_after
        report["machine"]["after"] = _machine_state()
        identity_end = _capture_identity(
            grafx_repo=grafx_repo,
            harness_repo=harness_repo,
            core_repo=core_repo,
            expected_grafx_head=args.grafx_sha,
            expected_core_head=args.core_sha,
            expected_tool_commit=args.tool_commit,
            expected_tool_blob=args.tool_blob,
        )
        report["provenance"]["identity"]["end"] = identity_end
        report["provenance"]["identity"]["stable"] = (
            identity_start["fingerprint_sha256"] == identity_end["fingerprint_sha256"]
        )
        report["official_shortfalls"] = ["check_only_has_no_measurements"]
        _atomic_json(out, report)
        shutil.rmtree(scratch)
        return report
    base_root = scratch / "m7profile-grafx-base"
    try:
        base_workspace = asyncio.run(
            harness._build_base(runner, backends, "grafx", fixtures)
        )
        base_digest = content_digest(base_workspace)
        report["provenance"]["fixture_base"] = {
            "workspace": str(base_workspace),
            **base_digest,
            "derived_from_source_sha256": source_before["sha256"],
        }
        selected = (
            list(SCENARIOS)
            if args.scenario == "all"
            else [scenario for scenario in SCENARIOS if scenario.identifier == args.scenario]
        )
        if not selected:
            raise MeasurementRefused(f"unknown scenario selector {args.scenario!r}")
        for pass_name in ("raw", "instrumented"):
            for scenario in selected:
                result = _run_scenario(config, base_workspace, base_digest, pass_name, scenario)
                report["results"].append(result)
                _atomic_json(out, report)
                if args.fail_fast and result["status"] != "passed":
                    raise MeasurementRefused(
                        f"{pass_name}/{scenario.identifier} failed: {result['shortfalls']}"
                    )
    finally:
        if base_root.exists():
            if base_root.parent != scratch or base_root.name != "m7profile-grafx-base":
                raise MeasurementRefused(f"refusing to remove unexpected fixture root {base_root}")
            shutil.rmtree(base_root)
    source_after = content_digest(source)
    report["source_unchanged_after_run"] = source_after == source_before
    report["provenance"]["source_workspace"]["after"] = source_after
    report["machine"]["after"] = _machine_state()
    identity_end = _capture_identity(
        grafx_repo=grafx_repo,
        harness_repo=harness_repo,
        core_repo=core_repo,
        expected_grafx_head=args.grafx_sha,
        expected_core_head=args.core_sha,
        expected_tool_commit=args.tool_commit,
        expected_tool_blob=args.tool_blob,
    )
    report["provenance"]["identity"]["end"] = identity_end
    report["provenance"]["identity"]["stable"] = (
        identity_start["fingerprint_sha256"] == identity_end["fingerprint_sha256"]
    )
    report["official_shortfalls"] = official_shortfalls(report)
    report["official"] = bool(args.official) and not report["official_shortfalls"]
    _atomic_json(out, report)
    shutil.rmtree(scratch)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-workspace", type=Path, required=True)
    parser.add_argument("--source-workspace-sha256")
    parser.add_argument("--source-provenance", type=Path, required=True)
    parser.add_argument("--source-provenance-sha256")
    parser.add_argument("--harness-repo", type=Path, required=True)
    parser.add_argument("--grafx", type=Path, required=True)
    parser.add_argument("--grafx-sha")
    parser.add_argument("--tool-commit")
    parser.add_argument("--tool-blob")
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--core-sha")
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--official", action="store_true")
    parser.add_argument("--machine-idle-asserted", action="store_true")
    parser.add_argument("--maximum-initial-cpu-percent", type=float, default=20.0)
    parser.add_argument("--per-family", type=int, default=OFFICIAL_PER_FAMILY)
    parser.add_argument(
        "--scenario",
        choices=("all", *(scenario.identifier for scenario in SCENARIOS)),
        default="all",
    )
    parser.add_argument("--barrier-timeout-seconds", type=float, default=300.0)
    parser.add_argument(
        "--child-timeout-seconds", type=float, default=DEFAULT_CHILD_TIMEOUT_SECONDS
    )
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--fail-fast", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.per_family <= 0:
        parser.error("--per-family must be > 0")
    if args.maximum_initial_cpu_percent < 0 or args.maximum_initial_cpu_percent > 100:
        parser.error("--maximum-initial-cpu-percent must be between 0 and 100")
    if args.barrier_timeout_seconds <= 0 or args.child_timeout_seconds <= 0:
        parser.error("timeouts must be > 0")
    if args.official:
        missing = [
            name
            for name, value in (
                ("--source-workspace-sha256", args.source_workspace_sha256),
                ("--source-provenance-sha256", args.source_provenance_sha256),
                ("--grafx-sha", args.grafx_sha),
                ("--tool-commit", args.tool_commit),
                ("--tool-blob", args.tool_blob),
                ("--core-sha", args.core_sha),
            )
            if not value
        ]
        if missing:
            parser.error("official runs require " + ", ".join(missing))
        if not args.machine_idle_asserted:
            parser.error("official runs require --machine-idle-asserted")
        if args.check_only:
            parser.error("--check-only cannot be official")
    try:
        report = run(args)
    except MeasurementRefused as failure:
        parser.exit(2, f"CE-3 measurement refused: {failure}\n")
    print(
        json.dumps(
            {
                "out": str(args.out.resolve()),
                "official": report["official"],
                "shortfalls": report.get("official_shortfalls", []),
                "results": len(report.get("results", [])),
            },
            sort_keys=True,
        )
    )
    return 0 if not args.official or report["official"] else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
