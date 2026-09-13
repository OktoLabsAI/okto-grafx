# Native node-label storage v1 — implementation foundation

[Roadmap](../../ROADMAP.md) · [Authorized scope](FUNCTIONAL_PARITY_PLAN.md#authorized-multiple-labels-and-retained-nested-storage)
· [Active TCK profile](../conformance/PROFILE_V3.md)

## Status and authority

Development source `0.0.6`, decision `FP-MULTILABEL-20260912`. The native
catalog/heap foundation and native transaction-intent publication are implemented.
**This is not yet a completed multiple-label feature or parity increment.**
The query checkpoints implement SET/REMOVE, membership predicates, owner overlays,
detached labels, multi-label CREATE/MERGE and raw scan metadata. Native retained
history now carries membership through publication, reads and recovery as recorded
below. Existing-target copy and fresh-store transfer/resume format 4 are integrated.
The [installed-wheel checkpoint](../reports/FP_NODE_LABEL_WHEEL_QUALIFICATION.md)
passes 36 native scenarios and 36 transfer worker checks. The subsequent
[complete V3 query run](../reports/FP_V3_INTEGRATED_QUERY_QUALIFICATION.md) passes
all 3,896 required cases, preserving the single Set1 divergence. Full repository
regression, supplemental evidence and final paired Pulse qualification remain required. Do not activate this
internal format in a production store or use heap internals as an application API.

The separate `FP-NESTED-STORAGE-20260912` decision retains lists of maps. Only
the exact pinned Set1 #0010 negative oracle is a divergence; label cases remain
required. No source expectations are rewritten by this storage extension.

## Identity and membership

A label set belongs to a **version of one node**, not a collection of duplicate
nodes or independent physical tables. Changing that set must retain the physical
table ID, record ID, properties, relationship endpoint identity and native
version chain. Typed constraints still belong to the physical table. A label
does not automatically add columns or bypass its schema.

`HeapVersion.node_labels` has three distinct states:

| Stored metadata | Meaning |
| --- | --- |
| `None` / no prefix | Historical implicit membership: the physical table name for an ordinary node table; no labels for its `unlabeled` flexible store |
| `()` / explicit prefix | No labels, even if the physical table is named |
| Canonical nonempty tuple | The complete explicit set; the table name is not added implicitly |

Names are nonempty, case-sensitive UTF-8 strings. Unicode, quoted punctuation,
NUL and newlines are preserved; no case folding or Unicode normalization occurs.
These logical names are intentionally independent of the physical table-name
identifier grammar. The future query resolver must preserve that distinction.

At the input normalization boundary, an exact tuple/list is copied to an owned,
sorted, duplicate-free tuple. Durable encoding requires an already canonical
exact tuple. Stored decoding rejects malformed order or duplicates rather than
repairing them. These rules do not change property list/map semantics.

## Catalog and heap bytes

Required catalog-v2 capability: **`node_labels_v1`, bit 28**. No v1 reinterpretation
is permitted. When that bit is present, every table definition carries a GXL1
candidate frame after its existing flexible-property/vector-owner flags.
Relationship tables carry an empty candidate frame and cannot have node labels.

The unchanged 40-byte heap record header reserves **flag bit 2 (`0x04`)** for an
explicit label prefix. Its payload is `GXL1 frame + existing positional tuple`.
`payload_len` counts both parts. The existing overflow mechanism applies to the
whole payload; its pointer, visibility stamps and previous-version reference
retain their formats. Unflagged historical tuples remain byte-compatible.

One GXL1 frame is:

| Field | Encoding |
| --- | --- |
| Magic | Four ASCII bytes `GXL1` |
| Label count | Unsigned little-endian 16-bit integer |
| Each label | Unsigned little-endian 16-bit byte length, then exact UTF-8 bytes |

Labels are ordered by their decoded Python string values. The complete frame
is bounded by **65,535 bytes**; the raw input count is bounded by **21,843**
elements, including duplicates before normalization. Empty sets occupy six
bytes. A single ASCII label can occupy at most 65,527 text bytes; multiple names
share the frame budget. UTF-8 byte length, not character count, is authoritative.
There is no new `connect()` or runtime configuration option for these limits.

`TableDef.extra_node_labels` is a canonical, conservative candidate summary.
`node_label_candidates` includes the implicit physical base label unless the
table is `unlabeled`. The full candidate union obeys the same encoded frame
ceiling and is validated when constructing a definition. The summary is
append-only: removing a label from a node must not remove it from this catalog
summary, since older versions can still use it.

The immutable definition caches its sorted candidate tuple and a membership
set. `admits_node_labels()` tests already validated labels in expected O(K),
where K is the number of labels in the row; it does not rebuild or encode the
table's entire candidate set for each read. **This summary is not a node lookup
index, an exact count or proof of any individual node's labels.** Query access
paths and membership indexing still require implementation/qualification.

## Implemented internal boundaries

These are engine/domain contracts for continued implementation, not recommended
application integration doors:

- `Catalog.extend_node_labels(table_id, labels)` extends candidates in a working
  v2 catalog and activates bit 28 even for an empty set. It does not itself make
  a durable commit. The caller must use the outer statement's schema journal.
- `HeapStore.insert`, `insert_reserved`, `insert_initial_reserved` and `update`
  accept keyword `node_labels=None`. Insert `None` keeps historical implicit
  membership. Update `None` preserves the previous version's explicit metadata;
  `()` explicitly removes all membership. They remain internal heap operations,
  not independent transaction/publication APIs.
- Admission resolves the effective native catalog by table ID and physical name.
  An expanded caller-supplied `TableDef` cannot grant additional labels. A stale
  conservative summary does not overrule the current native authority.
- Full reads, landing reads and projected reads validate metadata even when no
  property column is selected. Nullable schema expansion preserves the prefix
  while upgrading only the property tuple. Authenticated stored-byte accounting
  includes the prefix; property encoding proofs do not authorize label bytes.
- Catalog replay preflight checks committed after-images for candidate shrinkage
  and loss of the label capability before page effects. An old catalog reader
  must reject unknown bit 28, not interpret a flagged tuple as a legacy tuple.

Invalid caller metadata yields `GrafxConfigurationError`; corrupt stored frames
yield `GrafxCorruptionDetected`. Invalid catalog transitions yield
`GrafxRecoveryRefused`; unknown required catalog capabilities yield
`GrafxSchemaVersionMismatch`. Existing identity/extent validation can refuse an
invalid reserved-insert operation before label admission. No refusal grants
permission to repair, truncate or discard native user data.

## Qualification and remaining delivery

### Transactional publication checkpoint

The subsequent internal transaction integration adds `RowIntent.node_labels`
and optional `node_labels=` to `TransactionContext.stage_row_insert` and
`stage_row_update` (including the private proved-encoding insertion companion).
`None` preserves the implicit/inheritance distinction; `()` is an explicit
replacement, never a missing update. The common reducer folds pending updates
into one INSERT and stored updates into one final UPDATE, retaining the last
explicit label replacement across later property-only updates. DELETE cancels
a pending insertion or ends the stored node; it cannot carry replacement labels.

Canonical node metadata and working-table candidates are checked at staging even
without a byte budget. With `max_transaction_bytes`, each raw intent's explicitly
retained GXL1 metadata is charged together with its encoded property payload.
Inherited metadata not supplied in a later property-only intent is not charged
again as a separately retained label tuple. Statement discard restores the
accounting; hostile mutation is revalidated before commit. Property encoding
proofs still cannot authorize or hide label bytes.

The private query-engine admission helper uses the existing outer-statement
schema journal. Already-admitted candidates do not stage catalog pages again.
The native transaction manager validates the whole effective catalog from
committed state or exactly proved staged page images before entering the commit
window, and repeats the effective-authority check before materialization. The
heap receives an ephemeral, participant-local write-only schema view; it neither
adopts nor publishes a pending catalog to readers. All four canonical heap
write doors retain metadata. An uncertified custom heap is refused rather than
silently omitting it. Only the ordinary COMMIT publishes the capability and
versions; both OCC baselines, WAL barriers and rollback remain in force.

Public `TableDef` snapshots now preserve `extra_node_labels`. Candidate-only
growth does not masquerade as nullable-column/schema-layout drift; true physical
schema mismatches remain refusals. These changes do **not** complete query
membership, pending-row overlays, detached entity labels or the Cypher grammar.

At this transaction-foundation checkpoint, retained-history integration was still unfinished. Explicit label replacements
for history-enabled tables, and history activation over already explicitly
labeled versions (including `()`), raised `GrafxUnsupportedOperation`
with `field="system_time_history"`. This is a temporary non-loss boundary, not a
new scope exclusion. The [native retained-history integration](#native-retained-history-integration)
below replaces these temporary refusals. The original receipts in this section
qualify only their original guarded source checkpoint.

Focused source evidence: **67 passed**, zero failures/errors/skips, 5.260 s,
`.grafx-tmp/fp-label-transaction-complete-focused.xml`, SHA-256
`8b32fad7b062d60f6da52bfc82efdcc060b244b79285f4d53d86757d96cd27d5`.
The tests cover actual transaction commits/reopen/verification, independent
reader snapshots, reduction, exact quotas, schema journal unwind, candidate
forgery, custom-heap refusal, WAL append failure, first activation with a small
buffer and subsequent successful use, nullable schema and temporary history
refusals. See `tests/txn/test_node_label_intents.py`. They do not qualify complete
Cypher label operators, installed old binaries or process-kill recovery of this
new format.

The final grouped transactional regression passed **719 tests**, zero
failures/errors/skips, 210.218 s,
`.grafx-tmp/fp-label-transaction-grouped-corrective.xml`, SHA-256
`81c4daa904ae1128750140dbc907259f5bac2f1a4f7ac8370771a4f5942efeab`.
It adds primary-key storage coverage and includes row staging/reduction,
snapshots/OCC, native label storage/replay, DDL rollback, budgets, flexible
queries, existing native/adversarial/flexible history (including its process-cut
tests), API documentation, annotations and import boundaries. The process-cut
history cases exercise the existing implicit-label history implementation, not
the unfinished explicit-label history bridge.

The preceding 716-case group had two architectural failures because an initial
implementation imported `contextvars` into the pure engine. That dependency was
removed; the import policy was not relaxed. The ephemeral write schema now lives
only in the participant's already-protected materialization section, rejects
nested replacement, is cleared on failure/success, and never supplies read
authority. A 373-pass focused boundary correction preceded the final grouped run
(SHA-256 `089a517b7c9344604d40c109aacb5d0bdaf353150e7c5ec21112e72fb284866b`).
These overlapping runs are not added together. Documentation/API generation,
changed-source Ruff and `git diff --check` also pass.

### Preceding storage foundation evidence

Source tests are `tests/storage_core/test_node_labels.py` and
`tests/storage_core/test_node_label_storage.py`. They cover independent literal
wire expectations, UTF-8/size boundaries, all native insertion doors, explicit
empty membership, property-only preservation, snapshots and version identity,
overflow, nullable-column expansion, cold readback, forged authority, projected
corruption and committed catalog transitions. Synthetic committed WAL containing
real heap/catalog pages also exercises COMMIT-required replay and idempotence;
this is **not** a process-kill or installed-wheel qualification.

The initial affected-storage regression recorded **518 passes**, zero
failures/errors/skips, 34.441 s, `.grafx-tmp/fp-multilabel-storage-regression.xml`,
SHA-256 `655c0daa14a763742e1ee1289d6ec8dc62a731833755c3079c1e0ef1e14bf503`.
It predates the subsequent cached-candidate/authority and heap-replay tests.
The earlier 72-case first run had one test-fixture method-name failure; it was
corrected and is retained as a failed receipt, not counted as a pass.

Final foundation selection: **3,168 passes**, zero failures/errors/skips,
108.879 s, `.grafx-tmp/fp-multilabel-storage-grouped.xml`, SHA-256
`734b9557f5632203d56ff60f4e5f0084dac7553ae02bac93f0b9d2963b002c1c`.
It includes the entire storage-core suite, row staging/reduction, snapshots/OCC,
native ANY/implicit-label/unlabeled/flexible-relationship queries, annotation,
consumer documentation and import-boundary tests. The subsets overlap and must
not be added to this total. Reproduction from the repository root:

```powershell
$env:PYTHONPATH='src;.grafx-tmp/v006-optional-test-deps'
python -m pytest tests/storage_core tests/txn/test_row_staging.py tests/txn/test_pending_row_intents.py tests/txn/test_snapshot.py tests/txn/test_occ.py tests/query/test_any_properties.py tests/query/test_implicit_node_labels.py tests/query/test_unlabeled_nodes.py tests/query/test_flexible_relationships.py tests/foundation/test_fp_annotation_contracts.py tests/consumer/test_documentation.py tests/test_import_boundary.py -q --tb=short --junitxml=path/to/fresh-label-storage.xml
```

The new heap/catalog replay matrix includes **12 synthetic replay scenarios**
(three label states × inline/overflow × plain/compressed WAL). A focused 67-case
storage run passed, SHA-256
`6efae086eabc4c1a7aba279b6fe6477fd4ad78f054d0c843417ff7ac9b0be6f6`,
`.grafx-tmp/fp-multilabel-heap-replay-corrective.xml`. Its predecessor failed on
a missing required test-helper argument, which was corrected. The preceding
authority selection initially exposed four unknown-table error-taxonomy failures
and two invalid initial-extent test fixtures; all are corrected. Its 128-case
corrective selection passed, SHA-256
`cf746c4f5569c9a4a8b33bb33c639953eeeda4b7880cb46a2c24bd93ea416cc7`.
The final grouped selection includes these corrections. Unknown-reader refusal
here uses a source known-capability-mask test; actual installed historical
binaries for bit 28 remain part of the later package checkpoint.

Set1 was reexecuted on this source and retains **10 passes / one explicit
divergence**, no selected required-case failures; exit code 1 preserves the
upstream failure honestly. `.grafx-tmp/fp-multilabel-set1-native.json` has the
same SHA-256 as the preceding V3 observation:
`f74fc851764712979d602af678f4372df76d6393bb3ec8e9f84f44cddb90cc79`.
Generated API documentation, documentation checks, changed-source Ruff and
`git diff --check` also pass. No complete current-source TCK claim is made.

The remaining work is the already-authorized scope, not additional delivery
criteria: completing native CREATE/MERGE and broader query composition; retained
history and copy/transfer preservation; final rollback/fault/participant regressions;
installed old/new reader qualification; the full V3/supplemental profile and
paired Pulse adapter/API/UI acceptance. The earlier temporal/decimal/collection
wheel checkpoint does **not** qualify this newly added bit or payload layout.

No release, global installation or production-store change is part of this
foundation. Published `0.0.5` behavior and historical TCK receipts are unchanged.

## Query-label mutation checkpoint

The next source checkpoint adds `LabelSetItem` / `LabelAssignment` and executes
SET/REMOVE label items in the same row-major clause as property assignments.
Owner-private held rows now carry metadata into the existing journaled intent
door. NULL and idempotent operations do not stage a label version. Explicit empty
sets survive later property-only writes. Canonical label admission is checked
before staging, including schema permission for procedure-introduced candidates.

`labels()` and label predicates consult a reduced owner overlay once per
table/revision, with exact pending-token or stored-reference identities. They
never infer membership from conservative catalog candidates. Standalone MATCH
spans candidate physical owners and keeps exact version checks after index/PK
lookup. Edge traversal keeps physical endpoint constraints and applies logical
label predicates to landings before OPTIONAL null extension. For activated label
catalogs, physical-name-only typed-hop shortcuts currently use general native
traversal; restoring equivalent fast paths is remaining implementation/qualification,
not permission to omit membership predicates.

Spilled entity bindings preserve label metadata and validate canonical values
against the owning table before restoration. Pending-version, comprehension,
landing-result and fingerprint accounting include retained label payloads.
Detached `NodeValue.labels` and additive JSON `labels` carry the complete set;
the singular DTO `label` is the first canonical label (or None), whereas the
existing `label(n)` query extension still describes its physical owner.
Node/edge qualified identity and equality are unchanged. New capabilities and
usage are documented in [query language](../QUERY_LANGUAGE.md#versioned-label-mutations-partial-development-checkpoint)
and [entity values](../ENTITY_VALUES.md); no connection setting is introduced.

Qualification source: `tests/query/test_node_label_assignments.py`, plus grouped
AST/planner, entity, procedure, native transaction, optional traversal and boundary
regressions. The corrected group passes **1,110 tests**, zero failures/errors/skips,
163.169 s, `.grafx-tmp/fp-label-assignments-grouped-corrective.xml`, SHA-256
`743ee2bfcc9e3e558a430c5f909a8a6e6d131b9bd4e342052c125d365335c93a`.
Two historical tests expecting node-label rejection are updated
for the explicitly authorized membership model; an unintended lost empty-binding
label hint is corrected in the analyzer. No pinned upstream expectation changes.

The preceding group has 987 cases / three failures, 106.004 s,
`.grafx-tmp/fp-label-assignments-grouped-first.xml`, SHA-256
`f609d0177c0495a76b892b2e251d34a17d9459d2c4f2dfcdeaf8aeb6f0ac965e`.
Initial focused runs also exposed omitted assignment-union publication and
expression-root visitors; both are corrected, preserving the closed public-plan
grammar instead of accepting arbitrary dataclasses. An invalid test-file selection
produced no qualification result and was replaced with the actual entity suites.
Intermediate passing selections (7 and 13 cases) are not added to grouped totals.

The final follow-up covers an additional malformed direct-AST REMOVE guard and
read-only spill lookup reuse: when no owner label replacement exists, reading a
spilled snapshot's already validated metadata does not reacquire heap authority.
The follow-up passes **189 tests**, zero failures/errors/skips, 29.517 s,
`.grafx-tmp/fp-label-assignments-final-read-boundaries.xml`, SHA-256
`43bb5a0b66713d508beccc6a3b3e24b1846a832f1870d60b62747c4d953240d6`.
It is not a second full regression or a performance claim. A further explicit
negative-AST/no-heap-lookup guard selection initially found a missing method in
the narrow test context; the mock was corrected without changing production code.
The corrected guard selection passes **64 tests**, zero failures/errors/skips,
21.507 s, `.grafx-tmp/fp-label-assignments-final-guards-corrective.xml`, SHA-256
`27838bb4594432cee3446750112885087552cda9e29a318cbe1381605c6fe797`.
These overlapping selections are not summed. Documentation checks (39 configuration
fields, public signatures/DTOs and 11 preserved source plans), changed-source Ruff
and whitespace validation pass. No installed Pulse or release artifact was changed.

Set1 reexecution on this query checkpoint remains **10 passes / one failed
upstream oracle**, `.grafx-tmp/fp-label-assignments-set1-native.json`, SHA-256
`948c0ce8e8925b5d347be9b1cf1c0214b09c3516a873318d0b5e0d0c00340855`.
The failed expectation is the authorized Set1 #0010 list-of-maps divergence;
exit 1 is retained, not reported as upstream conformance. This report's complete
source parse inventory also reflects newly accepted label syntax; historical
reports and profile ledgers are unchanged.

This is not a new full-TCK result. The native TCK state observer also still needs
version-label integration before label side-effect qualification. Native full
CREATE/MERGE, historical/copy/transfer labels, installed old/new reader checks,
complete V3/supplemental execution and paired Pulse acceptance remain unfinished.

## Query CREATE MERGE and scan checkpoint

This subsequent development increment implements native multi-label written
patterns and the independent TCK state observer. It supersedes those two pending
items at the preceding checkpoint; its old receipts remain historical evidence.

`CreatedNode.labels` retains the written tuple. The first written name selects a
physical owner if it is a valid physical identifier. Existing typed columns/PKs
remain constraints of that owner, not of every secondary label. New valid names
use ordinary flexible-table creation. A first logical name outside the physical
identifier grammar uses the generated unlabeled flexible owner, with every
logical name preserved in native metadata. Duplicate names normalize to one
membership. `CREATE (:A:B)` and `CREATE (:B:A)` need not have the same physical
owner, but both label sets are the unordered conjunction `{A, B}`. They never
represent duplicate copies of a single node. Existing legacy catalogs retain
explicit `maintenance.ensure_identity_indexes()` activation before this v2
capability; refusal now names that remedy instead of an unrelated index error.

Single-node MERGE resolves candidate tables from the owner-visible native catalog,
then checks the current per-version set and every supplied property. Additional
labels are allowed. Missing typed properties disqualify that physical candidate;
they do not prevent a match in a different owner. No match means the normal
creation path with the selected owner's constraints; a match elsewhere does not
install the unused prospective table. Property expressions and matching bindings
are fixed before ON MATCH. Retained bindings are charged to the existing
`query_memory_budget_bytes` limit. Earlier held insertions may enter private
materialization phases for stable identity; they do not create inner commits or
escape the statement rollback mark. No read/write, OCC or durability premise is
weakened, and this checkpoint makes no new throughput claim.

Nested planners inherit prospective label candidates for CALL, EXISTS, pattern
predicates/comprehensions, UNION and whole-path MERGE. Subsequent native reads
still check actual node membership. When a later node admits another label on
the same physical owner, an earlier endpoint binding remains valid: endpoint
admission ignores only the changed candidate summary, not table ID, physical
schema, row-version identity or pending-reference ownership.

`ScanRowV1.node_labels` is an additive immutable API field:

| Value | Meaning |
|---|---|
| `None` on a node | Legacy implicit membership: physical table name, unless `TableDef.unlabeled` |
| `()` | Explicit empty logical label set |
| Canonical tuple | Complete exact logical membership, not additions to the physical name |
| `None` on a relationship | Relationships have a type, never a node label set |

Projected and paginated scans retain the metadata under their fixed snapshot,
including a projection of zero columns. Scan admission rejects noncanonical or
unadmitted metadata. Consumers must resolve **physical** table names for this raw
door; logical relationship groups are not individual heap tables. The TCK state
observer uses these rows, not the query executor's count/statistics, to compare
persisted labels and effects before/after writes and after reopen. Detached query
results are compared separately through independent reference values.

### Qualification of CREATE MERGE and scans

The original FP-4 selection now has **289 passed / one failed / 3,607 outside
selection**. All 289 required selected cases pass. The one failure remains exactly
`clauses/set/Set1.feature#0010`, the authorized list-of-maps divergence; the runner
retains exit **1** and the original expected TypeError. No original query, oracle
or V3 ledger was rewritten to obtain a pass.

Receipt `.grafx-tmp/fp-multilabel-create-merge-fp4-native.json`, SHA-256
`853058cac869e4cd78c5c9b2af01eba82839c6f11f0b548a44f22c077cc1c302`.
The earlier independent Set3 family has **8 passes**:
`.grafx-tmp/fp-multilabel-set3-native.json`, SHA-256
`c9a33ba3c47be83f870447babbbe51e3032c4add285e623d819607ac29da6be0`.
Both use the pinned 2024.3 source and the verified V3/V2/V1 ledger chain.

Initial focused CREATE/MERGE/label-action/general-MERGE coverage has **88 passes**,
40.597 s, `.grafx-tmp/fp-multilabel-merge-first.xml`, SHA-256
`601c788ded555850b0ae3eac5d4f102d4013e7a7a0bfe0d539157c87b26a337c`.
The expanded scan/oracle selection first had **192 passes / three failures**,
`.grafx-tmp/fp-multilabel-scan-oracles.xml`, SHA-256
`4da55e4a8b069568a3f601c35cd6a6d24625f1f7d10c2d9f059553a9a6691c46`:
two typed fixtures lacked required v2 activation; one exposed the real endpoint
candidate-summary mismatch corrected above. The next run had **199 passes / one
failure**, `.grafx-tmp/fp-multilabel-scan-oracles-corrective.xml`, SHA-256
`71f917d3d2dca17e84c5669c5a085b02bb4331dde86efcac2aaac593a8523185`.
That last test incorrectly used a logical relationship name for a physical scan;
it now resolves the native physical member without changing the API contract.
These failed runs are not reported as passing regressions.

The grouped query/transaction/API regression completed with the six failures
recorded below; a green final package regression is still required. Retained-history labels,
copy/transfer preservation, installed old-reader refusal, complete package and
paired Pulse acceptance remain the already-authorized unfinished delivery.

### Full query-profile execution and corrective verification

The complete V3 native execution terminated with **3,895 passed / two failed /
zero not run**, exit 1. Receipt `.grafx-tmp/fp-multilabel-query-full-native.json`,
SHA-256 `87f6faec6ab49d8c6cde93babf248b81a4794d6d95d35584dcbafa6edd4ef311`.
One failure is exactly the authorized Set1 #0010 divergence. The only required
failure was `expressions/graph/Graph3.feature#0009`: `labels(list[1])` on a
heterogeneous list was rejected during planning instead of execution.
The planner now retains the joined dynamic list element type for entity scalar
admission; it still validates subscript shapes and proven direct invalid arguments.
All nine unchanged Graph3 cases pass on the correction, followed by **61 passes**
for the complete original graph-expression family. Receipts:
`.grafx-tmp/fp-graph3-phase-native.json`, SHA-256
`bb584b943b9c90276b4936d38829d784bce3c7e40e280d280c2aea300d395853`,
and `.grafx-tmp/fp-entity-graph-family-qualified.json`, SHA-256
`6d338ac26afe1f8d385e62aa7ca8080e16a1b2e37d024860980fe31e740ccd63`.
The related native regression has **131 passes**, zero failures/errors/skips,
37.815 s, `.grafx-tmp/fp-graph3-phase-qualified.xml`, SHA-256
`3a54e3bf9b7a61354d713e73bf18a698e276fae9347b33ae0869673fb221fda0`.
It covers direct/literal/dynamic selections, all six entity functions, empty
streams, invalid shapes and late-error statement rollback. The initial attempted
test command named a nonexistent test file and collected no tests; that receipt
(`fp-graph3-phase-corrective.xml`) is not a validation result. This is a corrective
family result, **not** a relabeling of the preceding complete run as green.
Full current-source acceptance remains required at the final package checkpoint.

Two stale regression expectations rejected now-authorized multiple-label reads
and correlated rematches. Their replacements assert actual empty/NULL results,
unchanged catalog tables and exact correlated label-filtered rows. The corrected
affected collection has **141 passes**, zero failures/errors/skips, 43.093 s:
`.grafx-tmp/fp-multilabel-query-corrective-qualified.xml`, SHA-256
`ea5cf9306a64032c67db41b2bb5d8f9ea5d9c92aeff7b8c94d39e7697fc40a04`.
An additional optional-planner expectation was similarly obsolete. Two projection
fault probes needed to mutate the bytes, not truncate the new internal
`(payload, labels)` return pair. Corrupt projected/full payloads must still yield
the same `GrafxCorruptionDetected`; no error assertion or byte validation is waived.
That corrected optional/ordered-page/parser collection passes **231 tests**:
`.grafx-tmp/fp-multilabel-query-adjacent-corrective.xml`, SHA-256
`af50518f000d18577f8ca4374838acd259d81cab9f410a64f749aa2f6758da88`.
The original grouped job terminated with **5,911 passes / six failures**, zero
errors/skips, 1,691.464 s. Receipt `.grafx-tmp/fp-multilabel-query-grouped.xml`,
SHA-256 `fd0f15da34f431dfeb927cad5874532bb37146b4a70991898bd5cf120247a59e`.
Five failures are the stale expectations/fault-hook mismatches just described;
the sixth is the corresponding obsolete multiple-label one-hop planner refusal
in `test_untyped_relationship.py`. The updated test admits the valid conjunction
through the same canonical planner as the other supported forms. No required
TCK case becomes a divergence. The grouped job retains its collected earlier
tests and failed status; its replacement all-cause corrective selection is in
the completed receipt below, not a new full-suite result.

The final combined corrective collection has **392 passes**, zero failures,
errors or skips, 80.766 s, terminal exit 0:
`.grafx-tmp/fp-multilabel-checkpoint-corrective-final.xml`, SHA-256
`90ae2d9cd91aa8139a7aafefbaa99ae571b971e9a9f952449fb95966d8f9a147`.
It covers every grouped-failure file, current Graph3 phase behavior, multi-label
CREATE/MERGE, projected/snapshot raw scans and the new/preceding historical codecs.
Earlier collections overlap; their counts must not be added. All checkpoint jobs
have terminated. Current documentation links/anchors, 39 configuration fields,
public annotations/DTO signatures, the 11 preserved plans, changed-file Ruff and
`git diff --check` pass. No commit, push, package installation, production-data
operation or release was performed by this checkpoint.

### Historical codec follow-up — not activated

The next implementation step adds `HistoryChange.node_labels` and the bounded
`GXHM03` historical-schema trailer. It preserves historical table candidate sets
and distinguishes implicit membership from explicit empty/nonempty sets. An
explicit row's payload uses the same canonical GXL1 prefix as native heap rows.
Old frames without this metadata keep their exact encoding. Catalog validation
requires bit 28 and admitted historical/current candidates; no current label set
is substituted for missing historical metadata. Unknown flags, malformed framing,
noncanonical/unadmitted names and node labels on relationships are refused.
Delete, schema-only and redacted events cannot carry row label payloads.
Existing schema/row/batch byte ceilings still include the added bytes.

The independent byte/oracle and preceding flexible-history codec selection has
**40 passes**, zero failures/errors/skips, 0.355 s:
`.grafx-tmp/fp-node-label-history-codec-qualified.xml`, SHA-256
`624c9d6636719d6a0ac45c1af057be3d461699b0fe2272a753ea4fad13d80223`.
Two preceding fixture runs failed because the new test used the wrong integer
enum name; both receipts (`fp-node-label-history-codec.xml` and
`fp-node-label-history-codec-corrective.xml`) remain failed, not byte-codec evidence.

This is codec support only. The preceding query/full-profile jobs
started **before** this history codec follow-up and do not qualify it. Its separate
affected history regression has **172 passes**, zero failures/errors/skips,
136.865 s, `.grafx-tmp/fp-node-label-history-codec-regression.xml`, SHA-256
`beead9364949a66034edf6b4436837ffa92234ce269799eb5401b24bd7fb13a1`.
It includes native history activation/reopen/fault tests and the still-required
label-history refusal guards. Native publication, temporal DTOs,
scan/index readers, diff, retention/redaction, recovery consistency and consumer
bridges had to carry these fields before enabling label history. At this codec-only
checkpoint the two native history refusal guards deliberately remained in place;
the following integration supersedes that implementation state, not its receipts.

## Native retained-history integration

Native publication now captures actual heap membership in activation baselines and
row events, including explicit empty sets. Property-only updates inherit the
previous row's native metadata; the extra header read is restricted to
history-enabled node updates without a replacement. Effective after-COMMIT
candidate metadata, rather than a stale intent schema, owns each event. The two
temporary history refusal guards are removed. No new setting, transaction door,
OCC baseline, retention horizon or durability policy is introduced.

The native scan and temporal index preserve membership in
`TemporalVersion.node_labels`; the [public history contract](../SYSTEM_TIME_HISTORY.md#historical-node-labels)
defines None/empty/nonempty states and historical schema resolution. Candidate
growth is append-only and can retain the physical schema version; nullable column
growth still advances it. Label-only diff returns `updated` with no property
changes and explicit `labels_added`/`labels_removed`, each label charged to the
existing change budget. Endpoint record identities do not change with labels.
Verification compares native current and historical membership as well as values.

Retention erases the complete expired property/membership payload. A strict
`redacted_node_labels` framing witness is allowed only on redacted node events;
it carries no membership and retains GXHM03/flag bit 3 even when both the erased
set and candidate trailer were empty. This preserves the original schema/event
size during non-compacting retention. Redacted byte accounting includes GXL1;
compaction can remove zero padding without reintroducing labels. Historical schema
candidate names remain metadata, not an erasure promise. Existing byte ceilings,
native capability validation and WAL-before-pages apply to all of these bytes.

Initial native bridge coverage: **108 passes**, zero failures/errors/skips,
19.408 s, `.grafx-tmp/fp-native-label-history-first.xml`, SHA-256
`560b7f5b37c6d40e4ed39f4af6c676137e26d6a6b45827639a34c26ac3eb4c96`.
The retention framing/extent selection has **95 passes**, 16.866 s,
`.grafx-tmp/fp-native-label-history-retention.xml`, SHA-256
`32f8d09da9f367ff1f3b10ace19154d0d3c250ef12fa6b007527e9178ddafabc`.
The affected native/index/retention/compaction/adversarial/flexible-history,
diff and intent/codec regression has **238 passes**, zero failures/errors/skips,
285.116 s, `.grafx-tmp/fp-native-label-history-grouped.xml`, SHA-256
`d8d5d58e58339656c1e44d4c0dcaaf3fcef568aa00c7145b68310de2c5fce810`.
It includes 12 actual process cuts for label+property writes across pure/NumPy,
before COMMIT and at partial/current/history publication boundaries, with two
reopens and native scan/index/current/history verification. Three subsequent
verification-disagreement/pre-WAL-failure/physical-backup tests pass separately,
3.707 s, `.grafx-tmp/fp-native-label-history-verification-fault.xml`, SHA-256
`f29f722f11d9bbefd95577821b006576187a692b024baf41d10e4277b6aceba5`.
The final label-history feature file has **41 passes**, zero failures/errors/skips,
114.425 s, `.grafx-tmp/fp-native-label-history-final-focused.xml`, SHA-256
`1e586b0452851fb6ecf9f7d474f84d260415556c139a7ca2fafeb69d0ca266c6`.
It includes all subsequent verification fixes, independent handles and a writer
staged before history activation, nullable schema/candidate growth with stable edge
endpoints, exact Unicode labels on an unlabeled store with lists of maps, and six
additional activation/prune process cuts. The separate six-case participant/schema
selection passed in 5.266 s, `.grafx-tmp/fp-native-label-history-participants-schema.xml`,
SHA-256 `c9ab50f331ca5c1c6d1d6be3e21a9f1043c4452ff713915392c21f75fc920a37`.
These overlapping collections are not additive. They qualify this native-history
source integration, not copy/transfer, installed old binaries, final whole-profile
or paired Pulse acceptance. No commit, release or production store operation is
implied by these receipts.

## Existing-target copy integration

`capture_copy` now carries each native row's optional GXL1 membership prefix from
both raw scans and exact identity-index landings. The opaque `CopyTable.rows`
bytes remain source-record-qualified, but are no longer universally bare property
tuples. A v4 package digest binds complete row frames and candidate schema trailers;
packages with neither retain their earlier v1/v2/v3 hashes. Admission rejects
malformed/noncanonical/unadmitted labels and any membership on relationships,
independently of a caller-recomputed checksum. Complete frame bytes count against
the existing per-row and aggregate quotas. The package is not authenticated.

Application uses native creation/label operators and native target bindings in
one receipt transaction. The target keeps its physical table/column constraints;
needed candidate admission for inserted nodes shares their WAL COMMIT. Empty sets
on named owners remain logically empty. A target may use implicit representation
for a semantically identical singleton or unlabeled-empty set: logical copy is
not a physical page clone. Unused source candidates need not be installed.
Skipped PK nodes keep all target labels/properties; native exact lookup addresses
the physical PK even when its base label was removed or another table shares it.
This also applies to old implicit-only packages targeting a label-capable store.
Endpoints map to these actual target identities. No label is fabricated from a
source record ID, and no detached value grants mutation authority.

Current-only source history policy remains explicit. Enabled target history
records new target labels at its own copy/receipt commit, never source intervals.
Application rows on the native path share statement/intermediate-row bounds as
well as copy and transaction quotas. The public
[copy guide](../CATALOG_COPY.md#native-node-label-membership) specifies preparation,
framing, conflict, idempotency and schema-candidate behavior; receipt format is v1.

The first label-copy selection has **17 passes**, zero failures/errors/skips,
32.914 s, `.grafx-tmp/fp-node-label-copy-first.xml`, SHA-256
`9d510df85dce898756a967f47f9ceccc767aa6b7acc073c5f7e71fa109fa024c`.
The existing copy/group/flexible/namespace/endpoint-closure regression has
**77 passes**, zero failures/errors/skips, 154.647 s,
`.grafx-tmp/fp-node-label-copy-existing.xml`, SHA-256
`b1085633d35cddd13d408526fb8577bf61d14605ea864eb759f5a84ee2e351e9`.
The subsequent expanded selection found two fixture-provenance failures: those
fixtures enabled the commit journal after their last source data write and thus
correctly failed the existing tracked-source requirement. Fixtures now enable
provenance first; the API refusal is not weakened. The original failed receipt
`.grafx-tmp/fp-node-label-copy-qualified.xml` has 23 passes/two fixture failures,
62.968 s, SHA-256
`2b4e412cb3a4a9c53808d69437aca7c403c4a4e20c36fcaab0aff4d4328f4148`,
and remains failed. The corrected copy/label-history-codec/public-annotation/
documentation/import-boundary collection has **421 passes**, zero
failures/errors/skips, 79.198 s,
`.grafx-tmp/fp-node-label-copy-history-contract-corrective.xml`, SHA-256
`285c3f40e16e35c66bef41dbe4e65c707524342f17add62ef9d79f7cb0e93c7c`.
Its 25 label-copy cases include full/subset capture, independent reader snapshots,
target historical scan/index readback, skip with removed/shared base labels,
flexible owner remapping, budgets, malformed input, late schema/row rollback and
six actual process cuts across pure/NumPy with durable receipt replay and reopen.
Collections overlap and must not be summed. Logical fresh-store transfer, installed-wheel old-reader refusal
and final profile/Pulse acceptance remain separate pending requirements.

## Fresh-store transfer format 4

The native bridge uses `okto-grafx-logical-4` whenever the source catalog requires
`node_labels_v1`, taking precedence over formats 1/2/3. Earlier formats retain
their previous encoding and may not smuggle label metadata. Format 4 requires
`extra_node_labels` as a canonical JSON array on every node schema, absent on
relationships, and explicit `table_kind` on every custom index. Every object
descriptor adds a strict boolean `node_labels`, true exactly for node tables.

Inside the existing `<uint64 record_id, uint32 payload_length>` envelope, format-4
node payloads start with a single presence byte: 0 followed by the ordinary value
tuple means native implicit membership; 1 followed by one complete canonical GXL1
prefix and then the value tuple means explicit membership, including empty.
Other flags, truncation, noncanonical/unadmitted sets and trailing bytes refuse.
Relationship payloads retain their two unsigned endpoint IDs and value tuple;
they never gain a node-membership prefix. Object digests and all existing byte
limits include the presence byte/prefix. Old format readers refuse the manifest
before any destination or resume workspace is created.

Native schema installation admits all captured candidate metadata and bit 28
through its existing DDL transaction, including explicit empty membership with no
extra candidates. Private row batches carry membership in native intents. Final
readback and resumable prefix validation compare membership **and** properties;
matching values alone cannot prove that a prior batch belongs to this artifact.
Fresh target UUID/record mapping, independent source snapshots, WAL recovery,
private staging, locks, current-only history policy and final atomic directory
promotion remain unchanged. This format was specified before implementation.
The [public guide](../LOGICAL_TRANSFER.md#node-label-artifact-format-4) documents
consumption, byte framing, concurrency, limits and exact resume/readback semantics.

The previous-format transfer/resume/flexible/namespace regression has **67 passes**,
zero failures/errors/skips, 148.756 s,
`.grafx-tmp/fp-node-label-transfer-existing.xml`, SHA-256
`31e26181cca70b20ed260d78f6d72e4dbd15b01df3aa9f7ca04deba9b7eaf384`.
The first new-feature selection recorded 27 passes and 12 fixture failures because
the test used the native catalog capability accessor on a public CatalogView;
the implementation's row checks had passed, but that run was not complete.
`.grafx-tmp/fp-node-label-transfer-first.xml`, 42.375 s, SHA-256
`f8c59da0a148143283753074e8c8fe1f2ee99d5ca89c9c8ff5bf306a4e880e55`
remains failed. Corrected tests have **45 passes**, 52.152 s,
`.grafx-tmp/fp-node-label-transfer-corrective.xml`, SHA-256
`bb32dd22bf02a5dceefc4fe1009fa275d749b35ae194a1579889bf8708261cec`.

The grouped transfer/DECIMAL/temporal/typed-collection/consumer/verifier selection
has **236 passes**, zero failures/errors/skips, 324.607 s,
`.grafx-tmp/fp-node-label-transfer-consumer-grouped.xml`, SHA-256
`5be5f06da9cf144206a36dcf2b3153ad7198fbaaee37f9fd45811574384224e5`.
It includes all 58 new label-transfer cases: pure/NumPy, typed/flexible/overlapping
namespaces, one-row batches, candidate/row tampering, exact presence bytes,
independent writer interleaving, membership-only readback/resume disagreement,
schema rollback, byte limits and ten real process cuts across WAL/batch/promotion
boundaries with repeat reopen and lost-ACK retry. Overlapping counts are not additive.
These source tests do not certify archived installed binaries, the final full TCK
profile, all supplementals or paired Pulse; those requirements remain active.
