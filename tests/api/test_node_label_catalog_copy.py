"""Versioned label copy must preserve logical sets, identity and receipt atomicity."""

from dataclasses import replace

import pytest

from okto_grafx import connect, TemporalLimits
from okto_grafx.catalog_copy import CopyLimits, _digest, capture_copy, copy_graph, prepare_copy_target
from okto_grafx.domain.model.node_labels import decode_node_labels, encode_node_labels
from okto_grafx.errors import GrafxError


def prepare(source, target):
    for db in (source, target):
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, value STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N, value STRING)")
    source.enable_commit_history()
    prepare_copy_target(target)
    with source.begin("write") as tx:
        tx.execute("CREATE(a:N {id:1,value:'empty'})-[:R {value:'one'}]->(b:N:A {id:2,value:'many'}) "
                   "CREATE(b)-[:R {value:'two'}]->(c:N:`λ`:`a b` {id:3,value:'unicode'}) REMOVE a:N, c:N")
    with source.begin("read") as tx:
        return capture_copy(tx, tables=("N", "R"))


def assert_graph(db, *, skipped=False):
    labels = (("Local",) if skipped else ("A", "N"))
    assert db.execute("MATCH(a)-[r:R]->(b) RETURN a.id,labels(a),b.id,labels(b),r.value ORDER BY a.id").rows == (
        (1, (), 2, labels, "one"), (2, labels, 3, ("a b", "λ"), "two"),
    )
    assert db.verify("all").findings == ()


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("subset", [False, True])
@pytest.mark.parametrize("history", [False, True])
def test_copy_labels_scan_or_index_capture_history_snapshots_and_reopen(tmp_path, codec, subset, history):
    with connect(tmp_path / "source", page_size=512, codec=codec) as source, connect(tmp_path / "target", page_size=512) as target:
        package = prepare(source, target)
        if subset:
            ids = {item.schema.name: tuple(rid for rid, _ in item.rows) for item in package.tables}
            with source.begin("read") as tx:
                assert capture_copy(tx, tables=("N", "R"), record_ids=ids) == package
        if history:
            target.enable_system_history(("N", "R"))
            target.enable_system_history_index()
        with connect(target.path, page_size=512) as observer, observer.begin("read") as reader:
            receipt = copy_graph(package, target, idempotency_key="labels")
            assert receipt.rows == 5 and receipt.skipped_rows == 0
            assert reader.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((0,),)
        assert_graph(target)
        assert copy_graph(package, target, idempotency_key="labels") == replace(receipt, replayed=True)
        if history:
            for access in ("scan", "index"):
                graph = target.system_as_of(receipt.target_commit, tables=("N", "R"), limits=TemporalLimits(access_path=access))
                rows = sorted((row for row in graph.rows if row.table == "N"), key=lambda row: row.values[0])
                assert [row.node_labels for row in rows] == [(), ("A", "N"), ("a b", "λ")]
        target.checkpoint()
    with connect(tmp_path / "target", page_size=512) as db:
        assert_graph(db)
        assert copy_graph(package, db, idempotency_key="labels").replayed


def test_skip_preserves_labels_and_uses_physical_pk_not_shared_logical_name(tmp_path):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        package = prepare(source, target)
        with target.begin("write") as tx:
            tx.execute("CREATE(n:N:Local {id:2,value:'keep'}) REMOVE n:N")
            tx.execute("CREATE NODE TABLE Other(id INT64, value STRING, PRIMARY KEY(id))")
            tx.execute("CREATE(:Other:N {id:1,value:'not-the-copy-target'})")
        receipt = copy_graph(package, target, idempotency_key="skip", conflict="skip")
        assert (receipt.rows, receipt.skipped_rows) == (4, 1)
        assert_graph(target, skipped=True)
        assert target.execute("MATCH(n:Local) RETURN n.value").rows == (("keep",),)
        assert target.execute("MATCH(n:Other) RETURN n.value").rows == (("not-the-copy-target",),)
        assert "A" not in target.catalog.catalog.table("N").extra_node_labels
        assert copy_graph(package, target, idempotency_key="skip", conflict="skip").replayed


@pytest.mark.parametrize("damage", ["truncated", "unadmitted", "duplicate", "relationship", "trailing", "digest"])
def test_malformed_or_unbound_membership_refuses_without_target_effects(tmp_path, damage):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        package = prepare(source, target)
        index = 1 if damage == "relationship" else 0
        item = package.tables[index]
        rid, raw = item.rows[0]
        if damage == "relationship":
            raw = encode_node_labels(()) + raw
        else:
            _labels, offset = decode_node_labels(raw)
            body = raw[offset:]
            if damage == "truncated":
                raw = b"GXL1\x01\x00\xff"
            elif damage == "unadmitted":
                raw = encode_node_labels(("Foreign",)) + body
            elif damage == "duplicate":
                raw = b"GXL1\x02\x00\x01\x00A\x01\x00A" + body
            elif damage == "trailing":
                raw += b"\0"
            else:
                raw = encode_node_labels(("A",)) + body
        tables = tuple(replace(t, rows=((rid, raw),) + t.rows[1:]) if n == index else t
                       for n, t in enumerate(package.tables))
        sha = package.sha256 if damage == "digest" else _digest(package.source_commit, tables, package.spaces, CopyLimits())
        altered = replace(package, tables=tables, sha256=sha)
        before = target.transactions.published_state().last_committed_lsn
        catalog = target._catalog.catalog.serialize()
        with pytest.raises(GrafxError):
            copy_graph(altered, target, idempotency_key="bad")
        assert target.transactions.published_state().last_committed_lsn == before
        assert target._catalog.catalog.serialize() == catalog
        assert target.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((0,),)
        assert not copy_graph(package, target, idempotency_key="bad").replayed
        assert_graph(target)


def test_label_bytes_count_towards_copy_row_quota_and_candidates_bind_digest(tmp_path):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        package = prepare(source, target)
        maximum = max(len(raw) for item in package.tables for _, raw in item.rows)
        limits = CopyLimits(max_row_bytes=maximum-1)
        with source.begin("read") as tx, pytest.raises(GrafxError):
            capture_copy(tx, tables=("N", "R"), limits=limits)
        with pytest.raises(GrafxError):
            copy_graph(package, target, idempotency_key="bounded", limits=limits)
        item = package.tables[0]
        altered = replace(item, schema=replace(item.schema, extra_node_labels=("A", "Extra", "a b", "λ")))
        tables = (altered,) + package.tables[1:]
        assert _digest(package.source_commit, tables, package.spaces, CopyLimits()) != package.sha256


def test_late_copy_failure_rolls_back_label_schema_data_and_receipt(tmp_path, monkeypatch):
    import okto_grafx.engine.copy_execution as execution

    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        package = prepare(source, target)
        original = execution._write_one
        catalog = target._catalog.catalog.serialize()
        sequence = target.transactions.published_state().last_committed_lsn

        def fail(engine, operation, row, context):
            if getattr(operation, "relationships", ()):
                raise RuntimeError("label copy edge refusal")
            return original(engine, operation, row, context)

        with monkeypatch.context() as scoped:
            scoped.setattr(execution, "_write_one", fail)
            with pytest.raises(GrafxError):
                copy_graph(package, target, idempotency_key="late")
        assert target._catalog.catalog.serialize() == catalog
        assert target.transactions.published_state().last_committed_lsn == sequence
        assert not copy_graph(package, target, idempotency_key="late").replayed
        assert_graph(target)


def test_flexible_owner_remap_keeps_labeled_and_empty_nodes_distinct(tmp_path):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        source.ensure_identity_indexes()
        source.enable_commit_history()
        target.ensure_identity_indexes()
        with target.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Pad(id INT64)")
        for db in (source, target):
            with db.begin("write") as tx:
                tx.execute("CREATE(a {v:[{k:1}]})-[:R]->(b {v:[{k:1}]}) SET a:A:`λ`")
        with target.begin("write") as tx:
            tx.execute("MATCH(n) DETACH DELETE n")
        source.enable_commit_history()
        prepare_copy_target(target)
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=tuple(t.name for t in source.catalog.catalog.tables()))
        result = copy_graph(package, target, idempotency_key="flex-labels")
        assert result.rows == 3
        a, b = target.execute("MATCH(a)-[:R]->(b) RETURN a,b").rows[0]
        assert a.labels == ("A", "λ") and b.labels == () and a.identity != b.identity
        assert a.properties == b.properties == {"v": ({"k":1},)}
        assert target.verify("all").findings == ()


def test_implicit_source_package_skip_does_not_require_target_base_label(tmp_path):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        for db in (source, target):
            db.ensure_identity_indexes()
            db.enable_commit_history()
            with db.begin("write") as tx:
                tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
                tx.execute("CREATE(:N {id:1})")
        with target.begin("write") as tx:
            tx.execute("MATCH(n) REMOVE n:N SET n:Local")
        source.enable_commit_history()
        prepare_copy_target(target)
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=("N",))
        assert package.tables[0].schema.extra_node_labels == ()
        receipt = copy_graph(package, target, idempotency_key="old-shape", conflict="skip")
        assert receipt.rows == 0 and receipt.skipped_rows == 1
        assert target.execute("MATCH(n:Local) RETURN n.id,labels(n)").rows == ((1,("Local",)),)
        assert target.verify("all").findings == ()


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("cut,code,committed", [
    ("before_commit",71,False), ("before_heap_apply",73,True), ("after_commit",72,True),
])
def test_label_copy_process_cuts_keep_catalog_rows_and_receipt_together(tmp_path, codec, cut, code, committed):
    import os
    from pathlib import Path
    import subprocess
    import sys

    source_path, target_path = tmp_path / "source", tmp_path / "target"
    with connect(source_path) as source, connect(target_path) as target:
        package = prepare(source, target)
        source.checkpoint()
        target.checkpoint()
    process = subprocess.run([sys.executable, str(Path(__file__).with_name("catalog_copy_worker.py")),
                              str(source_path), str(target_path), cut, codec],
                             env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src")),
                             capture_output=True, text=True, timeout=90)
    assert process.returncode == code, process.stdout + process.stderr
    with connect(target_path, codec="pure") as recovered:
        assert recovered.execute("MATCH()-[r:R]->() RETURN count(r)").rows == ((2 if committed else 0,),)
        assert bool(recovered.catalog.catalog.table("N").extra_node_labels) is committed
        receipt = copy_graph(package, recovered, idempotency_key="crash")
        assert receipt.replayed is committed
        assert_graph(recovered)
        recovered.checkpoint()
    with connect(target_path, codec="pure") as reopened:
        assert copy_graph(package, reopened, idempotency_key="crash").replayed
        assert_graph(reopened)
