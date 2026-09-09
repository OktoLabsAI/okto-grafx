"""Static keyword contract for connect; DatabaseConfig remains runtime authority."""
from __future__ import annotations
from typing import Literal, TypedDict


class ConnectOptions(TypedDict, total=False):
    """Optional connect keywords with no defaults or validation duplicated here.

    This is an ordinary dictionary at runtime. DatabaseConfig still validates all
    types, ranges, combinations and unknown keys before opening a database.
    """

    page_size: int
    partitions_per_table: int
    identity_lease_size: int
    buffer_budget_bytes: int
    max_open_files: int
    recovery_policy: Literal["replay", "refuse"]
    lease_ttl_seconds: float
    lease_timeout_seconds: float
    commit_lock_timeout_seconds: float
    reader_stall_threshold_seconds: float
    wal_segment_bytes: int
    wal_max_bytes: int | None
    checkpoint_interval_records: int
    max_statement_writes: int | None
    max_result_rows: int | None
    max_intermediate_rows: int | None
    max_traversal_expansions: int | None
    max_traversal_paths: int | None
    max_transaction_rows: int | None
    max_transaction_bytes: int | None
    max_wal_batch_bytes: int | None
    max_index_build_entries: int | None
    automatic_index_expected_cardinality: int | None
    metrics: Literal["noop", "openmetrics", "json"]
    metrics_destination: str | None
    allow_remote_metrics: bool
    codec: Literal["pure", "numpy"]
    vector_math: Literal["auto", "pure", "numpy"]
    checksum: Literal["auto", "pure", "native"]
    vector_exact_scan_threshold: int
    vector_ef_search: int
    vector_hnsw_memory_budget_bytes: int | None
    read_only: bool
    descriptor_revalidation: Literal["strict", "generation"]
    max_query_value_characters: int
    query_memory_budget_bytes: int | None

__all__ = ["ConnectOptions"]
