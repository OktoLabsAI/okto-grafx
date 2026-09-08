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

WHY THE INSTALLER AND RUNTIME VALIDATE. A checksum accelerator that disagrees with the reference
on one input does not fail loudly: it reports corruption on a healthy page, which is data the
caller is then told to quarantine. :func:`install_crc32c` therefore preflights a candidate and
verifies every actual answer against the immutable reference. The shipped native adapter has a
narrower trust boundary: only its closed list of established packages may use the corpus-checked
fast path; a caller-supplied Python callable remains runtime-verified. In-process code can already
replace modules and write files, so this boundary protects against provider defects without
pretending arbitrary executable code can be proved by a finite corpus.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxError
from okto_grafx.domain.page.layout import CHECKSUM_SIZE, MAX_PAGE_SIZE
from okto_grafx.domain.ports.scoped_value import ScopedValue

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
    if type(crc) is bool or not issubclass(type(crc), int):
        # The seed is an argument, not a byte off a page. corruption_detected is reserved for
        # damage, because FR-8 and FR-10 turn that code into truncation, quarantine and a
        # forensic ledger entry -- a caller passing the wrong type must not manufacture an
        # integrity incident (A11-revised).
        observed: object = _builtin_type_name(crc)
        raise GrafxConfigurationError(
            f"A CRC-32C seed must be an unsigned 32-bit integer; got {observed!r}.",
            field="crc",
            value=observed,
        )
    plain = int.__int__(crc)
    if not 0 <= plain <= _MASK_32:
        observed = (
            plain
            if int.bit_length(plain) <= 256
            else f"int<{int.bit_length(plain)} bits>"
        )
        raise GrafxConfigurationError(
            f"A CRC-32C seed must be an unsigned 32-bit integer; got {observed!r}.",
            field="crc",
            value=observed,
        )
    return plain


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
    * the bytes either side of 255/256, 511/512, 4095/4096, 8191/8192 and the largest legal page
      checksum range, which are the word, page and block boundaries such a loop steps over;
    * non-zero seeds, because this module's chaining convention passes the previous RESULT (an
      already-finalised value) and a library that expects a raw internal register instead is
      wrong only when the seed is not zero -- which is exactly the case a page checksum of one
      contiguous range never exercises;
    * the published vectors, so the corpus pins the algorithm and not merely the reference.
    """
    pattern = bytes((index * 37 + 11) % 256 for index in range(MAX_PAGE_SIZE + 1))
    lengths = list(range(0, 41))
    for boundary in (
        63,
        64,
        65,
        127,
        128,
        129,
        255,
        256,
        257,
        511,
        512,
        513,
        1023,
        1024,
        1025,
        4095,
        4096,
        4097,
        8191,
        8192,
        MAX_PAGE_SIZE - CHECKSUM_SIZE - 1,
        MAX_PAGE_SIZE - CHECKSUM_SIZE,
        MAX_PAGE_SIZE - CHECKSUM_SIZE + 1,
        MAX_PAGE_SIZE,
    ):
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


def _builtin_type_name(value: object) -> str:
    """Name a candidate result without consulting a hostile metaclass descriptor."""
    value_type = type(value)
    declared = type.__dict__["__name__"].__get__(value_type, type(value_type))
    return str.__str__(declared)


def _require_crc32c_answer(
    value: object,
    *,
    field: str,
    name: str,
    length: int,
    seed: int,
) -> int:
    """Return an exact unsigned 32-bit checksum without invoking result hooks."""
    if type(value) is int:
        if 0 <= value <= _MASK_32:
            return value
        observed = _integer_diagnostic(value)
        raise GrafxConfigurationError(
            f"The CRC-32C {field} {name!r} returned {observed}, outside unsigned 32-bit range.",
            field=field,
            value=name,
            produced=observed,
            length=length,
            seed=seed,
        )
    if type(value) is bool or not issubclass(type(value), int):
        observed = _builtin_type_name(value)
        raise GrafxConfigurationError(
            f"The CRC-32C {field} {name!r} returned {observed}, not an unsigned 32-bit int.",
            field=field,
            value=name,
            result_type=observed,
            length=length,
            seed=seed,
        )
    answer = int.__int__(value)
    if not 0 <= answer <= _MASK_32:
        observed = _integer_diagnostic(answer)
        raise GrafxConfigurationError(
            f"The CRC-32C {field} {name!r} returned {observed}, outside unsigned 32-bit range.",
            field=field,
            value=name,
            produced=observed,
            length=length,
            seed=seed,
        )
    return answer


def _integer_diagnostic(value: int) -> int | str:
    """Describe an integer without triggering Python's decimal digit conversion limit."""
    bits = int.bit_length(value)
    return value if bits <= 256 else f"int<{bits} bits>"


def _require_crc32c_agreement(
    produced: int,
    payload: bytes,
    seed: int,
    *,
    name: str,
    field: str,
) -> int:
    """Return an answer only when the immutable reference agrees on these exact bytes."""
    expected = crc32c_reference(payload, seed)
    if produced != expected:
        raise GrafxConfigurationError(
            f"The CRC-32C {field} {name!r} answered {produced:#010x} where the reference "
            f"answers {expected:#010x}; using it would corrupt or falsely reject stored data.",
            field=field,
            value=name,
            length=len(payload),
            seed=seed,
            expected=expected,
            produced=produced,
        )
    return produced


def _candidate_answer(
    function: Callable[[bytes, int], int], payload: bytes, seed: int, name: str
) -> int:
    """Call one candidate and contain every ordinary provider failure as configuration."""
    try:
        observed = function(payload, seed)
    except GrafxError:
        raise
    except Exception as failure:
        cause = _builtin_type_name(failure)
        raise GrafxConfigurationError(
            f"The CRC-32C implementation {name!r} raised {cause} during validation.",
            field="crc32c",
            value=name,
            cause=cause,
            length=len(payload),
            seed=seed,
        ) from failure
    return _require_crc32c_answer(
        observed,
        field="implementation",
        name=name,
        length=len(payload),
        seed=seed,
    )


def _validate_candidate(function: Callable[[bytes, int], int], name: str) -> None:
    """Refuse an accelerator that does not reproduce the reference exactly, anywhere.

    The expected value comes from :func:`crc32c_reference` and never from the candidate, and the
    published answers are checked as well, so the two sides of the comparison cannot be the same
    thing (LESSONS L21). A candidate that fails is named together with the input that separated
    it, because "the checksums differ" is not something a caller can act on.
    """
    for payload, answer in CRC32C_KNOWN_ANSWERS:
        produced = _candidate_answer(function, payload, CRC32C_INITIAL, name)
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
        produced = _candidate_answer(function, payload, seed, name)
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


_implementation_state: tuple[Callable[[bytes, int], int], str] = (
    crc32c_reference,
    PURE_IMPLEMENTATION_NAME,
)
"""Atomically published checksum function and the name that describes that same function."""

_execution_scope: ScopedValue | None = None
"""Outer-composition context transport; the standalone installer remains the fallback."""


class _ChecksumSelection:
    """Mutable only while selecting a validated provider, never retained by a database."""

    def __init__(self) -> None:
        self.state: tuple[Callable[[bytes, int], int], str] = (crc32c_reference, PURE_IMPLEMENTATION_NAME)


def _current_implementation() -> tuple[Callable[[bytes, int], int], str]:
    scope = _execution_scope
    current = None if scope is None else scope.get()
    if type(current) is _ChecksumSelection:
        return current.state
    if current is not None:
        # Only the runtime composition binds these validated immutable pairs.
        return current  # type: ignore[return-value]
    return _implementation_state


def crc32c_implementation() -> str:
    """Return the name of the implementation :func:`crc32c` is currently calling.

    A database that reports what computed its checksums is a database whose numbers can be
    reproduced. It is the same disclosure the vector adapters make through ``name``.
    """
    return _current_implementation()[1]


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

    This standalone default does not override database-owned execution scopes. It remains
    process-wide for low-level callers, and is defensible for one reason that
    the installer above ENFORCES rather than assumes: every accepted implementation returns the
    same value as every other, so what is installed can change how long a checksum takes and can
    change nothing else. If that ever stopped being true this door would be a defect, which is
    why it is a validating installer and not an assignment.
    """
    if not callable(function):
        observed = _builtin_type_name(function)
        raise GrafxConfigurationError(
            f"A CRC-32C implementation must be callable; got {observed}.",
            field="crc32c",
            value=observed,
        )
    if not issubclass(type(name), str):
        observed = _builtin_type_name(name)
        raise GrafxConfigurationError(
            f"A CRC-32C implementation must have a string name; got {observed}.",
            field="name",
            value=observed,
        )
    name = str.__str__(name)
    if not name:
        raise GrafxConfigurationError(
            "A CRC-32C implementation must have a non-empty name.",
            field="name",
            value="",
        )
    if function is not crc32c_reference:
        _validate_candidate(function, name)

    def checked(data: bytes, crc: int) -> int:
        """Contain and oracle-check one answer from an injected implementation."""
        produced = _candidate_answer(function, data, crc, name)
        return _require_crc32c_agreement(
            produced,
            data,
            crc,
            name=name,
            field="implementation",
        )

    return _publish_implementation(
        crc32c_reference if function is crc32c_reference else checked,
        name,
    )


_ClosedProviderIdentity = tuple[str, str, str | None, str | None, object]
_ClosedProviderSlot = tuple[str, str]
_MAX_CLOSED_PROVIDER_PROOFS: int = 2
"""Maximum closed-provider slots the shipped adapter can offer (google-crc32c, crc32c)."""

_CLOSED_PROVIDER_CRC_FIRST: Mapping[_ClosedProviderSlot, bool] = MappingProxyType({
    ("google_crc32c", "extend"): True,
    ("crc32c", "crc32c"): False,
})
"""The complete closed provider list and its fixed argument convention."""

_validated_closed_identities: tuple[
    tuple[_ClosedProviderIdentity, Callable[[bytes, int], int]], ...
] = ()
"""Closed-list provider identities this door has proved, paired with the exact callable proved.

The identity is the strong one the native adapter hands over explicitly (module, attribute,
origin, version and the raw function object); the callable is the one that passed, so a
different object under the same identity is proved again rather than trusted. Only a
successful proof enters, and only when the caller names an identity, which the adapter does
for its closed list alone -- never for an injected provider, whatever its runtime setting.
Published by rebinding an immutable tuple, like the implementation slot below, so no shared
container is ever mutated in place. There is at most one proof per provider slot and at most
``_MAX_CLOSED_PROVIDER_PROOFS`` entries overall; replacement evicts the obsolete identity.
"""


def _require_closed_identity(value: object) -> _ClosedProviderIdentity:
    """Return one exact, inert closed-provider identity or refuse the private fast door.

    Equality of the raw callable is deliberately never consulted. A callable object may define
    hostile or merely surprising ``__eq__``/``__hash__`` methods; the proof is valid only for the
    same object kept alive by this tuple.
    """
    if type(value) is not tuple or len(value) != 5:
        raise GrafxConfigurationError(
            "A CRC-32C memo identity must be a five-item tuple.",
            field="memo_identity",
            value=_builtin_type_name(value),
        )
    module_name, attribute, origin, version, raw_function = value
    if (
        type(module_name) is not str
        or not module_name
        or type(attribute) is not str
        or not attribute
        or (origin is not None and type(origin) is not str)
        or (version is not None and type(version) is not str)
        or not callable(raw_function)
    ):
        raise GrafxConfigurationError(
            "A CRC-32C memo identity has invalid closed-provider fields.",
            field="memo_identity",
            value="invalid_fields",
        )
    if (module_name, attribute) not in _CLOSED_PROVIDER_CRC_FIRST:
        raise GrafxConfigurationError(
            "A CRC-32C memo identity does not name a closed provider slot.",
            field="memo_identity",
            value="unknown_slot",
        )
    return (module_name, attribute, origin, version, raw_function)


def _closed_slot(identity: _ClosedProviderIdentity) -> _ClosedProviderSlot:
    """Return the bounded module/attribute slot named by an identity."""
    return identity[0], identity[1]


def _same_closed_identity(
    left: _ClosedProviderIdentity, right: _ClosedProviderIdentity
) -> bool:
    """Compare inert metadata by value and the raw function strictly by object identity."""
    return left[:4] == right[:4] and left[4] is right[4]


def _proved_closed_callable(
    memo_identity: _ClosedProviderIdentity,
) -> Callable[[bytes, int], int] | None:
    """Return the callable already proved under this identity, or None."""
    for identity, function in _validated_closed_identities:
        if _same_closed_identity(identity, memo_identity):
            return function
    return None


def _remember_closed_proof(
    memo_identity: _ClosedProviderIdentity, function: Callable[[bytes, int], int]
) -> None:
    """Publish one proof, replacing the prior identity for this bounded provider slot."""
    global _validated_closed_identities
    slot = _closed_slot(memo_identity)
    kept = tuple(
        pair for pair in _validated_closed_identities if _closed_slot(pair[0]) != slot
    )
    _validated_closed_identities = (kept + ((memo_identity, function),))[
        -_MAX_CLOSED_PROVIDER_PROOFS:
    ]


def _forget_closed_proof(slot: _ClosedProviderSlot) -> None:
    """Evict the obsolete proof for one provider slot (called under the adapter guard)."""
    global _validated_closed_identities
    _validated_closed_identities = tuple(
        pair for pair in _validated_closed_identities if _closed_slot(pair[0]) != slot
    )


def _forget_closed_proofs() -> None:
    """Drop every memoized proof so the door replays the corpus (tests)."""
    global _validated_closed_identities
    _validated_closed_identities = ()


def _install_validated_crc32c(
    function: Callable[[bytes, int], int],
    *,
    name: str,
    memo_identity: object = None,
) -> str:
    """Install a corpus-validated callable from the native adapter's closed provider list.

    This private door exists so ``checksum='native'`` remains an honest acceleration. It is not
    used for injected providers: :class:`NativeCrc32c` routes those through
    :func:`install_crc32c`, whose runtime oracle makes arbitrary callables fail closed.

    ``memo_identity`` is the explicit permission to memoize (D-29): when the adapter names the
    strong identity of a closed-list provider and this exact callable already passed under it,
    the corpus is not replayed. Without it the door proves every time, as it always did.
    """
    if not callable(function):
        observed = _builtin_type_name(function)
        raise GrafxConfigurationError(
            f"A CRC-32C implementation must be callable; got {observed}.",
            field="crc32c",
            value=observed,
        )
    if not issubclass(type(name), str):
        observed = _builtin_type_name(name)
        raise GrafxConfigurationError(
            f"A CRC-32C implementation must have a string name; got {observed}.",
            field="name",
            value=observed,
        )
    plain_name = str.__str__(name)
    if not plain_name:
        raise GrafxConfigurationError(
            "A CRC-32C implementation must have a non-empty name.",
            field="name",
            value="",
        )
    accepted_identity = (
        _require_closed_identity(memo_identity) if memo_identity is not None else None
    )
    proved = (
        _proved_closed_callable(accepted_identity)
        if accepted_identity is not None
        else None
    )
    if proved is not function:
        _validate_candidate(function, plain_name)
        if accepted_identity is not None:
            _remember_closed_proof(accepted_identity, function)

    checked = (
        _closed_provider_checked(accepted_identity, plain_name)
        if accepted_identity is not None
        else lambda data, crc: _candidate_answer(function, data, crc, plain_name)
    )

    return _publish_implementation(checked, plain_name)


def _closed_provider_checked(
    identity: _ClosedProviderIdentity, name: str
) -> Callable[[bytes, int], int]:
    """Collapse adaptation, provider call and exact-answer validation into one hot frame.

    The raw callable and its argument convention come from the same closed identity that passed
    both adapter and domain corpora. The success path returns only an exact unsigned 32-bit int;
    exceptional and unusual-result paths retain the established typed diagnostics.
    """
    raw_function = identity[4]
    crc_first = _CLOSED_PROVIDER_CRC_FIRST[_closed_slot(identity)]

    def checked(data: bytes, crc: int) -> int:
        """Call the selected CRC backend and validate its bounded result."""
        try:
            observed = (
                raw_function(crc, data)  # type: ignore[operator]
                if crc_first
                else raw_function(data, crc)  # type: ignore[operator]
            )
        except GrafxError:
            raise
        except Exception as failure:
            cause = _builtin_type_name(failure)
            raise GrafxConfigurationError(
                f"The CRC-32C implementation {name!r} raised {cause} during validation.",
                field="crc32c",
                value=name,
                cause=cause,
                length=len(data),
                seed=crc,
            ) from failure
        if type(observed) is int and 0 <= observed <= _MASK_32:
            return observed
        return _require_crc32c_answer(
            observed,
            field="implementation",
            name=name,
            length=len(data),
            seed=crc,
        )

    return checked


def _publish_implementation(function: Callable[[bytes, int], int], name: str) -> str:
    """Atomically publish a validated function/name pair and return the replaced name."""
    global _implementation_state
    current = None if _execution_scope is None else _execution_scope.get()
    if type(current) is _ChecksumSelection:
        replaced = current.state[1]
        current.state = (function, name)
        return replaced
    replaced = _implementation_state[1]
    _implementation_state = (function, name)
    return replaced


def crc32c(data: bytes, crc: int = CRC32C_INITIAL) -> int:
    """Return the CRC-32C of the data, optionally continuing a previous computation.

    Passing the result of an earlier call as crc checksums the concatenation of the two byte
    ranges, which is what lets a caller checksum a page without first joining its parts.

    Injected implementations are compared with the reference on these exact bytes before an
    answer may reach storage. The shipped native adapter's closed provider list is the explicit
    trust boundary: those packages are corpus-validated and then run without the Python oracle,
    which keeps ``checksum='native'`` an honest acceleration.
    """
    implementation = _current_implementation()[0]
    return implementation(data, _require_seed(crc))
