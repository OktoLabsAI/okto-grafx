"""Raw-image decode reuse never weakens the durable page-zero certificate."""

from __future__ import annotations

from dataclasses import replace
from threading import Event, Thread, current_thread
from typing import Any

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.index import INDEX_HEADER_SLOT, IndexHeader, IndexVisibility
from okto_grafx.domain.page import HEADER_PAGE_INDEX
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.index_manager import HashIndex, IndexStore

from .conftest import (
    MemoryDevice,
    RecordingMetrics,
    build_database,
    cold_view,
    exact_definition,
)


def _count_decodes(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int]:
    counts = {"generic": 0, "semantic": 0}
    decode_page = PageCodecV1.decode_page
    decode_header = IndexStore._decode_header_page

    def counted_page(codec: PageCodecV1, raw: bytes, **kwargs: Any):
        counts["generic"] += 1
        return decode_page(codec, raw, **kwargs)

    def counted_header(store: IndexStore, page: Any) -> IndexHeader:
        counts["semantic"] += 1
        return decode_header(store, page)

    monkeypatch.setattr(PageCodecV1, "decode_page", counted_page)
    monkeypatch.setattr(IndexStore, "_decode_header_page", counted_header)
    return counts


def _rewrite_durable_header(
    device: MemoryDevice,
    file: str,
    transition: Any,
) -> tuple[bytes, bytes]:
    codec = PageCodecV1(device.page_size)
    original = device.raw_page(file, HEADER_PAGE_INDEX)
    page = codec.decode_page(original, page_index=HEADER_PAGE_INDEX)
    header = IndexHeader.decode(page.read_slot(INDEX_HEADER_SLOT))
    page.update_slot(INDEX_HEADER_SLOT, transition(header).encode())
    changed = codec.encode_page(page)
    device.poke_page(file, HEADER_PAGE_INDEX, changed)
    return original, changed


def test_an_identical_fresh_image_decodes_generically_and_semantically_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    index = database.exact
    index._fresh_certificate_memo = None
    reads_before = len(database.device.read_calls)
    counts = _count_decodes(monkeypatch)

    first = index._fresh_certificate()
    second = index._fresh_certificate()

    assert second == first
    assert counts == {"generic": 1, "semantic": 1}
    assert len(database.device.read_calls) - reads_before == 2, (
        "decode reuse must not reuse the physical observation"
    )


def test_a_class_monkeypatch_cannot_forge_an_unchanged_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = build_database()
    reader = cold_view(writer)
    reader.exact._fresh_certificate_memo = None
    reader.exact._fresh_certificate()
    writer.exact.advance_built_through(37)
    forged_calls = 0

    def forged_observer(
        _pool: BufferPool,
        _file: str,
        _page_index: int,
        previous: object | None = None,
    ) -> tuple[None, object | None]:
        nonlocal forged_calls
        forged_calls += 1
        return None, previous

    monkeypatch.setattr(BufferPool, "_observe_fresh_page", forged_observer)
    reads_before = len(writer.device.read_calls)

    changed = reader.exact._fresh_certificate()

    assert changed.header.built_through_lsn == 37
    assert forged_calls == 0
    assert len(writer.device.read_calls) == reads_before + 1


def test_a_valid_foreign_change_decodes_once_and_then_reuses_its_new_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = build_database()
    reader = cold_view(writer)
    reader.exact._fresh_certificate_memo = None
    before = reader.exact._fresh_certificate()
    writer.exact.advance_built_through(31)
    reads_before = len(writer.device.read_calls)
    counts = _count_decodes(monkeypatch)

    changed = reader.exact._fresh_certificate()
    repeated = reader.exact._fresh_certificate()

    assert changed != before
    assert changed.header.built_through_lsn == 31
    assert repeated == changed
    assert counts == {"generic": 1, "semantic": 1}
    assert len(writer.device.read_calls) - reads_before == 2


def test_different_bytes_with_the_same_sequence_are_decoded_and_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database()
    index = database.exact
    index._fresh_certificate_memo = None
    accepted = index._fresh_certificate()
    accepted_memo = index._fresh_certificate_memo
    original, changed = _rewrite_durable_header(
        database.device,
        index.file,
        lambda header: replace(
            header,
            digest=bytes([header.digest[0] ^ 0xFF]) + header.digest[1:],
        ),
    )
    codec = PageCodecV1(database.device.page_size)
    assert original != changed
    assert codec.decode_page(original).seq == codec.decode_page(changed).seq == accepted.seq
    counts = _count_decodes(monkeypatch)

    with pytest.raises(GrafxIndexError) as refused:
        index._fresh_certificate()

    assert refused.value.details["field"] == "digest"
    assert counts == {"generic": 1, "semantic": 1}
    assert index._fresh_certificate_memo is accepted_memo, (
        "a refused replacement must not poison the last accepted pair"
    )


@pytest.mark.parametrize("field", ["digest", "visibility", "artifact_nonce"])
def test_a_semantically_invalid_image_does_not_install_a_fresh_memo(
    field: str,
    pool: BufferPool,
    device: MemoryDevice,
    metrics: RecordingMetrics,
    person_table: Any,
) -> None:
    nonce = 0xA11CE
    definition = replace(
        exact_definition(person_table, name="memo_authority"),
        artifact_nonce=nonce,
    )
    index = HashIndex(definition, pool, metrics)
    index._set_creation_nonce(nonce)
    index.create()
    index._fresh_certificate_memo = None

    def damage(header: IndexHeader) -> IndexHeader:
        if field == "digest":
            return replace(
                header,
                digest=bytes([header.digest[0] ^ 0xFF]) + header.digest[1:],
            )
        if field == "visibility":
            return replace(header, visibility=IndexVisibility.PROXIMITY)
        return replace(header, artifact_nonce=nonce + 1)

    _rewrite_durable_header(device, index.file, damage)

    with pytest.raises(GrafxIndexError) as refused:
        index._fresh_certificate()

    assert refused.value.details["field"] == field
    assert index._fresh_certificate_memo is None


def test_a_local_header_write_clears_the_fresh_certificate_memo() -> None:
    database = build_database()
    index = database.exact
    index._fresh_certificate_memo = None
    certificate = index._fresh_certificate()
    assert index._fresh_certificate_memo is not None

    index._write_header(certificate.header)

    assert index._fresh_certificate_memo is None


def test_interleaved_fresh_reads_keep_each_witness_paired_with_its_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = build_database()
    reader = cold_view(writer)
    reader.exact._fresh_certificate_memo = None
    reader.exact._fresh_certificate()
    writer.exact.advance_built_through(11)
    paused = Event()
    release = Event()
    original_decode = IndexStore._decode_header_page
    older: list[Any] = []
    failures: list[BaseException] = []

    def delayed_decode(store: IndexStore, page: Any) -> IndexHeader:
        header = original_decode(store, page)
        if (
            store is reader.exact
            and current_thread().name == "older-fresh-certificate"
            and header.built_through_lsn == 11
        ):
            paused.set()
            if not release.wait(timeout=5):
                raise AssertionError("the interleaved reader was not released")
        return header

    def read_older() -> None:
        try:
            older.append(reader.exact._fresh_certificate())
        except BaseException as failure:  # pragma: no cover - reported by the parent thread
            failures.append(failure)

    monkeypatch.setattr(IndexStore, "_decode_header_page", delayed_decode)
    worker = Thread(target=read_older, name="older-fresh-certificate", daemon=True)
    worker.start()
    try:
        assert paused.wait(timeout=5), "the older observation never reached semantic decode"
        writer.exact.advance_built_through(22)
        newest = reader.exact._fresh_certificate()
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert failures == []
    assert older[0].header.built_through_lsn == 11
    assert newest.header.built_through_lsn == 22
    memo = reader.exact._fresh_certificate_memo
    assert memo is not None
    witness_page = PageCodecV1(writer.device.page_size).decode_page(
        memo.witness.image,
        page_index=HEADER_PAGE_INDEX,
    )
    witness_header = IndexHeader.decode(witness_page.read_slot(INDEX_HEADER_SLOT))
    assert (memo.certificate.seq, memo.certificate.header) == (
        witness_page.seq,
        witness_header,
    )
    assert reader.exact._fresh_certificate().header.built_through_lsn == 22
