"""Deterministic corpus, exact ground truth and recall@k for the C13 calibration (SPEC-VEC FR-8).

Pure module: no engine imports, no numpy requirement, no clock and no filesystem. Everything
here is the NORMATIVE side of c13_design_v4 (art_285e6c90): the ``uniform-int53-v1`` generator
whose bytes are bit-identical across platforms, the canonical ``math.fsum`` ground truth whose
result is a function of those bytes alone, the generous tie rule that keeps recall@k free of
one-ulp flips, the genuine float32 quantization check TR-4 asks for, and the differential that
an accelerated oracle must pass before its answers may stand in for the canonical ones.

Nothing in this module measures time or writes a file: the harness stage owns profiles,
subprocesses and publication order; this module owns only the mathematics, so its functions are
deterministic given their arguments — the property every test here pins.
"""

from __future__ import annotations

import hashlib
import math
import random
import struct
from dataclasses import dataclass

GENERATOR_NAME: str = "uniform-int53-v1"
"""The frozen corpus recipe: ``(getrandbits(53) - 2**52) / 2**52`` per component.

Mersenne Twister's ``getrandbits`` is integer-pure — identical on every CPython and platform —
and the division by a power of two is exact in binary64, so the float64 corpus bytes are
bit-identical everywhere without touching libm. A 53-bit significand does NOT generally fit in
float32, so the float64→float32 cast is a REAL quantization (IEEE round-to-nearest-even), which
is exactly what makes the TR-4 dtype check non-tautological.
"""

_HALF_RANGE: int = 2**52


def generate_vectors(seed: int, count: int, dimension: int) -> list[list[float]]:
    """Return ``count`` vectors of ``dimension`` float64 components in [-1.0, 1.0).

    Deterministic in (seed, count, dimension); no normalization (normalizing would drag libm's
    ``sqrt`` into the corpus bytes and break their cross-platform bit-identity).
    """
    source = random.Random(seed)
    return [
        [(source.getrandbits(53) - _HALF_RANGE) / _HALF_RANGE for _ in range(dimension)]
        for _ in range(count)
    ]


def vector_bytes_f64(vectors: list[list[float]]) -> bytes:
    """Serialize vectors as IEEE-754 binary64 little-endian, row-major."""
    flat = [component for vector in vectors for component in vector]
    return struct.pack(f"<{len(flat)}d", *flat)


def vector_bytes_f32(vectors: list[list[float]]) -> bytes:
    """Serialize vectors as IEEE-754 binary32 little-endian, row-major (RNE quantization)."""
    flat = [component for vector in vectors for component in vector]
    return struct.pack(f"<{len(flat)}f", *flat)


def quantize_f32(vectors: list[list[float]]) -> list[list[float]]:
    """Return the vectors after a float64→float32→float64 round trip.

    ``struct`` performs the IEEE round-to-nearest-even cast, so the values returned are exactly
    the ones a float32 store holds, expressed in float64 for exact downstream arithmetic.
    """
    quantized: list[list[float]] = []
    for vector in vectors:
        packed = struct.pack(f"<{len(vector)}f", *vector)
        quantized.append(list(struct.unpack(f"<{len(vector)}f", packed)))
    return quantized


def sha256_hex(payload: bytes) -> str:
    """Return the lowercase hex SHA-256 of a serialized corpus."""
    return hashlib.sha256(payload).hexdigest()


def cosine_distance(left: list[float], right: list[float]) -> float:
    """Return the canonical cosine distance ``1 - dot/(|a||b|)`` with exactly-rounded sums.

    ``math.fsum`` makes every sum independent of accumulation order, so this number is a function
    of the input BYTES alone — the property that lets one canonical ground truth exist at all.
    A zero-norm vector has no direction; its distance is defined as the maximum (2.0) so it can
    never displace a genuine neighbour.
    """
    dot = math.fsum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(math.fsum(a * a for a in left))
    right_norm = math.sqrt(math.fsum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 2.0
    return 1.0 - dot / (left_norm * right_norm)


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """The exact answer set for one query: distances, the cut, and the generous member set."""

    ordered: tuple[tuple[float, int], ...]
    cut_distance: float
    members: frozenset[int]


def ground_truth(
    corpus: list[list[float]],
    query: list[float],
    k: int,
    *,
    distance=cosine_distance,
) -> GroundTruth:
    """Return the generous exact ground truth of one query against the whole corpus.

    Total order is ``(distance asc, record_id asc)``; the generous rule admits EVERY record
    whose distance is <= the k-th smallest, so an ANN answer can never lose recall to a tie or
    to a one-ulp flip inside the tied band. ``k`` larger than the corpus keeps the whole corpus.
    """
    if k <= 0:
        raise ValueError(f"recall@k needs k >= 1; got {k}.")
    scored = sorted(
        ((distance(query, vector), index) for index, vector in enumerate(corpus)),
    )
    cut = scored[min(k, len(scored)) - 1][0] if scored else 0.0
    members = frozenset(index for score, index in scored if score <= cut)
    return GroundTruth(ordered=tuple(scored), cut_distance=cut, members=members)


def recall_at_k(answer: list[int], truth: GroundTruth, k: int) -> float:
    """Return ``|top-k(answer) ∩ generous truth| / k`` — the number the gauge publishes."""
    if k <= 0:
        raise ValueError(f"recall@k needs k >= 1; got {k}.")
    top = answer[:k]
    return len(set(top) & truth.members) / k


@dataclass(frozen=True, slots=True)
class DtypeCheck:
    """The TR-4 verdict: per-query overlaps of pre- vs post-quantization exact top-k."""

    mean_overlap: float
    min_overlap: float
    per_query: tuple[float, ...]

    def passes(self, *, mean_overlap_min: float, per_query_overlap_min: float) -> bool:
        """Return whether the frozen thresholds hold; the harness fails closed when not."""
        return (
            self.mean_overlap >= mean_overlap_min
            and self.min_overlap >= per_query_overlap_min
        )


def dtype_check(
    pre_quantization: list[list[float]],
    quantized: list[list[float]],
    queries: list[list[float]],
    k: int,
) -> DtypeCheck:
    """Measure how much the float32 quantization disturbs the EXACT top-k (TR-4).

    Both sides use the canonical ground truth with generous ties: ``overlap`` per query is
    ``|top-k(pre) ∩ generous(post)| / k`` intersected symmetrically — a rank disturbance shows
    up as a member that fell out of both generous bands.
    """
    overlaps: list[float] = []
    for query in queries:
        pre = ground_truth(pre_quantization, query, k)
        post = ground_truth(quantized, query, k)
        top_pre = [index for _, index in pre.ordered[: min(k, len(pre.ordered))]]
        overlap = len(set(top_pre) & post.members) / k
        overlaps.append(overlap)
    if not overlaps:
        raise ValueError("the dtype check needs at least one query.")
    return DtypeCheck(
        mean_overlap=math.fsum(overlaps) / len(overlaps),
        min_overlap=min(overlaps),
        per_query=tuple(overlaps),
    )


@dataclass(frozen=True, slots=True)
class DifferentialVerdict:
    """Whether an accelerated oracle agreed with the canonical one on the frozen subset."""

    agreed: bool
    queries_checked: int
    first_disagreement: str


def _require_eps_rel(eps_rel: float) -> float:
    """Refuse a tolerance that cannot refuse anything: NaN, infinity or a negative number."""
    if not isinstance(eps_rel, float) or not math.isfinite(eps_rel) or eps_rel < 0.0:
        raise ValueError(
            f"the accelerated-oracle tolerance must be a finite non-negative float; "
            f"got {eps_rel!r}."
        )
    return eps_rel


def compare_truths(
    canonical: GroundTruth,
    fast,
    *,
    eps_rel: float,
) -> str:
    """Return "" when a fast truth agrees with the canonical one, else the disagreement.

    Agreement demands identical generous membership AND every distance of the WHOLE subset —
    member or not — finite and within ``eps_rel`` relative tolerance. The comparison is
    written to REFUSE on NaN: a non-finite distance on either side, anywhere, is a
    disagreement, never a silent pass.
    """
    _require_eps_rel(eps_rel)
    if canonical.members != fast.members:
        return (
            f"member sets differ ({sorted(canonical.members)[:5]}... vs "
            f"{sorted(fast.members)[:5]}...)"
        )
    fast_distances = {index: score for score, index in fast.ordered}
    if set(fast_distances) != {index for _, index in canonical.ordered}:
        return "the fast truth scored a different record set"
    for score, index in canonical.ordered:
        other = fast_distances[index]
        if not math.isfinite(score) or not math.isfinite(other):
            return f"record {index}: non-finite distance ({score!r} vs {other!r})"
        delta = abs(other - score)
        if not delta / max(1.0, abs(score)) <= eps_rel:
            return (
                f"record {index}: |Δdistance| {delta:.3e} exceeds eps_rel {eps_rel:g}"
            )
    return ""


def differential(
    corpus: list[list[float]],
    queries: list[list[float]],
    k: int,
    accelerated_distance,
    *,
    eps_rel: float,
) -> DifferentialVerdict:
    """Compare an accelerated distance against the canonical one on a frozen subset.

    Agreement demands BOTH: identical generous top-k membership per query, and EVERY distance
    of the subset — member or not — finite and within ``eps_rel`` relative tolerance
    (c13_design_v4: "toda distância do subconjunto"). Any disagreement, any NaN anywhere, and
    any unusable tolerance is fail-closed — the accelerated path may speed the canonical
    answer up, never replace it.
    """
    _require_eps_rel(eps_rel)
    for query_index, query in enumerate(queries):
        canonical = ground_truth(corpus, query, k)
        fast = ground_truth(corpus, query, k, distance=accelerated_distance)
        disagreement = compare_truths(canonical, fast, eps_rel=eps_rel)
        if disagreement:
            return DifferentialVerdict(
                agreed=False,
                queries_checked=query_index + 1,
                first_disagreement=f"query {query_index}: {disagreement}",
            )
    return DifferentialVerdict(
        agreed=True, queries_checked=len(queries), first_disagreement=""
    )
