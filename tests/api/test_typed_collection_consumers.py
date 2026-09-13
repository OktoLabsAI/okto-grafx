"""Typed descriptors survive snapshots and never authorize lossy copy/import repair."""

import json

import pytest

from okto_grafx import connect, CommitId, TemporalLimits
from okto_grafx.backup import create_backup, restore_backup
from okto_grafx.catalog_copy import capture_copy, copy_graph, prepare_copy_target
from okto_grafx.errors import GrafxError, GrafxConfigurationError
from okto_grafx.transfer import export_graph, import_graph, TransferLimits
from tests.api.test_typed_collection_storage import seed, READ, EXPECTED


@pytest.mark.parametrize("declaration", ["LIST<STRING>", "LIST<INT64 NOT NULL>", "ARRAY<INT64,2>"])
@pytest.mark.parametrize("empty", [False, True])
def test_copy_refuses_same_native_family_with_different_descriptor(tmp_path, declaration, empty):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        source.ensure_identity_indexes()
        source.enable_commit_history()
        for db, type_name in ((source, "LIST<INT64>"), (target, declaration)):
            db.ensure_identity_indexes()
            with db.begin() as tx:
                tx.execute(f"CREATE NODE TABLE N(id INT64,v {type_name},PRIMARY KEY(id))")
        if not empty:
            with source.begin() as tx:
                tx.execute("CREATE(:N {id:1,v:[1,2]})")
        prepare_copy_target(target)
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=("N",))
        before = target.transactions.published_state().last_committed_lsn
        with pytest.raises(GrafxConfigurationError) as caught:
            copy_graph(package, target, idempotency_key="mismatch")
        assert caught.value.details["field"] == "target_schema"
        assert target.transactions.published_state().last_committed_lsn == before
        assert target.execute("MATCH(n:N) RETURN count(n)").rows == ((0,),)


@pytest.mark.parametrize("resumable", [False, True])
@pytest.mark.parametrize("changed", ["decimal_scale", "array_length", "struct_fields", "unknown_descriptor_field"])
def test_transfer_refuses_mismatched_descriptor_before_destination(tmp_path, monkeypatch, resumable, changed):
    import okto_grafx.transfer as transfer
    import okto_grafx.transfer_resume as resume
    with connect(tmp_path / "source") as db:
        seed(db)
        export_graph(db, tmp_path / "artifact")
    manifest_path = tmp_path / "artifact" / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    columns = next(t for t in manifest["schema"]["tables"] if t["name"] == "N")["columns"]
    by_name = {c["name"]: c for c in columns}
    if changed == "decimal_scale":
        by_name["amounts"]["stored_type"]["element"]["scale"] = 5
    elif changed == "array_length":
        by_name["pair"]["stored_type"]["length"] = 3
    elif changed == "struct_fields":
        by_name["info"]["stored_type"]["fields"].append({"name": "extra", "type": {"kind": "STRING", "nullable": True}})
    else:
        by_name["xs"]["stored_type"]["ignored"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def no_destination(*args, **kwargs):
        pytest.fail("Invalid collection schema/rows must refuse before destination or workspace opens")

    monkeypatch.setattr(transfer, "connect", no_destination)
    monkeypatch.setattr(resume, "connect", no_destination)
    with pytest.raises(GrafxError):
        import_graph(tmp_path / "artifact", tmp_path / "target",
                     **({"resume_directory": tmp_path / "work"} if resumable else {}))
    assert not (tmp_path / "target").exists() and not (tmp_path / "work").exists()


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("indexed", [False, True])
def test_history_delete_recreate_and_physical_restore_keep_full_types(tmp_path, codec, indexed):
    limits = TemporalLimits(access_path="index" if indexed else "scan")
    names = ("N", "R")
    with connect(tmp_path / "source", codec=codec) as db:
        seed(db)
        db.enable_commit_history()
        db.enable_system_history(names)
        if indexed:
            db.enable_system_history_index()
        baseline = db.commit_history().entries[-1].identity
        original = db.system_as_of(baseline, tables=names, limits=limits)
        with db.begin() as tx:
            tx.execute("MATCH(n:N {id:1}) SET n.xs=[8,9]")
        updated = CommitId(db.identity.database_uuid, tx.report.csn)
        changed = db.system_as_of(updated, tables=names, limits=limits)
        assert len(db.system_diff(baseline, updated, tables=names, limits=limits).rows) == 1
        with db.begin() as tx:
            tx.execute("MATCH()-[r:R]->() DELETE r")
        deleted = CommitId(db.identity.database_uuid, tx.report.csn)
        deleted_graph = db.system_as_of(deleted, tables=names, limits=limits)
        assert len(deleted_graph.rows) == 2
        with db.begin() as tx:
            tx.execute("MATCH(a:N {id:1}),(b:N {id:2}) CREATE(a)-[:R {v:[decimal('3.5',2,1)]}]->(b)")
        recreated = CommitId(db.identity.database_uuid, tx.report.csn)
        latest = db.system_as_of(recreated, tables=names, limits=limits)
        rel_id = next(t.table_id for t in original.schemas if t.name == "R")
        assert next(r.record_id for r in latest.rows if r.table_id == rel_id) != next(r.record_id for r in original.rows if r.table_id == rel_id)
        create_backup(db, tmp_path / "backup")
    restore_backup(tmp_path / "backup", tmp_path / "restored", confirm_original_offline=True)
    for directory in ("source", "restored"):
        with connect(tmp_path / directory, codec=codec) as db:
            for identity, expected in ((baseline, original), (updated, changed), (deleted, deleted_graph), (recreated, latest)):
                actual = db.system_as_of(identity, tables=names, limits=limits)
                assert actual.rows == expected.rows and actual.schemas == expected.schemas
            assert not db.verify("all").findings


def test_resumable_transfer_preserves_collection_schema_and_values(tmp_path):
    with connect(tmp_path / "source") as source:
        seed(source)
        declarations = source.catalog.catalog.tables()
        export_graph(source, tmp_path / "artifact")
    report = import_graph(tmp_path / "artifact", tmp_path / "target", resume_directory=tmp_path / "work",
                          limits=TransferLimits(batch_rows=1))
    assert report.rows == 3
    with connect(tmp_path / "target") as target:
        assert target.execute(READ).rows[0] == EXPECTED
        assert target.catalog.catalog.tables() == declarations
        assert not target.verify("all").findings
