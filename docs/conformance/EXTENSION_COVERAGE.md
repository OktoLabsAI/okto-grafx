# Frozen supplemental contracts: execution coverage

[Frozen requirements](EXTENSION_SCENARIOS_V1.json) · [Profile V3](PROFILE_V3.md)
· [Delivery plan](../specs/FUNCTIONAL_PARITY_PLAN.md#fp-8--qualify-the-profile-and-update-pulse)

This maps the existing **22** requirements to their native tests and external
evidence. It introduces no new functionality, scope exclusions or performance
thresholds. The frozen JSON remains unchanged, SHA-256
`b87801ba4952bbe9d4921edbf7aeea84f1ef8bea54fb40f9b4d50d1be026f3a0`.

**Current status: native rows 1–20 reconciled with the complete passing regression;
row 21 qualified separately; row 22's Neo4j execution explicitly deferred by the
user, not counted as passed.** A source path alone
is coverage, not a passing receipt. The [final native qualification](../reports/FP_FINAL_NATIVE_QUALIFICATION.md)
records 25,077 passes, 19 attributed skips, unchanged-input proof and all 51 mapped
native modules passing. The separate required-profile execution passes all 3,896
required cases. Neither those results nor the three literal examples substitute
for the two external requirements below.

## Native coverage map

Paths below are executable tests. Linked support/qualification documents specify
the supported and explicitly refused combinations, rather than assuming a type
is accepted by every consumer.

| # / frozen ID | Concrete native evidence and important assertions |
| --- | --- |
| 1 / FP3-ENTITY-IDENTITY | [Identity/incarnation](../../tests/query/test_fp3_entity_identity.py), [native UNION/grouping/nesting](../../tests/query/test_fp3_entity_union.py), [native entity results](../../tests/query/test_fp3_native_entity_results.py): store/table/kind identity, delete/recreate, same-valued distinct nodes, old snapshots, branch multiplicity and spill. |
| 2 / FP3-ENTITY-DETACH | [Detached entities](../../tests/query/test_fp3_entity_values.py): owned nested properties, exact tagged JSON, no retained transaction or foreign/stale parameter write authority; [procedure entity witnesses](../../tests/api/test_procedure_entities.py) reject forged, previous-invocation and foreign references. |
| 3 / FP3-GRAPH-SCALARS | [Graph scalars](../../tests/query/test_fp3_entity_scalars.py), [native entity results](../../tests/query/test_fp3_native_entity_results.py), [label mutations](../../tests/query/test_node_label_assignments.py): exact properties/NULL omission, labels/type/path components and length, including empty/multiple labels. Original graph-expression TCK cases remain separately required. |
| 4 / FP3-POLYMORPHIC | [Composed patterns](../../tests/query/test_fp3_composed_polymorphic.py), [polymorphic hops](../../tests/query/test_fp3_polymorphic_hops.py), [ranges](../../tests/query/test_fp3_polymorphic_ranges.py): compatible physical owners, optional landings, absent-property NULL, nested composition and exact multiplicity/identity. |
| 5 / FP3-PATH-TRAIL | [Named paths](../../tests/query/test_named_path.py), [independent exhaustive range oracle](../../tests/query/test_fp3_polymorphic_ranges.py), [written paths](../../tests/query/test_written_paths.py): direction, zero hops, cycles/repeated nodes, clause-wide edge uniqueness, staged overlays, spill and explicit exhaustion rather than truncation. |
| 6 / FP4-UNIT-CARDINALITY | [Literal frozen query](../../tests/query/test_frozen_extension_queries.py) executes the exact setup/query/columns/rows/effects from the JSON; [writing subqueries](../../tests/query/test_write_subqueries.py) additionally cover empty/multiple rows, nested unit calls and downstream visibility. |
| 7 / FP4-RETURNING-ZERO | [Literal frozen query](../../tests/query/test_frozen_extension_queries.py) keeps only the inner returning row; [writing subqueries](../../tests/query/test_write_subqueries.py) distinguish zero returned rows from successfully staged writes and preserve invocation cardinality. |
| 8 / FP4-LEADING-WITH-IMPORT | [Literal frozen query](../../tests/query/test_frozen_extension_queries.py) returns the exact aliased result; [import scope tests](../../tests/query/test_subquery_imports.py) independently distinguish leading WITH imports from explicit global imports. |
| 9 / FP4-SCOPES | [Import scope tests](../../tests/query/test_subquery_imports.py): missing/explicit/star imports, shadowing, branch-local names, nested empty imports, zero-row aggregation, spills and snapshot-bound cursor imports. Invalid scopes fail before effects. |
| 10 / FP4-ATOMIC-WRITES | [Writing subqueries](../../tests/query/test_write_subqueries.py), [recovery cuts](../../tests/txn/test_write_subquery_recovery.py): late inner/final-output failures, shared budgets, cancellation and statement rollback preserve earlier valid work, with one outer COMMIT and no inner commit. |
| 11 / FP4-READONLY-CONCURRENCY | [Writing subqueries](../../tests/query/test_write_subqueries.py), [recovery cuts](../../tests/txn/test_write_subquery_recovery.py): read-only/cursor refusal precedes effects; an independent reader retains its snapshot, a competing winner forces OCC conflict, durable reopen sees only the winner. |
| 12 / FP5-TEMPORAL-EXACT | [Native temporal queries](../../tests/query/test_temporal_functions.py), [operations](../../tests/query/test_temporal_operations.py), [arithmetic](../../tests/storage_core/test_temporal_arithmetic.py), [between](../../tests/storage_core/test_temporal_between.py), [zoned values](../../tests/storage_core/test_temporal_zoned_values.py): exact coordinates, month-end/leap/DST and signed duration; the 1,004 original temporal TCK cases remain separate evidence. |
| 13 / FP5-TEMPORAL-PERSISTENCE | [Native storage](../../tests/api/test_temporal_native_storage.py), [activation](../../tests/api/test_temporal_value_activation.py), [indexes](../../tests/api/test_temporal_indexes.py), [transfer](../../tests/api/test_temporal_transfer.py), [type package qualification](../reports/FP6_TYPE_WHEEL_QUALIFICATION.md): all six families, rollback/reopen/COMMIT recovery, retained history, backup and exact old-reader refusal. |
| 14 / FP6-DECIMAL | [Value/context independence](../../tests/storage_core/test_decimal_values.py), [conversions/aggregates](../../tests/storage_core/test_decimal_conversion_aggregation.py), [native numeric queries](../../tests/query/test_decimal_numeric.py), [spill](../../tests/query/test_decimal_spill.py), [storage](../../tests/api/test_decimal_native_storage.py): p/s limits, exact coefficients, declared rounding, overflow and ordering/hash without implicit DOUBLE arithmetic. |
| 15 / FP6-NESTED-STORAGE | [Owned descriptors](../../tests/storage_core/test_typed_collection_schema.py), [native columns](../../tests/api/test_typed_collection_storage.py), [queries](../../tests/query/test_typed_collection_queries.py), [consumers](../../tests/api/test_typed_collection_consumers.py): all four collection families, NULL/nesting/depth/size, exact ARRAY/STRUCT shape and mutation-proof snapshots. [ANY tests](../../tests/query/test_any_properties.py) retain lists of maps and reject nested nonfinite storage. |
| 16 / FP6-TYPE-TRANSFER | [Consolidated support matrix](../TYPE_SUPPORT.md) and its linked native/text/columnar receipts; [temporal](../../tests/api/test_temporal_tabular.py), [decimal](../../tests/api/test_decimal_tabular.py), [collection columnar](../../tests/api/test_typed_collection_tabular.py), [collection text](../../tests/api/test_typed_collection_text_import.py), [collection consumers](../../tests/api/test_typed_collection_consumers.py) and [CLI](../../tests/cli/test_typed_collection_json.py): exact declared types and pre-effect refusal across the named consumers, including history/copy. |
| 17 / FP6-FORMAT-ADMISSION | [Temporal](../../tests/storage_core/test_temporal_codec.py), [decimal](../../tests/storage_core/test_decimal_codec.py), [collections](../../tests/api/test_typed_collection_storage.py), [label transfer](../../tests/api/test_node_label_transfer.py), [type wheels](../reports/FP6_TYPE_WHEEL_QUALIFICATION.md) and [label wheels](../reports/FP_NODE_LABEL_WHEEL_QUALIFICATION.md): pure/NumPy, corrupt/truncated frames, tagged JSON, old live readers/writers, pending COMMIT and all-file non-mutation. |
| 18 / FP7-WRITE-CAPABILITY | [Writing procedures](../../tests/api/test_writing_procedures.py), [recursion/effects](../../tests/api/test_procedure_recursion.py), [native query authority](../../tests/api/test_procedure_queries.py), [schema authority](../../tests/api/test_procedure_schema.py): declared permissions/mode/types/determinism, cumulative quotas, expiry/thread/foreign-owner checks and no commit/store-switch door. |
| 19 / FP7-CALL-SHAPES | [Unit procedures](../../tests/api/test_unit_procedures.py), [invocation](../../tests/api/test_procedure_invocation.py), [recursion](../../tests/api/test_procedure_recursion.py), [entities](../../tests/api/test_procedure_entities.py), [native values](../../tests/api/test_procedure_native_values.py), [decimal values](../../tests/api/test_procedure_decimal_values.py): standalone/composed CALL, named/star YIELD, nested calls and validation of unselected output columns. |
| 20 / FP7-FAILURE-ISOLATION | [Writing procedures](../../tests/api/test_writing_procedures.py), [unit procedures](../../tests/api/test_unit_procedures.py), [recursion](../../tests/api/test_procedure_recursion.py): callback/iteration/close/final-output failure, primary error preservation, rollback, pre-callback denial and independent participants. [Public contract](../EXTENSIONS_AND_ARROW.md) states no implicit callback retry, sandbox or external-side-effect rollback. |

## External requirements — Pulse qualified, Neo4j deferred

The [current Pulse Python consumer checkpoint](../reports/FP_PULSE_CURRENT_VALUES_QUALIFICATION.md)
has 255 grouped source and 65 installed-package passes for exact values, native
identity, fenced writes and transfer membership admission. It is not the real
HTTP/browser/MCP requirement in row 21 and does not replace that final check.
Its subsequent current-candidate checkpoint passes 11 HTTP requests, real-browser
500 → 510 pagination, Key Decisions and node detail. The later
[operational qualification](../reports/FP_PULSE_OPERATIONAL_QUALIFICATION.md) adds
121 source/158 installed passes, source lifecycle, Settings/schema, nine actual
MCP calls, post-barrier recovery by the next ordinary writer and real-browser
semantic search. The [final installed candidate](../reports/FP_FINAL_PULSE_QUALIFICATION.md)
refreshes 158 installed tests, 12 HTTP checks, Settings/schema, recovery-witness
readback, nine MCP calls and real-browser pagination/search/detail on the final
runtime payload. These qualify row 21's exercised flows. Full native regression
has its own receipt; the row-22 comparison cannot be inferred from Pulse.

| # / frozen ID | Required final evidence |
| --- | --- |
| 21 / FP8-PULSE | Qualified: [final paired wheels and actual API/browser/MCP](../reports/FP_FINAL_PULSE_QUALIFICATION.md), with [source lifecycle, Settings/schema and ordinary-writer recovery](../reports/FP_PULSE_OPERATIONAL_QUALIFICATION.md), exact payload/delta proof and agnostic Core boundary checks. Earlier receipts retain their original candidate and are not relabeled as fresh executions. |
| 22 / FP8-COMPETITORS | Equivalent bounded scenarios, exact values/types/multiplicity/effects/errors and recorded dialect adaptations on the [pinned references](REFERENCE_VERSIONS_V1.json). Docker's Linux engine was unavailable during the current preflight; no Neo4j execution or comparative pass is claimed from image availability metadata. Grafx tests continue independently. |

The [bounded Ladybug execution](../reports/FP_BOUNDED_LADYBUG_COMPARISON.md) now
completes all 16 observations on pinned Ladybug 0.20.3: seven matches and nine
documented differences, zero unavailable scenarios. Its missing DLLs were supplied
privately with publisher hash/signature verification, not a global installation.
Native current-candidate reconciliation is complete. Neo4j execution remains
unperformed and is no longer a current delivery condition by
[explicit user decision](../specs/FUNCTIONAL_PARITY_PLAN.md#delivery-decision-defer-neo4j-execution).
Offline tests of its observer are not a competitor-engine result. The frozen
22-ID inventory is preserved; deferral is not a synthetic pass or a native waiver.

## Literal examples: completed focused execution

The three JSON entries containing literal queries execute unchanged with pure and
NumPy codecs, independent row/column and stored-effect observations, checkpoint and
pure read-only reopen. A separate assertion accounts for the exact three IDs so a
new or missing literal case cannot silently disappear from this selection.

Receipt `.grafx-tmp/fp-frozen-extension-queries.xml`: **7 passed**, zero
failures/errors/skips, 3.597 s; SHA-256
`262eb3e54fc7bcc5fc9f5cb0479d15443482ce39946b900f2e30e574298fe269`.
This is the three literal examples on two codecs plus one inventory assertion,
**not 22 supplemental contracts passed**. Final acceptance must inspect the complete
regression receipt and each external result, not infer success from this map.
