"""The accelerated adapter obeys the same VEC-4 rules as the oracle (SPEC-VEC BR-7, A54).

Same assertions as ``test_prepared_scoring.py`` against ``NumpyVectorMath``: the capability is
declared, the prepared scorer answers bit for bit what ``score`` answers under every metric, and
it refuses lazily with the details of the pairwise door.
"""

from __future__ import annotations

import pytest

numpy = pytest.importorskip("numpy")

from okto_grafx.adapters import vectormath_numpy as numpy_module  # noqa: E402
from okto_grafx.adapters.vectormath_numpy import NumpyVectorMath  # noqa: E402
from okto_grafx.domain.errors import GrafxVectorValidationError  # noqa: E402
from okto_grafx.domain.ports.vectormath import DistanceMetric, PreparedVectorMath  # noqa: E402

from .test_prepared_scoring import (  # noqa: E402
    check_a_prepared_scorer_refuses_each_vector_as_the_pairwise_door_does,
    check_prepared_scorer_answers_exactly_what_score_answers,
    check_preparing_an_unmeasurable_query_refuses_nothing_until_it_is_used,
)

pytestmark = pytest.mark.optional_dependency("numpy")


def test_the_accelerator_declares_the_capability() -> None:
    assert isinstance(NumpyVectorMath(), PreparedVectorMath)


def test_a_prepared_scorer_answers_exactly_what_score_answers() -> None:
    check_prepared_scorer_answers_exactly_what_score_answers(NumpyVectorMath())


def test_preparing_an_unmeasurable_query_refuses_nothing_until_it_is_used() -> None:
    check_preparing_an_unmeasurable_query_refuses_nothing_until_it_is_used(
        NumpyVectorMath()
    )


def test_a_prepared_scorer_refuses_each_vector_as_the_pairwise_door_does() -> None:
    check_a_prepared_scorer_refuses_each_vector_as_the_pairwise_door_does(
        NumpyVectorMath()
    )


def test_a_query_that_is_not_a_sequence_is_refused_at_the_first_call_after_the_length_check() -> (
    None
):
    accelerator = NumpyVectorMath()
    for metric in DistanceMetric:
        scorer = accelerator.prepare("abc", metric)  # type: ignore[arg-type]
        with pytest.raises(GrafxVectorValidationError) as pairwise:
            accelerator.score("abc", (1.0, 2.0, 3.0), metric)  # type: ignore[arg-type]
        with pytest.raises(GrafxVectorValidationError) as prepared:
            scorer((1.0, 2.0, 3.0))
        assert prepared.value.details == pairwise.value.details
        assert prepared.value.details["reason"] == "not_a_sequence"
        with pytest.raises(GrafxVectorValidationError) as mismatch:
            scorer((1.0, 2.0))
        assert mismatch.value.details["reason"] == "length_mismatch"


def test_each_prepared_cosine_candidate_uses_one_numpy_warning_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Right norm and similarity share the same warning policy without two context switches."""
    scopes: list[str] = []

    class CountingScope:
        def __enter__(self) -> None:
            scopes.append("enter")

        def __exit__(self, *_failure: object) -> None:
            scopes.append("exit")

    monkeypatch.setattr(numpy_module, "_quiet", CountingScope)
    scorer = NumpyVectorMath().prepare((1.0, 0.0), DistanceMetric.COSINE)
    assert scorer((1.0, 0.0)) == 1.0

    scopes.clear()
    assert scorer((0.0, 1.0)) == 0.0
    assert scopes == ["enter", "exit"]
