# Logical view dependencies in expression-local reads

September 12, 2026, `feature/v0.0.6` source follow-up to FP-3/FP-4 consumer
integration. This is not whole-profile, installed-Pulse or release acceptance.

## Reproduced defect

The view validator inspected outer MATCH and returning CALL branches, while its
dependency collector inspected materialized plan nodes. Expression-local queries
such as `EXISTS { MATCH(n:R)-[:R]->(m:R) RETURN n }` can be planned only when
evaluated. Consequently a view could persist **zero dependencies** despite reading
both tables. Unlabelled, untyped and reserved-metadata reads hidden inside EXISTS
also bypassed the stable-view admission rules. The initial four-case reproducer
failed all four assertions; it was not inferred from a search result alone.

## Correction and public contract

Validation now traverses language-owned AST structure across expression scopes,
including nested EXISTS, pattern predicates/comprehensions and returning read
CALL bodies. It does not traverse literal/parameter payloads. All view patterns
require explicit labels/types, and all scopes reject writes and reserved
`_grafx_` reads. Ordinary native queries retain their broader pattern contract.

Dependency collection retains plan dependencies and additionally resolves explicit
AST node labels by node kind and relationship names through their complete logical
group membership. Physical table IDs deduplicate the inventory; the existing
64-physical-table limit is enforced as dependencies are recorded. Full schema
hashes continue to include kind/identity. No new format, signature or configuration
is introduced, and no native transaction/read-publication rule changes.

This makes dependency changes visible even when a branch short-circuits or an
explicitly named table was absent when the view was created. Unrelated node DDL
does not invalidate a view. Growth of a referenced logical relationship group
does invalidate it, conservatively including newly added endpoint pairs excluded
by a particular node-label filter. Previously incomplete definitions refuse until
explicit validated replacement; no read-side rewriting or checksum bypass occurs.

See [consumer syntax and limits](../LOGICAL_VIEWS.md#graph-reads-inside-expressions-006-development).

## Tests

`tests/api/test_view_expression_dependencies.py` exercises actual native views:
EXISTS with/without RETURN, nested correlations/UNION, pattern predicates,
comprehensions, returning read CALL, both codecs, node/edge schema mutation,
replacement/reopen, absent named dependencies becoming present, logical group
growth, nested reserved reads and literal text that resembles query syntax.

The first expanded positive fixtures contained two ordinary query mistakes:
an unbound variable in a WHERE pattern predicate and different UNION output names.
Both correctly refused. The fixtures now use an anonymous labelled endpoint and
matching explicit output aliases. No parser/scope/error rule or original TCK
fixture was changed to make them pass; the failed receipt remains retained.

## Recorded evidence

Paths relative to `.grafx-tmp`; counts overlap and are not additive.

| Receipt | Result | Seconds | SHA-256 |
| --- | --- | ---: | --- |
| `view-expression-dependencies-first.xml` | 4 failed, before correction | 3.218 | `291fb572a397ab868d9166f4e492fddaa02fb89cb519d4c2912913ef114d7a95` |
| `view-expression-dependencies-qualified.xml` | 42 passed, no failures/errors/skips | 33.293 | `9493dac4d17dd512be554291fc8eb75fe1de9bf149f771a84078b0f17faae22a` |
| `view-expression-expanded.xml` | 12 passed, 4 fixture failures | 14.119 | `73e1d420c11feafcf884912c696192202298e0b215229f56d1aea7aca7b2bcef` |
| `view-expression-combined.xml` | 176 passed, no failures/errors/skips | 131.336 | `f6849d9c3f21013d12c671d787a4adf68e86a4430e9577e4aac97301f4cc5377` |

The combined selection includes the corrected 16 new view cases, all existing
logical views and same-name view tests, native EXISTS/predicate/comprehension
tests and eight namespace-consumer process cuts. It reached terminal exit 0.
The final public/import-boundary selection also includes all 19 current view
expression cases, including explicit returning-CALL and literal-text checks.

The first final-contract receipt (`view-expression-final-contracts.xml`) had
2,340 passes and one static annotation failure on the new local `record` helper,
63.188 s, SHA-256 `afa5755758a79fc8e47dc7c365d3c3146037d2802706e1f9a5ad8090a417b9eb`.
Its argument and return are now annotated `TableDef` / `None`; the static contract
was not bypassed by renaming/excluding the helper. Behavioral code is unchanged.

Final receipt `.grafx-tmp/view-expression-final-qualified.xml`: **2,341 passed**,
zero failures/errors/skips, 44.967 s, terminal exit 0; SHA-256
`01ed8a9eca858d9264b92138b71d9709b871d7af0094d3958c48fc0953453ffd`.
This includes the full public-surface/import-boundary suites, not 2,341 new
functional cases. Generated API, links/anchors, 39 configuration fields, 11
preserved source plans, changed/new Python lint and whitespace checks pass.

## Remaining delivery

This closes expression-local view dependency capture, not the full FP-3/4
checkpoint, all query shapes, final installed-wheel/Pulse validation or the wider
parity plan. The earlier installed transfer candidate predates this source-only
view correction and does not certify it. No production data, Pulse Core, global
installation, commit, push or release was changed.
