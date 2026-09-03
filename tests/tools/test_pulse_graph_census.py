"""Focused synthetic contracts for the authenticated P0.3 Pulse graph census."""

from __future__ import annotations

import argparse
import json
import os
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import okto_grafx
from okto_grafx.domain.model.record import RecordHeader
from okto_grafx.runtime.config import DatabaseConfig
from tools.perf_round import pulse_graph_census
from tools.perf_round.pulse_graph_census import (
    GraphCensusRefused,
    _inventory_matches,
    _open_and_collect,
    _require_authenticated_grafx_binding,
    _validate_runner_parent,
    collect_heap_census,
)

BOARD_ID = "00000000-0000-0000-0000-000000000001"


class _Table:
    def __init__(self, name: str) -> None:
        self.name = name


class _Heap:
    def __init__(
        self,
        populations: dict[_Table, tuple[tuple[int, ...], tuple[tuple[int, RecordHeader], ...]]],
        *,
        copied_content: bytes = b"",
    ) -> None:
        self._populations = populations
        self._copied_content = copied_content

    def pages_of(self, table: _Table) -> tuple[int, ...]:
        return self._populations[table][0]

    def _walk(
        self, table: _Table, *, copy_content: bool
    ) -> Any:
        assert copy_content is False
        for page, header in self._populations[table][1]:
            yield SimpleNamespace(page=page, slot=987_654_321), header, self._copied_content


class _Database:
    def __init__(
        self,
        graph_path: Path,
        *,
        records_checked: int = 2,
        recovery_report: object | None = None,
        copied_content: bytes = b"",
    ) -> None:
        first = _Table("secret-person-table")
        second = _Table("secret-edge-table")
        self._tables = (first, second)
        self._heap = _Heap(
            {
                first: (
                    (900_000_001, 17),
                    (
                        (
                            900_000_001,
                            RecordHeader(record_id=7_777_777_777, xmin=800_000_001),
                        ),
                    ),
                ),
                second: (
                    (81,),
                    (
                        (
                            81,
                            RecordHeader(
                                record_id=8_888_888_888,
                                xmin=800_000_002,
                                xmax=800_000_003,
                            ),
                        ),
                    ),
                ),
            },
            copied_content=copied_content,
        )
        self.catalog = SimpleNamespace(
            catalog=SimpleNamespace(tables=lambda: self._tables)
        )
        self.vectors = SimpleNamespace(
            spaces=lambda: (SimpleNamespace(name="secret-space"),),
            indexes=lambda: (
                SimpleNamespace(name="secret-vector-one", stale=False),
                SimpleNamespace(name="secret-vector-two", stale=True),
            ),
        )
        self.stale_indexes = ("secret-vector-two", "secret-exact")
        self.read_only = True
        self.recovery_report = recovery_report
        self.identity = SimpleNamespace(page_size=8192)
        self.pool = SimpleNamespace(budget_bytes=64 * 1024 * 1024)
        self.descriptor_revalidation = "strict"
        self.path = str(graph_path)
        self.maintenance = SimpleNamespace(
            status=lambda: SimpleNamespace(recovery_required=False)
        )
        self.closed = False
        self.close_complete = False
        self._records_checked = records_checked

    def verify(self, scope: str) -> Any:
        return SimpleNamespace(
            scope=scope,
            clean=True,
            findings=(),
            pages_checked=5 if scope == "pages" else 0,
            records_checked=self._records_checked if scope == "records" else 0,
        )

    def close(self) -> None:
        self.closed = True
        self.close_complete = True


def _args(copy: Path, out: Path) -> argparse.Namespace:
    return argparse.Namespace(
        copy=copy,
        out=out,
        board_id=BOARD_ID,
        run=0,
        mode="strict",
        seed=13,
        page_size=8192,
        buffer_budget_bytes=64 * 1024 * 1024,
        checksum="auto",
        kind="instrumented",
        thermal="mixed",
    )


def test_census_emits_only_aggregate_counts_and_reconciles_verifier(tmp_path: Path) -> None:
    result = collect_heap_census(_Database(tmp_path / "graph"))

    assert result["verification"] == {
        "pages": {"clean": True, "checked_total": 5},
        "records": {"clean": True, "checked_total": 2},
    }
    assert result["counts"] == {
        "tables_total": 2,
        "heap_pages_total": 3,
        "heap_record_slots_total": 2,
        "vector_spaces_total": 1,
        "vector_indexes_total": 2,
        "stale_indexes_total": 2,
        "stale_vector_indexes_total": 1,
    }
    encoded = json.dumps(result, sort_keys=True)
    for forbidden in (
        "secret-person-table",
        "secret-edge-table",
        "secret-space",
        "secret-vector-one",
        "secret-vector-two",
        "7777777777",
        "8888888888",
        "900000001",
        "800000001",
        "800000002",
        "800000003",
        "987654321",
        "record_id",
        "page_id",
        "slot_id",
    ):
        assert forbidden not in encoded


def test_census_refuses_a_dirty_or_empty_verification(tmp_path: Path) -> None:
    database = _Database(tmp_path / "graph")
    database.verify = lambda scope: SimpleNamespace(
        scope=scope,
        clean=False,
        findings=(object(),),
        pages_checked=1,
        records_checked=1,
    )
    with pytest.raises(GraphCensusRefused, match="verification is not clean"):
        collect_heap_census(database)


def test_census_refuses_a_population_that_does_not_reconcile(tmp_path: Path) -> None:
    with pytest.raises(GraphCensusRefused, match="does not reconcile"):
        collect_heap_census(_Database(tmp_path / "graph", records_checked=3))


def test_header_walk_must_not_copy_payload_content(tmp_path: Path) -> None:
    with pytest.raises(GraphCensusRefused, match="copied record content"):
        collect_heap_census(_Database(tmp_path / "graph", copied_content=b"secret"))


def test_binding_must_be_authenticated_grafx_inside_clone(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    graph = clone / "boards" / BOARD_ID / "grafx" / "generation"
    graph.mkdir(parents=True)
    valid = SimpleNamespace(
        scope="board",
        scope_id=BOARD_ID,
        backend="grafx",
        page_size=8192,
        physical_path=graph,
    )
    assert (
        _require_authenticated_grafx_binding(
            valid,
            clone_root=clone,
            board_id=BOARD_ID,
            page_size=8192,
        )
        == graph.resolve()
    )
    with pytest.raises(GraphCensusRefused, match="Grafx geometry"):
        _require_authenticated_grafx_binding(
            SimpleNamespace(**{**vars(valid), "backend": "ladybug"}),
            clone_root=clone,
            board_id=BOARD_ID,
            page_size=8192,
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(GraphCensusRefused, match="descendant"):
        _require_authenticated_grafx_binding(
            SimpleNamespace(**{**vars(valid), "physical_path": outside}),
            clone_root=clone,
            board_id=BOARD_ID,
            page_size=8192,
        )


def test_open_is_read_only_recovery_refusing_admitted_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = tmp_path / "graph"
    graph.mkdir()
    database = _Database(graph)
    observed: dict[str, Any] = {}

    def connect(path: Path, **options: object) -> _Database:
        observed["path"] = path
        observed["options"] = options
        return database

    def admit(opened: _Database, path: Path, args: argparse.Namespace) -> None:
        observed["admitted"] = (opened is database, path == graph, args.mode)

    monkeypatch.setattr(okto_grafx, "connect", connect)
    monkeypatch.setattr(pulse_graph_census, "_admit_database", admit)

    result = _open_and_collect(graph, _args(tmp_path / "clone", tmp_path / "out"))

    assert result["counts"]["heap_record_slots_total"] == 2
    assert observed["path"] == graph
    assert observed["options"] == {
        "read_only": True,
        "recovery_policy": "refuse",
        "page_size": 8192,
        "buffer_budget_bytes": 64 * 1024 * 1024,
        "checksum": "auto",
        "descriptor_revalidation": "strict",
    }
    DatabaseConfig(path=str(graph), **observed["options"])
    assert observed["admitted"] == (True, True, "strict")
    assert database.closed is True and database.close_complete is True


def test_open_refuses_any_recovery_report_and_still_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = tmp_path / "graph"
    graph.mkdir()
    database = _Database(graph, recovery_report=object())
    monkeypatch.setattr(
        pulse_graph_census, "_connect_read_only", lambda _path, _args: database
    )
    monkeypatch.setattr(
        pulse_graph_census, "_admit_database", lambda _database, _path, _args: None
    )
    with pytest.raises(GraphCensusRefused, match="recovery-free"):
        _open_and_collect(graph, _args(tmp_path / "clone", tmp_path / "out"))
    assert database.closed is True and database.close_complete is True


def test_child_requires_its_direct_runner_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKTO_GRAFX_PERF_RUNNER_PID", str(os.getppid()))
    _validate_runner_parent()
    monkeypatch.setenv("OKTO_GRAFX_PERF_RUNNER_PID", str(os.getppid() + 1))
    with pytest.raises(GraphCensusRefused, match="launched directly"):
        _validate_runner_parent()


class _Lock(AbstractContextManager[object]):
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __enter__(self) -> object:
        self._events.append("lock_enter")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._events.append("lock_exit")
        return None


def test_run_requires_full_clone_and_proves_post_close_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    graph = clone / "boards" / BOARD_ID / "grafx" / "generation"
    graph.mkdir(parents=True)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    args = _args(clone, out_dir / "census.json")
    for name in pulse_graph_census.DATA_HOME_ENV:
        monkeypatch.setenv(name, str(clone))
    monkeypatch.setenv("OKTO_GRAFX_PERF_RUNNER_PID", str(os.getppid()))

    files = [{"path": "opaque", "size": 1, "sha256": "a" * 64}]
    manifest = {
        "source": {"cloned_from_declared_copy": "b" * 64},
        "copy": {
            "sha256": "c" * 64,
            "file_count": 1,
            "total_bytes": 1,
            "files": files,
        },
    }
    observed_inventory = {
        "sha256": "c" * 64,
        "file_count": 1,
        "total_bytes": 1,
        "files": files,
    }
    binding = SimpleNamespace(
        scope="board",
        scope_id=BOARD_ID,
        backend="grafx",
        page_size=8192,
        physical_path=graph,
    )
    events: list[str] = []
    monkeypatch.setattr(
        pulse_graph_census,
        "require_declared_copy",
        lambda path, *, allow_effective_data_home: manifest,
    )
    monkeypatch.setattr(
        pulse_graph_census,
        "_validate_runtime_imports",
        lambda: {
            "okto_grafx_file": "grafx",
            "okto_pulse_community_file": "community",
            "okto_pulse_core_file": "core",
        },
    )
    monkeypatch.setattr(
        pulse_graph_census, "_exclusive_clone_lock", lambda _root: _Lock(events)
    )
    monkeypatch.setattr(
        pulse_graph_census,
        "_acquire_board_binding",
        lambda _root, _board: events.append("binding") or binding,
    )

    def collect(_path: Path, _args: argparse.Namespace) -> dict[str, Any]:
        events.append("handle_open_collect_close")
        return {"verification": {}, "counts": {}, "heap": {}}

    def final_inventory(_root: Path, *, exclude: tuple[str, ...]) -> dict[str, Any]:
        assert exclude == (
            pulse_graph_census.COPY_MANIFEST_NAME,
            pulse_graph_census.COPY_MANIFEST_NAME + ".sha256",
        )
        events.append("inventory_after_close")
        return observed_inventory

    monkeypatch.setattr(pulse_graph_census, "_open_and_collect", collect)
    monkeypatch.setattr(pulse_graph_census, "inventory", final_inventory)

    document = pulse_graph_census.run(args)

    assert events == [
        "lock_enter",
        "binding",
        "handle_open_collect_close",
        "lock_exit",
        "inventory_after_close",
    ]
    assert document["clone_inventory"] == {
        "unchanged": True,
        "sha256_before": "c" * 64,
        "sha256_after": "c" * 64,
        "file_count": 1,
        "total_bytes": 1,
    }
    encoded = json.dumps(document, sort_keys=True)
    assert BOARD_ID not in encoded
    assert str(graph) not in encoded
    assert not args.out.exists()


def test_inventory_proof_compares_file_population_not_only_tree_digest() -> None:
    expected = {
        "sha256": "a" * 64,
        "file_count": 1,
        "total_bytes": 5,
        "files": [{"path": "opaque", "size": 5, "sha256": "b" * 64}],
    }
    assert _inventory_matches(expected, dict(expected)) is True
    for field, changed in (
        ("sha256", "c" * 64),
        ("file_count", 2),
        ("total_bytes", 6),
        ("files", []),
    ):
        observed = dict(expected)
        observed[field] = changed
        assert _inventory_matches(expected, observed) is False


def test_main_creates_json_exclusively_only_after_run_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "census.json"
    state = {"closed": False}

    def finished(_args: argparse.Namespace) -> dict[str, object]:
        assert not out.exists()
        state["closed"] = True
        return {"closed_before_publish": True}

    monkeypatch.setattr(pulse_graph_census, "run", finished)
    exit_code = pulse_graph_census.main(
        [
            "--copy",
            str(tmp_path / "clone"),
            "--out",
            str(out),
            "--board-id",
            BOARD_ID,
            "--run",
            "0",
            "--mode",
            "strict",
            "--seed",
            "13",
            "--page-size",
            "8192",
            "--buffer-budget-bytes",
            str(64 * 1024 * 1024),
            "--checksum",
            "auto",
            "--kind",
            "instrumented",
            "--thermal",
            "mixed",
        ]
    )
    assert exit_code == 0 and state["closed"] is True
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "closed_before_publish": True
    }
    with pytest.raises(FileExistsError):
        out.open("x").close()


def test_unexpected_failure_does_not_emit_exception_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "secret-table-value-must-not-reach-stderr"

    def fail(_args: argparse.Namespace) -> dict[str, object]:
        raise RuntimeError(secret)

    monkeypatch.setattr(pulse_graph_census, "run", fail)
    exit_code = pulse_graph_census.main(
        [
            "--copy",
            str(tmp_path / "clone"),
            "--out",
            str(tmp_path / "out.json"),
            "--board-id",
            BOARD_ID,
            "--run",
            "0",
            "--mode",
            "strict",
            "--seed",
            "13",
            "--page-size",
            "8192",
            "--buffer-budget-bytes",
            str(64 * 1024 * 1024),
            "--checksum",
            "auto",
            "--kind",
            "instrumented",
            "--thermal",
            "mixed",
        ]
    )
    stderr = capsys.readouterr().err
    assert exit_code == 1
    assert "FAILED [RuntimeError]: pulse_graph_census_failed" in stderr
    assert secret not in stderr
