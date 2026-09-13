"""Native temporal values in evolved schemas and the flexible graph model."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.model.schema import ColumnDef
from okto_grafx.domain.model.value import value_type_of
from okto_grafx.domain.model.temporal_codec import TEMPORAL_VALUES_CAPABILITY
from tests.storage_core.test_temporal_admission import VALUES


@pytest.mark.parametrize("value", VALUES)
def test_append_temporal_column_preserves_older_row_layout(tmp_path, value):
    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1})")
        db.add_nullable_column("N", ColumnDef("val", value_type_of(value)))
        assert db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        assert db.execute("MATCH (n:N) RETURN n.val").rows == ((None,),)
        with db.begin() as tx:
            tx.execute("CREATE (:N {id:2, val:$v})", {"v": value})
        assert db.execute("MATCH (n:N) RETURN n.val ORDER BY n.id").rows == ((None,), (value,))
    with connect(path, page_size=512) as db:
        assert db.execute("MATCH (n:N) RETURN n.val ORDER BY n.id").rows == ((None,), (value,))
        assert not db.verify("all").findings


def test_unlabeled_nodes_and_implicit_relationships_persist_temporals(tmp_path):
    path = tmp_path / "db"
    with connect(path, page_size=512) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE (a {id:1, val:$v}), (b {id:2}), (a)-[:HAPPENED {val:$d}]->(b)",
                       {"v": VALUES[0], "d": VALUES[5]})
        assert db.execute("MATCH (n {id:1}) RETURN labels(n), n.val").rows == (((), VALUES[0]),)
        assert db.execute("MATCH ()-[r:HAPPENED]->() RETURN r.val").rows == ((VALUES[5],),)
    with connect(path, page_size=512) as db:
        assert db.execute("MATCH ()-[r:HAPPENED]->() RETURN r.val").rows == ((VALUES[5],),)
        assert not db.verify("all").findings


def test_late_nonfinite_nested_value_does_not_leave_earlier_temporal_row(tmp_path):
    from okto_grafx.errors import GrafxError
    with connect(tmp_path / "db", page_size=512) as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
        with db.begin() as tx:
            tx.execute("CREATE (:N {id:0, val:'prior'})")
            with pytest.raises(GrafxError):
                tx.execute("UNWIND $rows AS r CREATE (:N {id:r.id, val:r.val})",
                           {"rows": [{"id": 1, "val": VALUES[0]}, {"id": 2, "val": {"bad": float('nan')}}]})
        assert not db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        assert db.execute("MATCH (n:N) RETURN n.id").rows == ((0,),)
