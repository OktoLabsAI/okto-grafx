"""Whole-property assignment keeps native identity, constraints and rollback."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


def write(db, query, parameters=None):
    with db.begin("write") as tx:
        return tx.execute(query, parameters)


@pytest.mark.parametrize("kind", ["node", "rel"])
@pytest.mark.parametrize("operator", ["=", "+="])
def test_replace_merge_remove_and_identity(tmp_path, kind, operator):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(a:N {a:1,b:2}),(a)-[:R {a:1,b:2}]->(a)")
        match = "MATCH(n:N)" if kind == "node" else "MATCH()-[n:R]->()"
        before = db.execute(match+" RETURN n").rows[0][0]
        result = write(db,match+f" SET n {operator} $props RETURN n,properties(n)",
                       {"props":{"a":None,"c":3}}).rows[0]
        assert dict(result[1]) == ({"c":3} if operator == "=" else {"b":2,"c":3})
        assert result[0].identity == before.identity
        if kind == "rel":
            assert result[0].source == before.source and result[0].target == before.target
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("source_kind", ["node", "rel"])
@pytest.mark.parametrize("operator", ["=", "+="])
def test_copy_native_entity_properties_and_owner_updates(tmp_path, source_kind, operator):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(a:A {v:7}),(b:B {old:1}),(a)-[:R {v:7}]->(b)")
        source = "a" if source_kind == "node" else "r"
        result = write(db,"MATCH(a:A)-[r:R]->(b:B) SET "+source+".v=8 SET b "+operator+" "+source+" RETURN properties(b)")
        assert dict(result.rows[0][0]) == ({"v":8} if operator == "=" else {"v":8,"old":1})


def test_ordered_mixed_map_and_property_assignments_alias_one_entity(tmp_path):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(:N {id:1,old:0})")
        result = write(db,"MATCH(n:N),(m:N) SET n += {a:1},m = {b:2},n.c=3 RETURN properties(n),properties(m)")
        assert result.rows == (({"b":2,"c":3},{"b":2,"c":3}),)
        assert write(db,"MATCH(n:N) SET n = properties(n) RETURN properties(n)").rows == (({"b":2,"c":3},),)


@pytest.mark.parametrize("operator", ["=", "+="])
def test_null_target_skips_runtime_rhs_but_null_map_refuses(tmp_path, operator):
    with connect(tmp_path / "db") as db:
        assert write(db,f"OPTIONAL MATCH(n:Absent) SET n {operator} {{v:1/0}} RETURN n").rows == ((None,),)
        write(db,"CREATE(:N {v:1})")
        with pytest.raises(GrafxError) as failure:
            write(db,f"MATCH(n:N) SET n {operator} null")
        assert failure.value.details["reason"] == "set_map_type"
        assert db.execute("MATCH(n:N) RETURN n.v").rows == ((1,),)


@pytest.mark.parametrize("value", [1, "bad", [1], {"v":float("nan")}, {"v":{"deep":float("inf")}}])
def test_bad_map_or_stored_value_has_no_effect(tmp_path, value):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(:N {v:1})")
        with pytest.raises(GrafxError):
            write(db,"MATCH(n:N) SET n = $map", {"map":value})
        assert db.execute("MATCH(n:N) RETURN properties(n)").rows == (({"v":1},),)


def test_typed_replacement_constraints_and_unknown_fields(tmp_path):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE NODE TABLE N(id INT64,v STRING,PRIMARY KEY(id))")
        write(db,"CREATE(:N {id:1,v:'before'})")
        for value in ({"v":"missing PK"},{"id":1,"unknown":2},{"id":1,"v":2}):
            with pytest.raises(GrafxError):
                write(db,"MATCH(n:N) SET n = $m", {"m":value})
            assert db.execute("MATCH(n:N) RETURN n.id,n.v").rows == ((1,"before"),)
        assert write(db,"MATCH(n:N) SET n = {id:1} RETURN properties(n)").rows == (({"id":1},),)
        assert db.verify("all").findings == ()


def test_map_actions_nested_calls_drain_discarded_results(tmp_path):
    with connect(tmp_path / "db") as db:
        result = write(db,"UNWIND [1,1,2] AS i CALL(i){ MERGE(n:N {id:i}) ON CREATE SET n += {created:true} ON MATCH SET n += {matched:true} RETURN n } RETURN n LIMIT 0")
        assert result.rows == ()
        assert db.execute("MATCH(n:N) RETURN n.id,n.created,n.matched ORDER BY n.id").rows == ((1,True,True),(2,True,None))


def test_late_failure_removes_all_invocations_and_keeps_previous_statement(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Keep {v:9})")
            with pytest.raises(GrafxError):
                tx.execute("UNWIND [1,2] AS i MERGE(n:N {id:i}) ON CREATE SET n = {id:i,v:1/(2-i)} RETURN n")
            assert tx.execute("MATCH(n) RETURN properties(n)").rows == (({"v":9},),)
        assert db.verify("all").findings == ()


def test_map_parameter_ownership_and_read_snapshot(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        write(db,"CREATE(:N {id:1})")
        with db.begin("read") as reader, connect(path) as writer:
            props = {"nested":{"a":[1,2]}}
            with writer.begin("write") as tx:
                tx.execute("MATCH(n:N) SET n += $props", {"props":props})
                props["nested"]["a"].append(3)
            assert reader.execute("MATCH(n:N) RETURN n.nested").rows == ((None,),)
        assert db.execute("MATCH(n:N) RETURN n.nested").rows == (({"a":(1,2)},),)


def test_empty_merge_map_does_not_stage_update(tmp_path):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(:N {v:1})")
        result = write(db,"MATCH(n:N) SET n += {} RETURN n.v")
        assert result.rows == ((1,),)
        assert result.statistics.get("rows_updated",0) == 0


@pytest.mark.parametrize("operator", ["=", "+="])
def test_map_assignment_refuses_read_only_and_conflicting_writers(tmp_path, operator):
    from okto_grafx.errors import GrafxWriteConflict

    path = tmp_path / "db"
    with connect(path) as left, connect(path) as right:
        write(left,"CREATE(:N {id:1,v:0})")
        query = f"MATCH(n:N) SET n {operator} {{id:1,v:n.v+1}} RETURN n.v"
        with left.begin("read") as reader:
            with pytest.raises(GrafxError):
                reader.execute(query)
        first, second = left.begin("write"), right.begin("write")
        try:
            for tx in (first,second):
                assert tx.execute(query).rows == ((1,),)
            first.commit()
            with pytest.raises(GrafxWriteConflict):
                second.commit()
        finally:
            if first.active:
                first.rollback()
            if second.active:
                second.rollback()
        assert left.execute("MATCH(n:N) RETURN n.id,n.v").rows == ((1,1),)
        assert left.verify("all").findings == ()


def test_map_primary_key_collision_restores_entire_statement(tmp_path):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        write(db,"CREATE NODE TABLE N(id INT64,v STRING,PRIMARY KEY(id))")
        write(db,"CREATE(:N {id:1,v:'one'}),(:N {id:2,v:'two'})")
        with db.begin("write") as tx:
            tx.execute("CREATE(:Keep {v:9})")
            with pytest.raises(GrafxError):
                tx.execute("MATCH(n:N) SET n={id:1,v:'collision'}")
            assert tx.execute("MATCH(n:N) RETURN n.id,n.v ORDER BY n.id").rows == ((1,"one"),(2,"two"))
            assert tx.execute("MATCH(n:Keep) RETURN n.v").rows == ((9,),)
        assert db.verify("all").findings == ()


def test_map_and_scalar_rhs_use_clause_input_values(tmp_path):
    with connect(tmp_path / "db") as db:
        write(db,"CREATE(:N {v:1})")
        assert write(db,"MATCH(n:N) SET n += {v:2},n.copy=n.v RETURN n.v,n.copy").rows == ((2,1),)
        assert write(db,"MATCH(n:N) SET n += {v:3} SET n.copy=n.v RETURN n.v,n.copy").rows == ((3,3),)
