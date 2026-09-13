# FP-7 procedure query authority qualification

Date: September 12, 2026. Branch: `feature/v0.0.6`, dirty development worktree.
[Feature/API/configuration contract](../specs/PROCEDURE_QUERY_AUTHORITY_V1.md).
This qualification does not claim commit, push, release, installation, production
data changes or final Pulse/full-profile parity.

## Implemented and tested

An explicitly permissioned `graph_read=True` callback receives ProcedureReader;
default read callbacks remain unchanged. ProcedureWriter additionally inherits
query() for native read and returning-write queries. ProcedureResult exposes only
owned columns and rows, including invocation-witnessed entities, not a raw plan or
transaction. Child queries preserve the caller's snapshot, private writes,
temporal statement/transaction clocks, read cancellation and rollback boundary.

The new descriptor budgets count child queries, rows and bytes across invocations
of that procedure in the outer statement. Write-bearing queries also consume the
existing shared write-statement budget. Collection checks row admission before
retaining limit + 1; result cells use the existing native value/entity budget.
Permissions, expiry, foreign-thread and reentrancy checks use the same revocable
authority foundation. There is no extra transaction, writer lock or independent
commit. Recursive registered calls, DDL and implicit schema remain explicit gaps.

The **33 feature cases** cover:

- Read aggregates, parameters, materialized results and cursor consumption/close.
- Returning writes, native newly-created node/path witnesses, downstream SET,
  read-your-own-writes and pure/NumPy cold reopen/verification.
- Refused read-authority writes, including zero-row and UNION writes, unavailable
  permissions, forbidden DDL/nested calls and invalid descriptors.
- Row/byte/cell/query/write budgets, repeated input rows, prior-statement
  preservation and rollback after a late output error hidden by LIMIT.
- Revocation, foreign threads, unit callback poisoning and failed operations
  caught during early iterator cleanup; no such catch can publish outer effects.
- Independent writer/reader progress while a callback waits, unchanged original
  snapshot across its child queries, detached map ownership and stable outer
  statement clock. Outer cancellation reaches execute/cursor child queries.

## Receipts and corrections

All completed passing pytest selections below have zero failures/errors/skips.
Selections overlap; do not add them as distinct coverage. Failed attempts remain
recorded. Wall times include different selections, not performance benchmarks.

| Receipt under `.grafx-tmp/` | Result | Seconds / exit |
| --- | --- | --- |
| `fp7-query-baseline.xml` | 74 existing writer/entity tests passed | 19.609 / 0 |
| `fp7-query-first.xml` | 18 passed, 5 integration failures | 13.443 / 1 |
| `fp7-query-focused.xml` | 23 feature tests passed after correction | 1.880 / 0 |
| `fp7-query-regression.xml` | 460 combined procedure/query tests passed | 87.586 / 0 |
| `fp7-query-control.xml` | 76 query/cancellation tests passed | 11.060 / 0 |
| `fp7-query-contracts.xml` | 1,462 query/public-annotation/adapter/import/language tests passed | 37.012 / 0 |
| `fp7-query-temporal.xml` | 86 query/temporal tests passed | 3.817 / 0 |
| `fp7-query-final-regression.xml` | 562 combined procedure/subquery/UNION/deletion/cache/spill/temporal/cancellation tests passed | 101.320 / 0 |
| `fp7-query-cleanup.xml` | 125 final query/writer/cancellation tests passed | 30.065 / 0 |

The initial five failures had one cause: the new reader path called the atomic
write publication door even for a read-only outer statement. Native protection
correctly refused it. The integration now publishes a private write phase only
when the parent actually has atomic staging; no protection was relaxed.

Review added the outer statement-clock inheritance test and the early-close
poisoning check. The broad 562-test run began before the last cleanup hardening;
the 125-test final run covers that final source, including all 33 new cases and
existing writer/cancellation contracts. These are not presented as a new full
repository regression or a combined frozen-profile result.

SHA-256 receipts:

```text
fp7-query-baseline.xml 2840c7dd649b15650bff2de325cb38ebac67097634709ce804f22c06fe07b9f9
fp7-query-first.xml ba6b0d2f6fa76731d25396935a445ad23a23064d803d8c5a474599c42394c3d6
fp7-query-focused.xml 7e53c8d100568a2a943c08d0f78b6eb45411078895d1e415599a19d5d22e115c
fp7-query-regression.xml 97aa86c005b9356dcffe8e3f2243aa54dc1ec19f9c15d1cd2f21e238d53f7f10
fp7-query-control.xml c43c86398b13d383a9ee36e78c996cf5ecac8885495f0b467cbf7271f7ffa189
fp7-query-contracts.xml 64372946dde5175efbd1918934710d5a218b9d742b1aebea5d1d1a800f6174d5
fp7-query-temporal.xml 0bdb27a23b0f326705891de982bc431df5ee6cad60fdf3b4aedb39e8d8cd5aa8
fp7-query-final-regression.xml 4dd02da2a11e0e4ea9c2486d39df94f3597faf66247d869885839e32d2d7978c
fp7-query-cleanup.xml ae6789ef5da9655311701acf1f19200479409c6f715c9b477d164f8253b82d42
```

The original FP-7 TCK selection remains **52/52 passed**, exit 0, in
`fp7-query-native-20260912.json`. The other 3,845 cases are unselected, not newly
waived/certified. Pinned revision `677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`, ledger
and exclusions are unchanged. This owner selection does not replace feature tests.

## Reproduction and documentation

Use `PYTHONPATH=src;.grafx-tmp/v006-optional-test-deps`:

```text
python -m pytest tests/api/test_procedure_queries.py tests/api/test_writing_procedures.py tests/query/test_read_control.py -q --tb=short
python -m pytest tests/api/test_procedure_queries.py tests/api/test_procedure_entities.py tests/api/test_writing_procedures.py tests/api/test_procedure_native_values.py tests/api/test_procedure_invocation.py tests/api/test_procedure_numeric_signatures.py tests/api/test_unit_procedures.py tests/api/test_tabular_procedures.py tests/query/test_write_subqueries.py tests/query/test_updating_union.py tests/query/test_deleted_entity_access.py tests/query/test_prepared_plan_cache.py tests/query/test_query_memory_spill.py tests/query/test_temporal_functions.py tests/query/test_read_control.py -q --tb=short
python -m tools.check_opencypher --checkout .grafx-tmp/opencypher-2024.3 --output .grafx-tmp/fp7-query-native-20260912.json --verify-ledger docs/conformance/FP_CASE_LEDGER_V2.json --predecessor-ledger docs/conformance/FP_CASE_LEDGER_V1.json --execute-stateful --owner FP-7
```

The API generator now includes ProcedureReader methods and ProcedureResult fields.
README, roadmap, configuration, API, extension/composition/query guides, comparison,
compatibility and the parity plan document this increment and its remaining scope.
Documentation link/anchor/global-config coverage, generated-reference checks,
changed/new-source Ruff and whitespace checks accompany acceptance.

FP-7 still needs recursive registered calls, schema authority and additional
determinism/effect declarations. FP-6 persisted parameterized collections/DECIMAL,
pending FP-4 policy decisions and the full FP-8 frozen-profile/competitor/Pulse
qualification remain outstanding. The full functional-parity goal stays intact.
