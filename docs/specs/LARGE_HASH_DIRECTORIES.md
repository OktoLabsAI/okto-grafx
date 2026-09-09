# Large hash directories — item 6 contract

Status: implemented and locally validated. [Active roadmap](../../ROADMAP.md),
[acceptance receipt](../reports/V005_EIGHT_ITEM_CHECKPOINT.md).

Keep the existing eager directory layout, checksum/key placement, immutable rehash
publication and all reader/writer fences. Extend explicit sizing from 4,096 to
65,536 buckets; explicit counts retain their existing integer semantics, while
cardinality-based sizing rounds to powers of two. Defaults remain 64. Required capability
`large_hash_directories_v1` (catalog bit 7) gates every retained generation above
the old ceiling. Older builds refuse rather than write an incomplete directory.
At 8 KiB pages, 65,536 heads alone consume 512 MiB per index. This is an operator
sizing choice, not sparse allocation or a change to the default disk footprint.

Expose an explicit bounded physical-distribution diagnostic, never a query/commit
hook: entries, overflow/chain concentration and largest repeated-key fraction,
without returning key values. Physical old versions are included; this is not
live cardinality or proof that rehash solves a hot repeated key. Recommend inspecting
key skew when concentration dominates; otherwise report capacity pressure/balance.
Optional skew checking before assisted growth must refuse exhausted diagnostic
budgets and skip growth on dominant-key skew, never silently proceed after failure.

Acceptance: old/new capability refusal, boundary codecs, wide native creation,
rehash/reopen/exact old-reader results, WAL recovery and skew/collision/budget tests.
No sharding, sparse directory, online catalog retirement or universal O(1) claim.
