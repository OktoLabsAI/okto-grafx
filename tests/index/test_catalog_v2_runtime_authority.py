"""Catalog v2 is the sole runtime authority for exact index generations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import okto_grafx
import okto_grafx.engine.query_engine as query_engine_module
from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.domain.errors import GrafxRecoveryRefused
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index.catalog import (
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
    IndexGenerationState,
)
from okto_grafx.domain.index.records import (
    IndexChange,
    IndexOperation,
    change_of,
    wal_record_for,
)
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.schema import TableDef
from okto_grafx.domain.recovery.decision import CommittedReplay
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.commit_redo import CommitRedo
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexManager
from okto_grafx.engine.public_views import _indexes_view
from okto_grafx.engine.query_engine import QueryEngine

from .conftest import RecordingMetrics, TransactionDouble, exact_definition

_ACTIVE = "active_by_name"
_BUILDING = "building_by_name"
_STALE = "stale_by_name"
_ROGUE = "process_local_rogue"


class _HostileLogicalDefinition(CatalogIndexDefinition):
    """Layout-compatible value whose virtual metadata doors must never be dispatched."""

    __slots__ = ()

    @property
    def registry_key(self) -> str:
        raise RuntimeError("hostile registry_key executed")

    def active_generation(self) -> IndexGenerationDescriptor | None:
        raise RuntimeError("hostile active_generation executed")


@dataclass(frozen=True, slots=True)
class _AuthorityScenario:
    """One persisted authority plus deliberately broader process-local state."""

    catalog: CatalogStore
    heap: HeapStore
    pool: BufferPool
    manager: IndexManager
    metrics: RecordingMetrics
    table: TableDef
    active_nonce: int


def _catalog_definition(
    table: TableDef,
    *,
    name: str,
    nonce: int,
    state: IndexGenerationState,
) -> CatalogIndexDefinition:
    """Return one catalog-managed exact index over ``Person.name``."""

    return CatalogIndexDefinition(
        name=name,
        table_id=table.table_id,
        table_name=table.name,
        positions=(1,),
        visibility=IndexVisibility.EXACT,
        generations=(IndexGenerationDescriptor(nonce, 4, state),),
    )


@pytest.fixture
def authority_scenario(
    catalog_store: CatalogStore,
    heap_store: HeapStore,
    pool: BufferPool,
    manager: IndexManager,
    metrics: RecordingMetrics,
    person_table: TableDef,
) -> _AuthorityScenario:
    """Persist v2 authority, then contaminate only the process-local registry."""

    definitions = (
        _catalog_definition(
            person_table,
            name=_ACTIVE,
            nonce=0xA11CE,
            state=IndexGenerationState.ACTIVE,
        ),
        _catalog_definition(
            person_table,
            name=_BUILDING,
            nonce=0xB001D,
            state=IndexGenerationState.BUILDING,
        ),
        _catalog_definition(
            person_table,
            name=_STALE,
            nonce=0x57A1E,
            state=IndexGenerationState.STALE,
        ),
    )
    catalog_store.catalog.upgrade_index_catalog(definitions)
    catalog_store.save()
    pool.flush(catalog_store.file)

    durable = catalog_store.read_from_pages()
    assert durable.index_definitions() == definitions

    for logical in definitions:
        generation = logical.generations[0]
        manager.register(
            HashIndex(
                logical.runtime_definition(generation),
                pool,
                metrics,
            )
        )

    rogue = replace(
        exact_definition(person_table, name=_ROGUE),
        artifact_nonce=0xBAD,
    )
    manager.register(HashIndex(rogue, pool, metrics))

    return _AuthorityScenario(
        catalog=catalog_store,
        heap=heap_store,
        pool=pool,
        manager=manager,
        metrics=metrics,
        table=person_table,
        active_nonce=0xA11CE,
    )


def test_planner_sees_only_the_catalog_active_exact_generation(
    authority_scenario: _AuthorityScenario,
) -> None:
    scenario = authority_scenario
    engine = QueryEngine(
        catalog=scenario.catalog,
        heap=scenario.heap,
        pool=scenario.pool,
        metrics=scenario.metrics,
        clock=SystemClock(),
        indexes=scenario.manager,
    )

    definitions = engine._index_definitions(catalog=scenario.catalog.catalog)

    assert tuple(definition.name for definition in definitions) == (_ACTIVE,)
    assert definitions[0].artifact_nonce == scenario.active_nonce


def test_row_staging_targets_only_the_catalog_active_exact_generation(
    authority_scenario: _AuthorityScenario,
) -> None:
    scenario = authority_scenario
    txn = TransactionDouble()

    records = scenario.manager.stage_row_insert(
        txn,
        scenario.table.table_id,
        RecordRef(1, 0),
        (1, "Ada"),
        7,
        table_name=scenario.table.name,
        table=scenario.table,
    )

    assert tuple(change_of(record).index for record in records) == (_ACTIVE,)


@pytest.mark.parametrize(
    "index_name",
    [_BUILDING, _STALE, _ROGUE],
)
def test_redo_refuses_every_registered_exact_index_without_active_catalog_authority(
    authority_scenario: _AuthorityScenario,
    index_name: str,
) -> None:
    scenario = authority_scenario
    change = IndexChange(
        index=index_name,
        operation=IndexOperation.INSERT,
        key=b"Ada",
        ref=RecordRef(1, 0),
    )
    effect = wal_record_for(change, epoch=1, txn_id=1).with_lsn(7)
    replay = CommittedReplay(effects=(effect,), last_committed_lsn=8)

    with pytest.raises(GrafxRecoveryRefused) as refused:
        CommitRedo(scenario.pool, scenario.manager).preflight(replay)

    assert refused.value.details["field"] == "index"
    assert refused.value.details["index"] == index_name


def test_redo_accepts_the_catalog_active_generation(
    authority_scenario: _AuthorityScenario,
) -> None:
    scenario = authority_scenario
    change = IndexChange(
        index=_ACTIVE,
        operation=IndexOperation.INSERT,
        key=b"Ada",
        ref=RecordRef(1, 0),
    )
    effect = wal_record_for(change, epoch=1, txn_id=1).with_lsn(7)

    CommitRedo(scenario.pool, scenario.manager).preflight(
        CommittedReplay(effects=(effect,), last_committed_lsn=8)
    )


def test_public_inventory_exposes_only_active_and_preserves_its_physical_nonce(
    authority_scenario: _AuthorityScenario,
) -> None:
    scenario = authority_scenario

    inventory = _indexes_view(
        scenario.manager,
        scenario.catalog.catalog.tables(),
    )

    assert tuple(index.name for index in inventory.registered) == (_ACTIVE,)
    assert inventory.registered[0].definition.artifact_nonce == scenario.active_nonce
    assert inventory.registered[0].file == "index/g_00000000000a11ce.idx"


def test_public_inventory_reports_detached_logical_and_generation_metadata(
    authority_scenario: _AuthorityScenario,
) -> None:
    scenario = authority_scenario

    view = _indexes_view(
        scenario.manager,
        scenario.catalog.catalog.tables(),
    ).index(_ACTIVE)

    assert view.table_id == scenario.table.table_id
    assert view.table_name == "Person"
    assert view.columns == ("name",)
    assert view.positions == (1,)
    assert view.key_derivation == "columns"
    assert view.automatic is False
    assert view.generation_state == "active"
    assert view.active_nonce == scenario.active_nonce
    assert view.bucket_count == 4
    assert view.expected_cardinality is None


def test_public_inventory_never_dispatches_through_a_hostile_logical_definition(
    authority_scenario: _AuthorityScenario,
) -> None:
    scenario = authority_scenario
    logical = scenario.catalog.catalog.index_definition(_ACTIVE)
    object.__setattr__(logical, "__class__", _HostileLogicalDefinition)

    view = _indexes_view(
        scenario.manager,
        scenario.catalog.catalog.tables(),
    ).index(_ACTIVE)

    assert type(view.definition) is not _HostileLogicalDefinition
    assert view.name == _ACTIVE
    assert view.generation_state == "active"


def test_catalog_v2_inventory_preserves_schema_derived_vector_indexes(
    tmp_path: Path,
) -> None:
    with okto_grafx.connect(tmp_path / "vector-v2", page_size=512) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
            schema.execute(
                "CREATE NODE TABLE V(id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
            )

        assert "vector_V_s" in {
            index.name for index in database.indexes.registered
        }
        database.ensure_identity_indexes()
        vector = database.indexes.index("vector_V_s")

        assert vector.visibility is IndexVisibility.PROXIMITY
        assert vector.automatic is None
        assert vector.generation_state is None


def test_own_schema_transaction_resolves_primary_key_through_its_working_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A just-declared table uses its owner's catalog, never unrelated live authority."""

    observations: list[tuple[str, bool, bool]] = []
    original = query_engine_module._catalog_active_index

    def recording(
        manager: object,
        name: str,
        catalog: Catalog,
        *,
        txn: object | None = None,
    ) -> object | None:
        live = manager._heap.catalog.catalog  # type: ignore[attr-defined]
        observations.append(
            (
                name,
                catalog.has_table("Fresh"),
                live.has_table("Fresh"),
            )
        )
        return original(manager, name, catalog, txn=txn)

    monkeypatch.setattr(query_engine_module, "_catalog_active_index", recording)
    with okto_grafx.connect(tmp_path / "db", page_size=512) as database:
        with database.begin("write") as txn:
            txn.execute(
                "CREATE NODE TABLE Fresh(id INT64, name STRING, PRIMARY KEY(id))"
            )
            txn.execute("CREATE (:Fresh {id: 1, name: 'Ada'})")

        assert database.execute(
            "MATCH (n:Fresh) WHERE n.id = 1 RETURN n.name"
        ).rows == (("Ada",),)

    assert any(
        name == "pk_Fresh" and working_has_table and not live_has_table
        for name, working_has_table, live_has_table in observations
    )


def test_endpoint_bypass_resolves_its_store_through_catalog_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The traversal fast path cannot reach a raw endpoint registration by name."""

    with okto_grafx.connect(tmp_path / "db", page_size=512) as database:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE REL TABLE E(FROM A TO B, weight INT64)")
        with database.begin("write") as txn:
            txn.execute("CREATE (:A {id: 1})")
            txn.execute("CREATE (:B {id: 2})")
        with database.begin("write") as txn:
            txn.execute(
                "MATCH (a:A {id: 1}), (b:B {id: 2}) "
                "CREATE (a)-[:E {weight: 3}]->(b)"
            )

        selected: list[str] = []
        original = query_engine_module._catalog_active_index

        def recording(
            manager: object,
            name: str,
            catalog: Catalog,
            *,
            txn: object | None = None,
        ) -> object | None:
            selected.append(name)
            return original(manager, name, catalog, txn=txn)

        monkeypatch.setattr(query_engine_module, "_catalog_active_index", recording)

        assert database.execute(
            "MATCH (a:A {id: 1})-[:E]->(b:B) RETURN b.id"
        ).rows == ((2,),)

    assert "ef_E" in selected
