# Attached catalogs and bounded workspaces

[Documentation index](README.md) · [API reference](API_REFERENCE.md) · [Roadmap](../ROADMAP.md)

## Delivery boundary

The 0.0.6 development API provides named **single-store** transactions and an
optional non-persisting workspace resolver. It does not provide distributed
transactions, cross-store edges/joins, `USE CATALOG` syntax,
automatic project initialization or historical graph reads. Those are
separate milestones. Routing/resolution alone introduces no database format,
WAL or OCC change.
The separate [bounded copy API](CATALOG_COPY.md) now supplies durable receipts;
`session.apply_copy` preserves the target attachment's permissions and lifetime.

## Existing handles and explicit ownership

```python
from okto_grafx import CatalogPathPolicy, CatalogSession, connect

policy = CatalogPathPolicy(allowed_roots=("/srv/projects",))
with connect("/srv/projects/one") as first, connect("/srv/projects/two") as second:
    with CatalogSession(first, owned=False, policy=policy, read_only=False) as session:
        session.attach_handle(second, alias="reference", owned=False)
        with session.begin("write", catalog="main") as tx:
            tx.execute("CREATE NODE TABLE Item(id INT64, PRIMARY KEY(id))")
        session.use("reference")  # only affects future begins
```

Use existing absolute Windows paths such as `D:/Projects` instead on Windows.
`owned=False` means the caller closes the handle after the session. `owned=True`
transfers lifecycle responsibility **only on successful attachment**. Do not keep
using an owned handle outside the session. Borrowed handles must remain open for
the session's dependent operations. Permissions constrain this session, not a
caller's separate direct access to its own database handle.

`CatalogSession(main, *, owned, policy, read_only=True)` requires explicit
ownership. `main` is reserved. Aliases are case-normalized ASCII identifiers,
1–64 characters, starting with a letter. Duplicate aliases, database UUIDs
(including physical clones), and paths refuse. Inventory via `catalogs()` is
sorted and contains alias, UUID, permission, ownership and active transaction
count, including reserved begins. It omits filesystem paths.

`CatalogSession.open(path, *, policy, read_only=True)` and
`session.attach(path, *, alias, read_only=True)` own their acquired handles. They
require an existing directory containing a Grafx store and never initialize a
missing store. Read-only opens preserve native fail-closed admission: an
uncheckpointed store may refuse, requiring explicitly writable recovery and
checkpoint by the operator. Native reader coordination can leave empty lock
files; this is not graph/WAL recovery. **Writable open/attach can invoke native recovery**.
Failed attachment releases the acquired handle but does not roll back effects
of an explicitly authorized writable open. For custom connection options,
connect explicitly and use `attach_handle`.

## Transactions, concurrency and cleanup

`session.begin(mode="read", *, catalog=None, metadata=None)` returns the ordinary
native `Transaction`. Its reads, writes, cursors, metadata and commit use exactly
one database. `session.use(alias)` only changes the default for future begins.
It cannot redirect an existing transaction. Read-only session permission cannot
be escalated by asking for a write transaction, nor can a physically read-only
handle grant a writable attachment.

A short metadata lock reserves a slot before calling native `begin` **outside
that lock**. Execution and commit also run outside it. A slow store does not
introduce a session-wide writer lock; native per-store multi-reader/writer
fencing, OCC and snapshots remain unchanged. Independent stores do not acquire a
shared atomic snapshot. In-memory handles can be attached explicitly.

`detach(alias)` refuses while a transaction or begin reservation depends on that
catalog. It cannot detach `main`. Detaching the default resets it to `main`;
borrowed handles remain open. Finish transactions/cursors before detach.
`close()` refuses before closing anything if a transaction, attach or detach cleanup is active;
it does not silently roll it back. Otherwise it attempts every owned handle,
raises the first cleanup failure and records later failures as exception notes.
The session is terminal after this attempt; retained caller handles and native
diagnostics provide the recovery route for a failed close. Context cleanup never
replaces a body exception. Close is idempotent after completion.

## Path and workspace configuration

These options are operation/session-local, not `ConnectOptions`, persistent
capabilities or Pulse settings. No environment variable is consulted implicitly.

| Option | Default / bounds | Meaning |
| --- | --- | --- |
| `CatalogPathPolicy.allowed_roots` | Required tuple, 1–64 existing directories | Local filesystem allowlist |
| `max_catalogs` | 16; 1–4096 | Includes `main`, pending attaches and detach cleanup |
| `max_active_transactions` | 64; 1–4096 | Active transactions plus pending begins |
| `WorkspacePolicy.paths` | Required `CatalogPathPolicy` | Shared path validation |
| `markers` | `(".git",)`; 1–16 unique single names | Nearest ancestor with any marker wins |
| `max_parent_steps` | 8; 0–64 | Parent edges; cwd is checked even at zero |
| `allow_cwd` | `False` | Fallback to supplied cwd when no marker found |
| `allow_user_store` | `False` | Permits an explicitly supplied, distinct user store |
| `project_store_name` | `.grafx/store` | Normalized relative path beneath selected root |

Paths reject UNC/remote URLs, symlinks and Windows reparse points/junctions,
including ancestors. Windows drive-relative paths, trailing-dot/space aliases and alternate data streams
also refuse. No implicit `~` expansion occurs. Relative inputs use the process
cwd; pass absolute paths for reproducibility. Each begin revalidates the path
and captured directory identity. This is an allowlist/correctness policy, **not
an OS sandbox**: callers must control filesystem permissions and prevent hostile
concurrent namespace replacement. Native storage validation remains authoritative.

```python
from okto_grafx.workspace import WorkspacePolicy, resolve_workspace

workspace = resolve_workspace(
    policy=WorkspacePolicy(policy, max_parent_steps=4),
    project_root="/srv/projects/one",
)
assert workspace.source == "root"
# Resolves /srv/projects/one/.grafx/store; does not create or open it.
```

Precedence: `explicit_store` > `project_root` > bounded marker search from supplied
`cwd` > opted-in cwd fallback. Explicit store defaults its reported root to its
existing parent unless `project_root` is also supplied. Stores need not exist yet;
selected roots must exist. Higher-precedence selection ignores cwd inputs.
Discovery never leaves an allowed root, reads marker contents or follows links.
A missing marker never grants permission to use the user's home directory.

Frozen `ResolvedWorkspace(root, project_store, user_store, source)` contains
absolute paths. `source` is `explicit`, `root`, `marker` or `cwd`. User/global scope
has no implicit default even when enabled; supply `user_store` explicitly.
No agent manifest parsing, automatic attach or file writes occur. Resolution
produces a path decision, not an opened store identity.

## Typed failures

`GrafxConfigurationError` reports invalid aliases, duplicate identity/path,
denied roots, missing stores/roots, invalid limits and scope policy violations;
`field` identifies the rejected input/bound. `GrafxTransactionStateError` reports
closed sessions/handles, in-use detach/close and read-only permission violations.
Native open/begin errors propagate without converting a failed source to empty
results. Transactions retain ordinary rollback and uncertain-commit semantics.
