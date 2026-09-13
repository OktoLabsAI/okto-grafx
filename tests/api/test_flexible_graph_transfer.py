"""Logical model transfer preserves groups, dynamic values and fresh identities."""

import hashlib
import json
import struct

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxRecoveryRefused
from okto_grafx.transfer import TransferLimits, export_graph, import_graph


def seed(path, *, flexible):
    db = connect(path)
    db.maintenance.ensure_identity_indexes()
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
        tx.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        if not flexible:
            tx.execute("CREATE REL TABLE GROUP R(FROM A TO B, FROM B TO A, v ANY)")
        tx.execute("CREATE(a:A {id:1})-[:R {v:1}]->(b:B {id:1})")
        # A node table created after a relationship deliberately changes table IDs on import.
        tx.execute("CREATE NODE TABLE Empty(id INT64)")
        tx.execute("MATCH(a:A),(b:B) CREATE(b)-[:R {v:'reverse'}]->(a)")
        tx.execute("MATCH(a:A),(b:B) CREATE(a)-[:R {v:[1,{nested:true}]}]->(b)")
        if flexible:
            tx.execute("CREATE(a {v:'start'})-[:R {v:{deep:[1,null,'s']}}]->(b {v:2})")
    return db


@pytest.mark.parametrize("flexible", [False, True])
def test_roundtrip_groups_endpoint_remapping_and_independent_reimports(tmp_path, flexible):
    with seed(tmp_path / "source", flexible=flexible) as db:
        expected = db.execute("MATCH(a)-[r:R]->(b) RETURN labels(a),properties(a),type(r),r.v,labels(b),properties(b)").rows
        source_identity = db.identity.database_uuid
        exported = export_graph(db, tmp_path / "artifact", limits=TransferLimits(batch_rows=1))
        original_members = db.catalog.catalog.relationship_tables("R")
    manifest = json.loads((tmp_path / "artifact" / "manifest.json").read_bytes())
    assert manifest["format"] == "okto-grafx-logical-2"
    assert manifest["schema"]["relationship_types"] == [
        {"name":"R", "members":[t.name for t in original_members]}]
    for target in ("first", "second"):
        report = import_graph(tmp_path / "artifact", tmp_path / target, limits=TransferLimits(batch_rows=1))
        assert report.rows == exported.rows
        assert len(report.record_id_mapping) == report.rows
        with connect(tmp_path / target) as db:
            assert db.identity.database_uuid != source_identity
            assert db.execute("MATCH(a)-[r:R]->(b) RETURN labels(a),properties(a),type(r),r.v,labels(b),properties(b)").rows == expected
            assert len(db.catalog.catalog.relationship_tables("R")) == len(original_members)
            assert all(t.flexible_properties == flexible for t in db.catalog.catalog.relationship_tables("R"))
            entities = db.execute("MATCH(a)-[r:R]->(b) RETURN a,r,b").rows
            assert all(r.source == a.identity and r.target == b.identity for a,r,b in entities)
            assert db.verify("all").findings == ()
            if flexible:
                with db.begin("write") as tx:
                    tx.execute("CREATE(e:Empty {id:9}) WITH e MATCH(a:A) CREATE(e)-[:R {v:'new pair'}]->(a)")
                assert len(db.catalog.catalog.relationship_tables("R")) == len(original_members)+1
                db.checkpoint()
    with connect(tmp_path / "first", read_only=True) as db:
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("change", ["missing", "duplicate", "node", "duplicate_group", "extra", "wrong_model", "old_format"])
def test_malformed_group_artifact_refuses_before_destination(tmp_path, change):
    with seed(tmp_path / "source", flexible=True) as db:
        export_graph(db, tmp_path / "artifact")
    path = tmp_path / "artifact" / "manifest.json"
    manifest = json.loads(path.read_bytes())
    group = manifest["schema"]["relationship_types"][0]
    if change == "missing":
        group["members"][0] = "absent"
    elif change == "duplicate":
        group["members"].append(group["members"][0])
    elif change == "node":
        group["members"][0] = "A"
    elif change == "duplicate_group":
        manifest["schema"]["relationship_types"].append(dict(group))
    elif change == "extra":
        group["unexpected"] = True
    elif change == "wrong_model":
        member = next(t for t in manifest["schema"]["tables"] if t["name"] == group["members"][0])
        member["flexible_properties"] = False
    else:
        manifest["format"] = "okto-grafx-logical-1"
    path.write_text(json.dumps(manifest))
    with pytest.raises(GrafxError):
        import_graph(path.parent, tmp_path / "target")
    assert not (tmp_path / "target").exists()
    assert not list(tmp_path.glob(".target.incomplete-*"))


def test_reader_without_model_format_refuses_before_import(tmp_path, monkeypatch):
    import okto_grafx.transfer as transfer
    with seed(tmp_path / "source", flexible=True) as db:
        export_graph(db, tmp_path / "artifact")
    monkeypatch.setattr(transfer, "_MODEL_FORMAT", "unsupported")
    with pytest.raises(GrafxRecoveryRefused, match="refused"):
        import_graph(tmp_path / "artifact", tmp_path / "target")
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("value", [None, "not a property map", {"bad":None}, {"bad":[float("nan")]}])
def test_bad_stored_property_maps_refuse_before_private_database_creation(tmp_path, value, monkeypatch):
    import okto_grafx.transfer as transfer
    from okto_grafx.domain.model.value import encode_values
    with seed(tmp_path / "source", flexible=True) as db:
        export_graph(db, tmp_path / "artifact")
    path = tmp_path / "artifact" / "manifest.json"
    manifest = json.loads(path.read_bytes())
    table = next(t for t in manifest["schema"]["tables"] if t.get("unlabeled"))
    item = next(item for item in manifest["objects"] if item["table_id"] == table["table_id"])
    payload = encode_values((value,))
    content = struct.pack("<QI", 1, len(payload)) + payload
    (path.parent / item["file"]).write_bytes(content)
    item.update(rows=1, bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    path.write_text(json.dumps(manifest))
    def no_connect(*args, **kwargs):
        pytest.fail("Invalid stored value reached destination database creation")
    monkeypatch.setattr(transfer, "connect", no_connect)
    with pytest.raises(GrafxError):
        import_graph(path.parent, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_group_schema_attachment_late_failure_rolls_back_whole_schema(tmp_path, monkeypatch):
    from okto_grafx.transfer import _install_schema, _tables
    from okto_grafx.engine.query_engine import QueryEngine, _AttachRelationshipType
    with seed(tmp_path / "source", flexible=True) as source:
        export_graph(source, tmp_path / "artifact")
    schema = json.loads((tmp_path / "artifact" / "manifest.json").read_bytes())["schema"]
    tables, spaces = _tables(schema)
    original = QueryEngine._schema_change
    def fail_after_attachment(self, node, *args, **kwargs):
        result = original(self, node, *args, **kwargs)
        if isinstance(node, _AttachRelationshipType):
            raise RuntimeError("after group attachment")
        return result
    with connect(tmp_path / "private") as db:
        with monkeypatch.context() as scoped:
            scoped.setattr(QueryEngine, "_schema_change", fail_after_attachment)
            with pytest.raises(RuntimeError, match="after group attachment"):
                _install_schema(db, tables, spaces, tuple(schema["relationship_types"]))
        assert db.catalog.catalog.tables() == ()
        assert db.catalog.catalog.relationship_types() == ()
        assert db.verify("all").findings == ()
    with connect(tmp_path / "private") as db:
        assert db.catalog.catalog.tables() == ()
        _install_schema(db, tables, spaces, tuple(schema["relationship_types"]))
        assert len(db.catalog.catalog.relationship_tables("R")) == 3
        assert db.verify("all").findings == ()


def test_resume_rejects_durable_group_authority_drift(tmp_path):
    from okto_grafx.adapters.storage_local import LocalStorageDevice
    from okto_grafx.transfer import _manifest, _install_schema
    from okto_grafx.transfer_resume import validated_inventory
    with seed(tmp_path / "source", flexible=True) as db:
        export_graph(db, tmp_path / "artifact")
    limits = TransferLimits()
    with LocalStorageDevice(tmp_path / "artifact", create_root=False) as storage:
        manifest, _, tables, spaces = _manifest(storage, limits)
        groups = tuple({**group, "name":"Other"} for group in manifest["schema"]["relationship_types"])
        with connect(tmp_path / "private") as db:
            _install_schema(db, tables, spaces, groups)
        with connect(tmp_path / "private") as db:
            with pytest.raises(GrafxRecoveryRefused) as error:
                validated_inventory(db, storage, manifest, tables, spaces, limits)
            assert error.value.details["reason"] == "resume_relationship_types_mismatch"
            assert db.catalog.catalog.relationship_types()[0].name == "Other"
            assert db.verify("all").findings == ()
