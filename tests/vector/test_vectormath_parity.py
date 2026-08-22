"""The accelerated adapter must agree with the oracle (SPEC-VEC BR-7, AC-9, TR-6, IR-2).

This module needs the ``[accel]`` extra and says so with the marker the skip gate checks, so a
run without numpy attributes its own absence rather than vanishing (amendments A54, A54.1). The
oracle suite next door needs nothing at all, which is the other half of TR-6.

Agreement is asserted on two levels, because they can fail independently. Scores must agree
within ``SCORE_TOLERANCE``, which measures arithmetic. Rankings must be IDENTICAL, which measures
ordering, and the two are not the same claim: two implementations can agree on every score to
fifteen digits and still disagree on a tie. The ranking assertion is guarded by a precondition
that the corpus separates its scores by more than the tolerance, so a failure means the ordering
rule diverged rather than that the fixture was ambiguous (amendment A72).
"""

from __future__ import annotations

import math

import pytest

numpy = pytest.importorskip("numpy")

from okto_grafx.adapters.vectormath_numpy import (  # noqa: E402
    NUMPY_ADAPTER_NAME,
    NumpyVectorMath,
)
from okto_grafx.adapters.vectormath_pure import SCORE_TOLERANCE, PureVectorMath  # noqa: E402
from okto_grafx.domain.errors import (  # noqa: E402
    GrafxConfigurationError,
    GrafxVectorValidationError,
)
from okto_grafx.domain.ports.vectormath import DistanceMetric, VectorMath  # noqa: E402

from .conftest import seeded_vectors  # noqa: E402

pytestmark = pytest.mark.optional_dependency("numpy")

CORPUS_SIZE: int = 200
DIMENSION: int = 48
CORPUS_SEED: int = 0xC0FFEE


@pytest.fixture
def oracle() -> PureVectorMath:
    """Return the pure adapter, which decides what is correct."""
    return PureVectorMath()


@pytest.fixture
def accelerator() -> NumpyVectorMath:
    """Return the numpy adapter, which is judged against the oracle."""
    return NumpyVectorMath()


@pytest.fixture
def corpus() -> list[tuple[float, ...]]:
    """Return the seeded corpus both adapters are measured on."""
    return seeded_vectors(CORPUS_SIZE, DIMENSION, CORPUS_SEED)


def _agree(left: float, right: float) -> bool:
    """Return True when two scores are within the declared tolerance of each other."""
    scale = max(abs(left), abs(right), 1.0)
    return abs(left - right) <= SCORE_TOLERANCE * scale


def test_the_accelerator_satisfies_the_frozen_port(accelerator: NumpyVectorMath) -> None:
    """The accelerated adapter is a VectorMath by the same protocol as the oracle."""
    assert isinstance(accelerator, VectorMath)
    assert accelerator.name == NUMPY_ADAPTER_NAME == "numpy"


@pytest.mark.parametrize("metric", list(DistanceMetric))
def test_every_score_agrees_with_the_oracle_within_the_declared_tolerance(
    oracle: PureVectorMath,
    accelerator: NumpyVectorMath,
    corpus: list[tuple[float, ...]],
    metric: DistanceMetric,
) -> None:
    """Score agreement is arithmetic, measured pair by pair over the whole seeded corpus."""
    query = corpus[0]
    worst = 0.0
    for values in corpus:
        expected = oracle.score(query, values, metric)
        actual = accelerator.score(query, values, metric)
        worst = max(worst, abs(expected - actual) / max(abs(expected), 1.0))
        assert _agree(expected, actual), (metric, expected, actual)
    assert worst <= SCORE_TOLERANCE


@pytest.mark.parametrize("metric", list(DistanceMetric))
def test_the_rankings_are_identical_not_merely_close(
    oracle: PureVectorMath,
    accelerator: NumpyVectorMath,
    corpus: list[tuple[float, ...]],
    metric: DistanceMetric,
) -> None:
    """Ranking agreement is ordering, and it needs the corpus to separate its scores first."""
    query = corpus[0]
    candidates = list(enumerate(corpus))
    scores = sorted(oracle.score(query, values, metric) for values in corpus)
    gaps = [second - first for first, second in zip(scores, scores[1:]) if second != first]
    assert gaps, "the corpus produced no distinct scores at all"
    assert min(gaps) > SCORE_TOLERANCE * 10, "the corpus cannot distinguish an ordering defect"
    expected = oracle.top_k(query, candidates, 25, metric)
    actual = accelerator.top_k(query, candidates, 25, metric)
    assert [identifier for identifier, _score in expected] == [
        identifier for identifier, _score in actual
    ]
    for (_left_id, left), (_right_id, right) in zip(expected, actual):
        assert _agree(left, right)


def test_the_tie_rule_is_the_same_in_both_adapters(
    oracle: PureVectorMath, accelerator: NumpyVectorMath
) -> None:
    """Identical vectors tie exactly, and both adapters break the tie by ascending id."""
    candidates = [(9, (1.0, 0.0)), (3, (1.0, 0.0)), (5, (1.0, 0.0))]
    expected = oracle.top_k((1.0, 0.0), candidates, 3, DistanceMetric.COSINE)
    actual = accelerator.top_k((1.0, 0.0), candidates, 3, DistanceMetric.COSINE)
    assert [identifier for identifier, _score in expected] == [3, 5, 9]
    assert expected == actual


def test_both_adapters_answer_zero_for_a_vector_of_zero_length(
    oracle: PureVectorMath, accelerator: NumpyVectorMath
) -> None:
    """numpy would return a NaN here; the adapter is required not to."""
    zero = (0.0, 0.0, 0.0)
    other = (1.0, 2.0, 3.0)
    assert oracle.cosine(zero, other) == 0.0
    assert accelerator.cosine(zero, other) == 0.0
    assert accelerator.normalize(zero) == (0.0, 0.0, 0.0)


def test_both_adapters_refuse_a_non_finite_result(accelerator: NumpyVectorMath) -> None:
    """A NaN never enters a ranking through the accelerator either."""
    with pytest.raises(GrafxVectorValidationError) as failure:
        accelerator.dot((float("nan"), 1.0), (1.0, 1.0))
    assert failure.value.details["reason"] == "non_finite_result"


def test_both_adapters_refuse_mismatched_lengths(accelerator: NumpyVectorMath) -> None:
    """A ragged pair is a caller error in both, with the same reason detail."""
    with pytest.raises(GrafxVectorValidationError) as failure:
        accelerator.dot((1.0, 2.0), (1.0,))
    assert failure.value.details["reason"] == "length_mismatch"


def test_the_accelerator_refuses_an_argument_numpy_would_have_reshaped(
    accelerator: NumpyVectorMath,
) -> None:
    """A nested sequence is a caller error, not a two-dimensional array."""
    with pytest.raises(GrafxVectorValidationError) as failure:
        accelerator.norm([[1.0, 2.0], [3.0, 4.0]])
    assert failure.value.details["reason"] == "not_a_sequence"


def test_the_accelerator_refuses_a_neighbour_count_below_one(
    accelerator: NumpyVectorMath,
) -> None:
    """Both adapters share the refusal, because both import the same guard."""
    with pytest.raises(GrafxConfigurationError):
        accelerator.top_k((1.0,), [(1, (1.0,))], 0, DistanceMetric.DOT)


def test_a_float32_corpus_scores_the_same_in_both_adapters(
    oracle: PureVectorMath, accelerator: NumpyVectorMath
) -> None:
    """A caller holding float32 data must not silently lower the precision of the arithmetic."""
    values = numpy.asarray([0.1, 0.2, 0.3, 0.4], dtype=numpy.float32)
    query = numpy.asarray([0.5, 0.25, 0.125, 0.0625], dtype=numpy.float32)
    expected = oracle.dot([float(x) for x in query], [float(x) for x in values])
    assert _agree(expected, accelerator.dot(query, values))


def test_the_accelerator_holds_no_state_between_calls(accelerator: NumpyVectorMath) -> None:
    """A shared scratch array between two searches is the defect this rules out."""
    assert NumpyVectorMath.__slots__ == ()
    assert not hasattr(accelerator, "__dict__")


def test_the_norm_of_a_unit_vector_agrees_to_the_tolerance(
    oracle: PureVectorMath, accelerator: NumpyVectorMath, corpus: list[tuple[float, ...]]
) -> None:
    """Normalization is the operation a storage dtype decision leans on; it must agree too."""
    for values in corpus[:20]:
        left = oracle.normalize(values)
        right = accelerator.normalize(values)
        assert len(left) == len(right)
        for one, other in zip(left, right):
            assert math.isclose(one, other, rel_tol=SCORE_TOLERANCE, abs_tol=SCORE_TOLERANCE)


def test_the_tolerance_does_not_promise_agreement_on_cancelling_input(
    oracle: PureVectorMath, accelerator: NumpyVectorMath
) -> None:
    """The bound describes embedding-shaped data, and this is where it does not hold (A85).

    ``SCORE_TOLERANCE`` used to claim it was "far below any difference that could reorder a
    ranking". That is false on catastrophically cancelling input: the oracle sums exactly and the
    accelerator sums pairwise, so the two disagree by the whole magnitude of the answer and the
    same candidate is ranked first by one and last by the other. The sentence was corrected and
    this test is what the correction points at.
    """
    left = (1e16, 1.0, -1e16)
    ones = (1.0, 1.0, 1.0)
    assert oracle.dot(left, ones) == 1.0
    assert accelerator.dot(left, ones) == 0.0
    candidates = [(1, left), (2, (0.5, 0.0, 0.0))]
    assert [identifier for identifier, _score in oracle.top_k(ones, candidates, 2, DistanceMetric.DOT)] == [1, 2]
    assert [identifier for identifier, _score in accelerator.top_k(ones, candidates, 2, DistanceMetric.DOT)] == [2, 1]


def test_both_adapters_refuse_a_sum_that_leaves_double_range(
    oracle: PureVectorMath, accelerator: NumpyVectorMath
) -> None:
    """The overflow the oracle used to leak as an OverflowError is refused by both, alike."""
    poisoned = (1e308, 1e308, -1e308)
    ones = (1.0, 1.0, 1.0)
    for adapter in (oracle, accelerator):
        with pytest.raises(GrafxVectorValidationError) as failure:
            adapter.dot(poisoned, ones)
        assert failure.value.details["reason"] == "non_finite_result"


def test_both_adapters_refuse_many_large_squares_alike(
    oracle: PureVectorMath, accelerator: NumpyVectorMath
) -> None:
    """The overflow the battery found in ``norm`` must refuse the same way in both adapters."""
    components = tuple(math.sqrt(1e307) for _ in range(100))
    for adapter in (oracle, accelerator):
        with pytest.raises(GrafxVectorValidationError) as failure:
            adapter.norm(components)
        assert failure.value.details["reason"] == "non_finite_result"
