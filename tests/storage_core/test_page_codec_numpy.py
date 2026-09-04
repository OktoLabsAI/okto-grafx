"""Differential contract for the optional NumPy-backed page codec."""

from __future__ import annotations

import struct
import random
from collections.abc import Callable
from pathlib import Path

import pytest

numpy = pytest.importorskip("numpy")

from okto_grafx import connect  # noqa: E402
from okto_grafx.adapters.codec_numpy import (  # noqa: E402
    NUMPY_DECODE_MIN_SLOTS,
    NumpyPageCodecV1,
)
from okto_grafx.adapters.codec_v1 import PageCodecV1  # noqa: E402
from okto_grafx.domain.errors import GrafxError  # noqa: E402
from okto_grafx.domain.page import (  # noqa: E402
    PAGE_HEADER_SIZE,
    SLOT_ENTRY_SIZE,
    Page,
    PageType,
    crc32c,
)
from okto_grafx.domain.ports.codec import PageCodec  # noqa: E402
from okto_grafx.runtime.bootstrap import (  # noqa: E402
    build_default_registry,
    release_ports,
)
from okto_grafx.runtime.config import DatabaseConfig  # noqa: E402

pytestmark = pytest.mark.optional_dependency("numpy")

PAGE_SIZE: int = 8192


def _page(
    slot_count: int, *, payload_size: int = 1, page_size: int = PAGE_SIZE
) -> Page:
    page = Page(int(PageType.HEAP), page_size=page_size, page_index=7)
    page.page_lsn = 91
    for slot in range(slot_count):
        page.insert_slot(bytes((slot % 251,)) * payload_size)
    return page


def _reseal(image: bytearray) -> bytes:
    image[:4] = crc32c(bytes(image[4:])).to_bytes(4, "little")
    return bytes(image)


def _failure(codec: PageCodecV1 | NumpyPageCodecV1, image: bytes) -> tuple[object, ...]:
    try:
        codec.decode_page(image, page_index=7)
    except GrafxError as failure:
        return type(failure), failure.message, dict(failure.details), failure.retryable
    raise AssertionError("the planted damage was accepted")


def _outcome(
    codec: PageCodecV1 | NumpyPageCodecV1,
    image: bytes,
    *,
    verify: bool = True,
) -> tuple[object, ...]:
    try:
        return "ok", codec.decode_page(image, page_index=7, verify=verify)
    except GrafxError as failure:
        return type(failure), failure.message, dict(failure.details), failure.retryable


def test_the_numpy_codec_satisfies_the_unchanged_port() -> None:
    codec = NumpyPageCodecV1(PAGE_SIZE)
    assert isinstance(codec, PageCodec)
    assert codec.format_version == 1
    assert codec.page_size == PAGE_SIZE


def test_the_numpy_selector_binds_the_numpy_codec_per_database() -> None:
    registry = build_default_registry(
        DatabaseConfig(path=":memory:", page_size=PAGE_SIZE, codec="numpy")
    )
    try:
        codec = registry.get("codec")
        assert isinstance(codec, NumpyPageCodecV1)
        assert codec.page_size == PAGE_SIZE
    finally:
        release_ports(registry)


@pytest.mark.parametrize("slot_count", [0, 1, 8, 40, 96, 200, 1000])
def test_pure_and_numpy_encoders_are_byte_identical(slot_count: int) -> None:
    page = _page(slot_count)
    pure = PageCodecV1(PAGE_SIZE)
    accelerated = NumpyPageCodecV1(PAGE_SIZE)
    expected = pure.encode_page(page)
    actual = accelerated.encode_page(page)
    assert actual == expected
    assert accelerated.decode_page(actual, page_index=7) == pure.decode_page(
        expected,
        page_index=7,
    )


def test_freed_and_compacted_directories_remain_byte_identical() -> None:
    page = _page(200, payload_size=4)
    for slot in range(0, 200, 3):
        page.free_slot(slot)
    page.compact()
    pure = PageCodecV1(PAGE_SIZE)
    accelerated = NumpyPageCodecV1(PAGE_SIZE)
    image = accelerated.encode_page(page)
    assert image == pure.encode_page(page)
    assert accelerated.decode_page(image, page_index=7) == page


def test_dense_native_decode_does_not_repeat_the_pure_directory_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codec = NumpyPageCodecV1(PAGE_SIZE)
    image = PageCodecV1(PAGE_SIZE).encode_page(_page(NUMPY_DECODE_MIN_SLOTS))

    def unexpected(*_args: object, **_kwargs: object) -> Page:
        raise AssertionError("the valid dense page fell back to the pure decoder")

    monkeypatch.setattr(PageCodecV1, "decode_page", unexpected)
    decoded = codec.decode_page(image, page_index=7)
    assert decoded.slot_count == NUMPY_DECODE_MIN_SLOTS


def test_a_small_directory_deliberately_keeps_the_oracle_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codec = NumpyPageCodecV1(PAGE_SIZE)
    image = PageCodecV1(PAGE_SIZE).encode_page(_page(8))
    calls = 0
    original = PageCodecV1.decode_page

    def observed(delegate: PageCodecV1, *args: object, **kwargs: object) -> Page:
        nonlocal calls
        calls += 1
        return original(delegate, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(PageCodecV1, "decode_page", observed)
    assert codec.decode_page(image, page_index=7).slot_count == 8
    assert calls == 1


def test_dense_corruption_has_exact_oracle_error_parity() -> None:
    pure = PageCodecV1(PAGE_SIZE)
    accelerated = NumpyPageCodecV1(PAGE_SIZE)
    valid = pure.encode_page(_page(200))

    def checksum(image: bytearray) -> bytes:
        image[PAGE_HEADER_SIZE] ^= 0xFF
        return bytes(image)

    def free_end(image: bytearray) -> bytes:
        struct.pack_into("<H", image, 24, PAGE_SIZE - SLOT_ENTRY_SIZE)
        return _reseal(image)

    def outside(image: bytearray) -> bytes:
        struct.pack_into("<HH", image, PAGE_SIZE - SLOT_ENTRY_SIZE, 0, 7)
        return _reseal(image)

    def overlap(image: bytearray) -> bytes:
        struct.pack_into(
            "<HH",
            image,
            PAGE_SIZE - 2 * SLOT_ENTRY_SIZE,
            PAGE_HEADER_SIZE,
            2,
        )
        return _reseal(image)

    mutations: tuple[Callable[[bytearray], bytes], ...] = (
        checksum,
        free_end,
        outside,
        overlap,
    )
    for mutate in mutations:
        damaged = mutate(bytearray(valid))
        assert _failure(accelerated, damaged) == _failure(pure, damaged)


@pytest.mark.parametrize("page_size", [4096, 8192, 32768])
def test_seeded_dense_mutants_have_complete_result_or_refusal_parity(
    page_size: int,
) -> None:
    pure = PageCodecV1(page_size)
    accelerated = NumpyPageCodecV1(page_size)
    valid = pure.encode_page(_page(200, page_size=page_size))
    randomizer = random.Random(0x13C0DEC)
    for _case in range(1000):
        mutant = bytearray(valid)
        for _change in range(1 + randomizer.randrange(3)):
            offset = randomizer.randrange(4, page_size)
            mutant[offset] ^= 1 << randomizer.randrange(8)
        raw = bytes(mutant)
        assert _outcome(accelerated, raw, verify=False) == _outcome(
            pure,
            raw,
            verify=False,
        )


@pytest.mark.parametrize("invalid_entry", [(-1, 1), (70000, 1), (32, 70000)])
def test_privately_corrupted_slot_values_retain_exact_oracle_refusal(
    invalid_entry: tuple[int, int],
) -> None:
    page = _page(40)
    page._slots[0] = invalid_entry
    pure = PageCodecV1(PAGE_SIZE)
    accelerated = NumpyPageCodecV1(PAGE_SIZE)
    with pytest.raises(GrafxError) as expected:
        pure.encode_page(page)
    with pytest.raises(GrafxError) as actual:
        accelerated.encode_page(page)
    assert type(actual.value) is type(expected.value)
    assert actual.value.message == expected.value.message
    assert dict(actual.value.details) == dict(expected.value.details)


def test_wrong_length_and_non_page_encoding_have_exact_error_parity() -> None:
    pure = PageCodecV1(PAGE_SIZE)
    accelerated = NumpyPageCodecV1(PAGE_SIZE)
    with pytest.raises(GrafxError) as pure_length:
        pure.decode_page(bytes(PAGE_SIZE - 1))
    with pytest.raises(GrafxError) as native_length:
        accelerated.decode_page(bytes(PAGE_SIZE - 1))
    assert type(native_length.value) is type(pure_length.value)
    assert native_length.value.message == pure_length.value.message
    assert dict(native_length.value.details) == dict(pure_length.value.details)

    with pytest.raises(GrafxError) as pure_value:
        pure.encode_page(b"not a page")  # type: ignore[arg-type]
    with pytest.raises(GrafxError) as native_value:
        accelerated.encode_page(b"not a page")  # type: ignore[arg-type]
    assert type(native_value.value) is type(pure_value.value)
    assert native_value.value.message == pure_value.value.message
    assert dict(native_value.value.details) == dict(pure_value.value.details)


def test_numpy_and_pure_handles_share_one_durable_format(tmp_path: Path) -> None:
    root = tmp_path / "db"
    with connect(root, codec="numpy") as accelerated:
        assert accelerated.codec.implementation == "NumpyPageCodecV1"
        assert accelerated.codec.process_checksum_implementation in {"native", "pure"}
        with accelerated.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))"
            )
        with accelerated.begin("write") as rows:
            for identity in range(40):
                rows.execute(
                    "CREATE (:P {id: $id, name: $name})",
                    {"id": identity, "name": f"name-{identity}"},
                )
        accelerated.checkpoint()
        assert accelerated.verify("all").findings == ()

    with connect(root, codec="pure", read_only=True) as pure:
        assert pure.codec.implementation == "PageCodecV1"
        assert pure.codec.process_checksum_implementation in {"native", "pure"}
        assert pure.execute("MATCH (p:P) RETURN p.id").rows == tuple(
            (identity,) for identity in range(40)
        )
        assert pure.verify("all").findings == ()


def test_codec_receipts_separate_instance_codec_from_process_checksum() -> None:
    with (
        connect(":memory:", codec="pure", checksum="pure") as pure,
        connect(":memory:", codec="numpy", checksum="pure") as accelerated,
    ):
        assert pure.codec.implementation == "PageCodecV1"
        assert accelerated.codec.implementation == "NumpyPageCodecV1"
        assert pure.codec.process_checksum_implementation == "pure"
        assert accelerated.codec.process_checksum_implementation == "pure"
