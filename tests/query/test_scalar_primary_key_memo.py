"""Scalar PK anchors reuse decoded answers, never skip their physical fences."""
from collections import Counter
from types import SimpleNamespace

import pytest

import okto_grafx
from okto_grafx.errors import GrafxCorruptionDetected, GrafxWriteConflict
from okto_grafx.engine import query_engine as qe
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import HashIndex, IndexManager


QUERY = "MATCH (n:A) WHERE n.id=$id RETURN n.id,n.title,n.v"


def test_unknown_transaction_does_not_acquire_a_new_mode_callback():
    class CustomTransaction:
        @property
        def mode(self):
            raise AssertionError("optional memo must not inspect custom transaction mode")

    context = SimpleNamespace(txn=CustomTransaction())
    assert qe._scalar_primary_key_group(None, context, None, None, b"", None, 0) is None


@pytest.fixture
def graph(tmp_path):
    with okto_grafx.connect(tmp_path / "db", page_size=8192) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE emb {dimension:4, metric:'cosine'}")
            tx.execute("CREATE NODE TABLE A(id STRING, title STRING, v VECTOR(emb), PRIMARY KEY(id))")
            for number in range(4):
                tx.execute("CREATE (:A {id:$id,title:$title,v:$v})", {
                    "id": str(number), "title": f"node {number}", "v": [1.0] * 4,
                })
        yield db


def observe(monkeypatch):
    counts = Counter()
    groups = IndexManager._validated_version_groups
    stable = HashIndex._stable_view

    def counted_groups(manager, index, wanted, distinct, snapshot, certificate):
        counts["keys"] += len(distinct)
        return groups(manager, index, wanted, distinct, snapshot, certificate)

    def counted_stable(index, *args, **kwargs):
        counts["stable"] += 1
        return stable(index, *args, **kwargs)

    monkeypatch.setattr(IndexManager, "_validated_version_groups", counted_groups)
    monkeypatch.setattr(HashIndex, "_stable_view", counted_stable)
    return counts


@pytest.mark.parametrize("key", ["0", "missing"])
@pytest.mark.parametrize("mode", ["read", "write"])
def test_repeated_hit_and_miss_keep_a_fresh_certificate(graph, monkeypatch, key, mode):
    counts = observe(monkeypatch)
    with graph.begin(mode) as tx:
        first = tx.execute(QUERY, {"id": key})
        second = tx.execute(QUERY, {"id": key})
        assert first.rows == second.rows
        assert counts == {"keys": 1, "stable": 2}
        assert graph._queries._owner_budget._used_entries == 1
    assert graph._queries._owner_budget._used_entries == 0
    assert graph._queries._owner_budget._used_bytes == 0


@pytest.mark.parametrize("change", ["heap", "registry"])
def test_changed_local_authority_revalidates_the_key(graph, monkeypatch, change):
    counts = observe(monkeypatch)
    with graph.begin("read") as tx:
        first = tx.execute(QUERY, {"id": "0"}).rows
        if change == "heap":
            heap = graph._queries.heap
            heap._pool.discard_clean_file(heap.file)
        else:
            graph._queries._indexes._registry_revision += 1
        assert tx.execute(QUERY, {"id": "0"}).rows == first
    assert counts["keys"] == 2


@pytest.mark.parametrize("mode", ["read", "write"])
def test_foreign_writer_keeps_old_reader_and_new_transaction_sees_commit(graph, mode):
    with okto_grafx.connect(graph.path, page_size=8192) as writer:
        with graph.begin(mode) as reader:
            first = reader.execute(QUERY, {"id": "0"}).rows
            with writer.begin("write") as tx:
                tx.execute("MATCH (n:A {id:'0'}) SET n.title='updated'")
                tx.execute("CREATE (:A {id:'missing',title:'inserted',v:$v})", {"v": [2.0] * 4})
            assert reader.execute(QUERY, {"id": "0"}).rows == first
            assert reader.execute(QUERY, {"id": "missing"}).rows == ()
        assert graph.execute(QUERY, {"id": "0"}).rows[0][1] == "updated"
        assert graph.execute(QUERY, {"id": "missing"}).rows[0][1] == "inserted"


def test_writer_preflight_reuses_but_staging_returns_to_canonical_door(graph, monkeypatch):
    counts = observe(monkeypatch)
    with graph.begin("write") as tx:
        tx.execute(QUERY, {"id": "0"})
        assert counts["keys"] == 1
        counts.clear()
        tx.execute("MATCH (n:A {id:'0'}) SET n.id='changed', n.title='owned'")
        assert tx.execute(QUERY, {"id": "0"}).rows == ()
        assert tx.execute(QUERY, {"id": "changed"}).rows[0][1] == "owned"
    assert counts["keys"] == 0


def test_writer_preflight_records_same_read_partitions_as_scalar_oracle(graph, monkeypatch):
    def run():
        with graph.begin("write") as tx:
            results = [tx.execute(QUERY, {"id": key}).rows for key in ("0", "missing", "0")]
            assert not tx._context.wrote
            return results, set(tx._context.read_partitions)

    candidate = run()
    monkeypatch.setattr(qe, "_scalar_primary_key_group", lambda *_args: None)
    assert run() == candidate


def test_warmed_preflight_does_not_hide_foreign_write_conflict(graph):
    writer = graph.begin("write")
    try:
        before = writer.execute(QUERY, {"id": "0"}).rows
        assert writer.execute(QUERY, {"id": "0"}).rows == before
        with okto_grafx.connect(graph.path, page_size=8192) as other:
            with other.begin("write") as tx:
                tx.execute("MATCH (n:A {id:'0'}) SET n.title='winner'")
        assert writer.execute(QUERY, {"id": "0"}).rows == before
        writer.execute("MATCH (n:A {id:'0'}) SET n.title='must not publish'")
        with pytest.raises(GrafxWriteConflict):
            writer.commit()
    finally:
        if writer.active:
            writer.rollback()
    assert graph.execute(QUERY, {"id": "0"}).rows[0][1] == "winner"


@pytest.mark.parametrize("owner,name", [
    (HeapStore, "read"), (HeapStore, "_decode_version"), (HeapStore, "_read_slot"),
    (IndexManager, "validated_versions"), (IndexManager, "validated_versions_many"),
    (IndexManager, "_validated_items"),
])
def test_specialized_scalar_or_many_door_keeps_canonical_path(graph, monkeypatch, owner, name):
    counts = observe(monkeypatch)
    original = getattr(owner, name)

    def forwarded(*args, **kwargs):
        return original(*args, **kwargs)

    with graph.begin("read") as tx:
        expected = tx.execute(QUERY, {"id": "0"}).rows
        counts.clear()
        monkeypatch.setattr(owner, name, forwarded)
        assert tx.execute(QUERY, {"id": "0"}).rows == expected
        assert counts["keys"] == 0


def test_capacity_refusal_is_only_a_cache_miss(graph, monkeypatch):
    counts = observe(monkeypatch)
    engine = graph._queries
    engine._owner_budget = qe._OwnerLandingBudget(
        max_bytes=1, max_entries=1, guard=engine._endpoint_guard,
    )
    with graph.begin("read") as tx:
        first = tx.execute(QUERY, {"id": "0"}).rows
        assert tx.execute(QUERY, {"id": "0"}).rows == first
    assert counts["keys"] == 2
    assert engine._owner_budget._used_bytes == 0


def test_scalar_and_in_frontiers_share_only_same_snapshot_answers(graph, monkeypatch):
    counts = observe(monkeypatch)
    with graph.begin("read") as tx:
        first = tx.execute(QUERY, {"id": "0"}).rows
        assert tx.execute("MATCH (n:A) WHERE n.id IN $ids RETURN n.id,n.title,n.v",
                          {"ids": ["0"]}).rows == first
    assert counts["keys"] == 1
    assert counts["stable"] == 2


def test_candidate_equals_scalar_oracle_for_order_duplicates_and_full_vectors(graph, monkeypatch):
    def run():
        with graph.begin("read") as tx:
            return [tx.execute(QUERY, {"id": key}) for key in ("2", "0", "2", "absent")]

    candidate = run()
    monkeypatch.setattr(qe, "_scalar_primary_key_group", lambda *_args: None)
    oracle = run()
    assert [result.rows for result in candidate] == [result.rows for result in oracle]
    assert [result.columns for result in candidate] == [result.columns for result in oracle]
    assert sum(len(result.rows) for result in candidate) == 3


@pytest.mark.parametrize("mode", ["read", "write"])
def test_warm_cache_does_not_hide_a_failed_post_certificate(graph, monkeypatch, mode):
    with graph.begin(mode) as tx:
        tx.execute(QUERY, {"id": "0"})

        def refuse(*_args, **_kwargs):
            raise GrafxCorruptionDetected("post-read proof refused", field="checkpoint")

        monkeypatch.setattr(HashIndex, "finish_exact_read", refuse)
        with pytest.raises(GrafxCorruptionDetected) as candidate:
            tx.execute(QUERY, {"id": "0"})
        monkeypatch.setattr(qe, "_scalar_primary_key_group", lambda *_args: None)
        with pytest.raises(GrafxCorruptionDetected) as oracle:
            tx.execute(QUERY, {"id": "0"})
        assert candidate.value.to_dict() == oracle.value.to_dict()
    assert graph._queries._owner_budget._used_entries == 0


@pytest.mark.parametrize("mode", ["read", "write"])
def test_warm_cache_cannot_hide_a_specialized_slot_refusal(graph, monkeypatch, mode):
    with graph.begin(mode) as tx:
        tx.execute(QUERY, {"id": "0"})

        def refuse(*_args, **_kwargs):
            raise GrafxCorruptionDetected("slot proof refused", field="slot")

        monkeypatch.setattr(HeapStore, "_read_slot", refuse)
        with pytest.raises(GrafxCorruptionDetected) as candidate:
            tx.execute(QUERY, {"id": "0"})
        monkeypatch.setattr(qe, "_scalar_primary_key_group", lambda *_args: None)
        with pytest.raises(GrafxCorruptionDetected) as oracle:
            tx.execute(QUERY, {"id": "0"})
        assert candidate.value.to_dict() == oracle.value.to_dict()
