"""Required journal framing; no automatic publication or replay authorization."""

from __future__ import annotations

from collections.abc import Callable
from random import Random

import pytest

from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxSchemaVersionMismatch
from okto_grafx.domain.txn.records import (
    COMMIT_CATALOG_PAGE_FILES, decode_page_write, encode_page_write,
    encode_page_write_record, is_redoable_page_file,
)
from okto_grafx.domain.wal import codec
from okto_grafx.domain.wal.codec import FailureReason, decode_record
from okto_grafx.domain.wal.record import (
    WAL_V2_FLAG_COMMIT_CATALOG_V1, WalRecord, WalRecordType,
    v2_record_semantics_error,
)
from okto_grafx.engine.wal_manager import WalManager


@pytest.mark.parametrize("file", sorted(COMMIT_CATALOG_PAGE_FILES))
@pytest.mark.parametrize("compress, compressible", [(False, True), (True, True), (True, False)])
def test_required_journal_roundtrip_even_when_compression_does_not_win(
    file: str, compress: bool, compressible: bool,
) -> None:
    image = bytes(512) if compressible else Random(41).randbytes(512)
    encoded = encode_page_write_record(file, 1, image, compress=compress)
    expected_flags = 0x15 if compress and compressible else 0x11
    assert WAL_V2_FLAG_COMMIT_CATALOG_V1 == 0x10
    assert (encoded.format_version, encoded.flags) == (2, expected_flags)
    assert encoded.compressed == (compress and compressible)
    record = WalRecord(WalRecordType.WRITE_PAGE, encoded.payload, format_version=2, flags=encoded.flags)
    outcome = decode_record(record.encode())
    assert outcome.record == record and outcome.checked
    decoded = decode_page_write(record.payload, format_version=2, flags=record.flags)
    assert (decoded.file, decoded.page_index, decoded.image) == (file, 1, image)
    # Framing is not replay authority: the native integration remains closed.
    assert not is_redoable_page_file(file)


@pytest.mark.parametrize("flags", range(64))
def test_write_page_v2_accepts_only_the_three_exact_grammars(flags: int) -> None:
    assert (v2_record_semantics_error(WalRecordType.WRITE_PAGE, flags) is None) == (flags in {5, 17, 21})


@pytest.mark.parametrize("file", sorted(COMMIT_CATALOG_PAGE_FILES))
@pytest.mark.parametrize("version,flags", [(1, 0), (1, 17), (2, 5)])
def test_journal_targets_cannot_hide_in_legacy_or_compression_only_grammar(file: str, version: int, flags: int) -> None:
    # Valid prefix is sufficient: refusal must happen before trusting/inflating image bytes.
    raw = encode_page_write(file, 0, b"not-an-image")
    with pytest.raises(GrafxSchemaVersionMismatch):
        decode_page_write(raw, format_version=version, flags=flags)


@pytest.mark.parametrize("file", ["heap.dat", "catalog.dat", "index/test.idx", "../commits.dir", "commits.dir/", "commits\\dir"])
@pytest.mark.parametrize("flags", [17, 21])
def test_required_journal_semantics_refuse_other_targets_before_inflate(file: str, flags: int) -> None:
    raw = encode_page_write(file, 0, b"not-a-zlib-stream")
    with pytest.raises(GrafxCorruptionDetected) as failure:
        decode_page_write(raw, format_version=2, flags=flags)
    assert failure.value.details["field"] == "commit_catalog_target"


@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.parametrize("door", ["read_from", "append", "recycle"])
def test_old_grammar_refuses_without_log_mutation(
    compress: bool, door: str, make_wal: Callable[..., WalManager],
    memory_device: MemoryStorageDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    wal = make_wal(memory_device)
    encoded = encode_page_write_record("commits.dir", 0, bytes(512), compress=compress)
    wal.append(WalRecord(WalRecordType.WRITE_PAGE, encoded.payload, format_version=2, flags=encoded.flags))
    before = {file: memory_device.read_log(file, 0, memory_device.log_size(file)) for file in memory_device.list_files("wal/")}

    def old_semantics(record_type: int, flags: int) -> str | None:
        if record_type == int(WalRecordType.WRITE_PAGE):
            return None if flags == 5 else "Legacy decoder requires flags 0x0005."
        return v2_record_semantics_error(record_type, flags)

    monkeypatch.setattr(codec, "v2_record_semantics_error", old_semantics)
    reopened = make_wal(memory_device)
    assert reopened.damage is not None
    assert reopened.damage.reason is FailureReason.UNSUPPORTED_REQUIRED_RECORD
    with pytest.raises(GrafxSchemaVersionMismatch):
        if door == "read_from":
            tuple(reopened.read_from(0))
        elif door == "append":
            reopened.append(WalRecord(WalRecordType.BEGIN))
        else:
            reopened.recycle(reopened.last_lsn + 100)
    after = {file: memory_device.read_log(file, 0, memory_device.log_size(file)) for file in memory_device.list_files("wal/")}
    assert after == before


def test_ordinary_legacy_flags_keep_their_opaque_meaning() -> None:
    raw = encode_page_write("heap.dat", 1, b"raw")
    assert decode_page_write(raw, format_version=1, flags=17).image == b"raw"
