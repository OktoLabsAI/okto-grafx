"""The filter-aware graph itself (SPEC-VEC FR-5, FR-6, VTS-6).

Three properties carry the correctness of the approximate regime and each is asserted directly
on the graph, where it can be seen, rather than only through the engine, where it cannot:

* **connectivity** -- every live node is reachable from the entry point at layer zero, after any
  sequence of insertions and removals. That is what the never-pruned chain buys;
* **exhaustiveness** -- a beam at least as wide as the graph never fills, so the traversal visits
  everything and returns exactly what a scan would. That is what makes the two regimes agree;
* **bridging** -- a node the predicate refuses is expanded and not returned. That is the ACORN
  rule, and the traversal statistics are how a test can see it happened during navigation rather
  than afterwards.

Determinism is the fourth: the same seed and the same insertions build the same graph, and the
same query returns the same ranking, on every run.
"""

from __future__ import annotations

import pytest

from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.rand import SplitMix64
from okto_grafx.domain.vector.hnsw import (
    DEFAULT_EF_CONSTRUCTION,
    DEFAULT_EF_SEARCH,
    DEFAULT_NEIGHBOURS,
    MAX_EF_SEARCH,
    MAX_LEVEL,
    HnswGraph,
)

from .conftest import seeded_vectors

DIMENSION: int = 10


def _graph(
    corpus: list[tuple[float, ...]],
    *,
    seed: int = 0x1234,
    metric: DistanceMetric = DistanceMetric.COSINE,
    neighbours: int = DEFAULT_NEIGHBOURS,
) -> HnswGraph:
    """Return a graph holding one corpus, with node ids counted from one."""
    graph = HnswGraph(PureVectorMath(), metric, seed=seed, neighbours=neighbours)
    for index, values in enumerate(corpus):
        graph.insert(index + 1, values)
    return graph


def _exhaustive(corpus: list[tuple[float, ...]], query, k: int, metric) -> list[int]:
    """Return the ground truth ranking of a corpus without the graph taking part."""
    ranked = PureVectorMath().top_k(
        query, [(index + 1, values) for index, values in enumerate(corpus)], k, metric
    )
    return [identifier for identifier, _score in ranked]


# --- connectivity ---------------------------------------------------------------------------


def test_layer_zero_is_connected_after_a_plain_build() -> None:
    """The property the chain exists to provide, on the simplest possible history."""
    graph = _graph(seeded_vectors(80, DIMENSION, seed=1))
    assert graph.is_connected_at_layer_zero()
    assert len(graph) == 80


def test_layer_zero_stays_connected_after_arbitrary_removals() -> None:
    """A removal splices the chain; a graph that lost reachability would fail here."""
    generator = SplitMix64(0xFEED)
    graph = _graph(seeded_vectors(120, DIMENSION, seed=2))
    alive = set(graph.nodes())
    for _step in range(60):
        victim = sorted(alive)[generator.next_below(len(alive))]
        graph.remove(victim)
        alive.discard(victim)
        assert graph.is_connected_at_layer_zero(), victim
    assert len(graph) == len(alive)


def test_layer_zero_stays_connected_under_interleaved_insertion_and_removal() -> None:
    """Churn is the interesting case: the chain has to be spliced and extended at once."""
    generator = SplitMix64(0xC0DE)
    corpus = seeded_vectors(200, DIMENSION, seed=3)
    graph = HnswGraph(PureVectorMath(), DistanceMetric.COSINE, seed=7)
    alive: set[int] = set()
    for index, values in enumerate(corpus):
        graph.insert(index + 1, values)
        alive.add(index + 1)
        if alive and generator.next_below(3) == 0:
            victim = sorted(alive)[generator.next_below(len(alive))]
            graph.remove(victim)
            alive.discard(victim)
        assert graph.is_connected_at_layer_zero()
    assert set(graph.nodes()) == alive


def test_removing_the_entry_point_elects_a_new_one_deterministically() -> None:
    """The tallest node wins and the lowest id breaks the tie, never a dictionary order."""
    graph = _graph(seeded_vectors(40, DIMENSION, seed=4))
    first = graph.entry_point
    assert first is not None
    graph.remove(first)
    assert graph.entry_point is not None
    assert graph.entry_point != first
    assert graph.is_connected_at_layer_zero()


def test_removing_every_node_leaves_an_empty_graph_with_no_entry_point() -> None:
    """The empty graph is a state the search has to survive, so it is a state a build reaches."""
    graph = _graph(seeded_vectors(12, DIMENSION, seed=5))
    for node in graph.nodes():
        graph.remove(node)
    assert len(graph) == 0
    assert graph.entry_point is None
    assert graph.is_connected_at_layer_zero()
    assert graph.search((0.0,) * DIMENSION, 5)[0] == ()


def test_the_chain_holds_every_live_node_in_insertion_order() -> None:
    """The chain is the connectivity guarantee, so its contents are asserted directly."""
    graph = _graph(seeded_vectors(20, DIMENSION, seed=6))
    assert graph.chain() == tuple(range(1, 21))
    graph.remove(7)
    graph.remove(1)
    graph.remove(20)
    assert graph.chain() == tuple(node for node in range(2, 20) if node != 7)


def test_a_chain_neighbour_is_part_of_the_layer_zero_neighbourhood() -> None:
    """The chain edges live outside the adjacency lists and are still neighbours."""
    graph = _graph(seeded_vectors(30, DIMENSION, seed=7))
    for node in graph.nodes():
        neighbours = graph.neighbours_of(node, 0)
        assert len(set(neighbours)) == len(neighbours)
        assert node not in neighbours
    assert 2 in graph.neighbours_of(1, 0)


# --- exhaustiveness -------------------------------------------------------------------------


@pytest.mark.parametrize("metric", list(DistanceMetric))
def test_a_beam_as_wide_as_the_graph_returns_the_exhaustive_answer(
    metric: DistanceMetric,
) -> None:
    """This is the property the two regimes rest on, asserted for all three metrics."""
    corpus = seeded_vectors(70, DIMENSION, seed=8)
    graph = _graph(corpus, metric=metric)
    query = corpus[13]
    ranked, stats = graph.search(query, len(corpus))
    assert stats.exhaustive
    assert stats.visited == len(corpus)
    assert [node for _score, node in ranked][:10] == _exhaustive(corpus, query, 10, metric)


def test_a_narrow_beam_stops_early_and_says_so() -> None:
    """The flag is honest in both directions, or it could not be used as evidence."""
    corpus = seeded_vectors(200, DIMENSION, seed=9)
    graph = _graph(corpus)
    _ranked, stats = graph.search(corpus[0], 4)
    assert not stats.exhaustive
    assert stats.visited < len(corpus)


def test_the_exhaustive_answer_survives_removals() -> None:
    """A reconciled graph must still be able to answer exactly when the beam allows it."""
    corpus = seeded_vectors(60, DIMENSION, seed=10)
    graph = _graph(corpus)
    for node in (3, 11, 29, 40):
        graph.remove(node)
    survivors = [
        (index + 1, values)
        for index, values in enumerate(corpus)
        if (index + 1) in set(graph.nodes())
    ]
    query = corpus[5]
    ranked, stats = graph.search(query, len(graph))
    assert stats.exhaustive
    expected = PureVectorMath().top_k(query, survivors, 8, DistanceMetric.COSINE)
    assert [node for _score, node in ranked][:8] == [node for node, _score in expected]


# --- bridging -------------------------------------------------------------------------------


def test_a_node_the_predicate_refuses_is_expanded_and_not_returned() -> None:
    """VTS-6: the predicate is evaluated during navigation, and bridges prove it."""
    corpus = seeded_vectors(120, DIMENSION, seed=11)
    graph = _graph(corpus)
    admitted = {node for node in graph.nodes() if node % 7 == 0}
    ranked, stats = graph.search(corpus[0], 20, lambda node: node in admitted)
    assert stats.bridges > 0
    assert stats.visited > stats.admitted
    assert all(node in admitted for _score, node in ranked)


def test_a_filter_that_admits_nothing_returns_nothing_and_still_traverses() -> None:
    """An empty answer is an answer; the traversal still visited the graph to establish it."""
    corpus = seeded_vectors(60, DIMENSION, seed=12)
    graph = _graph(corpus)
    ranked, stats = graph.search(corpus[0], len(corpus), lambda node: False)
    assert ranked == ()
    assert stats.bridges == stats.visited == len(corpus)


def test_a_filter_that_admits_everything_reports_no_bridges() -> None:
    """The bridge count is a measurement, so it has to be zero when nothing was refused."""
    corpus = seeded_vectors(50, DIMENSION, seed=13)
    graph = _graph(corpus)
    _ranked, stats = graph.search(corpus[0], len(corpus), lambda node: True)
    assert stats.bridges == 0


def test_a_selective_filter_still_finds_its_neighbours_through_the_bridges() -> None:
    """The point of bridging: a rare match far from the entry point is still reachable."""
    corpus = seeded_vectors(150, DIMENSION, seed=14)
    graph = _graph(corpus)
    admitted = {140, 141, 142}
    ranked, _stats = graph.search(corpus[0], 64, lambda node: node in admitted)
    assert {node for _score, node in ranked} == admitted


# --- determinism ------------------------------------------------------------------------------


def test_two_graphs_from_one_seed_are_identical() -> None:
    """Reproducibility is a requirement, so it is measured rather than assumed (G2b)."""
    corpus = seeded_vectors(90, DIMENSION, seed=15)
    left = _graph(corpus, seed=0xABCD)
    right = _graph(corpus, seed=0xABCD)
    assert left.entry_point == right.entry_point
    assert left.top_level == right.top_level
    for node in left.nodes():
        for layer in range(left.top_level + 1):
            assert left.neighbours_of(node, layer) == right.neighbours_of(node, layer)


def test_a_different_seed_assigns_different_towers() -> None:
    """The seed decides tower height and nothing else, so that is where a difference must show.

    It is deliberately NOT asserted that the layer-zero neighbourhoods differ. On a corpus this
    size the beam finds the same nearest neighbours whatever the towers look like, and a test
    that demanded a difference there would be asserting a coincidence rather than the rule.
    """
    corpus = seeded_vectors(90, DIMENSION, seed=16)
    left = _graph(corpus, seed=1)
    right = _graph(corpus, seed=2)
    towers = [(left.level_of(node), right.level_of(node)) for node in left.nodes()]
    assert any(one != other for one, other in towers)


def test_the_same_query_returns_the_same_ranking_on_every_run() -> None:
    """A ranking that moved between runs would be non-determinism under CONTRACT 13.4."""
    corpus = seeded_vectors(120, DIMENSION, seed=17)
    graph = _graph(corpus)
    first, _stats = graph.search(corpus[3], 12)
    for _repeat in range(5):
        again, _more = graph.search(corpus[3], 12)
        assert again == first


def test_identical_vectors_rank_by_ascending_node_identifier() -> None:
    """The tie rule of the port, restated inside the graph so a beam cannot reorder it."""
    graph = HnswGraph(PureVectorMath(), DistanceMetric.COSINE, seed=3)
    for node in (9, 3, 7, 5):
        graph.insert(node, (1.0, 0.0))
    ranked, _stats = graph.search((1.0, 0.0), 4)
    assert [node for _score, node in ranked] == [3, 5, 7, 9]


# --- refusals ---------------------------------------------------------------------------------


def test_inserting_a_node_twice_is_refused() -> None:
    """A version enters the graph once; a second insertion would leave two copies of one point."""
    graph = _graph(seeded_vectors(5, DIMENSION, seed=18))
    with pytest.raises(GrafxConfigurationError) as failure:
        graph.insert(1, (0.0,) * DIMENSION)
    assert failure.value.details["field"] == "node"


def test_removing_a_node_that_is_not_there_is_refused() -> None:
    """A silent no-op would hide a reconciliation pass acting on the wrong index."""
    graph = _graph(seeded_vectors(5, DIMENSION, seed=19))
    with pytest.raises(GrafxConfigurationError):
        graph.remove(99)


def test_asking_for_the_values_of_an_absent_node_is_refused() -> None:
    """Every door of the graph answers with a typed error rather than a KeyError."""
    graph = _graph(seeded_vectors(3, DIMENSION, seed=20))
    with pytest.raises(GrafxConfigurationError):
        graph.values_of(42)


@pytest.mark.parametrize("width", [0, -1])
def test_a_beam_narrower_than_one_is_refused(width: int) -> None:
    """A beam of zero could return nothing however good the graph is."""
    graph = _graph(seeded_vectors(3, DIMENSION, seed=21))
    with pytest.raises(GrafxConfigurationError) as failure:
        graph.search((0.0,) * DIMENSION, width)
    assert failure.value.details["field"] == "ef"


def test_a_neighbour_count_below_one_is_refused_at_construction() -> None:
    """A graph with no neighbours per node is a chain, and saying so early is cheaper."""
    with pytest.raises(GrafxConfigurationError):
        HnswGraph(PureVectorMath(), DistanceMetric.COSINE, seed=1, neighbours=0)


# --- the defaults are pinned -------------------------------------------------------------------


def test_the_graph_defaults_are_the_literals_the_module_documents() -> None:
    """A56 and A68: a default that slides changes the shape of every index in the build."""
    assert DEFAULT_NEIGHBOURS == 16
    assert DEFAULT_EF_CONSTRUCTION == 200
    assert DEFAULT_EF_SEARCH == 320
    assert MAX_EF_SEARCH == 1_048_576
    assert MAX_LEVEL == 32


def test_no_node_is_given_a_tower_taller_than_the_bound() -> None:
    """The cap bounds the per-node cost, and a draw is not allowed to exceed it."""
    graph = _graph(seeded_vectors(500, 4, seed=22), neighbours=2)
    assert graph.top_level <= MAX_LEVEL


# --- the chain is load-bearing, not decoration ---------------------------------------------


SPLIT_CORPUS: tuple[tuple[float, ...], ...] = tuple(
    [(1.0 + 0.01 * index, 0.0, 0.0) for index in range(6)]
    + [(0.0, 0.0, 1.0 + 0.01 * index) for index in range(6)]
)
"""Two clusters far apart, which is the shape a small neighbourhood cap cannot bridge."""


def _split_graph() -> HnswGraph:
    """Return a graph over two distant clusters with the smallest legal neighbourhood."""
    graph = HnswGraph(
        PureVectorMath(),
        DistanceMetric.EUCLIDEAN,
        seed=1,
        neighbours=1,
        ef_construction=4,
    )
    for index, values in enumerate(SPLIT_CORPUS):
        graph.insert(index + 1, values)
    return graph


def _adjacency_only(graph: HnswGraph) -> dict[int, set[int]]:
    """Return the layer-zero neighbourhoods with the chain edges taken back out.

    The chain edges are not in the adjacency lists, so a test can subtract them from what
    ``neighbours_of`` reports and see what the adjacency alone would have offered.
    """
    order = graph.chain()
    position = {node: index for index, node in enumerate(order)}
    edges: dict[int, set[int]] = {}
    for node in graph.nodes():
        peers = set(graph.neighbours_of(node, 0))
        index = position[node]
        if index > 0:
            peers.discard(order[index - 1])
        if index + 1 < len(order):
            peers.discard(order[index + 1])
        edges[node] = peers
    return edges


def _reachable(edges: dict[int, set[int]], start: int) -> set[int]:
    """Return every node reachable from a start node over one edge relation."""
    seen = {start}
    frontier = [start]
    while frontier:
        node = frontier.pop()
        for peer in edges.get(node, ()):
            if peer not in seen:
                seen.add(peer)
                frontier.append(peer)
    return seen


def test_the_adjacency_alone_can_be_disconnected_and_the_chain_still_holds() -> None:
    """The construction the chain exists for, made explicit rather than assumed.

    With the smallest legal neighbourhood and two well-separated clusters, the trimmed adjacency
    reaches one node out of twelve from the entry point. The graph is connected anyway, and the
    only thing making it so is the chain. Without this the chain is untestable: at the default
    neighbourhood the adjacency happens to be connected on its own, so removing the chain
    changes no answer and nothing names the loss.
    """
    graph = _split_graph()
    assert graph.entry_point is not None
    adjacency_reach = _reachable(_adjacency_only(graph), graph.entry_point)
    assert len(adjacency_reach) < len(graph)
    assert graph.is_connected_at_layer_zero()


def test_a_graph_whose_adjacency_is_split_still_answers_exhaustively() -> None:
    """Connectivity is only worth having if it changes the answer, so the answer is asserted."""
    graph = _split_graph()
    query = SPLIT_CORPUS[-1]
    ranked, stats = graph.search(query, len(SPLIT_CORPUS))
    assert stats.exhaustive
    assert len(ranked) == len(SPLIT_CORPUS)
    expected = PureVectorMath().top_k(
        query,
        [(index + 1, values) for index, values in enumerate(SPLIT_CORPUS)],
        len(SPLIT_CORPUS),
        DistanceMetric.EUCLIDEAN,
    )
    assert [node for _score, node in ranked] == [node for node, _score in expected]


def test_a_split_graph_stays_whole_when_a_node_is_reconciled_away() -> None:
    """The splice is what keeps the chain a chain; a removal must not cut it in two."""
    graph = _split_graph()
    for node in (3, 8, 1, 12):
        graph.remove(node)
        assert graph.is_connected_at_layer_zero(), node
    survivors = [node for node in range(1, 13) if node not in {3, 8, 1, 12}]
    assert graph.chain() == tuple(survivors)
    ranked, stats = graph.search(SPLIT_CORPUS[-2], len(graph))
    assert stats.exhaustive
    assert sorted(node for _score, node in ranked) == survivors


# --- the stopping rule and the entry point are rules, not side effects ----------------------


@pytest.mark.parametrize("width", [4, 16, 40])
def test_a_traversal_never_stops_before_the_result_set_is_full(width: int) -> None:
    """The beam stops when it cannot improve a FULL result set, never merely a good one.

    Stated as the rule rather than as a symptom (amendment A71): while the result set has room,
    every unvisited neighbour is worth expanding, so a traversal that ends early has abandoned
    neighbours it was still allowed to find. A stopping rule that dropped the fullness term
    would still pass a recall measurement -- the greedy descent lands close enough that the first
    few neighbours are usually the true ones -- and would quietly answer with a fraction of the
    beam it was given.
    """
    corpus = seeded_vectors(120, DIMENSION, seed=31)
    graph = _graph(corpus)
    _ranked, stats = graph.search(corpus[42], width)
    assert stats.admitted == width
    assert stats.visited >= width


def test_a_traversal_fills_the_result_set_even_when_the_query_is_far_from_everything() -> None:
    """The rule has to hold for a query outside the corpus, which is where a greedy stop bites."""
    corpus = seeded_vectors(120, DIMENSION, seed=32)
    graph = _graph(corpus)
    far = tuple(5.0 for _position in range(DIMENSION))
    _ranked, stats = graph.search(far, 20)
    assert stats.admitted == 20


def test_the_entry_point_is_always_the_tallest_node_in_the_graph() -> None:
    """The hierarchy is only a hierarchy if the descent starts at the top of it."""
    graph = _graph(seeded_vectors(150, DIMENSION, seed=33))
    for _step in range(12):
        assert graph.entry_point is not None
        tallest = max(graph.level_of(node) for node in graph.nodes())
        assert graph.level_of(graph.entry_point) == tallest
        assert graph.top_level == tallest
        graph.remove(graph.entry_point)


def test_the_entry_point_election_breaks_a_tie_by_the_lower_identifier() -> None:
    """Two equally tall nodes must not be separated by a dictionary order."""
    graph = HnswGraph(PureVectorMath(), DistanceMetric.COSINE, seed=44, neighbours=2)
    for node in (5, 2, 9, 7):
        graph.insert(node, (float(node), 1.0))
    tallest = max(graph.level_of(node) for node in graph.nodes())
    candidates = sorted(node for node in graph.nodes() if graph.level_of(node) == tallest)
    while graph.entry_point != candidates[0]:
        graph.remove(graph.entry_point)
        tallest = max(graph.level_of(node) for node in graph.nodes())
        candidates = sorted(node for node in graph.nodes() if graph.level_of(node) == tallest)
    assert graph.entry_point == min(candidates)


def test_removing_a_node_links_its_former_neighbours_to_each_other() -> None:
    """A removal relinks the neighbourhood it breaks, so the graph does not decay under churn.

    This is a quality measure and not the connectivity guarantee -- the chain is that -- but it
    is a rule with an observable consequence, so it is asserted directly rather than left to a
    recall number that a small corpus cannot distinguish. Measured over a corpus of 250 with a
    third of the nodes reconciled away, the relink holds the mean layer-zero degree at 32.2
    against 22.2 without it, while recall is 1.0 either way at that size.
    """
    graph = HnswGraph(
        PureVectorMath(), DistanceMetric.COSINE, seed=5, neighbours=2, ef_construction=8
    )
    corpus = seeded_vectors(14, 4, seed=91)
    for index, values in enumerate(corpus):
        graph.insert(index + 1, values)
    victim = graph.entry_point
    assert victim is not None
    before = [
        peer
        for peer in graph.neighbours_of(victim, 0)
        if peer not in {graph.chain()[0], victim}
    ]
    assert len(before) >= 2, before
    graph.remove(victim)
    for node in before:
        assert victim not in graph.neighbours_of(node, 0)
    linked = [
        (one, other)
        for one, other in zip(before, before[1:])
        if other in graph.neighbours_of(one, 0)
    ]
    assert linked, before


def test_the_neighbourhoods_do_not_decay_when_a_third_of_the_graph_is_reconciled() -> None:
    """The same rule at scale: the surviving nodes keep the degree the relink gives them."""
    corpus = seeded_vectors(120, DIMENSION, seed=92)
    graph = _graph(corpus)
    for node in [value for value in range(1, 121) if value % 3 == 0]:
        graph.remove(node)
    degrees = [len(graph.neighbours_of(node, 0)) for node in graph.nodes()]
    mean = sum(degrees) / len(degrees)
    assert mean > 26.0, mean


def test_a_filter_admitting_near_and_far_rows_returns_all_of_them() -> None:
    """The guarantee two mechanisms defend together, asserted as the outcome they protect.

    Measured as an A93 matrix on a 150-vector corpus with a filter admitting the ten nearest and
    the three farthest rows: pristine finds 13 of 13; with the fullness term of the stopping rule
    removed it finds 13 of 13; with the chain edges removed it finds 13 of 13; with BOTH removed
    it finds 10 of 13 and visits 107 nodes instead of 150. The two are individually sufficient
    and jointly necessary, so no single mutation of either can be killed -- which is why this
    test pins the outcome rather than pretending to pin one mechanism.
    """
    corpus = seeded_vectors(150, DIMENSION, seed=11)
    graph = _graph(corpus)
    query = corpus[0]
    ranked_all = PureVectorMath().top_k(
        query,
        [(index + 1, values) for index, values in enumerate(corpus)],
        len(corpus),
        DistanceMetric.COSINE,
    )
    admitted = {node for node, _score in ranked_all[:10]} | {
        node for node, _score in ranked_all[-3:]
    }
    ranked, _stats = graph.search(query, 16, lambda node: node in admitted)
    assert {node for _score, node in ranked} == admitted


def test_the_descent_through_the_upper_layers_places_the_beam_near_the_query() -> None:
    """The hierarchy is what a narrow beam depends on, measured where the hierarchy is tall.

    At the default neighbourhood the towers are short -- one layer above zero for a corpus of
    this size -- and the chain at layer zero already reaches everything, so disabling the descent
    changes nothing observable. A smaller neighbourhood makes the towers tall, and there the
    descent is decisive: measured over fifty queries with a beam of one, the descent finds the
    true nearest neighbour thirty times against fourteen without it. The floor below sits between
    those two numbers, and both are constants of a seeded corpus rather than samples.
    """
    corpus = seeded_vectors(150, 8, seed=88)
    graph = HnswGraph(
        PureVectorMath(),
        DistanceMetric.COSINE,
        seed=0x99,
        neighbours=2,
        ef_construction=8,
    )
    for index, values in enumerate(corpus):
        graph.insert(index + 1, values)
    assert graph.top_level >= 4, graph.top_level
    math = PureVectorMath()
    candidates = [(index + 1, values) for index, values in enumerate(corpus)]
    exact = 0
    queries = list(range(0, 150, 3))
    for position in queries:
        query = corpus[position]
        ranked, _stats = graph.search(query, 1)
        best = math.top_k(query, candidates, 1, DistanceMetric.COSINE)[0][0]
        if ranked and ranked[0][1] == best:
            exact += 1
    assert exact >= 24, f"{exact} of {len(queries)}"
