"""CRC-32C known answer tests (CONTRACT.md section 6.3).

A checksum implementation that is merely self-consistent proves nothing: it would still agree
with itself after a wrong polynomial or a missing reflection, and every page ever written under
it would then be unreadable by anything else. These vectors come from the published CRC-32C
test set, so the implementation is pinned to the algorithm and not to itself.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator

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
    ("payload", "expected"), KNOWN_VECTORS, ids=[f"{index}" for index in range(len(KNOWN_VECTORS))]
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
    module does not offer. Every accepted implementation is corpus-equivalent to the reference,
    so reinstalling the reference restores exactly what was there.
    """
    yield
    install_crc32c(crc32c_reference, name=PURE_IMPLEMENTATION_NAME)
    assert crc32c_implementation() == PURE_IMPLEMENTATION_NAME


def mirror(data: bytes, crc: int) -> int:
    """An accelerator that is byte-identical because it is the reference wearing a hat."""
    return crc32c_reference(data, crc)


def test_the_shipped_door_and_the_reference_agree_on_the_whole_acceptance_corpus() -> None:
    """The digest-equality test. It is what makes the seam safe to have at all.

    An accelerated checksum that disagrees with the reference on one input does not fail loudly:
    it reports corruption on a healthy page, and the caller is then told to quarantine data that
    was never damaged. So the two are compared on every input of the corpus -- and on the
    published answers as well, so the comparison cannot be satisfied by two implementations that
    are wrong in the same way.
    """
    assert CRC32C_ACCEPTANCE_CORPUS, "an empty corpus would accept anything"
    for payload, seed in CRC32C_ACCEPTANCE_CORPUS:
        assert crc32c(payload, seed) == crc32c_reference(payload, seed), (len(payload), seed)
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
    for boundary in (255, 256, 257, 511, 512, 513, 4095, 4096, 4097, 8191, 8192):
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
    before_digests = [crc32c(payload, seed) for payload, seed in CRC32C_ACCEPTANCE_CORPUS]

    install_crc32c(mirror, name="mirror")

    assert [crc32c(payload, seed) for payload, seed in CRC32C_ACCEPTANCE_CORPUS] == before_digests
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
        ("wrong polynomial (zlib CRC-32, not CRC-32C)", lambda data, crc=0: zlib.crc32(data, crc)),
        ("arguments the wrong way round", lambda data, crc=0: crc32c_reference(
            bytes([crc & 0xFF]), len(data))),
        ("seed ignored", lambda data, crc=0: crc32c_reference(data, 0)),
        ("truncated to 16 bits", lambda data, crc=0: crc32c_reference(data, crc) & 0xFFFF),
        ("off by one on a tail length", lambda data, crc=0: crc32c_reference(data, crc)
            ^ (1 if len(data) % 8 == 5 else 0)),
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

    assert [crc32c(payload, seed) for payload, seed in CRC32C_ACCEPTANCE_CORPUS] == before
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
    assert [name for name, _attribute in CRC32C_PROVIDERS] == ["google_crc32c", "crc32c"]
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
    monkeypatch.setattr(checksum_native, "CRC32C_PROVIDERS", (("okto_grafx_no_such_crc", "x"),))
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
