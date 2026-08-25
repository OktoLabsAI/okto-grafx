"""Read-only, complete inventory of quarantine evidence (M3-Q1)."""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError, replace

import pytest

from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.adapters.storage_read_only import ReadOnlyStorageDevice
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxQuarantineError,
    GrafxStorageError,
)
from okto_grafx.domain.recovery.manifest import stamp_of
from okto_grafx.engine.quarantine import (
    QuarantineEntry,
    QuarantineInventoryItem,
    QuarantineStore,
)

from .conftest import FrozenClock, RecordingMetricsSink, Stack

INVENTORY_ORIGIN = "wal/quarantine-inventory.wal"
INVENTORY_BODY = bytes(range(64))


def _device(stack: Stack) -> MemoryStorageDevice:
    """Return the memory device used by this focused recovery fixture."""
    assert isinstance(stack.storage, MemoryStorageDevice)
    return stack.storage


def _capture(stack: Stack, offset: int) -> QuarantineEntry:
    """Capture one distinct byte so a test can assemble several independent entries."""
    device = _device(stack)
    if not device.exists(INVENTORY_ORIGIN):
        device.create(INVENTORY_ORIGIN, exclusive=False)
        device.append_log(INVENTORY_ORIGIN, INVENTORY_BODY)
    stack.clock.advance(1.0)
    return stack.quarantine.capture(
        origin=INVENTORY_ORIGIN,
        offset=offset,
        length=1,
        reason="inventory_test",
    )


def _rewrite(device: MemoryStorageDevice, file: str, body: bytes) -> None:
    """Replace one test fixture's log bytes through the ordinary append-only doors."""
    device.truncate_log(file, 0)
    if body:
        device.append_log(file, body)


def _put(device: MemoryStorageDevice, file: str, body: bytes) -> None:
    """Create one adversarial fixture file with exact bytes."""
    device.create(file, exclusive=False)
    if body:
        device.append_log(file, body)


def _tree(device: MemoryStorageDevice) -> tuple[tuple[str, bytes], ...]:
    """Snapshot every device file and its bytes without following a manifest."""
    return tuple(
        (file, device.read_log(file, 0, device.log_size(file)))
        for file in device.list_files()
    )


def _represented(inventory: tuple[QuarantineInventoryItem, ...]) -> tuple[str, ...]:
    """Flatten inventory file ownership without knowing any DTO implementation detail."""
    return tuple(file for item in inventory for file in item.files)


def _duplicate_identity(
    stack: Stack, entry: QuarantineEntry, *, body: bytes
) -> tuple[str, str]:
    """Plant a second structurally valid entry for the same origin/range identity."""
    device = _device(stack)
    captured_at = entry.manifest.captured_at_wall + 5.0
    name = f"{stamp_of(captured_at)}-{entry.manifest.suffix}"
    payload_file = f"quarantine/{name}/{entry.payload_file.rsplit('/', 1)[-1]}"
    manifest_file = f"quarantine/{name}/manifest.json"
    manifest = replace(
        entry.manifest,
        captured_at_wall=captured_at,
        digest=hashlib.sha256(body).hexdigest(),
        payload_file=payload_file,
        entry_name=name,
    )
    _put(device, payload_file, body)
    _put(device, manifest_file, manifest.serialize())
    return name, manifest.digest


class _CaseSensitiveMemoryDevice(MemoryStorageDevice):
    """Permit a POSIX-shaped namespace so casefold collisions can be inspected on Windows."""

    def _resolve_identity(self, name: str) -> bool:
        """Resolve exact spellings only; the inventory supplies the portability backstop."""
        return name in self._files  # noqa: SLF001 - deliberate adversarial storage fixture


def test_inventory_exposes_a_frozen_complete_item_and_preserves_the_legacy_view(
    stack: Stack,
) -> None:
    entry = _capture(stack, 1)

    inventory = stack.quarantine.inventory()

    assert len(inventory) == 1
    item = inventory[0]
    assert item.name == entry.name
    assert item.state == "complete"
    assert item.files == tuple(sorted((entry.manifest_file, entry.payload_file)))
    assert item.entry == entry
    with pytest.raises(FrozenInstanceError):
        setattr(item, "state", "incomplete")
    assert stack.quarantine.list() == (entry,)
    assert stack.quarantine.count() == 1


@pytest.mark.parametrize(
    ("damage", "expected"),
    (
        ("missing", "incomplete"),
        ("partial", "corrupt_manifest"),
        ("invalid", "corrupt_manifest"),
    ),
)
def test_inventory_keeps_missing_partial_and_invalid_manifests_visible(
    stack: Stack, damage: str, expected: str
) -> None:
    entry = _capture(stack, 2)
    device = _device(stack)
    if damage == "missing":
        device.remove(entry.manifest_file)
    elif damage == "partial":
        raw = entry.manifest.serialize()
        _rewrite(device, entry.manifest_file, raw[: len(raw) // 2])
    else:
        _rewrite(device, entry.manifest_file, b"not a quarantine manifest")

    item = stack.quarantine.inventory()[0]

    assert item.name == entry.name
    assert item.state == expected
    assert set(item.files) == set(device.list_files(f"{entry.directory}/"))
    assert stack.quarantine.list() == ()
    assert stack.quarantine.count() == 0


@pytest.mark.parametrize(
    ("operation", "expected", "failure_type"),
    (
        ("log_size", "corrupt_manifest", GrafxCorruptionDetected),
        ("read_log", "corrupt_manifest", GrafxCorruptionDetected),
        ("log_size", "unreadable_manifest", GrafxStorageError),
        ("read_log", "unreadable_manifest", GrafxStorageError),
    ),
)
def test_inventory_distinguishes_corrupt_manifest_storage_from_unreadable_access(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    expected: str,
    failure_type: type[GrafxCorruptionDetected] | type[GrafxStorageError],
) -> None:
    entry = _capture(stack, 3)
    device = _device(stack)
    original_read = device.read_log
    original_size = device.log_size

    def inaccessible(file: str, offset: int, length: int) -> bytes:
        if file == entry.manifest_file:
            raise failure_type(
                "The quarantine manifest cannot be read through this storage call.",
                operation="read_log",
                file=file,
            )
        return original_read(file, offset, length)

    def inaccessible_size(file: str) -> int:
        if file == entry.manifest_file:
            raise failure_type(
                "The quarantine manifest cannot be sized through this storage call.",
                operation="log_size",
                file=file,
            )
        return original_size(file)

    monkeypatch.setattr(
        device,
        operation,
        inaccessible_size if operation == "log_size" else inaccessible,
    )

    item = stack.quarantine.inventory()[0]

    assert item.name == entry.name
    assert item.state == expected
    assert item.manifest is None
    assert stack.quarantine.list() == ()


def test_inventory_reports_a_manifest_whose_entry_identity_does_not_match_its_directory(
    stack: Stack,
) -> None:
    entry = _capture(stack, 4)
    device = _device(stack)
    mismatched = replace(entry.manifest, entry_name="another-entry")
    _rewrite(device, entry.manifest_file, mismatched.serialize())

    item = stack.quarantine.inventory()[0]

    assert item.state == "manifest_mismatch"
    assert item.manifest == mismatched
    assert item.entry is None
    assert stack.quarantine.list() == ()


def test_inventory_rejects_a_self_consistent_but_arbitrarily_named_entry(
    stack: Stack,
) -> None:
    """Directory and manifest agreeing is insufficient when neither matches capture identity."""
    entry = _capture(stack, 14)
    device = _device(stack)
    body = device.read_log(entry.payload_file, 0, device.log_size(entry.payload_file))
    arbitrary = "arbitrary-entry-name"
    payload_file = f"quarantine/{arbitrary}/{entry.payload_file.rsplit('/', 1)[-1]}"
    manifest_file = f"quarantine/{arbitrary}/manifest.json"
    manifest = replace(
        entry.manifest,
        entry_name=arbitrary,
        payload_file=payload_file,
    )
    _put(device, payload_file, body)
    _put(device, manifest_file, manifest.serialize())
    device.remove(entry.payload_file)
    device.remove(entry.manifest_file)

    inventory = stack.quarantine.inventory()

    assert len(inventory) == 1
    assert inventory[0].name == arbitrary
    assert inventory[0].state == "manifest_mismatch"
    assert tuple(sorted(_represented(inventory))) == device.list_files("quarantine/")
    assert stack.quarantine.list() == ()
    assert stack.quarantine.count() == 0


@pytest.mark.parametrize(
    "different_digest", (False, True), ids=("same-digest", "different-digest")
)
def test_inventory_downgrades_every_duplicate_origin_range_identity(
    stack: Stack, different_digest: bool
) -> None:
    entry = _capture(stack, 15)
    device = _device(stack)
    original = device.read_log(
        entry.payload_file, 0, device.log_size(entry.payload_file)
    )
    duplicate_body = b"x" if different_digest else original
    duplicate_name, duplicate_digest = _duplicate_identity(
        stack, entry, body=duplicate_body
    )
    listed = device.list_files("quarantine/")

    first = stack.quarantine.inventory()
    second = stack.quarantine.inventory()

    assert {item.name for item in first} == {entry.name, duplicate_name}
    assert {item.state for item in first} == {"unexpected_layout"}
    assert tuple(sorted(_represented(first))) == listed
    assert len(_represented(first)) == len(set(_represented(first)))
    assert [item.detail for item in first] == [item.detail for item in second]
    assert all(entry.manifest.origin in item.detail for item in first)
    if different_digest:
        assert duplicate_digest != entry.manifest.digest
        assert all("different digests" in item.detail for item in first)
    assert stack.quarantine.list() == ()
    assert stack.quarantine.count() == 0


def test_inventory_never_follows_a_payload_path_that_escapes_from_a_manifest(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _capture(stack, 5)
    device = _device(stack)
    escaped = replace(entry.manifest, payload_file="../../heap.dat")
    _rewrite(device, entry.manifest_file, escaped.serialize())
    original = device.read_log
    reads: list[str] = []

    def recording_read(file: str, offset: int, length: int) -> bytes:
        reads.append(file)
        return original(file, offset, length)

    monkeypatch.setattr(device, "read_log", recording_read)

    item = stack.quarantine.inventory()[0]

    assert item.state == "manifest_mismatch"
    assert item.payload_file == entry.payload_file
    assert reads == [entry.manifest_file]
    assert all(file.startswith(f"{entry.directory}/") for file in reads)


def test_inventory_reports_a_valid_manifest_whose_canonical_payload_is_missing(
    stack: Stack,
) -> None:
    entry = _capture(stack, 6)
    device = _device(stack)
    device.remove(entry.payload_file)

    item = stack.quarantine.inventory()[0]

    assert item.state == "missing_payload"
    assert item.payload_file == entry.payload_file
    assert item.files == (entry.manifest_file,)
    assert stack.quarantine.list() == ()


def test_inventory_reports_top_level_and_nested_files_as_unexpected_layout(
    stack: Stack,
) -> None:
    entry = _capture(stack, 7)
    device = _device(stack)
    top_level = "quarantine/orphan.bin"
    nested = f"{entry.directory}/nested/note.bin"
    for file in (top_level, nested):
        device.create(file, exclusive=False)
        device.append_log(file, b"unclaimed evidence")

    inventory = stack.quarantine.inventory()

    assert any(
        item.state == "unexpected_layout" and item.files == (top_level,)
        for item in inventory
    )
    entry_item = next(item for item in inventory if item.name == entry.name)
    assert entry_item.state == "unexpected_layout"
    assert nested in entry_item.files
    assert stack.quarantine.list() == ()


def test_casefold_file_directory_collision_downgrades_every_involved_item() -> None:
    device = _CaseSensitiveMemoryDevice(page_size=512)
    store = QuarantineStore(device, FrozenClock(), RecordingMetricsSink())
    try:
        _put(device, INVENTORY_ORIGIN, INVENTORY_BODY)
        entry = store.capture(
            origin=INVENTORY_ORIGIN,
            offset=16,
            length=1,
            reason="casefold_test",
        )
        top_level = f"quarantine/{entry.name.swapcase()}"
        _put(
            device, top_level, b"a file where a directory exists on insensitive storage"
        )
        listed = device.list_files("quarantine/")

        inventory = store.inventory()

        assert len(inventory) == 2
        assert {item.state for item in inventory} == {"unexpected_layout"}
        assert tuple(sorted(_represented(inventory))) == listed
        assert len(_represented(inventory)) == len(set(_represented(inventory)))
        assert all("case-insensitive" in item.detail for item in inventory)
        assert store.list() == ()
        assert store.count() == 0
    finally:
        device.close()


def test_inventory_accounts_for_every_listed_file_exactly_once(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = _capture(stack, 8)
    incomplete = _capture(stack, 9)
    corrupt = _capture(stack, 10)
    device = _device(stack)
    device.remove(incomplete.manifest_file)
    _rewrite(device, corrupt.manifest_file, b"invalid")
    top_level = "quarantine/unexpected.txt"
    device.create(top_level, exclusive=False)
    device.append_log(top_level, b"unexpected")
    listed = device.list_files("quarantine/")
    original = device.list_files
    listings: list[str] = []

    def recording_listing(prefix: str = "") -> tuple[str, ...]:
        listings.append(prefix)
        return original(prefix)

    monkeypatch.setattr(device, "list_files", recording_listing)

    inventory = stack.quarantine.inventory()
    represented = tuple(file for item in inventory for file in item.files)

    assert listings == ["quarantine/"]
    assert tuple(sorted(represented)) == listed
    assert len(represented) == len(set(represented))
    assert (
        next(item for item in inventory if item.name == complete.name).state
        == "complete"
    )
    assert (
        next(item for item in inventory if item.name == incomplete.name).state
        == "incomplete"
    )
    assert (
        next(item for item in inventory if item.name == corrupt.name).state
        == "corrupt_manifest"
    )
    listings.clear()
    assert stack.quarantine.list() == (complete,)
    assert stack.quarantine.count() == 1


def test_inventory_and_legacy_listing_are_byte_identical_through_a_read_only_device(
    stack: Stack,
) -> None:
    first = _capture(stack, 11)
    second = _capture(stack, 12)
    device = _device(stack)
    before = _tree(device)
    read_only = QuarantineStore(
        ReadOnlyStorageDevice(device),
        stack.clock,
        RecordingMetricsSink(),
    )

    assert [item.state for item in read_only.inventory()] == ["complete", "complete"]
    assert read_only.list() == (first, second)
    assert read_only.count() == 2
    assert _tree(device) == before


def test_inventory_refuses_a_tuple_subclass_from_the_listing_port(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture(stack, 17)
    device = _device(stack)
    original = device.list_files

    class Listing(tuple):
        """A tuple lookalike that violates the port's exact built-in contract."""

    def subclassed(prefix: str = "") -> tuple[str, ...]:
        return Listing(original(prefix))

    monkeypatch.setattr(device, "list_files", subclassed)

    with pytest.raises(GrafxQuarantineError) as caught:
        stack.quarantine.inventory()

    assert caught.value.details["conclusive"] is False
    assert caught.value.details["observed"] == "Listing"


def test_inventory_refuses_a_string_subclass_inside_the_listing(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture(stack, 18)
    device = _device(stack)
    original = device.list_files

    class FileName(str):
        """A string lookalike that must not cross the exact scalar boundary."""

    def subclassed(prefix: str = "") -> tuple[str, ...]:
        return tuple(FileName(file) for file in original(prefix))

    monkeypatch.setattr(device, "list_files", subclassed)

    with pytest.raises(GrafxQuarantineError) as caught:
        stack.quarantine.inventory()

    assert caught.value.details["conclusive"] is False
    assert "FileName" in str(caught.value.details["observed"])


def test_inventory_refuses_an_exact_duplicate_listing_instead_of_silently_deduplicating(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture(stack, 19)
    device = _device(stack)
    original = device.list_files

    def duplicated(prefix: str = "") -> tuple[str, ...]:
        listed = original(prefix)
        return (*listed, listed[0])

    monkeypatch.setattr(device, "list_files", duplicated)

    with pytest.raises(GrafxQuarantineError) as caught:
        stack.quarantine.inventory()

    assert caught.value.details["conclusive"] is False
    assert caught.value.details["duplicates"]


def test_an_inventory_whose_namespace_listing_fails_is_typed_and_inconclusive(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture(stack, 13)
    device = _device(stack)
    before = _tree(device)
    failure = GrafxStorageError(
        "The quarantine directory could not be listed.",
        operation="list_files",
    )

    def unavailable(_prefix: str = "") -> tuple[str, ...]:
        raise failure

    monkeypatch.setattr(device, "list_files", unavailable)

    with pytest.raises(GrafxQuarantineError) as caught:
        stack.quarantine.inventory()

    assert caught.value.__cause__ is failure
    assert caught.value.retryable is True
    assert caught.value.details["field"] == "inventory"
    assert caught.value.details["conclusive"] is False
    assert caught.value.details["inconclusive"] is True
    monkeypatch.undo()
    assert _tree(device) == before
