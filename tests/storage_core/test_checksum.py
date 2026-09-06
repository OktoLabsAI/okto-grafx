"""CRC-32C known answer tests (CONTRACT.md section 6.3).

A checksum implementation that is merely self-consistent proves nothing: it would still agree
with itself after a wrong polynomial or a missing reflection, and every page ever written under
it would then be unreadable by anything else. These vectors come from the published CRC-32C
test set, so the implementation is pinned to the algorithm and not to itself.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from okto_grafx.adapters import checksum_native
from okto_grafx.adapters.checksum_native import (
    CRC32C_PROVIDERS,
    NativeCrc32c,
    load_provider,
)
from okto_grafx.adapters.checksum_pure import PureCrc32c
from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
)
from okto_grafx.domain.page import Page, PageType
from okto_grafx.domain.page.checksum import (
    CRC32C_ACCEPTANCE_CORPUS,
    CRC32C_INITIAL,
    CRC32C_KNOWN_ANSWERS,
    CRC32C_POLYNOMIAL,
    CRC32C_POLYNOMIAL_REFLECTED,
    CRC32C_TABLE_SIZE,
    PURE_IMPLEMENTATION_NAME,
    crc32c,
    crc32c_implementation,
    crc32c_reference,
    crc32c_table,
    install_crc32c,
)

KNOWN_VECTORS: tuple[tuple[bytes, int], ...] = (
    (b"", 0x00000000),
    (b"123456789", 0xE3069283),
    (b"\x00" * 32, 0x8A9136AA),
    (b"\xff" * 32, 0x62A8AB43),
    (bytes(range(32)), 0x46DD794E),
    (bytes(range(31, -1, -1)), 0x113FDB5C),
    (b"a", 0xC1D04330),
    (b"The quick brown fox jumps over the lazy dog", 0x22620404),
)
"""Published CRC-32C answers, including the two 32-byte patterns from RFC 3720 appendix B."""


@pytest.mark.parametrize(
    ("payload", "expected"),
    KNOWN_VECTORS,
    ids=[f"{index}" for index in range(len(KNOWN_VECTORS))],
)
def test_the_known_vectors_are_reproduced(payload: bytes, expected: int) -> None:
    assert crc32c(payload) == expected


def test_the_polynomial_constants_are_reflections_of_one_another() -> None:
    reflected = 0
    value = CRC32C_POLYNOMIAL
    for _ in range(32):
        reflected = (reflected << 1) | (value & 1)
        value >>= 1
    assert reflected == CRC32C_POLYNOMIAL_REFLECTED


def test_the_table_is_built_once_and_has_one_entry_per_byte() -> None:
    table = crc32c_table()
    assert len(table) == CRC32C_TABLE_SIZE
    assert table is crc32c_table()
    assert all(0 <= entry <= 0xFFFFFFFF for entry in table)
    assert table[0] == 0


def test_the_table_is_the_bit_by_bit_reduction_of_the_reflected_polynomial() -> None:
    # The table is an optimisation of the one-bit-at-a-time reduction; recomputing it the slow
    # way here is what proves the optimisation did not change the function.
    expected = []
    for index in range(CRC32C_TABLE_SIZE):
        value = index
        for _ in range(8):
            value = (value >> 1) ^ (CRC32C_POLYNOMIAL_REFLECTED if value & 1 else 0)
        expected.append(value)
    assert list(crc32c_table()) == expected


def test_chaining_a_computation_checksums_the_concatenation() -> None:
    whole = crc32c(b"123456789")
    chained = crc32c(b"9", crc32c(b"12345678"))
    assert chained == whole


def test_a_single_bit_flip_changes_the_checksum() -> None:
    payload = bytearray(b"okto grafx page payload")
    original = crc32c(bytes(payload))
    for position in range(len(payload)):
        for bit in (0, 3, 7):
            flipped = bytearray(payload)
            flipped[position] ^= 1 << bit
            assert crc32c(bytes(flipped)) != original


def test_the_checksum_is_always_an_unsigned_32_bit_integer() -> None:
    for length in range(0, 64):
        value = crc32c(bytes(range(length % 256)) * 3)
        assert 0 <= value <= 0xFFFFFFFF


def test_a_seed_outside_32_bits_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError):
        crc32c(b"x", 1 << 32)
    with pytest.raises(GrafxConfigurationError):
        crc32c(b"x", -1)


def test_a_bad_seed_is_not_reported_as_damage() -> None:
    """A wrong argument must not raise the code that means the bytes on disk are damaged.

    corruption_detected drives truncation and quarantine downstream, so a caller passing a
    float where an int belongs must not be able to manufacture an integrity incident. Only
    the reclassification produces this code; the type refusal on its own does not.
    """
    for bad in (1 << 32, -1, "0", 1.0, True):
        with pytest.raises(GrafxError) as caught:
            crc32c(b"x", bad)  # type: ignore[arg-type]
        assert caught.value.code == "configuration_error", bad
        assert not isinstance(caught.value, GrafxCorruptionDetected), bad


def test_the_codec_exposes_the_same_checksum() -> None:
    codec = PageCodecV1(512)
    assert codec.checksum(b"123456789") == 0xE3069283
    assert codec.checksum(b"") == 0
    assert codec.format_version == 1


# --- D2: native acceleration behind the port, and the equality that makes it safe ---------------


@pytest.fixture
def restore_the_reference() -> Iterator[None]:
    """Put the pure implementation back, whatever a test installed.

    Restoring through the PUBLIC door rather than by assigning the module's private slot is
    deliberate: it proves the installer round-trips, and it means no test depends on a name the
    module does not offer.

    It restores what was ACTUALLY there, not the reference. The two were the same string until a
    machine had a native provider, and then they were not: ``connect()`` installs the accelerator
    process-wide by default, so any earlier test that opened a database leaves ``native`` in the
    slot, and a fixture that always put back ``pure`` silently changed what the next test measured
    (LESSONS L28). The slot is deliberately process-global -- every component of one database must
    compute the same checksum -- which makes it shared state between tests, and shared state is
    restored to its previous value or not restored at all.
    """
    before = crc32c_implementation()
    install_crc32c(crc32c_reference, name=PURE_IMPLEMENTATION_NAME)
    yield
    if before == PURE_IMPLEMENTATION_NAME:
        install_crc32c(crc32c_reference, name=PURE_IMPLEMENTATION_NAME)
    else:
        from okto_grafx.adapters.checksum_native import NativeCrc32c

        NativeCrc32c().install()
    assert crc32c_implementation() == before


def mirror(data: bytes, crc: int) -> int:
    """An accelerator that is byte-identical because it is the reference wearing a hat."""
    return crc32c_reference(data, crc)


def test_the_shipped_door_and_the_reference_agree_on_the_whole_acceptance_corpus() -> (
    None
):
    """The digest-equality test. It is what makes the seam safe to have at all.

    An accelerated checksum that disagrees with the reference on one input does not fail loudly:
    it reports corruption on a healthy page, and the caller is then told to quarantine data that
    was never damaged. So the two are compared on every input of the corpus -- and on the
    published answers as well, so the comparison cannot be satisfied by two implementations that
    are wrong in the same way.
    """
    assert CRC32C_ACCEPTANCE_CORPUS, "an empty corpus would accept anything"
    for payload, seed in CRC32C_ACCEPTANCE_CORPUS:
        assert crc32c(payload, seed) == crc32c_reference(payload, seed), (
            len(payload),
            seed,
        )
    for payload, answer in CRC32C_KNOWN_ANSWERS:
        assert crc32c_reference(payload) == answer
        assert crc32c(payload) == answer


def test_the_acceptance_corpus_covers_the_inputs_a_fast_loop_gets_wrong() -> None:
    """The corpus is the gate, so narrowing it is how the gate is switched off (A56).

    Three properties are pinned by name because each corresponds to a defect class an
    accelerator actually has: the short tail a slice-by-N loop hands to its remainder, the bytes
    either side of a block boundary, and a non-zero seed, which a page checksum of one
    contiguous range never exercises and a chained one always does.
    """
    lengths = {len(payload) for payload, _seed in CRC32C_ACCEPTANCE_CORPUS}
    assert set(range(0, 41)) <= lengths, "the byte-at-a-time tail is not covered"
    for boundary in (
        255,
        256,
        257,
        511,
        512,
        513,
        4095,
        4096,
        4097,
        8191,
        8192,
        32763,
        32764,
        32765,
        32768,
    ):
        assert boundary in lengths, boundary
    seeds = {seed for _payload, seed in CRC32C_ACCEPTANCE_CORPUS}
    assert seeds - {CRC32C_INITIAL}, "every seed is zero, so chaining is untested"
    assert 0xFFFFFFFF in seeds


def test_an_accelerator_that_reproduces_the_reference_is_installed_and_reported(
    restore_the_reference: None,
) -> None:
    assert crc32c_implementation() == PURE_IMPLEMENTATION_NAME
    replaced = install_crc32c(mirror, name="mirror")
    assert replaced == PURE_IMPLEMENTATION_NAME
    assert crc32c_implementation() == "mirror"


def test_an_installed_accelerator_changes_no_digest_the_engine_produces(
    restore_the_reference: None,
) -> None:
    """The one property that makes a process-wide slot defensible, asserted rather than assumed.

    What is installed may change how long a checksum takes and nothing else. The comparison runs
    through the doors the engine actually uses -- a page image and the codec -- not only through
    the function, because those are what a wrong accelerator would corrupt.
    """
    page = Page(int(PageType.HEAP), page_size=512, page_index=3)
    page.insert_slot(b"payload that has to survive the swap")
    codec = PageCodecV1(512)
    before_image = codec.encode_page(page)
    before_digests = [
        crc32c(payload, seed) for payload, seed in CRC32C_ACCEPTANCE_CORPUS
    ]

    install_crc32c(mirror, name="mirror")

    assert [
        crc32c(payload, seed) for payload, seed in CRC32C_ACCEPTANCE_CORPUS
    ] == before_digests
    assert codec.encode_page(page) == before_image
    assert codec.decode_page(before_image, verify=True) == page


def test_an_accelerator_with_the_wrong_polynomial_is_refused(
    restore_the_reference: None,
) -> None:
    """zlib's CRC-32 has the same shape and a different polynomial, which is the trap.

    It was used as the COST proxy in the profile that motivated this seam, and it is exactly the
    thing that must never be installed by someone who read that profile and reached for the
    nearest fast function.
    """
    with pytest.raises(GrafxConfigurationError) as raised:
        install_crc32c(lambda data, crc: zlib.crc32(data, crc), name="zlib")
    assert raised.value.details["field"] == "crc32c"
    assert crc32c_implementation() == PURE_IMPLEMENTATION_NAME


def test_an_accelerator_that_is_wrong_only_on_a_tail_length_is_refused(
    restore_the_reference: None,
) -> None:
    """The commonest defect in a fast CRC: the whole-word loop is right, the remainder is not."""

    def wrong_tail(data: bytes, crc: int) -> int:
        value = crc32c_reference(data, crc)
        return value ^ 1 if len(data) % 8 == 3 else value

    with pytest.raises(GrafxConfigurationError) as raised:
        install_crc32c(wrong_tail, name="wrong-tail")
    assert raised.value.details["length"] % 8 == 3
    assert crc32c_implementation() == PURE_IMPLEMENTATION_NAME


def test_an_accelerator_that_is_wrong_only_on_a_non_zero_seed_is_refused(
    restore_the_reference: None,
) -> None:
    """A library whose seed is a raw internal register rather than a previous RESULT.

    A page checksum of one contiguous range never notices; a chained one is silently wrong, and
    the ledger, the catalog and the commit state all chain.
    """

    def wrong_seed(data: bytes, crc: int) -> int:
        return crc32c_reference(data, 0 if crc else crc)

    with pytest.raises(GrafxConfigurationError) as raised:
        install_crc32c(wrong_seed, name="wrong-seed")
    assert raised.value.details["seed"] != 0
    assert crc32c_implementation() == PURE_IMPLEMENTATION_NAME


def test_a_refused_accelerator_leaves_the_one_that_was_installed(
    restore_the_reference: None,
) -> None:
    """Refusing is total. Half-accepting would keep exactly the inputs a smoke test uses."""
    install_crc32c(mirror, name="mirror")
    with pytest.raises(GrafxConfigurationError):
        install_crc32c(lambda data, crc: 0, name="always-zero")
    assert crc32c_implementation() == "mirror"
    assert crc32c(b"123456789") == 0xE3069283


def test_installing_something_that_is_not_a_named_callable_is_refused(
    restore_the_reference: None,
) -> None:
    with pytest.raises(GrafxConfigurationError) as not_callable:
        install_crc32c(object(), name="thing")  # type: ignore[arg-type]
    assert not_callable.value.details["field"] == "crc32c"
    with pytest.raises(GrafxConfigurationError) as unnamed:
        install_crc32c(mirror, name="")
    assert unnamed.value.details["field"] == "name"
    assert crc32c_implementation() == PURE_IMPLEMENTATION_NAME


class _AlwaysEqualChecksum:
    """A non-integer result that used equality to impersonate every expected digest."""

    def __eq__(self, other: object) -> bool:
        return True

    def __format__(self, specification: str) -> str:
        return "00000000"


def test_a_non_integer_result_cannot_pass_validation_by_forging_equality(
    restore_the_reference: None,
) -> None:
    candidate = lambda data, crc: _AlwaysEqualChecksum()  # noqa: E731
    with pytest.raises(GrafxConfigurationError) as installed:
        install_crc32c(candidate, name="always-equal")  # type: ignore[arg-type]
    assert installed.value.details["field"] == "implementation"
    with pytest.raises(GrafxConfigurationError) as adapted:
        NativeCrc32c(candidate, provider_name="always-equal")  # type: ignore[arg-type]
    assert adapted.value.details["field"] == "provider"
    assert crc32c_implementation() == PURE_IMPLEMENTATION_NAME


class _HostileChecksumInt(int):
    def __eq__(self, other: object) -> bool:
        raise RuntimeError("caller equality ran")

    def __format__(self, specification: str) -> str:
        raise RuntimeError("caller formatting ran")


def test_an_integer_subclass_is_copied_before_comparison_and_runtime_use(
    restore_the_reference: None,
) -> None:
    def candidate(data: bytes, crc: int) -> int:
        return _HostileChecksumInt(crc32c_reference(data, crc))

    install_crc32c(candidate, name="integer-subclass")
    answer = crc32c(b"123456789")
    assert type(answer) is int
    assert answer == 0xE3069283


def test_an_ordinary_candidate_failure_is_a_typed_refusal(
    restore_the_reference: None,
) -> None:
    marker = RuntimeError("provider failed")

    def candidate(data: bytes, crc: int) -> int:
        raise marker

    with pytest.raises(GrafxConfigurationError) as raised:
        install_crc32c(candidate, name="raising")
    assert raised.value.details["cause"] == "RuntimeError"
    assert raised.value.__cause__ is marker
    assert crc32c_implementation() == PURE_IMPLEMENTATION_NAME


def test_a_huge_exact_integer_answer_is_a_typed_bounded_refusal(
    restore_the_reference: None,
) -> None:
    huge = 10**10000
    with pytest.raises(GrafxConfigurationError) as raised:
        install_crc32c(lambda data, crc: huge, name="huge")
    assert raised.value.details["field"] == "implementation"
    assert raised.value.details["produced"].startswith("int<")
    assert crc32c_implementation() == PURE_IMPLEMENTATION_NAME


def test_an_injected_provider_is_verified_again_on_each_real_input(
    restore_the_reference: None,
) -> None:
    armed = False

    def selective(data: bytes, crc: int) -> int:
        answer = crc32c_reference(data, crc)
        return answer ^ 1 if armed else answer

    adapter = NativeCrc32c(selective, provider_name="selective")
    adapter.install()
    armed = True

    with pytest.raises(GrafxConfigurationError) as installed:
        crc32c(bytes(32764))
    assert installed.value.details["field"] == "implementation"
    with pytest.raises(GrafxConfigurationError) as direct:
        adapter.checksum(bytes(32764))
    assert direct.value.details["field"] == "provider"


def test_a_vendored_provider_can_explicitly_accept_the_corpus_trust_boundary(
    restore_the_reference: None,
) -> None:
    adapter = NativeCrc32c(
        mirror,
        provider_name="vendored",
        verify_runtime=False,
    )
    adapter.install()
    assert crc32c(b"runtime") == crc32c_reference(b"runtime")

    with pytest.raises(GrafxConfigurationError) as raised:
        NativeCrc32c(
            mirror,
            provider_name="vendored",
            verify_runtime=1,  # type: ignore[arg-type]
        )
    assert raised.value.details["field"] == "verify_runtime"


@pytest.mark.parametrize("late_answer", ["float", "raise"])
def test_a_closed_list_provider_still_has_runtime_shape_and_error_containment(
    late_answer: str,
    monkeypatch: pytest.MonkeyPatch,
    restore_the_reference: None,
) -> None:
    armed = False
    marker = RuntimeError("native provider failed after validation")

    def provider(data: bytes, crc: int) -> object:
        if armed:
            if late_answer == "raise":
                raise marker
            return float(crc32c_reference(data, crc))
        return crc32c_reference(data, crc)

    with monkeypatch.context() as patch:
        patch.setattr(
            checksum_native, "load_provider", lambda: ("google_crc32c", provider)
        )
        adapter = NativeCrc32c()
        adapter.install()
    armed = True

    with pytest.raises(GrafxConfigurationError) as installed:
        crc32c(b"runtime")
    with pytest.raises(GrafxConfigurationError) as direct:
        adapter.checksum(b"runtime")
    if late_answer == "float":
        assert installed.value.details["result_type"] == "float"
        assert direct.value.details["result_type"] == "float"
    else:
        assert installed.value.__cause__ is marker
        assert direct.value.__cause__ is marker


def test_explicit_runtime_verification_always_wins_for_a_closed_list_provider(
    monkeypatch: pytest.MonkeyPatch,
    restore_the_reference: None,
) -> None:
    armed = False

    def provider(data: bytes, crc: int) -> int:
        answer = crc32c_reference(data, crc)
        return answer ^ 1 if armed else answer

    with monkeypatch.context() as patch:
        patch.setattr(
            checksum_native, "load_provider", lambda: ("google_crc32c", provider)
        )
        adapter = NativeCrc32c(verify_runtime=True)
        adapter.install()
    armed = True

    with pytest.raises(GrafxConfigurationError) as installed:
        crc32c(b"runtime")
    with pytest.raises(GrafxConfigurationError) as direct:
        adapter.checksum(b"runtime")
    assert installed.value.details["field"] == "implementation"
    assert direct.value.details["field"] == "provider"


@pytest.mark.parametrize("module_name", ["google_crc32c", "crc32c"])
def test_native_adaptation_does_not_coerce_a_provider_result(
    module_name: str,
) -> None:
    def float_answer(first: object, second: object) -> float:
        data, seed = (
            (second, first) if module_name == "google_crc32c" else (first, second)
        )
        return float(crc32c_reference(data, seed))  # type: ignore[arg-type]

    adapted = checksum_native._adapt(module_name, "provider", float_answer)
    with pytest.raises(GrafxConfigurationError) as raised:
        NativeCrc32c(adapted, provider_name=module_name)
    assert raised.value.details["field"] == "provider"
    assert raised.value.details["result_type"] == "float"


def test_the_reference_is_not_reachable_through_the_installer(
    restore_the_reference: None,
) -> None:
    """D2 says the pure implementation stays as the fallback and the reference. It is untouchable.

    Installing moves what crc32c calls; crc32c_reference is the thing every candidate is
    measured against, so an accelerator that could replace it would be marking its own homework
    (LESSONS L21).
    """
    install_crc32c(mirror, name="mirror2")
    assert crc32c_reference(b"123456789") == 0xE3069283
    assert crc32c_reference(b"") == 0
    for payload, answer in CRC32C_KNOWN_ANSWERS:
        assert crc32c_reference(payload) == answer


def test_a_bad_seed_is_refused_before_any_implementation_sees_it(
    restore_the_reference: None,
) -> None:
    """One place decides what a bad seed means, so an accelerator never repeats the refusal."""
    seen: list[int] = []

    def recording(data: bytes, crc: int) -> int:
        seen.append(crc)
        return crc32c_reference(data, crc)

    install_crc32c(recording, name="recording")
    seen.clear()
    with pytest.raises(GrafxConfigurationError):
        crc32c(b"x", 1 << 32)
    with pytest.raises(GrafxConfigurationError):
        crc32c(b"x", -1)
    with pytest.raises(GrafxConfigurationError):
        crc32c(b"x", True)  # type: ignore[arg-type]
    assert seen == [], "a rejected seed reached the implementation"
    assert crc32c(b"x", 7) == crc32c_reference(b"x", 7)
    assert seen == [7]


def test_a_hostile_integer_seed_is_copied_without_running_its_hooks(
    restore_the_reference: None,
) -> None:
    class HostileSeed(int):
        def __le__(self, other: object) -> bool:
            raise RuntimeError("caller comparison ran")

        def __repr__(self) -> str:
            raise RuntimeError("caller repr ran")

    answer = crc32c(b"x", HostileSeed(7))
    assert type(answer) is int
    assert answer == crc32c_reference(b"x", 7)


def test_checksum_function_and_name_share_one_atomic_publication() -> None:
    from okto_grafx.domain.page import checksum as module

    function, name = module._implementation_state
    assert callable(function)
    assert name == crc32c_implementation()
    assert not hasattr(module, "_implementation")
    assert not hasattr(module, "_implementation_name")


# --- the adapters: pure is the reference, native is proved against it ---------------------------


def has_native_provider() -> bool:
    """Return True when this interpreter really has a native CRC-32C installed."""
    try:
        load_provider()
    except ImportError:
        return False
    return True


def test_the_pure_adapter_computes_the_reference_and_nothing_else() -> None:
    adapter = PureCrc32c()
    assert adapter.name == PURE_IMPLEMENTATION_NAME
    for payload, answer in CRC32C_KNOWN_ANSWERS:
        assert adapter.checksum(payload) == answer
    for payload, seed in CRC32C_ACCEPTANCE_CORPUS:
        assert adapter.checksum(payload, seed) == crc32c_reference(payload, seed)


def test_a_provider_that_disagrees_with_the_reference_can_never_be_installed(
    restore_the_reference: None,
) -> None:
    """The guarantee, proved with nothing installed: byte-identical or refused, never neither.

    This is what makes the native path safe before anyone has a native library. Each candidate
    below is a real defect class rather than a random perturbation, and every one of them must
    be stopped both where the adapter is CONSTRUCTED and at the installer -- so each is tried
    through both doors.
    """
    wrong: tuple[tuple[str, object], ...] = (
        (
            "wrong polynomial (zlib CRC-32, not CRC-32C)",
            lambda data, crc=0: zlib.crc32(data, crc),
        ),
        (
            "arguments the wrong way round",
            lambda data, crc=0: crc32c_reference(bytes([crc & 0xFF]), len(data)),
        ),
        ("seed ignored", lambda data, crc=0: crc32c_reference(data, 0)),
        (
            "truncated to 16 bits",
            lambda data, crc=0: crc32c_reference(data, crc) & 0xFFFF,
        ),
        (
            "off by one on a tail length",
            lambda data, crc=0: (
                crc32c_reference(data, crc) ^ (1 if len(data) % 8 == 5 else 0)
            ),
        ),
    )
    for label, candidate in wrong:
        with pytest.raises(GrafxConfigurationError):
            NativeCrc32c(candidate, provider_name=label)  # type: ignore[arg-type]
        with pytest.raises(GrafxConfigurationError):
            install_crc32c(candidate, name=label)  # type: ignore[arg-type]
        assert crc32c_implementation() == PURE_IMPLEMENTATION_NAME, label
        assert crc32c(b"123456789") == 0xE3069283, label


def test_an_adapter_over_a_correct_provider_installs_and_changes_no_digest(
    restore_the_reference: None,
) -> None:
    """Installing an accelerator may change how long a checksum takes and nothing else.

    Compared through the doors the engine uses -- a page image and the codec -- because those
    are what a wrong accelerator would corrupt, and a function-level comparison would not see it.
    """
    codec = PageCodecV1(512)
    page = Page(int(PageType.HEAP), page_size=512, page_index=5)
    page.insert_slot(b"bytes that must not move when the implementation does")
    before_image = codec.encode_page(page)
    before = [crc32c(payload, seed) for payload, seed in CRC32C_ACCEPTANCE_CORPUS]

    adapter = NativeCrc32c(
        lambda data, crc=0: crc32c_reference(data, crc), provider_name="stand-in"
    )
    assert adapter.name == "native"
    assert adapter.provider == "stand-in"
    assert adapter.install() == PURE_IMPLEMENTATION_NAME
    assert crc32c_implementation() == "native"

    assert [
        crc32c(payload, seed) for payload, seed in CRC32C_ACCEPTANCE_CORPUS
    ] == before
    assert codec.encode_page(page) == before_image
    assert codec.decode_page(before_image, verify=True) == page
    for payload, answer in CRC32C_KNOWN_ANSWERS:
        assert crc32c(payload) == answer

    assert PureCrc32c().install() == "native"
    assert crc32c_implementation() == PURE_IMPLEMENTATION_NAME


def test_a_provider_that_is_not_callable_is_refused_as_a_caller_mistake() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        NativeCrc32c(object(), provider_name="thing")  # type: ignore[arg-type]
    assert raised.value.details["field"] == "provider"


def test_the_provider_list_is_closed_and_declares_what_it_takes_from_each() -> None:
    """A54.1: the providers are DECLARED, not discovered.

    "Whatever module happens to import" is an open-world question whose answer the author
    controls by choosing a name, and the argument order differs between the two providers -- so
    the list carries the attribute each one exposes, and the adaptation is written per provider
    rather than guessed.
    """
    assert CRC32C_PROVIDERS
    assert [name for name, _attribute in CRC32C_PROVIDERS] == [
        "google_crc32c",
        "crc32c",
    ]
    for name, attribute in CRC32C_PROVIDERS:
        assert isinstance(name, str) and name
        assert isinstance(attribute, str) and attribute


def test_the_missing_provider_failure_names_the_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine runs pure when the accelerator is absent, and says how to get one.

    The absence is CONSTRUCTED rather than waited for, so this runs on every machine and asserts
    the same thing on all of them. Keying it on what happens to be installed would make it a
    skip on some hosts, and a skip that no registered marker can attribute is a hole in G4 --
    which is exactly what happened when this was first written.
    """
    monkeypatch.setattr(
        checksum_native, "CRC32C_PROVIDERS", (("okto_grafx_no_such_crc", "x"),)
    )
    with pytest.raises(ImportError) as raised:
        load_provider()
    assert "[accel]" in str(raised.value)
    assert "okto_grafx_no_such_crc" in str(raised.value)


def test_the_native_adapter_produces_digests_identical_to_the_pure_reference(
    restore_the_reference: None,
) -> None:
    """The parity test against a REAL native library, when this interpreter has one.

    It is the measurement; the guarantee does not depend on it, because
    test_a_provider_that_disagrees_with_the_reference_can_never_be_installed proves that a
    provider which fails this comparison cannot be installed at all. Both are needed: one says
    the real library agrees, the other says nothing that disagrees can ever be reached.
    """
    if has_native_provider():
        provider_name, provider = load_provider()
        adapter = NativeCrc32c()
        assert adapter.provider == provider_name
    else:
        # No native library on this interpreter. The adapter is still exercised end to end
        # through its declared provider seam, so the test measures something on every host
        # rather than vanishing on most of them; what it cannot do without a library is speak
        # for that library, and it says which case it ran.
        provider_name = "stand-in"
        provider = lambda data, crc=0: crc32c_reference(data, crc)  # noqa: E731
        adapter = NativeCrc32c(provider, provider_name=provider_name)
    assert adapter.provider == provider_name
    for payload, answer in CRC32C_KNOWN_ANSWERS:
        assert adapter.checksum(payload) == answer, provider_name
    for payload, seed in CRC32C_ACCEPTANCE_CORPUS:
        assert adapter.checksum(payload, seed) == crc32c_reference(payload, seed), (
            provider_name,
            len(payload),
            seed,
        )
    assert provider(b"123456789", 0) == 0xE3069283
    adapter.install()
    assert crc32c_implementation() == "native"
    assert crc32c(b"123456789") == 0xE3069283


# --- D-29: the corpus proof of a closed-list provider is memoized per process ------------------


@pytest.fixture
def forget_the_memo() -> Iterator[None]:
    """Start and finish with no memoized proof, whatever earlier tests left in the process."""
    checksum_native._forget_validated_closed_providers()
    yield
    checksum_native._forget_validated_closed_providers()


def _google_order(function):
    """Wrap a ``(data, crc)`` function into google_crc32c's ``(crc, data)`` convention."""

    def extend(crc: int, data: bytes) -> int:
        return function(data, crc)

    return extend


def _closed(
    function, *, origin: str = "fake/google_crc32c/_crc32c.pyd", version="1.5.0"
):
    """Build a closed-list provider exactly the way load_provider does, over a fake module."""
    module = SimpleNamespace(
        __spec__=SimpleNamespace(origin=origin), __version__=version, extend=function
    )
    return checksum_native._ClosedProvider("google_crc32c", "extend", module, function)


class _ReferenceCounter:
    """Count the pure-reference calls the adapter makes while proving a provider."""

    def __init__(self, patch: pytest.MonkeyPatch) -> None:
        self.calls = 0

        def counted(data: bytes, crc: int = 0) -> int:
            self.calls += 1
            return crc32c_reference(data, crc)

        patch.setattr(checksum_native, "crc32c_reference", counted)


def test_a_proved_closed_list_provider_is_not_replayed_on_the_next_construction(
    monkeypatch: pytest.MonkeyPatch, forget_the_memo: None
) -> None:
    provider = _closed(_google_order(mirror))
    with monkeypatch.context() as patch:
        counter = _ReferenceCounter(patch)
        patch.setattr(
            checksum_native, "load_provider", lambda: ("google_crc32c", provider)
        )
        first = NativeCrc32c()
        proved = counter.calls
        second = NativeCrc32c()
        replayed = counter.calls - proved

    assert proved >= len(CRC32C_ACCEPTANCE_CORPUS), (
        "the first construction proves the corpus"
    )
    assert replayed == 0, (
        "the second construction over the same identity replays nothing"
    )
    assert checksum_native.validated_closed_providers() == ("google_crc32c",)
    assert first.checksum(b"123456789") == 0xE3069283
    assert second.checksum(b"123456789", 7) == crc32c_reference(b"123456789", 7)
    assert first._verify_runtime is False and second._verify_runtime is False


@pytest.mark.parametrize("difference", ["function", "origin", "version"])
def test_the_memo_is_keyed_on_the_strong_identity_not_on_the_module_name(
    difference: str, monkeypatch: pytest.MonkeyPatch, forget_the_memo: None
) -> None:
    proved_first = _closed(_google_order(mirror))
    if difference == "function":
        other = _closed(_google_order(mirror))  # a new function object, same module
    elif difference == "origin":
        other = _closed(proved_first._function, origin="elsewhere/_crc32c.pyd")
    else:
        other = _closed(proved_first._function, version="1.6.0")
    with monkeypatch.context() as patch:
        counter = _ReferenceCounter(patch)
        patch.setattr(
            checksum_native, "load_provider", lambda: ("google_crc32c", proved_first)
        )
        NativeCrc32c()
        proved = counter.calls
        patch.setattr(
            checksum_native, "load_provider", lambda: ("google_crc32c", other)
        )
        NativeCrc32c()
        replayed = counter.calls - proved

    assert replayed >= len(CRC32C_ACCEPTANCE_CORPUS), difference
    assert len(checksum_native._validated_closed_providers) == 1, (
        "a replacement occupies the same bounded provider slot"
    )


def test_a_refused_provider_never_enters_the_memo(
    monkeypatch: pytest.MonkeyPatch, forget_the_memo: None
) -> None:
    def wrong(data: bytes, crc: int) -> int:
        return crc32c_reference(data, crc) ^ 1

    broken = _closed(_google_order(wrong))
    with monkeypatch.context() as patch:
        counter = _ReferenceCounter(patch)
        patch.setattr(
            checksum_native, "load_provider", lambda: ("google_crc32c", broken)
        )
        with pytest.raises(GrafxConfigurationError) as first:
            NativeCrc32c()
        assert checksum_native._validated_closed_providers == {}
        before = counter.calls
        with pytest.raises(GrafxConfigurationError) as second:
            NativeCrc32c()

    assert first.value.details["field"] == "provider"
    assert second.value.details == first.value.details, (
        "the refusal is reproduced, not cached"
    )
    assert checksum_native.validated_closed_providers() == ()
    # The published vectors refuse before the corpus is reached, so the reference may not run
    # at all; what matters is that the second attempt was judged again rather than admitted.
    assert counter.calls >= before


def test_an_injected_provider_is_proved_on_every_construction_and_never_memoized(
    monkeypatch: pytest.MonkeyPatch, forget_the_memo: None
) -> None:
    with monkeypatch.context() as patch:
        counter = _ReferenceCounter(patch)
        first = NativeCrc32c(mirror, provider_name="mirror")
        proved = counter.calls
        second = NativeCrc32c(mirror, provider_name="mirror")
        replayed = counter.calls - proved

    assert proved >= len(CRC32C_ACCEPTANCE_CORPUS)
    assert replayed >= len(CRC32C_ACCEPTANCE_CORPUS)
    assert checksum_native.validated_closed_providers() == ()
    assert first._verify_runtime is True and second._verify_runtime is True


def test_explicit_runtime_verification_never_reads_the_memo(
    monkeypatch: pytest.MonkeyPatch, forget_the_memo: None
) -> None:
    armed = False

    def sometimes_wrong(data: bytes, crc: int) -> int:
        answer = crc32c_reference(data, crc)
        return answer ^ 1 if armed else answer

    provider = _closed(_google_order(sometimes_wrong))
    with monkeypatch.context() as patch:
        counter = _ReferenceCounter(patch)
        patch.setattr(
            checksum_native, "load_provider", lambda: ("google_crc32c", provider)
        )
        NativeCrc32c()  # memoizes this identity on the default path
        proved = counter.calls
        strict = NativeCrc32c(verify_runtime=True)
        replayed = counter.calls - proved
    armed = True

    assert replayed >= len(CRC32C_ACCEPTANCE_CORPUS), (
        "explicit verification replays the corpus"
    )
    with pytest.raises(GrafxConfigurationError) as refused:
        strict.checksum(b"runtime")  # and keeps the per-call oracle
    assert refused.value.details["field"] == "provider"
    assert strict._verify_runtime is True
    assert checksum_native.validated_closed_providers() == ("google_crc32c",)


def test_the_installer_door_still_replays_the_corpus_after_a_memo_hit(
    monkeypatch: pytest.MonkeyPatch, forget_the_memo: None, restore_the_reference: None
) -> None:
    from okto_grafx.domain.page import checksum as checksum_module

    provider = _closed(_google_order(mirror))
    validations: list[str] = []
    original = checksum_module._validate_candidate

    def counted(function, name: str) -> None:
        validations.append(name)
        original(function, name)

    with monkeypatch.context() as patch:
        patch.setattr(
            checksum_native, "load_provider", lambda: ("google_crc32c", provider)
        )
        NativeCrc32c()
        memoized = NativeCrc32c()
        patch.setattr(checksum_module, "_validate_candidate", counted)
        assert memoized.install() == PURE_IMPLEMENTATION_NAME

    native = checksum_native.NATIVE_ADAPTER_NAME
    assert validations == [native], "the domain door is not part of the memo"
    assert crc32c_implementation() == native


def test_the_real_provider_carries_a_strong_identity() -> None:
    google = pytest.importorskip("google_crc32c")
    name, provider = load_provider()

    assert name == "google_crc32c"
    assert isinstance(provider, checksum_native._ClosedProvider)
    module_name, attribute, origin, _version, function = provider.identity
    assert (module_name, attribute) == ("google_crc32c", "extend")
    assert function is google.extend
    assert origin == google.__spec__.origin
    assert provider(b"123456789", 0) == 0xE3069283


def _fake_google_module(function, *, origin: str = "fake/google_crc32c/__init__.py"):
    """A stand-in for the google_crc32c package, importable through checksum_native."""
    return SimpleNamespace(
        __spec__=SimpleNamespace(origin=origin), __version__="1.5.0", extend=function
    )


class _ProviderCounter:
    """Count the raw provider calls behind a fake closed-list module."""

    def __init__(self) -> None:
        self.calls = 0

    def extend(self, crc: int, data: bytes) -> int:
        self.calls += 1
        return crc32c_reference(data, crc)


def _install_fake_google(patch: pytest.MonkeyPatch, module) -> None:
    def import_fake(name: str):
        if name == "google_crc32c":
            return module
        raise ImportError(name)

    patch.setattr(checksum_native, "import_module", import_fake)


def test_a_second_connect_cycle_replays_no_corpus_in_either_door(
    monkeypatch: pytest.MonkeyPatch, forget_the_memo: None, restore_the_reference: None
) -> None:
    """D-29(d): construction AND install of the same closed provider cost no corpus twice."""
    from okto_grafx.domain.page import checksum as checksum_module

    provider = _ProviderCounter()
    module = _fake_google_module(provider.extend)
    reference_calls = 0

    def counted(data: bytes, crc: int = 0) -> int:
        nonlocal reference_calls
        reference_calls += 1
        return crc32c_reference(data, crc)

    with monkeypatch.context() as patch:
        _install_fake_google(patch, module)
        patch.setattr(checksum_native, "crc32c_reference", counted)
        patch.setattr(checksum_module, "crc32c_reference", counted)

        first = NativeCrc32c()
        first.install()
        after_first = (reference_calls, provider.calls)
        second = NativeCrc32c()
        second.install()
        assert second.checksum(b"123456789") == 0xE3069283
        assert crc32c(b"123456789") == 0xE3069283
        after_second = (reference_calls, provider.calls)

    corpus = len(CRC32C_ACCEPTANCE_CORPUS)
    assert after_first[0] >= 2 * corpus, (
        "the first cycle proves the corpus in both doors"
    )
    assert after_second[0] == after_first[0], (
        "the second cycle replays no reference at all"
    )
    assert after_second[1] - after_first[1] == 2, "only the two real checksums ran"
    assert second._provider is first._provider, "one wrapper per strong identity"
    assert crc32c_implementation() == checksum_native.NATIVE_ADAPTER_NAME


def test_replacing_the_provider_function_replays_both_doors_and_a_wrong_one_is_refused(
    monkeypatch: pytest.MonkeyPatch, forget_the_memo: None, restore_the_reference: None
) -> None:
    from okto_grafx.domain.page import checksum as checksum_module

    module = _fake_google_module(_ProviderCounter().extend)
    validations: list[str] = []
    original = checksum_module._validate_candidate

    def counted(function, name: str) -> None:
        validations.append(name)
        original(function, name)

    with monkeypatch.context() as patch:
        _install_fake_google(patch, module)
        patch.setattr(checksum_module, "_validate_candidate", counted)
        NativeCrc32c().install()
        NativeCrc32c().install()
        assert len(validations) == 1, "the same function object is proved once"

        replacement = _ProviderCounter()
        module.extend = replacement.extend  # a new function object under the same name
        adapter = NativeCrc32c()
        assert adapter._provider.identity[4] is module.extend
        adapter.install()
        assert len(validations) == 2, "a replaced function is proved again"

        def wrong(crc: int, data: bytes) -> int:
            return crc32c_reference(data, crc) ^ 1

        module.extend = wrong
        with pytest.raises(GrafxConfigurationError):
            NativeCrc32c()
        assert checksum_native._validated_closed_providers == {}
        assert checksum_module._validated_closed_identities == ()


class _EqualitySpoofingProvider:
    """A correct callable whose equality/hash claim it is every other provider."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, crc: int, data: bytes) -> int:
        self.calls += 1
        return crc32c_reference(data, crc)

    def __eq__(self, _other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 1


def test_callable_equality_cannot_spoof_either_closed_provider_memo(
    monkeypatch: pytest.MonkeyPatch, forget_the_memo: None, restore_the_reference: None
) -> None:
    """Only ``is`` authenticates the raw callable in both independent proof doors."""
    from okto_grafx.domain.page import checksum as checksum_module

    first = _EqualitySpoofingProvider()
    second = _EqualitySpoofingProvider()
    assert first == second and hash(first) == hash(
        second
    )  # prove the adversarial premise
    module = _fake_google_module(first)
    adapter_validations = 0
    domain_validations = 0
    original_adapter_validation = NativeCrc32c._require_agreement
    original_domain_validation = checksum_module._validate_candidate

    def count_adapter_validation(adapter: NativeCrc32c) -> None:
        nonlocal adapter_validations
        adapter_validations += 1
        original_adapter_validation(adapter)

    def count_domain_validation(function, name: str) -> None:
        nonlocal domain_validations
        domain_validations += 1
        original_domain_validation(function, name)

    with monkeypatch.context() as patch:
        _install_fake_google(patch, module)
        patch.setattr(NativeCrc32c, "_require_agreement", count_adapter_validation)
        patch.setattr(checksum_module, "_validate_candidate", count_domain_validation)
        NativeCrc32c().install()
        module.extend = second
        NativeCrc32c().install()

    assert adapter_validations == 2
    assert domain_validations == 2
    assert len(checksum_native._closed_providers) == 1
    assert len(checksum_native._validated_closed_providers) == 1
    assert len(checksum_module._validated_closed_identities) == 1
    identity, _wrapper = checksum_module._validated_closed_identities[0]
    assert identity[4] is second


def test_repeated_provider_replacement_keeps_both_memos_strictly_bounded(
    monkeypatch: pytest.MonkeyPatch, forget_the_memo: None, restore_the_reference: None
) -> None:
    """Reload churn replaces one slot instead of retaining every historical function."""
    from okto_grafx.domain.page import checksum as checksum_module

    providers = [_EqualitySpoofingProvider() for _ in range(12)]
    module = _fake_google_module(providers[0])
    with monkeypatch.context() as patch:
        _install_fake_google(patch, module)
        for provider in providers:
            module.extend = provider
            NativeCrc32c().install()
            assert len(checksum_native._closed_providers) == 1
            assert len(checksum_native._validated_closed_providers) == 1
            assert len(checksum_module._validated_closed_identities) == 1

    identity, _wrapper = checksum_module._validated_closed_identities[0]
    assert identity[4] is providers[-1]


def test_an_injected_provider_never_inherits_the_installer_memo(
    monkeypatch: pytest.MonkeyPatch, forget_the_memo: None, restore_the_reference: None
) -> None:
    from okto_grafx.domain.page import checksum as checksum_module

    validations: list[str] = []
    original = checksum_module._validate_candidate

    def counted(function, name: str) -> None:
        validations.append(name)
        original(function, name)

    with monkeypatch.context() as patch:
        patch.setattr(checksum_module, "_validate_candidate", counted)
        NativeCrc32c(mirror, provider_name="vendored", verify_runtime=False).install()
        NativeCrc32c(mirror, provider_name="vendored", verify_runtime=False).install()

    assert validations == [checksum_native.NATIVE_ADAPTER_NAME] * 2
    assert checksum_module._validated_closed_identities == ()
    assert checksum_native.validated_closed_providers() == ()


def test_a_memo_identity_that_is_not_a_tuple_is_refused_by_the_door() -> None:
    from okto_grafx.domain.page.checksum import _install_validated_crc32c

    with pytest.raises(GrafxConfigurationError) as raised:
        _install_validated_crc32c(mirror, name="native", memo_identity="google_crc32c")
    assert raised.value.details["field"] == "memo_identity"


def test_concurrent_constructions_share_one_proof_and_one_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    forget_the_memo: None,
    restore_the_reference: None,
) -> None:
    import threading

    from okto_grafx.domain.page import checksum as checksum_module

    module = _fake_google_module(_ProviderCounter().extend)
    adapters: list[NativeCrc32c] = []
    failures: list[BaseException] = []
    gate = threading.Barrier(4)
    adapter_validations = 0
    domain_validations = 0
    original_adapter_validation = NativeCrc32c._require_agreement
    original_domain_validation = checksum_module._validate_candidate

    def count_adapter_validation(adapter: NativeCrc32c) -> None:
        nonlocal adapter_validations
        adapter_validations += 1
        original_adapter_validation(adapter)

    def count_domain_validation(function, name: str) -> None:
        nonlocal domain_validations
        domain_validations += 1
        original_domain_validation(function, name)

    def construct() -> None:
        try:
            gate.wait(timeout=10)
            adapter = NativeCrc32c()
            adapter.install()
            adapters.append(adapter)
        except BaseException as failure:  # pragma: no cover - reported below
            failures.append(failure)

    with monkeypatch.context() as patch:
        _install_fake_google(patch, module)
        patch.setattr(NativeCrc32c, "_require_agreement", count_adapter_validation)
        patch.setattr(checksum_module, "_validate_candidate", count_domain_validation)
        threads = [threading.Thread(target=construct) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert failures == []
    assert len(adapters) == 4
    assert len({id(adapter._provider) for adapter in adapters}) == 1
    assert checksum_native.validated_closed_providers() == ("google_crc32c",)
    assert adapter_validations == 1, "the adapter corpus runs once under contention"
    assert domain_validations == 1, (
        "the independent domain corpus runs once under contention"
    )
    assert all(adapter.checksum(b"123456789") == 0xE3069283 for adapter in adapters)


def test_the_installer_door_never_memoizes_a_refused_candidate(
    forget_the_memo: None, restore_the_reference: None
) -> None:
    from okto_grafx.domain.page import checksum as checksum_module
    from okto_grafx.domain.page.checksum import _install_validated_crc32c

    def wrong(data: bytes, crc: int) -> int:
        return crc32c_reference(data, crc) ^ 1

    identity = ("google_crc32c", "extend", "fake", "1.5.0", wrong)
    for _attempt in range(2):
        with pytest.raises(GrafxConfigurationError) as raised:
            _install_validated_crc32c(wrong, name="native", memo_identity=identity)
        assert raised.value.details["field"] == "crc32c"
        assert checksum_module._validated_closed_identities == ()
    assert crc32c_implementation() == PURE_IMPLEMENTATION_NAME
