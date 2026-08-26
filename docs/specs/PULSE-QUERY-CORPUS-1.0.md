# Pulse query corpus — descriptor `pulse-1`

`KG_QUERY_CONTRACT_VERSION = 1.0`

M-PULSE-2A introduced this freeze of **what Pulse actually asks a graph to do**, so every
language sub-batch can ratchet against evidence rather than against an impression of how much
Cypher is in use. M-PULSE-2B added `coalesce`, `string_split` and `size`; M-PULSE-2C adds searched
and simple `CASE` plus list subscripts. The remaining gaps stay explicit.

The scanner and JSON do not widen the public endpoint or execute Pulse code. Engine changes
are reviewed in their own commits, and regenerating this corpus makes each accepted/refused
transition visible instead of silently redefining the target.

## Baselines

The corpus describes two exact commits and nothing else.

| Baseline | Repository | Pinned commit |
| --- | --- | --- |
| `community` | Pulse Community `feature/v0.3.3` | `befaf1e4f9da9d0cff7cfc0f4aee177ef0a3e595` |
| `core` | Pulse Core `milestone/grafx-transaction-contract` | `ab61b9a785f2018312fc91541a580877fd068bbb` |

The Community milestone earlier reported `36c2fc6`. `befaf1e` is the branch's final head and
its `src/` is byte-identical to `36c2fc6` — the delta is documentation and one test file,
confirmed with `git diff 36c2fc6 befaf1e -- src/` rather than taken on trust. Pinning the
final head therefore changes no entry.

Sources are read from one in-memory `git archive` of each pinned SHA, never from a working
tree. Those worktrees belong to other agents and move; a corpus read from a checkout would
quietly describe whatever happened to be checked out when it ran. Reading the immutable tree
at the commit is what makes regeneration reproducible from any clone, and batching it in one
archive avoids launching one `git show` process per source file. The dirty original
repositories are never needed.

## What is in the corpus

Three surfaces, kept apart because they answer different questions.

**`public_named_template`** — the read-only templates the contract exposes as module constants
in `okto_pulse/core/kg/cypher_templates.py`, plus the label-parametrized generator expanded
over the whole label allowlist. A constant that is a clause fragment rather than a query
(`_DEFAULT_FILTERS`) is recorded as `public_fragment` instead of being skipped.

**`public_raw_contract`** — the grammar the raw endpoint admits, as a matrix of constructs.

**`internal_family`** — the statements the audited originators hand to
`GraphTransactionScope.execute`, grouped into closed families. The surface is that audited
call graph, not "every `.execute` whose argument looks like Cypher": provider internals,
schema/runtime bootstrap, the SQL projection and the other ports belong to later milestones
and are excluded by name.

Statement content decides whether a candidate is graph or SQL, because a receiver name cannot:
`conn` is a SQL connection across Community and a graph connection inside these Core handlers.
The receiver name is used only to dismiss calls that are not statements at all, such as an
application use-case.

Each family carries a stable id, every origin that supports it, the statement class, the
normalized template, the parameter shape, a structured `expected`, and a classification.
For a read, `expected` freezes the projected expressions, cardinality and ordering together
with one `column_metadata` item per column (`expression`, logical `type` and `nullable`). For a
write, it freezes the effect, targets and expected count; a write that returns values has the
same typed oracle in `return_metadata`. `expected.error` is `null` when the materialized form
plans, otherwise it records the refusal phase, stable error code, exception type and message.
This keeps an unsupported family useful: closing the language gap must change the error to
`null` without silently changing the row or effect contract.

## Counts at `pulse-1`

`97` entries, digest
`75622dfe057ca446d200c91ad1041718bd15058d99e222948f3aae339901dfa0`.

The engine currently classifies 69 entries as `already_supported` and 26 as `generic_gap`;
the duplicate and declared fragment remain separate classifications.

**Internal families** — 68 closed families over the audited originators, 47 read and
21 write, identified `I01`..`I68`. Each records every origin that supports it. 66 are
runtime-current; 2 are preventive fallbacks that run only for a provider without the vector
tombstone primitive.

**Public named templates** — 28 entries over 27 distinct normalized texts: 17 named module
templates plus 11 generated expansions of `supersedence_chain_template`, one per label. The
`Decision` expansion returns the same normalized text as a named template and is recorded as
a declared duplicate, not invented as a 28th text. One further constant is a WHERE fragment,
not a query, and is recorded as the single `public_fragment`.

**Structural authorities** — every structural hole a frozen template still carries is bound
to a closed authority. The pinned schema has exactly 11 node labels, 16 relationship types
and 69 valid relationship endpoint pairs; the corpus binds 68 label holes and 14 relationship
type holes to those domains. Business values never appear in a template; they are always
`$parameters`. A hole matching no authority fails the freeze.

The single `public_fragment` is a real declared clause fragment. It is different from the
classification `runtime_fragment`: all executable families are fully materialized in this
freeze, so `classification:runtime_fragment` is exactly zero.

## The raw contract as a grammar

`public_raw_contract` is a versioned matrix of 87 probes, not a list of frozen application
queries. The raw endpoint is open by construction, so freezing texts would describe a
different endpoint from the one the contract publishes.

Each probe carries two independent answers. `contract_disposition` is what the **public
endpoint** admits; `engine_verdict` is what **this engine** does with the same text. They
differ on purpose: the contract blacklists writes, while the engine accepts writes because
the internal port needs them. Today the contract allows 74 probes and refuses 13 (10
`unsafe_cypher`, 3 `unsupported_operation`); the engine accepts 66 and refuses 21. The
intersection that matters to the public endpoint is the 13 allowed probes the engine still
refuses.

### What M-PULSE-2 owes

These 13 constructs are admitted by the public contract and refused by the engine:

| Construct | Category | Refused at |
| --- | --- | --- |
| `OPTIONAL MATCH` | root | parse error |
| `UNWIND` | root | parse error |
| `WITH` | root | parse error |
| `UNION` | clause | parse error |
| `map batch` | parameter | parse error |
| `map access` | expression | parse error |
| `untyped relationship` | pattern | plan error |
| `polymorphic node` | pattern | plan error |
| `named path` | pattern | parse error |
| `root operation as a homoglyph` | security | parse error |
| `unsupported clause after a supported root` | taxonomy | parse error |
| `unbounded variable length` | limits | parse error |
| `path projection` | result | parse error |

No function is owed any more. `label` and `timestamp` were the last two, and they failed at
**analysis** rather than at parsing, because an unknown function still parses into a generic
call node — which is why acceptance here runs `analyze` and `build_plan` and not the parser
alone. With M-PULSE-2D they join the three scalar helpers, standalone list indexing and both
CASE forms at `planned`; those ratchets are frozen in both the JSON and its sentinels.
`map access` remains owed because its public probe starts with `UNWIND`, so it
cannot become accepted until that separate clause exists. This is why acceptance records parse,
analysis and planning separately.

### The behavioural contract beside the grammar

The raw payload also freezes 36 code-derived behaviours in seven groups:
`schema_domain`, `context_shape`, `defaults_and_limits`, `security_normalization`,
`error_taxonomy`, `layer_enforcement` and `result_envelope`. These are separate from syntax
probes because a parser result cannot prove endpoint normalization, defaults or projection.

The real root domain is exactly `MATCH`, `OPTIONAL`, `UNWIND`, `WITH` and `RETURN`. The
published clause vocabulary is recorded too, but the pinned validator does not enforce that
vocabulary after the root: it applies the blacklist and root check. The probe for an
unsupported later clause makes that distinction executable instead of implying that a
published whitelist is already an enforcement boundary.

Before tokenization, the endpoint strips comments, applies Unicode NFKC normalization and
blanks quoted string literals. The matrix therefore proves that a word in a comment or
literal is not treated as an operation and that a fullwidth write token cannot bypass the
blacklist. Refusals also preserve the public taxonomy: a write is `unsafe_cypher`, an unknown
non-mutating root is `unsupported_operation`, an unsafe canonical rewrite is
`canonical_filter_unenforceable`, and a missing executor is `graph_backend_unconfigured`.

Typed values keep their boundary explicit. Graph `TIMESTAMP` values remain typed until
`KGService` converts them to ISO text for its public result. Similarity retrieval stays on
the structured `graph_store.vector_search` port; Pulse does not require a provider-specific
raw Cypher vector function. The raw `timestamp()` probe is recorded as an endpoint
language mismatch, while these behavioural facts prevent it from being confused with the
way current Pulse application paths store timestamps or request vector search.

Finally, both paired and single execution pass through the same canonical projection. Its
envelope contract covers `columns`, the recomputed `row_count`, the preserved `truncated`
flag, `working_omitted_count`, `working_omitted_count_exact` and `omitted_layer_counts`.
The paired-path and projection behaviours are frozen as well, so filtering mode and omitted
working-layer evidence cannot disappear behind a row-only compatibility test.

## How classification is decided

By execution, not by judgement.

1. A statement built by a named `GraphTransactionScope` primitive is `structured_primitive`.
   The list of primitive symbols is in the freezer and is the auditable part of this rule.
2. Otherwise the template is materialized and handed to this repository's own `parse`,
   `analyze` and `build_plan` against the closed Pulse catalog. Only a query accepted by all
   three is `already_supported`; the last phase reached is recorded in `acceptance_phase`.
   Parsing alone is not accepting: an unknown function becomes a generic call node and is
   only caught by `analyze`, while polymorphic nodes and untyped relationships reach
   planning before they are refused.
3. Materialization happens **twice**: once filling holes with identifiers, once with the
   empty string. A hole is not always an identifier — some carry an optional clause — and
   filling a clause hole with an identifier produces text that was never sent. If either
   filling plans, the shape is supported.
4. If both fail and the refusal names a placeholder, the result is `runtime_fragment`: the
   freezer could not present the real text, so the refusal is not evidence about the engine.
   Calling it a gap would put work on a milestone that may not need it.
5. Only a refusal that survives both fillings without naming a placeholder is a
   `generic_gap`, and the parser's message is recorded with it.

Holes are marked `<<expr>>`, not `{expr}`. Cypher's inline property maps are braces, and an
earlier version of this freezer conflated the two: real syntax looked like a hole, the
materialized template failed to parse, and 37 forms were reported as gaps that were not.

A hole that calls a pure clause builder is not a hole in the query Pulse sends — it is where
the query gets its `coalesce`. Those builders are evaluated from the AST at the pinned commit
by a bounded evaluator that understands f-strings, module constants, set unions, `sorted`,
`join` over a generator and local assignments, following imported constants into their own
module. Nothing from the baseline is imported or executed, and an expression the evaluator
cannot prove leaves the hole intact rather than inventing text for it.

At this baseline the bounded evaluator resolves every executable candidate: the corpus has
zero `runtime_fragment` entries. The one fragment in the counts is the intentionally declared
public WHERE fragment described above.

## Fail-closed

The freeze refuses rather than guesses:

- a statement the freezer cannot follow, even after resolving one hop back through an active
  wrapper to the callers that build it;
- a dynamic argument handed to an explicitly graph-scoped receiver in a module absent from
  both the audited inventory and the recorded exclusions;
- a template hole bound to no closed structural authority;
- an inventory that no longer matches the audited per-module family table;
- a preventive family whose numbering has drifted away from the audited id;
- a baseline pin that is not a complete commit hash;
- the same template classified two different ways.

`tests/corpus/pulse_query_allowlist.json` is **empty**, and the tests keep it that way. The
freeze stands on resolution alone; an entry there would be a form the corpus does not
describe, and each would have to say why in prose.

A name assigned in more than one branch is expanded into **all** of its forms rather than
refused or sampled — a statement chosen in an `if/else` is two statements Pulse really sends.

## Regenerating and verifying

Neither step needs the dirty working repositories.

```
# point at any clone that contains the pinned commits
export PULSE_COMMUNITY_BASELINE=/path/to/okto-pulse
export PULSE_CORE_BASELINE=/path/to/okto-pulse-core

python tools/pulse_query_corpus.py --check    # verify the frozen corpus is current
python tools/pulse_query_corpus.py --write    # regenerate it
python -m pytest tests/corpus -q              # the fail-closed inventory
```

`--check` compares the rendered corpus byte for byte and fails if it is stale. The inventory
test additionally re-derives the corpus and compares digests, and proves the refusals are
real by removing a known receiver and by emptying the allowlist, each of which must break the
freeze.

If the pinned commits are not reachable, the tests that need them skip with a message naming
the environment variable to set. They never pass silently on absent baselines.

## What this deliberately does not do

- No clauses, provider work or endpoint activation. M-PULSE-2B/2C/2D close only the scalar,
  CASE, standalone-list and function forms recorded above; the other 13 admitted/refused
  constructs remain later M-PULSE-2 work.
- No hidden profile switch or Pulse-specific bypass: the helpers use the ordinary typed planner
  and executor paths.
- No differential execution against Ladybug. The corpus records what Pulse sends and whether
  this engine plans it. Its structured expected rows/effects, types, nullability, cardinality
  and ordering are static compatibility oracles, not sampled Ladybug results.
