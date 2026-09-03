"""Focused contract tests for the catalog-v2 endpoint identity access path."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

import okto_grafx.engine.query_engine as query_engine_module
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index.catalog import (
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
    IndexGenerationState,
)
from okto_grafx.domain.index.definition import RECORD_ID_KEY_DERIVATION, IndexDefinition
from okto_grafx.domain.index.keys import record_id_key
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.record import HeapVersion
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.engine.query_engine import _visible_identity_with_ref


UNSIGNED_ID = (1 << 63) + 29


@dataclass(slots=True)
class _Store:
    """The physical identity generation selected by the fake framework."""

    definition: IndexDefinition
    stale: bool = False

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def file(self) -> str:
        return self.definition.file


class _Manager:
    """Record selection and validation calls without weakening their boundary."""

    def __init__(self, store: _Store) -> None:
        self.store = store
        self.active_calls: list[tuple[str, Catalog]] = []
        self.validated_calls: list[tuple[_Store, bytes, object]] = []
        self.answers: dict[bytes, tuple[tuple[RecordRef, HeapVersion], ...]] = {}
        self.failure: BaseException | None = None

    def active_index(self, name: str, *, catalog: Catalog) -> _Store:
        self.active_calls.append((name, catalog))
        return self.store

    def validated_versions(
        self,
        index: _Store,
        key: bytes,
        snapshot: object,
    ) -> tuple[tuple[RecordRef, HeapVersion], ...]:
        self.validated_calls.append((index, key, snapshot))
        if self.failure is not None:
            raise self.failure
        return self.answers.get(key, ())


class _Engine:
    """Only the index collaborator used before the canonical fallback door."""

    def __init__(self, manager: _Manager) -> None:
        self._indexes = manager

    def require_indexes(self) -> _Manager:
        return self._indexes


def _schema() -> tuple[Catalog, TableDef]:
    catalog = Catalog()
    person = TableDef(
        table_id=1,
        name="Person",
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
        primary_key="id",
    )
    relationship = TableDef(
        table_id=2,
        name="Knows",
        kind="rel",
        columns=(ColumnDef(name="since", type=ValueType.INT64),),
        from_table="Person",
        to_table="Person",
    )
    catalog.add_table(person)
    catalog.add_table(relationship)
    return catalog, person


def _logical_identity(
    table: TableDef,
    *generations: IndexGenerationDescriptor,
) -> CatalogIndexDefinition:
    return CatalogIndexDefinition(
        name=f"rid_t_{table.table_id:08x}",
        table_id=table.table_id,
        table_name=table.name,
        positions=(),
        visibility=IndexVisibility.EXACT,
        key_derivation=RECORD_ID_KEY_DERIVATION,
        automatic=True,
        generations=tuple(generations),
    )


def _active_scenario() -> tuple[Catalog, TableDef, CatalogIndexDefinition, _Manager]:
    catalog, table = _schema()
    logical = _logical_identity(
        table,
        IndexGenerationDescriptor(1, 4, IndexGenerationState.ACTIVE),
    )
    catalog.upgrade_index_catalog((logical,))
    manager = _Manager(_Store(logical.runtime_definition()))
    return catalog, table, logical, manager


def _context(catalog: Catalog) -> SimpleNamespace:
    return SimpleNamespace(
        snapshot=object(),
        schema=lambda: catalog,
        endpoint_identity_indexes={},
    )


def _version(record_id: int) -> HeapVersion:
    return HeapVersion(
        record_id=record_id,
        xmin=1,
        xmax=0,
        values=(1,),
        prev=None,
        schema_version=1,
        deleted=False,
        table_id=1,
    )


def _refuse_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        query_engine_module,
        "_endpoint_txn_memo",
        lambda *_args: pytest.fail(
            "an ACTIVE identity access path cannot enter the locator"
        ),
    )
    monkeypatch.setattr(
        query_engine_module,
        "_canonical_identity_with_ref",
        lambda *_args: pytest.fail("an ACTIVE identity miss cannot become a heap scan"),
    )


def _fixed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[int], tuple[RecordRef, HeapVersion]]:
    calls: list[int] = []
    answer = (RecordRef(9, 1), _version(UNSIGNED_ID))
    monkeypatch.setattr(query_engine_module, "_endpoint_txn_memo", lambda *_args: None)

    def canonical(
        _engine: object,
        _context: object,
        _table: TableDef,
        record_id: int,
    ) -> tuple[RecordRef, HeapVersion]:
        calls.append(record_id)
        return answer

    monkeypatch.setattr(query_engine_module, "_canonical_identity_with_ref", canonical)
    return calls, answer


def test_active_identity_is_selected_once_from_the_table_local_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, table, _logical, manager = _active_scenario()
    first = (RecordRef(3, 1), _version(UNSIGNED_ID))
    second_id = UNSIGNED_ID + 1
    second = (RecordRef(3, 2), _version(second_id))
    manager.answers[record_id_key(UNSIGNED_ID)] = (first,)
    manager.answers[record_id_key(second_id)] = (second,)
    context = _context(catalog)
    _refuse_fallback(monkeypatch)
    monkeypatch.setattr(
        Catalog,
        "active_index_definitions",
        lambda _self: pytest.fail(
            "endpoint routing must not project every database index"
        ),
    )

    assert (
        _visible_identity_with_ref(_Engine(manager), context, table, UNSIGNED_ID)
        == first
    )
    assert (
        _visible_identity_with_ref(_Engine(manager), context, table, second_id)
        == second
    )

    assert tuple(name for name, _catalog in manager.active_calls) == ("rid_t_00000001",)
    assert tuple(call[1] for call in manager.validated_calls) == (
        record_id_key(UNSIGNED_ID),
        record_id_key(second_id),
    )


def test_active_identity_miss_is_definitive_without_a_heap_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, table, _logical, manager = _active_scenario()
    _refuse_fallback(monkeypatch)

    assert (
        _visible_identity_with_ref(
            _Engine(manager), _context(catalog), table, UNSIGNED_ID
        )
        is None
    )
    assert manager.validated_calls[0][1] == record_id_key(UNSIGNED_ID)


def test_two_visible_identity_hits_are_reported_as_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, table, _logical, manager = _active_scenario()
    manager.answers[record_id_key(UNSIGNED_ID)] = (
        (RecordRef(4, 1), _version(UNSIGNED_ID)),
        (RecordRef(4, 2), _version(UNSIGNED_ID)),
    )
    _refuse_fallback(monkeypatch)

    with pytest.raises(GrafxCorruptionDetected) as corrupt:
        _visible_identity_with_ref(
            _Engine(manager), _context(catalog), table, UNSIGNED_ID
        )

    assert corrupt.value.details["field"] == "record_id"
    assert corrupt.value.details["record_id"] == UNSIGNED_ID
    assert corrupt.value.details["count"] == 2


def test_failure_after_identity_selection_propagates_without_reselection_or_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, table, _logical, manager = _active_scenario()
    answer = (RecordRef(5, 1), _version(UNSIGNED_ID))
    manager.answers[record_id_key(UNSIGNED_ID)] = (answer,)
    context = _context(catalog)
    _refuse_fallback(monkeypatch)
    engine = _Engine(manager)
    assert _visible_identity_with_ref(engine, context, table, UNSIGNED_ID) == answer

    manager.failure = GrafxIndexError("generation changed", field="generation")
    with pytest.raises(GrafxIndexError, match="generation changed"):
        _visible_identity_with_ref(engine, context, table, UNSIGNED_ID)

    assert len(manager.active_calls) == 1
    assert len(manager.validated_calls) == 2


def test_missing_catalog_selected_store_refuses_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, table, _logical, manager = _active_scenario()
    manager.store = None  # type: ignore[assignment]
    _refuse_fallback(monkeypatch)

    with pytest.raises(GrafxIndexError) as unavailable:
        _visible_identity_with_ref(
            _Engine(manager), _context(catalog), table, UNSIGNED_ID
        )

    assert unavailable.value.details["field"] == "index_authority"
    assert unavailable.value.details["registered"] is False


def test_catalog_selected_store_must_match_the_complete_physical_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, table, _logical, manager = _active_scenario()
    manager.store.definition = replace(manager.store.definition, artifact_nonce=2)
    _refuse_fallback(monkeypatch)

    with pytest.raises(GrafxIndexError) as mismatch:
        _visible_identity_with_ref(
            _Engine(manager), _context(catalog), table, UNSIGNED_ID
        )

    assert mismatch.value.details["field"] == "index_authority"
    assert mismatch.value.details["registered"] is True


def test_active_identity_requires_heap_validated_version_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, table, logical, _manager = _active_scenario()

    class _LookupOnlyManager:
        def active_index(self, _name: str, *, catalog: Catalog) -> _Store:
            return _Store(logical.runtime_definition())

    _refuse_fallback(monkeypatch)

    with pytest.raises(GrafxUnsupportedOperation) as unsupported:
        _visible_identity_with_ref(
            _Engine(_LookupOnlyManager()),  # type: ignore[arg-type]
            _context(catalog),
            table,
            UNSIGNED_ID,
        )

    assert unsupported.value.details["field"] == "component"
    assert unsupported.value.details["value"] == "validated_versions"


def test_a_store_stale_before_selection_fixes_the_statement_on_heap_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, table, _logical, manager = _active_scenario()
    manager.store.stale = True
    context = _context(catalog)
    fallback_calls, fallback = _fixed_fallback(monkeypatch)
    engine = _Engine(manager)

    assert _visible_identity_with_ref(engine, context, table, UNSIGNED_ID) == fallback
    manager.store.stale = False
    assert _visible_identity_with_ref(engine, context, table, UNSIGNED_ID) == fallback

    assert fallback_calls == [UNSIGNED_ID, UNSIGNED_ID]
    assert len(manager.active_calls) == 1
    assert manager.validated_calls == []


def test_v1_fallback_does_not_adopt_an_identity_index_mid_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, table = _schema()
    generation = IndexGenerationDescriptor(1, 4, IndexGenerationState.ACTIVE)
    logical = _logical_identity(table, generation)
    manager = _Manager(_Store(logical.runtime_definition(generation)))
    context = _context(catalog)
    fallback_calls, fallback = _fixed_fallback(monkeypatch)
    engine = _Engine(manager)

    assert _visible_identity_with_ref(engine, context, table, UNSIGNED_ID) == fallback
    catalog.upgrade_index_catalog((logical,))
    assert _visible_identity_with_ref(engine, context, table, UNSIGNED_ID) == fallback

    assert fallback_calls == [UNSIGNED_ID, UNSIGNED_ID]
    assert manager.active_calls == []
    assert manager.validated_calls == []


def test_no_active_generation_fixes_fallback_even_if_one_is_published_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, table, logical, manager = _active_scenario()
    old = logical.generations[0].mark_stale()
    without_active = replace(logical, generations=(old,))
    catalog.replace_index_definition(without_active)
    context = _context(catalog)
    fallback_calls, fallback = _fixed_fallback(monkeypatch)
    engine = _Engine(manager)

    assert _visible_identity_with_ref(engine, context, table, UNSIGNED_ID) == fallback
    new = IndexGenerationDescriptor(2, 8, IndexGenerationState.ACTIVE)
    published = replace(logical, generations=(old, new))
    catalog.replace_index_definition(published)
    manager.store = _Store(published.runtime_definition(new))
    assert _visible_identity_with_ref(engine, context, table, UNSIGNED_ID) == fallback

    assert fallback_calls == [UNSIGNED_ID, UNSIGNED_ID]
    assert manager.active_calls == []
    assert manager.validated_calls == []
