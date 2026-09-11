"""Independent detached graph value oracles, not evaluator-derived expectations."""

from dataclasses import FrozenInstanceError, replace
from collections.abc import Mapping
import json
from types import MappingProxyType

import pytest

from okto_grafx import connect
from okto_grafx.domain.model.value import Timestamp, Uuid, VectorValue
from okto_grafx.domain.query.entity_identity import EntityIdentity, EntityProvenance
from okto_grafx.domain.query.entity_values import NodeValue, PathValue, RelationshipValue
from okto_grafx.errors import GrafxConfigurationError


def node(record=1, *, database=b"d" * 16, read=10, table=1, properties=None):
    return NodeValue(EntityIdentity(database, table, "node", record_id=record), "N",
                     {} if properties is None else properties, EntityProvenance(read, 1, 1))


def relationship(source, target, record=1):
    return RelationshipValue(EntityIdentity(source.identity.database_uuid, 2, "relationship", record_id=record),
                             "R", source.identity, target.identity, {"weight": 3}, source.provenance)


def test_node_observations_compare_by_identity_not_properties_or_provenance():
    before, after = node(properties={"x": 1}), node(read=20, properties={"x": 2})
    assert before == after and hash(before) == hash(after)
    assert before.properties != after.properties
    assert len({before, after, node(2), node(table=3), node(database=b"e" * 16)}) == 4
    assert before.labels == ("N",)
    assert before != 1
    assert before != {"id": 1}
    with pytest.raises(FrozenInstanceError):
        before.label = "Changed"


def test_relationship_observations_compare_by_identity_not_payload():
    a, b = node(), node(2)
    first = relationship(a, b)
    second = replace(first, properties={"weight": 99}, provenance=EntityProvenance(20, 2, 15))
    assert first == second and hash(first) == hash(second)
    assert first != a
    assert first != relationship(a, b, record=2)


def test_deep_property_copy_and_json_cannot_mutate_observation():
    data = {"list": [1, {"bytes": bytearray(b"ab")}], "tag": {"type": "int64", "value": "9"}}
    observed = node(properties=data)
    data["list"][1]["bytes"][0] = 0
    data["list"].append(3)
    assert observed.properties["list"] == (1, {"bytes": b"ab"})
    with pytest.raises(TypeError):
        observed.properties["added"] = True
    with pytest.raises(TypeError):
        observed.properties["list"][1]["bytes"] = b"changed"
    exported = observed.to_dict()
    assert exported["properties"]["tag"] == {"type": "map", "entries": {"type": "int64", "value": "9"}}
    exported["properties"]["list"]["items"].clear()
    assert len(observed.properties["list"]) == 2


def test_json_has_exact_typed_values_and_no_private_capabilities():
    observed = node(properties={"big": 9223372036854775807, "flag": True, "float": 1.25,
                                "ts": Timestamp(-5), "uuid": Uuid(b"a" * 16),
                                "vector": VectorValue((1.0, 2.0), 1, "float64")})
    exported = json.loads(json.dumps(observed.to_dict(), allow_nan=False))
    assert exported["properties"] == {
        "big": {"type": "int64", "value": "9223372036854775807"}, "flag": True, "float": 1.25,
        "ts": {"type": "timestamp", "micros": "-5"}, "uuid": {"type": "uuid", "hex": "61" * 16},
        "vector": {"type": "vector", "dtype": "float64", "space_ref": "1", "components": [1.0, 2.0]},
    }
    assert set(exported) == {"format", "identity", "label", "properties", "provenance"}


@pytest.mark.parametrize("value", [object(), lambda: None, float("nan"), float("inf"), 1 << 63,
                                   {1: "not a property name"}])
def test_invalid_or_capability_properties_refused(value):
    with pytest.raises(GrafxConfigurationError):
        node(properties={"bad": value})


def test_cycles_and_depth_and_list_bound_fail_closed():
    cyclic = []
    cyclic.append(cyclic)
    for value in (cyclic, [None] * 1025):
        with pytest.raises(GrafxConfigurationError):
            node(properties={"bad": value})
    nested = None
    for _ in range(65):
        nested = [nested]
    with pytest.raises(GrafxConfigurationError):
        node(properties={"deep": nested})


def test_exact_container_frontiers_are_admitted_without_silent_truncation():
    assert len(node(properties={"list": [None] * 1024}).properties["list"]) == 1024
    assert len(node(properties={str(i): i for i in range(256)}).properties) == 256
    with pytest.raises(GrafxConfigurationError):
        node(properties={str(i): i for i in range(257)})
    nested = None
    for _ in range(63):
        nested = [nested]
    node(properties={"deep": nested})


def test_host_sequence_subclasses_are_not_invoked():
    class Host(list):
        def __iter__(self):
            pytest.fail("host iterator invoked")
    with pytest.raises(GrafxConfigurationError):
        node(properties={"host": Host()})


def test_mapping_proxy_cannot_bypass_actual_enumeration_bound_with_false_length():
    visited = []

    class Host(Mapping):
        def __len__(self):
            return 0

        def __iter__(self):
            for i in range(10000):
                visited.append(i)
                yield str(i)

        def __getitem__(self, key):
            return 1

    with pytest.raises(GrafxConfigurationError):
        node(properties={"host": MappingProxyType(Host())})
    assert visited == list(range(257))


def test_zero_reverse_and_cyclic_paths_have_exact_order_and_identity():
    a, b = node(), node(2)
    edge = relationship(a, b)
    zero = PathValue((a,), ())
    assert len(zero) == 0 and zero.to_dict()["relationships"] == []
    forward, reverse = PathValue((a, b), (edge,)), PathValue((b, a), (edge,))
    assert forward != reverse and len({forward, reverse}) == 2
    back = relationship(b, a, 2)
    assert len(PathValue((a, b, a), (edge, back))) == 2
    assert forward == PathValue((node(read=20), node(2, read=20)),
                                (relationship(node(read=20), node(2, read=20)),))
    nodes, edges = [a, b], [edge]
    detached = PathValue(nodes, edges)
    nodes.clear()
    edges.clear()
    assert detached == forward
    payload = detached.to_dict()
    assert payload["relationships"][0]["source"] == payload["nodes"][0]["identity"]
    assert payload["relationships"][0]["target"] == payload["nodes"][1]["identity"]


def test_paths_refuse_empty_disconnected_foreign_or_mixed_snapshot_observations():
    a, b, c = node(), node(2), node(3)
    edge = relationship(a, b)
    for nodes, edges in [((), ()), ((a, b), ()), ((a, c), (edge,)),
                         ((a, node(2, read=11)), (edge,)),
                         ((node(database=b"e" * 16), b), (edge,))]:
        with pytest.raises(GrafxConfigurationError):
            PathValue(nodes, edges)


def test_entity_kinds_and_pending_endpoints_must_be_coherent():
    a, b = node(), node(2)
    edge = relationship(a, b)
    with pytest.raises(GrafxConfigurationError):
        NodeValue(edge.identity, "N", {}, a.provenance)
    with pytest.raises(GrafxConfigurationError):
        replace(edge, source=node(database=b"f" * 16).identity, properties={})
    pending = EntityIdentity(b"d" * 16, 1, "node", provisional_id=b"p" * 16)
    with pytest.raises(GrafxConfigurationError):
        NodeValue(pending, "N", {}, a.provenance)
    with pytest.raises(GrafxConfigurationError):
        replace(edge, target=pending, properties={})
    assert NodeValue(pending, "N", {}, EntityProvenance(10, 1, None, pending=True)).identity == pending


def test_native_snapshots_materialize_into_detached_values_that_outlive_database(tmp_path):
    # Consume actual native entity results and independent commit receipts.
    with connect(tmp_path / "db") as db:
        tx = db.begin("write")
        tx.execute("CREATE NODE TABLE N(id INT64, value INT64, PRIMARY KEY(id))")
        tx.execute("CREATE (:N {id:1,value:7})")
        created = tx.commit()
        with db.begin("read") as old:
            before = old.execute("MATCH (n:N) RETURN n,n.value").rows[0]
            old_value = before[0]
            assert old_value.provenance == EntityProvenance(old.snapshot.read_lsn, 1, created.csn)
            tx = db.begin("write")
            tx.execute("MATCH (n:N) SET n.value=8")
            updated = tx.commit()
            assert old.execute("MATCH (n:N) RETURN n,n.value").rows == (before,)
            with db.begin("read") as latest:
                after = latest.execute("MATCH (n:N) RETURN n,n.value").rows[0]
                new_value = after[0]
                assert new_value.provenance == EntityProvenance(latest.snapshot.read_lsn, 1, updated.csn)
    assert old_value == new_value
    assert old_value.properties == {"id": 1, "value": 7}
    assert new_value.properties == {"id": 1, "value": 8}
    assert json.loads(json.dumps(old_value.to_dict()))["properties"]["value"]["value"] == "7"


@pytest.mark.parametrize("kind", ["node", "relationship", "path"])
def test_detached_values_cannot_be_parameters_that_acquire_write_authority(kind):
    a, b = node(), node(2)
    edge = relationship(a, b)
    value = {"node": a, "relationship": edge, "path": PathValue((a, b), (edge,))}[kind]
    with connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            with pytest.raises(GrafxConfigurationError):
                tx.execute("CREATE (:N {id:1}) RETURN $value", {"value": value})
            assert tx.execute("MATCH (n:N) RETURN count(*) AS count").rows == ((0,),)
