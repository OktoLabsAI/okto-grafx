"""The write-ahead log format: records, segments, the commit payload and replay decisions.

This package owns the on-disk shape of the log (CONTRACT.md sections 6.5 and 8.3) and the pure
decisions taken over it. It reads no device and holds no state: :mod:`okto_grafx.engine.wal_manager`
is what turns these shapes into files.

Four modules, one job each:

* :mod:`okto_grafx.domain.wal.record` -- the 48 byte header and its checksum, the one place a
  record becomes bytes;
* :mod:`okto_grafx.domain.wal.codec` -- turning bytes back into a record, or saying exactly why
  they are not one;
* :mod:`okto_grafx.domain.wal.segment` -- segment names, and the horizon rule that decides which
  segments may be recycled (BR-10);
* :mod:`okto_grafx.domain.wal.commit` -- the canonical COMMIT payload the optimistic validator
  of CONTRACT.md section 8.5 reads;
* :mod:`okto_grafx.domain.wal.replay` -- what a scan found and what a repair did.
"""

from __future__ import annotations

from okto_grafx.domain.wal.codec import (
    MAGIC_BYTES,
    DecodeOutcome,
    FailureReason,
    decode_record,
)
from okto_grafx.domain.wal.commit import (
    MAX_FILE_ID,
    MAX_PAGE_TOUCHES_PER_COMMIT,
    MAX_PARTITION_INDEX,
    MAX_PARTITIONS_PER_COMMIT,
    MAX_TABLE_ID,
    MAX_TOUCHED_PAGE_INDEX,
    PARTITION_INDEX_BITS,
    CommitPayload,
    PageTouch,
    partition_key,
    split_partition_key,
)
from okto_grafx.domain.wal.record import (
    CHECKSUM_LENGTH,
    HEADER_LENGTHS,
    MAX_DESCRIPTOR_BYTES,
    MAX_TOTAL_LENGTH,
    SUPPORTED_FORMAT_VERSIONS,
    WAL_FORMAT_VERSION,
    WAL_HEADER_LENGTH,
    WAL_MAGIC,
    WalRecord,
    WalRecordType,
    header_length_of,
    is_known_record_type,
)
from okto_grafx.domain.wal.replay import (
    MAX_FAILURE_SAMPLE_BYTES,
    RecycleReport,
    ScanFailure,
    ScanItem,
    TruncationReport,
)
from okto_grafx.domain.wal.segment import (
    MAX_SEGMENT_NUMBER,
    MIN_SEGMENT_NUMBER,
    SEGMENT_NUMBER_DIGITS,
    SEGMENT_SUFFIX,
    SegmentInfo,
    parse_segment_number,
    recyclable_prefix,
    segment_name,
)

__all__ = [
    "CHECKSUM_LENGTH",
    "HEADER_LENGTHS",
    "MAGIC_BYTES",
    "MAX_DESCRIPTOR_BYTES",
    "MAX_FAILURE_SAMPLE_BYTES",
    "MAX_FILE_ID",
    "MAX_PAGE_TOUCHES_PER_COMMIT",
    "MAX_PARTITIONS_PER_COMMIT",
    "MAX_PARTITION_INDEX",
    "MAX_SEGMENT_NUMBER",
    "MAX_TABLE_ID",
    "MAX_TOTAL_LENGTH",
    "MAX_TOUCHED_PAGE_INDEX",
    "MIN_SEGMENT_NUMBER",
    "PARTITION_INDEX_BITS",
    "SEGMENT_NUMBER_DIGITS",
    "SEGMENT_SUFFIX",
    "SUPPORTED_FORMAT_VERSIONS",
    "WAL_FORMAT_VERSION",
    "WAL_HEADER_LENGTH",
    "WAL_MAGIC",
    "CommitPayload",
    "DecodeOutcome",
    "FailureReason",
    "PageTouch",
    "RecycleReport",
    "ScanFailure",
    "ScanItem",
    "SegmentInfo",
    "TruncationReport",
    "WalRecord",
    "WalRecordType",
    "decode_record",
    "header_length_of",
    "is_known_record_type",
    "parse_segment_number",
    "partition_key",
    "recyclable_prefix",
    "segment_name",
    "split_partition_key",
]
