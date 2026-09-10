# CLI and agent consumption

[Documentation index](README.md) · [Operations](OPERATIONS.md) · [Python API](API_REFERENCE.md)

Installing the distribution adds `oktografx`. It is a local command-line interface,
not a database server, an MCP server or an authorization boundary. Use Python when
you need long-lived snapshots, all connection settings or reusable handles.

## Commands

### Table schema inventory (0.0.6 development)

`oktografx schema PATH --json --limit 100` captures one immutable public catalog
view without scanning graph rows. It always opens read-only and refuses `--create`
and write flags. An incomplete store needing recovery is refused, never repaired
by this command. Close/open and error behavior follow the standard CLI envelope.

Payload version `schema_version: 1` contains `read_only`, `tables`, `total_tables`,
`returned_tables`, `spaces`, `total_spaces`, `returned_spaces` and `truncated`. Each table has `table_id`, `name`, `kind`,
`schema_version`, `primary_key`, `from_table`, `to_table` and ordered `columns`;
each column has `name`, `type` (enum name), `nullable` and `vector_space`.
Tables retain numeric identity order; absent endpoints/keys/spaces are JSON null.
This is a captured schema, not a live freshness or database-health certificate.

The non-negative limit defaults to 100 and separately bounds returned table/space definitions,
not catalog loading, column count or serialized bytes. Zero returns counts only.
Successful truncation exits 0 with `truncated: true`; consumers requiring all
definitions must check this flag. Spaces expose identity, dimension, distance
metric, normalization, dtype, state and creation time from the captured DTO.

### Indexes and build capabilities

`oktografx indexes PATH --json --limit 100` always opens read-only and returns
`schema_version: 1`, `indexes`, `vector_indexes`, each inventory's `total_*`, and
`truncated`. Each array is independently limited. Secondary entries carry their
public `IndexView` fields (definition/layout/columns, generation, stale state,
reason and observed LSNs); vector entries carry `VectorIndexView` fields (space,
dimension/metric/dtype, ef_search, stale state and observed build LSN).
Enum values are JSON strings, tuples are arrays, absent values are null.

The two inventories are **independent metadata snapshots**, not one atomic
cross-registry certification. `secondary_published_lsn` qualifies the secondary
inventory. Null header LSNs can mean cold metadata; stale entries remain visible.
This command does not fault all index pages into cache, inspect entries, create
indexes, repair stale generations or prove freshness/health.

`oktografx capabilities --json` needs no path and does not open storage. It returns
`schema_version: 1`, package `version`, sorted CLI `commands`, the search contract
(rankers, `max_k`, partial policy) and the transaction scope/isolation contract.
Its `scope` is `build_cli_contracts_not_store_activation`: this deliberately does
**not** assert that a particular store enabled an index or persistent-format flag,
nor is it a complete manifest of every Python API.

### Text, vector and hybrid search

```sh
oktografx search text ./graph --index docs_text --query 'durable graph' --k 20 --json
oktografx search vector ./graph --space semantic --vector '[1,0]' --k 20 --json
oktografx search hybrid ./graph --table Doc --index docs_text --query graph --space semantic --vector '[1,0]' --json
```

These commands require existing indexes/spaces and always open read-only. All
sources in a search use one owned read transaction, closed before returning.
`--k` defaults to 20, range 1..1000. `--filter '[1,2]'` restricts physical record
IDs (not application primary keys); `[]` selects nothing; omission is unfiltered.
Vectors must be nonempty finite numeric JSON arrays; the engine checks dimension,
space and numeric storage rules. No embedding provider runs implicitly.

`--timeout-seconds` is a positive finite cooperative search timeout, default 30;
it does not bound connection open/close or preempt an arbitrary blocked OS call.
Native read/work budgets still apply. The CLI does not expose every Python
ranking/index option; use Python for custom BM25, hybrid weights or graph boosts.

The version-1 envelope's `search` field contains the complete public
`TextSearchResult`, `VectorSearchResult` or `HybridSearchResult` DTO, including hits,
regime, scores and source/snapshot diagnostics. No adapter reranking occurs.
Hybrid uses default RRF options with partial sources **disabled**: an unavailable
required source is a typed refusal, not a successful empty result. Exact/approximate
vector regimes remain visible. All failures use the existing nonzero CLI exits;
successful searches exit 0, including legitimate empty hits.

### JavaScript and TypeScript recipe

See [the bounded subprocess recipe](../examples/cli-consumer/README.md). It uses
argv arrays without a shell, bounds output and time, checks the envelope against
the actual exit status, and waits for process closure after cancellation. It is
not a native driver, pooling layer, server or long-lived transaction protocol.

| Command | Purpose |
| --- | --- |
| `oktografx schema PATH` | Read-only table/embedding-space definitions |
| `oktografx indexes PATH` | Read-only secondary/vector metadata, including stale entries |
| `oktografx capabilities` | Build CLI contracts; not per-store activation |
| `oktografx search text/vector/hybrid PATH` | Existing bounded native search APIs |
| `oktografx status PATH` | Open and report status/evidence |
| `oktografx query PATH STATEMENT ...` | Reads; `--write` makes all statements one write transaction |
| `oktografx verify PATH --scope all` | Located findings; scopes `pages`, `records`, `indexes`, `all` |
| `oktografx recovery PATH` | Report recovery at writable open |
| `oktografx ledger list PATH` | List forensic records |
| `oktografx ledger inspect PATH ENTRY_ID` | Inspect one forensic record |
| `oktografx ledger export PATH ENTRY_ID --output FILE` | Export one entry's preserved bytes |
| `oktografx quarantine list PATH` | List preserved artifacts |
| `oktografx quarantine inventory PATH` | Read-only inventory, including incomplete/malformed artifacts; refuses `--create` |
| `oktografx quarantine inspect PATH NAME` | Inspect one artifact |
| `oktografx quarantine read PATH NAME --output FILE` | Export checksum-verified evidence |
| `oktografx metrics PATH` | Report current metrics/endpoint |
| `oktografx control downgrade PATH` | Offline control-format v2→v1 conversion only; all participants must be stopped; not a general engine downgrade |

`recovery --rerun` requests another explicit recovery pass. Ledger list accepts
`--origin-class`, `--reason`, `--limit`, `--offset`; all filters are shown in help.

Use `oktografx COMMAND --help` (or `oktografx ledger inspect --help`) for all
arguments. All commands accept `--json`: one structured document on stdout.
Check exit status as well as data; an empty result is not proof of verification.

```sh
oktografx status ./graph --json --read-only
oktografx query ./graph 'MATCH (p:Person) WHERE p.id = $id RETURN p.name' --parameter id=1 --json
oktografx verify ./graph --scope all --read-only --json
```

In PowerShell and POSIX shells, single quotes around query text containing `$id`
prevent shell expansion. Repeat `--parameter NAME=VALUE`; values parse as JSON
when possible, otherwise as text. `--limit` limits **printed rows**, not execution
work/memory; zero prints all. Use query `LIMIT` or Python budgets for those limits.

Commands refuse a missing database path by default. `--create` explicitly permits
creation. Writable open may recover; a command without `--read-only` is therefore
not a forensic no-write inspection. `--read-only` does not replay WAL and may
refuse a database that needs recovery.

Shared connection options: `--page-size`, `--partitions-per-table`,
`--buffer-budget-bytes`, `--recovery-policy`, `--metrics`, `--metrics-destination`,
`--vector-math`, `--read-only`, `--create`. These are a subset of Python's
[36 configuration fields](CONFIGURATION.md), not aliases for every setting.

## Exit codes

| Code | Meaning / consumer action |
| ---: | --- |
| 0 | Clean/successful command |
| 1 | Findings/evidence; inspect report |
| 2 | Invalid command line |
| 3 | Typed nonretryable refusal; inspect code/details |
| 4 | Damaged bytes; preserve evidence |
| 5 | Retryable refusal; bounded retry with fresh operation |
| 6 | Inconclusive; do not report clean |
| 70 | Internal error; retain diagnostic and report defect |
| 130 | Interrupted; do not infer write rollback solely from exit status |

For agents, prefer allowlisted reads and bounded results; destructive maintenance
needs host-level approval. Never retry a mutation solely because its transport
timed out. Establish its durable outcome/idempotency first. Proposed Agent API/MCP
products are [roadmap work](../ROADMAP.md#optional-agent-product), not installed today.
