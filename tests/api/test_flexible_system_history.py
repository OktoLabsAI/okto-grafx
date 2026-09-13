"""Flexible metadata and logical edge identity survive native system-time history."""

import os
from pathlib import Path
import subprocess
import sys
from dataclasses import replace

import pytest

from okto_grafx import connect, CommitId, TemporalLimits
from okto_grafx.errors import GrafxError, GrafxHistoryExpired, GrafxHistoryUnavailable


def prepare(db):
    with db.begin("write") as tx:
        tx.execute("CREATE(a {v:'before'})-[:R {v:1}]->(b {v:2})")
    db.enable_commit_history()
    names = tuple(t.name for t in db.catalog.catalog.tables())
    db.enable_system_history(names)
    return names, db.commit_history().entries[-1].identity


@pytest.mark.parametrize("indexed", [False, True])
def test_flexible_history_values_models_groups_intervals_and_retention(tmp_path, indexed):
    with connect(tmp_path / "db", page_size=512) as db:
        names, activated = prepare(db)
        if indexed:
            db.enable_system_history_index()
        limits = TemporalLimits(access_path="index" if indexed else "scan")
        original = db.system_as_of(activated, tables=names, limits=limits)
        assert all(s.flexible_properties for s in original.schemas)
        assert original.schemas[0].unlabeled and not original.schemas[1].unlabeled
        assert [(g.name, g.table_ids) for g in original.relationship_types] == [("R",(2,))]
        edge = next(r for r in original.rows if r.logical_type == "R")
        assert edge.values[-1] == {"v":1}
        with db.begin("write") as tx:
            tx.execute("MATCH(n {v:'before'}) SET n.v={nested:[1,null,'s']},n.extra='kept'")
            tx.execute("MATCH()-[r:R]->() SET r.v='changed',r.extra=[true]")
        changed = CommitId(db.identity.database_uuid, tx.report.csn)
        newer = db.system_as_of(changed, tables=names, limits=limits)
        assert next(r for r in newer.rows if r.logical_type == "R").values[-1] == {"v":"changed","extra":(True,)}
        versions = db.system_versions(edge.table, edge.record_id, limits=limits).versions
        assert len(versions) == 2 and all(v.logical_type == "R" for v in versions)
        assert versions[0].system_to == changed
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("MATCH()-[r:R]->() SET r.v=0.0/0.0")
        with db.begin("write") as tx:
            tx.execute("MATCH()-[r:R]->() DELETE r")
            tx.execute("MATCH(n) DELETE n")
            tx.execute("CREATE(a {v:'new'})-[:R {v:9}]->(b {v:10})")
        recreated = CommitId(db.identity.database_uuid, tx.report.csn)
        latest = db.system_as_of(recreated, tables=names, limits=limits)
        assert edge.record_id not in {r.record_id for r in latest.rows if r.table == edge.table}
        assert db.system_as_of(activated, tables=names, limits=limits).rows == original.rows
        diff = db.system_diff(activated, changed, tables=names, limits=limits)
        assert len(diff.rows) == 2
        assert all(p.name == "_properties" for row in diff.rows for p in row.properties)
        db.prune_system_history(changed, tables=names)
        with pytest.raises(GrafxHistoryExpired):
            db.system_as_of(activated, tables=names, limits=limits)
        db.compact_system_history(confirm_quiescent=True)
        assert db.system_as_of(recreated, tables=names, limits=limits).rows == latest.rows
        assert db.verify("all").findings == ()
        db.checkpoint()
    with connect(tmp_path / "db", page_size=512, read_only=True) as db:
        restored = db.system_as_of(changed, tables=names, limits=limits)
        assert restored.schemas == newer.schemas
        assert restored.rows == newer.rows and restored.relationship_types == newer.relationship_types


@pytest.mark.parametrize("cut", ["before_commit", "before_apply", "after_current", "after_history_root", "after_history_chunk", "after_commit"])
def test_flexible_history_crash_recovery_keeps_current_and_history_together(tmp_path, cut):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        names, activated = prepare(db)
        db.checkpoint()
    env = {**os.environ, "PYTHONPATH":str(Path("src").resolve())}
    process = subprocess.run([sys.executable, str(Path(__file__).with_name("system_history_worker.py")),
                              str(root), "flex_update", cut], capture_output=True, env=env, timeout=60)
    assert process.returncode in (71,72,73,74), process.stderr.decode()
    for _ in range(2):
        with connect(root, page_size=512) as db:
            expected = 1 if cut == "before_commit" else "changed"*200
            assert db.execute("MATCH()-[r:R]->() RETURN r.v").rows == ((expected,),)
            at = db.commit_history().entries[-1].identity
            graph = db.system_as_of(at, tables=names)
            assert all(s.flexible_properties for s in graph.schemas)
            assert next(r for r in graph.rows if r.logical_type == "R").values[-1] == {"v":expected}
            assert db.system_as_of(activated, tables=names).relationship_types[0].name == "R"
            assert db.verify("all").findings == ()
            db.checkpoint()


def test_group_growth_does_not_invent_earlier_membership_or_automatic_history(tmp_path):
    with connect(tmp_path / "db") as db:
        names, activated = prepare(db)
        before = db.system_as_of(activated, tables=names)
        reader = db.begin("read")
        try:
            with db.begin("write") as tx:
                tx.execute("CREATE NODE TABLE Other(id INT64)")
                tx.execute("CREATE(a:Other {id:1}) WITH a MATCH(b {v:2}) CREATE(a)-[:R {v:'later'}]->(b)")
            all_names = tuple(t.name for t in db.catalog.catalog.tables())
            later = db.commit_history().entries[-1].identity
            with pytest.raises(GrafxHistoryUnavailable):
                db.system_as_of(later, tables=all_names)
            db.enable_system_history(all_names)
            enabled = db.commit_history().entries[-1].identity
            db.enable_system_history_index()
            for mode in ("scan","index"):
                old = db.system_as_of(activated, tables=names, limits=TemporalLimits(access_path=mode))
                assert old.relationship_types == before.relationship_types
                assert len(old.relationship_types[0].table_ids) == 1
                new = db.system_as_of(enabled, tables=all_names, limits=TemporalLimits(access_path=mode))
                assert len(new.relationship_types[0].table_ids) == 2
                assert {r.logical_type for r in new.rows if r.table_id in new.relationship_types[0].table_ids} == {"R"}
            assert reader.system_as_of(activated, tables=names).rows == before.rows
        finally:
            reader.rollback()
        assert db.verify("all").findings == ()


def test_backup_restores_history_while_logical_transfer_is_explicit_current_only(tmp_path):
    from okto_grafx.backup import create_backup, restore_backup
    from okto_grafx.transfer import export_graph, import_graph
    with connect(tmp_path / "source", page_size=512) as db:
        names, activated = prepare(db)
        db.enable_system_history_index()
        picture = db.system_as_of(activated, tables=names)
        db.pin_system_history("retain", activated, tables=names)
        with db.begin("write") as tx:
            tx.execute("MATCH()-[r:R]->() SET r.v={changed:true}")
        create_backup(db, tmp_path / "backup")
        with pytest.raises(GrafxError):
            export_graph(db, tmp_path / "refused")
        export_graph(db, tmp_path / "artifact", history="current-only")
    restore_backup(tmp_path / "backup", tmp_path / "restored", confirm_original_offline=True)
    with connect(tmp_path / "restored", page_size=512) as db:
        restored = db.system_as_of(activated, tables=names)
        assert restored.schemas == picture.schemas and restored.rows == picture.rows
        assert restored.relationship_types == picture.relationship_types
        assert db.system_history_pins()[0].name == "retain"
        assert db.verify("all").findings == ()
    import_graph(tmp_path / "artifact", tmp_path / "imported")
    with connect(tmp_path / "imported") as db:
        assert db._catalog.catalog.system_history_tables() == ()
        assert db.execute("MATCH()-[r:R]->() RETURN r.v.changed").rows == ((True,),)
        assert db.verify("all").findings == ()


def test_typed_group_history_preserves_logical_names_without_flexible_flags(tmp_path):
    with connect(tmp_path / "db") as db:
        db.maintenance.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE GROUP R(FROM A TO B, FROM B TO A, v INT64)")
            tx.execute("CREATE(a:A {id:1})-[:R {v:1}]->(b:B {id:1})-[:R {v:2}]->(a)")
        db.enable_commit_history()
        names = tuple(t.name for t in db.catalog.catalog.tables())
        db.enable_system_history(names)
        at = db.commit_history().entries[-1].identity
        assert db._catalog.catalog.requires_capability("system_history_models_v1")
        db.enable_system_history_index()
        pictures = [db.system_as_of(at,tables=names,limits=TemporalLimits(access_path=mode)) for mode in ("scan","index")]
        assert pictures[0].rows == pictures[1].rows and pictures[0].relationship_types == pictures[1].relationship_types
        assert all(not t.flexible_properties for t in pictures[0].schemas)
        assert pictures[0].relationship_types[0].table_ids == (3,4)
        assert {r.values[-1] for r in pictures[0].rows if r.logical_type == "R"} == {1,2}
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("operation", ["index", "prune", "compact"])
def test_history_maintenance_refuses_model_drift_before_mutation(tmp_path, monkeypatch, operation):
    from okto_grafx.engine.system_history_store import SystemHistoryStore
    with connect(tmp_path / "db") as db:
        names, _ = prepare(db)
        with db.begin("write") as tx:
            tx.execute("MATCH()-[r:R]->() SET r.v='later'")
        before = db.commit_history().entries[-1].identity
        original = SystemHistoryStore._iter_batches
        def lose_model(store, *args, **kwargs):
            for identity, changes, size in original(store, *args, **kwargs):
                yield identity, tuple(replace(change, logical_type=None,
                    table=replace(change.table,flexible_properties=False,unlabeled=False)) for change in changes), size
        with monkeypatch.context() as scoped:
            scoped.setattr(SystemHistoryStore,"_iter_batches",lose_model)
            with pytest.raises(GrafxError):
                if operation == "index":
                    db.enable_system_history_index()
                elif operation == "prune":
                    db.prune_system_history(before,tables=names)
                else:
                    db.compact_system_history(confirm_quiescent=True)
        assert db.commit_history().entries[-1].identity == before
        assert db.verify("all").findings == ()
