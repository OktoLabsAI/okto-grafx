"""Copy native entity identity, not equality of heterogeneous property bags."""

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import re

import pytest

from okto_grafx import connect
from okto_grafx.catalog_copy import CopyLimits, _digest, capture_copy, copy_graph, prepare_copy_target
from okto_grafx.domain.model.value import encode_values
from okto_grafx.errors import GrafxError


def prepare(db, *, pad=False):
    db.ensure_identity_indexes()
    with db.begin("write") as tx:
        if pad:
            tx.execute("CREATE NODE TABLE Pad(id INT64, PRIMARY KEY(id))")
        tx.execute("CREATE NODE TABLE Keyed(id INT64, title STRING, PRIMARY KEY(id))")
        tx.execute("CREATE(a {v:'same'})-[r:R {v:[1,'text']}]->(b {v:'same'})")
        tx.execute("CREATE(k:Keyed {id:1,title:'source'})")
        tx.execute("MATCH(a),(k:Keyed) WHERE a.v='same' CREATE(a)-[:S {v:1}]->(k)")


@pytest.fixture
def pair(tmp_path):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        prepare(source)
        prepare(target, pad=True)
        # Keep the schemas, deliberately leave an equal unlabeled node in target.
        with target.begin("write") as tx:
            tx.execute("MATCH(n) DETACH DELETE n")
            tx.execute("CREATE({v:'same'})")
        source.enable_commit_history()
        prepare_copy_target(target)
        with source.begin("write") as tx:
            tx.execute("MATCH()-[r:R]->() SET r.extra={nested:[true,null,2.5]}")
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=tuple(t.name for t in source.catalog.catalog.tables()))
        yield source, target, package


def assert_graph(db):
    assert db.execute("MATCH(n {v:'same'}) RETURN count(n)").rows == ((3,),)
    result = db.execute("MATCH(a)-[r:R]->(b) RETURN a,r,b")
    assert len(result.rows) == 1
    a, r, b = result.rows[0]
    assert a.identity != b.identity and a.labels == b.labels == ()
    assert dict(a.properties) == dict(b.properties) == {"v": "same"}
    assert r.source == a.identity and r.target == b.identity
    assert r.properties["v"] == (1, "text")
    assert db.execute("MATCH(a)-[:S]->(b:Keyed) RETURN b.id").rows == ((1,), (1,))
    assert db.verify("all").findings == ()


def test_flexible_copy_distinct_identities_pairs_snapshots_and_replay(pair):
    source, target, package = pair
    source_unlabeled = next(t for t in source.catalog.catalog.tables() if t.unlabeled)
    target_unlabeled = next(t for t in target.catalog.catalog.tables() if t.unlabeled)
    assert source_unlabeled.name != target_unlabeled.name
    reader = target.begin("read")
    try:
        receipt = copy_graph(package, target, idempotency_key="flex")
        assert receipt.rows == 6
        assert reader.execute("MATCH(n {v:'same'}) RETURN count(n)").rows == ((1,),)
    finally:
        reader.rollback()
    assert_graph(target)
    sequence = target.transactions.published_state().last_committed_lsn
    assert copy_graph(package, target, idempotency_key="flex") == replace(receipt, replayed=True)
    assert target.transactions.published_state().last_committed_lsn == sequence


def test_skip_only_keyed_nodes_not_equal_bags(pair):
    _, target, package = pair
    with target.begin("write") as tx:
        tx.execute("CREATE(:Keyed {id:1,title:'preserved'})")
    receipt = copy_graph(package, target, idempotency_key="skip", conflict="skip")
    assert (receipt.rows, receipt.skipped_rows) == (5, 1)
    assert target.execute("MATCH(n:Keyed) RETURN n.title").rows == (("preserved",),)
    assert_graph(target)


def test_no_pk_typed_nodes_preserve_multiplicity_and_self_edges(tmp_path):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        for db in (source, target):
            db.ensure_identity_indexes()
            with db.begin("write") as tx:
                tx.execute("CREATE NODE TABLE N(value STRING)")
                tx.execute("CREATE REL TABLE R(FROM N TO N)")
        source.enable_commit_history()
        prepare_copy_target(target)
        with source.begin("write") as tx:
            tx.execute("CREATE(a:N {value:'equal'})-[:R]->(a), (b:N {value:'equal'})-[:R]->(a)")
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=("N", "R"))
        assert copy_graph(package, target, idempotency_key="no_pk", conflict="skip").rows == 4
        assert target.execute("MATCH(n:N) RETURN count(n)").rows == ((2,),)
        assert target.execute("MATCH(a:N)-[:R]->(a) RETURN count(a)").rows == ((1,),)
        assert target.verify("all").findings == ()


def test_late_native_edge_failure_rolls_back_all_data_and_receipt(pair, monkeypatch):
    import okto_grafx.engine.copy_execution as execution
    _, target, package = pair
    original = execution._write_one
    calls = 0

    def fail(engine, operation, row, context):
        nonlocal calls
        if operation.relationships:
            calls += 1
            if calls == 2:
                raise RuntimeError("late copy edge")
        return original(engine, operation, row, context)

    before = target.transactions.published_state().last_committed_lsn
    with monkeypatch.context() as patch:
        patch.setattr(execution, "_write_one", fail)
        with pytest.raises(GrafxError) as failure:
            copy_graph(package, target, idempotency_key="late")
        assert isinstance(failure.value.__cause__, RuntimeError)
        assert str(failure.value.__cause__) == "late copy edge"
    assert target.transactions.published_state().last_committed_lsn == before
    assert target.execute("MATCH(n {v:'same'}) RETURN count(n)").rows == ((1,),)
    assert not copy_graph(package, target, idempotency_key="late").replayed
    assert_graph(target)


def test_missing_target_flexible_schema_does_not_create_it(pair, tmp_path):
    _, _, package = pair
    with connect(tmp_path / "missing") as target:
        prepare_copy_target(target)
        before = target.transactions.published_state().last_committed_lsn
        with pytest.raises(GrafxError):
            copy_graph(package, target, idempotency_key="missing")
        assert target.transactions.published_state().last_committed_lsn == before
        assert not any(t.flexible_properties for t in target.catalog.catalog.tables())


@pytest.mark.parametrize("cut,code,committed", [
    ("before_commit", 71, False), ("before_heap_apply", 73, True), ("after_commit", 72, True),
])
def test_process_death_preserves_native_endpoints_and_receipt(pair, cut, code, committed):
    source, target, package = pair
    source.checkpoint()
    target.checkpoint()
    source.close()
    target.close()
    run = subprocess.run([sys.executable, str(Path(__file__).with_name("catalog_copy_worker.py")),
                          source.path, target.path, cut],
                         env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src")),
                         capture_output=True, text=True, timeout=90)
    assert run.returncode == code, run.stderr
    with connect(target.path) as recovered:
        assert recovered.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((1 if committed else 0,),)
        receipt = copy_graph(package, recovered, idempotency_key="crash")
        assert receipt.replayed is committed
        assert_graph(recovered)
        recovered.checkpoint()
    with connect(target.path) as reopened:
        assert copy_graph(package, reopened, idempotency_key="crash").replayed
        assert_graph(reopened)


@pytest.mark.parametrize("value", [float("nan"), {"nested": [float("inf")]}, None, {"null": None}, "not a bag"])
def test_checksumming_does_not_admit_malformed_or_nonfinite_bags(pair, value):
    _, target, package = pair
    index = next(i for i, t in enumerate(package.tables) if t.schema.unlabeled)
    item = package.tables[index]
    rows = ((item.rows[0][0], encode_values((value,))),) + item.rows[1:]
    tables = tuple(replace(t, rows=rows) if i == index else t for i, t in enumerate(package.tables))
    altered = replace(package, tables=tables,
                      sha256=_digest(package.source_commit, tables, package.spaces, CopyLimits()))
    before = target.transactions.published_state().last_committed_lsn
    with pytest.raises(GrafxError):
        copy_graph(altered, target, idempotency_key="invalid_bag")
    assert target.transactions.published_state().last_committed_lsn == before
    assert target.execute("MATCH(n {v:'same'}) RETURN count(n)").rows == ((1,),)


def test_partial_capture_closes_exact_flexible_endpoints(pair):
    source, target, package = pair
    edge = next(t for t in package.tables if t.logical_type == "R")
    with source.begin("read") as tx:
        subset = capture_copy(tx, tables=(edge.schema.name,),
                              record_ids={edge.schema.name: tuple(rid for rid, _ in edge.rows)},
                              include_endpoints=True)
    assert len(subset.tables) == 2
    receipt = copy_graph(subset, target, idempotency_key="subset")
    assert receipt.rows == 3
    assert target.execute("MATCH(n {v:'same'}) RETURN count(n)").rows == ((3,),)
    assert target.execute("MATCH(a)-[:R]->(b) RETURN a=b").rows == ((False,),)
    assert target.execute("MATCH(n:Keyed) RETURN count(n)").rows == ((0,),)


def test_current_only_copy_into_history_enabled_target_is_a_new_target_commit(pair):
    source, target, package = pair
    source.enable_system_history(tables=tuple(t.schema.name for t in package.tables))
    target_tables = tuple(t.name for t in target.catalog.catalog.tables() if not t.name.startswith("_grafx_"))
    target.enable_system_history(tables=target_tables)
    activation = target.commit_history().entries[-1].identity
    with source.begin("read") as tx:
        with pytest.raises(GrafxError):
            capture_copy(tx, tables=tuple(t.schema.name for t in package.tables))
        current = capture_copy(tx, tables=tuple(t.schema.name for t in package.tables), history="current-only")
    receipt = copy_graph(current, target, idempotency_key="history")
    before = target.system_as_of(activation, tables=target_tables)
    after = target.system_as_of(receipt.target_commit, tables=target_tables)
    assert len(after.rows) - len(before.rows) == 6
    assert after.relationship_types
    assert_graph(target)


def test_concurrent_same_key_copies_do_not_duplicate_equal_nodes(pair):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    source, target, package = pair
    edge = next(t for t in package.tables if t.logical_type == "R")
    with source.begin("read") as reader:
        package = capture_copy(reader, tables=(edge.schema.name, edge.schema.from_table))
    barrier = Barrier(2)

    def apply():
        with connect(target.path) as participant:
            barrier.wait(timeout=20)
            try:
                return copy_graph(package, participant, idempotency_key="concurrent")
            except GrafxError as failure:
                return failure

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(lambda _: apply(), range(2)))
    assert any(not isinstance(outcome, Exception) for outcome in outcomes)
    assert copy_graph(package, target, idempotency_key="concurrent").replayed
    assert target.execute("MATCH(n {v:'same'}) RETURN count(n)").rows == ((3,),)
    assert target.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((1,),)
    assert target.verify("all").findings == ()


def test_native_statement_budget_refuses_after_private_nodes_without_receipt(pair):
    _, target, package = pair
    before = target.transactions.published_state().last_committed_lsn
    with connect(target.path, max_statement_writes=4) as limited:
        with pytest.raises(GrafxError) as failure:
            copy_graph(package, limited, idempotency_key="quota")
        assert failure.value.details["field"] == "max_statement_writes"
    assert target.transactions.published_state().last_committed_lsn == before
    assert target.execute("MATCH(n {v:'same'}) RETURN count(n)").rows == ((1,),)
    assert not copy_graph(package, target, idempotency_key="quota").replayed
    assert_graph(target)


def test_multiple_unlabeled_stores_and_model_tampering_refuse_before_mutation(pair):
    _, target, package = pair
    unlabelled = next(t for t in package.tables if t.schema.unlabeled)
    duplicate = replace(unlabelled, schema=replace(unlabelled.schema, name="AnotherPhysical", table_id=1000))
    tables = package.tables + (duplicate,)
    altered = replace(package, tables=tables, sha256=_digest(package.source_commit, tables, package.spaces, CopyLimits()))
    with pytest.raises(GrafxError) as failure:
        copy_graph(altered, target, idempotency_key="two_unlabeled")
    assert failure.value.details["field"] == "unlabeled_tables"
    tables = tuple(replace(t, schema=replace(t.schema, unlabeled=False)) if t.schema.unlabeled else t
                   for t in package.tables)
    altered = replace(package, tables=tables, sha256=_digest(package.source_commit, tables, package.spaces, CopyLimits()))
    with pytest.raises(GrafxError):
        copy_graph(altered, target, idempotency_key="wrong_model")
    assert target.execute("MATCH(n {v:'same'}) RETURN count(n)").rows == ((1,),)


def test_literal_property_keys_are_data_not_interpolated_query_syntax(pair):
    _, target, package = pair
    keys = {"": "empty", "x`}) DELETE n //": "inert", "_from": 19, "_properties": {"v": "nested"}}
    tables = tuple(replace(item, rows=tuple((rid, encode_values((keys,))) for rid, _ in item.rows))
                   if item.schema.unlabeled else item for item in package.tables)
    changed = replace(package, tables=tables, sha256=_digest(package.source_commit, tables, package.spaces, CopyLimits()))
    assert copy_graph(changed, target, idempotency_key="keys").rows == 6
    rows = target.execute("MATCH(a)-[:R]->(b) RETURN properties(a),properties(b)").rows
    assert rows == ((keys, keys),)
    assert target.verify("all").findings == ()


def test_skipped_existing_endpoint_deleted_before_commit_forces_occ_refusal(pair, monkeypatch):
    import okto_grafx.catalog_copy as copying
    _, target, package = pair
    with target.begin("write") as tx:
        tx.execute("CREATE(:Keyed {id:1,title:'existing'})")
    original = copying._stage

    def competing_delete(tx, captured, conflict):
        count = original(tx, captured, conflict)
        with connect(target.path) as competitor:
            with competitor.begin("write") as other:
                other.execute("MATCH(n:Keyed) DETACH DELETE n")
        return count

    with monkeypatch.context() as patch:
        patch.setattr(copying, "_stage", competing_delete)
        with pytest.raises(GrafxError) as failure:
            copy_graph(package, target, idempotency_key="endpoint_race", conflict="skip")
        assert failure.value.code == "write_conflict"
    assert target.execute("MATCH(n {v:'same'}) RETURN count(n)").rows == ((1,),)
    assert target.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((0,),)
    assert not copy_graph(package, target, idempotency_key="endpoint_race", conflict="skip").replayed
    assert_graph(target)


def test_empty_flexible_tables_and_schema_flags_are_not_confused_with_ordinary_any(pair):
    source, target, package = pair
    tables = tuple(t.schema.name for t in package.tables)
    with source.begin("read") as reader:
        empty = capture_copy(reader, tables=tables, record_ids={name: () for name in tables})
    receipt = copy_graph(empty, target, idempotency_key="empty")
    assert receipt.rows == receipt.skipped_rows == 0
    assert copy_graph(empty, target, idempotency_key="empty").replayed
    assert target.execute("MATCH(n {v:'same'}) RETURN count(n)").rows == ((1,),)


def test_internal_staging_failure_restores_prior_transaction_work_when_caught(pair, monkeypatch):
    import okto_grafx.catalog_copy as copying
    import okto_grafx.engine.copy_execution as execution
    _, target, package = pair
    original = execution._write_one

    def late_failure(engine, operation, row, context):
        if operation.relationships:
            raise RuntimeError("stage boundary")
        return original(engine, operation, row, context)

    with target.begin("write") as tx:
        tx.execute("CREATE(:Pad {id:42})")
        with monkeypatch.context() as patch:
            patch.setattr(execution, "_write_one", late_failure)
            with pytest.raises(GrafxError):
                copying._stage(tx, package)
        assert tx.execute("MATCH(n {v:'same'}) RETURN count(n)").rows == ((1,),)
        assert tx.execute("MATCH(n:Pad) RETURN n.id").rows == ((42,),)
    assert target.execute("MATCH(n:Pad) RETURN n.id").rows == ((42,),)
    assert not copy_graph(package, target, idempotency_key="after_catch").replayed
    assert_graph(target)


def test_documented_disposable_flexible_copy_example():
    guide = (Path(__file__).resolve().parents[2] / "docs" / "CATALOG_COPY.md").read_text(encoding="utf-8")
    section = guide.split("### Flexible and no-PK entity identity", 1)[1].split("## Atomicity", 1)[0]
    examples = re.findall(r"```python\n(.*?)```", section, re.DOTALL)
    assert len(examples) == 1
    exec(compile(examples[0], "docs/CATALOG_COPY.md", "exec"), {})
