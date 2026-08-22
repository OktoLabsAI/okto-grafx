"""The pure vector math adapter is the correctness oracle (SPEC-VEC FR-7, BR-7, TR-6).

Every rule an accelerated adapter must obey is stated here first, against the implementation
that ships with no optional dependency at all. The module deliberately imports nothing optional,
so it is the suite CONTRACT TR-6 requires to pass on an installation with no extras.
"""

from __future__ import annotations

import math

import pytest

from okto_grafx.adapters.vectormath_pure import (
    PURE_ADAPTER_NAME,
    SCORE_TOLERANCE,
    PureVectorMath,
)
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxVectorValidationError
from okto_grafx.domain.ports.vectormath import DistanceMetric, VectorMath

from .conftest import seeded_vectors


@pytest.fixture
def oracle() -> PureVectorMath:
    """Return the pure adapter under test."""
    return PureVectorMath()


def test_the_oracle_satisfies_the_frozen_port(oracle: PureVectorMath) -> None:
    """The adapter is a VectorMath by the runtime-checkable protocol of CONTRACT 4.6."""
    assert isinstance(oracle, VectorMath)
    assert oracle.name == PURE_ADAPTER_NAME == "pure"


def test_the_three_metrics_compute_what_they_are_named(oracle: PureVectorMath) -> None:
    """Dot, cosine and Euclidean are the textbook quantities, to the last bit."""
    a = (3.0, 4.0)
    b = (4.0, 3.0)
    assert oracle.dot(a, b) == 24.0
    assert oracle.norm(a) == 5.0
    assert oracle.euclidean(a, b) == pytest.approx(math.sqrt(2.0), abs=0.0, rel=1e-15)
    assert oracle.cosine(a, b) == pytest.approx(24.0 / 25.0, abs=0.0, rel=1e-15)


def test_higher_is_better_for_every_metric(oracle: PureVectorMath) -> None:
    """Euclidean is negated so one ranking routine serves all three (CONTRACT 4.6)."""
    query = (1.0, 0.0)
    near = (0.9, 0.1)
    far = (-1.0, 0.0)
    for metric in DistanceMetric:
        assert oracle.score(query, near, metric) > oracle.score(query, far, metric), metric
    assert oracle.score(query, near, DistanceMetric.EUCLIDEAN) == pytest.approx(
        -oracle.euclidean(query, near)
    )


def test_normalize_returns_a_unit_vector(oracle: PureVectorMath) -> None:
    """A normalized vector has length one within the precision of a double."""
    scaled = oracle.normalize((3.0, 4.0))
    assert scaled == pytest.approx((0.6, 0.8))
    assert oracle.norm(scaled) == pytest.approx(1.0, abs=1e-15)


def test_a_vector_of_zero_length_has_a_cosine_of_zero_and_not_a_nan(
    oracle: PureVectorMath,
) -> None:
    """The obvious expression divides by zero here; the documented answer is 0.0."""
    result = oracle.cosine((0.0, 0.0, 0.0), (1.0, 2.0, 3.0))
    assert result == 0.0
    assert not math.isnan(result)


def test_normalizing_a_vector_of_zero_length_returns_it_unchanged(
    oracle: PureVectorMath,
) -> None:
    """A vector with no direction has no direction to preserve, and no NaN either."""
    assert oracle.normalize((0.0, 0.0)) == (0.0, 0.0)


def test_a_nan_component_is_refused_rather_than_ranked(oracle: PureVectorMath) -> None:
    """The single guard fires on the result, which a NaN input always poisons."""
    with pytest.raises(GrafxVectorValidationError) as failure:
        oracle.dot((float("nan"), 1.0), (1.0, 1.0))
    assert failure.value.details["reason"] == "non_finite_result"
    assert failure.value.code == "vector_validation"


@pytest.mark.parametrize(
    "operation",
    ["dot", "cosine", "euclidean", "norm", "normalize"],
)
def test_every_operation_refuses_a_non_finite_result(
    oracle: PureVectorMath, operation: str
) -> None:
    """No door of the oracle can hand a NaN back, whichever one a caller reaches for."""
    poisoned = (float("nan"), 1.0)
    clean = (1.0, 1.0)
    call = getattr(oracle, operation)
    with pytest.raises(GrafxVectorValidationError):
        call(poisoned) if operation in {"norm", "normalize"} else call(poisoned, clean)


def test_an_overflow_of_finite_inputs_is_refused_too(oracle: PureVectorMath) -> None:
    """The same guard catches the finite pair whose product leaves the range of a double."""
    huge = (1e308, 1e308)
    with pytest.raises(GrafxVectorValidationError) as failure:
        oracle.dot(huge, huge)
    assert failure.value.details["reason"] == "non_finite_result"


def test_vectors_of_different_lengths_are_refused_before_anything_is_computed(
    oracle: PureVectorMath,
) -> None:
    """A length mismatch is a caller error, named by its own reason detail."""
    with pytest.raises(GrafxVectorValidationError) as failure:
        oracle.dot((1.0, 2.0), (1.0,))
    assert failure.value.details["reason"] == "length_mismatch"
    assert failure.value.details["value"] == 2
    assert failure.value.details["other"] == 1


def test_a_length_mismatch_is_caught_even_when_one_side_is_empty(
    oracle: PureVectorMath,
) -> None:
    """The zero-length branch of cosine must not become a way past the length check."""
    with pytest.raises(GrafxVectorValidationError) as failure:
        oracle.cosine((), (1.0,))
    assert failure.value.details["reason"] == "length_mismatch"


def test_top_k_orders_by_score_and_breaks_ties_by_ascending_identifier(
    oracle: PureVectorMath,
) -> None:
    """The tie rule is the frozen one, and it is what makes a ranking a function of the data."""
    candidates = [
        (9, (1.0, 0.0)),
        (3, (1.0, 0.0)),
        (7, (0.0, 1.0)),
    ]
    ranked = oracle.top_k((1.0, 0.0), candidates, 3, DistanceMetric.COSINE)
    assert [identifier for identifier, _score in ranked] == [3, 9, 7]


def test_top_k_returns_at_most_k_and_never_pads(oracle: PureVectorMath) -> None:
    """Fewer candidates than k is a shorter list, never an invented one."""
    ranked = oracle.top_k((1.0, 0.0), [(1, (1.0, 0.0))], 5, DistanceMetric.DOT)
    assert len(ranked) == 1


def test_top_k_of_no_candidates_is_empty(oracle: PureVectorMath) -> None:
    """An empty candidate list ranks to nothing rather than raising."""
    assert oracle.top_k((1.0, 0.0), [], 3, DistanceMetric.DOT) == []


@pytest.mark.parametrize("k", [0, -1])
def test_top_k_refuses_a_neighbour_count_below_one(oracle: PureVectorMath, k: int) -> None:
    """A request for zero neighbours describes no result and is a caller error."""
    with pytest.raises(GrafxConfigurationError) as failure:
        oracle.top_k((1.0,), [(1, (1.0,))], k, DistanceMetric.DOT)
    assert failure.value.details["field"] == "k"


def test_top_k_refuses_a_boolean_neighbour_count(oracle: PureVectorMath) -> None:
    """True is an integer in Python and is never a neighbour count here."""
    with pytest.raises(GrafxConfigurationError):
        oracle.top_k((1.0,), [(1, (1.0,))], True, DistanceMetric.DOT)


def test_a_metric_that_is_not_one_of_the_three_is_refused(oracle: PureVectorMath) -> None:
    """The metric is an enumeration member, never a string that happens to spell one."""
    with pytest.raises(GrafxConfigurationError) as failure:
        oracle.score((1.0,), (1.0,), "cosine")  # type: ignore[arg-type]
    assert failure.value.details["field"] == "metric"


def test_the_sum_does_not_depend_on_the_order_of_the_terms(oracle: PureVectorMath) -> None:
    """Correctly rounded accumulation is what makes a seeded corpus reproducible."""
    left = (1e16, 1.0, -1e16)
    right = (1.0, 1.0, 1.0)
    forward = oracle.dot(left, right)
    backward = oracle.dot(tuple(reversed(left)), tuple(reversed(right)))
    assert forward == backward == 1.0


def test_scores_are_identical_across_repeated_calls_on_a_seeded_corpus(
    oracle: PureVectorMath,
) -> None:
    """Determinism is asserted, not assumed: the same corpus scores the same bits every time."""
    corpus = seeded_vectors(64, 24, seed=0xA11CE)
    query = corpus[0]
    first = oracle.top_k(query, list(enumerate(corpus)), 10, DistanceMetric.COSINE)
    for _repeat in range(4):
        assert oracle.top_k(query, list(enumerate(corpus)), 10, DistanceMetric.COSINE) == first


def test_the_declared_tolerance_is_the_literal_the_module_documents() -> None:
    """The bound an accelerator is judged against is pinned, not derived (A56, A68)."""
    assert SCORE_TOLERANCE == 1e-9


def test_the_oracle_holds_no_state_between_calls(oracle: PureVectorMath) -> None:
    """A stateless adapter is what makes one instance safe to share across searches."""
    assert PureVectorMath.__slots__ == ()
    assert not hasattr(oracle, "__dict__")


# --- the accumulation has two exits and only one of them is a value ---------------------------


@pytest.mark.parametrize(
    "components",
    [(1e308, 1e308, -1e308), (1e308, 1e308), (-1e308, -1e308, 1e308)],
)
def test_a_sum_that_leaves_double_range_is_refused_in_the_taxonomy(
    oracle: PureVectorMath, components: tuple[float, ...]
) -> None:
    """math.fsum RAISES on an exact running sum that overflows; there is no result to check.

    Every component here is finite and every product is finite, so nothing before the sum can
    notice. The guard was written on the RESULT of an operation, and this is the shape that has
    no result -- so the accumulation is wrapped rather than only its value.
    """
    ones = tuple(1.0 for _ in components)
    with pytest.raises(GrafxVectorValidationError) as failure:
        oracle.dot(components, ones)
    assert failure.value.details["reason"] == "non_finite_result"
    assert failure.value.code == "vector_validation"


def test_the_overflowing_sum_is_refused_through_score_and_top_k_as_well(
    oracle: PureVectorMath,
) -> None:
    """The port has three doors onto the same accumulation and C10 consumes all three."""
    poisoned = (1e308, 1e308, -1e308)
    ones = (1.0, 1.0, 1.0)
    with pytest.raises(GrafxVectorValidationError):
        oracle.score(ones, poisoned, DistanceMetric.DOT)
    with pytest.raises(GrafxVectorValidationError):
        oracle.top_k(ones, [(1, poisoned)], 1, DistanceMetric.DOT)


def test_a_squaring_metric_reaches_the_guard_through_an_infinity_at_this_magnitude(
    oracle: PureVectorMath,
) -> None:
    """At three huge components the squares overflow first, so the RESULT guard answers."""
    poisoned = (1e308, 1e308, -1e308)
    for call in (
        lambda: oracle.norm(poisoned),
        lambda: oracle.euclidean(poisoned, (1.0, 1.0, 1.0)),
        lambda: oracle.cosine(poisoned, (1.0, 1.0, 1.0)),
    ):
        with pytest.raises(GrafxVectorValidationError) as failure:
            call()
        assert failure.value.details["reason"] == "non_finite_result"


MANY_LARGE_SQUARES: tuple[float, ...] = tuple(math.sqrt(1e307) for _ in range(100))
"""A hundred finite components whose finite SQUARES sum past the range of a double.

The first diagnosis of this defect said only ``dot`` was exposed, because the squaring metrics
overflow to an infinity before they sum. That is true of three huge components and false of many
merely large ones: every square here is about 1e307 and finite, and it is their exact SUM that
leaves the range -- which is the ``fsum`` raise, in ``norm``. The mutation battery found it by
reverting the wrap in ``norm`` and watching the suite stay green.
"""


@pytest.mark.parametrize("operation", ["norm", "euclidean", "cosine"])
def test_a_squaring_metric_is_exposed_too_when_the_squares_are_merely_large(
    oracle: PureVectorMath, operation: str
) -> None:
    """The accumulation guard is load-bearing in every metric, not only in dot."""
    assert all(math.isfinite(component) for component in MANY_LARGE_SQUARES)
    assert all(math.isfinite(component * component) for component in MANY_LARGE_SQUARES)
    zeros = tuple(0.0 for _ in MANY_LARGE_SQUARES)
    call = getattr(oracle, operation)
    with pytest.raises(GrafxVectorValidationError) as failure:
        call(MANY_LARGE_SQUARES) if operation == "norm" else call(MANY_LARGE_SQUARES, zeros)
    assert failure.value.details["reason"] == "non_finite_result"


def test_an_ordinary_large_sum_that_still_fits_is_not_refused(
    oracle: PureVectorMath,
) -> None:
    """The refusal must be the overflow and not the magnitude, or it would refuse real data."""
    assert oracle.dot((1e308, -1e307), (1.0, 1.0)) == pytest.approx(9e307, rel=1e-12)


def test_the_oracle_sums_exactly_where_a_pairwise_sum_cancels(
    oracle: PureVectorMath,
) -> None:
    """The oracle's half of the parity caveat, pinned where no optional extra is needed.

    Correctly rounded accumulation is what makes this 1.0 rather than 0.0, and it is the reason
    the declared tolerance describes ordinary embedding data rather than promising that no
    ranking can ever reorder. The accelerator's half is
    ``test_the_tolerance_does_not_promise_agreement_on_cancelling_input``.
    """
    assert oracle.dot((1e16, 1.0, -1e16), (1.0, 1.0, 1.0)) == 1.0
