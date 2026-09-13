# FP-6 native DECIMAL storage checkpoint

September 12, 2026 — `feature/v0.0.6`, source development only. No commit, push,
installation, production-data change or release is performed by this checkpoint.
This is **not FP-6 completion**, full Cypher conformance or installed Pulse acceptance.

## Implemented and exercised

- Native 19-byte frame, tag 18, precision/scale and exact signed coefficient;
  malformed/truncated frames and projected-away corrupt values refuse.
- DECIMAL(p,s) DDL, schema serialization, public catalog views and nullable append;
  exact normalization before staged row intents, quota capture and write results.
- Native parameters/scalar/entity results; nested ANY values; node/relationship
  properties and explicitly tagged detached observation JSON.
- Atomic decimal_values_v1 bit-26 admission with row/schema WAL; mixed temporal
  admission preserves private DDL and original OCC. No independent commit/retry.
- Snapshot visibility across two participants, conflicting schema commit refusal,
  rollback, durable-apply failure and cold reopen, verification, pure/NumPy.
- Four subprocess cuts (both codecs, before/after COMMIT) terminate via os._exit,
  without cleanup. Pre-COMMIT values/capability stay absent; committed values recover.
- A reader with bit-26 support removed refuses both replay preflight/apply **before
  any catalog/data page application**. This is an emulated capability-mask test,
  **not** an installed historical-reader qualification.

Contract: [DECIMAL_VALUES_V1](../specs/DECIMAL_VALUES_V1.md). Tests:
`tests/storage_core/test_decimal_codec.py`,
`tests/api/test_decimal_native_storage.py`, `tests/api/decimal_storage_worker.py`.
Numeric helper tests retain independent Fraction/Decimal oracles; the frame suite
adds 1,000 deterministic full-width roundtrips/mutations, not extra pytest counts.

## Receipts

Receipts are local `.grafx-tmp/` artifacts. Counts overlap and must not be summed.
All rows have zero errors/skips. Terminal exit is 0 for zero failures, 1 otherwise.

| Receipt stem (XML) | Tests | Failures | Seconds | SHA-256 |
| --- | ---: | ---: | ---: | --- |
| fp6-native-resume-baseline | 139 | 0 | 4.079 | e86df2ebf46eee0a094be96b152da524fd15befcbcadec0b0644f9c99ba02720 |
| fp6-native-codec-first | 197 | 1 | 10.171 | 4fe5cc2ede8c2f37c9ab79bffce63e47ae7433d533379d798ea7951d2b8818b4 |
| fp6-native-storage-first | 43 | 6 | 14.728 | cdb035566010856ca42850bf327bb3c8039e8b49e46b5199d07e81e656dc75ef |
| fp6-native-storage-second | 43 | 2 | 6.434 | 462fce13955c5d4f9d011027910c166fa65b4a3ca59578f07bc94aa12d00bf42 |
| fp6-native-storage-regression | 338 | 0 | 18.071 | 28cfc9f8ba1d94985df9effd7f163590ce60ac83ebb95bfd8d628e9652602183 |
| fp6-native-durable-qualified | 441 | 0 | 24.376 | b8c6263a073580d27267085a41a417bcfcb79f858449cfd09b5bd46a372d597a |
| fp6-native-contracts | 2416 | 1 | 48.861 | 88ec2196f1b9d03c4026300545ea911f587144eee46c8b4abad55c0d51a52f48 |
| fp6-native-adjacent-regression | 456 | 0 | 38.801 | 9578d3f80c0f0328995c3d64263799dad03a6b96710087a63e22961ae5f8975c |
| fp6-native-final-contracts | 2470 | 0 | 62.705 | b8be41715f8c27f6f525b5c8db8ae1aff8e186d46866b36bfc8cae9e0196946c |

The first codec failure was an incorrect test expectation: capability discovery
and generic spill-capable encoding do not prohibit expression NaN; **stored tuple
admission does**. Tests now distinguish these doors and still assert nested
nonfinite storage refusal. No production nonfinite prohibition was weakened.
The first storage run exposed missing detached-entity decimal capture and lost
nullable-column precision/scale; both production consumers were fixed. Two remaining
fixture failures used `db.catalog.table` instead of the existing
`db.catalog.catalog.table` public hierarchy. No public hierarchy was changed.
The contract run's single failure was stale generated API documentation while the
new DTO/column fields were being integrated; final validation is recorded below.

The 338 selection includes the initial native suite plus temporal row admission,
temporal native storage, parser/planner and AST structure. The 441 selection adds
the complete current native tests, decimal helper tests, value/ANY/temporal codecs,
catalog replay/fault cases, temporal commit-vs-close and nullable schema models.
The 456 adjacent selection covers pending row intents, schema and omitted
projections, public query/collaborator boundaries, procedure schema composition
and expression-versus-stored NaN policies.

Final terminal exit **0**: **2,470 passes**, zero failures/errors/skips. This
repeats public-surface, FP annotation and pure-core import contracts, executable
documentation (including the new decimal usage example) and all **53 new native
decimal cases** (25 API, 28 codec/fence). The stale reference is regenerated and
the exact failing documentation check passes; no failure is waived or excluded.
The 441/456 runs above remain separate, overlapping selections, not a whole-repo
regression. No production source changed after this final collection began.

API generation, documentation links/anchors and 39 configuration fields/11
preserved plans, Ruff across changed/new Python files and whitespace checks pass.
The pytest-asyncio unset fixture-loop-scope deprecation warning remains existing
test-environment output, not a test skip or a hidden failure. All process-cut
workers and pytest handles for this checkpoint have terminal results.

## Still required (not waived)

Subsequent [native numeric query/index qualification](FP6_DECIMAL_QUERY_QUALIFICATION.md)
supersedes the query/arithmetic/equality-index gaps in this earlier checkpoint.
The remaining full consumer/typed-collection/installed-reader obligations below
are not closed by that follow-up.

Native arithmetic/casts/aggregates; exact cross-numeric comparison, ordering,
grouping and index seek/key contracts; full PK/secondary-index support matrix;
typed LIST/MAP/ARRAY/STRUCT; complete history/copy/transfer and other consumer
support/refusal before partial effects; installed old/new readers; checkpoint C;
frozen/supplemental profile, fixed competitors and paired installed Pulse API/UI.
The Core stays backend-agnostic; no Pulse source/runtime change accompanies this
checkpoint. No frozen profile case or exclusion changed.
