# Physical backup and offline replacement restore

0.0.6 native system-time history is included as `system-history.dat` together with
catalog activation, horizons and durable pins. Restore verifies temporal lineage
and current-row agreement; it does not strip history or silently change UUIDs.
See [history operations and retention](SYSTEM_TIME_HISTORY.md).
The same file carries optional authenticated temporal access pages and compacted
logical extents (required bits 17/18). Backup/restore retains these capabilities;
an unused physical tail after interrupted compaction is not live history. Full
history/index verification remains mandatory; restore does not reconstruct
missing authoritative pages from an unqualified cache.

Available in the **0.0.5 development line**, through `okto_grafx.backup`.
This is a bounded local physical backup, not logical export or an independently
writable fork. No application service, Pulse adapter, or agent is required.

## Usage

```python
from okto_grafx import connect
from okto_grafx.backup import create_backup, restore_backup

with connect("./graph") as db:
    # This handle must have no open transaction. Other participants may stay open.
    report = create_backup(
        db, "./graph-backup-001",          # must not exist; parent must exist
        max_bytes=256 * 1024 * 1024,
        max_capture_seconds=5.0,
        capture_mode="disk",              # private temporary-file capture
    )
    print(report.database_uuid, report.checkpoint_lsn, report.files, report.bytes)

# STOP ALL original participants before this step. Do not resume the original
# alongside the replacement: both carry the same database/commit identity.
restored = restore_backup(
    "./graph-backup-001", "./graph-restored",  # new, disjoint directory
    confirm_original_offline=True,
    max_bytes=256 * 1024 * 1024,
)
with connect(restored.destination) as db:
    assert db.verify("all").findings == ()
```

For a non-default page size, use that same `page_size` when opening the restored
directory. Backup/restore themselves obtain it from the identity/manifest. An
existing `partitions_per_table` value is preserved, not reconfigured.

## Exact guarantees and concurrency boundary

1. Validate a new, disjoint destination and require a writable local source handle.
2. Execute the ordinary checkpoint protocol. Its existing writer/commit/WAL fences
   cover the capture: committed effects are applied and barriered; the published
   checkpoint equals the captured committed LSN; WAL cannot be recycled underneath
   the copy. First/second OCC and all ordinary writer admission rules are unchanged.
3. Capture bounded file bytes in 1 MiB chunks into a private temporary spool
   beside the destination (default `disk`), or a RAM buffer (`memory`), while that fence remains held.
   Other participants may read/stage work; **commit publication waits during this
   capture**, including spool I/O. This is not a no-pause hot backup. A caller should size its
   budgets and other participants' timeouts accordingly.
4. Release the source fence before writing the artifact, reading it back, verifying hashes,
   or running complete verification. Concurrent source commits can now advance;
   they do not change the already captured cut. No live reader/lock files are copied.
5. Materialize a private verification copy from the written artifact. Open it
   read-only, check exact UUID and checkpoint, and require `verify("all")` to have
   no findings. Only then publish the artifact directory with no-replace semantics.
6. Restore validates the manifest and every object's exact length/SHA-256, materializes
   a private new directory, and performs the same observational verification. It
   releases **only the copied writer lease**, retaining epoch lineage and database
   identity, then verifies again before publishing the new directory.

The source may acquire a newer checkpoint even if a later backup budget, output IO
or verification fails. Failure never authorizes deleting/rebuilding source data.
The source API cannot prevent manual filesystem copies or a dishonest offline
assertion; `confirm_original_offline=True` is an operator obligation, not automatic
detection that every original participant is stopped.

## API and parameters

| Function / parameter | Meaning |
| --- | --- |
| `create_backup(database, destination, *, max_bytes=268435456, max_capture_seconds=5.0, capture_mode="disk")` | Capture a verified artifact from a writable default-local-storage handle. Custom storage wrappers and read-only handles are refused. No source transaction may be open on this handle. |
| `restore_backup(backup, destination, *, confirm_original_offline=False, max_bytes=268435456)` | Restore for offline replacement only. The exact boolean `True` assertion is required. Original and existing destination data are never overwritten. |
| `max_bytes` | Positive maximum total database payload bytes. Includes retained WAL and copied index files. Bounds payload, not total Python RSS. Source capture, artifact copy/readback and restore use 1 MiB chunks; metadata and full verification have separate costs. |
| `capture_mode` | `disk` defaults to temporary storage, avoiding a whole-payload RAM copy. `memory` keeps the full captured payload in RAM and avoids spool disk writes while fenced. Use disk for larger cuts with adequate local free space, memory for small cuts when RAM is available and spool latency matters. Neither is resumable or no-pause. |
| `max_capture_seconds` | Positive finite capture budget, checked between source reads and after spool flush. Does not include prerequisite checkpoint or artifact verification and cannot interrupt a single blocked OS/device call. |
| destination | New directory with an existing parent, outside source/backup directory trees. No overwrite option. |
| `BackupReport.destination` | Absolute promoted directory. |
| `BackupReport.database_uuid` | Original UUID as lowercase hexadecimal. |
| `BackupReport.checkpoint_lsn` | Complete committed checkpoint represented by the artifact. |
| `BackupReport.files`, `.bytes` | Database payload file count and bytes; not manifest or temporary verification copies. |

Additional fixed bounds: 10,000 payload files; 4 MiB manifest. A workload exceeding
these limits needs another backup design or an explicit byte budget increase when
only `max_bytes` is exceeded. Do not raise budgets blindly on a memory-constrained host.

Disk mode needs temporary free space for the captured spool, artifact and verification
copy (approximately three payload copies at peak, besides the source). Spools are
unpublished and cleaned on ordinary success/refusal. Artifact publication, manifest
format, identity checks and same-UUID offline restore rules are unchanged.

## Artifact, identity and integrity

The artifact contains `manifest.json` and numbered `objects/` files, **not a database
root that can be opened as a second writer**. The closed manifest records format
`okto-grafx-physical-1`, UUID, page size, partition count, checkpoint LSN, and canonical
file/object mappings with lengths and SHA-256. Duplicate JSON keys, traversal,
duplicate names, invalid types, unknown format and over-budget content are refused.
Validation remains enabled under `python -O`.

Included payloads are metadata, heap, catalog, commit-history files, durable commit
and writer-epoch control records, first-open completion record, and index/WAL files.
Index inventory is physical: unreferenced index generations may also consume backup
budget. Live reader registrations, locks and temporary staging files are excluded;
application data, forensic/audit archives and arbitrary files outside this native
inventory are not part of this artifact.

All original commit IDs and provenance remain valid because UUID and journal bytes
are preserved. Restore is **replacement**, never a new independently writable
same-UUID database. Use a future logical export/import product for a fresh-UUID fork.
Hashes protect against accidental alteration, not a malicious actor who can replace
both manifest and objects. Store artifacts in appropriately protected storage.

## Failures and platform limits

Typed configuration errors cover invalid budgets/offline assertions. Operational
refusals use `GrafxRecoveryRefused` with `operation="physical_backup"` and reasons
such as `backup_budget`, `capture_timeout`, `manifest_invalid`, `object_mismatch`,
`identity_mismatch`, `checkpoint_mismatch`, `verification_failed`, `active_transaction`
or `destination_exists`. Native storage/recovery errors and OS publication errors
retain their types; `KeyboardInterrupt`/`SystemExit` propagate.

Interrupted work is never promoted as success. Handled failures clean only private
temporary directories created by the operation. Process death can leave
`.NAME.incomplete-*` directories; they are not the requested destination. Inspect
and remove those abandoned temporary directories only after proving the operation
is no longer running. No automatic repair or deletion of authoritative corruption
is performed. A publication durability failure can leave a complete destination
despite a raised error; inspect it instead of blindly retrying with overwrite.

Windows requests same-volume, write-through, no-replace directory publication via
[MoveFileExW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw).
Linux uses [renameat2(RENAME_NOREPLACE)](https://man7.org/linux/man-pages/man2/rename.2.html)
plus parent-directory `fsync`; unsupported hosts
refuse publication instead of silently weakening it. The current local acceptance
run is Windows/Python 3.13, not evidence of a completed Linux/platform matrix.

For an offline manual alternative, stop all participants, successfully close them,
copy the complete native directory, preserve its identity, and verify the copy.
Arbitrary copying of a live directory remains unsupported.
