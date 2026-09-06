"""RELSEEK-M4: the physical endpoint landing validates vectors without materialising them."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import okto_grafx
import okto_grafx.engine.query_engine as query_engine_module
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxIndexError
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.schema import (
    ColumnDef,
    TableDef,
    decode_tuple,
    decode_tuple_landing,
    encode_tuple,
)
from okto_grafx.domain.model.value import (
    VectorValue,
    ValueType,
    _ValidatedValue,
    encode_value,
    value_type_of,
)
from okto_grafx.engine.heap_store import HeapStore


def _vector_table() -> TableDef:
    return TableDef(
        table_id=3,
        name="N",
        kind="node",
        columns=(
            ColumnDef("id", ValueType.STRING, nullable=False),
            ColumnDef("n", ValueType.INT64),
            ColumnDef("v", ValueType.VECTOR_F32, vector_space="emb"),
            ColumnDef("s", ValueType.STRING),
        ),
        primary_key="id",
    )


def _row() -> tuple[object, ...]:
    return ("n-1", 7, VectorValue((0.5, -1.0, 2.0, 0.25), 1, "float32"), "tail")


def test_landing_decode_keeps_every_scalar_and_replaces_only_the_vector() -> None:
    table = _vector_table()
    payload = encode_tuple(table, _row())
    full = decode_tuple(table, payload)
    landed = decode_tuple_landing(table, payload)

    assert landed[0] == full[0] and landed[1] == full[1] and landed[3] == full[3]
    assert type(full[2]) is VectorValue
    assert type(landed[2]) is _ValidatedValue
    # The sentinel is a proof shape, not a stored value: it can be neither typed nor encoded.
    with pytest.raises(SchemaMismatchError):
        value_type_of(landed[2])  # type: ignore[arg-type]
    with pytest.raises(SchemaMismatchError):
        encode_value(landed[2])  # type: ignore[arg-type]


def test_landing_decode_refuses_exactly_what_the_full_decoder_refuses() -> None:
    table = _vector_table()
    payload = bytearray(encode_tuple(table, _row()))
    full_prefix_len = len(encode_tuple(table, ("n-1", 7, None, "tail")))
    assert full_prefix_len < len(payload)
    mutations: dict[str, bytes] = {
        "truncated inside the vector body": bytes(payload[:-8]),
        "trailing bytes after the row": bytes(payload) + b"\x00",
        "vector tag swapped for a string tag": bytes(
            payload[: _vector_offset(table)]
            + bytes([int(ValueType.STRING)])
            + payload[_vector_offset(table) + 1 :]
        ),
        "dimension header inflated": bytes(
            payload[: _vector_offset(table) + 1]
            + (99).to_bytes(4, "little")
            + payload[_vector_offset(table) + 5 :]
        ),
        "hostile tag byte": bytes(
            payload[: _vector_offset(table)]
            + b"\xfe"
            + payload[_vector_offset(table) + 1 :]
        ),
    }
    for label, hostile in mutations.items():
        expected: BaseException | None = None
        try:
            decode_tuple(table, hostile)
        except Exception as failure:  # noqa: BLE001 - the oracle's refusal is the fixture
            expected = failure
        assert expected is not None, label
        with pytest.raises(type(expected)) as caught:
            decode_tuple_landing(table, hostile)
        assert _refusal_shape(caught.value) == _refusal_shape(expected), label


def _vector_offset(table: TableDef) -> int:
    """Return the payload offset of the vector column's tag byte."""
    return len(encode_tuple(table, ("n-1", 7, None, "tail"))) - len(
        encode_value(None)
    ) - len(encode_value("tail"))


def _refusal_shape(failure: BaseException) -> tuple:
    to_dict = getattr(failure, "to_dict", None)
    if to_dict is None:
        return (type(failure).__name__, str(failure))
    payload = to_dict()
    return (type(failure).__name__, payload.get("field"), payload.get("message"))


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    handle = okto_grafx.connect(tmp_path / "db", page_size=4096)
    with handle.begin("write") as transaction:
        transaction.execute("CREATE VECTOR SPACE emb {dimension: 4, metric: 'cosine'}")
        transaction.execute(
            "CREATE NODE TABLE N(id STRING, n INT64, v VECTOR(emb), PRIMARY KEY(id))"
        )
        transaction.execute("CREATE REL TABLE E(FROM N TO N, w DOUBLE)")
    with handle.begin("write") as transaction:
        for number in range(4):
            transaction.execute(
                "CREATE (:N {id: $id, n: $n, v: $v})",
                {"id": f"n-{number}", "n": number, "v": [float(number), 1.0, 0.5, 0.25]},
            )
    handle.ensure_identity_indexes()
    try:
        yield handle
    finally:
        handle.close()


def test_edge_creation_lands_endpoints_without_materialising_their_vectors(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    landings = 0
    full_reads = 0
    original_landing = HeapStore.read_landing
    original_read = HeapStore.read

    def counted_landing(heap, ref):
        nonlocal landings
        landings += 1
        return original_landing(heap, ref)

    def counted_read(heap, ref):
        nonlocal full_reads
        full_reads += 1
        return original_read(heap, ref)

    monkeypatch.setattr(HeapStore, "read_landing", counted_landing)
    monkeypatch.setattr(HeapStore, "read", counted_read)
    landed_versions: list[object] = []
    original_endpoint = query_engine_module._require_physical_endpoint

    def observed_endpoint(*args, **kwargs):
        version = original_endpoint(*args, **kwargs)
        landed_versions.append(version)
        return version

    monkeypatch.setattr(
        query_engine_module, "_require_physical_endpoint", observed_endpoint
    )

    with database.begin("write") as transaction:
        transaction.execute(
            "MATCH (a:N {id: $s}), (b:N {id: $t}) CREATE (a)-[:E {w: 0.5}]->(b)",
            {"s": "n-1", "t": "n-2"},
        )
    assert len(landed_versions) == 2
    assert landings == 2, "both physical endpoint witnesses must take the landing door"
    for version in landed_versions:
        assert type(version.values[2]) is _ValidatedValue
    # The public row of the same nodes still carries the real vector: the sentinel never
    # crosses into a row a statement can return.
    rows = database.execute(
        "MATCH (a:N)-[r:E]->(b:N) RETURN a.id, b.id, a.v, b.n ORDER BY a.id"
    ).rows
    assert len(rows) == 1
    assert rows[0][0] == "n-1" and rows[0][1] == "n-2" and rows[0][3] == 2
    assert type(rows[0][2]) is VectorValue
    assert tuple(rows[0][2].values) == (1.0, 1.0, 0.5, 0.25)


def test_landing_validation_is_refused_for_a_column_derived_index(database: object) -> None:
    from okto_grafx.domain.index.keys import index_key

    engine = database._queries  # type: ignore[attr-defined]
    manager = engine.require_indexes()
    table = engine.catalog.catalog.table("N")
    with database.begin("read") as transaction:
        snapshot = transaction._context.snapshot  # type: ignore[attr-defined]
        primary = next(
            index
            for index in manager.active_indexes_for(table.table_id, table=table)
            if index.definition.positions
        )
        key = index_key(["n-1", None, None], (0,))
        with pytest.raises(GrafxIndexError):
            manager.validated_identity_landings(primary, key, snapshot)
        assert manager.validated_versions(primary, key, snapshot)


def test_a_collaborator_without_the_landing_capability_is_used_through_validated_versions(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An index collaborator of the old contract answers the landing with full versions."""
    from okto_grafx.engine.index_manager import IndexManager

    # First with the capability present: one edge lands through the landing door.
    with database.begin("write") as transaction:
        transaction.execute(
            "MATCH (a:N {id: $s}), (b:N {id: $t}) CREATE (a)-[:E {w: 0.5}]->(b)",
            {"s": "n-0", "t": "n-1"},
        )

    # Then the collaborator loses the capability: the landing is served by the ordinary
    # validated_versions contract, with full versions, the same rows and no signature probing.
    monkeypatch.delattr(IndexManager, "validated_identity_landings")
    landings = 0
    full_reads = 0
    original_landing = HeapStore.read_landing
    original_read = HeapStore.read

    def counted_landing(heap, ref):
        nonlocal landings
        landings += 1
        return original_landing(heap, ref)

    def counted_read(heap, ref):
        nonlocal full_reads
        full_reads += 1
        return original_read(heap, ref)

    monkeypatch.setattr(HeapStore, "read_landing", counted_landing)
    monkeypatch.setattr(HeapStore, "read", counted_read)
    versions: list[object] = []
    original_endpoint = query_engine_module._require_physical_endpoint

    def observed_endpoint(*args, **kwargs):
        version = original_endpoint(*args, **kwargs)
        versions.append(version)
        return version

    monkeypatch.setattr(
        query_engine_module, "_require_physical_endpoint", observed_endpoint
    )
    with database.begin("write") as transaction:
        transaction.execute(
            "MATCH (a:N {id: $s}), (b:N {id: $t}) CREATE (a)-[:E {w: 0.25}]->(b)",
            {"s": "n-2", "t": "n-3"},
        )
    assert landings == 0 and full_reads > 0
    assert len(versions) == 2
    for version in versions:
        assert type(version.values[2]) is VectorValue
    rows = database.execute(
        "MATCH (a:N)-[r:E]->(b:N) RETURN a.id, b.id, r.w ORDER BY a.id"
    ).rows
    assert rows == (("n-0", "n-1", 0.5), ("n-2", "n-3", 0.25))


def test_landing_refuses_a_corrupt_vector_body_exactly_like_a_full_read(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = database._queries  # type: ignore[attr-defined]
    heap = engine.heap
    table = engine.catalog.catalog.table("N")
    ref = next(reference for reference, _version in heap.scan_all(table))
    original_slot = HeapStore._read_slot

    def corrupt_vector_length(store, wanted):
        table_id, content = original_slot(store, wanted)
        # The vector is the last column: tag, dimension (u32), space (u32), 4 x f32 body.
        # Inflate the dimension header so the body no longer fits; the full decoder and the
        # landing decoder must refuse identically.
        content = bytearray(content)
        content[-24:-20] = (77).to_bytes(4, "little")
        return table_id, bytes(content)

    with pytest.raises(GrafxCorruptionDetected) as full:
        monkeypatch.setattr(HeapStore, "_read_slot", corrupt_vector_length)
        heap.read(ref)
    with pytest.raises(GrafxCorruptionDetected) as landing:
        heap.read_landing(ref)
    monkeypatch.undo()
    assert _refusal_shape(landing.value) == _refusal_shape(full.value)
    assert type(heap.read_landing(ref).values[2]) is _ValidatedValue
