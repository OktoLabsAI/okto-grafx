"""Native procedure entities preserve identity without trusting detached metadata."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from okto_grafx import connect, NodeValue, RelationshipValue, PathValue
from okto_grafx.errors import GrafxPlanError, GrafxQueryBudgetExceeded
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure


def registry(kind, callback=None, **options):
    """Declare one entity-aware callback without exposing a database handle."""
    return ExtensionRegistry(trusted=True, procedures=(TabularProcedure(
        "app.echo", (kind,), (("value", kind),), callback or (lambda value: ((value,),)), **options,
    ),), procedure_permissions=frozenset({"mutate"}))


def seed(db):
    """Populate two native nodes and an edge with typed persistent properties."""
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE T (id INT64, value INT64, PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE R (FROM T TO T, value INT64)")
        tx.execute("CREATE (a:T {id:1,value:10}), (b:T {id:2,value:20}), (a)-[:R {value:30}]->(b)")


@pytest.mark.parametrize("kind,expression,projection,expected,klass", (
    ("NODE", "a", "value.id, value.value", (1,10), NodeValue),
    ("RELATIONSHIP", "r", "value.value, type(value)", (30,"R"), RelationshipValue),
    ("PATH", "p", "length(value), size(nodes(value)), size(relationships(value))", (1,2,1), PathValue),
))
def test_native_entity_callback_and_downstream_identity(kind, expression, projection, expected, klass):
    seen = []
    def callback(value):
        seen.append(value)
        return ((value,),)
    with connect(":memory:", extensions=registry(kind, callback)) as db:
        seed(db)
        query = f"MATCH p=(a:T)-[r:R]->(b:T) CALL app.echo({expression}) YIELD value "
        assert db.execute(query + f"RETURN {projection}").rows == (expected,)
        assert type(seen[-1]) is klass
        assert db.execute(query + f"RETURN value = {expression}").rows == ((True,),)
        result = db.execute(query + "RETURN value").rows[0][0]
        assert type(result) is klass
        assert db.execute("CALL app.echo(null)").rows == ((None,),)


@pytest.mark.parametrize("kind,expression", (("LIST<NODE>", "[a,b]"), ("LIST<RELATIONSHIP>", "[r]")))
def test_typed_entity_lists_unwind_into_native_mutation(kind, expression):
    with connect(":memory:", extensions=registry(kind)) as db:
        seed(db)
        with db.begin("write") as tx:
            result = tx.execute(f"MATCH (a:T)-[r:R]->(b:T) CALL app.echo({expression}) YIELD value "
                                "UNWIND value AS item SET item.value=99 RETURN item.value")
            assert result.rows == (((99,), (99,)) if kind == "LIST<NODE>" else ((99,),))


@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_uncommitted_node_output_can_be_written_and_reopened(tmp_path, codec):
    path = tmp_path / "graph"
    seen = []
    def callback(value):
        seen.append(value)
        return ((value,),)
    with connect(path, codec=codec, extensions=registry("NODE", callback)) as db:
        seed(db)
        with db.begin("write") as tx:
            assert tx.execute("CREATE (n:T {id:3,value:30}) WITH n CALL app.echo(n) YIELD value "
                              "SET value.value=31 RETURN value.id, value.value").rows == ((3,31),)
        assert seen[-1].provenance.pending
        db.verify()
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH (n:T {id:3}) RETURN n.value").rows == ((31,),)
        db.verify()


@pytest.mark.parametrize("kind,expression", (("ANY","a"), ("LIST","[a,r,p]"), ("MAP","{node:a,path:p}")))
def test_entities_inside_generic_native_containers(kind, expression):
    with connect(":memory:", extensions=registry(kind)) as db:
        seed(db)
        result = db.execute(f"MATCH p=(a:T)-[r:R]->(b:T) CALL app.echo({expression}) YIELD value RETURN value")
        assert len(result.rows) == 1
        value = result.rows[0][0]
        if kind == "ANY":
            assert type(value) is NodeValue
        elif kind == "LIST":
            assert tuple(map(type, value)) == (NodeValue, RelationshipValue, PathValue)
        else:
            assert type(value["node"]) is NodeValue and type(value["path"]) is PathValue


def test_forged_copy_and_previous_invocation_are_refused_without_rolling_back_prior_statement():
    retained = []
    def callback(value):
        if not retained:
            retained.append(value)
            return ((replace(value),),)
        return ((retained[0],),)
    with connect(":memory:", extensions=registry("NODE", callback)) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:3,value:30})")
            for _ in range(2):
                with pytest.raises(GrafxPlanError, match="authority"):
                    tx.execute("CREATE (n:T {id:4,value:40}) WITH n CALL app.echo(n) YIELD value RETURN value")
                assert tx.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (2,), (3,))


def test_direct_invoke_does_not_treat_public_entity_as_native_authority():
    with connect(":memory:") as db:
        seed(db)
        entity = db.execute("MATCH (n:T {id:1}) RETURN n").rows[0][0]
    procedure = registry("NODE").procedures[0]
    with pytest.raises(GrafxPlanError):
        tuple(procedure.invoke((entity,)))


def test_path_components_are_valid_output_witnesses():
    procedure = TabularProcedure("app.parts", ("PATH",), (("node","NODE"),("edge","RELATIONSHIP")),
                                 lambda path: ((path.nodes[0], path.relationships[0]),))
    with connect(":memory:", extensions=ExtensionRegistry(trusted=True, procedures=(procedure,))) as db:
        seed(db)
        assert db.execute("MATCH p=(a:T)-[r:R]->(b:T) CALL app.parts(p) YIELD node,edge "
                          "RETURN node=a, edge=r, node.value, edge.value").rows == ((True,True,10,30),)


@pytest.mark.parametrize("expression", ("n", "[n]", "{node:n}"))
def test_entity_nested_property_maps_and_unlabeled_dynamic_storage(expression):
    with connect(":memory:", extensions=registry("ANY")) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE ({payload:{nested:[{id:1}, {id:'x'}]}})")
        assert len(db.execute(f"MATCH (n) CALL app.echo({expression}) YIELD value RETURN value").rows) == 1


def test_modified_observation_is_not_write_authority():
    def callback(value):
        object.__setattr__(value, "properties", {"id":2,"value":999})
        return ((value,),)
    with connect(":memory:", extensions=registry("NODE", callback)) as db:
        seed(db)
        with db.begin("write") as tx:
            assert tx.execute("MATCH (n:T {id:1}) CALL app.echo(n) YIELD value "
                              "SET value.value=11 RETURN value.id, value.value").rows == ((1,11),)
        assert db.execute("MATCH (n:T) RETURN n.value ORDER BY n.id").rows == ((11,), (20,))


def test_entity_output_refreshes_after_scoped_callback_write():
    def callback(writer, value):
        writer.execute("MATCH (n:T {id:$id}) SET n.value=77", {"id":value.properties["id"]})
        return ((value,),)
    with connect(":memory:", extensions=registry("NODE", callback, mode="write",
                                                 required_permissions=frozenset({"mutate"}))) as db:
        seed(db)
        with db.begin("write") as tx:
            assert tx.execute("MATCH (n:T {id:1}) CALL app.echo(n) YIELD value "
                              "RETURN value.value, n.value").rows == ((77,77),)


def test_late_invalid_unselected_entity_output_rolls_back_callback_writes():
    def callback(writer, value):
        writer.execute("MATCH (n:T {id:1}) SET n.value=77")
        yield (value, 1)
        yield (replace(value), 2)
    procedure = TabularProcedure("app.change", ("NODE",), (("entity","NODE"),("number","INT64")), callback,
                                 mode="write", required_permissions=frozenset({"mutate"}))
    extensions = ExtensionRegistry(trusted=True, procedures=(procedure,), procedure_permissions=frozenset({"mutate"}))
    with connect(":memory:", extensions=extensions) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:3,value:30})")
            with pytest.raises(GrafxPlanError):
                tx.execute("MATCH (n:T {id:1}) CALL app.change(n) YIELD number RETURN number LIMIT 1")
            assert tx.execute("MATCH (n:T) RETURN n.value ORDER BY n.id").rows == ((10,), (20,), (30,))


@pytest.mark.parametrize("limit", (1, 255, 300))
def test_entity_byte_budget_precedes_callback(limit):
    seen = []
    def callback(value):
        seen.append(value)
        return ((value,),)
    with connect(":memory:", extensions=registry("NODE", callback, max_value_bytes=limit)) as db:
        seed(db)
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.execute("MATCH (n:T) CALL app.echo(n) YIELD value RETURN value")
        assert seen == []


def test_entities_survive_union_subquery_and_cursor_boundaries():
    with connect(":memory:", extensions=registry("NODE")) as db:
        seed(db)
        query = "MATCH (n:T {id:1}) CALL app.echo(n) YIELD value RETURN value "
        assert len(db.execute(query + "UNION " + query).rows) == 1
        assert len(db.execute(query + "UNION ALL " + query).rows) == 2
        assert db.execute("MATCH (n:T) CALL (n) { CALL app.echo(n) YIELD value RETURN value } "
                          "RETURN value.id ORDER BY value.id").rows == ((1,), (2,))
        with db.query(query).cursor(batch_size=1) as cursor:
            assert type(tuple(cursor)[0][0]) is NodeValue


def test_waiting_entity_callback_does_not_serialize_independent_reader_writer(tmp_path):
    entered, resume = Event(), Event()
    def callback(value):
        entered.set()
        assert resume.wait(30)
        return ((value,),)
    path = tmp_path / "concurrent"
    with connect(path, extensions=registry("NODE", callback)) as slow:
        seed(slow)
        with connect(path) as writer, connect(path) as reader, ThreadPoolExecutor(max_workers=1) as executor:
            task = executor.submit(slow.execute, "MATCH (n:T {id:1}) CALL app.echo(n) YIELD value RETURN value.value")
            try:
                assert entered.wait(30)
                assert reader.execute("MATCH (n:T {id:1}) RETURN n.value").rows == ((10,),)
                with writer.begin("write") as tx:
                    tx.execute("MATCH (n:T {id:1}) SET n.value=12")
                assert reader.execute("MATCH (n:T {id:1}) RETURN n.value").rows == ((12,),)
            finally:
                resume.set()
            assert task.result(timeout=30).rows == ((10,),)


def test_foreign_connection_entity_with_same_local_record_id_is_not_authorized():
    foreign = []
    with connect(":memory:") as other:
        seed(other)
        foreign.append(other.execute("MATCH (n:T {id:1}) RETURN n").rows[0][0])
    with connect(":memory:", extensions=registry("NODE", lambda value: ((foreign[0],),))) as db:
        seed(db)
        with pytest.raises(GrafxPlanError):
            db.execute("MATCH (n:T {id:1}) CALL app.echo(n) YIELD value RETURN value")


def test_deleted_callback_output_refuses_content_and_rolls_back_delete():
    def callback(writer, value):
        writer.execute("MATCH (n:T {id:1}) DETACH DELETE n")
        return ((value,),)
    with connect(":memory:", extensions=registry("NODE", callback, mode="write",
                                                 required_permissions=frozenset({"mutate"}))) as db:
        seed(db)
        with db.begin("write") as tx:
            with pytest.raises(GrafxPlanError):
                tx.execute("MATCH (n:T {id:1}) CALL app.echo(n) YIELD value RETURN value")
            assert tx.execute("MATCH (n:T) RETURN count(n)").rows == ((2,),)
            assert tx.execute("MATCH ()-[r:R]->() RETURN count(r)").rows == ((1,),)


def test_unit_callback_deletion_invalidates_outer_alias_and_pending_reference():
    def callback(writer):
        writer.execute("MATCH (n:T {id:3}) DELETE n")
    procedure = TabularProcedure("app.remove", (), (), callback, mode="write",
                                 required_permissions=frozenset({"mutate"}))
    extensions = ExtensionRegistry(trusted=True, procedures=(procedure,), procedure_permissions=frozenset({"mutate"}))
    with connect(":memory:", extensions=extensions) as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id:3,value:30})")
            with pytest.raises(GrafxPlanError) as failure:
                tx.execute("MATCH (n:T {id:3}) CALL app.remove() RETURN n.value")
            assert failure.value.details["reason"] == "deleted_entity_access"
            assert tx.execute("MATCH (n:T {id:3}) RETURN n.value").rows == ((30,),)
