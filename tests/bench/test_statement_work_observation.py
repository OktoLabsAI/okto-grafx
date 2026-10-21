"""RF01 exact-read physical work, isolated from result multiplicity and quotas."""

from okto_grafx import connect
from contextvars import copy_context

import pytest

from okto_grafx.engine.index_manager import HashIndex
from okto_grafx.engine.statement_work import StatementWorkScope
from okto_grafx.runtime.scoped_value import ContextLocalValue
from tests.index.conftest import SnapshotDouble
from tests.index.test_validated_versions_many import BORN, _keys, _populated
from tests.query import test_batched_landing_without_vectors as landing_fixture
from okto_grafx.domain.index import RECORD_ID_KEY_DERIVATION
from okto_grafx.domain.index.keys import record_id_key

graph = landing_fixture.graph


def _observed_index(count=2):
    database, refs = _populated(count)
    scope = StatementWorkScope(ContextLocalValue("test_statement_work"))
    database.heap._statement_work = scope
    database.manager._statement_work = scope
    database.exact._statement_work = scope
    return database, refs, scope


def test_statement_index_work_observes_actual_probes(tmp_path):
    """A real native PK seek exposes work, not just its one visible result."""
    with connect(tmp_path / "db") as db:
        with db.transaction("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
        with db.transaction("write") as txn:
            txn.execute("CREATE (:Person {id: 1, name: 'one'})")
        result = db.execute("MATCH (p:Person {id: 1}) RETURN p.name")
        assert result.rows == (("one",),)
        assert result.statistics["index_probe_keys"] == 1
        assert result.statistics["certificate_attempts"] == 1
        assert result.statistics.get("certificate_retries", 0) == 0
        assert result.statistics["physical_node_versions_examined"] == 1


@pytest.mark.parametrize("visible", [True, False])
def test_grouped_probes_count_distinct_keys_and_invisible_versions(visible):
    database, _, scope = _observed_index()
    keys = _keys(database, 2)
    keys += [keys[0], database.key(999, "missing")]
    stats = {}
    with scope.bind(stats):
        groups = database.manager.validated_versions_many(
            database.exact, keys, SnapshotDouble(BORN if visible else BORN - 1)
        )
    assert tuple(map(len, groups)) == ((1, 1, 1, 0) if visible else (0, 0, 0, 0))
    assert stats["index_probe_keys"] == 3
    assert stats["physical_node_versions_examined"] == 2
    assert stats["certificate_attempts"] == 1
    assert "landing_candidates" not in stats  # This is a column-derived index.


def test_certificate_retry_counts_discarded_work_without_fallback(monkeypatch):
    database, _, scope = _observed_index()
    finish = HashIndex.finish_exact_read
    attempts = []

    def changed_once(store, certificate, lsn):
        accepted = finish(store, certificate, lsn)
        attempts.append(1)
        return accepted and len(attempts) > 1

    monkeypatch.setattr(HashIndex, "finish_exact_read", changed_once)
    stats = {}
    with scope.bind(stats):
        groups = database.manager.validated_versions_many(
            database.exact, _keys(database, 2), SnapshotDouble(BORN)
        )
    assert tuple(map(len, groups)) == (1, 1)
    assert stats == {"certificate_attempts": 2, "certificate_retries": 1,
                     "index_probe_keys": 4, "physical_node_versions_examined": 4}


def test_cached_groups_keep_certificate_but_skip_probes_and_heap_reads():
    database, _, scope = _observed_index()
    keys = _keys(database, 2)
    snapshot = SnapshotDouble(BORN)
    generation, _, groups = database.manager.validated_versions_many_reusing(
        database.exact, keys, snapshot, generation=None, cached={}
    )
    stats = {}
    with scope.bind(stats):
        _, reusable, answer = database.manager.validated_versions_many_reusing(
            database.exact, keys, snapshot, generation=generation, cached=dict(zip(keys, groups))
        )
    assert reusable and answer == groups
    assert stats == {"certificate_attempts": 1}


def test_nested_scopes_restore_and_copied_contexts_are_revoked():
    scope = StatementWorkScope(ContextLocalValue("test_revocation"))
    parent, child = {}, {}
    with scope.bind(parent):
        with pytest.raises(ValueError), scope.bind(child):
            delayed = copy_context()
            assert scope.current_statistics() is child
            raise ValueError("exit")
        assert scope.current_statistics() is parent
        assert delayed.run(scope.current_statistics) is None
    assert scope.current_statistics() is None


@pytest.mark.parametrize("method", ["validated_identity_landings_many", "validated_identity_counts_many"])
def test_identity_batches_separate_physical_candidates_from_repeated_outputs(graph, method):
    index = next(index for index in graph._indexes.active_indexes()
                 if index.definition.table_name == "B"
                 and index.definition.key_derivation == RECORD_ID_KEY_DERIVATION)
    scope = graph._queries._statement_work
    stats = {}
    keys = tuple(record_id_key(i) for i in (1, 2, 1, 999999))
    with graph.begin("read") as reader, scope.bind(stats):
        answer = getattr(graph._indexes, method)(index, keys, reader._context.snapshot)
    assert len(answer) == 4
    assert stats["landing_batches"] == 1
    assert stats["landing_candidates"] == 2
    assert stats["index_probe_keys"] == 3
    assert stats["physical_node_versions_examined"] == 2


def test_interleaved_cursors_restore_binding_and_keep_statistics_separate(graph):
    scope = graph._queries._statement_work
    query = "MATCH (b:B {id: $id}) RETURN b.title"
    with graph.query(query, {"id": "b0"}).cursor(batch_size=1) as first:
        with graph.query(query, {"id": "b1"}).cursor(batch_size=1) as second:
            assert next(first) == ("Title 0",)
            before = dict(first.statistics)
            assert scope.current_statistics() is None
            assert next(second) == ("Title 1",)
            assert first.statistics == before
            assert first.statistics["index_probe_keys"] == second.statistics["index_probe_keys"] == 1
            assert scope.current_statistics() is None
    assert scope.current_statistics() is None


@pytest.mark.parametrize("door", ["scan_page", "visible_cursor"])
def test_header_only_scans_observe_invisible_versions(door):
    database, _, scope = _observed_index()
    table = database.heap.catalog.catalog.table_by_id(database.exact.definition.table_id)
    stats = {}
    with scope.bind(stats):
        if door == "scan_page":
            rows, position = database.heap.scan_page(table, SnapshotDouble(BORN - 1), limit=10)
            assert rows == () and position is None
        else:
            cursor = database.heap._visible_record_cursor(table, SnapshotDouble(BORN - 1), admit_page=lambda: None)
            assert cursor.next_visible() is None
    assert stats == {"physical_node_versions_examined": 2}


def test_write_result_does_not_absorb_commit_or_later_query_work(graph):
    scope = graph._queries._statement_work
    with graph.begin("write") as tx:
        result = tx.execute("MATCH (b:B {id: 'b0'}) SET b.title = 'changed' RETURN b.title")
        before = dict(result.statistics)
        assert before["index_probe_keys"] >= 1
        assert scope.current_statistics() is None
    assert dict(result.statistics) == before
    assert graph.execute("MATCH (b:B {id: 'b0'}) RETURN b.title").rows == (("changed",),)
    assert dict(result.statistics) == before
    assert scope.current_statistics() is None


@pytest.mark.parametrize("settlement", ["close", "exhaustion", "error"])
def test_cursor_sink_callbacks_run_after_observation_scope_exits(graph, settlement):
    from okto_grafx.errors import GrafxError

    scope = graph._queries._statement_work
    observed = []

    class Sink:
        enabled = True

        def observe(self, *_args):
            observed.append(scope.current_statistics())

        def increment(self, *_args):
            observed.append(scope.current_statistics())

    original = graph._queries._metrics
    graph._queries._metrics = Sink()
    try:
        query = "MATCH (b:B) RETURN b.id"
        params = {}
        if settlement == "error":
            query = "MATCH (b:B) RETURN 1 / $zero"
            params = {"zero": 0}
        with graph.query(query, params).cursor(batch_size=1) as cursor:
            if settlement == "error":
                with pytest.raises(GrafxError):
                    next(cursor)
            elif settlement == "exhaustion":
                assert len(tuple(cursor)) == 3
            else:
                next(cursor)
    finally:
        graph._queries._metrics = original
    assert observed
    assert all(item is None for item in observed)
