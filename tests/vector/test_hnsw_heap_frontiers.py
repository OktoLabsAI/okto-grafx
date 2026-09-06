"""Heap frontiers preserve the former HNSW ranking, traversal and topology exactly."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

import pytest

from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.vector import hnsw as hnsw_module
from okto_grafx.domain.vector.hnsw import HnswGraph, TraversalStats, _insert_ranked


class _LegacyHnswGraph(HnswGraph):
    """The pre-heap algorithms, retained locally as a differential oracle."""

    def _trim(self, node: int, layer: int) -> None:
        adjacency = self._links[layer]  # noqa: SLF001 - differential oracle
        peers = adjacency.get(node)
        capacity = self._capacity(layer)  # noqa: SLF001 - differential oracle
        if peers is None or len(peers) <= capacity:
            return
        scorer = self._scorer(  # noqa: SLF001 - differential oracle
            self._components(node)  # noqa: SLF001 - differential oracle
        )
        ranked: list[tuple[float, int]] = []
        for peer in peers:
            _insert_ranked(ranked, scorer(peer), peer, capacity)
        kept = [peer for _score, peer in ranked]
        adjacency[node] = kept
        retained = set(kept)
        for peer in peers:
            if peer in retained:
                continue
            other = adjacency.get(peer)
            if other is not None and node in other:
                other.remove(node)

    def _search_layer_counted(
        self,
        scorer: Callable[[int], float],
        entry_points: Iterable[int],
        ef: int,
        layer: int,
        admits: Callable[[int], bool] | None,
    ) -> tuple[tuple[tuple[float, int], ...], TraversalStats]:
        visited: set[int] = set()
        beam: list[tuple[float, int]] = []
        results: list[tuple[float, int]] = []
        bridges = 0
        hops = 0
        for node in sorted(entry_points):
            if node in visited or node not in self._values:  # noqa: SLF001
                continue
            visited.add(node)
            score = scorer(node)
            _insert_ranked(beam, score, node)
            if admits is None or admits(node):
                _insert_ranked(results, score, node, ef)
            else:
                bridges += 1
        while beam:
            score, node = beam.pop(0)
            if len(results) >= ef and score < results[-1][0]:
                break
            hops += 1
            for neighbour in sorted(self.neighbours_of(node, layer)):
                if neighbour in visited:
                    continue
                visited.add(neighbour)
                neighbour_score = scorer(neighbour)
                if len(results) < ef or neighbour_score > results[-1][0]:
                    _insert_ranked(beam, neighbour_score, neighbour)
                    if admits is None or admits(neighbour):
                        _insert_ranked(results, neighbour_score, neighbour, ef)
                    else:
                        bridges += 1
        return tuple(results), TraversalStats(
            visited=len(visited),
            bridges=bridges,
            admitted=len(results),
            hops=hops,
            exhaustive=len(visited) >= len(self._values),  # noqa: SLF001
        )


class _TiedMath(PureVectorMath):
    """Give every pair the same finite score so only the node-id tie rule decides."""

    def prepare(
        self, query: Sequence[float], metric: DistanceMetric
    ) -> Callable[[Sequence[float]], float]:
        del query, metric
        return lambda _values: 0.5


def _values(node: int, dimension: int = 8) -> tuple[float, ...]:
    return tuple(
        (((node + 3) * (axis + 5)) % 23 - 11) / 11.0 for axis in range(dimension)
    )


def _pair(*, tied: bool = False) -> tuple[HnswGraph, HnswGraph]:
    math = _TiedMath() if tied else PureVectorMath()
    arguments = {
        "seed": 0xD15,
        "neighbours": 4,
        "ef_construction": 13,
    }
    current = HnswGraph(math, DistanceMetric.COSINE, **arguments)
    legacy = _LegacyHnswGraph(math, DistanceMetric.COSINE, **arguments)
    for node in range(1, 97):
        values = _values(node)
        current.insert(node, values)
        legacy.insert(node, values)
    return current, legacy


def _shape(graph: HnswGraph) -> tuple[object, ...]:
    return (
        graph.entry_point,
        graph.top_level,
        graph.chain(),
        tuple(
            (
                node,
                graph.level_of(node),
                tuple(
                    graph.neighbours_of(node, layer)
                    for layer in range(graph.level_of(node) + 1)
                ),
            )
            for node in graph.nodes()
        ),
    )


def test_heap_frontiers_and_single_sort_trim_preserve_complete_graph_shape() -> None:
    current, legacy = _pair()

    assert _shape(current) == _shape(legacy)


@pytest.mark.parametrize(
    ("ef", "selector"),
    [
        (17, None),
        (17, lambda node: node % 19 == 0),
        (96, lambda node: node % 7 != 0),
    ],
)
def test_heap_frontiers_match_legacy_ranking_and_stats(
    ef: int, selector: Callable[[int], bool] | None
) -> None:
    current, legacy = _pair()
    query = _values(211)

    actual = current.search(query, ef, selector)
    expected = legacy.search(query, ef, selector)

    assert actual == expected
    if ef >= len(current):
        assert actual[1].exhaustive is True


def test_heap_frontiers_preserve_ties_and_callback_order() -> None:
    current, legacy = _pair(tied=True)
    assert _shape(current) == _shape(legacy)

    def run(graph: HnswGraph) -> tuple[object, list[tuple[str, int]]]:
        events: list[tuple[str, int]] = []

        def score(node: int) -> float:
            events.append(("score", node))
            return 0.5

        def admits(node: int) -> bool:
            events.append(("admit", node))
            return node % 5 == 0

        answer = graph._search_layer_counted(  # noqa: SLF001 - exact callback oracle
            score, (23, 5, 11, 2, 17), 7, 0, admits
        )
        return answer, events

    actual, actual_events = run(current)
    expected, expected_events = run(legacy)

    assert actual == expected
    assert actual_events == expected_events


def test_initial_entry_points_replace_by_total_ranking_when_they_outnumber_ef() -> None:
    current, legacy = _pair()
    scores = {
        1: 0.1,
        2: 0.2,
        3: 0.3,
        4: 0.9,
        5: 0.9,
        6: 0.8,
    }

    def run(graph: HnswGraph) -> tuple[tuple[tuple[float, int], ...], TraversalStats]:
        # This deliberately asks a layer no node reaches: the answer is decided exclusively by
        # the >ef initial frontier, including replacement by later higher scores and an equal-
        # score tie. It isolates the initial rule from expansion's strict score-only gate.
        return graph._search_layer_counted(  # noqa: SLF001 - boundary under test
            scores.__getitem__, (6, 5, 4, 3, 2, 1), 3, 99, None
        )

    actual = run(current)

    assert actual == run(legacy)
    assert actual[0] == ((0.9, 4), (0.9, 5), (0.8, 6))


@pytest.mark.parametrize(
    ("node_count", "ef", "filtered", "uses_heap"),
    [
        (4095, 7, True, False),
        (4096, 7, False, False),
        (4096, 7, True, True),
        (4096, 4096, False, True),
    ],
)
def test_heap_frontier_dispatch_boundary_preserves_legacy_order_and_callbacks(
    monkeypatch: pytest.MonkeyPatch,
    node_count: int,
    ef: int,
    filtered: bool,
    uses_heap: bool,
) -> None:
    arguments = {"seed": 7, "neighbours": 4, "ef_construction": 8}
    current = HnswGraph(PureVectorMath(), DistanceMetric.DOT, **arguments)
    legacy = _LegacyHnswGraph(PureVectorMath(), DistanceMetric.DOT, **arguments)
    entry_points = tuple(range(1, node_count + 1))
    # Layer 99 has no links, isolating dispatch without paying to construct thousands of nodes.
    # The direct private setup is safe here because membership is the only graph fact this layer
    # reads; both implementations receive byte-for-byte equivalent state.
    for graph in (current, legacy):
        graph._values.update(  # noqa: SLF001 - isolated frontier differential
            {node: (float(node),) for node in entry_points}
        )
    heap_pushes: list[tuple[float, int, float]] = []
    original_heappush = hnsw_module.heappush

    def recording_heappush(
        frontier: list[tuple[float, int, float]], item: tuple[float, int, float]
    ) -> None:
        heap_pushes.append(item)
        original_heappush(frontier, item)

    monkeypatch.setattr(hnsw_module, "heappush", recording_heappush)

    def run(graph: HnswGraph) -> tuple[object, list[tuple[str, int]]]:
        events: list[tuple[str, int]] = []

        def score(node: int) -> float:
            events.append(("score", node))
            return float(node // 2)

        def admits(node: int) -> bool:
            events.append(("admit", node))
            return node % 257 == 0

        answer = graph._search_layer_counted(  # noqa: SLF001 - dispatch under test
            score, entry_points, ef, 99, admits if filtered else None
        )
        return answer, events

    actual, actual_events = run(current)
    expected, expected_events = run(legacy)

    assert actual == expected
    assert actual_events == expected_events
    assert bool(heap_pushes) is uses_heap
    assert actual[1].exhaustive is True


def test_large_filtered_heap_preserves_expansion_callback_order() -> None:
    arguments = {"seed": 7, "neighbours": 4, "ef_construction": 8}
    current = HnswGraph(PureVectorMath(), DistanceMetric.DOT, **arguments)
    legacy = _LegacyHnswGraph(PureVectorMath(), DistanceMetric.DOT, **arguments)
    values = {node: (float(node),) for node in range(1, 4097)}
    first_hop = list(range(2, 258))
    for graph in (current, legacy):
        graph._values.update(values)  # noqa: SLF001 - isolated traversal differential
        graph._links[0][1] = first_hop  # noqa: SLF001 - intentional synthetic graph
        for node in first_hop:
            graph._links[0][node] = [1000 + node]  # noqa: SLF001

    def run(graph: HnswGraph) -> tuple[object, list[tuple[str, int]]]:
        events: list[tuple[str, int]] = []

        def score(node: int) -> float:
            events.append(("score", node))
            return float((node * 17) % 31)

        def admits(node: int) -> bool:
            events.append(("admit", node))
            return node % 257 == 0

        answer = graph._search_layer_counted(  # noqa: SLF001 - heap expansion under test
            score, (1,), 17, 0, admits
        )
        return answer, events

    actual, actual_events = run(current)
    expected, expected_events = run(legacy)

    assert actual == expected
    assert actual_events == expected_events
