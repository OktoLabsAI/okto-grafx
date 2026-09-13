"""Native typed collection DDL, transactional updates, metadata consumers and recovery."""

from pathlib import Path
from dataclasses import FrozenInstanceError, replace
import subprocess
import sys

import pytest

from okto_grafx import connect, DecimalValue, DateValue, StoredType, CommitId
from okto_grafx.errors import GrafxError, GrafxParseError, GrafxWriteConflict
from okto_grafx.domain.model.schema import ColumnDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.catalog_copy import capture_copy, copy_graph, prepare_copy_target
from okto_grafx.transfer import export_graph, import_graph, TransferLimits

DDL = "CREATE NODE TABLE N(id INT64, xs LIST<INT64 NOT NULL>, amounts MAP<DECIMAL(12,4)>, pair ARRAY<STRING,2>, info STRUCT<day:DATE NOT NULL,meta:MAP<ANY>>,PRIMARY KEY(id))"
INPUT = {"xs": [1, 2], "amounts": {"price": DecimalValue(125, 3, 2)}, "pair": ["a", None],
         "info": {"day": DateValue(2024, 2, 29), "meta": {"nested": [DecimalValue(1, 1, 0), True]}}}
EXPECTED = ((1, 2), {"price": DecimalValue(12500, 12, 4)}, ("a", None),
            {"day": DateValue(2024, 2, 29), "meta": {"nested": (DecimalValue(1, 1, 0), True)}})
CREATE = "CREATE(n:N {id:$id,xs:$xs,amounts:$amounts,pair:$pair,info:$info}) RETURN n.xs,n.amounts,n.pair,n.info"
READ = "MATCH(n:N) RETURN n.xs,n.amounts,n.pair,n.info ORDER BY n.id"


def seed(db, *, data=True):
    db.ensure_identity_indexes()
    with db.begin() as tx:
        tx.execute(DDL)
        tx.execute("CREATE REL TABLE R(FROM N TO N, v LIST<DECIMAL(12,4)>)")
        if data:
            tx.execute(CREATE, {"id": 1, **INPUT})
            tx.execute("CREATE(:N {id:2})")
            tx.execute("MATCH(a:N {id:1}),(b:N {id:2}) CREATE(a)-[:R {v:[decimal('2.5',2,1),null]}]->(b)")


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_all_collection_columns_create_update_rollback_return_and_reopen(tmp_path, codec):
    with connect(tmp_path / "db", page_size=512, codec=codec) as db:
        seed(db, data=False)
        with db.begin() as tx:
            result = tx.execute(CREATE, {"id": 1, **INPUT})
            assert result.rows == (EXPECTED,)
            assert tx.execute(READ).rows == (EXPECTED,)
            tx.execute("CREATE(:N {id:2})")
            tx.execute("MATCH(a:N {id:1}),(b:N {id:2}) CREATE(a)-[:R {v:[decimal('2.5',2,1),null]}]->(b)")
        tx = db.begin()
        tx.execute("MATCH(n:N {id:1}) SET n.xs=[3,4]")
        tx.rollback()
        assert db.execute(READ).rows[0] == EXPECTED
        with db.begin() as tx:
            assert tx.execute("MATCH(n:N {id:1}) SET n.amounts={new:decimal('4.5',2,1)} RETURN n.amounts").rows == (({"new": DecimalValue(45000, 12, 4)},),)
        assert db.catalog.catalog.table("N").columns[3].stored_type == StoredType("ARRAY", element=StoredType("STRING"), length=2)
        assert db.execute("MATCH()-[r:R]->() RETURN r.v").rows == (((DecimalValue(25000, 12, 4), None),),)
        assert not db.verify("all").findings
        db.checkpoint()
    with connect(tmp_path / "db", page_size=512, codec=codec) as db:
        assert db.execute("MATCH(n:N {id:1}) RETURN n.amounts").rows == (({"new": DecimalValue(45000, 12, 4)},),)
        assert db.catalog.catalog.table("N").columns[4].stored_type.describe() == "STRUCT<day:DATE NOT NULL,meta:MAP<ANY>>"
        assert not db.verify("all").findings


@pytest.mark.parametrize("declaration", ["LIST", "LIST<>", "MAP<BOGUS>", "ARRAY<INT64>", "ARRAY<INT64,-1>",
    "ARRAY<INT64,1.0>", "ARRAY<INT64,4294967296>", "STRUCT<a:INT64,a:STRING>", "STRUCT<a:INT64,>",
    "STRUCT<a INT64>", "LIST<DECIMAL(12,13)>", "MAP<VECTOR(space)>", "LIST<INT64 NOT>"])
def test_invalid_collection_ddl_is_parse_error_without_schema_effect(tmp_path, declaration):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            with pytest.raises(GrafxParseError):
                tx.execute(f"CREATE NODE TABLE Bad(v {declaration})")
        assert not db._catalog.catalog.has_table("Bad")
        assert not db._catalog.catalog.requires_capability("typed_collections_v1")


@pytest.mark.parametrize("prop,bad", [("xs", [1, None]), ("xs", [True]), ("amounts", {"x": DecimalValue(123456, 6, 5)}),
    ("pair", ["a"]), ("pair", ["a", "b", "c"]), ("info", {}), ("info", {"day": DateValue(2024), "extra": 1})])
def test_late_invalid_collection_rolls_back_statement_not_prior_staging(tmp_path, prop, bad):
    with connect(tmp_path / "db") as db:
        seed(db, data=False)
        with db.begin() as tx:
            tx.execute("CREATE(:N {id:99})")
            with pytest.raises(GrafxError):
                tx.execute(f"UNWIND $rows AS row CREATE(:N {{id:row.id,{prop}:row.v}})",
                           {"rows": [{"id": 1, "v": INPUT[prop]}, {"id": 2, "v": bad}]})
            assert tx.execute("MATCH(n:N) RETURN n.id").rows == ((99,),)
        assert not db.verify("all").findings


def test_ddl_data_and_capability_rollback_leave_no_half_activation(tmp_path):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        before = db._catalog.catalog.serialize()
        tx = db.begin()
        tx.execute(DDL)
        tx.execute(CREATE, {"id": 1, **INPUT})
        tx.rollback()
        assert db._catalog.catalog.serialize() == before
        assert not db._catalog.catalog.requires_capability("typed_collections_v1")
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE Clean(id INT64)")
        assert not db.verify("all").findings


def test_public_schema_and_returned_plan_cannot_mutate_authoritative_descriptor(tmp_path):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        independent_plan = db.explain(DDL)
        with db.begin() as tx:
            result = tx.execute(DDL)
        exposed = db.catalog.catalog.table("N").columns[1].stored_type
        with pytest.raises(FrozenInstanceError):
            exposed.element.kind = "STRING"
        # Catalog observations may be memoized. Even forcibly bypassing their
        # frozen contract must never alter the independent live schema authority.
        object.__setattr__(exposed.element, "kind", "STRING")
        assert db._catalog.catalog.table("N").columns[1].stored_type.element.kind == "INT64"
        # Public plan rebuilding must include the entire descriptor in its closed grammar.
        assert result.plan is not None
        public_plan = result.plan
        object.__setattr__(public_plan.columns[1].stored_type.element, "kind", "BOOL")
        # A result memoizes its own materialized plan; different results/plans
        # must not share its descriptor or the engine's compiled authority.
        assert independent_plan.columns[1].stored_type.element.kind == "INT64"
        assert db._catalog.catalog.table("N").columns[1].stored_type.element.kind == "INT64"
        with db.begin() as tx:
            assert tx.execute(CREATE, {"id": 1, **INPUT}).rows == (EXPECTED,)
        assert db.execute(READ).rows == (EXPECTED,)
        assert not db.verify("all").findings
    with connect(tmp_path / "db") as reopened:
        assert reopened.catalog.catalog.table("N").columns[1].stored_type.element.kind == "INT64"
        assert reopened.execute(READ).rows == (EXPECTED,)


@pytest.mark.parametrize("declaration,value", [("STRUCT<>", {}), ("ARRAY<INT64,0>", []), ("LIST<MAP<LIST<STRING>>>", [{"x": ["a"]}])])
def test_empty_and_deeply_nested_collection_declarations(tmp_path, declaration, value):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute(f"CREATE NODE TABLE N(v {declaration} NOT NULL)")
            tx.execute("CREATE(:N {v:$v})", {"v": value})
            with pytest.raises(GrafxError):
                tx.execute("CREATE(:N)")
        assert db.execute("MATCH(n:N) RETURN count(n)").rows == ((1,),)
        assert not db.verify("all").findings


def test_scalar_not_null_and_native_collection_capability_prerequisite(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            with pytest.raises(GrafxError):
                tx.execute("CREATE NODE TABLE Bad(v LIST<INT64>)")
            tx.execute("CREATE NODE TABLE Scalar(v STRING NOT NULL)")
            with pytest.raises(GrafxError):
                tx.execute("CREATE(:Scalar)")
        assert not db._catalog.catalog.has_table("Bad")


@pytest.mark.parametrize("operation", ["CREATE INDEX ix ON N(xs)", "CREATE INDEX ix ON N(pair)",
    "CREATE INDEX ix ON N(amounts)", "CREATE NODE TABLE Bad(v LIST<INT64>,PRIMARY KEY(v))"])
def test_collection_indexes_and_primary_keys_refuse_before_effects(tmp_path, operation):
    with connect(tmp_path / "db") as db:
        seed(db, data=False)
        before = db._catalog.catalog.serialize()
        with db.begin() as tx:
            with pytest.raises(GrafxError):
                tx.execute(operation)
        assert db._catalog.catalog.serialize() == before


def test_collection_snapshot_and_conflicting_writers_keep_original_occ(tmp_path):
    with connect(tmp_path / "db") as first:
        seed(first)
        with connect(tmp_path / "db") as second, first.begin("read") as reader:
            assert reader.execute(READ).rows[0] == EXPECTED
            loser = first.begin()
            loser.execute("MATCH(n:N {id:1}) SET n.xs=[5]")
            with second.begin() as winner:
                winner.execute("MATCH(n:N {id:1}) SET n.xs=[6]")
            with pytest.raises(GrafxWriteConflict):
                loser.commit()
            loser.rollback()
            assert reader.execute(READ).rows[0] == EXPECTED
        assert first.execute("MATCH(n:N {id:1}) RETURN n.xs").rows == (((6,),),)
        assert not first.verify("all").findings


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_collection_history_nullable_append_and_transfer_preserve_descriptors(tmp_path, codec):
    with connect(tmp_path / "source", codec=codec) as db:
        seed(db)
        db.enable_commit_history()
        db.enable_system_history(("N", "R"))
        before = db.commit_history().entries[-1].identity
        original = db.system_as_of(before, tables=("N", "R"))
        column = ColumnDef("more", ValueType.LIST, stored_type=StoredType("LIST", element=StoredType("DATE")))
        db.add_nullable_column("N", column)
        with db.begin() as tx:
            tx.execute("MATCH(n:N {id:1}) SET n.more=[$day]", {"day": DateValue(2025)})
        after = CommitId(db.identity.database_uuid, tx.report.csn)
        assert db.system_as_of(before, tables=("N", "R")).schemas == original.schemas
        new = db.system_as_of(after, tables=("N", "R"))
        assert next(t for t in new.schemas if t.name == "N").columns[-1] == column
        export_graph(db, tmp_path / "artifact", history="current-only")
        assert not db.verify("all").findings
    import_graph(tmp_path / "artifact", tmp_path / "target", limits=TransferLimits(batch_rows=1))
    with connect(tmp_path / "target", codec=codec) as target:
        assert target.execute(READ).rows[0] == EXPECTED
        assert target.catalog.catalog.table("N").columns[-1] == column
        assert target.execute("MATCH(n:N {id:1}) RETURN n.more").rows == (((DateValue(2025),),),)
        assert not target.verify("all").findings


def test_collection_copy_matches_full_descriptor_not_only_list_map_tag(tmp_path):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        source.ensure_identity_indexes()
        source.enable_commit_history()
        seed(source)
        seed(target, data=False)
        prepare_copy_target(target)
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=("N", "R"))
        receipt = copy_graph(package, target, idempotency_key="typed-collection-copy")
        assert receipt.rows == 3
        assert copy_graph(package, target, idempotency_key="typed-collection-copy") == replace(receipt, replayed=True)
        assert target.execute(READ).rows[0] == EXPECTED
        assert not target.verify("all").findings


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("cut", ["before_commit", "before_apply"])
def test_collection_ddl_and_data_crash_publish_only_one_proven_outcome(tmp_path, codec, cut):
    with connect(tmp_path / "db", codec=codec) as db:
        db.ensure_identity_indexes()
    worker = Path(__file__).with_name("typed_collection_worker.py")
    outcome = subprocess.run([sys.executable, str(worker), str(tmp_path / "db"), codec, cut],
                             capture_output=True, text=True, timeout=60)
    assert outcome.returncode == (71 if cut == "before_commit" else 73), outcome.stderr
    for _ in range(2):
        with connect(tmp_path / "db", codec=codec) as db:
            assert db._catalog.catalog.has_table("N") == (cut == "before_apply")
            assert db._catalog.catalog.requires_capability("typed_collections_v1") == (cut == "before_apply")
            if cut == "before_apply":
                assert db.execute(READ).rows == (EXPECTED,)
            assert not db.verify("all").findings
