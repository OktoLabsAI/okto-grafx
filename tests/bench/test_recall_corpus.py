"""The C13 corpus/ground-truth mathematics is deterministic, generous and non-tautological.

What these tests pin, per c13_design_v4 (art_285e6c90):

* the ``uniform-int53-v1`` generator reproduces EXACT bytes — a frozen reference SHA-256 for
  both the float64 corpus and its float32 quantization, so any drift of the recipe (or any
  platform that disagrees at the bit level) fails loudly here rather than moving a frozen
  calibration silently;
* the quantization is REAL (components change), so the TR-4 dtype check can never pass
  tautologically;
* the canonical ``math.fsum`` ground truth matches hand-computed answers, orders totally, and
  the generous tie rule keeps recall@k immune to ties at the cut;
* recall@k behaves at the edges (k larger than the corpus, empty intersections, k <= 0);
* the dtype check aggregates per-query overlaps and fails closed against frozen thresholds;
* the accelerated-oracle differential agrees on itself and refuses a perturbed distance with
  the eps named in the verdict.
"""

from __future__ import annotations

import math

import pytest

from bench.recall_corpus import (
    GENERATOR_NAME,
    cosine_distance,
    differential,
    dtype_check,
    generate_vectors,
    ground_truth,
    quantize_f32,
    recall_at_k,
    sha256_hex,
    vector_bytes_f32,
    vector_bytes_f64,
)

REFERENCE_SEED = 1337
REFERENCE_F64 = "ac1fba32ae960a83c8f8ccfffeff761f8ff3bd1ef6046a96ae9aeba66309f563"
REFERENCE_F32 = "9d7bf3f99ae1223e320fce5bb6508c7eefc341853a2c78215649b79de53321a4"


def test_the_generator_reproduces_frozen_bytes_on_every_platform() -> None:
    """Same seed, same bytes: the cross-platform bit-identity claim, pinned by value."""
    vectors = generate_vectors(REFERENCE_SEED, 4, 8)
    assert GENERATOR_NAME == "uniform-int53-v1"
    assert sha256_hex(vector_bytes_f64(vectors)) == REFERENCE_F64
    assert sha256_hex(vector_bytes_f32(vectors)) == REFERENCE_F32
    assert vectors[0][0] == 0.851070993187762
    assert generate_vectors(REFERENCE_SEED, 4, 8) == vectors
    assert generate_vectors(REFERENCE_SEED + 1, 4, 8) != vectors


def test_every_component_lands_inside_the_unit_interval() -> None:
    """The recipe promises [-1.0, 1.0) exactly."""
    for vector in generate_vectors(7, 16, 32):
        for component in vector:
            assert -1.0 <= component < 1.0


def test_the_float32_quantization_is_genuine_not_tautological() -> None:
    """An int53 component does not generally fit float32 — TR-4 measures a REAL cast."""
    vectors = generate_vectors(REFERENCE_SEED, 4, 8)
    quantized = quantize_f32(vectors)
    changed = sum(
        1
        for original, cast in zip(
            [c for v in vectors for c in v],
            [c for v in quantized for c in v],
            strict=True,
        )
        if original != cast
    )
    assert changed == 32, (
        "quantization changed nothing; the dtype check would be a tautology"
    )
    assert quantize_f32(quantized) == quantized, "a second cast must be the identity"


def test_the_canonical_ground_truth_matches_a_hand_computed_case() -> None:
    """Three vectors, one axis query: distances, order and membership by hand."""
    corpus = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
    truth = ground_truth(corpus, [1.0, 0.0], 1)
    assert truth.ordered[0] == (0.0, 0)
    assert truth.ordered[1][1] == 2  # 1 - 1/sqrt(2)
    assert math.isclose(truth.ordered[1][0], 1.0 - 1.0 / math.sqrt(2.0))
    assert truth.ordered[2] == (1.0, 1)
    assert truth.members == {0}
    assert truth.cut_distance == 0.0


def test_zero_norm_vectors_never_displace_a_genuine_neighbour() -> None:
    """A vector with no direction gets the maximum distance by definition."""
    assert cosine_distance([0.0, 0.0], [1.0, 0.0]) == 2.0
    truth = ground_truth([[0.0, 0.0], [1.0, 0.0]], [1.0, 0.0], 1)
    assert truth.members == {1}


def test_the_generous_tie_rule_admits_every_record_at_the_cut() -> None:
    """Two identical vectors: either answer at k=1 scores full recall."""
    corpus = [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]
    truth = ground_truth(corpus, [1.0, 0.0], 1)
    assert truth.members == {0, 1}
    assert recall_at_k([0], truth, 1) == 1.0
    assert recall_at_k([1], truth, 1) == 1.0
    assert recall_at_k([2], truth, 1) == 0.0


def test_recall_at_the_edges() -> None:
    """k past the corpus keeps everything; empty intersections score zero; k<=0 refuses."""
    corpus = [[1.0, 0.0], [0.0, 1.0]]
    truth = ground_truth(corpus, [1.0, 0.0], 10)
    assert truth.members == {0, 1}
    assert recall_at_k([0, 1], truth, 10) == pytest.approx(0.2)
    assert recall_at_k([], truth, 10) == 0.0
    with pytest.raises(ValueError):
        ground_truth(corpus, [1.0, 0.0], 0)
    with pytest.raises(ValueError):
        recall_at_k([0], truth, 0)


def test_the_dtype_check_aggregates_and_fails_closed_against_frozen_thresholds() -> (
    None
):
    """Mean and min overlaps come from per-query truth; thresholds decide, nothing else."""
    vectors = generate_vectors(REFERENCE_SEED, 64, 16)
    quantized = quantize_f32(vectors)
    queries = generate_vectors(4242, 8, 16)
    verdict = dtype_check(vectors, quantized, queries, 4)
    assert len(verdict.per_query) == 8
    assert 0.0 <= verdict.min_overlap <= verdict.mean_overlap <= 1.0
    assert verdict.passes(mean_overlap_min=0.99, per_query_overlap_min=0.90) == (
        verdict.mean_overlap >= 0.99 and verdict.min_overlap >= 0.90
    )
    assert not verdict.passes(mean_overlap_min=1.1, per_query_overlap_min=1.1)
    with pytest.raises(ValueError):
        dtype_check(vectors, quantized, [], 4)


def test_the_differential_accepts_the_canonical_oracle_against_itself() -> None:
    """An accelerated path that IS the canonical one must agree on every query."""
    corpus = generate_vectors(REFERENCE_SEED, 32, 8)
    queries = generate_vectors(4242, 4, 8)
    verdict = differential(corpus, queries, 3, cosine_distance, eps_rel=1e-9)
    assert verdict.agreed
    assert verdict.queries_checked == 4
    assert verdict.first_disagreement == ""


def test_the_differential_refuses_a_perturbed_distance_and_names_the_eps() -> None:
    """A distance off by more than eps_rel is a disagreement, reported with its delta."""
    corpus = generate_vectors(REFERENCE_SEED, 32, 8)
    queries = generate_vectors(4242, 4, 8)

    def drifted(left: list[float], right: list[float]) -> float:
        return cosine_distance(left, right) * (1.0 + 5e-9)

    verdict = differential(corpus, queries, 3, drifted, eps_rel=1e-12)
    assert not verdict.agreed
    assert verdict.first_disagreement != ""


def test_the_differential_refuses_a_membership_swap() -> None:
    """An oracle that reorders the cut is refused on membership, before any eps math."""
    corpus = [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]]
    queries = [[1.0, 0.0]]

    def inverted(left: list[float], right: list[float]) -> float:
        return -cosine_distance(left, right)

    verdict = differential(corpus, queries, 1, inverted, eps_rel=1e-9)
    assert not verdict.agreed
    assert "member sets differ" in verdict.first_disagreement


def test_the_differential_checks_every_distance_not_only_members() -> None:
    """A drift on a NON-member distance is a disagreement (v4: every distance of the subset)."""
    corpus = [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]]
    queries = [[1.0, 0.0]]

    def drifted_far_only(left: list[float], right: list[float]) -> float:
        exact = cosine_distance(left, right)
        return exact + 1000.0 if exact > 0.5 else exact

    verdict = differential(corpus, queries, 1, drifted_far_only, eps_rel=1e-9)
    assert not verdict.agreed
    assert "exceeds eps_rel" in verdict.first_disagreement


def test_a_nan_anywhere_in_the_subset_is_a_disagreement() -> None:
    """NaN on a non-member refuses; a comparison that NaN can slip past is fail-open."""
    corpus = [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]]
    queries = [[1.0, 0.0]]

    def poisoned(left: list[float], right: list[float]) -> float:
        exact = cosine_distance(left, right)
        return float("nan") if exact > 0.5 else exact

    verdict = differential(corpus, queries, 1, poisoned, eps_rel=1e-9)
    assert not verdict.agreed
    assert "non-finite" in verdict.first_disagreement


@pytest.mark.parametrize("bad", (float("nan"), float("inf"), -1e-9))
def test_an_unusable_tolerance_is_refused_before_any_comparison(bad: float) -> None:
    """NaN, infinity and negative tolerances cannot refuse anything, so THEY are refused."""
    corpus = [[1.0, 0.0], [0.0, 1.0]]
    with pytest.raises(ValueError, match="finite non-negative"):
        differential(corpus, [[1.0, 0.0]], 1, cosine_distance, eps_rel=bad)


def test_compare_truths_agrees_on_identical_truths_and_names_nonfinite_sides() -> None:
    """The shared comparator both oracles delegate to: agreement, and the NaN refusal path."""
    from bench.recall_corpus import GroundTruth, compare_truths

    corpus = [[1.0, 0.0], [0.0, 1.0]]
    truth = ground_truth(corpus, [1.0, 0.0], 1)
    assert compare_truths(truth, truth, eps_rel=1e-9) == ""
    poisoned = GroundTruth(
        ordered=tuple(
            (float("nan") if index == 1 else score, index)
            for score, index in truth.ordered
        ),
        cut_distance=truth.cut_distance,
        members=truth.members,
    )
    disagreement = compare_truths(truth, poisoned, eps_rel=1e-9)
    assert "non-finite" in disagreement


def test_a_tie_broken_by_the_cast_is_a_perfect_overlap_not_a_spurious_zero() -> None:
    """The audit case: pre.members {0,1} (tie), post.members {1} -- bilateral generosity
    scores 1.0 where the one-sided formula scored 0.0."""
    from bench.recall_corpus import generous_overlap

    pre = ground_truth([[1.0, 1e-9], [1.0, -1e-9], [0.0, 1.0]], [1.0, 0.0], 1)
    assert pre.members == {0, 1}, "the construction must tie records 0 and 1"
    post = ground_truth([[1.0, 1e-3], [1.0, 1e-9], [0.0, 1.0]], [1.0, 0.0], 1)
    assert post.members == {1}, "the cast-like perturbation must break the tie"
    overlap = generous_overlap(pre, post, 1)
    assert overlap == 1.0
    verdict = dtype_check(
        [[1.0, 1e-9], [1.0, -1e-9], [0.0, 1.0]],
        [[1.0, 1e-3], [1.0, 1e-9], [0.0, 1.0]],
        [[1.0, 0.0]],
        1,
    )
    assert verdict.per_query == (1.0,)


def test_a_genuinely_displaced_ranking_still_drops_the_overlap() -> None:
    """Generosity is not amnesty: disjoint generous bands score exactly their intersection."""
    from bench.recall_corpus import generous_overlap

    pre = ground_truth([[1.0, 0.0], [0.5, 0.5], [0.0, 1.0]], [1.0, 0.0], 1)
    post = ground_truth([[0.0, 1.0], [0.5, 0.5], [1.0, 0.0]], [1.0, 0.0], 1)
    assert pre.members == {0}
    assert post.members == {2}
    assert generous_overlap(pre, post, 1) == 0.0


def test_the_post_side_tie_keeps_its_generosity_too() -> None:
    """The mirror case: a tie that only exists after the cast also counts in full."""
    from bench.recall_corpus import generous_overlap

    pre = ground_truth([[1.0, 1e-3], [1.0, 1e-9], [0.0, 1.0]], [1.0, 0.0], 1)
    post = ground_truth([[1.0, 1e-9], [1.0, -1e-9], [0.0, 1.0]], [1.0, 0.0], 1)
    assert pre.members == {1}
    assert post.members == {0, 1}
    assert generous_overlap(pre, post, 1) == 1.0


def test_generous_overlap_caps_at_one_and_refuses_bad_k() -> None:
    """Massive tie bands cannot inflate past 1.0; k <= 0 refuses."""
    from bench.recall_corpus import generous_overlap

    corpus = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
    tie = ground_truth(corpus, [1.0, 0.0], 1)
    assert tie.members == {0, 1, 2}
    assert generous_overlap(tie, tie, 1) == 1.0
    with pytest.raises(ValueError):
        generous_overlap(tie, tie, 0)


def test_duplicate_record_ids_in_a_truth_are_refused_before_the_distance_map() -> None:
    """A dict would keep only the last duplicate and could hide the very NaN we hunt."""
    from bench.recall_corpus import GroundTruth, compare_truths

    corpus = [[1.0, 0.0], [0.0, 1.0]]
    canonical = ground_truth(corpus, [1.0, 0.0], 1)
    duplicated = GroundTruth(
        ordered=((float("nan"), 0),) + canonical.ordered,
        cut_distance=canonical.cut_distance,
        members=canonical.members,
    )
    disagreement = compare_truths(canonical, duplicated, eps_rel=1e-9)
    assert "more than once" in disagreement


def test_a_hostile_float_subclass_is_refused_outright() -> None:
    """The tolerance demands an EXACT built-in float or int; a subclass whose comparison
    operators could lie never reaches any comparison at all."""

    class Generous(float):
        """A float whose comparisons lie."""

        def __le__(self, other: object) -> bool:
            return True

        def __ge__(self, other: object) -> bool:
            return True

    corpus = [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]]
    with pytest.raises(ValueError, match="built-in"):
        differential(corpus, [[1.0, 0.0]], 1, cosine_distance, eps_rel=Generous(1e-9))
    exact = differential(corpus, [[1.0, 0.0]], 1, cosine_distance, eps_rel=1e-9)
    assert exact.agreed, "an exact built-in float of the same value still works"


@pytest.mark.parametrize("bad_k", (True, 1.5, float("nan"), float("inf"), 0, -3, "3"))
def test_generous_overlap_refuses_every_non_exact_k(bad_k: object) -> None:
    """bools, floats, NaN, infinity, fractions, zero and strings all refuse."""
    from bench.recall_corpus import generous_overlap

    truth = ground_truth([[1.0, 0.0], [0.0, 1.0]], [1.0, 0.0], 1)
    with pytest.raises(ValueError):
        generous_overlap(truth, truth, bad_k)  # type: ignore[arg-type]


def test_generous_overlap_refuses_phantom_members() -> None:
    """A member no oracle scored would inflate the intersection; it refuses instead."""
    from bench.recall_corpus import GroundTruth, generous_overlap

    truth = ground_truth([[1.0, 0.0], [0.0, 1.0]], [1.0, 0.0], 1)
    phantom = GroundTruth(
        ordered=truth.ordered,
        cut_distance=truth.cut_distance,
        members=frozenset(truth.members | {99}),
    )
    with pytest.raises(ValueError, match="never scored"):
        generous_overlap(truth, phantom, 1)
    with pytest.raises(ValueError, match="never scored"):
        generous_overlap(phantom, truth, 1)


def test_dtype_check_refuses_a_corpus_that_lost_or_reshaped_records() -> None:
    """A missing record or a changed dimension is an identity break, never a 1.0."""
    pre = [[1.0, 0.0], [0.0, 1.0]]
    with pytest.raises(ValueError, match="SAME records"):
        dtype_check(pre, pre[:1], [[1.0, 0.0]], 1)
    with pytest.raises(ValueError, match="changed dimension"):
        dtype_check(pre, [[1.0, 0.0], [0.0, 1.0, 0.0]], [[1.0, 0.0]], 1)


def test_an_empty_differential_refuses_instead_of_passing_vacuously() -> None:
    """Zero queries or an empty corpus prove nothing; the quietest fail-open refuses."""
    corpus = [[1.0, 0.0], [0.0, 1.0]]
    with pytest.raises(ValueError, match="vacuous"):
        differential(corpus, [], 1, cosine_distance, eps_rel=1e-9)
    with pytest.raises(ValueError, match="vacuous"):
        differential([], [[1.0, 0.0]], 1, cosine_distance, eps_rel=1e-9)


def test_the_disagreement_diagnostics_are_ascii_only() -> None:
    """Windows CP-1252 consoles must be able to print every CLI diagnostic."""
    corpus = [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]]

    def drifted(left: list[float], right: list[float]) -> float:
        return cosine_distance(left, right) * (1.0 + 5e-9)

    verdict = differential(corpus, [[1.0, 0.0]], 1, drifted, eps_rel=1e-12)
    assert not verdict.agreed
    verdict.first_disagreement.encode("ascii")
