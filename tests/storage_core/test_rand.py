"""SplitMix64 is the only sanctioned randomness in the pure core (CONTRACT.md G2b, A5).

The point of the generator is not that it looks random; it is that the same seed produces the
same sequence forever, on every platform and every build. These tests pin the actual sequence
against the reference implementation, so a future rewrite that is faster but different fails
here rather than in a vector index that quietly stops reproducing.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.rand import GOLDEN_GAMMA, MASK_64, SplitMix64

REFERENCE_SEED_42: tuple[int, ...] = (
    13679457532755275413,
    2949826092126892291,
    5139283748462763858,
    6349198060258255764,
    701532786141963250,
)
"""The first five draws of the reference splitmix64 for seed 42."""


def reference_splitmix64(seed: int, count: int) -> list[int]:
    """Return the reference splitmix64 stream, written out in full as the oracle.

    Restating the algorithm here rather than importing it is the point: the test compares the
    delivered generator against the published recurrence, not against itself.
    """
    mask = (1 << 64) - 1
    state = seed & mask
    draws: list[int] = []
    for _ in range(count):
        state = (state + 0x9E3779B97F4A7C15) & mask
        mixed = state
        mixed = ((mixed ^ (mixed >> 30)) * 0xBF58476D1CE4E5B9) & mask
        mixed = ((mixed ^ (mixed >> 27)) * 0x94D049BB133111EB) & mask
        draws.append(mixed ^ (mixed >> 31))
    return draws


def test_the_sequence_matches_the_reference_implementation() -> None:
    generator = SplitMix64(42)
    assert tuple(generator.next_u64() for _ in range(5)) == REFERENCE_SEED_42


@pytest.mark.parametrize("seed", [0, 1, 42, 2**63, (1 << 64) - 1, 20260819])
def test_the_stream_agrees_with_the_published_recurrence(seed: int) -> None:
    generator = SplitMix64(seed)
    assert [generator.next_u64() for _ in range(32)] == reference_splitmix64(seed, 32)


def test_seed_zero_matches_the_reference_implementation() -> None:
    generator = SplitMix64(0)
    assert generator.next_u64() == 16294208416658607535
    assert generator.next_u64() == 7960286522194355700


def test_the_same_seed_replays_the_same_stream() -> None:
    first = SplitMix64(20260819)
    second = SplitMix64(20260819)
    assert [first.next_u64() for _ in range(64)] == [second.next_u64() for _ in range(64)]


def test_a_different_seed_gives_a_different_stream() -> None:
    assert SplitMix64(1).next_u64() != SplitMix64(2).next_u64()


def test_every_draw_is_an_unsigned_64_bit_integer() -> None:
    generator = SplitMix64(7)
    for _ in range(512):
        draw = generator.next_u64()
        assert isinstance(draw, int)
        assert 0 <= draw <= MASK_64


def test_the_seed_is_reduced_to_64_bits() -> None:
    assert SplitMix64(1 << 64).state == 0
    assert SplitMix64(-1).state == MASK_64
    assert SplitMix64((1 << 64) + 5).next_u64() == SplitMix64(5).next_u64()


def test_the_state_advances_by_the_golden_gamma() -> None:
    generator = SplitMix64(0)
    generator.next_u64()
    assert generator.state == GOLDEN_GAMMA


def test_clone_continues_the_stream_independently() -> None:
    generator = SplitMix64(99)
    generator.next_u64()
    twin = generator.clone()
    assert twin.state == generator.state
    assert [generator.next_u64() for _ in range(8)] == [twin.next_u64() for _ in range(8)]
    generator.next_u64()
    assert twin.state != generator.state


def test_next_float_stays_inside_the_unit_interval() -> None:
    generator = SplitMix64(5)
    draws = [generator.next_float() for _ in range(4096)]
    assert all(0.0 <= draw < 1.0 for draw in draws)
    assert min(draws) < 0.05
    assert max(draws) > 0.95


def test_next_float_is_reproducible() -> None:
    assert SplitMix64(3).next_float() == SplitMix64(3).next_float()


def test_next_below_stays_inside_the_bound() -> None:
    generator = SplitMix64(11)
    for bound in (1, 2, 3, 7, 10, 1000, (1 << 32) + 1):
        for _ in range(64):
            assert 0 <= generator.next_below(bound) < bound


def test_next_below_one_is_always_zero() -> None:
    generator = SplitMix64(13)
    assert [generator.next_below(1) for _ in range(16)] == [0] * 16


def test_next_below_covers_every_value_of_a_small_bound() -> None:
    generator = SplitMix64(17)
    seen = {generator.next_below(5) for _ in range(200)}
    assert seen == {0, 1, 2, 3, 4}


def test_next_below_is_close_to_uniform_on_a_non_power_of_two() -> None:
    # A modulo without rejection would favour the low residues; with 60000 draws over 3 buckets
    # the bias of a plain remainder is far outside this window.
    generator = SplitMix64(20260819)
    counts = [0, 0, 0]
    for _ in range(60000):
        counts[generator.next_below(3)] += 1
    assert all(19000 <= count <= 21000 for count in counts), counts


def test_a_power_of_two_bound_consumes_one_draw() -> None:
    generator = SplitMix64(23)
    twin = generator.clone()
    generator.next_below(8)
    twin.next_u64()
    assert generator.state == twin.state


@pytest.mark.parametrize("bound", [0, -1, -1000])
def test_a_bound_below_one_is_refused(bound: int) -> None:
    with pytest.raises(GrafxConfigurationError):
        SplitMix64(1).next_below(bound)


def test_a_bound_above_the_range_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError):
        SplitMix64(1).next_below((1 << 64) + 1)


@pytest.mark.parametrize("seed", ["42", 4.2, None, True])
def test_a_seed_that_is_not_an_integer_is_refused(seed: object) -> None:
    with pytest.raises(GrafxConfigurationError):
        SplitMix64(seed)  # type: ignore[arg-type]


def test_a_bound_that_is_not_an_integer_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError):
        SplitMix64(1).next_below(True)  # type: ignore[arg-type]


def test_the_repr_names_the_state() -> None:
    assert repr(SplitMix64(0)) == "SplitMix64(state=0x0000000000000000)"
