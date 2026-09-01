"""Startup index creation is fenced away from recovery and read-only inspection."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError
from okto_grafx.engine.coordination import COMMIT_SECTION
from okto_grafx.engine.index_manager import IndexManager, IndexStore
from okto_grafx.engine.recovery_manager import RecoveryManager
from okto_grafx.engine.txn_manager import TransactionManager

PAGE_SIZE = 512


@dataclass(frozen=True)
class _Event:
    phase: str
    file: str
    existing_only: bool | None
    schema_depth: int
    commit_depth: int
    wal_tail_depth: int


@dataclass
class _StartupTrace:
    phase: str = "outside"
    schema_depth: int = 0
    commit_depth: int = 0
    wal_tail_depth: int = 0
    recovery_runs: int = 0
    schema_entries: int = 0
    registrations: list[_Event] = field(default_factory=list)
    creations: list[_Event] = field(default_factory=list)

    def event(self, file: str, existing_only: bool | None) -> _Event:
        return _Event(
            phase=self.phase,
            file=file,
            existing_only=existing_only,
            schema_depth=self.schema_depth,
            commit_depth=self.commit_depth,
            wal_tail_depth=self.wal_tail_depth,
        )


def _data_tree(root: Path) -> dict[str, str]:
    """Hash database bytes other than the read-only reader-registration plane."""
    return {
        relative: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not (relative := path.relative_to(root).as_posix()).startswith("control/")
    }


def _seed_and_damage_index(root: Path, damage: str) -> Path:
    """Create one damaged target and one intact index recovery can visibly adopt."""
    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
            transaction.execute("CREATE (:Person {id: 1, name: 'ada'})")
            transaction.execute(
                "CREATE NODE TABLE Place(id INT64, name STRING, PRIMARY KEY(id))"
            )
            transaction.execute("CREATE (:Place {id: 2, name: 'london'})")
        # Retire the logical index effects before removing the derived artifact. Recovery must
        # refuse a missing target that retained WAL still needs to replay; this fixture isolates
        # the later startup repair path whose serialization is under test.
        database.checkpoint()

    matches = tuple(
        path for path in root.rglob("*") if path.is_file() and "pk_Person" in path.name
    )
    assert len(matches) == 1, matches
    target = matches[0]
    if damage == "missing":
        target.unlink()
    elif damage == "torn":
        target.write_bytes(b"")
    else:  # pragma: no cover - the parametrization is closed
        raise AssertionError(f"unknown damage shape {damage!r}")
    return target


def _trace_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> _StartupTrace:
    """Observe protocol boundaries while delegating every operation to production."""
    trace = _StartupTrace()
    original_recovery_run = RecoveryManager.run
    original_schema_section = TransactionManager.schema_artifact_section
    original_coordinator_section = TransactionManager._coordinator_section
    original_wal_tail = TransactionManager._hold_wal_tail
    original_register = IndexManager.register
    original_create = IndexStore._create_with_provenance

    def recovery_run(self: RecoveryManager):
        previous = trace.phase
        trace.phase = "recovery"
        trace.recovery_runs += 1
        try:
            return original_recovery_run(self)
        finally:
            trace.phase = previous

    @contextmanager
    def coordinator_section(self: TransactionManager, name: str, *, timeout: float):
        with original_coordinator_section(self, name, timeout=timeout) as entered:
            commit = name == COMMIT_SECTION
            if commit:
                trace.commit_depth += 1
            try:
                yield entered
            finally:
                if commit:
                    trace.commit_depth -= 1

    @contextmanager
    def wal_tail(self: TransactionManager):
        with original_wal_tail(self):
            trace.wal_tail_depth += 1
            try:
                yield
            finally:
                trace.wal_tail_depth -= 1

    @contextmanager
    def schema_section(
        self: TransactionManager, *, sync_if=None
    ):
        with original_schema_section(self, sync_if=sync_if):
            previous = trace.phase
            trace.phase = "schema_body"
            trace.schema_depth += 1
            trace.schema_entries += 1
            try:
                yield
            finally:
                trace.schema_depth -= 1
                trace.phase = previous

    def register(self: IndexManager, index: IndexStore, **options):
        existing_only = bool(options.get("existing_only", False))
        trace.registrations.append(trace.event(index.file, existing_only))
        return original_register(self, index, **options)

    def create(self: IndexStore, *, proved_present: bool = False):
        trace.creations.append(trace.event(self.file, None))
        return original_create(self, proved_present=proved_present)

    monkeypatch.setattr(RecoveryManager, "run", recovery_run)
    monkeypatch.setattr(TransactionManager, "_coordinator_section", coordinator_section)
    monkeypatch.setattr(TransactionManager, "_hold_wal_tail", wal_tail)
    monkeypatch.setattr(TransactionManager, "schema_artifact_section", schema_section)
    monkeypatch.setattr(IndexManager, "register", register)
    monkeypatch.setattr(IndexStore, "_create_with_provenance", create)
    return trace


@pytest.mark.parametrize("damage", ("missing", "torn"))
def test_writable_startup_creates_or_repairs_only_in_the_artifact_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    root = tmp_path / damage
    target = _seed_and_damage_index(root, damage)
    trace = _trace_startup(monkeypatch)

    with connect(root, page_size=PAGE_SIZE) as reopened:
        assert reopened.execute(
            "MATCH (p:Person) WHERE p.id = 1 RETURN p.name"
        ).rows == (("ada",),)

    assert target.exists() and target.stat().st_size > 0
    assert trace.recovery_runs == 1
    recovery_registrations = [
        event for event in trace.registrations if event.phase == "recovery"
    ]
    assert recovery_registrations, (
        "the intact control index was not adopted in recovery"
    )
    assert all(event.existing_only for event in recovery_registrations)

    target_creations = [
        event for event in trace.creations if event.file.endswith("pk_Person.idx")
    ]
    assert len(target_creations) == 1, target_creations
    assert trace.creations == target_creations
    assert all(
        event.phase == "schema_body"
        and event.schema_depth > 0
        and event.commit_depth > 0
        and event.wal_tail_depth > 0
        for event in target_creations
    ), target_creations
    assert trace.schema_entries == 1


@pytest.mark.parametrize("damage", ("missing", "torn"))
def test_read_only_startup_never_enters_a_create_capable_index_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    root = tmp_path / damage
    _seed_and_damage_index(root, damage)
    before = _data_tree(root)
    trace = _trace_startup(monkeypatch)

    try:
        reader = connect(root, page_size=PAGE_SIZE, read_only=True)
    except GrafxError as refusal:
        assert refusal.code
    else:
        with reader:
            assert reader.execute(
                "MATCH (p:Person) WHERE p.id = 1 RETURN p.name"
            ).rows == (("ada",),)

    assert trace.recovery_runs == 0
    assert trace.schema_entries == 0
    assert trace.creations == []
    assert all(event.existing_only for event in trace.registrations)
    assert _data_tree(root) == before
