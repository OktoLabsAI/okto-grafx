# Complementary capability specification index

These specifications implement GX-CAP-0 routing, not the capabilities themselves.
Every source requirement remains incorporated in full; the short specifications
add ownership, prerequisites and refusal/recovery boundaries without replacing the
[database-first roadmap](../../GRAFX_COMPLEMENTARY_EVOLUTION_PLAN_CODEX.md) or
[agent-first roadmap](../../AGENT_FIRST_EVOLUTION_PLAN_CODEX.md).
The [main evolution plan](../../EVOLUTION_PLAN_CODEX.md) governs their interaction.

| Milestone | Specification | Implementation status |
|---|---|---|
| GX-CAP-0 | [Boundaries/contracts](SPEC-GX-CAP-0.md) | Enforcement and contract checkpoint |
| GX-CAP-1 | [Commit identity and provenance](SPEC-GX-CAP-1.md) | Admission and record codec implemented; paged store/publication pending |
| GX-CAP-2 | [Catalog sessions and workspace scopes](SPEC-GX-CAP-2.md) | Not certified; contract routing only |
| GX-CAP-3 | [Temporal system time](SPEC-GX-CAP-3.md) | Not certified; contract routing only |
| GX-CAP-4 | [Valid time, bitemporal queries and graph diff](SPEC-GX-CAP-4.md) | Not certified; contract routing only |
| GX-CAP-5 | [Native full-text search](SPEC-GX-CAP-5.md) | Not certified; contract routing only |
| GX-CAP-6 | [Explainable hybrid retrieval](SPEC-GX-CAP-6.md) | Not certified; contract routing only |
| GX-CAP-7 | [Extension SPI and UDFs](SPEC-GX-CAP-7.md) | Not certified; contract routing only |
| GX-CAP-8 | [Arrow and external data](SPEC-GX-CAP-8.md) | Not certified; contract routing only |
| GX-CAP-9 | [Graph projections and algorithms](SPEC-GX-CAP-9.md) | Not certified; contract routing only |
| GX-CAP-10 | [Application schema evolution and views](SPEC-GX-CAP-10.md) | Not certified; contract routing only |
| GX-CAP-11 | [Database hardening and release](SPEC-GX-CAP-11.md) | Not certified; contract routing only |
| GX-AGENT-0 | [Optional Agent/MCP preview](SPEC-GX-AGENT-0.md) | Not certified; contract routing only |
| GX-AGENT-1 | [Optional integrated knowledge layer](SPEC-GX-AGENT-1.md) | Not certified; contract routing only |

## Detailed optional-product requirements remain separate

GX-AGENT-0 consumes the detailed AGENT-0 (contracts), AGENT-1 (workspace),
AGENT-2 (identity/provenance/idempotency), AGENT-3 (knowledge API),
AGENT-4 (MCP preview), and the required AGENT-5 namespace/policy work.
GX-AGENT-1 consumes AGENT-6 (hybrid ContextPack), AGENT-7 (temporal lifecycle),
and AGENT-8 (hardening). Their full source sections and gates remain mandatory.
Preview acceptance does not automatically complete namespace governance or hardening.

Database-first ownership resolves the overlap: generic temporal/FTS/catalog/commit
implementation is done once in the database; the consumer contract remains in the
agent roadmap. No prompt storage, automatic contradiction winner, unscoped user
write, implicit distributed transaction or mandatory MCP server is introduced.

## Next implementation boundary

GX-CAP-1 must prove the physical CommitId mapping, metadata admission/canonical bytes,
legacy-store boundary and crash protocol before exposing a typed API.
GX-CAP-2 lifecycle work may begin from its generic contracts, but promotion receipts
depend on commit identity. Shared format/commit changes remain serialized.
Current Grafx 0.0.4 performance work is tracked independently and remains open.
