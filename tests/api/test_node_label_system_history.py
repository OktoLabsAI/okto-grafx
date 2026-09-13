"""Native label history: stable identity, temporal readers, retention and WAL recovery."""

import pytest

from okto_grafx import CommitId, TemporalLimits, connect
from okto_grafx.errors import GrafxError, GrafxHistoryExpired, GrafxQueryBudgetExceeded


def write(db, query):
    with db.begin("write") as tx:
        tx.execute(query)
    return CommitId(db.identity.database_uuid, tx.report.csn)


def prepare(db, *, labeled=False):
    db.ensure_identity_indexes()
    db.enable_commit_history()
    write(db, "CREATE NODE TABLE N(id INT64, value STRING, PRIMARY KEY(id))")
    write(db, "CREATE (:N" + (":A" if labeled else "") + " {id:1,value:'first'})")
    db.enable_system_history(("N",))
    return db.commit_history().entries[-1].identity


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("labeled", [False, True])
def test_label_history_keeps_version_membership_identity_and_private_snapshot(tmp_path, codec, indexed, labeled):
    path = tmp_path / "db"
    with connect(path, page_size=512, codec=codec) as db:
        activation = prepare(db, labeled=labeled)
        if indexed:
            db.enable_system_history_index()
        initial = db.system_as_of(activation, tables=("N",)).rows[0]
        assert initial.node_labels == (("A", "N") if labeled else None)
        with db.begin("read") as reader:
            changed = write(db, "MATCH (n:N) REMOVE n:N:A SET n:Latest")
            assert reader.system_as_of(activation, tables=("N",)).rows == (initial,)
            assert reader.system_versions("N", initial.record_id).versions[0].system_to is None
        diff = db.system_diff(activation, changed, tables=("N",))
        assert len(diff.rows) == 1 and diff.rows[0].operation == "updated"
        assert diff.rows[0].properties == ()
        assert diff.rows[0].labels_added == ("Latest",)
        assert diff.rows[0].labels_removed == (("A", "N") if labeled else ("N",))
        latest = write(db, "MATCH (n:Latest) SET n.value='second'")
        for access in (("scan", "index") if indexed else ("scan",)):
            limits = TemporalLimits(access_path=access)
            assert db.system_as_of(activation, tables=("N",), limits=limits).rows == (initial,)
            current = db.system_as_of(latest, tables=("N",), limits=limits).rows[0]
            assert current.node_labels == ("Latest",) and current.record_id == initial.record_id
            assert current.values == (1, "second")
            versions = db.system_versions("N", initial.record_id, limits=limits).versions
            assert [row.node_labels for row in versions] == [initial.node_labels, ("Latest",), ("Latest",)]
        write(db, "MATCH (n:Latest) SET n:Latest")
        assert len(db.system_versions("N", initial.record_id).versions) == 3
        assert db.verify("all").findings == ()
    with connect(path, page_size=512, codec="pure") as db:
        assert db.system_as_of(activation, tables=("N",)).rows[0].node_labels == initial.node_labels
        assert db.system_as_of(latest, tables=("N",)).rows[0].node_labels == ("Latest",)
        assert db.verify("all").findings == ()


def test_label_history_retention_index_and_compaction_keep_latest_membership(tmp_path):
    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        activation = prepare(db, labeled=True)
        db.enable_system_history_index()
        retained = write(db, "MATCH (n) REMOVE n:N:A")
        report = db.prune_system_history(retained, tables=("N",))
        assert report.redacted_versions == 1 and report.redacted_bytes > 0
        with pytest.raises(GrafxHistoryExpired):
            db.system_as_of(activation, tables=("N",))
        for access in ("scan", "index"):
            assert db.system_as_of(retained, tables=("N",), limits=TemporalLimits(access_path=access)).rows[0].node_labels == ()
        db.compact_system_history(confirm_quiescent=True)
        assert db.verify("all").findings == ()
    with connect(path, page_size=512) as db:
        assert db.system_as_of(retained, tables=("N",)).rows[0].node_labels == ()
        assert db.verify("all").findings == ()


def test_failed_label_statement_leaves_no_history_or_candidate_schema(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        activation = prepare(db)
        initial = db.system_as_of(activation, tables=("N",)).rows[0]
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("MATCH(n:N) SET n:Discarded, n.id='bad'")
        at = db.commit_history().entries[-1].identity
        assert db.system_as_of(at, tables=("N",)).rows == (initial,)
        assert len(db.system_versions("N", initial.record_id).versions) == 1
        assert "Discarded" not in db.catalog.catalog.table("N").node_label_candidates


def test_verification_rejects_label_disagreement_even_when_values_match(tmp_path, monkeypatch):
    from dataclasses import replace
    from okto_grafx import Transaction

    with connect(tmp_path / "db") as db:
        prepare(db, labeled=True)
        original = Transaction.system_as_of

        def wrong_labels(self, *args, **kwargs):
            graph = original(self, *args, **kwargs)
            return replace(graph, rows=tuple(replace(row, node_labels=()) for row in graph.rows))

        monkeypatch.setattr(Transaction, "system_as_of", wrong_labels)
        report = db.verify("all")
        assert any(finding.location.file == "system-history.dat" for finding in report.findings)


def test_pre_wal_label_history_failure_preserves_current_and_historical_values(tmp_path, monkeypatch):
    from okto_grafx.engine.wal_manager import WalManager
    from okto_grafx.errors import GrafxDeviceFull

    with connect(tmp_path / "db", page_size=512, buffer_budget_bytes=4096) as db:
        activation = prepare(db, labeled=True)
        expected = db.system_as_of(activation, tables=("N",)).rows
        before = db._catalog.catalog.serialize()
        append = WalManager.append_many
        txn = db.begin("write")
        txn.execute("MATCH(n:N) SET n:Uncommitted, n.value='lost'")

        def fail(*args, **kwargs):
            raise GrafxDeviceFull("Injected before native history COMMIT")

        monkeypatch.setattr(WalManager, "append_many", fail)
        with pytest.raises(GrafxDeviceFull):
            txn.commit()
        monkeypatch.setattr(WalManager, "append_many", append)
        if txn.active:
            txn.rollback()
        assert db._catalog.catalog.serialize() == before
        assert db.system_as_of(activation, tables=("N",)).rows == expected
        assert db.execute("MATCH(n) RETURN labels(n),n.value").rows == ((("A", "N"), "first"),)
        write(db, "MATCH(n) SET n.value='survived'")
        assert db.verify("all").findings == ()


def test_physical_backup_restore_preserves_label_history_and_identity(tmp_path):
    from okto_grafx.backup import create_backup, restore_backup

    with connect(tmp_path / "db", page_size=512) as db:
        activation = prepare(db, labeled=True)
        db.enable_system_history_index()
        changed = write(db, "MATCH(n) REMOVE n:N SET n:Reviewed")
        expected = db.system_as_of(changed, tables=("N",)).rows
        create_backup(db, tmp_path / "backup")
    restore_backup(tmp_path / "backup", tmp_path / "restored", confirm_original_offline=True)
    with connect(tmp_path / "restored", page_size=512) as db:
        assert db.system_as_of(activation, tables=("N",)).rows[0].node_labels == ("A", "N")
        assert db.system_as_of(changed, tables=("N",)).rows == expected
        assert db.verify("all").findings == ()


def test_label_only_diff_obeys_change_budget(tmp_path):
    with connect(tmp_path / "db") as db:
        activation = prepare(db)
        changed = write(db, "MATCH(n) SET n:A:B:C")
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.system_diff(activation, changed, tables=("N",), max_changes=2)


@pytest.mark.parametrize("indexed", [False, True])
def test_independent_reader_and_pre_activation_writer_keep_original_label_history(tmp_path, indexed):
    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        db.ensure_identity_indexes()
        db.enable_commit_history()
        write(db, "CREATE NODE TABLE N(id INT64, value STRING, PRIMARY KEY(id))")
        write(db, "CREATE (:N:A {id:1,value:'before'})")
        with connect(path, page_size=512) as other:
            with other.begin("write") as old_writer:
                old_writer.execute("MATCH(n:N) SET n.value='old-writer'")
                db.enable_system_history(("N",))
                activation = db.commit_history().entries[-1].identity
            if indexed:
                db.enable_system_history_index()
            limits = TemporalLimits(access_path="index" if indexed else "scan")
            with other.begin("read") as reader:
                initial = reader.system_as_of(activation, tables=("N",), limits=limits).rows[0]
                assert initial.node_labels == ("A", "N") and initial.values == (1, "before")
                prior = reader.system_versions("N", initial.record_id, limits=limits).versions
                assert [row.node_labels for row in prior] == [("A", "N"), ("A", "N")]
                assert prior[-1].values == (1, "old-writer") and prior[-1].system_to is None
                changed = write(db, "MATCH(n:N) REMOVE n:N:A SET n:After")
                assert reader.system_versions("N", initial.record_id, limits=limits).versions == prior
                with pytest.raises(GrafxError):
                    reader.system_as_of(changed, tables=("N",), limits=limits)
            latest = other.system_as_of(changed, tables=("N",), limits=limits).rows[0]
            assert latest.record_id == initial.record_id and latest.node_labels == ("After",)
            assert latest.values == (1, "old-writer")
            assert other.verify("all").findings == ()


@pytest.mark.parametrize("indexed", [False, True])
def test_historical_nullable_schema_and_label_growth_preserve_edge_identity(tmp_path, indexed):
    from okto_grafx.domain.model.schema import ColumnDef
    from okto_grafx.domain.model.value import ValueType

    with connect(tmp_path / "db", page_size=512) as db:
        prepare(db, labeled=True)
        write(db, "CREATE(:N:B {id:2,value:'target'})")
        write(db, "CREATE REL TABLE R(FROM N TO N, weight INT64)")
        write(db, "MATCH(a:N {id:1}),(b:N {id:2}) CREATE(a)-[:R {weight:7}]->(b)")
        db.enable_system_history(("R",))
        activation = db.commit_history().entries[-1].identity
        if indexed:
            db.enable_system_history_index()
        limits = TemporalLimits(access_path="index" if indexed else "scan")
        old = db.system_as_of(activation, tables=("N", "R"), limits=limits)
        old_edge = next(row for row in old.rows if row.table == "R")
        db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
        changed = write(db, "MATCH(n:N {id:1}) REMOVE n:N:A SET n:Reviewed, n.extra='new'")
        new = db.system_as_of(changed, tables=("N", "R"), limits=limits)
        assert next(row for row in new.rows if row.table == "R") == old_edge
        assert old_edge.values[:2] == tuple(row.record_id for row in old.rows if row.table == "N")
        assert new.schemas[0].schema_version == old.schemas[0].schema_version + 1
        assert len(old.schemas[0].columns) == 2 and len(new.schemas[0].columns) == 3
        nodes = [row for row in new.rows if row.table == "N"]
        assert nodes[0].values == (1, "first", "new") and nodes[0].node_labels == ("Reviewed",)
        assert nodes[1].values == (2, "target", None) and nodes[1].node_labels == ("B", "N")
        assert db.system_as_of(activation, tables=("N", "R"), limits=limits).rows == old.rows
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("indexed", [False, True])
def test_unlabeled_history_preserves_exact_unicode_labels_and_nested_maps(tmp_path, indexed):
    with connect(tmp_path / "db", page_size=512) as db:
        write(db, "CREATE({v:[{k:1},{k:'text'}]})")
        db.enable_commit_history()
        table = db.catalog.catalog.tables()[0].name
        db.enable_system_history((table,))
        activation = db.commit_history().entries[-1].identity
        if indexed:
            db.enable_system_history_index()
        limits = TemporalLimits(access_path="index" if indexed else "scan")
        changed = write(db, "MATCH(n) SET n:`λ`:`a b`:`é`:`é`")
        initial = db.system_as_of(activation, tables=(table,), limits=limits).rows[0]
        latest = db.system_as_of(changed, tables=(table,), limits=limits).rows[0]
        assert initial.node_labels is None and latest.node_labels == ("a b", "é", "é", "λ")
        assert latest.values == initial.values and latest.record_id == initial.record_id
        assert latest.values[-1] == {"v": ({"k":1}, {"k":"text"})}
        delta = db.system_diff(activation, changed, tables=(table,), limits=limits).rows[0]
        assert delta.labels_added == latest.node_labels and delta.labels_removed == () and delta.properties == ()
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("indexed", [False, True])
def test_redacting_empty_membership_keeps_native_history_extents(tmp_path, indexed):
    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        db.ensure_identity_indexes()
        db.enable_commit_history()
        write(db, "CREATE NODE TABLE N(id INT64, value STRING, PRIMARY KEY(id))")
        write(db, "UNWIND range(1,40) AS id CREATE (n:N {id:id,value:'first'}) REMOVE n:N")
        db.enable_system_history(("N",))
        if indexed:
            db.enable_system_history_index()
        retained = write(db, "MATCH(n) SET n.value='second'")
        report = db.prune_system_history(retained, tables=("N",))
        assert report.redacted_versions == 40
        db.compact_system_history(confirm_quiescent=True)
        rows = db.system_as_of(retained, tables=("N",)).rows
        assert len(rows) == 40 and all(row.node_labels == () and row.values[1] == "second" for row in rows)
        assert db.verify("all").findings == ()
    with connect(path, page_size=512) as db:
        assert len(db.system_as_of(retained, tables=("N",)).rows) == 40
        assert db.verify("all").findings == ()


def test_delete_recreate_keeps_separate_historical_label_lineages(tmp_path):
    with connect(tmp_path / "db") as db:
        activation = prepare(db, labeled=True)
        initial = db.system_as_of(activation, tables=("N",)).rows[0]
        deleted = write(db, "MATCH (n) DELETE n")
        recreated = write(db, "CREATE (:N:B {id:1,value:'new'})")
        assert db.system_as_of(deleted, tables=("N",)).rows == ()
        current = db.system_as_of(recreated, tables=("N",)).rows[0]
        assert current.record_id != initial.record_id and current.node_labels == ("B", "N")
        assert db.system_versions("N", initial.record_id).versions[0].node_labels == ("A", "N")
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("operation", ["activate", "prune"])
@pytest.mark.parametrize("cut", ["before_commit", "after_history_root", "after_commit"])
def test_label_history_control_process_cuts_preserve_atomic_policy_and_membership(tmp_path, operation, cut):
    import os
    from pathlib import Path
    import subprocess
    import sys

    from okto_grafx.errors import GrafxHistoryUnavailable

    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        if operation == "activate":
            db.ensure_identity_indexes()
            db.enable_commit_history()
            write(db, "CREATE NODE TABLE N(id INT64, value STRING, PRIMARY KEY(id))")
            at = write(db, "CREATE(:N:A {id:1,value:'first'})")
        else:
            at = prepare(db, labeled=True)
            db.enable_system_history_index()
            write(db, "MATCH(n) REMOVE n:N:A")
        db.checkpoint()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    process = subprocess.run([sys.executable, str(Path(__file__).with_name("system_history_worker.py")),
                              str(path), operation, cut],
                             env=env, capture_output=True, text=True, timeout=90)
    assert process.returncode in (71,72,73,74), process.stdout + process.stderr
    committed = cut != "before_commit"
    for _ in range(2):
        with connect(path, page_size=512) as db:
            latest = db.commit_history().entries[-1].identity
            if operation == "activate" and not committed:
                with pytest.raises(GrafxHistoryUnavailable):
                    db.system_as_of(latest, tables=("N",))
            else:
                labels = ("A", "N") if operation == "activate" else ()
                assert db.system_as_of(latest, tables=("N",)).rows[0].node_labels == labels
                if operation == "prune":
                    if committed:
                        with pytest.raises(GrafxHistoryExpired):
                            db.system_as_of(at, tables=("N",))
                    else:
                        assert db.system_as_of(at, tables=("N",)).rows[0].node_labels == ("A", "N")
            assert db.execute("MATCH(n) RETURN labels(n)").rows == ((("A", "N") if operation == "activate" else (),),)
            assert db.verify("all").findings == ()
            db.checkpoint()


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("cut", ["before_commit", "before_apply", "after_current", "after_history_root", "after_history_chunk", "after_commit"])
def test_label_history_native_process_cuts_keep_current_and_historical_state_together(tmp_path, codec, cut):
    import os
    from pathlib import Path
    import subprocess
    import sys

    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        activation = prepare(db)
        db.enable_system_history_index()
        initial = db.system_as_of(activation, tables=("N",)).rows[0]
        db.checkpoint()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    process = subprocess.run([sys.executable, str(Path(__file__).with_name("system_history_worker.py")),
                              str(path), "node_labels", cut, codec],
                             env=env, capture_output=True, text=True, timeout=90)
    assert process.returncode in (71,72,73,74), process.stdout + process.stderr
    committed = cut != "before_commit"
    expected = ("Audit",) if committed else None
    for _ in range(2):
        with connect(path, page_size=512, codec="pure") as db:
            at = db.commit_history().entries[-1].identity
            for access in ("scan", "index"):
                limits = TemporalLimits(access_path=access)
                assert db.system_as_of(activation, tables=("N",), limits=limits).rows == (initial,)
                latest = db.system_as_of(at, tables=("N",), limits=limits).rows[0]
                assert latest.node_labels == expected
                assert latest.record_id == initial.record_id
                versions = db.system_versions("N", initial.record_id, limits=limits).versions
                assert len(versions) == (2 if committed else 1)
            row = db.execute("MATCH(n) RETURN labels(n), n.value").rows[0]
            assert row == (("Audit",) if committed else ("N",), "changed" * 200 if committed else "first")
            assert db.verify("all").findings == ()
            db.checkpoint()
