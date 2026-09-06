"""Closed multi-key incident-edge access path used by the Pulse graph projection."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query.plan import (
    FilterRows,
    RelationshipIncidentSeek,
    RelationshipScan,
    TraverseRelationship,
)
from okto_grafx.engine.heap_store import FIRST_RECORD_ID, HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexManager


QUERY = (
    "MATCH (a:A)-[r:R]->(b:B) "
    "WHERE a.id IN $node_ids OR b.id IN $node_ids "
    "RETURN a.id, b.id, r.confidence LIMIT 5000"
)
INCOMING_QUERY = (
    "MATCH (b:B)<-[r:R]-(a:A) "
    "WHERE a.id IN $node_ids OR b.id IN $node_ids "
    "RETURN a.id, b.id, r.confidence LIMIT 5000"
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    handle = _seed_database(tmp_path / "db", identity_indexes=True)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def database_without_identity_indexes(tmp_path: Path) -> Iterator[object]:
    handle = _seed_database(tmp_path / "db-v1", identity_indexes=False)
    try:
        yield handle
    finally:
        handle.close()


def _seed_database(path: Path, *, identity_indexes: bool) -> object:
    handle = okto_grafx.connect(path, page_size=512)
    with handle.begin("write") as transaction:
        transaction.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
        transaction.execute("CREATE NODE TABLE B(id STRING, PRIMARY KEY(id))")
        transaction.execute("CREATE REL TABLE R(FROM A TO B, confidence DOUBLE)")
    with handle.begin("write") as transaction:
        for name in ("a1", "a2", "a3"):
            transaction.execute("CREATE (:A {id: $id})", {"id": name})
        for name in ("b1", "b2", "b3"):
            transaction.execute("CREATE (:B {id: $id})", {"id": name})
        for source, target, confidence in (
            ("a1", "b1", 0.4),
            ("a1", "b1", 0.5),
            ("a2", "b1", 0.6),
            ("a3", "b3", 0.7),
        ):
            transaction.execute(
                "MATCH (a:A {id: $source}), (b:B {id: $target}) "
                "CREATE (a)-[:R {confidence: $confidence}]->(b)",
                {
                    "source": source,
                    "target": target,
                    "confidence": confidence,
                },
            )
    if identity_indexes:
        handle.ensure_identity_indexes()
    return handle


def test_closed_endpoint_union_retains_a_canonical_fallback(database: object) -> None:
    planned = database.explain(QUERY)
    incident = next(
        node for node in planned.walk() if type(node) is RelationshipIncidentSeek
    )

    assert type(incident.fallback) is FilterRows
    assert any(type(node) is RelationshipScan for node in incident.fallback.walk())
    assert not any(type(node) is TraverseRelationship for node in incident.fallback.walk())
    assert incident.from_table.name == "A"
    assert incident.to_table.name == "B"


def test_incident_union_preserves_parallel_edges_and_either_endpoint(
    database: object,
) -> None:
    rows = database.execute(QUERY, {"node_ids": ["a1", "b1"]}).rows

    assert sorted(rows) == [
        ("a1", "b1", 0.4),
        ("a1", "b1", 0.5),
        ("a2", "b1", 0.6),
    ]


def test_incoming_syntax_keeps_stored_endpoint_roles(database: object) -> None:
    incident = next(
        node
        for node in database.explain(INCOMING_QUERY).walk()
        if type(node) is RelationshipIncidentSeek
    )

    assert incident.from_variable == "a"
    assert incident.to_variable == "b"
    assert database.execute(INCOMING_QUERY, {"node_ids": ["a3"]}).rows == (
        ("a3", "b3", 0.7),
    )


def test_missing_multi_key_capability_executes_the_retained_fallback(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(IndexManager, "validated_versions_many", None, raising=False)

    assert database.execute(QUERY, {"node_ids": ["a3"]}).rows == (
        ("a3", "b3", 0.7),
    )


def test_non_exact_probe_values_return_to_canonical_membership(database: object) -> None:
    assert database.execute(QUERY, {"node_ids": [None, 1, "b3"]}).rows == (
        ("a3", "b3", 0.7),
    )


def test_wrong_probe_types_cannot_partially_encode_an_int64_frontier(
    tmp_path: Path,
) -> None:
    handle = okto_grafx.connect(tmp_path / "int64-db", page_size=512)
    try:
        with handle.begin("write") as transaction:
            transaction.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            transaction.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
            transaction.execute("CREATE REL TABLE R(FROM A TO B, confidence DOUBLE)")
        with handle.begin("write") as transaction:
            transaction.execute("CREATE (:A {id: 1})")
            transaction.execute("CREATE (:B {id: 3})")
            transaction.execute(
                "MATCH (a:A {id: 1}), (b:B {id: 3}) "
                "CREATE (a)-[:R {confidence: 0.9}]->(b)"
            )
        handle.ensure_identity_indexes()

        assert handle.execute(QUERY, {"node_ids": ["1", 1.5, 3]}).rows == (
            (1, 3, 0.9),
        )
    finally:
        handle.close()


def test_v1_without_identity_indexes_resolves_landings_outside_the_probe_frontier(
    database_without_identity_indexes: object,
) -> None:
    assert database_without_identity_indexes.execute(
        QUERY, {"node_ids": ["a1"]}
    ).rows == (
        ("a1", "b1", 0.4),
        ("a1", "b1", 0.5),
    )


def test_non_list_parameter_preserves_the_public_in_refusal(database: object) -> None:
    with pytest.raises(GrafxPlanError) as raised:
        database.execute(QUERY, {"node_ids": "a1"})

    assert raised.value.details["field"] == "operator"
    assert raised.value.details["value"] == "IN"


def test_opposite_landings_share_one_identity_certificate(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = HashIndex.begin_exact_read
    calls: list[tuple[int, ...]] = []

    def observed(index, required_lsn):
        calls.append(index.definition.positions)
        return original(index, required_lsn)

    monkeypatch.setattr(HashIndex, "begin_exact_read", observed)

    assert database.execute(QUERY, {"node_ids": ["a1"]}).rows == (
        ("a1", "b1", 0.4),
        ("a1", "b1", 0.5),
    )
    assert calls.count(()) == 1
    # The empty incoming edge frontier opens no certificate; the other three index batches and
    # the one opposite-landing identity lookup each open exactly one.
    assert len(calls) == 4


def test_repeated_incident_pages_reuse_node_pk_decodes_inside_one_transaction(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = IndexManager._validated_version_groups
    decoded: list[tuple[str, int]] = []

    def observed(manager, index, wanted, distinct, snapshot, certificate):
        if (
            index.definition.table_name in {"A", "B"}
            and index.definition.positions == (0,)
        ):
            decoded.append((index.definition.table_name, len(distinct)))
        return original(manager, index, wanted, distinct, snapshot, certificate)

    monkeypatch.setattr(IndexManager, "_validated_version_groups", observed)

    with database.begin("read") as transaction:
        first = transaction.execute(QUERY, {"node_ids": ["a1", "b1"]}).rows
        first_decodes = sum(count for _table, count in decoded)
        decoded.clear()
        second = transaction.execute(QUERY, {"node_ids": ["a1", "b1"]}).rows
        second_decodes = sum(count for _table, count in decoded)
        assert database._queries._owner_budget._used_entries > 0

    assert sorted(first) == sorted(second)
    assert first_decodes == 4
    assert second_decodes == 0
    assert database._queries._owner_budget._used_entries == 0


def test_incident_pk_memo_decodes_only_new_keys_in_the_same_snapshot(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = IndexManager._validated_version_groups
    decoded: list[tuple[str, int]] = []

    def observed(manager, index, wanted, distinct, snapshot, certificate):
        if (
            index.definition.table_name in {"A", "B"}
            and index.definition.positions == (0,)
        ):
            decoded.append((index.definition.table_name, len(distinct)))
        return original(manager, index, wanted, distinct, snapshot, certificate)

    monkeypatch.setattr(IndexManager, "_validated_version_groups", observed)

    with database.begin("read") as transaction:
        transaction.execute(QUERY, {"node_ids": ["a1"]})
        decoded.clear()
        transaction.execute(QUERY, {"node_ids": ["a1", "a2"]})

    assert sum(count for _table, count in decoded) == 2


def test_a_heap_epoch_change_revalidates_every_incident_pk_key(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = IndexManager._validated_version_groups
    decoded: list[tuple[str, int]] = []

    def observed(manager, index, wanted, distinct, snapshot, certificate):
        if (
            index.definition.table_name in {"A", "B"}
            and index.definition.positions == (0,)
        ):
            decoded.append((index.definition.table_name, len(distinct)))
        return original(manager, index, wanted, distinct, snapshot, certificate)

    monkeypatch.setattr(IndexManager, "_validated_version_groups", observed)

    with database.begin("read") as transaction:
        expected = transaction.execute(QUERY, {"node_ids": ["a1"]}).rows
        decoded.clear()
        heap = database._queries.heap
        heap._pool.discard_clean_file(heap.file)
        observed_rows = transaction.execute(QUERY, {"node_ids": ["a1"]}).rows

    assert observed_rows == expected
    assert sum(count for _table, count in decoded) == 2


def test_an_index_registry_revision_change_revalidates_every_incident_pk_key(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = IndexManager._validated_version_groups
    decoded: list[int] = []

    def observed(manager, index, wanted, distinct, snapshot, certificate):
        if (
            index.definition.table_name in {"A", "B"}
            and index.definition.positions == (0,)
        ):
            decoded.append(len(distinct))
        return original(manager, index, wanted, distinct, snapshot, certificate)

    monkeypatch.setattr(IndexManager, "_validated_version_groups", observed)

    with database.begin("read") as transaction:
        expected = transaction.execute(QUERY, {"node_ids": ["a1"]}).rows
        decoded.clear()
        database._queries._indexes._registry_revision += 1
        observed_rows = transaction.execute(QUERY, {"node_ids": ["a1"]}).rows

    assert observed_rows == expected
    assert sum(decoded) == 2


def test_a_custom_multi_key_door_never_uses_the_native_pk_memo(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = IndexManager.validated_versions_many
    node_batches = 0

    def observed(manager, index, keys, snapshot):
        nonlocal node_batches
        if (
            index.definition.table_name in {"A", "B"}
            and index.definition.positions == (0,)
        ):
            node_batches += 1
        return original(manager, index, keys, snapshot)

    monkeypatch.setattr(IndexManager, "validated_versions_many", observed)

    with database.begin("read") as transaction:
        first = transaction.execute(QUERY, {"node_ids": ["a1"]}).rows
        second = transaction.execute(QUERY, {"node_ids": ["a1"]}).rows

    assert first == second
    assert node_batches == 4


def test_pk_memo_capacity_changes_cost_only_and_settlement_releases_it(
    database: object,
) -> None:
    from okto_grafx.engine.query_engine import _OwnerLandingBudget

    expected = database.execute(QUERY, {"node_ids": ["a1", "b1"]}).rows
    engine = database._queries
    engine._owner_budget = _OwnerLandingBudget(
        max_bytes=1_000_000,
        max_entries=2,
        guard=engine._endpoint_guard,
    )

    with database.begin("read") as transaction:
        first = transaction.execute(QUERY, {"node_ids": ["a1", "b1"]}).rows
        second = transaction.execute(QUERY, {"node_ids": ["a1", "b1"]}).rows
        assert engine._owner_budget._used_entries == 2

    assert first == second == expected
    assert engine._owner_budget._used_entries == 0
    assert engine._owner_budget._used_bytes == 0


def test_tiny_relationship_frontier_uses_edge_first_scan_before_any_certificate(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = IndexManager.validated_versions_many
    calls = 0

    def observed(manager, index, keys, snapshot):
        nonlocal calls
        calls += 1
        return original(manager, index, keys, snapshot)

    monkeypatch.setattr(IndexManager, "validated_versions_many", observed)
    missing = [f"missing-{number}" for number in range(10)]

    assert database.execute(QUERY, {"node_ids": missing}).rows == ()
    assert calls == 0


def test_edge_first_scan_is_chosen_before_any_string_probe_is_encoded(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KGRUN-M3: a STRING frontier is counted, not encoded, when the scan wins."""
    import okto_grafx.engine.query_engine as query_engine_module

    original_key = query_engine_module.index_key
    encoded = 0

    def counted_index_key(values, positions):
        nonlocal encoded
        encoded += 1
        return original_key(values, positions)

    monkeypatch.setattr(query_engine_module, "index_key", counted_index_key)
    # Four edges against a frontier of ten distinct strings: the scan wins.  Repeated and null
    # probes must count exactly as the encoding route counts them, so they are included.
    probes = [f"missing-{number}" for number in range(10)] + ["missing-0", None]

    assert database.execute(QUERY, {"node_ids": probes}).rows == ()
    assert encoded == 0

    # Two distinct strings against four edges: the seek wins and every non-null probe is still
    # encoded on both endpoint sides, exactly as before (landings encode their own keys after).
    assert database.execute(QUERY, {"node_ids": ["a1", "a1", "b1"]}).rows == (
        ("a1", "b1", 0.4),
        ("a1", "b1", 0.5),
        ("a2", "b1", 0.6),
    )
    assert encoded >= 6


def test_probe_frontier_counts_exact_strings_and_declines_everything_else() -> None:
    from okto_grafx.domain.model.schema import ColumnDef, TableDef
    from okto_grafx.domain.model.value import ValueType
    from okto_grafx.engine.query_engine import _string_probe_frontier

    class Tagged(str):
        """A str subclass must keep the encoding route, not be counted here."""

    table = TableDef(
        table_id=7,
        name="A",
        kind="node",
        columns=(
            ColumnDef("id", ValueType.STRING, nullable=False),
            ColumnDef("n", ValueType.INT64),
        ),
        primary_key="id",
    )
    assert _string_probe_frontier(table, 0, ("x", "x", None, "y")) == 2
    assert _string_probe_frontier(table, 0, ()) == 0
    assert _string_probe_frontier(table, 0, ("x", Tagged("y"))) is None
    assert _string_probe_frontier(table, 0, ("x", 1)) is None
    assert _string_probe_frontier(table, 1, (1, 2)) is None
    # Only ASCII is provably UTF-8 without encoding: accented text and a lone surrogate both
    # return to the encoder, which accepts the former and refuses the latter exactly as before.
    assert _string_probe_frontier(table, 0, ("x", "ação")) is None
    assert _string_probe_frontier(table, 0, ("x", "\ud800")) is None


@pytest.mark.parametrize(
    "probes",
    (
        ["\ud800"],
        [f"missing-{number}" for number in range(10)] + ["\ud800"],
        ["a1", "\ud800"],
    ),
    ids=("alone", "inside-a-scan-sized-frontier", "beside-a-hit"),
)
def test_a_lone_surrogate_probe_is_refused_before_any_branch_exactly_as_before(
    database: object, monkeypatch: pytest.MonkeyPatch, probes: list[str]
) -> None:
    """A probe UTF-8 cannot encode must keep the encoder's refusal, class, details and timing."""
    from okto_grafx.domain.model.errors import SchemaMismatchError

    certificates = 0
    original = IndexManager.validated_versions_many

    def observed(manager, index, keys, snapshot):
        nonlocal certificates
        certificates += 1
        return original(manager, index, keys, snapshot)

    monkeypatch.setattr(IndexManager, "validated_versions_many", observed)

    with pytest.raises(SchemaMismatchError) as refused:
        database.execute(QUERY, {"node_ids": probes})
    details = refused.value.to_dict()
    assert details["code"] == "schema_mismatch"
    assert "surrogates not allowed" in details["message"]
    assert certificates == 0, "the refusal must come before any index certificate"
    # Accented text is not ASCII either, but it encodes: it takes the encoder route and answers.
    assert database.execute(QUERY, {"node_ids": ["ação"]}).rows == ()


@pytest.mark.parametrize(("allocated_upper", "expects_seek"), ((12, False), (60, True)))
def test_cost_selector_crosses_between_twelve_and_sixty_edges_for_eighty_four_keys(
    database: object,
    monkeypatch: pytest.MonkeyPatch,
    allocated_upper: int,
    expects_seek: bool,
) -> None:
    extent_of = HeapStore.extent_of
    calls = 0
    original_many = IndexManager.validated_versions_many

    def drifted_page_hint(heap, table):
        extent = extent_of(heap, table)
        if extent is not None and table.name == "R":
            return replace(
                extent,
                page_count=0,
                next_record_id=FIRST_RECORD_ID + allocated_upper,
            )
        return extent

    def observed(manager, index, keys, snapshot):
        nonlocal calls
        calls += 1
        return original_many(manager, index, keys, snapshot)

    monkeypatch.setattr(HeapStore, "extent_of", drifted_page_hint)
    monkeypatch.setattr(IndexManager, "validated_versions_many", observed)
    missing = [f"missing-{number}" for number in range(42)]

    assert database.execute(QUERY, {"node_ids": missing}).rows == ()
    assert (calls > 0) is expects_seek


@pytest.mark.parametrize(
    "node_ids",
    ([], ["a1"], ["b1"], ["a1", "b1"], ["a3"], [None, 1, "b3"]),
)
def test_incident_path_is_differentially_equal_to_its_canonical_fallback(
    database: object,
    monkeypatch: pytest.MonkeyPatch,
    node_ids: list[object],
) -> None:
    accelerated = database.execute(QUERY, {"node_ids": node_ids}).rows
    monkeypatch.setattr(IndexManager, "validated_versions_many", None, raising=False)
    canonical = database.execute(QUERY, {"node_ids": node_ids}).rows

    assert sorted(accelerated) == sorted(canonical)


def test_owner_dirty_tables_keep_read_your_writes_on_the_canonical_path(
    database: object,
) -> None:
    with database.begin("write") as transaction:
        transaction.execute(
            "MATCH (a:A {id: 'a2'}), (b:B {id: 'b2'}) "
            "CREATE (a)-[:R {confidence: 0.8}]->(b)"
        )

        assert transaction.execute(QUERY, {"node_ids": ["b2"]}).rows == (
            ("a2", "b2", 0.8),
        )


@pytest.mark.parametrize(
    "text",
    (
        "MATCH (a:A)-[r:R]->(b:B) WHERE a.id IN $ids RETURN a.id LIMIT 5000",
        "MATCH (a:A)-[r:R]->(b:B) WHERE a.id IN $ids AND b.id IN $ids "
        "RETURN a.id LIMIT 5000",
        "MATCH (a:A)-[r:R]->(b:B) WHERE a.id IN $ids OR b.id = 'b1' "
        "RETURN a.id LIMIT 5000",
        "MATCH (a:A)-[r:R]->(b:B) WHERE a.id IN $ids OR b.id IN $ids "
        "RETURN a.id LIMIT 64",
    ),
)
def test_near_misses_keep_the_existing_traversal(text: str, database: object) -> None:
    planned = database.explain(text)

    assert not any(type(node) is RelationshipIncidentSeek for node in planned.walk())
    assert any(type(node) is TraverseRelationship for node in planned.walk())


@pytest.fixture
def empty_database(tmp_path: Path) -> Iterator[object]:
    """The seeded schema and nodes, but a relationship table that never allocated a row."""
    handle = okto_grafx.connect(tmp_path / "db-empty", page_size=512)
    with handle.begin("write") as transaction:
        transaction.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
        transaction.execute("CREATE NODE TABLE B(id STRING, PRIMARY KEY(id))")
        transaction.execute("CREATE REL TABLE R(FROM A TO B, confidence DOUBLE)")
    with handle.begin("write") as transaction:
        for name in ("a1", "a2", "a3"):
            transaction.execute("CREATE (:A {id: $id})", {"id": name})
        for name in ("b1", "b2", "b3"):
            transaction.execute("CREATE (:B {id: $id})", {"id": name})
    handle.ensure_identity_indexes()
    try:
        yield handle
    finally:
        handle.close()


def _doors(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count every door the incident union may open: certificates, probes, encodes, scans."""
    import okto_grafx.engine.query_engine as query_engine_module
    from okto_grafx.engine.heap_store import HeapStore

    counts = {"many": 0, "encoded": 0, "scans": 0, "certificates": 0}
    original_many = IndexManager.validated_versions_many
    original_key = query_engine_module.index_key
    original_scan = HeapStore.scan
    original_begin = HashIndex.begin_exact_read

    def many(manager, index, keys, snapshot):  # type: ignore[no-untyped-def]
        counts["many"] += 1
        return original_many(manager, index, keys, snapshot)

    def key(values, positions):  # type: ignore[no-untyped-def]
        counts["encoded"] += 1
        return original_key(values, positions)

    def scan(heap, table, snapshot):  # type: ignore[no-untyped-def]
        if table.kind == "rel":
            counts["scans"] += 1
        return original_scan(heap, table, snapshot)

    def begin(index, lsn):  # type: ignore[no-untyped-def]
        counts["certificates"] += 1
        return original_begin(index, lsn)

    monkeypatch.setattr(IndexManager, "validated_versions_many", many)
    monkeypatch.setattr(query_engine_module, "index_key", key)
    monkeypatch.setattr(HeapStore, "scan", scan)
    monkeypatch.setattr(HashIndex, "begin_exact_read", begin)
    return counts


def _outcome(database: object, probes: object) -> tuple:
    from okto_grafx.domain.errors import GrafxError

    try:
        result = database.execute(QUERY, {"node_ids": probes})  # type: ignore[attr-defined]
    except GrafxError as failure:
        return ("erro", type(failure).__name__, repr(sorted(failure.to_dict().items())))
    return ("linhas", tuple(sorted(result.rows)), tuple(sorted(result.statistics.items())))


def test_a_table_that_never_allocated_a_relationship_answers_empty_without_opening_anything(
    empty_database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BATCH-REL-1: the durable authority proves the union empty before scan or certificate."""
    counts = _doors(monkeypatch)
    probes = [f"missing-{number}" for number in range(66)] + ["a1", "b1"]
    result = empty_database.execute(QUERY, {"node_ids": probes})  # type: ignore[attr-defined]
    assert result.rows == ()
    assert counts == {"many": 0, "encoded": 0, "scans": 0, "certificates": 0}
    # The statistic of the branch that answered is the one the scan reported.
    assert result.statistics.get("edge_scans") == 1


def test_the_empty_answer_still_refuses_what_the_encoder_refused_before_it(
    empty_database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okto_grafx.domain.model.errors import SchemaMismatchError

    counts = _doors(monkeypatch)
    with pytest.raises(SchemaMismatchError) as refused:
        empty_database.execute(QUERY, {"node_ids": ["\ud800", "a1"]})  # type: ignore[attr-defined]
    assert "surrogates not allowed" in refused.value.to_dict()["message"]
    assert counts["many"] == 0 and counts["certificates"] == 0 and counts["scans"] == 0


@pytest.mark.parametrize(
    "probes",
    ([], [None], ["a1"], ["a1", "b1"], [None, 1, "b3"], ["ação"], "a1", 7, ["a1", "a1", None]),
)
def test_the_empty_answer_is_differentially_equal_to_the_scan_of_the_empty_table(
    empty_database: object, monkeypatch: pytest.MonkeyPatch, probes: object
) -> None:
    """Rows, statistics and refusals: the same as the canonical scan of the empty table."""
    proved = _outcome(empty_database, probes)
    with monkeypatch.context() as scoped:
        scoped.setattr(IndexManager, "validated_versions_many", None, raising=False)
        scanned = _outcome(empty_database, probes)
    assert proved == scanned


def test_a_materialised_extent_at_the_first_id_keeps_the_canonical_scan(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review: only a missing extent proves the union empty.

    A table whose pages exist while next_record_id still sits at the first id is walked and
    validated by the canonical scan; the durable counter alone is not the proof.  The seeded
    table has rows, so a scan that was omitted would answer nothing where these rows are due.
    """
    from okto_grafx.engine.heap_store import HeapStore

    extent_of = HeapStore.extent_of

    def at_first_id(heap, table):  # type: ignore[no-untyped-def]
        extent = extent_of(heap, table)
        if extent is not None and table.name == "R":
            return replace(extent, next_record_id=FIRST_RECORD_ID)
        return extent

    monkeypatch.setattr(HeapStore, "extent_of", at_first_id)
    counts = _doors(monkeypatch)
    rows = database.execute(QUERY, {"node_ids": ["a1", "b1"]}).rows  # type: ignore[attr-defined]
    assert rows == (("a1", "b1", 0.4), ("a1", "b1", 0.5), ("a2", "b1", 0.6))
    assert counts["scans"] == 1 and counts["many"] == 0


def test_a_table_whose_relationships_were_all_deleted_keeps_the_selector(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ids once allocated are not the empty proof: the old rule and its scan still run."""
    with database.begin("write") as transaction:  # type: ignore[attr-defined]
        transaction.execute("MATCH (a:A)-[r:R]->(b:B) DELETE r")
    counts = _doors(monkeypatch)
    probes = [f"missing-{number}" for number in range(10)]
    assert database.execute(QUERY, {"node_ids": probes}).rows == ()  # type: ignore[attr-defined]
    assert counts["scans"] == 1 and counts["many"] == 0
