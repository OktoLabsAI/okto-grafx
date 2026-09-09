# Opt-in sparse hash directories — next round item 4

Implemented and locally validated; see the [acceptance receipt](../reports/V005_NEXT_EIGHT_PROGRESS.md).
`layout="sparse_hash"` selects
explicit exact property indexes. Eager `hash` remains the default. No automatic
index conversion, FTS/vector conversion or sharding is bundled into this slice.

Required catalog capability: `sparse_hash_directories_v1`, bit 10; catalog layout
tag 2. Definition digests include the layout. Native index header format 4 uses
the format-3 tagged header grammar with layout code 3, exact visibility and the
normal 1..65,536 bucket range. Format 3 remains ORDERED-only; older versions refuse
the new required capability/header/layout rather than treating it as eager hash.

After page 0, a small fixed directory stores u32 bucket-head references, initially
NO_PAGE, in page-sized chunks. Each directory page has a format discriminator,
its first bucket number and exact pointer count. Hash entry pages are allocated
only for populated buckets; standard entry encoding, hash selection, chains,
visibility, identity/generation certificates and complete-commit redo remain.
Missing buckets read empty only through a validated directory pointer, never
through missing files/pages or corruption. Directory bounds/types/padding and
selected head ownership must be validated. META directory pages have flags/reserved
zero, terminal next_page and exactly one slot: little-endian `<8sII>` magic
`GRFXSHD1`, first bucket, pointer count, followed by that many u32 heads. Capacity
is `(page_size - 32 - 4 - 16) // 4`. Exact length, identity and pointer bounds are
checked on access; no missing-directory fallback is allowed.

A one-page decoded-directory memo may reuse pointer decoding only when the exact
current slot bytes and directory ordinal match. Page shape is still checked on
every access and a selected populated head is checked against current file extent.
Empty buckets need no per-bucket extent probe; the ordinary generation certificate
still brackets reads. This memo is not a directory-presence or storage-authority cache.

The first insertion into an empty bucket initializes and durably barriers its
unreachable head before publishing the directory pointer. A pre-pointer crash
can leave only an unreachable orphan, not a reachable unwritten head. Logical
INDEX_WRITE replay reuses a valid published head or initializes an absent bucket
under complete-COMMIT authority. Duplicate replay does not duplicate entries.
This adds first-bucket publication I/O; it is a space/empty-creation improvement,
not a promise that all writes become faster. Existing hot buckets keep their
ordinary write protocol. Rebuild/rehash publishes an immutable new generation.

Acceptance must cover empty footprint at wide bucket counts, complete exact query
and update/delete/reopen parity, old snapshots/generations, directory corruption,
first-head crash cuts, repeated replay, physical backup/logical declaration
round-trip and typed refusal by previous readers. Numeric latency gates are not
required; physical page counts and exact answers are required evidence.
