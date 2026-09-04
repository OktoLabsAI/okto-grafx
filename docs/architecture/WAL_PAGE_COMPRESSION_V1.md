# WAL page-image compression v1

**Status:** accepted for `0.0.2`  
**Scope:** full-page `WRITE_PAGE` records only  
**Activation:** explicit, persistent and one way

## Decision

Grafx may encode a `WRITE_PAGE` record with WAL `format_version=2` when all of the following are
true:

1. catalog v2 is already active;
2. the catalog contains required capability `wal_record_v2`;
3. the complete raw commit batch fits the current WAL segment without a roll; and
4. zlib level 1 makes that page image strictly smaller after accounting for the four-byte
   uncompressed-length field.

Otherwise the record keeps the exact v1 full-image grammar. `COMMIT`, logical index effects and
segment headers remain v1. This is compression, not a delta protocol: redo never needs a base page
to reconstruct the post-image.

The public activation door is:

```python
db.maintenance.ensure_identity_indexes()  # activates catalog v2 when still needed
db.maintenance.enable_wal_page_compression()
```

`Database.enable_wal_page_compression()` is the equivalent direct door. Activation writes the
capability in its own ordinary WAL-before-data transaction, and that transaction contains only v1
records. The transaction manager refreshes the committed catalog capability only after durable
publication. Therefore no v2 record can precede the durable fence. Repeating activation is a
zero-write no-op.

## Wire grammar

The 48-byte WAL header is unchanged. WAL v1 flags stay opaque forever.

For v2 the only known combination in this milestone is:

| Record | Flags | Meaning |
|---|---:|---|
| `WRITE_PAGE` | `0x0001 REQUIRED` + `0x0004 PAGE_IMAGE_ZLIB1` | bounded zlib full-page image |

Bit `0x0008` is SKIPPABLE for a future unknown v2 record and must appear alone. Flags zero do not
make an unknown type optional: the safe default is required, so a future writer cannot silently
lose work merely by forgetting to set REQUIRED.

The payload is:

```text
file_name_length u16
file_name UTF-8 bytes
page_index u32
uncompressed_image_length u32
zlib_level_1_bytes
```

The target prefix `(file, page_index)` stays cleartext. Optional CE-3/read-view classification can
identify touched pages without inflating the image. The record CRC-32C covers the compressed bytes;
after bounded decompression the normal page decoder independently validates page size, identity and
page CRC-32C before any mutation.

If compression is not strictly smaller, the encoder emits the original v1 payload, version and
flags byte for byte. There is no v2 `RAW` variant.

## Closed decoder and resource limits

The v2 decoder requires the exact type/flag pair above. A known record type without a declared v2
grammar, an unknown type without the exact SKIPPABLE flag, or a malformed flag combination is
`unsupported_required_record`. Strict reads, append, recycle and recovery turn that verdict into
`GrafxSchemaVersionMismatch` before changing the WAL. An unknown v2 type with exactly the explicit
SKIPPABLE flag is the only skippable future record shape.

Inflation is bounded before allocation:

- declared length must be in `1..MAX_PAGE_SIZE`;
- `decompressobj.decompress` receives `declared_length + 1` as its output ceiling;
- output length must equal the declaration exactly;
- the zlib stream must reach EOF; and
- trailing or unconsumed bytes are corruption.

Malformed compressed data therefore fails closed before the first page in a replay batch moves.
`max_wal_batch_bytes` is applied once to the final encoded records, so admitted compressed bytes,
not their raw alternative, are charged. Transaction row/page staging budgets are unchanged.

## Segment roll and commit-number stability

`SEGMENT_HEADER` consumes an LSN. Changing record sizes while retargeting a batch can otherwise
create a feedback loop in which compression changes the roll decision, the roll changes the commit
CSN stamped into pages, and those bytes change compression size again.

The v1 policy is deliberately finite:

- plan the exact raw full-image batch first;
- if it does not roll, compress eligible page records only after the terminal CSN is final;
- if it rolls, retain every page record as v1 and retarget once using the existing protocol.

Compression only shortens a no-roll batch, so it cannot change that decision or its terminal LSN.
At most the commits that already require a segment roll miss compression. Atomicity remains one
`append_log` call for the complete transaction.

## Recovery, mixed writers and downgrade behavior

Recovery performs the normal committed-effect selection. Target-only inspection reads the clear
prefix; preflight and apply inflate with the record's version and flags and validate every image
before mutation. Reapplying the same compressed post-image remains idempotent through `page_lsn`.

A writer opened before activation must refresh catalog/WAL authority at its next protected boundary.
A current `0.0.2` participant adopts the capability and can then read and emit the grammar. A build
without this feature fails in one of two independent ways:

- unknown catalog capability bit, including after WAL recycling; or
- unsupported WAL format version while v2 records remain.

Downgrade after activation is unsupported. Stopping new compressed writes or recycling all v2 WAL
does not clear the catalog capability. There is intentionally no disable door.

## Benefits and costs

Benefits:

- materially fewer WAL bytes for sparse/slotted full pages;
- less WAL write, barrier and later scan/recovery I/O;
- no loss of full-post-image idempotence;
- no new dependency: zlib is in the Python standard library; and
- incompressible pages automatically retain v1 without expansion.

Costs:

- CPU is spent compressing committed page candidates and inflating during recovery;
- a database that activates the capability cannot be opened by an older build;
- segment-roll batches deliberately receive no compression in this first protocol; and
- compression reduces write amplification but does not change the number of WAL records.

Use it for Grafx/Pulse databases whose fleet has been upgraded together and whose workload writes
ordinary sparse or partially filled pages. Do not activate it when downgrade to a pre-capability
build must remain possible. Incompressible workloads remain correct but may not justify the CPU.

## Explicitly deferred or rejected

- **Block delta is deferred.** A safe future design needs a logged pre-image/base LSN, a
  full-page-write-after-checkpoint invariant and a four-way redo rule: exact post-image is a no-op;
  equal LSN with different post CRC fails; newer page LSN skips; exact base LSN applies; every other
  older base fails closed.
- **Physical chunking is rejected for this milestone.** Multiple `append_log` calls permit process
  death after complete effects but before `COMMIT`. Current recovery intentionally treats every
  incomplete effect tail as ambiguous and refuses availability because data pages may already
  contain those effects. `_undo_append` handles returned exceptions, not process death.
- **Physiological WAL is rejected for this milestone.** Replaying heap, slotted-page, hash-index and
  HNSW mutation logic would duplicate live mutation semantics inside recovery and enlarge the
  corruption surface without being necessary for the full-image compression gain.

These items are not silent follow-ups to this ADR. Each requires a separate accepted protocol and
its own crash/recovery proof.
