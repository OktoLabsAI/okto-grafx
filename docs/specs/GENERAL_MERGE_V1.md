# Whole-pattern MERGE — FP-4

September 12, 2026, `feature/v0.0.6` source. This closes the explicit unbound and
fixed multi-hop MERGE shape gap in the [functional parity plan](FUNCTIONAL_PARITY_PLAN.md).
It does not close label mutations, the full profile or installed Pulse acceptance.

## Query and consumption contract

Use the existing `Transaction.execute()` write door:

```python
with db.begin("write") as tx:
    result = tx.execute("""
        MERGE p=(a:Author {name:$name})-[:WROTE]->(b:Book {title:$title})-[:IN]->(:Topic {name:$topic})
        ON CREATE SET a.hops=length(p)
        RETURN p
    """, {"name": "Ada", "title": "Notes", "topic": "Computing"})
```

MERGE searches for the **complete pattern**, with zero, some or all node endpoints
already bound. Every complete matching path contributes one output and its
`ON MATCH` actions. If none matches, it creates every unbound node and relationship
once for that input. Only bound entities are reused in this branch. A matching
unbound node or prefix alone is **not** reused. Consequently a typed primary-key
constraint may reject whole-pattern creation; MERGE does not silently change to
an incremental node-by-node upsert. Bind an existing endpoint in a preceding MATCH
or separate node MERGE when that reuse is intended.

Repeated references to one node retain its identity. A complete match cannot
reuse the same relationship twice in one path; parallel edges retain multiplicity.
Incoming arrows keep their orientation. Plain undirected edges search both
orientations and, when creating, use the written left-to-right orientation.
Unlabeled matches may find labeled nodes and return their actual labels. An empty
unlabeled creation branch creates a genuinely unlabeled node. Typed endpoint
constraints and independent node/relationship namespaces remain authoritative.

Pattern property expressions are evaluated once per input and reused for both
matching and creation, including volatile expressions. Top-level NULL properties
raise `GrafxPlanError`, `reason="merge_null_property"`, during execution; persisted
nonfinite values remain forbidden. The complete match set is retained before any
conditional action. Action expressions then see earlier owner-visible updates,
including paths sharing a node. Subsequent input rows see preceding input writes.
Native provisional/durable identity rules are unchanged; a value returned before
commit is not retroactively rewritten into a durable receipt.

Read-only execution refuses. Late expression/action/constraint/resource failures
roll back the entire statement, including its implicit schema and private read
phases, while preserving earlier successful statements only through the existing
proven rollback boundary. Correlated writing CALL uses the same transaction. There
is no second transaction, independent commit, new lock mode or weaker OCC guarantee.
Pinned independent readers keep their own snapshot; conflicting writers still
fail rather than overwrite each other's updates.

## Implementation, resources and public surface

The planner retains specialized single-node/bound-single-edge paths. General
patterns use a compiled correlated native MATCH child, not parsing/evaluating
query text per input. The empty branch executes native CREATE descriptors, not
per-element MERGE. Public explain/plan inspection can therefore show the additional
read child of `MergePattern`; it shares the same statement authority.

The general path retains its complete match set per input under the existing
`query_memory_budget_bytes` when configured. Insufficient memory raises
`GrafxQueryBudgetExceeded` with `operator="merge_matches"` before that input's
actions; it never truncates matches. This buffer does not spill. No new setting or
default is introduced. Ordinary traversal/source/expression/plan limits still
apply. This is bounded **fixed-length** pattern support, not variable-length
relationship creation, unlimited patterns, label mutation or universal uniqueness
under concurrent MERGE. There is no new storage format, capability bit, Python
method signature or detached result DTO.

## Qualification

Independent positive/negative cases cover partial versus complete matching,
repeated inputs, shared-node actions, native labels/namespaces, typed uniqueness,
mixed arrows, repeated nodes, relationship uniqueness, writing CALL, NULL/action
rollback, memory refusal, read-only admission, deterministic random-source
observations, pinned readers, conflicting writers and cold reopen.

The grouped query/planner/action/CALL/UNION/memory/public-plan regression passes
**307 tests**, zero failures/errors/skips, in 77.800 seconds:
`.grafx-tmp/general-merge-qualified.xml`, SHA-256
`d086612f1f3b774c03d01f1f53d966923fb7508a560ee9ceb106dbb24056b5b8`.

The initial fault collection passes **20 tests**, zero failures/errors/skips,
94.690 seconds: `.grafx-tmp/general-merge-recovery.xml`, SHA-256
`b88d77de75f00f6aafc187be49b45c71fac856bc1bfbde20f6908f2ecd689053`.
Four new pure/NumPy process-cut cases interrupt before COMMIT or after durable
COMMIT before page application, recover twice and verify the entire graph. The
remaining cases exercise existing written-path/action recovery. That collection
predates the final action-refresh change; the final combined collection
re-executes all four new process cuts on the final source.

The final collection passes **2,390 tests**, zero failures/errors/skips, 84.669
seconds: `.grafx-tmp/general-merge-final-contracts.xml`, SHA-256
`0badc22674a5f380ee55f88548533e49cce0df33c441e567af17397047186826`.
It contains all 29 current general-MERGE cases, four new process cuts, public
surface/annotation contracts and import boundaries. These collections overlap;
their totals are not added into a distinct-test claim. All three pytest processes
reached terminal exit 0. This is affected-area qualification, not the full
repository regression or the frozen full-profile gate.

The original MERGE family still records **70 passed / five failed / 3,822 outside
the selection**, terminal exit 1. Four required label-action failures and one
retained multiple-label divergence remain; no source/expectation/ledger changes
were made. Receipt `.grafx-tmp/general-merge-tck.json`, SHA-256
`b79f497a62d4a57ee73d638690ab0f007f0ce343a4a0a9ce015612e8aaae3b33`.
These supplemental general-shape tests do not imply additional original TCK passes.

Historical failed receipts remain intact: the initial 12-case baseline had ten
shape-admission failures; two broad negative tests passed at admission and were
subsequently strengthened. The next 12-case run had two fixture assumptions
comparing precommit provisional IDs with committed IDs; the test now compares
durable read results. The 275-case run reproduced two real stale-action-value
failures (shared-node increments returned 2,2 instead of 2,3); native entity
refresh between frozen matches fixes them. None is relabeled passing evidence.

Global installations, production data, commits, pushes and releases are unchanged.

The subsequent [Pulse follow-up](../reports/GENERAL_MERGE_PULSE_QUALIFICATION.md)
qualifies the neutral transaction/read-only adapters on source and three private
installed wheels. It does not require a Core production change or grant UI writes;
HTTP/browser and full-profile acceptance remain outstanding.
