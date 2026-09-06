"""A query prepared once scores exactly what the pairwise door scores (VEC-4).

``PreparedVectorMath`` is an optional capability of the math port: ``prepare`` returns a scorer
with everything that depends on the query alone computed once. Three things are held here. The
scorer answers, bit for bit, what ``score`` answers for the same pair under every metric, and
refuses what it refuses with the same details -- lazily, so that a query nothing is scored
against refuses nothing. The graph uses the capability when the adapter declares it and scores
through ``score`` otherwise, and the two paths build the same graph, return the same rankings
and count the same traversal. And the graph prepares a query once per traversal, never per node.

This module imports nothing optional (TR-6); the accelerated adapter is held to the same rules
in ``test_prepared_scoring_numpy.py``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import GrafxVectorValidationError
from okto_grafx.domain.ports.vectormath import (
    DistanceMetric,
    PreparedCosineVectorMath,
    PreparedVectorMath,
    VectorMath,
)
from okto_grafx.domain.vector.hnsw import HnswGraph

from .conftest import seeded_vectors
from .test_topk_query_invariant import adversarial_corpus

DIMENSION: int = 16
CORPUS_SIZE: int = 120
GRAPH_SEED: int = 0x4EC4


class ScoreOnlyMath:
    """A host adapter that implements the port and nothing more: no ``prepare``."""

    def __init__(self, inner: VectorMath) -> None:
        self._inner = inner
        self.scores = 0

    @property
    def name(self) -> str:
        return "score-only"

    def dot(self, a: Sequence[float], b: Sequence[float]) -> float:
        return self._inner.dot(a, b)

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float:
        return self._inner.cosine(a, b)

    def euclidean(self, a: Sequence[float], b: Sequence[float]) -> float:
        return self._inner.euclidean(a, b)

    def norm(self, a: Sequence[float]) -> float:
        return self._inner.norm(a)

    def normalize(self, a: Sequence[float]) -> tuple[float, ...]:
        return self._inner.normalize(a)

    def score(
        self, a: Sequence[float], b: Sequence[float], metric: DistanceMetric
    ) -> float:
        self.scores += 1
        return self._inner.score(a, b, metric)

    def top_k(self, query, candidates, k, metric):
        return self._inner.top_k(query, candidates, k, metric)


class CountingPreparedMath(ScoreOnlyMath):
    """The same host adapter, declaring the capability and counting how often it is asked."""

    def __init__(self, inner: PureVectorMath) -> None:
        super().__init__(inner)
        self.prepared = 0

    def prepare(self, query: Sequence[float], metric: DistanceMetric):
        self.prepared += 1
        return self._inner.prepare(query, metric)


def _graph(
    math: VectorMath, corpus: list[tuple[float, ...]], metric: DistanceMetric
) -> HnswGraph:
    graph = HnswGraph(math, metric, seed=GRAPH_SEED)
    for index, values in enumerate(corpus):
        graph.insert(index + 1, values)
    return graph


def _adjacency(graph: HnswGraph, count: int) -> list[dict[int, tuple[int, ...]]]:
    layers: list[dict[int, tuple[int, ...]]] = []
    for layer in range(graph.top_level + 1):
        layers.append(
            {
                node: tuple(graph.neighbours_of(node, layer))
                for node in range(1, count + 1)
            }
        )
    return layers


def check_prepared_scorer_answers_exactly_what_score_answers(
    adapter: PreparedVectorMath,
) -> None:
    query, candidates = adversarial_corpus()
    zero = tuple(0.0 for _ in query)
    for metric in DistanceMetric:
        for probe in (query, zero):
            scorer = adapter.prepare(probe, metric)
            for _identifier, values in candidates:
                assert scorer(values) == adapter.score(probe, values, metric)  # type: ignore[attr-defined]


def check_preparing_an_unmeasurable_query_refuses_nothing_until_it_is_used(
    adapter: PreparedVectorMath,
) -> None:
    poisoned = (math.nan, 1.0)
    for metric in DistanceMetric:
        scorer = adapter.prepare(poisoned, metric)
        with pytest.raises(GrafxVectorValidationError) as pairwise:
            adapter.score(poisoned, (1.0, 0.0), metric)  # type: ignore[attr-defined]
        with pytest.raises(GrafxVectorValidationError) as prepared:
            scorer((1.0, 0.0))
        assert prepared.value.details == pairwise.value.details


def check_a_prepared_scorer_refuses_each_vector_as_the_pairwise_door_does(
    adapter: PreparedVectorMath,
) -> None:
    query = (1.0, 2.0, 3.0)
    good = (0.5, 0.25, 0.125)
    cases = [
        (math.nan, 1.0, 1.0),
        (1.0, 1.0),
        (math.nan, 1.0),
        (0.0, 0.0),
        (1e200, 1e200, 1e200),
    ]
    refused = 0
    for metric in DistanceMetric:
        scorer = adapter.prepare(query, metric)
        assert scorer(good) == adapter.score(query, good, metric)  # type: ignore[attr-defined]
        for bad in cases:
            # The same outcome, whichever it is: the overflowing vector is refused under cosine
            # and Euclidean, whose squares leave double range, and scored under dot, whose
            # products do not. Neither door decides that here; they only have to agree.
            assert _outcome(lambda: adapter.score(query, bad, metric)) == _outcome(  # type: ignore[attr-defined]
                lambda: scorer(bad)
            ), (metric, bad)
            refused += _outcome(lambda: scorer(bad))[0] == "refused"
        assert scorer(good) == adapter.score(query, good, metric)  # type: ignore[attr-defined]
    assert refused == len(cases) * len(DistanceMetric) - 1


def _outcome(call) -> tuple[str, object]:
    """Return what a call did: the details of its refusal, or the value it answered."""
    try:
        return "answered", call()
    except GrafxVectorValidationError as failure:
        return "refused", dict(failure.details)


def test_the_oracle_declares_the_capability() -> None:
    assert isinstance(PureVectorMath(), PreparedVectorMath)
    assert isinstance(PureVectorMath(), PreparedCosineVectorMath)
    assert not isinstance(ScoreOnlyMath(PureVectorMath()), PreparedVectorMath)


def test_a_prepared_scorer_answers_exactly_what_score_answers() -> None:
    check_prepared_scorer_answers_exactly_what_score_answers(PureVectorMath())


def test_preparing_an_unmeasurable_query_refuses_nothing_until_it_is_used() -> None:
    check_preparing_an_unmeasurable_query_refuses_nothing_until_it_is_used(
        PureVectorMath()
    )


def test_a_prepared_scorer_refuses_each_vector_as_the_pairwise_door_does() -> None:
    check_a_prepared_scorer_refuses_each_vector_as_the_pairwise_door_does(
        PureVectorMath()
    )


def test_pure_prepared_cosine_reuses_only_successfully_measured_norms() -> None:
    class CountingPureMath(PureVectorMath):
        def __init__(self) -> None:
            self.norm_calls = 0

        def norm(self, values: Sequence[float]) -> float:
            self.norm_calls += 1
            return super().norm(values)

    math_ = CountingPureMath()
    unused, _unused_cached = math_.prepare_cosine_with_norm((float("nan"), 0.0))
    measured, cached = math_.prepare_cosine_with_norm((1.0, 2.0, 3.0))
    candidate = (4.0, 5.0, 6.0)

    assert callable(unused)
    first, candidate_norm = measured(candidate)
    assert first == math_.score((1.0, 2.0, 3.0), candidate, DistanceMetric.COSINE)
    calls_after_oracle = math_.norm_calls
    assert cached(candidate, candidate_norm) == first
    assert math_.norm_calls == calls_after_oracle


def test_pure_hnsw_norm_cache_is_bound_to_the_node_generation() -> None:
    graph = HnswGraph(PureVectorMath(), DistanceMetric.COSINE, seed=17)
    graph.insert(7, (1.0, 0.0))

    assert graph.search((1.0, 0.0), ef=1)[0] == ((1.0, 7),)
    retained, first_norm = graph._norms[7]  # noqa: SLF001 - generation fence
    assert retained is graph._values[7]  # noqa: SLF001 - generation fence
    assert first_norm == 1.0

    graph.remove(7)
    graph.insert(7, (0.0, 2.0))
    assert graph.search((0.0, 1.0), ef=1)[0] == ((1.0, 7),)
    retained, second_norm = graph._norms[7]  # noqa: SLF001 - generation fence
    assert retained is graph._values[7]  # noqa: SLF001 - generation fence
    assert second_norm == 2.0


@pytest.mark.parametrize("metric", list(DistanceMetric))
def test_a_host_adapter_without_the_capability_builds_and_searches_the_same_graph(
    metric: DistanceMetric,
) -> None:
    """The fallback path and the prepared path are one graph, one ranking and one traversal."""
    corpus = seeded_vectors(CORPUS_SIZE, DIMENSION, 0x1234)
    corpus[40] = corpus[7]  # a full tie, broken by node id on both paths
    prepared = _graph(PureVectorMath(), corpus, metric)
    fallback = _graph(ScoreOnlyMath(PureVectorMath()), corpus, metric)
    assert _adjacency(prepared, CORPUS_SIZE) == _adjacency(fallback, CORPUS_SIZE)
    queries = seeded_vectors(5, DIMENSION, 0x77) + [corpus[7]]
    for query in queries:
        for ef in (4, 32, 512):
            for admits in (None, lambda node: node % 3 == 0):
                assert prepared.search(query, ef, admits) == fallback.search(
                    query, ef, admits
                )
    for node in (3, 7, 40, 99):
        prepared.remove(node)
        fallback.remove(node)
    assert _adjacency(prepared, CORPUS_SIZE) == _adjacency(fallback, CORPUS_SIZE)
    for query in queries:
        assert prepared.search(query, 64) == fallback.search(query, 64)


def test_the_graph_prepares_a_query_once_per_search_and_never_scores_pairwise() -> None:
    corpus = seeded_vectors(CORPUS_SIZE, DIMENSION, 0x1234)
    math_ = CountingPreparedMath(PureVectorMath())
    graph = _graph(math_, corpus, DistanceMetric.COSINE)
    assert math_.scores == 0
    math_.prepared = 0
    ranked, stats = graph.search(corpus[5], 64)
    assert math_.prepared == 1
    assert math_.scores == 0
    assert stats.visited > 1 and ranked[0][1] == 6


def test_an_insert_prepares_its_vector_and_never_scores_pairwise() -> None:
    corpus = seeded_vectors(CORPUS_SIZE, DIMENSION, 0x1234)
    math_ = CountingPreparedMath(PureVectorMath())
    graph = _graph(math_, corpus, DistanceMetric.COSINE)
    math_.prepared = 0
    graph.insert(CORPUS_SIZE + 1, seeded_vectors(1, DIMENSION, 0x99)[0])
    assert math_.prepared >= 1
    assert math_.scores == 0


def test_a_query_that_cannot_be_measured_is_refused_where_it_always_was() -> None:
    """Empty graph: nothing is scored, nothing is refused. Otherwise: the norm refusal."""
    poisoned = tuple([math.nan] + [0.0] * (DIMENSION - 1))
    empty = HnswGraph(PureVectorMath(), DistanceMetric.COSINE, seed=GRAPH_SEED)
    assert empty.search(poisoned, 8) == ((), empty.search(poisoned, 8)[1])
    graph = _graph(
        PureVectorMath(), seeded_vectors(10, DIMENSION, 0x1234), DistanceMetric.COSINE
    )
    with pytest.raises(GrafxVectorValidationError) as failure:
        graph.search(poisoned, 8)
    assert failure.value.details["reason"] == "non_finite_result"
    assert failure.value.details["operation"] == "norm"
