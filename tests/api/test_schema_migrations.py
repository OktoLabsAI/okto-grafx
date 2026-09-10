"""Application migration atomicity, refusal, dry-run and restart semantics."""

import subprocess
import sys

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxConfigurationError, GrafxLedgerError, GrafxUnsupportedOperation, GrafxWriteConflict, GrafxParseError
from okto_grafx.migrations import SchemaMigration, migrate_schema


def plan():
    return (
        SchemaMigration(1, ("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))",)),
        SchemaMigration(2, ("CREATE VECTOR SPACE s {dimension: 3, metric: 'cosine'}",
                            "CREATE NODE TABLE V(id INT64, embedding VECTOR(s), PRIMARY KEY(id))",
                            "CREATE REL TABLE R(FROM P TO V)")),
    )


def test_dry_run_apply_idempotence_and_reopen(tmp_path):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        lsn = db.wal.last_lsn
        preview = migrate_schema(db, plan(), namespace="app", dry_run=True)
        assert preview.pending == (1, 2) and not preview.applied
        assert not db.catalog.catalog.tables() and db.wal.last_lsn == lsn
        applied = migrate_schema(db, plan(), namespace="app")
        assert applied.applied == (1, 2) and len(applied.applied_lsns) == 2
        assert applied.applied_lsns[0][1] < applied.applied_lsns[1][1]
        repeat = migrate_schema(db, plan(), namespace="app")
        assert repeat.previously_applied == (1, 2) and not repeat.applied and not repeat.pending
        assert not db.verify("all").findings
        db.checkpoint()
    with connect(root, page_size=512, read_only=True) as db:
        assert migrate_schema(db, plan(), namespace="app", dry_run=True).previously_applied == (1, 2)


def test_checksums_gaps_ahead_and_reserved_or_unowned_ledger(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        migrate_schema(db, plan(), namespace="app")
        for changed in (plan()[:1], (SchemaMigration(1, (plan()[0].statements[0] + " ",)),) + plan()[1:]):
            with pytest.raises(GrafxLedgerError):
                migrate_schema(db, changed, namespace="app")
        with pytest.raises(GrafxConfigurationError):
            migrate_schema(db, plan()[1:], namespace="app")
        with pytest.raises(GrafxConfigurationError):
            migrate_schema(db, (SchemaMigration(1, ("CREATE NODE TABLE _grafx_migrations_other(id INT64)",)),), namespace="app")
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE _grafx_migrations_bad(version INT64, checksum STRING, PRIMARY KEY(version))")
        with pytest.raises(GrafxLedgerError):
            migrate_schema(db, (), namespace="bad")


@pytest.mark.parametrize("statement", ["CREATE (:P {id:1})", "MATCH (p:P) RETURN p", "CALL checkpoint()"])
def test_non_ddl_is_refused_before_any_change(tmp_path, statement):
    with connect(tmp_path / "db", page_size=512) as db:
        lsn = db.wal.last_lsn
        with pytest.raises((GrafxUnsupportedOperation, GrafxParseError)):
            migrate_schema(db, (SchemaMigration(1, (statement,)),), namespace="app")
        assert not db.catalog.catalog.tables() and db.wal.last_lsn == lsn


def test_atomic_runtime_failure_resume_and_bounded_occ_retry(tmp_path, monkeypatch):
    from okto_grafx.engine.database import Transaction

    with connect(tmp_path / "db", page_size=512) as db:
        original = Transaction.execute

        def fail_second(tx, text, *args, **kwargs):
            if text.startswith("CREATE REL TABLE"):
                raise OSError("injected step failure")
            return original(tx, text, *args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(Transaction, "execute", fail_second)
            with pytest.raises(OSError):
                migrate_schema(db, plan(), namespace="app")
        assert db.catalog.catalog.has_table("P") and not db.catalog.catalog.has_table("V")
        assert migrate_schema(db, plan(), namespace="app", dry_run=True).pending == (2,)
        conflicts = []

        def conflict_once(tx, text, *args, **kwargs):
            if text.startswith("CREATE VECTOR") and not conflicts:
                conflicts.append(True)
                raise GrafxWriteConflict("injected optimistic conflict")
            return original(tx, text, *args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(Transaction, "execute", conflict_once)
            report = migrate_schema(db, plan(), namespace="app")
        assert report.applied == (2,) and report.previously_applied == (1,)
        assert not db.verify("all").findings


def test_process_death_after_version_commit_resumes_only_pending(tmp_path):
    root = tmp_path / "db"
    code = '''
import os, sys
from okto_grafx import connect
from okto_grafx.migrations import SchemaMigration, migrate_schema
db = connect(sys.argv[1], page_size=512)
migrate_schema(db, (SchemaMigration(1, ("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))",)),), namespace="app")
os._exit(73)
'''
    child = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, text=True, timeout=30)
    assert child.returncode == 73, child.stderr
    with connect(root, page_size=512) as db:
        report = migrate_schema(db, plan(), namespace="app")
        assert report.previously_applied == (1,) and report.applied == (2,)
        assert not db.verify("all").findings


def test_independent_migrators_do_not_double_apply(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import okto_grafx.migrations as module

    barrier = threading.Barrier(2)
    local = threading.local()
    original = module._simulate

    def overlap(catalog, migrations):
        original(catalog, migrations)
        if not getattr(local, "started", False):
            local.started = True
            barrier.wait(timeout=10)

    root = tmp_path / "db"
    with connect(root, page_size=512) as left, connect(root, page_size=512) as right:
        monkeypatch.setattr(module, "_simulate", overlap)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(migrate_schema, db, plan(), namespace="app", max_attempts=8)
                       for db in (left, right)]
            reports = [f.result(timeout=30) for f in futures]
        assert sorted(v for report in reports for v in report.applied) == [1, 2]
        assert not left.verify("all").findings


def test_process_death_before_first_commit_leaves_no_applied_schema(tmp_path):
    root = tmp_path / "db"
    code = '''
import os, sys
from okto_grafx import connect
from okto_grafx.engine.database import Transaction
from okto_grafx.migrations import SchemaMigration, migrate_schema
db = connect(sys.argv[1], page_size=512)
Transaction.commit = lambda self: os._exit(74)
migrate_schema(db, (SchemaMigration(1, ("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))",)),), namespace="app")
'''
    child = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, text=True, timeout=30)
    assert child.returncode == 74, child.stderr
    with connect(root, page_size=512) as db:
        assert not db.catalog.catalog.has_table("P")
        assert not db.catalog.catalog.has_table("_grafx_migrations_app")
        assert migrate_schema(db, plan(), namespace="app").applied == (1, 2)
        assert not db.verify("all").findings
