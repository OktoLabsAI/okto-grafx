"""Deterministic, explicitly seeded randomness for the pure core (CONTRACT.md G2b, amendment A5).

``random`` is forbidden inside ``domain/`` and ``engine/``. An unseeded generator makes a failing
run impossible to replay, and replayability is a requirement of both specs: seeded interleavings,
seeded corpora and seeded fault injection. Every component that needs a random-looking number
takes one of these generators, built from a seed the caller chose and recorded.

SplitMix64 is the generator Java calls ``SplittableRandom`` and Rust calls ``splitmix64``: one
64-bit addition of a fixed gamma followed by an avalanche of three mixing steps. It holds no
state beyond a single 64-bit word, it is short enough to read in one screen, and the same seed
yields the same sequence on every platform and every Python build, because every operation is
integer arithmetic masked to 64 bits.
"""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxConfigurationError

__all__ = [
    "MASK_64",
    "GOLDEN_GAMMA",
    "FLOAT_MANTISSA_BITS",
    "SplitMix64",
]

MASK_64: int = 0xFFFFFFFFFFFFFFFF
"""Every intermediate result is reduced modulo two to the sixty-fourth power."""

GOLDEN_GAMMA: int = 0x9E3779B97F4A7C15
"""The odd increment of the state word: the 64-bit fixed point of the golden ratio."""

FLOAT_MANTISSA_BITS: int = 53
"""Bits of a draw that fit exactly in a double, which is what next_float consumes."""

_MIX_MULTIPLIER_A: int = 0xBF58476D1CE4E5B9
_MIX_MULTIPLIER_B: int = 0x94D049BB133111EB
_FLOAT_SCALE: float = 1.0 / float(1 << FLOAT_MANTISSA_BITS)
_MODULUS_64: int = 1 << 64


class SplitMix64:
    """A seeded 64-bit generator whose sequence depends on nothing but its seed.

    The generator is a value, not a service: two instances built from the same seed produce the
    same sequence forever, and an instance never reads a clock, an address or an environment.
    That is what makes a failing property run reproducible from the seed alone.
    """

    __slots__ = ("_state",)

    def __init__(self, seed: int) -> None:
        """Start a stream at the given seed, which is reduced to its low sixty-four bits."""
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise GrafxConfigurationError(
                f"A SplitMix64 seed must be an integer; got {type(seed).__name__}.",
                field="seed",
                value=repr(seed),
            )
        self._state: int = seed & MASK_64

    @property
    def state(self) -> int:
        """Return the current state word, which is all that is needed to resume the stream."""
        return self._state

    def clone(self) -> SplitMix64:
        """Return an independent generator positioned exactly where this one stands."""
        twin = SplitMix64(0)
        twin._state = self._state
        return twin

    def next_u64(self) -> int:
        """Return the next draw as an unsigned 64-bit integer."""
        self._state = (self._state + GOLDEN_GAMMA) & MASK_64
        mixed = self._state
        mixed = ((mixed ^ (mixed >> 30)) * _MIX_MULTIPLIER_A) & MASK_64
        mixed = ((mixed ^ (mixed >> 27)) * _MIX_MULTIPLIER_B) & MASK_64
        return mixed ^ (mixed >> 31)

    def next_float(self) -> float:
        """Return the next draw as a double in the half-open interval from zero to one.

        The draw keeps the 53 high bits, which is exactly the mantissa of a double, so every
        representable value in the interval is reachable and none is reachable twice.
        """
        return float(self.next_u64() >> (64 - FLOAT_MANTISSA_BITS)) * _FLOAT_SCALE

    def next_below(self, bound: int) -> int:
        """Return the next draw reduced to the range below bound, without modulo bias.

        A plain remainder favours the low values whenever the bound does not divide two to the
        sixty-fourth power. This rejects the short tail of the range instead, so every value
        below the bound is equally likely; the rejection loop terminates with probability one and
        in practice on the first draw.
        """
        if isinstance(bound, bool) or not isinstance(bound, int):
            raise GrafxConfigurationError(
                f"A SplitMix64 bound must be an integer; got {type(bound).__name__}.",
                field="bound",
                value=repr(bound),
            )
        if bound < 1:
            raise GrafxConfigurationError(
                f"A SplitMix64 bound must be at least 1; got {bound}.",
                field="bound",
                value=bound,
            )
        if bound > _MODULUS_64:
            raise GrafxConfigurationError(
                f"A SplitMix64 bound may not exceed 2**64; got {bound}.",
                field="bound",
                value=bound,
            )
        if bound & (bound - 1) == 0:
            # A power of two consumes the low bits with no bias and no rejection.
            return self.next_u64() & (bound - 1)
        threshold = _MODULUS_64 - (_MODULUS_64 % bound)
        while True:
            draw = self.next_u64()
            if draw < threshold:
                return draw % bound

    def __repr__(self) -> str:
        return f"SplitMix64(state=0x{self._state:016x})"
