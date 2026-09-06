"""The secondary-index contract of Okto Grafx (SPEC-M1 FR-12, BR-11, SD-3).

This package holds what an index IS, never where its pages are kept: the entry format, the two
visibility rules, the key derivation, the durable header and the log records that make every
index change replayable. The store that puts those bytes on pages lives in
:mod:`okto_grafx.engine.index_manager`.

The one thing to read first is :mod:`okto_grafx.domain.index.visibility`, which states the rule a
caller may rely on for each kind of index. Everything else in this package exists to make that
rule true on disk.
"""

from __future__ import annotations

from okto_grafx.domain.index.catalog import (
    IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY,
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
    IndexGenerationState,
    identity_index_name,
)
from okto_grafx.domain.index.contract import SecondaryIndex, StagingTransaction
from okto_grafx.domain.index.definition import (
    COLUMN_KEY_DERIVATION,
    DEFINITION_DIGEST_SIZE,
    INDEX_DIRECTORY,
    INDEX_FILE_SUFFIX,
    RECORD_ID_KEY_DERIVATION,
    IndexDefinition,
    index_file,
    index_generation_file,
    require_index_name,
)
from okto_grafx.domain.index.entry import (
    ENTRY_FLAG_VERSIONED,
    INDEX_ENTRY_HEADER_SIZE,
    MAX_INDEX_KEY_BYTES,
    IndexEntry,
)
from okto_grafx.domain.index.header import (
    INDEX_HEADER_FORMAT_VERSION,
    INDEX_HEADER_SIZE,
    INDEX_HEADER_SLOT,
    IndexHeader,
)
from okto_grafx.domain.index.keys import (
    DEFAULT_BUCKET_COUNT,
    MAX_BUCKET_COUNT,
    MAX_EXPECTED_CARDINALITY,
    MIN_BUCKET_COUNT,
    RECORD_ID_KEY_FORMAT_VERSION,
    TARGET_ENTRIES_PER_BUCKET,
    bucket_of,
    custom_index_sizing,
    identity_index_sizing,
    index_key,
    record_id_key,
    rehash_index_sizing,
    validate_bucket_count,
)
from okto_grafx.domain.index.records import (
    CHANGE_FLAG_VERSIONED,
    INDEX_CHANGE_FORMAT_VERSION,
    INDEX_CHANGE_HEADER_SIZE,
    IndexChange,
    IndexOperation,
    change_of,
    lsn_of,
    wal_record_for,
)
from okto_grafx.domain.index.visibility import (
    IndexVisibility,
    ReconcileReport,
    SnapshotLike,
    entry_visible,
    is_reclaimable,
)

__all__ = [
    "CHANGE_FLAG_VERSIONED",
    "COLUMN_KEY_DERIVATION",
    "DEFAULT_BUCKET_COUNT",
    "DEFINITION_DIGEST_SIZE",
    "ENTRY_FLAG_VERSIONED",
    "INDEX_CHANGE_FORMAT_VERSION",
    "INDEX_CHANGE_HEADER_SIZE",
    "INDEX_DIRECTORY",
    "INDEX_ENTRY_HEADER_SIZE",
    "INDEX_FILE_SUFFIX",
    "INDEX_HEADER_FORMAT_VERSION",
    "INDEX_HEADER_SIZE",
    "INDEX_HEADER_SLOT",
    "IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY",
    "MAX_BUCKET_COUNT",
    "MAX_EXPECTED_CARDINALITY",
    "MAX_INDEX_KEY_BYTES",
    "MIN_BUCKET_COUNT",
    "RECORD_ID_KEY_DERIVATION",
    "RECORD_ID_KEY_FORMAT_VERSION",
    "TARGET_ENTRIES_PER_BUCKET",
    "IndexChange",
    "CatalogIndexDefinition",
    "IndexDefinition",
    "IndexEntry",
    "IndexGenerationDescriptor",
    "IndexGenerationState",
    "IndexHeader",
    "IndexOperation",
    "IndexVisibility",
    "ReconcileReport",
    "SecondaryIndex",
    "SnapshotLike",
    "StagingTransaction",
    "bucket_of",
    "change_of",
    "custom_index_sizing",
    "entry_visible",
    "index_file",
    "index_generation_file",
    "identity_index_name",
    "identity_index_sizing",
    "index_key",
    "record_id_key",
    "rehash_index_sizing",
    "is_reclaimable",
    "lsn_of",
    "require_index_name",
    "validate_bucket_count",
    "wal_record_for",
]
