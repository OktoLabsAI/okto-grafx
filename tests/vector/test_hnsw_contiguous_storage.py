"""D-13H: compact HNSW residency without changing the public vector value.

The compact representation is deliberately narrower than the original D-13 proposal.  Only an
engine whose explicitly selected math adapter names NumPy enables it; the public graph and the
pure engine retain tuples.  Compact values are immutable bytes, and host math receives a fresh
read-only view for every score, so even a host that releases its borrowed view cannot poison the
next search.
"""

from __future__ import annotations

from array import array
from collections.abc import Sequence

import pytest

from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.vector.hnsw import HnswGraph

from .conftest import RecordingMetrics, StepClock, VectorFixture, seeded_vectors


class _ReleasingBufferMath:
    """A NumPy-labelled port double that releases every candidate view after copying it."""

    def __init__(self) -> None:
        self._inner = PureVectorMath()
        self.released = 0

    @property
    def name(self) -> str:
        """Select compact engine residency without depending on the optional NumPy package."""
        return "numpy"

    def _candidate(self, values: Sequence[float]) -> tuple[float, ...]:
        copied = tuple(values)
        if isinstance(values, memoryview):
            assert values.readonly
            values.release()
            self.released += 1
        return copied

    def dot(self, a: Sequence[float], b: Sequence[float]) -> float:
        return self._inner.dot(tuple(a), self._candidate(b))

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float:
        return self._inner.cosine(tuple(a), self._candidate(b))

    def euclidean(self, a: Sequence[float], b: Sequence[float]) -> float:
        return self._inner.euclidean(tuple(a), self._candidate(b))

    def norm(self, a: Sequence[float]) -> float:
        return self._inner.norm(tuple(a))

    def normalize(self, a: Sequence[float]) -> tuple[float, ...]:
        return self._inner.normalize(tuple(a))

    def score(
        self, a: Sequence[float], b: Sequence[float], metric: DistanceMetric
    ) -> float:
        return self._inner.score(tuple(a), self._candidate(b), metric)

    def top_k(
        self,
        query: Sequence[float],
        candidates: Sequence[tuple[int, Sequence[float]]],
        k: int,
        metric: DistanceMetric,
    ) -> list[tuple[int, float]]:
        return self._inner.top_k(query, candidates, k, metric)


def _rounded(
    corpus: Sequence[Sequence[float]], typecode: str
) -> list[tuple[float, ...]]:
    """Round a corpus exactly as its declared f32/f64 storage would."""
    return [tuple(array(typecode, values)) for values in corpus]


def _graph(
    corpus: Sequence[Sequence[float]],
    *,
    typecode: str | None,
    metric: DistanceMetric = DistanceMetric.COSINE,
) -> HnswGraph:
    graph = HnswGraph(
        PureVectorMath(),
        metric,
        seed=0xD13,
        neighbours=4,
        _component_typecode=typecode,
    )
    for node, values in enumerate(corpus, 1):
        graph.insert(node, values)
    return graph


def _shape(graph: HnswGraph) -> tuple[object, ...]:
    return (
        tuple(graph.level_of(node) for node in graph.nodes()),
        graph.chain(),
        tuple(
            tuple(graph.neighbours_of(node, layer) for node in graph.nodes())
            for layer in range(graph.top_level + 1)
        ),
    )


def test_an_unknown_compact_component_width_is_refused_before_graph_state() -> None:
    """The engine-only selector remains a closed f32/f64 domain."""
    with pytest.raises(GrafxConfigurationError) as failure:
        HnswGraph(
            PureVectorMath(),
            DistanceMetric.COSINE,
            seed=0xD13,
            _component_typecode="h",
        )
    assert failure.value.details["field"] == "component_typecode"


@pytest.mark.parametrize("typecode", ["f", "d"])
@pytest.mark.parametrize("metric", list(DistanceMetric))
def test_compact_components_preserve_shape_scores_and_churn(
    typecode: str, metric: DistanceMetric
) -> None:
    """Storage-rounded tuple and compact graphs remain the same deterministic graph."""
    corpus = _rounded(seeded_vectors(48, 12, seed=0xC013), typecode)
    ordinary = _graph(corpus, typecode=None, metric=metric)
    compact = _graph(corpus, typecode=typecode, metric=metric)

    assert _shape(compact) == _shape(ordinary)
    for query in corpus[::11]:
        assert compact.search(query, 16) == ordinary.search(query, 16)

    for victim in (3, 17, 31):
        ordinary.remove(victim)
        compact.remove(victim)
    extra = _rounded(seeded_vectors(3, 12, seed=0xADD), typecode)
    for node, values in enumerate(extra, 100):
        ordinary.insert(node, values)
        compact.insert(node, values)

    assert compact.is_connected_at_layer_zero()
    assert _shape(compact) == _shape(ordinary)
    assert compact.search(corpus[7], len(compact)) == ordinary.search(
        corpus[7], len(ordinary)
    )


@pytest.mark.parametrize("typecode", ["f", "d"])
def test_compact_backing_is_immutable_owned_bytes_and_values_stay_a_tuple(
    typecode: str,
) -> None:
    """Neither a mutable source nor the public accessor can mutate resident components."""
    source = [0.25, -0.5, 1.0]
    expected = tuple(array(typecode, source))
    graph = _graph((source,), typecode=typecode)
    source[0] = 99.0

    assert type(graph._values[1]) is bytes
    assert type(graph.values_of(1)) is tuple
    assert graph.values_of(1) == expected


def test_a_host_releasing_borrowed_views_cannot_poison_a_later_search() -> None:
    """Views are ephemeral; release affects one callback, never retained graph state."""
    math = _ReleasingBufferMath()
    corpus = _rounded(seeded_vectors(32, 8, seed=0xFEED), "f")
    graph = HnswGraph(
        math,
        DistanceMetric.COSINE,
        seed=0xD13,
        neighbours=4,
        _component_typecode="f",
    )
    for node, values in enumerate(corpus, 1):
        graph.insert(node, values)

    first = graph.search(corpus[3], 12)
    second = graph.search(corpus[3], 12)

    assert first == second
    assert math.released > 0
    assert all(type(stored) is bytes for stored in graph._values.values())


@pytest.mark.parametrize(
    ("storage_dtype", "typecode"), [("float32", "f"), ("float64", "d")]
)
def test_only_the_numpy_selected_engine_compacts_its_derived_graph(
    metrics: RecordingMetrics,
    clock: StepClock,
    storage_dtype: str,
    typecode: str,
) -> None:
    """Pure remains tuple-backed while the existing explicit NumPy label selects D-13H."""
    pure = VectorFixture(metrics=metrics, clock=clock, math=PureVectorMath())
    pure_space = pure.create_space("pure", 3, storage_dtype=storage_dtype)
    pure_table = pure.create_table("PureNode", pure_space.name)
    pure.insert_row(pure_table, 1, 0, pure_space, (1.0, 0.0, 0.0), csn=1)
    pure_graph = pure.engine.index(pure_space.name).graph()

    compact_math = _ReleasingBufferMath()
    compact = VectorFixture(
        metrics=RecordingMetrics(), clock=StepClock(), math=compact_math
    )
    compact_space = compact.create_space("compact", 3, storage_dtype=storage_dtype)
    compact_table = compact.create_table("CompactNode", compact_space.name)
    refs = [
        compact.insert_row(compact_table, node, 0, compact_space, values, csn=node)
        for node, values in enumerate(
            ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), 1
        )
    ]
    index = compact.engine.index(compact_space.name)
    before = index.snapshot()
    node = next(
        graph_node
        for graph_node, record_id in before.record_of_node.items()
        if record_id == 1
    )
    retained = before.graph._values[node]

    compact.delete_row(
        compact_table,
        refs[0],
        1,
        compact_space,
        (1.0, 0.0, 0.0),
        csn=10,
    )
    after = index.snapshot()

    assert all(type(stored) is tuple for stored in pure_graph._values.values())
    assert all(type(stored) is bytes for stored in after.graph._values.values())
    assert len(retained) == 3 * array(typecode).itemsize
    assert after.graph is before.graph
    assert after.graph._values[node] is retained
    assert not after.entry_of_node[node].live


@pytest.mark.parametrize("typecode", ["f", "d"])
@pytest.mark.parametrize("metric", list(DistanceMetric))
@pytest.mark.optional_dependency("numpy")
def test_the_real_numpy_adapter_keeps_compact_ranking_and_shape(
    typecode: str, metric: DistanceMetric
) -> None:
    """The optional adapter consumes ephemeral buffer views without changing its answer."""
    pytest.importorskip("numpy")
    from okto_grafx.adapters.vectormath_numpy import NumpyVectorMath

    corpus = _rounded(seeded_vectors(36, 16, seed=0xACC3), typecode)
    ordinary = HnswGraph(NumpyVectorMath(), metric, seed=0xD13, neighbours=4)
    compact = HnswGraph(
        NumpyVectorMath(),
        metric,
        seed=0xD13,
        neighbours=4,
        _component_typecode=typecode,
    )
    for node, values in enumerate(corpus, 1):
        ordinary.insert(node, values)
        compact.insert(node, values)

    assert _shape(compact) == _shape(ordinary)
    for query in corpus[::9]:
        assert compact.search(query, 12) == ordinary.search(query, 12)

    database = VectorFixture(
        metrics=RecordingMetrics(), clock=StepClock(), math=NumpyVectorMath()
    )
    space = database.create_space(
        "actual_numpy", 3, storage_dtype="float32" if typecode == "f" else "float64"
    )
    table = database.create_table("ActualNumpyNode", space.name)
    database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0), csn=1)
    graph = database.engine.index(space.name).graph()
    assert all(type(stored) is bytes for stored in graph._values.values())


@pytest.mark.parametrize("typecode", ["f", "d"])
@pytest.mark.parametrize("metric", list(DistanceMetric))
@pytest.mark.optional_dependency("numpy")
def test_transient_construction_scores_preserve_the_complete_numpy_graph(
    typecode: str, metric: DistanceMetric
) -> None:
    """Cold-build memoization changes neither topology nor any later graph answer."""
    pytest.importorskip("numpy")
    from okto_grafx.adapters.vectormath_numpy import NumpyVectorMath

    corpus = _rounded(seeded_vectors(72, 16, seed=0xC01D), typecode)
    canonical = HnswGraph(
        NumpyVectorMath(),
        metric,
        seed=0xD13,
        neighbours=4,
        ef_construction=16,
        _component_typecode=typecode,
    )
    accelerated = HnswGraph(
        NumpyVectorMath(),
        metric,
        seed=0xD13,
        neighbours=4,
        ef_construction=16,
        _component_typecode=typecode,
        _cache_construction_link_scores=True,
    )
    for node, values in enumerate(corpus, 1):
        canonical.insert(node, values)
        accelerated.insert(node, values)

    retained = accelerated._construction_link_scores
    assert retained is not None
    for layer, by_node in enumerate(retained):
        capacity = accelerated._capacity(layer)
        assert all(len(scores) <= capacity for scores in by_node.values())
    accelerated._finish_construction()

    assert accelerated._construction_link_scores is None
    assert _shape(accelerated) == _shape(canonical)
    for query in corpus[::13]:
        assert accelerated.search(query, 24) == canonical.search(query, 24)

    # Once published, incremental churn returns to the established scalar path.
    for victim in (7, 29, 61):
        canonical.remove(victim)
        accelerated.remove(victim)
    extra = _rounded(seeded_vectors(3, 16, seed=0xA66), typecode)
    for node, values in enumerate(extra, 100):
        canonical.insert(node, values)
        accelerated.insert(node, values)
    assert _shape(accelerated) == _shape(canonical)
    assert accelerated.search(corpus[5], 24) == canonical.search(corpus[5], 24)


@pytest.mark.parametrize("metric", list(DistanceMetric))
def test_transient_construction_scores_preserve_the_default_pure_graph(
    metric: DistanceMetric,
) -> None:
    """The optimisation used by ``vector_math='auto'`` preserves exact oracle answers."""
    from okto_grafx.adapters.vectormath_pure import PureVectorMath

    corpus = seeded_vectors(72, 16, seed=0xC01D)
    canonical = HnswGraph(
        PureVectorMath(), metric, seed=0xD13, neighbours=4, ef_construction=16
    )
    accelerated = HnswGraph(
        PureVectorMath(),
        metric,
        seed=0xD13,
        neighbours=4,
        ef_construction=16,
        _cache_construction_link_scores=True,
    )
    for node, values in enumerate(corpus, 1):
        canonical.insert(node, values)
        accelerated.insert(node, values)
    accelerated._finish_construction()

    assert _shape(accelerated) == _shape(canonical)
    for query in corpus[::13]:
        assert accelerated.search(query, 24) == canonical.search(query, 24)


def test_pure_adapter_declares_stable_pairs_but_a_subclass_must_opt_in_again() -> None:
    """The default oracle is eligible, while a customised subclass stays fail-closed."""
    from okto_grafx.adapters.vectormath_pure import PureVectorMath

    class CallbackCapablePureMath(PureVectorMath):
        pass

    capability = "_stable_pair_scores_for_construction"
    assert type(PureVectorMath()).__dict__.get(capability) is True
    assert type(CallbackCapablePureMath()).__dict__.get(capability) is None


@pytest.mark.optional_dependency("numpy")
def test_numpy_adapter_declares_stable_pairs_but_a_subclass_must_opt_in_again() -> None:
    """The built-in accelerator is eligible, while a customised subclass stays fail-closed."""
    pytest.importorskip("numpy")
    from okto_grafx.adapters.vectormath_numpy import NumpyVectorMath

    class CallbackCapableNumpyMath(NumpyVectorMath):
        pass

    capability = "_stable_pair_scores_for_construction"
    assert type(NumpyVectorMath()).__dict__.get(capability) is True
    assert type(CallbackCapableNumpyMath()).__dict__.get(capability) is None
