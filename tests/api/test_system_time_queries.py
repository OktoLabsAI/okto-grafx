"""Public temporal contracts over native commits, not retained MVCC accidents."""

import pytest

from okto_grafx import CommitId, TemporalLimits, Timestamp, connect
from okto_grafx.domain.model.schema import ColumnDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.errors import GrafxConfigurationError, GrafxHistoryUnavailable, GrafxQueryBudgetExceeded


def prepare(db):
    db.ensure_identity_indexes()
    db.enable_commit_history()
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE N(id INT64, value STRING, PRIMARY KEY(id))")
        tx.execute("CREATE (:N {id:1, value:'first'})")
    before = CommitId(db.identity.database_uuid, tx.report.csn)
    db.enable_system_history(("N",))
    return before, db.commit_history().entries[-1].identity


def write(db, query):
    with db.begin() as tx:
        tx.execute(query)
    return CommitId(db.identity.database_uuid, tx.report.csn)


def test_system_time_intervals_timestamps_delete_recreate_and_old_reader(tmp_path):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        before, activated = prepare(db)
        initial = db.system_as_of(activated, tables=("N",))
        assert len(initial.rows) == 1 and initial.rows[0].values == (1, "first")
        rid = initial.rows[0].record_id
        with db.begin("read") as reader:
            updated = write(db, "MATCH (n:N {id:1}) SET n.value = 'second'")
            assert reader.system_versions("N", rid).versions[0].system_to is None
            assert reader.system_as_of(activated, tables=("N",)).rows == initial.rows
        deleted = write(db, "MATCH (n:N {id:1}) DELETE n")
        recreated = write(db, "CREATE (:N {id:1, value:'new-lineage'})")
        versions = db.system_versions("N", rid).versions
        assert [(v.system_from, v.system_to) for v in versions] == [(activated, updated), (updated, deleted)]
        assert db.system_as_of(deleted, tables=("N",)).rows == ()
        current = db.system_as_of(recreated, tables=("N",))
        assert current.rows[0].record_id != rid
        instant = db.lookup_commit(updated).timing.ordered_at
        assert db.system_as_of(instant, tables=("N",)).as_of == updated
        assert db.system_as_of(instant, tables=("N",)).rows[0].values == (1, "second")
        with pytest.raises(GrafxHistoryUnavailable):
            db.system_as_of(before, tables=("N",))
        with pytest.raises(GrafxHistoryUnavailable):
            db.system_as_of(Timestamp(0), tables=("N",))
        db.checkpoint()
    with connect(root, page_size=512, read_only=True) as db:
        assert db.system_as_of(activated, tables=("N",)).rows == initial.rows
        assert db.system_as_of(recreated, tables=("N",)).rows == current.rows


def test_historical_schema_and_virtual_nullable_values(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        _, activated = prepare(db)
        old = db.system_as_of(activated, tables=("N",))
        db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
        after = db.commit_history().entries[-1].identity
        now = db.system_as_of(after, tables=("N",))
        assert len(old.schemas[0].columns) == 2
        assert len(now.schemas[0].columns) == 3
        assert now.rows[0].values == (1, "first", None)
        assert db.system_as_of(activated, tables=("N",)) .rows == old.rows


def test_historical_edges_keep_endpoint_lineage_through_detach_recreate(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        prepare(db)
        write(db, "CREATE (:N {id:2, value:'other'})")
        write(db, "CREATE REL TABLE R(FROM N TO N, label STRING)")
        write(db, "MATCH (a:N {id:1}), (b:N {id:2}) CREATE (a)-[:R {label:'old'}]->(b)")
        db.enable_system_history(("R",))
        connected = db.commit_history().entries[-1].identity
        old = db.system_as_of(connected, tables=("N", "R"))
        assert len(old.rows) == 3
        deleted = write(db, "MATCH (n:N {id:1}) DETACH DELETE n")
        assert len(db.system_as_of(deleted, tables=("N", "R")).rows) == 1
        recreated = write(db, "CREATE (:N {id:1, value:'new'})")
        assert len(db.system_as_of(recreated, tables=("N", "R")).rows) == 2
        assert db.system_as_of(connected, tables=("N", "R")).rows == old.rows
        with pytest.raises(GrafxConfigurationError):
            db.system_as_of(connected, tables=("R",))


@pytest.mark.parametrize("limits", [TemporalLimits(max_events=1), TemporalLimits(max_bytes=1)])
def test_bounded_read_refuses_without_partial_output(tmp_path, limits):
    with connect(tmp_path / "db") as db:
        _, activated = prepare(db)
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.system_as_of(activated, tables=("N",), limits=limits)


def test_invalid_temporal_inputs_do_not_begin_a_transaction(tmp_path, monkeypatch):
    with connect(tmp_path / "db") as db:
        from okto_grafx.engine.database import Database
        monkeypatch.setattr(Database, "begin", lambda *_args, **_kwargs: pytest.fail("Unexpected I/O admission"))
        with pytest.raises(GrafxConfigurationError):
            db.system_as_of(CommitId(b"z" * 16, 1), tables=("N",))
        with pytest.raises(GrafxConfigurationError):
            db.system_versions("N", True)
