"""Focused contracts for the one-shot P0.3 graph-census runner."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools.perf_round import pulse_graph_census_once
from tools.perf_round.profile_pulse_card import RuntimePins
from tools.perf_round.pulse_graph_census_once import (
    CensusOnceRefused,
    _authenticated_graph_relative,
    _child_argv,
    _parser,
    _validate_child_document,
    _validate_independent_inventory_delta,
)
from tools.perf_round.receipt import sha256_file, sha256_text
from tools.perf_round.replay_pulse_card import SUPPORTED_PULSE_BUFFER_BUDGET_BYTES

BOARD_ID = "00000000-0000-0000-0000-000000000001"


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        declared_copy=tmp_path / "declared",
        out_dir=tmp_path / "evidence",
        board_id=BOARD_ID,
        mode="strict",
        seed=17,
        page_size=8192,
        buffer_budget_bytes=SUPPORTED_PULSE_BUFFER_BUDGET_BYTES,
        checksum="auto",
        timeout_seconds=30.0,
        poll_seconds=0.02,
        grafx_sha="a" * 40,
        pulse_root=tmp_path / "pulse",
        pulse_sha="b" * 40,
        pulse_core_root=tmp_path / "core",
        pulse_core_sha="c" * 40,
    )


def _pins(tmp_path: Path) -> RuntimePins:
    pulse_root = tmp_path / "pulse"
    core_root = tmp_path / "core"
    pulse_file = pulse_root / "src" / "okto_pulse" / "community" / "__init__.py"
    core_file = core_root / "src" / "okto_pulse" / "core" / "__init__.py"
    pulse_file.parent.mkdir(parents=True)
    core_file.parent.mkdir(parents=True)
    pulse_file.write_text("", encoding="utf-8")
    core_file.write_text("", encoding="utf-8")
    return RuntimePins(
        grafx_file=(
            pulse_graph_census_once.SOURCE_ROOT / "src" / "okto_grafx" / "__init__.py"
        ),
        pulse_root=pulse_root,
        pulse_file=pulse_file,
        pulse_core_root=core_root,
        pulse_core_file=core_file,
    )


def _child_document(
    args: argparse.Namespace,
    *,
    clone: Path,
    pins: RuntimePins,
    digest: str,
    after_digest: str | None = None,
    lock_path: str = "graph/control/txn-0123abcd.lock",
    added_count: int = 0,
    file_count: int = 1,
    total_bytes: int = 1,
) -> dict[str, Any]:
    raw_unchanged = added_count == 0
    if after_digest is None:
        after_digest = digest if raw_unchanged else "f" * 64
    return {
        "schema": pulse_graph_census_once.CHILD_SCHEMA,
        "workload_semantics": pulse_graph_census_once.WORKLOAD_SEMANTICS,
        "measurement_semantics": {
            "timed": False,
            "filesystem_cache_controlled": False,
            "thermal_label": "mixed",
            "kind": "instrumented",
            "vacuum_safety_established": False,
            "vector_activity_established": False,
        },
        "route": {
            "binding_authenticated": True,
            "backend": "grafx",
            "path_inside_clone": True,
        },
        "engine": {
            "read_only": True,
            "recovery_ran": False,
            "recovery_required": False,
        },
        "verification": {
            "pages": {"clean": True, "checked_total": 2},
            "records": {"clean": True, "checked_total": 1},
        },
        "counts": {
            "tables_total": 1,
            "heap_pages_total": 1,
            "heap_record_slots_total": 1,
            "vector_spaces_total": 0,
            "vector_indexes_total": 0,
            "stale_indexes_total": 0,
            "stale_vector_indexes_total": 0,
        },
        "heap": {
            "mvcc_headers": {
                "versions_total": 1,
                "live_committed_open": 1,
                "dead_total": 0,
                "dead_committed_ended": 0,
                "dead_no_csn_birth": 0,
                "dead_provisional_birth": 0,
            },
            "live_version_tail_distance_pages": {
                "total": 1,
                "min": 0,
                "p50": 0,
                "p90": 0,
                "p99": 0,
                "max": 0,
                "last_10_percent": 1,
                "last_10_percent_ratio": 1.0,
            },
        },
        "clone_inventory": {
            "unchanged": raw_unchanged,
            "raw_unchanged": raw_unchanged,
            "unchanged_except_expected_participant_lock": True,
            "serve_lock_artifacts_absent": True,
            "sha256_before": digest,
            "sha256_after": after_digest,
            "file_count_before": file_count,
            "file_count_after": file_count + added_count,
            "file_count": file_count + added_count,
            "total_bytes_before": total_bytes,
            "total_bytes_after": total_bytes,
            "total_bytes": total_bytes,
            "expected_participant_lock_path_sha256": sha256_text(lock_path),
            "expected_participant_lock_added_count": added_count,
        },
        "_perf_round": {
            "python_executable": str(Path(sys.executable).resolve()),
            "okto_grafx_file": str(pins.grafx_file),
            "okto_pulse_community_file": str(pins.pulse_file),
            "okto_pulse_core_file": str(pins.pulse_core_file),
            "tool_sha256": sha256_file(pulse_graph_census_once.CENSUS_SCRIPT),
            "mode": args.mode,
            "seed": args.seed,
            "kind": "instrumented",
            "page_size": args.page_size,
            "buffer_budget_bytes": args.buffer_budget_bytes,
            "checksum": "auto",
            "thermal": "mixed",
            "run": 0,
            "effective_data_dir": str(clone.resolve()),
        },
    }


def test_cli_exposes_one_census_not_a_series_or_profiler_escape_hatch(
    tmp_path: Path,
) -> None:
    parser = _parser()
    for forbidden in ("--runs", "--warmup", "--kind", "--thermal", "--pid"):
        assert forbidden not in parser._option_string_actions
    args = _args(tmp_path)
    argv = _child_argv(
        args,
        clone=tmp_path / "clone",
        child_output=tmp_path / "result.json",
    )
    assert argv.count("--run") == 1 and argv[argv.index("--run") + 1] == "0"
    assert argv[argv.index("--kind") + 1] == "instrumented"
    assert argv[argv.index("--thermal") + 1] == "mixed"


def test_child_validation_refuses_false_semantics_or_clone_proof(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path)
    pins = _pins(tmp_path)
    clone = tmp_path / "clone"
    digest = "d" * 64
    expected = pulse_graph_census_once._expected_child(args, clone=clone, pins=pins)
    document = _child_document(args, clone=clone, pins=pins, digest=digest)
    _validate_child_document(
        document,
        expected_provenance=expected,
        expected_clone_inventory={
            "sha256": digest,
            "file_count": 1,
            "total_bytes": 1,
        },
    )

    false_thermal = json.loads(json.dumps(document))
    false_thermal["measurement_semantics"]["thermal_label"] = "warm"
    with pytest.raises(CensusOnceRefused, match="semantics are incomplete"):
        _validate_child_document(
            false_thermal,
            expected_provenance=expected,
            expected_clone_inventory={
                "sha256": digest,
                "file_count": 1,
                "total_bytes": 1,
            },
        )

    changed = json.loads(json.dumps(document))
    changed["clone_inventory"]["sha256_after"] = "e" * 64
    with pytest.raises(CensusOnceRefused, match="inconsistent clone inventory"):
        _validate_child_document(
            changed,
            expected_provenance=expected,
            expected_clone_inventory={
                "sha256": digest,
                "file_count": 1,
                "total_bytes": 1,
            },
        )

    missing = json.loads(json.dumps(document))
    missing["clone_inventory"].pop("expected_participant_lock_path_sha256")
    with pytest.raises(CensusOnceRefused, match="bounded clone inventory delta"):
        _validate_child_document(
            missing,
            expected_provenance=expected,
            expected_clone_inventory={
                "sha256": digest,
                "file_count": 1,
                "total_bytes": 1,
            },
        )

    inconsistent = json.loads(json.dumps(document))
    inconsistent["heap"]["mvcc_headers"]["versions_total"] = 2
    with pytest.raises(CensusOnceRefused, match="MVCC populations do not reconcile"):
        _validate_child_document(
            inconsistent,
            expected_provenance=expected,
            expected_clone_inventory={
                "sha256": digest,
                "file_count": 1,
                "total_bytes": 1,
            },
        )


def test_one_shot_independently_refuses_a_forged_lock_path_hash() -> None:
    lock_path = "graph/control/txn-0123abcd.lock"
    stable = {"path": "opaque", "size": 1, "sha256": "a" * 64}
    lock = {"path": lock_path, "size": 0, "sha256": sha256_text("")}
    expected = {
        "sha256": "b" * 64,
        "file_count": 1,
        "total_bytes": 1,
        "files": [stable],
    }
    observed = {
        "sha256": "c" * 64,
        "file_count": 2,
        "total_bytes": 1,
        "files": [stable, lock],
    }
    proof = {
        "unchanged": False,
        "raw_unchanged": False,
        "sha256_before": expected["sha256"],
        "sha256_after": observed["sha256"],
        "file_count_before": 1,
        "file_count_after": 2,
        "total_bytes_before": 1,
        "total_bytes_after": 1,
        "expected_participant_lock_added_count": 1,
        "expected_participant_lock_path_sha256": sha256_text(lock_path),
    }
    _validate_independent_inventory_delta(
        expected, observed, proof, expected_graph_relative="graph"
    )
    proof["expected_participant_lock_path_sha256"] = "d" * 64
    with pytest.raises(CensusOnceRefused, match="opaque path proof"):
        _validate_independent_inventory_delta(
            expected, observed, proof, expected_graph_relative="graph"
        )

    sibling_path = "sibling/control/txn-0123abcd.lock"
    sibling_lock = {**lock, "path": sibling_path}
    observed["files"] = [stable, sibling_lock]
    proof["expected_participant_lock_path_sha256"] = sha256_text(sibling_path)
    with pytest.raises(CensusOnceRefused, match="no valid empty lock"):
        _validate_independent_inventory_delta(
            expected, observed, proof, expected_graph_relative="graph"
        )


def test_binding_authentication_refuses_float_page_size(tmp_path: Path) -> None:
    graph_relative = f"boards/{BOARD_ID}/grafx/generation"
    binding_relative = f"boards/{BOARD_ID}/graph_backend_binding.json"
    binding = {
        "binding_format": "okto-pulse-community-graph-binding/1",
        "scope": "board",
        "scope_id": BOARD_ID,
        "backend": "grafx",
        "generation": "generation",
        "physical_path": graph_relative,
        "page_size": 8192.0,
    }
    binding["binding_sha256"] = sha256_text(
        json.dumps(binding, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )
    encoded = json.dumps(
        binding, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    binding_path = tmp_path.joinpath(*binding_relative.split("/"))
    binding_path.parent.mkdir(parents=True)
    binding_path.write_text(encoded, encoding="utf-8")
    tmp_path.joinpath(*graph_relative.split("/")).mkdir(parents=True)
    expected_inventory = {
        "files": [
            {
                "path": binding_relative,
                "size": len(encoded.encode("utf-8")),
                "sha256": sha256_text(encoded),
            }
        ]
    }

    with pytest.raises(CensusOnceRefused, match="did not authenticate"):
        _authenticated_graph_relative(
            tmp_path,
            board_id=BOARD_ID,
            page_size=8192,
            expected_inventory=expected_inventory,
        )


def test_runner_clones_and_launches_exactly_one_authenticated_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    args.declared_copy.mkdir()
    pins = _pins(tmp_path)
    digest = "d" * 64
    after_digest = "f" * 64
    graph_relative = f"boards/{BOARD_ID}/grafx/generation"
    lock_path = f"{graph_relative}/control/txn-0123abcd.lock"
    binding_relative = f"boards/{BOARD_ID}/graph_backend_binding.json"
    binding_body = {
        "binding_format": "okto-pulse-community-graph-binding/1",
        "scope": "board",
        "scope_id": BOARD_ID,
        "backend": "grafx",
        "generation": "generation",
        "physical_path": graph_relative,
        "page_size": args.page_size,
    }
    binding_body["binding_sha256"] = sha256_text(
        json.dumps(
            binding_body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
    )
    binding_text = json.dumps(
        binding_body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    stable_file = {"path": "opaque", "size": 1, "sha256": "e" * 64}
    binding_file = {
        "path": binding_relative,
        "size": len(binding_text.encode("utf-8")),
        "sha256": sha256_text(binding_text),
    }
    lock_file = {"path": lock_path, "size": 0, "sha256": sha256_text("")}
    expected_bytes = 1 + binding_file["size"]
    manifest = {
        "source": {"sha256_after_copy": digest},
        "copy": {
            "sha256": digest,
            "file_count": 2,
            "total_bytes": expected_bytes,
            "files": [stable_file, binding_file],
        },
        "commits": {
            "grafx": args.grafx_sha,
            "pulse": args.pulse_sha,
            "pulse_core": args.pulse_core_sha,
        },
    }
    observed: dict[str, Any] = {"spawns": 0}
    monkeypatch.setattr(
        pulse_graph_census_once, "_validate_source_pins", lambda _args: pins
    )

    def clone_once(source: Path, destination: Path) -> dict[str, Any]:
        assert source == args.declared_copy.resolve()
        destination.mkdir()
        (destination / "opaque").write_text("x", encoding="ascii")
        binding_path = destination.joinpath(*binding_relative.split("/"))
        binding_path.parent.mkdir(parents=True)
        binding_path.write_text(binding_text, encoding="utf-8")
        destination.joinpath(*graph_relative.split("/")).mkdir(parents=True)
        observed["clone"] = destination
        return manifest

    monkeypatch.setattr(pulse_graph_census_once, "_clone_copy", clone_once)
    monkeypatch.setattr(
        pulse_graph_census_once, "_validate_clone_commits", lambda *a, **k: None
    )
    monkeypatch.setattr(
        pulse_graph_census_once,
        "machine_sample",
        lambda **_kwargs: {"synthetic": True},
    )

    def spawn(argv: list[str], home: Path, pulse: Path, core: Path) -> Any:
        observed["spawns"] += 1
        assert home == observed["clone"]
        assert pulse == pins.pulse_root and core == pins.pulse_core_root
        output = Path(argv[argv.index("--out") + 1])
        output.write_text(
            json.dumps(
                _child_document(
                    args,
                    clone=home,
                    pins=pins,
                    digest=digest,
                    after_digest=after_digest,
                    lock_path=lock_path,
                    added_count=1,
                    file_count=2,
                    total_bytes=expected_bytes,
                )
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pulse_graph_census_once, "_spawn", spawn)
    monkeypatch.setattr(
        pulse_graph_census_once,
        "_watch",
        lambda process, poll, timeout: {
            "peak_rss_bytes_tree": 10,
            "peak_private_bytes_tree": 8,
            "samples": 1,
            "private_samples": 1,
            "timed_out": False,
        },
    )
    monkeypatch.setattr(
        pulse_graph_census_once,
        "inventory",
        lambda path, *, exclude: {
            "sha256": after_digest,
            "file_count": 3,
            "total_bytes": expected_bytes,
            "files": [stable_file, binding_file, lock_file],
        },
    )

    receipt = pulse_graph_census_once.census_once(args)

    assert observed["spawns"] == 1
    assert receipt["official"] is False
    assert receipt["results"]["one_shot"] is True
    assert receipt["results"]["copy"]["raw_unchanged"] is False
    assert (
        receipt["results"]["copy"]["unchanged_except_expected_participant_lock"] is True
    )
    assert receipt["results"]["supervision"]["wall_seconds_not_a_benchmark"] >= 0
    assert (args.out_dir / pulse_graph_census_once.RECEIPT_NAME).is_file()
    assert Path(
        str(args.out_dir / pulse_graph_census_once.RECEIPT_NAME) + ".sha256"
    ).is_file()
