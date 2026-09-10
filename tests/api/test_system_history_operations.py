"""Protected native retention, backup and verification contracts."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxConfigurationError, GrafxHistoryExpired, GrafxTransactionBudgetExceeded
from tests.api.test_system_time_queries import prepare, write


def test_pins_retention_current_rows_and_reopen(tmp_path):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        _, activation = prepare(db)
        rid = db.system_as_of(activation, tables=("N",)).rows[0].record_id
        changed = write(db, "MATCH (n:N {id:1}) SET n.value = 'second'")
        db.pin_system_history("audit", activation, tables=("N",))
        db.pin_system_history("audit", activation, tables=("N",))
        assert db.system_history_pins()[0].at == activation
        with pytest.raises(GrafxConfigurationError):
            db.prune_system_history(changed, tables=("N",))
        db.checkpoint()
    with connect(root, page_size=512) as db:
        assert len(db.system_history_pins()) == 1
        db.unpin_system_history("audit")
        with pytest.raises(GrafxTransactionBudgetExceeded):
            db.prune_system_history(changed, tables=("N",), max_bytes=1)
        report = db.prune_system_history(changed, tables=("N",))
        assert report.redacted_versions == 1 and report.redacted_bytes > 0
        assert report.physical_bytes_reclaimed == 0
        assert db.prune_system_history(changed, tables=("N",)).commit is None
        with pytest.raises(GrafxHistoryExpired):
            db.system_as_of(activation, tables=("N",))
        assert db.system_as_of(changed, tables=("N",)).rows[0].values == (1, "second")
        assert len(db.system_versions("N", rid).versions) == 1
        assert db.verify().clean
        db.checkpoint()
    with connect(root, page_size=512, read_only=True) as db:
        assert db.system_history_pins() == ()
        assert db.system_as_of(changed, tables=("N",)).rows[0].values == (1, "second")
        assert db.verify().clean


def test_physical_backup_preserves_history_pins_and_explicit_logical_export(tmp_path):
    from okto_grafx.backup import create_backup, restore_backup
    from okto_grafx.transfer import export_graph, import_graph
    from okto_grafx.errors import GrafxRecoveryRefused
    with connect(tmp_path / "db", page_size=512) as db:
        _, activation = prepare(db)
        changed = write(db, "MATCH (n:N {id:1}) SET n.value = 'second'")
        db.prune_system_history(changed, tables=("N",))
        db.pin_system_history("backup", changed, tables=("N",))
        create_backup(db, tmp_path / "backup")
        with pytest.raises(GrafxRecoveryRefused):
            export_graph(db, tmp_path / "refused")
        assert not (tmp_path / "refused").exists()
        export_graph(db, tmp_path / "logical", history="current-only")
    restore_backup(tmp_path / "backup", tmp_path / "restored", confirm_original_offline=True)
    with connect(tmp_path / "restored", page_size=512) as db:
        assert db.system_history_pins()[0].name == "backup"
        assert db.system_as_of(changed, tables=("N",)).rows[0].values == (1, "second")
        with pytest.raises(GrafxHistoryExpired):
            db.system_as_of(activation, tables=("N",))
        assert db.verify().clean
    import_graph(tmp_path / "logical", tmp_path / "imported")
    with connect(tmp_path / "imported") as db:
        assert db.execute("MATCH (n:N) RETURN n.value").rows == (("second",),)
        assert db._catalog.catalog.system_history_tables() == ()


@pytest.mark.parametrize("cut", ["before_commit", "before_apply", "after_current", "after_history_root", "after_history_chunk", "after_commit"])
def test_retention_process_cuts(tmp_path, cut):
    import os
    from pathlib import Path
    import subprocess
    import sys
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        _, activation = prepare(db)
        changed = write(db, "MATCH (n:N {id:1}) SET n.value = 'second'")
        db.checkpoint()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("system_history_worker.py")),
                             str(root), "prune", cut], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode in (71, 72, 73, 74), result.stdout + result.stderr
    for _ in range(2):
        with connect(root, page_size=512) as db:
            if cut == "before_commit":
                assert db.system_as_of(activation, tables=("N",)).rows[0].values == (1, "first")
            else:
                with pytest.raises(GrafxHistoryExpired):
                    db.system_as_of(activation, tables=("N",))
            assert db.system_as_of(changed, tables=("N",)).rows[0].values == (1, "second")
            assert db.verify().clean
            db.checkpoint()
