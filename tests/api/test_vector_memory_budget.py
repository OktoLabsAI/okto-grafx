"""HNSW budget admission, cache retirement and independent native participants."""

from dataclasses import FrozenInstanceError

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxConfigurationError, GrafxQueryBudgetExceeded
from okto_grafx.engine.vector_memory import work_tariff


def seed(db, count=1):
    with db.begin() as tx:
        tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
        tx.execute("CREATE NODE TABLE D(id INT64,v VECTOR(s),PRIMARY KEY(id))")
        for i in range(count):
            tx.execute("CREATE (:D {id:$id,v:$v})", {"id": i, "v": [1.0, float(i)]})


def search(db, reader):
    return db.search_vectors(reader, space="s", query=(1.0, 0.0), k=1)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "1024"])
def test_invalid_budget_refuses_before_storage(tmp_path, value):
    target = tmp_path / "db"
    with pytest.raises(GrafxConfigurationError) as raised:
        connect(target, vector_hnsw_memory_budget_bytes=value)
    assert raised.value.details["field"] == "vector_hnsw_memory_budget_bytes"
    assert not target.exists()


def test_cold_refusal_leaves_reader_and_builder_usable(tmp_path):
    with connect(tmp_path / "db", vector_exact_scan_threshold=0,
                 vector_hnsw_memory_budget_bytes=1) as db:
        seed(db)
        assert db.vector_memory_usage("s").cached_entries == 0
        with db.begin("read") as reader:
            for _ in range(2):
                with pytest.raises(GrafxQueryBudgetExceeded) as raised:
                    search(db, reader)
                assert raised.value.details["resource"] == "vector_hnsw_memory"
                assert reader.execute("MATCH (d:D) RETURN count(d)").rows == ((1,),)
        observed = db.vector_memory_usage("s")
        assert observed.budget_refusals == 2 and observed.cached_entries == 0
        assert observed.peak_requested_bytes > observed.limit_bytes
        with pytest.raises(FrozenInstanceError):
            observed.cached_entries = 999
        assert not db.verify("all").findings


def test_header_reservation_precedes_vector_resolution(tmp_path, monkeypatch):
    ceiling = work_tariff(1, 2, 16, 200, cold=True)
    with connect(tmp_path / "db", vector_exact_scan_threshold=0,
                 vector_hnsw_memory_budget_bytes=ceiling) as db:
        seed(db, 2)
        from okto_grafx.engine.vector_engine import VectorHnswIndex

        def unexpected(*args):
            raise AssertionError("Heap resolution began before complete header admission")

        monkeypatch.setattr(VectorHnswIndex, "_install", unexpected)
        with db.begin("read") as reader:
            with pytest.raises(GrafxQueryBudgetExceeded) as raised:
                search(db, reader)
        assert raised.value.details["requested_bytes"] == work_tariff(2, 2, 16, 200, cold=True)
        assert db.vector_memory_usage("s").cached_entries == 0


def test_warm_growth_retires_cache_without_failing_durable_commit(tmp_path):
    ceiling = work_tariff(1, 2, 16, 200, cold=True)
    path = tmp_path / "db"
    with connect(path, vector_exact_scan_threshold=0,
                 vector_hnsw_memory_budget_bytes=ceiling) as db:
        seed(db)
        with db.begin("read") as reader:
            assert search(db, reader).hits
        before = db.vector_memory_usage("s")
        assert before.cached_entries == 1 and before.cached_logical_bytes < ceiling
        for i in range(1, 8):
            with db.begin() as tx:
                tx.execute("CREATE (:D {id:$id,v:$v})", {"id": i, "v": [1.0, float(i)]})
        observed = db.vector_memory_usage("s")
        assert observed.warm_retirements == 1 and observed.cached_entries == 0
        with db.begin("read") as reader:
            assert reader.execute("MATCH (d:D) RETURN count(d)").rows == ((8,),)
            with pytest.raises(GrafxQueryBudgetExceeded):
                search(db, reader)
        assert not db.verify("all").findings
    with connect(path, vector_exact_scan_threshold=0) as reopened:
        with reopened.begin("read") as reader:
            assert search(reopened, reader).hits
        assert reopened.vector_memory_usage("s").cached_entries == 8


def test_exact_path_and_independent_handle_do_not_inherit_ann_limit(tmp_path):
    path = tmp_path / "db"
    with connect(path, vector_hnsw_memory_budget_bytes=1) as exact:
        seed(exact, 4)
        with exact.begin("read") as reader:
            expected = search(exact, reader).hits
        assert exact.vector_memory_usage("s").peak_requested_bytes == 0
        with connect(path, vector_exact_scan_threshold=0) as independent:
            with independent.begin("read") as reader:
                assert search(independent, reader).hits == expected
            assert independent.vector_memory_usage("s").cached_entries == 4
        assert exact.vector_memory_usage("s").limit_bytes == 1


def test_budgeted_and_unbounded_cold_warm_rankings_agree(tmp_path):
    path = tmp_path / "db"
    with connect(path, vector_exact_scan_threshold=0) as first:
        seed(first, 12)
        with first.begin("read") as reader:
            expected = search(first, reader)
        with connect(path, vector_exact_scan_threshold=0,
                     vector_hnsw_memory_budget_bytes=work_tariff(12, 2, 16, 200, cold=True)) as bounded:
            with bounded.begin("read") as reader:
                assert search(bounded, reader).hits == expected.hits
                assert search(bounded, reader).hits == expected.hits
            assert bounded.vector_memory_usage("s").budget_refusals == 0
