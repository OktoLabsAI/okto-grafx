"""The plan invariants: one tree, no over-fetch, and a walk that always ends (SPEC-VEC AC-7)."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.query.ast import Literal, Parameter, ReturnItem, Variable
from okto_grafx.domain.query.plan import (
    MAX_PLAN_DEPTH,
    FilterRows,
    LimitRows,
    PlanNode,
    ProduceResults,
    ProjectRows,
    SingleRow,
    VectorSearch,
    plan_nodes,
    validate_plan,
)
from tests.query.conftest import find_operator, operators, plan_text

HYBRID: str = (
    "MATCH (n:Chunk)-[:BELONGS_TO]->(d:Doc) "
    "WHERE n.layer = $layer AND d.active = true "
    "AND similarity(n.embedding, $q, space => 'minilm_v2') > 0.7 "
    "RETURN n.id, similarity_score() AS score ORDER BY score DESC LIMIT 10"
)


def bounded_search(child: PlanNode) -> VectorSearch:
    """Return a similarity operator that carries a top-k bound."""
    return VectorSearch(
        child=child,
        variable="n",
        space=Literal(value="minilm_v2"),
        query_vector=Parameter(name="q"),
        property_key="embedding",
        score_column="similarity score",
        column_space="minilm_v2",
        k=Literal(value=10),
    )


def unbounded_search(child: PlanNode) -> VectorSearch:
    """Return a similarity operator that scores every candidate."""
    return VectorSearch(
        child=child,
        variable="n",
        space=Literal(value="minilm_v2"),
        query_vector=Parameter(name="q"),
        property_key="embedding",
        score_column="similarity score",
        column_space="minilm_v2",
        k=None,
    )


# --- the one-tree rule ------------------------------------------------------------------------


def test_the_hybrid_query_carries_exactly_one_similarity_operator() -> None:
    assert operators(plan_text(HYBRID).root).count("VectorSearch") == 1


def test_the_hybrid_query_has_no_operator_that_fetches_more_than_it_keeps() -> None:
    # AC-7 in its checkable form: nothing that can discard a row sits above the bounded search.
    planned = plan_text(HYBRID)
    names = operators(planned.root)
    search_at = names.index("VectorSearch")
    discarding = {"FilterRows", "DistinctRows"}
    assert not [
        index for index, label in enumerate(names) if label in discarding and index < search_at
    ]


def test_the_candidate_set_reaches_the_search_as_a_child_and_not_as_a_second_query() -> None:
    planned = plan_text(HYBRID)
    search = find_operator(planned.root, "VectorSearch")
    assert isinstance(search, VectorSearch)
    below = operators(search.child)
    assert "TraverseRelationship" in below
    assert "FilterRows" in below


def test_a_filter_above_a_bounded_search_is_refused() -> None:
    root = ProduceResults(
        child=FilterRows(
            child=bounded_search(SingleRow()), predicate=Literal(value=True)
        ),
        columns=(),
    )
    with pytest.raises(GrafxPlanError) as failure:
        validate_plan(root)
    # The "space" detail is set by this guard and by no other, so the assertion cannot pass
    # because some neighbouring check refused the same tree first (amendment A62).
    assert failure.value.details["space"] == "'minilm_v2'"
    assert failure.value.details["value"] == "VectorSearch"


def test_a_filter_far_above_a_bounded_search_is_refused_too() -> None:
    # The rule is about reachability, not about being the immediate parent.
    root = ProduceResults(
        child=FilterRows(
            child=ProjectRows(
                child=LimitRows(child=bounded_search(SingleRow()), count=Literal(value=3)),
                items=(ReturnItem(expression=Variable(name="n"), alias="n"),),
            ),
            predicate=Literal(value=True),
        ),
        columns=(),
    )
    with pytest.raises(GrafxPlanError):
        validate_plan(root)


def test_a_filter_above_an_unbounded_search_is_allowed() -> None:
    # Nothing was truncated, so nothing can be missing: this is a filter on a computed column,
    # not an over-fetch. Refusing it would forbid a correct plan.
    root = ProduceResults(
        child=FilterRows(
            child=unbounded_search(SingleRow()), predicate=Literal(value=True)
        ),
        columns=(),
    )
    assert validate_plan(root) is root


def test_a_filter_below_a_bounded_search_is_allowed() -> None:
    root = ProduceResults(
        child=bounded_search(FilterRows(child=SingleRow(), predicate=Literal(value=True))),
        columns=(),
    )
    assert validate_plan(root) is root


def test_the_planner_never_produces_the_refused_shape() -> None:
    # Whatever the query, the planner's own output satisfies the rule; the two mechanisms are
    # checked against each other rather than either being taken on trust.
    for text in (
        HYBRID,
        HYBRID.replace("RETURN n.id", "RETURN DISTINCT n.id"),
        HYBRID.replace("ORDER BY score DESC", "ORDER BY n.id"),
        "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => 'minilm_v2') * 2 > 1 "
        "RETURN n.id, similarity_score() AS score ORDER BY score DESC LIMIT 3",
        "MATCH (n:Chunk) WHERE similarity(n.embedding, $q, space => 'minilm_v2') > 0.1 "
        "RETURN n.id",
    ):
        assert validate_plan(plan_text(text).root) is not None


# --- walking ------------------------------------------------------------------------------------


def test_a_walk_yields_parents_before_children() -> None:
    planned = plan_text("MATCH (p:Person) RETURN p.name")
    assert operators(planned.root) == ("ProduceResults", "ProjectRows", "NodeScan", "SingleRow")


def test_plan_nodes_and_walk_agree() -> None:
    planned = plan_text(HYBRID)
    assert tuple(node.label for node in plan_nodes(planned.root)) == operators(planned.root)


class _Pair(PlanNode):
    """A two-child operator, so a shared child is representable at all.

    Every operator this component ships has at most one child, which makes a plan a chain and a
    shared node impossible to build. The guard against one is still worth having -- a later
    operator with two inputs is the obvious next shape -- and it is only testable through a node
    like this one. It lives in the test rather than in the shipped surface (amendment A81).
    """

    __slots__ = ("left", "right")

    def __init__(self, left: PlanNode, right: PlanNode) -> None:
        self.left = left
        self.right = right

    def children(self) -> tuple[PlanNode, ...]:
        """Return both inputs, left first."""
        return (self.left, self.right)


def test_a_two_child_operator_walks_both_of_its_inputs() -> None:
    # The instrument first: a pair with two distinct children is walked, so the refusal in the
    # next test is about sharing and not about the shape of the node.
    pair = _Pair(SingleRow(), SingleRow())
    assert [node.label for node in pair.walk()] == ["_Pair", "SingleRow", "SingleRow"]


def test_a_shared_operator_is_refused_rather_than_walked_twice() -> None:
    shared = SingleRow()
    pair = _Pair(shared, shared)
    with pytest.raises(GrafxPlanError) as failure:
        tuple(pair.walk())
    assert failure.value.details["field"] == "operator"
    assert failure.value.details["value"] == "SingleRow"


def test_a_shared_operator_is_refused_by_every_view_of_the_tree() -> None:
    shared = SingleRow()
    pair = _Pair(shared, shared)
    with pytest.raises(GrafxPlanError):
        pair.render()
    with pytest.raises(GrafxPlanError):
        pair.to_dict()
    with pytest.raises(GrafxPlanError):
        validate_plan(pair)


def test_a_tree_deeper_than_the_ceiling_is_refused_rather_than_walked() -> None:
    node: PlanNode = SingleRow()
    for _ in range(MAX_PLAN_DEPTH + 2):
        node = FilterRows(child=node, predicate=Literal(value=True))
    with pytest.raises(GrafxPlanError) as failure:
        tuple(node.walk())
    assert failure.value.details["field"] == "depth"


def test_rendering_a_tree_deeper_than_the_ceiling_is_refused() -> None:
    node: PlanNode = SingleRow()
    for _ in range(MAX_PLAN_DEPTH + 2):
        node = FilterRows(child=node, predicate=Literal(value=True))
    with pytest.raises(GrafxPlanError):
        node.render()


def test_the_dictionary_view_of_a_tree_deeper_than_the_ceiling_is_refused() -> None:
    # A recursive to_dict would answer this with RecursionError, which is not a Grafx type and
    # would be a non-Grafx escape from a public door.
    node: PlanNode = SingleRow()
    for _ in range(MAX_PLAN_DEPTH + 2):
        node = FilterRows(child=node, predicate=Literal(value=True))
    with pytest.raises(GrafxPlanError) as failure:
        node.to_dict()
    assert failure.value.details["field"] == "depth"


def test_the_dictionary_view_of_a_very_deep_tree_never_exhausts_the_stack() -> None:
    node: PlanNode = SingleRow()
    for _ in range(5000):
        node = FilterRows(child=node, predicate=Literal(value=True))
    with pytest.raises(GrafxPlanError):
        node.to_dict()


def test_validating_something_that_is_not_a_plan_is_refused_in_the_taxonomy() -> None:
    with pytest.raises(GrafxPlanError) as failure:
        validate_plan("not a plan")  # type: ignore[arg-type]
    assert failure.value.details["field"] == "plan"


# --- inspection ---------------------------------------------------------------------------------


def test_the_rendered_tree_indents_by_depth() -> None:
    lines = plan_text("MATCH (p:Person) RETURN p.name").root.render()
    assert lines[0].startswith("ProduceResults")
    assert lines[1].startswith("  ProjectRows")
    assert lines[2].startswith("    NodeScan")


def test_the_dictionary_view_carries_the_same_operators_as_the_walk() -> None:
    planned = plan_text(HYBRID)
    view = planned.root.to_dict()

    def collect(node: dict[str, object]) -> list[str]:
        """Return the operator names of a dictionary view, parents first."""
        children = node["children"]
        assert isinstance(children, list)
        found = [str(node["operator"])]
        for child in children:
            assert isinstance(child, dict)
            found.extend(collect(child))
        return found

    assert tuple(collect(view)) == operators(planned.root)


def test_every_operator_describes_itself_without_raising() -> None:
    planned = plan_text(HYBRID)
    for node in planned.root.walk():
        assert isinstance(node.describe(), str)
        assert node.describe()
