"""The optional accelerated vector math adapter (SPEC-VEC FR-7, TR-6, IR-2, amendment A52).

numpy lives in the ``[accel]`` extra and is imported here and nowhere else in the package, which
is what keeps the domain free of any numerical dependency (TR-1, guideline G3). Importing this
module without the extra raises ``ImportError``; that is the intended shape, because the engine
runs the pure oracle when the accelerator is absent and the oracle suite must pass with nothing
installed at all (TR-6).

This adapter is never the authority. Every rule it implements is a rule
:mod:`okto_grafx.adapters.vectormath_pure` states first, and the parity suite proves the two
agree within ``SCORE_TOLERANCE`` on a seeded corpus (BR-7). The three rules easiest to get
silently wrong here, each of which numpy would otherwise answer differently:

* **A zero-length vector has cosine similarity 0.0 and normalizes to itself.** The natural numpy
  expression divides by zero and produces ``nan`` with a runtime warning.
* **A non-finite result is refused**, so a NaN can never enter a ranking. numpy propagates NaN
  silently and ``argsort`` then places it wherever the underlying sort happens to leave it.
* **Ties break by ascending candidate id.** ``argsort`` is only stable when asked to be, and
  "stable" means "keeps the caller's order", which is not the same rule.

Accumulation is in float64 for every space, whatever dtype the pages hold: the storage dtype
buys bytes on disk, never precision in the arithmetic (decision ``dec_6b65207f``). Inputs are
converted to float64 explicitly rather than inferred, so a float32 array from a caller cannot
quietly lower the precision of the whole computation.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from math import isfinite

import numpy

from okto_grafx.adapters.vectormath_pure import (
    require_metric,
    require_positive_k,
    require_same_length,
)
from okto_grafx.domain.errors import GrafxVectorValidationError
from okto_grafx.domain.ports.vectormath import DistanceMetric

__all__ = [
    "NUMPY_ADAPTER_NAME",
    "NumpyVectorMath",
]

NUMPY_ADAPTER_NAME: str = "numpy"
"""The bounded label this adapter reports, matching the ``numpy`` selector of DatabaseConfig."""


def _as_array(values: Sequence[float]) -> numpy.ndarray:
    """Return the components as a one-dimensional float64 array, refusing an unusable shape.

    A caller mistake -- a string, a nested sequence, an element that is not a number -- becomes
    the vector validation error of the taxonomy rather than a numpy exception, because only a
    ``Grafx*`` type may leave a public door (CONTRACT.md section 11 item 5).
    """
    if isinstance(values, (str, bytes, bytearray)):
        raise GrafxVectorValidationError(
            f"A vector needs a sequence of numbers; got {type(values).__name__}.",
            field="values",
            reason="not_a_sequence",
            value=type(values).__name__,
        )
    try:
        array = numpy.asarray(values, dtype=numpy.float64)
    except (TypeError, ValueError) as failure:
        raise GrafxVectorValidationError(
            f"A vector needs a sequence of numbers; got {type(values).__name__}.",
            field="values",
            reason="not_a_sequence",
            value=type(values).__name__,
        ) from failure
    if array.ndim != 1:
        raise GrafxVectorValidationError(
            f"A vector is a flat sequence of numbers; got an array of {array.ndim} dimensions.",
            field="values",
            reason="not_a_sequence",
            value=array.ndim,
        )
    return array


def _quiet() -> object:
    """Return a context in which numpy reports an overflow by its value, not by a warning.

    The guard below turns a non-finite result into a typed refusal, so the refusal IS the report
    and a warning printed beside it says the same thing twice -- into a stream the caller did not
    ask to be written to. Silencing it hides nothing: an operation that overflows still leaves
    this module as ``GrafxVectorValidationError`` naming the operation.
    """
    return numpy.errstate(over="ignore", invalid="ignore")


def _require_finite(value: float, operation: str) -> float:
    """Return the value, refusing a result that is not finite.

    The single guard of the pure oracle, restated for the accelerator so both doors refuse the
    same states with the same error and the same details.
    """
    result = float(value)
    # Conversion above deliberately collapses NumPy scalars to a Python float. Calling the
    # standard-library scalar predicate avoids re-entering NumPy for every result in a ranking.
    if not isfinite(result):
        raise GrafxVectorValidationError(
            f"The {operation} of these vectors is {result!r}, which is not a finite number; a "
            f"non-finite score has no place in a ranking.",
            field="score",
            reason="non_finite_result",
            operation=operation,
            value=repr(result),
        )
    return result


class NumpyVectorMath:
    """The same primitives as the oracle, computed with numpy, agreeing to a stated tolerance.

    The instance is stateless: it holds no buffer and no scratch array, so two searches running
    over one adapter cannot see each other's intermediate values.
    """

    __slots__ = ()

    @property
    def name(self) -> str:
        """Return the bounded label of this implementation, safe as a metric label value."""
        return NUMPY_ADAPTER_NAME

    def dot(self, a: Sequence[float], b: Sequence[float]) -> float:
        """Return the dot product of two vectors of equal length."""
        require_same_length(a, b)
        with _quiet():
            return _require_finite(numpy.dot(_as_array(a), _as_array(b)), "dot product")

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float:
        """Return the cosine similarity of two vectors of equal length.

        A vector of zero length answers 0.0, exactly as the oracle does, instead of the NaN a
        division by zero would produce.
        """
        require_same_length(a, b)
        left = _as_array(a)
        right = _as_array(b)
        with _quiet():
            length_a = _require_finite(numpy.sqrt(numpy.dot(left, left)), "norm")
        return self._cosine_from_arrays(left, length_a, right)

    def _cosine_from_arrays(
        self, left: numpy.ndarray, length_a: float, right: numpy.ndarray
    ) -> float:
        """Return the cosine similarity of two converted vectors, given the length of the left.

        The tail of :meth:`cosine`, split off so a ranking can convert and measure its query
        once instead of once per candidate (VEC-1). The steps kept here run in the order they
        always ran: the right norm, the zero-length rule, then the guarded division.
        """
        # Both operations need the same warning policy. One scope preserves their order and
        # refusals while avoiding a second NumPy context enter/exit for every scored candidate.
        with _quiet():
            length_b = _require_finite(numpy.sqrt(numpy.dot(right, right)), "norm")
            if length_a == 0.0 or length_b == 0.0:
                return 0.0
            return _require_finite(
                numpy.dot(left, right) / (length_a * length_b), "cosine similarity"
            )

    def euclidean(self, a: Sequence[float], b: Sequence[float]) -> float:
        """Return the Euclidean distance between two vectors of equal length."""
        require_same_length(a, b)
        with _quiet():
            difference = _as_array(a) - _as_array(b)
            return _require_finite(
                numpy.sqrt(numpy.dot(difference, difference)), "Euclidean distance"
            )

    def norm(self, a: Sequence[float]) -> float:
        """Return the Euclidean length of one vector."""
        array = _as_array(a)
        with _quiet():
            return _require_finite(numpy.sqrt(numpy.dot(array, array)), "norm")

    def normalize(self, a: Sequence[float]) -> tuple[float, ...]:
        """Return the vector scaled to unit length, leaving a zero-length vector unchanged."""
        array = _as_array(a)
        with _quiet():
            length = _require_finite(numpy.sqrt(numpy.dot(array, array)), "norm")
        if length == 0.0:
            return tuple(float(component) for component in array)
        scaled = array / length
        return tuple(_require_finite(component, "normalize") for component in scaled)

    def score(self, a: Sequence[float], b: Sequence[float], metric: DistanceMetric) -> float:
        """Return the similarity of two vectors under one metric, higher meaning closer."""
        require_metric(metric)
        if metric is DistanceMetric.COSINE:
            return self.cosine(a, b)
        if metric is DistanceMetric.DOT:
            return self.dot(a, b)
        return -self.euclidean(a, b)

    def top_k(
        self,
        query: Sequence[float],
        candidates: Sequence[tuple[int, Sequence[float]]],
        k: int,
        metric: DistanceMetric,
    ) -> list[tuple[int, float]]:
        """Return the k best candidates, descending by score, ties broken by ascending id.

        The ordering is done in Python on the scored pairs rather than by an array sort. The
        expensive half is the scoring, which is where the accelerator earns its place, and
        keeping the ordering rule spelled the same way as the oracle is what makes the two
        provably identical on ties instead of accidentally similar.
        """
        require_positive_k(k)
        require_metric(metric)
        score = self.prepare(query, metric)
        scored = [(identifier, score(values)) for identifier, values in candidates]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:k]

    def prepare(
        self, query: Sequence[float], metric: DistanceMetric
    ) -> Callable[[Sequence[float]], float]:
        """Return a scorer of stored vectors against one query, converted once.

        The conversion of the query to an array, and under cosine its norm, are the same for
        every vector; doing them once per pair was the largest single cost of a ranking or a
        traversal here (VEC-1, VEC-4). The scorer does them at its FIRST call, after that call's
        length check, which is exactly where the pairwise door does them: a query that nothing
        is scored against is never examined, and a query that cannot be converted or measured is
        refused in the same order with the same reason. From the second call on, only the steps
        that depend on the stored vector run.
        """
        require_metric(metric)
        if metric is DistanceMetric.COSINE:
            prepared: tuple[numpy.ndarray, float] | None = None

            def cosine(values: Sequence[float]) -> float:
                nonlocal prepared
                require_same_length(query, values)
                if prepared is None:
                    left = _as_array(query)
                    right = _as_array(values)
                    with _quiet():
                        length_query = _require_finite(
                            numpy.sqrt(numpy.dot(left, left)), "norm"
                        )
                    prepared = (left, length_query)
                else:
                    left, length_query = prepared
                    right = _as_array(values)
                return self._cosine_from_arrays(left, length_query, right)

            return cosine
        left_array: numpy.ndarray | None = None
        if metric is DistanceMetric.DOT:

            def dot(values: Sequence[float]) -> float:
                nonlocal left_array
                require_same_length(query, values)
                with _quiet():
                    if left_array is None:
                        left_array = _as_array(query)
                    return _require_finite(
                        numpy.dot(left_array, _as_array(values)), "dot product"
                    )

            return dot

        def euclidean(values: Sequence[float]) -> float:
            nonlocal left_array
            require_same_length(query, values)
            with _quiet():
                if left_array is None:
                    left_array = _as_array(query)
                difference = left_array - _as_array(values)
                return -_require_finite(
                    numpy.sqrt(numpy.dot(difference, difference)), "Euclidean distance"
                )

        return euclidean

    def __repr__(self) -> str:
        return f"NumpyVectorMath(name={NUMPY_ADAPTER_NAME!r})"
