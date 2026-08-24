"""The transaction layer of Okto Grafx: snapshots, partitions and what a commit writes.

Everything here is pure value logic (G2). The snapshot predicate, the partition bucket and the
page-write payload live in this package; the protocol that uses them lives in
``okto_grafx.engine.txn_manager``, because running it means calling ports.

The frozen log FORMATS -- the record envelope, the record types and the COMMIT payload -- are
owned by C4 in ``okto_grafx.domain.wal`` and are re-exported here rather than declared a second
time (amendment A24).
"""

from __future__ import annotations

from okto_grafx.domain.txn.commit_record import (
    CATALOG_FILE_ID,
    FIRST_DERIVED_FILE_ID,
    HEAP_FILE_ID,
    MAX_FILE_ID,
    MAX_PAGE_TOUCHES_PER_COMMIT,
    MAX_PARTITIONS_PER_COMMIT,
    CommitPayload,
    FileIdMap,
    PageTouch,
)
from okto_grafx.domain.txn.commit_state import (
    COMMIT_STATE_FILE,
    COMMIT_STATE_FORMAT_VERSION,
    COMMIT_STATE_MAGIC,
    COMMIT_STATE_SIZE,
    CommitState,
)
from okto_grafx.domain.txn.context import (
    CommitReport,
    TransactionContext,
    TransactionMode,
    TransactionState,
)
from okto_grafx.domain.txn.partitions import (
    MAX_PARTITION_INDEX,
    PAGE_PARTITION_TABLE_ID,
    MAX_TABLE_ID,
    PARTITION_INDEX_BITS,
    page_partition,
    partition_index_of,
    partition_key,
    partition_of,
    split_partition_key,
    validate_partitions_per_table,
)
from okto_grafx.domain.txn.records import (
    MAX_FILE_NAME_BYTES,
    WAL_FORMAT_VERSION,
    PageWrite,
    WalRecord,
    WalRecordLike,
    WalRecordType,
    decode_page_write,
    encode_page_write,
    is_redoable_page_file,
)
from okto_grafx.domain.txn.snapshot import Snapshot

__all__ = [
    "CATALOG_FILE_ID",
    "COMMIT_STATE_FILE",
    "COMMIT_STATE_FORMAT_VERSION",
    "COMMIT_STATE_MAGIC",
    "COMMIT_STATE_SIZE",
    "FIRST_DERIVED_FILE_ID",
    "HEAP_FILE_ID",
    "MAX_FILE_ID",
    "MAX_FILE_NAME_BYTES",
    "MAX_PAGE_TOUCHES_PER_COMMIT",
    "MAX_PARTITIONS_PER_COMMIT",
    "MAX_PARTITION_INDEX",
    "MAX_TABLE_ID",
    "PAGE_PARTITION_TABLE_ID",
    "PARTITION_INDEX_BITS",
    "WAL_FORMAT_VERSION",
    "CommitPayload",
    "CommitReport",
    "CommitState",
    "FileIdMap",
    "PageTouch",
    "PageWrite",
    "Snapshot",
    "TransactionContext",
    "TransactionMode",
    "TransactionState",
    "WalRecord",
    "WalRecordLike",
    "WalRecordType",
    "decode_page_write",
    "encode_page_write",
    "is_redoable_page_file",
    "page_partition",
    "partition_index_of",
    "partition_key",
    "partition_of",
    "split_partition_key",
    "validate_partitions_per_table",
]
