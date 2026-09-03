# P2-ID v1 — identity access path, persistent secondary indexes and growth-only rehash

**Status:** accepted for implementation in `0.0.2`  
**Decision date:** 2026-09-03  
**Scope:** item 10 of the post-P1 performance queue

This ADR is the finite implementation contract for P2-ID. It supersedes the original D-08
shortcut, not the transaction, WAL, recovery or descriptor-identity contracts. Performance is not
a release gate for this item; correctness, crash convergence and mixed-fleet refusal are.

## 1. Context and direct evidence

The current exact index is a fixed-directory chained hash. `DEFAULT_BUCKET_COUNT` is `64` and
`MAX_BUCKET_COUNT` is `4096` in `domain/index/keys.py`; pages `1..bucket_count` are eagerly reserved
as bucket heads by `IndexStore`, and an equality lookup walks the selected chain in
`engine/index_manager.py`. The cost therefore tends to `O(N / bucket_count)`. Merely adding a
`RecordId` definition with the current default would move the landing scan into a chain that grows
without a sizing or rebuild policy; it would not solve the scaling problem.

There are four further constraints in the code as it exists before this ADR:

1. `IndexDefinition.digest()` includes `bucket_count` and `key_derivation`. Opening one physical
   file under another size or derivation is, correctly, refused rather than treated as stale.
2. index header format 2 already persists `table_id`, `bucket_count`, the definition digest,
   `built_through_lsn`, `reconciled_through_lsn` and `artifact_nonce`. Those fields are sufficient
   to certify a new physical generation; an index header format 3 is unnecessary.
3. catalog format 1 persists only tables and embedding spaces. Automatic definitions are derived
   again at composition time and a process-local custom definition has no durable logical
   authority after reopen.
4. `RecordId` is stored as unsigned 64-bit. Its usable domain is `1..2**64 - 2`; zero means no
   identity and `2**64 - 1` is the exhausted marker. The public scalar encoding of `INT64` is
   signed and therefore cannot encode the usable ids from `2**63` through `2**64 - 2`.

The existing bounded transaction-local locator remains useful, but on a miss it eventually falls
back to the canonical heap lookup. Traversal landing can therefore still scan a node table once for
each distinct requested identity. The new access path must remove that ordinary `O(N)` fallback
without letting an incomplete, stale or foreign index return a short answer.

## 2. Decision summary

P2-ID v1 delivers exactly these capabilities:

- catalog format 2 with the required capability `identity_secondary_indexes_v1`, coactivated
  with commit-state payload format 2 as the pre-mutation mixed-fleet fence;
- durable logical definitions and explicit physical index generations;
- one automatic exact `RecordId -> RecordRef` index for each node table used as a relationship
  endpoint;
- equality-only custom exact indexes, including ordered compound keys;
- deterministic initial bucket sizing and an explicit growth-only shadow rehash;
- planner use of an eligible exact index followed by mandatory heap validation;
- a mixed-fleet fence that makes a `0.0.1` build refuse before any mutation after catalog v2 is
  authoritative.

It does **not** add a new concurrency model. A build or rehash owns the existing
`COMMIT_SECTION` for its complete scan, shadow durability barrier and catalog activation. Writers
wait; readers keep their valid snapshots. There is no background build, dual-write or WAL catch-up
protocol in this version.

## 3. Catalog v2 is authority and downgrade fence

### 3.1 Capability

Catalog v2 stores an ordered set of required capabilities. This ADR introduces exactly one:

```text
identity_secondary_indexes_v1
```

A decoder must refuse an unknown required capability with a typed schema-version/capability error.
It must never ignore one. The format number is raised from `1` to `2`, rather than putting meaning
in the v1 `reserved` word: the released v1 decoder ignores that word but already refuses any catalog
whose `format_version` is greater than the version it reads. That existing refusal is the fleet
fence.

Catalog v1 remains readable by `0.0.2`. Deserializing it produces no persisted index definitions
and does not invent the new capability. Serializing an unchanged v1 catalog must also remain v1;
an ordinary read, reopen or unrelated schema operation must not silently cross a one-way format
boundary. Only the explicit activation protocol in section 7 publishes v2. Once activated, every
later catalog image remains v2 and retains the required capability.

Catalog v2 is the semantic authority, but it is not by itself an adequate fence for a `0.0.1`
process that was already open. In particular, after activation WAL has been checkpointed and
recycled, the old recovery path may retain its process-local v1 catalog and mutate recovery state
before it reinterprets `catalog.dat`. Activation therefore also publishes commit-state payload
format 2. It retains the exact 36-byte layout and checksum of format 1 and changes only the version
field. Released `0.0.1` already reads this record before begin/write, DDL artifact work, checkpoint
and recovery mutation, and refuses an intact future version. A larger payload is forbidden because
the old recovery path classifies a length mismatch as recoverable corruption rather than a version
fence.

The two authorities are coactivated under `COMMIT_SECTION`: catalog v2 becomes durable through its
normal WAL transaction, and commit-state v2 is its final publication act. Every later commit and
checkpoint preserves version 2. No path may publish version 1 once catalog v2 has committed.

The concrete publication order is `WAL barrier -> catalog/page apply -> catalog flush -> durable
catalog decode -> commit-state publication`. Only a transaction whose durable page set contains
`catalog.dat` pays for that decode; an unrelated commit preserves the previously published
commit-state version and cannot promote from an unsaved process-local catalog. Foreign-gap
completion and recovery inspect the durable catalog after replay before publishing.

The first format-1-to-format-2 publication replaces both physical commit-state slots. A crash
after its first page write leaves format 2 newest and format 1 only as fallback; recovery heals the
second copy before returning. Readers and publishers inspect every outer-valid slot, not just the
highest generation: a future logical payload or an older physical slot carrying a stronger
supported fence is a pre-mutation refusal. WAL-authorized reconstruction of torn commit-state
bytes preserves the strongest decodable fallback fence. Header-checksum repair may inspect the
slots only when the independent remaining checksum, canonical page header, database/kind/nonce
binding and each slot checksum still validate; it never treats a foreign binding as torn local
state. Generation capacity for both promotion writes is checked before either write.

The v2 catalog checksum covers the complete body, including capabilities, logical definitions and
physical generation records. Required capabilities use a bounded `u64` bitset; bit zero names
`identity_secondary_indexes_v1`, and an unknown required bit is a typed capability/version refusal.
Encoding is deterministic: logical indexes sort by their case-folded registry key and generations
by nonce. Duplicate names, non-canonical order, duplicate nonces, unknown enum/state codes,
trailing bytes, inconsistent counts and invalid cross-references are corruption, not values to
normalize while reading.

### 3.2 Persisted logical definition

Catalog v2 persists the exact indexes controlled by this capability: automatic primary-key and
relationship-endpoint definitions when they receive a physical generation, the identity
definitions from this ADR, and user-created exact definitions. Existing proximity/vector indexes
remain derived from their durable table/embedding-space schema and retain their current specialized
runtime type; P2-ID v1 neither serializes them as a generic definition nor changes their lifecycle.
A persisted logical definition contains at least:

```text
name
table_id + table_name
ordered column positions
visibility
key_derivation
automatic/system flag
expected_cardinality (optional sizing hint)
physical generations
```

`table_id`, `table_name`, positions, visibility and derivation must agree with the committed table.
The existing complete provenance check remains in force. `expected_cardinality` is an unsigned
planning hint, not part of key semantics and therefore not part of the definition digest. The
selected `bucket_count` is physical meaning and remains in that digest.

A `record_id_u64_v1` definition is the sole legal definition with no column positions. Every other
exact definition still needs one or more valid, non-repeated positions. User names cannot occupy
the reserved automatic identity namespace.

### 3.3 Physical generations

A logical index owns zero or more generation records. Each record contains:

- a non-zero database-unique `artifact_nonce` (`u64`);
- its actual `bucket_count`;
- one state: `building`, `active` or `stale`;
- the physical artifact identity derived from the nonce.

The artifact is named `index/g_<nonce-as-16-lowercase-hex>.idx`. This is a canonical index path
under the existing ST-2 whitelist and is independent of a possibly 128-character logical name.
The catalog stores the nonce, not an arbitrary path supplied by a caller.

At most one generation of a logical index is `active`. A `building` or `stale` generation is never
planner-eligible. An active generation is eligible only after a fresh header certificate proves all
of the following against the catalog and heap authority:

- header format 2, exact expected visibility and table id;
- exact definition digest and bucket count;
- exact non-zero artifact nonce;
- enough physical pages for the directory it declares;
- `built_through_lsn` at or beyond the table high-water required by the statement;
- no ahead-of-publication state forbidden by the existing freshness contract.

Index header format 2 is retained unchanged. A rehash writes a new file and a new nonce; it never
renames or replaces the active index file. This preserves the premise of
`ST2_DESCRIPTOR_REVALIDATION.md`, especially in `descriptor_revalidation="generation"` mode.
Old and orphan generations are retained until a separate safe cleanup operation; their existence
does not make them eligible.

## 4. Canonical identity key

The identity derivation name is exactly:

```text
record_id_u64_v1
```

Its key is exactly nine bytes:

```text
0x01 || struct.pack("<Q", record_id)
```

The leading byte versions and domains the key independently of the public scalar codec. The input
must be an integer other than `bool` in the closed interval `1..2**64 - 2`; zero, the exhausted
marker, negative values, booleans and overflow are typed refusals. This encoding is deterministic
across process, Python version and platform and covers the complete usable durable `RecordId`
domain.

Key derivation for index staging, rebuild and verification is generalized from only
`key_for(values)` to the complete heap-version identity needed by the declared derivation. The
column derivation continues to consume positional values exactly as today; only
`record_id_u64_v1` consumes `HeapVersion.record_id`. INSERT, UPDATE and DELETE stage the identity
entry from the born/ended heap version and its `RecordRef`. A DELETE intent's empty value tuple is
not re-encoded for quota calculation; it pays the already-agreed fixed intent entry and uses the
ended version identity that the write path already obtained.

The automatic logical name is `rid_t_<table_id-as-8-lowercase-hex>`, for example
`rid_t_00000001`. Numeric table identity keeps the name bounded and stable even for a maximum-length
table name. The index is exact and unversioned in the existing sense: its candidates never decide
snapshot visibility by themselves.

## 5. Scope of automatic identity indexes

P2-ID v1 creates identity indexes only for node tables that are the declared `from_table` or
`to_table` of at least one committed relationship table. It does not allocate an unused index for
every node table.

For an existing v1 graph, the upgrade inventory is the unique set of those endpoint node tables.
For a new relationship table, both endpoint identity indexes must already be active, or be built and
made active in the same fenced schema operation, before the relationship definition becomes
committed/usable. A relation can never become visible first and depend on an index generation that
may still be partial.

Pending rows, owner overlays and the bounded transaction-local locator keep their current
semantics. They are consulted before the committed identity access path. An outside transaction
cannot observe an uncommitted identity merely because the owner staged an index change.

## 6. Sizing and growth-only rehash

### 6.1 Deterministic sizing

The two creation hints are mutually exclusive:

- `bucket_count`: the exact legal bucket count;
- `expected_cardinality`: a positive `u64` sizing hint.

When `expected_cardinality` is used, sizing is:

```text
TARGET_ENTRIES_PER_BUCKET = 64
required = ceil(expected_cardinality / TARGET_ENTRIES_PER_BUCKET)
bucket_count = next_power_of_two(max(MIN_BUCKET_COUNT, required))
```

The result must be within `MIN_BUCKET_COUNT..MAX_BUCKET_COUNT`; it is not silently truncated. A
value beyond the supported eager-directory bound is a typed configuration refusal naming the
largest representable expected cardinality. The operator can choose an explicit legal count, but
the engine makes no claim that a capped chained hash remains constant-cost under unbounded growth.
An extensible/sparse directory is deliberately outside this ADR.

With neither hint, custom indexes retain the current `DEFAULT_BUCKET_COUNT = 64`, which is
equivalent to a default expected cardinality of `4096` under the formula. An automatic identity
index uses `max(4096, 2 * current_visible_row_count)` as its expected cardinality at build time,
giving existing endpoint data one growth interval of headroom. The count and scan are taken from
the same fenced durable view used to build the shadow.

### 6.2 Rehash

`rehash_index` accepts exactly one of `bucket_count` or `expected_cardinality` and resolves it by the
same validation and formula. The resolved bucket count must be strictly greater than the active
generation's count. Equal or smaller values are typed refusals; P2-ID v1 never shrinks.

Rehash is explicit, foreground maintenance:

1. enter the normal writable/recovery-complete door and acquire `COMMIT_SECTION`;
2. refresh and validate catalog, publication state and the table's page-0/high-water authority;
3. allocate a distinct non-zero nonce and a never-before-active physical file;
4. scan the fenced heap view and build the complete `building` shadow with the new count;
5. verify the shadow, set its header horizons to the exact fenced durable position, flush it and
   establish the storage durability barrier;
6. run the unchanged first/second OCC and pre-WAL authority checks;
7. publish the catalog v2 image that marks the new generation `active` and the former generation
   `stale`, through the normal WAL-before-data transaction;
8. release `COMMIT_SECTION`; only later writers can target the new active generation.

No writer can interleave between steps 2 and 8, so there is no dual-write or catch-up interval.
Readers that began before activation may finish using their already-certified view; a new statement
refreshes the catalog/read view and resolves the active generation again. The longer writer pause is
an explicit cost of this finite protocol, not a change to the multiwriter/multireader premise.

There is no automatic background rehash in this version. Inventory/metrics expose active bucket
count and the configured/derived expected cardinality so operators can schedule foreground growth
before chains become material.

## 7. Activation and migration protocol

The activation point is the committed catalog v2 image. Before it, catalog v1 and the old access
paths remain authoritative. After it, catalog-managed exact paths come only from persisted v2
definitions and their active generations; unchanged automatic vector/proximity paths continue to
come from the durable table schema and their specialized composer.

The v1-to-v2 operation is:

1. open writable and complete normal WAL recovery before migration;
2. acquire `COMMIT_SECTION` and refresh the durable catalog inside it;
3. inventory all existing automatic **exact** definitions and all required endpoint identity
   definitions; vector/proximity definitions continue to be composed from schema;
4. build every missing generation as a distinct shadow against one fenced published view;
5. verify and durably flush every shadow; no v2 catalog byte has yet been published;
6. build one canonical v2 catalog containing the capability, all logical definitions and all
   active generation records;
7. stage that catalog through the existing catalog page-image/WAL transaction, retain both OCC
   validations, append and force WAL before applying catalog pages, then publish the same commit
   in the unchanged 36-byte commit-state payload with format version 2;
8. synchronize the in-memory registry only from the committed catalog authority.

An empty, read-only or ordinary v1 open does not run these steps. Activation occurs only through
the explicit, idempotent `Database.ensure_identity_indexes()` door or an operation that requires
v2 (`CREATE INDEX`, rehash, or creating a relationship whose endpoints are not yet covered). A new
database that has no relationship and no custom index may therefore remain catalog v1. There is no
half-migrated mode in which a process is allowed to answer from a shadow not named by the committed
catalog.

Physical v2-to-v1 downgrade is unsupported in this item. Rollback means restoring the complete
pre-activation backup/copy while no process is open, or logical export into a new v1 database. It is
not deleting the v2 catalog or copying old index files over active names. This one-way boundary must
be stated in release notes and Pulse deployment documentation before activation.

## 8. Compatibility and crash matrix

| Actor/state | Required result |
|---|---|
| `0.0.2` read-only, catalog v1 | Opens without mutation. Uses legacy definitions where safe and heap/locator fallback; creates no file and publishes no capability. |
| `0.0.2` writable, catalog v1, ordinary work | Opens and recovers v1 normally. It does not migrate merely by opening. An operation requiring P2-ID follows section 7. |
| `0.0.2` read-only, catalog v2 | Opens and uses only freshly certified active generations. It never repairs, builds, activates or deletes an artifact. |
| `0.0.2` writable, catalog v2 | Uses persisted definitions, stages every active-index effect in the existing transaction/WAL protocol and may run explicit foreground rehash. |
| `0.0.1`, cold open of catalog v2 | Refuses the coactivated commit-state/catalog future version with `GrafxSchemaVersionMismatch`; it must not bootstrap, quarantine, downgrade or mutate the database. |
| `0.0.1` already open when v2 activates | A pinned read may finish. Its next begin, row write, DDL, checkpoint/recycle or recovery mutation reads commit-state v2 first and refuses **before** read-view invalidation/write-back, WAL append, page materialization or publication. |
| crash while building a shadow | Catalog v1 or the former v2 active generation remains authoritative. The incomplete/unreferenced file is ignored and retained as an orphan. |
| crash after shadow barrier, before catalog WAL commit | The complete shadow is still unreachable. Reopen uses the old authority; later maintenance may identify the orphan by nonce. |
| crash during catalog activation | Existing WAL recovery converges to the complete old catalog or the complete committed v2 catalog. It never publishes a prefix of its index-definition set. |
| crash after committed activation | Reopen sees the complete v2 authority and requires its referenced generation/header certificate. Missing, mismatched or partial referenced bytes fail closed. |
| catalog v2 with a persisted `stale` generation | Planner does not use it. A correct canonical heap fallback is allowed; writable maintenance may rebuild it. |
| restore/logical import | Logical rows/schema are authoritative; generation files are rebuilt and newly nonced. A copied generation is never trusted without matching database/catalog/header authority. |

The already-open `0.0.1` row is a release blocker, not an aspirational test. In particular, a
staged old transaction that waited while activation held `COMMIT_SECTION` must read the v2 commit
state after it acquires the section and fail before appending its WAL. Recovery must fail at that
same first control-state read, including after activation WAL has been recycled; it must not repair
the ledger, truncate WAL, publish heap/index effects or advance commit/checkpoint state under a
build that cannot decode the catalog.

## 9. Lookup, fallback and correctness boundary

An identity lookup is scoped by committed table identity and exact `record_id_u64_v1` bytes. A hash
hit returns candidates only. The engine must read the referenced heap version and validate table,
`RecordId`, snapshot visibility, version/ref relationship and re-derived key before returning a
row. This retains the existing rule that the heap is truth for exact indexes.

Fallback is allowed only when decided before consuming an active index result:

- a catalog v1 graph has no persisted identity definition;
- catalog v2 has no applicable logical definition; or
- the applicable generation is already classified `building`/`stale` or fails the normal
  non-corrupt freshness eligibility check at statement preflight.

In those cases the existing canonical heap scan/transaction locator produces the answer. Fallback
is **not** allowed when an index selected as active changes or fails during the read. A missing
referenced file, nonce/digest/table/count mismatch, malformed page/entry, foreign replacement,
certificate change between pre/post reads or other corruption is a typed fail-closed outcome. The
engine must not combine a partial candidate set with a later scan and call it one statement view.

The same rule applies to custom exact secondary indexes: planner eligibility can improve an
equality predicate, but result semantics, `NULL`, owner overlay, snapshot and error timing remain
those of the canonical plan. Compound keys preserve declared position order. No index here implies
a uniqueness constraint.

## 10. Minimal API and configuration

The query surface is exactly:

```text
CREATE INDEX <name> FOR (<var>:<table>) ON (<var>.<column>[, <var>.<column> ...])
[OPTIONS bucket_count = <positive-integer>
 | OPTIONS expected_cardinality = <positive-integer>]
```

The `OPTIONS` clause is optional; when absent, section 6's default applies. At most one option is
legal. Values are integer literals in v1 of this grammar; unknown, repeated or parameterized
options are refused before mutation. Only exact equality hash indexes are created. DDL uses the
existing transactional schema path and does not expose a partially registered definition if any
validation/build/publication step fails.

The equivalent Python doors, needed by composition and maintenance, are:

```python
Database.create_index(
    name,
    table,
    columns,
    *,
    bucket_count=None,
    expected_cardinality=None,
)

Database.rehash_index(
    name,
    *,
    bucket_count=None,
    expected_cardinality=None,
)

Database.ensure_identity_indexes()
```

`create_index` and `rehash_index` accept exactly one sizing hint at most and return a detached
public index view only after the catalog commit is durable. The public view reports logical
name/table/positions/derivation, automatic flag, state, active nonce, bucket count, sizing hint and
freshness horizons; it exposes no writable store or raw storage capability.

`ensure_identity_indexes()` takes no sizing override. It is idempotent: on catalog v1 it executes
section 7 for the endpoint-table inventory; on catalog v2 it builds only missing/stale required
identity generations; and when every required generation is already active and fresh, it performs
no durable write. This is the explicit upgrade door for an existing graph and the integration door
Pulse may invoke during a controlled migration. It is never called merely as a side effect of
`connect()`.

No global runtime *sizing* flag is added. Sizing belongs to the index definition/maintenance
request, not `connect()`. Automatic identity sizing follows section 6 and can later be grown
explicitly. `connect(..., max_index_build_entries=N)` is instead an optional admission guard: it
does not choose bucket counts or persisted definitions and `None` preserves prior behavior. A
positive N bounds the final exact entries across one detached activation or catalog-v2 DDL batch.
Each committed, non-provisional heap version that derives a key charges one entry; an ended version
and its tombstone remain one final entry, while a newly declared empty table charges zero. The
bounded preflight stops at N+1 and refuses before catalog staging or the first exclusive generation
file creation.

There is no `DROP INDEX` in this delivery. There is likewise no public path to choose an artifact
nonce, physical filename, state or built-through horizon.

## 11. WAL, OCC, recovery and descriptor invariants

The following existing guarantees are unchanged and are acceptance conditions:

1. The first and second OCC validations remain intact. P2-ID does not weaken read/write interests,
   the authorized durable-baseline rule, table page-0 checks or catalog epoch checks.
2. A normal row commit stages index effects for the active logical generation and includes them in
   the same WAL-governed commit as the heap version. Index durability never acknowledges before the
   WAL barrier and commit-state publication sequence allows it.
3. Logical WAL identity remains the committed logical index name. Recovery resolves that name only
   through the refreshed catalog generation authority and refuses a missing/mismatched required
   definition; it never guesses the newest file by directory order.
4. A shadow is unreachable until its own data/header barrier is complete. Catalog activation is
   the single publication point and itself follows WAL-before-data.
5. `built_through_lsn` and the table high-water remain the freshness proof. Neither catalog presence,
   `expected_cardinality` nor a matching filename grants freshness.
6. The active header's digest/count/nonce are checked by fresh pre/post certificates. A catalog
   pointer is authority for *which* artifact to inspect, not proof that its bytes are healthy.
7. New generation files are never atomically replaced over a canonical live index name. Both ST-2
   descriptor modes are covered; `generation` does not substitute a freshness or OCC proof.
8. Writer serialization for build/rehash uses the existing cross-process `COMMIT_SECTION`; reader
   snapshots, writer leases, WAL retention and recovery-required latch semantics are unchanged.
9. Transaction quotas continue to cover DDL/catalog/WAL staging, and DELETE intents retain their
   fixed-entry quota behavior without encoding an empty value tuple. Detached exact generations
   have the separate `max_index_build_entries` admission guard: activation and catalog-v2 DDL sum
   one final entry per key-bearing committed heap version across the batch, stop at N+1, and raise
   `GrafxTransactionBudgetExceeded(field="max_index_build_entries")` before catalog staging and
   before any exclusive generation-file create. `None` is disabled and a new empty table counts
   zero; no refusal publishes partial catalog or index authority.
10. Commit-state formats 1 and 2 have the same 36-byte layout. Activation publishes format 2 only
    after the catalog commit is durable, and every later publisher preserves that version.

## 12. Focused quality gates

Implementation is admitted only after these bounded gates pass. Expensive broad regression is
grouped at the milestone rather than repeated after every patch.

1. **Catalog codec:** deterministic v2 round-trip; v1 read and byte-preserving non-activation;
   checksum/truncation/count/order/duplicate/cross-reference corruption; unknown required
   capability; and the released/frozen v1 decoder refusing v2.
2. **Mixed fleet:** frozen `0.0.1` decoding classifies the same-size commit-state v2 record as
   version mismatch, never corruption; a real already-open v1 process stages before activation and
   then loses begin, row write, DDL, checkpoint/recycle and recovery doors before read-view
   write-back, WAL or durable file change; a cold v1 process also refuses v2. The recovery case is
   repeated after activation WAL has been checkpointed/recycled.
3. **Unsigned identity:** golden bytes for `1`, `2**63-1`, `2**63` and `2**64-2`; refusal of zero,
   `2**64-1`, bool, negative and overflow; deterministic cross-process bucket/key results.
4. **Definition persistence:** custom, compound, identity and generation-managed automatic exact
   definitions survive cold reopen; vector/proximity definitions continue to survive through their
   table schema and specialized composer. Process-local unregistered exact definitions are never
   planner authority.
5. **Identity lifecycle:** insert, payload update with stable id/new ref, delete, rollback, conflict,
   pending-owner/outsider, self-loop, parallel relationships and cold reopen all match the canonical
   heap oracle.
6. **Relationship activation:** new relationship DDL cannot become visible before both endpoint
   identity generations are active and durable; fault injection at every shadow/activation boundary
   leaves the prior schema or the complete new schema.
7. **Rehash:** every legal growth boundary, refusal of shrink/equal/oversize, exact old/new result
   parity, distinct nonce/file, old generation retained, crash at every write/barrier/WAL/apply point,
   recovery convergence and cold `verify("all")`.
8. **Read races:** active generation stale/rebuilt/rehashed by a second process, descriptor
   replacement in both `strict` and `generation`, missing/malformed file and pre/post certificate
   changes; known preflight stale may scan, mid-read change/corruption must fail closed.
9. **Recovery:** activation WAL replay, index logical redo against the catalog-selected generation,
   committed catalog page replay followed by old-build refusal, and no commit/checkpoint advancement
   after an undecodable authority.
10. **Planner/API:** equality and ordered compound exact plans are oracle-equivalent for `NULL`,
    parameters, transaction overlays and errors; non-equality/range/prefix/order claims are absent;
    both sizing spellings and every invalid option refuse before mutation.
11. **Multiprocess quality:** concurrent readers/writers through activation and after rehash produce
    no lost row, duplicate, phantom or torn answer; all acknowledged writes survive cold reopen and
    verification. This is an integrity gate, not a throughput threshold.
12. **Milestone regression:** index/query/transaction/recovery/Pulse compatibility slices, lint,
    compile, diff check and one grouped broad suite. Performance measurements document direction and
    write amplification but do not create a new blocker or target.

## 13. Non-goals

The following are explicitly outside P2-ID v1 and cannot be pulled in as a prerequisite:

- B+tree, range, prefix, `ORDER BY`, full-text or fuzzy indexes;
- extensible/linear hashing or a sparse bucket directory;
- sharding, repartitioning or a change to the single-database local-first premise;
- online rehash, background workers, dual-write generations or WAL catch-up builds;
- index shrink, bucket merge, `DROP INDEX` or live reclamation of stale/orphan generations;
- general uniqueness constraints beyond primary-key behavior that already exists;
- persistent cardinality statistics, a cost-based optimizer or a new planner statistics format;
- MVCC vacuum/compaction (item 11), WAL delta/chunking/physiological redo (item 12), or a native
  codec beyond CRC (item 13);
- a new index header version, heap format, WAL record format, isolation level, reader protocol,
  writer lease, descriptor-revalidation mode or recovery policy;
- removal of the canonical heap/locator fallback where the catalog declares no eligible index;
- physical v2-to-v1 downgrade tooling.

These exclusions keep the delivery finite. A later ADR may address them only as a separate
milestone with its own format, migration and recovery analysis.

## 14. Consequences

The expected gain is structural: committed endpoint landing changes from a table scan to one hash
bucket chain sized for the declared/current cardinality, followed by the heap validation correctness
already requires. Custom equality predicates gain the same access path, and rehash can grow the
directory without rebinding a live physical index name.

The costs are explicit. Catalog activation is a one-way fleet boundary; index files reserve one
head page per bucket; every indexed row adds write/WAL work; foreground build/rehash pauses writers
for a complete scan; stale/orphan generations consume disk until later maintenance; and the eager
directory retains a finite ceiling rather than promising constant cost for arbitrary growth.

For Okto Pulse, the graph/query result contract does not change. The automatic identity index is an
internal access path and every answer remains heap-validated. Deployment must nevertheless pin a
Grafx build that understands catalog v2 before activating the capability, and rollback must use the
pre-activation backup/logical procedure described above rather than reinstalling `0.0.1` against
the migrated bytes.

## 15. Implementation trace

The implementation is intentionally split at reviewable durability boundaries:

| Milestone | State | Evidence |
|---|---|---|
| Canonical unsigned identity key | complete | `fa0c298`; nine-byte `record_id_u64_v1` codec over the complete usable `u64` domain, not yet runtime-eligible by itself |
| Catalog v2 and generation authority | complete | `4feec76`; deterministic v1/v2 codec, capability fence, logical definitions and immutable physical generation identity |
| Catalog/commit-state coactivation | complete | `12414d0` + documentation `1e68ae7`; catalog v2 becomes authoritative before commit-state v2 is the final publication act |
| ACTIVE runtime projection | complete | `99622af`; v2 exact-generation equality is enforced across composition, planner, row maintenance, redo, freshness, verifier and public inventory; v1 custom access paths remain compatible |
| Record-aware identity lifecycle | complete | `0d353ae`; quota, INSERT/UPDATE/DELETE, validated lookup, rebuild and bidirectional verification consume the durable `record_id`; logical WAL continues to name the immutable index definition |
| Statement-stable endpoint routing | complete | `01c496d`; ACTIVE identity lookup is `O(K_t)` to select and hash-directed thereafter; miss is definitive and post-selection failures never fall back |
| Activation and automatic DDL scope | complete | `ba8ca9a`; explicit/idempotent `ensure_identity_indexes`, automatic v2 generations for later NODE/REL DDL, endpoint identity scope, bounded build admission and pre-publication durability barriers |
| Custom secondary indexes | complete | `2fa81b1`; transactional `CREATE INDEX` and Python/maintenance doors, ordered compound keys, deterministic sizing, detached committed receipt and query-equality-safe execution |
| Growth-only rehash | next | foreground ACTIVE-to-STALE generation rotation with the recovery matrix in section 12 |

The ACTIVE projection uses a structural catalog map and a structural raw-registry map keyed by
`(table_id, table_name)`. Per-row count/staging is therefore `O(K_t + S_txn)`, where `K_t` is the
number of indexes on that table and `S_txn` its owner-observed speculative set; it does not scan
all indexes in a growing graph. A name lookup for a catalog-v2 exact index resolves the logical
definition directly and compares the complete runtime definition, including `artifact_nonce`.

Quality evidence for `99622af`: the grouped index/query/transaction/recovery/API slice passed
242/242 tests, focused discriminants passed, Ruff/compile/diff checks were green, and two
independent adversarial reviews reported no remaining blocker for this boundary. This does not
claim that identity indexes, DDL or rehash are already available; those remain the explicitly
listed subsequent milestones above.

Quality evidence for `0d353ae`: 435 grouped index and transaction tests passed. Dedicated tests
cover unsigned identities above `2**63`, stable identity across UPDATE, heap-authoritative DELETE,
quota/staging equality, validated lookup, rebuild and both verifier directions. Ruff lint,
`compileall` and diff checks passed, and the adversarial review found no blocker. Catalog v1's raw
registry remains visible only to its historical component diagnostic; catalog v2 verification
still refuses BUILDING, STALE and rogue registrations.

Quality evidence for `01c496d`: 26 focused routing/locator tests passed, including unsigned
identities above `2**63`, fixed fallback, definitive miss, duplicate-visible corruption, missing
store, physical-definition mismatch and missing heap-validation capability. The broader focused
relationship regression, Ruff, `compileall` and diff checks were green, and an independent
adversarial review found no blocker. The real-store cold-reopen proof belongs to activation because
catalog v1 deliberately has no persistent identity generation to reopen.

Quality evidence for `ba8ca9a`: explicit activation builds and verifies every automatic exact and
required endpoint-identity generation as an unreachable nonced shadow, then publishes the complete
catalog v2 authority through the ordinary OCC/WAL/apply/publication protocol. Later catalog-v2 NODE
and REL DDL creates the corresponding ACTIVE generations atomically; generations for new empty
tables cross a physical durability barrier before catalog staging, while builds over committed
endpoints retain complete-table OCC interests through commit. The keyword-only
`max_index_build_entries` guard sums the exact final entries across a shadow batch and refuses at
N+1 before catalog staging or the first generation-file create. Case-fold collisions preserve the
supported scan-only table, RecordId generations cannot be selected as generic property indexes,
rollback releases every process-local claim/cache binding, and retry uses fresh nonces. The grouped
activation/DDL/quota/planner/config gate passed 367 tests; the post-format DDL fault slice passed
5/5, Ruff lint, compile and diff checks were green, and two adversarial reviews found no remaining
blocker in the delivered boundary.

Quality evidence for `2fa81b1`: textual DDL and `Database.create_index()` share one analyzed plan
and seal migration/repair plus the custom shadow into one fresh dedicated transaction. Quota and
all caller refusals precede catalog staging or generation-file creation; build and verification
occur under the existing writer/publication fences and catalog activation remains the only
reachability point. Ordered compound positions and deterministic sizing survive cold reopen, and
the returned `IndexView` carries the ACTIVE nonce, logical metadata and freshly certified header
horizons. Query execution treats hash hits as candidates, keeps `NULL` equality unknown and takes
the canonical scan before consuming an index whenever the durable encoding cannot represent the
language's wider equality relation. The public inventory strips hostile descriptor subclasses and
continues to expose schema-derived vector/proximity indexes under catalog v2. Focused composed
gates passed 394/394 and 281/281 tests with live/cold `verify("all")`, Ruff, compile and diff checks;
the final adversarial review reported no remaining blocker.

The WAL is intentionally **not** qualified by physical generation. A logical name cannot be
rebound to different table/positions/visibility/derivation, a rehash shadow is complete through
its fenced horizon before activation, and index effects are idempotent. Recovery may therefore
apply an older logical effect to the current ACTIVE generation as repetition/repair of state the
shadow already contains. Adding a nonce to the WAL would contradict sections 11 and 13 and is not
a prerequisite for the remaining P2-ID work; the rehash crash matrix must prove these premises.
Endpoint routing must likewise make one access-path decision per `(table_id, table_name)` and
statement. If an ACTIVE identity index was selected, zero hits are definitive and any later
staleness, certificate change or damage propagates fail-closed; fallback is allowed only when it
was selected before the first identity result. Results from index and locator/scan must never be
mixed inside one statement.
