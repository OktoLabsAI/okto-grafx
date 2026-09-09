# Configuration reference

The continuation after `3f3819f` also adds `vacuum(index_free_pages=False)` and
explicit `create_index(layout="sparse_hash")`; these are operation options, not
global switches. `connect(extensions=None)` accepts a trusted immutable object
separately from `DatabaseConfig` and the port `registry`. Scalar descriptor budgets
and Arrow `batch_rows`/`max_batch_bytes` are documented in
[Extensions and Arrow](EXTENSIONS_AND_ARROW.md). The repeated-key page memo has
fixed per-index bounds (64 pages / 1 MiB logical), not an additional setting.

The eight-item continuation adds operation-local `search_vectors(timeout_seconds=,
cancellation=)` and `HybridSearchOptions.graph_access` (`auto`/`scan`). Hybrid
`max_memory_bytes` now applies to simultaneous logical source/fusion/graph tariffs,
with per-phase peak diagnostics; it is not an RSS cap. See [hybrid contracts](HYBRID_SEARCH.md)
and [vector controls](INDEXES_AND_VECTORS.md#cooperative-vector-read-control).

Other operation-local additions are `TextIndexOptions.statistics_mode="durable"`
(opt-in persisted compatibility fence), `index_distribution` with explicit page,
entry and logical-memory caps, `rehash_index_if_needed(check_skew=True)`,
`create_backup(capture_mode="disk")` (default temporary-storage streaming; `memory`
retains the RAM choice), and [application migrations](SCHEMA_MIGRATIONS.md) with
`namespace`, `dry_run=False`, `max_attempts=3`. Hash sizing now permits 65,536
explicit buckets / 4,194,304 expected entries; defaults remain unchanged. Large
eager directories consume disk even when empty. Backup still pauses commit
publication throughout source capture, including temporary-file I/O.

The connection settings below are distinct from operation-local options.
The next continuation adds `vector_hnsw_memory_budget_bytes=None` per connection
and persisted `TextIndexOptions.statistics_history_entries=0` (0..32, positive
only with durable mode). Both defaults preserve previous behavior. See
[HNSW memory](INDEXES_AND_VECTORS.md#hnsw-derived-picture-memory) and
[historical FTS](FULL_TEXT_SEARCH.md#bounded-historical-corpus-totals) for limits,
costs and when to use them.
[Full-text search](FULL_TEXT_SEARCH.md#analyzer-and-index-options) documents every
`TextIndexOptions` field (persisted), `TextSearchLimits` field (per search), BM25
parameters and cancellation/deadline semantics. [Logical transfer](LOGICAL_TRANSFER.md#limits-and-errors)
documents every `TransferLimits` field. Neither options class adds a `connect`
keyword; no existing connection default was changed for these features.
[Hybrid search](HYBRID_SEARCH.md#options) documents all `HybridSearchOptions`
fields, source windows, weights, graph controls and separate source budgets.
FTS statistics WAL limits bound an optional delta proof; exceeding them selects
the ordinary bounded census, not a partial result or weaker validation.

[Documentation index](README.md) · [Operations](OPERATIONS.md)



`connect(path, **options)` builds a `DatabaseConfig`. Every option is validated and copied to exact
built-in scalar values before any adapter or persisted descriptor sees it; an invalid option is
refused with the field name the caller actually wrote.

Use `from okto_grafx import DatabaseConfig, connect`. `DatabaseConfig` is immutable;
changing your own settings object does not reconfigure an open handle. Unless
noted otherwise, operational options are selected at open and apply to that handle.
`connect` also accepts `pathlib.Path`; `DatabaseConfig.path` itself is a string.
Unknown keywords and the removed `vector_recall_target` refuse with a typed error.

## Typed connection options (0.0.5)

`connect` exposes all configuration keywords through `Unpack[ConnectOptions]`.
Editors/type checkers supporting PEP 692 can suggest keyword names, reject typos
and check selector literals. For reusable dictionaries:

```python
from okto_grafx import ConnectOptions, connect

options: ConnectOptions = {
    "buffer_budget_bytes": 128 * 1024 * 1024,
    "descriptor_revalidation": "strict",
    "max_result_rows": 1000,
}
with connect("./graph", **options) as db:
    print(db.execute("RETURN 1").rows)
```

Every key is optional; omitted keys retain the defaults below. `ConnectOptions`
is a typing contract, not a validating constructor or another source of defaults.
For dynamic/untrusted JSON, validate it at your application boundary and retain
Grafx's runtime validation: annotations do not prove ranges, persisted identity or
provider availability. `registry=PortRegistry(...)` remains a separate keyword;
custom provider registration and runtime configuration errors are unchanged.

## Defaults and effect

| Option | Default | Notes |
|---|---|---|
| `path` | Required | Local database directory, or `":memory:"` for independent ephemeral storage |
| `page_size` | `8192` | Fixed for the life of the database |
| `partitions_per_table` | `64` | Persisted at creation; existing identity wins on reopen. Changes logical conflict granularity, not physical-page independence |
| `identity_lease_size` | `64` | Burn-only row-id range reserved durably per refill; larger values reduce heap page-0 metadata commits at the cost of wider harmless gaps after close/crash |
| `buffer_budget_bytes` | `64 MiB` | Per database, never shared; must hold at least two configured pages |
| `max_open_files` | `256` | Lazy per-database descriptor-cache ceiling; tune down for descriptor-constrained or multi-database hosts |
| `descriptor_revalidation` | `"strict"` | `"strict"` proves every cached descriptor hit; `"generation"` amortizes proofs for a closed canonical-file whitelist and requires an exclusively Grafx/Pulse-managed directory |
| `recovery_policy` | `"replay"` | `"replay"` permits safe replay/tail handling; `"refuse"` refuses damaged history. Neither permits unproved recovery; use `read_only` to prohibit repair |
| `lease_ttl_seconds` | `5.0` | How long a writer's lease stays valid without renewal |
| `lease_timeout_seconds` | `10.0` | How long to wait for another writer's lease |
| `commit_lock_timeout_seconds` | `30.0` | How long to wait at the commit section |
| `reader_stall_threshold_seconds` | `15.0` | Coordinator stall/liveness threshold; not permission to reclaim beneath an actual reader or proof of vacuum quiescence |
| `wal_segment_bytes` | `4 MiB` | Log segment target, from 256 B through the reader's 1 GiB ceiling; an exceptional batch that would cross the ceiling is refused before writing |
| `wal_max_bytes` | `None` | Optional soft high-water trigger: after a durable write, checkpoint when live WAL bytes reach this value; reader pins, atomic batches and deferred recycling may retain more without data loss |
| `checkpoint_interval_records` | `512` | After a durable write, checkpoint when the published WAL distance reaches this many records; a failed attempt is reported and retried after the next write |
| `max_statement_writes` | `None` | Optional hard limit on logical row writes retained by one statement |
| `max_result_rows` | `None` | Optional hard limit on public result rows; row N+1 is refused before it is retained and before any remaining input is consumed |
| `max_intermediate_rows` | `None` | Optional hard limit per non-terminal physical operator over one execution; it is not a cumulative query-wide count |
| `query_memory_budget_bytes` | `None` | Optional logical retained-byte ceiling per blocking sort, result-DISTINCT or aggregate operator; enables safe adapter-backed external spill without measuring RSS |
| `max_traversal_expansions` | `None` | Optional cumulative per-query limit on relationship candidates examined by graph-pattern operators; candidate N+1 is refused before derived landing/filter work |
| `max_traversal_paths` | `None` | Optional cumulative per-query limit on visible paths admitted by graph-pattern operators; path N+1 is refused before frontier retention or return |
| `max_query_value_characters` | `65536` | Per-string parameter/result boundary; configurable from 1 through the hard 1,048,576-character guard; query-source literals keep their separate 16,384-character ceiling |
| `max_transaction_rows` | `None` | Optional hard limit on retained `row_intents` in one transaction |
| `max_transaction_bytes` | `None` | Optional hard limit on encoded row tuples, staged logical-record `encoded_length()` values and retained page-image generations; ordinary replacement charges the byte delta, while a rollback preimage held by a live statement mark remains charged until settle/discard |
| `max_wal_batch_bytes` | `None` | Optional hard limit on the sum of final record `encoded_length()` values, including `COMMIT` and excluding `SEGMENT_HEADER`; checked before WAL append |
| `max_index_build_entries` | `None` | Optional hard limit on final exact entries across one detached shadow-build batch; counted to at most N+1 and refused before catalog staging or the first generation file is created |
| `automatic_index_expected_cardinality` | `None` | Keyword-only expected rows (1..4,194,304) per newly materialized automatic exact index; derives 1..65,536 eager buckets at 64 expected entries each, activates an empty writable catalog to v2, is persisted with that generation, and never rehashes an existing index. Counts above 4,096 buckets activate an additional required capability. |
| `metrics` | `"noop"` | `"noop"`, `"openmetrics"`, `"json"` |
| `metrics_destination` | `None` | Required file path for `"json"`; for `"openmetrics"`, `None` means `127.0.0.1:0` and an explicit IPv6 destination uses `[address]:port` |
| `allow_remote_metrics` | `False` | Exact boolean, valid only for `"openmetrics"`; permits a hostname or non-loopback address when explicitly `True` |
| `codec` | `"pure"` | `"pure"` binds the byte-contract oracle; `"numpy"` explicitly selects the NumPy-backed, byte-identical dense-directory codec and requires `[accel]` |
| `vector_math` | `"auto"` | `"auto"` and `"pure"` both bind the pure oracle; `"numpy"` requires `[accel]` |
| `checksum` | `"auto"` | `"auto"` detects an accepted accelerator, `"pure"` selects the reference, `"native"` requires the native provider; 0.0.5 connections capture an isolated per-database selection |
| `vector_exact_scan_threshold` | `4096` | Nonnegative candidate threshold for exact-vs-approximate selection; zero permits ANN whenever its other eligibility conditions hold. Inspect the returned regime, not just table size |
| `vector_ef_search` | `320` | Base HNSW beam in the approximate regime; integer from 1 through 1,048,576 |
| `vector_hnsw_memory_budget_bytes` | `None` | Positive integer or `None`; per-derived-picture HNSW logical construction/cache limit. Independent of query budgets; not RSS or an aggregate across pictures/handles. See [vector memory](INDEXES_AND_VECTORS.md#hnsw-derived-picture-memory). |
| `read_only` | `False` | No replay/repair; requires checkpoint-complete state and may refuse after a newer acknowledged commit. Distinct from `db.execute()`'s read transaction |

## Types, ranges and persistence

Integer settings reject booleans. Time settings accept finite positive int/float
seconds, not booleans, infinity or NaN. Optional integer limits accept `None` or a
strictly positive integer: zero does **not** mean unlimited. Strings are
case-sensitive selector values as listed above; booleans must be exact booleans.

| Setting family | Allowed values / compatibility |
| --- | --- |
| `page_size` | Integer power of two, 512–32,768 bytes; persisted, must match on every open |
| `partitions_per_table` | Integer 1–65,535 at creation; persisted/adopted on reopen |
| `identity_lease_size`, `max_open_files`, `checkpoint_interval_records` | Positive integer; operational; row-ID reservations already burned are never returned |
| `buffer_budget_bytes` | Positive integer, at least two pages of configured size; per handle, not total RSS |
| `wal_segment_bytes` | Integer 256–1,073,741,824 bytes; target segment size, not a hard per-transaction guarantee |
| `wal_max_bytes` | `None` or positive integer bytes; soft checkpoint trigger, not hard disk quota |
| `max_statement_writes`, `max_result_rows`, `max_intermediate_rows`, `max_traversal_expansions`, `max_traversal_paths`, `max_transaction_rows`, `max_index_build_entries` | `None` or positive integer counts; each has its own accounting scope above |
| `max_transaction_bytes`, `max_wal_batch_bytes`, `query_memory_budget_bytes` | `None` or positive integer bytes; different deterministic accounting models, not interchangeable |
| `automatic_index_expected_cardinality` | `None` or integer 1–262,144; only new generations, hint persisted with generation; not a resize command |
| `vector_exact_scan_threshold` | Integer ≥ 0; runtime selection, not a persisted HNSW construction parameter |
| `vector_ef_search` | Integer 1–1,048,576; approximate-query beam |
| `max_query_value_characters` | Integer 1–1,048,576 characters; default 65,536; source literal ceiling remains 16,384 |
| `lease_ttl_seconds`, `lease_timeout_seconds`, `commit_lock_timeout_seconds`, `reader_stall_threshold_seconds` | Finite positive seconds; neither a general statement deadline nor async cancellation |
| `metrics_destination` | `None` for noop; nonempty file for json; literal loopback `host:port`/`[IPv6]:port` for default OpenMetrics safety; port 0–65,535 |

Do not treat all positive integers as capacity promises: the relevant layout,
engine operation and available device space can impose additional typed refusals.
Custom registries supply their own adapters; selector presence does not override
caller-owned resources. See [ports](PORTS.md).

## Tuning guidance

- Start with `[accel]`, strict identity validation and defaults; measure before
  raising buffer sizes. Three independent 64 MiB handles have a 192 MiB nominal
  page envelope **plus** Python objects, vectors, temporary values and OS caches.
- Use query/transaction limits for untrusted or variable-size inputs. `None`
  preserves unbounded behavior. Split ingest into application-chosen atomic
  batches; Grafx does not secretly split a transaction to fit a budget.
- Increase timeouts only for a measured legitimate wait; never hide deadlock,
  stolen lease or unknown commit outcomes with endless retries.
- Larger identity ranges can reduce metadata work but create harmless gaps after
  abort/close/crash; never use internal row IDs as a gap-free business sequence.
- Size new indexes when cardinality is known. Oversizing eager buckets consumes
  memory/disk and slows full scans; unknown growth uses explicit maintenance.
- Each 0.0.5 connection retains its selected checksum provider. Opening another
  database or changing the standalone installer cannot change that handle's policy.

### Checksum isolation in 0.0.5

`connect` / `open_database`, including a custom `PortRegistry`, capture a validated
provider during construction. Public operations, transactions, checkpoint and close
bind that immutable selection using execution-local transport; nested calls and
threads restore their previous selection even on exceptions. No global operation
lock is introduced. Native provider proof memoization remains bounded and shared,
but the selected provider is not shared mutable configuration.

`pure`, `auto` and `native` retain their admission rules: explicit native absence
refuses before opening the store; auto may fall back to pure. Injected providers
retain per-call reference verification. The checksum algorithm, persisted bytes,
WAL grammar and disk format are unchanged, so a store written with pure can be
reopened with native/auto. This isolates CPU/provider policy, not different algorithms.

The low-level `install_crc32c` / `install_checksum` compatibility doors still set
the standalone default used by domain utilities outside a database operation.
Manually assembled low-level components do not implicitly acquire a database scope.
Installers invoked by a callback cannot replace its active database's selection.

## Detailed descriptor, metrics and budget contracts

In the 0.0.5 development line, optional internal preparation retains at most 256
parsed statements and 256 plans per engine. Texts exceeding 16,384 characters are
not retained in the parse/plan/statement-authority caches. Plans additionally use
a 32 MiB conservative admission tariff (source-derived objects, catalog images,
index definitions and dirty-table keys), not a measured heap-size or RSS bound.
These are internal acceleration limits, not new `connect` options. Exceeding one
executes the query normally without retaining that plan; it does not reject a statement
or waive schema/index/snapshot validation. Independent handles have independent
caches. Compiled predicates, row caches, buffers and caller-owned results are
separate retained state, so this tariff is not the whole database's memory budget.

`descriptor_revalidation="strict"` is the safe default: on every cached hit, the local adapter
proves that the logical name still names the physical file held by its descriptor. The opt-in
`"generation"` mode keeps control records, `grafx.meta` and unknown names strict, but amortizes that
proof for canonical heap, catalog, index and WAL files. It must be used only when Okto Grafx and
Okto Pulse are the exclusive writers of the database directory. An external replacement of a
whitelisted file can otherwise remain undetected until a directed proof of that name, a full
generation invalidation or reopen, and a stale descriptor can read or write an inode no longer
named by the directory. The option is inert for `":memory:"`; a caller-supplied registry is validated
but its storage adapter is not reconfigured by it. See
[`ST2_DESCRIPTOR_REVALIDATION.md`](architecture/ST2_DESCRIPTOR_REVALIDATION.md) for the exact
whitelist, transition table, coexistence rules, advantages and risks.
The read-only `database.descriptor_revalidation` property reports the effective process-local mode;
it is deliberately not part of the persisted `database.identity` record.

Without an override, an OpenMetrics destination must name a literal IP address that
`ipaddress.ip_address(host).is_loopback` classifies as loopback, for example IPv4 `127/8` or IPv6 `::1`.
Hostnames are not resolved for this decision, so even `localhost` is refused. A remote address or
hostname requires `allow_remote_metrics=True`; setting it for the no-op or JSON sink is itself
refused. Each remote-address or hostname publisher admitted by that override emits one
`RuntimeWarning` when it starts. This consent changes only where OpenMetrics may bind: it does not
add authentication, TLS or a firewall.

Configure IPv6 loopback as `metrics_destination="[::1]:0"`. The publisher binds `::1` with
`AF_INET6`, and `Database.metrics_endpoint` reports the usable bracketed URL
`http://[::1]:<chosen-port>/metrics`.

Recall is an offline calibration result, not a per-database runtime promise. The former
`vector_recall_target` connection option was removed; passing it now returns a typed migration
error. Set the benchmark floor with
`python -m bench.harness.gate --metrics <metrics.json> --require-recall --recall-target <floor>`.
Use `vector_ef_search` when the intended change is the HNSW work performed by runtime queries.

The four transaction limits are opt-in: `None` preserves the unbounded behaviour. Exceeding one
raises the non-retryable `GrafxTransactionBudgetExceeded`. A refused statement restores its exact
pre-statement staging, and a refused final WAL batch is rejected before append; these refusals do
not truncate the WAL or persist a partial statement.

The two query row limits are opt-in positive integers. `max_result_rows` counts the public
terminal incrementally; it consumes row N+1 only to refuse it, before retaining it or consuming the
rest of the stream and before `context.release()`. `max_intermediate_rows` counts each non-terminal
physical operator separately for the whole execution. A public terminal is charged only as result;
a terminal with no public columns is charged as intermediate. Overrun raises the non-retryable
`GrafxQueryBudgetExceeded`, without truncating state or releasing a partial write statement.

The two traversal limits are likewise opt-in positive integers, but are cumulative across every
graph-pattern operator in one query. Variable and untyped traversal charge an expansion for each
candidate yielded by their selected endpoint source, before repeat-edge and landing checks; a
fixed relationship scan charges each stored or pending relationship it encounters, before its
pushed predicate and endpoint checks. A path is charged only after the applicable pushed predicate
and landing visibility checks, immediately before the path can enter a variable-length frontier or
be returned by a one-hop scan. The first over-limit unit is refused before it is retained or
returned. These limits cover Cypher relationship traversal and scans, not the separate internal
HNSW navigation performed by a vector-search operator. Physical rows read once to construct a
grouped endpoint fallback are auxiliary scan work and are not charged as candidate expansions.
When disabled the limits do not add traversal counters to `QueryResult.statistics`; when enabled,
the corresponding `traversal_expansions` or `traversal_paths` statistic records admitted work on
successful queries.

`max_query_value_characters` bounds each string parameter and each string copied across the public
query-result boundary. It defaults to 65,536 characters, while query-source string literals retain
their independent 16,384-character lexer ceiling. Applications may lower the value or raise it up
to the hard 1,048,576-character guard; values above the effective ceiling are refused before page
access. The option is process-local and does not change the on-disk format.

`query_memory_budget_bytes` is a separate opt-in positive integer. `None` preserves the previous
in-memory sort, top-N, result-DISTINCT and aggregation paths. When configured, each `SortRows`,
`DistinctRows` and `AggregateRows` gets its own counter and uses adapter-owned external merge runs,
so input cardinality no longer causes those operators' retained logical bytes to grow without the
configured ceiling. The counter is deterministic **logical retention, never process RSS**: a
buffered/run-head record is
charged `32 + len(versioned_key) + len(versioned_payload)` bytes; one active aggregate group is
charged 64 bytes plus its versioned detached key and 64 bytes per aggregate slot; each retained
`COLLECT` or `MIN`/`MAX` value adds 16 bytes plus its versioned detached value; each strongly
retained NaN identity adds 64 bytes; and a transaction-private held-row identity needed by
result-DISTINCT adds 128 bytes plus its versioned detached values. Python object headers, allocator
arenas, encoding/comparison temporaries, OS caches and the final caller-owned result are
deliberately outside this portable accounting model.

Spill records use purpose- and version-tagged `Value` encodings and a versioned run header; they
never use pickle. Files live in an isolated adapter temporary directory, outside the database
namespace; binary merge levels keep their in-memory path metadata O(log N), and all artifacts are
removed on success, refusal, cancellation and cursor close. Cleanup failure is not silently
accepted. A single record must fit beside another merge head, so its logical charge must be at most
half the configured budget. Result `DISTINCT` and aggregate `DISTINCT` values spill too, preserving
the first occurrence and its order. `COLLECT` still
has to become one public tuple, so a group whose result itself exceeds the budget is refused rather
than represented by a disk proxy. Enabling this option also routes `ORDER BY ... LIMIT` through the
bounded external path instead of the faster O(K) top-N heap.

`max_result_rows` and `max_intermediate_rows` remain independent and authoritative with spill
enabled. This first byte-budget boundary does not cover `EagerRows`, vector-search candidate
materialisation, deadlines, RSS or public result retention; use the row limits and cursor API for
those separate boundaries. It changes no snapshot, transaction, WAL, OCC, durable format,
multiwriter or multireader rule.

---
