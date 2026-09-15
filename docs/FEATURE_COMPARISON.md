# Feature comparison: Okto Grafx, Ladybug and Neo4j

## Latest development evidence

The complete [native V3 query execution](reports/FP_V3_INTEGRATED_QUERY_QUALIFICATION.md)
passes **all 3,896 required cases**, with one explicit upstream divergence for
retained lists of maps and zero unexecuted cases. Native multiple labels now
include CREATE/MERGE, SET/REMOVE, exact membership, stable identities, history,
copy and transfer/resume. The [installed label-format matrix](reports/FP_NODE_LABEL_WHEEL_QUALIFICATION.md)
passes 36 native scenarios and 36 transfer worker checks.

This is evidence from the 0.0.6 development line, since released as 0.0.6; it is not full TCK conformance or
proof of matching every Ladybug/Neo4j feature. The [final repository regression](reports/FP_FINAL_NATIVE_QUALIFICATION.md)
passes 25,077 tests with 19 attributed skips; all 20 mapped native supplemental
contracts pass. [Final installed Pulse qualification](reports/FP_FINAL_PULSE_QUALIFICATION.md)
also passes its exercised API/UI/MCP flows. Neo4j execution was explicitly deferred
by the user and is not a current delivery gate or an observed pass. The
chronological increments below preserve their original scope; later qualifications
supersede their former implementation-pending statements.

The subsequent [bounded Ladybug 0.20.3 execution](reports/FP_BOUNDED_LADYBUG_COMPARISON.md)
records 16 actual scenarios: seven matching observations and nine explicit dialect,
result or admission differences, with zero unavailable cases. Grafx's existing
final installed candidate matches all 16 independent expected results. Neo4j execution
is deferred; this is not a full-product
parity or performance claim. [Installed Pulse operational evidence](reports/FP_PULSE_OPERATIONAL_QUALIFICATION.md)
also covers source lifecycle, Settings/schema, API/MCP/search UI and native recovery.

## Development increments and their original scope

Grafx development now includes native `EXISTS { ... }` read subqueries with
implicit correlations, non-exporting scopes, optional RETURN, aggregation, UNION
and nesting. All 10 pinned original existential cases pass. This narrows the
subquery gap; it is not general Cypher/Neo4j parity. [Grafx contract and qualification](specs/EXISTS_SUBQUERIES_V1.md),
[Neo4j semantic reference](https://neo4j.com/docs/cypher-manual/current/subqueries/existential/).

Grafx logical views now capture schema dependencies inside EXISTS, pattern
predicates/comprehensions and nested reads, retaining both physical kinds when
names overlap. Their explicit-label/type policy applies in those scopes too;
ordinary native expressions are unchanged. This is consumer-integrity coverage,
not an additional Cypher-parity claim. [Qualification](reports/VIEW_EXPRESSION_DEPENDENCY_QUALIFICATION.md).

Large consecutive CREATE queries now run as an iterative native program, retaining
dependencies and a single atomic statement. Both original large-CREATE scenarios
pass; the CREATE family has 76 passes and only two retained multiple-label
divergences. This is bounded source admission, not unlimited queries or full
mutation parity. [Contract](specs/WRITE_PATTERN_CONTRACTS_V1.md#large-create-pipelines).

CREATE and supported MERGE patterns now expose native named paths. Explicit
binding/null-pattern errors and mutation-to-MERGE read phases are qualified in
the current development line. Bound-edge undirected MERGE and native
`startNode()`/`endNode()` are now implemented. Unbound/fixed multi-hop MERGE now
matches the entire path or creates every unbound entity. Label actions were added
by the later [native-label increment](specs/NODE_LABELS_V1.md).
[General MERGE contract and evidence](specs/GENERAL_MERGE_V1.md).

Native expression/path DELETE now passes all 41 original DELETE cases, including
list/map selectors, null paths, repeated identities and error contracts. It uses
bounded input preparation and outer-statement rollback; this closes the earlier
expression-DELETE gap, not full mutation parity.
[Contract and evidence](specs/DELETE_EXPRESSIONS_V1.md).

Native conditional MERGE property actions now support `ON CREATE SET` and
`ON MATCH SET`, including every matching bound-edge instance and outer-statement
rollback. Whole-map replacement/overlay from maps/native entity properties is
also implemented ([contract](specs/SET_PROPERTY_MAPS_V1.md)). Label actions are
covered by the later native-label increment; general fixed-length MERGE is implemented,
not a claim of all vendor-specific MERGE/Cypher behavior.
[Evidence](specs/GENERAL_MERGE_V1.md).

Independent node labels and relationship types with the same spelling now work
in native queries and catalog v2, together with `r:TYPE` predicates. All 528
required FP-3 query cases pass without fixture adaptation. Qualified copy/logical
transfer/resume, history and full-text index ownership now preserve overlapping
names. Hybrid search and 24 installed catalog/WAL admission cases also pass.
Subsequent vector ownership, durable naming and explicit missing-index repair are
qualified separately; projection capture, logical-view schema dependencies and
metadata ledgers now distinguish both physical kinds as well. See the
[consumer evidence](reports/NAMESPACE_PROJECTION_VIEW_MIGRATION_QUALIFICATION.md).
Remaining consumer, Pulse and full-package qualification stays open; this is not a claim
of full Cypher or product parity. [Contract and evidence](specs/GRAPH_NAMESPACES_V1.md).

The latest property-access increment adds native `keys(node|relationship)`, dynamic
entity string-key reads and property `REMOVE` using the existing atomic SET path.
SET/REMOVE label changes and the remaining frozen-profile failures
are not covered by that claim. [Usage and limits](QUERY_LANGUAGE.md#dynamic-entity-properties-and-property-removal).

The current development increment adds returning and unit writing subqueries,
explicit/all-name persistent imports and branch-local leading-WITH imports, with
one outer statement rollback boundary. This closes the earlier blanket
writing-subquery exclusion. Updating/unit UNION branches now share ordered private
effects and statement rollback. This is not complete CALL parity or installed-Pulse
qualification. A subsequent [writing callback increment](specs/WRITING_PROCEDURES_V1.md)
adds explicit same-transaction, result-free DML on declared models, revocable
authority and shared budgets. Subsequent native signature/nesting increments are
described below; bounded schema authority is now added, not complete procedure parity.
The subsequent [native procedure value contract](specs/PROCEDURE_NATIVE_VALUES_V1.md)
adds temporal/DECIMAL, LIST/MAP/ANY and vector signatures with owned copies, recursive
budgets and native persistence/recovery. [Entity procedure signatures](specs/PROCEDURE_ENTITY_SIGNATURES_V1.md)
now restore native identity from invocation-scoped observations, including paths
and typed entity lists. Broader procedure capabilities and parameterized stored
collections/DECIMAL still remain distinct gaps. The later native DECIMAL storage
increment adds typed columns, native parameters/results and durable admission,
followed by native arithmetic/casts/aggregates, exact numeric grouping/order and
typed equality-index seeks and [qualified history/copy/logical transfer](reports/FP6_DECIMAL_CONSUMER_QUALIFICATION.md).
Tagged CLI/local imports and DECIMAL/NUMBER procedures are also implemented;
[exact decimal128 Arrow/Pandas/Polars/Parquet](EXTENSIONS_AND_ARROW.md#exact-native-decimals-006-development)
now preserve p/s with explicit descriptors, mandatory metadata and atomic native
imports. Decimal ordered indexes remain unsupported. The combined native type
package now has [installed-reader qualification](reports/FP6_TYPE_WHEEL_QUALIFICATION.md);
[scope and evidence](specs/DECIMAL_VALUES_V1.md#native-storage-contract-development).
The development [typed-collection increment](specs/TYPED_COLLECTIONS_V1.md) adds
native LIST/MAP element declarations, fixed ARRAY lengths and named STRUCT fields,
with nested nullability, public schema descriptors and catalog capability fencing.
Native history/copy/transfer and CLI preserve this metadata, with
[local grouped qualification](reports/FP6_TYPED_COLLECTION_NATIVE_QUALIFICATION.md).
The subsequent [exact JSON/CSV/JSONL/SQLite collection transport](COLLECTION_JSON.md)
preserves full descriptors, native leaf precision and heterogeneous map identity,
with bounded input and atomic imports. [Qualification](reports/FP6_TYPED_COLLECTION_TEXT_QUALIFICATION.md).
The following [columnar collection integration](COLLECTION_COLUMNAR.md) supports
Arrow/Pandas/Polars/Parquet with exact descriptors, typed nested values and
whole-call rollback. The [type support matrix](TYPE_SUPPORT.md) distinguishes
supported consumers and explicit refusals; the installed native-reader checkpoint
is qualified separately. This is not a claim of full nested-type interoperability
or Cypher parity. [Active V3](conformance/PROFILE_V3.md) requires multiple labels
and records Set1 #0010 as a deliberate divergence because native lists of maps
remain supported. The later native-label implementation and its current complete
query-profile result are recorded at the top of this comparison.
The latest [native query authority](specs/PROCEDURE_QUERY_AUTHORITY_V1.md) supplies
permissioned same-transaction readers and returning writer queries, bounded native
results, shared snapshot/clocks/cancellation and rollback. [Recursive registered
procedures and effect declarations](specs/PROCEDURE_NESTING_EFFECTS_V1.md) now add
inherited call depth, shared root budgets and explicit default-volatile callback
determinism. [Native schema authority](specs/PROCEDURE_SCHEMA_AUTHORITY_V1.md) adds
permissioned CREATE DDL, flexible schema and catalog-v2 index/DML composition; this is not
full Neo4j procedure compatibility or an installed-Pulse qualification.
[Updating UNION](COMPOSABLE_QUERIES.md#updating-union-branches),
[Import scopes](COMPOSABLE_QUERIES.md#subquery-import-scopes),
[native contract](COMPOSABLE_QUERIES.md#native-writing-and-unit-subqueries)
and [supplemental profile](specs/WRITE_SUBQUERIES_V1.md).

The development line now also stores heterogeneous node/edge properties through
native `ANY` columns, retaining concrete scalar/list/map types across updates and
logical transfer. Native unlabeled node creation and dynamic property maps now
extend that foundation, along with automatic flexible relationship types and
transactional endpoint-pair growth. Multiple labels were subsequently implemented;
unrestricted mixed-type property indexes are not claimed.
[ANY contract](architecture/HETEROGENEOUS_PROPERTIES_V1.md)
and [unlabeled node contract](architecture/FLEXIBLE_GRAPH_V1.md).

Re-matching a bound relationship now preserves its full identity through
MATCH/OPTIONAL and named path segments. The original path-count and duplicate
relationship-variable reference cases pass; the wider required MATCH/MATCH-WHERE
selection, after native map, spelling, interval, expression-binding and node-MERGE
support, has 363 required passes, no execution failures and one fixture blocker,
so this is not complete
path/language parity. See [current native evidence](conformance/FP3_PROGRESS.md).

Relationship property maps now compose with typed/untyped MATCH, OPTIONAL,
paths, predicates and comprehensions. A range map constrains every edge, with
vacuous truth at zero hops; no new schema-free storage or index promise is
implied. See [syntax and costs](QUERY_LANGUAGE.md#inline-relationship-property-maps).

Inverted explicit hop intervals now represent empty matches instead of parse
errors, while zero-hop paths remain distinct. Bounds outside `0..30` still
refuse; no omitted-upper truncation policy was added. These changes close three
positive interval cases and two malformed-range error cases, not all path parity.

Read arrows accept the bidirectional spelling `<-->` with existing undirected
semantics, and type alternatives accept `:R|:S` as well as `:R|S`. This improves
source-query compatibility without changing stored relationship direction or
making ambiguous CREATE/MERGE writes legal. See [canonical interpretation](QUERY_LANGUAGE.md#read-arrows-and-type-alternatives).

WITH can retain same-kind coalesce/CASE entity results and ordered relationship
lists as native graph bindings for later MATCH. This closes four recorded cases
without treating scalar/map expressions as graph authority. General expression
provenance and the remaining fixture cases are not declared complete. See
[binding semantics](COMPOSABLE_QUERIES.md#clause-order-and-scope).

Standalone node MERGE now preserves all matches per input instead of selecting
one row. Unlabeled searches preserve qualified identities across tables, but a
no-match creation still requires a typed label and otherwise refuses atomically.
This closes Match8 #0002 with its original query/expectation; it does not claim
full MERGE compatibility. Runtime-bound writes are described below. See
[matching and creation](QUERY_LANGUAGE.md#node-merge-matching-and-creation).

Polymorphic property reads now preserve different per-row scalar families across
node/relationship tables, including comparison/NULL and lazy scalar semantics.
This removes a query-level refusal, not the typed-storage boundary. All 26
required MATCH/WHERE cases pass (two original, 24 with declared schema adaptation).
See [property semantics](QUERY_LANGUAGE.md#heterogeneous-properties-across-tables)
and the [FP-3 progress record](conformance/FP3_PROGRESS.md).

Native node-label predicates (`n:Label`, conjunctive labels, NULL and composed
node expressions) are implemented in the 0.0.6 development line. Grafx supports
unlabeled nodes or one table label per node; this is not parity with multi-label
storage or label mutation. See [exact contract](QUERY_LANGUAGE.md#node-label-predicates).

Native existential WHERE pattern predicates now cover scoped bound-node/edge
references, directions and bounded ranges, NULL semantics and shared read/write
snapshot visibility. All 39 pinned Pattern1 cases pass (19 original, 20 explicitly
typed-fixture-adapted). Native pattern comprehensions now execute as scoped list
projections; the five required Pattern2 cases pass with explicit fixture-schema
adaptation, not as original upstream passes. This is not full Cypher
parity; [comprehensions](QUERY_LANGUAGE.md#pattern-comprehensions),
[predicate semantics](QUERY_LANGUAGE.md#existential-pattern-predicates) and
[scoped regression evidence](conformance/FP3_PROGRESS.md) identify the boundaries.

FP-3 development update: Grafx now has native logical relationship groups over
declared endpoint-table pairs, atomic group DDL, logical edge/path type results
and statically or runtime-resolved member writes. The formerly blocked Path2 endpoint-pair
cases pass with explicitly recorded typed-schema adaptation. This narrows the
typed-relationship expressiveness gap without changing the single-label node
model or establishing full Cypher parity. Bound SET/DELETE uses each actual row's
schema; relationship creation resolves an exact candidate member and retains
endpoint identity/OCC proofs. Group-wide
schema evolution and broader projection integration remain
pending. Bounded existing-target copy now preserves typed groups over keyed nodes,
with logical-name/endpoint-pair resolution and atomic receipts.
Fresh-store logical transfer now preserves typed/flexible groups and maps
through versioned artifacts and qualified endpoint remapping. Opt-in retained
history also preserves model flags/logical types through scan/index reads and
backup, with a separate capability fencing older writers.
[Native contract](QUERY_LANGUAGE.md#relationship-types-spanning-endpoint-tables)
and [working evidence](conformance/FP3_PROGRESS.md). No new competitor execution
or benchmark is claimed by this update.

Native `RETURN *` expands visible variables through existing projection, UNION
and returning read-subquery operators, preserving detached entity/path identity.
That scalar increment does not establish complete Cypher conformance; subsequent
native writing-call support is tracked in the current contract above.
[Projection contract](QUERY_LANGUAGE.md#returning-the-visible-scope).

Row windows accept row-independent scalar expressions, not just literal counts or
parameters, with per-invocation evaluation and explicit type/negative errors.
The 31 original RETURN window cases and all 9 WITH window cases pass. Plain WITH
ordering now reads source-only bindings temporarily without leaking them into
downstream scope; the wider ordering family remains incomplete. General expressions retain canonical execution instead of duplicated
top-K evaluation. [Window contract](QUERY_LANGUAGE.md#row-independent-skip-and-limit-expressions).

Native exact percentileDisc/percentileCont support DISTINCT/grouped samples and
bounded external sorting. The 35 original aggregation-expression cases pass;
this does not imply every aggregate or temporal function is implemented.
[Percentile contract](QUERY_LANGUAGE.md#percentile-aggregates).

RETURN aliases work inside sort expressions/property access, including shadowing.
Native grouping ambiguity and nested-aggregate errors have explicit evidence;
WithOrderBy4 passes all 20 original cases. Return6 now passes all 21: direct
volatile aggregate arguments refuse, while WITH materialization retains random
aggregation. [Contract and migration](QUERY_LANGUAGE.md#stable-aggregate-arguments-and-volatile-row-values).

WITH DISTINCT/grouped modifiers reuse exactly projected subexpressions and
aggregate results. Plain attached WHERE may read unexported source bindings only
until that stage ends; all 19 original WITH-WHERE scenarios pass. This does not
establish full aggregate-ordering or temporal parity.

Live property/label access after owner deletion now refuses with native runtime
evidence and whole-instruction rollback; all 18 original Return2 cases pass in
the focused run. Counts, immutable relationship type and detached observations
are preserved. [Deletion contract](QUERY_LANGUAGE.md#content-access-after-deletion).
This is a semantic correction, not a new competitor benchmark or parity claim.

Native implicit single-label creation, label-free nodes and heterogeneous persisted properties are implemented
parts of the broader model expansion. Automatic relationship creation now closes
the original Match4 #0004 without fixture adaptation. Existing-target copy now
preserves flexible/no-PK entity multiplicity and topology through native target
bindings and atomic receipts; installed-Pulse qualification remains pending.
See the [copy identity contract](CATALOG_COPY.md#flexible-and-no-pk-entity-identity)
and [expanded scope](specs/FUNCTIONAL_PARITY_PLAN.md#authorized-model-expansion-label-free-nodes-and-heterogeneous-properties).

[Project README](../README.md) · [Documentation](README.md) · [Roadmap](../ROADMAP.md)

Reviewed September 10, 2026. This is a capability and integration comparison,
not a benchmark, migration guarantee or product certification.

The cross-product tables describe **validated 0.0.6 source**, including the completed
NHC-1–8 round, published as 0.0.6 on September 13, 2026. They do **not** describe the
earlier 0.0.5 package. Section 5 identifies the 0.0.6 additions; installed versions must
be checked separately. Local feature/regression/documentation acceptance is recorded
in the [delivery evidence](reports/V006_NHC_ROUND.md), not inferred from feature names.

The query-language rows additionally identify the **language round shipped in 0.0.6**
(locally validated before publication). Its [acceptance evidence](reports/V006_QUERY_LANGUAGE_ROUND.md),
[separate contract](CYPHER_COMPATIBILITY.md) and
remaining reference divergences supersede the older two-branch query description;
they do not retroactively change the NHC checkpoint's test evidence.

## Scope and reading rules

Generic one-hop untyped and relationship-type alternatives now compose with
MATCH/OPTIONAL, returning subqueries and native path capture. They replace the
literal application-specific untyped-query recognizer. Direction, qualified
endpoints, parallel-edge multiplicity and clause-wide trail rules are native;
bounded variable ranges across multiple relationship tables now share the native
depth-first trail engine, including zero hops and an omitted-upper completeness
probe. See [query semantics](QUERY_LANGUAGE.md#heterogeneous-bounded-relationship-ranges)
and [working evidence](conformance/FP3_PROGRESS.md), not an overall parity claim.

The current FP-3 increment also validates absent-table MATCH/OPTIONAL reads:
empty results/null extension, static entity-kind checks and legitimate zero-hop
paths without implicit schema creation. This addresses original graph-function
cases, not schema-free storage parity. [Working evidence](conformance/FP3_PROGRESS.md)
records the remaining failures and the exact regression selection.

September 11 FP-2 development update: focused native tests now also cover
hexadecimal/octal INT64 literals, source-faithful headings, general expression
postfix access, adjacent comparison chains and nondeterministic `rand()`. The
[working conformance evidence](conformance/FP2_PROGRESS.md) separates these gains
from still-failing required cases and later-package work. No new competitor
execution, full regression or overall parity claim is implied by this update.
Subsequent focused corrections enforce BOOL/NULL logical operands and preserve
BOOL/INT64/DOUBLE literal types in aggregate projections. These improve native
semantics without changing the product-level parity claim or storage guarantees;
the linked working evidence retains all unresolved cases.
Empty map keys and bounded streaming direct `UNWIND range` are additionally
covered by native tests and unchanged reference cases; they introduce neither
a general unbounded sequence API nor a new cross-product parity claim.

The complete original range and float-literal families also pass in the current
FP-2 increment, including runtime RANGE argument errors and long finite DOUBLE
spellings. The numeric-token bound remains explicit; NaN/infinity policy is
unchanged. This is additional Grafx evidence, not a new competitor qualification.

Current development additionally supports lexical `WITH *` expansion (including
mixed explicit projections) and proves IN operand error phases; all 46 original
membership cases pass. The first FP-3 increment also composes standalone
polymorphic node reads, inline maps, rematches and optional/subquery pipelines.
Native typed/polymorphic node and relationship output now carries qualified
identity, immutable properties, snapshot provenance and tagged JSON, including
pending identities and cursor/sort/DISTINCT output. Native entity UNION, nested
aggregate spill and subsequent subquery/grouping scopes are now covered too.
The 12 original UNION-family cases run with 10 original passes and 2 passes with
declared typed-fixture adaptation; no selected failures/not-run. Mixed duplicate
policies require separate subquery scopes. [Result contract](ENTITY_VALUES.md).
Generalized named paths remain pending, so these additions do not establish
general query-language parity. See
[FP-3 evidence](conformance/FP3_PROGRESS.md) for exact remaining limits.

The existing typed one-hop named capture now returns native `PathValue` with
qualified entity components, immutable observations and tagged JSON; UNION,
spill and cursors preserve those observations. Metadata-named user properties
no longer collide with result structure. The subsequent FP-3 increment broadens
single-hop captures to incoming/undirected walks, WHERE/WITH/UNWIND, typed OPTIONAL
MATCH, windows and returning subqueries (including path-or-NULL UNION exports).
Clause-level relationship uniqueness now spans separate patterns/segments, while
separate MATCH clauses may reuse edges. The next increment adds explicit typed
variable ranges, zero length, cycles and concatenated segments with streaming
depth-first expansion. Omitted upper bounds now fail explicitly on an extendable
trail beyond the 30-hop resource ceiling, instead of silently truncating at 20.
Untyped alternatives remain incomplete; this is not complete path-language parity.
Node-only named patterns also publish zero-edge native paths without requiring a
relationship schema; aliases, optional nulls, UNION and aggregates preserve their
qualified identities. This increment shipped in 0.0.6; it is not a new competitor
execution result.

Native `properties()`, `labels()` and `type()` expose property maps, the current
node-label set and relationship type, including NULL, polymorphic/UNION/path
composition and owner writes. Graph-function typing keeps runtime checks lazy.
The subsequent complete V3 execution covers the original graph-function cases;
vendor-specific extensions and final integrated consumer qualification remain distinct.

| Product | Baseline and evidence |
| --- | --- |
| Okto Grafx | Published **0.0.6**, tag `v0.0.6`, merge `1e01be5`, PyPI September 13, 2026. Local consumer contracts and implementation are the authority. The latest publication receipt in this repository covers [0.0.5](reports/PYPI_0_0_5_PUBLICATION.md). |
| Okto Grafx comparison baseline | **0.0.6** as developed on `feature/v0.0.6`, including completed NHC-1–8, reviewed September 10, 2026 and since published as 0.0.6. Local acceptance, not a remote CI claim. [Changelog](../CHANGELOG.md), [NHC evidence](reports/V006_NHC_ROUND.md). |
| Ladybug | **0.20.3** release, plus official rolling documentation accessed on the review date. Source was inspected where the concurrency documentation leaves an important ambiguity. This is not the older Ladybug 0.16.0 used in some Grafx benchmarks. [Release](https://github.com/LadybugDB/ladybug/releases/tag/v0.20.3). |
| Neo4j | Current self-managed documentation accessed on the review date, distinguishing **Community (CE)**, **Enterprise (EE)** and separately installed libraries. Not an installed-version conformance test. Rolling documentation may describe features newer than a particular deployed release. [Edition guide](https://neo4j.com/docs/operations-manual/current/introduction/). |

The development line includes MP-1–MP-8 and subsequent catalog, temporal, posting
and NHC additions. These close specific API gaps; they do not establish general
Cypher compatibility, distributed transactions, full bitemporality or production
maturity. See the exact boundaries in section 5.

Neo4j was selected to complement the embedded Ladybug comparison with a
client/server graph database. A server is a different deployment trade-off, not
automatically a defect or an advantage.

**Native** means part of the database/library's public surface. **Extension** or
**library** means an additional integration layer. **Planned** is not delivered.
**Not established** means the inspected sources do not establish an equivalent
contract; it must not be read as proof that the other product cannot provide one.
No third-party database was installed or benchmarked for this document.

## 1. Deployment, concurrency and durability

| Capability | Grafx 0.0.6 development | Ladybug | Neo4j |
| --- | --- | --- | --- |
| Embedded Python use | Native synchronous Python library; no database server. [Integration](INTEGRATION.md). | Embedded engine with Python bindings. [Python API](https://docs.ladybugdb.com/client-apis/python/). | Python driver connects to a DBMS; not an in-process Python replacement. [Driver](https://neo4j.com/docs/python-manual/current/). |
| Other languages | Python and JSON CLI, with a JS/TS subprocess recipe; not a native multi-language driver family or network protocol. [CLI](CLI.md). | APIs include C/C++, Rust, Java, JavaScript, Go and Swift in addition to Python. [Installation](https://docs.ladybugdb.com/installation/). | Official network drivers include Python, Java, JavaScript, Go and .NET. [Edition guide](https://neo4j.com/docs/operations-manual/current/introduction/). |
| Storage/execution emphasis | Paged local storage; Python engine with NumPy codec/vector acceleration and accepted native CRC. [Architecture](ARCHITECTURE.md), [configuration](CONFIGURATION.md). | Columnar storage, CSR adjacency, vectorized/factorized processing and multicore query execution. [Overview](https://docs.ladybugdb.com/). | Native graph storage and server-managed query execution; runtime availability depends on edition. [Edition guide](https://neo4j.com/docs/operations-manual/current/introduction/). |
| Concurrent readers and writers | Independent participants, including processes, can own snapshots/write transactions against the same local store. OCC and publication fencing remain. [Operations](OPERATIONS.md#concurrency-contract). | Concurrent connections through one shared writable `Database`; separate directly opened processes are documented as read-only together or a single writable owner. [Concurrency](https://docs.ladybugdb.com/concurrency/). | Multiple clients transact through the server with locks and deadlock detection, rather than directly opening its files. [Transactions](https://neo4j.com/docs/operations-manual/current/database-internals/). |
| Isolation | Snapshot reads and optimistic write-conflict validation; not a blanket serializability claim. [Contract](OPERATIONS.md#concurrency-contract). | Manual describes serializable transactions and one writer; source contains a multiwrite switch. See the qualification below. [Transactions](https://docs.ladybugdb.com/cypher/transaction/). | Default is read committed; explicit locking can strengthen isolation. Do not assume a repeatable transaction-wide snapshot. [Isolation](https://neo4j.com/docs/operations-manual/current/database-internals/concurrent-data-access/). |
| Durable commit/recovery | ACID properties within the supported single-store contract, using snapshot/OCC isolation; WAL barrier, verified replay and typed post-commit outcomes. [ACID scope](OPERATIONS.md#acid-scope). | ACID transactions, WAL and checkpoint recovery. [Transactions](https://docs.ladybugdb.com/cypher/transaction/). | ACID transactions, write-ahead transaction log and recovery. [Internals](https://neo4j.com/docs/operations-manual/current/database-internals/). |
| Replication/high availability | Not supplied by the embedded engine; local multi-process access is not replication. [Roadmap](../ROADMAP.md). | Native HA clustering is not established by this review; an API facade is not replication. | Clustering and failover are EE capabilities; CE is single-instance. [Clustering](https://neo4j.com/docs/operations-manual/current/clustering/). |

### Concurrency qualification: avoid a misleading multiwriter checkbox

**ACID clarification:** Grafx is not missing ACID merely because the initial
comparison described its mechanisms instead of using the acronym. Atomicity,
supported consistency invariants, isolation and durability are implemented;
[their exact scope and evidence](OPERATIONS.md#acid-scope) matter more than a
checkbox. Snapshot/OCC is not a promise of universal serializability. Neo4j also
documents ACID together with read-committed isolation by default, illustrating
why those labels must not be conflated.
[Neo4j transaction contract](https://neo4j.com/docs/operations-manual/current/database-internals/).

Grafx's differentiating contract is **independent local processes sharing a
durable store while readers and writers coexist**. It still has an exclusive
commit-publication section; partition/page conflicts can reject independent-looking
writes. Same-handle protected sections, memory multiplication across handles and
the Python GIL also matter. It is neither lock-free nor a promise of linear
write throughput. [Grafx ownership and limits](OPERATIONS.md#concurrency-contract).

Ladybug's manual limits direct ownership to one writable database object or
multiple read-only objects. Connections sharing the writable object can operate
concurrently; remote clients can use a single-owner service.
[Documented ownership](https://docs.ladybugdb.com/concurrency/).
However, the **0.20.3 source** contains `enableMultiWrites` and conditionally
rejects an additional writer only when that setting is disabled. Therefore,
“Ladybug has no multiwriter code” would be false. The inspected manual's
single-writer/serializable description does not establish the isolation or support
guarantees of every enabled-switch configuration. Neither that switch nor this
inspection proves safe independent writable file owners across processes.
[Versioned transaction manager](https://github.com/LadybugDB/ladybug/blob/v0.20.3/src/transaction/transaction_manager.cpp),
[configuration declaration](https://github.com/LadybugDB/ladybug/blob/v0.20.3/src/include/main/db_config.h).

For Neo4j, concurrency is mediated by the DBMS, with read-committed visibility
and entity locking. It is a valid way to support many application processes, but
not the same deployment model or isolation contract as Grafx.
[Concurrent access](https://neo4j.com/docs/operations-manual/current/database-internals/concurrent-data-access/).

## 2. Modeling, queries and indexes

| Capability | Grafx 0.0.6 development | Ladybug | Neo4j |
| --- | --- | --- | --- |
| Property graph/schema | Explicit typed tables/constraints and flexible schema-free nodes/relationships with native heterogeneous properties. Nodes may have zero, one or multiple versioned labels on one stable identity; labels do not remove physical typed constraints. [Query reference](QUERY_LANGUAGE.md), [native labels](specs/NODE_LABELS_V1.md). | Structured property graph with typed tables. [Overview](https://docs.ladybugdb.com/). | Labels/types and property graphs with configurable constraints. [Constraints](https://neo4j.com/docs/cypher-manual/current/schema/constraints/). |
| Query language | Typed and flexible graph models with a bounded language, not full Cypher. Current 0.0.6 development includes ordered clauses, correlated `OPTIONAL MATCH`, up to 64 read/write or unit `UNION`/`UNION ALL` branches, returning/unit writing subqueries, explicit/wildcard/leading-WITH imports, native scalar/list/temporal families and trusted typed `CALL/YIELD`. Callbacks can receive explicit transaction-scoped native read/write/schema authority, not unrestricted database handles. Native branch effects share statement rollback; the complete native regression and separate installed-Pulse checks are qualified, not full TCK conformance. [Exact surface](QUERY_LANGUAGE.md), [fixed compatibility contract](CYPHER_COMPATIBILITY.md). | Cypher dialect; the pinned 0.20.3 run refused several scoped CALL forms accepted by Grafx. Do not assume either dialect is a superset from a feature checkbox. [Actual bounded observations](reports/FP_BOUNDED_LADYBUG_COMPARISON.md). | Broad Cypher language and procedure ecosystem; queries still need dialect/version validation. [Cypher manual](https://neo4j.com/docs/cypher-manual/current/). |
| Stored value breadth | INT64, DOUBLE, STRING, BOOL, BLOB, UUID, TIMESTAMP, vectors, native temporal families and DECIMAL(p,s). ANY retains nested values. Temporal constructors/clocks, arithmetic and row persistence are implemented. Native DECIMAL has explicit casts, basic arithmetic, SUM/AVG/MIN/MAX, exact comparisons/grouping/order, typed equality-index seeks, history/copy/logical transfer, CLI/local imports, native procedures and exact decimal128 Arrow/Pandas/Polars/Parquet. Typed LIST/MAP/ARRAY/STRUCT descriptors, DDL/catalog, native consumers, exact JSON/CSV/JSONL/SQLite and Arrow/Pandas/Polars/Parquet are implemented with local qualification; [combined installed-reader qualification](reports/FP6_TYPE_WHEEL_QUALIFICATION.md) is recorded separately from final profile/Pulse acceptance. [Types](QUERY_LANGUAGE.md#values-and-python-mapping), [temporal](TEMPORAL_VALUES.md), [decimal scope](specs/DECIMAL_VALUES_V1.md). | Additional integer widths, DECIMAL, DATE/INTERVAL and nested LIST/ARRAY/STRUCT/MAP/UNION families. [Types](https://docs.ladybugdb.com/cypher/data-types/). | A different property/constraint model; do not port typed table DDL unchanged. [Schema](https://neo4j.com/docs/cypher-manual/current/schema/constraints/). |
| Scalar/structural indexes | PK, endpoint and identity indexes; custom equality/hash and ordered indexes; optional repeated-key `posting_hash`, explicit sizing and maintenance. [Indexes](INDEXES_AND_VECTORS.md), [posting hash](POSTING_HASH.md). | PK hash/ART indexes and automatic column zone maps; not an identical secondary-index API. [Indexes](https://docs.ladybugdb.com/cypher/indexes/). | Range, text, point and token-lookup indexes, with planner integration. [Index families](https://neo4j.com/docs/cypher-manual/current/indexes/search-performance-indexes/). |
| Bulk and streamed consumption | Atomic `executemany`, bounded physical scans, cursor ownership and typed batch import/export. [Integration](INTEGRATION.md). | `COPY FROM` and Python data-frame interoperability. [Python](https://docs.ladybugdb.com/client-apis/python/). | Driver transactions/results and server import tooling; boundaries differ from an embedded transaction. [Python driver](https://neo4j.com/docs/python-manual/current/). |
| Multiple stores/federation | Attached `CatalogSession`, explicit alias/permission/workspace resolution and single-store transactions; no cross-store joins, edges or distributed COMMIT. [Catalogs](CATALOGS_AND_WORKSPACES.md). | `ATTACH`/`DETACH`; external systems through extensions. No cross-store atomicity equivalence is asserted. [Attach](https://docs.ladybugdb.com/cypher/attach/). | Additional databases/composite databases are EE features, not direct attachment of Grafx/Ladybug files. [Edition guide](https://neo4j.com/docs/operations-manual/current/introduction/). |

## 3. Retrieval, analytics and interoperability

| Capability | Grafx 0.0.6 development | Ladybug | Neo4j |
| --- | --- | --- | --- |
| Vector search | Native exact and HNSW search, declared spaces/metrics/precision, filters and memory/work controls. [Vectors](INDEXES_AND_VECTORS.md). | `vector` extension: disk-based HNSW and filtered search on node vector properties. [Vector extension](https://docs.ladybugdb.com/extensions/vector/). | Native ANN indexes for node/relationship embeddings in CE and EE; native `VECTOR` property storage has additional format/edition requirements. [Vector indexes](https://neo4j.com/docs/cypher-manual/current/indexes/semantic-indexes/vector-indexes/). |
| Full-text search | Native snapshot-bound weighted BM25 over node/relationship properties; explicit atomic analyzer/options replacement. New searches use the current analyzer with their owning data snapshot. [FTS](FULL_TEXT_SEARCH.md). | `fts` extension with BM25, stop words and stemming; documented FTS targets are node-table STRING properties. [FTS extension](https://docs.ladybugdb.com/extensions/full-text-search/). | Lucene-based full-text node/relationship indexes and analyzers. [Full-text indexes](https://neo4j.com/docs/cypher-manual/current/indexes/semantic-indexes/full-text-indexes/). |
| Text semantics and limits | Bounded prefix and exact phrase search; optional persisted positions, ordered slop and typed token-position output. No phrase/prefix combination, character offsets or language stemming. Python API is richer than CLI/closed CALL. [FTS limits](FULL_TEXT_SEARCH.md). | Conjunctive/disjunctive term matching and stemming are documented; equivalent typed slop/position-return contracts are not established here. BM25 alone does not imply identical visibility or analysis. [FTS contract](https://docs.ladybugdb.com/extensions/full-text-search/). | Lucene query syntax, including quoted exact matches, and optional eventually-consistent index updates; not equivalent to Grafx's snapshot contract or position DTO. [Full-text contract](https://neo4j.com/docs/cypher-manual/current/indexes/semantic-indexes/full-text-indexes/). |
| Hybrid retrieval | Native `search_hybrid`: weighted RRF plus bounded graph evidence on one owned snapshot; one target node table. [Hybrid](HYBRID_SEARCH.md). | Vector and FTS components exist; an equivalent single-snapshot RRF/graph-evidence API was not established here. [Extensions](https://docs.ladybugdb.com/extensions/). | Supported via query composition and the separate GraphRAG Python library (`HybridRetriever`); not the same native API/consistency contract. [GraphRAG](https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_rag.html). |
| Graph algorithms | Detached bounded projections: WCC/SCC, BFS/Dijkstra, PageRank/personalization, k-core, label propagation and topological ordering with cycle detection; optional NumPy PageRank. [Algorithms](GRAPH_PROJECTIONS.md). | `algo` extension includes PageRank, shortest paths, components, k-core and Louvain. [Algorithms](https://docs.ladybugdb.com/extensions/algo/). | Separate GDS library with extensive algorithms and ML pipelines; CE/EE licensing has its own limits. [GDS](https://neo4j.com/docs/graph-data-science/current/introduction/). |
| Tabular/file exchange | Native local CSV/JSONL; optional Arrow, Pandas, Polars, Parquet and NetworkX adapters. [Text](LOCAL_TEXT_IMPORT.md), [tabular](TABULAR_AND_PARQUET.md), [graph exchange](GRAPH_EXCHANGE.md). | CSV, Parquet, NumPy/DataFrame integration, plus external-store extensions. [Python](https://docs.ladybugdb.com/client-apis/python/), [extensions](https://docs.ladybugdb.com/extensions/). | Driver-based exchange plus ecosystem tooling; GDS Arrow integration is edition-dependent. [GDS editions](https://neo4j.com/docs/graph-data-science/current/introduction/). |
| Lakehouse/remote connectors | Not a native S3/relational federation layer; current local import APIs require explicit bounds/path policy. [Ingestion](LOCAL_TEXT_IMPORT.md). | Extensions include S3, Azure, GCS, ADBC, DuckDB, PostgreSQL, SQLite and lakehouse formats. [Extension inventory](https://docs.ladybugdb.com/extensions/). | Server ecosystem differs from embedded file scanners; connector-specific contracts must be assessed separately. |

GDS CE includes the algorithm library but limits concurrency to four CPU cores;
GDS EE removes that limit and adds catalog/operational features. Database EE and
GDS EE must not be silently treated as the same entitlement.
[GDS editions](https://neo4j.com/docs/graph-data-science/current/introduction/).

## 4. Operations, history, security and tooling

| Capability | Grafx 0.0.6 development | Ladybug | Neo4j |
| --- | --- | --- | --- |
| Physical backup/restore | Bounded verified backup; same-UUID replacement restore requires the original offline. It is not a concurrently writable fork. [Backup](BACKUP_RESTORE.md). | WAL/checkpoint and export are documented; equivalence to Grafx's manifest/identity-aware restore API was not established. [Transactions](https://docs.ladybugdb.com/cypher/transaction/), [export](https://docs.ladybugdb.com/export/). | CE: offline dump/load and consistency checks. EE: online backup and additional restore tooling. [Backup](https://neo4j.com/docs/operations-manual/current/backup-restore/). |
| Logical copy and resumption | Checksummed resumable fresh-store transfer plus bounded existing-target copy, durable receipts and optional one-hop endpoint closure. History-bearing transfers require explicit current-only semantics. [Transfer](LOGICAL_TRANSFER.md), [copy](CATALOG_COPY.md). | CSV/Parquet/JSON export; not proof of identical logical identity/resumption guarantees. [Export](https://docs.ladybugdb.com/export/). | Dump/load is not equivalent to Grafx's typed resumable logical-transfer API. [Backup modes](https://neo4j.com/docs/operations-manual/current/backup-restore/). |
| Commit provenance/change capture | Opt-in durable metadata and database-qualified commit lookup/paging. **Not row-change CDC**. [History](COMMIT_HISTORY.md). | An equivalent public commit-catalog/metadata API was not established by this review. | CDC is available in EE and specified Aura tiers; change records are not an exact database copy or Grafx's commit catalog. [CDC](https://neo4j.com/docs/cdc/current/). |
| Historical/bitemporal graph queries | Native opt-in retained system-time as-of, versions and graph/schema/property diff; optional authenticated temporal index, retention/pins and quiescent physical compaction. No valid time or full bitemporality. [Temporal scope](SYSTEM_TIME_HISTORY.md). | Temporal value types exist; equivalent native retained graph versions/diff or bitemporal semantics were not established. [Types](https://docs.ladybugdb.com/cypher/data-types/). | CDC is not evidence of built-in arbitrary historical graph snapshots; no bitemporal equivalence is claimed. [CDC scope](https://neo4j.com/docs/cdc/current/). |
| Authentication/authorization | Host application responsibility; no native users/RBAC/TLS service boundary or at-rest encryption. [Deployment](OPERATIONS.md#deployment-and-ownership). | Embedded file ownership is not an application authorization model; an equivalent native user/RBAC contract was not established. | Server security surface; advanced RBAC/LDAP belongs to EE. [Editions](https://neo4j.com/docs/operations-manual/current/introduction/). |
| Observability/control | Typed errors/commit reports, verification/ledger, metrics, query budgets and cooperative read cancellation. [Operations](OPERATIONS.md), [read controls](READ_CONTROL_AND_INDEX_CLEANUP.md), [configuration](CONFIGURATION.md). | Separate CLI/Explorer tooling; compare individual operational contracts rather than assuming Grafx's diagnostics are unique. [Documentation](https://docs.ladybugdb.com/). | Server query management and monitoring; tooling/metrics availability varies by edition. [Editions](https://neo4j.com/docs/operations-manual/current/introduction/). |
| GUI and agent interfaces | Local CLI; 0.0.6 adds script-free offline HTML snapshots, not an interactive GUI or native MCP server. Pulse's UI/MCP remain **Pulse features**. [CLI](CLI.md), [HTML snapshots](HTML_SNAPSHOTS.md). | Ladybug Explorer and visualization integrations. [Documentation](https://docs.ladybugdb.com/). | Neo4j Browser and additional tooling, some separate products. [Editions](https://neo4j.com/docs/operations-manual/current/introduction/). |
| License model | Elastic License 2.0 plus SaaS/branding addendum; do not describe it as MIT or unrestricted hosted-service licensing. [License](../LICENSE), [terms](../README.md#deployment-and-license). | MIT. [Versioned license](https://github.com/LadybugDB/ladybug/blob/v0.20.3/LICENSE). | CE is GPLv3; EE has separate licensing. [Edition guide](https://neo4j.com/docs/operations-manual/current/introduction/). |

License labels are descriptive, not legal advice; review the actual terms for
redistribution, embedding or hosted deployment.

## 5. Current Grafx development: implemented deltas and practical value

Every row below describes **0.0.6** (published September 13, 2026), not the earlier 0.0.5 package.
NHC-1–8 have completed local acceptance: 269 affected tests and 2,774 final
corrective tests passed, plus all 14 real-wheel compatibility cells. The initial
full run recorded 16,929 passes and nine failures; every failure was corrected
and explicitly covered by the passing rerun. Counts overlap; this is not a claim
that a second unchanged full run or a remote platform matrix executed.
[Exact validation provenance](reports/V006_NHC_ROUND.md).
The application examples are architectural uses of these contracts, not measured
customer outcomes or new engine guarantees.

| Capability in development | What it can solve | Boundary and evidence |
| --- | --- | --- |
| Native system-time history | Inspect retained nodes, edges and schema as of a qualified commit or timestamp; understand how project knowledge evolved. | Explicit activation captures a baseline, not pre-activation history. Typed `system_as_of`/`system_versions`; bounded retention and pins. System time only, not valid time or full bitemporality. [History](SYSTEM_TIME_HISTORY.md). |
| System-time graph diff (NHC) | Identify retained schema, node, relationship and property changes between two commits. | `system_diff` compares the same store with explicit bounds and lineage semantics; not arbitrary cross-database diff or row CDC streaming. [Diff API](SYSTEM_TIME_HISTORY.md). |
| Temporal access tree and physical compaction (NHC) | Use native indexed temporal access and reclaim eligible history storage. | Opt-in capability bits 17/18, one-way activation; compaction requires quiescence. No measured speed or space-saving ratio is asserted. [Temporal operations](SYSTEM_TIME_HISTORY.md), [compatibility](V006_COMPATIBILITY.md). |
| Named catalogs and workspace resolution | Route application operations to explicitly attached local stores. | `CatalogSession` pins each transaction to one store. No cross-store edges/joins or distributed transaction. [Catalogs](CATALOGS_AND_WORKSPACES.md). |
| Bounded existing-target copy and endpoint closure | Reuse selected records with durable receipts and indexed idempotent replay; optionally include endpoints of selected relationships. | Atomic target data/receipt, bounded explicit selection; NHC endpoint closure is one hop. Not distributed source/target atomicity. Historical copying requires explicit current-only policy. [Copy](CATALOG_COPY.md). |
| Typed logical views and nullable columns | Reuse read definitions and add optional fields to an evolving application's schema. | Logical views execute against snapshots and replace atomically. Nullable append preserves prior row layouts without a heap rewrite; neither means arbitrary schema migration. [Views](LOGICAL_VIEWS.md), [columns](NULLABLE_COLUMNS.md). |
| Exact phrases and durable token positions | Search terms in order; return analyzed token evidence for consumer-side explanations. | Opt-in positional postings, bounded candidate verification. Returned positions are token ordinals, not original character offsets or only matched phrase spans. [FTS](FULL_TEXT_SEARCH.md). |
| Ordered proximity and analyzer replacement (NHC) | Match ordered phrases with bounded gaps and change an index's analyzer/options under the same name. | `slop` requires durable positions; no phrase/prefix combination. Foreground fresh-generation replacement is atomic, may conflict with writers and uses the current analyzer even for an older data snapshot. [FTS](FULL_TEXT_SEARCH.md). |
| Posting-hash indexes | Share repeated property keys per physical page and bound repeated-key decoding. | Opt-in index layout; preserves WAL/OCC/verification. Not a universal throughput or write-amplification improvement. [Posting hash](POSTING_HASH.md). |
| CLI inventories and text/vector/hybrid search | Inspect local schemas, indexes, catalogs and bounded search results from scripts. | Machine-readable local CLI; JS/TS subprocess recipe is not a native network driver. [CLI](CLI.md). |
| Local SQLite ingestion and offline HTML snapshots | Bring data from a closed local SQLite source into Grafx and inspect/export a graph picture. | Bounded whole-call atomic staging; script-free static HTML, not an interactive graph console. [SQLite](LOCAL_SQLITE_IMPORT.md), [HTML](HTML_SNAPSHOTS.md). |
| Scalar functions, UNION ALL and topological ordering | Normalize values, preserve duplicate rows across branches and examine directed dependency order. | Native functions/list expressions and up to 64 read/write returning branches with identical ordered column names, or all-unit writing branches. Branches share ordered private effects and rollback. Topological results explicitly report blocked nodes when cycles prevent a full order. [Queries](QUERY_LANGUAGE.md), [algorithms](GRAPH_PROJECTIONS.md). |

### Competitor interpretation of these additions

These deltas correct Grafx's status; they do not automatically prove a unique
feature. Ladybug supports attached stores, richer types and an algorithm extension.
Neo4j has a broad query/procedure ecosystem, GDS and edition-specific CDC and
operations. Temporal values or CDC alone do not establish equivalence to Grafx's
retained system-time APIs; conversely, this review does not establish absence of
all historical-graph solutions in those ecosystems.
[Ladybug attach](https://docs.ladybugdb.com/cypher/attach/),
[Ladybug algorithms](https://docs.ladybugdb.com/extensions/algo/),
[Neo4j GDS](https://neo4j.com/docs/graph-data-science/current/introduction/),
[Neo4j CDC](https://neo4j.com/docs/cdc/current/).

## 6. Practical assessment

The following are architectural inferences from the contracts above, not measured
rankings:

| Need | Strongest fit to evaluate first | Important qualification |
| --- | --- | --- |
| Python local-first application with independent direct-file readers/writers | Grafx | Its distinctive ownership contract addresses this case, but the query subset, pre-alpha status, contention and upgrade policy still require acceptance testing. |
| Embedded graph analytics with broad typed data and external file/store integration | Ladybug | Its analytical execution and connectors make it a strong candidate; do not assume multi-process writable ownership from its multiple-connection API. |
| Shared graph service, multiple language clients, enterprise availability/security | Neo4j | Server operation is part of the deployment; required EE/GDS features introduce separate edition/license decisions. |
| Bounded lexical/vector/graph fusion without a separate database service | Grafx | Native snapshot-bound hybrid API is useful; one-table targeting and analyzer limits can be decisive constraints. |
| Extensive analytics/ML pipeline ecosystem | Neo4j with GDS, or Ladybug for embedded algorithms | Grafx's implemented algorithm set is useful but smaller; an algorithm name alone does not establish equal scalability, parallelism or semantics. |

### What Grafx offers that deserves emphasis

- Independent local participant ownership, explicit optimistic conflicts and
  durable-outcome reporting, rather than hiding a server inside the Python API.
- Native bounded FTS/hybrid/vector APIs with snapshot-aware contracts, cancellation
  and admission limits. These are useful integration controls, not a claim that
  competitors lack equivalent controls in other forms.
- Explicit provenance and copy/recovery identity: a physical replacement and an
  independently writable logical copy are deliberately different operations.

### Where Grafx is less complete

- Query language and stored type breadth, language drivers and graphical tooling.
- Remote federation, distributed availability and built-in authentication/authorization.
  The 0.0.6 development line now has local attached catalogs and bounded atomic
  copy, but no distributed transaction or federated query. [Catalogs](CATALOGS_AND_WORKSPACES.md),
  [copy boundaries](CATALOG_COPY.md).
- Valid-time and full bitemporal graph queries remain absent. Retained system-time
  reads, versions, diffs, an optional index and physical compaction are implemented
  in the 0.0.6 baseline; they are no longer listed as missing capabilities.
- Text retrieval still lacks original-character highlighting, language stemming
  and stopword configuration. Phrase/proximity and token positions close narrower
  gaps, not the broader language-analysis capabilities of Lucene or Ladybug FTS.
- Ecosystem and operational evidence: release publication and a large passing
  test suite do not establish production maturity or large-graph SLOs.

These gaps are now explicitly registered in the
[comparative gap register](../ROADMAP.md#comparative-feature-gaps-and-minimum-parity).
Its bounded minimum-parity candidates distinguish inexpensive API/tooling exposure
from new engine guarantees. Registration is not implementation approval, a version
promise, or permission to change Pulse. The roadmap remains the sole backlog.

## 7. Performance and migration boundaries

No common dataset, hardware, cache state, durability policy or query-result
contract was benchmarked across these three products for this comparison.
Consequently, **no overall speed ranking, throughput ratio or claim that Grafx
matches/exceeds Ladybug or Neo4j is supported here**. The existing
[Grafx performance evidence](PERFORMANCE.md) records specific builds and workloads;
historical 0.16.0 Ladybug measurements must not be relabeled as 0.20.3 results.

For a future selection test, measure the actual application's first KG page,
continuation pages, exact counts and edge completeness alongside concurrent
ingestion. Compare equal result sets, p50/p95/p99, memory, bytes written, durable
acknowledgement and crash/reopen behavior. Separate native calls from adapter/API/UI
time. Search comparisons also need equal filters, analyzer semantics and recall
targets; ANN is not interchangeable with exhaustive exact retrieval.

Do not migrate by replacing an import or changing a database filename. Validate
query syntax, parameter/result types, transaction retries, pagination identity,
search consistency, maintenance and schema/format activation. Grafx's public
contracts describe these boundaries in [integration](INTEGRATION.md),
[queries](QUERY_LANGUAGE.md) and [0.0.6 compatibility](V006_COMPATIBILITY.md).
Opt-in native layouts require compatible binaries for every participant; accepting
the development source does not make those layouts readable by the 0.0.5 wheel.

## Maintenance

Recheck this comparison when Grafx's release or a competitor's edition/version
changes. In particular, revalidate Ladybug's documented versus enabled multiwrite
semantics, Neo4j edition-dependent features and any claimed new temporal/cluster
capabilities. Linked rolling documentation is evidence accessed on the review
date, not a frozen promise about future releases.

### September 14, 2026 refresh record

- Reconciled release status after the 0.0.6 publication (tag `v0.0.6`, PyPI
  September 13, 2026): the comparison baseline is the published 0.0.6, and the
  development source is now 0.0.7 with no comparison-relevant additions yet.

### September 10, 2026 refresh record

- Promoted the main comparison baseline to validated 0.0.6 development; retained
  the published 0.0.5 reference explicitly rather than mixing release availability.
- Added NHC positional/proximity, temporal diff/access/compaction, endpoint closure
  and analyzer replacement with the acceptance and format boundaries above.
- Rechecked official Ladybug release, concurrency, transactions, vector/FTS/algo
  documentation and Neo4j edition, isolation, vector, GraphRAG, GDS and backup
  documentation. Ladybug 0.20.3 remains the pinned release comparison; rolling
  docs are not assumed to be release-frozen.
- Preserved the documented-versus-source Ladybug multiwrite qualification;
  retained primary source links instead of turning that ambiguity into a negative
  capability checkbox.
- No shared workload benchmark or competitor conformance test was performed.
