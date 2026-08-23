# Security Policy

## Supported releases

Security fixes are prepared for the latest published Okto Grafx release. When a fix is released,
users should upgrade to the newest available version. Older versions may not receive backports.

Okto Grafx is pre-alpha (0.0.1) and the on-disk format is not yet stable. An upgrade may require
rebuilding a database rather than migrating it.

## Reporting a vulnerability

Do not open a public GitHub issue for a suspected vulnerability. Send a private report to
**dev@oktolabs.ai** with:

- the affected version and installation mode (core, `accel`, from source);
- the operating system and filesystem;
- the relevant configuration — buffer budget, page size, lease and timeout settings, the metrics
  selector, and whether the database is opened read-only;
- reproduction steps or a minimal proof of concept;
- the security impact and the data or permissions affected;
- any suggested mitigation, if known.

Remove private data from the report unless it is strictly necessary to reproduce the issue: row
values, embeddings, database directories, and file paths outside the database. **A database
directory is not safe to attach as-is** — it contains everything that was stored in it. If sensitive
material must be shared, ask for a secure transfer method first.

Okto Labs will evaluate the report and coordinate remediation and disclosure as appropriate. This
policy does not promise a specific response or resolution time.

## What is in scope

Okto Grafx is an **embedded library**. It has no authentication, no authorization, and no network
listener other than the optional loopback metrics endpoint. The interesting boundary is therefore
what the library does with the input it is given and with the bytes it reads from disk. In scope:

- **A crafted or corrupted database directory** that causes something worse than a typed `Grafx*`
  refusal — an unbounded allocation, a read outside the database directory, a write to a path the
  configuration did not name, a hang, or arbitrary code execution.
- **A crafted query or parameter** that escapes the taxonomy, reads or writes data the caller has no
  handle to, allocates without bound, or does not terminate.
- **A path traversal** through any name a caller can influence — a table name, an index name, a
  metrics destination, a quarantine or ledger entry.
- **The OpenMetrics endpoint** exposing more than metric values, or binding an address other than
  loopback.
- **A concurrency defect that crosses a trust boundary**, such as one process being able to make
  another process write bytes it did not author.
- **The forensic ledger or quarantine** writing content it was supposed to sanitise, or writing
  outside the database directory.

## What is not in scope

- **Correctness defects that are not security issues** — data loss, a wrong query result, corruption
  under concurrency. These are serious and are triaged first, but they belong in a public
  correctness report, not here.
- **Filesystem permissions on the database directory.** Anyone who can read the directory can read
  the data; that is what an embedded database is.
- **A caller passing untrusted input as a query through string concatenation.** Use parameters.
- **Denial of service by configuration** — an unreasonably large page size, a one-page buffer budget.
- **Dependencies of the optional extras**, which should be reported upstream.

## Deployment responsibility

Okto Grafx is designed for local or controlled single-tenant use, embedded in an application that
owns its own security boundary. Operators are responsible for:

- filesystem permissions on the database directory and its parent;
- access control and authentication in the application that embeds the library;
- keeping the database off shared network filesystems whose locking semantics differ from a local
  disk — the multi-process guarantees rest on the platform's locking and atomic-replace behaviour;
- backup and restore;
- binding, or not exposing, the optional metrics endpoint;
- timely upgrades.

Review [Status and limitations](README.md#status-and-limitations) before deployment.
