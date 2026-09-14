# Documentation index

The consumer guides include source version **0.0.7 development** additions, marked
with their implementation and acceptance status; entries marked "0.0.6 development"
shipped in the published 0.0.6 release. They do not assert that the latest source is
installed in a particular application.

## Integrate without reading engine internals

[Procedure schema authority](specs/PROCEDURE_SCHEMA_AUTHORITY_V1.md) covers explicit
permissions, schema_write/max_schema_statements, native DDL, flexible CREATE/MERGE
and composable catalog-v2 indexes; [qualification](reports/FP7_SCHEMA_QUALIFICATION.md).

[Procedure nesting and effects](specs/PROCEDURE_NESTING_EFFECTS_V1.md) document
recursive registered CALL, max_call_depth, default-volatile callbacks, shared root
budgets and upgrade guidance; [qualification](reports/FP7_NESTING_QUALIFICATION.md).

[Procedure query authority](specs/PROCEDURE_QUERY_AUTHORITY_V1.md) covers graph_read,
ProcedureReader/ProcedureResult, returning writer queries, cumulative limits and
shared snapshot/clock/cancellation semantics; [qualification](reports/FP7_QUERY_QUALIFICATION.md).

[Writing procedures](specs/WRITING_PROCEDURES_V1.md) explain explicit mode and
permissions, ProcedureWriter usage, shared budgets, revocation, rollback and
remaining limits; [qualification](reports/FP7_WRITING_QUALIFICATION.md).
The subsequent [native value signature contract](specs/PROCEDURE_NATIVE_VALUES_V1.md)
covers temporal/vector families, LIST/MAP/ANY, callback-owned copies, budgets and
native persistence/recovery; [qualification](reports/FP7_NATIVE_VALUES_QUALIFICATION.md).

[Entity procedure signatures](specs/PROCEDURE_ENTITY_SIGNATURES_V1.md) document
NODE/RELATIONSHIP/PATH and typed entity lists, invocation-local identity, native
downstream writes, budgets and refusal of copied/foreign/expired observations;
[qualification](reports/FP7_ENTITY_QUALIFICATION.md).

[Native EXISTS subqueries](specs/EXISTS_SUBQUERIES_V1.md) document implicit imports,
non-exporting scopes, cardinality, short-circuiting, read-only admission and shared
snapshot/budget behavior through the existing query APIs.

Current FP-4 development: [whole-property SET maps](specs/SET_PROPERTY_MAPS_V1.md)
documents map replacement/overlay, conditional actions, constraints and qualification.
The subsequent [expression/path DELETE](specs/DELETE_EXPRESSIONS_V1.md) contract
covers native selectors, bounded preparation, transaction behavior and plan API changes.
[Written paths and MERGE phases](specs/WRITE_PATTERN_CONTRACTS_V1.md) document
named CREATE/MERGE, binding errors, owner-visible clause ordering, bound-edge
undirected matching, native endpoint functions and sorted write bindings.
The same contract now covers flat native large-CREATE programs, syntax admission
and immutable instruction snapshots without per-pattern operator recursion.

| Question | Read |
| --- | --- |
| What does the complete current native TCK run prove and what remains? | [All 3,896 required V3 cases pass; exact upstream divergence and remaining gates](reports/FP_V3_INTEGRATED_QUERY_QUALIFICATION.md) |
| Did the final complete native regression and supplemental contracts pass? | [25,077 passing tests, unchanged-input proof, all 20 native contract maps and fixed FP-1–FP-8 audit](reports/FP_FINAL_NATIVE_QUALIFICATION.md); Neo4j execution is explicitly deferred, not a current delivery gate. |
| What was verified on the final installed Pulse and Grafx pair? | [158 installed tests and real API/UI/MCP, exact package identity and recovered data](reports/FP_FINAL_PULSE_QUALIFICATION.md). |
| What are the latest measured native and isolated Pulse costs? | [Current performance table](PERFORMANCE.md), [fixed native graphs, queries, method and receipts](reports/FP_NATIVE_COST_OBSERVATIONS.md). |
| How do I register a procedure that returns no result rows? | [Unit procedures and invocation/rollback contract](specs/UNIT_PROCEDURES_V1.md) |
| How do standalone CALL, implicit arguments and YIELD * resolve? | [Native procedure invocation and diagnostics](specs/PROCEDURE_INVOCATION_V1.md) |
| What did the preceding procedure invocation checkpoint qualify? | [FP-7 invocation qualification and subsequent numeric follow-up](reports/FP7_INVOCATION_QUALIFICATION.md) |
| How do NUMBER and DOUBLE procedure inputs/results preserve or convert numeric values? | [Numeric signatures, precision and 52-case native qualification](specs/PROCEDURE_NUMERIC_SIGNATURES_V1.md) |
| How do native decimal columns, numeric queries, indexes and consumers work? | [Exact decimal contract and support matrix](specs/DECIMAL_VALUES_V1.md), [storage qualification](reports/FP6_DECIMAL_NATIVE_QUALIFICATION.md), [numeric query/index qualification](reports/FP6_DECIMAL_QUERY_QUALIFICATION.md), [history/copy/transfer qualification](reports/FP6_DECIMAL_CONSUMER_QUALIFICATION.md), [CLI/text/procedure qualification](reports/FP6_DECIMAL_INTERFACE_QUALIFICATION.md), [columnar contract](EXTENSIONS_AND_ARROW.md#exact-native-decimals-006-development) and [qualification](reports/FP6_DECIMAL_COLUMNAR_QUALIFICATION.md) |
| Are typed LIST/MAP/ARRAY/STRUCT columns already available? | [Native StoredType/ColumnDef/DDL, catalog capability, assignment and consumers](specs/TYPED_COLLECTIONS_V1.md), [native qualification](reports/FP6_TYPED_COLLECTION_NATIVE_QUALIFICATION.md), [exact JSON/CSV/JSONL/SQLite consumption](COLLECTION_JSON.md); [exact Arrow/Pandas/Polars/Parquet collections](COLLECTION_COLUMNAR.md); final native/Pulse evidence above. |
| Which native types work with each key, index, procedure and transport? | [Consolidated support/refusal matrix](TYPE_SUPPORT.md) |
| Has the combined temporal/decimal/collection package been tested with installed old binaries? | [60-case checkpoint C and exact wheels](reports/FP6_TYPE_WHEEL_QUALIFICATION.md), [coordinated upgrade procedure](V006_COMPATIBILITY.md#combined-native-type-upgrade-procedure) |
| What did the multiple-label and nested-storage decisions change in the TCK profile? | [V3 scope, exact divergence and preserved V1/V2 evidence](conformance/PROFILE_V3.md) |
| What is implemented for native multiple labels? | [Storage, query, history, copy and transfer contracts](specs/NODE_LABELS_V1.md); [36 native/36 transfer installed checks](reports/FP_NODE_LABEL_WHEEL_QUALIFICATION.md); [all 3,896 required V3 query cases pass](reports/FP_V3_INTEGRATED_QUERY_QUALIFICATION.md), with final native/Pulse evidence above. |
| Does the installed Pulse render and paginate the graph, including logical edge reads? | [Current HTTP/browser pagination and Key Decisions checkpoint](reports/FP_PULSE_CURRENT_VALUES_QUALIFICATION.md#current-http-and-real-browser-checkpoint); [source lifecycle, Settings, API/MCP, recovery and real-browser search](reports/FP_PULSE_OPERATIONAL_QUALIFICATION.md). [Earlier candidate](reports/PULSE_HTTP_UI_PARITY_QUALIFICATION.md). |
| Does current Pulse consume new exact values and avoid silently dropping labels during transfer? | [255 source / 65 installed adapter checks and explicit output/refusal contracts](reports/FP_PULSE_CURRENT_VALUES_QUALIFICATION.md) |
| Which comparative behaviors were actually executed on a pinned competitor? | [16 bounded Ladybug 0.20.3 observations, exact differences and private dependency provenance](reports/FP_BOUNDED_LADYBUG_COMPARISON.md); Neo4j execution is explicitly deferred, not a current delivery gate. |
| Does Pulse consume whole-pattern MERGE without backend-specific Core changes? | [Source and installed-package qualification](reports/GENERAL_MERGE_PULSE_QUALIFICATION.md) |
| How does MERGE handle unbound endpoints, partial matches and multi-hop paths? | [Whole-pattern MERGE, transactions and resources](specs/GENERAL_MERGE_V1.md) |
| How do tables share a vector space without mixing results? | [Physical vector owners and current limits](specs/VECTOR_PHYSICAL_OWNERS_V1.md) |
| How do I select a shared-space vector owner from the command line? | [CLI selector contract and qualification](reports/CLI_VECTOR_OWNER_QUALIFICATION.md) |
| Can projections, logical views and migration ledgers distinguish same-named nodes and relationships? | [Namespace consumer qualification](reports/NAMESPACE_PROJECTION_VIEW_MIGRATION_QUALIFICATION.md) |
| Do view dependencies include queries inside EXISTS and pattern expressions? | [Expression-local view authority](reports/VIEW_EXPRESSION_DEPENDENCY_QUALIFICATION.md) |
| How are vector index name collisions and old-reader admission handled? | [Durable vector owner names](specs/VECTOR_OWNER_NAMES_V1.md) |
| How do I inspect or rebuild only one shared-space vector owner? | [Qualified vector maintenance](specs/VECTOR_QUALIFIED_MAINTENANCE_V1.md) |
| How is repair of a genuinely missing old vector index validated? | [Component and legacy repair qualification](reports/VECTOR_OWNER_REPAIR_QUALIFICATION.md) |
| What resolved the parity increment's static-contract failures? | [Declaration and boundary qualification](reports/FP_STATIC_CONTRACT_QUALIFICATION.md) |
| How do I retain graph versions, query past commits and protect retention? | [System-time history](SYSTEM_TIME_HISTORY.md) |
| How is the 0.0.6 wheel upgrade/old-reader boundary tested? | [0.0.6 compatibility](V006_COMPATIBILITY.md) |
| Which old logical importers accept or refuse the new artifacts? | [Installed transfer matrix and legacy limits](reports/LOGICAL_TRANSFER_WHEEL_QUALIFICATION.md) |
| How can repeated property keys share physical index storage? | [Posting hash](POSTING_HASH.md) |
| How do I copy data into an existing catalog and safely retry? | [Atomic catalog copy](CATALOG_COPY.md) |
| How do I persist and consume named, typed read queries? | [Logical views](LOGICAL_VIEWS.md) |
| How do I add a nullable column without rewriting the graph? | [Append-only schema evolution](NULLABLE_COLUMNS.md) |
| How do I maintain same-named node and relationship tables? | [Qualified maintenance](OPERATIONS.md#observation-and-maintenance-contracts), [namespace contract and remaining gaps](specs/GRAPH_NAMESPACES_V1.md) |
| How do I attach databases and resolve explicit workspaces? | [Catalogs and workspaces](CATALOGS_AND_WORKSPACES.md) |
| How do I install, create a graph and reopen it? | [Getting started](GETTING_STARTED.md) |
| How do I integrate a synchronous library into services, workers or agents? | [Integration](INTEGRATION.md) |
| Which public methods and return values exist? | [API reference](API_REFERENCE.md) |
| How do native node/edge results expose identity, immutable properties and JSON? | [Detached entity results — FP-3 development](ENTITY_VALUES.md) |
| How do I apply/check versioned application DDL safely? | [Schema migrations](SCHEMA_MIGRATIONS.md) |
| How do I opt into commit provenance and page its history? | [Commit history](COMMIT_HISTORY.md) |
| Which settings exist, what are their defaults and risks? | [Configuration](CONFIGURATION.md) |
| Which Cypher constructs and value types are supported? | [Query language](QUERY_LANGUAGE.md) |
| How do I update properties differently when MERGE creates or matches? | [Conditional MERGE property actions](QUERY_LANGUAGE.md#conditional-merge-property-actions) |
| How do ordered clauses, subqueries and typed CALL/YIELD compose? | [Composable queries](COMPOSABLE_QUERIES.md) |
| What is the fixed language target and what has been tested? | [Cypher compatibility](CYPHER_COMPATIBILITY.md) |
| How do native dates, times, durations, query clocks and temporal operators work? | [Native temporal values and remaining qualification](TEMPORAL_VALUES.md) |
| What remains for the language, stored-type and TCK gaps? | [Functional parity plan — implementation in progress](specs/FUNCTIONAL_PARITY_PLAN.md) |
| Where is the eight-item language round's regression and Pulse migration evidence? | [Language acceptance record](reports/V006_QUERY_LANGUAGE_ROUND.md) |
| How do indexes, vectors and explicit growth work? | [Indexes and vectors](INDEXES_AND_VECTORS.md) |
| How do I index text and search with BM25, filters and snapshot consistency? | [Native full-text search](FULL_TEXT_SEARCH.md) |
| How do I combine lexical, vector and bounded graph evidence? | [Hybrid search](HYBRID_SEARCH.md) |
| How do I capture a graph snapshot and compute components, paths, PageRank or k-core? | [Graph projections](GRAPH_PROJECTIONS.md) |
| How do I export a graph or create a separately writable logical copy? | [Logical export/import](LOGICAL_TRANSFER.md) |
| How do I handle conflicts, uncertain writes, recovery and upgrades? | [Operations](OPERATIONS.md) |
| How can I cancel reads or safely reclaim orphan index files? | [Read control and index cleanup](READ_CONTROL_AND_INDEX_CLEANUP.md) |
| How can a script/agent consume structured results? | [CLI](CLI.md) |
| How do I ingest a bounded local SQLite selection? | [SQLite ingestion](LOCAL_SQLITE_IMPORT.md) |
| How can I inspect a graph offline without a server? | [HTML snapshots](HTML_SNAPSHOTS.md) |
| How can JavaScript/TypeScript consume the CLI safely? | [Subprocess recipe](../examples/cli-consumer/README.md) |
| What has actually been measured? | [Performance](PERFORMANCE.md) |
| How does Grafx compare with Ladybug and Neo4j? | [Feature comparison and trade-offs](FEATURE_COMPARISON.md) |
| What is missing or planned? | [Roadmap](../ROADMAP.md) |

Recommended reading order: tutorial → integration → configuration → operations;
then consult the API/query references for the capabilities your application uses.

## Extend or maintain the engine

- [Trusted scalar extensions and optional Arrow batches](EXTENSIONS_AND_ARROW.md).
- [Typed Pandas frames and local Parquet files](TABULAR_AND_PARQUET.md).
- [Typed Polars recipe](POLARS_RECIPE.md), [local CSV/JSONL](LOCAL_TEXT_IMPORT.md),
  [NetworkX and projection Arrow exchange](GRAPH_EXCHANGE.md).
- [0.0.5 compatibility/upgrade matrix](V005_COMPATIBILITY.md).

[Physical backup and restore](BACKUP_RESTORE.md) covers the bounded local API,
checkpoint fence, manifests, integrity checks and offline replacement obligations.

- [Ports and custom adapters](PORTS.md): trusted in-process extension boundary,
  ownership and examples. This is not a sandbox; scalar UDFs have a separate SPI above.
- [Architecture](ARCHITECTURE.md) and [normative engine contract](architecture/CONTRACT.md).
- [Capability specifications](specs/CAPABILITY_MILESTONES.md): detailed acceptance
  contracts; their existence is not evidence that a feature is callable.
- [Component design record](architecture/COMPONENTS.md), [lessons](architecture/LESSONS.md),
  [contributing](../CONTRIBUTING.md), [security policy](../SECURITY.md).

## Historical evidence, not competing plans

- [Report index](reports/README.md): dated experiments, implementation checkpoints,
  Pulse audits and historical comparisons.
- [Consolidated former plans](archive/ROADMAP_SOURCES.md): eleven complete source
  documents, including evolution, agent-first, complementary, Round 7 and performance
  plans. [Preservation manifest](archive/ROADMAP_SOURCES_MANIFEST.json).
- [Previous README](archive/README_BEFORE_REFACTOR.md): provenance only; superseded.

The **only current queue/status authority is [ROADMAP.md](../ROADMAP.md)**. Reports
remain reproducible evidence, specs define acceptance, and this index routes readers.
Historical “next step”, PID, deadline, approval and performance-gate statements do
not become current instructions by being preserved.
