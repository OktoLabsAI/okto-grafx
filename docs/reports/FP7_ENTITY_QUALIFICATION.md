# FP-7 native entity procedure qualification

Date: September 12, 2026. Source: dirty `feature/v0.0.6` development worktree.
No commit, push, release, global installation or production/Pulse-data operation
is claimed by this qualification. [Contract](../specs/PROCEDURE_ENTITY_SIGNATURES_V1.md).

## Delivered behavior

NODE/RELATIONSHIP/PATH and typed node/relationship list signatures preserve native
references through invocation-local detached observations. Generic native containers
can carry witnessed entities without implicitly asserting a static entity type.
The descriptor resolver supplies output kinds to analysis/planning; native execution
exports observations and restores only exact witnesses from the current invocation.
Properties are re-observed from native state, not accepted as callback write authority.

The 27 feature tests cover properties/identity, path functions/components, typed
UNWIND/SET, NULL, pending nodes with pure/NumPy cold reopen and verification, nested
unlabeled heterogeneous maps, generic containers, UNION/ALL, subqueries/cursors,
copied/retained/foreign and low-level modified observations, byte admission before
callback, post-callback write visibility, unselected late invalid outputs and prior
statement preservation. An independent reader/writer completes while a callback
waits; the waiting reader retains its original snapshot.

The adversarial tests also exposed a pre-existing writing-procedure integration
gap: a successful child DELETE did not notify the outer evaluation context that
an alias's content was no longer live. Child deleted references now propagate to
the parent after successful native execution; child-local insert tokens do not.
Both committed and pending owner references refuse stale content, and the outer
statement rollback restores the deletion while preserving prior statements.
This is not an O(N) rescan of graph data or a new concurrency lock.

## Execution receipts

All listed completed pytest runs have zero errors/skips. Selections overlap; counts
must not be added to claim unique coverage. Failed attempts remain recorded.

| Receipt under `.grafx-tmp/` | Result | Time / exit |
| --- | --- | --- |
| `fp7-entities-baseline.xml` | 115 existing native-value/writing tests passed | 26.642 s / 0 |
| `fp7-entities-first.xml` | 9 passed, 3 failed before reaching feature code | 4.665 s / 1 |
| `fp7-entities-focused.xml` | 12 passed after test-query correction | 2.187 s / 0 |
| `fp7-entities-expanded.xml` | 23 feature tests passed | 2.833 s / 0 |
| `fp7-entities-regression.xml` | 442 passed, 6 diagnostic-contract failures | 104.757 s / 1 |
| `fp7-entities-contracts.xml` | 363 feature/public-annotation/adapter/import tests passed | 16.985 s / 0 |
| `fp7-entities-diagnostics.xml` | 74 numeric/entity tests passed after production fix | 7.020 s / 0 |
| `fp7-entities-authority.xml` | 25 passed, 1 child-DELETE propagation failure | 4.001 s / 1 |
| `fp7-entities-delete.xml` | 104 entity/deleted-content/writing tests passed after production fix | 31.494 s / 0 |
| `fp7-entities-full-contracts.xml` | 1,455 entity/annotation/import/language tests passed | 40.968 s / 0 |
| `fp7-entities-final-regression.xml` | 452 combined feature/query/deletion/spill/cache tests passed | 104.529 s / 0 |

The initial three test queries omitted the existing mandatory WITH separator
between CREATE and CALL. Tests were corrected; parser policy did not change.
The six numeric failures showed that the entity bridge intercepted nonfinite
scalar inputs and changed the established `udf_type` diagnostic to `procedure_value`.
Scalar arguments now retain their existing signature validator without duplicate
copying/validation; numeric tests were not weakened. The deletion failure produced
the native child-to-parent invalidation fix described above.

SHA-256 receipts:

```text
fp7-entities-baseline.xml 335f4bd94a46090a7633e8e73525facbc8d8ec418c82549df48c29a316382f25
fp7-entities-first.xml a49416fd86b0dc5d0c3eeb6a352c5f0af44f72ed2f4d9e032684e1b946a4d8f6
fp7-entities-focused.xml b033fecbd7f6197d3a676416199846a5beeab845f819b4b13a9b7adf1b0369af
fp7-entities-expanded.xml e923951a144a38bb1d4709b9581089f5e9292010f8bf3d732cae1e0464b21f73
fp7-entities-regression.xml 363045756fe1f7ad3c3479cb052c87a3cdc4fa6bb4383f75a3da02f793a155e1
fp7-entities-contracts.xml 528a1cc483468861a9c533c91a4397bde613deaa8dd428209742a4256e02102b
fp7-entities-diagnostics.xml f2151941727766f9722ea01474c2cda4a03388d035c63ffa7ee4a174e74e8832
fp7-entities-authority.xml ffd3a9848e8ea4246e59fb235f2454b3c3b3a2cfe1e6a9dfdc8a9477348dfb49
fp7-entities-delete.xml 4e5030218e47d658faee0023e01416d58bfff64d46bbf5090aa2e8018513d3d5
fp7-entities-full-contracts.xml 39ffbc18bc137dc6bbbfd117bff9fab5498de6ffab97348aadc205d5f6e37632
fp7-entities-final-regression.xml 0dba86255a0d542c261b1b5f6f5b8e929a7ced14263384207dec04e059090d90
```

The separate original FP-7 TCK selection still passes **52/52**, exit 0. Receipt
`fp7-entities-native-20260912.json`, SHA-256
`74dafef0cbd3c7ce9bb64ca5db19fd41830908e24da2237cac194f335433446e`, is byte-identical
to the preceding owner results. The other 3,845 inventory entries are unselected,
not newly waived or certified. Pinned revision, V2 ledger/V1 predecessor and
architectural exclusions are unchanged.

## Reproduction and remaining work

Set `PYTHONPATH=src;.grafx-tmp/v006-optional-test-deps` and run:

```text
python -m pytest tests/api/test_procedure_entities.py tests/api/test_procedure_native_values.py tests/api/test_procedure_invocation.py tests/api/test_procedure_numeric_signatures.py tests/api/test_tabular_procedures.py tests/api/test_unit_procedures.py tests/api/test_writing_procedures.py tests/query/test_entity_unwind.py tests/query/test_deleted_entity_access.py tests/query/test_fp3_entity_values.py tests/query/test_fp3_entity_union.py tests/query/test_fp3_entity_identity.py tests/query/test_query_memory_spill.py tests/query/test_prepared_plan_cache.py -q --tb=short
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/fp7-entities-native-20260912.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --owner FP-7
```

README, roadmap, API/configuration, entity/query/composition/extension guides,
comparison and parity plan link the feature contract. Generated API, documentation
links/configuration coverage, changed-source Ruff and whitespace checks accompany
acceptance. No new persisted tag, capability bit or global setting was added.

FP-7 still needs broader native read/results, recursive procedures and schema
authority. Typed LIST<PATH> is not part of this descriptor increment; generic
containers may carry paths. FP-6 parameterized stored collections/DECIMAL, pending
FP-4 policy decisions and full FP-8 frozen-profile/Pulse qualification remain
outstanding. Neither the old full inventory nor the installed Pulse was requalified
by these owner-specific tests. This report does not narrow the parity goal.
