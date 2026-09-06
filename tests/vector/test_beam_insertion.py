"""The ranked insertion behind the beam keeps the total order it always had (VEC-5).

``_insert_ranked`` finds the position of a scored node by binary search now, where it used to
scan the list. The order it maintains -- descending by score, ascending by node id, an entry
equal to one already present placed after it -- is the tie rule of the port and the determinism
of every traversal, so the new insertion is held to a reference written as that rule reads, on
sequences chosen to hit every kind of tie, and with every kind of limit.
"""

from __future__ import annotations

import random

from okto_grafx.domain.vector.hnsw import _insert_ranked

SCORES: tuple[float, ...] = (
    -1.0,
    -0.5,
    -0.0,
    0.0,
    0.5,
    1.0,
    1.0,
    2.0,
    float("inf"),
    -float("inf"),
)
"""Few distinct values, so that score ties and both zeros occur constantly."""

NODES: int = 12
"""Few distinct node ids, so that full duplicates of a scored node occur too."""


def _reference_insert(
    ranked: list[tuple[float, int]], score: float, node: int, limit: int | None = None
) -> None:
    """Insert as the rule reads: before the first entry it beats, after every entry it ties."""
    for position, existing in enumerate(ranked):
        if score > existing[0] or (score == existing[0] and node < existing[1]):
            ranked.insert(position, (score, node))
            break
    else:
        ranked.append((score, node))
    if limit is not None and len(ranked) > limit:
        del ranked[limit:]


def _observable(ranked: list[tuple[float, int]]) -> list[tuple[str, int]]:
    """Return the list with each score spelled out, so that -0.0 and 0.0 stay distinguishable."""
    return [(repr(score), node) for score, node in ranked]


def test_the_binary_insertion_matches_the_linear_reference_on_every_kind_of_tie() -> (
    None
):
    generator = random.Random(0x5EC5)
    for limit in (None, 1, 3, 8, 64):
        for _sequence in range(40):
            actual: list[tuple[float, int]] = []
            expected: list[tuple[float, int]] = []
            for _step in range(64):
                score = generator.choice(SCORES)
                node = generator.randrange(NODES)
                _insert_ranked(actual, score, node, limit)
                _reference_insert(expected, score, node, limit)
                assert _observable(actual) == _observable(expected)
                assert limit is None or len(actual) <= limit


def test_equal_scores_are_ordered_by_ascending_node_identifier() -> None:
    ranked: list[tuple[float, int]] = []
    for node in (7, 3, 9, 1, 5):
        _insert_ranked(ranked, 0.5, node)
    assert ranked == [(0.5, 1), (0.5, 3), (0.5, 5), (0.5, 7), (0.5, 9)]


def test_a_better_score_goes_first_whatever_its_node_identifier() -> None:
    ranked: list[tuple[float, int]] = []
    _insert_ranked(ranked, 0.1, 1)
    _insert_ranked(ranked, 0.9, 99)
    _insert_ranked(ranked, 0.5, 50)
    assert ranked == [(0.9, 99), (0.5, 50), (0.1, 1)]


def test_the_limit_drops_the_worst_entries_and_only_after_the_insertion() -> None:
    ranked: list[tuple[float, int]] = []
    for score, node in ((0.2, 2), (0.4, 4), (0.6, 6)):
        _insert_ranked(ranked, score, node, 3)
    _insert_ranked(ranked, 0.5, 5, 3)
    assert ranked == [(0.6, 6), (0.5, 5), (0.4, 4)]
    _insert_ranked(ranked, 0.1, 1, 3)
    assert ranked == [(0.6, 6), (0.5, 5), (0.4, 4)]


def test_without_a_limit_nothing_is_ever_dropped() -> None:
    ranked: list[tuple[float, int]] = []
    for node in range(200):
        _insert_ranked(ranked, float(node % 7), node)
    assert len(ranked) == 200
    assert ranked == sorted(ranked, key=lambda item: (-item[0], item[1]))
