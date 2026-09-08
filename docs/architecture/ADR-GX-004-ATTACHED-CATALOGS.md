# ADR GX-004 — Named catalogs with single-store transactions

Status: accepted semantic contract; GX-CAP-2 implementation pending.
Source: complementary plan §8; agent-first §§6, 14 in full.

## Decision

CatalogSession binds immutable aliases to explicit store handles/identities.
Attached catalogs are read-only by default. Each transaction pins exactly one
catalog at begin; changing the session default cannot reroute that transaction.
There is no distributed commit, cross-store physical edge, hidden synchronization,
automatic user-store enablement or filesystem path derived from retrieved text.

The core receives explicit paths/handles. Workspace resolution and project/user
conventions are optional adapters: explicit configuration, explicit harness root,
bounded allowed-marker search, then policy-permitted cwd. User/global is opt-in.
Root allowlists, canonicalization/symlink policy and platform behavior are explicit.
Aliases are unique ASCII; main is reserved; ambiguous identity/path/alias refuses.
Remote URLs and unrestricted upward searches are out of scope.

Attach validates before publishing the alias. Failed attach releases acquired
resources. Detach must not invalidate a live transaction silently: an in-use
catalog refuses until its dependent operations are closed. Session close attempts
all owned handles and preserves the first failure with evidence of later failures.
Externally owned handle ownership must be declared, never guessed.

Federated results, when introduced, carry one snapshot token per catalog and
consistency=independent. Failure cannot be presented as an empty successful source;
partial results require explicit policy and per-source failure disclosure.
First delivery selects catalog at session/begin, without cross-catalog Cypher.

## Copy/promotion

Copy reads a pinned source snapshot, materializes a bounded logical package with
checksum, and writes one explicit target transaction via existing logical transfer.
Receipt binds source store/commit, target, selection hash, conflict policy,
idempotency key and target commit. Same key with different input must refuse;
same input after an ambiguous ACK returns the recorded outcome.
Source mutation after capture cannot silently change the replayed package.
No relationship may be imported without resolved endpoints.
No cross-store atomicity is claimed, including if source and target share a process.

## Required evidence

Two catalogs/processes; reserved/duplicate/non-ASCII aliases; same-store aliasing;
Windows and POSIX path attacks; read-only mutation refused before disk effects;
transaction pin despite use/default changes; close failure aggregation; detach
during reads/writes; copy crash before/after target durability and repeated replay;
conflict-policy refusal; no physical cross-store edge; explicit federated snapshots.
Publicly exposed UUIDs do not grant authority or disclose private paths.
