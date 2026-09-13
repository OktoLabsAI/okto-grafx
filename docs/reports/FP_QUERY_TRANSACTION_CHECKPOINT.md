# Grouped query/transaction checkpoint follow-up

September 12, 2026. **Corrective collection qualified; full parity checkpoint remains open.** The grouped command below
completed with **6,171 passed / 5 failed**, no errors/skips, 1,869.641 seconds,
terminal exit 1. Receipt `.grafx-tmp/fp-query-transaction-checkpoint.xml`, SHA-256
`715e207a3ab8bc54d5c1eb4bca3cc7dad36ea7847c07f2872bc0cd942a3ca59e`.
This is the complete selected query/transaction collection, not the full repository.
Its failures are retained; this report does not relabel that execution as passed.

```powershell
$env:PYTHONPATH='src;.grafx-tmp/v006-optional-test-deps'
python -m pytest tests/query tests/txn -q --tb=short --junitxml=.grafx-tmp/fp-query-transaction-checkpoint.xml
```

## Schema-refusal fixture migration

The independently reproduced failure is
`test_a_refused_statement_poisons_nothing_and_the_transaction_still_commits` in
`tests/query/test_schema_refusal_atomicity.py`. Its original scenario expected
`GrafxIndexError` from case-folded `Person`/`person` vector index names. The current
physical-owner namespace contract permits these names in catalog v2 and refuses
their use in a legacy catalog as `GrafxConfigurationError`, `field=format_version`.
The baseline receipt preserves that exact mismatch:
`.grafx-tmp/fp-checkpoint-schema-refusal-baseline.xml` (one failed test).

The test now activates catalog v2 and injects a typed failure **after actual vector
index attachment**, preserving the late-DDL failure point. It asserts the injected
error and that the injection was reached exactly once, retries the exact typed DDL,
commits distinct rows in both tables, verifies the database and cold-reopens it.
Pure and NumPy variants pass. This proves the failure did not leave a phantom table
or unusable index claim and did not discard earlier successful statements.

An undeclared-label CREATE is no longer used as a negative probe: it is legitimate
under the authorized flexible graph model. A separate legacy-catalog test preserves
the current explicit activation refusal and verifies the preceding schema/data can
still commit without adopting the refused table. No production error handling,
capability gate or rollback implementation was weakened to accommodate the test.

All six tests in the file pass, zero failures/errors/skips, 3.870 seconds, terminal
exit 0. Receipt `.grafx-tmp/fp-checkpoint-schema-refusal-qualified.xml`, SHA-256
`7512fd50cd8ac10bb1766c3e1eb366e9a509153e87fbb7422c6caa9dd4c92864`.
Only unused-import/docstring cleanup follows this run; Ruff passes. The grouped
process collected the earlier fixture, so its final result retains that failure
and must not be presented as rerunning the corrected file.

## Other identified fixture/consumer mismatches

| Failing test | Current contract and corrective verification |
| --- | --- |
| `test_a_column_attached_by_the_statement_is_searchable_at_once` | The fixture adds a second physical owner to an existing vector space. Its direct `stage_insert` must supply the actual Note table ID. The query still has to find the inserted vector through the newly attached owner. |
| `test_unwind_alias_is_a_value_and_never_a_set_or_delete_target` | DELETE now admits expression targets at analysis and rejects this scalar during typed planning; SET still rejects a proven map carrier at analysis. Replacement cases pin both phases and exact SET/DELETE error details, and verify failed-instruction writes are absent while a preceding successful statement commits. |
| `test_pending_vector_parameter_tuple_is_not_a_quota_attribute_error` (two modes) | Numeric lists are now valid vector SET inputs. Replacement coverage checks list/tuple carriers, optimized/full landing paths and explicit commit/rollback, native owner-visible vector values and released budget accounting. Existing invalid-vector storage regressions remain included. |

These changes do not modify production engine code or weaken the supported model.
The grouped process ran the prior test versions. The corrective collection includes
all of `test_query_engine`, `test_schema_refusal_atomicity`, `test_unwind`,
`test_entity_unwind`, `test_vector_free_traversal` and `test_set_vector_storage`;
all 427 tests now pass together as recorded below.

## Corrective qualification

The first 427-test corrective run passed 426 and failed one new assertion which
incorrectly assumed map-target SET must reach typed planning. The existing earlier
analysis refusal is retained: the final test pins SET's analysis error and DELETE's
typed-planning error separately, then checks both actual transaction paths.
No production semantics changed. Receipt
`.grafx-tmp/fp-checkpoint-corrective-regression.xml`, SHA-256
`fea359f6de558fd210872bd545006fa1feb2d5e6bd5300cd5bcd641962c86d04`.

Focused UNWIND/native-entity/DELETE regression: **84 passed**, no failures/errors/
skips, 42.884 seconds, terminal exit 0. Receipt
`.grafx-tmp/fp-checkpoint-unwind-final.xml`, SHA-256
`f4b2fc3025087b5ada17e90d6f548649887c2fb6e156553dbbacdff33566f333`.

Final complete corrective collection: **427 passed**, no failures/errors/skips,
113.887 seconds, terminal exit 0. Receipt
`.grafx-tmp/fp-checkpoint-corrective-final.xml`, SHA-256
`e82977e63ac9cac4f3a5a17b39f71c4316364baaf0f36ad2a3f7c01959dbcca7`.
All original failure causes are covered; counts overlap the grouped baseline and
focused reruns and must not be summed. The original 6,176-case execution remains
a failed historical run, not a fresh green full selection. Both processes have
terminated; no regression process is left running by this increment.

## Remaining work

Affected regressions are qualified. The full frozen profile and required final
checkpoint remain open; this increment does not certify the whole repository or
the unimplemented public DECIMAL/storage/typed-collection/procedure packages. No expected
failure waiver, TCK profile exclusion or model-policy change is introduced here.
The pending multiple-label/Set1 oracle decisions are separate from this fixture
migration. The full functional-parity objective remains open.
