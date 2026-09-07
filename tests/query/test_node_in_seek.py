"""NODE-IN-SEEK: ``MATCH (n:Label) WHERE n.<pk> IN $ids`` through the exact multi-key door."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxError, GrafxPlanError
from okto_grafx.domain.query.plan import (
    AllNodesScan,
    FilterRows,
    NodeMultiKeySeek,
    NodeScan,
)
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexManager

QUERY = "MATCH (n:A) WHERE n.id IN $ids RETURN n.id, n.v"
INT_QUERY = "MATCH (c:C) WHERE c.k IN $keys RETURN c.k, c.name"


def _seed_database(path: Path, *, identity_indexes: bool) -> object:
    handle = okto_grafx.connect(path, page_size=512)
    with handle.begin("write") as transaction:
        transaction.execute("CREATE NODE TABLE A(id STRING, v INT64, PRIMARY KEY(id))")
        transaction.execute("CREATE NODE TABLE B(id STRING, PRIMARY KEY(id))")
        transaction.execute("CREATE NODE TABLE C(k INT64, name STRING, PRIMARY KEY(k))")
        transaction.execute("CREATE REL TABLE R(FROM A TO B, confidence DOUBLE)")
    with handle.begin("write") as transaction:
        for number in range(1, 6):
            transaction.execute(
                "CREATE (:A {id: $id, v: $v})", {"id": f"a{number}", "v": number}
            )
        for name in ("b1", "b2"):
            transaction.execute("CREATE (:B {id: $id})", {"id": name})
        for key in (1, 2, 3):
            transaction.execute(
                "CREATE (:C {k: $k, name: $name})", {"k": key, "name": f"c{key}"}
            )
        transaction.execute(
            "MATCH (a:A {id: 'a1'}), (b:B {id: 'b1'}) "
            "CREATE (a)-[:R {confidence: 0.5}]->(b)"
        )
    if identity_indexes:
        handle.ensure_identity_indexes()
    return handle


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


def _doors(monkeypatch: pytest.MonkeyPatch, table_name: str = "A") -> dict[str, int]:
    """Count every door the seek may open: certificates, multi-key probes, encodes, scans."""
    import okto_grafx.engine.query_engine as query_engine_module

    counts = {"many": 0, "probes": 0, "encoded": 0, "scans": 0, "certificates": 0}
    original_many = IndexManager.validated_versions_many
    original_key = query_engine_module.index_key
    original_scan = HeapStore.scan
    original_begin = HashIndex.begin_exact_read

    def many(manager, index, keys, snapshot):  # type: ignore[no-untyped-def]
        counts["many"] += 1
        counts["probes"] += len(keys)
        return original_many(manager, index, keys, snapshot)

    def key(values, positions):  # type: ignore[no-untyped-def]
        counts["encoded"] += 1
        return original_key(values, positions)

    def scan(heap, table, snapshot):  # type: ignore[no-untyped-def]
        if table.name == table_name:
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


def _outcome(database: object, statement: str, params: dict[str, object]) -> tuple:
    try:
        result = database.execute(statement, params)  # type: ignore[attr-defined]
    except GrafxError as failure:
        return ("erro", type(failure).__name__, repr(sorted(failure.to_dict().items())))
    return ("linhas", tuple(result.rows))


def _seek_of(planned: object) -> NodeMultiKeySeek | None:
    return next(
        (node for node in planned.walk() if type(node) is NodeMultiKeySeek),  # type: ignore[attr-defined]
        None,
    )


def test_plan_admits_the_seek_and_keeps_the_predicate_above_a_canonical_scan(
    database: object,
) -> None:
    planned = database.explain(QUERY)  # type: ignore[attr-defined]
    seek = _seek_of(planned)

    assert seek is not None
    assert seek.table.name == "A"
    assert seek.key_position == 0
    assert seek.index.lower().endswith("a")
    assert type(seek.fallback) is NodeScan and seek.fallback.table.name == "A"
    # The whole conjunction stays above the seek, exactly where the scan path keeps it.
    filters = [
        node
        for node in planned.walk()
        if type(node) is FilterRows and node.child is seek
    ]
    assert len(filters) == 1
    assert "IN $ids" in filters[0].predicate.describe()
    assert "IN $ids" in seek.details()["predicate"]


def test_seek_answers_from_the_index_without_scanning_the_table(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _doors(monkeypatch)
    result = database.execute(QUERY, {"ids": ["a3", "a1", "a1", "missing", None]})  # type: ignore[attr-defined]

    assert result.rows == (("a1", 1), ("a3", 3))
    assert counts["scans"] == 0
    assert counts["many"] == 1
    # a3, a1, missing: duplicates and None never reach the index; every non-None probe is
    # encoded, duplicates included, exactly as the incident seek encodes its frontier.
    assert counts["probes"] == 3
    assert counts["encoded"] == 4
    assert result.statistics.get("rows_seeked") == 2
    assert not result.statistics.get("rows_scanned")


@pytest.mark.parametrize(
    "ids",
    (
        [],
        [None],
        ["a1"],
        ["a3", "a1"],
        [None, 1, "a3"],
        ["ação"],
        "a1",
        7,
        ["a1", "a1", None],
        ["\ud800", "a1"],
        [1.5, "a2"],
        ["zz"],
        ("a2", "a4"),
        None,
    ),
)
def test_seek_is_differentially_equal_to_the_canonical_scan(
    database: object, monkeypatch: pytest.MonkeyPatch, ids: object
) -> None:
    """Rows in order, and refusals with their details: the same as the scan of the table."""
    accelerated = _outcome(database, QUERY, {"ids": ids})
    with monkeypatch.context() as scoped:
        scoped.setattr(IndexManager, "validated_versions_many", None, raising=False)
        canonical = _outcome(database, QUERY, {"ids": ids})

    assert accelerated == canonical


def test_a_non_list_probe_keeps_the_public_in_refusal_at_the_scan(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _doors(monkeypatch)
    with pytest.raises(GrafxPlanError) as refused:
        database.execute(QUERY, {"ids": "a1"})  # type: ignore[attr-defined]

    assert "IN looks inside a list" in refused.value.to_dict()["message"]
    assert counts["many"] == 0 and counts["certificates"] == 0
    assert counts["scans"] == 1


def test_a_probe_the_key_cannot_encode_takes_the_scan_without_a_new_refusal(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lone surrogate is compared by value on the scan; the seek must not invent a refusal."""
    counts = _doors(monkeypatch)
    result = database.execute(QUERY, {"ids": ["\ud800", "a2"]})  # type: ignore[attr-defined]

    assert result.rows == (("a2", 2),)
    assert counts["many"] == 0 and counts["certificates"] == 0
    assert counts["scans"] == 1


def test_missing_multi_key_door_executes_the_retained_scan(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(IndexManager, "validated_versions_many", None, raising=False)
    result = database.execute(QUERY, {"ids": ["a4"]})  # type: ignore[attr-defined]

    assert result.rows == (("a4", 4),)
    assert result.statistics.get("rows_scanned") == 5


def test_a_stale_store_executes_the_retained_scan(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _doors(monkeypatch)
    monkeypatch.setattr(HashIndex, "stale", property(lambda self: True))
    result = database.execute(QUERY, {"ids": ["a4"]})  # type: ignore[attr-defined]

    assert result.rows == (("a4", 4),)
    assert counts["many"] == 0
    assert counts["scans"] == 1


def test_catalog_v1_keeps_its_automatic_primary_key_seek(
    database_without_identity_indexes: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The automatic primary-key index predates catalog v2: v1 seeks through it as well."""
    handle = database_without_identity_indexes
    planned = handle.explain(QUERY)  # type: ignore[attr-defined]
    assert _seek_of(planned) is not None

    accelerated = _outcome(handle, QUERY, {"ids": ["a5", "a1", None, 3]})
    with monkeypatch.context() as scoped:
        scoped.setattr(IndexManager, "validated_versions_many", None, raising=False)
        canonical = _outcome(handle, QUERY, {"ids": ["a5", "a1", None, 3]})
    assert accelerated == canonical == ("linhas", (("a1", 1), ("a5", 5)))


def test_owner_dirty_table_keeps_read_your_writes_on_the_scan(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _doors(monkeypatch)
    with database.begin("write") as transaction:  # type: ignore[attr-defined]
        transaction.execute("CREATE (:A {id: 'a6', v: 6})")
        transaction.execute("MATCH (n:A {id: 'a2'}) DELETE n")
        rows = transaction.execute(QUERY, {"ids": ["a6", "a2", "a1"]}).rows

    assert rows == (("a1", 1), ("a6", 6))
    assert counts["many"] == 0
    assert counts["scans"] >= 1


@pytest.mark.parametrize(
    "text",
    (
        "MATCH (n) WHERE n.id IN $ids RETURN n.id",
        "MATCH (n:A) WHERE n.id IN ['a1', 'a2'] RETURN n.id",
        "MATCH (n:A) WHERE n.v IN $ids RETURN n.id",
        "MATCH (n:A) WHERE n.v > 0 AND n.id IN $ids RETURN n.id",
        "MATCH (a:A), (n:A) WHERE n.id IN $ids RETURN n.id",
        "MATCH (n:A)-[r:R]->(b:B) WHERE n.id IN $ids RETURN n.id",
    ),
)
def test_near_misses_keep_the_scan(
    text: str, database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    planned = database.explain(text)  # type: ignore[attr-defined]

    assert _seek_of(planned) is None
    accelerated = _outcome(database, text, {"ids": ["a1", 1]})
    with monkeypatch.context() as scoped:
        scoped.setattr(IndexManager, "validated_versions_many", None, raising=False)
        canonical = _outcome(database, text, {"ids": ["a1", 1]})
    assert accelerated == canonical


def test_polymorphic_node_keeps_every_table_scan(database: object) -> None:
    planned = database.explain("MATCH (n) WHERE n.id IN $ids RETURN n.id")  # type: ignore[attr-defined]

    assert any(type(node) is AllNodesScan for node in planned.walk())


def test_a_residual_conjunct_is_judged_above_the_seek(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "MATCH (n:A) WHERE n.id IN $ids AND n.v > 2 RETURN n.id ORDER BY n.id"
    planned = database.explain(text)  # type: ignore[attr-defined]
    assert _seek_of(planned) is not None

    counts = _doors(monkeypatch)
    rows = database.execute(text, {"ids": ["a1", "a3", "a5"]}).rows  # type: ignore[attr-defined]

    assert rows == (("a3",), ("a5",))
    assert counts["scans"] == 0


def test_limit_stops_after_the_same_rows_as_the_scan(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "MATCH (n:A) WHERE n.id IN $ids RETURN n.id LIMIT 2"
    accelerated = database.execute(text, {"ids": ["a5", "a3", "a1", "a4"]}).rows  # type: ignore[attr-defined]
    with monkeypatch.context() as scoped:
        scoped.setattr(IndexManager, "validated_versions_many", None, raising=False)
        canonical = database.execute(text, {"ids": ["a5", "a3", "a1", "a4"]}).rows  # type: ignore[attr-defined]

    assert accelerated == canonical == (("a1",), ("a3",))


@pytest.mark.parametrize(
    "keys",
    ([1, 3], [1, 2.0], [2.0], [True], ["1"], [None, 2], [], [3, 3, 3]),
)
def test_int64_keys_seek_only_when_every_probe_encodes_completely(
    database: object, monkeypatch: pytest.MonkeyPatch, keys: list[object]
) -> None:
    accelerated = _outcome(database, INT_QUERY, {"keys": keys})
    with monkeypatch.context() as scoped:
        scoped.setattr(IndexManager, "validated_versions_many", None, raising=False)
        canonical = _outcome(database, INT_QUERY, {"keys": keys})
    assert accelerated == canonical


def test_a_double_probe_of_an_int64_key_takes_the_scan(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _doors(monkeypatch, table_name="C")
    rows = database.execute(INT_QUERY, {"keys": [1, 2.0]}).rows  # type: ignore[attr-defined]

    assert rows == ((1, "c1"), (2, "c2"))
    assert counts["many"] == 0
    assert counts["scans"] == 1


def test_exact_int64_probes_seek(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _doors(monkeypatch, table_name="C")
    rows = database.execute(INT_QUERY, {"keys": [3, 1, 9]}).rows  # type: ignore[attr-defined]

    assert rows == ((1, "c1"), (3, "c3"))
    assert counts["many"] == 1 and counts["probes"] == 3
    assert counts["scans"] == 0


def test_repeated_statements_in_one_transaction_reuse_the_primary_key_memo(
    database: object,
) -> None:
    with database.begin("read") as transaction:  # type: ignore[attr-defined]
        first = transaction.execute(QUERY, {"ids": ["a1", "a2"]})
        second = transaction.execute(QUERY, {"ids": ["a2", "a1"]})

    assert first.rows == second.rows == (("a1", 1), ("a2", 2))
