"""Native property enumeration, dynamic lookup and atomic REMOVE composition."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError


@pytest.fixture(params=[False, True], ids=["flexible", "typed"])
def db(request, tmp_path):
    with connect(tmp_path / "db") as database:
        with database.begin("write") as tx:
            if request.param:
                tx.execute("CREATE NODE TABLE N(id INT64, value INT64, note STRING, PRIMARY KEY(id))")
                tx.execute("CREATE REL TABLE R(FROM N TO N, value INT64)")
            tx.execute("CREATE(a:N {id:1,value:7}), (b:N {id:2}), (a)-[:R {value:9}]->(b)")
        yield database


def test_keys_and_dynamic_properties_of_native_nodes_and_relationships(db):
    row = db.execute("MATCH p=(n:N {id:1})-[r:R]->(m:N) "
                     "RETURN keys(n),keys(r),n[$key],r['val'+'ue'],nodes(p)[1]['id']", {"key": "value"}).rows[0]
    assert set(row[0]) == {"id", "value"}
    assert row[1:] == (("value",), 7, 9, 2)
    assert db.execute("MATCH(n:N {id:1}) RETURN n['missing'],n[null]").rows == ((None, None),)


def test_keys_map_null_and_lazy_invalid_arm(db):
    assert db.execute("RETURN keys({a:null,b:2}),keys(null),CASE WHEN true THEN 1 ELSE keys($x) END",
                      {"x":17}).rows == (
        (("a", "b"), None, 1),
    )


def test_remove_updates_owner_paths_but_not_independent_reader(db):
    with db.begin("read") as reader, db.begin("write") as writer:
        query = "MATCH(n:N {id:1}) RETURN n['value']"
        assert reader.execute(query).rows == ((7,),)
        row = writer.execute("MATCH p=(n:N {id:1})-[r:R]->(m:N) REMOVE n.value,r.value "
                             "RETURN keys(n),keys(r),n['value'],relationships(p)[0]['value']").rows[0]
        assert row == (("id",), (), None, None)
        assert reader.execute(query).rows == ((7,),)
    assert db.execute(query).rows == ((None,),)
    assert db.verify("all").findings == ()


@pytest.mark.parametrize("suffix", ["RETURN n LIMIT 0", "RETURN n SKIP 10", "WITH n WHERE false RETURN n"])
def test_discarded_output_does_not_discard_required_removals(db, suffix):
    with db.begin("write") as tx:
        assert tx.execute("MATCH(n:N) REMOVE n.value " + suffix).rows == ()
    assert db.execute("MATCH(n:N) RETURN n.value").rows == ((None,), (None,))


def test_late_error_rolls_back_removal_keeps_prior_statement(db):
    with db.begin("write") as tx:
        tx.execute("MATCH(n:N {id:1}) SET n.note='prior'")
        with pytest.raises(GrafxError):
            tx.execute("MATCH(n:N {id:1}) REMOVE n.value RETURN n[1]")
        assert tx.execute("MATCH(n:N {id:1}) RETURN n.value,n.note").rows == ((7,"prior"),)


def test_nullable_optional_target_and_missing_properties_are_noops(db):
    with db.begin("write") as tx:
        assert tx.execute("OPTIONAL MATCH(n:Missing) REMOVE n.value RETURN keys(n),n[$key]",
                          {"key":"value"}).rows == ((None,None),)
        # This column exists but is NULL on both nodes in explicit typed schemas.
        tx.execute("MATCH(n:N) REMOVE n.note")
    assert db.execute("MATCH(n:N) RETURN n.value ORDER BY n.id").rows == ((7,),(None,))


def test_remove_nested_in_union_call_is_write_even_with_zero_input(db):
    query = "CALL(){MATCH(n:N) REMOVE n.value RETURN n UNION ALL MATCH(n:N) RETURN n} RETURN n LIMIT 0"
    with db.begin("read") as reader, pytest.raises(GrafxError):
        reader.execute(query)
    with pytest.raises(GrafxError):
        with db.query(query).cursor(batch_size=1) as cursor:
            cursor.fetchmany()
    with db.begin("write") as tx:
        assert tx.execute(query).rows == ()
    assert db.execute("MATCH(n:N) RETURN n.value").rows == ((None,),(None,))


def test_keys_and_lookup_through_collected_entities_see_own_update(db):
    with db.begin("write") as tx:
        assert tx.execute("MATCH(n:N {id:1}) WITH collect(n) AS xs UNWIND xs AS n "
                          "SET n.value=12 RETURN xs[0]['value'],keys(xs[0])").rows == ((12,("id","value")),)


def test_dynamic_property_names_are_case_sensitive_and_not_entity_metadata(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(n {Name:'upper',name:'lower',_ID:'user'})")
            row = tx.execute("MATCH(n) REMOVE n.Name,n.missing "
                             "RETURN n['Name'],n['name'],n['_ID'],keys(n)").rows[0]
            assert row[:3] == (None,"lower","user")
            assert set(row[3]) == {"_ID","name"}
