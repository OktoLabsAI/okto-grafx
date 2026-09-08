"""Actual WAL size/roll planner with journal images; no journal WAL is appended.

These use required journal grammar, but remain size-only previews: replay and
automatic publication are still integration prerequisites. No append is performed.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.model.value import Timestamp
from okto_grafx.domain.txn.commit_identity import CommitId
from okto_grafx.domain.txn.commit_metadata import CommitMetadata
from okto_grafx.domain.txn.records import encode_page_write_record
from okto_grafx.domain.wal.commit import CommitPayload
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.engine.commit_catalog_store import CommitCatalogPlan, CommitCatalogStore
from okto_grafx.engine.wal_manager import WalManager


def size_envelopes(plan: CommitCatalogPlan) -> tuple[WalRecord, ...]:
    pages: list[WalRecord] = []
    for image in plan.images:
        encoded = encode_page_write_record(image.file, image.page_index, image.raw, compress=False)
        pages.append(WalRecord(
            int(WalRecordType.WRITE_PAGE), epoch=1, txn_id=9,
            payload=encoded.payload, format_version=encoded.format_version, flags=encoded.flags,
        ))
    return (*pages, WalRecord(
        int(WalRecordType.COMMIT), epoch=1, txn_id=9,
        payload=CommitPayload.build(
            snapshot_lsn=plan.head.activation_sequence,
            read_partitions=(), write_partitions=(), page_touches=(),
        ).encode(),
    ))


@pytest.mark.parametrize("roll", [False, True])
def test_rebind_converges_to_actual_wal_planner_without_a_journal_append(
    make_wal: Callable[..., WalManager], memory_device: MemoryStorageDevice, roll: bool,
) -> None:
    wal = make_wal(memory_device, segment_bytes=4096)
    wal.append(WalRecord(
        int(WalRecordType.BEGIN), payload=b"x" * (3000 if roll else 32), epoch=1, txn_id=1,
    ))
    baseline = wal.last_lsn
    uuid = bytes(range(16))
    pages: dict[tuple[str, int], bytes] = {}
    store = CommitCatalogStore(lambda file, index: pages[file, index], database_uuid=uuid, page_size=512)
    for image in store.plan_initialize(activation_sequence=baseline).images:
        pages[image.file, image.page_index] = image.raw
    prepared = store.prepare_append(
        expected_last_sequence=baseline, observed_at=Timestamp(50),
        metadata_bytes=CommitMetadata(reason="bounded WAL sizing").canonical_bytes,
    )
    predicted = baseline + prepared.image_count + 1
    before = {file: memory_device.log_size(file) for file in memory_device.list_files("wal/")}
    provisional = prepared.bind(predicted)
    terminal = wal.planned_terminal_lsn(size_envelopes(provisional))
    assert terminal == predicted + int(roll)
    final = prepared.bind(terminal)
    assert wal.planned_terminal_lsn(size_envelopes(final)) == terminal
    assert wal.last_lsn == baseline
    assert {file: memory_device.log_size(file) for file in memory_device.list_files("wal/")} == before
    for image in final.images:
        pages[image.file, image.page_index] = image.raw
    record = store.lookup(CommitId(uuid, terminal), read_lsn=terminal)
    assert record is not None and record.identity.sequence == terminal
    assert store.verify().last_sequence == terminal
