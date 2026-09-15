"""Run an existing instrument N >= 3 times per mode as independent processes, and publish the dispersion.

This is the P0.4 runner. It measures nothing itself and invents no gate: it wraps whatever
instrument the round already trusts (``tools/measure_m7_ce3.py``, the Pulse pf5 profiler,
``python -m bench.harness``), runs it once as a discarded warmup and then ``--runs`` times, each
in a fresh process with a finite timeout, extracts the numbers the caller names from each run's
JSON output, and reports them as a distribution -- minimum, median, maximum, relative spread --
never as one number.

Independence of the runs is enforced, not assumed:

* the child's argv is a list of tokens (``--arg`` repeated); a token that is exactly ``{out}``,
  ``{run}``, ``{mode}``, ``{seed}`` or ``{copy}`` is replaced whole, nothing else is
  interpolated, so quoting and braces inside JSON arguments survive on Windows and POSIX;
* every run gets its OWN data home: the child's environment has every live-data-home variable
  (``DATA_DIR``, ``OKTO_PULSE_HOME``, ``KG_BASE_DIR`` and the ``OKTO_PULSE_``-prefixed forms)
  SET to an isolated directory. A Pulse replay uses the per-run clone itself as ``DATA_DIR``;
  a Grafx-only run defaults to ``<out-dir>/<mode>_runNN_home``. Variables are set, not merely
  removed, because a Pulse child with no ``DATA_DIR`` falls back to ``~/.okto-pulse``;
* when a declared copy is given, the argv MUST reference it through ``{copy}``; the copy is
  never reused mutable: ``--copy-policy clone`` (default) gives every run its own byte-identical
  clone with its own manifest and records the clone's inventory after the run (mutation of a
  disposable clone is allowed, but it is provenance), ``verify`` re-hashes the shared copy after
  each run and fails the run if it mutated it;
* the instrument that runs is bound to the argv: a script path in the command or an inline
  ``-c`` payload is hashed into the receipt; ``--instrument-path`` may add supplementary files
  but cannot substitute for that binding, and ``python -m package.module`` alone is refused;
* argv starts with the same Python whose environment is recorded, its source path is pinned to
  this checkout, Pulse Community and Core are pinned separately when used, and the raw JSON must
  attest the effective runtime/config and data home in ``_perf_round``;
* an output file that already exists before its run is refused, and an output older than the
  run's start is refused as stale.

Labels are explicit: every run is a fresh process; whether the filesystem cache was cold or
warm and whether the command is RAW or instrumented are operator statements (``--thermal`` and
``--kind``), recorded, never inferred. The concrete driver is responsible for making those
statements true. The process-tree peak sums every member per sample. ``official`` is always false.

Policy (plan section 6 P0.4 and section 7): at least three measured runs, warmup discarded; a
fourth run is RECOMMENDED, never performed automatically, when any metric's relative spread
exceeds ``--max-spread``.

Usage::

    python tools/perf_round/baseline_runs.py --mode strict --thermal warm --runs 3 --out-dir <dir> \\
        --timeout-seconds 3600 --page-size 8192 --buffer-budget-bytes 67108864 \\
        --checksum auto --grafx-sha SHA --pulse-sha SHA \\
        --arg python --arg tools/measure_m7_ce3.py --arg --out --arg {out} ... \\
        --metric families_sum_ms=summary.sum_of_family_wall_ms_medians \\
        [--declared-copy <copy> --copy-policy clone] [--input PATH ...] [--machine-idle-asserted]
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import signal
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.perf_round.receipt import (  # noqa: E402
    COPY_MANIFEST_NAME,
    COPY_MANIFEST_SCHEMA,
    DATA_HOME_ENV,
    LiveBoardRefused,
    build_receipt,
    canonical_json,
    data_homes,
    declared_copy_input,
    git_sha_of,
    grafx_identity,
    guard_not_data_home,
    inventory,
    machine_sample,
    plain_input,
    require_declared_copy,
    sha256_file,
    sha256_text,
    tool_sha256,
    utc_now,
    write_receipt,
)

MIN_RUNS = 3
PLACEHOLDERS = (
    "{out}",
    "{run}",
    "{mode}",
    "{seed}",
    "{copy}",
    "{page_size}",
    "{buffer_budget_bytes}",
    "{checksum}",
    "{kind}",
    "{thermal}",
)
COPY_POLICIES = ("clone", "verify")
DATA_HOME_POLICIES = ("isolated", "declared-copy-clone")
_MANIFEST_FILES = (COPY_MANIFEST_NAME, COPY_MANIFEST_NAME + ".sha256")
SOURCE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_IMPORT_ROOT = SOURCE_ROOT / "src"


class RunRefused(ValueError):
    """The series cannot start as asked (arguments, policy, timeout, stale outputs)."""


def _source_status(repo: Path, relative_source: str) -> list[str]:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                relative_source,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as failure:
        raise RunRefused(
            f"cannot inspect source state at {repo}: {failure}"
        ) from failure
    if completed.returncode != 0:
        raise RunRefused(
            f"cannot inspect source state at {repo}: {completed.stderr.strip()}"
        )
    return [line for line in completed.stdout.splitlines() if line]


def extract(document: Any, dotted: str) -> float:
    """Follow ``a.b.0.c`` into a JSON document and return the finite number found there."""
    node = document
    for part in dotted.split("."):
        if isinstance(node, list):
            node = node[int(part)]
        elif isinstance(node, dict):
            node = node[part]
        else:
            raise KeyError(dotted)
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        raise TypeError(f"{dotted} is {type(node).__name__}, not a number")
    value = float(node)
    if not math.isfinite(value):
        raise ValueError(f"{dotted} is not finite: {node!r}")
    if value < 0:
        raise ValueError(f"{dotted} is negative: {node!r}")
    return value


def validate_child_provenance(document: Any, expected: dict[str, Any]) -> list[str]:
    """Validate the instrument's reserved child-runtime/config attestation."""
    if not isinstance(document, dict) or not isinstance(
        document.get("_perf_round"), dict
    ):
        return ["output misses the required _perf_round child provenance object"]
    observed = document["_perf_round"]
    errors: list[str] = []
    for field, expected_value in expected.items():
        observed_value = observed.get(field)
        if field in (
            "python_executable",
            "okto_grafx_file",
            "okto_pulse_community_file",
            "okto_pulse_core_file",
            "effective_data_dir",
        ):
            try:
                equal = Path(observed_value).resolve() == Path(expected_value).resolve()
            except (TypeError, OSError, ValueError):
                equal = False
        else:
            equal = (
                type(observed_value) is type(expected_value)
                and observed_value == expected_value
            )
        if not equal:
            errors.append(
                f"child provenance {field}={observed_value!r}, expected {expected_value!r}"
            )
    return errors


def summarize(values: list[float]) -> dict[str, Any]:
    if not values or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
        for value in values
    ):
        raise ValueError(
            "samples must be a non-empty list of finite non-negative numbers"
        )
    ordered = sorted(values)
    median = statistics.median(ordered)
    spread = (
        (ordered[-1] - ordered[0]) / median
        if median
        else (0.0 if ordered[0] == ordered[-1] else None)
    )

    def percentile(fraction: float) -> float:
        # Nearest-rank percentile: rank=ceil(p*N), converted to a zero-based index.
        return ordered[
            max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
        ]

    return {
        "n": len(ordered),
        "values": list(values),
        "min": ordered[0],
        "median": median,
        "max": ordered[-1],
        "mean": statistics.fmean(ordered),
        "stdev": statistics.stdev(ordered) if len(ordered) > 1 else 0.0,
        "p50": percentile(0.50),
        "p90": percentile(0.90),
        "p99": percentile(0.99),
        "relative_spread": spread,
    }


def format_summary(name: str, summary: dict[str, Any]) -> str:
    if "median" not in summary:
        return f"{name}: {summary.get('error')}"
    spread = summary["relative_spread"]
    spread_text = "undefined" if spread is None else f"{spread:.3%}"
    return (
        f"{name}: n={summary['n']} min={summary['min']:.6g} "
        f"median={summary['median']:.6g} max={summary['max']:.6g} spread={spread_text}"
    )


def child_environment(
    run_home: Path,
    pulse_root: Path | None = None,
    pulse_core_root: Path | None = None,
) -> dict[str, str]:
    """The child's environment with EVERY data-home variable set to this run's isolated home."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in DATA_HOME_ENV
    }
    for name in DATA_HOME_ENV:
        environment[name] = str(run_home)
    inherited_pythonpath = environment.get("PYTHONPATH")
    pinned_import_roots = [str(SOURCE_IMPORT_ROOT)]
    if pulse_root is not None:
        pinned_import_roots.append(str(pulse_root / "src"))
    if pulse_core_root is not None:
        pinned_import_roots.append(str(pulse_core_root / "src"))
    pinned_pythonpath = os.pathsep.join(pinned_import_roots)
    environment["PYTHONPATH"] = pinned_pythonpath + (
        os.pathsep + inherited_pythonpath if inherited_pythonpath else ""
    )
    environment["OKTO_GRAFX_EXPECTED_ROOT"] = str(SOURCE_ROOT)
    environment["OKTO_GRAFX_PERF_RUNNER_PID"] = str(os.getpid())
    if pulse_root is not None:
        environment["OKTO_PULSE_EXPECTED_ROOT"] = str(pulse_root.resolve())
    if pulse_core_root is not None:
        environment["OKTO_PULSE_CORE_EXPECTED_ROOT"] = str(pulse_core_root.resolve())
    return environment


def render_argv(template: Sequence[str], **values: str) -> list[str]:
    """Replace whole-token placeholders only; every other token is passed through verbatim."""
    rendered = []
    for token in template:
        if token in PLACEHOLDERS:
            rendered.append(values[token[1:-1]])
        else:
            rendered.append(token)
    return rendered


def _resolved_file_token(index: int, token: str) -> Path | None:
    candidate = Path(token)
    try:
        if candidate.is_file():
            return candidate.resolve()
    except OSError as failure:
        # argv can contain inline Python or other long literals. POSIX stat raises
        # ENAMETOOLONG for those non-path tokens; their payload hash is recorded
        # separately. Other I/O failures must still refuse incomplete provenance.
        if failure.errno != errno.ENAMETOOLONG:
            raise
    if index == 0:
        executable = shutil.which(token)
        if executable:
            return Path(executable).resolve()
    return None


def _guard_argv(template: Sequence[str], *, has_copy: bool) -> None:
    for token in template:
        for home in data_homes():
            token_lower = token.lower()
            overlaps_textually = str(home).lower() in token_lower
            overlaps_tilde_default = token_lower.replace("\\", "/").startswith(
                "~/.okto-pulse"
            )
            try:
                candidate = Path(token).expanduser().resolve()
            except (OSError, ValueError):
                candidate = None
            overlaps_resolved = candidate is not None and (
                candidate == home
                or home in candidate.parents
                or candidate in home.parents
            )
            if overlaps_textually or overlaps_tilde_default or overlaps_resolved:
                raise LiveBoardRefused(
                    f"argv token {token!r} names the live data home {home}"
                )
    if has_copy and "{copy}" not in template:
        raise RunRefused(
            "a --declared-copy was given but the argv never references {copy}; the child would not be bound to it"
        )
    if not has_copy and "{copy}" in template:
        raise RunRefused("argv references {copy} but no --declared-copy was given")
    if "{out}" not in template:
        raise RunRefused(
            "argv must reference {out} so the runner knows where the instrument writes"
        )
    for required in (
        "{run}",
        "{mode}",
        "{seed}",
        "{page_size}",
        "{buffer_budget_bytes}",
        "{checksum}",
        "{kind}",
        "{thermal}",
    ):
        if required not in template:
            raise RunRefused(
                f"argv must reference {required} so the executed workload is bound to its receipt"
            )
    expected_python = Path(sys.executable).resolve()
    if not template or _resolved_file_token(0, template[0]) != expected_python:
        raise RunRefused(
            f"argv[0] must be this receipt runtime {expected_python}; another launcher or Python would invalidate environment provenance"
        )
    if any(token in ("-E", "-I") for token in template[1:]):
        raise RunRefused(
            "argv may not use -E/-I because they discard the pinned PYTHONPATH"
        )
    if len(template) < 2:
        raise RunRefused(
            "argv must execute either a direct .py script or an inline -c instrument"
        )
    if template[1] == "-m":
        raise RunRefused(
            "argv may not use -m: the executed module cannot be bound to an immutable instrument hash"
        )
    if template[1] == "-c":
        if len(template) < 3 or not template[2] or template[2] in PLACEHOLDERS:
            raise RunRefused("argv must include a non-empty inline -c instrument")
    else:
        script = _resolved_file_token(1, template[1])
        if script is None or script.suffix.lower() not in (".py", ".pyw"):
            raise RunRefused(
                "argv must execute a direct .py script at argv[1] or an inline -c instrument"
            )


def _instrument_hashes(
    template: Sequence[str], explicit: Sequence[Path]
) -> list[dict[str, Any]]:
    """Hash the instrument: every explicit path, plus every argv token that is an existing file.

    The interpreter (argv[0]) is hashed too but does not count as naming the instrument: a
    ``python -m package.module`` argv names no file, and the receipt must still say which
    instrument ran, so at least one explicit path or one non-interpreter script is required.
    """
    hashed: list[dict[str, Any]] = []
    seen: set[str] = set()
    argv_files: set[str] = set()

    for index, token in enumerate(template):
        if token in PLACEHOLDERS:
            continue
        resolved_token = _resolved_file_token(index, token)
        if resolved_token is not None:
            argv_files.add(str(resolved_token))
    command_file = _resolved_file_token(0, template[0]) if template else None
    script_file = (
        _resolved_file_token(1, template[1])
        if len(template) > 1 and template[1] != "-c"
        else None
    )
    for path in explicit:
        resolved = guard_not_data_home(path)
        if not resolved.is_file():
            raise RunRefused(f"--instrument-path {path} is not a file")
        if str(resolved) not in seen:
            seen.add(str(resolved))
            hashed.append(
                {
                    "token": str(path),
                    "path": str(resolved),
                    "sha256": sha256_file(resolved),
                    "explicit": True,
                    "interpreter": resolved == Path(sys.executable).resolve(),
                    "bound_to_argv": str(resolved) in argv_files,
                    "command_executable": command_file is not None
                    and resolved == command_file,
                    "bound_instrument": (
                        script_file is not None and resolved == script_file
                    ),
                }
            )
    for index, token in enumerate(template):
        if token in PLACEHOLDERS:
            continue
        candidate = _resolved_file_token(index, token)
        if candidate is not None:
            resolved = guard_not_data_home(candidate)
            if str(resolved) in seen:
                continue
            seen.add(str(resolved))
            hashed.append(
                {
                    "token": token,
                    "path": str(resolved),
                    "sha256": sha256_file(resolved),
                    "explicit": False,
                    "interpreter": resolved == Path(sys.executable).resolve(),
                    "bound_to_argv": True,
                    "command_executable": index == 0,
                    "bound_instrument": (
                        script_file is not None
                        and index == 1
                        and resolved == script_file
                    ),
                }
            )
    if len(template) > 2 and template[1] == "-c":
        inline = template[2]
        hashed.append(
            {
                "token": "-c payload",
                "path": None,
                "sha256": sha256_text(inline),
                "explicit": False,
                "interpreter": False,
                "bound_to_argv": True,
                "command_executable": False,
                "bound_instrument": True,
                "inline": True,
            }
        )
    if not any(item.get("bound_instrument", False) for item in hashed):
        raise RunRefused(
            "no instrument is bound to argv: name a script file in argv (or use an inline -c payload); "
            "--instrument-path alone is supplementary evidence"
        )
    return hashed


def _terminate_process_tree(process: subprocess.Popen, root: Any) -> None:
    """Clean up known descendants; refuse when platform termination proof is unavailable."""
    posix_tree_kill_ok = True
    posix_group_gone = False
    windows_tree_kill_ok = True
    descendants = []
    if os.name == "nt" and root is not None:
        try:
            # taskkill can remove a launcher before its descendants. Retain
            # psutil's PID/birth-bound objects while the root still exists, so
            # the fallback can reach them even after that ancestry is lost.
            descendants.extend(root.children(recursive=True))
        except Exception:  # noqa: BLE001 - process may have exited
            pass
    if os.name == "posix":
        try:
            # _spawn(start_new_session=True) makes the child's PID its PGID.  Using
            # getpgid(pid) races with a fast-exiting leader while descendants remain.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            posix_group_gone = True
        except OSError:
            posix_tree_kill_ok = False
    elif os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            windows_tree_kill_ok = completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            windows_tree_kill_ok = False
    if root is not None:
        try:
            descendants.extend(root.children(recursive=True))
        except Exception:  # noqa: BLE001 - process may have exited
            pass
    for member in reversed(descendants):
        try:
            member.kill()
        except Exception:  # noqa: BLE001 - process may have exited
            pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as failure:
        raise RunRefused(
            f"timed-out process tree {process.pid} did not terminate"
        ) from failure
    if os.name == "posix" and posix_tree_kill_ok and not posix_group_gone:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                posix_group_gone = True
                break
            except OSError:
                posix_tree_kill_ok = False
                break
            if time.monotonic() >= deadline:
                posix_tree_kill_ok = False
                break
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                posix_group_gone = True
                break
            except OSError:
                posix_tree_kill_ok = False
                break
            time.sleep(0.05)
    if not posix_tree_kill_ok or (os.name == "posix" and not posix_group_gone):
        raise RunRefused(
            f"POSIX could not prove termination of timed-out process tree {process.pid}"
        )
    if not windows_tree_kill_ok:
        raise RunRefused(
            f"Windows could not prove termination of timed-out process tree {process.pid}"
        )


def _spawn(
    argv: Sequence[str],
    run_home: Path,
    pulse_root: Path | None = None,
    pulse_core_root: Path | None = None,
) -> subprocess.Popen:
    """Start one isolated process group so a timeout can terminate the whole instrument tree."""
    popen_kwargs: dict[str, Any] = {
        "env": child_environment(run_home, pulse_root, pulse_core_root),
        "start_new_session": os.name == "posix",
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(argv, **popen_kwargs)


def _watch(
    process: subprocess.Popen, poll_seconds: float, timeout_seconds: float
) -> dict[str, Any]:
    """Wait for the child within the timeout, sampling the SUM of RSS/private over its process tree."""
    peak_rss = 0
    peak_private = 0
    samples = 0
    private_samples = 0
    timed_out = False
    deadline = time.monotonic() + timeout_seconds
    try:
        import psutil  # type: ignore[import-not-found]

        root = psutil.Process(process.pid)
    except Exception:  # noqa: BLE001 - psutil missing or the child already gone
        root = None
    while process.poll() is None:
        if time.monotonic() > deadline:
            timed_out = True
            _terminate_process_tree(process, root)
            break
        if root is not None:
            try:
                total_rss = 0
                total_private = 0
                private_complete = True
                for member in [root, *root.children(recursive=True)]:
                    try:
                        info = member.memory_info()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        private_complete = False
                        continue
                    total_rss += int(getattr(info, "rss", 0))
                    private = getattr(info, "private", None)
                    if private is None:
                        try:
                            private = getattr(member.memory_full_info(), "uss", None)
                        except (
                            psutil.NoSuchProcess,
                            psutil.AccessDenied,
                            NotImplementedError,
                        ):
                            private = None
                    if private is None:
                        private_complete = False
                    else:
                        total_private += int(private)
                peak_rss = max(peak_rss, total_rss)
                if private_complete:
                    peak_private = max(peak_private, total_private)
                    private_samples += 1
                samples += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        time.sleep(poll_seconds)
    return {
        "peak_rss_bytes_tree": peak_rss if samples else None,
        "peak_private_bytes_tree": peak_private if private_samples else None,
        "samples": samples,
        "private_samples": private_samples,
        "timed_out": timed_out,
    }


def _clone_copy(source: Path, dest: Path) -> dict[str, Any]:
    """Give one run its own byte-identical declared copy, with its own manifest."""
    manifest = require_declared_copy(source)
    source = Path(source).resolve()
    dest = guard_not_data_home(dest)
    if dest.exists():
        raise RunRefused(f"clone destination {dest} already exists")
    dest.mkdir(parents=True)
    try:
        before = inventory(source, exclude=_MANIFEST_FILES)
        for entry in before["files"]:
            target = dest / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / entry["path"], target, follow_symlinks=False)
        copied = inventory(dest)
        after = inventory(source, exclude=_MANIFEST_FILES)
        identical = (
            copied["sha256"]
            == before["sha256"]
            == after["sha256"]
            == manifest["copy"]["sha256"]
        )
        if not identical:
            raise RunRefused(
                f"clone {dest} is not byte-identical to stable source {source}"
            )
        clone_manifest = {
            "schema": COPY_MANIFEST_SCHEMA,
            "timestamp_utc": utc_now(),
            "tool": {
                "path": str(Path(__file__).resolve()),
                "sha256": tool_sha256(__file__),
            },
            "declared_copy": True,
            "byte_identical": True,
            "source": {
                "path": str(source),
                "inside_data_home": False,
                "sha256_before_copy": before["sha256"],
                "sha256_after_copy": after["sha256"],
                "file_count": before["file_count"],
                "total_bytes": before["total_bytes"],
                "cloned_from_declared_copy": manifest["copy"]["sha256"],
            },
            "copy": {
                "path": str(dest),
                "sha256": copied["sha256"],
                "file_count": copied["file_count"],
                "total_bytes": copied["total_bytes"],
                "files": copied["files"],
            },
            "commits": manifest.get(
                "commits", {"grafx": None, "pulse": None, "pulse_core": None}
            ),
        }
        text = canonical_json(clone_manifest)
        (dest / COPY_MANIFEST_NAME).write_bytes(text.encode("utf-8"))
        (dest / (COPY_MANIFEST_NAME + ".sha256")).write_bytes(
            (sha256_text(text) + "\n").encode("ascii")
        )
        return clone_manifest
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def run_series(
    *,
    mode: str,
    thermal: str,
    runs: int,
    warmup: int,
    argv_template: Sequence[str],
    metrics: dict[str, str],
    out_dir: Path,
    seed: int,
    timeout_seconds: float,
    inputs: Sequence[Path],
    declared_copy: Path | None,
    copy_policy: str,
    kind: str,
    max_spread: float,
    machine_idle_asserted: bool,
    grafx_sha: str | None,
    pulse_sha: str | None,
    page_size: int,
    buffer_budget_bytes: int,
    checksum: str,
    pulse_root: Path | None = None,
    pulse_core_sha: str | None = None,
    pulse_core_root: Path | None = None,
    data_home_policy: str = "isolated",
    instrument_paths: Sequence[Path] = (),
    poll_seconds: float = 0.5,
    label: str = "",
) -> dict[str, Any]:
    if type(runs) is not int or runs < MIN_RUNS:
        raise RunRefused(f"--runs must be >= {MIN_RUNS} (plan P0.4); got {runs}")
    if type(warmup) is not int or warmup < 0:
        raise RunRefused("--warmup must be >= 0")
    if type(seed) is not int:
        raise RunRefused("--seed must be an integer")
    if not (0 < timeout_seconds < float("inf")):
        raise RunRefused("--timeout-seconds must be a finite positive number")
    if not (0 < poll_seconds < float("inf")):
        raise RunRefused("--poll-seconds must be a finite positive number")
    if not (0 <= max_spread < float("inf")):
        raise RunRefused("--max-spread must be a finite non-negative number")
    if mode not in ("strict", "generation"):
        raise RunRefused("--mode must be strict or generation")
    if thermal not in ("cold", "warm", "mixed"):
        raise RunRefused("--thermal must be cold, warm, or mixed")
    if kind not in ("raw", "instrumented"):
        raise RunRefused("--kind must be raw or instrumented")
    if type(page_size) is not int or page_size <= 0:
        raise RunRefused("--page-size must be a positive integer")
    if type(buffer_budget_bytes) is not int or buffer_budget_bytes <= 0:
        raise RunRefused("--buffer-budget-bytes must be a positive integer")
    if type(checksum) is not str or not checksum:
        raise RunRefused("--checksum must be a non-empty value")
    if type(grafx_sha) is not str or not grafx_sha:
        raise RunRefused("--grafx-sha is required for complete provenance")
    if type(pulse_sha) is not str or not pulse_sha:
        raise RunRefused(
            "--pulse-sha is required; use an explicit 'n/a' for a Grafx-only run"
        )
    measured_grafx = grafx_identity()
    measured_grafx_file = measured_grafx.get("file")
    if type(measured_grafx_file) is not str:
        raise RunRefused("could not resolve okto_grafx from this checkout")
    resolved_grafx_file = Path(measured_grafx_file).resolve()
    if not resolved_grafx_file.is_relative_to(SOURCE_IMPORT_ROOT):
        raise RunRefused(
            f"resolved Grafx {resolved_grafx_file} is outside pinned checkout {SOURCE_IMPORT_ROOT}"
        )
    if measured_grafx.get("git_sha") != grafx_sha:
        raise RunRefused(
            f"--grafx-sha {grafx_sha!r} does not match pinned checkout {measured_grafx.get('git_sha')!r}"
        )
    grafx_source_status = _source_status(SOURCE_ROOT, "src/okto_grafx")
    if grafx_source_status:
        raise RunRefused(
            f"Grafx source is not clean at {grafx_sha}: {grafx_source_status[:5]}"
        )
    resolved_pulse_root: Path | None = None
    resolved_pulse_core_root: Path | None = None
    if pulse_sha == "n/a":
        if pulse_root is not None:
            raise RunRefused("--pulse-root cannot accompany --pulse-sha n/a")
        if pulse_core_root is not None or pulse_core_sha is not None:
            raise RunRefused(
                "--pulse-core-root/--pulse-core-sha cannot accompany --pulse-sha n/a"
            )
    else:
        if pulse_root is None:
            raise RunRefused("--pulse-root is required when --pulse-sha is not n/a")
        resolved_pulse_root = guard_not_data_home(pulse_root)
        if not (
            resolved_pulse_root / "src" / "okto_pulse" / "community" / "__init__.py"
        ).is_file():
            raise RunRefused(
                f"--pulse-root {resolved_pulse_root} has no Community package"
            )
        observed_pulse_sha = git_sha_of(resolved_pulse_root)
        if observed_pulse_sha != pulse_sha:
            raise RunRefused(
                f"--pulse-sha {pulse_sha!r} does not match {resolved_pulse_root}: {observed_pulse_sha!r}"
            )
        pulse_source_status = _source_status(
            resolved_pulse_root, "src/okto_pulse/community"
        )
        if pulse_source_status:
            raise RunRefused(
                f"Pulse source is not clean at {pulse_sha}: {pulse_source_status[:5]}"
            )
        if pulse_core_root is None or not pulse_core_sha:
            raise RunRefused(
                "--pulse-core-root and --pulse-core-sha are required for a Pulse run"
            )
        resolved_pulse_core_root = guard_not_data_home(pulse_core_root)
        if not (
            resolved_pulse_core_root / "src" / "okto_pulse" / "core" / "__init__.py"
        ).is_file():
            raise RunRefused(
                f"--pulse-core-root {resolved_pulse_core_root} has no Core package"
            )
        observed_pulse_core_sha = git_sha_of(resolved_pulse_core_root)
        if observed_pulse_core_sha != pulse_core_sha:
            raise RunRefused(
                f"--pulse-core-sha {pulse_core_sha!r} does not match "
                f"{resolved_pulse_core_root}: {observed_pulse_core_sha!r}"
            )
        pulse_core_source_status = _source_status(
            resolved_pulse_core_root, "src/okto_pulse/core"
        )
        if pulse_core_source_status:
            raise RunRefused(
                f"Pulse Core source is not clean at {pulse_core_sha}: "
                f"{pulse_core_source_status[:5]}"
            )
    if not metrics or any(
        type(name) is not str or not name or type(path) is not str or not path
        for name, path in metrics.items()
    ):
        raise RunRefused("at least one non-empty metric name=dotted.path is required")
    if copy_policy not in COPY_POLICIES:
        raise RunRefused(f"--copy-policy must be one of {COPY_POLICIES}")
    if data_home_policy not in DATA_HOME_POLICIES:
        raise RunRefused(f"--data-home-policy must be one of {DATA_HOME_POLICIES}")
    if data_home_policy == "declared-copy-clone" and (
        declared_copy is None or copy_policy != "clone"
    ):
        raise RunRefused(
            "--data-home-policy declared-copy-clone requires --declared-copy and --copy-policy clone"
        )
    if resolved_pulse_root is not None and data_home_policy != "declared-copy-clone":
        raise RunRefused(
            "Pulse runs require --data-home-policy declared-copy-clone so DATA_DIR is the disposable full-home clone"
        )
    template = list(argv_template)
    _guard_argv(template, has_copy=declared_copy is not None)
    out_dir = guard_not_data_home(out_dir)
    resolved_inputs = [guard_not_data_home(path) for path in inputs]
    protected: list[tuple[str, Path]] = [("input", path) for path in resolved_inputs]
    if declared_copy is not None:
        declared_root = guard_not_data_home(declared_copy)
        require_declared_copy(declared_root)
        protected.append(("declared copy", declared_root))
    for label_name, protected_path in protected:
        if (
            out_dir == protected_path
            or out_dir in protected_path.parents
            or protected_path in out_dir.parents
        ):
            raise RunRefused(
                f"out-dir {out_dir} overlaps {label_name} {protected_path}; evidence output must be disjoint"
            )
    series_receipt = out_dir / f"{mode}_series_receipt.json"
    if series_receipt.exists() or Path(str(series_receipt) + ".sha256").exists():
        raise RunRefused(
            f"series receipt {series_receipt} or its sidecar already exists"
        )
    records: list[dict[str, Any]] = []
    for resolved in resolved_inputs:
        if not resolved.exists():
            raise RunRefused(f"input {resolved} does not exist")
        digest = (
            inventory(resolved)["sha256"]
            if resolved.is_dir()
            else sha256_file(resolved)
        )
        records.append(plain_input("input", resolved, inventory_digest=digest))
    if declared_copy is not None:
        records.append(declared_copy_input("declared_copy", declared_copy))
    instruments = _instrument_hashes(template, instrument_paths)
    for item in instruments:
        if item["path"] is not None:
            records.append(
                plain_input(
                    "interpreter" if item["interpreter"] else "instrument",
                    item["path"],
                    inventory_digest=item["sha256"],
                )
            )
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_child_base = {
        "python_executable": str(Path(sys.executable).resolve()),
        "okto_grafx_file": str(resolved_grafx_file),
        "mode": mode,
        "seed": seed,
        "kind": kind,
        "page_size": page_size,
        "buffer_budget_bytes": buffer_budget_bytes,
        "checksum": checksum,
        "thermal": thermal,
    }
    if resolved_pulse_root is not None:
        expected_child_base["okto_pulse_community_file"] = str(
            (
                resolved_pulse_root
                / "src"
                / "okto_pulse"
                / "community"
                / "__init__.py"
            ).resolve()
        )
        expected_child_base["okto_pulse_core_file"] = str(
            (
                resolved_pulse_core_root
                / "src"
                / "okto_pulse"
                / "core"
                / "__init__.py"
            ).resolve()
        )

    run_reports: list[dict[str, Any]] = []
    for index in range(warmup + runs):
        out = out_dir / f"{mode}_run{index:02d}.json"
        run_home = out_dir / f"{mode}_run{index:02d}_home"
        if out.exists():
            raise RunRefused(
                f"output {out} already exists; refusing to mix runs with stale outputs"
            )
        if run_home.exists():
            raise RunRefused(
                f"run home {run_home} already exists; refusing to reuse another run's state"
            )
        report: dict[str, Any] = {
            "run": index,
            "role": "warmup" if index < warmup else "measured",
            "discarded": index < warmup,
            "process": "fresh",
            "thermal": thermal,
            "data_home_policy": data_home_policy,
            "errors": [],
        }
        copy_for_run = ""
        run_copy_manifest: dict[str, Any] | None = None
        if declared_copy is not None:
            if copy_policy == "clone":
                clone_dir = out_dir / f"{mode}_run{index:02d}_copy"
                run_copy_manifest = _clone_copy(declared_copy, clone_dir)
                copy_for_run = str(clone_dir)
            else:
                run_copy_manifest = require_declared_copy(declared_copy)
                copy_for_run = str(Path(declared_copy).resolve())
            report["copy"] = {
                "path": copy_for_run,
                "policy": copy_policy,
                "sha256_before": run_copy_manifest["copy"]["sha256"],
            }
        effective_data_home = (
            Path(copy_for_run).resolve()
            if data_home_policy == "declared-copy-clone"
            else run_home.resolve()
        )
        if data_home_policy == "isolated":
            effective_data_home.mkdir()
        report["data_home"] = str(effective_data_home)
        argv = render_argv(
            template,
            out=str(out),
            run=str(index),
            mode=mode,
            seed=str(seed),
            copy=copy_for_run,
            page_size=str(page_size),
            buffer_budget_bytes=str(buffer_budget_bytes),
            checksum=checksum,
            kind=kind,
            thermal=thermal,
        )
        report["argv"] = argv
        report["machine_before"] = machine_sample(interval_seconds=1.0)
        started_wall = time.time()
        started = time.perf_counter()
        try:
            process = _spawn(
                argv,
                effective_data_home,
                resolved_pulse_root,
                resolved_pulse_core_root,
            )
        except OSError as failure:
            report["exit_code"] = None
            report["errors"].append(f"could not start: {failure}")
            run_reports.append(report)
            continue
        memory = _watch(process, poll_seconds, timeout_seconds)
        report["wall_seconds"] = time.perf_counter() - started
        report["exit_code"] = process.returncode
        report["memory"] = memory
        report["metrics"] = {}
        if memory["timed_out"]:
            report["errors"].append(
                f"timed out after {timeout_seconds} s and was killed"
            )
        if memory["peak_rss_bytes_tree"] is None:
            report["errors"].append("RSS for the process tree was unavailable")
        if memory["peak_private_bytes_tree"] is None:
            report["errors"].append(
                "private/USS memory for the process tree was unavailable"
            )
        if declared_copy is not None:
            after = inventory(Path(copy_for_run), exclude=_MANIFEST_FILES)["sha256"]
            report["copy"]["sha256_after"] = after
            report["copy"]["mutated"] = after != report["copy"]["sha256_before"]
            if copy_policy == "verify" and report["copy"]["mutated"]:
                report["errors"].append(
                    "the run mutated the shared declared copy; later runs would not be independent"
                )
        if out.is_file():
            if out.stat().st_mtime < started_wall - 1.0:
                report["errors"].append(f"output {out} predates the run (stale)")
            report["output"] = str(out)
            report["output_sha256"] = sha256_file(out)
            try:
                document = json.loads(out.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as failure:
                document = None
                report["errors"].append(f"output is not valid JSON: {failure}")
            if document is not None and process.returncode == 0:
                report["errors"].extend(
                    validate_child_provenance(
                        document,
                        {
                            **expected_child_base,
                            "run": index,
                            "effective_data_dir": str(effective_data_home),
                        },
                    )
                )
                for name, dotted in metrics.items():
                    try:
                        report["metrics"][name] = extract(document, dotted)
                    except (KeyError, IndexError, TypeError, ValueError) as failure:
                        report["errors"].append(f"{name}: {dotted}: {failure!r}")
        else:
            report["output"] = None
            report["output_sha256"] = None
            report["errors"].append("the run wrote no output")
        if process.returncode != 0:
            report["errors"].append(f"run exited {process.returncode}")
        run_reports.append(report)

    measured = [r for r in run_reports if not r["discarded"] and not r["errors"]]
    aggregates: dict[str, Any] = {}
    inconclusive: list[str] = []
    for name in metrics:
        values = [
            float(r["metrics"][name]) for r in measured if name in r.get("metrics", {})
        ]
        if len(values) < MIN_RUNS:
            aggregates[name] = {
                "n": len(values),
                "values": values,
                "error": f"fewer than {MIN_RUNS} clean measured runs",
            }
            inconclusive.append(name)
            continue
        summary = summarize(values)
        summary["exceeds_max_spread"] = (
            summary["relative_spread"] is None and summary["min"] != summary["max"]
        ) or (
            summary["relative_spread"] is not None
            and summary["relative_spread"] > max_spread
        )
        if summary["exceeds_max_spread"]:
            inconclusive.append(name)
        aggregates[name] = summary
    results = {
        "label": label,
        "runs_requested": runs,
        "warmup": warmup,
        "runs_measured_clean": len(measured),
        "max_spread": max_spread,
        "timeout_seconds": timeout_seconds,
        "copy_policy": copy_policy if declared_copy is not None else None,
        "instruments": instruments,
        "expected_child_provenance": expected_child_base,
        "aggregates": aggregates,
        "inconclusive_metrics": inconclusive,
        "recommendation": (
            "run exactly one more measured run and re-aggregate (plan P0.4: a fourth run only if the dispersion is inconclusive)"
            if inconclusive
            else "series is conclusive at this spread; do not add runs"
        ),
        "runs": run_reports,
    }
    receipt = build_receipt(
        tool=__file__,
        seed=seed,
        parameters={
            "argv_template": template,
            "metrics": dict(metrics),
            "runs": runs,
            "warmup": warmup,
            "poll_seconds": poll_seconds,
            "timeout_seconds": timeout_seconds,
            "copy_policy": copy_policy,
            "data_home_policy": data_home_policy,
            "thermal": thermal,
            "child_data_home_variables": list(DATA_HOME_ENV),
            "child_pythonpath_prepend": [
                str(SOURCE_IMPORT_ROOT),
                *(
                    [str(resolved_pulse_root / "src")]
                    if resolved_pulse_root is not None
                    else []
                ),
                *(
                    [str(resolved_pulse_core_root / "src")]
                    if resolved_pulse_core_root is not None
                    else []
                ),
            ],
            "pulse_root": str(Path(pulse_root).resolve()) if pulse_root else None,
            "pulse_core_root": (
                str(Path(pulse_core_root).resolve()) if pulse_core_root else None
            ),
            "grafx_source_clean": True,
            "pulse_source_clean": True if resolved_pulse_root else None,
            "pulse_core_source_clean": True if resolved_pulse_core_root else None,
        },
        series={"mode": mode, "thermal": thermal, "kind": kind},
        config={
            "descriptor_revalidation": mode,
            "page_size": page_size,
            "buffer_budget_bytes": buffer_budget_bytes,
            "checksum": checksum,
        },
        inputs=records,
        results=results,
        grafx_sha=grafx_sha,
        pulse_sha=pulse_sha,
        pulse_core_sha=pulse_core_sha,
        machine_idle_asserted=machine_idle_asserted,
        notes=[
            "warmup runs are recorded but discarded from every aggregate",
            "every run is a fresh process with its own isolated data home; a Pulse run uses its disposable full-home clone",
            "official is false by construction: this tool cannot prove the box idle",
            "thermal and raw/instrumented kind are explicit operator assertions; the concrete driver must enforce them",
        ],
    )
    write_receipt(series_receipt, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=("strict", "generation"), required=True)
    parser.add_argument(
        "--thermal",
        choices=("cold", "warm", "mixed"),
        required=True,
        help="the operator's statement about the filesystem cache",
    )
    parser.add_argument("--runs", type=int, default=MIN_RUNS)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        help="one argv token of the instrument; repeat; whole-token placeholders include {out} {run} {mode} {seed} {copy} {thermal}",
    )
    parser.add_argument(
        "--instrument-path",
        type=Path,
        action="append",
        default=[],
        help="a supplementary file to hash; a script path or inline -c payload must still be bound to argv",
    )
    parser.add_argument(
        "--metric",
        action="append",
        default=[],
        help="name=dotted.path into the run JSON",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--page-size", type=int, required=True)
    parser.add_argument("--buffer-budget-bytes", type=int, required=True)
    parser.add_argument("--checksum", required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--input", type=Path, action="append", default=[])
    parser.add_argument("--declared-copy", type=Path)
    parser.add_argument("--copy-policy", choices=COPY_POLICIES, default="clone")
    parser.add_argument(
        "--data-home-policy", choices=DATA_HOME_POLICIES, default="isolated"
    )
    parser.add_argument("--kind", choices=("raw", "instrumented"), default="raw")
    parser.add_argument("--max-spread", type=float, default=0.08)
    parser.add_argument("--machine-idle-asserted", action="store_true")
    parser.add_argument("--grafx-sha", required=True)
    parser.add_argument("--pulse-sha", required=True)
    parser.add_argument("--pulse-root", type=Path)
    parser.add_argument("--pulse-core-sha")
    parser.add_argument("--pulse-core-root", type=Path)
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    metrics: dict[str, str] = {}
    for item in args.metric:
        name, _, dotted = item.partition("=")
        if not name or not dotted:
            parser.error(f"--metric expects name=dotted.path, got {item!r}")
        metrics[name] = dotted
    if not metrics:
        parser.error("at least one --metric is required")
    if not args.arg:
        parser.error("at least one --arg is required")
    try:
        receipt = run_series(
            mode=args.mode,
            thermal=args.thermal,
            runs=args.runs,
            warmup=args.warmup,
            argv_template=args.arg,
            metrics=metrics,
            out_dir=args.out_dir,
            seed=args.seed,
            timeout_seconds=args.timeout_seconds,
            inputs=args.input,
            declared_copy=args.declared_copy,
            copy_policy=args.copy_policy,
            kind=args.kind,
            max_spread=args.max_spread,
            machine_idle_asserted=args.machine_idle_asserted,
            grafx_sha=args.grafx_sha,
            pulse_sha=args.pulse_sha,
            page_size=args.page_size,
            buffer_budget_bytes=args.buffer_budget_bytes,
            checksum=args.checksum,
            pulse_root=args.pulse_root,
            pulse_core_sha=args.pulse_core_sha,
            pulse_core_root=args.pulse_core_root,
            data_home_policy=args.data_home_policy,
            instrument_paths=args.instrument_path,
            label=args.label,
        )
    except (LiveBoardRefused, RunRefused) as refused:
        print(f"REFUSED: {refused}", file=sys.stderr)
        return 2
    results = receipt["results"]
    for name, summary in results["aggregates"].items():
        print(format_summary(name, summary))
    print(results["recommendation"])
    if results["runs_measured_clean"] < MIN_RUNS:
        print(
            f"INCOMPLETE: only {results['runs_measured_clean']} clean measured runs; need {MIN_RUNS}",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
