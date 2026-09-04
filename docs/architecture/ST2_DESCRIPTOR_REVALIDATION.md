# ST-2 — Descriptor identity revalidation

`LocalStorageDevice` keeps file descriptors open to avoid reopening a file for every operation. A
cached descriptor is useful only while the logical name in the database directory still names the
same physical file. If another process replaces or removes that name, an old descriptor can keep
reading an inode that is no longer visible in the directory and, on some filesystems, can write to
that detached inode. ST-2 controls how often the local adapter proves that identity.

This is an operational performance policy, not an on-disk format, isolation level or durability
level. The public option is:

```python
from okto_grafx import connect

# Default: prove every cached descriptor hit.
with connect("./grafx.db", descriptor_revalidation="strict") as strict_db:
    ...

# Opt-in: amortize proofs for the closed set of canonical paged/log files below.
with connect("./pulse.db", descriptor_revalidation="generation") as pulse_db:
    ...
```

Only the values `"strict"` and `"generation"` are accepted and they are canonicalized to built-in
strings. The default is `"strict"`.

## The two modes

| Mode | What a cached hit proves | Advantages | Costs and risks |
|---|---|---|---|
| `strict` *(default)* | On every cached hit, the directory entry and the open descriptor are compared by physical identity. A missing or replaced name is observed immediately on that next hit. | Strongest protection against out-of-protocol rename, replacement or removal; safe default for general deployments, maintenance and investigation. | Repeats namespace/descriptor identity system calls on hot files. |
| `generation` *(opt-in)* | Control records, `grafx.meta` and every non-whitelisted name remain strict. A whitelisted canonical file is proved on first open/first hit in an adapter generation and reuses that proof until the adapter invalidates it. | Amortizes repeated identity system calls on hot heap, catalog, index and WAL files without changing their bytes or transaction protocol. | A replacement or removal performed outside the Grafx protocol after a proof can remain undetected until the relevant stamp is invalidated, the adapter-wide generation advances, or the database is reopened. If no such event occurs, the delay is unbounded. Reads can continue from an old inode and writes can target an inode no longer named by the directory. |

`strict` is the recommendation unless the deployment can satisfy every `generation` prerequisite
in [When to use `generation`](#when-to-use-generation). `generation` is not a safer or weaker WAL
mode; it is only a different cache proof schedule.

## Closed whitelist

`generation` applies only to these canonical logical names:

- `heap.dat`;
- `catalog.dat`;
- `index/<identifier>.idx`, where `<identifier>` is the normal Grafx schema identifier: 1 through
  128 ASCII characters, starts with a letter or `_`, and then contains only letters, digits or `_`;
- `wal/<segment>.wal`, where `<segment>` is exactly twelve ASCII decimal digits and its numeric
  value is from `000000000001` through `999999999999`.

The whitelist is fail-closed. Nested index paths, malformed identifiers, WAL segment zero,
non-ASCII digits, temporary/staging names, `index_orphan/**`, control records, `grafx.meta`, reader
registrations, leases, ledger/quarantine files and every unknown name remain in `strict` behaviour
even when the selected mode is `generation`. A caller cannot extend this set by configuration.

## Strict control records and bounded image reads

Control records never use generation-amortized identity stamps. In both modes, every actual storage
operation on `control/**` proves that the cached descriptor still belongs to the logical name. A
v2 two-slot record has one immutable header page and two independently checksummed slot pages. Its
read path fetches that fixed three-page image through one adapter-only fused read instead of an
independent `exists` followed by `read_log`, or three separate `read_page` calls. The local adapter
walks the exact-case namespace once, retains the final no-follow `stat` observation and compares its
`(st_dev, st_ino)` with `fstat` of the warm descriptor. A miss/stale handle still enters the ordinary
open/reprove loop. This is one strict physical-identity proof; it is not a skipped proof.

The fused `read_log_if_exists` capability is deliberately not part of the frozen `StorageDevice`
port. Control records and the read-only wrapper use it only when the concrete adapter type declares
the method itself; a wrapper that merely forwards unknown attributes through `__getattr__` keeps the
literal `exists` then `read_log` sequence and propagates a disappearance after `exists=True`.
Absence is `None`, while an empty present file is `b""` and remains a present legacy/corrupt image.

The bounded read asks for the format-derived image size plus one byte and relies on the port's
fill-until-EOF rule: a conforming adapter returns the sentinel byte when it exists rather than an
arbitrary short chunk. Exactly three pages is decoded as v2. A short image is
accepted as legacy v1 only after `file_size` confirms that the
current logical file reports the same short length. A supported atomic replacement between these
calls may select either complete old or new bytes; a length disagreement fails closed, and the
strict proof makes the next descriptor hit follow the current name. An image longer than v2 is read
in full for a caller that needs its v1 payload and is never silently truncated to a valid-looking
v2 prefix. Migration publication only needs its shape, so it does not materialise an oversized v1
payload that it will immediately replace. Once a read has been classified as v2, every corruption
retry requires the exact three-page length and refuses a short or oversized replacement, including
an oversized image with a valid-looking v2 prefix. Header/database binding, slot CRC, generation
monotonicity and the rule that two populated slots cannot claim the same generation are unchanged.

A warm publication remains three independent storage operations: one fused bounded image read, one slot
page write and one durability barrier. Each operation receives its own strict descriptor proof in
both modes. The batching does not combine a read with a write or barrier, does not cache control
bytes across calls and does not acknowledge a generation before the existing write-plus-barrier
sequence succeeds. It bypasses the buffer pool exactly as the former direct page reads did, so it
does not add a stale buffer-pool view or alter WAL, OCC, lease or checkpoint ordering.

## Generation and invalidation contract

The “global generation” is global only inside one `LocalStorageDevice` instance. It is
process-local, non-persistent and not shared with another Grafx or Pulse participant. It is not a
WAL LSN, CSN, lease epoch, transaction generation or read-view token.

Each whitelisted cached descriptor carries the adapter generation in which its identity was last
proved. Global advancement and directed per-file invalidation are different operations. The exact
transitions are:

| Buffer-pool event | Adapter-wide generation | Per-file proof stamps in `generation` mode |
|---|---|---|
| `invalidate(None)` | Advance once, after invalidation preflight succeeds and before a dirty frame can be written back. | Every whitelisted descriptor must prove identity on its next hit. |
| Full foreign read-view refresh without a bounded delta | Advance once through the full `invalidate(None)` path. | Every whitelisted descriptor becomes stale. |
| A CE-3 partial proof whose expected baseline mismatches, forcing `every_file=True` | Advance once after the clean-only refusal preflight and before frames leave the old view. | Every whitelisted descriptor becomes stale. |
| `invalidate(file)` | Do not advance. | Remove only that file's stamp before possible write-back; the file is proved on its next descriptor hit. |
| Same read-view token | Do not advance. | With no unfenced target, change nothing. If the caller names an unfenced file, remove only that file's stamp before its frames move. |
| A proved own-publication read view | Do not advance. | With no unfenced target, change nothing. If the view must discard an unfenced file, remove only that file's stamp. |
| A valid bounded CE-3 partial foreign refresh | Do not advance. | After the dirty-target refusal preflight, remove only the stamps for the exact names in `changed_pages`, `changed_files` and the unfenced-file target. Unrelated proofs remain valid. |
| `read_fresh_page(file, page)` | Do not advance. | Remove only `file`'s stamp before the detached device read used by an optimistic certificate. |
| A fenced page-0 write | Do not advance. | Remove only `file`'s stamp before the fresh device read and sequence CAS, and before write-back. |

A failed invalidation preflight does not move frames or descriptor identity state. Closing and
reopening the adapter closes its descriptors and discards every process-local proof, so a reopened
database necessarily opens the current names again.

The non-advancing cases are deliberate: they identify either a same-picture/own-publication view or
an exact logical name that needs a directed proof, not evidence that the whole filesystem namespace
may have changed. A directed invalidation proves only its named file and does not refresh unrelated
whitelisted names. Consequently, these cases also define the risk boundary: an external replacement
of some other whitelisted name is not made safe merely because one of those read-view events
happened.

## Falsifiable safety premise and future-change gate

The supported normal Grafx protocol does **not** republish `heap.dat` or `catalog.dat` under their
live names. Their ordinary lifetime mutations are paged writes, not a rename that silently binds the
canonical name to a new physical file. Normal cross-process safety still comes from directed
`read_fresh_page` observations, page/header certificates, pre-WAL validation and page-0 CAS checks;
where a path needs a fresh descriptor proof, it invalidates/revalidates that name at its own fence.
Generation mode only amortizes the bulk descriptor-cache identity probes between those boundaries.
It is not the proof used by a certificate or CAS.

This premise is intentionally falsifiable. Any future code path that can republish, replace or
rebind a canonical paged name while another participant may hold it open MUST do one of the
following before that path ships:

1. add a directed descriptor-identity proof at the corresponding fence/certificate, before stale
   bytes can be read or a dirty frame can be written; or
2. make that name/path decline generation revalidation and use `strict` behaviour.

A future implementation that republishes a paged name live and does neither invalidates the ST-2
safety premise; benchmark improvement cannot justify accepting it. A regression for that path must
prove the directed revalidation or the fail-closed fallback. This gate applies to every canonical
paged name, not only heap and catalog.

## When to use `strict`

Use `strict` when selecting a mode for the first time, when filesystem activity outside Grafx/Pulse
cannot be ruled out, or during recovery, maintenance, migration and forensic work where directory
provenance is uncertain. It is also the appropriate mode for mixed-trust hosts and for workloads in
which descriptor-identity system calls are not a demonstrated bottleneck. It has no additional
deployment prerequisite beyond Grafx's normal local-filesystem contract.

There is no correctness or durability reason to avoid `strict`. Consider not selecting it only when
a representative measurement shows that repeated descriptor proofs materially constrain the target
workload **and** every prerequisite in the next section is enforced. In that case `generation` can
trade immediate detection of out-of-protocol name replacement for lower hot-path overhead. Do not
switch merely because `generation` exists or because a microbenchmark improves in isolation.

## When to use `generation`

Use `generation` only when all of the following are true:

1. The database directory is exclusively managed by Okto Grafx and the Okto Pulse processes that
   embed it. No other writer renames, replaces, deletes, restores, rotates or synchronizes files in
   that directory while any participant is open.
2. Every operational tool treats an open database directory as owned by Grafx/Pulse. Backups may
   read it only under the application's supported consistency procedure; restores and file-level
   synchronization happen only after all participants are closed.
3. The filesystem provides the local locking and atomic-replace semantics required elsewhere by
   Grafx. Shared/network filesystems with different semantics remain unsupported.
4. The saved namespace-system-call cost matters for the measured workload and the operator accepts
   the explicit delayed-detection risk for the whitelisted files.

This makes a controlled Pulse deployment, in which Pulse and Grafx are the only processes that
modify the database directory, the intended opt-in case.

Do **not** use `generation` when any external agent may mutate the directory, including a live
restore, file-by-file replication/synchronization, manual replacement, snapshot rollback mounted
over an open directory, or a storage/backup tool that swaps canonical files in place. Do not use it
as a workaround for a filesystem that fails Grafx's locking or atomic-replace requirements. Use
`strict` for forensic work, recovery/maintenance workflows with uncertain directory provenance,
mixed-trust hosts and any deployment whose ownership model cannot be proved.

Neither mode detects arbitrary in-place writes made by external code to the same physical inode:
descriptor identity has not changed. External mutation of an open database is outside the supported
storage contract in both modes. Checksums and `verify()` remain damage-detection mechanisms, not
authorization for such mutation.

## Coexistence and scope

The selected mode belongs to a local adapter instance and is not stored in the database. `strict`
and `generation` participants can therefore coexist on the same on-disk format and transaction
protocol. A strict participant continues to prove every hit; it does not upgrade, advance or
otherwise compensate for a generation participant's process-local proofs. If any open participant
uses `generation`, the directory must satisfy the exclusive Grafx/Pulse ownership requirements for
that participant.

The open `Database` handle exposes the effective process-local choice through the read-only
`database.descriptor_revalidation` property. Hosts should compare this observed value with their
requested policy before pooling or publishing the handle. It is intentionally absent from
`Database.identity`: the identity record is durable and shared, while this policy may legitimately
differ between processes opening the same on-disk database.

For `connect(":memory:")`, the option is accepted and validated but operationally inert:
`MemoryStorageDevice` has no operating-system descriptor identity to revalidate.

With the default registry, bootstrap passes the option to `LocalStorageDevice`, including the local
device wrapped for `read_only=True`. With a caller-supplied `PortRegistry`, configuration validation
still occurs, but `descriptor_revalidation` does **not** reconfigure the supplied storage adapter.
Custom storage remains trusted host code and is responsible for its own descriptor/cache semantics.
The frozen `StorageDevice` signature set is unchanged. Its existing `read_log` member is the
bounded, non-mutating byte-range read over the unified namespace (legacy control migration already
used it this way). It must fill the requested range unless it reaches the actual EOF, so a custom
adapter must also allow and fully serve that read over an allocated control file.
An adapter may optionally expose
`invalidate_descriptor_identity(file: str | None = None)` to consume the buffer pool's cache-only
invalidation signals; wrappers that want to preserve the optimization must forward it.

## Guarantees that do not change

ST-2 does not change file contents, the on-disk format, the first or second OCC validation, snapshot
isolation, writer leases, multiwriter/multireader coordination, WAL append/barrier ordering,
checkpoint durability or BR-10 WAL-retention/recycling horizons. It adds no durability bypass and
makes no write acknowledgement earlier. A descriptor-identity check or generation stamp is not an
OCC proof and cannot replace one.

Choosing `generation` therefore cannot fix a WAL, OCC, recovery, locking or BR-10 problem. Choosing
`strict` cannot make an unsupported external in-place mutation safe. The only choice here is how
often a cached logical name is re-proved against its open physical descriptor.

## Final acceptance evidence — 2026-09-01

The safety gate passed in both modes. The final F4 DDL matrix proves that a foreign canonical-index
replacement is observed before WAL append, with the losing writer refused and WAL/LSN unchanged.
The reader-retention F4 keeps a real reader pinned while checkpoint/recycle advances below its
horizon, kills an unflushed writer and recovers all durable rows on a cold reopen. The authenticated
full Grafx regression traversed 11,357 nodeids with exit 0; Ruff, compileall and diff-check passed.

The Pulse-oriented `generation` structural gate used the fixed operation digest
`c994255b0bf695040c972ce339cc5d580ec253d2146674664e7722cf6b5a7f81`, `continuous` mode and five
samples for each of 12 families. Relative to its full-route `generation` control, `_still_names`
remained 424, `os.lstat` remained 4,137 and `os.stat` fell from 2,393 to 1,727. The frozen limits are
respectively
`<500`, `<8,000` and `<2,000`. Logical work did not move: `_read_page=323`,
`read_fresh_page=146`, `write_page=96`, 123 authenticated binding acquisitions and 148 statements
in both artifacts. Board/Grafx statement fences remain present; the optimization removes only the
second resolver component walk after the binding has already authenticated the route and the exact
pool-pinned database has been re-admitted. Generic and Global routes remain unchanged, and a real
symlink/junction test pins alias refusal on the optimized path.

The final evidence names Grafx `f0b55b7b6facc916118f342c774cb06e56bf17e3`, Community
`050ced9b79533d50efed453d53ed450984f75cf3` and Core
`ccc1f345ece1db89a274cfdd634bd4da27028f63`. Artifact:
`D:\GrafxBenchEvidence\st2-pinned-route-20260901-final-a01\st2-pinned-route-generation-profile-pf5.json`,
969,630 bytes, SHA-256
`384a7722ff6772a2e89ca95225ab759ec5c7cab5af9939f405ef7b5ec2802aae`. Because the report records
`machine_idle_asserted=false`, these are structural acceptance counts, not a throughput claim. The
paired control artifact and full reproduction provenance are recorded in
[`docs/PERFORMANCE.md`](../PERFORMANCE.md).
