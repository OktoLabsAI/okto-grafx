# Okto Grafx

An embedded graph database for Python: correct under multi-process concurrency, verifiable on disk,
and recoverable by construction.

Status: under construction against two validated specs (see `docs/specs/`).

## Install

```bash
pip install okto-grafx            # pure Python, no runtime dependency
pip install "okto-grafx[accel]"   # recommended: same answers, measurably faster
```

The core is pure Python and the standard library is its only runtime requirement. `[accel]` adds two
optional accelerators behind ports the engine already declares: a native CRC-32C and numpy vector
math. **The accelerated checksum is not a different answer.** `install_crc32c` replays an acceptance
corpus against the pure-Python reference and refuses a candidate that disagrees on any input before
it is installed, so a database written by one build reads identically in the other. It is worth
installing: on Linux the durable-commit multiple against the reference engine falls from ~17x to
**~5x** with it, which is inside the ceiling binding decision D5 sets. See
`docs/architecture/COMPONENTS.md` for the measurements and for the Windows gap, which is open.

## Using it

Two doors, and CONTRACT.md section 10 draws the line between them: `db.execute(...)` is the
autocommit **read**; writes go through a transaction.

```python
from okto_grafx import connect

db = connect("./mydb")

with db.begin("write") as txn:
    txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    txn.execute("CREATE REL TABLE Knows(FROM Person TO Person, since INT64)")

with db.begin("write") as txn:
    txn.execute("CREATE (:Person {id: 1, name: 'Ada'})")
    txn.execute("CREATE (:Person {id: 2, name: 'Grace'})")

with db.begin("write") as txn:
    txn.execute(
        "MATCH (a:Person {id: 1}), (b:Person {id: 2}) CREATE (a)-[:Knows {since: 1994}]->(b)"
    )

db.execute("MATCH (a:Person)-[:Knows]->(b:Person) RETURN a.name, b.name")  # (('Ada', 'Grace'),)
db.verify("all")        # walks the database and reports every finding, precisely located
db.checkpoint()         # puts the committed state on the platter and reclaims the log
db.close()
```

A row's identity is allocated by the commit, so a `MATCH` cannot bind a row the same transaction
created: commit the rows, then match them. `SET` and `DELETE` work; `DETACH DELETE` and relationship
deletion do not yet and refuse in the taxonomy rather than pretending.

## Operator command line

```
oktografx status PATH            # what state this database is in
oktografx verify PATH            # walk it and report every finding
oktografx query PATH STMT        # autocommit read; add --write for a write transaction
oktografx recovery PATH          # what the recovery pass at open did
oktografx ledger|quarantine ...  # the evidence preserved when something went wrong
oktografx metrics PATH           # the endpoint and the current value of every metric
```

Exit codes are a contract: `0` clean, `1` not clean, `2` unreadable command line, `3` typed refusal,
`4` damaged bytes, `5` retryable refusal.

## What it guarantees

* **Multi-process reads and writes** (D1). N processes and N threads hold transactions on one
  database; a commit is refused only when its partitions genuinely intersect another's, never because
  another writer exists.
* **Snapshot isolation.** A reader sees the state of the instant it opened for its whole life, while
  other processes commit; readers never block writers and writers never block readers.
* **Durability that does not lie.** A commit returns only after its records survive the log's
  barrier. What is published is logged; what is applied is logged.
* **Verifiability.** `verify()` walks pages, records and indexes and reports findings with their
  location. A clean database reports nothing.
* **Windows and POSIX as equal citizens** (D9) in behaviour. Performance is not yet at parity: see
  the D5 record in `docs/architecture/COMPONENTS.md`.

## Documentation

* `docs/specs/` — the two validated specifications this is built against.
* `docs/architecture/CONTRACT.md` — the frozen coordination substrate: error taxonomy, on-disk
  formats, the commit protocol, the metric catalogue, and the Definition of Done every component is
  reviewed against.
* `docs/architecture/COMPONENTS.md` — the component register, the sign-off record, and every carried
  finding with the measurement behind it.
* `docs/architecture/LESSONS.md` — what went wrong while building this and what it taught.
* `docs/architecture/PUNCHLIST.md` — known gaps, written down rather than hidden.

## License

Apache 2.0. See `LICENSE`.
