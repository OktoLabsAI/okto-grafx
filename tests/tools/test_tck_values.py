"""Upstream literals have an independent, non-executable graph/value oracle."""

import math

import pytest

from tools.tck_values import ReferenceNode, ReferencePath, ReferenceRelationship, reference_key, reference_value


@pytest.mark.parametrize("text,value", [
    ("null", None), ("true", True), ("false", False), ("1337", 1337),
    ("-42", -42), ("1.0", 1.0), ("-1.2e+3", -1200.0),
    ("'a\\n漢'", "a\n漢"), ("[1, null, true]", [1, None, True]),
    ("{a: 1, `two words`: ['x']}", {"a": 1, "two words": ["x"]}),
    ("(:B:A {n: 1})", ReferenceNode(frozenset({"A", "B"}), (("n", 1),))),
    ("({n: 1})", ReferenceNode(frozenset(), (("n", 1),))),
    ("()", ReferenceNode(frozenset(), ())),
    ("[:KNOWS {since: 2020}]", ReferenceRelationship("KNOWS", (("since", 2020),))),
])
def test_independent_primitives_containers_and_graph_literals(text, value):
    assert reference_value(text) == value


def test_paths_preserve_every_node_relationship_and_direction():
    path = reference_value("<(:A {id: 1})-[:R]->(:B)<-[:S {x: 2}]-()>")
    assert isinstance(path, ReferencePath)
    assert path.nodes[0] == ReferenceNode(frozenset({"A"}), (("id", 1),))
    assert len(path.nodes) == 3 and len(path.relationships) == 2
    assert path.directions == ("outgoing", "incoming")
    assert reference_value("<()>").directions == ()


def test_graph_literals_work_inside_lists_maps_and_hashable_comparison_keys():
    left = reference_value("{nodes: [(:A:B {x: [1,2]}), ()]}")
    right = reference_value("{nodes: [(:B:A {x: [1,2]}), ()]}")
    assert reference_key(left) == reference_key(right)
    assert hash(reference_key(left)) == hash(reference_key(right))


def test_nan_and_infinities_are_oracle_values_not_storage_admission():
    assert math.isnan(reference_value("NaN"))
    assert reference_value("Inf") == math.inf
    assert reference_value("-Inf") == -math.inf
    assert reference_key(reference_value("NaN")) == reference_key(float("nan"))


@pytest.mark.parametrize("text", [
    "__import__('os')", "[x for x in y]", "date('2020')", "1 + 2", "1 2",
    "(:A", "[:R", "<()-[:R]()>", "{a:1,a:2}", "[1", "{a:unknown}", "",
])
def test_executable_malformed_and_trailing_notation_refuses(text):
    with pytest.raises((ValueError, SyntaxError)):
        reference_value(text)


def test_unordered_lists_still_preserve_duplicates_and_entity_kinds():
    assert reference_key([1, 2], unordered_lists=True) == reference_key([2, 1], unordered_lists=True)
    assert reference_key([1, 1], unordered_lists=True) != reference_key([1], unordered_lists=True)
    assert reference_key(True) != reference_key(1)
    assert reference_key(reference_value("(:A)")) != reference_key(reference_value("[:A]"))


def test_nested_reference_literals_are_bounded():
    with pytest.raises(ValueError, match="nesting"):
        reference_value("[" * 130 + "0" + "]" * 130)
