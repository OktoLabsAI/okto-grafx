# SPEC-GX-CAP-1 — Commit identity and provenance

Status: domain admission and bounded persistent/public N4 capability implemented and locally validated.
CAP-1B record codec, private paged image planner/reader, internal activation fence and
required WAL framing and native publication/recovery are implemented. Development public
activation, metadata/retry, snapshot lookup/paging, verifier/metrics and coordinated
logical-transfer mapping hooks are connected; [acceptance evidence and limits](../reports/V005_N3_N4_ACCEPTANCE.md)
record the full regression and corrective reruns. No release/platform certification is implied.
See the [consumer contract](../COMMIT_HISTORY.md). Historical private-only statuses below
describe their original checkpoints, not the current development facade.
Date: 2026-09-08. Current integration: feature/v0.0.5; historical checkpoints below retain their original branches.
Dependencies: M1 typed API; GX-CAP-0.

## Normative scope

[Full complementary requirements](../archive/ROADMAP_SOURCES.md#source-grafx-complementary-evolution-plan-codex), §6; §17 GX-CAP-1.
[Full agent requirements](../archive/ROADMAP_SOURCES.md#source-agent-first-evolution-plan-codex) remains incorporated
in full under the [roadmap](../../ROADMAP.md) governance rule.
All requirements, proposed interfaces, gates, non-goals and detailed subphases in
those source sections apply; this routing specification does not replace them.

Deliverables: CommitId; bounded immutable CommitMetadata; commit lookup/catalog; replay-idempotent recovery; typed views; metrics and verification; coordinated logical export/import hooks.

## Contract and implementation boundary

Binding decision: [ADR-GX-003-COMMIT-IDENTITY.md](../architecture/ADR-GX-003-COMMIT-IDENTITY.md).
Planned owning modules: Domain commit DTO/codec; engine transaction/recovery/catalog orchestration; runtime clock/identity ports; public API/verify; logical transfer hooks.
Public interfaces are typed before any new Cypher syntax. Proposed roadmap examples
are not advertised as implemented APIs. Existing multi-reader/writer, OCC, snapshot,
WAL, durability, fail-closed and bounded-resource contracts remain in force.

Error taxonomy to specify as public typed outcomes before implementation:
commit identity/type/range, metadata type/byte/key bounds, outcome ambiguity, missing identity, corrupt catalog, unsupported capability. Names/fields must be reconciled with existing public errors, not added as
generic catch-all wrappers. Cancellation and uncertain durable outcomes stay distinct.

## Persistence and recovery prerequisite

No byte format is frozen by this routing spec. Before a persisted implementation,
its milestone must define exact catalog capability, bytes/checksums/bounds, all WAL
effects, pre/post durability and ACK crash cuts, replay idempotence, upgrade/refusal,
backup/export/import and old-reader behavior. Non-persisting APIs document that they
have no durable effects. Optional consumers reuse public transactions, never add WAL
records for product-specific entities. See [manifest strategy](CAPABILITY_MANIFEST_STRATEGY.md).

## Acceptance and traceability

Prove multiprocess ordering, metadata atomicity, clock tie/regression handling, all new crash cuts, full regression and mutation tests. Source §6 requirements all remain mandatory.

Cross-cutting matrices in complementary §§19–21 and the applicable agent-first
sections remain binding. Tests must include failing-before implementation cases and
hostile inputs, not only happy paths. Publish operation counts and resource evidence
when claiming complexity/performance improvements; no new marginal timing threshold.

Execution status: **not implemented by GX-CAP-0**. No release, new on-disk capability,
installed Pulse feature, or successful crash matrix is inferred from this document.
Record immutable code SHA, test commands/results, audit and unresolved debt here when
implemented. Next prerequisite is the first unmet dependency, not another scope expansion.

## CAP-1A — Pure identity, metadata admission and logical time

This first implementation slice adds no exported public facade, begin parameter,
WAL record, format flag or durable effect. Domain modules are internal until the
complete capability is usable. Scope: `domain/txn/commit_identity.py` and
`domain/txn/commit_metadata.py`, with dedicated domain tests.

Current evidence: `CommitReport.csn` is the COMMIT record LSN; `_build_records`
predicts it and `_retarget_commit_batch` accounts for a segment-roll header before
append. `_commit_without_writing` returns the snapshot CSN without creating a new
commit. Therefore only a durable writing outcome can receive a newly published
CommitId. The eventual LSN mapping still requires persistent lookup, retention,
restore/fork and crash proofs; this slice does not certify it from a type alias.

CommitId is a value reference `(database_uuid: bytes[16], sequence: int)` with
`0 < sequence < PROVISIONAL_CSN`. Bool/subclass coercions are refused. Equality is
qualified by store; ordering different stores refuses instead of sorting UUIDs.
The canonical external token is 32 lowercase UUID hex digits, colon, 16 lowercase
sequence hex digits. Parsing validates exact spelling and reserved ranges.
Constructing an identity is not proof that the referenced commit exists.

Logical time uses existing signed-microsecond Timestamp values. Under the future
serialized publication boundary, `ordered_at=max(observed_at, previous+1us)`;
the first commit uses observed_at. Clock ties/regression set clock_adjusted. Signed
overflow refuses before publication; time is never sampled or used for leases by
this pure module. The persisted previous timestamp is still an integration task.

CommitMetadata admits optional actor/origin/correlation_id/reason strings and an
attributes tree of exact built-in dict/list/tuple/string/int64/finite-float/bool/None.
Lists become immutable tuples; dicts become private immutable mappings with
canonical UTF-8 key order. Unsupported objects are refused without invoking their
conversion/repr hooks. All input data are copied/normalized before later use.
Metadata equality/hash use canonical admission bytes, so bool/int and signed zero
are not silently conflated. Constructor and repr/errors do not disclose payloads.
Python hash remains process-local; it is not a persisted idempotency fingerprint.
Caller mutation after admission cannot change the value; callers must not mutate
input trees concurrently with their admission if they require one coherent source
snapshot. Limits are copied and revalidated on entry, not trusted by object type.

MetadataLimits are immutable and caller-configurable within hard admission bounds:

| Limit | Default | Allowed range |
|---|---:|---:|
| max_bytes (complete canonical admission encoding) | 16,384 | 14..65,536 |
| max_attributes (entries in any map) | 64 | 0..256 |
| max_key_bytes (UTF-8) | 128 | 1..1,024 |
| max_string_bytes (field or value, UTF-8) | 4,096 | 1..16,384 |
| max_depth (container depth; attributes root map is zero) | 4 | 0..16 |
| max_values (attributes containers/scalars, including root; not keys) | 256 | 1..4,096 |

The internal canonical admission representation is tagged and length-delimited,
with a five-byte discriminator, four optional fields, then the attributes map.
It permits deterministic byte-budget accounting without calling an external
serializer. At CAP-1A this was not a frozen on-disk format or WAL extension. The
subsequent CAP-1B [record/recovery contract](../architecture/COMMIT_CATALOG_V1.md)
selects it as the nested metadata v1 body and adds a strict bounded decoder. No
paged store, WAL grammar or activation is enabled before complete replay support.

Invalid types/encoding/shape/identity/time/limits use GrafxConfigurationError;
exhaustion of metadata admission budgets uses
GrafxTransactionBudgetExceeded with bounded field/limit/observed diagnostics.
Neither errors nor repr includes user-supplied key/content text. Identity mismatch
is not a write conflict and is not automatically retryable. Resource admission
performs no I/O and is independent of a transaction object.
Shallow type/size guards precede allocation and deeper validation: exhausting a
budget does not certify that the unvisited suffix would otherwise be valid.

Required slice tests: limits and exact boundary bytes, deep/nested/cyclic input,
caller mutation, unsupported subclasses/hooks, ordering/canonical hashes,
Unicode/surrogates, int64/float edge cases, cross-store comparison, token parsing,
clock tie/regression/overflow, no new public export, and the existing pure/import
gates. The remaining CAP-1 gate still requires all persisted integration, crash,
multiprocess, lookup/verify, metrics and logical-transfer work listed above.

### CAP-1A execution evidence — 2026-09-08

Immutable implementation checkpoint: `474335962eeae20a937d6a444bcc3f7b2c2f110f`.

The tests were written first and the initial run refused collection because the
new identity module did not exist. After implementation, the dedicated slice is
**87 passed in 0.23 s**. The all-transaction/public-output/import-boundary group
is **985 passed in 114.31 s**, using:

```text
python -m pytest tests/txn tests/test_import_boundary.py
  tests/test_optional_package_boundary.py tests/api/test_public_operation_outputs.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

Strict mypy (`--strict --follow-imports=silent`) passes both new domain modules;
Ruff passes both modules and their tests; diff-check is clean. Earlier 312-test
admission/import results overlap this final group and are not an additional total.

Adversarial follow-ups before the grouped run: reject oversized keys before sorting;
prove a too-large sequence does not reach the copying hook with a working small-
input sentinel; revalidate a host-mutated limits object; reject inconsistent time
coordinates; check signed-zero/type distinctions and exact length-delimited bytes;
refuse surrogates, deep/cyclic containers and hostile conversion/iteration hooks.
These tests certify pure admission only. Existing transaction/crash tests remain
green, but no new metadata publication crash matrix has been exercised because
this slice has no persistent effects. No Pulse operation, extra spec consolidation,
deployment, benchmark speedup claim, main merge or release occurred.

### Next required vertical slice (CAP-1B), not optional debt

Specify and implement the durable commit catalog and activation/recovery protocol,
then connect admission before begin/publication. Prove the current COMMIT-LSN mapping
across roll, gap completion, checkpoint/recycle, old stores and restore/fork; no-write
transactions must not fabricate new identities. Define how internal maintenance
commits are represented and how the legacy-history boundary is exposed. Metadata
objects remain caller-owned values, not physical authority or proof of admission
at a later public boundary. Publication must validate its own immutable captured
representation, not trust a Python class name or a mutable host field.

Only after the persistent catalog/replay/lookup is usable may the facade export
CommitId/CommitMetadata and accept metadata at begin. CAP-1C still owes metrics,
verify, logical transfer and the complete concurrent/crash/release conformance.
The full GX-CAP-1 remains **in progress**, not complete.

### CAP-1B record codec checkpoint — 2026-09-08

Immutable implementation checkpoint: `817fc8a9020fab12f9483ab7dec6d1411d66afcd`.

Implemented typed CommitCatalogEntry/CommitKind, checksummed record encoding,
bounded nested metadata decoding, expected store/sequence validation and explicit
unknown-format refusal. The format contract and required crash matrix are in
[COMMIT_CATALOG_V1](../architecture/COMMIT_CATALOG_V1.md). They distinguish a
verified record value from evidence of durable publication.

Final command:

```text
python -m pytest tests/txn/test_commit_catalog_record.py
  tests/txn/test_commit_provenance_values.py tests/test_import_boundary.py
  tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**379 passed in 7.82 s**, including 3,000 bounded mutated inputs and the maximum
65,596-byte record. Strict mypy passes identity/metadata/catalog modules; Ruff/diff
pass. This extends CAP-1A's admission contract; it is not a rerun or replacement of
that checkpoint's 985-test transaction group. Paged directory/record storage,
required-capability/WAL discrimination, commit/recovery integration, lookup/verify,
metrics and transfer are still required. No public export, live mutation or deploy.

### CAP-1B paged image planning/read checkpoint — 2026-09-08

Immutable implementation checkpoint: `d5c3ed1aaab9edf1ebd99752fc7cf3d4b6125043`.

Implemented `engine/commit_catalog_store.py`: arithmetic-addressed directory and
variable-length record stream; bounded append image plans; exact snapshot-filtered
binary lookup; full advertised-history verifier with bounded memory. The exact
headers/slots/bounds are in [COMMIT_CATALOG_V1](../architecture/COMMIT_CATALOG_V1.md).
Initialization/append are private image plans, not writes or independent durable
acknowledgements. Existing WAL target/capability allowlists are deliberately unchanged.

The tests were added first and failed import before implementation. The specific
suite is **59 passed in 6.48 s**. It checks four supported page sizes, absent and
maximum metadata, cross-page records, zero mutation while planning, detached input,
prefix preservation, qualified snapshots, real-device close/reopen, repeated image
application, missing images at every position of a large append, and CRC-valid
header/directory/fragment corruption. Unknown page versions refuse as schema mismatch.
Address exhaustion in a healthy append is a budget error; impossible persisted
coverage remains corruption. An erased mandatory slot is also corruption, unlike
an ordinary freed slot in the general-purpose slotted-page API.

Operation evidence: in the 1,000-commit/512-byte fixture, each small append produces
at most 4 full-page images and at most 8 page reads. Exact seek uses at most
ceil(log2(1000))+8 reads. These are executable operation-count bounds, not throughput
or live Pulse speedup claims. Maximum record size is 65,596 bytes; no maximum-size
allocation is charged to an absent-metadata record.

Final grouped command:

```text
python -m pytest tests/txn/test_commit_catalog_store.py
  tests/txn/test_commit_catalog_record.py tests/txn/test_commit_provenance_values.py
  tests/storage_core/test_page_layout.py tests/storage_core/test_slotted_page.py
  tests/storage_core/test_page_codec.py tests/index/test_ordered_format_discrimination.py
  tests/test_import_boundary.py tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**606 passed in 14.79 s**; strict mypy passes the new store and its tests; Ruff and
diff-check pass. The first broader group found an existing stale enum expectation:
ordered pages 9/10/11 were introduced by `f3a24ee6fcfc987bdf91d39d96ac4c8e1605b2bc`,
but the general page-layout test/contract table still stopped at 8. Both now name
the actual exact values; no new page type or weakened parser was introduced.

Full GX-CAP-1 remains incomplete. Next work is transaction/private staging, final
COMMIT-LSN retargeting, required-format discrimination, replay/control publication
and physically proved lookup. The integrated crash/multiprocess matrix, old-reader
refusal, restore/import identities, metrics and public facade remain required.
The page planner's replay test is byte-idempotence, **not** that integrated gate.
Pulse was not restarted/upgraded; the 20 reserved pending specs were not consumed.

### CAP-1B prepared terminal-LSN binding — 2026-09-08

Immutable implementation checkpoint: `2d01f1793b8190afe8a7b2f8c21fb0e41913b337`.

`prepare_append` now admits/captures metadata and observed time before storage
reads, requires head coverage equal to the coordinator's durable sequence, computes
ordered time once and returns a bounded attempt-local prepared value. Its `bind`
rebuilds the envelope, directory/head and fragment images at the final COMMIT LSN
without host reads or resampling time. Fixed-width sequences preserve image count,
locations and raw encoded size. Both data and maintenance records are tested.

The real WalManager planner is exercised with size-only envelopes for roll and
no-roll: a second preview after rebinding returns the same terminal, and the WAL
bytes/tail remain unchanged. These tests deliberately do **not** append unsupported
journal effects. Runtime staging, required grammar and recovery are still unwired;
the exact call sites/cleanup obligations are listed in
[COMMIT_CATALOG_V1](../architecture/COMMIT_CATALOG_V1.md).

Final grouped command:

```text
python -m pytest tests/txn/test_commit_catalog_store.py
  tests/txn/test_commit_catalog_record.py tests/txn/test_commit_provenance_values.py
  tests/txn/test_page_stamp_plan.py tests/wal/test_commit_catalog_batch_sizing.py
  tests/wal/test_wal_batch_planning.py tests/test_import_boundary.py
  tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**471 passed in 15.72 s**; strict mypy passes the store and both affected test modules;
Ruff/diff-check pass. The store-specific suite is now 71 tests (including its previous
59), with two additional real-planner cases. This group overlaps previous checkpoints
and is not a full engine regression or the outstanding integrated crash matrix.
No Pulse operation, additional consolidation, deployment, release or change to the
multi-reader/writer/OCC/WAL durability protocol occurred.

### CAP-1B internal activation horizon — 2026-09-08

Immutable implementation checkpoint: `3d6ef7f822cbe14293f72ee886f91a1fe365485a`.

Implemented the one-way required catalog bit and conditional activation LSN, plus
a dedicated private activation transaction using the normal commit protocol. The
catalog body is rebound to the final COMMIT LSN including segment roll; activation
always emits legacy v1 WAL, even with compression previously enabled. It creates
no journal files. Rollback/terminal cleanup discard its plan; pre-append retry keeps
the plan consistent with rebound staged bytes. A post-barrier apply failure recovers
exactly the original activation COMMIT and horizon on reopen.

This is an internal prerequisite, **not complete GX-CAP-1**. A temporary guard
refuses subsequent writing commits on activated fixtures until automatic journal
publication/replay is connected. It also refuses an already-open foreign writer
after current authority adoption. Non-activated databases remain writable. There
is no public activation method, metadata begin option or installed Pulse feature.

Grouped regression command:

```text
python -m pytest tests/txn tests/storage_core/test_catalog_v2.py
  tests/storage_core/test_catalog_store.py tests/storage_core/test_catalog_copy.py
  tests/storage_core/test_catalog.py tests/api/test_wal_page_compression.py
  tests/recovery/test_catalog_commit_state_coactivation.py
  tests/test_import_boundary.py tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**1,195 passed in 118.97 s**. Review then found a duplicate cleanup in `_forget`
and a missing one in terminal context abort. A new test failed before that fix;
activation plus terminal-close suites then passed **50 tests in 3.43 s**. The new
activation suite contains 19 cases. These results overlap, not 1,245 distinct tests.

Earlier failing integration tests drove fixes for stale foreign capability state,
legacy physical-only transactions' pre-barrier catalog reads, and rebound-plan
validation on pre-append retry. Record corruption helpers now pass immutable bytes
to CRC, respecting both native and pure implementations. No production CRC change.
Ruff/diff-check pass. Strict mypy passes the new activation test with local `src`
on MYPYPATH; Catalog/TransactionManager retain 49 existing errors, with identical
normalized diagnostics versus HEAD `aac08b9` using mypy shadow files. This is not
a claim of a type-clean engine.

The old-reader case tests the old known-capability mask's refusal ordering, not an
old executable or full multiprocess crash matrix. Remaining mandatory work: initial
empty journal vs missing-history classification; required WAL grammar; journal
staging before second OCC, including maintenance; full redo/coverage validation;
public lookup and legacy boundary; metrics/verify, transfer/restore/fork and the
complete concurrency/crash gates. Existing OCC, WAL/durability and multi-reader/
writer premises were not relaxed. No Pulse operation or extra spec was consumed.

### CAP-1B required journal WAL grammar — 2026-09-08

Immutable implementation checkpoint: `0feec917d0ac8d1cb2bfea6543e20cf7de156ddf`.

Implemented required flag `COMMIT_CATALOG_V1=0x0010`: raw journal pages use exact
v2 flags `0x0011`, compressed journal pages use `0x0015`. Existing compressed
ordinary pages retain `0x0005`. The journal marker survives unprofitable/disabled
compression and the raw segment-roll sizing path. The file names are fixed and
centrally defined. Journal/ordinary-target confusion refuses with typed errors;
ordinary v1 flags do not acquire retrospective meaning.

Decoding the grammar is not replay authorization. CommitRedo explicitly refuses
journal effects during full preflight, before even an earlier valid heap image
can move. Native redo target allowlists and automatic writing remain closed until
cross-file coverage/replay/staging are implemented. No user-facing activation.

The tests initially failed collection because the new grammar constants did not
exist. Focused framing/planner/redo suites: **144 passed in 0.52 s**. Final group:

```text
python -m pytest tests/wal tests/recovery/test_commit_redo.py
  tests/recovery/test_recovery_manager.py
  tests/recovery/test_catalog_commit_state_coactivation.py
  tests/txn/test_commit_catalog_activation.py tests/txn/test_commit_catalog_store.py
  tests/api/test_wal_page_compression.py tests/test_import_boundary.py
  tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**894 passed in 17.63 s** (overlapping, not added to 144). The framing suite has
95 cases, including all flag values 0..63 and the old decoder rule on real
WalManager read/append/recycle doors with unchanged log bytes. Four new dispatcher
tests cover both journal files with/without compression and a valid ordinary
prefix, proving refusal before mutation. The two real size/roll-planner cases
now use required v2 envelopes; they still perform no journal append.

Ruff and diff-check pass. Strict mypy diagnostics on the six checked modules are
identical with/without the change: four preexisting errors, three in imported
Catalog and one in CommitRedo's error-details forwarding. Comparison uses HEAD
`b032fef` shadow source for CommitRedo; no new type errors are concealed. This is
not a type-clean whole-engine claim. Old-binary execution, publication/crash and
complete concurrent history guarantees remain outstanding; injected legacy
semantics and replay refusal are prerequisites, not substitutes for those gates.
Pulse was not changed and no reserved spec was consumed.

### CAP-1B replay COMMIT boundaries — 2026-09-08

Immutable implementation checkpoint: `18d53f72442d3620eb84afd1bd61e2326107bfcd`.

The selector now retains individual COMMIT envelopes (not just the maximum LSN)
and both native startup recovery/checkpoint splits preserve them. Preflight
validates transaction ownership, forward/unique boundaries, complete/incomplete
classification and the advertised watermark before page decoding. The existing
private proof and its page projection bind the exact terminal tuple/signatures.
This supplies the missing per-transaction lineage for later journal validation;
it does not enable journal replay or remove its fail-closed integration guard.

Four initial pure selector tests failed because the field was absent. New tests
cover interleaved commits, epoch-qualified identities, empty COMMITs, malformed
boundary sequences/owners, tuple/signature replacement and projection preservation.
Review also found a callback cut: the terminal signature was first captured after
page decoding. Four tests reproduced acceptance/application after that mutation.
The fix captures first and checks again before sealing/applying; six final cases
cover both doors and changed epoch, payload and invalid payload type.

Final grouped command:

```text
python -m pytest tests/recovery tests/txn/test_catalog_commit_state_fence.py
  tests/api/test_checkpoint_and_reclamation.py tests/api/test_wal_page_compression.py
  tests/test_import_boundary.py tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**826 passed in 37.45 s**, including the existing recovery crash matrix. This
supersedes the earlier 812-test run; focused selector/dispatcher suites now contain
82 tests and overlap the group. The existing crash suite is not the unimplemented
journal publication crash matrix. Ruff/diff-check pass. Strict mypy comparison of
the selector/dispatcher against HEAD `d75f9dc` shadow sources gives the same four
preexisting errors (three imported Catalog, one CommitRedo details forwarding),
with no new diagnostics. It is not a type-clean full engine claim.

No persisted format or transaction ordering was changed by this slice. Additional
memory/validation is linear in the already selected replay range, with no new host
I/O or full commit-history scan. Both OCCs, durability and multi-reader/writer
premises remain intact. Automatic journal publication/replay, public APIs and all
remaining CAP-1 deliverables are still required. Pulse and reserved specs untouched.

### CAP-1B append transition validator — 2026-09-08

Immutable implementation checkpoint: `19633b93d102d7a8d12c37147a8eea3e98d9a939`.

Added `CommitCatalogStore.validate_append_images`: bounded/read-only validation
of an exact stamped image set against an independently supplied predecessor view.
It verifies previous/current/activation coordinates, one-entry advancement,
directory/stream extents and exact affected locations, reconstructed record/UUID/
sequence/time, and unchanged historical prefixes through canonical re-planning.
Duplicate, missing, extra, foreign-target or incorrectly stamped images refuse.
It does not return an apply permit, activate files or reconstruct a crash-cut view.

The first executable test failed because the method was absent. The suite also
reproduced acceptance of a monotonic but incorrectly large logical-clock jump;
the validator now enforces the exact assigned-clock rule, with tie/regression
and maintenance cases. CRC-valid old-prefix rewrites are refused even if the new
record itself is valid. The exact 65,596-byte record is exercised after a split
tail and after another maximum record. Existing internal file-name reexports are
now explicit for strict type consumers; the strings/format did not change.

Focused suite: **42 passed in 3.94 s**. Final group:

```text
python -m pytest tests/txn/test_commit_catalog_transition.py
  tests/txn/test_commit_catalog_store.py tests/txn/test_commit_catalog_record.py
  tests/txn/test_commit_provenance_values.py tests/txn/test_commit_catalog_activation.py
  tests/wal/test_commit_catalog_wal_grammar.py tests/wal/test_commit_catalog_batch_sizing.py
  tests/recovery/test_commit_redo.py tests/recovery/test_replay_decision.py
  tests/test_import_boundary.py tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**691 passed in 21.47 s**; overlap is not an additional test total. Ruff/diff-check
pass. Strict mypy on the store and new tests reports only the four existing
imported diagnostics (Catalog and CommitRedo); the store-only comparison against
HEAD `1cf6d29` shadow source has identical diagnostics. No claim of a type-clean
whole engine. Read bounds and precise validator contract are in
[COMMIT_CATALOG_V1](../architecture/COMMIT_CATALOG_V1.md).

This validator requires an intact predecessor view. It explicitly refuses to
reinterpret an already applied head as that predecessor. Full startup/gap replay
still needs safe predecessor reconstruction or an equivalent fully specified
cross-cut protocol, plus initialization/missing-history classification and normal
staging/OCC/publication wiring. Those are existing acceptance requirements, not
waived by this read-only validator. Journal writing/replay guards remain active;
public history, metrics/verify, transfer/restore/fork and the complete crash matrix
remain mandatory. No Pulse operation or additional spec consolidation occurred.

### CAP-1B durable after-image/range validator — 2026-09-08

Immutable implementation checkpoint: `aa1b40eff15c3ca26e19602758f3cadbbefd5926`.

Implemented bounded semantic redo validation without assuming an intact mutable
predecessor tail. A complete durable journal image set overlays all covered
targets; immutable older blocks remain physically checked. First append must
carry the immutable stream header and name activation as its predecessor; later
missing headers cannot bootstrap empty history. The selected-range validator
retains epoch-qualified COMMIT ownership and uses earlier validated WAL images as
the next append's independent predecessor. Gaps, missing effects and coherent
CRC-valid historical prefix rewrites between COMMITs refuse without applying.

The first crash-cut reconstruction inherits the durable WAL's prefix authority;
it is not proof of an unavailable preimage. Live publication still requires the
independent append validator. No authority cache, new persisted format, host-file
application, native replay allowlist change or writer-guard removal occurred.
Detailed proof obligations: [COMMIT_CATALOG_V1](../architecture/COMMIT_CATALOG_V1.md).

The transition suite grew from 42 to **69 tests**. New tests cover all 48 small
page-application subsets, repeated with covered targets torn and repeat validation,
complete images even when disk has already applied them, immutable missing/future
blocks, exact maximum record, raw/compressed multi-COMMIT ranges, reused txn IDs
across epochs, checkpoint/activation cuts, dropped middle COMMITs and coherent
prefix rewrites. Review exposed two failing cases: excess image counts were not
admitted until after decoding, and an empty replay accepted bool as watermark.
Both are corrected; image cardinality now refuses before any page decompression.

Focused suite: **69 passed in 5.83 s**. Final grouped command:

```text
python -m pytest tests/txn/test_commit_catalog_transition.py
  tests/txn/test_commit_catalog_store.py tests/txn/test_commit_catalog_record.py
  tests/txn/test_commit_provenance_values.py tests/txn/test_commit_catalog_activation.py
  tests/wal/test_commit_catalog_wal_grammar.py tests/wal/test_commit_catalog_batch_sizing.py
  tests/recovery/test_commit_redo.py tests/recovery/test_replay_decision.py
  tests/test_import_boundary.py tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**718 passed in 22.77 s**, overlapping the focused suite. Ruff/diff-check pass.
Strict mypy on store/tests reports the same four existing imported diagnostics:
three Catalog and one CommitRedo. Store-only comparison using HEAD `834d6f3`
shadow source gives identical diagnostics before/after; no whole-engine type-clean
claim. The batch fixtures are semantic records, not proof of native WAL crash
recovery or automatic history publication.

Remaining: native control/catalog/physical coverage proof, staging and publication,
prepared-proof integration, lookup/public metadata, verify/metrics, transfer,
restore/fork, concurrency and complete journal crash regression. No Pulse operation
occurred in this implementation checkpoint. The separately authorized real-spec
run is recorded in `PULSE_SINGLE_SPEC_LATEST_PERFORMANCE_0_0_4.md`; **19 pending
Specs remain reserved** and were not used by these tests.

### CAP-1B complete schema-catalog native preflight — 2026-09-08

Immutable implementation checkpoint: `f1534b0490a3466a6a4ca4af541975f5f54b1f0a`.

Native redo now checks complete `catalog.dat` snapshots per owning COMMIT before
any page apply, using only WAL after-images. It validates header/chain/released
pages, exact payload coverage and stamps, then one-way activation continuity.
Both standalone preflight and uncached apply use the check; existing proof-bound
recovery/checkpoint projections retain it. No current physical catalog is adopted
or consulted as a substitute for incomplete WAL coverage. Detailed contract and
remaining authority limits: [COMMIT_CATALOG_V1](../architecture/COMMIT_CATALOG_V1.md).

Tests added: **26** parser/dispatcher cases and **12** real WalManager/fenced
RecoveryManager cases. They include multi-page/shrinking catalogs, seven malformed
image sets at both entry points before apply, valid legacy/activation/later-schema
ranges, horizon removal/retargeting/future refusal, and idempotent torn-header repair.
Real native tests cover all four application subsets for the two-page activation,
with applied pages intact or torn, durable control publication, repeat recovery,
and byte-identical refusal for missing WAL header/chain even on fully applied disk.

The first group found three failures sharing an old fixture that called `save()`
before WAL and omitted the header from its record set. It also guessed terminal
LSN without the segment-header slot. The fixture now uses detached `stage()`, the
real raw-batch planner and expected-terminal append, and asserts the physical
catalog remains empty before recovery. Its adoption/save/missing-store assertions
remain intact; production checks were not weakened. A transient fixture-only
FrozenInstanceError was corrected by replacing immutable WalRecords.

Grouped validation:

```text
python -m pytest tests/recovery
  tests/storage_core/test_catalog_replay_images.py
  tests/storage_core/test_catalog_store.py tests/storage_core/test_catalog_v2.py
  tests/txn/test_commit_catalog_transition.py tests/txn/test_commit_catalog_store.py
  tests/txn/test_commit_catalog_record.py tests/txn/test_commit_provenance_values.py
  tests/txn/test_commit_catalog_activation.py
  tests/wal/test_commit_catalog_wal_grammar.py tests/wal/test_commit_catalog_batch_sizing.py
  tests/api/test_m0b_catalog_repair_from_log.py tests/api/test_checkpoint_and_reclamation.py
  tests/test_import_boundary.py tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**1,347 passed in 50.30 s**. After removing redundant fixture preview stamping,
the recovery-manager and both new suites passed **117 tests in 1.02 s** (overlap,
not an additional group total). Ruff/diff-check pass. Strict mypy on both changed
production modules and both new suites reports nine existing diagnostics: three
Catalog, five CatalogStore bootstrap typing, one CommitRedo error-details forwarding.
Using `--shadow-file` with HEAD `413d8d8` for both production modules reproduces
the same nine diagnostics with only line offsets changed. No type-clean engine
claim; the expanded check scope includes five previously unreported baseline errors.

Still required: activation relative to independent durable control and physical
coverage, journal staging/publication and guarded native application, public APIs,
metrics/verify, transfer/restore/fork and complete concurrency/crash regression.
No journal-target allowlist or writer guard was removed. Both OCCs, multi-reader/
writer semantics and durability remain unchanged. Pulse was not restarted or
modified; **19 pending specs remain reserved**. Full CAP-1 is not complete.

### CAP-1B native checkpoint anchor — 2026-09-08

Immutable implementation checkpoint: `1932d3519518854921ac0a8b62ec9a9f3c12d6d9`.

Recovery/checkpoint now carry their validated checkpoint into `CommitRedo` and
its private full/projected proofs. First schema activation after that checkpoint
must be established by its own COMMIT snapshot, not retrospectively claimed by
a later snapshot. A checkpoint mismatch invalidates proof reuse and any fallback
revalidates against the current caller's floor. Admission precedes page decoding.
No new physical I/O, authority cache, WAL/OCC ordering or publication was added.
See [native checkpoint anchoring](../architecture/COMMIT_CATALOG_V1.md).

Added 20 tests: six missing-activation-snapshot cases across standalone doors,
two checkpointed-activation cases without old WAL, five invalid/overlapping floor
admissions before decoding, four mismatched-proof entry points, two real native
recovery refusals (old/fully applied disk, all bytes preserved), and a public
checkpoint/reopen test after actual internal activation. The public test observes
the old floor for nonempty replay and the new floor for the subsequent empty
preflight. Initial fixture mistakes (state-store attribute, assuming only one
preflight) were corrected without changing production checks.

Grouped command:

```text
python -m pytest tests/recovery tests/storage_core/test_catalog_replay_images.py
  tests/txn/test_commit_catalog_activation.py tests/txn/test_commit_catalog_transition.py
  tests/api/test_checkpoint_and_reclamation.py tests/api/test_checkpoint_watermark_scope.py
  tests/api/test_checkpoint_meta_root_proof.py tests/api/test_m0b_checkpoint_lineage.py
  tests/api/test_m0b_catalog_repair_from_log.py tests/api/test_auto_checkpoint.py
  tests/api/test_recovery_floor_photo_reuse.py
  tests/test_import_boundary.py tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**1,012 passed in 57.18 s**. After the final apply-path admission placement and
public-checkpoint test, activation/catalog-image/native-catalog/CommitRedo suites:
**144 passed in 2.54 s**, overlapping the group. Ruff/diff-check pass. Expanded
strict mypy on CommitRedo/RecoveryManager/TransactionManager reports **69 errors**;
`--shadow-file` comparison with HEAD `79641ec` yields the identical 69 diagnostics
after line-number normalization. No newly introduced errors; no type-clean claim.
The three changed test modules have only eight imported existing Catalog/Store
diagnostics, with none in the tests themselves.

Next integration obligations remain finite and unchanged: independently current
catalog for no-schema ranges, journal physical/UUID/coverage checks, guarded native
application and automatic staging/publication, then public APIs and the remaining
verify/metrics/transfer/restore/fork/concurrency/crash requirements. The journal
allowlist and writer guards remain closed. Pulse and 19 reserved specs untouched.

### CAP-1B no-schema native replay and strict index subplans — 2026-09-08

Implementation checkpoint: `23d7acada70a45342fd3cbb7196f74266a98b9f4`.

Native ranges without schema effects now inspect current canonical catalog pages,
without adopting or trusting the caller's mutable catalog. Missing activation
coverage and orphan history files refuse. A durable post-activation COMMIT lacking
supported history cannot be published merely because its WAL suffix has no DDL.
An empty interval exactly at activation remains allowed. The guard still refuses
unsupported post-activation history; actual journal validation/application is not
enabled by this slice.

The first integration run exposed index-only replay misclassified as a standalone
range without the activation schema already applied by its sibling. Implemented
exact index-subplan proof inheritance, with strict index lookup/generation
preflight after catalog adoption. It does not inherit the former temporary
unregistered-index allowance. Five new adversarial tests then reproduced callback
substitution of full/subplan effects, COMMIT tuples or watermarks; post-callback
identity/signature revalidation closes all five before index dispatch.

Added **26 tests**: twelve persisted/no-schema/orphan-catalog cases, thirteen
index-subplan identity/strictness/callback cases and one real-WAL refusal proving
that an unacknowledged subsequent COMMIT cannot advance control without history.
The first grouped run had two failures in older heap/ABORT fixtures that discarded
their unflushed initial catalog bootstrap. Those fixtures now persist that baseline
before the tested WAL operation; recovery does not turn a pristine missing header
into an authoritative legacy catalog. Existing assertions remain. Focused closure:
**249 passed in 3.21 s**.

Final grouped command:

```text
python -m pytest tests/recovery tests/storage_core/test_catalog_replay_images.py
  tests/txn/test_commit_catalog_activation.py tests/txn/test_commit_catalog_transition.py
  tests/api/test_checkpoint_and_reclamation.py tests/api/test_checkpoint_watermark_scope.py
  tests/api/test_checkpoint_meta_root_proof.py tests/api/test_m0b_checkpoint_lineage.py
  tests/api/test_m0b_catalog_repair_from_log.py tests/api/test_auto_checkpoint.py
  tests/api/test_recovery_floor_photo_reuse.py
  tests/test_import_boundary.py tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**1,039 passed in 54.03 s**; focused totals overlap. Ruff/diff-check pass. Strict
mypy on the three production modules retains the same **69** diagnostics against
HEAD `b1ade66` shadow sources. The three exercised test modules retain **18**
existing/imported diagnostics against the old dispatcher test source; one newly
introduced tuple-arity annotation was corrected. Comparisons normalize only line
offsets. No type-clean engine or performance improvement is claimed.

Physical journal UUID/coverage checks and native application remain next, then
automatic staging/publication and the existing public API, verify/metrics,
transfer/restore/fork/concurrency/crash requirements. Cost and precise limitations:
[COMMIT_CATALOG_V1](../architecture/COMMIT_CATALOG_V1.md). No OCC/durability or
multi-reader/writer premise changed. Pulse and 19 reserved specs remain untouched.

### CAP-1B qualified physical published head — 2026-09-08

Implementation checkpoint: `55e68f2125711308f1640a3d77c74ddaa6b61ff7`.

Implemented bounded validation of the already-published journal head against an
independently supplied UUID, activation and checkpoint: exact whole-page file
extents, headers, first/last directory boundaries, required page stamps and the
complete final record. Native idle preflight uses it only for an empty replay
after activation, with the UUID passed by RecoveryManager/TransactionManager's
composition. No journal effects are admitted or applied, and no new history is
published. [Precise scope and bounds](../architecture/COMMIT_CATALOG_V1.md).

New suite has **42 cases**: 512/8192-byte pages, 1/8/14/128 entries and small/large
final metadata, ten physical-size faults, nine boundary/content faults (including
CRC-valid page with corrupted nested record CRC and cross-directory clock order),
four pre-provider coordinate refusals and three native idle-preflight UUID cases.
Physical materialization is test setup, not automatic native journal publication.
The positive native test validates detached checkpoint context against real memory
storage/pool; it is not a complete public startup/WAL/crash execution.

Initial fixture issues (relative imports in an unpackaged test directory, required
BufferPool label, strict import reexports) were corrected. The nested CRC test
flips an existing record-CRC byte without changing block length. Final focused
result: **42 passed in 7.37 s**. Grouped validation:

```text
python -m pytest tests/txn/test_commit_catalog_physical.py
  tests/txn/test_commit_catalog_store.py tests/txn/test_commit_catalog_transition.py
  tests/txn/test_commit_catalog_activation.py tests/recovery
  tests/storage_core/test_catalog_replay_images.py
  tests/api/test_checkpoint_and_reclamation.py tests/api/test_m0b_catalog_repair_from_log.py
  tests/test_import_boundary.py tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**1,102 passed in 61.70 s**. Focused suite after test-import cleanup overlaps this
group. Ruff/diff-check pass. Strict mypy on four production modules versus HEAD
`420e9de` shadow sources reports identical **69** existing diagnostics, normalized
only by line offsets. New test module has only eight imported Catalog/Store
baseline diagnostics after import correction; none in its own code. No global
type-clean, full-history integrity or latency/speedup claim.

Remaining: complete native journal-target/crash-cut coverage and application,
automatic staging/publication, full verify/metrics and public APIs, then existing
transfer/restore/fork/concurrency/crash acceptance. This bounded head verifier is
not a substitute for the full historical scan. All original multi-reader/writer,
OCC and durability premises remain. Pulse and 19 pending specs were untouched.

### CAP-1B native journal replay checkpoint — 2026-09-08

Implementation: `6b5163ea8f1dec57411920880731602e439ef539`.

The finite native-journal integration step is closed: qualified RecoveryManager
and TransactionManager replay now connect complete semantic journal validation
to the existing page-apply, flush and control-publication protocol. This supersedes
earlier journal-effect integration guards, **not** automatic writer activation or
the remaining CAP-1 requirements. [Protocol details](../architecture/COMMIT_CATALOG_V1.md#native-journal-effects-and-target-validation).

Implemented:

- Per-transaction image cardinality admission before any image decompression.
- Schema activation and every post-activation COMMIT's complete journal coverage,
  including maintenance and epoch-qualified reuse of transaction IDs.
- Physical and resident target UUID/role/address validation; future page LSNs and
  differing content at a WAL-covered LSN refuse before any ordinary effect applies.
  Seqlock-only publication changes do not create false content mismatches.
- Final WAL overlay plus exact projected file sizes checked against final head.
  Missing/zero-filled targets are replayable, not inferred historical records.
- Same-pool dirty replay, fresh-pool partial replay, native WAL/data barriers and
  checkpoint inventory containing both journals even without a modified-page ledger.
- Journal pages have a known empty heap-watermark scope, avoiding a whole-table
  watermark scan caused solely by appending audit history.

New native suite: **109 cases**, including 64 combinations of four-page physical
subsets, raw/compressed WAL and valid/unwritten materialization; 24 physical-target
faults; dirty-pool repetition; three invalid resident targets; missing middle
coverage; checkpointed suffixes; eight before/after native write interruptions;
three barrier failures with successful recovery retry; cardinality admission;
unqualified refusal; and public Database checkpoint/reopen on local storage.
These tests use actual WalManager and RecoveryManager paths. They do not claim
automatic normal-transaction journal staging or arbitrary damaged-page repair.

An expanded regression exposed the pre-existing mutable two-entry CRC argument
convention table. It is now a MappingProxyType with a dedicated exact-content
test; the structural isolation gate was not waived. Six schema-only fixture cases
were updated: schema-horizon validity is no longer presented as sufficient native
journal coverage, and checkpoint-proof reuse remains tested independently.

Final grouped run:

```text
python -m pytest tests/txn/test_commit_catalog_store.py
  tests/txn/test_commit_catalog_transition.py tests/txn/test_commit_catalog_physical.py
  tests/txn/test_commit_catalog_activation.py tests/recovery tests/storage_core
  tests/api/test_checkpoint_and_reclamation.py tests/api/test_m0b_catalog_repair_from_log.py
  tests/test_import_boundary.py tests/test_optional_package_boundary.py
  -q -o addopts="--strict-markers --timeout=60 --timeout-method=thread"
```

**2,433 passed in 77.27 s**. Focused runs overlap this result. Ruff and diff-check
pass. Strict mypy, using explicit repository MYPYPATH, nonincremental isolated
caches and four HEAD shadow sources, reports the same **78** existing production
diagnostics before/after; none added. Earlier mixed installed-package/cache
observations were discarded. Changed tests report only three existing errors in
test_no_shared_state; none in the new replay suite. This is not global type-clean.

CRC-invalid or odd-sequence targets remain fail-closed, with no blind replacement.
Full historical verification, normal writer staging/retarget/OCC, public metadata
and history APIs, metrics, transfer/restore/fork and full CAP-1 acceptance remain
separate unfinished requirements. No original multi-reader/writer, WAL, OCC or
durability premise changed. The user requested stopping at this checkpoint before
joint preparation/publication of version 0.0.4. No PyPI upload, main merge, runtime
deployment or additional spec consolidation was performed.
