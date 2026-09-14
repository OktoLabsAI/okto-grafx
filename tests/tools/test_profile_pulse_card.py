from __future__ import annotations

import io
import json
import os
import queue
import sys
import time
from pathlib import Path

import pytest

from tools.perf_round import profile_pulse_card
from tools.perf_round.profile_pulse_card import (
    PY_SPY_RATE_HZ,
    ProcessCounters,
    ProfileRefused,
    _await_profiler_attach,
    _counter_deltas,
    _parser,
    _profile_argv,
    _require_direct_interpreter,
    _run_profiled_process,
    _validate_profile_artifact,
)
from tools.perf_round.replay_pulse_card import (
    PROFILE_GATE_PROTOCOL,
    PROFILE_GATE_PROTOCOL_ENV,
    PROFILE_GATE_READY_PATH_ENV,
    PROFILE_GATE_TOKEN_ENV,
    DriverRefused,
    _profile_start_barrier,
)


TOKEN = "a" * 64


def test_replay_profile_barrier_is_optional_and_nonce_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    ready = tmp_path / "ready"
    for name in (
        PROFILE_GATE_PROTOCOL_ENV,
        PROFILE_GATE_READY_PATH_ENV,
        PROFILE_GATE_TOKEN_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    assert _profile_start_barrier(clone) == {
        "enabled": False,
        "protocol": None,
        "released": False,
    }

    monkeypatch.setenv(PROFILE_GATE_PROTOCOL_ENV, PROFILE_GATE_PROTOCOL)
    monkeypatch.setenv(PROFILE_GATE_READY_PATH_ENV, str(ready))
    monkeypatch.setenv(PROFILE_GATE_TOKEN_ENV, TOKEN)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"go-v1:{TOKEN}\n"))
    assert _profile_start_barrier(clone) == {
        "enabled": True,
        "protocol": PROFILE_GATE_PROTOCOL,
        "released": True,
    }
    assert ready.read_text(encoding="ascii") == f"ready-v1:{TOKEN}\n"


def test_replay_profile_barrier_refuses_partial_or_wrong_parent_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    monkeypatch.setenv(PROFILE_GATE_PROTOCOL_ENV, PROFILE_GATE_PROTOCOL)
    with pytest.raises(DriverRefused, match="environment is incomplete"):
        _profile_start_barrier(clone)

    ready = tmp_path / "ready"
    monkeypatch.setenv(PROFILE_GATE_READY_PATH_ENV, str(ready))
    monkeypatch.setenv(PROFILE_GATE_TOKEN_ENV, TOKEN)
    monkeypatch.setattr(sys, "stdin", io.StringIO("go-v1:" + "b" * 64 + "\n"))
    with pytest.raises(DriverRefused, match="did not release"):
        _profile_start_barrier(clone)


def test_interpreter_preflight_refuses_a_launcher_and_passes_a_direct_interpreter(
    tmp_path: Path,
) -> None:
    """A launcher that starts Python as a grandchild is refused before any PID is trusted."""
    base = getattr(sys, "_base_executable", None) or sys.executable
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import subprocess, sys\n"
        "sys.exit(subprocess.run([sys.executable, *sys.argv[1:]]).returncode)\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfileRefused, match="interpreter launcher detected"):
        _require_direct_interpreter((base, str(launcher)))
    report = _require_direct_interpreter((base,))
    assert report["direct"] is True
    assert report["child_pid_parent_is_runner"] is True
    assert report["argv_prefix"] == [base]


def test_profile_cli_has_no_operator_pid_and_argv_is_closed() -> None:
    parser = _parser()
    assert "--pid" not in parser._option_string_actions
    argv = _profile_argv(
        ["py-spy"], target_pid=123, output=Path("profile.speedscope.json")
    )
    assert argv == [
        "py-spy",
        "record",
        "--pid",
        "123",
        "--format",
        "speedscope",
        "--output",
        "profile.speedscope.json",
        "--rate",
        str(PY_SPY_RATE_HZ),
        "--threads",
    ]
    for forbidden in ("--locals", "--full-filenames", "--native", "--subprocesses"):
        assert forbidden not in argv


class _AttachedProfiler:
    @staticmethod
    def poll() -> None:
        return None


def test_attach_confirmation_is_exact_and_fail_closed() -> None:
    messages: queue.Queue[str | None] = queue.Queue()
    messages.put(
        f"py-spy> Sampling process {PY_SPY_RATE_HZ} times a second. "
        "Press Control-C to exit."
    )
    assert (
        _await_profiler_attach(
            _AttachedProfiler(),
            messages,
            timeout_seconds=0.1,  # type: ignore[arg-type]
        )
        >= 0
    )

    unexpected: queue.Queue[str | None] = queue.Queue()
    unexpected.put("py-spy> unexpected warning")
    with pytest.raises(ProfileRefused, match="unexpected pre-attach"):
        _await_profiler_attach(
            _AttachedProfiler(),
            unexpected,
            timeout_seconds=0.1,  # type: ignore[arg-type]
        )


def _speedscope(samples: int = 1) -> dict[str, object]:
    return {
        "$schema": "https://www.speedscope.app/file-format-schema.json",
        "shared": {"frames": [{"name": "work"}]},
        "profiles": [
            {
                "type": "sampled",
                "name": "process",
                "unit": "seconds",
                "startValue": 0,
                "endValue": samples,
                "samples": [[0] for _ in range(samples)],
                "weights": [1 for _ in range(samples)],
            }
        ],
    }


def test_speedscope_artifact_reconciles_samples_and_refuses_locals(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "profile.json"
    artifact.write_text(json.dumps(_speedscope(2)), encoding="utf-8")
    report = _validate_profile_artifact(
        artifact,
        ("py-spy> Wrote speedscope file. Samples: 2 Errors: 0",),
        started_wall_time=time.time() - 1,
    )
    assert report["sample_count"] == 2
    assert report["locals_collected"] is False

    with_locals = _speedscope()
    with_locals["locals"] = {"secret": "must-not-exist"}
    artifact.unlink()
    artifact.write_text(json.dumps(with_locals), encoding="utf-8")
    with pytest.raises(ProfileRefused, match="contains locals"):
        _validate_profile_artifact(
            artifact,
            ("Samples: 1 Errors: 0",),
            started_wall_time=time.time() - 1,
        )


def test_os_counters_are_deltas_from_pre_go_and_new_children_start_at_zero() -> None:
    existing = (101, 1000.0)
    later_child = (102, 1001.0)
    baseline = ProcessCounters(
        cpu_by_process={existing: 7.5},
        io_by_process={existing: (1000, 800)},
    )

    cpu, reads, writes = _counter_deltas(
        {existing: 9.0, later_child: 0.75},
        {existing: (1300, 850), later_child: (25, 40)},
        baseline,
    )

    assert cpu == 2.25
    assert reads == 325
    assert writes == 90

    with pytest.raises(ProfileRefused, match="CPU counter moved backwards"):
        _counter_deltas(
            {existing: 7.0},
            {existing: (1000, 800)},
            baseline,
        )


def test_synthetic_profile_uses_spawned_direct_child_and_ready_go_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A uv-created Windows venv executable is a launcher: its Python process is
    # a grandchild. The production preflight deliberately refuses that shape.
    # Exercise the successful protocol with the actual interpreter, retaining
    # the PID, parent and executable checks instead of weakening their proof.
    direct_python = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    _require_direct_interpreter((direct_python,))
    monkeypatch.setattr(sys, "executable", direct_python)
    target_script = tmp_path / "target.py"
    target_script.write_text(
        """
import os
import pathlib
import sys
import time

assert os.getppid() == int(os.environ["OKTO_GRAFX_PERF_RUNNER_PID"])
token = os.environ["OKTO_GRAFX_PERF_PROFILE_GATE_TOKEN"]
ready = pathlib.Path(os.environ["OKTO_GRAFX_PERF_PROFILE_READY_PATH"])
with ready.open("x", encoding="ascii", newline="\\n") as handle:
    handle.write(f"ready-v1:{token}\\n")
    handle.flush()
    os.fsync(handle.fileno())
if sys.stdin.readline(80) != f"go-v1:{token}\\n":
    raise SystemExit(3)
pathlib.Path(os.environ["SYNTHETIC_RAN"]).write_text("ran", encoding="ascii")
sum(index * index for index in range(500_000))
time.sleep(0.25)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    fake_profiler = tmp_path / "fake_py_spy.py"
    fake_profiler.write_text(
        """
import json
import pathlib
import sys
import time

args = sys.argv[1:]
assert args[0] == "record"
assert "--locals" not in args
out = pathlib.Path(args[args.index("--output") + 1])
rate = args[args.index("--rate") + 1]
print(f"py-spy> Sampling process {rate} times a second. Press Control-C to exit.", flush=True)
time.sleep(0.5)
document = {
    "$schema": "https://www.speedscope.app/file-format-schema.json",
    "shared": {"frames": [{"name": "synthetic"}]},
    "profiles": [{
        "type": "sampled", "name": "synthetic", "unit": "seconds",
        "startValue": 0, "endValue": 1, "samples": [[0]], "weights": [1]
    }],
}
out.write_text(json.dumps(document), encoding="utf-8")
print("py-spy> Stopped sampling because process exited", flush=True)
print(f"py-spy> Wrote speedscope file to '{out}'. Samples: 1 Errors: 0", flush=True)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    ready = tmp_path / "ready"
    profile = tmp_path / "profile.json"
    ran = tmp_path / "ran"
    target_environment = dict(os.environ)
    target_environment.update(
        {
            "OKTO_GRAFX_PERF_RUNNER_PID": str(os.getpid()),
            PROFILE_GATE_PROTOCOL_ENV: PROFILE_GATE_PROTOCOL,
            PROFILE_GATE_READY_PATH_ENV: str(ready),
            PROFILE_GATE_TOKEN_ENV: TOKEN,
            "SYNTHETIC_RAN": str(ran),
        }
    )
    started = time.time()
    result = _run_profiled_process(
        target_argv=[sys.executable, str(target_script)],
        target_environment=target_environment,
        profiler_command=[sys.executable, str(fake_profiler)],
        profiler_environment=os.environ,
        ready_path=ready,
        profile_path=profile,
        token=TOKEN,
        ready_timeout_seconds=3,
        attach_timeout_seconds=3,
        timeout_seconds=3,
        poll_seconds=0.02,
    )
    assert ran.read_text(encoding="ascii") == "ran"
    assert result.target_identity["parent_pid_matches_runner"] is True
    assert result.target_identity["pid_source"] == "internal_subprocess_popen"
    assert result.target_identity["identity_revalidated_after_attach"] is True
    assert result.target_exit_code == result.profiler_exit_code == 0
    assert result.os_tree["scope"].endswith("excludes_profiler_sibling")
    assert "cpu_seconds_sampled_delta_from_pre_go" in result.os_tree
    assert "cpu_seconds_observed_tree" not in result.os_tree
    artifact = _validate_profile_artifact(
        profile, result.profiler_lines, started_wall_time=started
    )
    assert artifact["sample_count"] == 1


def test_runner_source_has_no_public_pid_or_locals_escape_hatch() -> None:
    source = Path(profile_pulse_card.__file__).read_text(encoding="utf-8")
    assert 'parser.add_argument("--pid"' not in source
    assert (
        '"--locals",' in source
    )  # an explicit forbidden-token assertion, not an option
