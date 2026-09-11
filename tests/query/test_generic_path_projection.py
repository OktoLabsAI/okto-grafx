"""Schema-generic one-hop paths preserve the established public value and gates."""
import pytest

import okto_grafx
from okto_grafx.domain.query import parse, analyze
from okto_grafx.domain.query.analysis import exact_path_projection
from okto_grafx.errors import GrafxPlanError


@pytest.mark.parametrize("source,target,relation,path,left,edge,right", [
    ("Person", "Person", "Knows", "journey", "person", "knows", "friend"),
    ("Person", "Topic", "InterestedIn", "walk", "x", "link", "y"),
    ("Decision", "Decision", "supersedes__Decision__Decision", "path", "a", "r", "b"),
])
def test_identifiers_are_caller_defined_and_endpoints_remain_correlated(
    tmp_path, source, target, relation, path, left, edge, right,
):
    with okto_grafx.connect(tmp_path / "generic-path") as db:
        with db.begin("write") as tx:
            for label in sorted({source, target}):
                tx.execute(f"CREATE NODE TABLE {label}(id STRING, PRIMARY KEY(id))")
            tx.execute(f"CREATE REL TABLE {relation}(FROM {source} TO {target}, reason STRING)")
            tx.execute(f"CREATE (:{source} {{id:'source'}})")
            tx.execute(f"CREATE (:{target} {{id:'target'}})")
            tx.execute(f"MATCH (a:{source} {{id:'source'}}), (b:{target} {{id:'target'}}) "
                       f"CREATE (a)-[:{relation} {{reason:'evidence'}}]->(b)")
        query = f"MATCH {path} = ({left}:{source})-[{edge}:{relation}]->({right}:{target}) RETURN {path} LIMIT 1"
        statement = parse(query)
        assert exact_path_projection(statement) is not None
        assert parse(statement.describe()) == statement
        result = db.execute(query)
        assert result.columns == (path,)
        assert len(result.rows) == 1
        value = result.rows[0][0]
        assert type(value) is okto_grafx.PathValue
        assert [(node.label, node.properties["id"]) for node in value.nodes] == [
            (source, "source"), (target, "target"),
        ]
        relationship = value.relationships[0]
        assert relationship.label == relation
        assert relationship.properties["reason"] == "evidence"
        assert relationship.source == value.nodes[0].identity
        assert relationship.target == value.nodes[1].identity


def test_wrong_declared_target_is_refused_even_with_arbitrary_identifiers(tmp_path):
    with okto_grafx.connect(tmp_path / "wrong-endpoint") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Person(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE Topic(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE Links(FROM Person TO Topic)")
        with pytest.raises(GrafxPlanError, match="endpoint labels"):
            db.execute("MATCH journey = (x:Person)-[edge:Links]->(y:Person) RETURN journey")


def test_target_properties_no_longer_share_namespace_with_path_metadata(tmp_path):
    with okto_grafx.connect(tmp_path / "reserved-target") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Person(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE Topic(id STRING, _ID STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE Links(FROM Person TO Topic)")
        assert db.execute("MATCH journey = (x:Person)-[edge:Links]->(y:Topic) RETURN journey").rows == ()


@pytest.mark.parametrize("query", [
    "MATCH journey = (journey:Person)-[edge:Knows]->(y:Person) RETURN journey",
])
def test_generic_names_do_not_hide_unimplemented_range_or_name_collisions(query):
    with pytest.raises(GrafxPlanError):
        analyze(parse(query))


@pytest.mark.parametrize("query", [
    "MATCH journey = (x:Person)<-[edge:Knows]-(y:Person) RETURN journey",
    "MATCH journey = (x:Person)-[edge:Knows]-(y:Person) RETURN journey",
    "MATCH journey = (x:Person)-[edge:Knows]->(y:Person) WHERE x.id='x' RETURN journey",
])
def test_generic_names_support_direction_and_predicates(query):
    assert analyze(parse(query)).binding("journey").entity == "path"


def test_generic_names_support_native_bounded_variable_capture():
    query = "MATCH journey=(x:Person)-[edge:Knows*0..2]->(y:Person) RETURN journey"
    assert analyze(parse(query)).binding("journey").entity == "path"
