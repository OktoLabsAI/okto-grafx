# Native full-text search

[Documentation index](README.md) · [API](API_REFERENCE.md) · [Indexes](INDEXES_AND_VECTORS.md)

FTS-v1 is available in the **0.0.5 development source**. It is a persisted inverted
access path over one to four declared STRING properties of a node table, not a
second document database or an embedding service. Index creation is explicit and
activates a required capability: older builds without FTS support must refuse the
store. Back up or export before introducing it into a mixed-version deployment.

## Start with the typed API

```python
from okto_grafx import connect, TextIndexOptions, TextSearchLimits
from okto_grafx.domain.vector.filter import RecordIdFilter

with connect(":memory:") as db:
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Document(id INT64, title STRING, body STRING, PRIMARY KEY(id))")
        tx.execute("CREATE (:Document {id:1, title:'Graph recovery', body:'WAL and durable commits'})")
        tx.execute("CREATE (:Document {id:2, title:'Other', body:'Unrelated topic'})")
    db.create_text_index(
        "document_text", "Document", ("title", "body"),
        options=TextIndexOptions(analyzer="standard", field_weights=(2.0, 1.0)),
        bucket_count=64,
    )
    with db.begin("read") as reader:
        found = db.search_text(reader, index="document_text", query="recovery WAL", k=10)
        assert len(found.hits) == 1
        hit = found.hits[0]
        assert hit.matched_fields == ("body", "title")
        allowed = RecordIdFilter.of([hit.record_id])
        filtered = db.search_text(reader, index="document_text", query="WAL", filter=allowed)
        assert filtered.hits[0].record_id == hit.record_id
    db.rebuild_index("document_text")
    assert not db.verify("all").findings
```

`create_text_index(name, table, columns, *, options=None, bucket_count=64)` owns a
dedicated write transaction. `columns` is a tuple; each named column must be STRING
on the same node table. With `options=None`, all fields receive weight 1.0. A custom
options value must provide exactly one weight per field. Hash sizing uses the same
validated bucket-count contract as ordinary hash indexes; no implicit resize occurs.

`search_text(reader=None, *, index, query, k=20, filter=None, limits=None, k1=1.2,
b=0.75, timeout_seconds=None, cancellation=None)` returns `TextSearchResult`.
Omitting `reader` opens/closes a fresh read transaction. A supplied reader must be
active and belong to this database; it remains caller-owned after an error. Write
transactions are refused: use a new reader after commit to observe indexed changes.

Filters accept an immutable `RecordIdFilter`, not arbitrary callbacks under engine
locks. Derive allowed IDs from your own graph/property policy using **the same
reader snapshot**, then pass that set. Filtering happens before top-k, never as a
post-filter of an already truncated result. IDs refer to the indexed table, not
automatically to application primary keys. Corpus statistics use all indexed
documents, not only permitted hits; scores/count metadata are not a secrecy boundary.

## Procedure query

Materialized `Database.execute` and read `Transaction.execute` also accept:

```cypher
CALL grafx.search_text($index, $query, $k)
CALL grafx.search_text($index, $query, $k, $allowed_record_ids)
```

Index/query may be string literals, k an integer literal; arguments may instead be
parameters. The fourth argument is a list/tuple parameter. An optional final
semicolon is supported. Output columns are `record_id`, `score`, `matched_fields`,
`matched_terms`, `regime`, `index_built_through_commit`, `snapshot_commit`.
`QueryResult.statistics` retains coverage/counts even for an empty result.

This is a closed read-only procedure, **not** general CALL/YIELD/RETURN composition,
an extension registry or a cursor/explain plan. Trailing clauses and unsupported
procedures refuse. It uses default search limits and BM25 parameters; use the typed
API for custom limits/scoring. Statement cancellation/deadline is shared with the
procedure, not restarted inside it. FTS creation uses the typed API; proposed
`CREATE FULLTEXT INDEX ... WITH ...` syntax is not implemented.

## Analyzer and index options

`TextIndexOptions` is immutable, validated and persisted in the index identity:

| Field | Default | Allowed values and meaning |
| --- | --- | --- |
| `analyzer` | `standard` | `standard`, `keyword`, `code_identifier`, `whitespace`; see below. |
| `analyzer_version` | `1` | Only 1. Unknown versions refuse, never silently map to latest. |
| `normalization` | `NFC` | `none`, `NFC`, `NFKC` using the frozen Unicode 3.2 database. NFKC folds compatibility forms and may conflate distinctions; use none/NFC for identifiers where those distinctions matter. |
| `case_folding` | `unicode` | `none`, `ascii`, `unicode`. Unicode uses a checked-in full case-fold table from Unicode 15.1, not the host Python table. Case folding can expand terms (e.g. ß → ss); choose none for case-sensitive identity. |
| `locale` | `und` | Only und (locale-independent). No host locale or Turkish-specific folding. |
| `stopwords` | `none` | Only none; exact technical words are not silently removed. |
| `stemming` | `none` | Only none; no language stemmer or optional-package behavior. |
| `max_token_bytes` | `64` | 1–128 UTF-8 bytes per complete normalized term. Overlong terms refuse, not truncate. Raising increases key/storage cost; keyword fields may need a larger bound. |
| `max_document_characters` | `65,536` | 1–16,777,216; aggregate source length across fields, plus each field's normalized length before splitting. Folded terms are separately bounded by max_token_bytes. |
| `max_document_tokens` | `4,096` | 1–65,536 emitted tokens across fields, including identifier components and repetition. Bounds write/rebuild/analyzer work. |
| `field_weights` | `(1.0,)` | One positive finite weight ≤1,000 per field, up to four fields. Weights multiply per-field BM25 scores. |

| Analyzer | Semantics / when to use |
| --- | --- |
| standard | Text terms separated by fixed whitespace/ASCII punctuation and Unicode-3.2 punctuation, separator and control/unassigned categories. Preserves accents; useful for natural-language/technical prose. Recently assigned Unicode characters outside that frozen category database are not promised to tokenize as in a current-language NLP library. |
| keyword | Entire field, after trimming declared boundary whitespace, is one term. Use for short exact names/IDs; not whole paragraphs. Normalization/folding still apply as configured. |
| whitespace | Split only the explicitly enumerated Unicode whitespace set; punctuation remains within terms. Useful for diagnosing token spelling. |
| code_identifier | Retain each complete whitespace-delimited token plus snake_case, camelCase, acronym, dot/path/scope and digit components. Useful for symbols/qualified names; expansion consumes the token budget. |

Normalization occurs before splitting and again after case folding. No embedding,
model provider, stopword download or plugin is needed. To change analyzer settings,
create a new named index with the desired options (a complete rebuild), verify it,
then switch the consumer's index name. `rebuild_index(name)` and `rehash_index(name)`
preserve the existing analyzer identity; in-place analyzer/version replacement under
one name is not supported. Old generations remain governed by ordinary index cleanup
rules; rebuilding does not authorize deleting them.

## Ranking, results and snapshots

V1 queries mean **OR over distinct analyzed query terms**. They do not implement
Boolean operators, prefix wildcards or phrase/position search; punctuation is analyzer
input, not a separate query language. Use keyword for an entire exact identifier.
Repeated query terms do not multiply their weight, but still consume query-token work.

For each matched term and field, the score contributes:

`weight × log(1 + (N - df + 0.5)/(df + 0.5)) × tf × (k1 + 1) / (tf + k1 × (1 - b + b × field_length/average_field_length))`

`df` counts documents containing the term in any indexed field. N includes all
snapshot-visible documents, even null/empty fields. Empty average lengths use 1.
`0 < k1 <= 100`, `0 <= b <= 1`; lower b reduces length normalization. Larger field
weights favor that field. Scores sum contributions and ties sort by ascending
RecordId. Exact score reproducibility is within the supported floating-math regime,
not a promise of identical libm rounding on every architecture.

`TextHit` exposes `record_id`, `score`, sorted `matched_fields`, sorted `matched_terms`.
The result exposes `hits`, `regime='exact_index'`, `index_built_through_commit`,
`snapshot_commit`, `postings_visited`, `candidates`, `corpus_documents`,
`statistics_regime` and `statistics_wal_records`. Statistics regimes are
`full_census`, `snapshot_cache` and `wal_delta`; all produce the same snapshot BM25
semantics. The procedure reports numeric `statistics_wal_records`,
`statistics_from_wal_delta` and `statistics_from_snapshot_cache` flags. Build coverage
is a table-relative index certificate and can be lower than the global snapshot when
unrelated tables committed later. A success is complete within the specified top-k;
empty results and unavailable/stale indexes are not conflated.
`statistics_wal_records` counts the accepted delta, not every physical read made by
a declined proof or the storage subsystem. It is zero for a census/cache result.

Each version receives distinct-term postings and a field-length statistics entry.
Inserts, updates and tombstones use the same transaction and WAL as the heap. Exact
postings are candidates revalidated against the heap and reader snapshot. A pre/post
index certificate brackets statistics, candidates and ranking. A changed generation
retries only within the existing bounded exact-read protocol. Stale or incomplete
generations refuse instead of returning a short answer. No implicit heap-scan fallback
is advertised as indexed search. Foreground rebuild uses complete nonced generations,
ordinary writer/OCC fences and atomic catalog publication, retaining the old generation.

## Budgets and performance boundaries

`TextSearchLimits` is per search, separate from connection-wide query-spill options:

| Field | Default | Bound |
| --- | --- | --- |
| `max_query_tokens` | 64 | Emitted query tokens, before de-duplication. |
| `max_postings` | 100,000 | Entries visited across statistics/term buckets and certificate retries, not just matches. |
| `max_candidates` | 10,000 | Distinct retained filter-admitted candidate documents before ranking. |
| `max_explanation_bytes` | 65,536 | Total UTF-8 field/term text in returned hit explanations. Refuses instead of silently truncating. |
| `max_memory_bytes` | 33,554,432 | Logical retention: 64 bytes per admitted filter/statistics/DF identity, 128 per retained candidate plus 64 + UTF-8 bytes per retained analyzed term. Not RSS, heap decode buffers or allocator overhead. |
| `max_statistics_wal_records` | 4,096 | Maximum records in the optional complete committed interval; exceeding declines to the census. |
| `max_statistics_wal_bytes` | 8,388,608 | Physical read budget for the optional WAL interval, including foreign-tail refresh and sparse-mark read amplification. Separate from max_memory_bytes; decoded records and payloads are additional bounded retention. |

All fields require integers in 1..2³¹; `k` is separately limited to 1..10,000.
Cancellation/deadlines are cooperative between units of work; neither preempts one
storage call nor interrupts a durable write/commit. The initial typed autocommit reader
admission is not a preemptive timeout guarantee.

The first statistics read for a snapshot/generation visits the index and validates
document lengths against the heap: **O(stored postings + document text)**, including
retained old versions. Up to eight certificate/snapshot-keyed scalar statistics
summaries per handle are cached; warm searches navigate term buckets and validate
candidate documents. After ordinary writes, the newest same-generation prior snapshot
summary can be advanced using the **complete committed native WAL interval**. Insert
and tombstone length entries update N and field totals; ABORT/incomplete effects are
never counted. No old physical row address becomes current authority. Pre/post index
certificates and candidate heap/snapshot validation remain mandatory. Recycled or
oversized intervals, new generations, catalog/direct-index writes, versioned entries,
unknown record semantics and ambiguous coverage decline to a full census. WAL
corruption is not silently swallowed. This avoids a corpus recensus on eligible DML,
but is not graph-size-independent search: term buckets, candidates, WAL delta and
occasional cold censuses still cost work. Summaries are process-local, not new durable
aggregate pages; a newly opened handle starts cold.

Repeated analysis uses a pure operation-owned memo keyed by exact STRING/None leaves
and the complete persisted analyzer identity. Counting quotas and staging keys reuse
tokens in a write transaction; commit/abort release the memo. Retention is capped at
min(1 MiB, max_transaction_bytes/8), additional to normal transaction staging charges.
Verification uses at most 1 MiB; each search uses min(1 MiB, max_memory_bytes/4), counted
inside its logical memory budget. The tariff includes source/token characters at four
bytes each plus object allowances. On cache saturation analysis proceeds uncached;
no document limit, quota, row visibility, OCC or WAL check is omitted. This is derived
text only, not a row, page, permission or durability-authority cache.
Index size/write amplification grows with distinct terms; write cost is bounded by
document and ordinary transaction quotas. Tune against your corpus, not only top-k.

Invalid settings use configuration/index errors; exhausted analyzer/search work uses
`GrafxQueryBudgetExceeded` (also possible when committing an indexed write).
Cancellation/deadline have their existing specific error codes. Corrupt postings/heap
refuse with corruption/index errors; `verify('all')` checks entry semantics and missing
coverage. Recovery can replay only proven committed effects. Checksums do not authorize
automatic repair of unproved corruption.

## Operations and compatibility

[Logical transfer](LOGICAL_TRANSFER.md) preserves analyzer declarations and rebuilds
fresh postings. [Physical backup](BACKUP_RESTORE.md) preserves index/catalog bytes and
UUID under its offline-restore contract. Both include verification. Required capability
and wire layout are specified in [FTS-v1 format](specs/FULLTEXT_V1_FORMAT.md).

This delivery does not implement relationship-property FTS, highlighting positions,
language stemming/stopwords, arbitrary CALL extensions, online
generation deletion or automatic schema/format downgrade. Those remain explicit
[roadmap](../ROADMAP.md) limitations rather than undocumented implied capabilities.

[Hybrid search](HYBRID_SEARCH.md) composes this native source with vectors on one
reader; it does not change BM25 or require an embedding provider inside Grafx.
