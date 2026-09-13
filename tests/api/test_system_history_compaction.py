"""Quiescent native replacement, physical reclamation and retained history agreement."""

import pytest

from okto_grafx import TemporalLimits, connect
from okto_grafx.errors import GrafxUnsupportedOperation, GrafxTransactionStateError
from test_system_time_queries import prepare, write


@pytest.mark.parametrize("indexed", [False, True])
def test_compaction_reclaims_and_preserves_retained_pictures(tmp_path, indexed):
    from okto_grafx.backup import create_backup, restore_backup
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        prepare(db)
        if indexed:
            db.enable_system_history_index()
        for i in range(6):
            write(db, "MATCH (n:N {id:1}) SET n.value = '" + str(i) * 4000 + "'")
        retained = write(db, "MATCH (n:N {id:1}) SET n.value = 'retained'")
        db.prune_system_history(retained, tables=("N",))
        db.pin_system_history("keep", retained, tables=("N",))
        expected = db.system_as_of(retained, tables=("N",))
        policy = db.system_history_pins()
        with pytest.raises(GrafxUnsupportedOperation):
            db.compact_system_history()
        with db.begin("read"):
            with pytest.raises(GrafxTransactionStateError):
                db.compact_system_history(confirm_quiescent=True)
        report = db.compact_system_history(confirm_quiescent=True)
        assert report.commit is not None
        assert report.physical_bytes_reclaimed > 12_000
        assert report.physical_bytes_after < report.physical_bytes_before
        assert (root / "system-history.dat").stat().st_size == report.physical_bytes_after
        assert db.system_history_pins() == policy
        assert db.system_as_of(retained, tables=("N",)).rows == expected.rows
        assert db.system_as_of(retained, tables=("N",), limits=TemporalLimits(access_path="scan")).rows == expected.rows
        write(db, "MATCH (n:N {id:1}) SET n.value = 'after compaction'")
        assert db.verify().clean
        db.checkpoint()
        create_backup(db, tmp_path / "backup")
    with connect(root, page_size=512, read_only=True) as db:
        assert db.system_as_of(retained, tables=("N",)).rows == expected.rows
        assert db.verify().clean
    restore_backup(tmp_path / "backup", tmp_path / "restored", confirm_original_offline=True)
    with connect(tmp_path / "restored", page_size=512, read_only=True) as db:
        assert db.system_history_pins() == policy
        assert db.system_as_of(retained, tables=("N",)).rows == expected.rows
        assert db.verify().clean


def test_repeated_compaction_and_retention_after_compaction(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        prepare(db)
        db.enable_system_history_index()
        for number in range(3):
            write(db, "MATCH (n:N {id:1}) SET n.value='" + str(number) * 4000 + "'")
        retained = write(db, "MATCH (n:N {id:1}) SET n.value='keep'")
        db.prune_system_history(retained, tables=("N",))
        assert db.compact_system_history(confirm_quiescent=True).physical_bytes_reclaimed > 0
        assert db.compact_system_history(confirm_quiescent=True).commit is None
        newer = write(db, "MATCH (n:N {id:1}) SET n.value='new'")
        db.prune_system_history(newer, tables=("N",))
        assert db.system_as_of(newer, tables=("N",)).rows[0].values == (1, "new")
        db.checkpoint()
        assert db.verify().clean


def test_committed_untruncated_tail_can_receive_a_new_native_write(tmp_path, monkeypatch):
    from okto_grafx.adapters.storage_local import LocalStorageDevice
    from okto_grafx.errors import GrafxError
    with connect(tmp_path / "db", page_size=512) as db:
        prepare(db)
        db.enable_system_history_index()
        for number in range(3):
            write(db, "MATCH (n:N {id:1}) SET n.value='" + str(number) * 4000 + "'")
        retained = write(db, "MATCH (n:N {id:1}) SET n.value='keep'")
        db.prune_system_history(retained, tables=("N",))
        before = (tmp_path / "db" / "system-history.dat").stat().st_size
        original = LocalStorageDevice.truncate_log
        def fail_tail(self, file, size):
            if file == "system-history.dat":
                raise OSError("injected before physical reclaim")
            return original(self, file, size)
        with monkeypatch.context() as patch:
            patch.setattr(LocalStorageDevice, "truncate_log", fail_tail)
            with pytest.raises(GrafxError):
                db.compact_system_history(confirm_quiescent=True)
        assert (tmp_path / "db" / "system-history.dat").stat().st_size == before
        newer = write(db, "MATCH (n:N {id:1}) SET n.value='definitely a new version'")
        assert db.system_as_of(newer, tables=("N",)).rows[0].values == (1, "definitely a new version")
        db.checkpoint()
        assert db.verify().clean
        assert db.compact_system_history(confirm_quiescent=True).physical_bytes_reclaimed > 0
