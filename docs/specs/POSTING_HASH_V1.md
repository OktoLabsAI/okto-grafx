# Posting hash v1 (0.0.6 development)

Implementation contract for continuation item 7. Explicit property indexes only:
`layout="posting_hash"`, exact visibility and existing columns key derivation.
No automatic conversion of existing indexes. Required catalog bit 14 is
`posting_hash_v1`; catalog layout tag 3; index header format 5, layout code 4,
using the existing tagged header grammar and requiring zero reserved bytes.
Old readers must refuse the required capability before reading index pages.

The hash directory and checksummed slotted pages retain their existing layout.
Each page stores page-local key dictionary slots and reference posting slots:

* Dictionary: byte `K` followed by the exact encoded key (remaining slot bytes).
* Posting: byte `P`, dictionary slot u16, heap reference u64, dead CSN u64;
  all integer fields little endian. Exactly 19 bytes. No birth stamp: candidates
  must still pass native heap visibility/key validation under a stable certificate.
* Dictionary slots are unique by key on a page. Postings reference a live dictionary
  slot on the same page. Missing keys, invalid tags/lengths/references/stamps refuse.
* Physical posting slot IDs remain stable. Tombstones replace fixed-width bytes;
  reconciliation frees postings, and clears a page only after its last posting.
  An unused dictionary entry is allowed (including interrupted insertion) and is
  not an index candidate. Keys never span pages.

Creation/build uses the native private generation protocol. Mutations use existing
INDEX_WRITE records, complete-COMMIT admission, generation fencing, native stale
publication and idempotent key/reference replay. No new WAL record, heap format,
visibility rule, OCC baseline or concurrency mode. Physical backups preserve the
layout. Logical transfer preserves the explicit layout and rebuilds the physical
access path from transferred rows. Drop/rebuild/rehash must retain native
generation ownership. Format support does not authorize production migration.

Storage per occurrence becomes 19 bytes plus the native slot directory, with one
key slot per distinct key per page. Benefits depend on key length and repetition;
unique keys may cost more. Complete lookup remains O(output + visited bucket
pages), and scalar writes still inspect the bucket. No general latency or write
speedup is claimed without measurement. Feature/replay/old-reader/maintenance
tests and bounded physical page counts are required before this item is complete.
