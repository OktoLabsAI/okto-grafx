# Persisted capability strategy for complementary milestones

Status: governing GX-CAP-0 strategy, with subsequent implemented capabilities listed
below. The strategy document itself does not activate or upgrade a store.
Authority: complementary plan D16, §§6–15, 19.5; existing catalog/recovery contracts.

## Existing mechanism to extend, not replace

`src/okto_grafx/domain/model/catalog.py` supports legacy catalog v1 and v2 with
a required-capability bitmap, bounded index metadata and checksum. Current known
bits cover identity secondary indexes, heap reclamation, WAL-v2 records and
ordered secondary indexes, commit catalog v1 (bit 4), full-text indexes v1 (bit 5),
durable FTS statistics (`fulltext_statistics_v1`, bit 6) and large hash directories
(`large_hash_directories_v1`, bit 7). The latter two are opt-in persisted additions
in the eight-item 0.0.5 continuation; unchanged defaults retain their prior bytes.
Unknown required bits raise a typed refusal.
The following continuation adds opt-in `fulltext_statistics_history_v1` (bit 8),
requiring bits 5 and 6 as well. Its bounded fixed page-0 series is specified in
[durable FTS statistics](FTS_DURABLE_STATISTICS.md); capacity zero preserves older bytes.
Bit 9 (`heap_free_page_index_v1`, requires bit 1) adds immutable
[retired-page candidate directories](HEAP_FREE_PAGE_INDEX.md). Bit 10
(`sparse_hash_directories_v1`) admits [sparse exact hash](SPARSE_HASH_DIRECTORIES.md).
Both are explicit operation opt-ins, and remain required after activation.
Bit 11 (`fulltext_prefixes_v1`, requires bit 5) adds opt-in bounded
[prefix postings and v4 metadata](FTS_PREFIX_V1.md). Bit 12
(`fulltext_relationships_v1`, requires bit 5) admits
[relationship text indexes](FTS_RELATIONSHIPS_V1.md). Both preserve the existing
catalog-before-effects WAL protocol and remain required after index removal.
Arrow import and detached projections add no persisted format or required bit.
The 0.0.6 nullable-column development slice uses required bit 13
(`nullable_columns_v1`) for bounded prior table layouts. Its catalog bytes and
atomic activation/refusal rules are specified in [Nullable columns v1](NULLABLE_COLUMNS_V1.md).
Bit 14 (`posting_hash_v1`) selects explicit exact property indexes with page-local
key dictionaries; [Posting hash v1](POSTING_HASH_V1.md) freezes the layout and
unchanged native WAL/visibility boundaries.
`tests/storage_core/test_catalog_v2.py` and
`tests/index/test_ordered_format_discrimination.py` exercise unknown/missing bits
and older-reader discrimination. The subsequent layouts are specified in
[Commit catalog v1](../architecture/COMMIT_CATALOG_V1.md) and
[Full-text v1](FULLTEXT_V1_FORMAT.md). Neither implies support for future temporal
encodings or unknown analyzer versions.

No second free-form JSON manifest may bypass that admission. Optional package
installation, Python API availability, enabled runtime policy, index readiness and
persisted required capabilities are separate facts. User-visible capability
reporting must distinguish them rather than returning one optimistic boolean.

## Rules for each persisted milestone

1. Define the feature's required name/version and minimum reader/writer before
   allocating a bit. Specify the exact bytes, reserved fields, bounds, checksums,
   dependencies and unknown-version refusal. Do not reuse bits or repurpose bytes.
2. Give the feature a one-way activation protocol before any incompatible effect is
   possible. Prove all crash cuts, including retained-WAL replay by an old build:
   a catalog bit written too late does not protect an old recovery implementation.
   Activation ordering must follow the applicable existing WAL/catalog protocol,
   not an invented generic “write flag last” rule.
3. Define empty activation and partially built index/history state. Metadata claiming
   readiness before complete durable coverage is forbidden. Interrupted builds use
   a proved prior generation/fallback or refuse; they never answer a silent subset.
4. Preserve concurrency fences, dual OCC, old-reader schemas/snapshots, WAL-before-
   data, durable ACK and replay idempotence. New feature metadata must not introduce
   a shared mutable authority cache or process-global writer bottleneck.
5. Enumerate normal open, read-only open after crash, repair/recovery, verify, backup,
   restore, logical export/import, offline format migration and downgrade behavior.
   “No downgrade” must be explicit, with a supported logical transfer route when
   possible; dropping an index is not permission to remove a required format bit.
6. Verify new structures and cross-structure links. Corrupt payloads, missing files,
   false coverage/UUID/generation, unknown bits and truncated writes are hostile
   inputs. A fresh valid checksum alone is insufficient semantic authority.
7. Keep old-format golden fixtures; prove byte compatibility for unchanged paths,
   new-build compatibility with old stores and old-build refusal of new features.
   Include Windows/POSIX and multiprocess/fault tests when mechanisms are touched.

## Optional/skippable metadata

The current required bitmap is not a generic optional-TLV extension mechanism.
Skippable metadata needs its own versioned, bounded grammar and proof that omitting
it cannot affect correctness, visibility, ranking interpretation, durability or
authorization. Metadata required to interpret an analyzer/history/index is required,
even if its implementation happens to reside in an optional wheel.

## GX-CAP-0 limit

Six ADRs and the milestone routing specs define semantics and ownership only.
GX-CAP-1 subsequently proved the bounded N4 mapping/legacy boundary and exposed
the [commit history API](../COMMIT_HISTORY.md). No store was upgraded by the
original GX-CAP-0 documentation milestone; activation remains explicit per feature.
