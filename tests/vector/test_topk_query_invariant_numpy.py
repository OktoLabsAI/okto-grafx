"""The accelerated ranking obeys the same VEC-1 rules as the oracle (SPEC-VEC BR-7, A54).

Same assertions as ``test_topk_query_invariant.py``, run against ``NumpyVectorMath``: the ranking
equals the adapter's own pairwise cosine bit for bit, an empty ranking never examines the query,
and every refusal keeps its place, type and reason. One case is specific to this adapter: a query
that is not a sequence of numbers is refused at the first candidate, after the length check,
exactly where the pairwise door refuses it.
"""

from __future__ import annotations

import pytest

numpy = pytest.importorskip("numpy")

from okto_grafx.adapters.vectormath_numpy import NumpyVectorMath  # noqa: E402
from okto_grafx.domain.errors import GrafxVectorValidationError  # noqa: E402
from okto_grafx.domain.ports.vectormath import DistanceMetric  # noqa: E402

from .test_topk_query_invariant import (  # noqa: E402
    check_an_unmeasurable_query_is_refused_at_the_first_candidate,
    check_count_and_metric_are_refused_before_the_query_is_measured,
    check_every_candidate_refusal_keeps_its_type_reason_and_place,
    check_no_candidates_never_examines_the_query,
    check_ranking_matches_the_pairwise_door,
)

pytestmark = pytest.mark.optional_dependency("numpy")


@pytest.fixture
def accelerator() -> NumpyVectorMath:
    """Return the numpy adapter, judged against its own pairwise doors."""
    return NumpyVectorMath()


def test_the_cosine_ranking_matches_the_pairwise_door_exactly(
    accelerator: NumpyVectorMath,
) -> None:
    check_ranking_matches_the_pairwise_door(accelerator)


def test_a_ranking_of_no_candidates_never_examines_the_query(
    accelerator: NumpyVectorMath,
) -> None:
    check_no_candidates_never_examines_the_query(accelerator)


def test_the_neighbour_count_and_the_metric_are_refused_before_the_query_is_measured(
    accelerator: NumpyVectorMath,
) -> None:
    check_count_and_metric_are_refused_before_the_query_is_measured(accelerator)


def test_a_query_that_cannot_be_measured_is_refused_at_the_first_candidate(
    accelerator: NumpyVectorMath,
) -> None:
    check_an_unmeasurable_query_is_refused_at_the_first_candidate(accelerator)


def test_every_candidate_refusal_keeps_its_type_reason_and_place(
    accelerator: NumpyVectorMath,
) -> None:
    check_every_candidate_refusal_keeps_its_type_reason_and_place(accelerator)


def test_a_query_that_is_not_a_sequence_of_numbers_is_refused_where_the_pairwise_door_refuses_it(
    accelerator: NumpyVectorMath,
) -> None:
    """The conversion of the query happens once, at the first candidate, after the length check."""
    query = "abc"
    candidates = [(1, (1.0, 2.0, 3.0)), (2, (3.0, 2.0, 1.0))]
    with pytest.raises(GrafxVectorValidationError) as pairwise:
        accelerator.cosine(query, candidates[0][1])  # type: ignore[arg-type]
    with pytest.raises(GrafxVectorValidationError) as ranked:
        accelerator.top_k(query, candidates, 2, DistanceMetric.COSINE)  # type: ignore[arg-type]
    assert ranked.value.details == pairwise.value.details
    assert ranked.value.details["reason"] == "not_a_sequence"
    with pytest.raises(GrafxVectorValidationError) as mismatch:
        accelerator.top_k(query, [(1, (1.0, 2.0))], 1, DistanceMetric.COSINE)  # type: ignore[arg-type]
    assert mismatch.value.details["reason"] == "length_mismatch"
