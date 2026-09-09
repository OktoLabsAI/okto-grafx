# Documentation index

The consumer guides include source version **0.0.5 development** additions, marked
with their implementation and acceptance status. They do not assert PyPI
publication or that the latest source is installed in a particular application.

## Integrate without reading engine internals

| Question | Read |
| --- | --- |
| How do I install, create a graph and reopen it? | [Getting started](GETTING_STARTED.md) |
| How do I integrate a synchronous library into services, workers or agents? | [Integration](INTEGRATION.md) |
| Which public methods and return values exist? | [API reference](API_REFERENCE.md) |
| How do I apply/check versioned application DDL safely? | [Schema migrations](SCHEMA_MIGRATIONS.md) |
| How do I opt into commit provenance and page its history? | [Commit history](COMMIT_HISTORY.md) |
| Which settings exist, what are their defaults and risks? | [Configuration](CONFIGURATION.md) |
| Which Cypher constructs and value types are supported? | [Query language](QUERY_LANGUAGE.md) |
| How do indexes, vectors and explicit growth work? | [Indexes and vectors](INDEXES_AND_VECTORS.md) |
| How do I index text and search with BM25, filters and snapshot consistency? | [Native full-text search](FULL_TEXT_SEARCH.md) |
| How do I combine lexical, vector and bounded graph evidence? | [Hybrid search](HYBRID_SEARCH.md) |
| How do I export a graph or create a separately writable logical copy? | [Logical export/import](LOGICAL_TRANSFER.md) |
| How do I handle conflicts, uncertain writes, recovery and upgrades? | [Operations](OPERATIONS.md) |
| How can I cancel reads or safely reclaim orphan index files? | [Read control and index cleanup](READ_CONTROL_AND_INDEX_CLEANUP.md) |
| How can a script/agent consume structured results? | [CLI](CLI.md) |
| What has actually been measured? | [Performance](PERFORMANCE.md) |
| What is missing or planned? | [Roadmap](../ROADMAP.md) |

Recommended reading order: tutorial → integration → configuration → operations;
then consult the API/query references for the capabilities your application uses.

## Extend or maintain the engine

- [Trusted scalar extensions and optional Arrow batches](EXTENSIONS_AND_ARROW.md).
- [0.0.5 compatibility/upgrade matrix](V005_COMPATIBILITY.md).

[Physical backup and restore](BACKUP_RESTORE.md) covers the bounded local API,
checkpoint fence, manifests, integrity checks and offline replacement obligations.

- [Ports and custom adapters](PORTS.md): trusted in-process extension boundary,
  ownership and examples. This is not a sandbox; scalar UDFs have a separate SPI above.
- [Architecture](ARCHITECTURE.md) and [normative engine contract](architecture/CONTRACT.md).
- [Capability specifications](specs/CAPABILITY_MILESTONES.md): detailed acceptance
  contracts; their existence is not evidence that a feature is callable.
- [Component design record](architecture/COMPONENTS.md), [lessons](architecture/LESSONS.md),
  [contributing](../CONTRIBUTING.md), [security policy](../SECURITY.md).

## Historical evidence, not competing plans

- [Report index](reports/README.md): dated experiments, implementation checkpoints,
  Pulse audits and historical comparisons.
- [Consolidated former plans](archive/ROADMAP_SOURCES.md): eleven complete source
  documents, including evolution, agent-first, complementary, Round 7 and performance
  plans. [Preservation manifest](archive/ROADMAP_SOURCES_MANIFEST.json).
- [Previous README](archive/README_BEFORE_REFACTOR.md): provenance only; superseded.

The **only current queue/status authority is [ROADMAP.md](../ROADMAP.md)**. Reports
remain reproducible evidence, specs define acceptance, and this index routes readers.
Historical “next step”, PID, deadline, approval and performance-gate statements do
not become current instructions by being preserved.
