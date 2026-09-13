# Native FTS positional postings v1

0.0.6 opt-in `TextIndexOptions.positions=True` uses `fulltext_v5_` plus the same
canonical analyzer header and field weights as v4, followed by three bytes:
durable-statistics mode (0/1), statistics-history capacity, prefix character bound.
Unlike v4, the prefix bound may be zero. The v5 identifier implies positions; no
unrecognized trailer is ignored. Catalog required bit 16 depends on base FTS.

Length statistics (tag 0), lexical postings (tag 1) and optional prefixes (tag 2)
are unchanged. Additional tag-3 keys encode little endian:

`03 | term_utf8_length:u16 | term_utf8 | field:u8 | chunk:u16 |
last_field_position:u16 | count:u8 | positions[count]:u16`.

The term has 1–128 UTF-8 bytes. Fields are 0–3; chunks are zero-based, consecutive
groups of 32 occurrences; count is 1–32, final chunks may be shorter. Positions
are strictly increasing, zero-based and bounded by the field's last position.
The existing analyzer permits at most 65,536 tokens/document. Empty fields emit
no positional chunks. Canonical generation orders fields, terms, then chunks.

`IndexDefinition.bucket_for` uses the ordinary CRC-32C term key (`01|term`) for
tag-3 routing; other keys/derivations keep existing CRC placement. This rule is
part of the persisted derivation identity and is used by build, live writes and
redo. No generic reinterpretation of property-index bytes is permitted.

Each chunk is an ordinary native exact IndexEntry carrying its heap reference
and tombstone semantics. Native index WAL/reconciliation, catalog generations,
full-image page durability and semantic verification remain the only write path.
Phrase reads verify chunk shape, exact heap-derived positions, complete chunk
coverage and lexical counterparts under the same snapshot and pre/post index
certificate before intersecting relative positions. No missing chunk is silently
reconstructed into a successful result. Search/transaction budgets charge extra
entries and retained evidence; failures return no partial hits.

Rebuild and logical transfer regenerate positions from the canonical analyzer;
physical backup preserves bytes. Old readers without required bit 16 refuse.
There is no silent conversion, downgrade, public highlighting offset API, slop
or prefix/phrase combination. [Consumer trade-offs](../FULL_TEXT_SEARCH.md).
