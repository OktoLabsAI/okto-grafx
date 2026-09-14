"""A landing table with no vector column reaches the same batched endpoint port (READ-3).

``_closed_vector_free_landings`` declines a target that stores no vector -- its proof has
nothing to withhold -- and BATCH-REL-1 read that verdict as its own admission, so a graph
without vector columns paid one page-0 certificate per landing.  These tests pin the port
counts, prove the batched rows are the scalar rows, and keep the batch out of every shape
the scalar path would have re-proved.
"""
from __future__ import annotations

import pytest

import okto_grafx
from okto_grafx.domain.index.definition import RECORD_ID_KEY_DERIVATION
from okto_grafx.engine import query_engine as qe
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import IndexManager, IndexStore
from okto_grafx.errors import GrafxCorruptionDetected

PREFIX = "MATCH (a:A)-[r:E]->(b:B) WHERE a.id='a' "
QUERY = PREFIX + "RETURN b.id, b.title, count(r) ORDER BY b.id"


@pytest.fixture
def graph(tmp_path):
    """The vector-free twin of tests/query/test_vector_free_traversal.py's board."""
    with okto_grafx.connect(tmp_path / "db", page_size=8192) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id STRING, title STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM A TO B, rank INT64)")
            tx.execute("CREATE (:A {id:'a'})")
            for i in range(3):
                tx.execute("CREATE (:B {id:$id, title:$title})",
                           {"id": f"b{i}", "title": f"Title {i}"})
                for rank in range(2):
                    tx.execute(
                        "MATCH (a:A {id:'a'}), (b:B {id:$id}) CREATE (a)-[:E {rank:$rank}]->(b)",
                        {"id": f"b{i}", "rank": rank},
                    )
        db.ensure_identity_indexes()
        yield db


@pytest.fixture
def vector_graph(tmp_path):
    """The same board with a vector column on the landing table."""
    with okto_grafx.connect(tmp_path / "db", page_size=8192) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE emb {dimension: 8, metric:'cosine'}")
            tx.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id STRING, title STRING, v VECTOR(emb), "
                       "PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM A TO B, rank INT64)")
            tx.execute("CREATE (:A {id:'a'})")
            for i in range(3):
                tx.execute("CREATE (:B {id:$id, title:$title, v:$v})",
                           {"id": f"b{i}", "title": f"Title {i}", "v": [float(i + 1)] * 8})
                for rank in range(2):
                    tx.execute(
                        "MATCH (a:A {id:'a'}), (b:B {id:$id}) CREATE (a)-[:E {rank:$rank}]->(b)",
                        {"id": f"b{i}", "rank": rank},
                    )
        db.ensure_identity_indexes()
        yield db


def _port(monkeypatch):
    """Count the three landing doors of the index-manager port, per operation.

    The canonical ``__func__`` fences compare the live class attribute against the constant
    query_engine pinned at import, so an observer must re-pin it or it silently turns off the
    path under measurement.
    """
    counts: dict[str, int] = {"many": 0, "landing": 0, "versions": 0}
    keys: list[int] = []
    for label, name, constant in (
        ("many", "validated_identity_landings_many", "_BATCH_LANDING_CANONICAL_MANY"),
        ("landing", "validated_identity_landings", "_BATCH_LANDING_CANONICAL_SCALAR"),
        ("versions", "validated_versions", "_VECTOR_FREE_CANONICAL_VALIDATED"),
    ):
        original = getattr(IndexManager, name)

        def observed(manager, index, key, snapshot, *, _label=label, _original=original):
            counts[_label] += 1
            if _label == "many":
                keys.append(len(key))
            return _original(manager, index, key, snapshot)

        monkeypatch.setattr(IndexManager, name, observed)
        monkeypatch.setattr(qe, constant, observed)
    return counts, keys


def _scalar_oracle(monkeypatch):
    """Force the fallback the batched port replaces, exactly as NODE-IN-SEEK's oracle does."""
    monkeypatch.setattr(IndexManager, "validated_identity_landings_many", None)


def test_vector_free_landings_reach_the_batched_port(graph, monkeypatch):
    counts, keys = _port(monkeypatch)
    rows = graph.execute(QUERY).rows
    assert rows == tuple((f"b{i}", f"Title {i}", 2) for i in range(3))
    # One batch for the six steps of the single source; three distinct destinations.
    assert counts["many"] == 1 and keys == [3]
    assert counts["landing"] == 0


def test_scalar_oracle_pays_one_certificate_per_landing(graph, monkeypatch):
    counts, keys = _port(monkeypatch)
    monkeypatch.setattr(qe, "_admits_batched_landings", lambda *_args: False)
    graph.execute(QUERY)
    assert counts["many"] == 0 and keys == []
    assert counts["versions"] == 3  # one page-0 certificate per distinct landing


@pytest.mark.parametrize("suffix", [
    "RETURN b.id, b.title, count(r) ORDER BY b.id",
    "RETURN b.id, r.rank ORDER BY b.id DESC, r.rank ASC LIMIT 3",
    "RETURN count(r)",
    "RETURN b.id, b.title ORDER BY b.title DESC",
    "RETURN DISTINCT b.id ORDER BY b.id",
    "RETURN b ORDER BY b.id",
])
def test_batched_rows_order_columns_and_statistics_equal_the_scalar_path(
    graph, monkeypatch, suffix,
):
    counts, _keys = _port(monkeypatch)
    candidate = graph.execute(PREFIX + suffix)
    assert counts["many"] == 1
    _scalar_oracle(monkeypatch)
    scalar = graph.execute(PREFIX + suffix)
    assert candidate.rows == scalar.rows
    assert candidate.columns == scalar.columns
    assert candidate.statistics == scalar.statistics


@pytest.mark.parametrize("suffix", ["RETURN b.id LIMIT 1", "RETURN b.id",
                                    "RETURN b", "WITH b RETURN count(b)"])
def test_streaming_or_open_consumers_keep_the_scalar_landing(graph, monkeypatch, suffix):
    counts, _keys = _port(monkeypatch)
    graph.execute(PREFIX + suffix)
    assert counts["many"] == 0


def test_vector_landing_without_the_closed_proof_is_never_batched(vector_graph, monkeypatch):
    """The batch withholds vector components; only the closed proof may admit that."""
    monkeypatch.setattr(qe, "_closed_vector_free_landings", lambda _root: frozenset())
    counts, _keys = _port(monkeypatch)
    rows = vector_graph.execute(PREFIX + "RETURN b.id, b.v ORDER BY b.id").rows
    assert counts["many"] == 0
    assert all(len(row[1].values) == 8 for row in rows)


def test_vector_landing_with_the_closed_proof_still_batches(vector_graph, monkeypatch):
    counts, _keys = _port(monkeypatch)
    assert vector_graph.execute(QUERY).rows == tuple(
        (f"b{i}", f"Title {i}", 2) for i in range(3))
    assert counts["many"] == 1


def test_runtime_landing_table_is_reproved_before_any_batch(graph, monkeypatch):
    """A plan-level verdict never admits a batch on a table that stores a vector."""
    original = qe._declares_vector
    monkeypatch.setattr(qe, "_total_landings", lambda engine, node: True)
    monkeypatch.setattr(qe, "_declares_vector",
                        lambda table: True if table.name == "B" else original(table))
    counts, _keys = _port(monkeypatch)
    expected = tuple((f"b{i}", f"Title {i}", 2) for i in range(3))
    assert graph.execute(QUERY).rows == expected
    assert counts["many"] == 0


def test_owner_staged_write_keeps_the_scalar_overlay(graph, monkeypatch):
    counts, _keys = _port(monkeypatch)
    with graph.begin("write") as tx:
        tx.execute("MATCH (a:A),(b:B {id:'b0'}) CREATE (a)-[:E {rank:9}]->(b)")
        assert tx.execute(PREFIX + "RETURN count(r)").rows == ((7,),)
    assert counts["many"] == 0


def test_reader_snapshot_survives_an_independent_writer(graph):
    with graph.begin("read") as reader:
        before = reader.execute(QUERY).rows
        with okto_grafx.connect(graph.path, page_size=8192) as other:
            with other.begin("write") as writer:
                writer.execute("MATCH (b:B {id:'b1'}) SET b.title='new generation'")
        assert reader.execute(QUERY).rows == before
    assert ("b1", "new generation", 2) in graph.execute(QUERY).rows
    assert graph._queries._owner_budget._used_bytes == 0


def test_duplicate_identity_refuses_exactly_as_the_scalar_path(graph, monkeypatch):
    index = next(index for index in graph._indexes.active_indexes()
                 if index.definition.table_name == "B"
                 and index.definition.key_derivation == RECORD_ID_KEY_DERIVATION)
    original = IndexStore._candidates_unchecked

    def duplicates(store, key):
        for entry in original(store, key):
            yield entry
            if store.file == index.file:
                yield entry

    monkeypatch.setattr(IndexStore, "_candidates_unchecked", duplicates)
    with pytest.raises(GrafxCorruptionDetected) as candidate:
        graph.execute(QUERY)
    _scalar_oracle(monkeypatch)
    with pytest.raises(GrafxCorruptionDetected) as scalar:
        graph.execute(QUERY)
    assert candidate.value.to_dict() == scalar.value.to_dict()


def test_invisible_candidates_are_validated_but_never_become_rows(graph, monkeypatch):
    original = HeapStore.read_landing
    seen: list[int] = []

    def invisible(heap, ref):
        version = original(heap, ref)
        seen.append(version.record_id)
        return version.__class__(
            record_id=version.record_id, xmin=10**12, xmax=version.xmax,
            values=version.values, prev=version.prev,
            schema_version=version.schema_version, deleted=version.deleted,
            table_id=version.table_id, node_labels=version.node_labels,
        )

    monkeypatch.setattr(HeapStore, "read_landing", invisible)
    assert graph.execute(QUERY).rows == ()
    assert seen


def test_every_landing_of_a_batch_carries_its_own_certificate(graph, monkeypatch):
    """One probed identity per step, and the rows the scalar path would have produced."""
    original = qe._OwnerLandingView.landings_many
    probed: list[tuple] = []

    def observed(view, identities, context, index):
        answers = original(view, identities, context, index)
        probed.append(tuple(identities))
        assert len(answers) == len(identities)
        return answers

    monkeypatch.setattr(qe._OwnerLandingView, "landings_many", observed)
    rows = graph.execute(QUERY).rows
    # Six steps leave the single source; every one of them is probed, in order.
    assert len(probed) == 1 and len(probed[0]) == 6
    _scalar_oracle(monkeypatch)
    assert rows == graph.execute(QUERY).rows
    assert rows == tuple((f"b{i}", f"Title {i}", 2) for i in range(3))
