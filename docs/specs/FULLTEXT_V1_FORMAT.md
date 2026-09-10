# Full-text v1 persistent contract

This defines the 0.0.5 development implementation of [GX-CAP-5](SPEC-GX-CAP-5.md).
Consumer semantics/options are in [full-text search](../FULL_TEXT_SEARCH.md).

## Authority and discrimination

Catalog format 2 gains required bit **5** (value 32), `fulltext_indexes_v1`.
Existing catalog required bits, CRC-32C, field order and ordinary index bytes remain
unchanged. A full-text logical definition is an EXACT/HASH, non-automatic node index.
The existing index metadata's derivation tag is **4**; immediately after metadata and
before column positions comes the existing u16-length UTF-8 text encoding of the
canonical analyzer derivation. Other tags have no extra text. A tag-4 record must
declare the FTS family; FTS records without the required bit refuse. Unknown bits,
analyzers, tags, malformed options, noncanonical hex and type/arity mismatches refuse.

The derivation is `fulltext_v1_` followed by lowercase hex of:

`analyzer:u8 | field_count:u8 | normalization:u8 | case_folding:u8 | max_token_bytes:u16 | max_document_characters:u32 | max_document_tokens:u32 | field_weights:field_count*f64`

All numbers are little-endian. Analyzer tags 0..3 are standard, keyword,
code_identifier, whitespace. Normalization 0..2 is none/NFC/NFKC; folding 0..2 is
none/ascii/unicode. Field count is 1..4; all option bounds are validated. The version-1
family fixes locale und, no stop/stem, Unicode-3.2 normalization/categories and the
checked-in Unicode-15.1 case-fold table. These constants cannot change under the same
family. The complete derivation already participates in the index definition digest.

No new independent manifest is authoritative. Existing nonzero generation nonces,
catalog ACTIVE/STALE states, HASH placement, checksummed pages and matching header
definition digest own physical identity and publication.

## Entries and logical WAL

For each heap version, exactly one statistics key is `0x00 | field_lengths:u32[]`.
For each distinct term across fields, a posting key is `0x01 | term:UTF-8`.
Empty/null documents still owe statistics. Each key uses the existing exact
`IndexEntry` header/ref/dead_csn encoding: unversioned candidate, birth stamp zero,
heap revalidation required. TF is derived from the candidate heap version at search;
the statistics entry stores field lengths, not an independent authoritative document.

Multi-key derivation is shared by quota counts, insert/delete/update, detached build,
ordinary rebuild and both semantic/coverage verifiers. Updates tombstone all previous
version keys and insert all new version keys, even if text is unchanged. Every delta
uses existing INDEX_WRITE/IndexChange-v1 records, in the **same COMMIT** as its heap
pages. WAL counts include every posting/statistics entry; no one-entry-per-row quota
shortcut is valid for FTS. Replay remains idempotent on key+physical ref and tombstones.

## Publication, interruption and readers

Creation/rebuild constructs a fresh complete generation under ordinary publication
fences and the existing complete-table read/OCC protocol. It verifies and barriers
the detached file before the catalog WAL publication selects it. Prior to the catalog
COMMIT it is not an active search source; an interrupted build can leave only an
unowned artifact. A proven catalog COMMIT selects the complete file on recovery.
Rebuild preserves the prior physical generation; no half-built replacement is eligible.

Updates keep both OCC phases and the WAL-before-data order. A missing/unproved COMMIT
permits no effects. After a complete durable WAL batch, interruption during heap,
posting, header or control publication must replay idempotently or refuse with evidence.
Successful read answers require a stable pre/post certificate, heap snapshot visibility
and the existing table-relative coverage proof. Both heap and index corruption are
verified; unknown capability is not a trigger for destructive repair.

Native replay validates complete catalog after-images before applying page effects.
An older build with that preflight but without bit 5 refuses activation WAL before
page application; an already-published unsupported catalog also refuses open. Do not
run a mixed writer fleet or claim arbitrary ancient binaries are compatible. Physical
restore requires a capable reader; logical transfer reconstructs supported definitions
with a fresh UUID. Analyzer upgrades use a new named complete index, not reinterpretation
of retained generations. Tests/evidence are recorded in the delivery report linked
from the [roadmap](../../ROADMAP.md), not inferred from this format document.
