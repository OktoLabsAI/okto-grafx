# Complementary capability specification index

These specifications define acceptance, not an independent implementation queue.
The single [roadmap](../../ROADMAP.md) owns priority/current status. Full former
requirements remain in the [source archive](../archive/ROADMAP_SOURCES.md).

| Milestone | Specification | Implementation status |
|---|---|---|
| GX-CAP-0 | [Boundaries/contracts](SPEC-GX-CAP-0.md) | Enforcement and contract checkpoint |
| GX-CAP-1 | [Commit identity and provenance](SPEC-GX-CAP-1.md) | Bounded N4 implemented: opt-in native publication, metadata, snapshot history, verification and transfer hooks; [acceptance and limits](../reports/V005_N3_N4_ACCEPTANCE.md) |
| GX-CAP-2 | [Catalog sessions and workspace scopes](SPEC-GX-CAP-2.md) | Not certified; contract routing only |
| GX-CAP-3 | [Temporal system time](SPEC-GX-CAP-3.md) | Not certified; contract routing only |
| GX-CAP-4 | [Valid time, bitemporal queries and graph diff](SPEC-GX-CAP-4.md) | Not certified; contract routing only |
| GX-CAP-5 | [Native full-text search](SPEC-GX-CAP-5.md) | FTS-v1 implemented; [consumer API and limits](../FULL_TEXT_SEARCH.md); [local acceptance](../reports/V005_OPS2_FTS_CHECKPOINT.md) |
| GX-CAP-6 | [Explainable hybrid retrieval](SPEC-GX-CAP-6.md) | Bounded typed v1 implemented in 0.0.5 development; [contract](../HYBRID_SEARCH.md) and [acceptance receipt](../reports/V005_SEARCH_RESUME_CHECKPOINT.md) |
| GX-CAP-7 | [Extension SPI and UDFs](SPEC-GX-CAP-7.md) | Partial: trusted typed scalar registry/UDFs implemented; broader SPI remains planned; [usage](../EXTENSIONS_AND_ARROW.md) |
| GX-CAP-8 | [Arrow and external data](SPEC-GX-CAP-8.md) | Partial: scalar/vector Arrow, Pandas/Polars, local Parquet/CSV/JSONL and detached graph export implemented; external query scans/COPY and graph ingestion remain; [receipt](../reports/V005_AFTER_A4DD85A.md) |
| GX-CAP-9 | [Graph projections and algorithms](SPEC-GX-CAP-9.md) | Partial: weighted graph, retained lookup/CSR/transitions/simple topology, degree/WCC/SCC, BFS/Dijkstra, PageRank, k-core and label propagation implemented; catalog/write-back/Louvain remain; [receipt](../reports/V005_AFTER_A4DD85A.md) |
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

## Dependency boundary

GX-CAP-1 must prove the physical CommitId mapping, metadata admission/canonical bytes,
legacy-store boundary and crash protocol before exposing a typed API.
GX-CAP-2 lifecycle work may begin from its generic contracts, but promotion receipts
depend on commit identity. Shared format/commit changes remain serialized.
Current status and finite performance residuals are tracked only in
[ROADMAP.md](../../ROADMAP.md); do not create a separate queue here.
