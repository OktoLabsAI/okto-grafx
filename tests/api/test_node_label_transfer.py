"""Native label transport, exact resume witnesses and no partial promotion."""

import hashlib
import json
import struct

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxRecoveryRefused
from okto_grafx.transfer import TransferLimits, export_graph, import_graph


def prepare(db, model="typed"):
    db.ensure_identity_indexes()
    owner = "R" if model == "namespace" else "N"
    with db.begin("write") as tx:
        if model != "flexible":
            tx.execute(f"CREATE NODE TABLE {owner}(id INT64, value STRING, PRIMARY KEY(id))")
            tx.execute(f"CREATE REL TABLE R(FROM {owner} TO {owner}, value STRING)")
        label = "" if model == "flexible" else ":" + owner
        tx.execute(f"CREATE(a{label} {{id:1,value:'empty'}})-[:R {{value:'one'}}]->"
                   f"(b{label} {{id:2,value:'many'}})-[:R {{value:'two'}}]->"
                   f"(c{label} {{id:3,value:'unicode'}}), (d{label} {{id:4,value:'implicit'}}) "
                   f"SET a:A, b:A:Unused, c:`λ`:`a b` REMOVE a:A:{owner}, c:{owner}, b:Unused")
        if model == "flexible":
            tx.execute("MATCH(n {id:2}) SET n.nested=[{k:1},{k:'text'}]")
    if model != "flexible":
        db.create_index("by_value", owner, ("value",))
        db.create_text_index("text", owner, ("value",), kind="node", bucket_count=16)


def image(db):
    graph = db.execute("MATCH(a)-[r:R]->(b) RETURN a.id,labels(a),properties(a),b.id,labels(b),properties(b),r.value ORDER BY a.id").rows
    native = {}
    with db.begin("read") as tx:
        for table in db.catalog.catalog.tables():
            if table.kind == "node":
                native[table.name] = (table.extra_node_labels, tuple((row.values, row.node_labels)
                    for row in tx.scan_rows_v1(table.name, kind="node", limit=100).rows))
    return graph, native


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("model", ["typed", "flexible", "namespace"])
@pytest.mark.parametrize("resume", [False, True])
def test_format4_exact_labels_endpoints_candidates_indexes_and_reopen(tmp_path, codec, model, resume):
    with connect(tmp_path / "source", codec=codec) as db:
        prepare(db, model)
        expected = image(db)
        exported = export_graph(db, tmp_path / "artifact", limits=TransferLimits(batch_rows=1))
    manifest = json.loads((tmp_path / "artifact" / "manifest.json").read_bytes())
    assert manifest["format"] == "okto-grafx-logical-4"
    for table, obj in zip(manifest["schema"]["tables"], manifest["objects"], strict=True):
        assert ("extra_node_labels" in table) == obj["node_labels"] == (table["kind"] == "node")
    assert all("table_kind" in index for index in manifest["schema"]["indexes"])
    kwargs = {"resume_directory": tmp_path / "work"} if resume else {}
    result = import_graph(tmp_path / "artifact", tmp_path / "target", limits=TransferLimits(batch_rows=1), **kwargs)
    assert result.rows == 6 and result.source_database_uuid == exported.source_database_uuid
    assert result.target_database_uuid != exported.source_database_uuid
    assert len(result.record_id_mapping) == 6
    with connect(tmp_path / "target", codec="pure") as db:
        assert image(db) == expected
        assert "node_labels_v1" in db._catalog.catalog.required_capabilities()
        if model != "flexible":
            assert len(db.search_text(index="text", query="unicode").hits) == 1
        assert db.verify("all").findings == ()
        db.checkpoint()
    if resume:
        assert import_graph(tmp_path / "artifact", tmp_path / "target", **kwargs) == result
    with connect(tmp_path / "target", read_only=True) as db:
        assert image(db) == expected and db.verify("all").findings == ()


def artifact(tmp_path):
    with connect(tmp_path / "source") as db:
        prepare(db)
        export_graph(db, tmp_path / "artifact")
    return tmp_path / "artifact", json.loads((tmp_path / "artifact" / "manifest.json").read_bytes())


def assert_no_import_effect(root, tmp_path, resume):
    before = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob("*") if p.is_file()}
    kwargs = {"resume_directory": tmp_path / "work"} if resume else {}
    with pytest.raises(GrafxError):
        import_graph(root, tmp_path / "target", **kwargs)
    assert not (tmp_path / "target").exists() and not (tmp_path / "work").exists()
    assert not list(tmp_path.glob(".target.incomplete-*"))
    assert before == {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("damage", ["downgrade", "missing_candidates", "candidate_order", "candidate_type",
                                    "relationship_candidates", "missing_flag", "integer_flag", "relationship_flag"])
def test_manifest_label_model_refuses_before_any_destination_or_workspace(tmp_path, damage, resume):
    root, manifest = artifact(tmp_path)
    table = manifest["schema"]["tables"][0]
    if damage == "downgrade":
        manifest["format"] = "okto-grafx-logical-3"
    elif damage == "missing_candidates":
        table.pop("extra_node_labels")
    elif damage == "candidate_order":
        table["extra_node_labels"] = ["Z", "A"]
    elif damage == "candidate_type":
        table["extra_node_labels"] = "A"
    elif damage == "relationship_candidates":
        manifest["schema"]["tables"][1]["extra_node_labels"] = []
    elif damage == "missing_flag":
        manifest["objects"][0].pop("node_labels")
    elif damage == "integer_flag":
        manifest["objects"][0]["node_labels"] = 1
    else:
        manifest["objects"][1]["node_labels"] = True
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert_no_import_effect(root, tmp_path, resume)


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("damage", ["flag", "truncated", "duplicate", "unadmitted", "trailing"])
def test_rechecksummed_label_frames_do_not_bypass_preflight(tmp_path, damage, resume):
    from okto_grafx.domain.model.node_labels import encode_node_labels, decode_node_labels

    root, manifest = artifact(tmp_path)
    obj = manifest["objects"][0]
    path = root / obj["file"]
    raw = path.read_bytes()
    rid, size = struct.unpack_from("<QI", raw)
    payload = raw[12:12+size]
    assert payload[0] == 1
    _labels, offset = decode_node_labels(payload, 1)
    values = payload[offset:]
    if damage == "flag":
        payload = b"\2" + payload[1:]
    elif damage == "truncated":
        payload = b"\1GXL1\x01\x00\xff"
    elif damage == "duplicate":
        payload = b"\1GXL1\x02\x00\x01\x00A\x01\x00A" + values
    elif damage == "unadmitted":
        payload = b"\1" + encode_node_labels(("Foreign",)) + values
    else:
        payload += b"\0"
    raw = struct.pack("<QI", rid, len(payload)) + payload + raw[12+size:]
    path.write_bytes(raw)
    obj["sha256"] = hashlib.sha256(raw).hexdigest()
    obj["bytes"] = len(raw)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert_no_import_effect(root, tmp_path, resume)


def test_import_readback_rejects_membership_disagreement_with_matching_properties(tmp_path, monkeypatch):
    import okto_grafx.transfer as transfer

    root, _ = artifact(tmp_path)
    original = transfer._stage_rows

    def wrong_labels(db, table, rows):
        return original(db, table, [(rid, values, None) for rid, values, _ in rows])

    monkeypatch.setattr(transfer, "_stage_rows", wrong_labels)
    with pytest.raises(GrafxRecoveryRefused, match="Logical graph transfer refused") as error:
        import_graph(root, tmp_path / "target")
    assert error.value.details["reason"] == "import_readback_mismatch"
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("labels,prefix", [
    (None, b"\0"), ((), b"\1GXL1\0\0"), (("A",), b"\1GXL1\1\0\1\0A"),
])
def test_format4_presence_bytes_are_independently_specified(labels, prefix):
    from okto_grafx.domain.model.schema import TableDef, ColumnDef
    from okto_grafx.domain.model.value import ValueType, encode_values
    from okto_grafx.transfer import _row_payload

    table = TableDef(1, "N", "node", (ColumnDef("id", ValueType.INT64),), extra_node_labels=("A",))
    assert _row_payload(table, (42,), node_labels=labels, labels_format=True) == prefix + encode_values((42,))
    if labels is not None:
        with pytest.raises(GrafxRecoveryRefused):
            _row_payload(table, (42,), node_labels=labels)


def test_resume_rejects_changed_labels_even_when_properties_schema_and_row_counts_match(tmp_path, monkeypatch):
    import okto_grafx.transfer as transfer

    root, _ = artifact(tmp_path)
    original = transfer._stage_rows
    calls = 0

    def interrupt(db, table, rows):
        nonlocal calls
        result = original(db, table, rows)
        calls += 1
        if calls == 2:
            raise RuntimeError("after two committed node batches")
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(transfer, "_stage_rows", interrupt)
        with pytest.raises(RuntimeError, match="after two committed"):
            import_graph(root, tmp_path / "target", resume_directory=tmp_path / "work", limits=TransferLimits(batch_rows=1))
    with connect(tmp_path / "work" / "database") as db:
        before = db.execute("MATCH(n {id:2}) RETURN properties(n)").rows
        with db.begin("write") as tx:
            tx.execute("MATCH(n {id:2}) REMOVE n:A")
        assert db.execute("MATCH(n {id:2}) RETURN properties(n)").rows == before
        sequence = db.transactions.published_state().last_committed_lsn
    with pytest.raises(GrafxRecoveryRefused) as error:
        import_graph(root, tmp_path / "target", resume_directory=tmp_path / "work")
    assert error.value.details["reason"] == "resume_row_mismatch"
    assert not (tmp_path / "target").exists()
    with connect(tmp_path / "work" / "database") as db:
        assert db.transactions.published_state().last_committed_lsn == sequence
        assert db.execute("MATCH(n {id:2}) RETURN labels(n)").rows == ((("N",),),)


@pytest.mark.parametrize("grow_candidates", [False, True])
def test_export_owns_old_membership_or_refuses_concurrent_schema_growth(tmp_path, monkeypatch, grow_candidates):
    from okto_grafx import Transaction

    with connect(tmp_path / "source") as db, connect(tmp_path / "source") as writer:
        prepare(db)
        expected = image(db)
        scan = Transaction.scan_rows_v1
        injected = False

        def interleave(self, *args, **kwargs):
            nonlocal injected
            page = scan(self, *args, **kwargs)
            if self._database is db and not injected:
                injected = True
                with writer.begin("write") as tx:
                    tx.execute("MATCH(n {id:2}) SET n:BrandNew" if grow_candidates else "MATCH(n {id:2}) REMOVE n:A")
            return page

        with monkeypatch.context() as scoped:
            scoped.setattr(Transaction, "scan_rows_v1", interleave)
            if grow_candidates:
                with pytest.raises(GrafxRecoveryRefused) as error:
                    export_graph(db, tmp_path / "artifact", limits=TransferLimits(batch_rows=1))
                assert error.value.details["reason"] == "schema_changed"
                assert not (tmp_path / "artifact").exists()
            else:
                export_graph(db, tmp_path / "artifact", limits=TransferLimits(batch_rows=1))
        assert injected
    if not grow_candidates:
        import_graph(tmp_path / "artifact", tmp_path / "target")
        with connect(tmp_path / "target") as db:
            assert image(db) == expected


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("cut", ["after_batch", "before_wal_barrier", "after_wal_barrier", "before_promotion", "after_promotion"])
def test_native_label_transfer_process_crash_resume_and_lost_ack(tmp_path, codec, cut):
    import os
    from pathlib import Path
    import subprocess
    import sys

    with connect(tmp_path / "source", codec=codec) as db:
        prepare(db, "namespace")
        expected = image(db)
        export_graph(db, tmp_path / "artifact")
    process = subprocess.run([sys.executable, str(Path(__file__).with_name("node_label_transfer_worker.py")),
                              str(tmp_path), cut, codec],
                             env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src")),
                             capture_output=True, text=True, timeout=90)
    assert process.returncode == 73, process.stdout + process.stderr
    assert (tmp_path / "target").exists() == (cut == "after_promotion")
    report = import_graph(tmp_path / "artifact", tmp_path / "target", resume_directory=tmp_path / "work",
                          limits=TransferLimits(batch_rows=2))
    assert report.rows == len(report.record_id_mapping) == 6
    with connect(tmp_path / "target", codec="pure") as db:
        assert image(db) == expected
        assert db.verify("all").findings == ()
        db.checkpoint()
    assert import_graph(tmp_path / "artifact", tmp_path / "target", resume_directory=tmp_path / "work") == report
    with connect(tmp_path / "target", codec="pure") as db:
        assert image(db) == expected and db.verify("all").findings == ()


def test_schema_label_admission_failure_rolls_back_before_resumable_rows(tmp_path, monkeypatch):
    from okto_grafx.engine.query_engine import QueryEngine

    root, _ = artifact(tmp_path)
    original = QueryEngine._admit_node_labels

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("after private label catalog admission")

    with monkeypatch.context() as scoped:
        scoped.setattr(QueryEngine, "_admit_node_labels", fail)
        with pytest.raises(RuntimeError, match="after private label"):
            import_graph(root, tmp_path / "target", resume_directory=tmp_path / "work")
    assert not (tmp_path / "target").exists()
    with connect(tmp_path / "work" / "database") as db:
        assert db.catalog.catalog.tables() == ()
        assert "node_labels_v1" not in db._catalog.catalog.required_capabilities()
        assert db.verify("all").findings == ()
    report = import_graph(root, tmp_path / "target", resume_directory=tmp_path / "work")
    assert report.rows == 6


@pytest.mark.parametrize("bound", ["row", "artifact"])
def test_encoded_membership_is_charged_before_export_or_import_promotion(tmp_path, bound):
    root, manifest = artifact(tmp_path)
    lengths = []
    for obj in manifest["objects"]:
        raw = (root / obj["file"]).read_bytes()
        offset = 0
        while offset < len(raw):
            _rid, size = struct.unpack_from("<QI", raw, offset)
            lengths.append(size)
            offset += 12 + size
    total = sum(obj["bytes"] for obj in manifest["objects"]) + (root / "manifest.json").stat().st_size
    limits = TransferLimits(max_row_bytes=max(lengths)-1) if bound == "row" else TransferLimits(
        max_bytes=total-1, max_row_bytes=max(lengths))
    for kwargs in ({}, {"resume_directory": tmp_path / "work"}):
        with pytest.raises(GrafxRecoveryRefused):
            import_graph(root, tmp_path / "target", limits=limits, **kwargs)
        assert not (tmp_path / "target").exists() and not (tmp_path / "work").exists()
    with connect(tmp_path / "source") as db:
        sequence = db.transactions.published_state().last_committed_lsn
        with pytest.raises(GrafxRecoveryRefused):
            export_graph(db, tmp_path / "bounded", limits=limits)
        assert db.transactions.published_state().last_committed_lsn == sequence
        assert not (tmp_path / "bounded").exists()
