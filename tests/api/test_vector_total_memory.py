"""Aggregate picture admission counts held snapshots, not only active index caches."""

import gc

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxConfigurationError, GrafxQueryBudgetExceeded
from okto_grafx.engine.vector_memory import picture_tariff, work_tariff


def seed(db):
    with db.begin() as tx:
        tx.execute("CREATE VECTOR SPACE a {dimension:2,metric:'cosine'}")
        tx.execute("CREATE VECTOR SPACE b {dimension:2,metric:'cosine'}")
        tx.execute("CREATE NODE TABLE D(id INT64,v VECTOR(a),w VECTOR(b),PRIMARY KEY(id))")
        tx.execute("CREATE (:D {id:1,v:[1.0,0.0],w:[1.0,0.0]})")


def search(db, space):
    with db.begin("read") as reader:
        return db.search_vectors(reader, space=space, query=(1.0, 0.0), k=1)


def test_multiple_spaces_and_retired_held_picture_share_limit(tmp_path):
    resident = picture_tariff(1, 2, 16)
    ceiling = work_tariff(1, 2, 16, 200, cold=True) + resident - 1
    with connect(tmp_path / "db", vector_exact_scan_threshold=0,
                 vector_hnsw_total_memory_budget_bytes=ceiling) as db:
        seed(db)
        assert search(db, "a").hits
        usage = db.vector_total_memory_usage()
        assert usage.pictures == 1 and usage.reserved_bytes == resident
        with pytest.raises(GrafxQueryBudgetExceeded) as refused:
            search(db, "b")
        assert refused.value.details["resource"] == "vector_hnsw_total_memory"
        index = db._vectors._by_space["a"]
        held = index._snapshot
        index._retire(held)
        assert db.vector_total_memory_usage().reserved_bytes == resident
        del held
        gc.collect()
        assert db.vector_total_memory_usage().reserved_bytes == 0
        assert search(db, "b").hits
        assert db.vector_total_memory_usage().peak_reserved_bytes <= ceiling
        assert not db.verify().findings


def test_failed_build_releases_reservation_and_exact_reader_remains_usable(tmp_path):
    with connect(tmp_path / "db", vector_exact_scan_threshold=0,
                 vector_hnsw_total_memory_budget_bytes=1) as db:
        seed(db)
        for _ in range(2):
            with pytest.raises(GrafxQueryBudgetExceeded):
                search(db, "a")
            assert db.vector_total_memory_usage().reserved_bytes == 0
        assert db.execute("MATCH (d:D) RETURN d.id").rows == ((1,),)
    with connect(tmp_path / "db") as independent:
        assert search(independent, "a").hits
        assert independent.vector_total_memory_usage().limit_bytes is None


def test_aggregate_warm_pressure_does_not_fail_committed_writes(tmp_path):
    ceiling = work_tariff(1, 2, 16, 200, cold=True)
    with connect(tmp_path / "db", vector_exact_scan_threshold=0,
                 vector_hnsw_total_memory_budget_bytes=ceiling) as db:
        seed(db)
        assert search(db, "a").hits
        for i in range(2, 9):
            with db.begin() as tx:
                tx.execute("CREATE (:D {id:$i,v:[1.0,0.0],w:[1.0,0.0]})", {"i": i})
        assert db.execute("MATCH (d:D) RETURN count(d)").rows == ((8,),)
        assert db.vector_memory_usage("a").warm_retirements == 1
        with pytest.raises(GrafxQueryBudgetExceeded):
            search(db, "a")
        assert not db.verify().findings


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_invalid_total_budget_is_rejected_before_storage(tmp_path, bad):
    with pytest.raises(GrafxConfigurationError):
        connect(tmp_path / "db", vector_hnsw_total_memory_budget_bytes=bad)
    assert not (tmp_path / "db").exists()


def test_custom_vector_collaborator_without_diagnostic_refuses_typed(tmp_path):
    from okto_grafx.errors import GrafxUnsupportedOperation
    with connect(tmp_path / "db") as db:
        original = db._vectors
        try:
            db._vectors = object()
            with pytest.raises(GrafxUnsupportedOperation):
                db.vector_total_memory_usage()
        finally:
            db._vectors = original
