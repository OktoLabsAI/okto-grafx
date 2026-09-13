"""Logical transfer never merges same-spelled node/relationship identities."""

import json
import subprocess
import sys

import pytest

from okto_grafx import connect, TextIndexOptions
from okto_grafx.errors import GrafxError
from okto_grafx.transfer import TransferLimits, export_graph, import_graph


def seed(path, *, grouped=False, codec="pure"):
    db = connect(path, codec=codec)
    db.maintenance.ensure_identity_indexes()
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE R(id INT64, body STRING, PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE " + ("GROUP " if grouped else "") + "R(FROM R TO R, body STRING)")
        tx.execute("UNWIND [1,2,3] AS i CREATE(a:R {id:i,body:'node alpha'})-[:R {body:'edge beta'}]->(a)")
    db.create_index("by_body", "R", ("body",), bucket_count=16)
    db.create_text_index("node_text", "R", ("body",), kind="node", bucket_count=16)
    relation = db.catalog.catalog.relationship_tables("R")[0]
    db.create_text_index("edge_text", relation.name, ("body",), kind="rel", bucket_count=16)
    return db


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_transfer_preserves_kind_indexes_and_fresh_entity_identity(tmp_path, grouped, codec):
    with seed(tmp_path / "source", grouped=grouped, codec=codec) as db:
        source_uuid = db.identity.database_uuid
        expected = db.execute("MATCH(n:R)-[r:R]->(m:R) RETURN n.id,n.body,r.body,m.id ORDER BY n.id").rows
        report = export_graph(db, tmp_path / "artifact", limits=TransferLimits(batch_rows=1))
    manifest = json.loads((tmp_path / "artifact" / "manifest.json").read_bytes())
    assert manifest["format"] == "okto-grafx-logical-3"
    assert {i["name"]:i["table_kind"] for i in manifest["schema"]["indexes"]} == {
        "by_body":"node", "node_text":"node", "edge_text":"rel"}
    imported = import_graph(tmp_path / "artifact", tmp_path / "target", limits=TransferLimits(batch_rows=1))
    assert imported.rows == report.rows == 6
    assert len({(m.kind,m.table,m.source_record_id) for m in imported.record_id_mapping}) == 6
    assert {m.kind for m in imported.record_id_mapping} == {"node", "rel"}
    for _ in range(2):
        with connect(tmp_path / "target", codec=codec) as db:
            assert db.identity.database_uuid != source_uuid
            assert db.execute("MATCH(n:R)-[r:R]->(m:R) RETURN n.id,n.body,r.body,m.id ORDER BY n.id").rows == expected
            assert len(db.search_text(index="node_text", query="alpha").hits) == 3
            assert len(db.search_text(index="edge_text", query="beta").hits) == 3
            assert not db.search_text(index="node_text", query="beta").hits
            assert not db.search_text(index="edge_text", query="alpha").hits
            for n,r,m in db.execute("MATCH(n:R)-[r:R]->(m:R) RETURN n,r,m").rows:
                assert r.source == n.identity and r.target == m.identity
            assert db.verify("all").findings == ()
            db.checkpoint()


def test_fulltext_kind_refusal_and_replacement_preserve_sibling(tmp_path):
    with seed(tmp_path / "source") as db:
        with pytest.raises(GrafxError) as error:
            db.create_text_index("ambiguous", "R", ("body",))
        assert error.value.details["reason"] == "ambiguous_table_name"
        for kind in (False, [], "relationship"):
            with pytest.raises(GrafxError):
                db.create_text_index("bad", "R", ("body",), kind=kind)
        db.replace_text_index("edge_text", options=TextIndexOptions(field_weights=(2.0,)))
        assert len(db.search_text(index="node_text", query="alpha").hits) == 3
        assert len(db.search_text(index="edge_text", query="beta").hits) == 3
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("change", ["old_format", "missing_kind", "invalid_kind", "exact_on_rel", "duplicate_node"])
def test_malformed_namespace_manifest_refuses_before_destination(tmp_path, change):
    with seed(tmp_path / "source") as db:
        export_graph(db, tmp_path / "artifact")
    path = tmp_path / "artifact" / "manifest.json"
    data = json.loads(path.read_bytes())
    index = next(i for i in data["schema"]["indexes"] if i["name"] == "by_body")
    if change == "old_format":
        data["format"] = "okto-grafx-logical-2"
    elif change == "missing_kind":
        del index["table_kind"]
    elif change == "invalid_kind":
        index["table_kind"] = "relationship"
    elif change == "exact_on_rel":
        index["table_kind"] = "rel"
    else:
        data["schema"]["tables"][1]["kind"] = "node"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(GrafxError):
        import_graph(tmp_path / "artifact", tmp_path / "target")
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("cut", ["after_node", "after_edge", "after_promotion"])
def test_real_process_cut_resumes_qualified_prefix_and_lost_ack(tmp_path, cut):
    with seed(tmp_path / "source") as db:
        export_graph(db, tmp_path / "artifact")
    worker = r'''
import os,sys
from pathlib import Path
import okto_grafx.transfer as transfer
import okto_grafx.transfer_resume as resume
root, cut = Path(sys.argv[1]), sys.argv[2]
if cut == 'after_promotion':
    original = resume._promote
    def promote(*args):
        original(*args)
        os._exit(73)
    resume._promote = promote
else:
    original = transfer._stage_rows
    def stage(db, table, rows):
        original(db,table,rows)
        if table.kind == ('node' if cut == 'after_node' else 'rel'):
            os._exit(73)
    transfer._stage_rows = stage
transfer.import_graph(root/'artifact',root/'target',resume_directory=root/'work',limits=transfer.TransferLimits(batch_rows=1))
raise AssertionError('Cut not reached')
'''
    proc = subprocess.run([sys.executable,"-c",worker,str(tmp_path),cut], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 73, proc.stderr
    assert (tmp_path / "target").exists() == (cut == "after_promotion")
    report = import_graph(tmp_path / "artifact",tmp_path / "target",resume_directory=tmp_path / "work",limits=TransferLimits(batch_rows=2))
    assert report.rows == 6 and len(report.record_id_mapping) == 6
    assert {m.kind for m in report.record_id_mapping} == {"node", "rel"}
    assert import_graph(tmp_path / "artifact",tmp_path / "target",resume_directory=tmp_path / "work") == report
    with connect(tmp_path / "target") as db:
        assert db.execute("MATCH(n:R)-[r:R]->() RETURN n.id,r.body ORDER BY n.id").rows == ((1,"edge beta"),(2,"edge beta"),(3,"edge beta"))
        assert len(db.search_text(index="node_text",query="alpha").hits) == 3
        assert len(db.search_text(index="edge_text",query="beta").hits) == 3
        assert db.verify("all").findings == ()
