"""A ranking measures its query once and answers exactly as the pairwise doors do (VEC-1).

``top_k`` under cosine hoists the norm of the query out of the candidate loop. Three things have
to survive that, and each is asserted here rather than assumed. The scores and the order are
IDENTICAL to a cosine computed pair by pair with the documented formula -- bit for bit, not to a
tolerance, because the same terms reach the same correctly rounded sum. A ranking with no
candidates never examines the query. And every refusal keeps its place, its type and its reason:
the neighbour count and the metric are still checked first, a query that cannot be measured is
still refused at the first candidate, and a candidate that cannot be scored is still refused in
its turn with what the pairwise door says about it.

This module imports nothing optional (TR-6). The accelerated adapter is held to the same rules
next door, in ``test_topk_query_invariant_numpy.py``.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence

import pytest

from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxVectorValidationError
from okto_grafx.domain.ports.vectormath import DistanceMetric, VectorMath

from .conftest import seeded_vectors

CORPUS_SIZE: int = 200
DIMENSION: int = 48
CORPUS_SEED: int = 0x5EC1

Candidate = tuple[int, Sequence[float]]


@pytest.fixture
def adapter() -> PureVectorMath:
    """Return the oracle, which is the adapter this module holds to its own pairwise doors."""
    return PureVectorMath()


def _reference_score(
    a: Sequence[float], b: Sequence[float], metric: DistanceMetric
) -> float:
    """Return the score the documented formulas give, written without the adapter."""
    if metric is DistanceMetric.COSINE:
        length_a = math.sqrt(math.fsum(x * x for x in a))
        length_b = math.sqrt(math.fsum(y * y for y in b))
        if length_a == 0.0 or length_b == 0.0:
            return 0.0
        return math.fsum(x * y for x, y in zip(a, b)) / (length_a * length_b)
    if metric is DistanceMetric.DOT:
        return math.fsum(x * y for x, y in zip(a, b))
    return -math.sqrt(math.fsum((x - y) * (x - y) for x, y in zip(a, b)))


def _reference_ranking(
    query: Sequence[float],
    candidates: Sequence[Candidate],
    k: int,
    metric: DistanceMetric,
) -> list[tuple[int, float]]:
    """Return the ranking the port documents: descending score, ascending id, at most k."""
    scored = [
        (identifier, _reference_score(query, values, metric))
        for identifier, values in candidates
    ]
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:k]


def adversarial_corpus() -> tuple[tuple[float, ...], list[Candidate]]:
    """Return a query and candidates that exercise every tie and edge the ranking has.

    Beyond the seeded corpus: the query itself, a scaled copy of it (a cosine tie with a
    different dot product), an exact duplicate of a corpus member (a full tie broken by id), a
    zero vector (the special case of cosine), and a negated member.
    """
    corpus = seeded_vectors(CORPUS_SIZE, DIMENSION, CORPUS_SEED)
    query = corpus[0]
    extras: list[tuple[float, ...]] = [
        query,
        tuple(component * 2.5 for component in query),
        corpus[17],
        tuple(0.0 for _ in range(DIMENSION)),
        tuple(-component for component in corpus[3]),
    ]
    members = corpus[1:] + extras
    return query, [
        (identifier + 1, values) for identifier, values in enumerate(members)
    ]


class _CountingCandidates:
    """A candidate source that records how many candidates were pulled before a refusal."""

    def __init__(self, candidates: Sequence[Candidate]) -> None:
        self._candidates = candidates
        self.pulled = 0

    def __iter__(self) -> Iterator[Candidate]:
        for candidate in self._candidates:
            self.pulled += 1
            yield candidate

    def __len__(self) -> int:
        return len(self._candidates)


def check_ranking_matches_the_reference(adapter: VectorMath) -> None:
    """Every metric and every k answer exactly what the documented formula gives."""
    query, candidates = adversarial_corpus()
    for metric in DistanceMetric:
        for k in (1, 7, len(candidates), len(candidates) + 50):
            assert adapter.top_k(query, candidates, k, metric) == _reference_ranking(
                query, candidates, k, metric
            )


def check_ranking_matches_the_pairwise_door(adapter: VectorMath) -> None:
    """The hoisted path scores exactly what the adapter's own ``cosine`` scores, pair by pair."""
    query, candidates = adversarial_corpus()
    ranked = adapter.top_k(query, candidates, len(candidates), DistanceMetric.COSINE)
    pairwise = {
        identifier: adapter.cosine(query, values) for identifier, values in candidates
    }
    assert [
        (identifier, pairwise[identifier]) for identifier, _score in ranked
    ] == ranked
    assert sorted(identifier for identifier, _score in ranked) == sorted(pairwise)


def check_no_candidates_never_examines_the_query(adapter: VectorMath) -> None:
    """An empty ranking is empty whatever the query is, because nothing is compared to it."""
    for query in ((math.nan, 1.0), (math.inf, 0.0), ("not", "numbers"), ()):
        assert adapter.top_k(query, [], 3, DistanceMetric.COSINE) == []  # type: ignore[arg-type]


def check_count_and_metric_are_refused_before_the_query_is_measured(
    adapter: VectorMath,
) -> None:
    """The guards on k and on the metric still come first, ahead of anything about the query."""
    poisoned = (math.nan, 0.0)
    candidates = [(1, (1.0, 0.0))]
    with pytest.raises(GrafxConfigurationError) as failure:
        adapter.top_k(poisoned, candidates, 0, DistanceMetric.COSINE)
    assert failure.value.details["field"] == "k"
    with pytest.raises(GrafxConfigurationError) as failure:
        adapter.top_k(poisoned, candidates, 1, "cosine")  # type: ignore[arg-type]
    assert failure.value.details["field"] == "metric"


def check_an_unmeasurable_query_is_refused_at_the_first_candidate(
    adapter: VectorMath,
) -> None:
    """A query with a NaN is refused with the norm reason, once the first candidate is in hand."""
    source = _CountingCandidates([(1, (1.0, 0.0)), (2, (0.0, 1.0))])
    with pytest.raises(GrafxVectorValidationError) as failure:
        adapter.top_k((math.nan, 1.0), source, 1, DistanceMetric.COSINE)  # type: ignore[arg-type]
    assert failure.value.details["reason"] == "non_finite_result"
    assert failure.value.details["operation"] == "norm"
    assert source.pulled == 1


def _refusal(call, *args) -> tuple[type, dict[str, object]]:
    """Return the type and the details of the refusal a call raises."""
    with pytest.raises(GrafxVectorValidationError) as failure:
        call(*args)
    return type(failure.value), dict(failure.value.details)


def check_every_candidate_refusal_keeps_its_type_reason_and_place(
    adapter: VectorMath,
) -> None:
    """A candidate the pairwise door refuses is refused by the ranking with the same details.

    The cases cover the order of the checks inside one cosine: a non-finite candidate is
    refused by its norm before its length is compared, a finite one of the wrong length by the
    length check, and a zero vector of the wrong length by the length check the zero rule
    keeps. Two bad candidates in either order prove the loop still refuses the FIRST of them.
    """
    query = (1.0, 2.0, 3.0)
    good = (3, (0.5, 0.25, 0.125))
    cases: list[Candidate] = [
        (11, (math.nan, 1.0, 1.0)),
        (12, (1.0, 1.0)),
        (13, (math.nan, 1.0)),
        (14, (0.0, 0.0)),
        (15, (1e200, 1e200, 1e200)),
    ]
    for bad in cases:
        pairwise = _refusal(adapter.cosine, query, bad[1])
        ranked = _refusal(
            adapter.top_k, query, [good, bad, good], 3, DistanceMetric.COSINE
        )
        assert ranked == pairwise, bad
    mismatch, poisoned = cases[1], cases[0]
    first = _refusal(
        adapter.top_k, query, [mismatch, poisoned], 2, DistanceMetric.COSINE
    )
    assert first[1]["reason"] == "length_mismatch"
    second = _refusal(
        adapter.top_k, query, [poisoned, mismatch], 2, DistanceMetric.COSINE
    )
    assert second[1]["reason"] == "non_finite_result"


def test_the_ranking_matches_the_documented_formula_on_every_metric(
    adapter: PureVectorMath,
) -> None:
    check_ranking_matches_the_reference(adapter)


def test_the_cosine_ranking_matches_the_pairwise_door_exactly(
    adapter: PureVectorMath,
) -> None:
    check_ranking_matches_the_pairwise_door(adapter)


def test_a_ranking_of_no_candidates_never_examines_the_query(
    adapter: PureVectorMath,
) -> None:
    check_no_candidates_never_examines_the_query(adapter)


def test_the_neighbour_count_and_the_metric_are_refused_before_the_query_is_measured(
    adapter: PureVectorMath,
) -> None:
    check_count_and_metric_are_refused_before_the_query_is_measured(adapter)


def test_a_query_that_cannot_be_measured_is_refused_at_the_first_candidate(
    adapter: PureVectorMath,
) -> None:
    check_an_unmeasurable_query_is_refused_at_the_first_candidate(adapter)


def test_every_candidate_refusal_keeps_its_type_reason_and_place(
    adapter: PureVectorMath,
) -> None:
    check_every_candidate_refusal_keeps_its_type_reason_and_place(adapter)


def test_the_pairwise_doors_still_match_the_documented_formula(
    adapter: PureVectorMath,
) -> None:
    """The accumulation rewrite feeds the same terms to the same sum: dot and norm are exact."""
    query, candidates = adversarial_corpus()
    for _identifier, values in candidates:
        assert adapter.dot(query, values) == math.fsum(
            x * y for x, y in zip(query, values)
        )
        assert adapter.norm(values) == math.sqrt(math.fsum(x * x for x in values))
        assert adapter.cosine(query, values) == _reference_score(
            query, values, DistanceMetric.COSINE
        )
