# ADR GX-003 — Commit identity and bounded generic metadata

Status: accepted semantic contract; GX-CAP-1 encoding/public API pending.
Source: complementary plan §6 in full. Precedes temporal publication.

## Decision

A CommitId orders durable logical commits within one store. It is distinct from
an ephemeral transaction identifier and from a page LSN; cross-store comparisons
have no ordering meaning. Gaps are allowed, reuse after durability is forbidden.
The externally meaningful reference includes store identity. Existing physical
commit LSN reuse is permitted only after proving uniqueness, monotonicity,
retention independence, restore/import behavior and crash replay semantics.
This ADR does not select a new counter or a WAL-byte mapping without that proof.

Allocation/publication uses the existing serialized commit protocol, not a new
global lock, retained writer lease or single-writer process. Both OCC passes and
snapshot guarantees remain mandatory. A crash before ACK may leave a committed
outcome discoverable by identity; callers may not assume every exception rolled
back, nor retry a semantic operation blindly. Metadata is not an idempotency key.

CommitMetadata is optional and contains generic actor/origin/correlation_id/reason
and bounded canonical attributes. Admission must bound total encoded bytes,
key count, nesting/types and individual strings before durable effects.
It must snapshot caller-owned inputs at begin so later mutations cannot change
the committed payload. Concrete limits and canonical encoding belong to GX-CAP-1;
no public knobs or unbounded placeholder API are introduced here.
Actor text is supplied provenance, not authenticated authority. Agent identity
authentication belongs to its configured identity provider, outside the database.

Metadata and the logical commit are atomic, replay-idempotent and queryable through
typed lookup/history/changefeed. Raw WAL is not the public audit API. No observer,
serialization or metadata validation may first fail after the durability barrier
and misreport an otherwise acknowledged logical commit as rolled back.

The writer samples observed_at via a clock port. Publication assigns
ordered_at = max(observed_at, prior ordered_at + one representable tick);
clock_adjusted records ties/regressions. CommitId remains the canonical order.
Overflow must refuse before durable publication, not wrap or reuse an identity.

## Migration, privacy and required evidence

Existing stores need an explicit legacy-history boundary: do not invent actors
or historical commits that were never recorded. Export/import must preserve a
qualified source identity or return an explicit mapping; restore/fork identity
rules must be specified before enabling the capability. A backup copied to a new
location is not automatically a second independent writer to the same identity.

Metadata is not a place for prompts, secrets or full payloads. Default telemetry
contains only counts/byte totals/high watermark and bounded refusal reasons.
Sanitization does not promise to recognize every secret embedded in arbitrary text.

Required GX-CAP-1 gate: multiprocess ordering/uniqueness, old snapshot isolation,
metadata mutation/oversize/invalid-type rejection before I/O, clock tie/regression/
overflow, all durability/publication crash cuts, repeated recovery, n-1 refusal,
lookup/verify corruption, export/import mapping, and metrics-on/off parity.
No new format bit is allocated by this contract-only milestone.
