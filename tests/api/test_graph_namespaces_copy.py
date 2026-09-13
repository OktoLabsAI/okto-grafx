"""Copy both kinds with one spelling, remap endpoints and publish one receipt."""

from dataclasses import replace

import pytest

from okto_grafx import connect
from okto_grafx.catalog_copy import CopyLimits, capture_copy, copy_graph, prepare_copy_target, _digest
from okto_grafx.errors import GrafxError


def schema(db, *, grouped, flexible, pad=False):
    db.maintenance.ensure_identity_indexes()
    with db.begin("write") as tx:
        if pad:
            tx.execute("CREATE NODE TABLE Pad(id INT64)")
        if flexible:
            tx.execute("CREATE(a:R {v:'seed'})-[:R {v:'seed'}]->(a)")
            tx.execute("MATCH(n:R) DETACH DELETE n")
        else:
            tx.execute("CREATE NODE TABLE R(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE " + ("GROUP " if grouped else "") + "R(FROM R TO R, v STRING)")


@pytest.mark.parametrize("grouped,flexible", [(False,False),(True,False),(True,True)])
def test_complete_copy_and_retry_preserve_siblings_and_snapshots(tmp_path, grouped, flexible):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        schema(source, grouped=grouped, flexible=flexible)
        schema(target, grouped=grouped, flexible=flexible, pad=True)
        source.enable_commit_history()
        prepare_copy_target(target)
        with source.begin("write") as tx:
            tx.execute("UNWIND [1,2] AS i CREATE(a:R {" + ("v:i" if flexible else "id:i") + "})-[:R {v:'edge'}]->(a)")
        selections = tuple((t.kind,t.name) for t in source.catalog.catalog.tables())
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=selections)
        expected = source.execute("MATCH(n:R)-[r:R]->(m:R) RETURN properties(n),r.v,properties(m)").rows
        with target.begin("read") as reader:
            receipt = copy_graph(package, target, idempotency_key="names")
            assert receipt.rows == 4
            assert reader.execute("MATCH(n:R) RETURN count(n)").rows == ((0,),)
        assert copy_graph(package, target, idempotency_key="names") == replace(receipt,replayed=True)
        assert target.execute("MATCH(n:R)-[r:R]->(m:R) RETURN properties(n),r.v,properties(m)").rows == expected
        assert target.verify("all").findings == ()
    with connect(tmp_path / "target") as target:
        assert copy_graph(package,target,idempotency_key="names").replayed
        assert target.execute("MATCH(n:R)-[r:R]->() RETURN count(r)").rows == ((2,),)
        assert target.verify("all").findings == ()


def test_subset_endpoint_closure_uses_node_namespace_not_same_named_edge(tmp_path):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        schema(source, grouped=False, flexible=False)
        schema(target, grouped=False, flexible=False)
        source.enable_commit_history()
        prepare_copy_target(target)
        with source.begin("write") as tx:
            tx.execute("CREATE(a:R {id:1})-[:R {v:'edge'}]->(b:R {id:2})")
        with source.begin("read") as tx:
            rid = tx.scan_rows_v1("R",kind="rel",limit=1).rows[0].record_id
            package = capture_copy(tx,tables=(("rel","R"),),record_ids={("rel","R"):(rid,)},include_endpoints=True)
            with pytest.raises(GrafxError) as error:
                capture_copy(tx,tables=("R",))
            assert error.value.details["reason"] == "ambiguous_table_name"
        assert sorted((t.schema.kind,len(t.rows)) for t in package.tables) == [("node",2),("rel",1)]
        assert copy_graph(package,target,idempotency_key="closure").rows == 3
        assert target.execute("MATCH(n:R)-[r:R]->(m:R) RETURN n.id,m.id,r.v").rows == ((1,2,"edge"),)


@pytest.mark.parametrize("selection", [("node",), ("relationship","R"), (False,"R"), ["node","R"], ("node", "")])
def test_invalid_selector_refuses(tmp_path, selection):
    with connect(tmp_path / "db") as db, db.begin("read") as tx:
        with pytest.raises(GrafxError):
            capture_copy(tx,tables=(selection,))


def test_duplicate_resolved_selection_and_rechecksummed_same_kind_refuse(tmp_path):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        schema(source, grouped=True, flexible=False)
        schema(target, grouped=True, flexible=False)
        source.enable_commit_history()
        prepare_copy_target(target)
        with source.begin("write") as tx:
            tx.execute("CREATE(a:R {id:1})-[:R {v:'edge'}]->(a)")
        with source.begin("read") as tx:
            with pytest.raises(GrafxError):
                capture_copy(tx,tables=("R",("node","R")))
            package = capture_copy(tx,tables=tuple((t.kind,t.name) for t in source.catalog.catalog.tables()))
        duplicated = (*package.tables, replace(package.tables[0],schema=replace(package.tables[0].schema,table_id=99)))
        forged = replace(package,tables=duplicated,sha256=_digest(package.source_commit,duplicated,package.spaces,CopyLimits()))
        with pytest.raises(GrafxError):
            copy_graph(forged,target,idempotency_key="bad")
        assert target.execute("MATCH(n:R) RETURN count(n)").rows == ((0,),)
        assert target.verify("all").findings == ()
