# 0.0.6 compatibility and upgrade matrix

The newest native label extension requires catalog-v2 bit **28** (`node_labels_v1`),
GXL1 per-version heap metadata, GXHM03 retained-history metadata and logical artifact
format **4**. The [isolated installed-wheel checkpoint](reports/FP_NODE_LABEL_WHEEL_QUALIFICATION.md)
passes 36 native scenarios and 36 format-4 transfer worker checks, with exact
old-reader refusal, all-file non-mutation and candidate wheel/source identity.
The earlier type/namespace receipts do not cover this later bit or row format.
Do not activate it in a production store before the complete package/consumer
checkpoint. [Native contract and current evidence](specs/NODE_LABELS_V1.md).

Typed collection columns add catalog-v2 capability bit **27**
(`typed_collections_v1`) and schema-column marker **252**, followed by a bounded
GXT1 descriptor. Existing row LIST/MAP tags are unchanged. Nested ANY, DECIMAL and
temporals require their own bits too. Explicit catalog-v2 activation precedes
typed DDL; publication is atomic with schema/data COMMIT. Old readers must refuse
before interpreting unsupported tables. The
[combined type checkpoint C](reports/FP6_TYPE_WHEEL_QUALIFICATION.md) qualifies its
recorded candidate; the later label candidate has the separate matrix above.
[Format, admission and consumer contract](specs/TYPED_COLLECTIONS_V1.md).

**Native decimal boundary:** later development adds catalog-v2 bit **26**
(`decimal_values_v1`), value tag **18** and two DECIMAL column parameters. First
native decimal values in ANY activate the fence in their user COMMIT; typed
DECIMAL schema commits activate it with the metadata. Unsupported readers must
refuse before replay effects. Source pure/NumPy, fault and capability-mask tests
pass; the [type support matrix](TYPE_SUPPORT.md) and
[combined installed-wheel qualification](reports/FP6_TYPE_WHEEL_QUALIFICATION.md)
record subsequent consumer and old/new-reader evidence. Pre-decimal wheel receipts
do not qualify this new format. [Contract](specs/DECIMAL_VALUES_V1.md),
[native evidence](reports/FP6_DECIMAL_NATIVE_QUALIFICATION.md).

**Logical artifact admission:** a separate installed-wheel matrix passes 42 worker
checks (10 seeds, 16 imports, 16 exact refusals) across formats 1/2/3. Current
imports use one-row batches; archived format-1 importers are qualified only at
their default 256-row size, with their reproduced tiny-batch failure disclosed.
Unsupported ordinary/resumable imports leave all audited files/directories
unchanged. See [wheel hashes, commands and limits](reports/LOGICAL_TRANSFER_WHEEL_QUALIFICATION.md).

**Missing legacy vector artifact repair:** a separate 12-case qualification uses
actual archived 0.0.5/early-0.0.6 writers and explicit current source. Read-only
opening preserves graph data and does not invent the absent relationship index;
writable opening attaches it stale, and explicit selected-owner rebuild restores
it. Ordinary repair and cuts before COMMIT/before page application pass with
verification and repeated cold reopen. This is not installed-current-wheel or
Pulse evidence. See [scope, commands and receipts](reports/VECTOR_OWNER_REPAIR_QUALIFICATION.md).

**Later vector-owner naming boundary:** catalog-v2 bit **25**
(`vector_owner_names_v1`) stores each table's immutable naming policy. Existing
unambiguous vector indexes retain their names; new collisions/oversized names
use physical table/column identities. The installed-wheel matrix passed 24/24
old-reader/writer scenarios with unchanged files on refusal and verified current
recovery. See [exact format, wheel hashes and limits](specs/VECTOR_OWNER_NAMES_V1.md).
This is additional focused evidence, not replacement of the broader gates below.

Source/development validation, **not a published release or Pulse installation**.

The later [independent graph namespaces](specs/GRAPH_NAMESPACES_V1.md) increment
uses catalog-v2 required bit 24 only when node and relationship names overlap.
Its logical export format is `okto-grafx-logical-3` and every custom-index schema
entry carries `table_kind`; older importers must refuse that format, not flatten
the namespaces. Import reports now populate `RecordIdMapping.kind`, including
unique-name graphs: mapping consumers must use `(kind, table, source_record_id)`.
Destination IDs are table-local allocations, not a dense graph-global sequence.
Qualified copy/history selectors and full-text `kind` are documented in their
respective API guides. The older wheel matrix below does **not** qualify bit 24;
its separate 24-case installed catalog/WAL matrix is described below and in the
[namespace evidence](specs/GRAPH_NAMESPACES_V1.md#installed-catalogwal-admission).

The reproducible command installs both supplied wheels into separate temporary
targets, invokes isolated interpreters and checks imported package origins:

```sh
python tools/check_v006_upgrade.py --legacy-wheel legacy/okto_grafx-0.0.5-py3-none-any.whl --current-wheel current/okto_grafx-0.0.6-py3-none-any.whl
```

Install NumPy, google-crc32c and tzdata in the invoking Python environment first. Isolated
workers ignore user-site/PYTHONPATH; a venv avoids accidentally relying on user
packages. The script never installs globally or touches production stores.

Fourteen cells cover pure and native-CRC/NumPy writers for each boundary:

| Store case | New wheel reads/appends to 0.0.5 | Old wheel after opt-in | Opposite selector readback |
| --- | --- | --- | --- |
| Default, no new capability | Required | Opens | Required |
| `posting_hash`, bit 14 | Required | Refuses | Required |
| Nullable column layouts, bit 13 | Required | Refuses | Required |
| System-time history, bit 15 | Required | Refuses | Required |
| Positional FTS, bit 16 | Required | Refuses | Required |
| Temporal access tree, bit 17 | Required | Refuses | Required |
| Quiescent temporal compaction, bit 18 | Required | Refuses | Required |

Each cell verifies native rows, full verification and checkpoint/reopen. Temporal
and positional cells also exercise their public APIs; old-reader refusal must
leave payload hashes unchanged. Wheel SHA-256, platform, Python and results are
emitted as JSON. Both package versions are asserted, not inferred from filenames.

The GitHub workflow `v006-compatibility.yml` defines Windows/Ubuntu and Python
3.11/3.12/3.13 coverage, with the focused feature/crash tests. A configured workflow
is not evidence of a remote run. Actual local regression, artifact hashes and
matrix outcomes are recorded in the [NHC acceptance report](reports/V006_NHC_ROUND.md).
The [preceding round report](reports/V006_NATIVE_HISTORY_ROUND.md) preserves its
earlier ten-cell checkpoint rather than claiming the later boundaries were tested then.
No format downgrade is provided; install compatible binaries on all participants
before opting into new capabilities. Native exact-source tests remain separate
from wheel consumption checks.

## Subquery scope expansion in the development line

`CALL (x)` imports now persist throughout that body, including after a WITH that
does not list `x`. They cannot be redeclared; this deliberately replaces the older
descope behavior without a legacy switch. `CALL (*)` applies the same rule to all
currently visible names. For ordinary scope/drop/rebind behavior use
`CALL { WITH x ... }`: each UNION branch imports separately. `CALL ()` imports
nothing. Call outputs still cannot overwrite outer names.
[Examples and exact restrictions](COMPOSABLE_QUERIES.md#subquery-import-scopes).

This changes query semantics/plan metadata only; no setting, persisted capability
bit, schema layout or Python method is added. `RestoreImports` is a native public
plan node; consumers that inspect exhaustive operator sets should recognize it.
Read cursors, native entity authority, statement rollback and participant OCC
remain unchanged. These source tests are not evidence of installing this build in
Pulse or of testing every Pulse query.

## Updating UNION expansion in the development line

Previously rejected writing UNION branches are now admitted under the existing
statement transaction. Returning branches must agree on ordered output names;
all-unit writing branches export no rows. Mixing those forms still refuses.
Branches observe preceding effects, with lazy schema publication and native
entity identity stabilized before returning. Read APIs still reject all writes.
[Detailed API, windows, rollback and budget policy](COMPOSABLE_QUERIES.md#updating-union-branches).

No legacy switch, persistence bit, setting or dependency is added. The native
`UnionRows` plan gains a `writes` boolean (default false for existing read plans).
The parser's `UnionQuery.writes` now accounts for nested effects. Consumers of
exhaustive plan/AST metadata should recognize these fields. Neither this semantic
expansion nor its local regression is a claim of installing/validating Pulse.

## Later functional-parity temporal boundary

Native DATE/LOCALTIME/TIME/LOCALDATETIME/DATETIME/DURATION use value tags 12–17
and catalog capability `temporal_values_v1`, bit 23. Typed DDL or the first
temporal ANY/nested write publishes the capability transactionally; there is no
format downgrade. Readers without the bit must refuse before applying page effects.
[Native temporal API and remaining integration](TEMPORAL_VALUES.md).

The fourteen-cell wheel runner above predates this boundary and **does not qualify
bit 23**. A separate installed-wheel runner now qualifies this temporal boundary:

```sh
python tools/qualify_temporal_wheels.py --current-python path/to/current-venv/python --old-python path/to/old005-venv/python --old-python path/to/old006-venv/python --profile pure --output path/to/fresh-temporal-matrix
```

On Windows the venv interpreter is `Scripts/python.exe`; on Unix it is `bin/python`.
Install the supplied wheels and their declared dependencies into separate venvs
before running. Repeat with `--profile accelerated` and a **different fresh output**
for NumPy/native CRC writes with pure readback. The command never installs globally,
accepts an existing output directory, deletes stores or touches Pulse. Workers use
`-I`, assert that the imported package belongs to the selected venv and preserve
the fixtures plus a JSON report with imported origins, versions and all-file hashes.

For each old wheel and read/write mode, it first creates an ordinary old-format
store, executes an ordinary idempotent update with the current wheel without temporal activation, and
proves the old reader still opens the checkpointed result. It then exercises:

| Boundary | Required old-wheel result |
| --- | --- |
| Materialized temporal table/rows | `schema_version_mismatch`, exactly unsupported bit 23 |
| Durable temporal COMMIT, before page application; writable old open | Same bit-23 refusal before WAL replay changes files |
| Same pending WAL; read-only old open | `unsupported_operation`, `field=read_only_consistency` |
| Already-open old handle after temporal activation | Bit-23 refusal on its next read or attempted write |

Unrelated errors, timeouts or other unknown bits cannot satisfy the verifier.
It hashes **every file** before/after refusal, with no control/WAL exclusions; new,
removed or changed files fail. The current wheel then recovers/verifies all six
exact values and reopens again. Fixtures include expanded years, nanoseconds,
signed offsets, maximal duration months and an intentionally unavailable recorded
zone. Old handles are tested as **fail-closed**, not as usable mixed-version peers:
drain and upgrade all participants before activation. No downgrade is introduced.

The local Windows/Python 3.13 evidence and wheel hashes are recorded in the
[temporal receipts](specs/TEMPORAL_VALUES_V1.md#current-evidence). Archived wheels
are identified by hashes, not merely their version labels. This does not establish
every historical release, OS/Python combination, the later FP-6 layouts or installed
Pulse behavior; Checkpoint C must cover the final combined type package.

## Independent graph namespaces: installed-wheel admission

The same isolated verifier also supports `--capability graph_namespaces`:

```sh
python tools/qualify_temporal_wheels.py --capability graph_namespaces --current-python path/to/current-venv/python --old-python path/to/old005-venv/python --old-python path/to/old006-venv/python --profile pure --output path/to/fresh-namespace-matrix
```

Repeat with `--profile accelerated` in a new output directory. Its ordinary old
store remains readable until activation. Activation creates a typed node and
relationship both called `R`, with distinct properties and a self-loop. Expected
unsupported capability is **exactly bit 24**, not bit 23. Materialized, pending-WAL
and already-open handle cases retain the all-file non-mutation proof above;
read-only pending-WAL refusal retains the checkpoint-consistency rule. Current
readback verifies both properties and endpoint identity, distinct table IDs,
`verify("all")` and repeated reopen. This matrix does not certify old logical
artifact importers or every possible overlapping vector-space configuration.
See [namespace evidence and remaining scope](specs/GRAPH_NAMESPACES_V1.md).

## Combined native type upgrade procedure

The combined FP-5/FP-6 checkpoint now covers native temporal values, DECIMAL(p,s),
typed LIST/MAP/ARRAY/STRUCT and nested ANY. The [60-cell installed qualification](reports/FP6_TYPE_WHEEL_QUALIFICATION.md)
records exact wheels, capability masks, pure/accelerated writes, readback and
all-file refusal proofs. It supersedes the pending *type-package* checkpoint
above for that candidate, not earlier receipts or later untested format changes.

1. Drain writes and close **all** participants, including background workers and
   independently opened reader handles. Preserve the current package artifacts
   and their hashes. Never rely on a reused `0.0.6` version string alone.
2. Take and verify a supported pre-activation backup following
   [operations](OPERATIONS.md). Record the source schema/capabilities and the last
   committed state. Do not reset the graph or delete WAL files as an upgrade step.
3. Upgrade every participating process to the same qualified type-aware package
   and its selected dependencies. Use an isolated restored copy first, verify
   its existing data, and exercise the intended new schema/values there.
4. Activate catalog v2 with the documented `ensure_identity_indexes()` maintenance
   API before these typed declarations. New DDL or supported first stored values
   then publish required bits **transactionally**: temporal 23, DECIMAL 26,
   typed collections 27, and nested ANY 20 as applicable. Do not manually edit
   capability flags. An empty typed table can already activate required bits.
5. On the real store, use normal transaction/commit/recovery APIs and run
   `verify("all")`, checkpoint and reopen with the current package. Resume workers
   only with the upgraded participants. An old process must refuse safely; it is
   not a supported mixed-version peer even if it opened before activation.

There is **no in-place downgrade** after activation. If an operator needs the old
binary, restore the verified pre-activation backup to a separate controlled target
and assess any commits made since that backup before switching. Those later
commits are not magically represented by the old format. Preserve the upgraded
store for recovery/reconciliation; do not erase it or clear capability bits.

After a proven durable COMMIT interrupted before page application, reopen with a
current writable participant to perform native recovery. A read-only old binary
may refuse on checkpoint consistency first; an old writable binary must refuse
unknown capabilities before replay changes files. Neither result authorizes a
fallback that skips WAL or converts native values to strings/floats.

The selected type layouts/coordinates are in the [support matrix](TYPE_SUPPORT.md).
This procedure adds no connection tuning option or backend-specific Pulse Core
contract. Final paired Pulse conversion and installed acceptance remain separate;
this checkpoint did not install or restart Pulse.
