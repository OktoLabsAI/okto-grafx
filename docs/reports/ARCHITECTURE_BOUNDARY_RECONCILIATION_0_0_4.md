> Historical measurement/report. Not a current roadmap or release gate.
> See [current performance](../PERFORMANCE.md) and [roadmap](../../ROADMAP.md).

# Architecture boundary debt: query synchronization and pure algorithms

2026-09-08, `feature/v0.0.4`. This addresses the nine findings already recorded
in `RETAINED_METRIC_SAMPLING_0_0_4.md`; it is not a new performance acceptance gate.
The complete static architecture gate now **passes with zero occurrences**.
The final tuple-proof checkpoint below closes the original nine findings, without
new waivers. This closes that gate only, not the entire evolution/performance plan.

## Corrected engine synchronization placement

The query engine directly imported `threading.Lock` and `_thread.LockType` for
compiled-predicate publication and lazy result-plan materialization. G2 requires
mechanism creation in the composition, not in domain/engine code.

- `api.assembly` now supplies the dedicated compiled-predicate lock and a factory
  producing one independent lock per lazy public result.
- `QueryEngine` accepts a context-manager guard. Its lookup/publication/eviction
  sections are unchanged; compilation, evaluation and I/O remain outside them.
  Manual compositions omitting this dedicated guard reuse their existing injected
  endpoint guard. The standard API still supplies separate locks, not one global
  lock or a newly serialized reader/writer path.
- `Database` passes the plan-guard factory through the existing public snapshot
  boundary. `_OwnedPlanDoor` consumes a supplied guard instead of creating one.
  The API still clones each result's plan only on first access, exactly once under
  concurrent readers, independently of other results and after database close.
- Manual pure compositions without a plan-guard factory detach plans eagerly;
  they do not publish an unsynchronized lazy door. The result/plan contents and
  public `connect` configuration are unchanged. A supplied factory failure is
  normalized at the existing result boundary, not ignored.

No WAL, OCC, snapshot, on-disk format, query semantics or owner cache budget changed.

## Reconciled pure algorithm classification

The `heapq` imports in HNSW and the query engine implement deterministic heap
operations over caller-owned lists. They do not obtain locks, I/O, clocks, entropy
or process state. This meets the existing G2 purity criterion just as the already
accepted `bisect`, `math` and `itertools` do. The static test allowlist was missing
this algorithm module; it now admits `heapq` with an explicit rationale.

This is not an exception for host mechanism. `threading`, `_thread`, `contextvars`,
`weakref` and `inspect` remain excluded, with explicit negative assertions.
Synthetic checker cases accept domain/engine heap algorithms, reject an unrelated
`heapq_network` prefix and still reject a mixed heap-algorithm/thread-lock import.
HNSW implementation, scores, tie-breaking and asymptotic cost were not replaced
with a slower handwritten algorithm to satisfy an obsolete module list.
The frozen architecture contract was not changed.

## Public value-graph test synchronization

The broader boundary slice found two additional failures because its exact enum
list omitted the existing public `IndexLayout` (`HASH`, `ORDERED`). Both failures
were reproduced against the installed **a82d3bf** wheel, before these source edits;
they are not a new capability leak caused by guard injection. The test now includes
only that exact domain enum. A new negative case proves a foreign enum with the
same spelling is still rejected; the rule was not widened to arbitrary enums.

## Evidence

- Initial lazy-plan slice: all eight existing tests passed.
- Guard-factory/fallback tests plus compiled-predicate concurrency, cursor and
  public boundary concurrency: **65 passed in 3.98 s**.
- Combined query-result, compiled-predicate, cursor, public concurrency/query/
  operation/collaborator boundaries and the complete import gate:
  **610 passed, 5 failed in 10.80 s**. All five failures are the four unresolved
  modules plus the aggregate zero-budget assertion; they are not skipped/xfail.
- Ruff and `git diff --check` passed. These runs preserve strict markers and the
  existing 60-second per-test thread timeout; no hang guard was removed.

## Remaining existing architecture debt after query checkpoint

| Module | Occurrences | Mechanism still to separate |
| --- | ---: | --- |
| domain/control_record.py | 1 | `vars` for explicitly declared fused-read capability selection |
| domain/model/schema.py | 2 | thread lock and weak-key lifetime registry for tuple-encoding proofs |
| engine/index_manager.py | 1 | context-local live-commit authority/projection state |
| engine/recovery_manager.py | 1 | static descriptor inspection for recovery capability selection |

These guards/proofs may not simply be removed: forwarded storage capabilities,
encoding-proof authenticity/lifetime, concurrent commit isolation and recovery
admission must retain their existing behavior when mechanism moves outward.
The original nine occurrences are now five, not zero. This document is a partial
correction checkpoint, not completion of the architecture/evolution objective.

Source-only; accumulate with the remaining corrections before the next deployment.
Pulse PID 34048 remains on installed a82d3bf, with Core 9e91ea9 code. No additional
spec consolidation, recovery, redrive, reset, live graph write or restart was done.

## Recovery composition checkpoint — 2026-09-08

Moved static descriptor inspection from `engine.recovery_manager` to
`runtime.capability_probe.port_has_attribute`, supplied explicitly by API assembly.
The runtime helper preserves the former algorithm exactly: a declared member is
observed without evaluating its descriptor; only absent declarations use dynamic
lookup, only AttributeError means absence, and all other errors propagate. No
observation is cached, and this shape check grants no transaction/storage authority.

The engine still owns the complete mandatory member lists, aggregate missing-member
refusal, and all recovery/WAL/fencing policy. It additionally refuses non-callable
probes and non-exact-bool answers instead of admitting them by truthiness. The
recovery algorithm and frozen architecture contract are unchanged. The native
cold-WAL regression still proves only one walk rather than an eager shape scan.

Public `connect`/configuration/Pulse contracts are unchanged. **Internal manual
compositions constructing RecoveryManager now must provide the keyword-only
`attribute_probe`**, normally `port_has_attribute` imported in their outer layer.
All repository compositions and capturing subclasses were updated. There is no
default `hasattr` fallback that would reintroduce descriptor I/O; no public setting
or new required PortRegistry slot was added. The helper is not substituted for
PortRegistry's stricter static-only member validation, which has different semantics.

New tests cover inherited descriptors and slots without evaluation, instance
members, dynamic absence, malformed probes/results, preserved exception identity,
and engine-owned missing-WAL-member policy. Existing dynamic-wrapper, one-WAL-walk,
fence lifetime, control retirement, recovery and public crash tests remain intact.
The initial run identified two capturing test subclasses missing the explicit
injection; their compositions were fixed, not their assertions.

Grouped run (recovery WAL shape/manager/fence/retirement/commit-section tests,
transaction commit-state fallback, API startup/public crash recovery, entire import
gate): **366 passed, 4 failed in 13.10 s**. The four failures are exactly the three
remaining modules below and the aggregate zero-budget assertion. Strict markers
and 60-second thread timeouts remain enabled. Ruff and diff whitespace checks pass.

| Remaining module | Occurrences | Mechanism still to separate |
| --- | ---: | --- |
| domain/control_record.py | 1 | explicit concrete fused-read capability selection |
| domain/model/schema.py | 2 | synchronized weak-lifetime tuple-encoding proof registry |
| engine/index_manager.py | 1 | context-local live-commit authority/projection state |

No gate waiver and no all-plan completion claim. This checkpoint is source-only:
the running Pulse remains on installed Grafx a82d3bf; no pending spec was consumed,
no live graph was opened by tests, and no restart/deployment/rebuild/redrive occurred.

## Control-record I/O composition checkpoint — 2026-09-08

Removed concrete-type namespace inspection from `domain.control_record`.
`adapters.control_record_io.read_control_if_exists` now owns exactly the previous
selection algorithm. The concrete type must explicitly declare a callable fused
method; inherited methods and generic `__getattr__` forwarding do not opt in.
Selection is fresh on every call, then resolves through the instance so local
instrumentation remains effective. No capability, identity or authority is cached.
Absent opt-in retains exists/read_log, including an error if the name disappears
between those calls. None and empty bytes remain distinct.

TwoSlotControlRecordStore receives an optional `read_if_exists` operation typed by
the pure ControlRecordReader alias. Manual compositions without it use the literal
storage port; they do not inspect adapter implementation namespaces. An invalid
non-callable reader is refused. The normal public composition explicitly supplies
the adapter operation through TransactionManager/RecoveryManager -> CommitStateStore
-> slot store. LocalProcessCoordinator and offline control migration also supply it.
ReadOnlyStorageDevice uses the same adapter selection logic while retaining every
write refusal and its unchanged external signature.

This preserves the optimized native path, not just a slower compatible fallback.
The public integration test spies actual LocalStorageDevice fused operations and
proves calls for writer.lease and commit.state during a commit, plus commit.state on
writable and read-only reopen. Existing strict/generation descriptor tests still
prove the exact observation counts. Manual low-level consumers wanting the fused
optimization now inject this reader from their composition layer; neither public
connect settings nor the mandatory StorageDevice/PortRegistry contract is expanded.

The domain still owns bounded image sizes, slot checksums, generation rollback,
legacy length consistency, reread limits, physical publication and durability
barriers. No format, snapshot/OCC protocol, multi-reader/writer assumption or public
Pulse API changed. This is an architectural repair preserving an existing gain,
not a newly measured speedup or another full-write benchmark.

Evidence:

- Initial control/readonly/descriptor/downgrade slice: **99 passed in 1.10 s**.
- Added tests for pure fallback, inherited capability refusal, fresh selection
  after class declaration replacement, instance instrumentation, malformed callback
  and exception identity. Public reopen fixture initially omitted its prerequisite
  checkpoint and was correctly refused; the fixture now checkpoints, not the engine.
- Grouped control/readonly/descriptor/downgrade/slot-crash/control-retirement/
  commit-state-fallback/startup/public-crash/import gate: **394 passed, 3 failed in
  12.43 s**. Failures are schema.py, index_manager.py and the aggregate import gate.
- Coordination publication: **10 passed in 0.28 s**, separately collected because
  its absolute `from conftest` conflicts with txn/conftest when combined. The failed
  collection attempt is not a passing or skipped-test result; both intended groups
  were rerun to completion without relaxing the skip gate.
- Strict markers, 60-second thread timeouts, Ruff and diff checks retained.

Remaining occurrences: schema.py thread lock/weak-key proof registry (two), and
index_manager.py context-local commit authority/projection (one). No gate waiver.
Source-only; Pulse stays on installed a82d3bf and all twenty benchmark specs remain
reserved. No live consolidation, migration, graph reset, redrive or restart.

## Commit selection context checkpoint — 2026-09-08

Separated the attempt-local index-selection channel from the physical live-commit
authority channel. The former no longer creates a module-global ContextVar in
IndexManager. API assembly injects an independent `runtime.scoped_value.ContextLocalValue`
per manager through the pure `domain.ports.scoped_value.ScopedValue` protocol.
No mandatory PortRegistry slot or public connect/Pulse setting was added.

The runtime transport only gets/binds a value with ContextVar token restoration.
The engine still creates/seals the projection, matches manager and exact transaction,
checks registry/schema/detached-claim drift, and revokes the payload before transport
reset. Context copies retain the same payload identity and therefore observe its
revocation. Failures in bind, context entry, body (including BaseException), or exit
cannot leave an active retained projection. Two threads/tasks cannot see one another's
binding; identically named but separately constructed slots remain independent.
Even an intentionally shared transport does not bypass manager identity checks.

Manual low-level IndexManager compositions may omit `projection_context`, in which
case they retain canonical fresh index selection. To keep the optimization they
must supply ContextLocalValue in their outer composition. Public assembly always
supplies it. The structural cost test explicitly checks both paths: **three** table
selection calls with the transport (unchanged optimized expectation), **four** without.
Its initial regression failed because the old manual fixture omitted the newly
explicit mechanism; injection restored the original three-call assertion and the
four-call fallback is now tested separately, not substituted for the optimized claim.

New tests cover nested scopes, copied-context revocation, two concurrent threads
on one manager, concurrent async tasks on one thread, cross-manager refusal, malformed
transport, registry drift, failures at each transport boundary, and actual public
commit reuse plus persisted row readback. Initial focused slice: 12 passed in 0.31 s;
the subsequent async case and fallback parameter are included in the grouped run.
Grouped scope/live-hot/detached-generation/speculative-registration/commit-protocol/
index-wiring/identity-activation/multiprocess-fence/read-only-adoption/public-boundary/
import-gate run: **538 passed, 3 failed in 23.06 s**. Ruff/diff checks pass; strict
markers and per-test 60-second thread timeout remain enabled.

**The import-gate count has not decreased in this checkpoint.** IndexManager still
uses ContextVar for the separate physical live-commit authority, shared with IndexStore;
that channel was not weakened or replaced with ordinary mutable instance state.
The remaining debt is still three occurrences across schema.py and index_manager.py,
plus the aggregate failing assertion. This is a verified implementation step toward
separating the remaining mechanisms, not an all-green gate or new performance claim.
No WAL/OCC/durability/reader-writer guarantee, persisted format or Pulse Core changed.
Source-only; installed Pulse and the twenty reserved specs were not touched.

## Physical live-commit context checkpoint — 2026-09-08

Removed the remaining ContextVar import/global from IndexManager/IndexStore.
API assembly now supplies two distinct ContextLocalValue instances per manager:
one for commit selection and one for physical live-commit authority. IndexManager
still mints the sealed manager/transaction authority only through its fenced private
door, after the surrounding TransactionManager's WAL/fencing protocol. The payload
is revoked before context reset and also after failed bind/entry. Context copies
retain the same revoked payload, not a fresh grant.

The unchanged native IndexStore commit is selected by an exact bound MethodType,
bound receiver, and captured canonical function identity. That selection calls the
private native body with the **context transport**, not a detached authority value.
The store reads the current execution-context value and checks the active seal,
the manager's exact transport identity, the exact transaction and selected store.
Passing the transport into another thread therefore finds no scope and cannot
enable a hot batch. No ordinary shared manager/store flag replaces context isolation.

`IndexStore.commit(txn, csn)` keeps its original signature. Store overrides, including
callables spoofing method metadata, are invoked once with their original two arguments
and keep full scalar checks. Calling `super().commit` from such an override does not
implicitly inherit the native hot-batch privilege. This is an explicit optimization
fallback for customized commit implementations, not deletion of their functionality;
their invocation and resulting entries are tested. OrderedIndex's distinct COW commit
also remains on its existing method/signature. Existing physical-hook fallbacks and
hot-bucket bounds are unchanged. Manager commit overrides still execute inside their
injected lexical scope; nested and exceptional overrides restore/revoke it correctly.

Manual low-level IndexManager compositions wanting the physical hot-batch optimization
now supply `live_commit_context=ContextLocalValue(...)` in their outer composition.
Omitting it preserves scalar application. The native test fixture explicitly injects
it so the existing threshold, bucket-preflight-count, byte/effect-equivalence,
failure-prefix/retry and staging-preservation tests continue exercising the accelerator,
not a silently disabled path. Public composition injects it automatically; public
commit tests observe actual native preparation and read all eight written rows back.

Evidence: initial unchanged hot/projection slice **34 passed in 0.57 s**; additional
transport/revocation/customization/public cases **43 passed in 0.52 s**; final grouped
hot/context/detached/speculative/index-wiring/commit-protocol/identity/multiprocess/
read-only/public-boundary/import gate **549 passed, 2 failed in 21.97 s**. The only
failures are schema.py and the aggregate zero-budget assertion. Ruff/diff checks pass,
strict markers and 60-second thread timeouts remain. No new timing-gain claim or
changed performance threshold; the existing optimized structural checks remain.

Remaining architecture debt is only schema.py's synchronized weak-lifetime tuple
encoding proof registry (two occurrences: threading and weakref). The gate is not
waived or complete. Public connect/Pulse configuration, persisted format, OCC/WAL,
multi-reader/writer and durability policy are unchanged. Source-only; no Pulse
restart/install, live consolidation, reset/rebuild/redrive or benchmark-spec consumption.

## Tuple-encoding proof composition — gate closed, 2026-09-08

Removed threading/weakref mechanism creation from schema.py. Runtime now allocates
a synchronized WeakKeyDictionary through `new_tuple_encoding_proofs()` for each
database participant. API assembly shares that protocol with QueryEngine,
TransactionManager (which passes it into its transaction contexts), and HeapStore.
There is no process-global proof registry or mechanism-setting mutation. The mapping
and guard are implementation dependencies supplied by the trusted composition,
not user-configurable runtime knobs or a new mandatory StorageDevice/PortRegistry slot.

The domain factory still owns the private Proof class, the exact immutable-value
admission rules, canonical encoding before proof publication, exact table/values
identity checks, and revocation. A copied Proof, an unregistered instance of its
class, a caller object, another table/value tuple, or a proof minted by another
participant cannot authorize reuse. Unknown/mutable nested values remain on the
canonical path. Foreign registries cannot revoke the original proof. The registry
retains exact table/values/payload references while its proof lives; weak keys drop
the entry on GC, and existing transaction budget/rollback/commit revocation remains.
Registry operations alone hold the guard; canonical encoding and heap I/O do not.

Manual internal compositions wanting one-encode reuse now create one protocol in
their outer layer and pass the **same** `tuple_encoding_proofs` to their query,
transaction manager and heap. TransactionManager wires TransactionContext itself.
Omitted protocol means canonical encoding/no proof; it does not discover a global
registry or accept a caller-supplied proof by shape. The old private helper names
remain with an optional explicit `protocol` argument for internal compatibility.
Public connect/Pulse settings, canonical encode_tuple behavior and durable bytes
are unchanged. The internal protocol is not returned as public graph/result data.

Evidence:

- Initial one-encode tests plus full import gate: **215 passed in 6.34 s**.
- Strengthened proof checks (including the existing post-commit assertion now
  querying the actual participant registry), forged/copy/deepcopy/unregistered
  proofs, participant isolation, GC retention, malformed rows, guard scope,
  concurrent encoding and omitted-protocol/mutable fallbacks: **15 passed in 0.99 s**.
- Grouped row-proof, pending relationship overlays, transaction quotas/intents/
  commit protocol, heap writes, both index context channels and live hot batches,
  public collaborator/query/operation boundaries, and **the full unmodified import
  gate**: **958 passed in 16.01 s**, no failures or skips. Strict markers and the
  60-second per-test thread timeout remain enabled. Ruff and diff checks pass.
- Existing native public CREATE and UPDATE assertions still count **one** canonical
  encode per final row. Exhausting the private retention budget still costs two,
  not a bypass; a legacy overridden heap door still re-encodes and receives its
  original call contract. DELETE intent accounting and pending-endpoint validation
  were not relaxed.

All nine original static findings are closed; the frozen architecture contract and
the remaining mechanism import prohibitions were not relaxed. This is not proof
that every evolution requirement is complete, nor a new real-spec latency result.
Source-only until accumulated deployment: Pulse remains on installed a82d3bf,
with no restart, graph mutation, recovery/redrive or consumption of reserved specs.

## Accumulated installed-wheel deployment — 2026-09-08

The source-only checkpoint above is now deployed: **Grafx 0.0.4@9b41f36**,
wheel SHA-256 `93DE9672912947DD933BB603634368603B25D5248CBDAC394EC30BB479129EAF`.
Before replacing the package, an isolated wheel installation passed **79 tests in
1.35 s** (encoding proofs/composition, both index contexts, two-slot controls and
WAL port shape), importing from that installed target rather than source. Strict
markers and the 60-second thread timeout remained enabled.

Pulse PID 34048 shut down gracefully; its owning execution handle reached terminal
exit 1, the PID disappeared and both listening ports were free before replacement.
The wheel was installed into Python 3.13's user environment, with NumPy 2.5.2 and
google-crc32c 1.8.0 retained. Twelve installed module hashes match source exactly.
Pulse **0.3.3 PID 28232** now serves 8100/8101, using unchanged Core code `9e91ea9`
and Community checkout `7158383`; Grafx is imported from the installed package,
not injected from its source tree. HTTP root returned 200.

Authenticated MCP schema inspection and canonical readback succeeded: the five
Alternative/Decision rows are exactly equal before/after. The readback took
0.144 s MCP / 54.2 ms executor; this is a readiness sample, not a speedup claim.
All 20 pending projections and the entire 40-item ledger are unchanged (20
consolidated, zero in progress/failed/skipped). No new spec was consolidated,
and no graph reset, rebuild or DLQ redrive was issued.

The initial cold health probe explicitly returned unavailable metrics while its
refresh was running. The follow-up returned Board/Discovery healthy, metrics
available, 2,965 nodes and queue depth zero. Overall remains at_risk due to the
historical policy projection DLQ; historical Global DLQ and canonical debt were
not repaired or hidden by this deployment. This is not an all-green health claim
or a new full-write benchmark: the previous real-spec measurements remain
22.981 s commit and 131.489 s downstream delivery on their documented source.

Sanitized local evidence: `.grafx-tmp/deploy-9b41f36-live-evidence.json`, SHA-256
`429B591127ECAB897E7B2FCC55155588BAC7E2BE1980D755C5886394EA82ACC6`.
No PyPI publication or main merge was performed.
