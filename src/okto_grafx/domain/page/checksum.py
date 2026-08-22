"""CRC-32C, the integrity check under every page and every log record (CONTRACT.md section 6).

The engine writes one checksum algorithm and only one: Castagnoli's CRC-32C, the polynomial
0x1EDC6F41, evaluated in its reflected form 0x82F63B78 so the message is consumed low bit first.
It is the same function the hardware instruction on modern processors computes, which is what
lets an accelerated adapter be installed here without changing a single stored byte.

The reference implementation is table driven and pure Python: the 256-entry table is built once
at import and the loop is a byte-at-a-time reduction. Nothing here touches a mechanism, so the
domain can verify its own bytes without asking an adapter for permission.

WHY THERE IS A SEAM. A profile of the 234 ms durable commit put 59.4% of the whole commit in
this file: the byte-at-a-time loop runs at about 1.3 MiB/s, and a native CRC-32C does the same
work at roughly 184 times that. D2 says the core stays pure Python and native acceleration goes
behind a port, so the pure implementation below is the FALLBACK and the REFERENCE, and it is
never replaced, never accelerated in place and never deleted -- :func:`crc32c_reference` is the
authority and :func:`install_crc32c` cannot touch it.

WHY THE INSTALLER VALIDATES. A checksum accelerator that disagrees with the reference on one
input does not fail loudly: it reports corruption on a healthy page, which is data the caller is
then told to quarantine. That is the worst outcome in the taxonomy arriving from an optimisation,
so byte-identity is not left to a test somebody might not run -- :func:`install_crc32c` proves it
against :data:`CRC32C_ACCEPTANCE_CORPUS` before the candidate can ever be called on real bytes,
and a candidate that differs anywhere is refused with the input that separated them. The corpus
is hostile where accelerators actually break: every short length, the bytes either side of the
word and block boundaries a slice-by-N loop steps over, and non-zero seeds.
"""

from __future__ import annotations

from collections.abc import Callable

from okto_grafx.domain.errors import GrafxConfigurationError

__all__ = [
    "CRC32C_POLYNOMIAL",
    "CRC32C_POLYNOMIAL_REFLECTED",
    "CRC32C_INITIAL",
    "CRC32C_TABLE_SIZE",
    "CRC32C_ACCEPTANCE_CORPUS",
    "CRC32C_KNOWN_ANSWERS",
    "PURE_IMPLEMENTATION_NAME",
    "crc32c",
    "crc32c_reference",
    "crc32c_table",
    "crc32c_implementation",
    "install_crc32c",
]

CRC32C_POLYNOMIAL: int = 0x1EDC6F41
"""The Castagnoli polynomial in its normal, most significant bit first form."""

CRC32C_POLYNOMIAL_REFLECTED: int = 0x82F63B78
"""The same polynomial reflected, which is the form a least significant bit first loop uses."""

CRC32C_INITIAL: int = 0
"""The seed of a fresh checksum. Chaining a computation passes the previous result instead."""

CRC32C_TABLE_SIZE: int = 256
"""One table entry per possible byte value."""

PURE_IMPLEMENTATION_NAME: str = "pure"
"""The name the reference implementation reports, matching the ``pure`` adapter selector."""

_MASK_32: int = 0xFFFFFFFF


def _build_table() -> tuple[int, ...]:
    """Build the byte-at-a-time reduction table for the reflected polynomial."""
    entries: list[int] = []
    for index in range(CRC32C_TABLE_SIZE):
        value = index
        for _ in range(8):
            value = (value >> 1) ^ (CRC32C_POLYNOMIAL_REFLECTED if value & 1 else 0)
        entries.append(value & _MASK_32)
    return tuple(entries)


_TABLE: tuple[int, ...] = _build_table()


def crc32c_table() -> tuple[int, ...]:
    """Return the precomputed reduction table, so a test can inspect it without rebuilding it."""
    return _TABLE


def _require_seed(crc: object) -> int:
    """Return the seed after checking it is an unsigned 32-bit integer.

    It is checked HERE and nowhere else on purpose. An accelerator never sees an argument this
    has not already accepted, so it does not have to reimplement the refusal -- and there is one
    place in the build that decides what a bad seed means, rather than one per implementation
    (A67).
    """
    if not isinstance(crc, int) or isinstance(crc, bool) or not 0 <= crc <= _MASK_32:
        # The seed is an argument, not a byte off a page. corruption_detected is reserved for
        # damage, because FR-8 and FR-10 turn that code into truncation, quarantine and a
        # forensic ledger entry -- a caller passing the wrong type must not manufacture an
        # integrity incident (A11-revised).
        raise GrafxConfigurationError(
            f"A CRC-32C seed must be an unsigned 32-bit integer; got {crc!r}.",
            field="crc",
            value=repr(crc),
        )
    return crc


def crc32c_reference(data: bytes, crc: int = CRC32C_INITIAL) -> int:
    """Return the CRC-32C of the data by the pure table reduction: the authority, always.

    This is what an accelerator is checked against and what the engine falls back to, so it is
    deliberately the plainest implementation of the algorithm that can be written. It is never
    replaced: :func:`install_crc32c` moves what :func:`crc32c` calls and cannot reach this.
    """
    table = _TABLE
    value = (crc ^ _MASK_32) & _MASK_32
    for byte in data:
        value = table[(value ^ byte) & 0xFF] ^ (value >> 8)
    return (value ^ _MASK_32) & _MASK_32


CRC32C_KNOWN_ANSWERS: tuple[tuple[bytes, int], ...] = (
    (b"", 0x00000000),
    (b"a", 0xC1D04330),
    (b"123456789", 0xE3069283),
    (b"\x00" * 32, 0x8A9136AA),
    (b"\xff" * 32, 0x62A8AB43),
    (bytes(range(32)), 0x46DD794E),
    (bytes(range(31, -1, -1)), 0x113FDB5C),
    (b"The quick brown fox jumps over the lazy dog", 0x22620404),
)
"""Published CRC-32C answers, including the two 32-byte patterns of RFC 3720 appendix B.

They are here rather than only in the suite because they are what stops the whole file agreeing
with itself: an implementation checked only against its own reference would still be wrong, in
the same way, after a changed polynomial or a dropped reflection, and every page ever written
under it would be unreadable by anything else that claims to speak CRC-32C.
"""


def _acceptance_inputs() -> tuple[tuple[bytes, int], ...]:
    """Return the (data, seed) pairs an accelerator has to reproduce exactly.

    Chosen where accelerators actually break rather than where they are easy to test:

    * every length from 0 to 40, which is the tail a slice-by-4, -8 or -16 loop hands to its
      byte-at-a-time remainder -- the single most common defect in a fast CRC;
    * the bytes either side of 255/256, 511/512, 4095/4096 and 8191/8192, which are the word,
      page and block boundaries such a loop steps over;
    * non-zero seeds, because this module's chaining convention passes the previous RESULT (an
      already-finalised value) and a library that expects a raw internal register instead is
      wrong only when the seed is not zero -- which is exactly the case a page checksum of one
      contiguous range never exercises;
    * the published vectors, so the corpus pins the algorithm and not merely the reference.
    """
    pattern = bytes((index * 37 + 11) % 256 for index in range(8200))
    lengths = list(range(0, 41))
    for boundary in (63, 64, 65, 127, 128, 129, 255, 256, 257, 511, 512, 513,
                     1023, 1024, 1025, 4095, 4096, 4097, 8191, 8192):
        lengths.append(boundary)
    seeds = (CRC32C_INITIAL, 1, 0xFFFFFFFF, CRC32C_POLYNOMIAL_REFLECTED, 0x12345678)
    inputs: list[tuple[bytes, int]] = []
    for length in lengths:
        inputs.append((pattern[:length], CRC32C_INITIAL))
    for seed in seeds:
        for length in (0, 1, 7, 8, 9, 32, 512):
            inputs.append((pattern[:length], seed))
    for payload, _answer in CRC32C_KNOWN_ANSWERS:
        inputs.append((payload, CRC32C_INITIAL))
    return tuple(inputs)


CRC32C_ACCEPTANCE_CORPUS: tuple[tuple[bytes, int], ...] = _acceptance_inputs()
"""Every (data, seed) an accelerator must reproduce byte for byte before it may be installed."""


def _validate_candidate(function: Callable[[bytes, int], int], name: str) -> None:
    """Refuse an accelerator that does not reproduce the reference exactly, anywhere.

    The expected value comes from :func:`crc32c_reference` and never from the candidate, and the
    published answers are checked as well, so the two sides of the comparison cannot be the same
    thing (LESSONS L21). A candidate that fails is named together with the input that separated
    it, because "the checksums differ" is not something a caller can act on.
    """
    for payload, answer in CRC32C_KNOWN_ANSWERS:
        produced = function(payload, CRC32C_INITIAL)
        if produced != answer:
            raise GrafxConfigurationError(
                f"The CRC-32C implementation {name!r} answered {produced:#010x} for a published "
                f"vector whose CRC-32C is {answer:#010x}, so it is not computing CRC-32C.",
                field="crc32c",
                value=name,
                length=len(payload),
                expected=answer,
                produced=produced,
            )
    for payload, seed in CRC32C_ACCEPTANCE_CORPUS:
        expected = crc32c_reference(payload, seed)
        produced = function(payload, seed)
        if produced != expected:
            raise GrafxConfigurationError(
                f"The CRC-32C implementation {name!r} answered {produced:#010x} where the "
                f"reference answers {expected:#010x}, on {len(payload)} bytes with seed "
                f"{seed:#010x}; installing it would report corruption on healthy pages.",
                field="crc32c",
                value=name,
                length=len(payload),
                seed=seed,
                expected=expected,
                produced=produced,
            )


_implementation: Callable[[bytes, int], int] = crc32c_reference
_implementation_name: str = PURE_IMPLEMENTATION_NAME


def crc32c_implementation() -> str:
    """Return the name of the implementation :func:`crc32c` is currently calling.

    A database that reports what computed its checksums is a database whose numbers can be
    reproduced. It is the same disclosure the vector adapters make through ``name``.
    """
    return _implementation_name


def install_crc32c(function: Callable[[bytes, int], int], *, name: str) -> str:
    """Install an accelerated CRC-32C, after proving it agrees with the reference, and say
    which implementation it replaced.

    The candidate is called with ``(data, crc)`` where ``crc`` is the previous RESULT of this
    same function -- the chaining convention of :func:`crc32c_reference`, not a raw internal
    register -- and must return an unsigned 32-bit integer. It never sees a seed this module has
    not already validated, so it does not repeat the argument check.

    Refusing is total: a candidate that fails anywhere in the corpus is not installed at all, and
    what was installed before is still installed. Half-accepting an accelerator would be worse
    than rejecting it, because the inputs it got right are exactly the ones a smoke test uses.

    This is the only process-wide state in the domain, and it is defensible for one reason that
    the installer above ENFORCES rather than assumes: every accepted implementation returns the
    same value as every other, so what is installed can change how long a checksum takes and can
    change nothing else. If that ever stopped being true this door would be a defect, which is
    why it is a validating installer and not an assignment.
    """
    if not callable(function):
        raise GrafxConfigurationError(
            f"A CRC-32C implementation must be callable; got {type(function).__name__}.",
            field="crc32c",
            value=type(function).__name__,
        )
    if not isinstance(name, str) or not name:
        raise GrafxConfigurationError(
            f"A CRC-32C implementation must be named so a database can report it; got {name!r}.",
            field="name",
            value=repr(name),
        )
    _validate_candidate(function, name)
    global _implementation, _implementation_name
    replaced = _implementation_name
    _implementation = function
    _implementation_name = name
    return replaced


def crc32c(data: bytes, crc: int = CRC32C_INITIAL) -> int:
    """Return the CRC-32C of the data, optionally continuing a previous computation.

    Passing the result of an earlier call as crc checksums the concatenation of the two byte
    ranges, which is what lets a caller checksum a page without first joining its parts.

    The answer is the reference's answer whatever is installed: an accelerator reached this door
    only by reproducing :func:`crc32c_reference` on every input of the acceptance corpus, so the
    bytes on disk do not depend on which implementation a given process happened to load.
    """
    return _implementation(data, _require_seed(crc))
