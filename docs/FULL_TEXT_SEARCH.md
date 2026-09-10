# Native full-text search

[Documentation index](README.md) · [API](API_REFERENCE.md) · [Indexes](INDEXES_AND_VECTORS.md)

FTS-v1 is available in the **0.0.5 development source**. It is a persisted inverted
access path over one to four declared STRING properties of a node or relationship table, not a
second document database or an embedding service. Index creation is explicit and
activates a required capability: older builds without FTS support must refuse the
store. Back up or export before introducing it into a mixed-version deployment.

## Start with the typed API

### Exact analyzed phrases (0.0.6 development)

Use `db.search_text(index="document_text", query="graph database", phrase=True)`
to require the entire analyzed query, in order and contiguously, within at least
one declared field. Repeated tokens matter: `graph graph` is not the same phrase
as `graph`. Phrases never cross field boundaries. Token positions follow the
index's fixed analyzer/normalization/case rules, not byte offsets or literal
substring matching. For `code_identifier`, positions follow the analyzer's
emitted whole-token/component sequence; they are not source-code character offsets.

Without `TextIndexOptions.positions=True`, the result regime is `phrase_verified`: existing whole-term postings nominate
candidates and their **same-snapshot heap tokens** verify contiguous positions.
That default mode introduces no positional-posting format, capability bit, index rebuild,
persisted position list, term-offset response or extra positional write amplification. This
is exact phrase semantics, not a claim of a durable positional index. Verification
is linear in candidate field tokens, with O(query tokens) KMP state. It does not
add a table scan beyond the existing documented corpus-statistics fallback.

BM25 scoring and `matched_fields` include only fields containing the full phrase;
document frequencies/corpus totals still use the complete snapshot. `matched_terms`
remains the distinct analyzed terms. `candidates` counts whole-term candidates
examined, including those rejected by phrase verification. Ranking/ties and `k`
are applied **after** verification, never by filtering a truncated term top-k.

`phrase` defaults to `False` and accepts only exact booleans. `prefix=True` plus
`phrase=True` refuses with `GrafxUnsupportedOperation`. The non-positional path requires
`slop=0`; ordered slop for durable positions is documented below. No quote-based
query grammar or implicit phrase mode is added. Empty analyzed queries
return no hits. Existing token/posting/candidate/memory/explanation bounds, filters,
deadlines and cancellation apply; repeated query tokens count toward token bounds.
Phrase state reserves additional logical memory before allocation. Updates,
deletes, read-only reopen and rebuild retain the same native snapshot/certificate
rules. Node and relationship text fields are supported.

The closed `CALL grafx.search_text`, CLI and hybrid entry points continue their
documented whole-term behavior; this slice adds the typed Python `phrase` keyword.
Changing default term search or adding phrase options to those protocols is not
implied. This is an operation option, not `TextIndexOptions` or a connection flag.

### Prefix search and relationship properties

Opt into prefix postings when creating an index with
`TextIndexOptions(prefix_max_characters=8)`, then call
`db.search_text(index="text", query="graph", prefix=True)`. The default is zero
(disabled); allowed values are exact integers 0..32. Query analysis uses the
index's frozen analyzer. Each analyzed query token expands into actual indexed
terms beginning with that token. No wildcard, substring or phrase query syntax is added;
the closed `CALL grafx.search_text` and hybrid API continue to use whole terms.

`TextSearchLimits(max_expanded_terms=128)` limits distinct expansion terms;
all limits are exact positive bounded integers. Too many terms raise
`GrafxQueryBudgetExceeded`, without truncating results. A prefix longer than the
configured character cap, or prefix search against a non-prefix index, raises
`GrafxUnsupportedOperation`. Existing postings/document/memory/time/cancellation
bounds still apply. Empty analyzed queries return no hits. Filters are applied in
the same snapshot; they do not hide corpus statistics.

Each document stores distinct prefix postings in addition to whole terms, at most
65,536 distinct prefixes per document. Exceeding that hard bound is a typed budget
refusal. This can materially increase write amplification, WAL and storage: use it
for interactive term completion/search only when needed; prefer whole-term indexes
for long documents or write-heavy corpora. Expansion reads selected hash buckets,
not all corpus terms. Corpus statistics can still require the documented census
fallback. BM25 is exactly whole-term scoring over the expanded term set, not a new
approximate ranking rule; `regime` is `prefix_index` and hits report actual terms.

Full-text can also index declared STRING properties of relationship tables:
`db.create_text_index("edge_text", "MENTIONS", ("description",))`. `_from`/`_to`
are structural integer endpoints, never text fields. Hits identify physical
relationship records, preserving parallel edges and self-loops; `matched_fields`
contains property names. NULL fields contribute no tokens. Weights, prefix mode,
durable/history totals, updates/deletes/rebuild and snapshot filters work as for
nodes. The closed search procedure returns scalar hit rows, not fabricated node
objects. Native hybrid vector targets remain node-only; relationship FTS does not
turn a relationship into an embedding target.

Prefix mode activates required capability bit 11 (`fulltext_prefixes_v1`) and
`fulltext_v4_` derivation metadata; relationship indexes activate bit 12
(`fulltext_relationships_v1`). Both require base FTS support. Current backup,
logical transfer, native replay and verification preserve these features. Older
builds lacking either required bit refuse the store; there is no in-place downgrade
or automatic clearing of bits after dropping an index. Default node whole-term
index bytes remain unchanged. See [prefix format](specs/FTS_PREFIX_V1.md) and
[relationship format](specs/FTS_RELATIONSHIPS_V1.md).

BM25 query-term frequencies are computed in one pass per candidate field. Ranking
keeps the original term/field accumulation order and exact scores/tie ordering;
only requested terms are retained. Temporary counting tables reserve
`128 + fields * (64 + 64 * query_terms)` logical bytes before construction and are
released per candidate. This changes neither analyzer/index bytes nor corpus
document frequency, and adds no configuration keyword. Hybrid retrieval also
accounts this work against its aggregate operation envelope.

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
on the same table. With `options=None`, all fields receive weight 1.0. A custom
options value must provide exactly one weight per field. Hash sizing uses the same
validated bucket-count contract as ordinary hash indexes; no implicit resize occurs.

`search_text(reader=None, *, index, query, k=20, filter=None, limits=None, k1=1.2,
b=0.75, timeout_seconds=None, cancellation=None, prefix=False, phrase=False,
return_positions=False, slop=0)` returns `TextSearchResult`.
Omitting `reader` opens/closes a fresh read transaction. A supplied reader must be
active and belong to this database; it remains caller-owned after an error. Write
transactions are refused: use a new reader after commit to observe indexed changes.

Filters accept an immutable `RecordIdFilter`, not arbitrary callbacks under engine
locks. Derive allowed IDs from your own graph/property policy using **the same
reader snapshot**, then pass that set. Filtering happens before top-k, never as a
post-filter of an already truncated result. IDs refer to the indexed table, not
automatically to application primary keys. Corpus statistics use all indexed
documents, not only permitted hits; scores/count metadata are not a secrecy boundary.

## Durable corpus totals (opt-in)

Declare `TextIndexOptions(statistics_mode="durable", field_weights=(1.0,))`
when creating a new text index. The `fulltext_statistics_v1` required capability
(catalog bit 6) and `fulltext_v2_` derivation prevent older readers/writers from
opening it. Existing `wal` indexes are unchanged; rebuilding one preserves its
declaration rather than upgrading it. Use a separately named index, or the explicit
atomic replacement API below, to change modes.

`TextSearchResult.statistics_regime="durable_summary"` means the page-0 corpus
count and field-length totals cover this reader's table high-water and do not
exceed its snapshot. It eliminates the cold full-corpus statistics walk at eligible
cuts, not query-term posting reads or BM25 candidate scoring. Same-snapshot caches
may still report `snapshot_cache`; older snapshots retain exact WAL/census paths.
Procedure statistics add numeric `statistics_from_durable_summary` (0/1).

Complete COMMIT effects advance the summary once; INSERT/TOMBSTONE use existing
length postings, without retokenizing or extra logical WAL records. Rollback cannot
advance it. Native recovery replays only proved complete commits, idempotently.
Private builds accumulate totals from their fenced canonical entries before
verification/publication. Full verification independently compares corpus totals.
Malformed/future/wrong-identity metadata refuses; no guessed zero or silent repair.

Use durable mode for repeated cold/reopened searches over large corpora when every
participant supports the capability. Prefer the default `wal` mode for mixed older
builds or when its warm cache already suffices: durable mode adds summary updates
to commits and a compatibility commitment, with no universal latency improvement.
Physical backup preserves it; logical export/import rebuilds it from its declaration.
It is not a history store, an authorization filter or an upgrade that can be undone
by removing a capability bit. See [format/recovery contract](specs/FTS_DURABLE_STATISTICS.md).

### Bounded historical corpus totals

For long-lived readers concurrent with writers, explicitly declare
`TextIndexOptions(statistics_mode="durable", statistics_history_entries=8)`.
The integer range is 0..32; default 0 preserves the original scalar-only bytes.
A positive capacity retains up to that many older reductions **plus the current
one**, in a fixed-size page-0 slot. It requires `fulltext_statistics_history_v1`
(catalog bit 8) and the `fulltext_v3_` derivation. Every participant must support it.
The declaration is immutable for an index generation; rebuild preserves it.

Creation refuses if the record cannot fit the database page size. Required page
bytes are `140 + 64*(capacity+1)` (32-byte page header, three 4-byte slots,
28-byte file header, 52-byte index header and 16-byte history envelope). The check uses
the native `PAGE_HEADER_SIZE`, `SLOT_ENTRY_SIZE`, `FILE_HEADER_SIZE` and
`INDEX_HEADER_SIZE`, plus the 16-byte history envelope and 64-byte scalar records.
No index or capability is published on this refusal.

Consecutive complete reductions prove the interval from an older marker through
the commit before the next marker. Reads inside that retained window can report
`durable_summary`, including gaps with unrelated commits. Older/otherwise
unproved snapshots still use exact WAL/census; no approximate corpus totals or
changed BM25 scores. Same-snapshot cache hits still report `snapshot_cache`.

Benefits: bounded avoidance of historical full-corpus statistics scans without
pinning new row history. Costs: larger page-0 records/copies on affected commits
and bounded extra verification work. Prefer zero for short-lived readers or when
warm caches suffice. This is not arbitrary time travel or history retention:
evicted summaries are not recoverable through this API, and the heap's reclaimed
snapshot floor still applies. No RSS or whole-query O(1) promise is implied.

Recovery appends only complete proved COMMIT reductions and is idempotent.
Verification reads the device and checks retained historical totals against
canonical heap visibility; summaries below the reclaim floor receive structural
validation only. Physical backup preserves the series. Logical export preserves
the capacity, but import/rebuild computes destination-generation statistics rather
than copying source historical markers. Malformed/missing/future/nonmonotonic
metadata refuses; it is not silently converted to an empty history or fallback.

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
| `positions` | `False` | Exact boolean; opt-in persisted positional chunks and bit 16. Extra write/storage/query evidence cost; no implicit conversion. |
| `prefix_max_characters` | `0` | Exact integer 0..32; positive values materialize distinct prefixes, activate bit 11 (or the encompassing positional bit 16) and increase write/storage cost. Zero disables prefix search. |
| `statistics_mode` | `wal` | `wal` retains legacy index bytes and bounded memo/WAL/census statistics; `durable` explicitly activates a required format capability and persists corpus totals. See below. |
| `statistics_history_entries` | `0` | Integer 0..32; positive values require `durable` and reserve this many historical summaries plus the current one. Page-size fit checked before creation; additional required capability bit 8. |
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
| `max_expanded_terms` | 128 | Distinct actual terms expanded from query prefixes; overflow refuses without partial results. |
| `max_postings` | 100,000 | Entries visited across statistics/term buckets and certificate retries, not just matches. |
| `max_candidates` | 10,000 | Distinct retained filter-admitted candidate documents before ranking. |
| `max_explanation_bytes` | 65,536 | Total UTF-8 field/term text in returned hit explanations. Refuses instead of silently truncating. |
| `max_position_results` | 100,000 | Aggregate returned token ordinals; positive exact integer up to 2^31. |
| `max_proximity_work` | 1,000,000 | Ordered matching work across retries; positive exact integer up to 2^31. |
| `max_memory_bytes` | 33,554,432 | Logical retention: 64 bytes per admitted filter/statistics/DF identity, 128 per retained candidate plus 64 + UTF-8 bytes per retained analyzed term. Not RSS, heap decode buffers or allocator overhead. |
| `max_statistics_wal_records` | 4,096 | Maximum records in the optional complete committed interval; exceeding declines to the census. |
| `max_statistics_wal_bytes` | 8,388,608 | Physical read budget for the optional WAL interval, including foreign-tail refresh and sparse-mark read amplification. Separate from max_memory_bytes; decoded records and payloads are additional bounded retention. |

All fields require integers in 1..2³¹; `k` is separately limited to 1..10,000.
Cancellation/deadlines are cooperative between units of work; neither preempts one
storage call nor interrupts a durable write/commit. The initial typed autocommit reader
admission is not a preemptive timeout guarantee.

With default `statistics_mode="wal"`, the first statistics read for a snapshot/generation visits the index and validates
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
aggregate pages; a newly opened handle starts cold in that default mode. Opt-in
`durable` mode uses the native scalar page when its coverage is eligible, including
after reopen; old snapshots can still require the exact census described above.

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

This delivery does not implement original-character highlighting/returned character offsets,
language stemming/stopwords, arbitrary CALL extensions, online
generation deletion or automatic schema/format downgrade. Those remain explicit
[roadmap](../ROADMAP.md) limitations rather than undocumented implied capabilities.

[Hybrid search](HYBRID_SEARCH.md) composes this native source with vectors on one
reader; it does not change BM25 or require an embedding provider inside Grafx.

## Durable positional postings (0.0.6 development)

```python
from okto_grafx import connect, TextIndexOptions

with connect(":memory:") as positional_db:
    with positional_db.begin() as tx:
        tx.execute("CREATE NODE TABLE Document(id INT64, body STRING, PRIMARY KEY(id))")
        tx.execute("CREATE (:Document {id:1,body:'graph database'})")
    positional_db.create_text_index("body_positions", "Document", ("body",),
        options=TextIndexOptions(positions=True, statistics_mode="durable"))
    hits = positional_db.search_text(index="body_positions", query="graph database", phrase=True)
    assert hits.regime == "phrase_positions" and len(hits.hits) == 1
```

`TextIndexOptions.positions` is an opt-in persisted boolean, default `False`.
It adds canonical field/term/position chunks of up to 32 occurrences, routed to
the same bucket as that term's ordinary posting. Field-relative token positions
are zero-based after the declared analyzer, not original character offsets.
Repeated terms, token order and field boundaries are exact; fields never join
into one phrase. Phrase/prefix combination remains unsupported; ordered slop is
available explicitly as described below.

Search intersects persisted relative positions, while validating every chunk's
coverage against the native visible heap row under the existing pre/post index
certificate. Missing/duplicate/foreign/incorrect chunks refuse rather than produce
partial hits. This does **not** remove candidate heap validation or all text
analysis; do not infer a measured query speedup. The ordinary non-positional
`phrase_verified` path remains available and avoids additional write/storage cost.

Creation, updates, deletes, rebuild, WAL replay, backup and logical transfer use
the native generation/index protocols. Existing indexes do not silently change.
Extra postings count against transaction limits and `max_postings`; positional
query evidence counts against `max_memory_bytes`. Common/repeated long documents
may increase write amplification significantly. Use the opt-in when persistent
phrase positions are needed and benchmark the actual corpus; keep it off for
term-only workloads with tight write/space budgets.

The `fulltext_v5_` derivation preserves analyzer/statistics/prefix configuration
and requires catalog bit **16**, `fulltext_positions_v1`, dependent on base FTS.
Older builds refuse before using incompatible routing. No in-place downgrade or
automatic conversion is provided. See [format](specs/FTS_POSITIONAL_POSTINGS.md)
and [wheel compatibility](V006_COMPATIBILITY.md).

## Position results and ordered proximity

```python
from okto_grafx import connect, TextIndexOptions

with connect(":memory:") as proximity_db:
    with proximity_db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Document(id INT64, body STRING, PRIMARY KEY(id))")
        tx.execute("CREATE (:Document {id:1,body:'graph native database'})")
    proximity_db.create_text_index("body_positions", "Document", ("body",),
        options=TextIndexOptions(positions=True))
    result = proximity_db.search_text(index="body_positions", query="graph database",
        phrase=True, slop=1, return_positions=True)
    assert len(result.hits) == 1
    assert {item.term: item.positions for item in result.hits[0].positions} == {
        "graph": (0,), "database": (2,)}
```

`return_positions=True` opts into output (default `False`) and requires persisted
`positions=True`.
`TextHit.positions` defaults to `()` and contains frozen
`TextMatchPositions(field, term, positions)` values: zero-based analyzed token
ordinals for each matched term in matched fields, ordered by term then index field.
These are **all occurrences of those terms in the matched fields**, not only the
spans participating in one phrase. They are not original Unicode character offsets
and must not be used to slice original strings without a consumer mapping.
`max_position_results=100_000` bounds the aggregate returned ordinals across top-k;
memory and explanation budgets also include this output. No partial output on refusal.

`slop=0` retains exact phrase semantics. Integers 1–65,535 require `phrase=True`
and durable positional postings. Terms must occur in query order with distinct,
strictly increasing ordinals in one field; the sum of intervening extra tokens is
at most slop. Repeated terms need separate occurrences. `a x b y c` matches
`a b c` at slop 2, not 1; `b a` does not match `a b` at any slop.
The result regime is `proximity_positions`. `max_proximity_work=1_000_000`
charges start candidates and binary successor searches, including certificate
retries; query deadline/cancellation remain in force. Both new positive limits
accept exact integers up to 2^31, not bools. No phrase/prefix combination is added.

## Atomic analyzer replacement

```python
from okto_grafx import connect, TextIndexOptions

with connect(":memory:") as replacement_db:
    with replacement_db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Document(id INT64, body STRING, PRIMARY KEY(id))")
        tx.execute("CREATE (:Document {id:1,body:'graph database'})")
    replacement_db.create_text_index("docs_text", "Document", ("body",))
    replacement_db.replace_text_index("docs_text", options=TextIndexOptions(
        analyzer="keyword", field_weights=(1.0,), positions=True))
    result = replacement_db.search_text(index="docs_text", query="graph database")
    assert len(result.hits) == 1
    assert not replacement_db.search_text(index="docs_text", query="graph").hits
```

Supply complete options, including one field weight per existing field. This
retains name, table, columns and physical bucket sizing, but builds a fresh full
generation with the requested analyzer/statistics/prefix/position configuration.
The catalog COMMIT is the only switch; failure before publication keeps the old
definition. Old files are not modified, relabeled or immediately deleted. They
become retained orphan artifacts eligible for the existing explicit cleanup rules.
Retired generations with the old analyzer are deliberately not described using
the new analyzer. Ordinary `rebuild_index` still retains the analyzer unchanged.

Each newly issued search uses the **currently published analyzer**, even inside
an older data snapshot. Row visibility still belongs to that snapshot. An
in-flight search must validate its selected generation before returning; a
publication race cannot mix old postings with a new analyzer. This is not a
historical analyzer-selection API or an online background rebuild. The foreground
build fences scanned data, uses native OCC/quotas, and may conflict with writers.
Use replacement when changing search semantics; keep the existing generation if
only queries/limits change. Recovery, backup and unsupported-capability refusal
follow the native detached-generation protocol.
