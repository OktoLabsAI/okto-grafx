# Native system-history v1 — 0.0.6 development

The bounded native implementation is accepted locally in the authorized follow-up
after `204bd2e`. This specifies its bytes and recovery rules; evidence for all eight
items is in the [round report](../reports/V006_NATIVE_HISTORY_ROUND.md). It is not
a published release or a claim that every future temporal capability is complete.

## Activation and persisted capability

`Database.enable_system_history(tables: tuple[str, ...])` uses a dedicated native
write transaction. Identity indexes and commit history must already be enabled.
Each newly enabled table is fully read under the original snapshot, with all of
its logical partitions registered before first OCC. Its schema and visible rows
form the baseline in that same activation COMMIT. Relationship opt-in requires
both endpoint tables already enabled or enabled in the same transaction.
Activation is idempotent, explicit and one-way; it does not invent pre-activation
row versions. Existing-reader and writer guarantees remain intact.

Catalog v2 required bit **15**, `system_history_v1`, adds, after the existing
commit-history activation coordinate and before table definitions:

`count u32 | repeated (table_id u32, activation u64, retained_horizon u64) |
retention_revision u64 | pin_count u16 | repeated pins`.

Each pin is `name_length u16 | name_utf8 | sequence u64 | table_count u32 |
table_ids[table_count] u32`, with names sorted strictly and table IDs sorted/unique.
Names are 1–128 UTF-8 bytes, at most 1,024 pins. Pinned tables must be active and
their retained horizons cannot exceed the pin sequence. Revision is zero before
first retention; each advance is rebound to the exact native retention COMMIT.

Entries are sorted by strictly increasing existing table IDs. Count is positive
and no greater than table count. Commit-history capability is mandatory. Initially
each retained horizon equals that table's activation COMMIT. The global file
activation is the minimum table activation. Old required-bit readers refuse.

## Atomic publication and WAL

After first OCC, the native coordinator captures settled heap effects, with actual
RecordIds and materialized values; it prepares history image locations before the
second physical OCC, under the same current durable baseline as other newly
materialized pages. Caller-prestaged pages keep their original baseline. It binds
both history and journal images to the final COMMIT, including WAL segment rolls.
All history image bytes count toward transaction/WAL limits. There is no second
transaction, post-commit callback, mutable user history table or authority cache.

`system-history.dat` uses mandatory WAL-v2 flags **0x0021** (required + history),
or **0x0025** with bounded page compression. These flags are not interchangeable
with commit-journal flags. V1 framing, mismatched targets, mixed semantics and
unsupported readers refuse. The ordinary filename whitelist remains closed to
caller staging; only native coordinator preparation supplies these images.

Every writing COMMIT after file activation appends a batch, including empty
maintenance/data batches. Activation publishes its baseline at its own COMMIT.
Read-only/no-op transactions do not manufacture COMMITs. Schema changes affecting
enabled tables carry their schema event in the same native batch.

## File bytes

Pages use the native checksum and slotted-page codec, META type, one live slot,
zero flags/reserved, even publication seqlock and no chain-page link. There is no
generic FileHeader in this dedicated file; page zero is its qualified root.

The [append format description](SYSTEM_HISTORY_APPEND_DRAFT.md#prototype-bytes-and-fixed-bounds)
defines little-endian head/chunk structures, SHA-256 chain and bounded payloads.
The native extension adds operation **4 = schema**, with RecordId **0** and empty
values. Operations 1–3 require positive RecordIds; deletes have empty values.
Operations **5/6** are expired create/update payloads. Their original encoded
payload extent is retained as zero bytes, and their values are empty. Folding
requires every redacted interval to close at/before its table's retained horizon;
a redacted current version is corruption. Lineage/schema framing is preserved.
Activation permits the first batch sequence to equal file activation. Subsequent
batches strictly advance. Table schemas use the native binary catalog codec and
exact prior nullable layouts. Event bytes do not depend on future heap retention.

Limits: 4,096 settled events/batch, 64 KiB schema/event, 1 MiB values/event and
16 MiB encoded batch. Activation includes schema events in this budget. Exceeding
a limit refuses the entire operation; nothing is silently truncated. These are
internal persisted-format bounds, not new connection settings. Recovery validation
streams retained batches and is not capped by the public read's aggregate budget.

## Recovery and current acceptance boundary

The native dispatcher requires qualified checkpoint/COMMIT lineage and catalog
activation. It validates whole batches and transitions before applying any effect.
Activation images are regenerated exactly. Later appends bind their predecessor
root, ordinal, consecutive extent and hashes; overwritten roots can be reconstructed
only from the selected COMMIT's complete images plus the verified retained prefix.
Missing chunks are not borrowed from an already-applied future image.

Both resident and stored targets are checked. Publication seqlock differences are
normalized only after even-sequence validation. Foreign UUIDs, future/unproved
LSNs, conflicting images, old-chunk overwrites, extra extents and damaged pages
refuse; a greater LSN alone is never permission to skip an effect. Canonical FREE
pages allocated by interrupted application may receive their proved full images.

Native tests cover activation baseline/update/delete, checkpoint and read-only
reopen, a writer opened before activation, and process deaths before COMMIT,
before apply, after current effects, after history root/chunk and after commit,
followed by repeated recovery. API, retention, backup/transfer and adversarial
integration acceptance are recorded separately; this format specification is
not itself their completion receipt. Current consumer APIs and limits are
in [system-time history](../SYSTEM_TIME_HISTORY.md); final evidence is recorded
in the [round report](../reports/V006_NATIVE_HISTORY_ROUND.md).

## Protected retention rewrite

Retention is a dedicated native transaction with sealed catalog bytes, read
partitions and a bounded fully validated input capture. It redacts only payloads
of versions closed at/before the new selected horizons; persistent pins can refuse
the advance. Canonical old batches preserve extents and event commit coordinates,
with recomputed payload/chain digests. An empty batch binds the retention COMMIT.
Every rewritten page has the retention COMMIT's LSN; embedded event coordinates
remain older. A page stamp may therefore exceed its event sequence only within
the qualified current head boundary.

Redo admits old-chunk replacement only for a selected native COMMIT whose catalog
revision equals that COMMIT. It requires the complete consecutive file image,
same UUID/activation, exact stamps, full chain/interval validation and proved
post-checkpoint batch commit identities. No page is applied until the whole
preflight succeeds. An earlier append's resident prefix may already have been
replaced by a later selected retention COMMIT; that prefix is validated by the
later complete rewrite rather than mixing pre/post-redaction hashes. Final
resident targets still need exact known after-image witnesses, never only a
greater LSN. There is no online truncation or reclamation of old WAL/backups.
