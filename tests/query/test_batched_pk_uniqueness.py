"""Certified CREATE probes preserve occurrence order and transaction uniqueness."""

import multiprocessing

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError, GrafxIndexError
from okto_grafx.engine import query_engine as qe
from okto_grafx.engine.index_manager import IndexManager, IndexStore


INSERT = "UNWIND $ids AS id CREATE (n:N {id: id}) RETURN n.id"


def _schema(db):
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")


def test_128_creates_use_two_certified_groups(tmp_path):
    with connect(tmp_path / "db") as db:
        _schema(db)
        with db.begin("write") as tx:
            result = tx.execute(INSERT, {"ids": list(range(128))})
            assert result.rows == tuple((i,) for i in range(128))
            assert result.statistics.get("pk_uniqueness_batches", 0) == 2
            assert result.statistics["certificate_attempts"] == 2
            assert result.statistics["index_probe_keys"] == 128
        assert db.execute("MATCH (n:N) RETURN count(n)").rows == ((128,),)


@pytest.mark.parametrize("ids", [[1, 1], [2, 1], [2, None], [2, "bad"], [1, "bad"]])
def test_failed_statement_rolls_back_all_occurrences(tmp_path, ids):
    with connect(tmp_path / "db") as db:
        _schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id: 1})")
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute(INSERT, {"ids": ids})
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)
            tx.execute("CREATE (:N {id: 3})")
        assert db.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))


def test_prior_statement_and_deleted_key_reconciliation(tmp_path):
    with connect(tmp_path / "db") as db:
        _schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id: 7})")
        with db.begin("write") as tx:
            tx.execute("MATCH (n:N {id: 7}) DELETE n")
            result = tx.execute(INSERT, {"ids": [7, 8]})
            assert result.statistics.get("pk_uniqueness_batches", 0) == 1
            with pytest.raises(GrafxError):
                tx.execute(INSERT, {"ids": [9, 8]})
        assert db.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == ((7,), (8,))


@pytest.mark.parametrize(
    "ids", [[1, 1], [2, 1], [2, None], [1, "bad"], [True, 3], [1 << 63, 3]]
)
def test_first_error_matches_scalar_oracle(tmp_path, monkeypatch, ids):
    with connect(tmp_path / "db") as db:
        _schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id: 1})")

        def failed():
            with db.begin("write") as tx:
                with pytest.raises(GrafxError) as caught:
                    tx.execute(INSERT, {"ids": ids})
                return type(caught.value), str(caught.value), caught.value.details

        actual = failed()
        monkeypatch.setattr(qe, "_create_primary_key_batch_index", lambda *_: None)
        assert failed() == actual


@pytest.mark.parametrize(
    "query,params",
    [
        ("UNWIND $ids AS id CREATE (:N {id: id + 0})", {"ids": [1, 2]}),
        ("UNWIND $ids AS id MERGE (:N {id: id})", {"ids": [1, 2]}),
        ("UNWIND $ids AS id CREATE (n:N {id: id}) SET n.id = id + 10", {"ids": [1, 2]}),
        ("UNWIND range(1, 2) AS id CREATE (:N {id: id})", {}),
    ],
)
def test_unproved_shapes_keep_scalar_path(tmp_path, query, params):
    with connect(tmp_path / "db") as db:
        _schema(db)
        with db.begin("write") as tx:
            assert (
                tx.execute(query, params).statistics.get("pk_uniqueness_batches", 0)
                == 0
            )


def test_missing_many_capability_uses_scalar(tmp_path, monkeypatch):
    with connect(tmp_path / "db") as db:
        _schema(db)
        monkeypatch.setattr(IndexManager, "validated_versions_many", None)
        with db.begin("write") as tx:
            result = tx.execute(INSERT, {"ids": [1, 2]})
            assert result.rows == ((1,), (2,))
            assert result.statistics.get("pk_uniqueness_batches", 0) == 0


def test_duplicate_after_batch_boundary_has_no_visible_prefix(tmp_path):
    with connect(tmp_path / "db") as db:
        _schema(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute(INSERT, {"ids": [*range(65), 0]})
        assert db.execute("MATCH (n:N) RETURN count(n)").rows == ((0,),)
    with connect(tmp_path / "db") as reopened:
        assert reopened.verify("all").findings == ()
        assert reopened.execute("MATCH (n:N) RETURN count(n)").rows == ((0,),)


def test_quota_uses_existing_scalar_memory_frontier(tmp_path):
    with connect(tmp_path / "db", query_memory_budget_bytes=4096) as db:
        _schema(db)
        with db.begin("write") as tx:
            result = tx.execute(INSERT, {"ids": [1, 2]})
            assert result.rows == ((1,), (2,))
            assert result.statistics.get("pk_uniqueness_batches", 0) == 0


def test_intermediate_row_quota_keeps_first_error_in_scalar_order(tmp_path, monkeypatch):
    with connect(tmp_path / "db", max_intermediate_rows=1) as db:
        _schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id: 1})")

        def first_error():
            with db.begin("write") as tx:
                with pytest.raises(GrafxError) as caught:
                    tx.execute(INSERT, {"ids": [1, 2]})
                return type(caught.value), caught.value.details

        actual = first_error()
        with monkeypatch.context() as scalar:
            scalar.setattr(qe, "_create_primary_key_batch_index", lambda *_: None)
            assert first_error() == actual
        assert actual[1]["constraint"] == "primary_key"
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)

    with connect(tmp_path / "clean", max_intermediate_rows=2) as db:
        _schema(db)
        with db.begin("write") as tx:
            result = tx.execute(INSERT, {"ids": [1, 2]})
            assert result.rows == ((1,), (2,))
            assert result.statistics.get("pk_uniqueness_batches", 0) == 0


def _competing_writer(path, barrier, results):
    try:
        with connect(path) as db:
            tx = db.begin("write")
            try:
                result = tx.execute(INSERT, {"ids": [1, 2]})
                assert result.statistics["pk_uniqueness_batches"] == 1
                barrier.wait(timeout=20)
                tx.commit()
                results.put("committed")
            except GrafxError as error:
                results.put((type(error).__name__, error.details))
            finally:
                if tx.active:
                    tx.rollback()
    except BaseException as error:
        results.put(("unexpected", repr(error)))


def test_two_processes_cannot_commit_same_keys(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        _schema(db)
    spawn = multiprocessing.get_context("spawn")
    barrier = spawn.Barrier(2)
    results = spawn.Queue()
    workers = [
        spawn.Process(target=_competing_writer, args=(path, barrier, results))
        for _ in range(2)
    ]
    try:
        for worker in workers:
            worker.start()
        outcomes = [results.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
        assert outcomes.count("committed") == 1, outcomes
        assert any(
            isinstance(value, tuple) and value[0] == "GrafxWriteConflict"
            for value in outcomes
        ), outcomes
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        results.close()
    with connect(path) as db:
        assert db.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == ((1,), (2,))
        assert db.verify("all").findings == ()


def test_failed_group_replays_scalar_in_order(tmp_path, monkeypatch):
    original = IndexStore._candidate_groups_unchecked

    def fail_group(self, keys):
        if len(keys) > 1:
            raise GrafxIndexError("Injected whole-group refusal", field="test")
        return original(self, keys)

    with connect(tmp_path / "db") as db:
        _schema(db)
        monkeypatch.setattr(IndexStore, "_candidate_groups_unchecked", fail_group)
        with db.begin("write") as tx:
            result = tx.execute(INSERT, {"ids": [1, 2]})
            assert result.rows == ((1,), (2,))
            assert result.statistics["pk_uniqueness_scalar_replays"] == 1


def test_later_corrupt_key_does_not_mask_prior_duplicate(tmp_path, monkeypatch):
    from okto_grafx.domain.index.keys import index_key

    original = IndexStore._candidate_groups_unchecked
    scalar = IndexStore._candidates_unchecked
    corrupt_key = index_key((2,), (0,))

    def corrupt(self, keys):
        if corrupt_key in keys:
            raise GrafxIndexError(
                "Injected corrupt key", field="test", reason="corrupt"
            )
        return original(self, keys)

    def corrupt_scalar(self, key):
        if key == corrupt_key:
            raise GrafxIndexError(
                "Injected corrupt key", field="test", reason="corrupt"
            )
        return scalar(self, key)

    with connect(tmp_path / "db") as db:
        _schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id: 1})")
        monkeypatch.setattr(IndexStore, "_candidate_groups_unchecked", corrupt)
        monkeypatch.setattr(IndexStore, "_candidates_unchecked", corrupt_scalar)
        with db.begin("write") as tx:
            with pytest.raises(GrafxError) as first:
                tx.execute(INSERT, {"ids": [1, 2]})
            assert first.value.details["constraint"] == "primary_key"
            with pytest.raises(GrafxIndexError, match="Injected corrupt key"):
                tx.execute(INSERT, {"ids": [2, 3]})
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)


def test_stale_index_preserves_uniqueness(tmp_path):
    with connect(tmp_path / "db") as db:
        _schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id: 1})")
        db._indexes.index("pk_N").mark_stale("test")
        with db.begin("write") as tx:
            with pytest.raises(GrafxError) as caught:
                tx.execute(INSERT, {"ids": [2, 1]})
            assert caught.value.details["constraint"] == "primary_key"
            result = tx.execute(INSERT, {"ids": [3, 4]})
            assert result.statistics.get("pk_uniqueness_batches", 0) == 0


def test_case_colliding_table_does_not_read_other_index(tmp_path):
    with connect(tmp_path / "db") as db:
        _schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE n(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:n {id: 1})")
        with db.begin("write") as tx:
            with pytest.raises(GrafxError) as caught:
                tx.execute(INSERT.replace(":N", ":n"), {"ids": [2, 1]})
            assert caught.value.details["constraint"] == "primary_key"
        assert db.execute("MATCH (n:n) RETURN n.id").rows == ((1,),)
        assert db.execute("MATCH (n:N) RETURN count(n)").rows == ((0,),)


def test_executemany_does_not_advance_generator_after_failure(tmp_path):
    seen = []

    def parameters():
        for values in ([1, 2], [3, 1], [4, 5]):
            seen.append(values)
            yield {"ids": values}

    with connect(tmp_path / "db") as db:
        _schema(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.executemany(INSERT.removesuffix(" RETURN n.id"), parameters())
            assert seen == [[1, 2], [3, 1]]
            assert tx.execute("MATCH (n:N) RETURN count(n)").rows == ((0,),)


def test_replaced_key_and_new_statement_use_fresh_authority(tmp_path):
    with connect(tmp_path / "db") as db:
        _schema(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id: 7})")
        with db.begin("write") as tx:
            tx.execute("MATCH (n:N {id: 7}) SET n.id = 9")
            result = tx.execute(INSERT, {"ids": [7, 8]})
            assert result.statistics["pk_uniqueness_batches"] == 1
            with pytest.raises(GrafxError) as caught:
                tx.execute(INSERT, {"ids": [10, 9]})
            assert caught.value.details["constraint"] == "primary_key"
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute(INSERT, {"ids": [10, 7]})
        assert db.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == (
            (7,),
            (8,),
            (9,),
        )


@pytest.mark.parametrize("method", ["read", "_decode_version", "_read_slot"])
def test_custom_heap_port_keeps_scalar_validation(tmp_path, monkeypatch, method):
    from okto_grafx.engine.heap_store import HeapStore

    original = getattr(HeapStore, method)

    def forwarded(*args, **kwargs):
        return original(*args, **kwargs)

    with connect(tmp_path / "db") as db:
        _schema(db)
        monkeypatch.setattr(HeapStore, method, forwarded)
        with db.begin("write") as tx:
            result = tx.execute(INSERT, {"ids": [1, 2]})
            assert result.statistics.get("pk_uniqueness_batches", 0) == 0
            assert result.rows == ((1,), (2,))


def test_exhausted_group_certificate_does_not_reset_retry_budget(tmp_path, monkeypatch):
    from okto_grafx.engine.index_manager import INDEX_READ_RETRY_BUDGET

    calls = []

    def changed(self, certificate, required_lsn):
        calls.append(certificate)
        return False

    with connect(tmp_path / "db") as db:
        _schema(db)
        with db.begin("write") as tx:
            with monkeypatch.context() as patch:
                patch.setattr(IndexStore, "finish_exact_read", changed)
                with pytest.raises(GrafxIndexError) as caught:
                    tx.execute(INSERT, {"ids": [1, 2]})
            assert caught.value.details["field"] == "index_view_changed"
            assert len(calls) == INDEX_READ_RETRY_BUDGET + 1
            assert tx.execute("MATCH (n:N) RETURN count(n)").rows == ((0,),)
