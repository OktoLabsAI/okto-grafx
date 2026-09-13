"""Native optional temporal access publication and same-snapshot parity."""

import pytest

from okto_grafx import connect, TemporalLimits
from okto_grafx.errors import GrafxQueryBudgetExceeded
from test_system_time_queries import prepare, write


def test_index_activation_updates_checkpoint_reopen(tmp_path):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        _, activated = prepare(db)
        rid = db.system_as_of(activated, tables=("N",)).rows[0].record_id
        write(db, "MATCH (n:N {id:1}) SET n.value = 'second'")
        assert db.enable_system_history_index()
        assert not db.enable_system_history_index()
        with db.begin("read") as old:
            previous = old.system_versions("N", rid)
            write(db, "MATCH (n:N {id:1}) SET n.value = 'third'")
            assert old.system_versions("N", rid) == previous
        indexed = db.system_versions("N", rid)
        assert indexed == db.system_versions("N", rid, limits=TemporalLimits(access_path="scan"))
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.system_versions("N", rid, limits=TemporalLimits(max_events=1))
        db.checkpoint()
    with connect(root, page_size=512, read_only=True) as db:
        assert db.system_versions("N", rid) == indexed


def test_index_retention_rebuilds_same_authority_and_preserves_pin(tmp_path):
    from okto_grafx.errors import GrafxConfigurationError
    with connect(tmp_path / "db", page_size=512) as db:
        _, activated = prepare(db)
        rid = db.system_as_of(activated, tables=("N",)).rows[0].record_id
        db.enable_system_history_index()
        db.checkpoint()
        updated = write(db, "MATCH (n:N {id:1}) SET n.value = 'second'")
        db.pin_system_history("keep", activated, tables=("N",))
        with pytest.raises(GrafxConfigurationError):
            db.prune_system_history(updated, tables=("N",))
        db.unpin_system_history("keep")
        db.prune_system_history(updated, tables=("N",))
        assert db.system_versions("N", rid) == db.system_versions("N", rid, limits=TemporalLimits(access_path="scan"))
        db.checkpoint()
        assert db.verify().clean


@pytest.mark.parametrize("damage", ["payload", "foreign", "future"])
def test_index_damaged_path_refuses_without_scan_fallback(tmp_path, damage):
    from okto_grafx.domain.page import Page
    from okto_grafx.engine.system_history_index_store import head_root
    from okto_grafx.engine.system_history_store import _HEAD
    from okto_grafx.errors import GrafxError
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        _, at = prepare(db)
        db.enable_system_history_index()
        db.checkpoint()
    path = root / "system-history.dat"
    raw = bytearray(path.read_bytes())
    node, _ = head_root(Page.from_bytes(raw[:512]).read_slot(0), _HEAD.size)
    page = Page.from_bytes(raw[node * 512:(node + 1) * 512])
    body = bytearray(page.read_slot(0))
    if damage == "future":
        page.page_lsn += 10000
    elif damage == "foreign":
        body[8:24] = b"x" * 16
    else:
        body[-1] ^= 1
    page.update_slot(0, bytes(body))
    raw[node * 512:(node + 1) * 512] = page.to_bytes()
    path.write_bytes(raw)
    before = path.read_bytes()
    with pytest.raises(GrafxError):
        with connect(root, page_size=512, read_only=True) as db:
            db.system_as_of(at, tables=("N",))
    assert path.read_bytes() == before


def test_independent_participant_open_before_index_keeps_read_snapshot(tmp_path):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        _, at = prepare(db)
        rid = db.system_as_of(at, tables=("N",)).rows[0].record_id
        with connect(root, page_size=512) as other:
            with other.begin("read") as old:
                db.enable_system_history_index()
                write(db, "MATCH (n:N {id:1}) SET n.value='later'")
                assert old.system_as_of(at, tables=("N",)).rows[0].values == (1, "first")
                versions = old.system_versions("N", rid).versions
                assert len(versions) == 1 and versions[0].system_to is None
            assert other.system_versions("N", rid).versions[-1].values == (1, "later")
            write(other, "MATCH (n:N {id:1}) SET n.value='other writer'")
            assert db.system_versions("N", rid).versions[-1].values == (1, "other writer")
        assert db.verify().clean


def test_writer_prepared_before_index_activation_is_not_omitted(tmp_path):
    from okto_grafx.errors import GrafxWriteConflict
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        prepare(db)
        with connect(root, page_size=512) as other:
            try:
                with other.begin("write") as staged:
                    staged.execute("MATCH (n:N {id:1}) SET n.value='old writer'")
                    db.enable_system_history_index()
            except GrafxWriteConflict:
                # A genuine first-OCC conflict is not permission to drop a row
                # effect or bypass validation. Retry through a new transaction.
                write(other, "MATCH (n:N {id:1}) SET n.value='old writer'")
            at = db.commit_history().entries[-1].identity
            assert db.system_as_of(at, tables=("N",)).rows[0].values == (1, "old writer")
        assert db.verify().clean


def test_index_asof_schema_edges_and_structural_work(tmp_path, monkeypatch):
    from okto_grafx.domain.model.schema import ColumnDef
    from okto_grafx.domain.model.value import ValueType
    from okto_grafx.engine.system_history_store import SystemHistoryStore
    with connect(tmp_path / "db", page_size=512) as db:
        _, activated = prepare(db)
        write(db, "CREATE (:N {id:2, value:'other'})")
        write(db, "CREATE REL TABLE R(FROM N TO N, label STRING)")
        write(db, "MATCH (a:N {id:1}), (b:N {id:2}) CREATE (a)-[:R {label:'edge'}]->(b)")
        db.enable_system_history(("R",))
        connected = db.commit_history().entries[-1].identity
        db.enable_system_history_index()
        db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
        altered = db.commit_history().entries[-1].identity
        for n in range(20):
            write(db, f"MATCH (n:N {{id:1}}) SET n.value = 'value-{n}'")
        current = db.commit_history().entries[-1].identity
        expected = {at: db.system_as_of(at, tables=("N", "R"), limits=TemporalLimits(access_path="scan"))
                    for at in (connected, altered, current)}
        def forbidden(*args, **kwargs):
            pytest.fail("eligible indexed read scanned the full retained history")
        with monkeypatch.context() as patch:
            patch.setattr(SystemHistoryStore, "_iter_batches", forbidden)
            for at, scan in expected.items():
                indexed = db.system_as_of(at, tables=("N", "R"))
                assert indexed.schemas == scan.schemas and indexed.rows == scan.rows
                assert indexed.events_scanned <= 8
            rid = expected[current].rows[0].record_id
            assert len(db.system_versions("N", rid).versions) == 21
        deleted = write(db, "MATCH (n:N {id:1}) DETACH DELETE n")
        recreated = write(db, "CREATE (:N {id:1, value:'new'})")
        for at in (deleted, recreated):
            assert db.system_as_of(at, tables=("N", "R")).rows == db.system_as_of(
                at, tables=("N", "R"), limits=TemporalLimits(access_path="scan")).rows
        assert db.verify().clean
