"""The accelerated adapter obeys the same VEC-4 rules as the oracle (SPEC-VEC BR-7, A54).

Same assertions as ``test_prepared_scoring.py`` against ``NumpyVectorMath``: the capability is
declared, the prepared scorer answers bit for bit what ``score`` answers under every metric, and
it refuses lazily with the details of the pairwise door.
"""

from __future__ import annotations

from threading import Event, Thread

import pytest

numpy = pytest.importorskip("numpy")

from okto_grafx.adapters import vectormath_numpy as numpy_module  # noqa: E402
from okto_grafx.adapters.vectormath_numpy import NumpyVectorMath  # noqa: E402
from okto_grafx.domain.errors import GrafxVectorValidationError  # noqa: E402
from okto_grafx.domain.ports.vectormath import (  # noqa: E402
    DistanceMetric,
    PreparedCosineVectorMath,
    PreparedVectorMath,
)
from okto_grafx.domain.vector.hnsw import HnswGraph  # noqa: E402

from .test_prepared_scoring import (  # noqa: E402
    check_a_prepared_scorer_refuses_each_vector_as_the_pairwise_door_does,
    check_prepared_scorer_answers_exactly_what_score_answers,
    check_preparing_an_unmeasurable_query_refuses_nothing_until_it_is_used,
)

pytestmark = pytest.mark.optional_dependency("numpy")


def test_the_accelerator_declares_the_capability() -> None:
    assert isinstance(NumpyVectorMath(), PreparedVectorMath)
    assert isinstance(NumpyVectorMath(), PreparedCosineVectorMath)


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


def test_finite_result_guard_does_not_redispatch_a_python_float_to_numpy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scalar result check stays outside NumPy after its explicit float conversion."""

    def unexpected_numpy_isfinite(_value: object) -> bool:
        raise AssertionError("a Python float was dispatched back through numpy.isfinite")

    monkeypatch.setattr(numpy_module.numpy, "isfinite", unexpected_numpy_isfinite)
    scorer = NumpyVectorMath().prepare((1.0, 0.0), DistanceMetric.COSINE)

    assert scorer((1.0, 0.0)) == 1.0


def test_prepared_cosine_with_norm_is_lazy_and_exact() -> None:
    accelerator = NumpyVectorMath()
    unused, _unused_cached = accelerator.prepare_cosine_with_norm(
        (float("nan"), 0.0)
    )
    measured, scorer = accelerator.prepare_cosine_with_norm((1.0, 2.0, 3.0))
    candidates = ((4.0, 5.0, 6.0), (0.0, 0.0, 0.0), (-2.0, 1.0, 9.0))

    assert callable(unused)
    measured_pairs = [measured(values) for values in candidates]
    assert [score for score, _norm in measured_pairs] == [
        accelerator.score((1.0, 2.0, 3.0), values, DistanceMetric.COSINE)
        for values in candidates
    ]
    assert [
        scorer(values, right_norm)
        for values, (_score, right_norm) in zip(candidates, measured_pairs, strict=True)
    ] == [
        accelerator.score((1.0, 2.0, 3.0), values, DistanceMetric.COSINE)
        for values in candidates
    ]


def test_hnsw_caches_only_successful_norms_and_invalidates_reused_node_ids() -> None:
    class CountingNumpyMath(NumpyVectorMath):
        def __init__(self) -> None:
            self.measured_calls = 0
            self.cached_calls = 0

        def prepare_cosine_with_norm(self, query: object) -> object:
            measured, cached = super().prepare_cosine_with_norm(query)  # type: ignore[arg-type]

            def count_measured(values: object) -> tuple[float, float]:
                self.measured_calls += 1
                return measured(values)  # type: ignore[arg-type]

            def count_cached(values: object, norm: float) -> float:
                self.cached_calls += 1
                return cached(values, norm)  # type: ignore[arg-type]

            return count_measured, count_cached

    math = CountingNumpyMath()
    graph = HnswGraph(math, DistanceMetric.COSINE, seed=17)
    graph.insert(7, (1.0, 0.0))

    assert math.measured_calls == 0
    assert graph.search((1.0, 0.0), ef=1)[0] == ((1.0, 7),)
    assert math.measured_calls == 1
    assert graph.search((0.0, 1.0), ef=1)[0] == ((0.0, 7),)
    assert math.measured_calls == 1
    assert math.cached_calls == 1

    graph.remove(7)
    graph.insert(7, (0.0, 1.0))
    assert graph.search((0.0, 1.0), ef=1)[0] == ((1.0, 7),)
    assert math.measured_calls == 2


def test_hnsw_does_not_cache_a_norm_when_the_legacy_score_refuses() -> None:
    graph = HnswGraph(NumpyVectorMath(), DistanceMetric.COSINE, seed=19)
    graph.insert(3, (1e308, 1e308))

    with pytest.raises(GrafxVectorValidationError):
        graph.search((1.0, 0.0), ef=1)

    assert graph._norms == {}  # noqa: SLF001 - this is the refusal/cache invariant


def test_norm_cache_is_bound_to_the_immutable_node_generation_under_reentry() -> None:
    class ReentrantNumpyMath(NumpyVectorMath):
        def __init__(self) -> None:
            self.graph: HnswGraph | None = None
            self.armed = True

        def prepare_cosine_with_norm(self, query: object) -> object:
            measured, cached = super().prepare_cosine_with_norm(query)  # type: ignore[arg-type]

            def reentrant(values: object) -> tuple[float, float]:
                result = measured(values)  # type: ignore[arg-type]
                if self.armed:
                    self.armed = False
                    assert self.graph is not None
                    self.graph.remove(7)
                    self.graph.insert(7, (0.0, 2.0))
                return result

            return reentrant, cached

    math = ReentrantNumpyMath()
    graph = HnswGraph(math, DistanceMetric.COSINE, seed=23)
    math.graph = graph
    graph.insert(7, (1.0, 0.0))

    assert graph.search((1.0, 0.0), ef=1)[0] == ((1.0, 7),)
    assert graph._norms == {}  # noqa: SLF001 - old norm did not cross generations
    assert graph.search((0.0, 1.0), ef=1)[0] == ((1.0, 7),)
    retained, norm = graph._norms[7]  # noqa: SLF001 - generation proof
    assert retained is graph._values[7]  # noqa: SLF001 - generation proof
    assert norm == 2.0


def test_norm_reuse_shares_one_lazy_snapshot_of_a_mutable_query() -> None:
    query = [1.0, 0.0]
    measured, cached = NumpyVectorMath().prepare_cosine_with_norm(query)
    first_score, first_norm = measured((1.0, 0.0))
    query[:] = (0.0, 1.0)

    assert first_score == 1.0
    assert first_norm == 1.0
    assert cached((1.0, 0.0), first_norm) == 1.0
    assert cached((0.0, 1.0), 1.0) == 0.0


def test_concurrent_remove_between_norm_check_and_publish_leaves_no_orphan() -> None:
    checked = Event()
    resume = Event()

    class GatedValues(dict[int, tuple[float, ...] | bytes]):
        armed = True

        def get(self, key: int, default: object = None) -> object:
            value = super().get(key, default)
            if key == 7 and self.armed:
                self.armed = False
                checked.set()
                assert resume.wait(5.0), "concurrent remove did not release norm publication"
            return value

    graph = HnswGraph(NumpyVectorMath(), DistanceMetric.COSINE, seed=29)
    graph.insert(7, (1.0, 0.0))
    graph._values = GatedValues(graph._values)  # noqa: SLF001 - deterministic race gate
    failures: list[BaseException] = []

    def search() -> None:
        try:
            graph.search((1.0, 0.0), ef=1)
        except BaseException as failure:  # pragma: no cover - asserted below
            failures.append(failure)

    worker = Thread(target=search)
    worker.start()
    assert checked.wait(5.0), "norm publication never reached the race gate"
    graph.remove(7)
    resume.set()
    worker.join(5.0)

    assert not worker.is_alive()
    assert failures == []
    assert 7 not in graph
    assert graph._norms == {}  # noqa: SLF001 - no historical generation is retained
