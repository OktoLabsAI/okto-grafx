# Persisted capability strategy for complementary milestones

Status: GX-CAP-0 contract only; no new format bits or on-disk writes.
Authority: complementary plan D16, §§6–15, 19.5; existing catalog/recovery contracts.

## Existing mechanism to extend, not replace

`src/okto_grafx/domain/model/catalog.py` supports legacy catalog v1 and v2 with
a required-capability bitmap, bounded index metadata and checksum. Current known
bits cover identity secondary indexes, heap reclamation, WAL-v2 records and
ordered secondary indexes. Unknown required bits raise a typed refusal.
`tests/storage_core/test_catalog_v2.py` and
`tests/index/test_ordered_format_discrimination.py` exercise unknown/missing bits
and older-reader discrimination. These tests prove existing behavior, not support
for future temporal, FTS or commit-metadata encodings.

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
GX-CAP-1 must first decide/prove the CommitId physical mapping and legacy boundary.
No store is upgraded and no experimental API is exported by this milestone.
