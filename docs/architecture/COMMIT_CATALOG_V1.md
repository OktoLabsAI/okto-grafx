# Commit catalog v1 — CAP-1B persistence contract

Status: record codec, private paged planner/reader, internal activation fence and required WAL framing implemented;
automatic journal publication/replay and public API NOT enabled. Activated test stores
refuse subsequent writing commits until publication is wired. Not deployable as a
public capability. Authority: ADR GX-003 and SPEC-GX-CAP-1.
Date: 2026-09-08.

## Storage and ordering decision

The logical catalog must outlive WAL recycling. It cannot be a scan of the retained
WAL or an in-memory list rewritten on every commit. The selected layout has a
paged append-only record stream and an ordinal directory of fixed-width entries
for binary-search lookup by CommitId. Variable-sized records may span pages; an
empty metadata commit must not reserve the maximum 64 KiB payload. The private page
layout below is implemented in `engine/commit_catalog_store.py`. Journal file targets
remain unregistered. Catalog capability bit 4 now guards the independently persisted
activation horizon; it does not enable automatic journal emission.

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
Catalog required bit 4 (`1 << 4`, `commit_catalog_v1`) is assigned. Required journal
WAL framing is implemented below; automatic journal emission remains pending and
journal targets must not be emitted as legacy effects.

### Internal activation fence implemented

Immutable implementation checkpoint: `3d6ef7f822cbe14293f72ee886f91a1fe365485a`.

Catalog v2 adds one conditional `u64 activation_commit_lsn` immediately after its
16-byte v2 extension (absolute offset 44), before table/space/index bodies. It is
present exactly when required capability bit 4 is set. Range: exact integer
1..PROVISIONAL_CSN-1; the existing whole-catalog CRC covers it. Without the bit,
the existing v1/v2 bytes are unchanged. Unknown required bits are refused before
interpreting this field. Detached copy/round-trip preserve the horizon; reactivation
at the same horizon is idempotent, replacing a published horizon is refused.

The private TransactionManager preparation requires a fresh dedicated write and an
already-v2 identity catalog. It stages catalog pages only. The placeholder is
rebound to the final COMMIT LSN during batch construction and segment-roll sizing;
both catalog body and page stamp change, with unchanged page cardinality. Activation
uses uncompressed v1 WAL even when compression was enabled earlier. It creates no
`commits.dir`/`commits.dat`. Extra work added to the dedicated transaction is refused,
not discarded. Retry before append retains a verifiable rebound plan; rollback,
terminal abort and close discard the plan. After the durability barrier, existing
full-page recovery restores the same horizon without a second COMMIT.

Until automatic publication is integrated, a temporary guard refuses writing on
activated fixtures, including a participant that began before foreign activation.
The guard rechecks current catalog authority under the existing commit fence; it
does not authorize a second-OCC shortcut. This is deliberately NOT a public API or
an instruction to activate production. Non-activated stores retain normal behavior.
Both fresh data and maintenance publication still need journal staging, emission
using the required WAL grammar, coverage checks and replay before this guard can be replaced.

The independent horizon will distinguish a legal empty tracked interval (durable
control still at activation) from missing history after later commits. Missing files
must never silently redefine the beginning of the tracked interval. Initialization
and partial-file crash classification remain part of publication/recovery work.

### Required journal WAL framing implemented

Immutable implementation checkpoint: `0feec917d0ac8d1cb2bfea6543e20cf7de156ddf`.

`WRITE_PAGE`, format_version 2, uses required flag `COMMIT_CATALOG_V1=0x0010`
together with REQUIRED `0x0001`. Exactly three WRITE_PAGE-v2 flag combinations
are understood: ordinary compressed page `0x0005`, raw journal `0x0011`, and
compressed journal `0x0015`. No new record type or global format bump is needed.
Unknown/reserved/skippable mixtures remain upgrade refusals, never skipped writes.

Journal targets are exactly `commits.dir` and `commits.dat`. Raw pages use the
legacy clear target-prefix layout followed by the raw image, but **not** the
legacy envelope. Compressed pages retain the clear prefix, u32 uncompressed length
and bounded single zlib stream. Compression refusal/non-benefit returns raw v2
`0x0011`, never v1. The segment-roll plan likewise must preserve this required
marker; its fixed-width flags do not change image count or raw batch size.

A required journal bit on another target is invalid known semantics (corruption).
A journal target with legacy or ordinary-compression framing is an unsupported
schema/grammar refusal. V1 flags remain opaque for ordinary targets. The generic
raw payload helper is not emission authorization; the envelope-aware encoder owns
the required flags. Common target names now live in `domain/txn/records.py` and
the private store imports them instead of maintaining a second spelling.

The current CommitRedo preflight explicitly refuses journal effects with field
`commit_catalog_replay` before **any** effect in the batch is applied. Knowing the
bytes is not proof of journal coverage. The generic redo target allowlist remains
closed to these two files. This temporary guard is replaced only by complete
cross-file replay validation, not removed merely to make an append test pass.
The transaction activation guard also remains in force. These private codecs
must not be treated as a usable public history feature or deployed to Pulse.

Tests exercise exact flags, round trips with compression on/off/unprofitable,
target/grammar confusion and an old compression-only semantic decoder in the
real WalManager. Old read/append/recycle doors refuse with unchanged WAL bytes.
This is an injected legacy semantic rule, not execution of an old binary. The
required-grammar size-only roll planner still appends no journal WAL; dedicated
framing fixtures append only to isolated in-memory WALs, without a database commit.

After activation, the existing commit protocol must incorporate the full record,
directory and tail/head effects as ordinary full-page WAL images. No independently
fsynced sidecar ACK, best-effort audit write or post-commit append is allowed.
Metadata and observed time are admitted/captured before durability; previous
ordered_at comes from the durable journal head under the existing publication
fence. No extra global writer mode or relaxed descriptor proof is introduced.

## Mandatory crash/concurrency matrix for the integrated capability

### Individual COMMIT boundaries now retained by replay

Immutable implementation checkpoint: `18d53f72442d3620eb84afd1bd61e2326107bfcd`.

The existing selector previously flattened durable effects and retained only
the greatest COMMIT LSN. That is insufficient to prove which transaction owns
each journal entry when replay spans several commits. It now retains the exact
individual COMMIT envelopes in `CommittedReplay.commit_records`, including
effect-free COMMITs, with no additional storage read or record re-encoding.
Startup and checkpoint page/index subplans carry the same tuple.

When boundaries are supplied, preflight validates forward/unique COMMIT sequence,
unique `(epoch, txn_id)`, maximum watermark and effect ownership before decoding
pages. Each selected effect must precede its own terminal; a terminal transaction
cannot also advertise incomplete effects. The private passage-bound proof includes
terminal tuple and record signatures. Page projection cannot substitute/drop the
boundaries while reusing the full proof. Signatures are captured **before** page
decoding and checked afterwards, preventing a codec callback from mutating the
terminals between validation and sealing/application.

Validation is O(C+E), temporary key mapping O(C), over the already selected WAL
range (C COMMITs, E effects), not a scan of all retained logical history. Signature
checks reuse the existing preflight passage policy; no new physical-authority
cache, lock, barrier or I/O door is introduced. This does add in-memory lineage
work; no end-to-end speedup is claimed. Legacy manually composed effect-only
plans retain their prior contract, but cannot certify journal publication.

This is a prerequisite, not complete journal replay. The integration still needs
activation/UUID authority, one journal append per post-activation writing COMMIT,
cross-file count/time/fragment coverage, initialization vs missing-history checks,
normal staging/OCC and idempotent page replay. Both journal integration guards
remain in force until those conditions are implemented and tested.

### Remaining complete-capability acceptance matrix

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

Immutable checkpoint: `817fc8a9020fab12f9483ab7dec6d1411d66afcd`.

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

## Paged image planner and reader — CAP-1B storage checkpoint

Immutable implementation checkpoint: `d5c3ed1aaab9edf1ebd99752fc7cf3d4b6125043`.

Implementation: `engine/commit_catalog_store.py`. This is **not a separate durable
writer**: a callable supplies complete page bytes from a caller-proved stable view;
planning returns detached immutable full-page images and performs no allocation,
write, barrier, lease acquisition or acknowledgement. Reopening through the real
StorageDevice port is tested using test-only materialization, not offered as an
alternative production publication protocol.

### Files and exact page encoding

Selected internal names are `commits.dir` and `commits.dat`; both remain refused by
the current redo-target allowlist. They are not interchangeable with `catalog.dat`.
All pages use the existing checksummed slotted layout, exactly two live slots, even
sequence, zero flags/reserved and NO_PAGE next link. There is no new PageType enum.

Page zero is META. Slot zero is the exact standard FileHeader v1, kind CATALOG,
configured page size, NO_PAGE root and zero chained-payload length. Slot one is a
68-byte little-endian header, struct `<8sHH16sQQQQq>`:

| Offset | Field |
|---|---|
| 0 | 8-byte magic `GXCMHEAD` |
| 8 | u16 version = 1 |
| 10 | u16 role: 1 directory, 2 stream |
| 12 | database UUID, 16 bytes |
| 28 | u64 activation COMMIT sequence; positive and non-provisional |
| 36 | u64 entry_count |
| 44 | u64 stream_bytes |
| 52 | u64 last_sequence |
| 60 | i64 last_ordered_micros |

The directory header describes current coverage. Its empty state has count/bytes
zero, last_sequence equal to activation, ordered time zero. The stream header is
immutable and always has these empty counters: its job is qualified file identity
and matching activation horizon, not a second independently published head.

Pages 1 onward are CATALOG. Slot zero is the 36-byte descriptor
`<8sHH16sQ>`: `GXCMBLK\0`, version 1, role, UUID, logical base. Slot one is the
payload. For page size P, stream capacity C=P−76 bytes; directory capacity
D=floor(C/32) entries. Stream page index is 1+floor(byte_offset/C), and its base is
(index−1)×C. Directory index is 1+floor(ordinal/D), base (index−1)×D. Ordinals are
zero-based and are not CommitIds. Every non-tail block is full; tail length is
determined exactly by the directory head. No linked chains or arbitrary traversal
offsets are trusted from a fragment.

Each directory entry is 32 bytes, `<QQIIq>`: sequence u64, stream offset u64,
record length u32, zero reserved u32, ordered timestamp i64. Entries are contiguous
in stream bytes, strictly increasing in sequence and ordered time. Record length
is 60..65,596; first offset is zero and the final entry matches all head counters.
Records retain their own envelope CRC and qualified identity/time checks after
fragments are joined. Page checksums alone do not validate the nested record.

Append rewrites only the stream tail/new fragments, the directory tail/new block,
and directory head. No 64 KiB reservation for absent metadata; no historical list
copy. At 512-byte pages, the maximum record needs at most 152 stream images plus
one directory image and one head image (the extra stream image covers a used tail).
Address exhaustion while appending is a transaction budget error, not corruption;
an already-persisted impossible extent is corruption. Plans carry neutral page
stamps: existing private staging/WAL must assign final page LSN and sequence stamps.

### Read and verification scope

Exact lookup binary-searches fixed ordinals and filters the qualified identity by
read_lsn. It checks each visited directory page, search ordering bounds, referenced
fragments and envelope. It does not certify all unvisited history: full `verify`
streams every advertised entry, checks cross-page adjacency and decodes every
record using bounded memory. It does not yet inspect unreferenced physical pages,
prove WAL correspondence or check the head against authoritative control state.
Those are mandatory engine-integration checks, not optional trust in this parser.

The surrounding engine must establish and revalidate physical authority around
reads. Lookup's None means no entry in the tracked interval/snapshot, not proof of
nonexistence before activation. The public result must disclose that legacy horizon.
Append-only prefixes allow old read_lsn filtering using a current stable physical
view; a reader must never combine an old captured head with concurrently changing
tail images without the existing drift/proof protocol. No view certificate/cache,
retry policy or concurrency-mode change is introduced here.

Initialization is an image plan only. Activation must prove unused/appropriately
recovered file targets before staging it; it must never blindly overwrite existing
files. Reapplication of a complete image set is byte-idempotent, but this is not
yet a claim of transactional/crash recovery. Future wiring must incorporate these
images in private staging, both OCC passes, WAL segment-roll retargeting, full-image
redo, control publication, required-grammar refusal, backup/restore and verify.

## Prepared append and terminal-LSN binding — CAP-1B

Immutable implementation checkpoint: `2d01f1793b8190afe8a7b2f8c21fb0e41913b337`.

`CommitCatalogStore.prepare_append` now closes the journal-image-count/CommitId
dependency without rereading host storage while retargeting. It requires the exact
current durable COMMIT sequence supplied by the coordinator. A valid catalog head
that does not cover that sequence refuses as `published_coverage`; syntactic validity
alone is insufficient. Metadata/kind/observed-time admission and detachment precede
the first page-provider call. Ordered time is then assigned once from the validated
head; ties/regression advance one microsecond and overflow refuses before effects.

The prepared value holds only immutable bytes for the bounded pages actually read
(the two reserved headers, last directory block and last-record/tail fragments),
the canonical new record, and its image count. `bind(final_sequence)` rebuilds the
record CRC, directory entry, head and fragment images at the terminal COMMIT LSN.
It preserves locations/cardinality/raw page sizes, checks the captured baseline and
does not call the original storage provider or a clock. Metadata/time/kind do not
change when the WAL inserts a segment-header LSN. This is an attempt-local payload
capture, **not a cache/bundle of physical authority** or permission to reuse old
proofs: the coordinator still owes the normal fence, descriptor, staging and OCC
protocols. Discard it when the attempt ends; it cannot authorize another commit.

Integration locations are concrete and remain **unwired** in this checkpoint:

- `_commit_with_writing`: prepare from the current view after any identity-floor
  subcommit; privately stage locations before the physical second OCC. Keep the
  first OCC and every originally pre-staged page on the original snapshot.
- `_build_records`: include the already known journal image count, then bind the
  record to the calculated COMMIT sequence and preserve private staging proofs.
- `_retarget_commit_batch`: rebind journal contents as well as page stamps/index
  effects when the WAL's real raw-batch plan rolls. Do not merely alter page_lsn
  while leaving the envelope/directory/head at the provisional candidate.
- `_commit_identity_floor_plan` also calls those builders: it needs a maintenance
  catalog entry, not an exemption that leaves the head behind durable control.
- Attempt cleanup must discard these staged inputs without carrying them into a
  retry's original-snapshot page set. Budgets include captured/staged payloads and
  the final WAL batch. No metadata serialization may first run after durability.

Activation/recovery must persist the legacy boundary in a way that distinguishes
an activated-but-not-yet-initialized catalog from lost catalog files. It must not
infer an empty history solely from missing files and the latest control LSN. This
is the existing legacy-boundary/non-corruption requirement, not an optional fallback.

Tests cover data/maintenance, small/large metadata, deterministic rebinding,
caller timestamp mutation, stale/future head relative to control, pre-I/O admission,
clock overflow and history-independent capture. The actual WalManager size/roll
planner converges in both roll/no-roll cases without appending catalog records:
the size-only test envelopes are not a supported journal WAL grammar. All production
allowlists, capability bits, activation and public API remained unchanged at that
prepared-binding checkpoint. The later internal activation section above assigns
the catalog bit/horizon without yet registering journal WAL effects.
