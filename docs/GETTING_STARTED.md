# Getting started

[Documentation index](README.md) · [API](API_REFERENCE.md) · [Operations](OPERATIONS.md)

## Installation and runtime identity

Use Python 3.11, 3.12 or 3.13 on a local filesystem:

```sh
pip install okto-grafx
python -c "import okto_grafx; print(okto_grafx.__version__, okto_grafx.__file__)"
oktografx --help
```

Pin the package version your application has validated. For unreleased source,
`pip install -e ".[dev,accel]"` selects this checkout; do not assume an already
running process adopts an installation change. Drain and restart that process.

In the latest 0.0.5 development revision, NumPy and `google-crc32c` are base
dependencies; `[accel]` remains a compatible installation alias. Both `codec`
and `vector_math` default to `"numpy"`. `checksum="auto"` detects an accepted
native checksum provider. Explicit `vector_math="auto"` still uses the pure
oracle, and `"pure"` remains available for both selectors. Missing NumPy refuses
instead of silently falling back. Earlier releases may require `[accel]` and
explicit NumPy selectors. Saved explicit settings are not overwritten.

## A complete durable example

This uses a fresh temporary directory, so it is safe to run repeatedly. Replace
the temporary path with a fixed application-owned directory in production.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from okto_grafx import connect

with TemporaryDirectory() as scratch:
    path = Path(scratch) / "graph"
    with connect(path) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
            txn.execute("CREATE REL TABLE Knows(FROM Person TO Person, since INT64)")
            txn.executemany(
                "CREATE (:Person {id: $id, name: $name})",
                [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}],
            )
            txn.execute(
                "MATCH (a:Person {id: 1}), (b:Person {id: 2}) "
                "CREATE (a)-[:Knows {since: 2026}]->(b)"
            )
        assert txn.report is not None and txn.report.durable
        rows = db.execute(
            "MATCH (a:Person)-[:Knows]->(b:Person) RETURN a.name, b.name"
        ).rows
        assert rows == (("Ada", "Grace"),)
    with connect(path) as reopened:
        assert reopened.execute("MATCH (p:Person) RETURN count(*)").rows == ((2,),)
        assert not reopened.verify("all").findings
```

Schema DDL is transactional. Later statements in the same write transaction see
its earlier schema/row changes. An edge may refer to nodes created by an earlier
statement in that transaction, not endpoints being created by that same statement.
Use parameters for values; do not interpolate user input into query text.

## Transaction and result rules

- `db.execute(text, parameters)` creates a short read transaction. DML/DDL refuse.
- `db.begin("write")` or `db.transaction("write")` creates an explicit writer.
  Clean context exit commits; exceptional exit rolls back. Read contexts release
  their snapshots without writing application data.
- `QueryResult.columns` identifies positional columns; `rows` is a tuple of tuples.
  Project explicit properties for stable application DTOs. Results are detached;
  a cursor, transaction and physical scan continuation are not transferable DTOs.
- An exception from commit is not sufficient to conclude that nothing committed.
  Follow the [durable-outcome rules](OPERATIONS.md#commit-outcomes-and-retries).
- `connect(":memory:")` is isolated ephemeral storage. It is not a shared-memory
  database among separate `connect()` calls/processes.

Do not put `verify(all)`, recovery, vacuum or full rebuilds on every request.
They are explicit operational work, not required boilerplate after every commit.
