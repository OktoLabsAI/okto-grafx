"""Reversible quarantine: a copy of the damage, kept so nothing has to be destroyed blind.

SPEC-M1 FR-10 and BR-1, CONTRACT.md section 6.1 and G6.

A quarantine entry is a directory holding a manifest and the bytes it describes::

    quarantine/<stamp>-<origin>-<offset>-<length>/manifest.json
    quarantine/<stamp>-<origin>-<offset>-<length>/<payload file>

Four rules govern everything in this module, and each is a rule about NOT losing something.

**Capture copies; it never moves.** The source is read and written elsewhere. No sanctioned
operation of this engine moves, renames or deletes ``heap.dat``, ``catalog.dat`` or ``index/*``
(G6, BR-1), and :meth:`QuarantineStore.capture` never removes its source at all -- destroying the
original is the caller's separate, deliberate act, taken only after the copy is durable. Proven by
``test_a_capture_never_touches_the_file_it_copied_from`` and, for the restore side,
``test_a_restore_never_writes_over_a_main_data_file``.

**A capture is idempotent.** The name of an entry ends in a suffix derived from the origin, the
offset and the length, so a second recovery pass over the same damage RECOGNISES the copy it
already made and reuses it. Recovery can therefore be interrupted and re-run without the
quarantine growing a duplicate of every range each time. Proven by
``test_capturing_the_same_range_twice_returns_the_entry_that_already_holds_it`` and
``test_recovery_interrupted_after_the_quarantine_does_not_duplicate_the_entry``.

**Nothing overwrites evidence.** The manifest is written once. A restore leaves a numbered
receipt beside it. A capture whose entry already exists with the same digest is a no-op; one
whose digest DISAGREES is a refusal, not a replacement -- two different ranges cannot both be the
truth about one origin, and picking either silently is how the evidence stops being evidence.
Proven by ``test_capturing_a_range_whose_bytes_changed_refuses_rather_than_replacing_the_copy``
and ``test_two_restores_leave_two_receipts_and_neither_replaces_the_other``.

**Restore refuses the files the engine may never overwrite.** Putting quarantined bytes back is
an operator act, and the one target it can never have is a main data file (G6).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
    GrafxPortNotConfigured,
    GrafxQuarantineError,
)
from okto_grafx.domain.ids import NO_LSN, Lsn
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricsSink
from okto_grafx.domain.ports.storage import StorageDevice
from okto_grafx.domain.recovery.manifest import (
    MANIFEST_FILE_NAME,
    RESTORE_RECEIPT_PREFIX,
    QuarantineManifest,
    RestoreReceipt,
    entry_suffix,
    sanitize_name,
    stamp_of,
)
from okto_grafx.domain.recovery.retry import is_retryable
from okto_grafx.engine.metrics_catalog import metric

__all__ = [
    "COPY_ATTEMPTS",
    "MAX_CAPTURE_BYTES",
    "PROTECTED_FILES",
    "PROTECTED_PREFIXES",
    "QUARANTINE_DIRECTORY",
    "QUARANTINE_ENTRIES",
    "QUARANTINE_METRICS",
    "STORAGE_PORT_METHODS",
    "CLOCK_PORT_METHODS",
    "METRICS_PORT_METHODS",
    "QuarantineInventoryItem",
    "QuarantineInventoryState",
    "QuarantineEntry",
    "QuarantineStore",
    "RestoreReport",
    "entry_names_of",
    "is_protected",
]

QUARANTINE_DIRECTORY: str = "quarantine"
"""Where quarantined copies live inside a database directory (CONTRACT.md section 6.1)."""

PROTECTED_FILES: frozenset[str] = frozenset(
    {"heap.dat", "catalog.dat", "grafx.meta", "control/commit.state"}
)
"""Files no generic quarantine/retirement door may overwrite, move, rename or delete.

``commit.state`` is reconstructed and atomically published only by the commit/recovery
protocol. Treating it as an arbitrary damaged coordination record lets the generic retirement
door erase the snapshot/checkpoint fence after a probe/capture TOCTOU.
"""

PROTECTED_PREFIXES: tuple[str, ...] = ("bootstrap/", "index/")
"""Protected derived indexes and the authority-bearing first-open protocol namespace.

Bootstrap staging is retired only while holding the first-open cross-process section and only
after proving that no published/transaction state exists.  A generic quarantine restore has
neither proof, so it may not manufacture or replace an intent/staging name behind that protocol.
"""

COPY_ATTEMPTS: int = 3
"""How many times a copy rides out a device condition its details declare retryable (A47)."""

MAX_CAPTURE_BYTES: int = 256 * 1024 * 1024
"""Largest range one capture copies, so a damaged length cannot ask for unbounded work."""

QUARANTINE_ENTRIES: str = "oktografx_quarantine_entries"
"""Gauge of how many quarantined ranges are being kept (CONTRACT.md section 9)."""

QUARANTINE_METRICS: tuple[MetricDescriptor, ...] = (metric(QUARANTINE_ENTRIES),)
"""Every metric this store emits, taken from the frozen catalogue by name and never invented."""

STORAGE_PORT_METHODS: tuple[str, ...] = (
    "exists",
    "create",
    "append_log",
    "read_log",
    "log_size",
    "list_files",
    "truncate_log",
    "durable_barrier",
)
"""The storage doors this store opens. A port missing one of them is refused at construction."""

CLOCK_PORT_METHODS: tuple[str, ...] = ("wall",)
"""The clock a capture is stamped with. Human-facing, per CONTRACT.md section 4.2."""

METRICS_PORT_METHODS: tuple[str, ...] = ("enabled", "register", "set_gauge")
"""The metrics doors this store uses."""


def is_protected(file: str) -> bool:
    """Return True when G6 forbids this engine from overwriting or destroying that file."""
    if not isinstance(file, str) or not file:
        return False
    normalised = file.replace("\\", "/").lstrip("./")
    if normalised in PROTECTED_FILES:
        return True
    return any(normalised.startswith(prefix) for prefix in PROTECTED_PREFIXES)


@dataclass(frozen=True, slots=True)
class QuarantineEntry:
    """One quarantined range: its directory, its manifest, and where its bytes are kept."""

    name: str
    manifest: QuarantineManifest
    payload_file: str
    manifest_file: str

    @property
    def directory(self) -> str:
        """Return the directory of this entry inside the quarantine."""
        return f"{QUARANTINE_DIRECTORY}/{self.name}"


QuarantineInventoryState = Literal[
    "complete",
    "incomplete",
    "corrupt_manifest",
    "unreadable_manifest",
    "missing_payload",
    "manifest_mismatch",
    "unexpected_layout",
]
"""Every conclusive state a read-only quarantine inventory can report."""


@dataclass(frozen=True, slots=True)
class QuarantineInventoryItem:
    """One disjoint group of files observed by a quarantine namespace inventory.

    ``files`` accounts for physical names from the one namespace snapshot; inventory items never
    follow names supplied by a manifest. ``complete`` means that the manifest, canonical payload
    and optional restore receipts have a self-consistent layout. Reading the payload remains the
    door that verifies its digest, so listing evidence does not become an O(total payload bytes)
    operation.
    """

    name: str
    state: QuarantineInventoryState
    files: tuple[str, ...]
    manifest_file: str | None = None
    payload_file: str | None = None
    manifest: QuarantineManifest | None = None
    detail: str = ""

    @property
    def entry(self) -> QuarantineEntry | None:
        """Return the legacy entry view only when this item is structurally complete."""
        if (
            self.state != "complete"
            or self.manifest is None
            or self.payload_file is None
            or self.manifest_file is None
        ):
            return None
        return QuarantineEntry(
            name=self.name,
            manifest=self.manifest,
            payload_file=self.payload_file,
            manifest_file=self.manifest_file,
        )


@dataclass(frozen=True, slots=True)
class RestoreReport:
    """What one restore put back, and where (FR-10: the restore is auditable)."""

    entry_name: str
    restored_to: str
    restored_bytes: int
    receipt_file: str
    digest: str


def _require_port(slot: str, instance: object, methods: Sequence[str]) -> None:
    """Refuse a port that cannot answer the doors this store opens (G5, fail-closed)."""
    missing = [name for name in methods if not hasattr(instance, name)]
    if missing:
        raise GrafxPortNotConfigured(
            f"The {slot} port of the quarantine is missing {', '.join(missing)}.",
            slot=slot,
            missing=tuple(missing),
        )


class QuarantineStore:
    """The quarantine of one database: capture, list, inspect, restore."""

    __slots__ = ("_storage", "_clock", "_metrics", "_directory")

    def __init__(
        self,
        storage: StorageDevice,
        clock: Clock,
        metrics: MetricsSink,
        *,
        directory: str = QUARANTINE_DIRECTORY,
    ) -> None:
        """Build the store over its three ports. Nothing touches the device until it is used."""
        _require_port("storage", storage, STORAGE_PORT_METHODS)
        _require_port("clock", clock, CLOCK_PORT_METHODS)
        _require_port("metrics", metrics, METRICS_PORT_METHODS)
        self._storage: StorageDevice = storage
        self._clock: Clock = clock
        self._metrics: MetricsSink = metrics
        self._directory: str = _validate_directory(directory)
        if self._metrics.enabled:
            for declared in QUARANTINE_METRICS:
                self._metrics.register(declared)

    @property
    def directory(self) -> str:
        """Return the directory quarantined copies are kept under."""
        return self._directory

    # --- capturing ---------------------------------------------------------------------------

    def capture(
        self,
        *,
        origin: str,
        offset: int,
        length: int,
        reason: str,
        detail: str = "",
        expected_lsn: Lsn = NO_LSN,
        payload: bytes | None = None,
    ) -> QuarantineEntry:
        """Copy a range into quarantine with a manifest, and return the entry that now holds it.

        The bytes are read from the origin when the caller does not supply them, so the copy is
        of what is actually on the device rather than of a sample somebody kept: C4's
        ``ScanFailure.sample`` is capped at 64 bytes for messages, and the evidence has to be the
        whole range.

        Capturing the same range twice returns the SAME entry, provided the bytes still digest
        the same. That is what makes an interrupted recovery safe to re-run. If the bytes have
        changed, the capture is refused rather than replacing what was kept -- overwriting the
        first copy would destroy the only record of the state that was found first.
        """
        origin_name = _require_text("origin", origin)
        start = _require_offset("offset", offset)
        size = _require_offset("length", length)
        if size > MAX_CAPTURE_BYTES:
            raise GrafxQuarantineError(
                f"A quarantine capture of {size} bytes is past the {MAX_CAPTURE_BYTES} one entry "
                "copies.",
                field="length",
                value=size,
            )
        body = (
            self._read_range(origin_name, start, size)
            if payload is None
            else _require_body(payload)
        )
        digest = hashlib.sha256(body).hexdigest()
        suffix = entry_suffix(origin_name, start, size)
        existing = self._find_by_suffix(suffix)
        if existing is not None:
            if existing.manifest.digest != digest:
                # The message names the origin THE ENTRY HOLDS, not the one that was asked for.
                # An entry name is built from ``sanitize_name``, which is deliberately lossy --
                # every character outside a small ASCII set becomes an underscore -- so two
                # different origins can land on one suffix. Reporting the requester's name back
                # at it would tell an operator the entry holds bytes from a file it does not,
                # and the one case where that message is read is the case where the two differ.
                held = existing.manifest.origin
                raise GrafxQuarantineError(
                    f"Quarantine entry {existing.name!r} already holds {existing.manifest.length} "
                    f"bytes from {held!r} at byte {existing.manifest.offset} with a different "
                    f"digest, and this capture is of {size} bytes from {origin_name!r} at byte "
                    f"{start}; the earlier copy is kept and nothing was overwritten.",
                    field="digest",
                    entry=existing.name,
                    file=held,
                    requested=origin_name,
                    offset=existing.manifest.offset,
                )
            return existing
        captured_at = float(self._clock.wall())
        name = f"{stamp_of(captured_at)}-{suffix}"
        payload_file = f"{self._directory}/{name}/{_payload_file_name(origin_name)}"
        manifest = QuarantineManifest(
            origin=origin_name,
            offset=start,
            length=size,
            reason=_require_text("reason", reason),
            detail=detail if isinstance(detail, str) else "",
            captured_at_wall=captured_at,
            digest=digest,
            payload_file=payload_file,
            entry_name=name,
            expected_lsn=_require_offset("expected_lsn", expected_lsn),
        )
        manifest_file = f"{self._directory}/{name}/{MANIFEST_FILE_NAME}"
        # The bytes go first and the manifest last, so an interruption leaves an entry with no
        # manifest -- which the listing skips and the next capture replaces -- rather than a
        # manifest promising bytes that are not there.
        self._write_file(payload_file, body)
        self._write_file(manifest_file, manifest.serialize())
        self._require_two_files(name, payload_file, manifest_file)
        self._publish_count()
        return QuarantineEntry(
            name=name,
            manifest=manifest,
            payload_file=payload_file,
            manifest_file=manifest_file,
        )

    # --- reading -----------------------------------------------------------------------------

    def inventory(self) -> tuple[QuarantineInventoryItem, ...]:
        """Account exactly once for every file under the quarantine, without writing.

        The namespace is listed once. A failed listing therefore makes the whole answer typed and
        explicitly inconclusive instead of manufacturing the dangerous answer "nothing is
        quarantined". Every later read uses a canonical path derived from that snapshot and the
        entry directory; ``manifest.payload_file`` is evidence to compare, never a path to follow.
        """
        prefix = f"{self._directory}/"
        try:
            observed = self._storage.list_files(prefix)
        except GrafxError as failure:
            raise GrafxQuarantineError(
                f"The quarantine inventory is inconclusive because {self._directory!r} could "
                "not be listed.",
                retryable=failure.retryable,
                field="inventory",
                operation="list_files",
                directory=self._directory,
                conclusive=False,
                inconclusive=True,
                cause=failure.code,
            ) from failure

        files = self._require_inventory_listing(observed, prefix)
        grouped: dict[str, list[str]] = {}
        unexpected: list[str] = []
        for file in files:
            remainder = file[len(prefix) :]
            name, separator, tail = remainder.partition("/")
            if (
                separator != "/"
                or not name
                or not tail
                or name in {".", ".."}
                or "\\" in name
            ):
                unexpected.append(file)
                continue
            grouped.setdefault(name, []).append(file)

        items = [
            self._inventory_item(name, tuple(grouped[name])) for name in sorted(grouped)
        ]
        items.extend(
            QuarantineInventoryItem(
                name=file[len(prefix) :],
                state="unexpected_layout",
                files=(file,),
                detail="A quarantine file is not inside one entry directory.",
            )
            for file in unexpected
        )
        items.sort(key=lambda item: (item.name, item.files))
        return self._apply_global_inventory_invariants(items, files)

    def list(self) -> tuple[QuarantineEntry, ...]:
        """Return every complete entry, oldest first by the stamp its name begins with.

        Incomplete or doubtful evidence remains visible through :meth:`inventory` but is excluded
        here for compatibility with callers that only consume structurally complete entries. No
        read door removes or repairs evidence.
        """
        entries: list[QuarantineEntry] = []
        for item in self.inventory():
            entry = item.entry
            if entry is not None:
                entries.append(entry)
        return tuple(entries)

    def count(self) -> int:
        """Return how many complete entries the quarantine holds."""
        return len(self.list())

    def inspect(self, name: str) -> QuarantineEntry:
        """Return one entry by name, refusing a name the quarantine does not hold."""
        wanted = _require_text("name", name)
        entry = self._read_entry(_require_entry_name(wanted))
        if entry is None:
            raise GrafxQuarantineError(
                f"The quarantine holds no readable entry {wanted!r}.",
                field="name",
                entry=wanted,
            )
        return entry

    def read(self, name: str) -> bytes:
        """Return the bytes one entry preserved, after proving they still match its digest."""
        entry = self.inspect(name)
        body = self._storage.read_log(
            entry.payload_file, 0, self._storage.log_size(entry.payload_file)
        )
        digest = hashlib.sha256(body).hexdigest()
        if digest != entry.manifest.digest:
            raise GrafxCorruptionDetected(
                f"The bytes of quarantine entry {entry.name!r} no longer match the digest its "
                "manifest recorded.",
                file=entry.payload_file,
                field="digest",
                expected=entry.manifest.digest,
                observed=digest,
            )
        return body

    # --- restoring ---------------------------------------------------------------------------

    def restore(self, name: str, *, target: str | None = None) -> RestoreReport:
        """Put a quarantined range back, and leave a receipt saying so (FR-10).

        Three refusals, in the order they are checked. A main data file is never a target (G6).
        An existing target is never overwritten -- moving the current file aside is the operator's
        decision and this component will not make it for them. And a range that is not the WHOLE
        origin file is refused, because writing a fragment into the middle of a file is not a
        restore, it is a patch, and the append-only storage port has no door for it.

        The quarantined copy is never removed by a restore, whatever happens afterwards.
        """
        entry = self.inspect(name)
        wanted_target = target if target is not None else entry.manifest.origin
        destination = _require_text("target", wanted_target)
        if is_protected(destination):
            raise GrafxQuarantineError(
                f"{destination!r} is a main data file, and no sanctioned operation of this engine "
                "writes over one; restore to another name and move it yourself.",
                field="target",
                file=destination,
            )
        if entry.manifest.offset != 0:
            raise GrafxQuarantineError(
                f"Quarantine entry {entry.name!r} holds bytes from offset "
                f"{entry.manifest.offset} of {entry.manifest.origin!r}, which is a fragment "
                "rather than a whole file; export it and repair the file deliberately.",
                field="offset",
                entry=entry.name,
                offset=entry.manifest.offset,
            )
        if self._storage.exists(destination):
            raise GrafxQuarantineError(
                f"{destination!r} already exists, so restoring onto it would destroy what is "
                "there; move it aside first.",
                field="target",
                file=destination,
            )
        body = self.read(entry.name)
        self._write_file(destination, body)
        receipt = RestoreReceipt(
            entry_name=entry.name,
            restored_to=destination,
            restored_bytes=len(body),
            restored_at_wall=float(self._clock.wall()),
            digest=entry.manifest.digest,
        )
        receipt_file = self._next_receipt_file(entry.name)
        self._write_file(receipt_file, receipt.serialize())
        return RestoreReport(
            entry_name=entry.name,
            restored_to=destination,
            restored_bytes=len(body),
            receipt_file=receipt_file,
            digest=entry.manifest.digest,
        )

    def receipts(self, name: str) -> tuple[str, ...]:
        """Return the restore receipts one entry carries, in the order they were written."""
        entry = self.inspect(name)
        prefix = f"{entry.directory}/{RESTORE_RECEIPT_PREFIX}"
        found = self._files_under(entry.name)
        return tuple(sorted(name for name in found if name.startswith(prefix)))

    # --- internals ---------------------------------------------------------------------------

    def _require_inventory_listing(
        self, observed: object, prefix: str
    ) -> tuple[str, ...]:
        """Return one trustworthy namespace snapshot or refuse it as inconclusive."""
        if type(observed) is not tuple:
            raise GrafxQuarantineError(
                "The quarantine inventory is inconclusive because list_files returned a value "
                "that is not its exact built-in tuple contract.",
                field="inventory",
                operation="list_files",
                directory=self._directory,
                conclusive=False,
                inconclusive=True,
                observed=type(observed).__name__,
            )
        for file in observed:
            if type(file) is not str:
                raise GrafxQuarantineError(
                    "The quarantine inventory is inconclusive because list_files returned a "
                    "name that is not an exact built-in string.",
                    field="inventory",
                    operation="list_files",
                    directory=self._directory,
                    conclusive=False,
                    inconclusive=True,
                    observed=f"{type(file).__name__}: {file!r}",
                )
            if not file.startswith(prefix) or file == prefix:
                raise GrafxQuarantineError(
                    "The quarantine inventory is inconclusive because list_files returned a "
                    "name outside the requested namespace.",
                    field="inventory",
                    operation="list_files",
                    directory=self._directory,
                    conclusive=False,
                    inconclusive=True,
                    observed=repr(file),
                )
        counts: dict[str, int] = {}
        for file in observed:
            counts[file] = counts.get(file, 0) + 1
        duplicates = tuple(sorted(file for file, count in counts.items() if count > 1))
        if duplicates:
            raise GrafxQuarantineError(
                "The quarantine inventory is inconclusive because list_files returned exact "
                "duplicate names.",
                field="inventory",
                operation="list_files",
                directory=self._directory,
                conclusive=False,
                inconclusive=True,
                duplicates=duplicates,
            )
        return tuple(sorted(observed))

    def _apply_global_inventory_invariants(
        self,
        items: list[QuarantineInventoryItem],
        files: tuple[str, ...],
    ) -> tuple[QuarantineInventoryItem, ...]:
        """Downgrade every item participating in a global identity or namespace collision."""
        problems: dict[int, set[str]] = {}

        identities: dict[tuple[str, int, int], list[int]] = {}
        for index, item in enumerate(items):
            manifest = item.manifest
            if manifest is None:
                continue
            identity = (manifest.origin, manifest.offset, manifest.length)
            identities.setdefault(identity, []).append(index)
        for identity, indexes in identities.items():
            if len(indexes) < 2:
                continue
            entries = tuple(sorted(items[index].name for index in indexes))
            digests = tuple(
                sorted(
                    {
                        items[index].manifest.digest
                        for index in indexes
                        if items[index].manifest is not None
                    }
                )
            )
            agreement = "different digests" if len(digests) > 1 else "the same digest"
            detail = (
                f"Duplicate quarantine identity {identity!r} appears in entries {entries!r} "
                f"with {agreement} {digests!r}."
            )
            for index in indexes:
                problems.setdefault(index, set()).add(detail)

        owner_of = {
            file: index for index, item in enumerate(items) for file in item.files
        }
        nodes: dict[str, dict[tuple[str, str], set[str]]] = {}
        for file in files:
            segments = file.split("/")
            for depth in range(1, len(segments)):
                node = "/".join(segments[:depth])
                nodes.setdefault(node.casefold(), {}).setdefault(
                    (node, "directory"), set()
                ).add(file)
            nodes.setdefault(file.casefold(), {}).setdefault((file, "file"), set()).add(
                file
            )
        for shapes in nodes.values():
            if len(shapes) < 2:
                continue
            labels = tuple(
                f"{role}:{node}"
                for node, role in sorted(shapes, key=lambda shape: shape)
            )
            detail = f"A case-insensitive namespace collision exists among {labels!r}."
            affected = {file for owned in shapes.values() for file in owned}
            for file in affected:
                problems.setdefault(owner_of[file], set()).add(detail)

        classified: list[QuarantineInventoryItem] = []
        for index, item in enumerate(items):
            found = problems.get(index)
            if not found:
                classified.append(item)
                continue
            details: list[str] = []
            if item.state != "complete" and item.detail:
                details.append(f"Initial {item.state}: {item.detail}")
            details.extend(sorted(found))
            classified.append(
                replace(
                    item,
                    state="unexpected_layout",
                    detail=" ".join(details),
                )
            )
        return tuple(classified)

    def _inventory_item(
        self, name: str, files: tuple[str, ...]
    ) -> QuarantineInventoryItem:
        """Classify one entry directory using only paths from the namespace snapshot."""
        directory = f"{self._directory}/{name}"
        manifest_file = f"{directory}/{MANIFEST_FILE_NAME}"
        if manifest_file not in files:
            return QuarantineInventoryItem(
                name=name,
                state="incomplete",
                files=files,
                manifest_file=manifest_file,
                detail="The entry directory contains evidence but no canonical manifest.",
            )

        try:
            raw = self._storage.read_log(
                manifest_file, 0, self._storage.log_size(manifest_file)
            )
        except GrafxCorruptionDetected as failure:
            return QuarantineInventoryItem(
                name=name,
                state="corrupt_manifest",
                files=files,
                manifest_file=manifest_file,
                detail=f"{failure.code}: {failure.message}",
            )
        except GrafxError as failure:
            return QuarantineInventoryItem(
                name=name,
                state="unreadable_manifest",
                files=files,
                manifest_file=manifest_file,
                detail=f"{failure.code}: {failure.message}",
            )

        try:
            manifest = QuarantineManifest.parse(raw)
        except GrafxError as failure:
            return QuarantineInventoryItem(
                name=name,
                state="corrupt_manifest",
                files=files,
                manifest_file=manifest_file,
                detail=f"{failure.code}: {failure.message}",
            )

        payload_file = f"{directory}/{_payload_file_name(manifest.origin)}"
        try:
            expected_name = f"{stamp_of(manifest.captured_at_wall)}-{manifest.suffix}"
        except (GrafxError, OverflowError, ValueError) as failure:
            return QuarantineInventoryItem(
                name=name,
                state="corrupt_manifest",
                files=files,
                manifest_file=manifest_file,
                payload_file=payload_file,
                manifest=manifest,
                detail=f"The manifest cannot derive an entry identity: {failure!s}",
            )
        mismatch: list[str] = []
        if name != expected_name:
            mismatch.append("name")
        if manifest.entry_name != name:
            mismatch.append("entry_name")
        if manifest.payload_file != payload_file:
            mismatch.append("payload_file")
        if mismatch:
            return QuarantineInventoryItem(
                name=name,
                state="manifest_mismatch",
                files=files,
                manifest_file=manifest_file,
                payload_file=payload_file,
                manifest=manifest,
                detail="The manifest disagrees with its directory in: "
                + ", ".join(mismatch),
            )
        if payload_file not in files:
            return QuarantineInventoryItem(
                name=name,
                state="missing_payload",
                files=files,
                manifest_file=manifest_file,
                payload_file=payload_file,
                manifest=manifest,
                detail="The canonical payload named by this entry is absent.",
            )

        expected = {manifest_file, payload_file}
        extra = tuple(
            file
            for file in files
            if file not in expected and not _is_restore_receipt(file, directory)
        )
        if extra:
            return QuarantineInventoryItem(
                name=name,
                state="unexpected_layout",
                files=files,
                manifest_file=manifest_file,
                payload_file=payload_file,
                manifest=manifest,
                detail=f"Unexpected files share the entry directory: {extra!r}.",
            )
        return QuarantineInventoryItem(
            name=name,
            state="complete",
            files=files,
            manifest_file=manifest_file,
            payload_file=payload_file,
            manifest=manifest,
        )

    def _entry_names(self) -> tuple[str, ...]:
        """Return the name of every directory directly under the quarantine."""
        prefix = f"{self._directory}/"
        names: set[str] = set()
        for found in self._storage.list_files(prefix):
            remainder = found[len(prefix) :]
            head, _, tail = remainder.partition("/")
            if head and tail:
                names.add(head)
        return tuple(sorted(names))

    def _files_under(self, name: str) -> tuple[str, ...]:
        """Return every file of one entry, by its full device name."""
        prefix = f"{self._directory}/{name}/"
        return tuple(sorted(self._storage.list_files(prefix)))

    def _read_entry(self, name: str) -> QuarantineEntry | None:
        """Return one entry, or None when its manifest is missing or unreadable."""
        manifest_file = f"{self._directory}/{name}/{MANIFEST_FILE_NAME}"
        if not self._storage.exists(manifest_file):
            return None
        try:
            manifest = QuarantineManifest.parse(
                self._storage.read_log(
                    manifest_file, 0, self._storage.log_size(manifest_file)
                )
            )
        except GrafxError:
            return None
        return QuarantineEntry(
            name=name,
            manifest=manifest,
            payload_file=manifest.payload_file,
            manifest_file=manifest_file,
        )

    def _find_by_suffix(self, suffix: str) -> QuarantineEntry | None:
        """Return the entry that already holds this exact range, or None when there is none.

        Two questions, and the second is not a formality. ``endswith`` is a cheap filter over the
        NAMES, and a name is ``<stamp>-<origin>-<offset>-<length>``: the suffix of one range is a
        genuine string suffix of another whenever one origin ends with the other, which
        ``sanitize_name`` makes ordinary (``wal/1.wal`` becomes ``wal_1.wal``, and
        ``1.wal-0-16`` is a suffix of ``wal_1.wal-0-16``). So the MANIFEST is asked what range it
        actually describes, and an entry that only looked like a match is passed over. Without
        it, a capture would be handed an entry holding a different file's bytes -- returned as
        already preserved, or refused for a digest that was never meant to match.

        Proven by ``test_an_entry_whose_name_merely_ends_with_the_suffix_is_not_that_range``.
        """
        for name in self._entry_names():
            if not name.endswith(suffix):
                continue
            entry = self._read_entry(name)
            if entry is not None and entry.manifest.suffix == suffix:
                return entry
        return None

    def _next_receipt_file(self, name: str) -> str:
        """Return the name of the next restore receipt, so no receipt replaces another."""
        prefix = f"{self._directory}/{name}/{RESTORE_RECEIPT_PREFIX}"
        taken = sum(1 for found in self._files_under(name) if found.startswith(prefix))
        return f"{prefix}{taken + 1}.json"

    def _require_two_files(
        self, name: str, payload_file: str, manifest_file: str
    ) -> None:
        """Prove against the DEVICE that the capture wrote both files, not one twice.

        LESSONS L5, and this component has now supplied the third instance in this build: an
        operation that reports success while the namespace says otherwise. Every content check
        available here passes when the payload is written to the manifest's own name -- capture
        returns a valid entry, the listing shows it, the digest matches the bytes that are there
        -- because all of them read back through the entry the operation just built. The only
        witness that can see it is the device's own listing, so that is what is asked.

        ``test_a_capture_whose_origin_is_named_like_the_manifest_keeps_both_files`` does NOT
        prove this guard: ``_payload_file_name`` prefixes the reserved names, so that capture
        never produces a collision for this check to catch, and the two mechanisms were alibiing
        each other (A62/A67a) -- replacing the device listing here with the operation's own answer
        left the suite green. The test that only this guard can satisfy is
        ``test_a_device_that_reports_a_write_it_did_not_perform_fails_the_capture``, where the
        names are distinct and correct and the DEVICE is the thing that lied.
        """
        found = set(self._files_under(name))
        missing = [item for item in (payload_file, manifest_file) if item not in found]
        if payload_file == manifest_file or missing:
            # Leave no half-entry behind. A manifest with no bytes beside it is exactly the
            # thing a listing must never show: an operator would find it, trust it, and be
            # unable to read it. Removing OUR OWN manifest is safe -- it was written a moment
            # ago by this call, and whatever bytes exist stay where they are, unlisted, for the
            # next capture of the same range to complete.
            if manifest_file in found:
                try:
                    self._storage.remove(manifest_file)
                except GrafxError:  # pragma: no cover - reported below either way
                    pass
            raise GrafxQuarantineError(
                f"Quarantine entry {name!r} was written and the device holds "
                f"{len(found)} file(s) under it instead of the manifest and the bytes it "
                "describes; the capture is refused rather than reported as preserved.",
                field="payload_file",
                entry=name,
                observed=tuple(sorted(found)),
            )

    def _read_range(self, origin: str, offset: int, length: int) -> bytes:
        """Read the bytes a capture is about, refusing an origin that cannot supply them."""
        if not self._storage.exists(origin):
            raise GrafxQuarantineError(
                f"There is nothing to quarantine: {origin!r} does not exist.",
                field="origin",
                file=origin,
            )
        available = self._storage.log_size(origin)
        if offset > available:
            raise GrafxQuarantineError(
                f"{origin!r} holds {available} bytes, so there is nothing at byte {offset} to "
                "quarantine.",
                field="offset",
                file=origin,
                offset=offset,
            )
        return self._storage.read_log(origin, offset, min(length, available - offset))

    def _write_file(self, name: str, body: bytes) -> str:
        """Create a file, write it whole, and make it durable, riding out a retryable refusal.

        The retry predicate reads ``details["retryable"]`` and never the exception class (A47):
        an indexer holding a fresh directory open for a moment must not cost the evidence.
        """
        attempt = 1
        while True:
            try:
                if not self._storage.exists(name):
                    self._storage.create(name, exclusive=False)
                if self._storage.log_size(name):
                    self._storage.truncate_log(name, 0)
                if body:
                    self._storage.append_log(name, body)
                self._storage.durable_barrier(name)
                return name
            except GrafxError as failure:
                if attempt >= COPY_ATTEMPTS or not is_retryable(failure):
                    raise
                attempt += 1

    def _publish_count(self) -> None:
        """Publish the quarantine gauge of CONTRACT.md section 9."""
        if not self._metrics.enabled:
            return
        self._metrics.set_gauge(QUARANTINE_ENTRIES, float(self.count()))

    def __repr__(self) -> str:
        """Return a short description naming the directory this quarantine keeps."""
        return f"QuarantineStore(directory={self._directory!r})"


def _is_restore_receipt(file: str, directory: str) -> bool:
    """Return whether one direct child has the exact numbered receipt shape capture writes."""
    prefix = f"{directory}/{RESTORE_RECEIPT_PREFIX}"
    if not file.startswith(prefix) or not file.endswith(".json"):
        return False
    sequence = file[len(prefix) : -len(".json")]
    return (
        sequence.isascii()
        and sequence.isdigit()
        and int(sequence) >= 1
        and sequence == str(int(sequence))
    )


def _basename(name: str) -> str:
    """Return the last path element of a device name."""
    return name.rsplit("/", 1)[-1] or name


def _payload_file_name(origin: str) -> str:
    """Return the name the copied bytes take inside an entry, never a name the entry reserves.

    An entry holds a manifest and the bytes it describes, and the manifest's name is fixed. A
    file whose own basename sanitizes to that name -- or to a restore receipt's -- would be
    written first and then written OVER by the manifest, leaving one file where there must be
    two: the evidence destroyed by the act that exists to preserve it, reported as success. The
    reserved names are prefixed instead, which cannot collide in turn because the prefix is not
    itself reserved.
    """
    name = sanitize_name(_basename(origin))
    # Case-insensitively, because DoD item 9 requires file names to be case-insensitive-safe and
    # Windows is a first-class platform (G4). ``MANIFEST.JSON`` and ``manifest.json`` are two
    # names on POSIX and ONE file on NTFS, so a case-sensitive comparison here would let the
    # manifest overwrite the evidence on the platform this engine calls primary -- and pass on
    # the one the suite happens to run on.
    folded = name.casefold()
    reserved = folded == MANIFEST_FILE_NAME.casefold() or folded.startswith(
        RESTORE_RECEIPT_PREFIX.casefold()
    )
    return f"payload-{name}" if reserved else name


def _validate_directory(directory: object) -> str:
    """Return the quarantine directory, refusing one that could escape the database."""
    if not isinstance(directory, str) or not directory:
        raise GrafxConfigurationError(
            "The quarantine needs a non-empty directory name.",
            field="directory",
            value=repr(directory),
        )
    cleaned = directory.rstrip("/")
    if "\\" in cleaned or ".." in cleaned.split("/") or not cleaned:
        raise GrafxConfigurationError(
            f"The quarantine directory {directory!r} must be a forward-slash name inside the "
            "database.",
            field="directory",
            value=directory,
        )
    return cleaned


def _require_text(field: str, value: object) -> str:
    """Return a non-empty string argument, refusing anything else."""
    if not isinstance(value, str) or not value:
        raise GrafxConfigurationError(
            f"The {field} of a quarantine operation must be a non-empty string; got {value!r}.",
            field=field,
            value=repr(value),
        )
    return value


def _require_body(payload: object) -> bytes:
    """Return the bytes a caller supplied, refusing anything that is not a byte buffer.

    ``bytes(17)`` is seventeen zero bytes, so an integer reaching this door would be QUARANTINED
    as the preserved evidence, under a manifest whose digest matches it perfectly. A component
    whose whole purpose is keeping evidence must not be able to invent any.
    """
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise GrafxConfigurationError(
            f"The bytes handed to a quarantine capture must be a byte buffer; got "
            f"{type(payload).__name__}.",
            field="payload",
            value=type(payload).__name__,
        )
    return bytes(payload)


def _require_offset(field: str, value: object) -> int:
    """Return a non-negative integer argument, refusing anything else."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GrafxConfigurationError(
            f"The {field} of a quarantine operation must be a non-negative integer; "
            f"got {value!r}.",
            field=field,
            value=repr(value),
        )
    return value


def _require_entry_name(name: str) -> str:
    """Return an entry name that names one directory of the quarantine and nothing else."""
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise GrafxConfigurationError(
            f"A quarantine entry name is one directory name; got {name!r}.",
            field="name",
            value=name,
        )
    return name


def entry_names_of(entries: Iterable[QuarantineEntry]) -> tuple[str, ...]:
    """Return the names of a run of entries, which is what a report and a test both want."""
    return tuple(entry.name for entry in entries)
