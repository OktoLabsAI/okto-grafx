# FP-7 schema authority qualification — 2026-09-12

Scope: local `feature/v0.0.6` source, native procedure schema and catalog-v2
custom-index/DML composition. No installed Pulse, production database, publish,
commit or push is claimed. [Consumer contract](../specs/PROCEDURE_SCHEMA_AUTHORITY_V1.md).

## Implementation and safety argument

- `TabularProcedure` declares schema_write/max_schema_statements; literal schema
  permission and write mode are validated before invocation. Ancestors cannot
  grant descendants authority they do not hold.
- `ProcedureWriter.schema()` uses the existing revocable same-thread capability
  and poison protocol. Explicit and implicit schema operations share quotas.
- QueryEngine adopts private schema through ancestor contexts, preserving selected
  old index generations. Native artifact/staging marks undo only the failing
  statement, including failures after callback output or iterator cleanup.
- Catalog-v2 custom CREATE INDEX reuses the already established endpoint-identity
  generation protocol: fence all durable-source table partitions, build under
  schema publication serialization, retain a private nonce, record transactional
  artifact observations, and let normal row/index WAL add all own pending deltas.
  Dedicated maintenance activation seals are unchanged.

## Executed receipts

All XML paths below are under `.grafx-tmp/`. Counts overlap; they are not one
summed regression. Terminal exit codes are retained, including failed attempts.

| Receipt | Tests | Passed | Failed | Seconds | Exit |
| --- | ---: | ---: | ---: | ---: | ---: |
| fp7-schema-first.xml | 16 | 16 | 0 | 2.020 | 0 |
| fp7-schema-expanded.xml | 31 | 27 | 4 | 23.176 | 1 |
| fp7-schema-implicit-first.xml | 7 | 5 | 2 | 4.982 | 1 |
| fp7-schema-composable-first.xml | 38 | 38 | 0 | 15.006 | 0 |
| fp7-schema-index-regression.xml | 85 | 83 | 2 | 36.769 | 1 |
| fp7-schema-index-final.xml | 91 | 91 | 0 | 45.970 | 0 |
| fp7-schema-authority-final.xml | 62 | 61 | 1 | 33.972 | 1 |
| fp7-schema-consumer-contracts.xml | 348 | 346 | 2 | 42.864 | 1 |
| fp7-schema-consumer-final.xml | 348 | 348 | 0 | 41.956 | 0 |
| fp7-schema-index-access.xml | 4 | 4 | 0 | 3.591 | 0 |
| fp7-schema-expanded-regression.xml | 7122 | 7120 | 2 | 1929.259 | 1 |
| fp7-schema-final-corrections.xml | 107 | 107 | 0 | 66.810 | 0 |

No skipped/error cases occurred in these twelve runs. The final correction
selection resolves both expanded-run failures; that is not a claim that the
original expanded run exited successfully.

SHA-256, in table order:

```text
3d1e0cba08137a1a96764322f95e773772d1a57f918403da8b118ea29597aa82
1bcaebe3741ff69a28df113ac14e4b64510f746c613ae5bc5fccf8b93c25eb48
4082aea68fcc96398e384433c2bc3d341d64183a222df23c5e600d1334a0ce7f
fb92c6071704376767b8590d2bd52d808390331ea4fe1e7ce44352cbb6a28c2b
0b3c114f80ac3340a4853dd10e5226f04eb7e4c506c0a0eeeaf0d5d036371897
a148c07bec16bef13787c687cf7278ef227ae5412554064d18e9ab7f07926f68
ae7309124584a3c1ccd0a82c9650a0933d538b966e0737961f9695ce6d1c4b86
8add8637e97240d2f7cfc4a71597a144f9d9694268c455f30dbe20ed7efe78c1
19879f87cec1cce1116ff557ccf5851c275582f512486f088e0624e7be03dc86
4eeb62da75af72535b29d198081def845bc1bdc4b1e9b7ab41e6b9c7a4170689
02743e9e58c6106fa874784330f23222f755ef562d2f36e331189a88f9a83529
9cb3844452b094d482ed3b7f35c0859fcc0bc8a64cff33e8cde0eb74a1835f5a
```

### What failed and changed

The initial custom-index cases hit a real native constraint: the maintenance
activation port only builds durable rows in a fresh dedicated transaction.
Removing its freshness check would omit own pending DML. Instead, textual v2 DDL
now uses the existing transaction-journaled generation path described above;
the four originally failing cases passed in the 38-case run and later selections.

Other intermediate failures were test assumptions, not native correctness errors:
two callback-list assertions expected public tuple freezing rather than the
callback-owned mutable list; two catalog assertions used a method absent from
the detached public CatalogView; one unsupported DROP assertion expected a plan
error rather than the preserved native parse error. Those assertions were corrected
without changing value ownership, catalog authority or native error classification.
The last error-taxonomy correction is included in the final expanded regression.
It also passes in the terminal 348-case consumer/configuration/extension selection,
which includes all 62 new schema cases and executes the published schema example.
The subsequent four-case access-path selection additionally proves actual
IndexSeek planning before/after cold reopen, over new/populated tables with both
codecs. Correct answers are not being accepted solely through scan fallback.

The first consumer run exposed two older test assumptions: vector examples were
selected by their ordinal position before a temporal example was inserted, and
rollback expected an error for MATCH on an absent label. Recipes are now selected
by unique content; rollback proves actual catalog absence plus the native empty
MATCH result. No vector feature or rollback check was removed. The corrected
348-case selection has zero failures, errors or skips.

### Coverage

`tests/api/test_procedure_schema.py` contains 62 cases: pure/NumPy durable reopen;
native tables/relationships/vectors and custom indexes; explicit/implicit quotas;
returned new entities and paths; nested non-escalation; callback/output/cleanup
failures; same-name retry; prior caller staging preservation; all textual index
layouts over new and populated tables; writes before/after index DDL; foreign
writes on both sides of the build; and read-only/expired/poisoned authority.

The isolated subprocess oracle `schema_procedure_worker.py` exits at callback,
statement and post-commit boundaries. Both codecs verify absence of uncommitted
schema/data, then index coverage of durable updates/inserts after COMMIT. Existing
schema/data apply fault injection confirms durable outcome metadata and cold
recovery. `verify("all")` checks both derived structures and primary data in the
new index scenarios.

The 91-case selection additionally runs existing custom-index APIs, dedicated
manager preparation, artifact provenance and endpoint-index query regressions.
Original procedure TCK selection remains **52 passed / 0 selected failed /
0 selected not-run**, terminal exit 0, with 3,845 other-owner cases not selected.
No frozen ledger or fixture waiver was changed. The older whole-profile inventory
is not superseded by this owner-specific result.
Native receipt `.grafx-tmp/fp7-schema-native.json` SHA-256:
`74dafef0cbd3c7ce9bb64ca5db19fd41830908e24da2237cac194f335433446e`.

### Reproduction

```powershell
$env:PYTHONPATH='src;.grafx-tmp/v006-optional-test-deps'
python -m pytest tests/api/test_procedure_schema.py -q --tb=short
python -m pytest tests/query tests/txn tests/index tests/api/test_procedure_schema.py tests/api/test_procedure_recursion.py tests/api/test_procedure_queries.py tests/api/test_procedure_entities.py tests/api/test_procedure_native_values.py tests/api/test_writing_procedures.py tests/api/test_custom_exact_indexes.py tests/api/test_ddl_artifact_provenance.py tests/api/test_vector_ddl_attach_atomicity.py -q --tb=short --junitxml=.grafx-tmp/fp7-schema-expanded-regression.xml
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/fp7-schema-native.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --owner FP-7
python -m pytest tests/query/test_implicit_index_authority.py tests/index/test_table_local_catalog_authority.py tests/index/test_catalog_active_projection.py tests/api/test_vector_owner_names.py tests/api/test_procedure_schema.py tests/query/test_schema_transactionality.py tests/query/test_schema_refusal_atomicity.py -q --tb=short --junitxml=.grafx-tmp/fp7-schema-final-corrections.xml
python tools/generate_api_reference.py
python tools/check_documentation.py
```

## Final regression and boundaries

The expanded run completed with 7,120 passes and two fixture failures:

1. `tests.query.test_implicit_index_authority::test_adoption_keeps_captured_stores_and_does_not_reopen_omitted_existing_authority`
   constructed a minimal context without the newly declared procedure_parent.
   The double now declares procedure_parent=None; captured-store identity and
   exclusion of previously omitted authority remain asserted.
2. `tests.index.test_table_local_catalog_authority::test_inexpressible_vector_name_does_not_hide_valid_v1_scalar_path`
   used current DDL to synthesize an old catalog whose overlong vector name now
   requires the bounded-name v2 capability. The fixture now first proves current
   DDL refusal, then installs the historical definition using the legacy decoder's
   installation primitive and round-trips actual v1 bytes. Every prior scalar
   projection/staging assertion is retained; the test was not converted to v2.

The final 107-case selection passes with zero failures/errors/skips and contains
both exact original testcase identities, all 62 schema cases, current vector-name
capability tests, table-local/implicit authority and schema rollback families.
No production Python source changed after the expanded run began at
`2026-09-12T13:41:45.741469-03:00`; only fixtures/documentation and the separately
qualified index-plan assertions changed. Thus there are **zero unresolved failures
in this selected regression**, using the expanded receipt plus final reruns. A
second full 7,122-case clean-exit execution was not performed or claimed.

Generated API, documentation links/anchors, all 39 DatabaseConfig fields and
11 preserved source plans pass the documentation check. Ruff on changed/untracked
Python and whitespace checks pass. Read-only source search in the paired Pulse
Community/Core trees found no direct TabularProcedure/ProcedureReader/ProcedureWriter/
ExtensionRegistry usage (rg exit 1, no matches); this is not installed runtime evidence.

The descriptor fields/method add no new DatabaseConfig option, durable tag or
external dependency. Consumer/API/configuration/language/index/roadmap documentation
is updated. The compiled-table-set behavior and catalog-v1 activation prerequisite
are explicit, not a claim of arbitrary dynamic schema or ALTER/DROP support.
FP-6 persisted parameterized types, pending FP-4 decisions, full frozen-profile
and competitor qualification, and paired installed Pulse API/UI testing remain
open. No production server or data was changed.
