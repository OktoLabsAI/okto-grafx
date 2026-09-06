"""Profile exactly one Pulse card on a fresh authenticated full-home clone.

This P0.3 runner never accepts a PID.  It creates one disposable clone, starts
``replay_pulse_card.py`` as its direct child, proves that exact child identity, and attaches the
pinned ``py-spy record`` executable only to the PID returned by that ``Popen`` call.  The replay
publishes a nonce-bound READY marker immediately before its measured operation and blocks on
stdin.  The runner releases GO only after py-spy 0.4.2 reports that sampling attached.

The process topology preserves the replay's direct-parent guard::

    profile_pulse_card.py
      +-- replay_pulse_card.py       (profile target; disposable clone only)
      +-- py-spy record --pid <PID>  (sibling; PID is never operator supplied)

Wrapping the replay as ``py-spy record -- python replay_pulse_card.py`` is deliberately not
supported: py-spy would become the replay's parent and invalidate the parent marker.  The fixed
record command has no ``--locals`` option, does not request full filenames/native frames, and
writes a speedscope artifact outside every data home.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, IO, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.perf_round.baseline_runs import (  # noqa: E402
    RunRefused,
    _clone_copy,
    _source_status,
    _terminate_process_tree,
    child_environment,
    validate_child_provenance,
)
from tools.perf_round.receipt import (  # noqa: E402
    COPY_MANIFEST_NAME,
    DATA_HOME_ENV,
    LiveBoardRefused,
    build_receipt,
    git_sha_of,
    grafx_identity,
    guard_not_data_home,
    inventory,
    machine_sample,
    plain_input,
    sha256_file,
    sha256_text,
    write_receipt,
)
from tools.perf_round.replay_pulse_card import (  # noqa: E402
    PROFILE_GATE_PROTOCOL,
    PROFILE_GATE_PROTOCOL_ENV,
    PROFILE_GATE_READY_PATH_ENV,
    PROFILE_GATE_TOKEN_ENV,
    SUPPORTED_PULSE_BUFFER_BUDGET_BYTES,
)

SCHEMA = "okto-grafx.perf-round-0.0.2.pulse-card-profile.v1"
PY_SPY_VERSION = "0.4.2"
PY_SPY_RATE_HZ = 100
PY_SPY_FORMAT = "speedscope"
PROFILE_NAME = "pulse_card.speedscope.json"
CHILD_OUTPUT_NAME = "pulse_card.json"
READY_NAME = "pulse_card.ready"
RECEIPT_NAME = "profile_receipt.json"
RUN_COPY_NAME = "pulse_card_run_copy"
PROFILER_EXIT_GRACE_SECONDS = 15.0
SOURCE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_IMPORT_ROOT = SOURCE_ROOT / "src"
REPLAY_SCRIPT = SOURCE_ROOT / "tools" / "perf_round" / "replay_pulse_card.py"
_MANIFEST_FILES = (COPY_MANIFEST_NAME, COPY_MANIFEST_NAME + ".sha256")
_PROFILE_SUMMARY = re.compile(r"Samples: ([0-9]+) Errors: ([0-9]+)")


class ProfileRefused(RuntimeError):
    """The requested profile cannot satisfy the fail-closed evidence contract."""


@dataclass(frozen=True)
class RuntimePins:
    grafx_file: Path
    pulse_root: Path
    pulse_file: Path
    pulse_core_root: Path
    pulse_core_file: Path


@dataclass(frozen=True)
class ProfilerBinary:
    command: tuple[str, ...]
    path: Path
    sha256: str
    version: str
    help_sha256: str


@dataclass(frozen=True)
class ProcessRun:
    target_pid: int
    profiler_pid: int
    attach_wait_seconds: float
    target_wall_seconds: float
    target_exit_code: int
    profiler_exit_code: int
    target_identity: Mapping[str, Any]
    os_tree: Mapping[str, Any]
    profiler_lines: tuple[str, ...]


@dataclass(frozen=True)
class ProcessCounters:
    cpu_by_process: Mapping[tuple[int, float], float]
    io_by_process: Mapping[tuple[int, float], tuple[int, int]]


def _overlaps(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left in right.parents or right in left.parents


def _canonical_uuid(value: str, *, field: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as failure:
        raise ProfileRefused(f"--{field.replace('_', '-')} must be a UUID") from failure
    if str(parsed) != value.lower():
        raise ProfileRefused(
            f"--{field.replace('_', '-')} must use canonical lowercase UUID form"
        )
    return str(parsed)


def _validate_source_pins(args: argparse.Namespace) -> RuntimePins:
    measured = grafx_identity()
    measured_file = measured.get("file")
    if type(measured_file) is not str:
        raise ProfileRefused("could not resolve okto_grafx from this checkout")
    grafx_file = Path(measured_file).resolve()
    if not grafx_file.is_relative_to(SOURCE_IMPORT_ROOT):
        raise ProfileRefused("okto_grafx resolved outside the pinned checkout")
    if measured.get("git_sha") != args.grafx_sha:
        raise ProfileRefused("--grafx-sha does not match the pinned checkout")
    if _source_status(SOURCE_ROOT, "src/okto_grafx"):
        raise ProfileRefused("the pinned Grafx package source is not clean")

    pulse_root = guard_not_data_home(args.pulse_root)
    pulse_file = (
        pulse_root / "src" / "okto_pulse" / "community" / "__init__.py"
    ).resolve()
    if not pulse_file.is_file():
        raise ProfileRefused("--pulse-root has no Community package")
    if git_sha_of(pulse_root) != args.pulse_sha:
        raise ProfileRefused("--pulse-sha does not match --pulse-root")
    if _source_status(pulse_root, "src/okto_pulse/community"):
        raise ProfileRefused("the pinned Pulse Community package source is not clean")

    pulse_core_root = guard_not_data_home(args.pulse_core_root)
    pulse_core_file = (
        pulse_core_root / "src" / "okto_pulse" / "core" / "__init__.py"
    ).resolve()
    if not pulse_core_file.is_file():
        raise ProfileRefused("--pulse-core-root has no Core package")
    if git_sha_of(pulse_core_root) != args.pulse_core_sha:
        raise ProfileRefused("--pulse-core-sha does not match --pulse-core-root")
    if _source_status(pulse_core_root, "src/okto_pulse/core"):
        raise ProfileRefused("the pinned Pulse Core package source is not clean")
    return RuntimePins(
        grafx_file=grafx_file,
        pulse_root=pulse_root,
        pulse_file=pulse_file,
        pulse_core_root=pulse_core_root,
        pulse_core_file=pulse_core_file,
    )


def _resolve_profiler() -> ProfilerBinary:
    located = shutil.which("py-spy")
    if not located:
        raise ProfileRefused("py-spy is not installed on PATH")
    path = Path(located).resolve()
    if not path.is_file():
        raise ProfileRefused("the resolved py-spy executable is not a file")
    try:
        version_run = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        help_run = subprocess.run(
            [str(path), "record", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as failure:
        raise ProfileRefused("py-spy preflight failed") from failure
    expected_version = f"py-spy {PY_SPY_VERSION}"
    if version_run.returncode != 0 or version_run.stdout.strip() != expected_version:
        raise ProfileRefused(f"the profiler must be exactly {expected_version}")
    help_text = help_run.stdout + help_run.stderr
    if help_run.returncode != 0:
        raise ProfileRefused("py-spy record --help failed")
    for required in ("--pid", "--output", "--format", "--rate", "--threads"):
        if required not in help_text:
            raise ProfileRefused(
                f"py-spy record does not expose required option {required}"
            )
    if "--locals" in help_text:
        raise ProfileRefused("the pinned record command unexpectedly exposes --locals")
    return ProfilerBinary(
        command=(str(path),),
        path=path,
        sha256=sha256_file(path),
        version=PY_SPY_VERSION,
        help_sha256=sha256_text(help_text),
    )


_INTERPRETER_PROBE = (
    "import os, sys; print(os.getpid(), os.getppid(), sys.executable, sep=chr(10))"
)


def _require_direct_interpreter(
    argv_prefix: Sequence[str] = (sys.executable,),
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Refuse a launcher between Popen and the interpreter before any PID is trusted.

    A uv trampoline, the ``py`` launcher or a Store alias starts the real Python as a
    grandchild: the PID returned by ``Popen`` is the launcher's, so the identity proof
    would pass on the wrong process and py-spy would attach to a non-Python process. The
    probe spawns the interpreter exactly as the replay will be spawned and requires the
    Python that runs to be that very child.
    """
    argv = [*argv_prefix, "-c", _INTERPRETER_PROBE]
    try:
        probe = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            cwd=SOURCE_ROOT,
        )
    except (OSError, subprocess.SubprocessError) as failure:
        raise ProfileRefused("the interpreter preflight could not run") from failure
    lines = probe.stdout.splitlines()
    if probe.returncode != 0 or len(lines) < 3:
        raise ProfileRefused("the interpreter preflight did not report its identity")
    try:
        child_pid = int(lines[0])
        child_ppid = int(lines[1])
    except ValueError as failure:
        raise ProfileRefused(
            "the interpreter preflight reported a malformed identity"
        ) from failure
    reported_executable = lines[2]
    # subprocess.run does not expose the Popen pid it used; the parent relation is the
    # decisive proof: the Python that ran must be OUR direct child.
    if child_ppid != os.getpid():
        raise ProfileRefused(
            "interpreter launcher detected: "
            f"{argv_prefix[0]} starts the Python process as a grandchild (pid {child_pid}, "
            f"parent {child_ppid}, runner {os.getpid()}); a uv trampoline, the py launcher or a "
            "Store alias breaks the PID/READY/GO proof and the py-spy attach. Run the profiler with "
            "a direct interpreter (the base python.exe of the environment)."
        )
    return {
        "direct": True,
        "argv_prefix": [str(item) for item in argv_prefix],
        "reported_executable": reported_executable,
        "child_pid_parent_is_runner": True,
    }


def _profile_argv(
    profiler_command: Sequence[str], *, target_pid: int, output: Path
) -> list[str]:
    if not profiler_command or target_pid <= 0:
        raise ProfileRefused("the internal profiler command or target PID is invalid")
    argv = [
        *profiler_command,
        "record",
        "--pid",
        str(target_pid),
        "--format",
        PY_SPY_FORMAT,
        "--output",
        str(output),
        "--rate",
        str(PY_SPY_RATE_HZ),
        "--threads",
    ]
    for forbidden in ("--locals", "--full-filenames", "--native", "--subprocesses"):
        if forbidden in argv:
            raise ProfileRefused(f"the profiler command may not use {forbidden}")
    return argv


def _creation_flags() -> int:
    return subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0


def _spawn_target(
    argv: Sequence[str], environment: Mapping[str, str]
) -> subprocess.Popen:
    return subprocess.Popen(
        list(argv),
        cwd=SOURCE_ROOT,
        env=dict(environment),
        stdin=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=os.name == "posix",
        creationflags=_creation_flags(),
    )


def _wait_for_ready(
    target: subprocess.Popen,
    *,
    ready_path: Path,
    token: str,
    timeout_seconds: float,
) -> float:
    expected = f"ready-v1:{token}\n".encode("ascii")
    started = time.monotonic()
    deadline = started + timeout_seconds
    while time.monotonic() < deadline:
        if target.poll() is not None:
            raise ProfileRefused("the replay exited before publishing READY")
        if ready_path.exists():
            try:
                info = os.lstat(ready_path)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_nlink", 1) != 1
                    or getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                    or ready_path.resolve() != ready_path
                ):
                    raise ProfileRefused(
                        "the READY marker is not an independent regular file"
                    )
                observed = ready_path.read_bytes()
            except OSError as failure:
                raise ProfileRefused(
                    "the READY marker could not be authenticated"
                ) from failure
            if observed == expected:
                return time.monotonic() - started
            if len(observed) >= len(expected):
                raise ProfileRefused("the READY marker does not match this run")
        time.sleep(0.02)
    raise ProfileRefused("timed out waiting for the replay READY marker")


def _prove_target_identity(
    target: subprocess.Popen, *, spawn_wall_time: float
) -> tuple[Any, dict[str, Any]]:
    try:
        import psutil  # type: ignore[import-not-found]

        process = psutil.Process(target.pid)
        parent_matches = process.ppid() == os.getpid()
        executable = Path(process.exe()).resolve()
        executable_matches = executable == Path(sys.executable).resolve()
        created = float(process.create_time())
    except Exception as failure:  # noqa: BLE001 - every unavailable proof refuses attach
        raise ProfileRefused(
            "could not prove the spawned replay process identity"
        ) from failure
    creation_matches = spawn_wall_time - 2.0 <= created <= time.time() + 2.0
    if target.poll() is not None or not (
        parent_matches and executable_matches and creation_matches
    ):
        raise ProfileRefused(
            "the process selected for profiling is not the spawned direct child"
        )
    return process, {
        "pid": target.pid,
        "pid_source": "internal_subprocess_popen",
        "parent_pid_matches_runner": parent_matches,
        "executable": str(executable),
        "executable_matches_receipt_python": executable_matches,
        "create_time": created,
        "creation_time_in_spawn_window": creation_matches,
        "ready_nonce_matched": True,
    }


def _spawn_profiler(
    argv: Sequence[str], environment: Mapping[str, str]
) -> subprocess.Popen:
    return subprocess.Popen(
        list(argv),
        cwd=SOURCE_ROOT,
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=os.name == "posix",
        creationflags=_creation_flags(),
    )


def _start_profiler_drain(
    stream: IO[str], messages: queue.Queue[str | None], lines: list[str]
) -> threading.Thread:
    def drain() -> None:
        try:
            for raw in stream:
                line = raw.rstrip("\r\n")
                lines.append(line)
                messages.put(line)
        finally:
            messages.put(None)

    thread = threading.Thread(target=drain, name="py-spy-output", daemon=True)
    thread.start()
    return thread


def _await_profiler_attach(
    profiler: subprocess.Popen,
    messages: queue.Queue[str | None],
    *,
    timeout_seconds: float,
) -> float:
    expected = (
        f"py-spy> Sampling process {PY_SPY_RATE_HZ} times a second. "
        "Press Control-C to exit."
    )
    started = time.monotonic()
    deadline = started + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProfileRefused("timed out waiting for py-spy to confirm attach")
        try:
            line = messages.get(timeout=remaining)
        except queue.Empty as failure:
            raise ProfileRefused(
                "timed out waiting for py-spy to confirm attach"
            ) from failure
        if line is None:
            raise ProfileRefused("py-spy exited before confirming attach")
        if not line:
            continue
        if line != expected:
            raise ProfileRefused("py-spy emitted an unexpected pre-attach diagnostic")
        if profiler.poll() is not None:
            raise ProfileRefused("py-spy exited while confirming attach")
        return time.monotonic() - started


def _release_target(target: subprocess.Popen, *, token: str) -> None:
    if target.stdin is None or target.poll() is not None:
        raise ProfileRefused("the replay cannot receive the profiler GO marker")
    try:
        target.stdin.write(f"go-v1:{token}\n")
        target.stdin.flush()
        target.stdin.close()
    except (BrokenPipeError, OSError, UnicodeError) as failure:
        raise ProfileRefused(
            "the profiler GO marker could not be delivered"
        ) from failure


def _sample_tree(root: Any, aggregate: dict[str, Any]) -> None:
    import psutil  # type: ignore[import-not-found]

    try:
        members = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    rss = 0
    private = 0
    private_complete = True
    sampled = 0
    for member in members:
        try:
            identity = (int(member.pid), float(member.create_time()))
            memory = member.memory_info()
            cpu = member.cpu_times()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            private_complete = False
            continue
        sampled += 1
        rss += int(getattr(memory, "rss", 0))
        private_value = getattr(memory, "private", None)
        if private_value is None:
            try:
                private_value = getattr(member.memory_full_info(), "uss", None)
            except (psutil.NoSuchProcess, psutil.AccessDenied, NotImplementedError):
                private_value = None
        if private_value is None:
            private_complete = False
        else:
            private += int(private_value)
        aggregate["cpu_by_process"][identity] = max(
            aggregate["cpu_by_process"].get(identity, 0.0),
            float(cpu.user) + float(cpu.system),
        )
        try:
            io = member.io_counters()
        except (psutil.NoSuchProcess, psutil.AccessDenied, NotImplementedError):
            io = None
        if io is not None:
            previous = aggregate["io_by_process"].get(identity, (0, 0))
            aggregate["io_by_process"][identity] = (
                max(previous[0], int(getattr(io, "read_bytes", 0))),
                max(previous[1], int(getattr(io, "write_bytes", 0))),
            )
    if sampled:
        aggregate["samples"] += 1
        aggregate["process_count_peak"] = max(aggregate["process_count_peak"], sampled)
        aggregate["peak_rss_bytes"] = max(aggregate["peak_rss_bytes"], rss)
        if private_complete:
            aggregate["private_samples"] += 1
            aggregate["peak_private_bytes"] = max(
                aggregate["peak_private_bytes"], private
            )


def _counter_snapshot(root: Any) -> ProcessCounters:
    """Capture process-lifetime counters immediately before GO for later subtraction."""
    try:
        import psutil  # type: ignore[import-not-found]

        members = [root, *root.children(recursive=True)]
    except Exception as failure:  # noqa: BLE001 - the pre-GO baseline is mandatory
        raise ProfileRefused("pre-GO process counters were unavailable") from failure
    cpu_by_process: dict[tuple[int, float], float] = {}
    io_by_process: dict[tuple[int, float], tuple[int, int]] = {}
    for member in members:
        try:
            identity = (int(member.pid), float(member.create_time()))
            cpu = member.cpu_times()
            io = member.io_counters()
        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            NotImplementedError,
        ) as failure:
            raise ProfileRefused(
                "pre-GO process counters were unavailable"
            ) from failure
        cpu_by_process[identity] = float(cpu.user) + float(cpu.system)
        io_by_process[identity] = (
            int(getattr(io, "read_bytes", 0)),
            int(getattr(io, "write_bytes", 0)),
        )
    if not cpu_by_process:
        raise ProfileRefused("pre-GO process counters were unavailable")
    return ProcessCounters(cpu_by_process=cpu_by_process, io_by_process=io_by_process)


def _counter_deltas(
    observed_cpu: Mapping[tuple[int, float], float],
    observed_io: Mapping[tuple[int, float], tuple[int, int]],
    baseline: ProcessCounters,
) -> tuple[float, int, int]:
    for identity, value in observed_cpu.items():
        if value < baseline.cpu_by_process.get(identity, 0.0):
            raise ProfileRefused("a sampled CPU counter moved backwards after GO")
    for identity, value in observed_io.items():
        before = baseline.io_by_process.get(identity, (0, 0))
        if value[0] < before[0] or value[1] < before[1]:
            raise ProfileRefused("a sampled I/O counter moved backwards after GO")
    cpu_seconds = sum(
        value - baseline.cpu_by_process.get(identity, 0.0)
        for identity, value in observed_cpu.items()
    )
    read_bytes = sum(
        value[0] - baseline.io_by_process.get(identity, (0, 0))[0]
        for identity, value in observed_io.items()
    )
    write_bytes = sum(
        value[1] - baseline.io_by_process.get(identity, (0, 0))[1]
        for identity, value in observed_io.items()
    )
    return cpu_seconds, read_bytes, write_bytes


def _watch_target(
    target: subprocess.Popen,
    profiler: subprocess.Popen,
    root: Any,
    *,
    counter_baseline: ProcessCounters,
    timeout_seconds: float,
    poll_seconds: float,
) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    deadline = started + timeout_seconds
    aggregate: dict[str, Any] = {
        "samples": 0,
        "private_samples": 0,
        "process_count_peak": 0,
        "peak_rss_bytes": 0,
        "peak_private_bytes": 0,
        "cpu_by_process": {},
        "io_by_process": {},
    }
    timed_out = False
    while target.poll() is None:
        if profiler.poll() is not None and target.poll() is None:
            raise ProfileRefused("py-spy stopped before the replay completed")
        if time.monotonic() >= deadline:
            timed_out = True
            _terminate_process_tree(target, root)
            break
        _sample_tree(root, aggregate)
        time.sleep(poll_seconds)
    wall_seconds = time.monotonic() - started
    cpu_seconds, io_read_bytes, io_write_bytes = _counter_deltas(
        aggregate.pop("cpu_by_process"),
        aggregate.pop("io_by_process"),
        counter_baseline,
    )
    result = {
        **aggregate,
        "peak_rss_bytes_tree": (
            aggregate["peak_rss_bytes"] if aggregate["samples"] else None
        ),
        "peak_private_bytes_tree": (
            aggregate["peak_private_bytes"] if aggregate["private_samples"] else None
        ),
        "cpu_seconds_sampled_delta_from_pre_go": cpu_seconds,
        "io_read_bytes_sampled_delta_from_pre_go": io_read_bytes,
        "io_write_bytes_sampled_delta_from_pre_go": io_write_bytes,
        "counter_delta_semantics": (
            "last_sample_minus_immediate_pre_go_by_pid_and_create_time_lower_bound"
        ),
        "timed_out": timed_out,
        "scope": "spawned_replay_process_tree_excludes_profiler_sibling",
    }
    result.pop("peak_rss_bytes")
    result.pop("peak_private_bytes")
    if result["samples"] == 0 or result["private_samples"] == 0:
        raise ProfileRefused(
            "OS RSS/private counters were unavailable for the replay tree"
        )
    if timed_out:
        raise ProfileRefused("the profiled replay exceeded its finite timeout")
    return result, wall_seconds


def _finish_profiler(
    profiler: subprocess.Popen,
    drain_thread: threading.Thread,
    lines: list[str],
) -> tuple[int, tuple[str, ...]]:
    try:
        exit_code = profiler.wait(timeout=PROFILER_EXIT_GRACE_SECONDS)
    except subprocess.TimeoutExpired as failure:
        try:
            import psutil  # type: ignore[import-not-found]

            root = psutil.Process(profiler.pid)
        except Exception:  # noqa: BLE001 - process may already be disappearing
            root = None
        _terminate_process_tree(profiler, root)
        raise ProfileRefused(
            "py-spy did not finish after the target exited"
        ) from failure
    drain_thread.join(timeout=2.0)
    if drain_thread.is_alive():
        raise ProfileRefused("py-spy diagnostics did not reach EOF")
    return int(exit_code), tuple(lines)


def _ensure_stopped(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        import psutil  # type: ignore[import-not-found]

        root = psutil.Process(process.pid)
    except Exception:  # noqa: BLE001 - cleanup remains best effort without psutil handle
        root = None
    _terminate_process_tree(process, root)


def _cleanup_processes(*processes: subprocess.Popen | None) -> None:
    failure: BaseException | None = None
    for process in processes:
        try:
            _ensure_stopped(process)
        except (
            BaseException
        ) as exc:  # preserve proof failure, but still stop every sibling
            if failure is None:
                failure = exc
    if failure is not None:
        raise failure


def _run_profiled_process(
    *,
    target_argv: Sequence[str],
    target_environment: Mapping[str, str],
    profiler_command: Sequence[str],
    profiler_environment: Mapping[str, str],
    ready_path: Path,
    profile_path: Path,
    token: str,
    ready_timeout_seconds: float,
    attach_timeout_seconds: float,
    timeout_seconds: float,
    poll_seconds: float,
) -> ProcessRun:
    if ready_path.exists() or profile_path.exists():
        raise ProfileRefused("READY/profile output already exists")
    target: subprocess.Popen | None = None
    profiler: subprocess.Popen | None = None
    drain_thread: threading.Thread | None = None
    profiler_lines: list[str] = []
    try:
        spawn_wall = time.time()
        target = _spawn_target(target_argv, target_environment)
        _wait_for_ready(
            target,
            ready_path=ready_path,
            token=token,
            timeout_seconds=ready_timeout_seconds,
        )
        root, identity = _prove_target_identity(target, spawn_wall_time=spawn_wall)
        profile_argv = _profile_argv(
            profiler_command, target_pid=target.pid, output=profile_path
        )
        profiler = _spawn_profiler(profile_argv, profiler_environment)
        if profiler.stdout is None:
            raise ProfileRefused("py-spy diagnostics pipe was not created")
        messages: queue.Queue[str | None] = queue.Queue()
        drain_thread = _start_profiler_drain(profiler.stdout, messages, profiler_lines)
        attach_wait = _await_profiler_attach(
            profiler, messages, timeout_seconds=attach_timeout_seconds
        )
        confirmed_root, confirmed_identity = _prove_target_identity(
            target, spawn_wall_time=spawn_wall
        )
        if confirmed_identity["create_time"] != identity["create_time"]:
            raise ProfileRefused("the profiled target identity changed after attach")
        identity["identity_revalidated_after_attach"] = True
        root = confirmed_root
        counter_baseline = _counter_snapshot(root)
        _release_target(target, token=token)
        os_tree, target_wall = _watch_target(
            target,
            profiler,
            root,
            counter_baseline=counter_baseline,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )
        target_exit = int(target.returncode)
        profiler_exit, lines = _finish_profiler(profiler, drain_thread, profiler_lines)
        if target_exit != 0:
            raise ProfileRefused("the profiled replay did not complete successfully")
        if profiler_exit != 0:
            raise ProfileRefused("py-spy did not complete successfully")
        return ProcessRun(
            target_pid=target.pid,
            profiler_pid=profiler.pid,
            attach_wait_seconds=attach_wait,
            target_wall_seconds=target_wall,
            target_exit_code=target_exit,
            profiler_exit_code=profiler_exit,
            target_identity=identity,
            os_tree=os_tree,
            profiler_lines=lines,
        )
    finally:
        if target is not None and target.stdin is not None and not target.stdin.closed:
            try:
                target.stdin.close()
            except OSError:
                pass
        try:
            _cleanup_processes(target, profiler)
        finally:
            if drain_thread is not None:
                drain_thread.join(timeout=2.0)


def _validate_profile_artifact(
    path: Path, profiler_lines: Sequence[str], *, started_wall_time: float
) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_mtime < started_wall_time - 1.0:
        raise ProfileRefused("py-spy wrote no fresh speedscope artifact")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as failure:
        raise ProfileRefused("the speedscope artifact is not valid JSON") from failure
    if not isinstance(document, dict) or not isinstance(document.get("profiles"), list):
        raise ProfileRefused("the speedscope artifact has no profile list")
    sample_count = 0
    for profile in document["profiles"]:
        if not isinstance(profile, dict) or profile.get("type") != "sampled":
            raise ProfileRefused(
                "the speedscope artifact contains a non-sampled profile"
            )
        samples = profile.get("samples")
        weights = profile.get("weights")
        if not isinstance(samples, list) or not isinstance(weights, list):
            raise ProfileRefused("the speedscope sampled profile is incomplete")
        if len(samples) != len(weights):
            raise ProfileRefused("the speedscope samples and weights do not reconcile")
        sample_count += len(samples)

    def contains_locals(value: Any) -> bool:
        if isinstance(value, dict):
            return "locals" in value or any(
                contains_locals(item) for item in value.values()
            )
        if isinstance(value, list):
            return any(contains_locals(item) for item in value)
        return False

    if contains_locals(document):
        raise ProfileRefused("the speedscope artifact unexpectedly contains locals")
    summaries = [
        (int(samples), int(errors))
        for line in profiler_lines
        for samples, errors in _PROFILE_SUMMARY.findall(line)
    ]
    if len(summaries) != 1:
        raise ProfileRefused("py-spy emitted no unique terminal sample summary")
    reported_samples, reported_errors = summaries[0]
    if (
        reported_errors != 0
        or reported_samples <= 0
        or sample_count != reported_samples
    ):
        raise ProfileRefused("py-spy samples are empty, erroneous or unreconciled")
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "format": PY_SPY_FORMAT,
        "sample_count": sample_count,
        "reported_errors": reported_errors,
        "locals_collected": False,
        "full_filenames_requested": False,
        "native_frames_requested": False,
        "subprocesses_requested": False,
        "nonblocking_requested": False,
        "threads_requested": True,
        "rate_hz": PY_SPY_RATE_HZ,
    }


def _validate_clone_commits(
    manifest: Mapping[str, Any], *, grafx: str, pulse: str, pulse_core: str
) -> None:
    commits = manifest.get("commits")
    expected = {"grafx": grafx, "pulse": pulse, "pulse_core": pulse_core}
    if not isinstance(commits, dict) or commits != expected:
        raise ProfileRefused("the declared copy commit pins do not match this profile")


def _validate_arguments(args: argparse.Namespace) -> None:
    if args.mode not in ("strict", "generation"):
        raise ProfileRefused("--mode must be strict or generation")
    if args.kind not in ("raw", "instrumented"):
        raise ProfileRefused("--kind must be raw or instrumented")
    if args.thermal not in ("warm", "mixed"):
        raise ProfileRefused("--thermal must be warm or mixed")
    if isinstance(args.seed, bool) or not isinstance(args.seed, int):
        raise ProfileRefused("--seed must be an integer")
    if (
        isinstance(args.page_size, bool)
        or not isinstance(args.page_size, int)
        or args.page_size <= 0
    ):
        raise ProfileRefused("--page-size must be a positive integer")
    if args.buffer_budget_bytes != SUPPORTED_PULSE_BUFFER_BUDGET_BYTES:
        raise ProfileRefused(
            "the pinned Pulse adapter supports only a 67108864-byte budget"
        )
    if args.checksum != "auto":
        raise ProfileRefused("the pinned Pulse adapter supports only --checksum auto")
    for name in (
        "timeout_seconds",
        "ready_timeout_seconds",
        "attach_timeout_seconds",
        "poll_seconds",
    ):
        value = float(getattr(args, name))
        if not (0 < value < float("inf")):
            raise ProfileRefused(
                f"--{name.replace('_', '-')} must be finite and positive"
            )
    if not 0.02 <= args.poll_seconds <= 5.0:
        raise ProfileRefused("--poll-seconds must be between 0.02 and 5")
    args.board_id = _canonical_uuid(args.board_id, field="board_id")
    args.card_id = _canonical_uuid(args.card_id, field="card_id")


def profile_once(args: argparse.Namespace) -> dict[str, Any]:
    _validate_arguments(args)
    pins = _validate_source_pins(args)
    profiler = _resolve_profiler()
    interpreter = _require_direct_interpreter()
    declared_copy = guard_not_data_home(args.declared_copy)
    out_dir = guard_not_data_home(args.out_dir)
    protected = (
        declared_copy,
        SOURCE_ROOT,
        pins.pulse_root,
        pins.pulse_core_root,
    )
    if any(_overlaps(out_dir, path) for path in protected):
        raise ProfileRefused(
            "--out-dir must be disjoint from copies and pinned source trees"
        )
    if out_dir.exists():
        raise ProfileRefused("--out-dir already exists")
    if not out_dir.parent.is_dir():
        raise ProfileRefused("--out-dir parent must already exist")
    out_dir.mkdir()

    clone = out_dir / RUN_COPY_NAME
    child_output = out_dir / CHILD_OUTPUT_NAME
    ready_path = out_dir / READY_NAME
    profile_path = out_dir / PROFILE_NAME
    receipt_path = out_dir / RECEIPT_NAME
    for artifact in (clone, child_output, ready_path, profile_path, receipt_path):
        if artifact.exists() or any(
            _overlaps(artifact, home) for home in (declared_copy,)
        ):
            raise ProfileRefused("profile paths are stale or overlap the declared copy")

    clone_manifest = _clone_copy(declared_copy, clone)
    _validate_clone_commits(
        clone_manifest,
        grafx=args.grafx_sha,
        pulse=args.pulse_sha,
        pulse_core=args.pulse_core_sha,
    )
    token = secrets.token_hex(32)
    target_environment = child_environment(clone, pins.pulse_root, pins.pulse_core_root)
    target_environment.update(
        {
            PROFILE_GATE_PROTOCOL_ENV: PROFILE_GATE_PROTOCOL,
            PROFILE_GATE_READY_PATH_ENV: str(ready_path),
            PROFILE_GATE_TOKEN_ENV: token,
        }
    )
    profiler_environment = {
        key: value
        for key, value in target_environment.items()
        if key
        not in (
            PROFILE_GATE_PROTOCOL_ENV,
            PROFILE_GATE_READY_PATH_ENV,
            PROFILE_GATE_TOKEN_ENV,
        )
    }
    child_argv = [
        str(Path(sys.executable).resolve()),
        str(REPLAY_SCRIPT.resolve()),
        "--copy",
        str(clone),
        "--out",
        str(child_output),
        "--board-id",
        args.board_id,
        "--card-id",
        args.card_id,
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
        args.checksum,
        "--kind",
        args.kind,
        "--thermal",
        args.thermal,
    ]
    machine_before = machine_sample(interval_seconds=1.0)
    profile_started_wall = time.time()
    process_run = _run_profiled_process(
        target_argv=child_argv,
        target_environment=target_environment,
        profiler_command=profiler.command,
        profiler_environment=profiler_environment,
        ready_path=ready_path,
        profile_path=profile_path,
        token=token,
        ready_timeout_seconds=args.ready_timeout_seconds,
        attach_timeout_seconds=args.attach_timeout_seconds,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    profile_artifact = _validate_profile_artifact(
        profile_path,
        process_run.profiler_lines,
        started_wall_time=profile_started_wall,
    )
    if (
        not child_output.is_file()
        or child_output.stat().st_mtime < profile_started_wall - 1.0
    ):
        raise ProfileRefused("the replay wrote no fresh JSON output")
    try:
        child_document = json.loads(child_output.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as failure:
        raise ProfileRefused("the replay output is not valid JSON") from failure
    expected_child = {
        "python_executable": str(Path(sys.executable).resolve()),
        "okto_grafx_file": str(pins.grafx_file),
        "okto_pulse_community_file": str(pins.pulse_file),
        "okto_pulse_core_file": str(pins.pulse_core_file),
        "mode": args.mode,
        "seed": args.seed,
        "kind": args.kind,
        "page_size": args.page_size,
        "buffer_budget_bytes": args.buffer_budget_bytes,
        "checksum": args.checksum,
        "thermal": args.thermal,
        "run": 0,
        "effective_data_dir": str(clone.resolve()),
    }
    provenance_errors = validate_child_provenance(child_document, expected_child)
    if provenance_errors:
        raise ProfileRefused("the replay runtime/config provenance does not match")
    if child_document.get("profile_barrier") != {
        "enabled": True,
        "protocol": PROFILE_GATE_PROTOCOL,
        "released": True,
    }:
        raise ProfileRefused("the replay did not attest the released profiler barrier")
    if child_document.get("serve_lock_artifacts_absent") is not True:
        raise ProfileRefused("the replay did not attest complete serve-lock release")
    if token in child_output.read_text(encoding="utf-8"):
        raise ProfileRefused("the replay output leaked its profiler barrier nonce")

    after = inventory(clone, exclude=_MANIFEST_FILES)
    ready_hash = sha256_file(ready_path)
    inputs = [
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
            "pulse_card_replay",
            REPLAY_SCRIPT,
            inventory_digest=sha256_file(REPLAY_SCRIPT),
        ),
        plain_input(
            "py_spy_executable",
            profiler.path,
            inventory_digest=profiler.sha256,
        ),
    ]
    results = {
        "schema": SCHEMA,
        "complete": True,
        "interpreter": interpreter,
        "target": process_run.target_identity,
        "processes": {
            "target_pid": process_run.target_pid,
            "profiler_pid": process_run.profiler_pid,
            "topology": "runner_parent_with_replay_and_profiler_as_siblings",
            "target_exit_code": process_run.target_exit_code,
            "profiler_exit_code": process_run.profiler_exit_code,
            "attach_wait_seconds": process_run.attach_wait_seconds,
            "target_wall_seconds_from_go": process_run.target_wall_seconds,
        },
        "barrier": {
            "protocol": PROFILE_GATE_PROTOCOL,
            "ready_path": str(ready_path.resolve()),
            "ready_sha256": ready_hash,
            "ready_before_attach": True,
            "attach_confirmed_before_go": True,
            "nonce_in_receipt": False,
            "nonce_scope": "ready_file_and_parent_stdin_only",
        },
        "profiler": {
            "path": str(profiler.path),
            "sha256": profiler.sha256,
            "version": profiler.version,
            "record_help_sha256": profiler.help_sha256,
            "pid_parameter_source": "internal_replay_popen_pid",
            "argv": _profile_argv(
                profiler.command,
                target_pid=process_run.target_pid,
                output=profile_path,
            ),
            **profile_artifact,
            "scope": (
                "attached_before_process_batch_through_replay_exit_includes_"
                "postvalidation_and_close"
            ),
        },
        "replay": {
            "path": str(child_output.resolve()),
            "sha256": sha256_file(child_output),
            "schema": child_document.get("schema"),
            "summary": child_document.get("summary"),
            "profile_barrier": child_document.get("profile_barrier"),
            "child_provenance_matched": True,
        },
        "copy": {
            "declared_copy": str(declared_copy),
            "run_copy": str(clone.resolve()),
            "sha256_before": clone_manifest["copy"]["sha256"],
            "sha256_after": after["sha256"],
            "mutated": after["sha256"] != clone_manifest["copy"]["sha256"],
        },
        "os_tree": dict(process_run.os_tree),
    }
    receipt = build_receipt(
        tool=__file__,
        seed=args.seed,
        parameters={
            "board_id": args.board_id,
            "card_id": args.card_id,
            "one_shot": True,
            "operator_pid_argument_supported": False,
            "ready_timeout_seconds": args.ready_timeout_seconds,
            "attach_timeout_seconds": args.attach_timeout_seconds,
            "timeout_seconds": args.timeout_seconds,
            "poll_seconds": args.poll_seconds,
            "child_data_home_variables": list(DATA_HOME_ENV),
        },
        series={"mode": args.mode, "thermal": args.thermal, "kind": args.kind},
        config={
            "descriptor_revalidation": args.mode,
            "page_size": args.page_size,
            "buffer_budget_bytes": args.buffer_budget_bytes,
            "checksum": args.checksum,
        },
        inputs=inputs,
        results=results,
        grafx_sha=args.grafx_sha,
        pulse_sha=args.pulse_sha,
        pulse_core_sha=args.pulse_core_sha,
        machine_idle_asserted=args.machine_idle_asserted,
        machine=machine_before,
        notes=[
            "the profiler PID target comes only from the replay Popen handle; the CLI exposes no PID",
            "READY is published before attach and GO is delivered only after py-spy confirms sampling",
            "py-spy is an external sibling, so the replay retains the runner as its direct parent",
            "the parent marker and nonce prevent accidental misuse, not a malicious local operator",
            "the profile is attribution evidence, never an unprofiled wall-time baseline",
            "the speedscope capture includes post-validation and runtime close after the timed card operation",
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
    parser.add_argument("--card-id", required=True)
    parser.add_argument("--mode", choices=("strict", "generation"), required=True)
    parser.add_argument("--kind", choices=("raw", "instrumented"), default="raw")
    parser.add_argument("--thermal", choices=("warm", "mixed"), required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--page-size", type=int, default=8192)
    parser.add_argument(
        "--buffer-budget-bytes",
        type=int,
        default=SUPPORTED_PULSE_BUFFER_BUDGET_BYTES,
    )
    parser.add_argument("--checksum", default="auto")
    parser.add_argument("--timeout-seconds", type=float, default=7200.0)
    parser.add_argument("--ready-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--attach-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--machine-idle-asserted", action="store_true")
    parser.add_argument("--grafx-sha", required=True)
    parser.add_argument("--pulse-root", type=Path, required=True)
    parser.add_argument("--pulse-sha", required=True)
    parser.add_argument("--pulse-core-root", type=Path, required=True)
    parser.add_argument("--pulse-core-sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        profile_once(args)
    except (LiveBoardRefused, ProfileRefused, RunRefused) as refusal:
        print(f"REFUSED [{type(refusal).__name__}]: {refusal}", file=sys.stderr)
        return 2
    except Exception as failure:
        print(
            f"FAILED [{type(failure).__name__}]: pulse_card_profile_failed",
            file=sys.stderr,
        )
        return 1
    print(f"profile receipt: {(args.out_dir / RECEIPT_NAME).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
