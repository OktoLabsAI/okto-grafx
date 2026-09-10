# ADR GX-005 — Native full-text and explainable hybrid retrieval

Status: accepted semantic contract; GX-CAP-5 v1 implemented in development with
local validation; bounded GX-CAP-6 v1 is implemented in development. See [hybrid contract](../HYBRID_SEARCH.md), [FTS contract](../FULL_TEXT_SEARCH.md)
and [wire format](../specs/FULLTEXT_V1_FORMAT.md) for actual supported semantics and limits.
Source: complementary plan §§9–10 in full; agent-first §12.

## Decision

FTS is a native transactional access path over declared STRING node properties,
not a parallel authoritative document store. V1 uses persisted analyzer identity
and deterministic BM25, supports multiple weighted fields, structured filters,
updates/deletes/tombstones and generation-aware rebuild. Relationship-property
indexes remain a later scope, not an implicit claim of support.

Built-in keyword, standard, code_identifier and whitespace analyzers have explicit
version, normalization/case policy, locale (or und), token-length bounds, and
supported stopword/stemming profiles. Code identifiers retain whole tokens and
declared component splitting. Incompatible analyzer changes require rebuild;
host locale or an installed optional extension cannot silently reinterpret tokens.

Freshness/visibility follows the reader snapshot. A stale subset is never complete
evidence; a proved covering superset may revalidate candidates against the heap.
Build publication is single-flight and complete. Readers retain a valid generation
or a declared correct fallback while rebuilding, without relaxing writer concurrency.
Exact physical postings/stats/WAL codecs are a GX-CAP-5 prerequisite, not frozen here.

Hybrid v1 fuses bounded lexical/vector/graph candidate sets with RRF/explicit weighted
RRF, union/intersection and stable-identity tie breaks. It uses one declared reader
snapshot. Exact/approximate vector regime and all source budgets are visible.
Graph expansion has explicit seeds, relationship allowlist, direction, hop/work
bounds and cycle/multiplicity semantics. No implicit exponential traversal.
Generation of embeddings, model credentials and probabilistic reranking stay outside
the core. Native search remains useful without an LLM or agent package.

## Honest results and budgets

Hits disclose source ranks/scores, fusion, filters, graph distance/explanation bound,
index coverage and snapshot. Query tokens, postings visits, candidate rows, explanation
bytes, time/cancellation and memory are bounded. Partial results are opt-in and marked;
missing FTS or unavailable HNSW is an explicit policy choice between a correct bounded
fallback and typed refusal. No fallback may silently remove authorization filters.
A successful empty result and an unavailable source are distinct outcomes.

## Required evidence

Fixed technical-term/Unicode/code corpus; deterministic rank/ties; exact lookup with
no embedding provider; filtered candidate non-leakage; concurrent update/delete and
rebuild; independent heap/posting corruption; recovery at every publication cut;
n-1 capability refusal; bounded cancellation; cold/warm latency, recall/precision,
write amplification, index size and peak RSS. Analyzer and fusion choices are
versioned contracts; changing them is not merely a performance optimization.
