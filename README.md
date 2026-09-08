# Okto Grafx

Embedded, local-first graph database for Python, with concurrent readers and writers,
snapshot isolation, WAL-backed durability, verification and fail-closed recovery.
No database server is required. The core has no third-party runtime dependencies.

**Source version: 0.0.4, pre-alpha.** This describes the checked-out source, not a
claim that 0.0.4 is already published on PyPI. API and persistent-format compatibility
must be checked before upgrading; see [operations](docs/OPERATIONS.md).

## Install and start

Python 3.11–3.13; local Windows and POSIX filesystems.

```sh
pip install "okto-grafx[accel]"
```

This installs the release available from your configured package index. For this
source revision, use `pip install -e ".[dev,accel]"` in a checkout. `okto-grafx`
is the distribution name; `okto_grafx` is the Python import; `oktografx` is the CLI.
The `[accel]` extra adds native CRC-32C and NumPy. NumPy vector math/page codec
require explicit selectors; installing the extra does not select them automatically.

```python
from okto_grafx import connect

with connect(":memory:") as db:
    with db.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
        txn.execute("CREATE (:Person {id: $id, name: $name})", {"id": 1, "name": "Ada"})
    result = db.execute("MATCH (p:Person) RETURN p.id, p.name")
    assert result.rows == ((1, "Ada"),)
```

Use a directory instead of `:memory:` for persistent data. `db.execute()` is a
read-only autocommit door; writes use explicit transactions. A normal transaction
context exit commits, and an exceptional exit rolls back. See
[the integration tutorial](docs/GETTING_STARTED.md) for relationships and durable reopen.

## What is available

| Capability | Consumer documentation |
| --- | --- |
| Multi-process/multi-thread access, snapshot transactions, optimistic conflicts and writer fencing | [Concurrency and recovery](docs/OPERATIONS.md) |
| Typed nodes/relationships, parameters, writes, traversal, aggregates, limited OPTIONAL MATCH and UNION | [Supported query language](docs/QUERY_LANGUAGE.md) |
| Atomic `executemany`, streaming results and snapshot-bound physical scan cursors | [Integration recipes](docs/INTEGRATION.md) |
| Automatic PK/endpoint/identity indexes; custom hash/ordered indexes; foreground rebuild/rehash | [Indexes and vectors](docs/INDEXES_AND_VECTORS.md) |
| Exact and approximate vector search with declared space, metric and precision | [Indexes and vectors](docs/INDEXES_AND_VECTORS.md) |
| Query/transaction budgets, optional spill, acceleration and metrics | [All configuration fields](docs/CONFIGURATION.md) |
| Verification, evidence ledger/quarantine, recovery/checkpoint, manual vacuum and WAL compression | [Operations](docs/OPERATIONS.md) |
| Bounded physical backup and verified offline replacement restore (0.0.5 development) | [Backup and restore](docs/BACKUP_RESTORE.md) |
| Embedded Python, machine-readable CLI, configurable ports/adapters | [API](docs/API_REFERENCE.md), [CLI](docs/CLI.md), [ports](docs/PORTS.md) |

Concurrent transactions do not imply lock-free commits: publication has an exclusive
section, different rows can conflict on physical pages, and operations may wait or
time out. Recovery repairs only states that the WAL and durable identity prove safe;
it does not silently rebuild or discard damaged authoritative data.

There is no native async/HTTP/MCP server, SQLAlchemy backend, public time-travel API
or no-pause streaming hot-backup API in this release surface. Proposed capabilities
are explicitly separated in the [roadmap](ROADMAP.md).

## Current measured performance

Latest **recorded** real-consumer sample: September 8, 2026, Grafx
`0.0.4@fa8f188` with `[accel]`, Pulse 0.3.3, Windows/Python 3.13. This is not a
benchmark of the later GX-CAP-1 recovery checkpoint or a universal latency promise.

| Operation / workload | Observed time | Boundary |
| --- | ---: | --- |
| Consolidation commit, 11 new nodes / 22 edges | 10.726 s | Full Pulse MCP call, not native commit only |
| Global delivery of that commit | 54.952 s | Outbox creation to processed ACK; includes scheduling/verification |
| Exact readback of the 11 nodes | 0.376 s | MCP call; executor 91.1 ms |
| Readback of 11 judgement links / 11 source links | 0.408 / 0.182 s | Separate MCP calls |
| Exact-title natural retrieval | 2.709 s | One query; not broad-query recall/latency |

These are one-sample observations, not p50/p99 statistics or an identical-input
comparison with Ladybug. Latest full KG UI cold/warm latency at the current source
checkpoint is **not measured**. Native component measurements and their limitations
are in [performance](docs/PERFORMANCE.md); no marginal timing gate blocks delivery.

## Documentation

Start with the [documentation index](docs/README.md). It separates tutorials,
consumer references, operational procedures, implementation specifications and
historical evidence; integrating the library does not require reading the latter.

- [Getting started](docs/GETTING_STARTED.md)
- [Integration: services, workers, cursors and agents](docs/INTEGRATION.md)
- [Public API and result types](docs/API_REFERENCE.md)
- [Configuration](docs/CONFIGURATION.md)
- [Query language](docs/QUERY_LANGUAGE.md)
- [Operations, concurrency, errors and upgrades](docs/OPERATIONS.md)
- [Performance and measurement boundaries](docs/PERFORMANCE.md)
- **[Roadmap: evolution, known limitations and corrective work](ROADMAP.md)** — the sole active backlog
- [Changelog](CHANGELOG.md), [contributing](CONTRIBUTING.md), [security](SECURITY.md)

## Deployment and license

The host application owns authorization, input policy and filesystem permissions.
Do not place live database files on cloud-sync/network filesystems or replace them
while participants are using them. OpenMetrics has no authentication/TLS; remote
binding requires explicit consent. See [operational safety](docs/OPERATIONS.md).

Licensed under [Elastic License 2.0 with the SaaS/Branding Addendum](LICENSE).
Check those terms before offering a hosted service.
