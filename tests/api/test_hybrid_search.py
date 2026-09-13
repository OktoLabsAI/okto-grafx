"""Real native FTS/vector fusion; no source-result mocks."""

import pytest
from okto_grafx import connect, HybridSearchOptions, CancellationToken
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.errors import (
    GrafxConfigurationError,
    GrafxQueryBudgetExceeded,
    GrafxQueryCancelled,
    GrafxUnsupportedOperation,
)


def seed(path):
    db = connect(path)
    with db.begin() as tx:
        tx.execute("CREATE VECTOR SPACE semantic {dimension: 2, metric: 'cosine'}")
        tx.execute(
            "CREATE NODE TABLE Doc(id INT64, body STRING, embedding VECTOR(semantic), PRIMARY KEY(id))"
        )
        tx.execute("CREATE REL TABLE Link(FROM Doc TO Doc)")
        for rid, body, vector in [
            (1, "wal durable", [1.0, 0.0]),
            (2, "wal", [0.8, 0.2]),
            (3, "other", [0.0, 1.0]),
        ]:
            tx.execute(
                "CREATE (:Doc {id:$id, body:$body, embedding:$v})",
                {"id": rid, "body": body, "v": vector},
            )
        tx.execute("MATCH (a:Doc {id:1}), (b:Doc {id:2}) CREATE (a)-[:Link]->(b)")
    db.create_text_index("text", "Doc", ("body",))
    db.ensure_identity_indexes()
    return db


def search(db, reader=None, **kwargs):
    return db.search_hybrid(
        reader,
        table="Doc",
        index="text",
        query="wal",
        space="semantic",
        vector=(1.0, 0.0),
        k=3,
        **kwargs,
    )


def test_native_rrf_sources_and_filters(tmp_path):
    with seed(tmp_path / "db") as db:
        result = search(db)
        assert {h.record_id for h in result.hits} == {1, 2, 3}
        assert result.regime == "complete" and result.vector_regime != "disabled"
        for h in result.hits:
            expected = (1 / (60 + h.lexical_rank) if h.lexical_rank else 0) + (
                1 / (60 + h.vector_rank) if h.vector_rank else 0
            )
            assert h.score == expected
        assert {
            h.record_id
            for h in search(db, options=HybridSearchOptions(fusion="intersection")).hits
        } == {1, 2}
        assert [
            h.record_id for h in search(db, filter=RecordIdFilter.of([2])).hits
        ] == [2]
        assert not search(db, filter=RecordIdFilter.of([])).hits
        lexical = search(db, options=HybridSearchOptions(vector_weight=0))
        assert [h.record_id for h in lexical.hits] == [
            h.record_id for h in db.search_text(index="text", query="wal").hits
        ]


@pytest.mark.parametrize("direction", ["out", "in", "both"])
def test_incident_matches_scan_and_skips_unrelated_edges(tmp_path, direction):
    from dataclasses import replace

    with seed(tmp_path / "db") as db:
        with db.begin() as tx:
            for _ in range(20):
                tx.execute("MATCH (d:Doc {id:3}) CREATE (d)-[:Link]->(d)")
            tx.execute("MATCH (a:Doc {id:1}), (b:Doc {id:2}) CREATE (b)-[:Link]->(a)")
        options = HybridSearchOptions(graph_relations=("Link",), graph_seeds=(1,),
                                      graph_direction=direction, graph_hops=2,
                                      graph_weight=2, graph_filter=True)
        with db.begin("read") as reader:
            indexed = search(db, reader, options=options)
            scanned = search(db, reader, options=replace(options, graph_access="scan"))
            assert indexed.hits == scanned.hits
            assert indexed.graph_regime == "incident_index"
            assert scanned.graph_regime == "scan"
            assert indexed.graph_edges_visited == 2
            assert scanned.graph_edges_visited == 22
            with db.begin() as tx:
                tx.execute("MATCH (a:Doc {id:2}), (b:Doc {id:3}) CREATE (a)-[:Link]->(b)")
            assert search(db, reader, options=options).hits == indexed.hits
        assert not db.verify("all").findings


def test_incident_refusal_never_falls_back(tmp_path, monkeypatch):
    from okto_grafx.engine.index_manager import IndexStore
    from okto_grafx.errors import GrafxCorruptionDetected

    original = IndexStore._scan_bucket

    def damaged(self, *args, **kwargs):
        if self.definition.name.lower() == "ef_link":
            raise GrafxCorruptionDetected("test damaged endpoint bucket")
        return original(self, *args, **kwargs)

    with seed(tmp_path / "db") as db:
        monkeypatch.setattr(IndexStore, "_scan_bucket", damaged)
        with pytest.raises(GrafxCorruptionDetected):
            search(db, options=HybridSearchOptions(graph_relations=("Link",),
                                                  graph_seeds=(1,), graph_weight=1))


@pytest.mark.parametrize("phase", ["exact", "cold_ann", "warm_ann"])
def test_vector_cancellation_inside_work_releases_resources(tmp_path, monkeypatch, phase):
    from okto_grafx.engine.heap_store import HeapStore
    from okto_grafx.engine.vector_engine import VectorHnswIndex
    from okto_grafx.domain.vector.hnsw import HnswGraph

    with seed(tmp_path / "db"):
        pass
    with connect(tmp_path / "db", vector_exact_scan_threshold=100 if phase == "exact" else 0) as db:
        token = CancellationToken()
        calls = []
        if phase == "warm_ann":
            search(db)
            original = HnswGraph._scorer

            def scorer(self, query):
                score = original(self, query)

                def scored(node):
                    calls.append(node)
                    token.cancel()
                    return score(node)

                return scored

            monkeypatch.setattr(HnswGraph, "_scorer", scorer)
        elif phase == "cold_ann":
            original = VectorHnswIndex._install

            def install(self, *args, **kwargs):
                result = original(self, *args, **kwargs)
                calls.append(1)
                token.cancel()
                return result

            monkeypatch.setattr(VectorHnswIndex, "_install", install)
        else:
            original = HeapStore.read_if

            def read(self, *args, **kwargs):
                result = original(self, *args, **kwargs)
                calls.append(1)
                token.cancel()
                return result

            monkeypatch.setattr(HeapStore, "read_if", read)
        with db.begin("read") as reader:
            with pytest.raises(GrafxQueryCancelled):
                db.search_vectors(reader, space="semantic", query=(1.0, 0.0), k=3,
                                  cancellation=token)
            assert calls and len(calls) < 3
            assert reader.active
        monkeypatch.undo()
        assert len(search(db).hits) == 3
        with db.begin() as tx:
            tx.execute("MATCH (d:Doc {id:1}) SET d.body='wal still writable'")
        assert not db.verify("all").findings


def test_vector_deadline_and_invalid_control(tmp_path):
    from okto_grafx.errors import GrafxQueryDeadlineExceeded

    with seed(tmp_path / "db") as db, db.begin("read") as reader:
        with pytest.raises(GrafxQueryDeadlineExceeded):
            db.search_vectors(reader, space="semantic", query=(1.0, 0.0), k=3,
                              timeout_seconds=1e-30)
        with pytest.raises(GrafxConfigurationError):
            db.search_vectors(reader, space="semantic", query=(1.0, 0.0), k=3,
                              timeout_seconds=False)


def test_hybrid_aggregate_budget_and_phase_diagnostics(tmp_path):
    from dataclasses import replace

    with seed(tmp_path / "db") as db:
        options = HybridSearchOptions(candidate_k=3)
        result = search(db, options=options)
        assert result.memory_peak_bytes > options.candidate_k * 1024
        assert result.lexical_memory_peak_bytes > 0
        assert result.vector_memory_peak_bytes > 0
        assert result.graph_memory_peak_bytes == 0
        with pytest.raises(GrafxQueryBudgetExceeded):
            search(db, options=replace(options, max_memory_bytes=options.candidate_k * 1024))
        assert search(db, options=options).hits == result.hits
        graph = search(db, options=replace(options, graph_relations=("Link",),
                                           graph_seeds=(1,), graph_weight=1))
        assert graph.graph_memory_peak_bytes > 0
        assert graph.memory_peak_bytes <= options.max_memory_bytes


def test_graph_filter_boost_cycle_and_budget(tmp_path):
    with seed(tmp_path / "db") as db:
        opts = HybridSearchOptions(
            graph_relations=("Link",),
            graph_seeds=(1,),
            graph_weight=2,
            graph_filter=True,
        )
        result = search(db, options=opts)
        assert {h.record_id: h.graph_distance for h in result.hits} == {1: 0, 2: 1}
        assert result.graph_edges_visited == 1
        with pytest.raises(GrafxConfigurationError):
            search(db, options=opts, filter=RecordIdFilter.of([2]))
        with db.begin() as tx:
            tx.execute("MATCH (a:Doc {id:1}), (b:Doc {id:2}) CREATE (b)-[:Link]->(a)")
        with pytest.raises(GrafxQueryBudgetExceeded):
            search(
                db,
                options=HybridSearchOptions(
                    graph_relations=("Link",),
                    graph_seeds=(1,),
                    graph_weight=1,
                    max_graph_edges=1,
                    graph_access="scan",
                ),
            )


def test_missing_source_partial_is_explicit_and_snapshot_is_fixed(tmp_path):
    with seed(tmp_path / "db") as db, db.begin("read") as old:
        with pytest.raises(GrafxUnsupportedOperation):
            db.search_hybrid(
                table="Doc",
                index="missing",
                query="wal",
                space="semantic",
                vector=(1.0, 0.0),
            )
        partial = db.search_hybrid(
            table="Doc",
            index="missing",
            query="wal",
            space="semantic",
            vector=(1.0, 0.0),
            options=HybridSearchOptions(allow_partial=True),
        )
        assert partial.regime == "partial" and partial.source_errors == (
            ("lexical", "missing_index"),
        )
        before = search(db, old)
        with db.begin() as tx:
            tx.execute("MATCH (d:Doc {id:3}) SET d.body='wal'")
        assert search(db, old).hits == before.hits
        assert search(db).lexical_candidates == 3
        token = CancellationToken()
        token.cancel()
        with pytest.raises(GrafxQueryCancelled):
            search(db, cancellation=token)
        with pytest.raises(GrafxQueryBudgetExceeded):
            search(db, options=HybridSearchOptions(max_memory_bytes=1))


def test_real_approximate_source_reopen_and_reference_recall(tmp_path):
    with seed(tmp_path / "db"):
        pass
    with (
        connect(
            tmp_path / "db", vector_exact_scan_threshold=0, vector_ef_search=32
        ) as db,
        db.begin("read") as reader,
    ):
        result = search(db, reader)
        native = db.search_vectors(reader, space="semantic", query=(1.0, 0.0), k=3)
        assert result.vector_regime == native.regime
        assert result.vector_regime not in ("exact_scan", "disabled", "unavailable")
        assert {h.record_id for h in result.hits} == {1, 2, 3}
        assert result.lexical_index_built_through_commit is not None
        vector_only = search(db, reader, options=HybridSearchOptions(lexical_weight=0))
        assert [h.record_id for h in vector_only.hits] == [
            h.record_id for h in native.hits
        ]


@pytest.mark.parametrize(
    "values",
    [
        dict(lexical_weight=float("nan")),
        dict(vector_weight=-1),
        dict(candidate_k=True),
        dict(fusion="fallback"),
        dict(graph_hops=0),
        dict(allow_partial=1),
        dict(lexical_weight=0, vector_weight=0),
        dict(graph_weight=1),
        dict(graph_direction="sideways"),
    ],
)
def test_invalid_options(values):
    with pytest.raises(GrafxConfigurationError):
        HybridSearchOptions(**values)


def test_partial_does_not_swallow_source_failure_or_accept_ambiguous_binding(
    tmp_path, monkeypatch
):
    import okto_grafx.engine.fulltext as text
    from okto_grafx.errors import GrafxCorruptionDetected, GrafxVectorValidationError

    with seed(tmp_path / "db") as db:
        with monkeypatch.context() as scoped:

            def fail(*a, **k):
                raise GrafxCorruptionDetected("damaged")

            scoped.setattr(text, "search_text", fail)
            with pytest.raises(GrafxCorruptionDetected):
                search(db, options=HybridSearchOptions(allow_partial=True))
        with pytest.raises(GrafxVectorValidationError):
            db.search_hybrid(
                table="Doc",
                index="text",
                query="wal",
                space="semantic",
                vector=(float("nan"), 0.0),
            )
        with pytest.raises(GrafxConfigurationError):
            search(
                db,
                options=HybridSearchOptions(
                    graph_relations=("Link",), graph_seeds=(99,), graph_filter=True
                ),
            )
        with db.begin() as tx:
            tx.execute(
                "CREATE NODE TABLE Other(id INT64, v VECTOR(semantic), PRIMARY KEY(id))"
            )
        # A second physical owner no longer makes the explicit Doc target
        # ambiguous. Its independent vector index must not replace Doc's.
        result = search(db, options=HybridSearchOptions(allow_partial=True))
        assert result.regime == "complete"
        assert {hit.record_id for hit in result.hits} == {1, 2, 3}


def test_reader_ownership_and_empty_graph_direction(tmp_path):
    from okto_grafx.errors import GrafxTransactionStateError

    with seed(tmp_path / "db") as db, connect(tmp_path / "foreign") as foreign:
        with foreign.begin("read") as reader:
            with pytest.raises(GrafxTransactionStateError):
                search(db, reader)
        with db.begin() as writer:
            with pytest.raises(GrafxTransactionStateError):
                search(db, writer)
        with db.begin("read") as closed:
            pass
        with pytest.raises(GrafxTransactionStateError):
            search(db, closed)
        opts = HybridSearchOptions(
            graph_relations=("Link",),
            graph_seeds=(2,),
            graph_direction="in",
            graph_filter=True,
        )
        assert {
            h.record_id: h.graph_distance for h in search(db, options=opts).hits
        } == {1: 1, 2: 0}
        result = db.search_hybrid(
            table="Doc",
            index="text",
            query="absent",
            space=None,
            vector=(),
            options=HybridSearchOptions(vector_weight=0),
        )
        assert (
            not result.hits
            and result.regime == "complete"
            and result.lexical_candidates == 0
        )
