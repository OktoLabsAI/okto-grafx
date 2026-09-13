# FP-6 native DECIMAL history, copy and logical-transfer qualification

Development `feature/v0.0.6`; isolated synthetic stores, not a release, installed
Pulse validation or full functional-parity acceptance. This follows the
[native storage](FP6_DECIMAL_NATIVE_QUALIFICATION.md) and
[numeric query/index](FP6_DECIMAL_QUERY_QUALIFICATION.md) increments.

## Implemented corrections and contracts

- Logical schema export now includes `decimal_precision` and `decimal_scale` only
  for DECIMAL columns. Detached schema validation and target installation activate
  the existing v2 catalog before adding typed decimal definitions. Other artifact
  columns retain their earlier shape; graph-model format selection remains 1/2/3.
- Copy schema shape and package digest include decimal p/s. An empty source table
  still requires a matching target declaration. Exact numeric equivalence is not
  schema compatibility; neither copy nor logical import is a rescaling assignment.
- Initial complete artifact validation compares native tuple admission with the
  offered value frames. Invalid p/s and checksummed frames carrying a different
  declaration refuse before any destination or resume workspace is opened. Copy
  also rejects a rechecksummed package whose stored frames disagree with its schema.
- Native history already used the parameterized catalog/tuple codecs. Its source
  implementation needed no new history format or weakened recovery rule. Tests now
  cover exact values and schemas across version/diff reads, nullable append,
  deletion/recreation, retained baseline, backup/restore and WAL replay.

Source: `src/okto_grafx/transfer.py` (`_schema`, `_tables`, `_rows`, `_install_schema`)
and `src/okto_grafx/catalog_copy.py` (`_shape`, `_digest`, `_values`, target checks).
The existing native history schema decoder validates round-trip schema/value
encoding in `engine/system_history_store.py`.

No new connection option, native capability, graph-model artifact version or
public method. `decimal_values_v1`, source provenance, copy preparation/receipts,
operation-local bounds, current-only logical-transfer/history policy, independent
readers/writers, both OCC validations and WAL durability remain unchanged. No
source/target production data is touched. DECIMAL-aware readers are required;
installed old/new reader checkpoint C remains a separate outstanding obligation.

## Executable evidence

`tests/api/test_decimal_transfer.py` covers pure/NumPy, typed/ANY/flexible models,
overlapping node/logical-relationship namespaces, 38-digit coefficients and scale,
nested temporal/decimal values, custom equality indexes, exact declarations,
endpoint remapping, cold reopen/verify, copy snapshots and receipt retry. Scan and
indexed history readers cover update/delete/recreate, old lineage and both original
and restored databases. Adversarial tests rechecksum malformed frames, change
precision/scale in manifests and replace copy package declarations/rows. No invalid
import may reach the patched destination-opening function; existing-target refusals
must leave the committed LSN and application data unchanged.

`tests/api/test_decimal_consumer_recovery.py` and its real-process worker cover
durable private import batches and lost promotion ACKs, resumed one-row batches
without duplicate nodes/edges, and history/current-row agreement after process
termination before COMMIT or after durable COMMIT before page application. Reopen
and verification run twice around a checkpoint. Nullable decimal schema addition
retains earlier historical declarations and exports complete current values.

## Local receipts

JUnit XML under `.grafx-tmp/`; selections overlap and must not be added. All listed
runs have zero errors/skips. Initial failures below are preserved, not waived.

| Receipt stem | Tests | Failures | Seconds | SHA-256 |
| --- | ---: | ---: | ---: | --- |
| fp6-decimal-transfer-first | 51 | 15 | 78.874 | 0e306ec325fe1446845a65f217058f7ac0b9ff7b95d9be01e11f1823048e37bd |
| fp6-decimal-transfer-second | 51 | 8 | 84.316 | 9c5ea896f56b931ebcb378c4eb8d896ddc75e8cd720b0d132e8d57cd5f67303e |
| fp6-decimal-copy-corrective | 13 | 0 | 17.934 | f09204d2c8f8d4e7973a4c783249d3a88c4cc41ed9ae4c42395d86209fdaf230 |
| fp6-decimal-consumer-recovery-first | 18 | 0 | 91.660 | 7b39f9b78e4a3714c715b61e64ed0554e6f58315ce878bda1df63cf36d026534 |
| fp6-decimal-consumer-contracts | 2427 | 0 | 56.451 | aa19bb00add3f1a7e7c775fda785e482052029e8f291d23cc2d507349209265f |
| fp6-decimal-consumer-regression | 529 | 0 | 732.436 | d49ec7aa2b949e2a822dfda72efe1e89636d71152dd1bcb8353b7214bd391850 |

The first fixture used a nonexistent `table_kind` keyword for `create_index` and
enabled commit history after the data/schema commit captured by copy. Both were
corrected to the existing public contracts. The second fixture counted the copy
preparation owner row as an application node; it now explicitly selects application
IDs and proves old-reader invisibility and fresh-reader visibility together. All
13 copy tests then passed. No product contract, permission or refusal was weakened.

Final grouped affected regression: **529 passed**, zero failures/errors/skips,
terminal exit **0**. It includes both complete new consumer modules (all 69 tests),
previous decimal storage/index/codec/math contracts, existing typed/flexible/grouped
and namespace copy/transfer tests, temporal-property transfer, resume process cuts,
native history operations/index/compaction/adversarial tests and storage/history
codecs. All initial failed IDs above are covered by passing tests in this selection.
No production source or test code changed after this collection began.

The separate **2,427-pass** contract selection covers the public API surface,
FP annotation contracts, import boundaries, executable consumer documentation and
history documentation examples. Both receipts have terminal exit 0. Generated API
reference, documentation links/anchors/39 configuration fields/11 preserved source
plans, Ruff across changed/new Python and whitespace checks also pass. All test
processes are terminal. The pre-existing pytest-asyncio fixture-loop-scope warning
is not a failure or skip. This is one grouped affected regression, not a claim of
running the full repository, frozen TCK, installed readers or Pulse suites.

## Remaining objective

DECIMAL CLI JSON, text/SQLite import, Arrow/Pandas/Polars/Parquet, registered
procedure signatures/owned capture and other declared
interop consumers still need their exact support/refusal contracts and tests.
Typed LIST/MAP/ARRAY/STRUCT descriptors remain outstanding, as do checkpoint C,
installed old/new readers, frozen/supplemental profile, fixed competitors and paired
installed Pulse API/UI. Unsupported ordered/full-text decimal indexes and other
numeric families are not implied by these transports. See the
[support matrix](../specs/DECIMAL_VALUES_V1.md) and
[complete functional parity plan](../specs/FUNCTIONAL_PARITY_PLAN.md).
