"""Temporal lifecycle failures must refuse without changing authoritative payloads."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.page import Page
from okto_grafx.errors import GrafxError, GrafxHistoryExpired
from tests.api.test_system_time_queries import prepare, write


@pytest.mark.parametrize("damage", ["future_lsn", "foreign_uuid", "missing_chunk"])
def test_temporal_damage_is_not_silently_repaired_or_returned(tmp_path, damage):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        _, activation = prepare(db)
        db.checkpoint()
    path = root / "system-history.dat"
    raw = bytearray(path.read_bytes())
    if damage == "missing_chunk":
        del raw[-512:]
    else:
        page = Page.from_bytes(raw[:512])
        if damage == "future_lsn":
            page.page_lsn += 100
        else:
            payload = bytearray(page.read_slot(0))
            payload[8:24] = b"foreign-identity"
            page.update_slot(0, payload)
        raw[:512] = page.to_bytes()
    path.write_bytes(raw)
    before = path.read_bytes()
    with pytest.raises(GrafxError):
        with connect(root, page_size=512, read_only=True) as db:
            db.system_as_of(activation, tables=("N",))
    assert path.read_bytes() == before


def test_retention_conflicts_with_concurrent_commit_before_staging_publication(tmp_path, monkeypatch):
    from okto_grafx.engine.system_history_publication import HistoryPublication
    root = tmp_path / "db"
    with connect(root, page_size=512) as db, connect(root, page_size=512) as other:
        _, activation = prepare(db)
        changed = write(db, "MATCH (n:N {id:1}) SET n.value='second'")
        original = HistoryPublication.stage_control
        def race(self, txn, **kwargs):
            result = original(self, txn, **kwargs)
            if self.manager is db._transactions:
                write(other, "MATCH (n:N {id:1}) SET n.value='third'")
            return result
        with monkeypatch.context() as scoped:
            scoped.setattr(HistoryPublication, "stage_control", race)
            with pytest.raises(GrafxError):
                db.prune_system_history(changed, tables=("N",))
        assert db.system_as_of(activation, tables=("N",)).rows[0].values == (1, "first")
        assert db.execute("MATCH (n:N) RETURN n.value").rows == (("third",),)
        assert db.verify().clean
        db.prune_system_history(changed, tables=("N",))
        with pytest.raises(GrafxHistoryExpired):
            db.system_as_of(activation, tables=("N",))
        assert db.verify().clean


def test_retention_keeps_edges_and_nullable_current_schema(tmp_path):
    from okto_grafx.domain.model.schema import ColumnDef
    from okto_grafx.domain.model.value import ValueType
    with connect(tmp_path / "db", page_size=512) as db:
        _, activation = prepare(db)
        write(db, "CREATE (:N {id:2,value:'other'})")
        write(db, "CREATE REL TABLE R(FROM N TO N, label STRING)")
        write(db, "MATCH (a:N {id:1}),(b:N {id:2}) CREATE (a)-[:R {label:'old'}]->(b)")
        db.enable_system_history(("R",))
        db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
        changed = write(db, "MATCH (n:N {id:1}) SET n.value='new'")
        db.prune_system_history(changed, tables=("N", "R"))
        graph = db.system_as_of(changed, tables=("N", "R"))
        assert len(graph.rows) == 3
        assert all(len(row.values) == 3 for row in graph.rows)
        assert db.verify().clean
