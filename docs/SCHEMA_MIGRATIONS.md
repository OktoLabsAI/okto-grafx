# Application schema migrations

In 0.0.6 development, the migration ledger resolves specifically to the node
namespace. A same-named relationship neither becomes the ledger nor hides the
owned node ledger. Node schema, owner marker and checksum validation remain
mandatory; an unrelated edge cannot authorize a corrupt/missing ledger row.
Dry-run, per-version atomicity and bounded OCC retry are unchanged. Overlapping
names retain the catalog-v2 namespace capability and upgrade requirements.

For the separate 0.0.6 typed nullable-column operation, see
[append-only schema evolution](NULLABLE_COLUMNS.md). The string-based migration
runner below does not yet accept ALTER statements or typed operations.

[Index](README.md) · [API](API_REFERENCE.md) · [Roadmap](../ROADMAP.md)

The 0.0.5 development API `okto_grafx.migrations` provides a bounded additive
application migration runner. It is distinct from engine file-format migration,
does not enable commit history, and adds no connection option or background job.

```python
from okto_grafx import connect
from okto_grafx.migrations import SchemaMigration, migrate_schema

plan = (
    SchemaMigration(1, ("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))",)),
    SchemaMigration(2, ("CREATE REL TABLE Knows(FROM Person TO Person)",)),
)
with connect(":memory:") as db:
    preview = migrate_schema(db, plan, namespace="contacts", dry_run=True)
    assert preview.pending == (1, 2)
    report = migrate_schema(db, plan, namespace="contacts")
    assert report.applied == (1, 2)
    assert migrate_schema(db, plan, namespace="contacts").previously_applied == (1, 2)
```

## Inputs, supported operations and checksum contract

Always supply the complete ordered tuple of versions 1..N (at most 1,024); a
`SchemaMigration` has integer `version` and a tuple of 1..64 nonempty DDL strings.
The entire plan is limited to 1 MiB of UTF-8 statement text. Supported statements
are `CREATE NODE TABLE`, `CREATE REL TABLE`, `CREATE VECTOR SPACE`. Dependencies
must appear first. Parameterized/external callbacks, DML/backfills, ALTER, DROP,
views and derived graphs are not supported. Native parser/planner/domain refusals
are retained; unsupported parsed statements raise `GrafxUnsupportedOperation`.

`SchemaMigration.checksum` is SHA-256 over the ASCII encoding of canonical JSON
`["grafx-application-migration-v1", version, statements]`, with `ensure_ascii=True`
and separators `(',', ':')`. Exact original statement strings participate:
changing whitespace also changes the checksum. Never edit an applied migration;
append a new version. A shorter plan than the database history, gaps, altered
checksums or unexpected ledger ownership/schema raise `GrafxLedgerError` before
DDL. This does not re-audit all already-applied objects against manual modification.

| Option | Contract |
| --- | --- |
| `namespace` | Required ASCII schema identifier, at most 32 characters; use a stable per-application value. |
| `dry_run=False` | Exact boolean. True reads history and runs native planning against a detached catalog, with no DDL execution, WAL writes or ledger creation. |
| `max_attempts=3` | Integer 1..16, per next-version attempt; OCC conflicts retry with a fresh transaction. Stable schema/checksum errors do not retry. A schema-binding error retries only when publication movement proves concurrent interference. |

Dry-run validates the whole remaining plan and its dependencies, not device space,
physical index-building budgets, later races or runtime I/O. It is not “execute and
rollback.” A read-only database can preview if the normal read-only-open consistency
requirements are satisfied (writable recovery/checkpoint first when necessary).
Apply requires a writable handle. Use an otherwise idle dedicated handle, not a
caller-owned transaction whose atomicity you expect to include these migrations.

## Atomicity, concurrency and resumption

Each version's DDL and checksum row commit in **one normal native write transaction**;
initial ledger creation/ownership marker shares the first version's transaction.
Normal OCC, multi-reader/writer, WAL and durability remain in force. No global
single-writer migration daemon is introduced. A competing migrator may apply a
version first; a fresh attempt recognizes it rather than executing it again.
Under sustained interference the bounded call may refuse; resubmit the same plan.

Versions commit separately: a later runtime failure leaves earlier committed
versions applied. There is no all-plan rollback or downgrade. On restart, native
recovery establishes committed state; resubmission verifies checksums and executes
only the remaining suffix. A post-COMMIT error is not permission to replay arbitrary
DDL blindly. Recovery can require a fresh writable open before retrying.

`MigrationReport` contains `namespace`, `dry_run`, `previously_applied`, `applied`,
`pending`, and `applied_lsns`. Versions applied by another participant are observed,
not credited to this call. `applied_lsns` contains `(version, COMMIT LSN)` only for
versions newly committed by this invocation; LSNs are **database-local**, not
globally qualified provenance IDs. An exception may follow a committed prefix;
inspect with dry-run or resubmit the full unchanged plan.

## Ledger ownership and limits

The reserved node table `_grafx_migrations_<namespace>` has `version INT64` primary
key and `checksum STRING`. Version 0 holds SHA-256 of ASCII
`grafx-migration-ledger-v1:<namespace>`; positive versions hold migration checksums.
Migration DDL cannot declare objects under the reserved prefix. Existing tables
without the exact schema and marker are refused, not adopted or erased. Do not
modify these rows directly. They are ordinary application data, preserved by
physical backup and logical transfer, not a tamper-proof security boundary against
a host that can write the database. Arbitrary manual schema rewrites, rollback
migrations, materialized views and online column conversions remain roadmap work.
