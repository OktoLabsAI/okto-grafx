"""The pure Python vector math adapter, which is the correctness oracle (SPEC-VEC FR-7, BR-7).

This module is the reference implementation of the ``VectorMath`` port. It depends on nothing
but the standard library, it ships in the universal wheel, and every accelerated adapter is
judged against it: a divergence beyond :data:`SCORE_TOLERANCE` is a defect of the accelerator,
never of this file (BR-7). The continuous integration suite runs this oracle with no optional
extra installed at all (TR-6).

Accumulation
------------
Every sum here goes through :func:`math.fsum`, which returns the correctly rounded sum of its
arguments. Two consequences matter. The first is reproducibility: the answer does not depend on
the order the terms are added, so the same corpus produces the same score on every platform, on
every Python build and in every run, which is what makes a seeded test corpus meaningful. The
second is that the oracle is the *exact* answer to compare an accelerator against, rather than
one particular rounding of it, so the tolerance below measures the accelerator's error and not a
disagreement between two equally arbitrary summation orders.

Accumulation is always in double precision regardless of the storage dtype of the space
(decision ``dec_6b65207f``): float32 halves the bytes on the page, never the precision of the
arithmetic done over them.

The single guard, and the exit it originally missed
---------------------------------------------------
One check protects the whole module: the RESULT of every operation must be finite. It is
deliberately not a per-component scan of the inputs, and that is not a shortcut. A non-finite
component poisons every one of these operations -- a NaN propagates through ``fsum`` and through
every product, and an overflow shows up as an infinity -- so one test on the way out catches both
the poisoned input and the finite input whose product overflows, with one comparison instead of
one per component. A second, redundant input scan would only make this one unkillable (A67).

That guard had a hole, and the hole was the word RESULT. ``math.fsum`` keeps an exact running
sum, and when THAT leaves double range it raises ``OverflowError`` instead of returning an
infinity -- so there was no result to check and a non-``Grafx`` exception left the port. Only
``dot`` was exposed: ``norm`` and ``euclidean`` square their terms first, so they overflow to
``inf`` and reach the guard. The accumulation is therefore wrapped rather than only its value,
which closes both exits of the same step instead of adding a second guard somewhere else.

What that guard buys is a total order. ``top_k`` sorts by score, and a NaN compares False against
everything, so a single NaN would make the ranking depend on the initial order of the candidate
list rather than on the data. Refusing a non-finite score is what keeps a similarity search
deterministic across runs.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from math import fsum, isfinite, sqrt
from operator import mul

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxVectorValidationError
from okto_grafx.domain.ports.vectormath import DistanceMetric

__all__ = [
    "SCORE_TOLERANCE",
    "PURE_ADAPTER_NAME",
    "PureVectorMath",
    "require_metric",
    "require_positive_k",
    "require_same_length",
]

SCORE_TOLERANCE: float = 1e-9
"""How far an accelerated adapter may differ from this oracle on any single score (BR-7).

The bound is absolute and relative at once: two scores agree when they differ by no more than
this in absolute terms, or by no more than this times the larger magnitude. It is chosen far
above the difference actually measured on embedding-shaped data: an accelerator summing in
pairwise order differs from a correctly rounded sum by a few units in the last place, around
1e-16 relative for the magnitudes an embedding produces.

It is NOT a promise that no ranking can reorder, and an earlier version of this sentence claimed
it was. On catastrophically cancelling input the two summation orders disagree by far more than
any tolerance can absorb -- ``dot([1e16, 1, -1e16], [1, 1, 1])`` is exactly 1.0 here and 0.0
under pairwise summation, which moves that candidate from first place to last. The bound
describes the agreement expected of ordinary embedding data, and
``test_the_tolerance_does_not_promise_agreement_on_cancelling_input`` pins the case where it does
not hold (amendment A85).
"""

PURE_ADAPTER_NAME: str = "pure"
"""The bounded label this adapter reports, matching the ``pure`` selector of DatabaseConfig."""

def require_same_length(a: Sequence[float], b: Sequence[float]) -> int:
    """Return the shared length of two vectors, refusing a pair that cannot be compared.

    Comparing vectors of different lengths is a caller mistake and never damaged bytes, so it
    raises the vector validation error of the taxonomy (A11-revised).
    """
    length_a = len(a)
    length_b = len(b)
    if length_a != length_b:
        raise GrafxVectorValidationError(
            f"Two vectors compared with each other must have the same number of components; "
            f"got {length_a} and {length_b}.",
            field="dimension",
            reason="length_mismatch",
            value=length_a,
            other=length_b,
        )
    return length_a


def _accumulate(terms: object, operation: str) -> float:
    """Return the correctly rounded sum of the terms, refusing one that leaves double range.

    ``math.fsum`` has THREE ways out and only one of them is a value.

    It returns an infinity when a TERM is infinite. It raises ``OverflowError`` when the exact
    running sum passes the largest double while every term is still finite -- reachable from
    three ordinary components of a float64 space. And it raises ``ValueError`` (``-inf + inf in
    fsum``) when the partials it is carrying include infinities of BOTH signs, which needs the
    products themselves to overflow in opposite directions: ``[1e300, 1e300]`` against
    ``[1e300, -1e300]`` does it, every component finite and accepted by the write door.

    That third exit was missed because every test of this guard used inputs whose products stay
    finite, so ``OverflowError`` answered before ``ValueError`` could. All three exits mean the
    same thing to a caller -- there is no finite score to rank with -- so all three leave here as
    the same typed refusal. ``NumpyVectorMath`` already refuses this case, so leaving it would
    make one query answer under ``[accel]`` and crash on the default stdlib adapter.
    """
    try:
        return fsum(terms)  # type: ignore[arg-type]
    except (OverflowError, ValueError) as failure:
        raise GrafxVectorValidationError(
            f"The {operation} of these vectors leaves the range of a double before it can be "
            f"rounded, so there is no finite score to rank with.",
            field="score",
            reason="non_finite_result",
            operation=operation,
            value="overflow",
        ) from failure


def _require_finite(value: float, operation: str) -> float:
    """Return the value, refusing a result that is not finite.

    This is the single guard of the module. It fires for a non-finite input, which poisons the
    accumulation, and for a finite input whose product or sum leaves the range of a double.
    """
    if not isfinite(value):
        raise GrafxVectorValidationError(
            f"The {operation} of these vectors is {value!r}, which is not a finite number; a "
            f"non-finite score has no place in a ranking.",
            field="score",
            reason="non_finite_result",
            operation=operation,
            value=repr(value),
        )
    return value


def require_positive_k(k: int) -> int:
    """Return the neighbor count, refusing one that could not describe a result."""
    if isinstance(k, bool) or not isinstance(k, int):
        raise GrafxConfigurationError(
            f"A neighbor count must be an integer; got {type(k).__name__}.",
            field="k",
            value=repr(k),
        )
    if k < 1:
        raise GrafxConfigurationError(
            f"A neighbor count must be at least 1; got {k}.",
            field="k",
            value=k,
        )
    return k


def require_metric(metric: DistanceMetric) -> DistanceMetric:
    """Return the metric, refusing anything that is not one of the three declared ones."""
    if not isinstance(metric, DistanceMetric):
        raise GrafxConfigurationError(
            f"A distance metric must be a DistanceMetric; got {metric!r}.",
            field="metric",
            value=repr(metric),
        )
    return metric


class PureVectorMath:
    """Distance and ranking primitives in pure Python, with no dependency of any kind.

    The instance is stateless and therefore safe to share: it holds no buffer, no cache and no
    lock, so nothing here can be racing anything else.
    """

    # Repeating a pair score is exact for this stateless oracle: every accumulation uses the
    # same correctly-rounded ``fsum`` path.  The HNSW builder reads this capability directly
    # from the concrete class (never through inheritance) before retaining transient trim
    # scores, so an adapter subclass must opt in again after accounting for its own behaviour.
    _stable_pair_scores_for_construction: bool = True

    __slots__ = ()

    @property
    def name(self) -> str:
        """Return the bounded label of this implementation, safe as a metric label value."""
        return PURE_ADAPTER_NAME

    def dot(self, a: Sequence[float], b: Sequence[float]) -> float:
        """Return the dot product of two vectors of equal length."""
        require_same_length(a, b)
        return _require_finite(
            _accumulate(map(mul, a, b), "dot product"), "dot product"
        )

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float:
        """Return the cosine similarity of two vectors of equal length.

        A vector of zero length points nowhere, so there is no angle to measure. This returns
        0.0 for that pair rather than a NaN: 0.0 is the similarity of two perpendicular
        directions, it keeps the ranking totally ordered, and it is the same answer on every
        adapter. A test pins it, because the obvious floating point expression would produce a
        NaN here and NaN is the one value this component may never hand back.
        """
        return self._cosine_from_length(a, _require_finite(self.norm(a), "norm"), b)

    def _cosine_from_length(
        self, a: Sequence[float], length_a: float, b: Sequence[float]
    ) -> float:
        """Return the cosine similarity of ``a`` to ``b``, given the length of ``a``.

        This is the body of :meth:`cosine` with the left norm supplied by the caller, so that
        a ranking can measure its query once instead of once per candidate (VEC-1). Everything
        after that norm -- the candidate norm, the zero-length rule, the length check, the dot
        product and the finite guard -- runs here in the order it always ran, so a refusal
        surfaces for the same candidate with the same reason whichever door measured ``a``.
        """
        length_b = _require_finite(self.norm(b), "norm")
        if length_a == 0.0 or length_b == 0.0:
            require_same_length(a, b)
            return 0.0
        return _require_finite(
            self.dot(a, b) / (length_a * length_b), "cosine similarity"
        )

    def euclidean(self, a: Sequence[float], b: Sequence[float]) -> float:
        """Return the Euclidean distance between two vectors of equal length."""
        require_same_length(a, b)
        squared = _accumulate(
            ((x - y) * (x - y) for x, y in zip(a, b)), "Euclidean distance"
        )
        return _require_finite(sqrt(squared), "Euclidean distance")

    def norm(self, a: Sequence[float]) -> float:
        """Return the Euclidean length of one vector."""
        return _require_finite(sqrt(_accumulate(map(mul, a, a), "norm")), "norm")

    def normalize(self, a: Sequence[float]) -> tuple[float, ...]:
        """Return the vector scaled to unit length.

        A vector of zero length is returned unchanged, for the reason cosine gives: it has no
        direction to preserve, and dividing by its length would produce a tuple of NaN.
        """
        length = self.norm(a)
        if length == 0.0:
            return tuple(float(component) for component in a)
        return tuple(_require_finite(float(component) / length, "normalize") for component in a)

    def score(self, a: Sequence[float], b: Sequence[float], metric: DistanceMetric) -> float:
        """Return the similarity of two vectors under one metric, higher meaning closer.

        Euclidean distance is negated so that every metric agrees on the direction of better,
        which is what lets one ranking routine serve all three (CONTRACT.md section 4.6).
        """
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

        The tie rule is what makes the ranking a function of the data alone: two candidates that
        score identically would otherwise be ordered by whichever the caller listed first, and
        the same query would answer differently depending on how the candidate list was built.
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
        """Return a scorer of stored vectors against one query, its norm measured once.

        Under cosine the norm of the query is the one term that is the same for every vector,
        and measuring it once per pair was the largest single cost of a ranking or a traversal
        (VEC-1, VEC-4). The scorer measures it at its FIRST call, never here, and a test pins
        both consequences: preparing a query that nothing is scored against refuses nothing --
        so a ranking with no candidates never examines the query, exactly as before -- and a
        query that cannot be measured is refused where the pairwise door refuses it, once there
        is a vector to compare with, with the same reason. Everything after that norm runs in
        :meth:`_cosine_from_length` in the order it always ran. Under dot and Euclidean there is
        no query-only term to keep, so the scorer is the pairwise door itself.
        """
        require_metric(metric)
        if metric is DistanceMetric.DOT:
            return lambda values: self.dot(query, values)
        if metric is DistanceMetric.EUCLIDEAN:
            return lambda values: -self.euclidean(query, values)
        length_query: float | None = None

        def cosine(values: Sequence[float]) -> float:
            nonlocal length_query
            if length_query is None:
                length_query = _require_finite(self.norm(query), "norm")
            return self._cosine_from_length(query, length_query, values)

        return cosine

    def __repr__(self) -> str:
        return f"PureVectorMath(name={PURE_ADAPTER_NAME!r})"
