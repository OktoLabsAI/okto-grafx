"""Focused tests for the round-0.0.2 instruments (P0.2): receipts, guards, copy proof, series."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from tools.perf_round import baseline_runs, board_copy, receipt

TEST_GRAFX_SHA = receipt.git_sha_of(receipt.PROJECT_ROOT)
assert TEST_GRAFX_SHA is not None

# A fake instrument: writes JSON with a metric derived from its run index and seed, no timing.
_FAKE_INSTRUMENT = (
    "import json, sys, pathlib; run = int(sys.argv[1]); out = pathlib.Path(sys.argv[2]); "
    "mode = sys.argv[3]; seed = int(sys.argv[4]); page = int(sys.argv[5]); budget = int(sys.argv[6]); "
    "checksum = sys.argv[7]; kind = sys.argv[8]; thermal = sys.argv[9]; copy = sys.argv[10] if len(sys.argv) > 10 else ''; "
    "import okto_grafx; "
    "out.write_text(json.dumps({'summary': {'wall_ms': 100.0 + run, 'nested': [{'v': 7}]}, "
    "'copy': copy, 'mode': mode, 'seed': seed, 'home': __import__('os').environ.get('DATA_DIR'), "
    "'grafx_file': okto_grafx.__file__, '_perf_round': {'python_executable': "
    "str(pathlib.Path(sys.executable).resolve()), 'okto_grafx_file': str(pathlib.Path(okto_grafx.__file__).resolve()), "
    "'mode': mode, 'seed': seed, 'kind': kind, 'page_size': page, "
    "'buffer_budget_bytes': budget, 'checksum': checksum, 'thermal': thermal, 'run': run, "
    "'effective_data_dir': str(pathlib.Path(__import__('os').environ['DATA_DIR']).resolve())}}))"
)


def _fake_argv(*extra: str) -> list[str]:
    return [
        sys.executable,
        "-c",
        _FAKE_INSTRUMENT,
        "{run}",
        "{out}",
        "{mode}",
        "{seed}",
        "{page_size}",
        "{buffer_budget_bytes}",
        "{checksum}",
        "{kind}",
        "{thermal}",
        *extra,
    ]


_FAKE_PULSE_INSTRUMENT = (
    "import json, os, pathlib, sys; run = int(sys.argv[1]); out = pathlib.Path(sys.argv[2]); "
    "mode = sys.argv[3]; seed = int(sys.argv[4]); page = int(sys.argv[5]); budget = int(sys.argv[6]); "
    "checksum = sys.argv[7]; kind = sys.argv[8]; thermal = sys.argv[9]; copy = sys.argv[10]; "
    "import okto_grafx, okto_pulse.community, okto_pulse.core; data_dir = pathlib.Path(os.environ['DATA_DIR']).resolve(); "
    "out.write_text(json.dumps({'summary': {'wall_ms': 1.0}, 'copy': copy, "
    "'_perf_round': {'python_executable': str(pathlib.Path(sys.executable).resolve()), "
    "'okto_grafx_file': str(pathlib.Path(okto_grafx.__file__).resolve()), "
    "'okto_pulse_community_file': str(pathlib.Path(okto_pulse.community.__file__).resolve()), "
    "'okto_pulse_core_file': str(pathlib.Path(okto_pulse.core.__file__).resolve()), "
    "'effective_data_dir': str(data_dir), 'mode': mode, 'seed': seed, 'kind': kind, "
    "'thermal': thermal, 'page_size': page, 'buffer_budget_bytes': budget, "
    "'checksum': checksum, 'run': run}}))"
)


def _fake_pulse_argv() -> list[str]:
    return [
        sys.executable,
        "-c",
        _FAKE_PULSE_INSTRUMENT,
        "{run}",
        "{out}",
        "{mode}",
        "{seed}",
        "{page_size}",
        "{buffer_budget_bytes}",
        "{checksum}",
        "{kind}",
        "{thermal}",
        "{copy}",
    ]


def _fake_pulse_roots(tmp_path: Path) -> tuple[Path, Path]:
    community = tmp_path / "community"
    core = tmp_path / "core"
    (community / "src" / "okto_pulse" / "community").mkdir(parents=True)
    (core / "src" / "okto_pulse" / "core").mkdir(parents=True)
    (community / "src" / "okto_pulse" / "community" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    (core / "src" / "okto_pulse" / "core" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    return community, core


@pytest.fixture(autouse=True)
def fast_machine_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this focused suite fast; sampling semantics are covered without a one-second sleep."""
    sample = {
        "psutil": "test",
        "cpu_percent": 0.0,
        "cpu_count": 1,
        "memory_total_bytes": 1,
        "memory_available_bytes": 1,
    }
    monkeypatch.setattr(
        receipt, "machine_sample", lambda interval_seconds=1.0: dict(sample)
    )
    monkeypatch.setattr(
        baseline_runs, "machine_sample", lambda interval_seconds=1.0: dict(sample)
    )


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every data-home variable at a directory under tmp_path so guards are testable."""
    home = tmp_path / "live-home"
    home.mkdir()
    for name in receipt.DATA_HOME_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(home))
    return home


def _board(root: Path, files: int = 3) -> Path:
    root.mkdir(parents=True)
    (root / "index").mkdir()
    for ordinal in range(files):
        (root / f"file{ordinal}.dat").write_bytes(bytes([ordinal]) * (128 + ordinal))
    (root / "index" / "pk.idx").write_bytes(b"idx" * 40)
    return root


# --- receipt ----------------------------------------------------------------------------------


def test_a_receipt_is_canonical_pure_json_with_a_stable_identity_and_a_volatile_digest(
    tmp_path: Path,
) -> None:
    kwargs = dict(
        tool=receipt.__file__,
        seed=7,
        parameters={"b": 2, "a": [1, 2]},
        series={"mode": "strict", "thermal": "warm", "kind": "raw"},
        config={"descriptor_revalidation": "strict"},
        inputs=[receipt.plain_input("input", tmp_path, inventory_digest=None)],
        results={"x": 1.5},
        machine={"cpu_percent": 3.0},
        timestamp="2026-09-03T00:00:00Z",
    )
    first = receipt.build_receipt(**kwargs)
    second = receipt.build_receipt(
        **dict(kwargs, results={"x": 2.5}, machine={"cpu_percent": 9.0})
    )
    assert receipt.identity_digest(first) == receipt.identity_digest(second)
    assert receipt.receipt_digest(first) != receipt.receipt_digest(second)
    written = receipt.write_receipt(tmp_path / "r.json", first)
    text = written.read_text(encoding="utf-8")
    assert text == receipt.canonical_json(json.loads(text))
    assert (tmp_path / "r.json.sha256").read_text().strip() == receipt.sha256_text(text)
    loaded = json.loads(text)
    assert loaded["official"] is False
    assert loaded["environment"]["okto_grafx"]["file"]
    assert (
        Path(loaded["environment"]["okto_grafx"]["file"])
        .resolve()
        .is_relative_to(receipt.PROJECT_ROOT)
    )
    assert loaded["identity_sha256"] == receipt.identity_digest(first)
    loaded["results"]["x"] = 99
    with pytest.raises(receipt.ReceiptInvalid, match="receipt_sha256"):
        receipt.validate_receipt(loaded)
    with pytest.raises(FileExistsError, match="overwrite"):
        receipt.write_receipt(written, first)


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda r: r.pop("seed"), "misses"),
        (lambda r: r.__setitem__("official", True), "official"),
        (lambda r: r["series"].__setitem__("kind", "fast"), "kind"),
        (
            lambda r: r["config"].__setitem__("descriptor_revalidation", "lenient"),
            "descriptor_revalidation",
        ),
        (
            lambda r: r["config"].__setitem__("descriptor_revalidation", "generation"),
            "must equal series.mode",
        ),
        (lambda r: r["results"].__setitem__("when", Path("x")), "not a JSON value"),
        (lambda r: r["results"].__setitem__("nan", float("nan")), "NaN"),
        (lambda r: r["inputs"][0].__setitem__("declared_copy", "yes"), "boolean"),
    ],
)
def test_validation_refuses_what_could_not_answer_the_policy(
    tmp_path: Path, mutation, message
) -> None:
    built = receipt.build_receipt(
        tool=receipt.__file__,
        seed=None,
        parameters={},
        series={"mode": "strict", "thermal": "cold", "kind": "instrumented"},
        config={"descriptor_revalidation": "strict"},
        inputs=[receipt.plain_input("input", tmp_path, inventory_digest=None)],
        machine={},
    )
    mutation(built)
    with pytest.raises(receipt.ReceiptInvalid, match=message):
        receipt.validate_receipt(built)


# --- guards -----------------------------------------------------------------------------------


def test_the_data_home_is_refused_at_the_root_below_it_and_through_a_link(
    tmp_path: Path, fake_home: Path
) -> None:
    board = fake_home / "boards" / "b1"
    board.mkdir(parents=True)
    with pytest.raises(receipt.LiveBoardRefused):
        receipt.guard_not_data_home(fake_home)
    with pytest.raises(receipt.LiveBoardRefused):
        receipt.guard_not_data_home(board)
    with pytest.raises(receipt.LiveBoardRefused):
        receipt.guard_not_data_home(tmp_path)
    with pytest.raises(receipt.LiveBoardRefused):
        receipt.require_declared_copy(board)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert receipt.guard_not_data_home(outside) == outside.resolve()
    link = tmp_path / "link-to-home"
    try:
        os.symlink(fake_home, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable to this user")
    with pytest.raises(receipt.LiveBoardRefused):
        receipt.guard_not_data_home(link / "boards" / "b1")


def test_an_inventory_refuses_links_reparse_points_and_hard_links(
    tmp_path: Path,
) -> None:
    root = _board(tmp_path / "board")
    assert receipt.inventory(root)["file_count"] == 4
    hard = root / "alias.dat"
    try:
        os.link(root / "file0.dat", hard)
    except (OSError, NotImplementedError):
        hard = None
    if hard is not None:
        with pytest.raises(receipt.LiveBoardRefused, match="hard links"):
            receipt.inventory(root)
        hard.unlink()
    link = root / "index" / "escape"
    try:
        os.symlink(tmp_path, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable to this user")
    with pytest.raises(receipt.LiveBoardRefused, match="symlink"):
        receipt.inventory(root)


def test_an_inventory_never_silences_a_walk_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _board(tmp_path / "board")

    def denied_walk(path, *, followlinks, onerror):
        onerror(PermissionError(13, "denied", str(Path(path) / "hidden")))
        return []

    monkeypatch.setattr(receipt.os, "walk", denied_walk)
    with pytest.raises(receipt.LiveBoardRefused, match="cannot inventory"):
        receipt.inventory(root)


def test_a_malformed_manifest_is_a_typed_refusal(tmp_path: Path) -> None:
    root = _board(tmp_path / "copy")
    manifest = root / receipt.COPY_MANIFEST_NAME
    sidecar = root / (receipt.COPY_MANIFEST_NAME + ".sha256")

    def put(content: str) -> None:
        manifest.write_bytes(content.encode("utf-8"))
        sidecar.write_text(receipt.sha256_file(manifest) + "\n", encoding="ascii")

    for content in (
        "{not json",
        "[]",
        json.dumps({"schema": "other"}),
        json.dumps({"schema": receipt.COPY_MANIFEST_SCHEMA, "declared_copy": "yes"}),
    ):
        put(content)
        with pytest.raises(receipt.LiveBoardRefused):
            receipt.require_declared_copy(root)
    put(
        json.dumps(
            {
                "schema": receipt.COPY_MANIFEST_SCHEMA,
                "declared_copy": True,
                "byte_identical": True,
                "copy": {
                    "path": str(root),
                    "sha256": "0" * 64,
                    "file_count": 4,
                    "total_bytes": 510,
                },
            }
        )
    )
    with pytest.raises(receipt.LiveBoardRefused, match="no longer hashes"):
        receipt.require_declared_copy(root)
    manifest.write_text("tampered", encoding="utf-8")
    with pytest.raises(receipt.LiveBoardRefused, match="does not match its sidecar"):
        receipt.require_declared_copy(root)


# --- board copy -------------------------------------------------------------------------------


def test_a_copy_is_proved_byte_identical_declared_and_refused_once_touched(
    tmp_path: Path,
) -> None:
    source = _board(tmp_path / "source")
    dest = tmp_path / "copies" / "c1"
    manifest = board_copy.copy_board(source, dest, declare_copy=True, drained=False)
    assert manifest["byte_identical"] is True
    assert manifest["copy"]["file_count"] == 4
    assert not list(tmp_path.glob("copies/c1.tmp-*"))
    proved = receipt.require_declared_copy(dest)
    assert proved["copy"]["sha256"] == manifest["copy"]["sha256"]
    assert receipt.declared_copy_input("copy", dest)["declared_copy"] is True
    (dest / "file1.dat").write_bytes(b"changed")
    with pytest.raises(receipt.LiveBoardRefused, match="no longer hashes"):
        receipt.require_declared_copy(dest)


def test_a_declared_copy_requires_the_complete_stable_source_proof(
    tmp_path: Path,
) -> None:
    source = _board(tmp_path / "source")
    dest = tmp_path / "copy"
    board_copy.copy_board(source, dest, declare_copy=True, drained=False)
    manifest_path = dest / receipt.COPY_MANIFEST_NAME
    sidecar_path = dest / (receipt.COPY_MANIFEST_NAME + ".sha256")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("source")
    text = receipt.canonical_json(manifest)
    manifest_path.write_bytes(text.encode("utf-8"))
    sidecar_path.write_text(receipt.sha256_file(manifest_path) + "\n", encoding="ascii")
    with pytest.raises(receipt.LiveBoardRefused, match="source proof"):
        receipt.require_declared_copy(dest)


def test_the_copier_refuses_an_existing_destination_an_empty_source_and_the_live_home(
    tmp_path: Path, fake_home: Path
) -> None:
    source = _board(tmp_path / "source")
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(receipt.LiveBoardRefused, match="already exists"):
        board_copy.copy_board(source, existing, declare_copy=True, drained=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(receipt.LiveBoardRefused, match="no files"):
        board_copy.copy_board(
            empty, tmp_path / "empty-copy", declare_copy=True, drained=False
        )
    with pytest.raises(receipt.LiveBoardRefused, match="mandatory"):
        board_copy.copy_board(
            source, tmp_path / "undeclared", declare_copy=False, drained=False
        )
    live = _board(fake_home / "boards" / "live")
    with pytest.raises(receipt.LiveBoardRefused, match="live data home"):
        board_copy.copy_board(
            live, tmp_path / "from-live", declare_copy=True, drained=False
        )
    with pytest.raises(receipt.LiveBoardRefused, match="contains live data home"):
        board_copy.copy_board(
            tmp_path,
            tmp_path.parent / "too-broad-copy",
            declare_copy=True,
            drained=True,
        )
    with pytest.raises(receipt.LiveBoardRefused):
        board_copy.copy_board(
            source, fake_home / "into-live", declare_copy=True, drained=False
        )
    # After a drain the operator may state it, and the copy is still proved outside the home.
    manifest = board_copy.copy_board(
        live, tmp_path / "from-drained", declare_copy=True, drained=True
    )
    assert manifest["byte_identical"] is True
    assert manifest["source"]["inside_data_home"] is True


def test_the_copier_cli_writes_a_receipt_whose_copy_input_is_derived_from_the_proof(
    tmp_path: Path,
) -> None:
    source = _board(tmp_path / "source")
    dest = tmp_path / "c2"
    receipt_path = tmp_path / "c2.receipt.json"
    assert (
        board_copy.main(
            [
                "--source",
                str(source),
                "--dest",
                str(dest),
                "--declare-copy",
                "--receipt",
                str(receipt_path),
                "--grafx-sha",
                "test-grafx-sha",
                "--pulse-sha",
                "test-pulse-sha",
                "--pulse-core-sha",
                "test-core-sha",
            ]
        )
        == 0
    )
    written = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.validate_receipt(written)
    roles = {item["role"]: item for item in written["inputs"]}
    assert roles["copy"]["declared_copy"] is True
    assert roles["source"]["declared_copy"] is False
    assert roles["copy"]["inventory_sha256"] == roles["source"]["inventory_sha256"]
    assert written["commits"]["pulse_core"] == "test-core-sha"


def test_a_source_change_discards_staging_and_never_publishes_a_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _board(tmp_path / "moving-source")
    dest = tmp_path / "copies" / "discarded"
    original_copy2 = board_copy.shutil.copy2
    copied = 0

    def mutate_after_first_copy(src, dst, *, follow_symlinks=True):
        nonlocal copied
        result = original_copy2(src, dst, follow_symlinks=follow_symlinks)
        copied += 1
        if copied == 1:
            (source / "file0.dat").write_bytes(b"source moved")
        return result

    monkeypatch.setattr(board_copy.shutil, "copy2", mutate_after_first_copy)
    with pytest.raises(board_copy.CopyNotProved) as failure:
        board_copy.copy_board(source, dest, declare_copy=True, drained=False)
    assert failure.value.manifest["byte_identical"] is False
    assert not dest.exists()
    assert not list(dest.parent.glob(f"{dest.name}.tmp-*"))


def test_copy_receipt_location_is_external_and_fresh(
    tmp_path: Path, fake_home: Path
) -> None:
    source = _board(tmp_path / "source")
    dest = tmp_path / "dest"
    with pytest.raises(receipt.LiveBoardRefused, match="inside the source"):
        board_copy.receipt_location(source / "receipt.json", source, dest)
    with pytest.raises(receipt.LiveBoardRefused, match="inside the destination"):
        board_copy.receipt_location(dest / "receipt.json", source, dest)
    with pytest.raises(receipt.LiveBoardRefused, match="live data home"):
        board_copy.receipt_location(fake_home / "receipt.json", source, dest)
    occupied = tmp_path / "occupied.json"
    occupied.write_text("old", encoding="utf-8")
    with pytest.raises(receipt.LiveBoardRefused, match="already exists"):
        board_copy.receipt_location(occupied, source, dest)


# --- baseline runs ----------------------------------------------------------------------------


def test_a_series_runs_warmup_plus_three_fresh_processes_and_reports_a_distribution(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "series"
    result = baseline_runs.run_series(
        mode="strict",
        thermal="warm",
        runs=3,
        warmup=1,
        argv_template=_fake_argv(),
        metrics={"wall_ms": "summary.wall_ms", "v": "summary.nested.0.v"},
        out_dir=out_dir,
        seed=1,
        timeout_seconds=120.0,
        inputs=[],
        declared_copy=None,
        copy_policy="clone",
        kind="raw",
        max_spread=0.08,
        machine_idle_asserted=False,
        grafx_sha=TEST_GRAFX_SHA,
        pulse_sha="n/a",
        page_size=8192,
        buffer_budget_bytes=64 * 1024 * 1024,
        checksum="auto",
        instrument_paths=[Path(__file__)],
    )
    results = result["results"]
    assert [run["role"] for run in results["runs"]] == [
        "warmup",
        "measured",
        "measured",
        "measured",
    ]
    assert all(
        run["thermal"] == "warm" and run["process"] == "fresh"
        for run in results["runs"]
    )
    wall = results["aggregates"]["wall_ms"]
    assert wall["values"] == [
        101.0,
        102.0,
        103.0,
    ]  # run 0 (100.0) was the discarded warmup
    assert wall["median"] == 102.0 and wall["min"] == 101.0 and wall["max"] == 103.0
    assert results["aggregates"]["v"]["values"] == [7.0, 7.0, 7.0]
    assert results["inconclusive_metrics"] == []
    assert (out_dir / "strict_series_receipt.json").is_file()
    written = json.loads(
        (out_dir / "strict_series_receipt.json").read_text(encoding="utf-8")
    )
    receipt.validate_receipt(written)
    assert written["series"] == {"mode": "strict", "thermal": "warm", "kind": "raw"}
    assert written["config"] == {
        "descriptor_revalidation": "strict",
        "page_size": 8192,
        "buffer_budget_bytes": 64 * 1024 * 1024,
        "checksum": "auto",
    }
    # The child cannot fall back to the default: every data-home variable names its run home.
    first = json.loads((out_dir / "strict_run01.json").read_text(encoding="utf-8"))
    assert first["home"] == results["runs"][1]["data_home"]
    assert first["home"] != str(Path.home() / receipt.DEFAULT_DATA_HOME_NAME)
    assert Path(first["grafx_file"]).resolve().is_relative_to(baseline_runs.SOURCE_ROOT)
    assert first["mode"] == "strict" and first["seed"] == 1


def test_a_wide_spread_recommends_exactly_one_more_run_and_never_loops(
    tmp_path: Path,
) -> None:
    result = baseline_runs.run_series(
        mode="generation",
        thermal="cold",
        runs=3,
        warmup=0,
        argv_template=_fake_argv(),
        metrics={"wall_ms": "summary.wall_ms"},
        out_dir=tmp_path / "wide",
        seed=1,
        timeout_seconds=120.0,
        inputs=[],
        declared_copy=None,
        copy_policy="clone",
        kind="raw",
        max_spread=0.001,
        machine_idle_asserted=True,
        grafx_sha=TEST_GRAFX_SHA,
        pulse_sha="n/a",
        page_size=8192,
        buffer_budget_bytes=64 * 1024 * 1024,
        checksum="auto",
        instrument_paths=[Path(__file__)],
    )
    results = result["results"]
    assert len(results["runs"]) == 3
    assert results["inconclusive_metrics"] == ["wall_ms"]
    assert "one more" in results["recommendation"]
    assert result["machine"]["idle_asserted_by_operator"] is True


def test_a_series_binds_the_child_to_a_fresh_clone_of_the_declared_copy(
    tmp_path: Path,
) -> None:
    source = _board(tmp_path / "source")
    declared = tmp_path / "declared"
    board_copy.copy_board(source, declared, declare_copy=True, drained=False)
    out_dir = tmp_path / "bound"
    result = baseline_runs.run_series(
        mode="strict",
        thermal="warm",
        runs=3,
        warmup=0,
        argv_template=_fake_argv("{copy}"),
        metrics={"wall_ms": "summary.wall_ms"},
        out_dir=out_dir,
        seed=0,
        timeout_seconds=120.0,
        inputs=[],
        declared_copy=declared,
        copy_policy="clone",
        kind="raw",
        max_spread=0.5,
        machine_idle_asserted=False,
        grafx_sha=TEST_GRAFX_SHA,
        pulse_sha="n/a",
        page_size=8192,
        buffer_budget_bytes=64 * 1024 * 1024,
        checksum="auto",
        instrument_paths=[Path(__file__)],
    )
    runs = result["results"]["runs"]
    clones = {run["copy"]["path"] for run in runs}
    assert len(clones) == 3
    for run in runs:
        clone = Path(run["copy"]["path"])
        assert clone.parent == out_dir
        receipt.require_declared_copy(clone)
        assert json.loads(Path(run["output"]).read_text(encoding="utf-8"))[
            "copy"
        ] == str(clone)
    roles = [item["role"] for item in result["inputs"]]
    assert "declared_copy" in roles and "instrument" in roles
    assert any(
        item.get("inline") and item["bound_to_argv"]
        for item in result["results"]["instruments"]
    )


def test_a_pulse_series_pins_community_and_core_and_uses_each_clone_as_data_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    community, core = _fake_pulse_roots(tmp_path)
    shas = {community.resolve(): "community-sha", core.resolve(): "core-sha"}
    monkeypatch.setattr(
        baseline_runs, "git_sha_of", lambda path: shas.get(Path(path).resolve())
    )
    monkeypatch.setattr(baseline_runs, "_source_status", lambda *_args: [])
    source = _board(tmp_path / "source-home")
    declared = tmp_path / "declared-home"
    board_copy.copy_board(
        source,
        declared,
        declare_copy=True,
        drained=False,
        grafx_sha=TEST_GRAFX_SHA,
        pulse_sha="community-sha",
        pulse_core_sha="core-sha",
    )

    result = baseline_runs.run_series(
        mode="strict",
        thermal="warm",
        runs=3,
        warmup=0,
        argv_template=_fake_pulse_argv(),
        metrics={"wall_ms": "summary.wall_ms"},
        out_dir=tmp_path / "pulse-series",
        seed=4,
        timeout_seconds=120.0,
        inputs=[],
        declared_copy=declared,
        copy_policy="clone",
        data_home_policy="declared-copy-clone",
        kind="raw",
        max_spread=0.5,
        machine_idle_asserted=False,
        grafx_sha=TEST_GRAFX_SHA,
        pulse_sha="community-sha",
        pulse_root=community,
        pulse_core_sha="core-sha",
        pulse_core_root=core,
        page_size=8192,
        buffer_budget_bytes=64 * 1024 * 1024,
        checksum="auto",
    )

    assert result["commits"] == {
        "grafx": TEST_GRAFX_SHA,
        "pulse": "community-sha",
        "pulse_core": "core-sha",
    }
    for run in result["results"]["runs"]:
        assert run["data_home"] == run["copy"]["path"]
        child = json.loads(Path(run["output"]).read_text(encoding="utf-8"))
        assert child["copy"] == run["data_home"]
        assert child["_perf_round"]["effective_data_dir"] == run["data_home"]
        assert child["_perf_round"]["thermal"] == "warm"
    assert result["results"]["expected_child_provenance"][
        "okto_pulse_community_file"
    ] == str(
        (community / "src" / "okto_pulse" / "community" / "__init__.py").resolve()
    )
    assert result["results"]["expected_child_provenance"][
        "okto_pulse_core_file"
    ] == str((core / "src" / "okto_pulse" / "core" / "__init__.py").resolve())


def test_a_pulse_series_refuses_unpinned_core_or_a_non_clone_data_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    community, core = _fake_pulse_roots(tmp_path)
    shas = {community.resolve(): "community-sha", core.resolve(): "core-sha"}
    monkeypatch.setattr(
        baseline_runs, "git_sha_of", lambda path: shas.get(Path(path).resolve())
    )
    monkeypatch.setattr(baseline_runs, "_source_status", lambda *_args: [])
    source = _board(tmp_path / "source-home")
    declared = tmp_path / "declared-home"
    board_copy.copy_board(source, declared, declare_copy=True, drained=False)
    common = dict(
        mode="strict",
        thermal="warm",
        runs=3,
        warmup=0,
        argv_template=_fake_pulse_argv(),
        metrics={"wall_ms": "summary.wall_ms"},
        seed=0,
        timeout_seconds=10.0,
        inputs=[],
        declared_copy=declared,
        copy_policy="clone",
        kind="raw",
        max_spread=0.5,
        machine_idle_asserted=False,
        grafx_sha=TEST_GRAFX_SHA,
        pulse_sha="community-sha",
        pulse_root=community,
        page_size=8192,
        buffer_budget_bytes=64 * 1024 * 1024,
        checksum="auto",
    )
    with pytest.raises(baseline_runs.RunRefused, match="pulse-core-root"):
        baseline_runs.run_series(
            out_dir=tmp_path / "missing-core", data_home_policy="isolated", **common
        )
    with pytest.raises(baseline_runs.RunRefused, match="declared-copy-clone"):
        baseline_runs.run_series(
            out_dir=tmp_path / "empty-home",
            data_home_policy="isolated",
            pulse_core_sha="core-sha",
            pulse_core_root=core,
            **common,
        )
    with pytest.raises(baseline_runs.RunRefused, match="copy-policy clone"):
        baseline_runs.run_series(
            out_dir=tmp_path / "shared-home",
            data_home_policy="declared-copy-clone",
            pulse_core_sha="core-sha",
            pulse_core_root=core,
            **dict(common, copy_policy="verify"),
        )


def test_a_series_refuses_unbound_copies_stale_outputs_and_missing_timeouts(
    tmp_path: Path, fake_home: Path
) -> None:
    common = dict(
        mode="strict",
        thermal="warm",
        runs=3,
        warmup=0,
        metrics={"wall_ms": "summary.wall_ms"},
        seed=0,
        inputs=[],
        copy_policy="clone",
        kind="raw",
        max_spread=0.5,
        machine_idle_asserted=False,
        grafx_sha=TEST_GRAFX_SHA,
        pulse_sha="n/a",
        page_size=8192,
        buffer_budget_bytes=64 * 1024 * 1024,
        checksum="auto",
        instrument_paths=[Path(__file__)],
    )
    source = _board(tmp_path / "source")
    declared = tmp_path / "declared"
    board_copy.copy_board(source, declared, declare_copy=True, drained=False)
    with pytest.raises(baseline_runs.RunRefused, match="never references"):
        baseline_runs.run_series(
            argv_template=_fake_argv(),
            out_dir=tmp_path / "a",
            timeout_seconds=10.0,
            declared_copy=declared,
            **common,
        )
    with pytest.raises(baseline_runs.RunRefused, match="runs must be"):
        baseline_runs.run_series(
            argv_template=_fake_argv(),
            out_dir=tmp_path / "b",
            timeout_seconds=10.0,
            declared_copy=None,
            **dict(common, runs=2),
        )
    with pytest.raises(baseline_runs.RunRefused, match="finite"):
        baseline_runs.run_series(
            argv_template=_fake_argv(),
            out_dir=tmp_path / "c",
            timeout_seconds=float("inf"),
            declared_copy=None,
            **common,
        )
    with pytest.raises(receipt.LiveBoardRefused, match="live data home"):
        baseline_runs.run_series(
            argv_template=_fake_argv(str(fake_home)),
            out_dir=tmp_path / "d",
            timeout_seconds=10.0,
            declared_copy=None,
            **common,
        )
    with pytest.raises(receipt.LiveBoardRefused, match="live data home"):
        baseline_runs.run_series(
            argv_template=_fake_argv("~/.okto-pulse/boards/live"),
            out_dir=tmp_path / "tilde-live",
            timeout_seconds=10.0,
            declared_copy=None,
            **common,
        )
    stale_dir = tmp_path / "e"
    stale_dir.mkdir()
    (stale_dir / "strict_run00.json").write_text("{}", encoding="utf-8")
    with pytest.raises(baseline_runs.RunRefused, match="already exists"):
        baseline_runs.run_series(
            argv_template=_fake_argv(),
            out_dir=stale_dir,
            timeout_seconds=10.0,
            declared_copy=None,
            **common,
        )
    receipt_dir = tmp_path / "f"
    receipt_dir.mkdir()
    (receipt_dir / "strict_series_receipt.json.sha256").write_text(
        "occupied", encoding="ascii"
    )
    with pytest.raises(baseline_runs.RunRefused, match="series receipt"):
        baseline_runs.run_series(
            argv_template=_fake_argv(),
            out_dir=receipt_dir,
            timeout_seconds=10.0,
            declared_copy=None,
            **common,
        )
    with pytest.raises(baseline_runs.RunRefused, match="poll-seconds"):
        baseline_runs.run_series(
            argv_template=_fake_argv(),
            out_dir=tmp_path / "g",
            timeout_seconds=10.0,
            declared_copy=None,
            poll_seconds=0,
            **common,
        )
    with pytest.raises(baseline_runs.RunRefused, match="max-spread"):
        baseline_runs.run_series(
            argv_template=_fake_argv(),
            out_dir=tmp_path / "h",
            timeout_seconds=10.0,
            declared_copy=None,
            **dict(common, max_spread=float("nan")),
        )
    with pytest.raises(baseline_runs.RunRefused, match="overlaps declared copy"):
        baseline_runs.run_series(
            argv_template=_fake_argv("{copy}"),
            out_dir=declared / "evidence",
            timeout_seconds=10.0,
            declared_copy=declared,
            **common,
        )
    assert not (declared / "evidence").exists()
    missing_seed = [token for token in _fake_argv() if token != "{seed}"]
    with pytest.raises(baseline_runs.RunRefused, match="seed"):
        baseline_runs.run_series(
            argv_template=missing_seed,
            out_dir=tmp_path / "i",
            timeout_seconds=10.0,
            declared_copy=None,
            **common,
        )


def test_a_series_requires_the_actual_instrument_to_be_hashed(tmp_path: Path) -> None:
    with pytest.raises(baseline_runs.RunRefused, match="direct .py script"):
        baseline_runs.run_series(
            mode="strict",
            thermal="warm",
            runs=3,
            warmup=0,
            argv_template=[
                sys.executable,
                "--version",
                "{out}",
                "{run}",
                "{mode}",
                "{seed}",
                "{page_size}",
                "{buffer_budget_bytes}",
                "{checksum}",
                "{kind}",
                "{thermal}",
            ],
            metrics={"wall_ms": "summary.wall_ms"},
            out_dir=tmp_path / "no-instrument",
            seed=0,
            timeout_seconds=10.0,
            inputs=[],
            declared_copy=None,
            copy_policy="clone",
            kind="raw",
            max_spread=0.5,
            machine_idle_asserted=False,
            grafx_sha=TEST_GRAFX_SHA,
            pulse_sha="n/a",
            page_size=8192,
            buffer_budget_bytes=64 * 1024 * 1024,
            checksum="auto",
        )


def test_a_decoy_python_file_cannot_bind_an_unhashed_module_instrument() -> None:
    argv = [
        sys.executable,
        "-m",
        "unhashed.module",
        str(Path(__file__)),
        "{out}",
        "{run}",
        "{mode}",
        "{seed}",
        "{page_size}",
        "{buffer_budget_bytes}",
        "{checksum}",
        "{kind}",
        "{thermal}",
    ]
    with pytest.raises(baseline_runs.RunRefused, match="may not use -m"):
        baseline_runs._guard_argv(argv, has_copy=False)


def test_a_timed_out_or_broken_run_is_an_error_not_a_sample(tmp_path: Path) -> None:
    hanging = [
        sys.executable,
        "-c",
        "import time, sys; time.sleep(30)",
        "{out}",
        "{run}",
        "{mode}",
        "{seed}",
        "{page_size}",
        "{buffer_budget_bytes}",
        "{checksum}",
        "{kind}",
        "{thermal}",
    ]
    result = baseline_runs.run_series(
        mode="strict",
        thermal="mixed",
        runs=3,
        warmup=0,
        argv_template=hanging,
        metrics={"wall_ms": "summary.wall_ms"},
        out_dir=tmp_path / "hang",
        seed=0,
        timeout_seconds=1.5,
        inputs=[],
        declared_copy=None,
        copy_policy="clone",
        kind="raw",
        max_spread=0.5,
        machine_idle_asserted=False,
        grafx_sha=TEST_GRAFX_SHA,
        pulse_sha="n/a",
        page_size=8192,
        buffer_budget_bytes=64 * 1024 * 1024,
        checksum="auto",
        poll_seconds=0.2,
        instrument_paths=[Path(__file__)],
    )
    runs = result["results"]["runs"]
    assert all(run["memory"]["timed_out"] for run in runs)
    assert all(any("timed out" in error for error in run["errors"]) for run in runs)
    assert all(
        run["memory"]["peak_private_bytes_tree"] is None
        or run["memory"]["peak_private_bytes_tree"] > 0
        for run in runs
    )
    assert result["results"]["aggregates"]["wall_ms"]["error"].startswith("fewer than")
    broken = [
        sys.executable,
        "-c",
        "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text('{not json')",
        "{out}",
        "{run}",
        "{mode}",
        "{seed}",
        "{page_size}",
        "{buffer_budget_bytes}",
        "{checksum}",
        "{kind}",
        "{thermal}",
    ]
    result = baseline_runs.run_series(
        mode="strict",
        thermal="mixed",
        runs=3,
        warmup=0,
        argv_template=broken,
        metrics={"wall_ms": "summary.wall_ms"},
        out_dir=tmp_path / "broken",
        seed=0,
        timeout_seconds=30.0,
        inputs=[],
        declared_copy=None,
        copy_policy="clone",
        kind="raw",
        max_spread=0.5,
        machine_idle_asserted=False,
        grafx_sha=TEST_GRAFX_SHA,
        pulse_sha="n/a",
        page_size=8192,
        buffer_budget_bytes=64 * 1024 * 1024,
        checksum="auto",
        instrument_paths=[Path(__file__)],
    )
    assert all(
        any("not valid JSON" in error for error in run["errors"])
        for run in result["results"]["runs"]
    )


def test_timeout_terminates_descendants_before_they_can_write_late_output(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "late.txt"
    grandchild = (
        "import pathlib,sys,time; time.sleep(1.5); "
        "pathlib.Path(sys.argv[1]).write_text('late')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, {str(sentinel)!r}]); "
        "time.sleep(30)"
    )
    run_home = tmp_path / "isolated-home"
    run_home.mkdir()
    process = baseline_runs._spawn([sys.executable, "-c", parent], run_home)
    watched = baseline_runs._watch(process, poll_seconds=0.05, timeout_seconds=0.3)
    assert watched["timed_out"] is True
    time.sleep(1.7)
    assert not sentinel.exists()


def test_posix_timeout_targets_the_known_process_group_and_proves_it_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 4242

        def kill(self) -> None:
            pass

        def wait(self, timeout: float) -> int:
            assert timeout == 10
            return 0

    calls: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        calls.append((pgid, sig))
        if sig == 0:
            raise ProcessLookupError

    with monkeypatch.context() as patch:
        patch.setattr(baseline_runs.os, "name", "posix")
        patch.setattr(baseline_runs.os, "killpg", fake_killpg, raising=False)
        patch.setattr(baseline_runs.signal, "SIGKILL", 9, raising=False)
        patch.setattr(
            baseline_runs.os,
            "getpgid",
            lambda _pid: pytest.fail("getpgid reintroduces the leader-exit race"),
            raising=False,
        )
        baseline_runs._terminate_process_tree(  # type: ignore[arg-type]
            FakeProcess(), root=None
        )

    assert calls == [
        (FakeProcess.pid, 9),
        (FakeProcess.pid, 0),
    ]


def test_placeholders_are_whole_tokens_and_braces_elsewhere_survive() -> None:
    template = ["python", "x.py", "--json", '{"a": {run}}', "{out}", "{run}"]
    rendered = baseline_runs.render_argv(
        template, out="O", run="3", mode="strict", seed="0", copy=""
    )
    assert rendered == ["python", "x.py", "--json", '{"a": {run}}', "O", "3"]


def test_statistics_use_nearest_rank_and_refuse_invalid_samples() -> None:
    values = [float(value) for value in range(1, 31)]
    summary = baseline_runs.summarize(values)
    assert summary["p50"] == 15.0
    assert summary["p90"] == 27.0
    assert summary["p99"] == 30.0
    assert baseline_runs.summarize([0.0, 0.0, 0.0])["relative_spread"] == 0.0
    zero_median = baseline_runs.summarize([0.0, 0.0, 1.0])
    assert zero_median["relative_spread"] is None
    assert "spread=undefined" in baseline_runs.format_summary("metric", zero_median)
    for invalid in (True, float("nan"), float("inf"), -1):
        with pytest.raises((TypeError, ValueError)):
            baseline_runs.extract({"metric": invalid}, "metric")


def test_child_provenance_is_required_and_exact() -> None:
    expected = {
        "python_executable": str(Path(sys.executable).resolve()),
        "okto_grafx_file": str(
            receipt.SOURCE_IMPORT_ROOT / "okto_grafx" / "__init__.py"
        ),
        "mode": "strict",
        "seed": 7,
        "thermal": "warm",
        "effective_data_dir": str(Path.cwd().resolve()),
    }
    assert baseline_runs.validate_child_provenance({}, expected)
    observed = {"_perf_round": dict(expected)}
    assert baseline_runs.validate_child_provenance(observed, expected) == []
    observed["_perf_round"]["seed"] = 8
    assert "seed=8" in baseline_runs.validate_child_provenance(observed, expected)[0]
    observed["_perf_round"].update(seed=7, thermal="cold")
    assert "thermal='cold'" in baseline_runs.validate_child_provenance(
        observed, expected
    )[0]
