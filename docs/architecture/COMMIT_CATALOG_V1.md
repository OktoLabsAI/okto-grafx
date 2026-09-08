# Commit catalog v1 — CAP-1B persistence contract

Status: record codec implemented and validated; activation, paged store and transactional
wiring NOT enabled. Authority: ADR GX-003 and SPEC-GX-CAP-1. Date: 2026-09-08.

## Storage and ordering decision

The logical catalog must outlive WAL recycling. It cannot be a scan of the retained
WAL or an in-memory list rewritten on every commit. The selected layout has a
paged append-only record stream and an ordinal directory of fixed-width entries
for binary-search lookup by CommitId. Variable-sized records may span pages; an
empty metadata commit must not reserve the maximum 64 KiB payload. Directory/page
headers and their exact encoding remain to be specified before enabling the store.
No file or format capability is registered by this record-codec checkpoint.

CommitId.sequence maps to the final writing COMMIT LSN, including segment-roll
retargeting, not the speculative LSN before WAL planning. The existing forward-
commit guard and durable control state remain authoritative. Every writing COMMIT
after activation, including maintenance commits, receives a catalog entry; the
record distinguishes data and maintenance. Non-writing transaction completion
returns its snapshot and creates no entry. The activation commit precedes the
tracked interval; a persisted legacy boundary must disclose the absence of earlier
logical history, never fabricate timestamps/metadata for recycled commits.

## Logical record encoding v1

The record is independent of its eventual page fragmentation. Integers are little
endian; the envelope has a 56-byte header, metadata bytes, then CRC-32C over all
preceding bytes. Maximum total size is 65,596 bytes.

| Offset | Field | Encoding |
|---:|---|---|
| 0 | magic | 8 bytes `GXCMREC` followed by NUL |
| 8 | version | u16, exactly 1 |
| 10 | flags | u16: bit 0 clock_adjusted, bit 1 metadata_present, bit 2 maintenance |
| 12 | database_uuid | 16 bytes |
| 28 | sequence | u64, 1..PROVISIONAL_CSN-1 |
| 36 | observed_at | signed microseconds, i64 |
| 44 | ordered_at | signed microseconds, i64 |
| 52 | metadata_length | u32, 0..65,536 |
| 56 | metadata | exactly metadata_length bytes |
| end-4 | checksum | u32 CRC-32C |

Unknown envelope versions/flag semantics refuse as schema-version mismatch when
the envelope is otherwise intact. Invalid size, CRC, identity, time relationship,
presence/length or supplied expected identity are corruption. Wrong argument types
to the internal codec remain configuration errors. Errors/repr must not print
metadata bytes, keys or content. Record validity is not proof of publication.

## Metadata body v1

The CAP-1A canonical admission representation becomes the nested v1 body: five
bytes `GXCM` + version byte 1, four optional text fields in actor/origin/
correlation_id/reason order, and one attributes map. No trailing data is allowed.

Tags: `n` None, `f` false, `t` true, `i` i64, `d` finite IEEE754 binary64,
`s` u32 UTF-8 byte length + bytes, `a` u32 element count + tagged values,
`m` u32 entry count + tagged string key/value pairs. Map keys are strictly ordered
by UTF-8 bytes; duplicates, invalid UTF-8/surrogates, unknown tags and nonfinite
floats refuse. List/tuple share the array encoding; signed zero is preserved.
The four leading fields admit only None/string; the final root must be a map.

Decoding uses format hard ceilings, not the caller's smaller current write limits:
64 KiB total, 256 entries/map, 1,024 key bytes, 16,384 string bytes, container depth
16 and 4,096 values including the root (keys/leading fields are not values).
Bounds and available bytes are checked before loops, slicing or allocation from
declared counts. A value written with a larger permitted budget must still be
readable under a smaller current write budget. No arbitrary object deserialization.
The decoder returns the same immutable validated value as admission and proves
canonical re-encoding. Python hash is not a cross-process identifier.

## Activation and complete recovery specification required before wiring

The capability will be explicit, one-way and idempotent, never an implicit format
upgrade inside begin. A v1-compatible activation transaction must publish the
required capability/fence before incompatible journal effects. The WAL must also
carry required grammar discrimination so an already-open older writer/recovery
cannot interpret new file targets as repairable corruption. Do not register a new
record type as known until replay can apply or explicitly refuse it safely.
Exact bit/record assignments and page encodings are pending the integration slice.

After activation, the existing commit protocol must incorporate the full record,
directory and tail/head effects as ordinary full-page WAL images. No independently
fsynced sidecar ACK, best-effort audit write or post-commit append is allowed.
Metadata and observed time are admitted/captured before durability; previous
ordered_at comes from the durable journal head under the existing publication
fence. No extra global writer mode or relaxed descriptor proof is introduced.

## Mandatory crash/concurrency matrix for the integrated capability

| Cut / race | Required outcome |
|---|---|
| Invalid admission / clock overflow | No durable effects or identity allocation |
| Activation before/after its WAL barrier and control publication | Old builds refuse before mutation; repeated activation converges, no partial enabled store |
| Reader/writer already open across activation | Revalidate persisted capability, fail closed if unsupported |
| Both OCC boundaries | Original logical interests intact; only freshly materialized physical pages use current durable baseline |
| WAL segment roll after initial planning | Record/directory identity retargets to the same final COMMIT LSN as heap/index/report |
| Partial append / failed WAL barrier | Existing rollback/uncertain-outcome contract; no independently acknowledged journal entry |
| Barrier complete, only some heap/index/journal images applied | Repeated full-image replay yields exactly one entry and matching data, never a new append |
| Data applied, before control publication / ACK | No reader selects a half-applied commit; outcome lookup settles any durable unacknowledged commit |
| N writers, equal/distinct user identities | Unique forward COMMIT IDs; no lost record, timestamps monotone despite clock ties/regression |
| Long readers vs later commits/checkpoint | Snapshot-bounded lookup and consistent descriptor proofs; metadata does not retain writers |
| Checkpoint and WAL recycling | Records/lookups remain complete without the recycled log |
| Corrupt record, directory, fragments, UUID, sequence or advertised coverage | Typed refusal before returning incomplete/foreign metadata |
| Backup/restore/fork and logical import | Qualified source identity or explicit remapping; no writable fork silently reuses future identities |

These are acceptance requirements, not tests already passed. Verify must cover
cross-file ordering/count/identity/time invariants. Metrics must remain bounded and
outside the commit failure boundary. Public exports/metadata begin options wait
for the complete store/replay/lookup vertical slice. No Pulse store is upgraded by
the present record-codec implementation.

## Record-codec checkpoint — 2026-09-08

`domain/txn/commit_catalog.py` now validates/encodes/decodes the envelope and
optional directory identity expectations. `commit_metadata.py` supplies the bounded
nested decoder, returns detached immutable values and checks canonical re-encoding.
The decoder uses the format ceilings so local write limits do not hide valid data.
Unknown versions/flags, even with valid checksums, are not silently accepted.

Final proportional group: **379 passed in 7.82 s**, covering record/admission tests
and the complete pure-core/all-core import gates. The 1,500-iteration mutation corpus
tests 3,000 body/envelope candidates, checks canonical re-encoding on acceptance,
and requires both acceptance and refusal to avoid a vacuous always-refusing parser.
Tests include checksum-valid semantic corruption, foreign directory identity,
truncation, forged counts, container depth/value bounds, exact maximum body/record,
missing versus empty metadata, maintenance kind, mutated input fields and redaction.

The tests were added first and initially failed import because the new decoder did
not exist. The first implemented run exposed test-harness defects: pytest tried to
use a 65 KiB payload as a node identifier, and a fixture treated the catalog's
required_capabilities method as a property. Bounded case IDs and the correct method
call fixed those tests; no production refusal was relaxed.

Strict mypy passes all three provenance domain modules; Ruff and diff-check pass.
No WAL/page/capability registration, migration, public API, live Pulse operation or
extra spec consolidation was performed. This is not a full transactional regression
or the integrated crash matrix above; the latter remains a prerequisite to activation.
