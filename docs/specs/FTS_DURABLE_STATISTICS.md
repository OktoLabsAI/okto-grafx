# Durable FTS corpus statistics — implementation contract

Status: approved item 5 implemented and locally validated. See the
[eight-item acceptance receipt](../reports/V005_EIGHT_ITEM_CHECKPOINT.md).
This extends the active [roadmap](../../ROADMAP.md), not a second backlog.

## Bounded scope

Opt-in `TextIndexOptions.statistics_mode="durable"`; default `"wal"` retains the
existing derivation and file semantics. A new fulltext derivation and required
catalog capability identify durable-statistics indexes. Older builds must refuse
before writing. Existing indexes are not silently upgraded. Logical transfer preserves
the declaration and rebuilds summaries; physical backup preserves its native pages.

One page-0 record stores a format discriminator, the last **complete COMMIT LSN**,
document count and unsigned field-length totals (one to four fields). The ordinary
index file/page identity, checksum, artifact nonce and pre/post certificates cover
that record. Missing/malformed/foreign/future metadata refuses; it is never guessed
as zero. Summary lookup is eligible only when its committed coverage reaches the
covered table's known high-water and does not exceed the reader snapshot. Older
snapshots retain the exact census/WAL path; no historical-summary retention promise.

## Publication and recovery

Lengths already exist in native per-document FTS postings (`00 + u32[]`). Reduce
the complete committed index-effect multiset once: INSERT adds one document and
lengths, TOMBSTONE subtracts, REMOVE only reclaims old physical entries, RESET
starts a new build. No additional tokenization or WAL effect type is necessary.
Publish the scalar summary only after all effects of that COMMIT have been applied,
and before commit-state publication or WAL recycling. Summary and its last-COMMIT
marker occupy one checksummed page image, not independently written counters.

Replay requires the existing full commit-boundary preflight. Group effects by their
proved COMMIT, in commit order. Replaying a COMMIT at/below the scalar's marker is
idempotent even when bucket effects were already applied before interruption. Native
standalone logical-effect dispatch is not sufficient authority to advance a summary.
Both OCC validations, publication fences and WAL barriers remain unchanged.

Initial detached-generation construction is a separate authority path: the existing
fenced canonical build accumulates count/lengths from its already-generated live
statistics entries, writes the summary at that committed source cut, and independently
verifies entries/totals before barriers and catalog publication. It does not pretend
that a partial private build is a complete runtime COMMIT reduction.

Acceptance must cut before/after bucket and summary publication, reopen with a foreign
reader/writer, recycle WAL, compare exact BM25 against independent census, exercise
update/delete/rollback/empty/null/multi-field/rebuild and refuse bad metadata. Full
verification independently recomputes the scalar totals. Performance claims require
operation counts; no numeric latency gate. The acceptance receipt records these
tests, device-only semantic tamper validation and the final clean regression.
