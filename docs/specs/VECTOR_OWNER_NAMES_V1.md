# Durable vector owner names — catalog capability 25

September 12, 2026, 0.0.6 development. Completes the persisted-name part of the
[physical ownership repair](VECTOR_PHYSICAL_OWNERS_V1.md); it does not certify
the entire functional-parity plan or the installed Pulse.

## Selection and immutable identity

Existing unambiguous vector indexes keep `vector_{table.name}_{space.name}`.
When adding a table would collide with an active index's case-insensitive key,
or the derived name exceeds the identifier grammar/budget, that **new table**
uses `_grafx_vec_t{table_id}_p{column_position}` for all its vector columns.
Physical table IDs distinguish node/relationship namespaces; positions include
relationship endpoint columns. No existing file is renamed, no entry is copied
from a sibling, and no retired index is treated as current merely by spelling.

The choice is persisted as exact-boolean `TableDef.vector_identity_names`.
It survives detached catalog views, catalog copy, reopen, historical schemas and
append-only nullable-column evolution. Column positions and physical table IDs
remain unchanged by a nullable append. This is a format observation, not a
connection setting or public in-place naming toggle. Logical transfer/copy
reconstructs target schema and derives target names against target identities;
the source's physical naming policy is not a portable logical property.

New custom index definitions cannot use the `_grafx_vec_` prefix. An existing
legacy custom index is not deleted or renamed; if it occupies a required physical
name, new-table admission refuses before catalog publication. Operators must
resolve that legacy name conflict explicitly. Multiple vector columns within one
table/space still retain their existing ambiguity restriction.

## Exact format and admission

Required catalog-v2 capability: `vector_owner_names_v1`, bit **25** (`1 << 25`).
The capability is activated transactionally with the new flagged table and is
one-way. Explicit catalog-v2/identity-index activation is required before such a
table can be admitted; unambiguous legacy DDL retains its existing behavior.

When bit 25 is present, each table body has one additional byte after its ordinary
table encoding and any existing nullable-layout/flexible-model extensions:

- `0`: legacy spelling-derived vector names;
- `1`: table-ID/column-position-derived vector names.

Any other byte refuses as corruption. A flagged table requires at least one vector
column. Serialization refuses flagged tables without their capability. Binaries
without bit 25 reject the catalog before serving it; a read-only pending-WAL open
may instead refuse checkpoint consistency before recovery. No unsupported bit is
masked off and no missing format authority is inferred from files on disk.

Automatic definition projection, DDL attachment and reopen adoption share the
same naming function. An obsolete vector name cannot pass the generic custom-
index positional fallback in `index_definition_matches_table`. Every adopted
artifact retains its exact table/column/space definition, digest, generation,
freshness and WAL/recovery checks.

## History and recovery

Historical schema metadata uses `GXHM02` with flag bit 2 set when identity naming
is present; legacy `GXHM01` bytes remain unchanged otherwise. Decode requires
canonical re-encoding, valid flags and exact framing. Historical model validation
compares the naming flag with catalog authority; losing the flag is corruption,
not a reason to infer it from current names. Catalog replay also refuses a change
to an established table's naming model, like its property/label model. No current
path toggles the policy on an existing table.

The normal schema/registry journal owns speculative files and observations.
Before COMMIT, a failed statement/transaction leaves no committed capability or
table. After proven COMMIT, recovery applies the complete catalog/data/index
effects. The earlier owner's identity is preserved in either outcome. Physical
backup retains the flag and artifact files; ordinary read-only open never repairs
or rebuilds missing legacy artifacts silently.

## Qualification and remaining scope

Source tests cover same-named node/relationship tables in both creation orders,
pure/NumPy search/reopen/verify, delimiter collisions without graph-name overlap,
catalog roundtrip/copy, capability/flag refusals, old-name provenance refusal,
reserved custom names, historical schema flags and process cuts during capability
activation. The installed-wheel verifier's `--capability vector_owners` mode starts
with a vector index written by an actual archived wheel, preserves that name,
adds a colliding owner in the current wheel and audits old reader/writer refusals
in materialized, pending-WAL and already-open-handle states. It hashes **all**
files around refused operations, without control/WAL exclusions.

The subsequent [public diagnostic/rebuild selector](VECTOR_QUALIFIED_MAINTENANCE_V1.md)
is implemented separately. [Component and missing-artifact qualification](../reports/VECTOR_OWNER_REPAIR_QUALIFICATION.md)
subsequently tests selected mutations and explicit repair of genuinely missing
legacy indexes, distinct from adoption of an intact old artifact. Full package,
Pulse and frozen-profile acceptance remain open; this does not authorize blind
adoption of conflicting or corrupt artifacts.

## Recorded evidence

The affected regression passed **185 tests**, including twelve pre-/post-COMMIT
process cuts, both codecs, catalog admission, history, namespace transport and
DDL rollback. Receipt `.grafx-tmp/vector-owner-names-combined.xml` (151.686 s),
SHA-256 `b6881a0088aee1fc60320573a73d3aec2f6439e91e5c25dcbfabb87c58ce0272`.
The subsequent API suite passed **20 tests** (21.526 s), including long names,
exact-boolean validation and four copy/transfer cases with different source/target
physical naming policies. Receipt `.grafx-tmp/vector-owner-names-transport-qualified.xml`,
SHA-256 `1475d049a6daac9c9cca8a1b45f48dde6ad8a7e912a71ecb6364e1414a9c8d00`.
These suites overlap; their counts are not additive full-repository coverage.

Installed-wheel qualification passed **24/24 cases**: archived 0.0.5 and early
0.0.6 binaries, pure/accelerated profiles, reader/writer operations and three
states (materialized, pending WAL, already-open old handle). Every refused
operation preserved all audited files. Current-wheel recovery/search/reopen
verified both physical owners and preserved the old wheel's intact index name.

Artifacts under `.grafx-tmp/vector-owner-names-wheel-qualification/`:

| Artifact | SHA-256 |
| --- | --- |
| `candidate/okto_grafx-0.0.6-py3-none-any.whl` | `b95400211458e3ada876735658b48bd473c854b7d68b6173a2f5a2c36b2ce81a` |
| `pure-qualified/report.json` (12 passed) | `66c490d9f8301a6445c5fef3f953bc176ecf5f31e2df5fbc0dfc4f90777231a2` |
| `accelerated-qualified/report.json` (12 passed) | `a2c98976ceada9fdb52acf26ccab0918abaae4d7c2842fdcffb728b74d7b0216` |

All 239 source Python files matched the installed candidate when qualified.
Initial failed receipts remain retained: private corruption fixtures needed
serialization-cache invalidation; a DDL rollback test needed explicit fault
injection after bounded names removed its old trigger; copy fixtures needed
commit tracking before the source data write. Initial isolated-wheel runs lacked
declared dependencies, which were then installed only in the private environment.
No runtime guarantees were weakened to make these tests pass. This is not a
global/Pulse installation, release, or full functional-parity acceptance.
