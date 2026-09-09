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
