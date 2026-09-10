"""State/lineage differences, schema changes, aggregate bounds and no mutation."""

import pytest
from okto_grafx import connect, CommitId, TemporalLimits
from okto_grafx.domain.model.schema import ColumnDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.errors import GrafxConfigurationError, GrafxQueryBudgetExceeded, GrafxHistoryExpired
from tests.api.test_system_time_queries import prepare, write


def test_diff_properties_schema_recreate_and_read_boundary(tmp_path):
    with connect(tmp_path / 'db') as db:
        _, start = prepare(db)
        updated = write(db, "MATCH (n:N {id:1}) SET n.value='updated'")
        diff = db.system_diff(start, updated, tables=('N',))
        assert len(diff.rows) == 1 and diff.rows[0].operation == 'updated'
        assert diff.rows[0].properties[0].name == 'value'
        assert (diff.rows[0].properties[0].before, diff.rows[0].properties[0].after) == ('first', 'updated')
        assert not db.system_diff(updated, updated, tables=('N',)).rows
        with db.begin('read') as reader:
            db.add_nullable_column('N', ColumnDef('extra', ValueType.STRING))
            added_column = db.commit_history().entries[-1].identity
            with pytest.raises(GrafxConfigurationError):
                reader.system_diff(start, added_column, tables=('N',))
        diff = db.system_diff(updated, added_column, tables=('N',))
        assert len(diff.schemas) == 1
        prop = diff.rows[0].properties[0]
        assert prop.name == 'extra' and not prop.before_present and prop.after_present and prop.after is None
        write(db, 'MATCH (n:N {id:1}) DELETE n')
        end = write(db, "CREATE (:N {id:1,value:'recreated'})")
        diff = db.system_diff(start, end, tables=('N',))
        assert {r.operation for r in diff.rows} == {'added', 'removed'}
        assert len({r.record_id for r in diff.rows}) == 2
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.system_diff(start, end, tables=('N',), max_changes=1)
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.system_diff(start, end, tables=('N',), limits=TemporalLimits(max_rows=1))
        for before, after in ((end, start), (start, CommitId(bytes(16), end.sequence))):
            with pytest.raises(GrafxConfigurationError):
                db.system_diff(before, after, tables=('N',))
        db.prune_system_history(end, tables=('N',))
        with pytest.raises(GrafxHistoryExpired):
            db.system_diff(start, end, tables=('N',))
        assert db.verify().clean


def test_diff_relationships_and_empty_schema_changes(tmp_path):
    with connect(tmp_path / 'db') as db:
        prepare(db)
        write(db, 'CREATE REL TABLE R(FROM N TO N, weight INT64)')
        db.enable_system_history(('R',))
        start = db.commit_history().entries[-1].identity
        end = write(db, 'MATCH (a:N),(b:N) WHERE a.id=1 AND b.id=1 CREATE (a)-[:R {weight:2}]->(b)')
        diff = db.system_diff(start, end, tables=('N','R'))
        assert len(diff.rows) == 1
        assert diff.rows[0].table_kind == 'rel' and diff.rows[0].operation == 'added'
        assert any(p.name == 'weight' and p.after == 2 for p in diff.rows[0].properties)
        with pytest.raises(GrafxConfigurationError):
            db.system_diff(start, end, tables=('R',))
        assert db.verify().clean
