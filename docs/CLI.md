# CLI and agent consumption

[Documentation index](README.md) · [Operations](OPERATIONS.md) · [Python API](API_REFERENCE.md)

Installing the distribution adds `oktografx`. It is a local command-line interface,
not a database server, an MCP server or an authorization boundary. Use Python when
you need long-lived snapshots, all connection settings or reusable handles.

## Commands

| Command | Purpose |
| --- | --- |
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
