"""Bounded copy binds logical type authority without confusing physical member IDs."""

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import hashlib
import json
import struct

import pytest

from okto_grafx import connect
from okto_grafx.catalog_copy import (
    CopyLimits, _digest, capture_copy, copy_graph, prepare_copy_target,
)
from okto_grafx.errors import GrafxError


def schema(db, *, pad=False):
    db.ensure_identity_indexes()
    with db.begin("write") as tx:
        if pad:
            tx.execute("CREATE NODE TABLE Padding(id INT64, PRIMARY KEY(id))")
        for name in ("A", "B", "C"):
            tx.execute(f"CREATE NODE TABLE {name}(id INT64, PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE GROUP R(FROM A TO B, FROM B TO C, value STRING)")


@pytest.fixture
def pair(tmp_path):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        schema(source)
        schema(target, pad=True)
        source.enable_commit_history()
        prepare_copy_target(target)
        with source.begin("write") as tx:
            tx.execute("CREATE(a:A {id:1})-[:R {value:'first'}]->(b:B {id:1})-[:R {value:'second'}]->(c:C {id:1})")
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=tuple(t.name for t in source.catalog.catalog.tables()))
        yield source, target, package


def assert_graph(db):
    result = db.execute("MATCH(a)-[r:R]->(b) RETURN labels(a),labels(b),r.value,type(r) ORDER BY r.value")
    assert result.rows == ((("A",), ("B",), "first", "R"), (("B",), ("C",), "second", "R"))
    assert db.verify("all").findings == ()


def test_grouped_copy_target_ids_replay_snapshot_and_reopen(pair):
    source, target, package = pair
    assert source.catalog.catalog.table("A").table_id != target.catalog.catalog.table("A").table_id
    before = target.begin("read")
    try:
        receipt = copy_graph(package, target, idempotency_key="group")
        assert receipt.rows == 5
        assert before.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((0,),)
    finally:
        before.rollback()
    assert_graph(target)
    sequence = target.transactions.published_state().last_committed_lsn
    assert copy_graph(package, target, idempotency_key="group") == replace(receipt, replayed=True)
    assert target.transactions.published_state().last_committed_lsn == sequence
    path = target.path
    target.checkpoint()
    target.close()
    with connect(path) as reopened:
        assert copy_graph(package, reopened, idempotency_key="group").replayed
        assert_graph(reopened)


def test_selected_group_member_and_automatic_endpoint_closure(pair):
    source, target, package = pair
    edge = next(t for t in package.tables if t.schema.kind == "rel")
    with source.begin("read") as tx:
        subset = capture_copy(tx, tables=(edge.schema.name,),
                              record_ids={edge.schema.name: tuple(rid for rid, _ in edge.rows)},
                              include_endpoints=True)
    assert len(subset.tables) == 3
    assert next(t for t in subset.tables if t.schema.kind == "rel").logical_type == "R"
    assert copy_graph(subset, target, idempotency_key="subset").rows == 3
    assert target.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((1,),)


def test_grouped_skip_resolves_existing_node_keys_without_cross_table_collision(pair):
    _, target, package = pair
    with target.begin("write") as tx:
        tx.execute("CREATE(:B {id:1})")
    receipt = copy_graph(package, target, idempotency_key="skip", conflict="skip")
    assert (receipt.rows, receipt.skipped_rows) == (4, 1)
    assert_graph(target)


def test_group_metadata_is_hash_bound_and_counted_against_package_bytes(pair):
    _, target, package = pair
    stripped = replace(package, tables=tuple(replace(t, logical_type=None) for t in package.tables))
    with pytest.raises(GrafxError) as failure:
        copy_graph(stripped, target, idempotency_key="stripped")
    assert failure.value.details["field"] == "package_sha256"
    # An independent ordinary-package digest oracle: no extra fields/domain change
    # for previously supported ungrouped typed tables.
    nodes = tuple(t for t in package.tables if t.schema.kind == "node")
    expected = hashlib.sha256(b"grafx-copy-package-v1")

    def feed(raw):
        expected.update(struct.pack("<Q", len(raw)))
        expected.update(raw)

    def encoded(value):
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")

    feed(package.source_commit.to_token().encode("ascii"))
    feed(encoded([]))
    for item in nodes:
        t = item.schema
        shape = (t.name, t.kind, t.primary_key, t.from_table, t.to_table,
                 tuple((c.name, c.type.name, c.nullable, c.vector_space) for c in t.columns))
        feed(encoded((shape, t.table_id, t.schema_version)))
        for rid, raw in item.rows:
            feed(struct.pack("<Q", rid) + raw)
    assert _digest(package.source_commit, nodes, (), CopyLimits()) == expected.hexdigest()
    # Every optional name is fed as a length-delimited, budgeted component.
    bare_tables = stripped.tables
    def fits(tables, ceiling):
        try:
            _digest(package.source_commit, tables, package.spaces,
                    CopyLimits(max_bytes=ceiling, max_row_bytes=ceiling))
            return True
        except GrafxError:
            return False
    ceiling = next(n for n in range(1, 10000) if fits(bare_tables, n))
    assert not fits(package.tables, ceiling)


@pytest.mark.parametrize("defect", ["pair", "columns", "missing_target_type"])
def test_inconsistent_selected_group_definition_is_rejected_before_transaction(pair, defect):
    _, target, package = pair
    items = list(package.tables)
    positions = [i for i, t in enumerate(items) if t.schema.kind == "rel"]
    first, second = (items[i] for i in positions)
    if defect == "pair":
        replacement = replace(second.schema, from_table=first.schema.from_table, to_table=first.schema.to_table)
        items[positions[1]] = replace(second, schema=replacement)
    elif defect == "columns":
        replacement = replace(second.schema, columns=second.schema.columns[:2] + (replace(second.schema.columns[2], nullable=False),))
        items[positions[1]] = replace(second, schema=replacement)
    else:
        # A node named A no longer conflicts with relationship type A. This is
        # a valid package, but the destination has no matching logical type A.
        items = [replace(t, logical_type="A") if t.logical_type else t for t in items]
    changed = replace(package, tables=tuple(items))
    changed = replace(changed, sha256=_digest(changed.source_commit, changed.tables, changed.spaces, CopyLimits()))
    before = target.transactions.published_state().last_committed_lsn
    with pytest.raises(GrafxError) as failure:
        copy_graph(changed, target, idempotency_key="bad_group")
    assert failure.value.details["field"] == {"pair": "logical_type_endpoints", "columns": "logical_type_schema",
                                               "missing_target_type": "target_logical_type"}[defect]
    assert target.transactions.published_state().last_committed_lsn == before


def test_grouped_history_capture_requires_current_only(pair):
    source, target, package = pair
    source.enable_system_history(tables=tuple(t.schema.name for t in package.tables))
    with source.begin("read") as tx:
        with pytest.raises(GrafxError):
            capture_copy(tx, tables=tuple(t.schema.name for t in package.tables))
        current = capture_copy(tx, tables=tuple(t.schema.name for t in package.tables), history="current-only")
    assert copy_graph(current, target, idempotency_key="current").rows == 5
    assert target._catalog.catalog.system_history_tables() == ()
    assert_graph(target)


@pytest.mark.parametrize("rewrite", ["missing", "wrong", "on_node", "invalid", "duplicate_id"])
def test_group_authority_tampering_refuses_without_effects(pair, rewrite):
    _, target, package = pair
    items = list(package.tables)
    index = next(i for i, item in enumerate(items) if item.schema.kind == "rel")
    if rewrite == "duplicate_id":
        items[index] = replace(items[index], schema=replace(items[index].schema, table_id=items[0].schema.table_id))
    elif rewrite == "on_node":
        items[0] = replace(items[0], logical_type="R")
    else:
        items[index] = replace(items[index], logical_type={"missing": None, "wrong": "Wrong", "invalid": "bad name"}[rewrite])
    altered = replace(package, tables=tuple(items))
    altered = replace(altered, sha256=_digest(altered.source_commit, altered.tables, altered.spaces, CopyLimits()))
    before = target.transactions.published_state().last_committed_lsn
    with pytest.raises(GrafxError):
        copy_graph(altered, target, idempotency_key="tampered")
    assert target.transactions.published_state().last_committed_lsn == before
    assert target.execute("MATCH(n:A) RETURN count(n)").rows == ((0,),)
    assert target.verify("all").findings == ()


def test_late_group_write_failure_rolls_back_nodes_edges_and_receipt(pair, monkeypatch):
    from okto_grafx.engine.database import Transaction
    _, target, package = pair
    original = Transaction.executemany
    calls = 0

    def fail(tx, statement, parameters, *args, **kwargs):
        nonlocal calls
        if "CREATE (a)-[:R" in statement:
            calls += 1
            if calls == 2:
                raise RuntimeError("injected late edge failure")
        return original(tx, statement, parameters, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Transaction, "executemany", fail)
        with pytest.raises(RuntimeError, match="late edge"):
            copy_graph(package, target, idempotency_key="retry")
    assert target.execute("MATCH(n:A) RETURN count(n)").rows == ((0,),)
    assert target.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((0,),)
    assert not copy_graph(package, target, idempotency_key="retry").replayed
    assert_graph(target)


@pytest.mark.parametrize("cut,code,committed", [
    ("before_commit", 71, False), ("before_heap_apply", 73, True), ("after_commit", 72, True),
])
def test_grouped_copy_process_death_and_idempotent_recovery(pair, cut, code, committed):
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
        assert recovered.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((2 if committed else 0,),)
        receipt = copy_graph(package, recovered, idempotency_key="crash")
        assert receipt.replayed is committed
        assert_graph(recovered)
        recovered.checkpoint()
    with connect(target.path) as reopened:
        assert copy_graph(package, reopened, idempotency_key="crash").replayed
        assert_graph(reopened)
